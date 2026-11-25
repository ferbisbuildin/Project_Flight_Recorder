"""Edge cases: concurrency, crashes, odd model output, nested replay, the LLM client's own retries."""
from __future__ import annotations

import json

import httpx
import pytest

from flightrec.config import Budget, Settings
from flightrec.gateway import Gateway, Manifest
from flightrec.llm import LLMError, LLMReply, OpenAICompatLLM
from flightrec.runner import Deps, Runner
from flightrec.service import Service
from flightrec.store import Store
from flightrec.world import Exchange
from tests.fakes import TOOLS, FakeWorld, ScriptedLLM, call

GOAL = "Handle support ticket T-101 end to end."


def deps(llm, world, budget=Budget()):
    return Deps(llm, world, budget, sleep=lambda s: None, jitter=False)


def approve_all(store, run_id, ok=True, note=""):
    for a in store.approvals(run_id, status="pending"):
        store.decide(run_id, a["call_id"], ok, note)


def settings(tmp_path, key=""):
    return Settings("http://llm", key, "scripted", 0.1, 0.4, "http://unused", tmp_path / "db.sqlite", Budget())


class SimulatedCrash(BaseException):
    """Stands in for the process dying (BaseException: not caught by the runner's error handling)."""


# ------------------------------------------------------------------------------------------------ concurrency
def test_a_run_held_by_another_worker_is_not_advanced(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rid = store.create_run("T-101", GOAL, "scripted")
    assert store.claim(rid, "other-process")
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")]])
    assert Runner(store).advance(rid, deps(llm, FakeWorld())) == "running"
    assert llm.seen == [] and store.steps(rid) == []


def test_lease_of_a_dead_worker_expires(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    rid = store.create_run("T-101", GOAL, "scripted")
    assert store.claim(rid, "dead-process", ttl=-1)  # already expired
    llm = ScriptedLLM([[call("finish", status="failed", summary="nothing to do")]])
    assert Runner(store).advance(rid, deps(llm, FakeWorld())) == "failed"


# ------------------------------------------------------------------------------------------------ crash recovery
class CrashAfterEmail(FakeWorld):
    def __init__(self):
        super().__init__()
        self.crash = True

    def request(self, method, path, body=None):
        x = super().request(method, path, body)
        if path.endswith("/send_email") and self.crash:
            self.crash = False
            raise SimulatedCrash()  # the email went out, the process died before saving the result
        return x


def test_email_sent_just_before_a_crash_is_not_sent_again(tmp_path):
    db, world = tmp_path / "db.sqlite", CrashAfterEmail()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")],
                       [call("send_email", customer_id="C-01", subject="Refund", body="Your refund is done.")],
                       [call("finish", status="succeeded", summary="emailed")]])
    store = Store(db)
    rid = Runner(store).start("T-101", GOAL, deps(llm, world))
    approve_all(store, rid)
    with pytest.raises(SimulatedCrash):
        Runner(store).advance(rid, deps(llm, world))
    store2 = Store(db)  # restart
    assert Runner(store2).advance(rid, deps(llm, world)) == "succeeded"
    assert len(world.executed("send_email")) == 1
    step = next(s for s in store2.steps(rid) if s["kind"] == "tool" and s["data"]["name"] == "send_email")
    assert step["data"]["result"]["recovered_after_restart"] is True


def test_unexpected_error_ends_the_run_as_failed_not_stuck(tmp_path):
    class Exploding(FakeWorld):
        def request(self, method, path, body=None):
            if path.endswith("/get_ticket"):
                raise RuntimeError("boom")
            return super().request(method, path, body)
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", GOAL, deps(ScriptedLLM([[call("get_ticket", ticket_id="T-101")]]), Exploding()))
    run = store.get_run(rid)
    assert run["status"] == "failed" and "internal error" in run["summary"] and run["lease_owner"] is None


def test_world_down_at_start_fails_cleanly(tmp_path):
    class Down:
        def request(self, method, path, body=None):
            return Exchange(method, path, body, None, "", 1.0, error="ConnectError")
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", GOAL, deps(ScriptedLLM([]), Down()))
    assert store.get_run(rid)["status"] == "failed" and "manifest" in store.get_run(rid)["summary"]


