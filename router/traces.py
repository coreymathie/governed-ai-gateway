# Corey Mathie, 2026
"""
Per-request decision traces for the console's Traces screen (GET /admin/traces).

Every chat request that passes authentication gets a trace: one entry per stage of the request
path (auth, policy, budgets, anomaly, pre_request hooks, exact cache, semantic cache, TPM, the
fallback chain with each attempt, settle, post_response hooks, content log), with the decision,
a one-line summary, small structured details and the wall time the stage took.

Traces hold metadata only: team, key label and fingerprint, alias, deployments, statuses, token
counts and cost. Never prompt or completion text, never a key. They live in a bounded in-memory
ring buffer (ROUTER_TRACE_BUFFER, default 500) in this worker; the usage table and the audit
trail remain the durable record.

routing.py calls begin(stage) before a step and end(decision, ...) after it. If the step raises
(a 402, 403, 429, ...), finish() records the pending stage as the one that refused the request.
Outside a request (tests calling routing functions directly) every call here is a no-op.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

STAGES = (
    "auth",
    "policy",
    "budgets",
    "anomaly",
    "pre_hooks",
    "cache",
    "semantic_cache",
    "tpm",
    "chain",
    "settle",
    "post_hooks",
    "content_log",
)
LABELS = {
    "auth": "Auth + RPM",
    "policy": "Policy (YAML / OPA)",
    "budgets": "Budgets",
    "anomaly": "Anomaly pause",
    "pre_hooks": "Content hooks (request)",
    "cache": "Exact cache",
    "semantic_cache": "Semantic cache",
    "tpm": "TPM reservation",
    "chain": "Fallback chain",
    "settle": "Settle tokens + cost",
    "post_hooks": "Content hooks (response)",
    "content_log": "Content log",
}

_seq = itertools.count(1)
_buffer: deque[Trace] = deque(maxlen=500)
_current: ContextVar[Trace | None] = ContextVar("router_trace", default=None)


@dataclass
class Trace:
    id: str
    ts: str
    key: str
    key_fp: str
    team: str
    alias: str
    stream: bool = False
    stages: list[dict] = field(default_factory=list)
    status: int | None = None
    outcome: str = "pending"  # ok | cache_hit | rejected | error | pending
    reason: str = ""
    served_by: str | None = None
    fell_back: bool = False
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    cache: str = "miss"
    latency_ms: float = 0.0
    pending: str | None = None
    _t0: float = field(default_factory=time.perf_counter)
    _mark: float = field(default_factory=time.perf_counter)

    def as_dict(self, full: bool = True) -> dict:
        out = {
            "id": self.id,
            "ts": self.ts,
            "key": self.key,
            "key_fp": self.key_fp,
            "team": self.team,
            "alias": self.alias,
            "stream": self.stream,
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "served_by": self.served_by,
            "fell_back": self.fell_back,
            "attempts": self.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "saved_usd": round(self.saved_usd, 8),
            "cache": self.cache,
            "latency_ms": round(self.latency_ms, 1),
            "simulated": False,
        }
        if full:
            out["stages"] = list(self.stages)
        return out


def configure(maxlen: int) -> None:
    global _buffer
    if maxlen != _buffer.maxlen:
        _buffer = deque(_buffer, maxlen=max(1, int(maxlen)))


def start(key_label: str, key_fp: str, team: str, alias: str, stream: bool = False) -> Token:
    tr = Trace(
        id=f"tr_{int(time.time()):x}{next(_seq):05d}",
        ts=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        key=key_label,
        key_fp=key_fp,
        team=team,
        alias=alias,
        stream=stream,
    )
    _buffer.append(tr)
    token = _current.set(tr)
    stage("auth", "pass", f"key {key_label} ({key_fp}) · team {team}", ms=None)
    return token


def current() -> Trace | None:
    return _current.get()


def detach(token: Token) -> None:
    _current.reset(token)


def begin(name: str) -> None:
    tr = _current.get()
    if tr is not None:
        tr.pending = name
        tr._mark = time.perf_counter()


def stage(name: str, decision: str, summary: str = "", ms: float | None = -1.0, **detail: Any) -> None:
    """Record a finished stage. decision: pass | deny | error | hit | miss | skip."""
    tr = _current.get()
    if tr is None:
        return
    record(tr, name, decision, summary, ms, **detail)


def record(tr: Trace, name: str, decision: str, summary: str = "", ms: float | None = -1.0, **detail: Any) -> None:
    now = time.perf_counter()
    took = (now - tr._mark) * 1000 if ms == -1.0 else ms
    tr.stages.append(
        {
            "stage": name,
            "label": LABELS.get(name, name),
            "decision": decision,
            "summary": summary,
            "ms": round(took, 2) if took is not None else None,
            "detail": {k: v for k, v in detail.items() if v is not None},
        }
    )
    tr._mark = now
    if tr.pending == name:
        tr.pending = None


def end(decision: str, summary: str = "", **detail: Any) -> None:
    tr = _current.get()
    if tr is None or tr.pending is None:
        return
    record(tr, tr.pending, decision, summary, **detail)


def update(**fields: Any) -> None:
    tr = _current.get()
    if tr is None:
        return
    for k, v in fields.items():
        setattr(tr, k, v)


def finish(status: int, reason: str = "", outcome: str | None = None) -> None:
    tr = _current.get()
    if tr is None:
        return
    if tr.pending is not None:
        record(tr, tr.pending, "error" if status >= 500 else "deny", reason)
    tr.status = status
    tr.reason = reason
    if outcome:
        tr.outcome = outcome
    elif status < 400:
        tr.outcome = "cache_hit" if tr.cache in ("hit", "semantic-hit") else "ok"
    else:
        tr.outcome = "error" if status >= 500 else "rejected"
    tr.latency_ms = (time.perf_counter() - tr._t0) * 1000


def rows(limit: int = 100, team: str | None = None, outcome: str | None = None, q: str | None = None) -> list[dict]:
    out = []
    needle = (q or "").lower().strip()
    for tr in reversed(_buffer):
        if team and tr.team != team:
            continue
        if outcome and tr.outcome != outcome:
            continue
        if needle:
            hay = " ".join(
                str(x) for x in (tr.id, tr.key, tr.key_fp, tr.team, tr.alias, tr.served_by, tr.reason, tr.status)
            ).lower()
            if needle not in hay:
                continue
        out.append(tr.as_dict(full=False))
        if len(out) >= limit:
            break
    return out


def get(trace_id: str) -> dict | None:
    for tr in _buffer:
        if tr.id == trace_id:
            return tr.as_dict()
    return None


def stats() -> dict:
    """Counts over the traces in the buffer (this worker, since start or the buffer's oldest entry)."""
    done = [t for t in _buffer if t.outcome != "pending"]
    served = [t for t in done if t.outcome in ("ok", "cache_hit")]
    rejected: dict[str, int] = {}
    for t in done:
        if t.outcome in ("rejected", "error"):
            rejected[str(t.status)] = rejected.get(str(t.status), 0) + 1
    lat = sorted(t.latency_ms for t in served)
    return {
        "requests": len(done),
        "served": len(served),
        "fallbacks": sum(1 for t in served if t.fell_back),
        "fallback_rate": round(sum(1 for t in served if t.fell_back) / len(served), 4) if served else 0.0,
        "by_status": rejected,
        "latency_p50_ms": round(lat[len(lat) // 2], 1) if lat else None,
        "window": f"this worker, last {_buffer.maxlen or 0} requests",
    }


def reset() -> None:
    _buffer.clear()
