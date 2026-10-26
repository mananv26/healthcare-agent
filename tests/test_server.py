import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import server
from agent import HealthcareAgent
from fakes import ScriptedClaude, reply, text


def client(monkeypatch, code=None):
    monkeypatch.setattr(server, "ACCESS_CODE", code)
    server.demo = server.Demo()
    return TestClient(server.app)


def test_index_and_health(monkeypatch):
    c = client(monkeypatch)
    assert "Healthcare Agent" in c.get("/").text
    assert c.get("/healthz").json()["ok"]


def test_chat_with_fake_claude(monkeypatch):
    c = client(monkeypatch)
    server.demo._agent = HealthcareAgent(server.demo.store, client=ScriptedClaude([reply(text("Hello! Your date of birth?"))]))
    r = c.post("/api/chat", json={"session_id": "abcdefgh12", "phone": "+919845011111", "message": "hi"})
    assert r.status_code == 200 and r.json()["reply"] == "Hello! Your date of birth?"


def test_red_flag_needs_no_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = client(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = c.post("/api/chat", json={"session_id": "abcdefgh12", "phone": "+919845011111", "message": "severe chest pain"})
    assert r.json()["escalated"] and "108" in r.json()["reply"]
    assert any(t["priority"] == "urgent" for t in c.get("/api/state").json()["tasks"])


def test_missing_key_is_503(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    c = client(monkeypatch)
    r = c.post("/api/chat", json={"session_id": "abcdefgh12", "phone": "+919845011111", "message": "hi"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_access_code_and_tick(monkeypatch):
    c = client(monkeypatch, code="letmein")
    assert c.get("/api/state").status_code == 401
    r = c.post("/api/tick", json={"minutes": 60}, headers={"X-Access-Code": "letmein"})
    assert r.status_code == 200 and len(r.json()["sent"]) >= 3
