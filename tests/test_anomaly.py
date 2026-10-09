# Corey Mathie, 2026
"""Spend-anomaly verdicts per key: new keys not judged, flagged at 3x and paused at 10x the 7-day hourly baseline,
failed calls not counted as spend."""

from datetime import UTC, datetime, timedelta

import pytest

from router import anomaly, store


def _ts(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_baseline(key: str, per_hour: float = 0.01, hours: int = 48) -> None:
    for h in range(2, 2 + hours):  # whole previous hours only
        store.record_call(key, "default", "smart-fast", "openai", "m", 10, 10, per_hour, ts=_ts(h))


def test_new_key_is_not_judged():
    k = store.create_key("new")
    store.record_call(k.id, "default", "smart-fast", "openai", "m", 10, 10, 5.0)
    assert anomaly.evaluate(k.id).verdict == "ok"


def test_flagged_at_3x_baseline():
    k = store.create_key("steady")
    _seed_baseline(k.id)
    store.record_call(k.id, "default", "smart-fast", "openai", "m", 10, 10, 0.05)  # 5x
    s = anomaly.evaluate(k.id)
    assert s.history_hours == 48
    assert s.baseline_hourly == pytest.approx(0.01)
    assert s.verdict == "flagged" and 4.9 < s.multiple < 5.1


def test_pause_at_10x_baseline():
    k = store.create_key("runaway")
    _seed_baseline(k.id)
    store.record_call(k.id, "default", "smart-fast", "openai", "m", 10, 10, 0.20)  # 20x
    assert anomaly.evaluate(k.id).verdict == "pause"


def test_failed_calls_do_not_count_as_spend():
    k = store.create_key("errors")
    _seed_baseline(k.id)
    store.record_call(k.id, "default", "smart-fast", "openai", "m", 0, 0, 0.0, error="timeout")
    assert anomaly.evaluate(k.id).hour_spend == 0.0
