# Corey Mathie, 2026
"""Token estimation, TPM limiter, budget hierarchy, showback aggregation (pure functions)."""

import csv
import io
import math

import pytest

from router import showback, tokens
from router.budgets import Hierarchy, Limits, check, utilization
from router.tokens import TokenLimitExceeded, TokenRateLimiter

# ---------- estimation ----------


@pytest.fixture
def heuristic(monkeypatch):
    monkeypatch.setattr(tokens, "_encoding_tried", True)
    monkeypatch.setattr(tokens, "_encoding", None)


def test_heuristic_estimate_is_documented_formula(heuristic):
    msgs = [{"role": "system", "content": "x" * 40}, {"role": "user", "content": "y" * 9}]
    # 3 primer + (4 + 40/4) + (4 + ceil(9/4))
    assert tokens.estimate_prompt_tokens(msgs) == 3 + 14 + 7
    assert tokens.estimate_request_tokens(msgs, max_tokens=100, default_output=256) == 24 + 100
    assert tokens.estimate_request_tokens(msgs, max_tokens=None, default_output=256) == 24 + 256
    assert tokens.estimator_name() == "heuristic:chars/4"


def test_env_var_forces_heuristic(monkeypatch):
    monkeypatch.setattr(tokens, "_encoding_tried", False)
    monkeypatch.setattr(tokens, "_encoding", None)
    monkeypatch.setenv("ROUTER_TOKEN_ESTIMATOR", "heuristic")
    assert tokens.estimator_name() == "heuristic:chars/4"


def test_tiktoken_estimate_when_available(monkeypatch):
    tiktoken = pytest.importorskip("tiktoken")
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - encoding file not cached and no network
        pytest.skip("cl100k_base encoding not available offline")
    monkeypatch.setattr(tokens, "_encoding_tried", True)
    monkeypatch.setattr(tokens, "_encoding", enc)
    assert tokens.count_text_tokens("hello world") == 2
    assert tokens.estimator_name() == "tiktoken:cl100k_base"


# ---------- TPM limiter ----------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_tpm_reserve_and_reject_with_retry_after():
    clock = Clock()
    lim = TokenRateLimiter(clock)
    lim.reserve({"team:a": 1000}, 600)
    clock.t += 20
    lim.reserve({"team:a": 1000}, 300)
    with pytest.raises(TokenLimitExceeded) as exc:
        lim.reserve({"team:a": 1000}, 200)
    e = exc.value
    assert (e.scope, e.limit, e.used, e.requested) == ("team:a", 1000, 900, 200)
    assert e.retry_after == pytest.approx(40)  # the first 600 leave the window 60s after they arrived
    assert "per-team token rate limit" in str(e)


def test_tpm_window_slides():
    clock = Clock()
    lim = TokenRateLimiter(clock)
    lim.reserve({"key:x": 100}, 100)
    clock.t += 60
    lim.reserve({"key:x": 100}, 100)  # the old entry aged out
    assert lim.used("key:x") == 100


def test_settle_corrects_estimate_both_ways():
    lim = TokenRateLimiter(Clock())
    r = lim.reserve({"team:a": 1000}, 800)
    r.settle(150)  # the response was much smaller than estimated
    assert lim.used("team:a") == 150
    r2 = lim.reserve({"team:a": 1000}, 700)
    r2.settle(900)  # larger than estimated
    assert lim.used("team:a") == 1050
    with pytest.raises(TokenLimitExceeded):
        lim.reserve({"team:a": 1000}, 1)


def test_release_and_all_or_nothing():
    lim = TokenRateLimiter(Clock())
    r = lim.reserve({"team:a": 1000, "key:k": 500}, 400)
    r.release()
    assert lim.used("team:a") == 0 and lim.used("key:k") == 0
    lim.reserve({"key:k": 500}, 450)
    with pytest.raises(TokenLimitExceeded):
        lim.reserve({"team:a": 1000, "key:k": 500}, 100)  # key full -> team untouched
    assert lim.used("team:a") == 0


def test_zero_means_unlimited_and_oversized_requests_wait_a_full_window():
    lim = TokenRateLimiter(Clock())
    lim.reserve({"team:a": 0, "key:k": None}, 10**9)
    with pytest.raises(TokenLimitExceeded) as exc:
        lim.reserve({"team:a": 100}, 101)
    assert exc.value.retry_after == 60


# ---------- budget hierarchy ----------


def spends(table):
    return lambda scope, ident, period: table.get((scope, ident, period), 0.0)


H = Hierarchy(
    org=Limits(daily_usd=100, monthly_usd=1000),
    team_default=Limits(daily_usd=20, monthly_usd=0, tpm=5000),
    key_default=Limits(daily_usd=5),
    teams={"data": Limits(daily_usd=40, tpm=20000)},
    keys={"batch": Limits(daily_usd=15, monthly_usd=100)},
)