# ------------------------------------------------------------------------------------------------ approvals
def test_two_risky_calls_in_one_turn_each_wait_for_a_human(tmp_path):
    store, world = Store(tmp_path / "db.sqlite"), FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="duplicate"),
                        call("send_email", customer_id="C-01", subject="Refund", body="Refunded PAY-2.")],
                       [call("finish", status="succeeded", summary="done")]])
    runner = Runner(store)
    rid = runner.start("T-101", GOAL, deps(llm, world))
    assert [a["tool"] for a in store.approvals(rid, status="pending")] == ["refund_payment"]
    approve_all(store, rid)
    assert runner.advance(rid, deps(llm, world)) == "awaiting_approval"
    assert [a["tool"] for a in store.approvals(rid, status="pending")] == ["send_email"]
    assert len(world.executed("refund_payment")) == 1 and world.executed("send_email") == []
    approve_all(store, rid)
    assert runner.advance(rid, deps(llm, world)) == "succeeded"


def test_email_cannot_carry_another_customers_data(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("send_email", customer_id="C-01", subject="History", body="Payments of C-09: PAY-9")],
                       [call("send_email", customer_id="C-01", subject="Yours", body="Your payment PAY-2 was refunded.")]])
    rid = Runner(store).start("T-101", GOAL, deps(llm, FakeWorld()))
    tools = [s["data"] for s in store.steps(rid) if s["kind"] == "tool"]
    assert tools[-1]["status"] == "blocked" and "C-09" in tools[-1]["error"]
    assert [a["args"]["body"] for a in store.approvals(rid)] == ["Your payment PAY-2 was refunded."]


def test_agent_cannot_touch_a_different_ticket(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    llm = ScriptedLLM([[call("close_ticket", ticket_id="T-102", resolution="closing someone else's ticket")],
                       [call("finish", status="failed", summary="x")]])
    world = FakeWorld()
    rid = Runner(store).start("T-101", GOAL, deps(llm, world))
    assert next(s for s in store.steps(rid) if s["kind"] == "tool")["data"]["status"] == "blocked"
    assert world.executed("close_ticket") == []


# ------------------------------------------------------------------------------------------------ odd model output
def test_reused_or_missing_call_ids_never_share_an_approval(tmp_path):
    class OddLLM(ScriptedLLM):
        def complete(self, messages, tools):
            r = super().complete(messages, tools)
            for c in r.message["tool_calls"]:
                c["id"] = "same"                                        # every call reuses one id
                c["function"]["arguments"] = json.loads(c["function"]["arguments"])  # and sends a dict
            return r
    store, world = Store(tmp_path / "db.sqlite"), FakeWorld()
    llm = OddLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                  [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")],
                  [call("refund_payment", payment_id="PAY-1", amount=4999, reason="again")]])
    runner = Runner(store)
    rid = runner.start("T-101", GOAL, deps(llm, world))
    approve_all(store, rid)
    runner.advance(rid, deps(llm, world))
    ids = [c["id"] for s in store.steps(rid) if s["kind"] == "llm" for c in s["data"]["output"]["tool_calls"]]
    assert len(ids) == len(set(ids))
    assert store.get_run(rid)["status"] == "awaiting_approval"  # the second refund needs its own approval
    assert len(world.executed("refund_payment")) == 1


