"""Settings from the environment (and a local .env file, if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Budget:
    max_steps: int = 25          # LLM calls per run
    max_seconds: float = 300.0   # active wall-clock time (time spent waiting for a human is not counted)
    max_tokens: int = 200_000    # prompt + completion tokens


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    price_in_per_m: float        # USD per 1M prompt tokens
    price_out_per_m: float       # USD per 1M completion tokens
    world_url: str
    db_path: Path
    budget: Budget


def settings() -> Settings:
    e = os.environ.get
    return Settings(
        llm_base_url=e("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        llm_api_key=e("LLM_API_KEY", ""),
        llm_model=e("LLM_MODEL", "gemini-3.5-flash-lite"),
        price_in_per_m=float(e("LLM_PRICE_IN_PER_M", "0.10")),
        price_out_per_m=float(e("LLM_PRICE_OUT_PER_M", "0.40")),
        world_url=e("WORLD_URL", "http://127.0.0.1:7070"),
        db_path=Path(e("DB_PATH", str(ROOT / "data" / "flightrec.db"))),
        budget=Budget(int(e("MAX_STEPS", "25")), float(e("MAX_SECONDS", "300")), int(e("MAX_TOKENS", "200000"))),
    )
