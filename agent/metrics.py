"""Metrics from the product brief (section 7), computed from the event log.

Baseline these in the first fortnight; pick one as the north star for the deployment.
"""

from __future__ import annotations

from statistics import median

from .store import Store


def _ratio(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def compute(store: Store) -> dict:
    ev = store.events
    count = lambda name, **kw: sum(1 for e in ev if e["event"] == name and all(e.get(k) == v for k, v in kw.items()))
    appts = list(store.appointments.values())
    past = [a for a in appts if a.start < store.now and a.status != "cancelled"]
    decided = [e for e in ev if e["event"] == "preauth_decided" and e.get("minutes") is not None]
    finals = [e for e in decided if e.get("stage") == "final_discharge"]
    sessions = {e["session"] for e in ev if e["event"] == "inbound"}
    handoffs = count("handoff") + count("red_flag_escalated")
    claims = [c for c in store.claims.values() if c.first_pass is not None]
    followups_sent = sum(1 for m in store.outbox if m["template"] == "post_visit_followup")
    recall_sent = sum(1 for m in store.outbox if m["template"] and m["template"].startswith("recall_"))
    return {
        "intake": {
            "digital_intake_completed": count("intake_document_confirmed"),
            "no_show_rate_pct": _ratio(sum(a.status == "no_show" for a in past), len(past)),
            "released_slot_refill_rate_pct": _ratio(count("released_slot_refilled"), count("slot_released")),
            "bookings_by_agent": count("appointment_booked"),
        },
        "engagement": {
            "followup_contact_achieved_pct": _ratio(count("followup_completed"), followups_sent),
            "followups_flagged": count("followup_completed", flagged=True),
            "recall_response_rate_pct": _ratio(count("appointment_booked", source="recall"), recall_sent),
            "opt_outs": count("opt_out"),
        },
        "insurance": {
            "preauth_turnaround_median_min": median([e["minutes"] for e in decided]) if decided else None,
            "discharges_authorised_within_3h_pct": _ratio(sum(e["minutes"] <= 180 for e in finals), len(finals)),
            "irdai_clock_breaches": count("irdai_clock_breached"),
            "first_pass_claim_acceptance_pct": _ratio(sum(bool(c.first_pass) for c in claims), len(claims)),
            "claim_gaps_logged": count("claim_gap"),
        },
        "all": {
            "conversations": len(sessions),
            "containment_pct": _ratio(len(sessions) - min(handoffs, len(sessions)), len(sessions)),
            "red_flag_escalations": count("red_flag_escalated"),
            "guard_blocks": count("guard_blocked"),
        },
    }
