# Corey Mathie, 2026
"""Latency-aware ordering and the provider fallback chain (no HTTP, no LiteLLM)."""

import asyncio
from dataclasses import dataclass

import pytest

from router.breaker import OPEN, BreakerConfig, BreakerRegistry
from router.fallback import ChainExhausted, deployment_id, plan, run_chain
from router.latency import LatencyConfig, LatencyTracker


@dataclass(frozen=True)
class T:
    provider: str
    model: str


A, B, C = T("anthropic", "haiku"), T("openai", "mini"), T("ollama", "llama")
ROUTE = [A, B, C]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Fail(Exception):
    def __init__(self, status_code=None, retry_after=None):
        super().__init__("fail")
        self.status_code = status_code
        self.retry_after = retry_after


def tracker(**kw):
    return LatencyTracker(LatencyConfig(**{"alpha": 0.5, "min_samples": 2, "tolerance": 0.1, **kw}))


def feed(lt, dep, *values, kind="latency"):
    for v in values:
        lt.observe(dep, v, kind)


# ---------- ordering ----------


def test_default_strategy_keeps_configured_order():
    lt = tracker()
    feed(lt, "anthropic/haiku", 5, 5)
    feed(lt, "openai/mini", 0.1, 0.1)
    assert plan(ROUTE, "ordered", lt) == ROUTE


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        plan(ROUTE, "random", None)


def test_ewma_math_and_min_samples():
    lt = tracker()
    lt.observe("x", 1.0)
    assert lt.ewma("x") is None  # needs 2 samples
    lt.observe("x", 3.0)
    assert lt.ewma("x") == pytest.approx(2.0)  # 0.5*3 + 0.5*1
    lt.observe("x", 2.0)
    assert lt.ewma("x") == pytest.approx(2.0)


def test_latency_orders_fastest_first():
    lt = tracker()
    feed(lt, "anthropic/haiku", 2.0, 2.0)
    feed(lt, "openai/mini", 0.5, 0.5)
    feed(lt, "ollama/llama", 1.0, 1.0)
    assert lt.order(ROUTE, deployment_id) == [B, C, A]


def test_unmeasured_deployments_go_first_in_configured_order():
    lt = tracker()
    feed(lt, "anthropic/haiku", 0.2, 0.2)
    assert lt.order(ROUTE, deployment_id) == [B, C, A]  # B and C still warming up


def test_near_ties_keep_configured_order():
    lt = tracker()
    feed(lt, "anthropic/haiku", 1.05, 1.05)  # within 10% of the fastest
    feed(lt, "openai/mini", 1.0, 1.0)
    feed(lt, "ollama/llama", 3.0, 3.0)
    assert lt.order(ROUTE, deployment_id) == [A, B, C]


def test_ttft_is_tracked_separately():
    lt = tracker()
    feed(lt, "anthropic/haiku", 0.1, 0.1, kind="ttft")
    feed(lt, "openai/mini", 0.9, 0.9, kind="ttft")
    feed(lt, "anthropic/haiku", 9, 9)
    feed(lt, "openai/mini", 1, 1)
    feed(lt, "ollama/llama", 1, 1)
    feed(lt, "ollama/llama", 0.5, 0.5, kind="ttft")
    assert lt.order(ROUTE, deployment_id, "ttft") == [A, C, B]
    assert lt.order(ROUTE, deployment_id, "latency")[0] in (B, C)


# ---------- chain ----------


def run(coro):
    return asyncio.run(coro)


def test_chain_falls_through_failures_and_records_attempts():
    reg = BreakerRegistry(BreakerConfig(failure_threshold=5))

    async def call(t):
        if t is A:
            raise Fail(500)
        return f"answer from {t.provider}"

    res = run(run_chain(ROUTE, call, breakers=reg))
    assert res.value == "answer from openai" and res.target is B and res.fell_back
    assert [(a.deployment, a.outcome) for a in res.attempts] == [("anthropic/haiku", "error"), ("openai/mini", "ok")]
    assert res.attempts[0].status == 500


def test_chain_skips_open_breakers():
    reg = BreakerRegistry(BreakerConfig(failure_threshold=1))
    reg.get("anthropic/haiku").record_failure()
    calls = []

    async def call(t):
        calls.append(t)
        return "ok"

    res = run(run_chain(ROUTE, call, breakers=reg))
    assert calls == [B]
    assert res.skipped[0].deployment == "anthropic/haiku" and res.skipped[0].breaker_state == OPEN
    assert res.tried == 1


def test_caller_errors_do_not_trip_breakers():
    reg = BreakerRegistry(BreakerConfig(failure_threshold=1))

    async def call(t):
        raise Fail(400)

    with pytest.raises(ChainExhausted) as exc:
        run(run_chain(ROUTE, call, breakers=reg))
    assert not exc.value.all_skipped
    assert all(b["state"] == "closed" for b in reg.snapshot())


def test_all_circuits_open_reports_soonest_retry():
    clock = Clock()
    reg = BreakerRegistry(BreakerConfig(failure_threshold=1, cooldown_seconds=30), clock=clock)
    for dep, wait in (("anthropic/haiku", 30), ("openai/mini", 5), ("ollama/llama", 12)):
        reg.get(dep).record_failure(retry_after=wait)

    async def call(t):
        raise AssertionError("no provider should be called")

    with pytest.raises(ChainExhausted) as exc:
        run(run_chain(ROUTE, call, breakers=reg))
    assert exc.value.all_skipped and exc.value.retry_after == pytest.approx(5)


def test_retry_after_from_provider_opens_breaker():
    reg = BreakerRegistry(BreakerConfig(failure_threshold=10))

    async def call(t):
        if t is A:
            raise Fail(429, retry_after=20)
        return "ok"

    run(run_chain(ROUTE, call, breakers=reg))
    assert reg.get("anthropic/haiku").current_state() == OPEN
    assert reg.get("anthropic/haiku").retry_in() == pytest.approx(20, abs=1)


def test_cancellation_releases_half_open_probe():
    clock = Clock()
    reg = BreakerRegistry(BreakerConfig(failure_threshold=1, cooldown_seconds=1), clock=clock)
    reg.get("anthropic/haiku").record_failure()
    clock.t = 2  # half-open

    async def call(t):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(run_chain([A], call, breakers=reg))
    assert reg.get("anthropic/haiku").allow_request()  # slot was given back


def test_latency_strategy_learns_from_the_chain():
    clock = Clock()
    lt = tracker()
    speed = {"anthropic": 2.0, "openai": 0.3, "ollama": 0.9}

    async def call(t):
        clock.t += speed[t.provider]
        return t.provider

    # Warm-up: each deployment gets measured, then the fastest leads.
    served = []
    for _ in range(4):
        for first in ROUTE:  # force each target to be tried by putting it alone in the route
            run(run_chain([first], call, strategy="latency", latency=lt, clock=clock))
    for _ in range(3):
        served.append(run(run_chain(ROUTE, call, strategy="latency", latency=lt, clock=clock)).value)
    assert served == ["openai"] * 3
    assert run(run_chain(ROUTE, call, strategy="ordered", latency=lt, clock=clock)).value == "anthropic"
