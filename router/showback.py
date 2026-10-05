# Corey Mathie, 2026
"""
Showback / chargeback: roll recorded usage up by team, key, model and provider.

Input rows are usage records (one per provider attempt or cache hit). Output
rows carry the unit metrics FinOps teams allocate on:

  requests             successful responses served (provider calls + cache hits)
  cache_hits           responses served from the cache at $0
  failed_attempts      provider attempts that errored (fallbacks, outages)
  input/output_tokens  tokens billed by providers (cache hits excluded)
  cost_usd             recorded spend (includes partial streams that failed mid-way)
  saved_usd            spend avoided by cache hits
  cost_per_1k_requests cost_usd / requests * 1000
  cost_per_1k_tokens   cost_usd / (input + output tokens) * 1000
  share_of_cost        this row's fraction of the period's total cost

Keys appear by label plus a short SHA-256 fingerprint, never the secret.
CSV cells that start with = + - @ are prefixed with ' so spreadsheets don't
execute them (CSV formula injection). Pure Python; the demo runs it unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Mapping, Sequence

DIMENSIONS = {
    "team": ("team",),
    "key": ("key_label", "key_fp"),
    "alias": ("alias",),
    "provider": ("provider",),
    "model": ("model",),
}
DEFAULT_GROUP_BY = ("team", "key", "provider", "model")
METRICS = (
    "requests",
    "cache_hits",
    "failed_attempts",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "saved_usd",
    "cost_per_1k_requests",
    "cost_per_1k_tokens",
    "share_of_cost",
)


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def parse_group_by(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    if raw is None or raw == "":
        return DEFAULT_GROUP_BY
    parts = [p.strip() for p in raw.split(",")] if isinstance(raw, str) else [str(p).strip() for p in raw]
    unknown = [p for p in parts if p not in DIMENSIONS]
    if unknown or not parts:
        raise ValueError(f"unknown group_by {unknown}; choose from {sorted(DIMENSIONS)}")
    return tuple(dict.fromkeys(parts))


def _columns(group_by: Sequence[str]) -> list[str]:
    return [c for dim in group_by for c in DIMENSIONS[dim]]


def _unit(cost: float, n: int) -> float:
    return round(cost / n * 1000, 6) if n else 0.0


def aggregate(rows: Iterable[Mapping], group_by: Sequence[str] = DEFAULT_GROUP_BY) -> list[dict]:
    cols = _columns(group_by)
    groups: dict[tuple, dict] = {}
    for r in rows:
        k = tuple(r.get(c) if r.get(c) is not None else "" for c in cols)
        g = groups.get(k)
        if g is None:
            g = dict(zip(cols, k, strict=True))
            g.update(dict.fromkeys(("requests", "cache_hits", "failed_attempts", "input_tokens", "output_tokens"), 0))
            g.update(cost_usd=0.0, saved_usd=0.0)
            groups[k] = g
        cached = bool(r.get("cached"))
        if r.get("error"):
            g["failed_attempts"] += 1
        else:
            g["requests"] += 1
            g["cache_hits"] += int(cached)
        if not cached:
            g["input_tokens"] += int(r.get("prompt_tokens") or 0)
            g["output_tokens"] += int(r.get("completion_tokens") or 0)
        g["cost_usd"] += float(r.get("cost_usd") or 0.0)
        g["saved_usd"] += float(r.get("saved_usd") or 0.0)

    total_cost = sum(g["cost_usd"] for g in groups.values())
    out = []
    for g in groups.values():
        g["cost_per_1k_requests"] = _unit(g["cost_usd"], g["requests"])
        g["cost_per_1k_tokens"] = _unit(g["cost_usd"], g["input_tokens"] + g["output_tokens"])
        g["share_of_cost"] = round(g["cost_usd"] / total_cost, 4) if total_cost else 0.0
        g["cost_usd"] = round(g["cost_usd"], 6)
        g["saved_usd"] = round(g["saved_usd"], 6)
        out.append(g)
    return sorted(out, key=lambda g: (-g["cost_usd"], *[str(g[c]) for c in cols]))


def totals(rows: Iterable[Mapping]) -> dict:
    agg = aggregate(rows, group_by=())
    if not agg:
        return {m: 0 for m in METRICS if m != "share_of_cost"}
    t = agg[0]
    t.pop("share_of_cost", None)
    return t


def _safe(value) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rows: Sequence[Mapping], group_by: Sequence[str] = DEFAULT_GROUP_BY) -> str:
    cols = _columns(group_by) + list(METRICS)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_safe(r.get(c)) for c in cols])
    return buf.getvalue()
