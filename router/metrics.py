# Corey Mathie, 2026
"""
Minimal Prometheus metrics, no extra dependency.

Counters live in process memory and reset on restart, which is what
Prometheus expects from a counter. Scrape /metrics with the admin key as a
bearer token. Gauges (breaker state, latency EWMA, TPM in use) are passed in
at render time from the live registries.
"""

from __future__ import annotations

from collections import Counter

_requests: Counter[tuple[str, str, str]] = Counter()  # (alias, provider, outcome)
_spend: Counter[tuple[str, str]] = Counter()  # (alias, provider) -> USD
_saved: Counter[str] = Counter()  # alias -> USD saved by cache
_tokens: Counter[tuple[str, str, str]] = Counter()  # (alias, provider, direction) -> tokens
_rejections: Counter[str] = Counter()  # reason -> requests refused before any provider call
_transitions: Counter[tuple[str, str, str]] = Counter()  # (deployment, from, to)
_policy_actions: Counter[tuple[str, str]] = Counter()  # (category, action) written to the audit log
_shadow: Counter[tuple[str, str, str]] = Counter()  # (alias, candidate, outcome)
_semantic: Counter[tuple[str, str]] = Counter()  # (alias, result)
_tool_calls: Counter[tuple[str, str, str]] = Counter()  # (server, tool, decision)


def observe(
    alias: str,
    provider: str,
    outcome: str,
    cost_usd: float = 0.0,
    saved_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """outcome: ok | error | cache_hit | skipped"""
    _requests[(alias, provider, outcome)] += 1
    if cost_usd:
        _spend[(alias, provider)] += cost_usd
    if saved_usd:
        _saved[alias] += saved_usd
    if input_tokens:
        _tokens[(alias, provider, "input")] += int(input_tokens)
    if output_tokens:
        _tokens[(alias, provider, "output")] += int(output_tokens)


def rejection(reason: str) -> None:
    _rejections[reason] += 1


def circuit_transition(deployment: str, from_state: str, to_state: str) -> None:
    _transitions[(deployment, from_state, to_state)] += 1


def policy_action(category: str, action: str) -> None:
    _policy_actions[(category, action)] += 1


def tool_call(server: str, tool: str, decision: str) -> None:
    """decision: allow | deny | rate_limited | cap_reached | upstream_error"""
    _tool_calls[(server, tool, decision)] += 1


def semantic(alias: str, result: str) -> None:
    """result: hit | miss | guard | embed_error"""
    _semantic[(alias, result)] += 1


def shadow(alias: str, candidate: str, outcome: str) -> None:
    """outcome: ok | error | skipped_policy | skipped_cap | skipped_hook"""
    _shadow[(alias, candidate, outcome)] += 1


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in kv.items()) + "}"


def render(breakers: list[dict] | None = None, latency: list[dict] | None = None, tpm: dict | None = None) -> str:
    lines = [
        "# HELP router_requests_total Provider attempts, skips and cache hits by alias, provider, and outcome.",
        "# TYPE router_requests_total counter",
    ]
    for (alias, provider, outcome), n in sorted(_requests.items()):
        lines.append(f"router_requests_total{_labels(alias=alias, provider=provider, outcome=outcome)} {n}")
    lines += [
        "# HELP router_spend_usd_total Spend recorded by alias and provider.",
        "# TYPE router_spend_usd_total counter",
    ]
    for (alias, provider), usd in sorted(_spend.items()):
        lines.append(f"router_spend_usd_total{_labels(alias=alias, provider=provider)} {usd:.6f}")
    lines += [
        "# HELP router_cache_saved_usd_total Spend avoided by cache hits.",
        "# TYPE router_cache_saved_usd_total counter",
    ]
    for alias, usd in sorted(_saved.items()):
        lines.append(f"router_cache_saved_usd_total{_labels(alias=alias)} {usd:.6f}")
    lines += [
        "# HELP router_tokens_total Provider-reported tokens by alias, provider, and direction.",
        "# TYPE router_tokens_total counter",
    ]
    for (alias, provider, direction), n in sorted(_tokens.items()):
        lines.append(f"router_tokens_total{_labels(alias=alias, provider=provider, direction=direction)} {n}")
    lines += [
        "# HELP router_rejections_total Requests refused before any provider call, by reason.",
        "# TYPE router_rejections_total counter",
    ]
    for reason, n in sorted(_rejections.items()):
        lines.append(f"router_rejections_total{_labels(reason=reason)} {n}")
    lines += [
        "# HELP router_circuit_transitions_total Circuit breaker state changes.",
        "# TYPE router_circuit_transitions_total counter",
    ]
    for (dep, frm, to), n in sorted(_transitions.items()):
        lines.append(f"router_circuit_transitions_total{_labels(deployment=dep, **{'from': frm}, to=to)} {n}")
    lines += [
        "# HELP router_policy_actions_total Audit-logged policy actions by category and action.",
        "# TYPE router_policy_actions_total counter",
    ]
    for (category, action), n in sorted(_policy_actions.items()):
        lines.append(f"router_policy_actions_total{_labels(category=category, action=action)} {n}")
    lines += [
        "# HELP router_mcp_tool_calls_total MCP tool calls through the gateway by server, tool and decision.",
        "# TYPE router_mcp_tool_calls_total counter",
    ]
    for (server, tool, decision), n in sorted(_tool_calls.items()):
        lines.append(f"router_mcp_tool_calls_total{_labels(server=server, tool=tool, decision=decision)} {n}")
    lines += [
        "# HELP router_semantic_cache_total Semantic-cache lookups by alias and result.",
        "# TYPE router_semantic_cache_total counter",
    ]
    for (alias, result), n in sorted(_semantic.items()):
        lines.append(f"router_semantic_cache_total{_labels(alias=alias, result=result)} {n}")
    lines += [
        "# HELP router_shadow_requests_total Mirrored (shadow) requests by alias, candidate and outcome.",
        "# TYPE router_shadow_requests_total counter",
    ]
    for (alias, cand, outcome), n in sorted(_shadow.items()):
        lines.append(f"router_shadow_requests_total{_labels(alias=alias, candidate=cand, outcome=outcome)} {n}")
    lines += [
        "# HELP router_circuit_state Circuit breaker state per deployment (0 closed, 1 half-open, 2 open).",
        "# TYPE router_circuit_state gauge",
    ]
    for b in breakers or []:
        lines.append(f"router_circuit_state{_labels(deployment=b['deployment'])} {b['state_value']}")
    lines += [
        "# HELP router_provider_latency_ewma_seconds EWMA of successful-call latency (or TTFT for streams).",
        "# TYPE router_provider_latency_ewma_seconds gauge",
    ]
    for s in latency or []:
        lines.append(
            f"router_provider_latency_ewma_seconds{_labels(deployment=s['deployment'], kind=s['kind'])} {s['ewma_s']}"
        )
    lines += [
        "# HELP router_tpm_in_use Tokens counted in the current 60 s window, by scope.",
        "# TYPE router_tpm_in_use gauge",
    ]
    for scope, n in sorted((tpm or {}).items()):
        lines.append(f"router_tpm_in_use{_labels(scope=scope)} {n}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    _requests.clear()
    _spend.clear()
    _saved.clear()
    _tokens.clear()
    _rejections.clear()
    _transitions.clear()
    _policy_actions.clear()
    _shadow.clear()
    _semantic.clear()
    _tool_calls.clear()
