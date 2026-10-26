"""Proactive engagement engine (pillar 2) and insurance clock watcher (pillar 3).

Runs on a schedule (e.g. every 15 minutes). Deciding who gets which message is deterministic code, not
the model. Message text comes from fixed, clinician- or compliance-approved templates, rendered in the
patient's language by Sarvam. When a patient replies, a normal agent Session picks up the thread with
the outbound message as context.

The three motions are kept apart on purpose (brief section 3.2):
  care reminders  - booked appointments, care-plan items         purpose = care_communication
  follow-ups      - post-visit check-ins with red-flag questions  purpose = care_communication
  re-engagement   - recall / health-check promotions              purpose = marketing_recall (separate opt-in,
                                                                   DND on SMS/voice, suppression list, STOP)
"""

from __future__ import annotations

from datetime import date, timedelta

from .sarvam_layer import SarvamLanguageLayer
from .store import CARE, MARKETING, Store

SENDER_ID = "SUNRSH"  # registered DLT header; promotional SMS must identify the sender


def _fmt(dt) -> str:
    return dt.strftime("%a %d %b, %I:%M %p")


class CampaignEngine:
    def __init__(self, store: Store, language: SarvamLanguageLayer | None = None):
        self.store = store
        self.language = language

    def run(self) -> list[dict]:
        before = len(self.store.outbox)
        self.appointment_reminders()
        self.post_visit_followups()
        self.care_plan_reminders()
        self.re_engagement()
        self.insurance_clock_watch()
        return self.store.outbox[before:]

    # ---- helpers ----------------------------------------------------------------------------------

    def _send_once(self, key: str, patient_id: str, english: str, purpose: str, template: str) -> dict | None:
        s = self.store
        if key in s.campaign_sent:
            return None
        if not s.has_consent(patient_id, purpose):
            s.log("campaign_suppressed", patient=patient_id, template=template, why="no consent for " + purpose)
            return None
        s.campaign_sent.add(key)
        lang = s.patients[patient_id].language
        text = self.language.localise(english, lang) if self.language else english
        msg = s.send(patient_id, text, purpose, template)
        msg["english"] = english
        return msg

    # ---- care: appointment reminders at T-24h and T-2h ----------------------------------------------

    def appointment_reminders(self) -> None:
        s = self.store
        for ap in s.appointments.values():
            if ap.status not in ("booked", "confirmed"):
                continue
            hours = (ap.start - s.now).total_seconds() / 3600
            prov = s.providers[ap.provider_id]
            for label, lo, hi in (("T-24h", 2, 26), ("T-2h", 0, 2.5)):
                if not lo < hours <= hi or label in ap.reminders_sent:
                    continue
                if label == "T-24h":
                    prep = next((c["text"] for k, c in s.content.items()
                                 if k.startswith("prep:") and k.split(":", 1)[1] in ap.reason.lower()), None)
                    text = (f"Reminder: your appointment with {prov.name} is on {_fmt(ap.start)} at {prov.location}. "
                            + (f"Please note: {prep} " if prep else "")
                            + "Reply 1 to confirm, 2 to reschedule, 3 to cancel.")
                else:
                    text = f"See you soon: {prov.name} at {_fmt(ap.start)}, {prov.location}. Please arrive 15 minutes early."
                if self._send_once(f"rem:{ap.id}:{label}:{ap.start}", ap.patient_id, text, CARE, f"reminder_{label}"):
                    ap.reminders_sent.append(label)

    # ---- care: post-visit follow-up one to two days after the visit ---------------------------------

    def post_visit_followups(self) -> None:
        s = self.store
        for ap in s.appointments.values():
            if ap.status != "completed" or ap.followup_done:
                continue
            days = (s.now.date() - ap.start.date()).days
            if not 1 <= days <= 2:
                continue
            spec = s.providers[ap.provider_id].specialty
            q_key = "followup:pediatric fever" if spec == "pediatrics" else "followup:post-procedure"
            questions = s.content[q_key]["questions"]
            p = s.patients[ap.patient_id]
            who = f"{p.name}'s" if p.guardian_name else "your"
            text = (f"Hello from Sunrise Hospital. We're checking on {who} recovery after the visit with "
                    f"{s.providers[ap.provider_id].name}. " + " ".join(f"({i}) {q}" for i, q in enumerate(questions, 1))
                    + " If there is an emergency, call 108.")
            self._send_once(f"fu:{ap.id}", ap.patient_id, text, CARE, "post_visit_followup")

    # ---- care: lab-due and refill nudges from the care plan -----------------------------------------

    def care_plan_reminders(self) -> None:
        s = self.store
        for p in s.patients.values():
            for item in p.care_plan:
                due = date.fromisoformat(item["due"])
                if not 0 <= (due - s.now.date()).days <= 3:
                    continue
                if item["kind"] == "lab_due":
                    text = (f"Your {item['what']} test is due on {due:%d %b}, as planned by your doctor. "
                            "Reply BOOK and we'll find you a lab slot.")
                elif item["kind"] == "medication_refill":
                    text = (f"Your {item['what']} supply is due for refill around {due:%d %b}. "
                            "Please don't stop it without speaking to your doctor. Reply REFILL for pharmacy help.")
                else:
                    continue
                self._send_once(f"cp:{p.id}:{item['kind']}:{item['what']}:{item['due']}", p.id, text, CARE,
                                f"care_{item['kind']}")

    # ---- marketing: recall for lapsed chronic patients ---------------------------------------------

    def re_engagement(self, lapsed_months: int = 6) -> None:
        s = self.store
        cutoff = s.now.date() - timedelta(days=30 * lapsed_months)
        service = s.content.get("service:health check")
        if not service:
            return
        for p in s.patients.values():
            if not p.conditions or not p.last_visit or p.last_visit > cutoff:
                continue
            if p.phone in s.suppression:
                s.log("campaign_suppressed", patient=p.id, why="suppression list")
                continue
            if p.dnd and p.preferred_channel in ("sms", "voice"):
                s.log("campaign_suppressed", patient=p.id, why="DND on " + p.preferred_channel)
                continue
            text = (f"[{SENDER_ID} - promotional] It's been a while since your last review. {service['text']} "
                    "Reply YES to book. Reply STOP to stop offers - this won't affect messages about your care.")
            self._send_once(f"mk:{p.id}:{s.now:%Y-%m}", p.id, text, MARKETING, "recall_diabetes_review")

    # ---- insurance: IRDAI clocks and status pushes --------------------------------------------------

    def insurance_clock_watch(self) -> None:
        s = self.store
        for pa in s.preauths.values():
            if not pa.requested_at or pa.decided_at or pa.status not in ("queued_for_desk", "submitted"):
                continue
            limit = timedelta(hours=3 if pa.stage == "final_discharge" else 1)
            elapsed = s.now - pa.requested_at
            key = f"clock:{pa.id}"
            if elapsed > limit * 0.75 and key + ":warn" not in s.campaign_sent:
                s.campaign_sent.add(key + ":warn")
                s.add_task("insurance_desk", "urgent", f"{pa.id} at {int(elapsed.total_seconds() // 60)} min of "
                           f"{int(limit.total_seconds() // 60)} min IRDAI limit - chase {s.policies[pa.policy_number].tpa}",
                           preauth=pa.id)
                if pa.stage == "final_discharge":
                    s.add_task("ward", "high", f"Discharge authorisation for {s.patients[pa.patient_id].name} still "
                               "pending; bed release at risk", preauth=pa.id)
            if elapsed > limit and key + ":breach" not in s.campaign_sent:
                s.campaign_sent.add(key + ":breach")
                s.event("irdai_clock_breached", preauth=pa.id, stage=pa.stage)
                s.add_task("billing", "high", f"{pa.id} exceeded IRDAI limit; under the 2024 Master Circular any "
                           "extra charge from the delay is borne by the insurer - record the timestamps", preauth=pa.id)


