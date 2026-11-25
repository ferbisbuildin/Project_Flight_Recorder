"""Offline tests: no API key, no world server. Each one targets a failure that would hurt Northwind."""
from __future__ import annotations

import json

import pytest

from flightrec.config import Budget, Settings
from flightrec.gateway import Gateway, Manifest
from flightrec.policy import ApprovalRequired
from flightrec.runner import Deps, Runner
from flightrec.service import Service, compare
from flightrec.store import Store, args_hash
from tests.fakes import TOOLS, FakeWorld, ScriptedLLM, call

NOSLEEP = {"sleep": lambda s: None, "jitter": False}


def deps(llm, world, budget=Budget()):
    return Deps(llm, world, budget, **NOSLEEP)


def refund_script(after_refund=None):
    return [
        [call("get_ticket", ticket_id="T-101")],
        [call("list_payments", customer_id="C-01")],
        [call("refund_payment", payment_id="PAY-2", amount=4999, reason="duplicate October charge")],
        *(after_refund or [[call("finish", status="succeeded", summary="Refunded the duplicate.")]]),
    ]


# ---------------------------------------------------------------------------------------------- approvals
def test_high_risk_tool_never_executes_without_approval(tmp_path):
    world = FakeWorld()
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", "Handle support ticket T-101 end to end.", deps(ScriptedLLM(refund_script()), world))
    assert store.get_run(rid)["status"] == "awaiting_approval"
    assert world.executed("refund_payment") == []
    assert not any(n == "refund_payment" for n, _ in world.calls)
    [a] = store.approvals(rid, status="pending")
    assert a["tool"] == "refund_payment" and a["args"]["payment_id"] == "PAY-2" and a["reason"]


def test_gateway_refuses_high_risk_without_matching_approval():
    world = FakeWorld()
    gw = Gateway(world, Manifest(TOOLS), **NOSLEEP)
    args = {"payment_id": "PAY-2", "amount": 4999, "reason": "dup"}
    with pytest.raises(ApprovalRequired):
        gw.execute("refund_payment", args, None)
    with pytest.raises(ApprovalRequired):
        gw.execute("refund_payment", args, {"status": "rejected", "args_hash": args_hash(args)})
    with pytest.raises(ApprovalRequired):  # approved, but for different arguments
        gw.execute("refund_payment", {**args, "amount": 1}, {"status": "approved", "args_hash": args_hash(args)})
    assert world.calls == []
    assert gw.execute("refund_payment", args, {"status": "approved", "args_hash": args_hash(args)}).ok


def test_approval_survives_restart_and_run_resumes(tmp_path):
    db = tmp_path / "db.sqlite"
    world, llm = FakeWorld(), ScriptedLLM(refund_script())
    rid = Runner(Store(db)).start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    # "restart": brand-new store and runner on the same file
    store2 = Store(db)
    [a] = store2.approvals(rid, status="pending")
    store2.decide(rid, a["call_id"], True, "ok, duplicate confirmed")
    status = Runner(store2).advance(rid, deps(llm, world))
    assert status == "succeeded"
    assert len(world.executed("refund_payment")) == 1


def test_rejection_is_fed_back_to_the_agent(tmp_path):
    store, world = Store(tmp_path / "db.sqlite"), FakeWorld()
    llm = ScriptedLLM(refund_script([[call("finish", status="needs_human", summary="Refund rejected; escalating.")]]))
    runner = Runner(store)
    rid = runner.start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    [a] = store.approvals(rid, status="pending")
    store.decide(rid, a["call_id"], False, "customer already got a credit note")
    assert runner.advance(rid, deps(llm, world)) == "needs_human"
    assert world.executed("refund_payment") == []
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert "REJECTED" in tool_msgs[-1]["content"] and "credit note" in tool_msgs[-1]["content"]


# ---------------------------------------------------------------------------------------------- failures
def test_refund_lost_response_is_never_paid_twice(tmp_path):
    """The double refund: money moves, the world answers 504, a naive retry pays again."""
    world = FakeWorld({"refund_payment": ["504_after"]})
    store, runner = Store(tmp_path / "db.sqlite"), Runner(Store(tmp_path / "db.sqlite"))
    llm = ScriptedLLM(refund_script())
    rid = runner.start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    [a] = runner.store.approvals(rid, status="pending")
    runner.store.decide(rid, a["call_id"], True)
    assert runner.advance(rid, deps(llm, world)) == "succeeded"
    assert len(world.refunds) == 1 and world.payments["PAY-2"]["refunded"] == 4999
    step = next(s for s in runner.store.steps(rid) if s["kind"] == "tool" and s["data"]["name"] == "refund_payment")
    assert [x["status"] for x in step["data"]["attempts"]] == [504, 200]
    assert step["data"]["result"]["replayed"] is True


def test_retry_policy_by_failure_type():
    world = FakeWorld({"list_payments": ["429", "503"], "search_kb": ["broken"], "get_customer": ["timeout"]})
    slept = []
    gw = Gateway(world, Manifest(TOOLS), sleep=slept.append, jitter=False)
    out = gw.execute("list_payments", {"customer_id": "C-01"})
    assert out.ok and [a["status"] for a in out.attempts] == [429, 503, 200]
    assert slept[0] == 1.0  # honoured Retry-After
    assert gw.execute("search_kb", {"query": "refund"}).ok
    assert gw.execute("get_customer", {"customer_id": "C-01"}).ok
    out = gw.execute("get_ticket", {"ticket_id": "T-999"})  # 404: the request is wrong, retrying cannot help
    assert not out.ok and len(out.attempts) == 1