def test_bad_json_and_bad_finish_are_fed_back_not_fatal(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    broken = {"id": "x1", "type": "function", "function": {"name": "get_ticket", "arguments": "{not json"}}
    llm = ScriptedLLM([[broken], [call("finish", status="done", summary="wrong status")],
                       [call("finish", status="needs_human", summary="Could not read the ticket.")]])
    rid = Runner(store).start("T-101", GOAL, deps(llm, FakeWorld()))
    tools = [s["data"] for s in store.steps(rid) if s["kind"] == "tool"]
    assert [t["status"] for t in tools] == ["error", "error", "ok"]
    assert store.get_run(rid)["status"] == "needs_human"


def test_prose_reply_is_nudged_and_still_bounded_by_budget(tmp_path):
    class Chatty:
        model = "chatty"

        def complete(self, messages, tools):
            return LLMReply({"role": "assistant", "content": "I think the customer is right."}, 10, 5, 1.0)
    store = Store(tmp_path / "db.sqlite")
    rid = Runner(store).start("T-101", GOAL, deps(Chatty(), FakeWorld(), Budget(max_steps=3)))
    assert store.get_run(rid)["status"] == "stopped"
    assert sum(1 for s in store.steps(rid) if s["kind"] == "nudge") == 3


# ------------------------------------------------------------------------------------------------ retries
def test_rate_limit_that_never_clears_gives_up_cleanly():
    world = FakeWorld({"list_payments": ["429"] * 10})
    out = Gateway(world, Manifest(TOOLS), sleep=lambda s: None, jitter=False).execute("list_payments", {"customer_id": "C-01"})
    assert not out.ok and len(out.attempts) == 6 and "rate limited" in out.error


def test_rate_limited_refund_is_retried_with_the_same_key():
    world = FakeWorld({"refund_payment": ["429", "504_after"]})
    gw = Gateway(world, Manifest(TOOLS), sleep=lambda s: None, jitter=False)
    args = {"payment_id": "PAY-2", "amount": 4999, "reason": "dup", "idempotency_key": "run:PAY-2:4999"}
    from flightrec.store import args_hash
    out = gw.execute("refund_payment", args, {"status": "approved", "args_hash": args_hash(args)})
    assert out.ok and len(world.refunds) == 1
    assert {a["body"]["idempotency_key"] for a in out.attempts} == {"run:PAY-2:4999"}


def test_llm_client_retries_transient_errors_but_not_bad_requests(monkeypatch):
    replies = [httpx.Response(429, text="slow down"), httpx.Response(503, text="busy"),
               httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})]
    sent = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append(1) or replies.pop(0))
    llm = OpenAICompatLLM("http://llm", "key", "m", sleep=lambda s: None)
    r = llm.complete([{"role": "user", "content": "x"}], [])
    assert r.attempts == 3 and (r.tokens_in, r.tokens_out) == (7, 3)
    sent.clear()
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append(1) or httpx.Response(400, text="bad tool schema"))
    with pytest.raises(LLMError):
        llm.complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 1


# ------------------------------------------------------------------------------------------------ keys and replay
def test_live_run_without_key_is_refused_up_front(tmp_path):
    svc = Service(settings(tmp_path))
    with pytest.raises(LLMError):
        svc.create_live("T-101")
    assert svc.store.list_runs() == []  # no half-created run left behind


def test_replay_of_a_replay_is_still_identical(tmp_path):
    svc = Service(settings(tmp_path))
    world = FakeWorld({"refund_payment": ["504_after"], "search_kb": ["broken"]})
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("search_kb", query="refund")],
                       [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")],
                       [call("add_ticket_note", ticket_id="T-101", note="refunded duplicate")],
                       [call("finish", status="succeeded", summary="done")]])
    rid = svc.runner.start("T-101", GOAL, deps(llm, world))
    approve_all(svc.store, rid)
    svc.runner.advance(rid, deps(llm, world))
    first = svc.replay(rid)
    second = svc.replay(first["run_id"])
    assert first["identical"] and second["identical"], (first, second)


def test_replay_of_a_rejected_approval_reproduces_the_rejection(tmp_path):
    svc = Service(settings(tmp_path))
    world = FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")],
                       [call("finish", status="needs_human", summary="rejected")]])
    rid = svc.runner.start("T-101", GOAL, deps(llm, world))
    approve_all(svc.store, rid, ok=False, note="outside policy")
    svc.runner.advance(rid, deps(llm, world))
    res = svc.replay(rid)
    assert res["identical"] and res["status_replay"] == "needs_human"


# ------------------------------------------------------------------------------------------------ security review fixes
def test_crash_recovery_with_unreadable_audit_does_not_resend(tmp_path):
    class NoAuditAfterCrash(CrashAfterEmail):
        def request(self, method, path, body=None):
            if path == "/audit" and not self.crash:
                return Exchange(method, path, body, 503, "{}", 1.0)
            return super().request(method, path, body)
    db, world = tmp_path / "db.sqlite", NoAuditAfterCrash()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")],
                       [call("send_email", customer_id="C-01", subject="Refund", body="Done.")],
                       [call("finish", status="needs_human", summary="unsure if email went out")]])
    store = Store(db)
    rid = Runner(store).start("T-101", GOAL, deps(llm, world))
    approve_all(store, rid)
    with pytest.raises(SimulatedCrash):
        Runner(store).advance(rid, deps(llm, world))
    assert Runner(Store(db)).advance(rid, deps(llm, world)) == "needs_human"
    assert len(world.executed("send_email")) == 1
    step = next(s for s in Store(db).steps(rid) if s["kind"] == "tool" and s["data"]["name"] == "send_email")
    assert step["data"]["status"] == "error" and "escalate" in step["data"]["error"]


