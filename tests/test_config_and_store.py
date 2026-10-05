# Corey Mathie, 2026
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from router import config_loader, store


def _ts(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_routes_load_with_policies():
    cfg = config_loader.current()
    assert [t.provider for t in cfg.aliases["smart-fast"]] == ["anthropic", "openai"]
    assert cfg.policies.per_key_daily_usd == 50
    assert cfg.policies.auto_pause_on_anomaly is False


def test_unknown_provider_fails_at_load(router_env):
    router_env.write_text("aliases:\n  bad:\n    - { provider: openaii, model: x }\n")
    with pytest.raises(ValidationError):
        config_loader.load_routes()


def test_time_windows_only_count_recent_calls():
    """Regression: ISO 'T' timestamps compared as text used to count the whole day as 'last 60 minutes'."""
    k = store.create_key("app")
    store.record_call(k.id, "default", "smart-fast", "openai", "gpt-4.1-mini", 1, 1, 0.01, error="boom", ts=_ts(3))
    store.record_call(k.id, "default", "smart-fast", "openai", "gpt-4.1-mini", 1, 1, 0.01, ts=_ts(0))
    errs = store.per_provider_errors(60)
    assert errs == [{"provider": "openai", "errors": 0, "total": 1}]


def test_spend_today_by_key_and_team():
    a = store.create_key("a", team="product")
    b = store.create_key("b", team="product")
    store.record_call(a.id, "product", "smart-fast", "openai", "m", 1, 1, 0.25)
    store.record_call(b.id, "product", "smart-fast", "openai", "m", 1, 1, 0.50)
    assert store.spend_today(key=a.id) == pytest.approx(0.25)
    assert store.spend_today(team="product") == pytest.approx(0.75)
    assert store.spend_today() == pytest.approx(0.75)
    assert store.calls_today() == 2
