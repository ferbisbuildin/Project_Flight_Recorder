"""Ananya's asks (CHANGES.md): company-wide billing limit across parallel runs, kill switch, cost in rupees, per-run model."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from flightrec.config import Budget, Settings
from flightrec.gateway import Gateway, Manifest, RateLimiter
from flightrec.runner import Deps
from flightrec.service import RunBusy, Service
from tests.fakes import TOOLS, FakeWorld, ScriptedLLM, call

GOAL = "Handle support ticket T-101 end to end."


def make_service(tmp_path, **kw):
    cfg = Settings("http://llm", "key", "cheap", 0.1, 0.4, "http://unused", tmp_path / "db.sqlite", Budget(),
                   models=("cheap", "strong"), usd_inr=85.0, **kw)
    return Service(cfg)


def deps(svc, llm, world):
    return Deps(llm, world, Budget(), sleep=lambda s: None, jitter=False, limiter=svc.limiter)


# ------------------------------------------------------------------------------------ 1. shared billing limit
def test_limiter_spaces_calls_and_backs_off_for_everyone():
    now = [0.0]
    lim = RateLimiter(4, clock=lambda: now[0], sleep=lambda s: None)
    waits = [lim.acquire() for _ in range(5)]  # five callers at the same instant
    assert waits == [0, 0.25, 0.5, 0.75, 1.0]
    lim.back_off(3)                            # someone got a 429 with Retry-After: 3
    assert lim.acquire() >= 3


def test_parallel_runs_never_exceed_the_company_billing_rate():
    stamps, lock = [], threading.Lock()

    class Recording(FakeWorld):
        def request(self, method, path, body=None):
            if path.endswith("/list_payments"):
                with lock:
                    stamps.append(time.monotonic())
            return super().request(method, path, body)

    lim = RateLimiter(20)  # 20/s so the test is quick; production uses 4/s
    world = Recording()

    def agent(_):
        gw = Gateway(world, Manifest(TOOLS), sleep=lambda s: None, jitter=False, limiter=lim)
        for _ in range(5):
            assert gw.execute("list_payments", {"customer_id": "C-01"}).ok
            gw.execute("get_ticket", {"ticket_id": "T-101"})  # not billing: never queued

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(agent, range(8)))  # 8 agents x 5 billing calls
    stamps.sort()
    worst = max(sum(1 for t in stamps if s <= t < s + 1.0) for s in stamps)
    assert len(stamps) == 40 and worst <= 21  # never more than the rate in any 1-second window


def test_non_billing_tools_are_not_queued():
    lim = RateLimiter(0.001)  # effectively closed
    lim.acquire()
    out = Gateway(FakeWorld(), Manifest(TOOLS), limiter=lim).execute("get_ticket", {"ticket_id": "T-101"})
    assert out.ok


# ------------------------------------------------------------------------------------ 2. kill switch
def test_kill_switch_stops_a_running_agent_before_its_next_action(tmp_path):
    svc = make_service(tmp_path)
    world = FakeWorld()

    class KillMidway(ScriptedLLM):
        def complete(self, messages, tools):
            if len(self.seen) == 1:
                svc.kill_all("bad data in billing")  # pressed while the model is thinking
            return super().complete(messages, tools)

    llm = KillMidway([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                      [call("finish", status="succeeded", summary="x")]])
    rid = svc.runner.start("T-101", GOAL, deps(svc, llm, world))
    run = svc.store.get_run(rid)
    assert run["status"] == "stopped" and "kill switch: bad data in billing" in run["summary"]
    assert "no refunds were made" in run["summary"]
    assert [n for n, _ in world.calls] == ["get_ticket"]  # list_payments was decided but never sent


def test_kill_switch_cancels_parked_runs_and_blocks_new_work(tmp_path):
    svc = make_service(tmp_path)
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")]])
    world = FakeWorld()
    rid = svc.runner.start("T-101", GOAL, deps(svc, llm, world))
    [pending] = svc.store.approvals(rid, status="pending")
    assert svc.kill_all("audit") == [rid]
    run = svc.store.get_run(rid)
    assert run["status"] == "stopped" and "PAY-2 INR 4999: not made (approval cancelled)" in run["summary"]
    assert svc.store.get_approval(rid, pending["call_id"])["status"] == "cancelled"
    with pytest.raises(RunBusy):
        svc.decide(rid, pending["call_id"], True)
    with pytest.raises(RunBusy):
        svc.create_live("T-102")
    assert world.refunds == []
    svc.release_kill()
    assert svc.create_live("T-102")


def test_refund_in_flight_is_resolved_and_reported(tmp_path):
    """Kill pressed while a refund's response was lost: we finish resolving it (same key) and say it happened."""
    svc = make_service(tmp_path)

    class KillDuringRefund(FakeWorld):
        def request(self, method, path, body=None):
            x = super().request(method, path, body)
            if path.endswith("/refund_payment") and svc.store.kill_state() is None:
                svc.store.set_kill(True, "pressed mid-refund")
            return x

    world = KillDuringRefund({"refund_payment": ["504_after"]})
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")],
                       [call("send_email", customer_id="C-01", subject="Done", body="Refunded.")]])
    rid = svc.runner.start("T-101", GOAL, deps(svc, llm, world))
    for a in svc.store.approvals(rid, status="pending"):
        svc.store.decide(rid, a["call_id"], True)
    svc.runner.advance(rid, deps(svc, llm, world))
    run = svc.store.get_run(rid)
    assert len(world.refunds) == 1                      # paid exactly once
    assert run["status"] == "stopped" and "PAY-2 INR 4999: DONE (refund RF-1)" in run["summary"]
    assert world.executed("send_email") == []           # nothing after the kill


