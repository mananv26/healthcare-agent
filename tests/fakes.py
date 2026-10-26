"""Offline stand-ins for the Anthropic and Sarvam clients."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace as NS


def text(t: str):
    return NS(type="text", text=t)


def call(name: str, **inp):
    call.n = getattr(call, "n", 0) + 1
    return NS(type="tool_use", id=f"tu_{call.n}", name=name, input=inp)


def reply(*blocks, stop=None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return NS(content=list(blocks), stop_reason=stop)


def last_tool_result(messages) -> dict:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list) and m["content"] and m["content"][0].get("type") == "tool_result":
            return json.loads(m["content"][-1]["content"])
    raise AssertionError("no tool result")


class ScriptedClaude:
    """Each step is a response, or a function(messages) -> response, consumed in order."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        step = self.steps.pop(0)
        return step(kw["messages"]) if callable(step) else step


class FakeSarvam:
    """Dictionary-backed translation; records calls."""

    def __init__(self, lid=None, to_en=None, from_en=None, transcript=("", None)):
        self.lid, self.to_en, self.from_en, self.transcript = lid or {}, to_en or {}, from_en or {}, transcript
        self.calls = []
        self.text = NS(identify_language=self._lid, translate=self._translate)
        self.speech_to_text = NS(transcribe=self._stt)
        self.text_to_speech = NS(convert=self._tts)

    def _lid(self, input):
        self.calls.append(("lid", input))
        return NS(language_code=self.lid.get(input, "en-IN"), script_code=None)

    def _translate(self, input, source_language_code, target_language_code, **kw):
        self.calls.append(("translate", source_language_code, target_language_code, kw.get("mode")))
        table = self.to_en if target_language_code == "en-IN" else self.from_en
        return NS(translated_text=table.get(input, f"<{target_language_code}>{input}"),
                  source_language_code=source_language_code)

    def _stt(self, file, **kw):
        self.calls.append(("stt", kw))
        return NS(transcript=self.transcript[0], language_code=self.transcript[1])

    def _tts(self, text, language_code, **kw):
        self.calls.append(("tts", language_code, kw))
        return NS(audios=[base64.b64encode(f"AUDIO[{language_code}]".encode()).decode()])
