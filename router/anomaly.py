# Corey Mathie, 2026
"""
Spend-velocity anomaly detection per API key.

Same idea as transaction-velocity checks in fraud operations, applied to LLM
spend: compare this hour's spend for a key against its own 7-day hourly
baseline, and flag it before it becomes an invoice problem.

  ok       under 3x baseline
  flagged  3x or more        -> shown on the dashboard
  pause    10x or more       -> blocked at the gateway if policies.auto_pause_on_anomaly is true

Baseline = total spend over the previous 7 days (excluding the current hour)
divided by the number of hours that had any spend. Keys with less than 24 hours
of history aren't judged; there's nothing to compare against yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .store import connect

MIN_HISTORY_HOURS = 24
FLAG_MULTIPLE = 3.0
PAUSE_MULTIPLE = 10.0
CACHE_SECONDS = 30.0

_cache: dict[str, tuple[float, SpendSignal]] = {}


@dataclass(frozen=True)
class SpendSignal:
    key: str
    hour_spend: float
    baseline_hourly: float
    history_hours: int
    multiple: float
    verdict: str  # "ok" | "flagged" | "pause"


def _baseline_hourly(key: str) -> tuple[float, int]:
    with connect() as c:
        row = c.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total,
                   COUNT(DISTINCT strftime('%Y-%m-%d %H', ts)) AS hours
            FROM usage
            WHERE key = ?
              AND error IS NULL
              AND datetime(ts) >= datetime('now', '-7 days')
              AND datetime(ts) < datetime('now', 'start of day', '+' || strftime('%H', 'now') || ' hours')
            """,
            (key,),
        ).fetchone()
    hours = int(row["hours"] or 0)
    return (float(row["total"]) / hours if hours else 0.0), hours


def _current_hour_spend(key: str) -> float:
    with connect() as c:
        row = c.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM usage
            WHERE key = ?
              AND error IS NULL
              AND datetime(ts) >= datetime('now', 'start of day', '+' || strftime('%H', 'now') || ' hours')
            """,
            (key,),
        ).fetchone()
    return float(row["total"] or 0.0)


def evaluate(key: str, use_cache: bool = False) -> SpendSignal:
    if use_cache:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]

    hour = _current_hour_spend(key)
    baseline, hist_hours = _baseline_hourly(key)
    if hist_hours < MIN_HISTORY_HOURS or baseline <= 0:
        signal = SpendSignal(key, hour, baseline, hist_hours, 0.0, "ok")
    else:
        multiple = hour / baseline
        verdict = "pause" if multiple >= PAUSE_MULTIPLE else "flagged" if multiple >= FLAG_MULTIPLE else "ok"
        signal = SpendSignal(key, hour, baseline, hist_hours, multiple, verdict)

    _cache[key] = (time.monotonic(), signal)
    return signal


def evaluate_all() -> list[SpendSignal]:
    with connect() as c:
        rows = c.execute("SELECT key FROM api_keys WHERE revoked_at IS NULL").fetchall()
    return [evaluate(r["key"]) for r in rows]


def clear_cache() -> None:
    _cache.clear()
