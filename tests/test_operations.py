"""Running ~200 tickets tonight: many parallel runs, restarts, duplicates, no-key replay of shipped traces."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from flightrec.config import Budget, Settings
from flightrec.runner import Deps
from flightrec.service import RunBusy, Service
from tests.fakes import FakeWorld, ScriptedLLM, call

GOAL = "Handle support ticket {} end to end."


def service(tmp_path, key="key", name="db.sqlite"):
    return Service(Settings("http://llm", key, "cheap", 0.1, 0.4, "http://unused", tmp_path / name, Budget(),
                            models=("cheap",), workers=8))


def refund_flow():
    return [[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
            [call("refund_payment", payment_id="PAY-2", amount=4999, reason="duplicate")],
            [call("finish", status="succeeded", summary="refunded")]]


def test_many_parallel_runs_all_finish_cleanly(tmp_path):
    svc, world = service(tmp_path), FakeWorld({"search_kb": ["503", "broken"] * 10, "list_payments": ["429"] * 5})
    llms = {}
    tasks = [f"T-{n}" for n in range(200, 240)]            # 40 tickets at once
    for t in tasks:
        rid = svc.create_live(t)
        llms[rid] = ScriptedLLM([[call("search_kb", query="refund policy")], [call("list_payments", customer_id="C-01")],
                                 [call("finish", status="succeeded", summary=f"{t} done")]])
    svc.live_deps = lambda model=None: None
    run = lambda rid: svc.runner.advance(rid, Deps(llms[rid], world, Budget(), sleep=lambda s: None, jitter=False,
                                                    limiter=svc.limiter))
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(run, list(llms)))
    assert statuses == ["succeeded"] * len(tasks)
    for rid in llms:                                        # every trace is complete and well-ordered
        seqs = [s["seq"] for s in svc.store.steps(rid)]
        assert seqs == list(range(1, len(seqs) + 1))
        assert svc.store.get_run(rid)["lease_owner"] is None


def test_kill_switch_stops_queued_runs_before_they_do_anything(tmp_path):
    svc, world = service(tmp_path), FakeWorld()
    rids = [svc.create_live(f"T-{n}") for n in range(300, 310)]  # queued: created, not started
    svc.kill_all("bad night")
    llm = ScriptedLLM([])
    for rid in rids:
        assert svc.runner.advance(rid, Deps(llm, world, Budget(), sleep=lambda s: None, jitter=False)) == "stopped"
    assert llm.seen == [] and world.calls == []


def test_one_active_run_per_ticket(tmp_path):
    svc = service(tmp_path)
    first = svc.create_live("T-101")
    with pytest.raises(RunBusy):
        svc.create_live("T-101")
    svc.store.update_run(first, status="succeeded")
    assert svc.create_live("T-101")                          # fine once the first one is finished


def test_a_second_run_on_the_same_ticket_cannot_refund_again(tmp_path):
    svc, world = service(tmp_path), FakeWorld({"refund_payment": ["504_after"]})
    for _ in range(2):                                       # e.g. a follow-up run after an escalation
        rid = svc.create_live("T-101")
        llm = ScriptedLLM(refund_flow())
        d = Deps(llm, world, Budget(), sleep=lambda s: None, jitter=False)
        svc.runner.advance(rid, d)
        [a] = svc.store.approvals(rid, status="pending")
        svc.decide(rid, a["call_id"], "approved")
        svc.runner.advance(rid, d)
    assert len(world.refunds) == 1                           # the world refused the second (nothing left to refund)


def test_stalled_runs_are_found_for_the_janitor(tmp_path):
    svc = service(tmp_path)
    dead = svc.create_live("T-101")
    svc.store.claim(dead, "crashed-worker", ttl=-1)         # its worker died; lease already expired
    alive = svc.create_live("T-102")
    svc.store.claim(alive, "busy-worker", ttl=600)
    waiting = svc.create_live("T-103")
    svc.store.update_run(waiting, status="awaiting_approval")
    assert set(svc.stalled()) == {dead}


def test_shipped_traces_load_and_replay_without_a_key(tmp_path):
    live, world = service(tmp_path, name="live.sqlite"), FakeWorld({"list_payments": ["429"], "refund_payment": ["504_after"]})
    rid = live.create_live("T-101")
    llm = ScriptedLLM(refund_flow())
    d = Deps(llm, world, Budget(), sleep=lambda s: None, jitter=False)
    live.runner.advance(rid, d)
    [a] = live.store.approvals(rid, status="pending")
    live.decide(rid, a["call_id"], "approved", "ok")
    live.runner.advance(rid, d)
    out = tmp_path / "final"
    out.mkdir()
    (out / f"T-101_{rid}.json").write_text(json.dumps({"run": live.store.get_run(rid), "steps": live.store.steps(rid),
                                                        "approvals": live.store.approvals(rid)}), encoding="utf-8")

    fresh = service(tmp_path, key="", name="fresh.sqlite")   # a fresh clone: no key, empty database
    assert fresh.import_traces(out) == [rid]
    assert fresh.import_traces(out) == []                    # idempotent
    res = fresh.replay(rid)
    assert res["identical"] and res["status_replay"] == "succeeded"


def test_import_refuses_unfinished_runs_and_unknown_columns(tmp_path):
    svc = service(tmp_path, key="")
    base = {"id": "r1", "task_id": "T-101", "goal": "g", "status": "running", "mode": "live", "created_at": 1.0}
    with pytest.raises(ValueError):                           # would otherwise be resumed live by the janitor
        svc.store.import_run({"run": base, "steps": []})
    evil = {**base, "status": "succeeded", "id); DROP TABLE runs; --": 1}
    assert svc.store.import_run({"run": evil, "steps": []})  # unknown key ignored, never reaches SQL
    assert svc.store.get_run("r1")["status"] == "succeeded" and svc.stalled() == []
