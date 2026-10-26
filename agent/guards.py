"""Deterministic guardrails that sit outside the model.

The brief's rule is "the model proposes, deterministic tools execute". These checks run in plain code
on every inbound and outbound message, so a model mistake cannot bypass them:

  screen_red_flags      inbound   -> stop the conversation and route to emergency / clinician, no triage
  is_explicit_yes       inbound   -> gate for committing any proposed state change
  is_opt_out            inbound   -> STOP / unsubscribe honoured immediately on every channel
  check_outbound        outbound  -> block coverage over-statements and clinical advice before sending
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# English, Hinglish and a few common Hindi/Tamil phrasings. Production needs a per-language list
# reviewed by clinicians and native speakers, plus evaluation (brief section 9: language risk).
RED_FLAGS: dict[str, list[str]] = {
    "cardiac": [r"chest pain", r"chest (is )?(tight|heavy|pressure)", r"seene? (me|mein) dard", r"heart attack",
                r"நெஞ்சு வலி", r"सीने में दर्द"],
    "respiratory": [r"can'?t breathe", r"cannot breathe", r"breathless", r"short(ness)? of breath",
                    r"saans (nahi|nahin|phool|lene)", r"सांस (नहीं|फूल)", r"fast breathing", r"turning blue"],
    "bleeding": [r"bleeding (a lot|heavily|won'?t stop|nonstop)", r"heavy bleeding", r"vomit(ing)? blood",
                 r"khoon (beh|aa) raha", r"blood in (vomit|stool)"],
    "neuro": [r"unconscious", r"fainted", r"behosh", r"seizure", r"\bfits?\b", r"convulsion", r"face (is )?droop",
              r"slurred speech", r"can'?t move (my|his|her) (arm|leg)", r"stroke"],
    "self_harm": [r"kill (my|him|her)self", r"suicid", r"end (my|it) (life|all)", r"want to die", r"marna chahta",
                  r"marna chahti", r"jeena nahi", r"self[- ]harm", r"hurt myself"],
    "pediatric": [r"baby (is )?not breathing", r"child (is )?(limp|unresponsive|not waking)", r"not feeding at all",
                  r"newborn.*(fever|blue|yellow)"],
    "allergic": [r"throat (is )?(closing|swelling)", r"anaphyla", r"face swelling"],
}
_RED_FLAG_RE = {cat: re.compile("|".join(pats), re.I) for cat, pats in RED_FLAGS.items()}
_NEGATED = re.compile(r"\b(no|not|without|never|nahi|nahin)\b[^.,;!?]{0,20}$", re.I)

EMERGENCY_NUMBER = "108"
HOSPITAL_EMERGENCY_LINE = "+91-80-4000-0000"


@dataclass
class RedFlag:
    category: str
    matched: str


def screen_red_flags(text: str) -> RedFlag | None:
    """Return the first red flag in a patient message, ignoring simple negations ("no chest pain")."""
    for cat, rx in _RED_FLAG_RE.items():
        for m in rx.finditer(text):
            if _NEGATED.search(text[: m.start()]):
                continue
            return RedFlag(cat, m.group(0))
    return None


def emergency_reply(flag: RedFlag, language: str = "en") -> str:
    if flag.category == "self_harm":
        en = ("I'm really sorry you're feeling this way. You don't have to go through this alone. "
              "Please call Tele-MANAS on 14416 (free, 24x7) or 112 if you are in immediate danger. "
              "I've also asked a member of our care team to call you right now.")
    else:
        en = ("This could be an emergency. Please call " + EMERGENCY_NUMBER + " for an ambulance now, or come "
              "straight to our Emergency department. Our emergency line is " + HOSPITAL_EMERGENCY_LINE + ". "
              "I've alerted our clinical team and someone will call you immediately.")
    if language == "hi":
        return en + "\n\n(यह आपातकाल हो सकता है। कृपया तुरंत " + EMERGENCY_NUMBER + " पर कॉल करें या सीधे इमरजेंसी विभाग आएं।)"
    return en


_YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|y|ok(ay)?|sure|confirm(ed)?|correct|go ahead|please do|book it|do it|"
    r"haan|haa|han|ha ji|haan ji|ji|ji haan|theek hai|thik hai|sahi hai|kar do|aamaa|seri|हाँ|हां|जी)\b",
    re.I,
)
_NO_QUALIFIER = re.compile(r"\b(no|not|don'?t|wait|but|change|instead|nahi|nahin|mat|ruko)\b", re.I)


def is_explicit_yes(text: str) -> bool:
    """True only for an unqualified affirmative. "yes but change the time" is not a yes."""
    return bool(_YES.search(text)) and not _NO_QUALIFIER.search(text)


def is_opt_out(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(stop|unsubscribe|stop all|opt out|band karo)\s*[.!]?\s*", text, re.I))


# Phrases that assert an insurer decision. Allowed only when an approved pre-auth with an insurer
# reference exists for the patient (brief section 3.3 and risk table: "agent overstates coverage").
_COVERAGE_ASSERTION = re.compile(
    r"\b(is|are|has been|have been|got|been|was)\s+(fully\s+|now\s+)?(approved|sanctioned|cleared)\b|"
    r"\byou(?:'re| are)\s+(fully\s+)?covered\b|\b(no|nothing) (need )?to pay\b|\bguaranteed\b|\bwill be covered\b|"
    r"\bcashless is confirmed\b",
    re.I,
)
# Dosing / diagnostic language the agent must never generate (Telemedicine Practice Guidelines 2020).
_CLINICAL_ADVICE = re.compile(
    r"\b(take|increase|decrease|reduce|double|skip|stop taking)\b[^.]{0,30}\b\d+\s?(mg|ml|tablets?|units)\b|"
    r"\byou (probably |likely |might )?have (a |an )?(infection|fracture|diabetes|dengue|covid|pneumonia|heart)|"
    r"\bit'?s (probably|likely) (just |nothing|a )",
    re.I,
)


@dataclass
class OutboundVerdict:
    ok: bool
    reason: str = ""


def check_outbound(text: str, insurer_confirmed_approval: bool) -> OutboundVerdict:
    if _COVERAGE_ASSERTION.search(text) and not insurer_confirmed_approval:
        return OutboundVerdict(False, "coverage_overstatement")
    if _CLINICAL_ADVICE.search(text):
        return OutboundVerdict(False, "clinical_advice")
    return OutboundVerdict(True)


def mentions_coverage(text: str) -> bool:
    return bool(re.search(r"\b(cover|covered|coverage|cashless|pre-?auth|claim|insur|co-?pay|room rent)", text, re.I))
