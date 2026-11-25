"""Tool gateway: loads the manifest, validates arguments, enforces approvals, and retries by failure type."""
from __future__ import annotations

import copy
import json
import random
import time
from dataclasses import dataclass, field
from typing import Callable

import jsonschema

from .policy import check_approval
from .world import World

MAX_ATTEMPTS = 6
NO_RETRY = {400, 401, 403, 404, 409, 422}
RATIONALE = {"type": "string", "description": "One or two sentences: why you are making this call, citing the KB article if relevant."}


@dataclass
class Outcome:
    ok: bool
    result: object = None
    error: str | None = None
    attempts: list[dict] = field(default_factory=list)


class Manifest:
    def __init__(self, tools: list[dict]):
        self.tools = {t["name"]: t for t in tools}

    def get(self, name: str) -> dict | None:
        return self.tools.get(name)

    def llm_tools(self) -> list[dict]:
        """OpenAI-style tool definitions. Every tool gets a required `rationale` (shown to approvers, stripped before the
        world sees the call); `idempotency_key` is set by the runtime, so the model never sees it."""
        out = []
        for t in self.tools.values():
            p = copy.deepcopy(t["params"])
            p.setdefault("properties", {}).pop("idempotency_key", None)
            p["properties"]["rationale"] = RATIONALE
            p["required"] = [*p.get("required", []), "rationale"]
            desc = t["description"] + (" HIGH RISK: pauses for human approval." if t.get("risk") == "high" else "")
            out.append({"type": "function", "function": {"name": t["name"], "description": desc, "parameters": p}})
        return out


def validate(tool: dict, args: dict) -> str | None:
    try:
        jsonschema.validate(args, tool["params"])
    except jsonschema.ValidationError as e:
        return f"invalid arguments for {tool['name']}: {e.message}"
    return None


class Gateway:
    def __init__(self, world: World, manifest: Manifest, sleep: Callable[[float], None] = time.sleep, jitter: bool = True):
        self.world, self.manifest, self._sleep, self._jitter = world, manifest, sleep, jitter

    def _wait(self, seconds: float) -> float:
        s = seconds + (random.uniform(0, 0.5) if self._jitter else 0)
        self._sleep(s)
        return round(s, 2)

    def execute(self, name: str, args: dict, approval: dict | None = None) -> Outcome:
        tool = self.manifest.get(name)
        if not tool:
            return Outcome(False, error=f"unknown tool {name}")
        err = validate(tool, args)
        if err:
            return Outcome(False, error=err)
        check_approval(tool, args, approval)   # raises ApprovalRequired: the world is never called
        retry_safe = bool(tool.get("idempotent")) or "idempotency_key" in args
        attempts: list[dict] = []
        audit_mark = None
        if not retry_safe:
            audit_mark = self._audit_len(attempts)
        backoff = 0.5
        for n in range(1, MAX_ATTEMPTS + 1):
            x = self.world.request("POST", f"/tools/{name}", args)
            rec = {**x.to_dict(), "attempt": n}
            attempts.append(rec)
            if x.status == 200:
                try:
                    return Outcome(True, json.loads(x.text), attempts=attempts)
                except ValueError:
                    rec["classified"] = "broken body"
            elif x.status == 429:
                rec["classified"] = "rate limited"
                rec["waited_s"] = self._wait(self._retry_after(x))
                continue
            elif x.status in NO_RETRY:
                rec["classified"] = "rejected by world, not retried"
                return Outcome(False, error=f"HTTP {x.status}: {x.text[:500]}", attempts=attempts)
            else:
                rec["classified"] = "timeout" if x.status is None else f"server error {x.status}"
            # transient failure or broken body
            if not retry_safe:
                landed = self._landed(name, args, audit_mark, attempts)
                if landed is True:
                    return Outcome(True, {"ok": True, "confirmed_via_audit": True}, attempts=attempts)
                if landed is None:
                    return Outcome(False, error=f"{name} failed ambiguously and the audit could not confirm it. "
                                                f"Do not repeat it; escalate to a human.", attempts=attempts)
            if n < MAX_ATTEMPTS:
                rec["waited_s"] = self._wait(backoff)
                backoff = min(backoff * 2, 8)
        return Outcome(False, error=f"{name} still failing after {MAX_ATTEMPTS} attempts: {attempts[-1].get('classified')}",
                       attempts=attempts)

    @staticmethod
    def _retry_after(x) -> float:
        """Honour Retry-After, clamped to 0-30s so a hostile or buggy header can't stall a run past its lease."""
        try:
            if x.headers and "Retry-After" in x.headers:
                v = float(x.headers["Retry-After"])
            else:
                v = float(json.loads(x.text).get("retry_after_seconds", 1))
        except (ValueError, AttributeError, TypeError):
            v = 1.0
        return max(0.0, min(v, 30.0)) if v == v else 1.0  # v == v filters NaN

    def _audit(self, attempts: list[dict]) -> list | None:
        x = self.world.request("GET", "/audit")
        attempts.append({**x.to_dict(), "attempt": 0, "classified": "audit check"})
        if x.status != 200:
            return None
        try:
            data = json.loads(x.text)
            return data if isinstance(data, list) else None
        except ValueError:
            return None

    def already_in_audit(self, name: str, args: dict, attempts: list[dict]) -> bool | None:
        """Crash recovery: did this exact call already land in the world? None = the audit could not be read."""
        a = self._audit(attempts)
        if a is None:
            return None
        return any(e.get("tool") == name and e.get("args") == args for e in a)

    def _audit_len(self, attempts: list[dict]) -> int | None:
        a = self._audit(attempts)
        return None if a is None else len(a)

    def _landed(self, name: str, args: dict, mark: int | None, attempts: list[dict]) -> bool | None:
        """For non-idempotent tools: did the failed call actually take effect? None = cannot tell."""
        if mark is None:
            return None
        a = self._audit(attempts)
        if a is None:
            return None
        return any(e.get("tool") == name and e.get("args") == args for e in a[mark:])
