"""The viewer's API, end to end with a fake world and scripted model (no key, no network)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import flightrec.api as api
from flightrec.config import Budget, Settings
from flightrec.runner import Deps
from flightrec.service import Service
from tests.fakes import FakeWorld, ScriptedLLM, call


@pytest.fixture
def client(tmp_path, monkeypatch):
    svc = Service(Settings("http://llm", "", "scripted", 0.1, 0.4, "http://127.0.0.1:9", tmp_path / "db.sqlite", Budget()))
    world = FakeWorld()
    llm = ScriptedLLM([[call("get_ticket", ticket_id="T-101")], [call("list_payments", customer_id="C-01")],
                       [call("refund_payment", payment_id="PAY-2", amount=4999, reason="duplicate")],
                       [call("finish", status="succeeded", summary="refunded")]])
    svc.live_deps = lambda model=None: Deps(llm, world, Budget(), sleep=lambda s: None, jitter=False)
    monkeypatch.setattr(api, "svc", svc)
    monkeypatch.setattr(api, "advance_later", svc.advance)  # run synchronously in tests
    with TestClient(api.app) as c:
        yield c, svc, world


def test_without_a_key_the_viewer_works_but_live_runs_are_refused(client):
    c, _, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/api/config").json()["live_enabled"] is False
    r = c.post("/api/runs", json={"task_id": "T-101"})
    assert r.status_code == 400 and "LLM_API_KEY" in r.json()["detail"]


def test_approve_from_the_api_resumes_the_run(client):
    c, svc, world = client
    rid = svc.store.create_run("T-101", "Handle support ticket T-101 end to end.", "scripted")
    svc.advance(rid)
    [pending] = c.get("/api/approvals").json()
    assert pending["tool"] == "refund_payment" and pending["args"]["payment_id"] == "PAY-2"
    assert c.post(f"/api/approvals/{rid}/{pending['call_id']}", json={"approve": True, "note": "ok"}).status_code == 200
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["run"]["status"] == "succeeded" and len(world.refunds) == 1
    assert c.post(f"/api/approvals/{rid}/{pending['call_id']}", json={"approve": True}).status_code == 409  # decided once
    replay = c.post(f"/api/runs/{rid}/replay").json()
    assert replay["identical"]


def test_bad_input_is_rejected(client):
    c, _, _ = client
    assert c.get("/api/runs/nope").status_code == 404
    assert c.post("/api/runs", json={"task_id": "../../etc"}).status_code == 400