def test_overrides_inherit_unset_fields():
    assert H.team("data") == Limits(40, 0, 20000)
    assert H.team("product") == Limits(20, 0, 5000)
    assert H.key("batch") == Limits(15, 100, None)
    assert H.key("web") == Limits(5, None, None)


def test_under_every_cap_is_allowed():
    assert check(H, "data", "batch", "k1", spends({("key", "k1", "daily"): 14.99})) is None


@pytest.mark.parametrize(
    ("spent", "expected"),
    [
        ({("org", None, "monthly"): 1000}, ("org", "monthly", "org monthly spend cap reached")),
        ({("org", None, "daily"): 100, ("key", "k1", "daily"): 99}, ("org", "daily", "org daily spend cap reached")),
        ({("team", "data", "daily"): 40}, ("team", "daily", "per-team daily spend cap reached")),
        ({("key", "k1", "daily"): 15}, ("key", "daily", "per-key daily spend cap reached")),
        ({("key", "k1", "monthly"): 100}, ("key", "monthly", "per-key monthly spend cap reached")),
    ],
)
def test_first_breached_cap_broadest_first(spent, expected):
    b = check(H, "data", "batch", "k1", spends(spent))
    assert (b.scope, b.period, b.message) == expected


def test_zero_cap_means_no_cap():
    h = Hierarchy(key_default=Limits(daily_usd=0))
    assert check(h, "t", "k", "k1", spends({("key", "k1", "daily"): 10**6})) is None


def test_hierarchy_warnings_and_utilization():
    h = Hierarchy(org=Limits(daily_usd=10), team_default=Limits(daily_usd=5), teams={"big": Limits(daily_usd=50)})
    assert h.warnings() == ["team 'big' daily cap $50 exceeds the org cap $10"]
    u = utilization(h, ["big", "small"], spends({("team", "small", "daily"): 2.5, ("org", None, "daily"): 2.5}))
    assert u["teams"]["small"]["daily"] == {"spent_usd": 2.5, "cap_usd": 5.0, "used_pct": 50.0}
    assert u["org"]["daily"]["used_pct"] == 25.0 and u["org"]["monthly"]["cap_usd"] is None


# ---------- showback ----------

ROWS = [
    {"team": "product", "key_label": "web", "key_fp": "aaaa", "alias": "smart-fast", "provider": "openai",
     "model": "gpt-4.1-mini", "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.003, "error": None,
     "cached": 0, "saved_usd": 0.0},
    {"team": "product", "key_label": "web", "key_fp": "aaaa", "alias": "smart-fast", "provider": "openai",
     "model": "gpt-4.1-mini", "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.0, "error": None,
     "cached": 1, "saved_usd": 0.003},
    {"team": "product", "key_label": "web", "key_fp": "aaaa", "alias": "smart-fast", "provider": "anthropic",
     "model": "claude-haiku-4-5", "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "error": "timeout",
     "cached": 0, "saved_usd": 0.0},
    {"team": "data", "key_label": "=cmd()", "key_fp": "bbbb", "alias": "cheap-batch", "provider": "openai",
     "model": "gpt-4.1-nano", "prompt_tokens": 3000, "completion_tokens": 1000, "cost_usd": 0.001, "error": None,
     "cached": 0, "saved_usd": 0.0},
]  # fmt: skip


def test_aggregate_unit_metrics():
    rows = showback.aggregate(ROWS, ("team",))
    product = next(r for r in rows if r["team"] == "product")
    assert product["requests"] == 2 and product["cache_hits"] == 1 and product["failed_attempts"] == 1
    assert product["input_tokens"] == 1000 and product["output_tokens"] == 500  # cache hit not billed
    assert product["cost_per_1k_requests"] == pytest.approx(1.5)  # $0.003 over 2 requests
    assert product["cost_per_1k_tokens"] == pytest.approx(0.002)  # $0.003 over 1500 tokens
    assert product["share_of_cost"] == pytest.approx(0.75)
    assert product["saved_usd"] == pytest.approx(0.003)
    assert [r["team"] for r in rows] == ["product", "data"]  # most expensive first


def test_group_by_parsing():
    assert showback.parse_group_by(None) == showback.DEFAULT_GROUP_BY
    assert showback.parse_group_by("model, team,model") == ("model", "team")
    with pytest.raises(ValueError):
        showback.parse_group_by("team,secret")


def test_csv_escapes_formulas_and_has_unit_columns():
    grouped = showback.aggregate(ROWS, ("team", "key", "model"))
    parsed = list(csv.DictReader(io.StringIO(showback.to_csv(grouped, ("team", "key", "model")))))
    assert {"cost_per_1k_requests", "cost_per_1k_tokens", "key_fp"} <= set(parsed[0])
    assert any(r["key_label"] == "'=cmd()" for r in parsed)


def test_totals():
    t = showback.totals(ROWS)
    assert t["requests"] == 3 and t["failed_attempts"] == 1
    assert math.isclose(t["cost_usd"], 0.004)