def record_insurer_decision(store: Store, engine: CampaignEngine, preauth_id: str, status: str, amount: int,
                            reference: str, note: str = "") -> None:
    """Called when the insurance desk (or the NHCX webhook) records the insurer's written decision.

    Updates go to ward, billing and patient at the same time, so nobody has to phone around.
    """
    s = store
    pa = s.preauths[preauth_id]
    pa.status, pa.approved_amount, pa.insurer_reference, pa.insurer_note = status, amount, reference, note
    pa.decided_at = s.now
    s.event("preauth_decided", preauth=pa.id, status=status, stage=pa.stage,
            minutes=int((s.now - pa.requested_at).total_seconds() // 60) if pa.requested_at else None)
    s.add_task("ward", "normal", f"Pre-auth {pa.id} {status} ({reference})", preauth=pa.id)
    s.add_task("billing", "normal", f"Pre-auth {pa.id} {status}: Rs {amount:,} ({reference})", preauth=pa.id)
    if status in ("approved", "partially_approved"):
        text = (f"Update from your insurer ({s.policies[pa.policy_number].insurer}): the {pa.procedure} request is "
                f"{status.replace('_', ' ')} for Rs {amount:,}, reference {reference}. Any amount above this, co-pay "
                "and non-medical items are payable at the hospital. Our billing desk can explain the details.")
    elif status == "query_raised":
        text = (f"Your insurer has asked for more information on the {pa.procedure} request: {note} "
                "Reply here and I'll help you send it.")
    else:
        text = (f"Your insurer has not approved the {pa.procedure} request (reference {reference}). Reason given: "
                f"{note or 'not stated'}. Our insurance desk will call you to explain the options.")
    engine._send_once(f"pa:{pa.id}:{status}:{reference}", pa.patient_id, text, CARE, "preauth_update")
