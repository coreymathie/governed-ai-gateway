# ADR 0001: Use LiteLLM for provider adapters; own the control plane

- Status: accepted
- Date: 2026-10

## Context

The gateway exposes one OpenAI-compatible endpoint over OpenAI, Anthropic, Gemini and a local Ollama. Each provider has its own request schema, streaming format, error types, usage reporting and price list. There are two ways to handle that:

1. Write and maintain an adapter per provider (request mapping, SSE parsing, error mapping, usage extraction, pricing).
2. Use a maintained adapter library and spend the effort on what sits around the call: routing policy, spend controls, resilience and observability.

The value of this project is in the second layer: deciding *whether* a call may happen, *where* it goes, *what it cost* and *who pays*. Provider wire formats change often. Keeping four adapters current is undifferentiated work.

## Decision

Provider calls go through `litellm.acompletion` (streaming and non-streaming), `litellm.token_counter` (stream usage fallback) and `litellm.cost_per_token` (price catalog). Everything else is owned here and kept free of LiteLLM types:

| Concern | Module | Depends on LiteLLM? |
|---|---|---|
| Fallback chain, breaker accounting | `router/fallback.py`, `router/breaker.py` | No |
| Latency ordering | `router/latency.py` | No |
| Budgets, TPM limits, token estimate | `router/budgets.py`, `router/tokens.py` | No (tiktoken optional) |
| Anomaly thresholds | `router/anomaly.py` | No |
| Showback | `router/showback.py` | No |
| Cost per call | `router/costs.py` | Only for catalog prices; overrides need nothing |
| Provider call + spans | `router/routing.py`, `router/telemetry.py` | Yes (the call itself) |

The browser demo enforces that boundary: it loads the LiteLLM-free modules into Pyodide and runs them unchanged. `tests/test_demo_engine.py` fails if one of them gains a third-party import at module level.

## Consequences

- Adding a provider is a one-line change to the `Provider` literal plus credentials, not a new adapter.
- LiteLLM is a supply-chain dependency on the hot path (see [threat model](../threat-model.md), LLM03). `requirements.txt` sets a floor (`litellm>=1.50.0`), not a pin. Production deployments should pin and review upgrades.
- Error classification for breakers reads `status_code` and the `Retry-After` header from LiteLLM's exceptions (which subclass the OpenAI SDK's). If LiteLLM changes those attributes, unknown errors default to "counts as a provider failure", which fails safe toward skipping a provider rather than hammering it.
- The catalog can lag new models. Unpriced models are logged at startup, listed under `/health`, and can be priced explicitly with `prices:` overrides.

## Alternatives considered

- **Hand-written adapters.** Full control, no transitive dependencies, but a large maintenance surface for no product differentiation.
- **LiteLLM's own proxy server.** It does more (many providers, its own budgets and keys). This repo stays a small, readable control plane where every policy decision is in code you can review and test offline; the README points to LiteLLM's proxy for teams that outgrow it.
- **A provider SDK per vendor.** Same maintenance problem as hand-written adapters, plus inconsistent error types.
