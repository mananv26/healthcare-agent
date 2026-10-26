"""Interactive demo.

  python cli.py                         chat as Ravi Kumar (WhatsApp, +919845011111)
  python cli.py --phone +919845044444   chat as Lakshmi (Tamil, voice-first) - try writing in Tamil
  python cli.py --voice reply.wav       one voice turn from an audio file (Sarvam STT -> agent -> Sarvam TTS)
  python cli.py --campaigns             run the proactive engine once and print what would be sent
  python cli.py --no-sarvam             Claude only, no Sarvam language layer

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) and SARVAM_API_KEY.
Commands inside the chat: /tick <minutes>  advance the clock and run campaigns
                          /tasks  /outbox  /audit  /metrics  /quit
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent import CampaignEngine, HealthcareAgent, SarvamLanguageLayer, Session, Store
from agent.metrics import compute


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="+919845011111")
    ap.add_argument("--channel", default="whatsapp", choices=["whatsapp", "sms", "voice", "web", "kiosk"])
    ap.add_argument("--voice", help="WAV/MP3 file for one voice turn")
    ap.add_argument("--campaigns", action="store_true")
    ap.add_argument("--no-sarvam", action="store_true")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    args = ap.parse_args()

    store = Store()
    language = None if args.no_sarvam or not os.environ.get("SARVAM_API_KEY") else SarvamLanguageLayer()
    if language is None and not args.no_sarvam:
        print("(SARVAM_API_KEY not set - running without the Sarvam language layer)", file=sys.stderr)
    engine = CampaignEngine(store, language)

    if args.campaigns:
        for m in engine.run():
            print(f"[{m['purpose']}/{m['template']}] -> {m['to']} ({m['channel']})\n  {m['text']}\n")
        return

    agent = HealthcareAgent(store, language=language, effort=args.effort)
    session = Session(store, channel="voice" if args.voice else args.channel, channel_phone=args.phone)

    if args.voice:
        with open(args.voice, "rb") as f:
            out = agent.handle_voice(session, f.read(), os.path.basename(args.voice))
        print(f"heard: {out.transcript}\nreply: {out.text}")
        if out.audio:
            with open("reply.wav", "wb") as f:
                f.write(out.audio)
            print("audio: reply.wav")
        return

    print(f"Chatting as {args.phone} on {args.channel}. /quit to exit.\n")
    while True:
        try:
            text = input("patient> ").strip()
        except EOFError:
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text.startswith("/tick"):
            minutes = int(text.split()[1]) if len(text.split()) > 1 else 60
            store.advance(minutes=minutes)
            for m in engine.run():
                print(f"  [outbound {m['template']}] {m['text']}")
            print(f"  clock: {store.now:%a %d %b %H:%M}")
            continue
        if text in ("/tasks", "/outbox", "/audit"):
            data = {"/tasks": store.tasks, "/outbox": store.outbox, "/audit": store.audit[-20:]}[text]
            print(json.dumps(data, indent=2, default=str))
            continue
        if text == "/metrics":
            print(json.dumps(compute(store), indent=2))
            continue
        print(f"agent> {agent.handle(session, text)}\n")


if __name__ == "__main__":
    main()
