import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agent import CampaignEngine, HealthcareAgent, SarvamLanguageLayer, Session, Store, record_insurer_decision
from agent import guards
from agent.metrics import compute
from agent.tools import run_tool
from fakes import FakeSarvam, ScriptedClaude, call, last_tool_result, reply, text

RAVI = "+919845011111"
LAKSHMI = "+919845044444"


def make(steps=(), sarvam=None, phone=RAVI, channel="whatsapp"):
    store = Store()
    claude = ScriptedClaude(steps)
    layer = SarvamLanguageLayer(client=sarvam) if sarvam else None
    agent = HealthcareAgent(store, client=claude, language=layer)
    return store, claude, agent, Session(store, channel=channel, channel_phone=phone)


# ---- guards ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("msg,cat", [
    ("I have chest pain since morning", "cardiac"),
    ("papa ko seene mein dard ho raha hai", "cardiac"),
    ("I want to end my life", "self_harm"),
    ("my son had fits an hour ago", "neuro"),
])
def test_red_flags_detected(msg, cat):
    assert guards.screen_red_flags(msg).category == cat


def test_red_flag_negation_ignored():
    assert guards.screen_red_flags("no chest pain, just want to book a review") is None


@pytest.mark.parametrize("msg,ok", [("yes", True), ("Haan ji", True), ("ok book it", True),
                                    ("yes but change the time", False), ("nahi", False), ("what time?", False)])
def test_explicit_yes(msg, ok):
    assert guards.is_explicit_yes(msg) is ok


def test_coverage_guard():
    assert not guards.check_outbound("Good news, your cashless is approved!", False).ok
    assert guards.check_outbound("Good news, your cashless is approved!", True).ok
    assert guards.check_outbound("Your request is submitted, not yet approved.", False).ok
    assert guards.check_outbound("Take 1000 mg of paracetamol", False).reason == "clinical_advice"


# ---- confirm-before-commit -------------------------------------------------------------------------

def test_booking_requires_readback_then_yes():
    store, claude, agent, ses = make([
        reply(call("verify_identity", dob="1979-04-12")),
        reply(call("search_slots", specialty="endocrinology", date_from="2026-10-27", date_to="2026-10-30")),
        lambda m: reply(call("propose_booking", slot_id=last_tool_result(m)["slots"][0]["slot_id"],
                             reason="diabetes review", visit_type="consult")),
        lambda m: reply(text("Read-back: " + last_tool_result(m)["readback"] + " Reply YES to confirm.")),
        # turn 2: patient says yes
        lambda m: reply(call("commit_action", token=next(
            b for msg in m if msg["role"] == "user" and isinstance(msg["content"], list)
            for b in msg["content"] if b.get("type") == "tool_result" and "token" in b["content"]
        )["content"].split('"token": "')[1].split('"')[0])),
        lambda m: reply(text(f"Booked, ref {last_tool_result(m)['appointment_id']}.")),
    ])
    out1 = agent.handle(ses, "I need a diabetes review with the endocrinologist this week")
    assert "Reply YES" in out1
    assert not any(a.reason == "diabetes review" for a in store.appointments.values())
    out2 = agent.handle(ses, "yes")
    assert "Booked" in out2
    assert any(a.reason == "diabetes review" for a in store.appointments.values())
    assert any(m["template"] == "appointment_confirmation" for m in store.outbox)
    req = claude.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["thinking"] == {"type": "adaptive"}


def test_commit_blocked_in_same_turn_and_without_yes():
    store, _, agent, ses = make()
    ses.patient_id, ses.turn, ses.last_patient_text = "PT-1", 1, "book the first one"
    slot = next(s for s in store.slots.values() if not s.booked)
    proposal, err = run_tool(ses, "propose_booking", {"slot_id": slot.id, "reason": "x", "visit_type": "consult"})
    token = proposal.split('"token": "')[1].split('"')[0]
    out, err = run_tool(ses, "commit_action", {"token": token})
    assert err and "not seen" in out
    ses.turn, ses.last_patient_text = 2, "hmm, maybe later"
    out, err = run_tool(ses, "commit_action", {"token": token})
    assert err and "not an explicit confirmation" in out
    ses.last_patient_text = "yes"
    out, err = run_tool(ses, "commit_action", {"token": token})
    assert not err and store.slots[slot.id].booked
    again, err = run_tool(ses, "commit_action", {"token": token})
    assert '"idempotent_replay": true' in again  # retry does not double-book


def test_tools_require_verified_identity():
    _, _, _, ses = make()
    out, err = run_tool(ses, "get_patient_summary", {})
    assert err and "Identity not verified" in out


# ---- Sarvam language layer ---------------------------------------------------------------------------

