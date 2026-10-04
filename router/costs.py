# Corey Mathie, 2026
"""
Per-call cost from LiteLLM's model price catalog.

Local models (Ollama) cost $0. If a hosted model isn't in the catalog, the call
is recorded at $0 and a warning is logged once per model, because unpriced calls
don't count toward spend caps. `unpriced_models()` feeds /health so it's visible.
"""

from __future__ import annotations

import logging

log = logging.getLogger("router.costs")

_LOCAL_PROVIDERS = {"ollama"}
_unpriced: set[str] = set()


def dollars_for(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if provider in _LOCAL_PROVIDERS:
        return 0.0
    qualified = f"{provider}/{model}"
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
    if provider in _LOCAL_PROVIDERS:
        return True
    try:
        from litellm import cost_per_token

        cost_per_token(model=f"{provider}/{model}", prompt_tokens=1, completion_tokens=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def unpriced_models() -> list[str]:
    return sorted(_unpriced)
