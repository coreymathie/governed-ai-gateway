# Corey Mathie, 2026
"""
Router evaluation harness: replay a JSONL set of prompts across candidate
routes, score the answers, and report cost against quality.

Each case (one JSON object per line):

    {"id": "math-01", "messages": [{"role": "user", "content": "What is 17 * 23?"}],
     "reference": {"type": "exact" | "contains" | "regex", "value": "391"},
     "answer": "391", "tags": ["math"], "max_tokens": 64}

`prompt` may replace `messages` for single-turn cases. `answer` is used only
by the simulated provider (what a correct model would say); real providers
ignore it. Scoring: exact (case- and whitespace-insensitive), contains
(case-insensitive substring), regex (re.search, IGNORECASE). An optional judge
callable `(case, output) -> float in [0, 1]` scores cases without a reference,
and is reported separately for cases that have one.

Routes run through the same fallback chain (router/fallback.py) and pricing
(router/costs.py) as the gateway. The simulated provider is deterministic: a
deployment answers a case correctly when a hash of (seed, deployment, case id)
falls under its configured quality, so simulated results measure the harness
and the configured profiles, not any real model. Reports say which provider
produced them. Pure Python (stdlib only).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from . import costs
from .fallback import ChainExhausted, run_chain

REFERENCE_TYPES = ("exact", "contains", "regex")


class EvalConfigError(ValueError):
    pass


@dataclass
class Case:
    id: str
    messages: list[dict]
    reference: dict | None = None
    answer: str | None = None
    tags: list[str] = field(default_factory=list)
    max_tokens: int | None = None


def load_cases(lines: Iterable[str]) -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise EvalConfigError(f"line {n}: invalid JSON ({e.msg})") from None
        unknown = set(raw) - {"id", "messages", "prompt", "reference", "answer", "tags", "max_tokens"}
        if unknown:
            raise EvalConfigError(f"line {n}: unknown fields {sorted(unknown)}")
        cid = raw.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise EvalConfigError(f"line {n}: id must be a unique non-empty string")
        seen.add(cid)
        if ("messages" in raw) == ("prompt" in raw):
            raise EvalConfigError(f"line {n}: give exactly one of messages or prompt")
        msgs = raw.get("messages") or [{"role": "user", "content": raw["prompt"]}]
        if not isinstance(msgs, list) or not all(isinstance(m, dict) and "content" in m for m in msgs):
            raise EvalConfigError(f"line {n}: messages must be a list of {{role, content}}")
        ref = raw.get("reference")
        if ref is not None:
            if (
                not isinstance(ref, dict)
                or ref.get("type") not in REFERENCE_TYPES
                or not isinstance(ref.get("value"), str)
            ):
                raise EvalConfigError(f"line {n}: reference must be {{type: {'|'.join(REFERENCE_TYPES)}, value: str}}")
            if ref["type"] == "regex":
                try:
                    re.compile(ref["value"])
                except re.error as e:
                    raise EvalConfigError(f"line {n}: bad regex ({e})") from None
        cases.append(Case(cid, msgs, ref, raw.get("answer"), list(raw.get("tags") or []), raw.get("max_tokens")))
    if not cases:
        raise EvalConfigError("no cases")
    return cases


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def score_reference(reference: Mapping[str, str], output: str) -> float:
    kind, value = reference["type"], reference["value"]
    if kind == "exact":
        return 1.0 if _norm(output).strip(" .") == _norm(value).strip(" .") else 0.0
    if kind == "contains":
        return 1.0 if _norm(value) in _norm(output) else 0.0
    if kind == "regex":
        return 1.0 if re.search(value, output, re.IGNORECASE) else 0.0
    raise EvalConfigError(f"unknown reference type {kind!r}")


Judge = Callable[[Case, str], float]


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float


Provider = Callable[[Any, Case], Awaitable[Completion]]  # (target with .provider/.model, case) -> Completion


@dataclass
class CaseResult:
    case_id: str
    tags: list[str]
    served_by: str | None
    fell_back: bool
    score: float | None  # reference score, or the judge's when there's no reference
    judge_score: float | None
    cost_usd: float
    latency_s: float
    tokens: int
    error: str | None = None
    output: str = ""


async def run_route(
    name: str, targets: Sequence[Any], cases: Sequence[Case], provider: Provider, judge: Judge | None = None
) -> list[CaseResult]:
    """Run every case through `targets` with the gateway's fallback chain (no breakers: a replay judges each call)."""
    results = []
    for case in cases:

        async def call(target, case=case):
            return await provider(target, case)

        try:
            res = await run_chain(targets, call, strategy="ordered")
        except ChainExhausted as e:
            results.append(
                CaseResult(
                    case.id,
                    case.tags,
                    None,
                    False,
                    0.0 if case.reference else None,
                    None,
                    0.0,
                    0.0,
                    0,
                    error=e.attempts[-1].error if e.attempts else "no targets",
                )
            )
            continue
        c: Completion = res.value
        t = res.target
        cost = costs.dollars_for(t.provider, t.model, c.prompt_tokens, c.completion_tokens)
        ref = score_reference(case.reference, c.text) if case.reference else None
        jdg = None
        if judge is not None:
            jdg = max(0.0, min(1.0, float(judge(case, c.text))))
        latency = c.latency_s + sum(a.seconds for a in res.attempts if a.outcome == "error")
        results.append(
            CaseResult(
                case.id,
                case.tags,
                f"{t.provider}/{t.model}",
                res.fell_back,
                ref if ref is not None else jdg,
                jdg,
                cost,
                latency,
                c.prompt_tokens + c.completion_tokens,
                output=c.text,
            )
        )
    return results


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[k]


