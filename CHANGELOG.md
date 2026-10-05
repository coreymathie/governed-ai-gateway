# Changelog

## [0.7.0] — 2026-10

Renamed: the project is now **governed-ai-gateway** ("Governed AI Gateway"); repository, badges and Pages URLs changed. Python package and import paths (`router`) are unchanged.

Added
- **Console** (`demo/`): a product console with Overview, Playground (chat, route comparison, provider fault injection, semantic cache), Traces (filterable list and decision-timeline drawer, deep-linkable), Policies (edit, validate, preview decisions, apply), Budgets & Keys (caps, keys, pause/unpause, anomaly override, showback CSV), MCP tools, Evals and Settings, with a guided tour. One codebase, two modes: **demo** (the gateway's `router/*.py` in Pyodide with simulated providers, GitHub Pages) and **live** (served by the gateway at `/console/`, auto-detected through `/console/api-mode`, header badge shows which). Replaces the single-page demo at the same URL.
- **Request traces** (`router/traces.py`): every chat request records each stage (auth, policy/OPA, budgets, anomaly, request hooks, exact and semantic cache, TPM, every fallback attempt, settle, response hooks, content log) with decision, summary and wall time; `x-router-trace-id` header; `GET /admin/traces`, `GET /admin/traces/{id}`; bounded per-worker buffer (`ROUTER_TRACE_BUFFER`); metadata only.
- **Simulated providers** (`router/mock_provider.py`, `ROUTER_MOCK_PROVIDERS=true`): OpenAI-shaped responses and streams answered in-process, with per-provider outage, 429, error-rate and latency knobs (`GET/PUT /admin/mock/providers`), plus simulated MCP servers at `/mock/mcp/{server}` (`config/mcp.mock.yaml`).
- **`docker compose up`** (`Dockerfile`, `docker-compose.yml`): gateway + console with simulated providers and no API keys; admin key from `ROUTER_ADMIN_KEY` or generated once into `ROUTER_ADMIN_KEY_FILE` (mode 0600, never logged).
- **Config from the API** (`router/configcheck.py`, `router/console.py`): `GET /admin/config`, `GET /admin/config/{name}`, `POST /admin/config/{name}/validate` (the gateway's own loaders; errors with line numbers), `PUT /admin/config/{name}` (off unless `ROUTER_ALLOW_CONFIG_WRITES=true`; atomic write, reload, previous file restored on failure; audited), `POST /admin/policies/dry-run` (team × alias decisions for the active or a candidate policy).
- **Budget overrides** stored in SQLite and winning over `routes.yaml` field by field: `PUT/DELETE /admin/budgets/{org|team|key}/{name}`; listed in `GET /admin/budgets`; audited.
- **Key holds**: `POST /admin/keys/{id}/pause` (429 until unpaused), `POST /admin/keys/{id}/unpause` (lifts a pause; on an anomaly-paused key, an override until the end of the UTC hour), `GET /admin/key-holds`; audited.
- `GET /admin/overview` (today's spend by team and model, hourly spend vs. the 7-day average per hour, cache, anomalies, breakers, trace stats), `GET /admin/routes`, `POST /admin/circuits/reset`, public `GET /console/api-mode`.
- `scripts/build_console_data.py` writes the Evals screen's data (`demo/data/*.json`) from the repo's eval code; `tests/test_console_data.py` fails when it is stale.
- `scripts/demo_smoke.py` rewritten: every console screen in demo mode and live mode (uvicorn on a free port), desktop and 390 px screenshots with `--screenshots`; CI runs the live smoke (and the demo smoke, allowed to fail on CDN problems).
- Tests: `test_console_api.py`, `test_demo_console.py`, `test_console_data.py`.

Changed
- `router/__init__.py` defines `__version__`; the FastAPI title is `governed-ai-gateway`; the OpenTelemetry tracer name is `governed-ai-gateway`.
- The classic `/dashboard` links to the console.

## [0.6.0] — 2026-10

Added
- **PII redaction and content hooks** (`router/pii.py`, `router/privacy.py`): `pre_request` hooks over message contents before any provider call and `post_response` hooks over completions, configured per route and team under `privacy:` (layers union). Built-ins `pii_redact`, `pii_block` (422), `pii_detect`; detectors for emails, phone numbers, SSNs, Luhn-valid card numbers and API-key-like secrets. Custom hooks via `register_hook()` in modules listed in `ROUTER_HOOK_MODULES`. A failing hook fails the request closed (503). Headers `x-router-content-hooks`, `x-router-redactions` (counts only).
- **Content logging policy**: off by default; a team opts in with `privacy.teams.<team>.log_content: true`, and stored prompts/completions are redacted with every detector and truncated. `GET /admin/content-log`.
- **Audit trail** (`router/audit.py`): append-only `audit_log` table (UPDATE/DELETE refused by triggers), hash-chained, with `GET /admin/audit` and `GET /admin/audit/verify`. Records content-hook actions, content-log writes, and admin key creation/revocation and reloads. `router_policy_actions_total` metric. Dashboard card with chain status.
- **Policy-as-code** (`config/policies.yaml`, `router/policy.py`, `router/admission.py`): schema-validated, evaluated before budgets/cache/providers. Per default/team/route: alias allow/deny lists, provider allow-list (data residency), model allow/deny globs, `max_tokens` ceiling (reject or clamp; unset requests get the ceiling), `max_request_bytes`, `max_messages`, required content hooks. Most restrictive layer wins. Deployments a rule removes are never tried (no fallback to an excluded provider). Decisions in `x-router-policy*` headers and the audit trail; `GET /admin/policies`.
- **Optional OPA** (`router/opa.py`, `OPA_URL`): metadata-only input, can narrow but never widen the route, fails closed (503) on timeout/error/undefined/malformed results. Example `policies/router.rego` with `opa test` cases.
- **Route evaluation harness** (`router/evals.py`, `scripts/eval_routes.py`): replays a JSONL case set (40 fictional cases bundled in `evals/sample_cases.jsonl`) through routes with the gateway's fallback chain and pricing; exact/contains/regex scoring plus an optional judge hook; cost-vs-quality report in Markdown and JSON. Deterministic simulated provider by default (`evals/sim_profiles.json`, invented profiles); real providers only when every key is present.
- **CI gate** `scripts/eval_gate.py`: exit 1 when quality drops or cost rises beyond limits between two routes files or two reports.
- **Shadow mode** (`shadow:` in routes.yaml, `router/shadow.py`): background mirror of a sampled share of an alias's traffic to a candidate alias; never returned; policy-admitted; separate breakers; separate `shadow_usage` ledger or `billing: suppress`; `max_daily_usd`; `GET /admin/shadow` agreement/cost/latency comparison; `router_shadow_requests_total`.
- **Semantic cache** (`router/semcache.py`, `semantic_cache:` in routes.yaml), **off by default**: offline hashing embedder or a provider embedding model; number and negation guards; partitions by team, alias, policy decision, content hooks, earlier messages and parameters; per-team opt-in and thresholds; TTL and LRU; `x-router-cache: semantic-hit`, `x-router-semantic-*` headers, `router_semantic_cache_total`, `GET /admin/semantic-cache`.
- **Calibration** `scripts/calibrate_semcache.py` on 160 bundled fictional pairs: threshold picked on one half (0.88; 0/13 wrong hits), measured on the other: hit rate 26.3%, false-hit rate 9.1% (1 of 11 hits), which misses the 1% target; 28.6% without guards. ADR 0005.
- **MCP tool gateway** (`POST /mcp/{server}`, `router/mcp_gateway.py`, `router/mcp_policy.py`, `config/mcp.example.yaml`): JSON-RPC proxy for `initialize`, `ping`, `notifications/*`, `tools/list` (filtered) and `tools/call` over Streamable HTTP (JSON or SSE replies); default-deny per-team tool allow-lists; per-key and per-team velocity and an identical-call loop guard; daily count and spend caps from the usage table; PII-redacted arguments in the audit log (optionally upstream); allowed calls recorded in usage/showback (`provider: mcp`); fail closed on upstream errors or missing credentials; `GET /admin/mcp`; `router_mcp_tool_calls_total`. ADR 0006 documents the scope.
- **Demo**: semantic-cache panel (hits, number guard, team isolation, the known rotate/revoke false hit, in-browser calibration) and MCP panel (allowed, denied, velocity-limited tool calls with redacted arguments).
- **Request body limit** `ROUTER_MAX_BODY_BYTES` (default 1 MiB): 413 before parsing, chunked bodies included.
- `GET /admin/audit?exclude_allow=true` hides routine policy allows (the dashboard uses it).
- **Demo**: the governance panel adds the policy stage (a regulated team refused on a public-only route, public providers removed on a mixed route, alias allow-list, max_tokens clamp), using `router/policy.py` with the same rules as `config/policies.yaml`.
- **Demo**: governance panel showing what a provider receives with redaction off / detect / redact / block, the audit trail and the redacted content log, using `router/pii.py` in Pyodide.
- **App keys hashed at rest** (`router/store.py`): rows keep an id, a display prefix, a fingerprint, a salt and HMAC-SHA256(`ROUTER_KEY_PEPPER`, salt + key); the key is returned once at creation. Lookups compare in constant time. Usage rows reference the key id.
- `scripts/demo_smoke.py`: headless Chromium check of every demo panel (`--pyodide-dir` serves a local Pyodide copy).

Behaviour changes (check before upgrading)
- **`config/policies.yaml` is loaded by default when present.** The shipped file sets a 4096 `max_tokens` ceiling (clamp), so requests without `max_tokens` are now sent with 4096 and TPM reservations use it; `heavy-reasoning` rejects over-ceiling requests with 400; team `regulated` is local-only; team `contractors` has alias and model lists. Point `ROUTER_POLICIES_FILE` at an empty file to opt out.
- **Bodies over 1 MiB get 413** (`ROUTER_MAX_BODY_BYTES`, 0 disables).
- **An invalid policies file, or a `ROUTER_POLICIES_FILE` that doesn't exist, stops startup.** `POST /admin/reload` now reloads routes and policies together.
- Every request that reaches policy evaluation writes an audit row.
- **Keys are migrated on startup**: 0.5.x databases have their plaintext keys hashed and usage rows re-pointed to key ids. `GET /admin/keys` no longer returns secrets; `DELETE /admin/keys/{id}` accepts a key id (or the key itself) and returns 404 for unknown keys. Back up the database before upgrading.

Changed
- The response-cache key includes a fingerprint of the policy decision and the content hooks in force, so existing cache entries miss once after upgrading.
- `stream: true` returns 400 when a `post_response` hook applies.
- CI lints the whole repository (`ruff check .`).

## [0.5.0] — 2026-10

Added
- **Circuit breakers per provider deployment** (`router/breaker.py`): closed → open on consecutive failures, error rate in a sliding window, or a 429/503 `Retry-After`; half-open with limited probes after a cooldown; closed on probe success. Only provider-health errors count (connection, timeout, 401/403/408/429, 5xx). Open deployments are skipped and fall through; if every target is open the gateway returns 503 + `Retry-After` without calling a provider.
- **Latency-aware routing** (`strategy: latency` per alias, `router/latency.py`): EWMA of observed latency (TTFT for streams), configured order breaks near-ties, bounded warm-up for unmeasured deployments. Default ordering is unchanged.
- **Provider-independent fallback chain** (`router/fallback.py`) shared by the gateway and the browser demo.
- **Tokens-per-minute limits** per key and per team (`router/tokens.py`): estimate reserved before the call (tiktoken `cl100k_base` when available, else a documented chars/4 heuristic), corrected to provider-reported usage after, released on failure. 429 + `Retry-After`.
- **Budget hierarchy** org → team → key, daily and monthly (`router/budgets.py`, `budgets:` in routes.yaml), with per-team and per-key-label overrides and config warnings when a child cap exceeds the org cap.
- **Showback / chargeback export**: `GET /admin/showback` (JSON or CSV, admin-only) grouped by team, key, alias, provider and/or model, with cost per 1K requests, cost per 1K tokens and share of cost. Keys appear as label + fingerprint. CSV cells are escaped against formula injection.
- **OpenTelemetry GenAI spans** per provider attempt (`router/telemetry.py`), a no-op without OpenTelemetry.
- **Prometheus metrics**: `router_tokens_total`, `router_rejections_total`, `router_circuit_state`, `router_circuit_transitions_total`, `router_provider_latency_ewma_seconds`, `router_tpm_in_use`; `skipped` outcome on `router_requests_total`.
- **Admin endpoints** `/admin/circuits` and `/admin/budgets`; dashboard panels for breakers, budgets and a showback CSV download.
- **Response headers** `x-router-strategy`, `x-router-attempts`, `x-router-fallback-from`, `x-router-circuit-skipped` (also on 502/503).
- **Price overrides** (`prices:`, USD per 1M tokens) that take precedence over LiteLLM's catalog, including for local models.
- **Browser demo** (`demo/`): the repo's breaker, fallback, latency, token, budget, anomaly, cost and showback modules running in Pyodide against simulated providers.
- **Docs**: ADRs 0001–0004, `docs/threat-model.md` (STRIDE + OWASP LLM Top 10 2025), `docs/controls.md` (NIST AI RMF / AI 600-1, FinOps for AI).
- 85 new tests (111 total).

Changed
- `anomaly.py` exposes pure `classify()` / `baseline_from_hourly()` and imports the store lazily; thresholds and behaviour are unchanged.
- The routes file accepts `alias: {strategy, targets}` alongside the existing list form.
- The dashboard's dark palette.
- CI lints `demo/` too; `requirements.txt` adds opentelemetry-api/sdk so span tests run in CI.

## [0.4.0] — 2026-10

Added
- **Streaming** (`stream: true`), relayed as server-sent events in the OpenAI chunk format. Fallback happens before the first byte; a mid-stream provider failure sends an error event. Usage is recorded when the stream ends, even if the client disconnects, and token counts are computed locally when a provider doesn't report them, so spend caps still apply.
- **Response cache** for deterministic requests: per-team, TTL-based, exact match. Hits cost $0, return `x-router-cache: hit`, and record the dollars saved. Dashboard tiles for savings and hit rate.
- **Prometheus metrics** at `/metrics`.
- Schema migration: existing v0.3 databases gain the new `cached` and `saved_usd` columns automatically.
- 8 new tests (26 total), including streaming with LiteLLM's real chunk objects.


## [0.3.0] — 2026-10

Added
- **Auto-pause** (`policies.auto_pause_on_anomaly`): a key whose spend this hour reaches 10x its 7-day baseline gets a 429 until an admin steps in.
- **Dashboard anomalies card** and a "keys flagged now" counter. `/admin/anomalies` now returns key labels, teams, and history, sorted worst first.
- **Pricing check**: models missing from LiteLLM's price catalog are logged at startup and listed under `/health`, since unpriced calls can't count toward spend caps.
- `/v1/models` lists aliases for OpenAI-client discovery.
- Test suite (18 tests, offline): fallback, clean 502s, spend caps, anomaly thresholds, auto-pause, time windows, config validation and safe reload, admin auth. CI runs ruff, format check, and pytest.

Fixed
- **Time windows were wrong.** Timestamps were stored as ISO strings with a `T` and compared as text against SQLite's space-separated format, so "this hour" and "last 60 minutes" included the whole day. Timestamps are now stored in SQLite's format and compared through `datetime()`; a regression test covers it.
- Unknown providers in `routes.yaml` now fail at load instead of during a request. A bad `POST /admin/reload` returns 400 and keeps the previous config.
- `stream: true` returned a non-streamed response; it now returns a clear 400 until streaming is implemented.
- 502 responses no longer include upstream error text.
- Dashboard "calls today" counted at most 50; it now uses the real count. Labels are HTML-escaped; the admin key lives in session storage.
- Removed a leftover string-replace hack in the SQL schema; anomaly code uses the store's public `connect()`.
- Admin env key is compared in constant time. FastAPI startup uses `lifespan`; dependencies use `Annotated`.


## [0.2.0] — 2026-10

Added — anomaly detection and a regulated-workload routing profile.

- `router/anomaly.py` — per-key spend-velocity anomaly detection against a
  rolling 7-day hourly baseline. Three verdicts: `ok` / `flagged` (3x) /
  `pause` (10x). Pattern applied from dispute/fraud ops work.
- `GET /admin/anomalies` — returns the current signal for every active key.
  Dashboards and alerting hooks can poll it.
- `config/regulated.yaml` — ships a drop-in routes file for HIPAA / PCI /
  SOC2-adjacent workloads. On-prem-only aliases with no public-provider
  fallback.
- Author lines added to every source file.

## [0.1.0] — 2026-10

Initial release.

- OpenAI-compatible gateway in front of OpenAI / Anthropic / Gemini / Ollama
- Alias-based routing with ordered fallback on error, timeout, 429
- Hard per-key and per-team daily spend caps enforced before provider call
- Live dashboard with per-key / per-team spend and provider error rates
- `x-router-used-provider` and `x-router-used-model` response headers
- Hot reload of `routes.yaml` without restart (`POST /admin/reload`)