@pytest.mark.parametrize("body", ["history of c-09", "Ｃ－０９ paid", "C‑09 and C−09", "payment pay - 9"])
def test_email_leak_guard_survives_case_and_unicode_tricks(tmp_path, body):
    store = Store(tmp_path / "db.sqlite")
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("send_email", customer_id="C-01", subject="Info", body=body)]])
    rid = Runner(store).start("T-101", GOAL, deps(llm, FakeWorld()))
    email = next(s["data"] for s in store.steps(rid) if s["kind"] == "tool" and s["data"]["name"] == "send_email")
    assert email["status"] == "blocked" and store.approvals(rid) == []


def test_other_customers_data_never_enters_the_conversation(tmp_path):
    store, world = Store(tmp_path / "db.sqlite"), FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-09")],
                       [call("get_customer", customer_id="C-09")], [call("finish", status="needs_human", summary="x")]])
    rid = Runner(store).start("T-101", GOAL, deps(llm, world))
    assert [s["data"]["status"] for s in store.steps(rid) if s["kind"] == "tool"][1:3] == ["blocked", "blocked"]
    assert [n for n, _ in world.calls] == ["get_ticket"]


@pytest.mark.parametrize("header", ["99999", "-5", "nan", "soon"])
def test_retry_after_is_clamped(header):
    class OddRetryAfter(FakeWorld):
        def request(self, method, path, body=None):
            if path.startswith("/tools/") and not self.calls:  # first tool call: 429 with a hostile header
                self.calls.append(("429", {}))
                return Exchange(method, path, body, 429, "{}", 1.0, headers={"Retry-After": header})
            return super().request(method, path, body)
    slept = []
    out = Gateway(OddRetryAfter(), Manifest(TOOLS), sleep=slept.append, jitter=False).execute("get_ticket", {"ticket_id": "T-101"})
    assert out.ok and 0 <= slept[0] <= 30


@pytest.mark.parametrize("bad", ["../../etc/x", "T-101/../../x", "", "T-1; DROP", "t-101"])
def test_task_ids_are_strict(tmp_path, bad):
    with pytest.raises(ValueError):
        Store(tmp_path / "db.sqlite").create_run(bad, "goal", "m")


def test_agent_reads_only_its_own_ticket(tmp_path):
    store, world = Store(tmp_path / "db.sqlite"), FakeWorld()
    llm = ScriptedLLM([[call("search_tickets")], [call("get_ticket", ticket_id="T-102")],
                       [call("finish", status="needs_human", summary="x")]])
    rid = Runner(store).start("T-101", GOAL, deps(llm, world))
    assert [s["data"]["status"] for s in store.steps(rid) if s["kind"] == "tool"][:2] == ["blocked", "blocked"]
    assert world.calls == []


def test_replay_mirrors_the_trace_even_if_an_approval_landed_after_it(tmp_path):
    """Approved in the UI, but the original hasn't executed it yet: the replay must stop where the trace stops."""
    svc = Service(settings(tmp_path))
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="dup")]])
    rid = svc.runner.start("T-101", GOAL, deps(llm, FakeWorld()))
    approve_all(svc.store, rid)  # decided, but the original run has not been advanced yet
    res = svc.replay(rid)
    assert res["identical"] and res["status_replay"] == "awaiting_approval"


def test_replay_refuses_a_run_that_is_executing(tmp_path):
    from flightrec.service import RunBusy
    svc = Service(settings(tmp_path))
    rid = svc.store.create_run("T-101", GOAL, "scripted")  # status "running"
    with pytest.raises(RunBusy):
        svc.replay(rid)


def test_llm_429_waits_for_the_providers_retry_delay(monkeypatch):
    from flightrec.gateway import RateLimiter
    body = '{"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}'
    replies = [httpx.Response(429, text=body),
               httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: replies.pop(0))
    slept, now = [], [0.0]
    lim = RateLimiter(1, clock=lambda: now[0], sleep=lambda s: None)
    llm = OpenAICompatLLM("http://llm", "key", "m", sleep=slept.append, limiter=lim)
    assert llm.complete([{"role": "user", "content": "x"}], []).attempts == 2
    assert 34 <= slept[0] < 36           # honoured the provider's delay, not our 2s backoff
    assert lim.acquire() >= 33           # and every other agent was told to wait too
