# Corey Mathie, 2026
"""
Alias resolution, admission control, the fallback loop, the response cache, and streaming.

Order of operations for every request:
  0. policy    policy-as-code (router/policy.py, optional OPA): alias, size, max_tokens ceiling,
               provider/model allow and deny lists; removes deployments the policy excludes
               (403 / 400 / 413, or 503 if the policy can't be evaluated: fail closed)
  1. budgets   org -> team -> key, daily and monthly caps (402 if one is reached)
  2. anomaly   auto-pause a key at 10x its 7-day baseline, if enabled (429)
  3. content   pre_request hooks (PII redaction / block) over message contents (422, or 503 if a hook fails)
  4. cache     exact-match, per-team, deterministic requests only (no tokens reserved); then the
               semantic cache if enabled (router/semcache.py), partitioned by team, policy and context
  5. tokens    reserve estimated tokens against per-key / per-team TPM limits (429 + Retry-After)
  6. chain     walk the route (configured or latency order), skipping deployments whose
               circuit breaker is open; the first success wins (fallback.run_chain)
  7. settle    correct the token reservation to real usage, record cost, run post_response
               hooks, cache the answer, write the opt-in redacted content log

Streaming follows the same steps, except that fallback can only happen before the
first chunk reaches the client; after that, a provider failure ends the stream
with an error event. Usage and cost are recorded when the stream finishes,
including when it ends early (generator closed or cancelled).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import HTTPException

from . import admission, costs, metrics, mock_provider, privacy, semcache, shadow, telemetry, traces
from .anomaly import evaluate
from .breaker import BreakerConfig, BreakerRegistry, Transition, counts_as_failure
from .budgets import Hierarchy, Limits
from .budgets import check as check_budgets
from .config_loader import current, settings
from .costs import dollars_for
from .fallback import Attempt, ChainExhausted, run_chain
from .latency import LatencyConfig, LatencyTracker
from .models import ApiKey, ChatCompletionRequest, LimitOverride, RouterConfig, RouteTarget
from .store import budget_overrides, cache_get, cache_put, key_hold, record_call, spend_for
from .tokens import Reservation, TokenLimitExceeded, TokenRateLimiter, estimate_request_tokens

log = logging.getLogger("router")

# Process-local resilience state (one set per worker).
breakers = BreakerRegistry()
latency = LatencyTracker()
tpm = TokenRateLimiter()
semantic = semcache.SemanticCache(threshold=0.90)
_hashing = semcache.HashingEmbedder()
_applied: RouterConfig | None = None
_overrides: list[dict] | None = None  # budget caps set through the admin API (store.budget_overrides)


def _on_transition(t: Transition) -> None:
    log.warning("circuit %s: %s -> %s (%s)", t.deployment, t.from_state, t.to_state, t.reason)
    metrics.circuit_transition(t.deployment, t.from_state, t.to_state)


breakers.listeners.append(_on_transition)


def sync_config(cfg: RouterConfig | None = None) -> RouterConfig:
    """Push the active routes config into the breakers, latency tracker and price overrides."""
    global _applied
    cfg = cfg or current()
    if cfg is not _applied:
        breakers.configure(BreakerConfig(**cfg.resilience.circuit_breaker.model_dump()))
        latency.configure(LatencyConfig(**cfg.resilience.latency.model_dump()))
        costs.set_price_overrides({k: (p.input_usd_per_1m, p.output_usd_per_1m) for k, p in cfg.prices.items()})
        sc = cfg.semantic_cache
        semantic.threshold, semantic.ttl_seconds = sc.threshold, float(sc.ttl_seconds)
        semantic.max_entries = sc.max_entries_per_partition
        semantic.check_numbers, semantic.check_negation = sc.guards.numbers, sc.guards.negation
        _applied = cfg
    return cfg


def reset_state() -> None:
    """Forget breaker, latency and TPM state (tests, or an operator reset)."""
    global _applied
    breakers.reset()
    latency.reset()
    tpm.reset()
    shadow.reset()
    semantic.clear()
    invalidate_budget_overrides()
    _applied = None


def invalidate_budget_overrides() -> None:
    global _overrides
    _overrides = None


def _budget_overrides() -> list[dict]:
    global _overrides
    if _overrides is None:
        _overrides = budget_overrides()
    return _overrides


def hierarchy(cfg: RouterConfig) -> Hierarchy:
    pol = cfg.policies

    def lim(o: LimitOverride) -> Limits:
        return Limits(o.daily_usd, o.monthly_usd, o.tpm)

    org = lim(cfg.budgets.org)
    teams = {t: lim(o) for t, o in cfg.budgets.teams.items()}
    keys = {k: lim(o) for k, o in cfg.budgets.keys.items()}
    # Caps set from the admin API win over the routes file, field by field (unset fields keep the file's value).
    for o in _budget_overrides():
        if o["scope"] == "org":
            base = org
        else:
            base = (teams if o["scope"] == "team" else keys).get(o["name"], Limits())
        merged = Limits(
            o["daily_usd"] if o["daily_usd"] is not None else base.daily_usd,
            o["monthly_usd"] if o["monthly_usd"] is not None else base.monthly_usd,
            o["tpm"] if o["tpm"] is not None else base.tpm,
        )
        if o["scope"] == "org":
            org = merged
        elif o["scope"] == "team":
            teams[o["name"]] = merged
        else:
            keys[o["name"]] = merged
    return Hierarchy(
        org=org,
        team_default=Limits(pol.per_team_daily_usd, pol.per_team_monthly_usd, pol.per_team_tpm),
        key_default=Limits(pol.per_key_daily_usd, pol.per_key_monthly_usd, pol.per_key_tpm),
        teams=teams,
        keys=keys,
    )


def _litellm_model(provider: str, model: str) -> str:
    if provider not in {"openai", "anthropic", "gemini", "ollama"}:
        raise ValueError(f"unknown provider: {provider}")
    return f"{provider}/{model}"


def _provider_kwargs(provider: str) -> dict:
    return {
        "openai": {"api_key": settings.OPENAI_API_KEY},
        "anthropic": {"api_key": settings.ANTHROPIC_API_KEY},
        "gemini": {"api_key": settings.GEMINI_API_KEY},
        "ollama": {"api_base": settings.OLLAMA_HOST},
    }.get(provider, {})


def check_policies(key: ApiKey) -> None:
    cfg = current()
    traces.begin("budgets")
    h = hierarchy(cfg)
    breach = check_budgets(h, key.team, key.label, key.id, spend_for)
    if breach:
        metrics.rejection(breach.reason)
        raise HTTPException(402, breach.message)
    if traces.current() is not None:
        cap = h.team(key.team).daily_usd or 0.0
        spent = spend_for("team", key.team, "daily")
        cap_text = f"${cap:,.2f}" if cap else "no cap"
        traces.end("pass", f"team {key.team} ${spent:,.4f} of {cap_text} today", team_spent_usd=round(spent, 6))
    traces.begin("anomaly")
    hold = key_hold(key.id)
    if hold == "paused":
        metrics.rejection("admin_pause")
        raise HTTPException(429, "key paused by an admin. Contact an admin.")
    if cfg.policies.auto_pause_on_anomaly:
        signal = evaluate(key.id, use_cache=True)
        if signal.verdict == "pause" and hold != "override":
            metrics.rejection("anomaly_pause")
            log.warning("auto-paused key %s: hourly spend %.1fx baseline", key.label, signal.multiple)
            raise HTTPException(
                429, f"key paused: this hour's spend is {signal.multiple:.0f}x its 7-day baseline. Contact an admin."
            )
        note = " (admin override this hour)" if hold == "override" and signal.verdict == "pause" else ""
        traces.end("pass", f"{signal.verdict}: {signal.multiple:.1f}x the 7-day baseline{note}", verdict=signal.verdict)
    else:
        traces.end("skip", "auto_pause_on_anomaly is off")


def _key_scope(key: ApiKey) -> str:
    return f"key:{key.label}:{key.fingerprint}"


def reserve_tokens(key: ApiKey, body: ChatCompletionRequest, cfg: RouterConfig) -> Reservation | None:
    """Reserve estimated tokens against TPM limits. None when no TPM limit applies to this key."""
    h = hierarchy(cfg)
    limits = {f"team:{key.team}": h.team(key.team).tpm or 0, _key_scope(key): h.key(key.label).tpm or 0}
    traces.begin("tpm")
    if not any(limits.values()):
        traces.end("skip", "no TPM limit for this key or team")
        return None
    estimate = estimate_request_tokens(
        [m.model_dump() for m in body.messages], body.max_tokens, cfg.policies.default_output_tokens_estimate
    )
    try:
        res = tpm.reserve(limits, estimate)
        traces.end("pass", f"reserved {estimate} estimated tokens", estimate=estimate)
        return res
    except TokenLimitExceeded as e:
        metrics.rejection(f"tpm_{e.scope.split(':', 1)[0]}")
        raise HTTPException(429, str(e), headers={"Retry-After": str(max(1, math.ceil(e.retry_after)))}) from e


def _targets(body: ChatCompletionRequest) -> list[RouteTarget]:
    cfg = current()
    if body.model not in cfg.aliases:
        raise HTTPException(400, f"unknown model alias {body.model!r}; available: {sorted(cfg.aliases)}")
    return cfg.aliases[body.model]


def cache_key(alias: str, body: ChatCompletionRequest, policy_fp: str = "") -> str:
    material = {
        "alias": alias,
        "messages": [m.model_dump() for m in body.messages],
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    if policy_fp:  # content hooks in force; a policy change must not serve answers cached under the old one
        material["policy"] = policy_fp
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _serve_cached(key, body, content, strategy, extra, hit: dict, kind: str = "hit") -> RouteOutcome:
    """Answer from the exact or semantic cache: $0, recorded as a cache hit with the dollars saved."""
    data = json.loads(hit["response_json"])
    usage = data.get("usage") or {}
    record_call(
        key.id,
        key.team,
        body.model,
        hit["provider"],
        hit["model"],
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        0.0,
        cached=True,
        saved_usd=hit["cost_usd"],
    )
    metrics.observe(body.model, hit["provider"], "cache_hit", saved_usd=hit["cost_usd"])
    traces.update(
        cache=kind,
        served_by=f"{hit['provider']}/{hit['model']}",
        saved_usd=float(hit["cost_usd"] or 0),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
    traces.stage("settle", "pass", f"served from cache at $0; saved ${float(hit['cost_usd'] or 0):.6f}", ms=None)
    privacy.log_content(key, body, privacy.response_text(data), content)
    _trace_content_log(content)
    return RouteOutcome(data, hit["provider"], hit["model"], True, strategy, extra_headers=extra, cache_kind=kind)


async def _embed(text: str, cfg: RouterConfig) -> list[float]:
    sc = cfg.semantic_cache
    if sc.embedder == "hashing":
        return _hashing.embed(text)
    import litellm

    provider = sc.embedding_model.split("/", 1)[0]
    resp = await litellm.aembedding(model=sc.embedding_model, input=[text], **_provider_kwargs(provider))
    data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    return [float(x) for x in data["data"][0]["embedding"]]


async def _semantic_lookup(cfg, key, body, content, decision, extra):
    """Semantic cache: a RouteOutcome on a hit, (partition, text, vector) to store after a miss, or None."""
    sc = cfg.semantic_cache
    if not sc.applies_to(key.team) or body.temperature > sc.max_temperature or body.messages[-1].role != "user":
        traces.stage("semantic_cache", "skip", "off for this team" if not sc.applies_to(key.team) else "not eligible")
        return None
    alias = body.model
    text = body.messages[-1].content
    try:
        vec = await _embed(text, cfg)
    except Exception as e:  # noqa: BLE001 - the cache is an optimisation: on embedder failure, just route
        log.warning("semantic cache embedder failed for alias %s: %s", alias, type(e).__name__)
        metrics.semantic(alias, "embed_error")
        traces.stage("semantic_cache", "skip", "embedder failed; routed normally")
        return None
    # Everything except the final user turn must match exactly: team, alias, policy decision and content hooks,
    # earlier messages (system prompt, history), generation parameters, and the embedding space.
    context = json.dumps([m.model_dump() for m in body.messages[:-1]], sort_keys=True)
    part = semcache.partition_key(
        key.team, alias, partition(content, decision), context, body.max_tokens, sc.embedder, sc.embedding_model
    )
    semantic.threshold = sc.threshold_for(key.team)
    found = semantic.lookup(part, text, vec)
    extra["x-router-semantic-similarity"] = f"{found.similarity:.4f}"
    if found.hit:
        metrics.semantic(alias, "hit")
        extra["x-router-semantic-match"] = "hit"
        traces.stage(
            "semantic_cache", "hit", f"similarity {found.similarity:.3f}", similarity=round(found.similarity, 4)
        )
        return _serve_cached(key, body, content, cfg.strategy(alias), extra, found.entry.value, "semantic-hit")
    metrics.semantic(alias, "guard" if found.reason.startswith("guard") else "miss")
    traces.stage(
        "semantic_cache",
        "miss",
        f"{found.reason} (similarity {found.similarity:.3f})",
        similarity=round(found.similarity, 4),
    )
    extra["x-router-semantic-match"] = found.reason.replace("guard: ", "guard-").replace(" ", "-")
    return part, text, vec


async def complete(target: RouteTarget, body: ChatCompletionRequest) -> tuple[dict, int, int, float]:
    """One non-streaming provider call: (response dict, prompt tokens, completion tokens, cost)."""
    resp = await acompletion(**_call_kwargs(target, body))
    data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    usage = data.get("usage") or {}
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return data, pt, ct, dollars_for(target.provider, target.model, pt, ct)


async def acompletion(**kwargs):
    """LiteLLM, or the simulated providers when ROUTER_MOCK_PROVIDERS is on."""
    if settings.ROUTER_MOCK_PROVIDERS:
        return await mock_provider.acompletion(**kwargs)
    import litellm

    return await litellm.acompletion(**kwargs)


async def _admit(key: ApiKey, body: ChatCompletionRequest):
    """Policy before anything else: returns the body (max_tokens possibly set/lowered) and the permitted targets."""
    traces.begin("policy")
    decision = await admission.decide(key, body, _targets(body))
    if decision.max_tokens != body.max_tokens:
        body = body.model_copy(update={"max_tokens": decision.max_tokens})
    removed = [r["deployment"] for r in decision.removed]
    summary = f"allow ({', '.join(decision.rules)})"
    if removed:
        summary += f"; removed {', '.join(removed)}"
    if decision.max_tokens_clamped:
        summary += f"; max_tokens {decision.max_tokens}"
    traces.end(
        "pass",
        summary,
        rules=decision.rules,
        removed=decision.removed or None,
        targets=[f"{t.provider}/{t.model}" for t in decision.targets],
        max_tokens=decision.max_tokens,
        required_hooks=decision.required_hooks or None,
        source=decision.source,
    )
    return body, decision.targets, decision


def _trace_hooks(stage: str, names: list[str], content: privacy.ContentPolicy) -> None:
    if traces.current() is None:
        return
    key = "pre_request" if stage == "pre_hooks" else "post_response"
    if not names:
        traces.end("skip", "no hooks configured")
        return
    found = content.findings.get(key) or {}
    what = ", ".join(f"{k}={v}" for k, v in sorted(found.items())) or "nothing found"
    traces.end("pass", f"{', '.join(names)}: {what}", hooks=names, findings=found or None)


def _trace_content_log(content: privacy.ContentPolicy) -> None:
    if content.log_content:
        traces.stage("content_log", "pass", "stored redacted (team opted in)", ms=None)
    else:
        traces.stage("content_log", "skip", "off: metadata only", ms=None)


def _trace_chain(strategy: str, attempts: list[Attempt], ok: bool) -> None:
    if traces.current() is None:
        return
    tried = [a for a in attempts if a.outcome != "skipped"]
    failed = [a.deployment for a in attempts if a.outcome == "error"]
    skipped = [a.deployment for a in attempts if a.outcome == "skipped"]
    served = next((a.deployment for a in attempts if a.outcome == "ok"), None)
    parts = [f"{strategy} order"]
    if served:
        parts.append(f"served by {served}")
    if failed:
        parts.append(f"failed: {', '.join(failed)}")
    if skipped:
        parts.append(f"skipped (circuit open): {', '.join(skipped)}")
    traces.end("pass" if ok else "error", "; ".join(parts), attempts=[a.as_dict() for a in attempts], strategy=strategy)
    traces.update(attempts=len(tried), fell_back=len(attempts) > 1, served_by=served)


def partition(content: privacy.ContentPolicy, decision) -> str:
    """Cache partition beyond the team: the content hooks and the policy decision in force."""
    return hashlib.sha256(f"{content.fingerprint()}|{decision.fingerprint()}".encode()).hexdigest()[:16]


def _cacheable(body: ChatCompletionRequest) -> bool:
    pol = current().policies
    return pol.cache_ttl_seconds > 0 and body.temperature <= pol.cache_max_temperature


def _call_kwargs(target: RouteTarget, body: ChatCompletionRequest) -> dict:
    return {
        "model": _litellm_model(target.provider, target.model),
        "messages": [m.model_dump() for m in body.messages],
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "timeout": target.timeout_s,
        **_provider_kwargs(target.provider),
    }


def _on_attempt(key: ApiKey, alias: str):
    def handle(target: RouteTarget, a: Attempt) -> None:
        if a.outcome == "error":
            log.warning("provider %s failed for alias %s: %s", a.deployment, alias, a.error)
            record_call(key.id, key.team, alias, target.provider, target.model, 0, 0, 0.0, error=a.error[:200])
            metrics.observe(alias, target.provider, "error")
        elif a.outcome == "skipped":
            metrics.observe(alias, target.provider, "skipped")

    return handle


def chain_headers(strategy: str, attempts: list[Attempt]) -> dict[str, str]:
    """Response headers describing how the request was routed."""
    h = {"x-router-strategy": strategy, "x-router-attempts": str(sum(1 for a in attempts if a.outcome != "skipped"))}
    failed = [a.deployment for a in attempts if a.outcome == "error"]
    skipped = [f"{a.deployment}={a.breaker_state}" for a in attempts if a.outcome == "skipped"]
    if failed:
        h["x-router-fallback-from"] = ",".join(failed)
    if skipped:
        h["x-router-circuit-skipped"] = ",".join(skipped)
    return h


def _exhausted(alias: str, strategy: str, e: ChainExhausted) -> HTTPException:
    headers = chain_headers(strategy, e.attempts)
    if e.retry_after:
        headers["Retry-After"] = str(max(1, math.ceil(e.retry_after)))
    if e.all_skipped:
        metrics.rejection("circuit_open")
        return HTTPException(503, f"no provider available for alias {alias!r}: all circuits open", headers=headers)
    # Provider error details stay in logs and the usage table; the client gets a clean message.
    return HTTPException(502, f"all providers failed for alias {alias!r}", headers=headers)


@dataclass
class RouteOutcome:
    data: dict
    provider: str
    model: str
    cached: bool
    strategy: str = "ordered"
    attempts: list[Attempt] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)
    cache_kind: str = "hit"  # "hit" (exact) or "semantic-hit"

    def headers(self) -> dict[str, str]:
        h = {
            "x-router-used-provider": self.provider,
            "x-router-used-model": self.model,
            "x-router-cache": self.cache_kind if self.cached else "miss",
        }
        if not self.cached:
            h.update(chain_headers(self.strategy, self.attempts))
        h.update(self.extra_headers)
        return h


# ---------- Non-streaming ----------


async def route(key: ApiKey, body: ChatCompletionRequest) -> RouteOutcome:
    cfg = sync_config()
    alias = body.model
    strategy = cfg.strategy(alias)
    body, targets, decision = await _admit(key, body)
    check_policies(key)
    content = privacy.ContentPolicy.for_request(cfg, key.team, alias, decision.required_hooks)
    traces.begin("pre_hooks")
    body = privacy.apply_pre(key, body, content)
    _trace_hooks("pre_hooks", content.pre, content)
    extra = {**content.headers(), **admission.headers(decision)}

    ck = cache_key(alias, body, partition(content, decision)) if _cacheable(body) else None
    if ck:
        traces.begin("cache")
        hit = cache_get(key.team, ck, cfg.policies.cache_ttl_seconds)
        if hit:
            traces.end("hit", f"exact match for team {key.team}")
            return _serve_cached(key, body, content, strategy, extra, hit)
        traces.end("miss", "no cached answer")
    else:
        why = (
            "cache_ttl_seconds is 0"
            if cfg.policies.cache_ttl_seconds <= 0
            else "temperature above cache_max_temperature"
        )
        traces.stage("cache", "skip", why, ms=None)

    sem_slot = await _semantic_lookup(cfg, key, body, content, decision, extra)
    if isinstance(sem_slot, RouteOutcome):
        return sem_slot

    reservation = reserve_tokens(key, body, cfg)
    attempt_no = 0

    async def call(target: RouteTarget) -> tuple[dict, int, int, float]:
        nonlocal attempt_no
        attempt_no += 1
        span = telemetry.start_chat_span(target.provider, target.model, alias, attempt_no, strategy)
        try:
            data, pt, ct, cost = await complete(target, body)
        except BaseException as e:
            span.fail(e)
            span.end()
            raise
        span.set_usage(pt, ct, cost, data.get("model"))
        span.end()
        return data, pt, ct, cost

    traces.begin("chain")
    try:
        result = await run_chain(
            targets, call, strategy=strategy, breakers=breakers, latency=latency, on_attempt=_on_attempt(key, alias)
        )
    except ChainExhausted as e:
        if reservation:
            reservation.release()
        _trace_chain(strategy, e.attempts, False)
        raise _exhausted(alias, strategy, e) from None
    except BaseException:
        if reservation:
            reservation.release()
        raise
    _trace_chain(strategy, result.attempts, True)

    target = result.target
    data, pt, ct, cost = result.value
    if reservation:
        reservation.settle(pt + ct)
    record_call(key.id, key.team, alias, target.provider, target.model, pt, ct, cost)
    metrics.observe(alias, target.provider, "ok", cost_usd=cost, input_tokens=pt, output_tokens=ct)
    traces.update(prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)
    traces.stage("settle", "pass", f"{pt} in + {ct} out tokens · ${cost:.6f}", ms=None, cost_usd=round(cost, 8))
    data["model"] = f"{target.provider}/{target.model}"
    raw_text = privacy.response_text(data)
    # The call is paid for and recorded above; a post_response hook can still withhold the answer (422).
    traces.begin("post_hooks")
    data = privacy.apply_post(key, alias, data, content)
    _trace_hooks("post_hooks", content.post, content)
    # Shadow mode: mirror to the candidate alias in the background; never affects this response.
    shadow.maybe_mirror(cfg, key, body, target, raw_text, cost, sum(a.seconds for a in result.attempts), complete)
    if ck:
        cache_put(key.team, ck, alias, target.provider, target.model, json.dumps(data, default=str), cost)
    if sem_slot is not None:
        part, text, vec = sem_slot
        value = {"provider": target.provider, "model": target.model, "cost_usd": cost}
        semantic.put(part, text, vec, {**value, "response_json": json.dumps(data, default=str)})
    privacy.log_content(key, body, privacy.response_text(data), content)
    _trace_content_log(content)
    return RouteOutcome(
        data,
        target.provider,
        target.model,
        False,
        strategy,
        result.attempts,
        extra_headers={**extra, **content.headers()},  # content findings may have grown in post_response
    )


# ---------- Streaming ----------


async def open_stream(
    key: ApiKey, body: ChatCompletionRequest
) -> tuple[RouteTarget, AsyncIterator[str], dict[str, str]]:
    """Find a provider that starts streaming. Returns (target, server-sent-event generator, routing headers)."""

    cfg = sync_config()
    alias = body.model
    strategy = cfg.strategy(alias)
    body, targets, decision = await _admit(key, body)
    check_policies(key)
    content = privacy.ContentPolicy.for_request(cfg, key.team, alias, decision.required_hooks)
    if content.post:
        # Redacting a stream chunk by chunk can't see matches split across chunks, so it isn't offered.
        metrics.rejection("privacy_stream_unsupported")
        raise HTTPException(400, "stream: true is not available here: a post_response content hook applies")
    traces.begin("pre_hooks")
    body = privacy.apply_pre(key, body, content)
    _trace_hooks("pre_hooks", content.pre, content)
    traces.stage("cache", "skip", "streams are not cached", ms=None)
    reservation = reserve_tokens(key, body, cfg)
    attempt_no = 0

    async def call(target: RouteTarget):
        nonlocal attempt_no
        attempt_no += 1
        span = telemetry.start_chat_span(target.provider, target.model, alias, attempt_no, strategy, stream=True)
        try:
            stream = await acompletion(
                **_call_kwargs(target, body), stream=True, stream_options={"include_usage": True}
            )
            iterator = stream.__aiter__()
            try:
                first = await iterator.__anext__()
            except StopAsyncIteration:
                raise RuntimeError("empty stream") from None
        except BaseException as e:
            span.fail(e)
            span.end()
            raise
        return first, iterator, span

    traces.begin("chain")
    try:
        # Time to first chunk feeds the TTFT average used by latency routes.
        result = await run_chain(
            targets,
            call,
            strategy=strategy,
            breakers=breakers,
            latency=latency,
            kind="ttft",
            on_attempt=_on_attempt(key, alias),
        )
    except ChainExhausted as e:
        if reservation:
            reservation.release()
        _trace_chain(strategy, e.attempts, False)
        raise _exhausted(alias, strategy, e) from None
    except BaseException:
        if reservation:
            reservation.release()
        raise
    _trace_chain(strategy, result.attempts, True)

    first, iterator, span = result.value
    events = _relay(key, body, result.target, first, iterator, span, reservation, content, traces.current())
    return (
        result.target,
        events,
        {**chain_headers(strategy, result.attempts), **content.headers(), **admission.headers(decision)},
    )


async def _relay(
    key: ApiKey,
    body: ChatCompletionRequest,
    target: RouteTarget,
    first,
    iterator,
    span: telemetry.GenAISpan | None = None,
    reservation: Reservation | None = None,
    content: privacy.ContentPolicy | None = None,
    trace: traces.Trace | None = None,
) -> AsyncIterator[str]:
    alias = body.model
    served_as = f"{target.provider}/{target.model}"
    text_parts: list[str] = []
    usage: dict | None = None
    error: str | None = None
    span = span or telemetry.GenAISpan(None)

    def sse(chunk) -> str:
        nonlocal usage
        data = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
        for choice in data.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                text_parts.append(piece)
        if data.get("usage"):
            usage = data["usage"]
        data["model"] = served_as
        return f"data: {json.dumps(data, default=str)}\n\n"

    try:
        yield sse(first)
        try:
            async for chunk in iterator:
                yield sse(chunk)
        except Exception as e:  # noqa: BLE001 - mid-stream failure: no fallback possible, tell the client
            error = f"stream interrupted: {e}"[:200]
            log.warning("stream from %s failed mid-response for alias %s: %s", served_as, alias, e)
            span.fail(e)
            if counts_as_failure(e):
                breakers.get(served_as).record_failure(reason="mid-stream")
            yield f"data: {json.dumps({'error': {'message': 'The provider stopped responding mid-stream.'}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        # Runs when the stream ends, fails, or the generator is closed early (client went away),
        # so tokens received so far are recorded and count toward caps.
        pt, ct = _stream_tokens(target, body, usage, "".join(text_parts))
        cost = dollars_for(target.provider, target.model, pt, ct)
        if reservation:
            reservation.settle(pt + ct)
        record_call(key.id, key.team, alias, target.provider, target.model, pt, ct, cost, error=error)
        metrics.observe(
            alias, target.provider, "error" if error else "ok", cost_usd=cost, input_tokens=pt, output_tokens=ct
        )
        span.set_usage(pt, ct, cost)
        span.end()
        if content is not None:
            privacy.log_content(key, body, "".join(text_parts), content)
        if trace is not None:
            trace.prompt_tokens, trace.completion_tokens, trace.cost_usd = pt, ct, cost
            summary = f"{pt} in + {ct} out tokens · ${cost:.6f}" + (f" · {error}" if error else "")
            traces.record(trace, "settle", "error" if error else "pass", summary, ms=None, cost_usd=round(cost, 8))
            if content is not None:
                log_note = "stored redacted (team opted in)" if content.log_content else "off: metadata only"
                traces.record(trace, "content_log", "pass" if content.log_content else "skip", log_note, ms=None)


def _stream_tokens(target: RouteTarget, body: ChatCompletionRequest, usage: dict | None, text: str) -> tuple[int, int]:
    if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    # Some providers don't report usage on streams; count tokens ourselves so spend caps still work.
    try:
        import litellm

        model = _litellm_model(target.provider, target.model)
        pt = litellm.token_counter(model=model, messages=[m.model_dump() for m in body.messages])
        ct = litellm.token_counter(model=model, text=text) if text else 0
        return int(pt), int(ct)
    except Exception:  # noqa: BLE001 - counting is best effort
        return 0, 0
