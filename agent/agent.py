"""Conversation orchestrator: one Session per patient encounter, across channels.

Per inbound message:
  0. (voice) Sarvam speech-to-text
  1. Sarvam language ID + translation to English (when a language layer is configured)
  2. deterministic pre-checks on the original AND the English text: opt-out, red-flag screen
  3. model turn with tools (Claude decides intent and which tool to call; tools execute)
  4. deterministic outbound check on the English draft (coverage over-statement, clinical advice)
  5. Sarvam translation into the patient's language, and (voice) Sarvam text-to-speech
History is append-only so prompt caching and thinking blocks stay valid across turns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import anthropic
from sarvamai.core.api_error import ApiError

from . import guards
from .prompts import SYSTEM_PROMPT
from .sarvam_layer import Inbound, SarvamLanguageLayer, to_sarvam, to_short
from .store import MARKETING, Store
from .tools import TOOLS, insurer_confirmed_approval, run_tool

MODEL = os.environ.get("HEALTHCARE_AGENT_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 12

SAFE_INSURANCE_REPLY = ("I can't confirm approval myself - only your insurer can, and they confirm it in writing. "
                        "I've asked our insurance desk to check and update you shortly.")
SAFE_CLINICAL_REPLY = ("That's a question for your doctor, so I've asked our care team to call you back. "
                       "If you feel worse in the meantime, please call 108 or come to our Emergency department.")
FAILURE_REPLY = ("Sorry, I'm having trouble with this right now. I've passed your message to our front desk "
                 "and someone will get back to you shortly.")


@dataclass
class Session:
    store: Store
    channel: str = "whatsapp"
    channel_phone: str | None = None
    hospital: str = "Sunrise Multispeciality Hospital"
    id: str = ""
    patient_id: str | None = None
    language: str = "en"  # short code of the language the patient is using right now
    messages: list = field(default_factory=list)
    turn: int = 0
    last_patient_text: str = ""
    last_patient_english: str = ""
    failed_verifications: int = 0
    escalated: bool = False
    handed_off: bool = False
    outbound_context: str | None = None  # the proactive message this conversation is replying to, if any

    def __post_init__(self):
        self.id = self.id or self.store.new_id("SES")

    def patient_said_yes(self) -> bool:
        return guards.is_explicit_yes(self.last_patient_text) or guards.is_explicit_yes(self.last_patient_english)

    def switch_channel(self, channel: str) -> None:
        """Same encounter, new channel (e.g. started on voice, continues on WhatsApp)."""
        self.store.log("channel_switch", session=self.id, frm=self.channel, to=channel)
        self.channel = channel


@dataclass
class VoiceReply:
    transcript: str
    text: str
    audio: bytes | None  # WAV from Sarvam TTS; None means play the text via the IVR's fallback TTS


class HealthcareAgent:
    def __init__(self, store: Store, client: anthropic.Anthropic | None = None,
                 language: SarvamLanguageLayer | None = None, effort: str = "medium"):
        self.store = store
        self.client = client or anthropic.Anthropic()
        self.language = language
        self.effort = effort

    # ---- public API -------------------------------------------------------------------------------

    def handle(self, session: Session, text: str) -> str:
        s = self.store
        session.turn += 1
        inbound = self._understand(session, text)
        session.last_patient_text, session.last_patient_english = inbound.original, inbound.english
        s.event("inbound", session=session.id, channel=session.channel, language=inbound.language,
                translated=inbound.translated)

        if guards.is_opt_out(inbound.original) or guards.is_opt_out(inbound.english):
            return self._localise(session, self._opt_out(session, inbound))

        flag = guards.screen_red_flags(inbound.original) or guards.screen_red_flags(inbound.english)
        if flag:
            s.add_task("clinician", "urgent", f"Red flag ({flag.category}): '{flag.matched}'", patient=session.patient_id,
                       phone=session.channel_phone, channel=session.channel, message=inbound.original,
                       english=inbound.english)
            s.event("red_flag_escalated", patient=session.patient_id, category=flag.category, source="screen")
            session.escalated = True
            english = guards.emergency_reply(flag, "en")
            self._append_exchange(session, inbound, english, note=f"[system: red-flag screen fired ({flag.category}); "
                                                                  "emergency message sent; clinician alerted]")
            local = self._localise(session, english)
            return local if local == english else f"{local}\n\n{english}"  # keep English so the numbers are never lost

        draft = self._model_turn(session, inbound)
        return self._localise(session, self._check_outbound(session, draft))

    def handle_voice(self, session: Session, audio: bytes, filename: str = "call.wav") -> VoiceReply:
        """One voice/IVR turn: Sarvam STT -> handle() -> Sarvam TTS."""
        if session.channel != "voice":
            session.switch_channel("voice")
        if not self.language:
            raise RuntimeError("Voice needs the Sarvam language layer.")
        try:
            transcript, lang = self.language.transcribe(audio, filename)
        except ApiError as e:
            self.store.add_task("front_desk", "high", "Voice call could not be transcribed; call the patient back",
                                phone=session.channel_phone, error=e.status_code)
            text = self._localise(session, "Sorry, I couldn't hear that clearly. A member of our team will call you back shortly.")
            return VoiceReply("", text, self.language.speak(text, session.language))
        if lang:
            session.language = to_short(lang)
        text = self.handle(session, transcript)
        return VoiceReply(transcript, text, self.language.speak(text, session.language))

    # ---- language ---------------------------------------------------------------------------------

    def _understand(self, session: Session, text: str) -> Inbound:
        if not self.language:
            return Inbound(text, to_sarvam(session.language), text, False)
        inbound = self.language.understand(text, hint=session.language)
        session.language = to_short(inbound.language)
        return inbound

    def _pivot(self, session: Session) -> bool:
        return bool(self.language and self.language.config.pivot and session.language != "en")

    def _localise(self, session: Session, english: str) -> str:
        return self.language.localise(english, session.language) if self._pivot(session) else english

    # ---- internals --------------------------------------------------------------------------------

    def _context_block(self, session: Session) -> str:
        s = self.store
        lines = [
            f"now: {s.now:%a %d %b %Y %H:%M} IST",
            f"channel: {session.channel}" + (f" (from {session.channel_phone})" if session.channel_phone else ""),
            f"verified_patient: {session.patient_id or 'none'}",
            f"patient_language: {to_sarvam(session.language)}",
        ]
        if self._pivot(session):
            lines.append("reply_in: English. The translation layer will render your reply in the patient's language, "
                         "so write plain, short sentences that translate cleanly.")
        else:
            lines.append(f"reply_in: {to_sarvam(session.language)}")
        if session.outbound_context and session.turn == 1:
            lines.append(f"this conversation is a reply to our message: \"{session.outbound_context}\"")
        return "<context>\n" + "\n".join(lines) + "\n</context>"

    def _user_content(self, session: Session, inbound: Inbound) -> list[dict]:
        if inbound.translated:
            body = (f"[patient wrote in {inbound.language}]: {inbound.original}\n"
                    f"[English translation by Sarvam]: {inbound.english}")
        else:
            body = inbound.original
        return [{"type": "text", "text": self._context_block(session)}, {"type": "text", "text": body}]

    def _append_exchange(self, session: Session, inbound: Inbound, reply: str, note: str) -> None:
        session.messages.append({"role": "user", "content": self._user_content(session, inbound)})
        session.messages.append({"role": "assistant", "content": [{"type": "text", "text": f"{note}\n{reply}"}]})

    def _opt_out(self, session: Session, inbound: Inbound) -> str:
        s = self.store
        if session.patient_id:
            s.set_consent(session.patient_id, MARKETING, False, session.channel, "STOP keyword")
        elif session.channel_phone:
            s.suppression.add(session.channel_phone)
        s.event("opt_out", session=session.id)
        reply = ("Done - you won't receive promotional messages from us. You'll still get messages about your own "
                 "appointments and care; reply STOP CARE if you want those stopped too.")
        self._append_exchange(session, inbound, reply, note="[system: opt-out processed]")
        return reply

    def _create(self, session: Session):
        return self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT.format(hospital=session.hospital),
            tools=TOOLS,
            messages=session.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def _model_turn(self, session: Session, inbound: Inbound) -> str:
        session.messages.append({"role": "user", "content": self._user_content(session, inbound)})
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = self._create(session)
            except anthropic.RateLimitError:
                return self._fail_to_human(session, "assistant rate-limited")
            except anthropic.APIStatusError as e:
                return self._fail_to_human(session, f"assistant API error {e.status_code}")
            except anthropic.APIConnectionError:
                return self._fail_to_human(session, "assistant unreachable")

            session.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return self._fail_to_human(session, "model declined")
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text").strip()

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                out, is_error = run_tool(session, block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out,
                                **({"is_error": True} if is_error else {})})
            session.messages.append({"role": "user", "content": results})
        return self._fail_to_human(session, "too many tool rounds")

    def _fail_to_human(self, session: Session, reason: str) -> str:
        self.store.add_task("front_desk", "high", f"Assistant could not complete: {reason}", patient=session.patient_id,
                            session=session.id, last_message=session.last_patient_text)
        session.handed_off = True
        self.store.event("handoff", queue="front_desk", reason=reason)
        return FAILURE_REPLY

    def _check_outbound(self, session: Session, reply: str) -> str:
        s = self.store
        english = reply
        if self.language and not self._pivot(session) and session.language != "en":
            # Native mode: the draft is in the patient's language; check an English rendering of it.
            english = self.language.translate(reply, to_sarvam(session.language), "en-IN") or reply
        verdict = guards.check_outbound(english, insurer_confirmed_approval(s, session.patient_id))
        if guards.mentions_coverage(english):
            s.log("coverage_statement", patient=session.patient_id, text=english)  # every coverage statement is kept
        if verdict.ok:
            return reply
        s.log("outbound_blocked", patient=session.patient_id, reason=verdict.reason, draft=english)
        s.event("guard_blocked", reason=verdict.reason)
        queue = "insurance_desk" if verdict.reason == "coverage_overstatement" else "clinician"
        s.add_task(queue, "high", f"Assistant reply blocked ({verdict.reason})", patient=session.patient_id, draft=english)
        safe = SAFE_INSURANCE_REPLY if verdict.reason == "coverage_overstatement" else SAFE_CLINICAL_REPLY
        # Record what the patient actually received so the next turn's context is truthful.
        session.messages.append({"role": "user", "content": [{"type": "text", "text":
            f"<context>\n[system: your previous reply was withheld by the {verdict.reason} guard. "
            f"The patient was sent instead: \"{safe}\"]\n</context>"}]})
        session.messages.append({"role": "assistant", "content": [{"type": "text", "text": "Understood."}]})
        return safe
