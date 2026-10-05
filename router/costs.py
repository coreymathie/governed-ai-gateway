# Corey Mathie, 2026
"""
Per-call cost from LiteLLM's model price catalog.

Local models (Ollama) cost $0. If a hosted model isn't in the catalog, the call
is recorded at $0 and a warning is logged once per model, because unpriced calls
don't count toward spend caps. `unpriced_models()` feeds /health so it's visible.

Price overrides (`prices:` in routes.yaml, USD per 1M tokens) take precedence
over the catalog and over the $0 local default. Use them for negotiated rates,
or to charge back self-hosted GPU capacity. LiteLLM is imported lazily, so with
overrides only (as in the browser demo) this module needs no dependencies.
"""

from __future__ import annotations

import logging

log = logging.getLogger("router.costs")

_LOCAL_PROVIDERS = {"ollama"}
_unpriced: set[str] = set()
_overrides: dict[str, tuple[float, float]] = {}  # "provider/model" -> (input, output) USD per 1M tokens


def set_price_overrides(prices: dict[str, tuple[float, float]]) -> None:
    """Replace all overrides. Keys are "provider/model"; values (input, output) in USD per 1M tokens."""
    _overrides.clear()
    _overrides.update({k: (float(v[0]), float(v[1])) for k, v in prices.items()})


def price_overrides() -> dict[str, tuple[float, float]]:
    return dict(_overrides)


def dollars_for(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    qualified = f"{provider}/{model}"
    if qualified in _overrides:
        per_in, per_out = _overrides[qualified]
        return round((prompt_tokens * per_in + completion_tokens * per_out) / 1_000_000, 6)
    if provider in _LOCAL_PROVIDERS:
        return 0.0
    try:
        from litellm import cost_per_token

        in_cost, out_cost = cost_per_token(
            model=qualified, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        return round(float(in_cost) + float(out_cost), 6)
    except Exception:  # noqa: BLE001 - LiteLLM raises several types for unknown models
        if qualified not in _unpriced:
            _unpriced.add(qualified)
            log.warning("no price for %s; calls are recorded at $0 and won't count toward spend caps", qualified)
        return 0.0


def is_priced(provider: str, model: str) -> bool:
    if f"{provider}/{model}" in _overrides or provider in _LOCAL_PROVIDERS:
        return True
    try:
        from litellm import cost_per_token

        cost_per_token(model=f"{provider}/{model}", prompt_tokens=1, completion_tokens=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def unpriced_models() -> list[str]:
    return sorted(_unpriced)
