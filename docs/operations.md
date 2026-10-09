# Operations

This document covers how the gateway is deployed, observed and operated: deployment options, environment settings, telemetry, the admin API, and the gateway's behaviour in each failure mode with the test that provides evidence for it. Control configuration is in [configuration](configuration.md); the module map is in [architecture](architecture.md).

## Deployment options

| Option | Command | Providers | Use |
|---|---|---|---|
| Local process | `uvicorn router.main:app --port 4000` | Real, from keys in `.env` | Development, single-node deployment |
| Local process, simulated | `ROUTER_MOCK_PROVIDERS=true uvicorn router.main:app --port 4000` | **Simulated**, in-process | Evaluation without keys |
| Docker Compose | `docker compose up` → http://localhost:4000/console/ | **Simulated** by default (`ROUTER_MOCK_PROVIDERS=true`); set it to `false` and add provider keys for real traffic | Evaluation, console in live mode |
| Regulated profile | `ROUTER_ROUTES_FILE=./config/regulated.yaml` | On-prem Ollama only | Workloads that must not leave the institution's network |
| OPA sidecar | `OPA_URL=http://127.0.0.1:8181` with `opa run --server policies/` | Any | Central policy decision point in addition to the local policy file |
| Hosted console | GitHub Pages, branch `main`, folder `/` | **Simulated**, in the browser | Public demonstration ([console](console.md)) |

Compose publishes the gateway on `127.0.0.1:4000`, stores the database and a generated admin key in the `gateway-data` volume, enables config writes from the console (`ROUTER_ALLOW_CONFIG_WRITES=true`) and uses simulated MCP servers ([config/mcp.mock.yaml](../config/mcp.mock.yaml)). The admin key is `ROUTER_ADMIN_KEY` from `.env`, or one generated on first start: `docker compose exec gateway cat /data/admin-key.txt` (never logged).

The network in front of the gateway (TLS termination, WAF) is outside the gateway's scope and must be supplied by the deployment ([threat model](threat-model.md)).

### Scaling constraints

