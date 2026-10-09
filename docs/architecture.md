# Architecture

This document is the reference architecture of the gateway: the request pipeline and its enforcement points, the module that owns each responsibility, and the design choices behind them. Each design choice links to the ADR that records its trade-offs. Configuration is in [configuration](configuration.md) and run-time behaviour in [operations](operations.md).

## Request pipeline

Stages 0 to 5 run before any provider is called; each of them except the cache is an enforcement point that can refuse the request.

```
   ┌────────────┐   POST /v1/chat/completions   ┌──────────────────────────────────────────┐
   │ Client app │──────────────────────────────▶│  gateway (FastAPI, :4000)                 │
   └────────────┘   Authorization: Bearer key   │                                          │
                                                │  0. body size limit (413)      main.py   │
                                                │  1. auth + requests/min        auth.py   │
                                                │  1b. policy-as-code (+OPA)     policy.py │
                                                │  2. budgets org→team→key       budgets.py│
                                                │  3. anomaly auto-pause         anomaly.py│
                                                │  3b. content hooks (PII)  pii.py/privacy │
                                                │  4. exact-match cache (team)   routing.py│
                                                │  5. reserve tokens (TPM)       tokens.py │
                                                │  6. fallback chain             fallback.py
                                                │       order: configured | latency.py     │
                                                │       skip open circuits     breaker.py  │
                                                │  7. settle tokens, price,      costs.py  │
                                                │     record, post hooks, cache, store.py  │
                                                │     span, opt-in content log  audit.py   │
                                                └──────┬───────────────────────────────────┘
                                                       │ LiteLLM
       ┌───────────────┬───────────────┬───────────────┼───────────────┐
       ▼               ▼               ▼               ▼
   ┌────────┐     ┌──────────┐    ┌─────────┐     ┌─────────┐
   │ OpenAI │     │ Anthropic│    │  Gemini │     │ Ollama  │
   └────────┘     └──────────┘    └─────────┘     └─────────┘

  Refused before any provider call:  400/403/413 policy · 402 budget · 429 TPM / RPM / anomaly pause · 422 PII block
                                     503 policy or hook failure (fail closed) / all circuits open
  Provider failure (error / timeout / 429 / 5xx)  →  next target; breaker counts it
```

## Modules

| Module | Responsibility | Third-party imports |
|---|---|---|
| `main.py` | HTTP API, admin endpoints, console mount (`/console/`), dashboard | FastAPI |
| `routing.py` | Request pipeline above; streaming relay; response headers | FastAPI, LiteLLM (lazy) |
| `fallback.py` | `run_chain`: walk targets, skip open breakers, record attempts | none |
| `breaker.py` | Circuit breaker state machine, error classification, Retry-After parsing | none |
| `latency.py` | EWMA latency/TTFT per deployment; `strategy: latency` ordering | none |
| `tokens.py` | Token estimate (tiktoken or heuristic); TPM sliding windows | none (tiktoken optional) |
| `budgets.py` | Org → team → key caps, daily and monthly | none |
| `anomaly.py` | Spend this hour vs. the key's 7-day baseline | none at import (SQLite queries import the store lazily) |
| `costs.py` | Cost per call: price overrides, then LiteLLM's catalog | LiteLLM (lazy) |
| `showback.py` | Roll-ups and unit metrics; CSV export | none |
| `telemetry.py` | OpenTelemetry GenAI spans; no-op without OpenTelemetry | opentelemetry-api (optional) |
| `metrics.py` | Prometheus text exposition | none |
| `store.py` | SQLite: keys, usage, cache, audit and content-log tables | none (stdlib sqlite3) |
| `pii.py` | PII/secret detectors, redaction, hook registry, `run_hooks` | none |
| `privacy.py` | Runs hooks on requests/responses, audits them, fails closed, writes the opt-in redacted content log | FastAPI (HTTPException) |
| `audit.py` | Append-only, hash-chained audit trail; `verify()` | none (stdlib sqlite3 via store) |
| `policy.py` | Policy schema validation, layer combination, `evaluate()`, OPA result handling | none |
| `admission.py` | Runs the policy (and OPA) per request, fails closed, audits, sets `x-router-policy*` headers | FastAPI |
| `opa.py` | `POST /v1/data/<path>` to an OPA sidecar | httpx |
| `shadow.py` | Background mirror to a candidate alias: policy, own breakers, separate ledger, agreement metrics | none beyond the gateway modules |
| `semcache.py` | Hashing embedder, guards, partitioned TTL/LRU semantic cache, calibration and threshold pick | none |
| `mcp_policy.py` | MCP config schema, tool allow-lists, velocity limiter, caps, argument redaction | none |
| `mcp_gateway.py` | `POST /mcp/{server}`: JSON-RPC proxy (JSON or SSE replies), filtering, decisions, audit, usage | httpx, FastAPI |
| `evals.py` | Eval cases, scoring, simulated provider, replay through `run_chain`, summaries, gate, Markdown report | none |
| `evalrun.py` | CLI glue: load routes, sim or real (LiteLLM) provider, key checks | PyYAML, pydantic; LiteLLM for real runs |
| `traces.py` | Per-request decision traces (stage, decision, summary, wall time) in a bounded per-worker buffer | none |
| `configcheck.py` | Validate policies/routes/MCP YAML with the gateway's own loaders; policy decision matrices | none at import (PyYAML, pydantic lazily) |
| `console.py` | Live console back end: config read/validate/apply (atomic, reload, restore on failure), dry runs, overview | PyYAML (via configcheck) |
| `mock_provider.py` | Simulated OpenAI-compatible providers and MCP servers for `ROUTER_MOCK_PROVIDERS=true`, with runtime fault knobs | none |

