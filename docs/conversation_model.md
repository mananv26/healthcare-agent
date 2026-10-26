# Healthcare Agent: Conversation Model

This document specifies how the agent converses: who says what, when, in which language, and what the
system does at each step. It implements the three pillars and the design rules in the *Healthcare Agent
Product Brief* (Oct 2026). The code in `agent/` follows this spec.

## 1. Layers

```
 Patient ──► Channel ───────► Language layer ──────► Guards (in) ──► Reasoning ──► Tools ──► Systems of record
          WhatsApp/SMS      Sarvam AI               deterministic     Claude        validated,   HIS · EMR · billing
          Voice/IVR         - LID (text-lid)        - opt-out         Opus 5.5      idempotent,  TPA portal / NHCX
          Web/kiosk         - STT (saaras:v3)       - red-flag        tool use      logged       consent ledger
                            - translate (mayura)      screen                                     messaging gateway
 Patient ◄── Channel ◄──── - TTS (bulbul:v3) ◄──── Guards (out) ◄───────┘
                                                   - coverage over-statement
                                                   - clinical advice
```

| Concern | Owner | Why |
|---|---|---|
| Hearing and speaking Indian languages | **Sarvam AI** | Built for Indic speech and text, including code-mixed Hinglish. The brief names language/dialect quality as a top risk. |
| Understanding intent, choosing tools, composing replies | **Claude** | Multi-step tool orchestration across booking, consent and insurance. |
| Anything that changes a record or decides safety | **Deterministic code** | "The model proposes, deterministic tools execute." |

**Pivot mode (default).** Sarvam translates the patient's message to English. Claude reasons and replies in
English, and Sarvam renders the reply in the patient's language using the `modern-colloquial` register. The
guards always inspect English, so one set of rules covers every language. Claude sees both the original
text and the translation, and it asks the patient whenever they disagree on a date, a yes/no, or a symptom.
Native mode (`SarvamConfig(pivot=False)`) lets Claude reply directly in the patient's language. Sarvam then
back-translates the draft so the guards still run.

## 2. Conversation state

A **Session** is one encounter, held across channels. A patient can start on an IVR call and finish on
WhatsApp, and memory is scoped to that encounter.

| Field | Meaning |
|---|---|
| `patient_id` | Set only by `verify_identity` (phone + DOB) or by a committed registration |
| `language` | Detected per message by Sarvam LID. It follows the patient's actual language, not the record |
| `channel`, `channel_phone` | Controls reply length and format: voice is short, with no links or markdown |
| `turn`, `last_patient_text`, `last_patient_english` | Inputs to the confirmation gate |
| `outbound_context` | The proactive message that this conversation is a reply to |
| `escalated`, `handed_off` | Terminal flags for the encounter |

### Dialogue states

```
            ┌──────────── red flag (any state) ───────────► EMERGENCY ─► clinician task, 108 script, end
            │
 GREETING ─► IDENTIFY ─► SERVE ─► PROPOSE ─► AWAIT_YES ─► COMMIT ─► CONFIRM ─► SERVE … ─► CLOSE
               │  ▲         │         ▲          │ no/changes
               │  └ retry   │         └──────────┘
               │ 2 fails    └─ cannot do / asked for human / 2 misunderstandings ─► HANDOFF (summary to queue)
               └────────────────────────────────────────────────────────────────► HANDOFF
```

- **IDENTIFY** has to finish before any record is read. For a child, the guardian verifies with the child's
  DOB. If DOB and number don't match twice, the conversation hands off to the front desk.
- **PROPOSE → AWAIT_YES → COMMIT** is enforced in code (`commit_action`). The read-back has to be shown in an
  earlier turn, and the patient's next message has to be an unqualified yes, in either the original
  language or the Sarvam translation (`haan ji`, `ஆமாம்`, `yes`). "Yes but change the time" does not count
  as a yes. Every commit carries an idempotency key, so a retry can't double-book.
- **EMERGENCY** short-circuits before the model is called. The red-flag screen runs on both the original
  words and their English translation.

## 3. Intents and the tools behind them

