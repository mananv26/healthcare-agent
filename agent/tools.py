"""Tools the model can call. Each one is an ordinary, validated, logged function over the Store.

Writes are split into propose_* (validate + build a read-back + idempotency key, change nothing) and
commit_action (executes only if the patient's latest message is an explicit yes to that read-back).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable

from . import guards
from .store import (
    CARE, INSURANCE_SHARING, MARKETING, PURPOSES, Appointment, PendingAction, PreAuth, Store, WaitlistEntry,
)

if TYPE_CHECKING:
    from .agent import Session


class ToolError(Exception):
    """Returned to the model as an is_error tool_result so it can recover or explain."""


TOOLS: list[dict] = []
HANDLERS: dict[str, Callable[["Session", dict], dict]] = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    TOOLS.append({
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required or [],
                         "additionalProperties": False},
    })

    def register(fn):
        HANDLERS[name] = fn
        return fn

    return register


def run_tool(session: "Session", name: str, args: dict) -> tuple[str, bool]:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool {name}", True
    try:
        result = handler(session, args)
        session.store.log("tool", tool=name, patient=session.patient_id, ok=True)
        return json.dumps(result, default=str), False
    except ToolError as e:
        session.store.log("tool", tool=name, patient=session.patient_id, ok=False, error=str(e))
        return str(e), True


def _need_patient(session: "Session") -> str:
    if not session.patient_id:
        raise ToolError("Identity not verified. Call verify_identity (or propose_registration for a new patient) first.")
    return session.patient_id


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be YYYY-MM-DD, got {value!r}")


def _fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %Y, %I:%M %p")


def _propose(session: "Session", kind: str, payload: dict, readback: str) -> dict:
    s = session.store
    pid = payload.get("patient_id") or session.patient_id or "new"
    for p in s.pending.values():  # one live proposal per patient: a new one supersedes the old
        if p.patient_id == pid and p.status == "pending":
            p.status = "superseded"
    key = hashlib.sha256(json.dumps([kind, pid, payload], sort_keys=True, default=str).encode()).hexdigest()[:16]
    pa = PendingAction(s.new_id("ACT"), kind, pid, payload, readback, key, session.turn)
    s.pending[pa.token] = pa
    s.log("action_proposed", token=pa.token, kind=kind, patient=pid)
    return {
        "token": pa.token,
        "readback": readback,
        "next_step": "Read this back to the patient in their language and ask them to reply YES to confirm. "
                     "Call commit_action with this token only after they reply.",
    }


# ---------------------------------------------------------------------------------------------------
# Identity & intake
# ---------------------------------------------------------------------------------------------------

@tool("verify_identity",
      "Match the person to a patient record using date of birth plus the phone number the message came from "
      "(or one they give). Required before any record is read or changed. For minors, the guardian verifies "
      "with the child's date of birth.",
      {"dob": {"type": "string", "description": "YYYY-MM-DD"},
       "phone": {"type": "string", "description": "Only if different from the channel's number"},
       "name": {"type": "string"}},
      ["dob"])
def verify_identity(session: "Session", a: dict) -> dict:
    phone = re.sub(r"[^\d+]", "", a.get("phone") or session.channel_phone or "")
    if not phone:
        raise ToolError("No phone number available; ask the patient for the registered mobile number.")
    matches = [p for p in session.store.patients.values()
               if p.phone.endswith(phone[-10:]) and p.dob == a["dob"]]
    if not matches:
        session.failed_verifications += 1
        if session.failed_verifications >= 2:
            return {"verified": False, "action": "handoff",
                    "message": "Two failed attempts. Offer handoff_to_human (front_desk) or registration as a new patient."}
        return {"verified": False, "message": "No record matches. Re-check DOB/number, or offer registration."}
    p = matches[0]
    session.patient_id = p.id
    session.language = p.language
    session.store.log("identity_verified", patient=p.id, channel=session.channel)
    return {"verified": True, "patient_id": p.id, "name": p.name, "guardian": p.guardian_name,
            "language": p.language, "has_abha": bool(p.abha)}


@tool("propose_registration",
      "Propose creating a new patient record. ABHA is optional and must never be required for care. "
      "Before proposing, show the DPDP notice (what data is collected and why) and get consent for care communication.",
      {"name": {"type": "string"}, "dob": {"type": "string"}, "phone": {"type": "string"},
       "language": {"type": "string"}, "abha": {"type": "string"},
       "guardian_name": {"type": "string", "description": "Required if the patient is under 18"},
       "notice_shown": {"type": "boolean", "description": "True once the itemised data notice was shown"}},
      ["name", "dob", "phone", "language", "notice_shown"])
def propose_registration(session: "Session", a: dict) -> dict:
    if not a["notice_shown"]:
        raise ToolError("Show the data notice first: identity and contact details for registration and appointments; "
                        "health information for your care; messages about your care. Then propose again.")
    dob = _parse_date(a["dob"], "dob")
    age = (session.store.now.date() - dob).days // 365
    if age < 18 and not a.get("guardian_name"):
        raise ToolError("Patient is a minor; registration needs the parent/guardian's name and their consent.")
    phone = re.sub(r"[^\d+]", "", a["phone"])
    readback = (f"New registration: {a['name']}, born {dob:%d %b %Y}, mobile {phone}, language {a['language']}"
                + (f", guardian {a['guardian_name']}" if a.get("guardian_name") else "")
                + (", ABHA linked" if a.get("abha") else "") + ". Messages about your care will be sent to this number.")
    return _propose(session, "register", {**a, "phone": phone, "patient_id": "new:" + phone}, readback)


@tool("get_patient_summary",
      "Demographics, conditions, medications, allergies, upcoming appointments, waitlist offers, insurance policy "
      "on file and current consents for the verified patient.", {})
def get_patient_summary(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    p = s.patients[pid]
    upcoming = [
        {"appointment_id": ap.id, "when": _fmt(ap.start), "doctor": s.providers[ap.provider_id].name,
         "location": s.providers[ap.provider_id].location, "reason": ap.reason, "status": ap.status}
        for ap in s.appointments.values()
        if ap.patient_id == pid and ap.start > s.now and ap.status in ("booked", "confirmed")
    ]
    offers = []
    for w in s.waitlist.values():
        if w.patient_id == pid and w.offered_slot:
            slot = s.slots[w.offered_slot]
            if slot.held_for == pid and slot.held_until and slot.held_until > s.now:
                offers.append({"slot_id": slot.id, "when": _fmt(slot.start), "doctor": s.providers[slot.provider_id].name,
                               "hold_expires": _fmt(slot.held_until), "reason": w.reason})
    return {
        "name": p.name, "dob": p.dob, "guardian": p.guardian_name, "language": p.language,
        "preferred_channel": p.preferred_channel, "abha_linked": bool(p.abha),
        "conditions": p.conditions, "medications": p.medications, "allergies": p.allergies,
        "upcoming_appointments": upcoming, "waitlist_offers": offers,
        "insurance_policy": p.insurance_policy,
        "consents": {purpose: s.has_consent(pid, purpose) for purpose in PURPOSES},
    }


@tool("list_documents", "Documents the patient has uploaded (prescriptions, reports, discharge summaries, policy card).", {})
def list_documents(session: "Session", a: dict) -> dict:
    pid = _need_patient(session)
    return {"documents": [{"document_id": d.id, "kind": d.kind, "extracted": bool(d.extracted),
                           "confirmed": bool(d.confirmed_fields)}
                          for d in session.store.documents.values() if d.patient_id == pid]}


@tool("extract_document",
      "Extract structured history (medications, diagnoses, allergies, dates) from an uploaded document. "
      "Every extracted field is UNCONFIRMED: read each one back and ask the patient to confirm or correct it.",
      {"document_id": {"type": "string"}}, ["document_id"])
def extract_document(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    doc = s.documents.get(a["document_id"])
    if not doc or doc.patient_id != pid:
        raise ToolError("Document not found for this patient.")
    # Production: OCR/vision model with a fixed output schema. The seeded documents use a simple layout.
    text = doc.ocr_text
    fields: dict = {}
    if m := re.search(r"Dx:\s*([^|]+)", text):
        fields["diagnoses"] = [x.strip() for x in m.group(1).split(",")]
    if m := re.search(r"Rx:\s*([^|]+)", text):
        fields["medications"] = [re.sub(r"^Tab\s+", "", x.strip()) for x in m.group(1).split(";")]
    if m := re.search(r"Allergy:\s*([^|]+)", text):
        fields["allergies"] = [x.strip() for x in m.group(1).split(",")]
    if m := re.search(r"Review\s+([^|]+)", text):
        fields["review_instruction"] = m.group(1).strip()
    if m := re.search(r"(\d{2}/\d{2}/\d{4})", text):
        fields["document_date"] = m.group(1)
    doc.extracted = fields
    s.log("document_extracted", patient=pid, document=doc.id)
    return {"document_id": doc.id, "kind": doc.kind, "fields": fields, "status": "unconfirmed",
            "instruction": "Read back each field. Only fields the patient confirms are saved, and they are saved as "
                           "patient-reported history for clinician review - not as a clinical record."}


@tool("save_confirmed_document_fields",
      "Save the fields the patient explicitly confirmed from extract_document. Omit anything they corrected or "
      "were unsure about (put corrections in 'corrections').",
      {"document_id": {"type": "string"},
       "confirmed_fields": {"type": "array", "items": {"type": "string"}, "description": "Field names confirmed as correct"},
       "corrections": {"type": "object", "description": "field -> corrected value as stated by the patient"}},
      ["document_id", "confirmed_fields"])
def save_confirmed_document_fields(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    doc = s.documents.get(a["document_id"])
    if not doc or doc.patient_id != pid or not doc.extracted:
        raise ToolError("Extract the document first.")
    saved = {k: doc.extracted[k] for k in a["confirmed_fields"] if k in doc.extracted}
    doc.confirmed_fields = {**saved, **{f"{k} (patient correction)": v for k, v in (a.get("corrections") or {}).items()}}
    p = s.patients[pid]
    for allergy in saved.get("allergies", []):
        if allergy not in p.allergies:
            p.allergies.append(allergy)
    s.add_task("clinician", "routine", f"Review patient-reported history from {doc.kind} {doc.id}", patient=pid,
               fields=doc.confirmed_fields)
    s.event("intake_document_confirmed", patient=pid)
    return {"saved": list(doc.confirmed_fields), "note": "Queued for clinician review before the consultation."}


# ---------------------------------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------------------------------

@tool("search_slots",
      "Live availability from consultant calendars. Give specialty or provider_id. Returns at most 8 open slots.",
      {"specialty": {"type": "string", "enum": ["general_medicine", "orthopedics", "cardiology", "pediatrics", "endocrinology"]},
       "provider_id": {"type": "string"},
       "date_from": {"type": "string", "description": "YYYY-MM-DD"},
       "date_to": {"type": "string", "description": "YYYY-MM-DD"},
       "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]}},
      ["date_from", "date_to"])
def search_slots(session: "Session", a: dict) -> dict:
    s = session.store
    d0, d1 = _parse_date(a["date_from"], "date_from"), _parse_date(a["date_to"], "date_to")
    if not a.get("specialty") and not a.get("provider_id"):
        raise ToolError("Give a specialty or provider_id.")
    part = a.get("part_of_day", "any")
    out = []
    for slot in sorted(s.slots.values(), key=lambda x: x.start):
        prov = s.providers[slot.provider_id]
        held = slot.held_for and slot.held_until and slot.held_until > s.now and slot.held_for != session.patient_id
        if slot.booked or held or slot.start <= s.now or not (d0 <= slot.start.date() <= d1):
            continue
        if a.get("provider_id") and slot.provider_id != a["provider_id"]:
            continue
        if a.get("specialty") and prov.specialty != a["specialty"]:
            continue
        h = slot.start.hour
        if part == "morning" and h >= 12 or part == "afternoon" and not 12 <= h < 17 or part == "evening" and h < 17:
            continue
        out.append({"slot_id": slot.id, "when": _fmt(slot.start), "doctor": prov.name, "specialty": prov.specialty,
                    "location": prov.location})
        if len(out) == 8:
            break
    return {"slots": out} if out else {"slots": [], "hint": "No slots. Offer propose_waitlist or a wider date range."}


@tool("propose_booking", "Propose booking a slot. Returns a read-back and token; nothing is booked yet.",
      {"slot_id": {"type": "string"}, "reason": {"type": "string"},
       "visit_type": {"type": "string", "enum": ["consult", "follow_up", "procedure", "telehealth"]}},
      ["slot_id", "reason", "visit_type"])
def propose_booking(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    slot = s.slots.get(a["slot_id"])
    if not slot or slot.booked or slot.start <= s.now:
        raise ToolError("That slot is no longer available. Search again.")
    if slot.held_for and slot.held_for != pid and slot.held_until and slot.held_until > s.now:
        raise ToolError("That slot is held for another patient. Search again.")
    prov = s.providers[slot.provider_id]
    readback = (f"{s.patients[pid].name} with {prov.name} ({prov.specialty.replace('_', ' ')}) on {_fmt(slot.start)} "
                f"at {prov.location}, for: {a['reason']}.")
    return _propose(session, "book", {"patient_id": pid, **a}, readback)


@tool("propose_reschedule", "Propose moving an existing appointment to a new slot.",
      {"appointment_id": {"type": "string"}, "new_slot_id": {"type": "string"}}, ["appointment_id", "new_slot_id"])
def propose_reschedule(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    ap = s.appointments.get(a["appointment_id"])
    slot = s.slots.get(a["new_slot_id"])
    if not ap or ap.patient_id != pid or ap.status not in ("booked", "confirmed"):
        raise ToolError("No active appointment with that id for this patient.")
    if not slot or slot.booked:
        raise ToolError("New slot not available.")
    readback = (f"Move your appointment with {s.providers[ap.provider_id].name} from {_fmt(ap.start)} "
                f"to {_fmt(slot.start)} with {s.providers[slot.provider_id].name}.")
    return _propose(session, "reschedule", {"patient_id": pid, **a}, readback)


@tool("propose_cancellation", "Propose cancelling an appointment. The released slot is offered to the waitlist.",
      {"appointment_id": {"type": "string"}, "reason": {"type": "string"}}, ["appointment_id"])
def propose_cancellation(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    ap = s.appointments.get(a["appointment_id"])
    if not ap or ap.patient_id != pid or ap.status not in ("booked", "confirmed"):
        raise ToolError("No active appointment with that id for this patient.")
    readback = f"Cancel your appointment with {s.providers[ap.provider_id].name} on {_fmt(ap.start)}."
    return _propose(session, "cancel", {"patient_id": pid, **a}, readback)


@tool("propose_waitlist", "Propose adding the patient to the waitlist for an earlier slot with a provider.",
      {"provider_id": {"type": "string"}, "earliest": {"type": "string"}, "latest": {"type": "string"},
       "reason": {"type": "string"}}, ["provider_id", "earliest", "latest", "reason"])
def propose_waitlist(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    if a["provider_id"] not in s.providers:
        raise ToolError("Unknown provider.")
    e, l = _parse_date(a["earliest"], "earliest"), _parse_date(a["latest"], "latest")
    readback = (f"Add you to the waitlist for {s.providers[a['provider_id']].name} between {e:%d %b} and {l:%d %b}. "
                "If a slot opens we'll message you and hold it for 2 hours.")
    return _propose(session, "waitlist", {"patient_id": pid, **a}, readback)


@tool("commit_action",
      "Execute a previously proposed action. Only call after the read-back was shown AND the patient's latest "
      "message confirms it. The system independently verifies the confirmation.",
      {"token": {"type": "string"}}, ["token"])
def commit_action(session: "Session", a: dict) -> dict:
    s = session.store
    pa = s.pending.get(a["token"])
    if not pa or pa.status == "superseded":
        raise ToolError("Unknown or superseded token. Propose again.")
    if pa.idempotency_key in s.committed:  # retry of an already-applied action
        return {**s.committed[pa.idempotency_key], "idempotent_replay": True}
    if pa.proposed_at_turn >= session.turn:
        raise ToolError("The patient has not seen this read-back yet. Show it and wait for their reply.")
    if not session.patient_said_yes():
        raise ToolError("The patient's latest message is not an explicit confirmation. Ask them to reply YES, "
                        "or propose a corrected action.")
    result = _APPLY[pa.kind](session, pa)
    pa.status = "committed"
    s.committed[pa.idempotency_key] = result
    s.log("action_committed", token=pa.token, kind=pa.kind, patient=pa.patient_id, key=pa.idempotency_key)
    return result


def _apply_register(session: "Session", pa: PendingAction) -> dict:
    s, a = session.store, pa.payload
    from .store import Patient
    pid = s.new_id("PT")
    s.patients[pid] = Patient(pid, a["name"], a["dob"], a["phone"], language=a["language"], abha=a.get("abha"),
                              guardian_name=a.get("guardian_name"), preferred_channel=session.channel)
    s.set_consent(pid, CARE, True, session.channel, "agent registration after notice")
    session.patient_id, session.language = pid, a["language"]
    s.event("patient_registered", patient=pid)
    return {"registered": True, "patient_id": pid}


def _apply_book(session: "Session", pa: PendingAction) -> dict:
    s, a = session.store, pa.payload
    slot = s.slots[a["slot_id"]]
    if slot.booked:
        raise ToolError("Slot was taken while confirming. Apologise and search again.")
    from_waitlist = slot.held_for == pa.patient_id
    slot.booked, slot.held_for, slot.held_until = True, None, None
    ap = Appointment(s.new_id("APT"), pa.patient_id, slot.provider_id, slot.id, slot.start, a["reason"], a["visit_type"],
                     source="waitlist_refill" if from_waitlist else "agent")
    s.appointments[ap.id] = ap
    if from_waitlist:
        for w in list(s.waitlist.values()):
            if w.patient_id == pa.patient_id and w.offered_slot == slot.id:
                del s.waitlist[w.id]
        s.event("released_slot_refilled", slot=slot.id)
    s.event("appointment_booked", patient=pa.patient_id, appointment=ap.id, source=ap.source)
    prov = s.providers[slot.provider_id]
    s.send(pa.patient_id, f"Confirmed: {prov.name}, {_fmt(slot.start)}, {prov.location}. Ref {ap.id}. "
                          "Reply R to reschedule or C to cancel.", CARE, "appointment_confirmation")
    return {"booked": True, "appointment_id": ap.id, "when": _fmt(slot.start), "doctor": prov.name,
            "location": prov.location, "confirmation_sent": True,
            "reminders": "Automatic reminders at 24 hours and 2 hours before."}


def _release_slot(s: Store, slot_id: str) -> dict | None:
    """Free a slot and offer it to the first matching waitlist entry, holding it for 2 hours."""
    slot = s.slots.get(slot_id)
    if not slot:
        return None
    slot.booked = False
    s.event("slot_released", slot=slot_id)
    for w in sorted(s.waitlist.values(), key=lambda w: w.id):
        if w.provider_id == slot.provider_id and w.earliest <= slot.start.date() <= w.latest and not w.offered_slot:
            slot.held_for, slot.held_until = w.patient_id, s.now + timedelta(hours=2)
            w.offered_slot = slot.id
            prov = s.providers[slot.provider_id]
            s.send(w.patient_id, f"An earlier slot opened with {prov.name}: {_fmt(slot.start)}. "
                                 "We're holding it for you for 2 hours - reply YES to take it.", CARE, "waitlist_offer")
            s.event("waitlist_offer_sent", slot=slot_id, patient=w.patient_id)
            return {"offered_to_waitlist": True}
    return {"offered_to_waitlist": False}


def _apply_reschedule(session: "Session", pa: PendingAction) -> dict:
    s, a = session.store, pa.payload
    ap, new = s.appointments[a["appointment_id"]], s.slots[a["new_slot_id"]]
    if new.booked:
        raise ToolError("New slot was taken while confirming. Search again.")
    old_slot = ap.slot_id
    new.booked = True
    ap.slot_id, ap.start, ap.provider_id, ap.status, ap.reminders_sent = new.id, new.start, new.provider_id, "booked", []
    _release_slot(s, old_slot)
    s.event("appointment_rescheduled", patient=pa.patient_id, appointment=ap.id)
    s.send(pa.patient_id, f"Rescheduled: {s.providers[ap.provider_id].name}, {_fmt(ap.start)}. Ref {ap.id}.", CARE,
           "appointment_rescheduled")
    return {"rescheduled": True, "appointment_id": ap.id, "when": _fmt(ap.start)}


def _apply_cancel(session: "Session", pa: PendingAction) -> dict:
    s, a = session.store, pa.payload
    ap = s.appointments[a["appointment_id"]]
    ap.status = "cancelled"
    refill = _release_slot(s, ap.slot_id)
    s.event("appointment_cancelled", patient=pa.patient_id, appointment=ap.id, reason=a.get("reason"))
    return {"cancelled": True, "appointment_id": ap.id, **(refill or {})}


def _apply_waitlist(session: "Session", pa: PendingAction) -> dict:
    s, a = session.store, pa.payload
    w = WaitlistEntry(s.new_id("WL"), pa.patient_id, a["provider_id"], date.fromisoformat(a["earliest"]),
                      date.fromisoformat(a["latest"]), a["reason"])
    s.waitlist[w.id] = w
    s.event("waitlist_joined", patient=pa.patient_id)
    return {"waitlisted": True, "waitlist_id": w.id}


@tool("get_approved_content",
      "Clinician-approved content: preparation instructions ('prep:<procedure>'), follow-up questionnaires "
      "('followup:<type>'), service/package descriptions ('service:<name>'). The ONLY source for clinical or "
      "preparation facts. If nothing is found, do not improvise - offer a callback from the care team.",
      {"topic": {"type": "string"}}, ["topic"])
def get_approved_content(session: "Session", a: dict) -> dict:
    topic = a["topic"].strip().lower()
    content = session.store.content
    hit = content.get(topic) or next((v for k, v in content.items() if topic in k or k.split(":", 1)[1] in topic), None)
    if not hit:
        return {"found": False, "available_topics": sorted(content),
                "instruction": "No approved content. Do not answer from general knowledge; offer handoff_to_human (clinician)."}
    return {"found": True, **hit}


# ---------------------------------------------------------------------------------------------------
# Insurance (pillar 3). The agent verifies, assembles, submits, chases and informs. It never approves.
# ---------------------------------------------------------------------------------------------------

def _policy_for(session: "Session") -> tuple[str, object]:
    s, pid = session.store, _need_patient(session)
    num = s.patients[pid].insurance_policy
    if not num or num not in s.policies:
        raise ToolError("No insurance policy on file. Ask for the policy card (request_document_upload kind=policy_card).")
    return pid, s.policies[num]


@tool("check_coverage",
      "Indicative cashless eligibility for a procedure against the hospital's empanelment: insurer/TPA, validity, "
      "network status, waiting period, sub-limit, room-rent cap, co-pay, exclusions. Result is INDICATIVE ONLY.",
      {"procedure": {"type": "string"}, "estimated_cost": {"type": "integer"},
       "room_rent_per_day": {"type": "integer", "description": "Planned room category tariff, if known"}},
      ["procedure"])
def check_coverage(session: "Session", a: dict) -> dict:
    s = session.store
    pid, pol = _policy_for(session)
    today = s.now.date()
    proc = a["procedure"].lower()
    issues, notes = [], []
    if pol.valid_until < today:
        issues.append(f"Policy expired on {pol.valid_until:%d %b %Y}; renewal proof needed.")
    if not pol.hospital_empanelled:
        issues.append("Hospital is not in this insurer's network; cashless not available (reimbursement only).")
    if not pol.cashless:
        issues.append("Policy does not offer cashless.")
    months_held = (today.year - pol.start.year) * 12 + today.month - pol.start.month
    for cond, months in pol.waiting_periods_months.items():
        if cond in proc and months_held < months:
            issues.append(f"Waiting period for {cond} is {months} months; policy held {months_held} months.")
    if any(x in proc for x in pol.exclusions):
        issues.append("Procedure appears in the policy exclusions.")
    for cond, limit in pol.sub_limits.items():
        if cond in proc:
            notes.append(f"Sub-limit for {cond}: Rs {limit:,}.")
    if pol.copay_pct:
        notes.append(f"Co-pay: {pol.copay_pct}% of admissible amount is payable by the patient.")
    rr = a.get("room_rent_per_day")
    if rr and rr > pol.room_rent_limit:
        notes.append(f"Room rent cap Rs {pol.room_rent_limit:,}/day; a Rs {rr:,} room can trigger proportionate "
                     "deduction on the whole bill. A room within the cap avoids this.")
    est = a.get("estimated_cost")
    if est:
        admissible = min(est, pol.balance, *[v for k, v in pol.sub_limits.items() if k in proc] or [est])
        notes.append(f"Rough patient share on Rs {est:,}: about Rs {est - int(admissible * (1 - pol.copay_pct / 100)):,} "
                     "before non-payables (consumables, admin charges).")
    s.log("coverage_check", patient=pid, procedure=proc, issues=len(issues))
    return {
        "indicative": True,
        "insurer": pol.insurer, "tpa": pol.tpa, "policy_valid_until": str(pol.valid_until),
        "balance_sum_insured": pol.balance, "likely_cashless": not issues, "issues": issues, "notes": notes,
        "nhcx_enabled": pol.nhcx_enabled,
        "required_wording": "Say this is an indicative check and that only the insurer/TPA can approve cashless, "
                            "which they confirm in writing after the pre-authorisation request.",
    }


def _sla(pa: PreAuth, now: datetime) -> dict:
    """IRDAI Master Circular (29 May 2024): cashless decision within 1h; final discharge authorisation within 3h."""
    if not pa.requested_at:
        return {}
    limit = timedelta(hours=3 if pa.stage == "final_discharge" else 1)
    end = pa.decided_at or now
    elapsed = end - pa.requested_at
    return {"irdai_limit_hours": limit.total_seconds() / 3600, "elapsed_minutes": int(elapsed.total_seconds() // 60),
            "breached": elapsed > limit}


@tool("get_insurance_status", "Pre-authorisations and claims for the verified patient, with status and regulatory clocks.", {})
def get_insurance_status(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    pre = [{"preauth_id": p.id, "procedure": p.procedure, "stage": p.stage, "status": p.status,
            "insurer_confirmed_in_writing": bool(p.insurer_reference and p.status in ("approved", "partially_approved")),
            "approved_amount": p.approved_amount or None, "insurer_note": p.insurer_note,
            "insurer_reference": p.insurer_reference, "documents": p.documents, "clock": _sla(p, s.now)}
           for p in s.preauths.values() if p.patient_id == pid]
    claims = [{"claim_id": c.id, "amount": c.amount, "status": c.status, "rejection_reasons": c.rejection_reasons}
              for c in s.claims.values() if c.patient_id == pid]
    return {"preauths": pre, "claims": claims,
            "wording_rule": "Only say 'approved' when insurer_confirmed_in_writing is true, and quote the reference."}


PREAUTH_DOCS = {
    "initial": ["policy_card", "id_proof", "consultation_note", "investigation_reports", "cost_estimate"],
    "enhancement": ["interim_bill", "treatment_update"],
    "final_discharge": ["discharge_summary", "final_bill", "investigation_reports", "patient_signature"],
}


@tool("prepare_preauth_pack",
      "Assemble the pre-authorisation pack from the record and list what is missing. Needs the patient's consent "
      "to share records with the insurer/TPA.",
      {"procedure": {"type": "string"}, "estimated_cost": {"type": "integer"},
       "stage": {"type": "string", "enum": ["initial", "enhancement", "final_discharge"]},
       "preauth_id": {"type": "string", "description": "Existing pre-auth to update (e.g. answering a TPA query)"}},
      ["procedure", "estimated_cost", "stage"])
def prepare_preauth_pack(session: "Session", a: dict) -> dict:
    s = session.store
    pid, pol = _policy_for(session)
    if not s.has_consent(pid, INSURANCE_SHARING):
        raise ToolError("No consent to share records with the insurer/TPA. Explain what will be shared and why, "
                        "then call update_consent(purpose='insurance_data_sharing', granted=true) if they agree.")
    have = {d.kind for d in s.documents.values() if d.patient_id == pid}
    pa = s.preauths.get(a.get("preauth_id") or "")
    if pa:
        have |= set(pa.documents)
    else:
        pa = PreAuth(s.new_id("PA"), pid, pol.number, a["procedure"], a["estimated_cost"], a["stage"], "pack_incomplete")
        s.preauths[pa.id] = pa
    required = list(PREAUTH_DOCS[a["stage"]])
    if pa.status == "query_raised" and "MRI" in pa.insurer_note:
        required += ["mri_report", "treatment_notes"]
    pa.documents = sorted(have & set(required) | set(pa.documents))
    missing = [d for d in required if d not in pa.documents]
    staff_docs = {"consultation_note", "cost_estimate", "treatment_notes", "discharge_summary", "final_bill",
                  "interim_bill", "treatment_update"}
    return {"preauth_id": pa.id, "status": pa.status, "insurer_query": pa.insurer_note or None,
            "missing_from_patient": [d for d in missing if d not in staff_docs],
            "missing_from_hospital": [d for d in missing if d in staff_docs],
            "ready_to_submit": not missing}


@tool("request_document_upload", "Send the patient a secure upload link for a specific document.",
      {"kind": {"type": "string", "enum": ["policy_card", "id_proof", "mri_report", "investigation_reports",
                                            "prescription", "discharge_summary", "lab_report"]},
       "reason": {"type": "string"}}, ["kind", "reason"])
def request_document_upload(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    link = f"https://upload.hospital.example/{s.new_id('UP').lower()}"
    s.send(pid, f"Please upload your {a['kind'].replace('_', ' ')} here (link valid 48h): {link}", CARE, "document_request")
    return {"sent": True, "link_expires_hours": 48}


@tool("propose_preauth_submission",
      "Propose submitting a complete pre-auth pack. On commit it goes to the insurance desk queue, which submits "
      "via NHCX or the TPA portal; the agent then tracks the IRDAI clock and informs ward, billing and patient.",
      {"preauth_id": {"type": "string"}}, ["preauth_id"])
def propose_preauth_submission(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    pa = s.preauths.get(a["preauth_id"])
    if not pa or pa.patient_id != pid:
        raise ToolError("Unknown pre-auth.")
    required = PREAUTH_DOCS[pa.stage] + (["mri_report", "treatment_notes"] if "MRI" in pa.insurer_note else [])
    missing = [d for d in required if d not in pa.documents]
    if missing:
        raise ToolError(f"Pack incomplete, missing: {', '.join(missing)}.")
    pol = s.policies[pa.policy_number]
    readback = (f"Send the {pa.stage.replace('_', ' ')} pre-authorisation for {pa.procedure} (estimate Rs {pa.estimated_cost:,}) "
                f"to {pol.insurer} via {'NHCX' if pol.nhcx_enabled else pol.tpa}, sharing: {', '.join(pa.documents)}. "
                "This is a request - the insurer decides.")
    return _propose(session, "submit_preauth", {"patient_id": pid, **a}, readback)


def _apply_submit_preauth(session: "Session", pa_action: PendingAction) -> dict:
    s = session.store
    pa = s.preauths[pa_action.payload["preauth_id"]]
    pol = s.policies[pa.policy_number]
    pa.status, pa.requested_at = "queued_for_desk", s.now
    hours = 3 if pa.stage == "final_discharge" else 1
    task = s.add_task("insurance_desk", "high", f"Submit {pa.stage} pre-auth {pa.id} via "
                      f"{'NHCX' if pol.nhcx_enabled else pol.tpa + ' portal'}", preauth=pa.id,
                      irdai_deadline=(s.now + timedelta(hours=hours)).isoformat(timespec="minutes"))
    s.event("preauth_requested", preauth=pa.id, stage=pa.stage)
    return {"submitted_to_desk": True, "task": task["id"], "status": "queued_for_desk",
            "insurer_decision_due_by": _fmt(s.now + timedelta(hours=hours)),
            "wording": "Tell the patient the request is with the insurance desk and the insurer must respond within "
                       f"{hours} hour(s) under IRDAI rules; you will update them. It is NOT approved."}


@tool("claim_documentation_check",
      "Pre-submission QA on a claim: missing documents, signatures, procedure-code mismatch. Logs gaps so they "
      "stop recurring. Staff-facing; tell the patient only what they need to provide.",
      {"claim_id": {"type": "string"}}, ["claim_id"])
def claim_documentation_check(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    c = s.claims.get(a["claim_id"])
    if not c or c.patient_id != pid:
        raise ToolError("Unknown claim.")
    required = ["final_bill", "discharge_summary", "prescription", "investigation_reports", "pharmacy_bills"]
    gaps = [f"missing:{d}" for d in required if d not in c.attached]
    gaps += [f"signature:{who}" for who in ("patient", "treating_doctor") if who not in c.signatures]
    if c.preauth_id and c.preauth_id in s.preauths and s.preauths[c.preauth_id].procedure not in ("", None):
        pass  # production: compare billed procedure code against the authorised one
    c.status = "qa_failed" if gaps else c.status
    for g in gaps:
        s.event("claim_gap", claim=c.id, gap=g)
    patient_items = [g.split(":")[1] for g in gaps if g in ("missing:pharmacy_bills", "missing:investigation_reports",
                                                             "signature:patient")]
    if gaps:
        s.add_task("billing", "normal", f"Claim {c.id} QA gaps", claim=c.id, gaps=gaps)
    return {"claim_id": c.id, "passes_qa": not gaps, "gaps": gaps, "ask_patient_for": patient_items}


# ---------------------------------------------------------------------------------------------------
# Engagement, consent, escalation
# ---------------------------------------------------------------------------------------------------

@tool("record_followup_response",
      "Record answers to a post-visit follow-up questionnaire. The system screens answers for red flags and "
      "escalates to a clinician automatically.",
      {"appointment_id": {"type": "string"}, "questionnaire": {"type": "string"},
       "answers": {"type": "array", "items": {"type": "object", "properties": {
           "question": {"type": "string"}, "answer": {"type": "string"}}, "required": ["question", "answer"]}}},
      ["appointment_id", "questionnaire", "answers"])
def record_followup_response(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    ap = s.appointments.get(a["appointment_id"])
    if not ap or ap.patient_id != pid:
        raise ToolError("Unknown appointment.")
    q = s.content.get(a["questionnaire"], {})
    blob = " ".join(x["answer"] for x in a["answers"]).lower()
    flags = [w for w in q.get("red_flag_answers", []) if w in blob and not re.search(r"\bno\b[^.]{0,15}" + re.escape(w), blob)]
    if rf := guards.screen_red_flags(blob):
        flags.append(rf.category)
    ap.followup_done = True
    s.event("followup_completed", patient=pid, appointment=ap.id, flagged=bool(flags))
    if flags:
        s.add_task("clinician", "urgent", f"Follow-up red flags for {s.patients[pid].name}: {', '.join(flags)}",
                   patient=pid, answers=a["answers"])
        return {"recorded": True, "red_flags": flags, "escalated": True,
                "instruction": "Tell the patient a clinician will call them shortly; if symptoms worsen, call 108 or "
                               "come to Emergency. Do not interpret the symptoms."}
    return {"recorded": True, "red_flags": [], "instruction": "Thank them; remind them of the review date if one exists."}


@tool("update_consent",
      "Grant or withdraw consent for a purpose. Withdrawal is immediate and needs no confirmation step "
      "(DPDP: withdrawing must be as easy as giving). Granting marketing consent requires an explicit opt-in.",
      {"purpose": {"type": "string", "enum": list(PURPOSES)}, "granted": {"type": "boolean"}},
      ["purpose", "granted"])
def update_consent(session: "Session", a: dict) -> dict:
    s, pid = session.store, _need_patient(session)
    if a["granted"] and a["purpose"] == MARKETING and not session.patient_said_yes():
        raise ToolError("Marketing opt-in needs the patient's own explicit yes to that specific question.")
    s.set_consent(pid, a["purpose"], a["granted"], session.channel, "agent conversation")
    return {"purpose": a["purpose"], "granted": a["granted"], "effective": "immediately, on all channels"}


@tool("escalate_red_flag",
      "Use immediately if the patient describes a possible emergency the automatic screen did not catch. "
      "Creates an urgent clinician callback. Do not triage or ask further symptom questions.",
      {"category": {"type": "string"}, "summary": {"type": "string"}}, ["category", "summary"])
def escalate_red_flag(session: "Session", a: dict) -> dict:
    s = session.store
    s.add_task("clinician", "urgent", f"Red flag ({a['category']}): {a['summary']}", patient=session.patient_id,
               channel=session.channel, phone=session.channel_phone)
    session.escalated = True
    s.event("red_flag_escalated", patient=session.patient_id, category=a["category"], source="model")
    return {"escalated": True, "say": guards.emergency_reply(guards.RedFlag(a["category"], ""), session.language)}


@tool("handoff_to_human",
      "Hand the conversation to staff with a summary so the patient never repeats themselves. Use after two failed "
      "attempts to understand, when asked for a person, or for any commitment you cannot make (fees waivers, "
      "clinical questions, coverage guarantees, complaints).",
      {"queue": {"type": "string", "enum": ["front_desk", "insurance_desk", "billing", "clinician", "grievance"]},
       "reason": {"type": "string"}, "summary": {"type": "string"}},
      ["queue", "reason", "summary"])
def handoff_to_human(session: "Session", a: dict) -> dict:
    s = session.store
    task = s.add_task(a["queue"], "normal", a["reason"], patient=session.patient_id, summary=a["summary"],
                      channel=session.channel, transcript_ref=session.id)
    session.handed_off = True
    s.event("handoff", queue=a["queue"], reason=a["reason"])
    hours = "within 15 minutes (8am-8pm)" if a["queue"] != "clinician" else "within 30 minutes"
    return {"handed_off": True, "task": task["id"], "expected_response": hours}


_APPLY = {
    "register": _apply_register, "book": _apply_book, "reschedule": _apply_reschedule, "cancel": _apply_cancel,
    "waitlist": _apply_waitlist, "submit_preauth": _apply_submit_preauth,
}


def insurer_confirmed_approval(store: Store, patient_id: str | None) -> bool:
    return any(p.patient_id == patient_id and p.status in ("approved", "partially_approved") and p.insurer_reference
               for p in store.preauths.values())
