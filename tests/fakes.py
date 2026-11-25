"""Offline stand-ins: a tiny Northwind world with scriptable failures, and a scripted LLM."""
from __future__ import annotations

import itertools
import json

from flightrec.llm import LLMReply
from flightrec.world import Exchange

TOOLS = [
    {"name": "get_ticket", "description": "Get one ticket.", "risk": "low", "idempotent": True,
     "params": {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"], "additionalProperties": False}},
    {"name": "get_customer", "description": "Get a customer.", "risk": "low", "idempotent": True,
     "params": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}},
    {"name": "list_payments", "description": "List payments.", "risk": "low", "idempotent": True,
     "params": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}},
    {"name": "search_kb", "description": "Search KB.", "risk": "low", "idempotent": True,
     "params": {"type": "object", "properties": {"query": {"type": "string", "minLength": 2}}, "required": ["query"], "additionalProperties": False}},
    {"name": "add_ticket_note", "description": "Add note.", "risk": "low", "idempotent": False,
     "params": {"type": "object", "properties": {"ticket_id": {"type": "string"}, "note": {"type": "string", "minLength": 3}},
                "required": ["ticket_id", "note"], "additionalProperties": False}},
    {"name": "close_ticket", "description": "Close.", "risk": "low", "idempotent": True,
     "params": {"type": "object", "properties": {"ticket_id": {"type": "string"}, "resolution": {"type": "string", "minLength": 3}},
                "required": ["ticket_id", "resolution"], "additionalProperties": False}},
    {"name": "refund_payment", "description": "Refund.", "risk": "high", "idempotent": False,
     "params": {"type": "object", "properties": {"payment_id": {"type": "string"}, "amount": {"type": "integer", "minimum": 1},
                                                 "reason": {"type": "string"}, "idempotency_key": {"type": "string"}},
                "required": ["payment_id", "amount", "reason"], "additionalProperties": False}},
    {"name": "send_email", "description": "Email.", "risk": "high", "idempotent": False,
     "params": {"type": "object", "properties": {"customer_id": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["customer_id", "subject", "body"], "additionalProperties": False}},
]


class FakeWorld:
    """failures: {tool_name: ["429", "503", "broken", "504_after", "503_after", "timeout"]}, consumed one per call."""

    def __init__(self, failures: dict[str, list[str]] | None = None):
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: list[tuple[str, dict]] = []
        self.audit: list[dict] = []
        self.refunds: list[dict] = []
        self.tickets = {"T-101": {"ticket_id": "T-101", "customer_id": "C-01", "status": "open", "text": "Charged twice in October"}}
        self.payments = {"PAY-1": {"payment_id": "PAY-1", "customer_id": "C-01", "amount": 4999, "refunded": 0},
                         "PAY-2": {"payment_id": "PAY-2", "customer_id": "C-01", "amount": 4999, "refunded": 0},
                         "PAY-9": {"payment_id": "PAY-9", "customer_id": "C-09", "amount": 1499, "refunded": 0}}

    def request(self, method: str, path: str, body: dict | None = None) -> Exchange:
        if (method, path) == ("GET", "/tools"):
            return Exchange(method, path, body, 200, json.dumps(TOOLS), 1.0)
        if (method, path) == ("GET", "/audit"):
            return Exchange(method, path, body, 200, json.dumps(self.audit), 1.0)
        name = path.rsplit("/", 1)[-1]
        self.calls.append((name, dict(body or {})))
        fail = self.failures.get(name, []).pop(0) if self.failures.get(name) else None
        if fail == "429":
            return Exchange(method, path, body, 429, json.dumps({"error": "rate_limited"}), 1.0, headers={"Retry-After": "1"})
        if fail == "503":
            return Exchange(method, path, body, 503, json.dumps({"error": "upstream unavailable"}), 1.0)
        if fail == "timeout":
            return Exchange(method, path, body, None, "", 15000.0, error="ReadTimeout")
        status, out = self._call(name, body or {})
        if fail == "broken":
            return Exchange(method, path, body, 200, "BROKEN:" + json.dumps(out)[:5], 1.0)
        if fail in ("504_after", "503_after"):  # the action took effect, the response was lost
            return Exchange(method, path, body, int(fail[:3]), json.dumps({"error": "gateway timeout"}), 1.0)
        return Exchange(method, path, body, status, json.dumps(out), 1.0)

    def _call(self, name: str, a: dict):
        if name == "get_ticket":
            t = self.tickets.get(a.get("ticket_id"))
            return (200, t) if t else (404, {"error": "no such ticket"})
        if name == "get_customer":
            return 200, {"customer_id": a["customer_id"], "name": "Test"}
        if name == "list_payments":
            return 200, [p for p in self.payments.values() if p["customer_id"] == a["customer_id"]]
        if name == "search_kb":
            return 200, [{"id": "KB-1", "title": "Refund policy", "text": "Refund duplicate charges in full."}]
        if name == "refund_payment":
            key = a.get("idempotency_key")
            prev = next((r for r in self.refunds if key and r.get("idempotency_key") == key), None)
            if prev:
                return 200, {"ok": True, "refund_id": prev["refund_id"], "replayed": True}
            p = self.payments[a["payment_id"]]
            if p["refunded"] + a["amount"] > p["amount"]:
                return 422, {"error": "refund exceeds remaining amount"}
            p["refunded"] += a["amount"]
            self.refunds.append({**a, "refund_id": f"RF-{len(self.refunds) + 1}"})
            self._log(name, a)
            return 200, {"ok": True, "refund_id": self.refunds[-1]["refund_id"]}
        if name in ("add_ticket_note", "close_ticket", "send_email"):
            self._log(name, a)
            return 200, {"ok": True}
        return 404, {"error": "unknown"}

    def _log(self, tool: str, args: dict) -> None:
        self.audit.append({"seq": len(self.audit) + 1, "tool": tool, "args": args})

    def executed(self, name: str) -> list[dict]:
        return [e["args"] for e in self.audit if e["tool"] == name]


_ids = itertools.count(1)


def call(name: str, rationale: str = "because", **args) -> dict:
    return {"id": f"call_{next(_ids)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps({**args, "rationale": rationale})}}


class ScriptedLLM:
    """Each turn returns the next scripted list of tool calls."""

    model = "scripted"

    def __init__(self, turns: list[list[dict]], tokens: int = 100):
        self.turns, self.tokens, self.seen = list(turns), tokens, []

    def complete(self, messages, tools) -> LLMReply:
        self.seen.append(messages)
        calls = self.turns.pop(0) if self.turns else [call("finish", status="failed", summary="script ran out")]
        return LLMReply({"role": "assistant", "content": "thinking", "tool_calls": calls}, self.tokens, self.tokens // 2, 5.0)