| Pillar | Intent | Tools | Commit? |
|---|---|---|---|
| Intake & scheduling | register as a new patient | `propose_registration` (DPDP notice first; ABHA optional) | yes |
| | book / reschedule / cancel | `search_slots` → `propose_booking` / `propose_reschedule` / `propose_cancellation` | yes |
| | earlier slot wanted | `propose_waitlist`; a cancellation auto-offers the slot to the waitlist with a 2h hold | yes |
| | share old reports | `list_documents` → `extract_document` → read back → `save_confirmed_document_fields` | patient confirms each field |
| | prep instructions | `get_approved_content("prep:…")` only | — |
| Engagement | answer a follow-up check-in | `record_followup_response`, with red-flag answers escalated automatically | — |
| | stop messages / change consent | `update_consent` (immediate), or the `STOP` keyword (handled before the model) | no (withdrawal is one step) |
| Insurance | "Will insurance cover this?" | `check_coverage` (indicative: validity, empanelment, waiting period, sub-limit, room-rent cap, co-pay) | — |
| | pre-auth / claim status | `get_insurance_status` (with IRDAI clock) | — |
| | respond to TPA query / submit | `prepare_preauth_pack` → `request_document_upload` → `propose_preauth_submission` | yes, then goes to the human insurance desk |
| | claim documents | `claim_documentation_check` | — |
| Any | wants a person / can't be helped | `handoff_to_human` (queue + summary) | — |
| Any | possible emergency | `escalate_red_flag` (model path) / screen (code path) | — |

## 4. Proactive flows (agent speaks first)

The `CampaignEngine` runs on a schedule. Code, not the model, decides who receives which approved
template. Sarvam localises the template, and a reply opens a Session with the outbound message as context.

| Motion | Trigger | Consent purpose | Content |
|---|---|---|---|
| Reminder T-24h | booked appointment 2–26h away | `care_communication` | time, place, approved prep text, 1/2/3 to confirm/reschedule/cancel |
| Reminder T-2h | ≤2.5h away | care | arrive 15 min early |
| Post-visit follow-up | 1–2 days after a visit | care | clinician-authored questionnaire; red-flag answers → urgent clinician task |
| Care-plan nudges | lab due / refill due within 3 days | care | "as planned by your doctor", reply BOOK / REFILL |
| Re-engagement | chronic patient lapsed > 6 months | **`marketing_recall`** | labelled promotional with sender ID, STOP footer; skipped on DND for SMS/voice, on the suppression list, or without opt-in |
| Pre-auth updates | insurer decision recorded by desk or NHCX | care | approved/partial amount **with insurer reference**, query details, or rejection + callback |
| IRDAI clock | 75% of 1h (initial) / 3h (final discharge) elapsed | — (staff) | urgent tasks to insurance desk and ward; breach logged for billing |

Care and marketing never share a message. `STOP` withdraws marketing consent on every channel straight away,
and care messages carry on unless the patient also withdraws those.

## 5. Guardrails (brief section 5, enforced in code)

1. **Confirm before commit.** Every state change uses propose → read-back → explicit yes → commit, with an
   idempotency key.
2. **Retrieve, never recall.** Clinical and preparation facts come only from `get_approved_content`. With
   no approved content, the agent hands off to a clinician. The outbound guard blocks dosing and
   diagnostic language.
3. **Escalate on red flags.** The multilingual screen runs on the original text plus the Sarvam
   translation, before the model. The agent gives no triage, sends an emergency script (108 /
   Tele-MANAS 14416), and opens an urgent clinician task. The English text is kept under the localised
   reply so the numbers are never mistranslated.
4. **Human handoff is a feature.** It triggers after two failed verifications or misunderstandings, a
   request for a person, any commitment the agent can't make, model refusals, API errors, or tool loops.
   Each handoff goes to a named queue with a summary.
5. **No coverage over-statement.** "Approved", "covered" and "nothing to pay" can't reach the patient
   unless an insurer decision with a written reference exists. If they appear without one, the reply is
   withheld, a safe message goes out, and the insurance desk gets a task. Every coverage statement is
   logged.

## 6. Sample conversations

