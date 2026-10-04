# Corey Mathie, 2026
"""
Minimal Prometheus metrics, no extra dependency.

Counters live in process memory and reset on restart, which is what
Prometheus expects from a counter. Scrape /metrics with the admin key as a
bearer token.
"""

from __future__ import annotations

from collections import Counter

_requests: Counter[tuple[str, str, str]] = Counter()  # (alias, provider, outcome)
_spend: Counter[tuple[str, str]] = Counter()  # (alias, provider) -> USD
_saved: Counter[str] = Counter()  # alias -> USD saved by cache


def observe(alias: str, provider: str, outcome: str, cost_usd: float = 0.0, saved_usd: float = 0.0) -> None:
    """outcome: ok | error | cache_hit"""
    _requests[(alias, provider, outcome)] += 1
    if cost_usd:
        _spend[(alias, provider)] += cost_usd
    if saved_usd:
        _saved[alias] += saved_usd


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    lines = [
        "# HELP router_requests_total Provider attempts and cache hits by alias, provider, and outcome.",
        "# TYPE router_requests_total counter",
    ]
    for (alias, provider, outcome), n in sorted(_requests.items()):
        lines.append(
            f'router_requests_total{{alias="{_esc(alias)}",provider="{_esc(provider)}",outcome="{_esc(outcome)}"}} {n}'
        )
    lines += [
        "# HELP router_spend_usd_total Spend recorded by alias and provider.",
        "# TYPE router_spend_usd_total counter",
    ]
    for (alias, provider), usd in sorted(_spend.items()):
        lines.append(f'router_spend_usd_total{{alias="{_esc(alias)}",provider="{_esc(provider)}"}} {usd:.6f}')
    lines += [
        "# HELP router_cache_saved_usd_total Spend avoided by cache hits.",
        "# TYPE router_cache_saved_usd_total counter",
    ]
    for alias, usd in sorted(_saved.items()):
        lines.append(f'router_cache_saved_usd_total{{alias="{_esc(alias)}"}} {usd:.6f}')
    return "\n".join(lines) + "\n"


def reset() -> None:
    _requests.clear()
    _spend.clear()
    _saved.clear()