def test_non_idempotent_failure_checks_audit_before_retrying():
    world = FakeWorld({"add_ticket_note": ["503_after"]})
    gw = Gateway(world, Manifest(TOOLS), **NOSLEEP)
    out = gw.execute("add_ticket_note", {"ticket_id": "T-101", "note": "checked"})
    assert out.ok and out.result.get("confirmed_via_audit")
    assert len(world.executed("add_ticket_note")) == 1  # landed once, not retried into a duplicate
    world = FakeWorld({"add_ticket_note": ["503"]})  # failed before taking effect: safe to retry
    gw = Gateway(world, Manifest(TOOLS), **NOSLEEP)
    assert gw.execute("add_ticket_note", {"ticket_id": "T-101", "note": "checked"}).ok
    assert len(world.executed("add_ticket_note")) == 1


def test_invalid_arguments_never_reach_the_world(tmp_path):
    world = FakeWorld()
    llm = ScriptedLLM([[call("get_ticket")], [call("finish", status="failed", summary="could not")]])
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    step = next(s for s in store.steps(rid) if s["kind"] == "tool")
    assert step["data"]["status"] == "error" and "ticket_id" in step["data"]["error"]
    assert world.calls == []


# ---------------------------------------------------------------------------------------------- guards
def test_guards_block_refunds_and_emails_to_other_accounts(tmp_path):
    world = FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-9", amount=1499, reason="colleague")],
                       [call("send_email", customer_id="C-09", subject="hi", body="leak")],
                       [call("finish", status="needs_human", summary="blocked")]])
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    tools = [s["data"] for s in store.steps(rid) if s["kind"] == "tool"]
    assert [t["status"] for t in tools if t["name"] in ("refund_payment", "send_email")] == ["blocked", "blocked"]
    assert store.approvals(rid) == []  # blocked before a human is even asked
    assert store.get_run(rid)["status"] == "needs_human"


# ---------------------------------------------------------------------------------------------- budgets
def test_step_budget_stops_the_run(tmp_path):
    llm = ScriptedLLM([[call("search_kb", query="refund")] for _ in range(50)])
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", "x T-101", deps(llm, FakeWorld(), Budget(max_steps=4)))
    run = store.get_run(rid)
    assert run["status"] == "stopped" and "model steps" in run["summary"]
    assert sum(1 for s in store.steps(rid) if s["kind"] == "llm") == 4


def test_token_budget_stops_the_run(tmp_path):
    llm = ScriptedLLM([[call("search_kb", query="refund")] for _ in range(50)], tokens=1000)
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", "x T-101", deps(llm, FakeWorld(), Budget(max_tokens=3000)))
    assert store.get_run(rid)["status"] == "stopped" and "tokens" in store.get_run(rid)["summary"]


def test_time_budget_stops_the_run(tmp_path):
    ticks = iter(range(0, 10_000, 100))
    store = Store(tmp_path / "db.sqlite")
    runner = Runner(store, clock=lambda: next(ticks))
    llm = ScriptedLLM([[call("search_kb", query="refund")] for _ in range(50)])
    rid = runner.start("T-101", "x T-101", deps(llm, FakeWorld(), Budget(max_seconds=250)))
    assert store.get_run(rid)["status"] == "stopped" and "active time" in store.get_run(rid)["summary"]


# ---------------------------------------------------------------------------------------------- replay
def test_replay_is_identical_and_offline(tmp_path):
    cfg = Settings("", "", "scripted", 0.1, 0.4, "http://unused", tmp_path / "db.sqlite", Budget())
    svc = Service(cfg)
    world = FakeWorld({"list_payments": ["429"], "refund_payment": ["504_after"]})
    llm = ScriptedLLM(refund_script())
    rid = svc.runner.start("T-101", "Handle support ticket T-101 end to end.", deps(llm, world))
    [a] = svc.store.approvals(rid, status="pending")
    svc.store.decide(rid, a["call_id"], True, "fine")
    svc.runner.advance(rid, deps(llm, world))
    calls_before = len(world.calls)
    res = svc.replay(rid)
    assert res["identical"], res
    assert res["status_replay"] == "succeeded"
    assert len(world.calls) == calls_before  # the replay never touched the world
    assert compare(svc.store, rid, res["run_id"])["identical"]


def test_replay_reports_divergence_instead_of_lying(tmp_path):
    cfg = Settings("", "", "scripted", 0.1, 0.4, "http://unused", tmp_path / "db.sqlite", Budget())
    svc = Service(cfg)
    rid = svc.runner.start("T-101", "x T-101", deps(ScriptedLLM([[call("get_ticket", ticket_id="T-101")],
                                                                 [call("finish", status="succeeded", summary="ok")]]), FakeWorld()))
    # tamper with the recording: drop the world response the replay will need
    with svc.store._lock:
        svc.store._db.execute("UPDATE steps SET data=json_set(data, '$.attempts', json('[]')) WHERE run_id=? AND kind='tool' "
                              "AND json_extract(data, '$.name')='get_ticket'", (rid,))
    res = svc.replay(rid)
    assert not res["identical"] and res["status_replay"] == "failed"