**A. Hinglish booking, WhatsApp (Ravi, verified by DOB)**
```
Patient: mujhe ortho doctor se milna hai, ghutne ka MRI dikhana hai
  [Sarvam LID hi-IN → "I want to see the ortho doctor, need to show the knee MRI"]
Agent:   Zaroor. Pehle verify kar lein - aapki date of birth kya hai?
Patient: 12 April 1979
  [verify_identity ✓, get_patient_summary: already booked with Dr. Shah on Wed 28 Oct, 9:00 AM]
Agent:   Aapka Dr. Vikram Shah ke saath Wed 28 Oct, 9:00 AM ka appointment pehle se hai (OPD 12).
         Kya aap ise rakhna chahenge ya badalna hai?
Patient: Thursday shaam ko kar do
  [search_slots → propose_reschedule → read-back]
Agent:   Aapka appointment Wed 28 Oct, 9:00 AM se badal kar Thu 29 Oct, 5:00 PM, Dr. Vikram Shah ke saath kar dein?
         Confirm karne ke liye HAAN likhiye.
Patient: haan ji
  [commit_action ✓ (gate passed on "haan ji"); released slot offered to waitlist]
Agent:   Ho gaya - Thu 29 Oct, 5:00 PM, OPD 12. Ref APT-1. MRI films aur reports saath laayein.
```

**B. Tamil voice call, red flag (Lakshmi)**
```
Caller (audio, Tamil): "அப்பாவுக்கு மூச்சு விட முடியவில்லை"
  [Sarvam saaras:v3 transcript, ta-IN → English "Father can't breathe" → red-flag screen: respiratory]
Agent (bulbul:v3, Tamil): இது அவசரநிலையாக இருக்கலாம். உடனே 108 ஐ அழைக்கவும்…
  (+ English text in the SMS copy) · urgent clinician task · no model call, no triage questions
```

**C. Insurance status. The guard holds the line.**
```
Patient: kya mera knee surgery ka cashless approve ho gaya?
  [get_insurance_status: PA-1 query_raised – TPA wants MRI report + treatment notes]
Agent:   Abhi approve nahi hua hai. Insurance company ne MRI report aur doctor ke treatment notes maange hain.
         Main aapko MRI report upload karne ka link bhej deta hoon; treatment notes hum hospital se bhejenge.
  ... later, the desk records insurer decision MA-AP-99812 ...
Proactive: Star Health ne knee arthroscopy request Rs 1,50,000 ke liye approve kar di hai, reference MA-AP-99812.
           Co-pay aur non-medical items hospital mein dene honge.
```
If the model had said "approved" before the insurer decision, the outbound guard would have replaced the
message and alerted the insurance desk.

**D. Post-visit follow-up, guardian (Arjun, paediatrics)**
```
Proactive: Checking on Arjun Mehta's recovery… (1) Is the fever lower? … (3) Any fast breathing, drowsiness, rash or fits?
Guardian:  fever kam hai but bahut drowsy hai aaj
  [record_followup_response → red flag "drowsy" → urgent clinician task]
Agent:     Thank you for telling us. A doctor from our team will call you shortly. If he gets worse, call 108 or
           come straight to Emergency.
```

## 7. Mapping to the brief's metrics

`agent/metrics.py` computes these from the event log: digital intake completion, no-show rate,
released-slot refill rate, follow-up contact achieved, recall response, pre-auth turnaround (median),
discharges authorised within 3h, IRDAI breaches, first-pass claim acceptance, claim gaps, containment, red-flag
escalations and guard blocks.

## 8. What is real, assisted, or stubbed in this build

| Component | Status |
|---|---|
| Conversation loop, tools, guards, campaigns, metrics | Implemented and tested (27 offline tests) |
| Sarvam LID / translate / STT / TTS | Implemented against the `sarvamai` SDK (v0.1.39). Not yet exercised live from this environment: the sandbox's network policy blocks api.sarvam.ai |
| HIS / EMR / billing / TPA / NHCX | In-memory `Store` with the same interface the adapters need to provide |
| Insurer decisions | Human-in-the-loop: the insurance desk submits on the portal / NHCX and records the decision (`record_insurer_decision`), as phase 2 of the brief recommends |
| Document extraction | Simple parser over seeded OCR text; in production, OCR/vision + Sarvam document digitisation, still with patient confirmation per field |

## 9. Open decisions (from brief section 10) that change this model

- **Primary channel.** With voice-first, Sarvam STT/TTS latency and `bulbul` voice choice become the
  critical path. With WhatsApp-first, out-of-session template approval governs proactive flows.
- **Recall: care or marketing?** This build treats recall as marketing (separate opt-in). Reclassifying it
  is a single change in `campaigns.re_engagement`, but it is a legal decision.
- **Pivot vs native per language.** Evaluate per language with native speakers. Pivot is the safer default
  for languages where guard coverage matters most.