def summarize(results: Sequence[CaseResult]) -> dict:
    scored = [r.score for r in results if r.score is not None]
    ok = [r for r in results if r.error is None]
    total_cost = sum(r.cost_usd for r in results)
    by_tag: dict[str, list[float]] = {}
    for r in results:
        if r.score is not None:
            for t in r.tags or ["untagged"]:
                by_tag.setdefault(t, []).append(r.score)
    served: dict[str, int] = {}
    for r in ok:
        served[r.served_by or "?"] = served.get(r.served_by or "?", 0) + 1
    return {
        "cases": len(results),
        "scored": len(scored),
        "quality": round(sum(scored) / len(scored), 4) if scored else None,
        "errors": len(results) - len(ok),
        "fallbacks": sum(1 for r in ok if r.fell_back),
        "total_cost_usd": round(total_cost, 6),
        "cost_per_1k_requests_usd": round(total_cost / len(results) * 1000, 4) if results else 0.0,
        "latency_p50_s": round(_pct([r.latency_s for r in ok], 0.50), 4),
        "latency_p95_s": round(_pct([r.latency_s for r in ok], 0.95), 4),
        "tokens": sum(r.tokens for r in results),
        "quality_by_tag": {t: round(sum(v) / len(v), 4) for t, v in sorted(by_tag.items())},
        "served_by": dict(sorted(served.items())),
        "judge_mean": _mean([r.judge_score for r in results if r.judge_score is not None]),
    }


def _mean(v: Sequence[float]) -> float | None:
    return round(sum(v) / len(v), 4) if v else None


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]
    quality_delta: float | None
    cost_change_pct: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def gate(
    baseline: Mapping, candidate: Mapping, max_quality_drop: float = 0.02, max_cost_increase: float = 0.10
) -> GateResult:
    """Fail when quality drops more than `max_quality_drop` (absolute, 0-1) or cost rises more than `max_cost_increase`
    (fraction, 0.10 = +10%). Errors on the candidate count as quality 0 for their cases already."""
    reasons = []
    qb, qc = baseline.get("quality"), candidate.get("quality")
    dq = None if qb is None or qc is None else round(qc - qb, 4)
    if dq is None:
        reasons.append("quality unavailable (no scored cases)")
    elif dq < -max_quality_drop:
        reasons.append(f"quality fell {-dq:.3f} (limit {max_quality_drop:.3f}): {qb:.3f} -> {qc:.3f}")
    cb, cc = float(baseline.get("total_cost_usd", 0.0)), float(candidate.get("total_cost_usd", 0.0))
    change = None if cb == 0 else round((cc - cb) / cb, 4)
    if change is None:
        if cc > 0:
            reasons.append("baseline cost is 0, candidate cost is not")
    elif change > max_cost_increase:
        reasons.append(f"cost rose {change:.1%} (limit {max_cost_increase:.0%}): ${cb:.4f} -> ${cc:.4f}")
    return GateResult(not reasons, reasons, dq, change)


# ---------- simulated provider ----------


@dataclass
class SimProfile:
    quality: float  # probability of a correct answer, 0-1
    latency_ms: float = 500.0
    error_rate: float = 0.0
    price_in_per_1m: float = 0.0  # USD per 1M tokens, applied as price overrides
    price_out_per_1m: float = 0.0


class SimulatedProviderError(Exception):
    status_code = 503


