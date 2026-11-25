"""How the runtime talks to the Northwind world: a real HTTP client, and a replay stand-in that serves a recorded run."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx


@dataclass
class Exchange:
    """One HTTP request/response with the world, exactly as recorded in the trace."""
    method: str
    path: str
    body: dict | None
    status: int | None          # None = no response (timeout, connection error)
    text: str
    latency_ms: float
    error: str | None = None
    headers: dict | None = None  # only the ones we act on (Retry-After)

    def to_dict(self) -> dict:
        return asdict(self)


class World(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> Exchange: ...


class HttpWorld:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def request(self, method: str, path: str, body: dict | None = None) -> Exchange:
        t0 = time.perf_counter()
        try:
            r = self._client.request(method, path, json=body)
            ms = (time.perf_counter() - t0) * 1000
            hdr = {"Retry-After": r.headers["Retry-After"]} if "Retry-After" in r.headers else None
            return Exchange(method, path, body, r.status_code, r.text, round(ms, 1), headers=hdr)
        except httpx.HTTPError as e:
            ms = (time.perf_counter() - t0) * 1000
            return Exchange(method, path, body, None, "", round(ms, 1), error=f"{type(e).__name__}: {e}")


class ReplayDivergence(RuntimeError):
    """The replayed run asked the world for something the recorded run did not."""


class ReplayWorld:
    """Serves recorded exchanges in order. Never touches the network."""

    def __init__(self, exchanges: list[dict]):
        self._queue = list(exchanges)

    def request(self, method: str, path: str, body: dict | None = None) -> Exchange:
        if not self._queue:
            raise ReplayDivergence(f"replay asked for {method} {path} but the recording has no more world calls")
        x = self._queue.pop(0)
        if (x["method"], x["path"]) != (method, path) or json.dumps(x["body"], sort_keys=True) != json.dumps(body, sort_keys=True):
            raise ReplayDivergence(f"replay asked for {method} {path} {body}, recording has {x['method']} {x['path']} {x['body']}")
        return Exchange(**x)
