# Corey Mathie, 2026
"""
Alias resolution, the fallback loop, the response cache, and streaming.

Non-streaming: check policies, serve from cache if allowed, otherwise try each
target in order; the first success wins and is cached.

Streaming: check policies, then open a stream on each target in order until
one produces its first chunk. Fallback can only happen before the first byte
reaches the client; after that, a provider failure ends the stream with an
error event. Usage and cost are recorded when the stream finishes, even if the
client disconnects early.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import HTTPException

from . import metrics
from .anomaly import evaluate
from .config_loader import current, settings
from .costs import dollars_for
from .models import ApiKey, ChatCompletionRequest, RouteTarget
from .store import cache_get, cache_put, record_call, spend_today

log = logging.getLogger("router")


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
    pol = current().policies
    if pol.per_key_daily_usd and spend_today(key=key.key) >= pol.per_key_daily_usd:
        raise HTTPException(402, "per-key daily spend cap reached")
    if pol.per_team_daily_usd and spend_today(team=key.team) >= pol.per_team_daily_usd:
        raise HTTPException(402, "per-team daily spend cap reached")
    if pol.auto_pause_on_anomaly:
        signal = evaluate(key.key, use_cache=True)
        if signal.verdict == "pause":
            log.warning("auto-paused key %s: hourly spend %.1fx baseline", key.label, signal.multiple)
            raise HTTPException(
                429, f"key paused: this hour's spend is {signal.multiple:.0f}x its 7-day baseline. Contact an admin."
            )


def _targets(body: ChatCompletionRequest) -> list[RouteTarget]:
    cfg = current()
    if body.model not in cfg.aliases:
        raise HTTPException(400, f"unknown model alias {body.model!r}; available: {sorted(cfg.aliases)}")
    return cfg.aliases[body.model]


def cache_key(alias: str, body: ChatCompletionRequest) -> str:
    material = {
        "alias": alias,
        "messages": [m.model_dump() for m in body.messages],
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


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


def _record_failure(key: ApiKey, alias: str, target: RouteTarget, err: Exception) -> None:
    log.warning("provider %s/%s failed for alias %s: %s", target.provider, target.model, alias, err)
    record_call(key.key, key.team, alias, target.provider, target.model, 0, 0, 0.0, error=str(err)[:200])
    metrics.observe(alias, target.provider, "error")


# ---------- Non-streaming ----------


async def route(key: ApiKey, body: ChatCompletionRequest) -> tuple[dict, str, str, bool]:
    """Returns (response, provider, model, served_from_cache)."""
    import litellm

    alias = body.model
    targets = _targets(body)
    check_policies(key)

    ck = cache_key(alias, body) if _cacheable(body) else None
    if ck:
        hit = cache_get(key.team, ck, current().policies.cache_ttl_seconds)
        if hit:
            data = json.loads(hit["response_json"])
            usage = data.get("usage") or {}
            record_call(
                key.key,
                key.team,
                alias,
                hit["provider"],
                hit["model"],
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                0.0,
                cached=True,
                saved_usd=hit["cost_usd"],
            )
            metrics.observe(alias, hit["provider"], "cache_hit", saved_usd=hit["cost_usd"])
            return data, hit["provider"], hit["model"], True

    for target in targets:
        try:
            resp = await litellm.acompletion(**_call_kwargs(target, body))
        except Exception as e:  # noqa: BLE001 - any provider failure triggers fallback
            _record_failure(key, alias, target, e)
            continue

        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        usage = data.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        cost = dollars_for(target.provider, target.model, pt, ct)
        record_call(key.key, key.team, alias, target.provider, target.model, pt, ct, cost)
        metrics.observe(alias, target.provider, "ok", cost_usd=cost)
        data["model"] = f"{target.provider}/{target.model}"
        if ck:
            cache_put(key.team, ck, alias, target.provider, target.model, json.dumps(data, default=str), cost)
        return data, target.provider, target.model, False

    # Provider error details stay in logs and the usage table; the client gets a clean message.
    raise HTTPException(502, f"all providers failed for alias {alias!r}")


# ---------- Streaming ----------


async def open_stream(key: ApiKey, body: ChatCompletionRequest) -> tuple[RouteTarget, AsyncIterator[str]]:
    """Find a provider that starts streaming. Returns (target, server-sent-event generator)."""
    import litellm

    alias = body.model
    targets = _targets(body)
    check_policies(key)

    for target in targets:
        try:
            stream = await litellm.acompletion(
                **_call_kwargs(target, body), stream=True, stream_options={"include_usage": True}
            )
            iterator = stream.__aiter__()
            first = await iterator.__anext__()
        except StopAsyncIteration:
            _record_failure(key, alias, target, RuntimeError("empty stream"))
            continue
        except Exception as e:  # noqa: BLE001 - any failure before the first chunk triggers fallback
            _record_failure(key, alias, target, e)
            continue
        return target, _relay(key, body, target, first, iterator)

    raise HTTPException(502, f"all providers failed for alias {alias!r}")


async def _relay(key: ApiKey, body: ChatCompletionRequest, target: RouteTarget, first, iterator) -> AsyncIterator[str]:
    alias = body.model
    served_as = f"{target.provider}/{target.model}"
    text_parts: list[str] = []
    usage: dict | None = None
    error: str | None = None

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
            yield f"data: {json.dumps({'error': {'message': 'The provider stopped responding mid-stream.'}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        # Runs even if the client disconnects, so partial streams are still billed.
        pt, ct = _stream_tokens(target, body, usage, "".join(text_parts))
        cost = dollars_for(target.provider, target.model, pt, ct)
        record_call(key.key, key.team, alias, target.provider, target.model, pt, ct, cost, error=error)
        metrics.observe(alias, target.provider, "error" if error else "ok", cost_usd=cost)


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