The modules marked "none" (plus `configcheck.py`, and `models.py` on demand with Pyodide's pydantic) are what the [console's demo mode](../demo/) loads into Pyodide unchanged; `tests/test_demo_engine.py` fails if one of them gains a third-party import at module level.

## Design choices

**Aliases with ordered targets.** A readable config file states "try X first, then Y, then Z". Operators diff it in a pull request, review it and roll it back. `strategy: latency` is opt-in per alias. See [ADR 0002](adr/0002-ordered-fallback-and-circuit-breakers.md).

**Circuit breakers per deployment.** A provider that keeps failing, or that returns 429/503 with `Retry-After`, is skipped until a cooldown passes and a probe succeeds, so an outage stops adding its timeout to every request. If every target is open, the gateway answers 503 with `Retry-After` without calling any provider.

**Admission control before the provider call.** Budgets (org → team → key, daily and monthly), TPM reservations and anomaly pauses are all decided before any money is spent. Caps compare against spend already recorded, so concurrent in-flight requests can overshoot a cap by their own cost; TPM reservations are estimates, corrected to provider-reported usage when the call returns.

**Spend anomalies, fraud-style.** Each key's spend this hour is compared with its own 7-day baseline: flagged at 3x, paused at 10x when `auto_pause_on_anomaly` is on. A key with fewer than 24 active hours of history is not judged. The pattern is the velocity check used in card-fraud monitoring. See [ADR 0003](adr/0003-per-key-baseline-anomaly-model.md).

**Policy before everything else.** `config/policies.yaml` is evaluated right after authentication: it can refuse the request (alias lists, size, max_tokens) or remove deployments from the route (provider and model lists), and the rest of the pipeline only ever sees the permitted deployments, so fallback cannot reach an excluded provider. An optional OPA check runs after the local policy and can only narrow it. Every failure mode of the policy layer denies. The cache key includes a fingerprint of the decision, so answers never cross policies.

**Tools get the same treatment as models.** `POST /mcp/{server}` authenticates the caller's gateway key, applies the team's tool allow-list, velocity windows and daily caps from `config/mcp.yaml`, forwards one JSON-RPC message upstream, and records the call in usage and the audit trail. Anything it cannot decide or complete is an error to the client, never a partial result. See [ADR 0006](adr/0006-mcp-tool-gateway-scope.md).

