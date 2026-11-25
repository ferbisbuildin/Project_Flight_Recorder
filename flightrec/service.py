"""Wiring: live and replay dependencies, replay comparison, export. Shared by the API and the CLI."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from .config import Settings
from .gateway import RateLimiter
from .llm import LLMError, OpenAICompatLLM, ReplayLLM
from .runner import Deps, Runner
from .store import Store
from .world import HttpWorld, ReplayWorld


class RunBusy(RuntimeError):
    pass


class Service:
    def __init__(self, cfg: Settings, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.runner = Runner(self.store)
        self.limiter = RateLimiter(cfg.billing_per_second)  # ONE limiter for every live run in this process
        self.llm_limiter = RateLimiter(cfg.llm_per_minute / 60) if cfg.llm_per_minute else None

    @property
    def models(self) -> list[str]:
        return list(self.cfg.models) or [self.cfg.llm_model]

    # ------------------------------------------------------------ live
    def live_deps(self, model: str | None = None) -> Deps:
        llm = OpenAICompatLLM(self.cfg.llm_base_url, self.cfg.llm_api_key, model or self.cfg.llm_model, limiter=self.llm_limiter)
        return Deps(llm, HttpWorld(self.cfg.world_url), self.cfg.budget, limiter=self.limiter,
                    price_in_per_m=self.cfg.price_in_per_m, price_out_per_m=self.cfg.price_out_per_m)

    def create_live(self, task_id: str, goal: str | None = None, model: str | None = None) -> str:
        if not self.cfg.llm_api_key:
            raise LLMError("LLM_API_KEY is not set: live runs need a key. Replay and the viewer work without one.")
        if self.store.kill_state():
            raise RunBusy("the kill switch is engaged; release it before starting runs")
        if model and model not in self.models:
            raise ValueError(f"model {model!r} is not allowed; choose one of {self.models}")
        return self.store.create_run(task_id, goal or f"Handle support ticket {task_id} end to end.", model or self.models[0])

    def advance(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        deps = self.replay_deps(run["replay_of"]) if run["mode"] == "replay" else self.live_deps(run["model"])
        return self.runner.advance(run_id, deps)

    def decide(self, run_id: str, call_id: str, approve: bool, note: str = "") -> dict:
        if self.store.kill_state():
            raise RunBusy("the kill switch is engaged; no approvals until it is released")
        return self.store.decide(run_id, call_id, approve, note)

    # ------------------------------------------------------------ kill switch
    def kill_all(self, reason: str = "") -> list[str]:
        """Engage the switch: running runs stop at their next safe point; parked runs (awaiting approval) stop now.
        Returns the runs stopped immediately."""
        self.store.set_kill(True, reason or "stop everything")
        stopped = []
        for r in self.store.list_runs():
            if r["status"] == "awaiting_approval" and self.store.claim(r["id"], "kill-switch"):
                try:
                    if self.store.get_run(r["id"])["status"] == "awaiting_approval":
                        self.runner.stop_by_operator(r["id"])
                        stopped.append(r["id"])
                finally:
                    self.store.release(r["id"], "kill-switch")
        return stopped

    def release_kill(self) -> None:
        self.store.set_kill(False)

    def cost_today(self) -> dict:
        start = time.mktime(date.today().timetuple())
        runs = [r for r in self.store.list_runs() if r["mode"] == "live" and r["created_at"] >= start]
        usd = sum(r["cost"] for r in runs)
        return {"runs": len(runs), "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in runs), "usd": usd,
                "inr": round(usd * self.cfg.usd_inr, 2), "usd_inr": self.cfg.usd_inr}

    def tasks(self) -> list[dict]:
        try:
            x = HttpWorld(self.cfg.world_url, timeout=3).request("GET", "/tasks")
            if x.status == 200:
                return json.loads(x.text)
        except ValueError:
            pass
        return [{"task_id": f"T-{n}", "goal": f"Handle support ticket T-{n} end to end."} for n in range(101, 107)]

    # ------------------------------------------------------------ replay
    def replay_deps(self, original_id: str) -> Deps:
        steps = self.store.steps(original_id)
        replies = [{"message": s["data"]["output"], "tokens_in": s["data"]["tokens_in"], "tokens_out": s["data"]["tokens_out"],
                    "latency_ms": s["data"]["latency_ms"], "attempts": s["data"].get("attempts", 1)}
                   for s in steps if s["kind"] == "llm"]
        exchanges = []
        for s in steps:
            if s["kind"] in ("tools", "tool"):
                exchanges += [{k: a[k] for k in ("method", "path", "body", "status", "text", "latency_ms", "error", "headers")}
                              for a in s["data"].get("attempts", [])]
        budget = replace(self.cfg.budget, max_seconds=float("inf"))  # wall time is not part of the trace
        return Deps(ReplayLLM(replies, model="replay"), ReplayWorld(exchanges), budget, sleep=lambda s: None, jitter=False,
                    obey_kill=False, price_in_per_m=self.cfg.price_in_per_m, price_out_per_m=self.cfg.price_out_per_m)

    def replay(self, original_id: str) -> dict:
        orig = self.store.get_run(original_id)
        if orig is None:
            raise KeyError(original_id)
        if orig["status"] == "running":
            raise RunBusy("this run is executing right now; replay it once it pauses or ends")
        deps = self.replay_deps(original_id)  # one snapshot of the trace...
        acted_on = {s["data"]["call_id"] for s in self.store.steps(original_id) if s["kind"] == "tool"}
        run_id = self.store.create_run(orig["task_id"], orig["goal"], "replay", mode="replay", replay_of=original_id)
        for a in self.store.approvals(original_id):  # ...and the human decisions that trace actually reflects
            self.store.request_approval(run_id, a["call_id"], a["tool"], a["args"], a["reason"] or "")
            if a["status"] != "pending" and a["call_id"] in acted_on:
                self.store.decide(run_id, a["call_id"], a["status"] == "approved", a["note"] or "")
        self.runner.advance(run_id, deps)
        return {"run_id": run_id, **compare(self.store, original_id, run_id)}

    # ------------------------------------------------------------ export
    def export(self, run_ids: list[str], out: Path) -> list[Path]:
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for rid in run_ids:
            run = self.store.get_run(rid)
            p = (out / f"{run['task_id']}_{rid}.json").resolve()
            if p.parent != out.resolve():  # task ids are validated on create; this guards the path as well
                raise ValueError(f"refusing to write outside {out}: {p}")
            p.write_text(json.dumps({"run": run, "steps": self.store.steps(rid), "approvals": self.store.approvals(rid)},
                                    indent=2), encoding="utf-8")
            paths.append(p)
        x = HttpWorld(self.cfg.world_url).request("GET", "/audit")
        if x.status == 200:
            audit = out / "world_audit.jsonl"
            audit.write_text("".join(json.dumps(e) + "\n" for e in json.loads(x.text)), encoding="utf-8")
            paths.append(audit)
        return paths


def signature(steps: list[dict]) -> list[tuple]:
    """What must be identical between a run and its replay: every decision, call, outcome and the end state."""
    sig = []
    for s in steps:
        d = s["data"]
        if s["kind"] == "llm":
            sig.append(("llm", json.dumps(d["output"], sort_keys=True)))
        elif s["kind"] == "tool":
            sig.append(("tool", d["name"], json.dumps(d["args"], sort_keys=True), d["status"],
                        json.dumps(d["result"], sort_keys=True), len(d["attempts"])))
        elif s["kind"] in ("approval", "end", "nudge"):
            sig.append((s["kind"], json.dumps({k: v for k, v in d.items()}, sort_keys=True)))
    return sig


def compare(store: Store, a: str, b: str) -> dict:
    sa, sb = signature(store.steps(a)), signature(store.steps(b))
    first = next((i for i, (x, y) in enumerate(zip(sa, sb)) if x != y), None)
    if first is None and len(sa) != len(sb):
        first = min(len(sa), len(sb))
    return {"identical": first is None, "steps_original": len(sa), "steps_replay": len(sb), "first_difference": first,
            "status_original": store.get_run(a)["status"], "status_replay": store.get_run(b)["status"]}
