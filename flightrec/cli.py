"""Command line: python -m flightrec <command>

  run T-101 [T-102 ...] | run --all   start runs in parallel (pauses at approvals; use `approve` or the viewer)
  stop-all [reason]                   kill switch: stop every agent safely; report what happened to money
  release                             release the kill switch
  approvals                           list pending approvals
  approve <run_id> <call_id> [note]   approve and continue the run
  reject  <run_id> <call_id> [note]   reject and continue the run
  revise  <run_id> <call_id> <note>   request changes: nothing runs; the agent revises and asks again
  resume  <run_id>                    continue a run (e.g. after a restart)
  runs                                list runs
  show    <run_id>                    print a run's timeline
  replay  <run_id>                    re-run from the trace with no LLM and no world calls
  export  <out_dir> <run_id>...       write traces + world_audit.jsonl
  import  <dir>                       load exported traces (e.g. runs/final) to view / replay without a key
  serve                               start the viewer on :8080
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import settings
from .service import Service


def _short(v, n=140) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def show(svc: Service, run_id: str) -> None:
    r = svc.store.get_run(run_id)
    print(f"{r['id']}  {r['task_id']}  {r['status'].upper()}  steps={r['steps']}  tokens={r['tokens_in'] + r['tokens_out']}"
          f"  cost=${r['cost']:.4f}  active={r['active_seconds']:.1f}s")
    for s in svc.store.steps(run_id):
        d = s["data"]
        if s["kind"] == "llm":
            names = [c["function"]["name"] for c in d["output"].get("tool_calls") or []]
            print(f"  #{s['seq']:<3} LLM   {d['latency_ms']:>7.0f}ms  -> {', '.join(names) or '(no tool call)'}  {_short(d['output'].get('content') or '', 90)}")
        elif s["kind"] == "tool":
            tries = len([a for a in d["attempts"] if a.get("attempt")])
            extra = f" attempts={tries}" if tries > 1 else ""
            print(f"  #{s['seq']:<3} TOOL  {(s['latency_ms'] or 0):>7.0f}ms  {d['name']}({_short(d['args'], 80)}) [{d['status']}{extra}]"
                  f"  {_short(d['error'] or d['result'], 100)}")
            if d.get("reason"):
                print(f"        why: {_short(d.get('reason'), 130)}")
        elif s["kind"] == "approval":
            print(f"  #{s['seq']:<3} WAIT  approval for {d['name']}({_short(d['args'], 90)})  call_id={d['call_id']}")
        elif s["kind"] == "end":
            print(f"  #{s['seq']:<3} END   {d['status']}: {d['summary']}")
        else:
            print(f"  #{s['seq']:<3} {s['kind'].upper()}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on ₹ or …
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    a = argv if argv is not None else sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = a[0], a[1:]
    if cmd == "serve":
        import uvicorn
        # No login on the viewer (single operator), so listen on loopback unless told otherwise (Docker sets HOST).
        uvicorn.run("flightrec.api:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
        return 0
    svc = Service(settings())
    if cmd == "run":
        ids = [t["task_id"] for t in svc.tasks()] if rest == ["--all"] else rest
        goals = {t["task_id"]: t["goal"] for t in svc.tasks()}
        rids = [svc.create_live(tid, goals.get(tid)) for tid in ids]
        with ThreadPoolExecutor(max_workers=svc.cfg.workers) as pool:  # billing calls share one company-wide limiter
            list(pool.map(svc.advance, rids))
        for rid in rids:
            show(svc, rid)
    elif cmd == "stop-all":
        stopped = svc.kill_all(" ".join(rest))
        print(f"Kill switch ENGAGED. Running agents stop at their next safe point. Stopped now: {len(stopped)}")
        for rid in stopped:
            print(f"  {rid}: {svc.store.get_run(rid)['summary']}")
    elif cmd == "release":
        svc.release_kill()
        print("Kill switch released. Runs can start again.")
    elif cmd == "approvals":
        for p in svc.store.approvals(status="pending"):
            print(f"{p['run_id']} {p['call_id']} {p['tool']} {json.dumps(p['args'])}\n    why: {p['reason']}")
    elif cmd in ("approve", "reject", "revise"):
        decision = {"approve": "approved", "reject": "rejected", "revise": "changes_requested"}[cmd]
        svc.decide(rest[0], rest[1], decision, " ".join(rest[2:]))
        svc.advance(rest[0])
        show(svc, rest[0])
    elif cmd == "resume":
        svc.advance(rest[0])
        show(svc, rest[0])
    elif cmd == "runs":
        for r in svc.store.list_runs():
            print(f"{r['id']}  {r['task_id']}  {r['status']:<18} steps={r['steps']:<3} {r['mode']}")
    elif cmd == "show":
        show(svc, rest[0])
    elif cmd == "replay":
        print(json.dumps(svc.replay(rest[0]), indent=2))
    elif cmd == "import":
        print(f"imported {len(svc.import_traces(Path(rest[0])))} run(s)")
    elif cmd == "export":
        for p in svc.export(rest[1:], Path(rest[0])):
            print(p)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