**Measure before switching.** `scripts/eval_routes.py` replays a case set through candidate routes with the same fallback chain and pricing the gateway uses, and `scripts/eval_gate.py` turns a quality drop or cost rise into a failing exit code. Shadow mode then mirrors a sample of live traffic to the candidate after the client has its answer, through the candidate's own policy decision, with separate breakers and a separate ledger, so the comparison never changes live behaviour or live budgets.

**Content hooks, then cache.** `pre_request` hooks run before the cache lookup, so the cache key is computed on what the provider would actually receive, and it includes a fingerprint of the hooks in force. `post_response` hooks run before the answer is cached, returned or logged. Content logging is off unless a team opts in, and what it stores is redacted with every detector. Every hook action is audited with counts only.

**Exact-match cache, per team.** Only deterministic requests, only within a team, with a TTL. See [ADR 0004](adr/0004-exact-match-cache-now-semantic-later.md).

**Semantic cache, off by default.** After an exact miss, the last user turn is embedded and compared with entries in the same partition (team, alias, policy decision, content hooks, earlier messages, parameters, embedding space). A hit needs the threshold and the number/negation guards. Its calibration is published as measured on held-out pairs, including the miss against a 1% target. See [ADR 0005](adr/0005-semantic-cache-off-by-default-calibrated.md).

**LiteLLM for wire formats.** Provider adapters are LiteLLM's job; the control plane is this repo's. See [ADR 0001](adr/0001-litellm-for-provider-adapters.md).

**Routing headers.** Successful responses carry `x-router-used-provider`, `x-router-used-model` and `x-router-cache`; responses that went through the provider chain (including 502/503 errors) also carry `x-router-strategy`, `x-router-attempts`, and, when relevant, `x-router-fallback-from` and `x-router-circuit-skipped`.

**Reload on request.** `POST /admin/reload` re-reads the routes and policy files in the worker that receives it; an invalid file is rejected and the previous config stays active. It does not watch the file, and with several workers each one must be reloaded (or restarted).

**SQLite by default.** Small and self-contained: the store is one module behind plain functions, so moving to Postgres means rewriting that file. No throughput figures are claimed for it. SQLite serializes writes, so heavy concurrent traffic needs a server database.

**Process-local state.** Rate limits, TPM windows, breaker state and latency averages live in each worker's memory. Multiple workers or instances each enforce their own limits; a shared store is on the roadmap.

**Decision traces and the audit trail are separate records.** Each request's stages (decision, summary, wall time) are held as a decision trace in a bounded per-worker buffer for explanation and debugging (`router/traces.py`). Policy decisions, content-hook actions, MCP tool-call decisions and administrative changes go to the audit trail, an append-only, hash-chained SQLite table that is the durable evidence (`router/audit.py`).

## Observability

Operational detail, including the full admin API and failure modes, is in [operations](operations.md).

- **Spans:** one OpenTelemetry CLIENT span per provider attempt, named `chat {model}`, with `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `error.type`, plus `router.cost_usd`, `router.alias`, `router.attempt`, `router.strategy`. Nothing is exported unless the host configures an OpenTelemetry SDK/exporter. Message content is never recorded.
- **Metrics** (`/metrics`, admin key): `router_requests_total{alias,provider,outcome}` (ok, error, skipped, cache_hit), `router_spend_usd_total`, `router_cache_saved_usd_total`, `router_tokens_total{direction}`, `router_rejections_total{reason}`, `router_circuit_state`, `router_circuit_transitions_total`, `router_provider_latency_ewma_seconds`, `router_tpm_in_use`.
- **Admin API:** `/admin/circuits`, `/admin/budgets`, `/admin/showback`, `/admin/anomalies`, `/admin/usage/today`, `/admin/recent`, `/admin/policies`, `/admin/shadow`, `/admin/semantic-cache`, `/admin/mcp`, `/admin/audit`, `/admin/audit/verify`, `/admin/content-log`.
- **Audit trail:** `router_policy_actions_total{category,action}` mirrors what `audit_log` records.

## Further reading

- [Threat model](threat-model.md): STRIDE, OWASP LLM Top 10 (2025)
- [Controls mapping](controls.md): NIST AI RMF / AI 600-1, FinOps for AI
- [Configuration](configuration.md), [operations](operations.md), [evaluation](evaluation.md), [console](console.md)
