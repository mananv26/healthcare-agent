"""Web server for the demo deployment.

  GET  /                 chat UI (static/index.html)
  POST /api/chat         {"session_id", "phone", "channel", "message"} -> {"reply"}
  POST /api/voice        multipart: session_id, phone, audio -> {"transcript", "reply", "audio_b64"}
  POST /api/tick         {"minutes"} -> advance the demo clock and run campaigns
  GET  /api/state        tasks, outbox, metrics, clock
  POST /api/reset        fresh demo data
  GET  /healthz          liveness probe

This serves SEEDED DEMO DATA ONLY. State is in memory and resets on restart. Do not enter real patient
information. Set DEMO_ACCESS_CODE so strangers can't spend your API credits.

Run locally:  uvicorn server:app --reload
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sarvamai.core.api_error import ApiError

from agent import CampaignEngine, HealthcareAgent, SarvamLanguageLayer, Session, Store
from agent.metrics import compute

STATIC = Path(__file__).parent / "static"
ACCESS_CODE = os.environ.get("DEMO_ACCESS_CODE")

app = FastAPI(title="Healthcare Agent demo", docs_url=None, redoc_url=None)
_lock = threading.Lock()  # one shared demo store; serialise turns so state stays consistent


class Demo:
    def __init__(self):
        self.store = Store()
        self.language = SarvamLanguageLayer() if os.environ.get("SARVAM_API_KEY") else None
        self.engine = CampaignEngine(self.store, self.language)
        self.sessions: dict[str, Session] = {}
        self._agent: HealthcareAgent | None = None

    @property
    def agent(self) -> HealthcareAgent:
        if self._agent is None:
            self._agent = HealthcareAgent(self.store, language=self.language,
                                          effort=os.environ.get("AGENT_EFFORT", "medium"))
        return self._agent

    def run(self, fn, *args):
        """Run an agent turn. Emergency and opt-out paths need no model, so they work even without a Claude key."""
        try:
            return fn(*args)
        except TypeError as e:  # the Anthropic SDK raises TypeError when no credentials resolve
            if "authentication" not in str(e):
                raise
            raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server.")

    def session(self, session_id: str, phone: str, channel: str) -> Session:
        ses = self.sessions.get(session_id)
        if ses is None or ses.channel_phone != phone:
            ses = Session(self.store, channel=channel, channel_phone=phone)
            self.sessions[session_id] = ses
        elif ses.channel != channel:
            ses.switch_channel(channel)
        return ses


demo = Demo()


def check_access(x_access_code: str | None = Header(default=None)) -> None:
    if ACCESS_CODE and not (x_access_code and secrets.compare_digest(x_access_code, ACCESS_CODE)):
        raise HTTPException(401, "Access code required.")


class ChatIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    phone: str = Field(min_length=10, max_length=16)
    channel: str = Field(default="whatsapp", pattern="^(whatsapp|sms|web|kiosk|voice)$")
    message: str = Field(min_length=1, max_length=2000)


class TickIn(BaseModel):
    minutes: int = Field(default=60, ge=1, le=60 * 24 * 7)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "sarvam": demo.language is not None, "access_code": bool(ACCESS_CODE),
            "claude_key": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))}


@app.post("/api/chat", dependencies=[Depends(check_access)])
def chat(body: ChatIn):
    with _lock:
        ses = demo.session(body.session_id, body.phone, body.channel)
        reply = demo.run(demo.agent.handle, ses, body.message)
        return {"reply": reply, "language": ses.language, "verified": ses.patient_id,
                "escalated": ses.escalated, "handed_off": ses.handed_off}


@app.post("/api/voice", dependencies=[Depends(check_access)])
def voice(session_id: str = Form(...), phone: str = Form(...), audio: UploadFile = File(...)):
    if demo.language is None:
        raise HTTPException(503, "Voice needs SARVAM_API_KEY on the server.")
    data = audio.file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Audio too large (max 10 MB).")
    with _lock:
        ses = demo.session(session_id, phone, "voice")
        try:
            out = demo.run(demo.agent.handle_voice, ses, data, audio.filename or "voice.webm")
        except ApiError as e:
            raise HTTPException(502, f"Sarvam error {e.status_code}")
        return {"transcript": out.transcript, "reply": out.text, "language": ses.language,
                "audio_b64": base64.b64encode(out.audio).decode() if out.audio else None}


@app.post("/api/tick", dependencies=[Depends(check_access)])
def tick(body: TickIn):
    with _lock:
        demo.store.advance(minutes=body.minutes)
        sent = demo.engine.run()
        return {"now": demo.store.now.isoformat(timespec="minutes"),
                "sent": [{k: m[k] for k in ("patient_id", "template", "purpose", "channel", "text")} for m in sent]}


@app.get("/api/state", dependencies=[Depends(check_access)])
def state():
    with _lock:
        s = demo.store
        return {"now": s.now.isoformat(timespec="minutes"), "tasks": s.tasks[-30:], "outbox": s.outbox[-30:],
                "metrics": compute(s)}


@app.post("/api/reset", dependencies=[Depends(check_access)])
def reset():
    global demo
    with _lock:
        demo = Demo()
    return {"reset": True}
