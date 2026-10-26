"""Indic language and voice layer backed by Sarvam AI.

Division of labour:
  Sarvam  - language identification, Indic <-> English translation, speech-to-text (voice/IVR),
            text-to-speech for spoken replies.
  Claude  - understanding intent, deciding which tool to call, composing the reply.

In "pivot" mode (default) Claude reasons and replies in English and Sarvam translates the reply into the
patient's language. That keeps the deterministic guards (red-flag screen, coverage and clinical-advice
checks, explicit-yes gate) working on English text for every language the hospital serves, and puts
translation quality in a model built for Indian languages. The red-flag screen still also runs on the
patient's original words.

Every call degrades gracefully: if Sarvam is unreachable the agent keeps working (Claude handles the
language natively) and the failure is logged; voice falls back to a human callback.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

log = logging.getLogger(__name__)

# Patient record short codes -> Sarvam BCP-47 codes.
SARVAM_CODES = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "gu": "gu-IN", "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN",
    "od": "od-IN", "or": "od-IN", "pa": "pa-IN", "ta": "ta-IN", "te": "te-IN", "as": "as-IN", "ur": "ur-IN",
    "ne": "ne-IN", "mai": "mai-IN", "kok": "kok-IN",
}
TTS_LANGUAGES = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}


def to_sarvam(code: str) -> str:
    return code if "-" in code else SARVAM_CODES.get(code, "en-IN")


def to_short(code: str | None) -> str:
    return (code or "en-IN").split("-")[0]


@dataclass
class Inbound:
    original: str
    language: str  # Sarvam code, e.g. "ta-IN"
    english: str  # equals original when the patient wrote English or translation was unavailable
    translated: bool


@dataclass
class SarvamConfig:
    translate_model: str = "mayura:v1"  # supports mode= for patient-friendly register
    outbound_mode: str = "modern-colloquial"  # formal | modern-colloquial | classic-colloquial | code-mixed
    stt_model: str = "saaras:v3"
    tts_model: str = "bulbul:v3"
    tts_speaker: str | None = None  # None = model default voice; set per hospital brand
    tts_pace: float = 0.9  # slightly slower for older and voice-first patients
    pivot: bool = True  # Claude works in English; Sarvam translates in and out


class SarvamLanguageLayer:
    def __init__(self, client: SarvamAI | None = None, config: SarvamConfig | None = None):
        self.client = client or SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
        self.config = config or SarvamConfig()
        self.failures = 0

    # ---- text -------------------------------------------------------------------------------------

    def detect(self, text: str) -> str | None:
        if text.isascii() and not _looks_romanised_indic(text):
            return "en-IN"
        try:
            return self.client.text.identify_language(input=text).language_code
        except ApiError as e:
            self._failed("identify_language", e)
            return None

    def understand(self, text: str, hint: str | None = None) -> Inbound:
        """Detect the language and produce an English rendering for the model and the guards."""
        lang = self.detect(text) or (to_sarvam(hint) if hint else "en-IN")
        if lang == "en-IN":
            return Inbound(text, lang, text, False)
        english = self.translate(text, lang, "en-IN", mode="code-mixed")
        return Inbound(text, lang, english or text, english is not None)

    def translate(self, text: str, source: str, target: str, mode: str | None = None) -> str | None:
        if source == target or not text.strip():
            return text
        try:
            kwargs = {"model": self.config.translate_model}
            if self.config.translate_model.startswith("mayura") and mode:
                kwargs["mode"] = mode
            return self.client.text.translate(input=text, source_language_code=source, target_language_code=target,
                                              **kwargs).translated_text
        except ApiError as e:
            self._failed("translate", e)
            return None

    def localise(self, english: str, language: str) -> str:
        """Translate an outbound English message into the patient's language, keeping the English if it fails."""
        lang = to_sarvam(language)
        if lang == "en-IN":
            return english
        return self.translate(english, "en-IN", lang, mode=self.config.outbound_mode) or english

    # ---- voice ------------------------------------------------------------------------------------

    def transcribe(self, audio: bytes, filename: str = "call.wav", language: str | None = None) -> tuple[str, str | None]:
        """Speech -> (transcript in the spoken language, detected language code)."""
        kwargs = {"model": self.config.stt_model, "mode": "transcribe"}
        if language:
            kwargs["language_code"] = to_sarvam(language)
        r = self.client.speech_to_text.transcribe(file=(filename, audio), **kwargs)
        return r.transcript, r.language_code

    def speak(self, text: str, language: str) -> bytes | None:
        lang = to_sarvam(language)
        if lang not in TTS_LANGUAGES:
            lang = "en-IN"
        kwargs = {"model": self.config.tts_model, "pace": self.config.tts_pace}
        if self.config.tts_speaker:
            kwargs["speaker"] = self.config.tts_speaker
        try:
            r = self.client.text_to_speech.convert(text=text[:2500], language_code=lang, **kwargs)
            return b"".join(base64.b64decode(chunk) for chunk in r.audios)
        except ApiError as e:
            self._failed("text_to_speech", e)
            return None

    def _failed(self, op: str, e: ApiError) -> None:
        self.failures += 1
        log.warning("sarvam %s failed: %s %s", op, e.status_code, e.body)


_HINGLISH = {"hai", "nahi", "nahin", "mujhe", "kal", "aaj", "kya", "kab", "dard", "doctor sahab", "ji", "haan",
             "karna", "chahiye", "mera", "meri", "hoga", "kaise", "theek", "bhai", "saans", "khoon", "bukhar"}


def _looks_romanised_indic(text: str) -> bool:
    words = set(text.lower().split())
    return len(words & _HINGLISH) >= 2
