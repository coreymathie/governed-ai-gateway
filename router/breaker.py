# Corey Mathie, 2026
"""
Circuit breakers, one per provider deployment ("provider/model").

    closed ──(N consecutive failures, or error rate >= X over the window,
    │          or a 429/503 carrying Retry-After)──▶ open
    ▲                                                 │
    │                                     cooldown (or Retry-After) elapses
    │                                                 ▼
    └──(success_threshold probe successes)──── half_open ──(probe fails)──▶ open

While a breaker is open the gateway skips that deployment and falls through to
the next target in the route, so a dead provider costs nothing instead of a
timeout on every request. Half-open lets a limited number of probe requests
through; the first failure re-opens it.

What counts as a failure: connection errors, timeouts, 408, 429, 401/403 (a
broken credential is a deployment problem) and 5xx. Other 4xx responses are
the caller's fault (bad request, context too long) and don't move the breaker.

State lives in process memory: each gateway worker keeps its own breakers.
This module has no third-party dependencies, so the browser demo runs it as is.
The clock is injectable so tests can drive every transition deterministically.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"
STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}  # Prometheus gauge encoding

_HEALTH_STATUSES = {401, 403, 408, 429}
_RETRY_AFTER_STATUSES = {429, 503}


@dataclass
class BreakerConfig:
    enabled: bool = True
    failure_threshold: int = 5  # consecutive failures that open the breaker
    error_rate_threshold: float = 0.5  # ...or this error rate over the window
    min_requests: int = 10  # ...once the window holds at least this many calls
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0  # open -> half_open after this long
    half_open_max_probes: int = 1  # concurrent probe requests allowed while half-open
    success_threshold: int = 1  # probe successes needed to close again
    max_retry_after_seconds: float = 300.0  # cap on a provider's Retry-After


@dataclass(frozen=True)
class Transition:
    deployment: str
    from_state: str
    to_state: str
    reason: str
    at: float

    def as_dict(self) -> dict:
        return {
            "deployment": self.deployment,
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "at": self.at,
        }


@dataclass
class CircuitBreaker:
    deployment: str
    config: BreakerConfig
    clock: Callable[[], float]
    on_transition: Callable[[Transition], None] | None = None
    state: str = CLOSED
    opened_at: float | None = None
    open_until: float = 0.0
    consecutive_failures: int = 0
    probes_in_flight: int = 0
    probe_successes: int = 0
    last_reason: str = ""
    _window: deque = field(default_factory=deque)  # (timestamp, ok)

    # ---- state machine ----

    def _now(self) -> float:
        return self.clock()

    def _move(self, to_state: str, reason: str) -> None:
        if to_state == self.state:
            return
        t = Transition(self.deployment, self.state, to_state, reason, self._now())
        self.state = to_state
        self.last_reason = reason
        if self.on_transition:
            self.on_transition(t)

    def _open(self, seconds: float, reason: str) -> None:
        now = self._now()
        self.opened_at = now
        self.open_until = now + max(0.0, seconds)
        self.probes_in_flight = 0
        self.probe_successes = 0
        self._move(OPEN, reason)

    def _tick(self) -> None:
        """Time-based transition: open -> half_open once the cooldown has elapsed."""
        if self.state == OPEN and self._now() >= self.open_until:
            self.probes_in_flight = 0
            self.probe_successes = 0
            self._move(HALF_OPEN, "cooldown elapsed")

    def _prune(self) -> None:
        cutoff = self._now() - self.config.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def current_state(self) -> str:
        self._tick()
        return self.state

    # ---- caller API ----

    def allow_request(self) -> bool:
        """True if a request may be sent now. In half-open, this claims a probe slot."""
        if not self.config.enabled:
            return True
        self._tick()
        if self.state == CLOSED:
            return True
        if self.state == HALF_OPEN and self.probes_in_flight < self.config.half_open_max_probes:
            self.probes_in_flight += 1
            return True
        return False

    def record_success(self) -> None:
        if not self.config.enabled:
            return
        self._tick()
        self._window.append((self._now(), True))
        self._prune()
        self.consecutive_failures = 0
        if self.state == HALF_OPEN:
            self.probes_in_flight = max(0, self.probes_in_flight - 1)
            self.probe_successes += 1
            if self.probe_successes >= self.config.success_threshold:
                self._window.clear()
                self._move(CLOSED, "probe succeeded")

    def record_failure(self, retry_after: float | None = None, reason: str = "error") -> None:
        if not self.config.enabled:
            return
        self._tick()
        self._window.append((self._now(), False))
        self._prune()
        self.consecutive_failures += 1
        capped = min(retry_after, self.config.max_retry_after_seconds) if retry_after else None

        if self.state == HALF_OPEN:
            self._open(capped or self.config.cooldown_seconds, f"probe failed: {reason}")
        elif self.state == CLOSED:
            if capped:
                self._open(capped, f"provider asked to back off (Retry-After {capped:g}s)")
            elif self.consecutive_failures >= self.config.failure_threshold:
                self._open(self.config.cooldown_seconds, f"{self.consecutive_failures} consecutive failures")
            else:
                total = len(self._window)
                errors = sum(1 for _, ok in self._window if not ok)
                if total >= self.config.min_requests and errors / total >= self.config.error_rate_threshold:
                    self._open(
                        self.config.cooldown_seconds,
                        f"error rate {errors}/{total} in {self.config.window_seconds:g}s",
                    )
        elif self.state == OPEN and capped:
            # A late failure from a request that started before the trip: honour a longer Retry-After.
            self.open_until = max(self.open_until, self._now() + capped)

    def release(self) -> None:
        """Neutral outcome (caller error, cancellation): free a half-open probe slot without judging."""
        if self.state == HALF_OPEN:
            self.probes_in_flight = max(0, self.probes_in_flight - 1)

    def retry_in(self) -> float:
        """Seconds until an open breaker will let a probe through (0 if not open)."""
        self._tick()
        return max(0.0, self.open_until - self._now()) if self.state == OPEN else 0.0

    def snapshot(self) -> dict:
        self._tick()
        self._prune()
        total = len(self._window)
        errors = sum(1 for _, ok in self._window if not ok)
        return {
            "deployment": self.deployment,
            "state": self.state,
            "state_value": STATE_VALUE[self.state],
            "consecutive_failures": self.consecutive_failures,
            "window_requests": total,
            "window_errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "retry_in_s": round(self.retry_in(), 3),
            "probes_in_flight": self.probes_in_flight,
            "reason": self.last_reason,
        }


class BreakerRegistry:
    """One breaker per deployment, created on first use. Keeps a bounded transition log."""

    def __init__(self, config: BreakerConfig | None = None, clock: Callable[[], float] = time.monotonic):
        self.config = config or BreakerConfig()
        self.clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self.transitions: deque[Transition] = deque(maxlen=200)
        self.listeners: list[Callable[[Transition], None]] = []

    def _on_transition(self, t: Transition) -> None:
        self.transitions.append(t)
        for fn in self.listeners:
            fn(t)

    def configure(self, config: BreakerConfig) -> None:
        """Apply new thresholds (e.g. after a routes reload) while keeping current state."""
        self.config = config
        for b in self._breakers.values():
            b.config = config

    def get(self, deployment: str) -> CircuitBreaker:
        b = self._breakers.get(deployment)
        if b is None:
            b = CircuitBreaker(deployment, self.config, lambda: self.clock(), self._on_transition)
            self._breakers[deployment] = b
        return b

    def snapshot(self) -> list[dict]:
        return [self._breakers[d].snapshot() for d in sorted(self._breakers)]

    def recent_transitions(self, limit: int = 50) -> list[dict]:
        return [t.as_dict() for t in list(self.transitions)[-limit:]][::-1]

    def reset(self) -> None:
        self._breakers.clear()
        self.transitions.clear()


# ---------- Classifying provider errors ----------


def status_code_of(err: BaseException) -> int | None:
    code = getattr(err, "status_code", None)
    if code is None:
        code = getattr(getattr(err, "response", None), "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def counts_as_failure(err: BaseException) -> bool:
    """Does this error say something about the provider's health (vs. the caller's request)?"""
    code = status_code_of(err)
    if code is None:  # connection error, timeout, unexpected exception
        return True
    return code >= 500 or code in _HEALTH_STATUSES


def _header(err: BaseException, name: str) -> str | None:
    for source in (getattr(getattr(err, "response", None), "headers", None), getattr(err, "headers", None)):
        if source is None:
            continue
        try:
            value = source.get(name) or source.get(name.title())
        except AttributeError:
            continue
        if value:
            return str(value)
    return None


def retry_after_seconds(err: BaseException, now: datetime | None = None) -> float | None:
    """Retry-After from a 429/503 (delta-seconds or HTTP-date), else None."""
    if status_code_of(err) not in _RETRY_AFTER_STATUSES:
        return None
    raw = getattr(err, "retry_after", None)
    if raw is None:
        raw = _header(err, "retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
