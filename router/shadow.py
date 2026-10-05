# Corey Mathie, 2026
"""
Shadow mode: mirror a share of an alias's live traffic to a candidate alias,
asynchronously, after the client already has its answer.

    shadow:
      smart-fast: { candidate: cheap-batch, percent: 10, billing: ledger, max_daily_usd: 5 }

Guarantees, each covered by a test:
  - the mirrored answer is never returned to the client, and a shadow failure
    never affects the live response (it runs as a background task);
  - the mirror goes through the same policy evaluation as a live request for
    the candidate alias (router/policy.py, OPA if configured), so a team's
    data-residency and model rules still hold, and it receives the request as
    the live path sent it (after pre_request content hooks); hooks the
    candidate's policy requires on top are applied too;
  - its cost goes to a separate `shadow_usage` ledger (or, with
    billing: suppress, isn't recorded at all), never to the usage table, so
    budgets, TPM, anomaly baselines and showback don't see it;
  - it uses its own circuit breakers and latency averages, so shadow failures
    can't open a live breaker or reorder a latency route;
  - `max_daily_usd` stops mirroring once today's shadow ledger reaches it.

Comparison per mirrored call: exact match and word-level Jaccard agreement
between the live and shadow answers, plus cost and latency of each side
(GET /admin/shadow). Streams and cache hits are not mirrored. Shadow calls are
real provider calls: with billing: suppress the provider still charges.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time

from . import audit, costs, metrics, pii
from .admission import evaluate_request
from .breaker import BreakerRegistry
from .fallback import ChainExhausted, run_chain
from .latency import LatencyTracker
from .models import ApiKey, ChatCompletionRequest, RouterConfig, RouteTarget
from .privacy import key_fp
from .store import shadow_record, shadow_spend_today

log = logging.getLogger("router")

breakers = BreakerRegistry()  # isolated from the live registries in routing.py
latency = LatencyTracker()
_tasks: set[asyncio.Task] = set()
_rng = random.Random()


def reset() -> None:
    breakers.reset()
    latency.reset()


def sampled(percent: float) -> bool:
    return percent >= 100 or (percent > 0 and _rng.random() * 100 < percent)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def agreement(a: str, b: str) -> float:
    """Word-level Jaccard similarity of two answers (1.0 for two empty answers)."""
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)


def maybe_mirror(
    cfg: RouterConfig,
    key: ApiKey,
    body: ChatCompletionRequest,
    primary: RouteTarget,
    primary_text: str,
    primary_cost: float,
    primary_latency: float,
    call,
) -> asyncio.Task | None:
    """Schedule a mirror of this request when the alias has a shadow config and the sample hits."""
    sh = cfg.shadow.get(body.model)
    if sh is None or not sampled(sh.percent):
        return None
    task = asyncio.get_running_loop().create_task(
        _mirror(cfg, key, body, primary, primary_text, primary_cost, primary_latency, call)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain(timeout: float = 5.0) -> None:
    """Wait for in-flight mirrors (tests, graceful shutdown)."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=timeout)


async def _mirror(cfg, key, body, primary, primary_text, primary_cost, primary_latency, call) -> None:
    alias = body.model
    sh = cfg.shadow[alias]
    base = {
        "team": key.team,
        "key_fp": key_fp(key),
        "alias": alias,
        "candidate": sh.candidate,
        "primary_deployment": f"{primary.provider}/{primary.model}",
        "primary_cost_usd": primary_cost,
        "primary_latency_s": primary_latency,
    }
    try:
        if sh.max_daily_usd and shadow_spend_today() >= sh.max_daily_usd:
            metrics.shadow(alias, sh.candidate, "skipped_cap")
            audit.record("shadow", "skipped_cap", team=key.team, key_fp=key_fp(key), subject=alias,
                         detail={"candidate": sh.candidate, "max_daily_usd": sh.max_daily_usd})  # fmt: skip
            return
        mirror = body.model_copy(update={"model": sh.candidate})
        decision, _ = await evaluate_request(key, mirror, cfg.aliases[sh.candidate])
        if not decision.allow:
            metrics.shadow(alias, sh.candidate, "skipped_policy")
            audit.record("shadow", "skipped_policy", team=key.team, key_fp=key_fp(key), subject=alias,
                         detail={"candidate": sh.candidate, "reasons": decision.reasons})  # fmt: skip
            return
        mirror = mirror.model_copy(update={"max_tokens": decision.max_tokens})
        if decision.required_hooks:
            ctx = pii.HookContext("pre_request", key.team, sh.candidate, tuple(cfg.privacy.kinds))
            run = pii.run_hooks(decision.required_hooks, [m.content for m in mirror.messages], ctx)
            if run.blocked:
                metrics.shadow(alias, sh.candidate, "skipped_hook")
                return
            msgs = [m.model_copy(update={"content": t}) for m, t in zip(mirror.messages, run.texts, strict=True)]
            mirror = mirror.model_copy(update={"messages": msgs})

        started = time.perf_counter()
        try:
            res = await run_chain(
                decision.targets, lambda t: call(t, mirror), strategy=cfg.strategy(sh.candidate),
                breakers=breakers, latency=latency,
            )  # fmt: skip
        except ChainExhausted as e:
            metrics.shadow(alias, sh.candidate, "error")
            err = e.attempts[-1].error if e.attempts else "no permitted deployment"
            shadow_record({**base, "error": err[:200], "latency_s": time.perf_counter() - started})
            return
        data, pt, ct, _cost = res.value
        t = res.target
        cost = costs.dollars_for(t.provider, t.model, pt, ct)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        shadow_record(
            {
                **base,
                "provider": t.provider,
                "model": t.model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cost_usd": cost if sh.billing == "ledger" else None,
                "latency_s": time.perf_counter() - started,
                "agreement": agreement(primary_text, text),
                "exact_match": int(" ".join(primary_text.split()) == " ".join(text.split())),
            }
        )
        metrics.shadow(alias, sh.candidate, "ok")
    except Exception:  # a shadow problem must never surface anywhere near the live request
        log.exception("shadow mirror %s -> %s failed", alias, sh.candidate)
        metrics.shadow(alias, sh.candidate, "error")
