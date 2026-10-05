# Corey Mathie, 2026
"""
The provider fallback chain, independent of any HTTP or SDK code.

`run_chain` walks a route's targets (in configured order, or latency order for
`strategy: latency`), skips deployments whose circuit breaker is open, calls
the rest one at a time, and returns the first success. Every attempt, skip and
failure is recorded so the gateway can expose it in headers, logs and metrics.

The gateway (`routing.py`) passes a `call` that wraps LiteLLM; the browser demo
passes a simulated provider. Both run this same function.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .breaker import BreakerRegistry, counts_as_failure, retry_after_seconds, status_code_of
from .latency import LatencyTracker

STRATEGIES = ("ordered", "latency")


class Target(Protocol):
    provider: str
    model: str


def deployment_id(target: Target) -> str:
    return f"{target.provider}/{target.model}"


@dataclass
class Attempt:
    deployment: str
    outcome: str  # "ok" | "error" | "skipped"
    seconds: float = 0.0
    error: str = ""
    status: int | None = None
    breaker_state: str = "closed"
    counted: bool = True  # did this error count against the breaker?

    def as_dict(self) -> dict:
        return {
            "deployment": self.deployment,
            "outcome": self.outcome,
            "seconds": round(self.seconds, 4),
            "error": self.error,
            "status": self.status,
            "breaker_state": self.breaker_state,
        }


@dataclass
class ChainResult:
    value: Any
    target: Any
    strategy: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def skipped(self) -> list[Attempt]:
        return [a for a in self.attempts if a.outcome == "skipped"]

    @property
    def tried(self) -> int:
        return sum(1 for a in self.attempts if a.outcome != "skipped")

    @property
    def fell_back(self) -> bool:
        return len(self.attempts) > 1


class ChainExhausted(Exception):
    """No target produced a response."""

    def __init__(self, attempts: list[Attempt], retry_after: float | None):
        self.attempts = attempts
        self.retry_after = retry_after
        self.all_skipped = bool(attempts) and all(a.outcome == "skipped" for a in attempts)
        super().__init__("all circuits open" if self.all_skipped else "all providers failed")


def plan(targets: Sequence[Any], strategy: str, latency: LatencyTracker | None, kind: str = "latency") -> list[Any]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    if strategy == "latency" and latency is not None:
        return latency.order(targets, deployment_id, kind)
    return list(targets)


async def run_chain(
    targets: Sequence[Any],
    call: Callable[[Any], Awaitable[Any]],
    *,
    strategy: str = "ordered",
    breakers: BreakerRegistry | None = None,
    latency: LatencyTracker | None = None,
    kind: str = "latency",
    clock: Callable[[], float] = time.perf_counter,
    on_attempt: Callable[[Any, Attempt], None] | None = None,
) -> ChainResult:
    attempts: list[Attempt] = []
    retry_hints: list[float] = []

    for target in plan(targets, strategy, latency, kind):
        dep = deployment_id(target)
        breaker = breakers.get(dep) if breakers is not None else None

        if breaker is not None and not breaker.allow_request():
            wait = breaker.retry_in()
            if wait > 0:
                retry_hints.append(wait)
            a = Attempt(dep, "skipped", breaker_state=breaker.current_state(), error="circuit open")
            attempts.append(a)
            if on_attempt:
                on_attempt(target, a)
            continue

        started = clock()
        try:
            value = await call(target)
        except Exception as e:  # noqa: BLE001 - any provider failure moves on to the next target
            elapsed = clock() - started
            counted = counts_as_failure(e)
            if breaker is not None:
                if counted:
                    breaker.record_failure(retry_after_seconds(e), reason=type(e).__name__)
                else:
                    breaker.release()
            a = Attempt(
                dep,
                "error",
                elapsed,
                error=f"{type(e).__name__}: {e}"[:200],
                status=status_code_of(e),
                breaker_state=breaker.current_state() if breaker is not None else "closed",
                counted=counted,
            )
            attempts.append(a)
            if on_attempt:
                on_attempt(target, a)
            continue
        except BaseException:
            # Cancellation (client went away): don't judge the provider, but never leak a probe slot.
            if breaker is not None:
                breaker.release()
            raise

        elapsed = clock() - started
        if breaker is not None:
            breaker.record_success()
        if latency is not None:
            latency.observe(dep, elapsed, kind)
        a = Attempt(dep, "ok", elapsed, breaker_state=breaker.current_state() if breaker is not None else "closed")
        attempts.append(a)
        if on_attempt:
            on_attempt(target, a)
        return ChainResult(value, target, strategy, attempts)

    raise ChainExhausted(attempts, min(retry_hints) if retry_hints else None)
