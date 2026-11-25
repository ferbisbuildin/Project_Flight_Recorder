"""SQLite store: runs, steps (append-only trace) and approvals.

Every step is committed before the runner takes its next action, so a crash or restart loses nothing that happened.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
    summary TEXT, model TEXT, mode TEXT NOT NULL DEFAULT 'live', replay_of TEXT,
    created_at REAL NOT NULL, ended_at REAL, steps INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
    active_seconds REAL NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL
);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
    latency_ms REAL, data TEXT NOT NULL, msg TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals (
    run_id TEXT NOT NULL, call_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, args_hash TEXT NOT NULL,
    reason TEXT, status TEXT NOT NULL, note TEXT, created_at REAL NOT NULL, decided_at REAL,
    PRIMARY KEY (run_id, call_id)
);
"""

TERMINAL = {"succeeded", "failed", "stopped", "needs_human"}
TASK_ID = re.compile(r"T-\d{1,6}")


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    # ---------------------------------------------------------------- runs
    def create_run(self, task_id: str, goal: str, model: str, mode: str = "live", replay_of: str | None = None) -> str:
        if not TASK_ID.fullmatch(task_id or ""):
            raise ValueError(f"invalid task id {task_id!r}: expected e.g. T-101")
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self._lock:
            self._db.execute("INSERT INTO runs (id, task_id, goal, status, model, mode, replay_of, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", (run_id, task_id, goal, "running", model, mode, replay_of, time.time()))
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def list_runs(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def claim(self, run_id: str, owner: str, ttl: float = 600.0) -> bool:
        """Atomic lease so only one worker (thread OR process) advances a run at a time. Expires if the holder dies."""
        now = time.time()
        with self._lock:
            cur = self._db.execute("UPDATE runs SET lease_owner=?, lease_until=? WHERE id=? AND "
                                   "(lease_owner IS NULL OR lease_owner=? OR lease_until < ?)", (owner, now + ttl, run_id, owner, now))
        return cur.rowcount == 1

    def release(self, run_id: str, owner: str) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE id=? AND lease_owner=?", (run_id, owner))

    def add_usage(self, run_id: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, cost=cost+? WHERE id=?",
                             (tokens_in, tokens_out, cost, run_id))

    def add_active_seconds(self, run_id: str, seconds: float) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET active_seconds=active_seconds+? WHERE id=?", (seconds, run_id))

    # ---------------------------------------------------------------- steps
    def add_step(self, run_id: str, kind: str, data: dict, msg: dict | None = None, latency_ms: float | None = None) -> int:
        with self._lock:
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
            self._db.execute("INSERT INTO steps (run_id, seq, kind, ts, latency_ms, data, msg) VALUES (?,?,?,?,?,?,?)",
                             (run_id, seq, kind, time.time(), latency_ms, json.dumps(data), json.dumps(msg) if msg else None))
            self._db.execute("UPDATE runs SET steps=? WHERE id=?", (seq, run_id))
        return seq

    def steps(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"]), "msg": json.loads(r["msg"]) if r["msg"] else None} for r in rows]

    # ---------------------------------------------------------------- approvals
    def request_approval(self, run_id: str, call_id: str, tool: str, args: dict, reason: str) -> dict:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO approvals (run_id, call_id, tool, args, args_hash, reason, status, created_at) "
                             "VALUES (?,?,?,?,?,?,'pending',?)", (run_id, call_id, tool, json.dumps(args), args_hash(args), reason, time.time()))
        return self.get_approval(run_id, call_id)

    def decide(self, run_id: str, call_id: str, approve: bool, note: str = "") -> dict:
        with self._lock:
            cur = self._db.execute("UPDATE approvals SET status=?, note=?, decided_at=? WHERE run_id=? AND call_id=? AND status='pending'",
                                   ("approved" if approve else "rejected", note, time.time(), run_id, call_id))
            if cur.rowcount == 0:
                raise ValueError("no pending approval with that id")
        return self.get_approval(run_id, call_id)

    def get_approval(self, run_id: str, call_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM approvals WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
        return {**dict(r), "args": json.loads(r["args"])} if r else None

    def approvals(self, run_id: str | None = None, status: str | None = None) -> list[dict]:
        q, p = "SELECT * FROM approvals WHERE 1=1", []
        if run_id:
            q, p = q + " AND run_id=?", p + [run_id]
        if status:
            q, p = q + " AND status=?", p + [status]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY created_at", p).fetchall()
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]