def test_tamil_red_flag_caught_via_sarvam_translation_without_calling_claude():
    tamil = "அப்பாவுக்கு மூச்சு விட முடியவில்லை"
    sarvam = FakeSarvam(lid={tamil: "ta-IN"}, to_en={tamil: "Father can't breathe"})
    store, claude, agent, ses = make(sarvam=sarvam, phone=LAKSHMI)
    out = agent.handle(ses, tamil)
    assert claude.requests == []  # deterministic path, no model call
    assert out.startswith("<ta-IN>") and "108" in out  # localised, with English kept underneath
    assert any(t["queue"] == "clinician" and t["priority"] == "urgent" for t in store.tasks)


def test_pivot_mode_sends_english_to_claude_and_localises_reply():
    tamil = "நாளை டாக்டரை பார்க்க வேண்டும்"
    sarvam = FakeSarvam(lid={tamil: "ta-IN"}, to_en={tamil: "I need to see the doctor tomorrow"},
                        from_en={"Sure. What is your date of birth?": "நிச்சயமாக. உங்கள் பிறந்த தேதி என்ன?"})
    _, claude, agent, ses = make([reply(text("Sure. What is your date of birth?"))], sarvam=sarvam, phone=LAKSHMI)
    out = agent.handle(ses, tamil)
    assert out == "நிச்சயமாக. உங்கள் பிறந்த தேதி என்ன?"
    sent = claude.requests[0]["messages"][0]["content"]
    assert "reply_in: English" in sent[0]["text"] and "I need to see the doctor tomorrow" in sent[1]["text"]
    assert ("translate", "en-IN", "ta-IN", "modern-colloquial") in sarvam.calls


def test_translated_yes_commits():
    sarvam = FakeSarvam(lid={"ஆமாம்": "ta-IN"}, to_en={"ஆமாம்": "Yes"})
    store, _, agent, ses = make(sarvam=sarvam, phone=LAKSHMI)
    ses.patient_id, ses.turn = "PT-4", 1
    slot = next(s for s in store.slots.values() if not s.booked)
    token = run_tool(ses, "propose_booking", {"slot_id": slot.id, "reason": "review", "visit_type": "consult"})[0] \
        .split('"token": "')[1].split('"')[0]
    agent.client = ScriptedClaude([reply(call("commit_action", token=token)), reply(text("Booked."))])
    agent.handle(ses, "ஆமாம்")
    assert store.slots[slot.id].booked


def test_coverage_overstatement_blocked_and_localised():
    sarvam = FakeSarvam(lid={"kya mera cashless ho gaya?": "hi-IN"}, to_en={"kya mera cashless ho gaya?": "Is my cashless done?"})
    store, _, agent, ses = make([reply(text("Yes, your cashless is approved, nothing to pay."))], sarvam=sarvam)
    ses.patient_id = "PT-1"
    out = agent.handle(ses, "kya mera cashless ho gaya?")
    assert out.startswith("<hi-IN>I can't confirm approval myself")
    assert any(a["action"] == "outbound_blocked" for a in store.audit)
    assert any(t["queue"] == "insurance_desk" for t in store.tasks)


def test_voice_turn_uses_sarvam_stt_and_tts():
    hindi = "मेरा अपॉइंटमेंट कब है"
    sarvam = FakeSarvam(lid={hindi: "hi-IN"}, to_en={hindi: "When is my appointment"}, transcript=(hindi, "hi-IN"))
    _, _, agent, ses = make([reply(text("Please tell me your date of birth."))], sarvam=sarvam)
    out = agent.handle_voice(ses, b"RIFF....", "call.wav")
    assert out.transcript == hindi and out.audio == b"AUDIO[hi-IN]"
    assert ses.channel == "voice"


def test_stop_withdraws_marketing_immediately():
    store, claude, agent, ses = make()
    ses.patient_id = "PT-4"
    agent.handle(ses, "STOP")
    assert not store.has_consent("PT-4", "marketing_recall") and claude.requests == []
    assert store.has_consent("PT-4", "care_communication")  # care messages continue


# ---- scheduling, waitlist, engagement, insurance -------------------------------------------------------