- Rate limits, TPM windows, breaker state, latency averages, semantic-cache entries and decision traces live in each worker's memory. Multiple workers or instances each enforce their own limits; a shared store (Redis) is on the roadmap.
- `POST /admin/reload` and `PUT /admin/config/{name}` reload only the worker that receives them.
- SQLite serializes writes. Every request that reaches policy evaluation writes one audit row plus the usage row. Heavy concurrent traffic needs a server database; the store is one module behind plain functions ([architecture](architecture.md#design-choices)).
- Budget overrides and key holds set from the console are in SQLite and shared, but each worker caches budget overrides until it changes them itself.

## Environment

From [.env.example](../.env.example):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_HOST` | Provider credentials and the on-prem Ollama endpoint |
| `ROUTER_HOST`, `ROUTER_PORT` | Bind address |
| `ROUTER_ROUTES_FILE` | Routes file (default `./config/routes.yaml`) |
| `ROUTER_DB_PATH` | SQLite database |
| `ROUTER_ADMIN_KEY`, `ROUTER_ADMIN_KEY_FILE` | Admin key; with the key empty, read from or generated into the file (mode 0600) on first start |
| `ROUTER_HOOK_MODULES` | Modules that register custom content hooks |
| `ROUTER_POLICIES_FILE` | Policy file; empty means `./config/policies.yaml` if it exists; a path that is set but missing or invalid stops startup (fail closed) |
| `OPA_URL` | Optional OPA sidecar; when set, OPA must also allow every request, and requests get 503 if it cannot be reached |
| `ROUTER_MAX_BODY_BYTES` | Largest request body accepted (default 1 MiB, 413 above it; 0 disables the check) |
| `ROUTER_MCP_FILE` | MCP tool gateway config; empty means `./config/mcp.yaml` if it exists |
| `ROUTER_KEY_PEPPER` | Optional pepper for hashing app keys at rest; changing it invalidates existing keys |
| `ROUTER_MOCK_PROVIDERS` | Answer every provider call in-process with simulated providers |
| `ROUTER_ALLOW_CONFIG_WRITES` | Allow admins to replace policy, routes and MCP files from the console (off by default) |
| `ROUTER_TRACE_BUFFER` | Size of the per-worker decision-trace buffer (default 500) |

## Observability

- **OpenTelemetry GenAI spans.** One CLIENT span per provider attempt, named `chat {model}`, with `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens`, `error.type`, plus `router.cost_usd`, `router.alias`, `router.attempt`, `router.strategy`. A no-op if OpenTelemetry is not installed; nothing is exported unless the host configures an exporter. Prompts and completions are never put on spans.
- **Prometheus** at `/metrics` (admin key): requests by outcome (ok, error, skipped, cache_hit), spend, tokens, cache savings, rejections by reason, breaker state and transitions, latency EWMA, TPM in use. Metric names are listed in [architecture](architecture.md#observability).
- **Decision traces.** Every chat request gets an `x-router-trace-id` header and a trace (`GET /admin/traces`, `GET /admin/traces/{id}`): each stage's decision, a one-line summary and its wall time. Metadata only (team, key label and fingerprint, alias, deployments, statuses, tokens, cost), never prompt or completion text; a bounded in-memory buffer per worker (`ROUTER_TRACE_BUFFER`, default 500). Traces explain a single request; they are not the durable record.
- **Audit trail.** The hash-chained `audit_log` table is the durable record of policy decisions, content-hook actions, MCP tool-call decisions and administrative changes (`GET /admin/audit`, `GET /admin/audit/verify`). `router_policy_actions_total{category,action}` counts what the audit trail records; `router_shadow_requests_total{alias,candidate,outcome}` counts mirrors; `router_mcp_tool_calls_total{server,tool,decision}` counts tool calls.
- **Console** at `/console/` ([console](console.md)) and the classic dashboard at `/dashboard`.

### Admin API

`/admin/circuits`, `/admin/circuits/reset`, `/admin/budgets`, `/admin/budgets/{scope}/{name}`, `/admin/showback`, `/admin/anomalies`, `/admin/usage/today`, `/admin/recent`, `/admin/keys`, `/admin/keys/{id}/pause`, `/admin/keys/{id}/unpause`, `/admin/key-holds`, `/admin/reload`, `/admin/routes`, `/admin/policies`, `/admin/policies/dry-run`, `/admin/config`, `/admin/config/{name}` (+ `/validate`), `/admin/overview`, `/admin/traces`, `/admin/shadow`, `/admin/semantic-cache`, `/admin/mcp`, `/admin/mock/providers`, `/admin/audit`, `/admin/audit/verify`, `/admin/content-log`.

Every `/admin/*` route requires `ROUTER_ADMIN_KEY` (constant-time compare) or an admin-flagged key. `GET /console/api-mode` is public.

## Failure modes

| Situation | Gateway behaviour | Evidence |
|---|---|---|
| Primary provider down | Falls back; after `failure_threshold` failures the breaker opens and later requests skip it | `test_breaker_opens_and_later_requests_skip_the_dead_provider` |
| Provider recovers | After cooldown, one probe; success closes the breaker | `test_half_open_probe_closes_the_breaker_when_provider_recovers` |
| Provider returns 429 + Retry-After | Breaker opens for that long (capped) | `test_provider_429_retry_after_is_honoured` |
| Every provider failing | 502 with a clean message (no upstream detail) | `test_all_providers_failing_returns_clean_502` |
| Every circuit open | 503 + `Retry-After`, no provider call | `test_all_circuits_open_returns_503_with_retry_after` |
| Caller sends a bad request (400) | Falls through, but breakers are not tripped | `test_bad_request_does_not_trip_breaker` |
| Provider dies mid-stream | Error event to the client, partial usage recorded | `test_mid_stream_failure_sends_error_event_and_records_it` |
| Client disconnects mid-stream | Tokens received so far recorded | `test_stream_closed_early_is_still_recorded` |
| Team bursts past its TPM | 429 + `Retry-After`; other teams unaffected | `test_team_tpm_limit_rejects_with_retry_after_and_settles_to_real_usage` |
| Org budget exhausted | 402 for every team | `test_org_daily_cap_blocks_every_team` |
| Runaway key | Flagged at 3x baseline, 429 at 10x if auto-pause is on | `test_auto_pause_blocks_runaway_key` |
| Invalid routes file on reload | 400, previous config kept | `test_bad_reload_keeps_previous_config` |
| Model missing from price catalog | Warning at startup, listed on `/health`; calls record $0 unless `prices:` overrides it | `router/costs.py` |
| Regulated team calls a route with only public providers | 403 before any provider call; audit `deny` | `test_regulated_team_blocked_from_external_provider` |
| Regulated team's only permitted (local) provider is down | 502; public providers in the same route are never tried | `test_regulated_team_routes_local_with_hooks_and_ceiling` |
| OPA unreachable, erroring or returning no decision | 503, no provider call (fail closed) | `test_opa_failures_fail_closed` |
| Policy engine raises | 503, `error` audit row | `test_policy_engine_crash_fails_closed` |
| Policy file invalid at startup or on reload | Startup fails; reload returns 400 and keeps the previous routes and policies | `test_reload_is_atomic_and_missing_explicit_file_fails` |
| Body over `ROUTER_MAX_BODY_BYTES` | 413 before the JSON is parsed | `test_body_size_middleware_refuses_before_parsing` |
| MCP tool outside the team's allow-list | JSON-RPC `-32001`; the server is never called; audit `deny` | `test_denied_tools_never_reach_the_server` |
| Agent repeats the same tool call in a loop | `-32002` + `Retry-After` after `max_identical_per_minute` | `test_velocity_identical_calls_and_daily_cap` |
| MCP server down or its credential missing | `-32004`, no partial result; audit `upstream_error` | `test_upstream_failures_and_missing_credentials_fail_closed` |
| Shadow candidate fails or times out | Live response unaffected; error row in `shadow_usage`; only the shadow breaker counts it | `test_shadow_failure_never_touches_the_live_path` |
| Shadow candidate not permitted for the caller's team | Not mirrored; `skipped_policy` audit row | `test_shadow_respects_the_candidates_policy` |
| Real eval run without provider keys | Refused with the list of missing credentials (exit 2) | `test_real_runs_refuse_without_credentials` |
| Prompt contains PII on a `pii_block` route/team | 422 before any provider call; audit row with counts | `test_team_block_refuses_before_any_provider_call` |
| A content hook raises | 503, nothing sent to a provider (fail closed); `hook_error` audit row | `test_failing_hook_fails_closed` |
| `post_response` hook blocks a completion | 422 to the client; the provider call is still recorded and billed | `test_post_response_redact_and_block` |
| `stream: true` where a `post_response` hook applies | 400 before any provider call (chunk-wise redaction can miss matches split across chunks) | `test_streaming_with_pre_hooks_and_refused_with_post_hooks` |
| Content policy changes while answers are cached | Cache key includes the hook-set fingerprint, so old entries are not served | `test_cache_entries_do_not_cross_a_content_policy_change` |

## Routine operations

| Task | How |
|---|---|
| Issue an application key | `POST /admin/keys` with a label and team; the secret is returned once |
| Export showback for the month | `GET /admin/showback?group_by=team,model&format=csv` |
| Respond to a spend anomaly | `GET /admin/anomalies`; pause with `POST /admin/keys/{id}/pause`, or lift an anomaly pause for the rest of the UTC hour with `/unpause` |
| Change a policy | Edit `config/policies.yaml` in a pull request, then `POST /admin/reload`; or validate and dry-run a candidate with `POST /admin/config/policies/validate` and `POST /admin/policies/dry-run` before applying |
| Verify the audit trail | `GET /admin/audit/verify` returns the first altered row, if any |
| Reset breakers after an incident | `POST /admin/circuits/reset` (audited) |