def test_kill_does_not_retry_a_failed_note(tmp_path):
    svc = make_service(tmp_path)

    class KillOnFailure(FakeWorld):
        def request(self, method, path, body=None):
            x = super().request(method, path, body)
            if path.endswith("/add_ticket_note"):
                svc.store.set_kill(True, "now")
            return x

    world = KillOnFailure({"add_ticket_note": ["503"]})
    gw = Gateway(world, Manifest(TOOLS), sleep=lambda s: None, jitter=False, stop=lambda: svc.store.kill_state() is not None)
    out = gw.execute("add_ticket_note", {"ticket_id": "T-101", "note": "checked"})
    assert not out.ok and "stopped by operator" in out.error
    assert world.executed("add_ticket_note") == []      # the audit confirmed it never landed; not retried


# ------------------------------------------------------------------------------------ 3 + 4. rupees, per-run model
def test_cost_today_in_rupees(tmp_path):
    svc = make_service(tmp_path)
    rid = svc.store.create_run("T-101", GOAL, "cheap")
    svc.store.add_usage(rid, 1000, 500, 0.02)
    today = svc.cost_today()
    assert today["runs"] == 1 and today["inr"] == round(0.02 * 85.0, 2)


def test_model_is_chosen_per_run_from_an_allowlist(tmp_path):
    svc = make_service(tmp_path)
    assert svc.store.get_run(svc.create_live("T-101"))["model"] == "cheap"
    assert svc.store.get_run(svc.create_live("T-102", model="strong"))["model"] == "strong"
    with pytest.raises(ValueError):
        svc.create_live("T-103", model="gpt-anything")


# ------------------------------------------------------------------------------------ reviewer notes, escalations
def test_request_changes_sends_the_note_back_and_only_the_revised_version_runs(tmp_path):
    svc = make_service(tmp_path)
    world = FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")],
                       [call("send_email", customer_id="C-01", subject="Refund", body="Refund done.")],
                       [call("send_email", customer_id="C-01", subject="Refund", body="Hi! Sorry for the trouble, your refund is done.")],
                       [call("finish", status="succeeded", summary="emailed")]])
    rid = svc.runner.start("T-101", GOAL, deps(svc, llm, world))
    [first] = svc.store.approvals(rid, status="pending")
    with pytest.raises(ValueError):
        svc.decide(rid, first["call_id"], "changes_requested", "")      # a change request must say what to change
    svc.decide(rid, first["call_id"], "changes_requested", "make it more polite")
    svc.runner.advance(rid, deps(svc, llm, world))
    assert "make it more polite" in json.dumps(llm.seen[-1])            # the agent saw the note
    [second] = svc.store.approvals(rid, status="pending")
    assert second["args"]["body"].startswith("Hi! Sorry")
    svc.decide(rid, second["call_id"], "approved", "thanks")
    assert svc.runner.advance(rid, deps(svc, llm, world)) == "succeeded"
    assert [e["body"] for e in world.executed("send_email")] == ["Hi! Sorry for the trouble, your refund is done."]
    assert "thanks" in json.dumps(llm.seen[-1])                          # approve-with-note also reaches the agent


def test_escalation_can_be_handed_back_with_guidance(tmp_path):
    svc = make_service(tmp_path)
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")],
                       [call("finish", status="needs_human", summary="Asked to refund another account.")]])
    rid = svc.runner.start("T-101", GOAL, deps(svc, llm, FakeWorld()))
    with pytest.raises(ValueError):
        svc.follow_up(rid, "  ")
    new = svc.follow_up(rid, "Decline politely; only the account owner can ask for that refund.")
    goal = svc.store.get_run(new)["goal"]
    assert "Asked to refund another account." in goal and "only the account owner" in goal
    assert [s["kind"] for s in svc.store.steps(rid)][-1] == "handoff"
    svc.mark_handled(rid, "called the customer")
    assert svc.store.steps(rid)[-1]["data"]["note"] == "called the customer"
