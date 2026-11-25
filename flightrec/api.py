"""HTTP API + the viewer page. Runs advance in a small background pool so requests return immediately."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import os
import threading
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .config import ROOT, settings
from .llm import LLMError
from .service import RunBusy, Service

svc = Service(settings())
pool = ThreadPoolExecutor(max_workers=svc.cfg.workers, thread_name_prefix="run")  # runs in parallel; billing calls share one limiter
PAGE = Path(__file__).parent / "static" / "index.html"


def advance_later(run_id: str) -> None:
    pool.submit(svc.advance, run_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Recorded traces ship in runs/final/: load them so a fresh clone can view and replay with no LLM key.
    svc.import_traces(Path(os.environ.get("TRACES_DIR", ROOT / "runs" / "final")))
    stop = threading.Event()

    def janitor():  # every 30s: resume live runs whose worker died (crash, restart) once their lease has expired
        while not stop.wait(30):
            if svc.cfg.llm_api_key:
                for rid in svc.stalled():
                    advance_later(rid)

    if svc.cfg.llm_api_key:
        for rid in svc.stalled():
            advance_later(rid)
    threading.Thread(target=janitor, daemon=True, name="janitor").start()
    yield
    stop.set()
    pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Flight Recorder", lifespan=lifespan)

# The viewer has no login and is meant for this machine only. Two cheap locks against a hostile web page:
# 1) only answer requests addressed to a local host name (blocks DNS rebinding)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


# 2) refuse state-changing requests sent by another site (a cross-site form or fetch to localhost)
@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


class StartRun(BaseModel):
    task_id: str
    model: str | None = None


class Kill(BaseModel):
    reason: str = ""


class Guidance(BaseModel):
    guidance: str = ""


class Decision(BaseModel):
    decision: Literal["approved", "rejected", "changes_requested"]
    note: str = ""


@app.get("/")
def page() -> FileResponse:
    return FileResponse(PAGE)


@app.get("/api/config")
def config() -> dict:
    return {"model": svc.models[0], "models": svc.models, "live_enabled": bool(svc.cfg.llm_api_key),
            "world_url": svc.cfg.world_url, "budget": vars(svc.cfg.budget), "usd_inr": svc.cfg.usd_inr,
            "kill": svc.store.kill_state(), "today": svc.cost_today()}


@app.post("/api/kill")
def kill(body: Kill) -> dict:
    """The kill switch: every agent stops at its next safe point; nothing new starts until it is released."""
    return {"stopped_now": svc.kill_all(body.reason.strip()), "kill": svc.store.kill_state()}


@app.post("/api/kill/release")
def release() -> dict:
    svc.release_kill()
    return {"kill": None}


@app.get("/api/tasks")
def tasks() -> list[dict]:
    return svc.tasks()


@app.get("/api/runs")
def runs() -> list[dict]:
    return svc.store.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str) -> dict:
    r = svc.store.get_run(run_id)
    if not r:
        raise HTTPException(404, "no such run")
    return {"run": r, "steps": svc.store.steps(run_id), "approvals": svc.store.approvals(run_id)}


@app.post("/api/runs")
def start(body: StartRun) -> dict:
    try:
        rid = svc.create_live(body.task_id, model=body.model)
    except (LLMError, ValueError) as e:
        raise HTTPException(400, str(e))
    except RunBusy as e:
        raise HTTPException(409, str(e))
    advance_later(rid)
    return {"run_id": rid}


@app.post("/api/runs/{run_id}/follow-up")
def follow_up(run_id: str, body: Guidance) -> dict:
    """Answer an escalation: a new run for the same ticket, carrying the person's guidance."""
    try:
        rid = svc.follow_up(run_id, body.guidance)
    except KeyError:
        raise HTTPException(404, "no such run")
    except (ValueError, LLMError) as e:
        raise HTTPException(400, str(e))
    except RunBusy as e:
        raise HTTPException(409, str(e))
    advance_later(rid)
    return {"run_id": rid}


@app.post("/api/runs/{run_id}/handled")
def handled(run_id: str, body: Guidance) -> dict:
    try:
        svc.mark_handled(run_id, body.guidance)
    except KeyError:
        raise HTTPException(404, "no such run")
    return {"ok": True}


@app.post("/api/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    if not svc.store.get_run(run_id):
        raise HTTPException(404, "no such run")
    advance_later(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/replay")
def replay(run_id: str) -> dict:
    try:
        return svc.replay(run_id)
    except KeyError:
        raise HTTPException(404, "no such run")
    except RunBusy as e:
        raise HTTPException(409, str(e))


@app.get("/api/approvals")
def approvals(status: str | None = "pending") -> list[dict]:
    return svc.store.approvals(status=status)


@app.post("/api/approvals/{run_id}/{call_id}")
def decide(run_id: str, call_id: str, body: Decision) -> dict:
    try:
        a = svc.decide(run_id, call_id, body.decision, body.note.strip())
    except (ValueError, RunBusy) as e:
        raise HTTPException(409, str(e))
    advance_later(run_id)
    return a