def _unit(*parts: str) -> float:
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def simulated_provider(profiles: Mapping[str, SimProfile], seed: str = "eval-v1") -> Provider:
    """Deterministic stand-in for real models: same seed, profiles and cases -> same report."""

    async def call(target, case: Case) -> Completion:
        dep = f"{target.provider}/{target.model}"
        p = profiles.get(dep)
        if p is None:
            raise EvalConfigError(f"no simulation profile for {dep}")
        if _unit(seed, dep, case.id, "error") < p.error_rate:
            raise SimulatedProviderError(f"simulated 503 from {dep}")
        correct = _unit(seed, dep, case.id) < p.quality
        text = (case.answer or "") if correct else "I'm not certain; it may depend on details not given."
        prompt_chars = sum(len(str(m.get("content", ""))) for m in case.messages)
        pt = max(1, prompt_chars // 4)
        ct = max(1, len(text) // 4)
        latency = p.latency_ms / 1000 * (0.8 + 0.4 * _unit(seed, dep, case.id, "latency"))
        return Completion(text, pt, ct, latency)

    return call


def apply_sim_prices(profiles: Mapping[str, SimProfile]) -> None:
    costs.set_price_overrides(
        {**costs.price_overrides(), **{d: (p.price_in_per_1m, p.price_out_per_1m) for d, p in profiles.items()}}
    )


def load_profiles(data: Mapping[str, Mapping]) -> dict[str, SimProfile]:
    out = {}
    for dep, raw in data.items():
        if "/" not in dep:
            raise EvalConfigError(f"profile key {dep!r} must be provider/model")
        unknown = set(raw) - {"quality", "latency_ms", "error_rate", "price_in_per_1m", "price_out_per_1m"}
        if unknown:
            raise EvalConfigError(f"profile {dep}: unknown fields {sorted(unknown)}")
        p = SimProfile(**{k: float(v) for k, v in raw.items()})
        if not (0 <= p.quality <= 1 and 0 <= p.error_rate <= 1):
            raise EvalConfigError(f"profile {dep}: quality and error_rate must be within 0..1")
        out[dep] = p
    return out


# ---------- reports ----------


def report(
    routes: Mapping[str, Sequence[Any]],
    results: Mapping[str, Sequence[CaseResult]],
    provider_label: str,
    cases_source: str,
) -> dict:
    summaries = {name: summarize(results[name]) for name in routes}
    return {
        "provider": provider_label,  # "simulated" unless real providers were called
        "cases_source": cases_source,
        "routes": {
            name: {"targets": [f"{t.provider}/{t.model}" for t in routes[name]], "summary": summaries[name]}
            for name in routes
        },
        "cases": {name: [asdict(r) | {"output": r.output[:200]} for r in results[name]] for name in routes},
    }


def to_markdown(rep: Mapping, gate_result: GateResult | None = None) -> str:
    label = rep["provider"]
    lines = [
        "# Route evaluation",
        "",
        f"Provider: **{label}**"
        + (
            " (deterministic simulation; these numbers describe the configured profiles, not real models)"
            if label == "simulated"
            else ""
        ),
        f"Cases: `{rep['cases_source']}`",
        "",
        "| Route | Targets | Quality | Errors | Fallbacks | Cost (USD) | $ / 1K req | p50 s | p95 s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in rep["routes"].items():
        s = r["summary"]
        q = "-" if s["quality"] is None else f"{s['quality']:.3f}"
        lines.append(
            f"| `{name}` | {' → '.join(r['targets'])} | {q} | {s['errors']} | {s['fallbacks']} | "
            f"{s['total_cost_usd']:.6f} | {s['cost_per_1k_requests_usd']:.4f} | {s['latency_p50_s']:.3f} | "
            f"{s['latency_p95_s']:.3f} |"
        )
    lines += ["", "Quality by tag:", ""]
    tags = sorted({t for r in rep["routes"].values() for t in r["summary"]["quality_by_tag"]})
    lines.append("| Route | " + " | ".join(tags) + " |")
    lines.append("|---|" + "---|" * len(tags))
    for name, r in rep["routes"].items():
        qbt = r["summary"]["quality_by_tag"]
        lines.append(f"| `{name}` | " + " | ".join(f"{qbt[t]:.2f}" if t in qbt else "-" for t in tags) + " |")
    if gate_result is not None:
        lines += ["", f"Gate: **{'PASS' if gate_result.ok else 'FAIL'}**"]
        lines += [f"- {r}" for r in gate_result.reasons]
    return "\n".join(lines) + "\n"
