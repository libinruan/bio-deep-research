"""What the model actually costs, in dollars.

The harness reports a `total_cost_usd` priced at Anthropic's rates whatever endpoint was
used, so on another provider it is wrong — on one measured call it said $0.032 for work
GLM-5.3 charges about $0.0002 for. What the harness does report accurately is **tokens**,
so cost is recomputed here from the token counts and the provider's own price list.

Every model call appends one line to `data/usage.jsonl`. Nothing is ever rewritten, so a
reset only moves a marker: running totals start again while the history stays intact.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "usage.jsonl"
STATE = DATA / "usage-state.json"
PRICES = DATA / "pricing.json"

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
PRICE_TTL = 24 * 3600
HOUR, DAY = 3600, 86400


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens, plus where the figures came from."""

    inp: float
    out: float
    cache_read: float
    source: str

    def cost(self, tokens: dict[str, int]) -> float:
        # A cache write is billed at the ordinary input rate unless a provider says otherwise,
        # which is the case for every model this app has been pointed at so far.
        billed_input = tokens["input"] + tokens["cache_write"]
        return (billed_input * self.inp
                + tokens["cache_read"] * self.cache_read
                + tokens["output"] * self.out) / 1_000_000


def tokens_from(usage: dict[str, Any] | None) -> dict[str, int]:
    """The four counts that matter, from the harness's usage block."""
    usage = usage or {}
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
    }


# --------------------------------------------------------------------------- prices

def _env_price() -> Price | None:
    """A manual override, for providers with no machine-readable price list."""
    try:
        inp = float(os.environ["BDR_PRICE_IN"])
        out = float(os.environ["BDR_PRICE_OUT"])
    except (KeyError, ValueError):
        return None
    cache = float(os.environ.get("BDR_PRICE_CACHE_READ", inp))
    return Price(inp, out, cache, "BDR_PRICE_IN / BDR_PRICE_OUT")


def _cached_prices() -> dict[str, Any] | None:
    try:
        blob = json.loads(PRICES.read_text())
    except (OSError, ValueError):
        return None
    return blob if time.time() - blob.get("fetched", 0) < PRICE_TTL else None


async def _openrouter_prices() -> dict[str, Any]:
    blob = _cached_prices()
    if blob:
        return blob["models"]
    resp = await sources.client().get(OPENROUTER_MODELS, timeout=30.0)
    resp.raise_for_status()
    models = {}
    for entry in resp.json().get("data", []):
        p = entry.get("pricing") or {}
        try:
            models[entry["id"]] = {
                "in": float(p.get("prompt", 0)) * 1e6,
                "out": float(p.get("completion", 0)) * 1e6,
                "cache_read": float(p["input_cache_read"]) * 1e6 if p.get("input_cache_read") else None,
            }
        except (TypeError, ValueError):
            continue
    DATA.mkdir(parents=True, exist_ok=True)
    PRICES.write_text(json.dumps({"fetched": time.time(), "models": models}))
    return models


async def price_for(model: str, base_url: str, third_party: bool) -> Price | None:
    """Per-million rates for the configured engine, or None if they cannot be established."""
    override = _env_price()
    if override:
        return override
    if not third_party:
        return None  # the harness prices Claude correctly; its own figure is used instead
    if "openrouter.ai" not in base_url:
        return None
    try:
        models = await _openrouter_prices()
    except Exception:
        return None
    row = models.get(model)
    if not row:
        return None
    return Price(row["in"], row["out"],
                 row["cache_read"] if row["cache_read"] is not None else row["in"],
                 "openrouter.ai")


# --------------------------------------------------------------------------- recording

def record(*, kind: str, engine: str, tokens: dict[str, int], cost: float | None,
           basis: str, seconds: float | None = None, thread_id: str | None = None) -> None:
    """Append one model call to the log. Never raises: accounting must not fail a search."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        entry = {"t": time.time(), "kind": kind, "engine": engine, "cost": cost,
                 "basis": basis, "seconds": seconds, "thread": thread_id, **tokens}
        with LOG.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _events() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn final line, if the process died mid-write
    return out


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def reset() -> float:
    """Start the running total again, keeping the history."""
    at = time.time()
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"reset_at": at}))
    return at


def _bucket(events: list[dict[str, Any]], since: float) -> dict[str, Any]:
    chosen = [e for e in events if e["t"] >= since]
    known = [e["cost"] for e in chosen if e.get("cost") is not None]
    return {
        "cost": round(sum(known), 4),
        "calls": len(chosen),
        "unpriced": len(chosen) - len(known),
        "input": sum(e.get("input", 0) + e.get("cache_write", 0) + e.get("cache_read", 0) for e in chosen),
        "output": sum(e.get("output", 0) for e in chosen),
    }


def summary(days: int = 14) -> dict[str, Any]:
    """Totals for the current hour, today, since the last reset, and all time."""
    events = _events()
    now = time.time()
    midnight = now - (now % DAY) if time.localtime().tm_gmtoff is None else (
        time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    reset_at = _state().get("reset_at", 0)

    by_day = []
    for back in range(days - 1, -1, -1):
        start = midnight - back * DAY
        bucket = _bucket([e for e in events if start <= e["t"] < start + DAY], start)
        by_day.append({"date": time.strftime("%Y-%m-%d", time.localtime(start)), **bucket})

    by_hour = []
    hour_start = now - (now % HOUR)
    for back in range(23, -1, -1):
        start = hour_start - back * HOUR
        bucket = _bucket([e for e in events if start <= e["t"] < start + HOUR], start)
        by_hour.append({"hour": time.strftime("%H:00", time.localtime(start)), **bucket})

    return {
        "hour": _bucket(events, hour_start),
        "today": _bucket(events, midnight),
        "since_reset": {**_bucket(events, reset_at), "at": reset_at or None},
        "all_time": _bucket(events, 0),
        "by_day": by_day,
        "by_hour": by_hour,
        "first_event": min((e["t"] for e in events), default=None),
    }
