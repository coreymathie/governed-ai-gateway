# Corey Mathie, 2026
"""Circuit breaker state machine: every transition, driven by a fake clock."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from router.breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    BreakerConfig,
    BreakerRegistry,
    counts_as_failure,
    retry_after_seconds,
)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class ProviderError(Exception):
    def __init__(self, status_code=None, headers=None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


def make(**overrides):
    clock = Clock()
    cfg = BreakerConfig(**{"failure_threshold": 3, "cooldown_seconds": 30.0, **overrides})
    reg = BreakerRegistry(cfg, clock=clock)
    return reg, reg.get("anthropic/claude-haiku-4-5"), clock


def test_starts_closed_and_allows():
    _, b, _ = make()
    assert b.current_state() == CLOSED and b.allow_request()


def test_closed_to_open_after_consecutive_failures():
    reg, b, _ = make()
    for _ in range(2):
        b.record_failure()
    assert b.current_state() == CLOSED
    b.record_failure()
    assert b.current_state() == OPEN
    assert not b.allow_request()
    t = reg.recent_transitions()[0]
    assert (t["from"], t["to"]) == (CLOSED, OPEN) and "3 consecutive" in t["reason"]


def test_success_resets_consecutive_count():
    _, b, _ = make()
    for _ in range(2):
        b.record_failure()
    b.record_success()
    for _ in range(2):
        b.record_failure()
    assert b.current_state() == CLOSED


def test_closed_to_open_on_error_rate_in_window():
    _, b, _ = make(failure_threshold=100, min_requests=10, error_rate_threshold=0.5)
    for _ in range(5):
        b.record_success()
        b.record_failure()
    assert b.current_state() == OPEN
    assert "error rate 5/10" in b.last_reason


def test_error_rate_ignores_calls_outside_window():
    _, b, clock = make(failure_threshold=100, min_requests=4, error_rate_threshold=0.5, window_seconds=60)
    b.record_failure()
    b.record_failure()
    clock.advance(61)  # those two age out
    b.record_success()
    b.record_success()
    b.record_success()
    b.record_failure()
    assert b.current_state() == CLOSED  # 1/4 in the window, not 3/6


def test_open_to_half_open_after_cooldown():
    reg, b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.advance(29.9)
    assert b.current_state() == OPEN and b.retry_in() == pytest.approx(0.1)
    clock.advance(0.1)
    assert b.current_state() == HALF_OPEN
    assert reg.recent_transitions()[0]["reason"] == "cooldown elapsed"


def test_half_open_limits_concurrent_probes():
    _, b, clock = make(half_open_max_probes=1)
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.allow_request() is True  # the probe
    assert b.allow_request() is False  # everyone else keeps falling through


def test_half_open_to_closed_on_probe_success():
    reg, b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.allow_request()
    b.record_success()
    assert b.current_state() == CLOSED
    assert [(t["from"], t["to"]) for t in reg.recent_transitions()][::-1] == [
        (CLOSED, OPEN),
        (OPEN, HALF_OPEN),
        (HALF_OPEN, CLOSED),
    ]


def test_half_open_to_open_on_probe_failure():
    _, b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.allow_request()
    b.record_failure()
    assert b.current_state() == OPEN and "probe failed" in b.last_reason
    assert b.retry_in() == pytest.approx(30)  # a fresh cooldown


def test_success_threshold_needs_several_probes():
    _, b, clock = make(success_threshold=2, half_open_max_probes=2)
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.allow_request() and b.allow_request()
    b.record_success()
    assert b.current_state() == HALF_OPEN
    b.record_success()
    assert b.current_state() == CLOSED


def test_release_frees_a_probe_slot_without_judging():
    _, b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.advance(30)
    assert b.allow_request()
    b.release()  # e.g. the caller sent a bad request, or the client disconnected
    assert b.current_state() == HALF_OPEN and b.allow_request()


def test_retry_after_on_429_opens_immediately_for_that_long():
    _, b, clock = make(failure_threshold=10)
    err = ProviderError(429, {"retry-after": "12"})
    b.record_failure(retry_after_seconds(err))
    assert b.current_state() == OPEN and "Retry-After 12s" in b.last_reason
    clock.advance(11.9)
    assert b.current_state() == OPEN
    clock.advance(0.1)
    assert b.current_state() == HALF_OPEN


def test_retry_after_is_capped():
    _, b, _ = make(max_retry_after_seconds=60)
    b.record_failure(retry_after=3600)
    assert b.retry_in() == pytest.approx(60)


def test_retry_after_parsing():
    assert retry_after_seconds(ProviderError(503, {"Retry-After": "7"})) == 7
    when = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    now = when - timedelta(seconds=30)
    assert retry_after_seconds(ProviderError(429, {"retry-after": format_datetime(when, usegmt=True)}), now) == 30
    assert retry_after_seconds(ProviderError(500, {"retry-after": "7"})) is None  # only 429/503
    assert retry_after_seconds(ProviderError(429, {})) is None
    assert retry_after_seconds(ProviderError(429, {"retry-after": "soon"})) is None


def test_which_errors_count_against_a_provider():
    assert counts_as_failure(ConnectionError("refused"))
    assert counts_as_failure(TimeoutError())
    assert counts_as_failure(ProviderError(500)) and counts_as_failure(ProviderError(503))
    assert counts_as_failure(ProviderError(429)) and counts_as_failure(ProviderError(401))
    assert not counts_as_failure(ProviderError(400))  # caller's fault
    assert not counts_as_failure(ProviderError(422))


def test_disabled_breaker_never_opens():
    _, b, _ = make(enabled=False)
    for _ in range(10):
        b.record_failure()
    assert b.allow_request() and b.current_state() == CLOSED


def test_registry_listeners_and_reconfigure_keep_state():
    reg, b, _ = make()
    seen = []
    reg.listeners.append(lambda t: seen.append(t.to_state))
    for _ in range(3):
        b.record_failure()
    reg.configure(BreakerConfig(failure_threshold=50))
    assert reg.get("anthropic/claude-haiku-4-5").current_state() == OPEN
    assert b.config.failure_threshold == 50
    assert seen == [OPEN]
    snap = reg.snapshot()[0]
    assert snap["state"] == OPEN and snap["state_value"] == 2 and snap["window_errors"] == 3
