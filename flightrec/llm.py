"""The LLM sits behind one small interface. Real model: any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter).
Replay: hands back the recorded responses in order, no network."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx


@dataclass
class LLMReply:
    message: dict          # the raw assistant message (kept verbatim: Gemini needs its thought_signature echoed back)
    tokens_in: int
    tokens_out: int
    latency_ms: float
    attempts: int = 1


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    model: str

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90.0,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 5):
        if not api_key:
            raise LLMError("LLM_API_KEY is not set: live runs need a key (replay works without one)")
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout, self._sleep, self._max = timeout, sleep, max_attempts

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        body = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "required"}
        last = ""
        for attempt in range(1, self._max + 1):
            t0 = time.perf_counter()
            try:
                r = httpx.post(self._url, headers=self._headers, json=body, timeout=self._timeout)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    try:
                        data = r.json()
                        msg = data["choices"][0]["message"]
                    except (ValueError, KeyError, IndexError):
                        last = f"unparseable LLM response: {r.text[:200]}"
                    else:
                        u = data.get("usage") or {}
                        return LLMReply(msg, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)),
                                        round((time.perf_counter() - t0) * 1000, 1), attempt)
                elif r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                else:  # 4xx other than 429: our request is wrong, retrying will not help
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
            if attempt < self._max:
                self._sleep(min(30.0, 2 ** attempt) + random.uniform(0, 1))
        raise LLMError(f"LLM unavailable after {self._max} attempts: {last}")


class ReplayLLM:
    """Returns the recorded LLM outputs in order."""

    def __init__(self, replies: list[dict], model: str = "replay"):
        self._queue, self.model = list(replies), model

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        if not self._queue:
            raise LLMError("replay ran out of recorded LLM responses")
        r = self._queue.pop(0)
        return LLMReply(r["message"], r["tokens_in"], r["tokens_out"], r["latency_ms"], r.get("attempts", 1))
