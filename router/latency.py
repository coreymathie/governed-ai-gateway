# Corey Mathie, 2026
"""
Latency tracking and the optional `strategy: latency` route ordering.

Each deployment keeps an exponentially weighted moving average (EWMA) of
successful-call latency: total latency for normal calls, time-to-first-token
(TTFT) for streams. Failed attempts are not averaged in; breakers handle those.

Ordering for a latency route:
  1. Deployments with fewer than `min_samples` observations go first, in their
     configured order, so each one gets measured (bounded warm-up).
  2. Measured deployments whose EWMA is within `tolerance` of the fastest form
     a "fast tier" kept in configured order. Near-ties don't reshuffle the
     route, so a cheaper or preferred primary keeps its place.
  3. Everyone else follows, fastest first, configured order breaking ties.

Routes without `strategy: latency` keep the configured order. Pure Python, no
dependencies, so the browser demo runs it unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
KINDS = ("latency", "ttft")


@dataclass
class LatencyConfig:
    alpha: float = 0.3  # weight of the newest sample
    min_samples: int = 3
    tolerance: float = 0.10  # within 10% of the fastest counts as a tie


@dataclass
class _Ewma:
    value: float = 0.0
    samples: int = 0

    def add(self, x: float, alpha: float) -> None:
        self.value = x if self.samples == 0 else alpha * x + (1 - alpha) * self.value
        self.samples += 1


class LatencyTracker:
    def __init__(self, config: LatencyConfig | None = None):
        self.config = config or LatencyConfig()
        self._stats: dict[tuple[str, str], _Ewma] = {}

    def configure(self, config: LatencyConfig) -> None:
        self.config = config

    def observe(self, deployment: str, seconds: float, kind: str = "latency") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown latency kind {kind!r}")
        self._stats.setdefault((deployment, kind), _Ewma()).add(max(0.0, seconds), self.config.alpha)

    def ewma(self, deployment: str, kind: str = "latency") -> float | None:
        """EWMA in seconds, or None until the deployment has min_samples observations."""
        s = self._stats.get((deployment, kind))
        if not s or s.samples < self.config.min_samples:
            return None
        return s.value

    def samples(self, deployment: str, kind: str = "latency") -> int:
        s = self._stats.get((deployment, kind))
        return s.samples if s else 0

    def order(self, targets: Sequence[T], deployment_of: Callable[[T], str], kind: str = "latency") -> list[T]:
        unmeasured: list[tuple[int, T]] = []
        measured: list[tuple[float, int, T]] = []
        for i, t in enumerate(targets):
            value = self.ewma(deployment_of(t), kind)
            if value is None:
                unmeasured.append((i, t))
            else:
                measured.append((value, i, t))
        if not measured:
            return [t for _, t in unmeasured]
        best = min(m[0] for m in measured)
        limit = best * (1 + self.config.tolerance)
        fast = sorted((m for m in measured if m[0] <= limit), key=lambda m: m[1])
        rest = sorted((m for m in measured if m[0] > limit), key=lambda m: (m[0], m[1]))
        return [t for _, t in unmeasured] + [m[2] for m in fast] + [m[2] for m in rest]

    def snapshot(self) -> list[dict]:
        out = []
        for (dep, kind), s in sorted(self._stats.items()):
            out.append({"deployment": dep, "kind": kind, "ewma_s": round(s.value, 4), "samples": s.samples})
        return out

    def reset(self) -> None:
        self._stats.clear()