def test_cancellation_offers_slot_to_waitlist_and_refill_counts():
    store, _, _, ses = make()
    ses.patient_id, ses.turn, ses.last_patient_text = "PT-1", 1, ""
    slot = next(s for s in store.slots.values() if s.provider_id == "DR-5" and not s.booked
                and s.start.date().isoformat() == "2026-10-29")
    tok = run_tool(ses, "propose_booking", {"slot_id": slot.id, "reason": "r", "visit_type": "consult"})[0] \
        .split('"token": "')[1].split('"')[0]
    ses.turn, ses.last_patient_text = 2, "yes"
    apt = run_tool(ses, "commit_action", {"token": tok})[0].split('"appointment_id": "')[1].split('"')[0]
    ses.turn, ses.last_patient_text = 3, ""
    tok = run_tool(ses, "propose_cancellation", {"appointment_id": apt})[0].split('"token": "')[1].split('"')[0]
    ses.turn, ses.last_patient_text = 4, "yes"
    run_tool(ses, "commit_action", {"token": tok})
    assert store.slots[slot.id].held_for == "PT-2"
    assert any(m["template"] == "waitlist_offer" and m["patient_id"] == "PT-2" for m in store.outbox)
    priya = Session(store, channel_phone="+919845022222")
    priya.patient_id, priya.turn = "PT-2", 1
    tok = run_tool(priya, "propose_booking", {"slot_id": slot.id, "reason": "thyroid review", "visit_type": "follow_up"})[0] \
        .split('"token": "')[1].split('"')[0]
    priya.turn, priya.last_patient_text = 2, "yes"
    run_tool(priya, "commit_action", {"token": tok})
    assert compute(store)["intake"]["released_slot_refill_rate_pct"] == 100.0


def test_campaigns_separate_care_from_marketing():
    store = Store()
    engine = CampaignEngine(store)
    sent = engine.run()
    by = {(m["patient_id"], m["template"]) for m in sent}
    assert ("PT-3", "post_visit_followup") in by  # paediatric follow-up, 2 days after visit
    assert ("PT-1", "care_lab_due") in by
    assert ("PT-4", "recall_diabetes_review") in by  # lapsed chronic patient with marketing opt-in
    assert not any(p == "PT-1" and t.startswith("recall") for p, t in by)  # no marketing consent
    assert not any(p == "PT-2" and t.startswith("recall") for p, t in by)  # DND on SMS
    assert all(m["purpose"] == "marketing_recall" for m in sent if m["template"].startswith("recall"))
    assert engine.run() == []  # dedupe
    store.advance(hours=24)
    assert any(m["template"] == "reminder_T-24h" for m in engine.run())


def test_followup_red_flag_answer_escalates():
    store, _, _, ses = make(phone="+919845033333")
    ses.patient_id = "PT-3"
    out, _ = run_tool(ses, "record_followup_response", {
        "appointment_id": "APT-0", "questionnaire": "followup:pediatric fever",
        "answers": [{"question": "Any fast breathing, drowsiness, rash or fits?", "answer": "yes he is very drowsy"}]})
    assert '"escalated": true' in out
    assert any(t["priority"] == "urgent" for t in store.tasks)


def test_coverage_check_is_indicative_and_flags_room_rent():
    _, _, _, ses = make()
    ses.patient_id = "PT-1"
    out, _ = run_tool(ses, "check_coverage", {"procedure": "knee arthroscopy", "estimated_cost": 180000,
                                              "room_rent_per_day": 8000})
    assert '"indicative": true' in out and "proportionate deduction" in out and "Sub-limit" in out


def test_preauth_query_pack_and_irdai_clock():
    store, _, _, ses = make()
    ses.patient_id = "PT-1"
    out, _ = run_tool(ses, "prepare_preauth_pack", {"procedure": "knee arthroscopy", "estimated_cost": 180000,
                                                    "stage": "initial", "preauth_id": "PA-1"})
    assert "mri_report" in out and '"ready_to_submit": false' in out
    pa = store.preauths["PA-1"]
    pa.documents += ["investigation_reports", "cost_estimate", "mri_report", "treatment_notes"]
    ses.turn = 1
    tok = run_tool(ses, "propose_preauth_submission", {"preauth_id": "PA-1"})[0].split('"token": "')[1].split('"')[0]
    ses.turn, ses.last_patient_text = 2, "haan ji"
    out, _ = run_tool(ses, "commit_action", {"token": tok})
    assert "NOT approved" in out
    engine = CampaignEngine(store)
    store.advance(minutes=50)
    engine.run()
    assert any("IRDAI limit" in t["summary"] for t in store.tasks)
    record_insurer_decision(store, engine, "PA-1", "approved", 150000, "MA-AP-99812")
    assert any(m["template"] == "preauth_update" and "MA-AP-99812" in m["text"] for m in store.outbox)
    assert guards.check_outbound("Your pre-auth has been approved (MA-AP-99812).",
                                 __import__("agent.tools", fromlist=["x"]).insurer_confirmed_approval(store, "PT-1")).ok


def test_claude_api_error_hands_off():
    import anthropic, httpx2

    def boom(m):
        raise anthropic.InternalServerError("down", response=httpx2.Response(500, request=httpx2.Request("POST", "https://x")),
                                            body=None)

    store, _, agent, ses = make([boom])
    out = agent.handle(ses, "I want to book")
    assert "front desk" in out and any(t["queue"] == "front_desk" for t in store.tasks)
