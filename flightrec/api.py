"""HTTP API + the viewer page. Runs advance in a small background pool so requests return immediately."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import settings
from .llm import LLMError
from .service import RunBusy, Service

svc = Service(settings())
pool = ThreadPoolExecutor(max_workers=svc.cfg.workers, thread_name_prefix="run")  # runs in parallel; billing calls share one limiter
PAGE = Path(__file__).parent / "static" / "index.html"


def advance_later(run_id: str) -> None:
    pool.submit(svc.advance, run_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # After a restart, pick up runs that were mid-flight (their lease expires if the old process died).
    for r in svc.store.list_runs():
        if r["status"] == "running" and r["mode"] == "live" and svc.cfg.llm_api_key:
            advance_later(r["id"])
    yield
    pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Flight Recorder", lifespan=lifespan)


class StartRun(BaseModel):
    task_id: str
    model: str | None = None


class Kill(BaseModel):
    reason: str = ""


class Decision(BaseModel):
    approve: bool
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
        a = svc.decide(run_id, call_id, body.approve, body.note.strip())
    except (ValueError, RunBusy) as e:
        raise HTTPException(409, str(e))
    advance_later(run_id)
    return a
