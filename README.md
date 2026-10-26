# Healthcare Agent

A conversational agent for Indian hospitals that covers patient intake and scheduling, proactive
engagement, and insurance workflows (cashless verification, pre-authorisation, claims). **Claude** handles
reasoning and tool use, and **Sarvam AI** handles Indian-language speech and text.

The design spec is in [`docs/conversation_model.md`](docs/conversation_model.md).

```
agent/
  agent.py         orchestrator: Session, per-turn pipeline, voice entry point
  sarvam_layer.py  Sarvam AI: language ID, translation (mayura), STT (saaras:v3), TTS (bulbul:v3)
  guards.py        deterministic red-flag screen, explicit-yes gate, opt-out, outbound coverage/clinical guard
  tools.py         23 tools; writes are propose -> read-back -> commit with idempotency keys
  campaigns.py     reminders, follow-ups, care-plan nudges, consent-gated recall, IRDAI clock watcher
  store.py         in-memory HIS/EMR/billing/TPA/consent ledger (replace with real adapters)
  metrics.py       the brief's metrics from the event log
  prompts.py       system prompt
cli.py             interactive demo
tests/             offline tests with scripted Claude and Sarvam fakes
```

## Run

```bash
pip install anthropic sarvamai pytest
export ANTHROPIC_API_KEY=...        # or `ant auth login`
export SARVAM_API_KEY=...           # from dashboard.sarvam.ai
python cli.py                       # chat as a seeded patient (DOB 1979-04-12)
python cli.py --phone +919845044444 # Tamil-speaking patient (DOB 1958-02-17)
python cli.py --voice sample.wav    # one voice turn: Sarvam STT -> agent -> Sarvam TTS (writes reply.wav)
python cli.py --campaigns           # what the proactive engine would send now
python -m pytest -q tests           # offline, no keys needed
```

Inside the chat, `/tick 60` advances the clock and runs campaigns. `/tasks`, `/outbox`, `/audit` and
`/metrics` show the internal state.

## Configuration

- `HEALTHCARE_AGENT_MODEL` defaults to `claude-opus-5-5` at `medium` effort with adaptive thinking.
  Server-side refusal fallback (`fallbacks: "default"`) is on, so a declined turn is retried on another
  model, and a final refusal hands off to the front desk.
- `SarvamConfig`: `pivot` (default on), `translate_model`, `outbound_mode` (`modern-colloquial`),
  `stt_model`, `tts_model`, `tts_speaker`, `tts_pace`.
