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

`classify()` and `baseline_from_hourly()` are pure functions (no database), so
the browser demo runs the same thresholds against its simulated ledger. The
SQLite queries below compute the same baseline in SQL.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

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


def baseline_from_hourly(hour_totals: Iterable[float]) -> tuple[float, int]:
    """Baseline from one spend total per active hour: (average per active hour, number of hours)."""
    totals = list(hour_totals)
    return (sum(totals) / len(totals) if totals else 0.0), len(totals)


def classify(key: str, hour_spend: float, baseline_hourly: float, history_hours: int) -> SpendSignal:
    if history_hours < MIN_HISTORY_HOURS or baseline_hourly <= 0:
        return SpendSignal(key, hour_spend, baseline_hourly, history_hours, 0.0, "ok")
    multiple = hour_spend / baseline_hourly
    verdict = "pause" if multiple >= PAUSE_MULTIPLE else "flagged" if multiple >= FLAG_MULTIPLE else "ok"
    return SpendSignal(key, hour_spend, baseline_hourly, history_hours, multiple, verdict)


def _baseline_hourly(key: str) -> tuple[float, int]:
    from .store import connect  # imported lazily so the pure functions above work without the database

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
    from .store import connect

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
    signal = classify(key, hour, baseline, hist_hours)
    _cache[key] = (time.monotonic(), signal)
    return signal


def evaluate_all() -> list[SpendSignal]:
    from .store import connect

    with connect() as c:
        rows = c.execute("SELECT id FROM api_keys WHERE revoked_at IS NULL").fetchall()
    return [evaluate(r["id"]) for r in rows]  # usage rows reference keys by id


def clear_cache() -> None:
    _cache.clear()
