# Configuration

Every control the gateway enforces is declared in a reviewable YAML file, validated against a schema at load, and changed through a pull request or an audited admin action. This document describes each file, the controls it configures, the behaviour of each control and the test that provides evidence for it. Deployment settings (environment variables) are listed in [operations](operations.md#environment).

| File | Configures |
|---|---|
| [config/routes.yaml](../config/routes.yaml) | Aliases and their ordered targets, routing strategy, circuit breakers, latency ordering, budgets, TPM, prices, cache, content hooks, semantic cache, shadow mode |
| [config/policies.yaml](../config/policies.yaml) | Policy as code: alias and model lists, data residency, `max_tokens` ceilings, request limits, required hooks, optional OPA |
| [config/regulated.yaml](../config/regulated.yaml) | A routes file whose aliases reach only on-prem Ollama |
| [config/mcp.example.yaml](../config/mcp.example.yaml) | MCP tool servers and per-team tool allow-lists, velocity and caps (copy to `config/mcp.yaml`) |
| [policies/router.rego](../policies/router.rego) | Example OPA policy |

## Policy as code

`config/policies.yaml` is evaluated before budgets, cache or any provider call ([router/policy.py](../router/policy.py)). Layers combine so the most restrictive wins: `defaults` → `teams.<team>` → `routes.<alias>`. The caller's team comes from its key, set by an administrator, never from the request.

```yaml
defaults:
  max_tokens_ceiling: 4096        # also sent when a request doesn't set max_tokens
  max_tokens_mode: clamp          # or reject (400)
  max_request_bytes: 262144       # serialized messages; 413 above it
  max_messages: 200
teams:
  regulated:
    allowed_providers: [ollama]   # public providers are removed from every route, fallback included
    required_hooks: [pii_redact]  # runs even if the team's privacy config has no hooks
    max_tokens_ceiling: 2048
  contractors:
    allow_aliases: [smart-fast, cheap-batch, local-first]
    deny_models: ["anthropic/claude-sonnet-*", "openai/gpt-4.1"]
```

| Behaviour | Detail | Evidence |
|---|---|---|
| Schema validation | Unknown keys, providers, modes, unregistered hooks and malformed globs are errors. An invalid file stops startup; a bad `POST /admin/reload` returns 400 and keeps the previous routes **and** policies. | `test_schema_errors`, `test_reload_is_atomic_and_missing_explicit_file_fails` |
| Combination | Allow-lists intersect, deny-lists union, ceilings take the smallest, `reject` beats `clamp`, required hooks union. A route cannot loosen a team rule. | `test_most_restrictive_layer_wins` |
| Decisions | Removed deployments are never tried. Nothing left → 403. Alias not allowed → 403; over-size → 413; over the ceiling → 400 or clamped. | `test_regulated_team_blocked_from_external_provider`, `test_regulated_team_routes_local_with_hooks_and_ceiling`, `test_max_tokens_reject_and_request_limits` |
| Visibility | `x-router-policy`, `x-router-policy-rules`, `x-router-policy-source`, `x-router-policy-removed`, `x-router-policy-max-tokens`; every decision is an audit row; `GET /admin/policies?team=&alias=` shows the effective rule. | `test_admin_policies_shows_effective_rule` |
| Optional OPA | With `OPA_URL` set, OPA must also allow (`POST /v1/data/<opa.path>`, input is metadata only, never content). OPA can narrow the route via `allowed_targets`, never widen it. Timeouts, errors, non-200, undefined or malformed results → 503 (fail closed). | `test_opa_allow_deny_narrow_and_input_has_no_content`, `test_opa_failures_fail_closed` (fake OPA over real HTTP) |
| Body size | `ROUTER_MAX_BODY_BYTES` (default 1 MiB) refused with 413 before parsing, including chunked bodies. | `test_body_size_middleware_refuses_before_parsing` |

[policies/router.rego](../policies/router.rego) is an example OPA policy (local-only teams, off-peak batch alias, org-wide `max_tokens`, model deny list) with its own tests: `opa test policies/` passed 8/8 locally on OPA 1.4.2, and the gateway was checked against a real `opa run --server` with that policy. CI does not run OPA.

## FinOps controls

All of these are decided **before** a provider is called.

| Control | Behaviour | Config |
|---|---|---|
| Budget hierarchy | Org, team and key caps, daily and monthly (UTC). Broadest scope checked first; 402 when a cap is reached. Team/key overrides inherit unset fields. | `policies.per_*_usd`, `budgets:` |
| Tokens per minute | Per key and per team. An estimate (prompt + `max_tokens`) is reserved, then corrected to provider-reported usage; released if every provider fails. 429 + `Retry-After`. | `policies.per_*_tpm`, `budgets.*.tpm` |
| Requests per minute | Per key. | `policies.rate_limit_rpm` |
| Spend anomalies | This hour vs. the key's 7-day baseline: flagged at 3x, auto-paused (429) at 10x if enabled. | `policies.auto_pause_on_anomaly` |
| Showback / chargeback | `GET /admin/showback?group_by=team,key,model,provider&format=csv` with cost, tokens, cost per 1K requests, cost per 1K tokens, share of cost. Keys appear as label + fingerprint, never the secret. | admin key |
| Price overrides | Negotiated or self-hosted rates (USD per 1M tokens) take precedence over LiteLLM's catalog. Unpriced models are flagged at startup and on `/health`. | `prices:` |
| Data residency | A team's `allowed_providers` in `config/policies.yaml` removes every other provider from any route it calls, fallback included; `config/regulated.yaml` is a routes file with only on-prem Ollama. | `policies.teams.<team>`, separate routes file |

Budget overrides set through the admin API (`PUT/DELETE /admin/budgets/{org|team|key}/{name}`) are stored in SQLite, win over `routes.yaml` field by field, and are audited. Key holds (`POST /admin/keys/{id}/pause`, `/unpause`) are audited the same way; unpausing an anomaly-paused key is an override until the end of the UTC hour.

What "enforced" means precisely: caps compare against spend already recorded, so concurrent in-flight requests can overshoot a cap by their own cost, and all limits are per gateway process.

## Routing and resilience

- **Ordered fallback.** Any provider error, timeout or 429 moves to the next target in the alias.
- **Circuit breakers per `provider/model`.** Closed → open after N consecutive failures or an error-rate threshold in a sliding window, or immediately when a 429/503 carries `Retry-After`. Open → half-open after the cooldown; limited probes; closed again on success. Caller errors (400, 422) do not count. If every target is open: **503 + `Retry-After`** with no provider call.
- **Latency-aware routing (opt-in).** `strategy: latency` orders healthy targets by an EWMA of observed latency (time-to-first-token for streams). Near-ties within 10% keep the configured order; new deployments get a short warm-up. Routes without it are unchanged.
- **Streaming.** Fallback before the first chunk; a mid-stream failure ends the stream with an error event. Tokens received are recorded when the stream ends or the client disconnects.
- **Visible routing.** `x-router-used-provider`, `x-router-used-model`, `x-router-cache`, `x-router-strategy`, `x-router-attempts`, `x-router-fallback-from`, `x-router-circuit-skipped`.

```yaml
aliases:
  smart-fast:                       # configured order
    - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 20 }
    - { provider: openai,    model: gpt-4.1-mini,     timeout_s: 20 }
    - { provider: ollama,    model: llama3.1:8b,      timeout_s: 60 }
  fast-chat:                        # fastest healthy first
    strategy: latency
    targets:
      - { provider: anthropic, model: claude-haiku-4-5 }
      - { provider: openai,    model: gpt-4.1-mini }

resilience:
  circuit_breaker: { failure_threshold: 5, error_rate_threshold: 0.5, min_requests: 10,
                     window_seconds: 60, cooldown_seconds: 30, half_open_max_probes: 1 }
  latency: { alpha: 0.3, min_samples: 3, tolerance: 0.10 }

budgets:
  org:   { daily_usd: 2000, monthly_usd: 40000 }
  teams: { data: { daily_usd: 200, tpm: 400000 } }
  keys:  { fraud-scoring-batch: { monthly_usd: 300 } }
```

The full annotated file is [config/routes.yaml](../config/routes.yaml). The budget figures above are examples; the shipped file leaves org and team overrides empty. Design rationale: [ADR 0002](adr/0002-ordered-fallback-and-circuit-breakers.md).

## Privacy and audit controls

| Control | Behaviour | Config |
|---|---|---|
| PII redaction hooks | `pre_request` hooks run over message contents before any provider call; `post_response` hooks run over the completion before it reaches the client, the cache or the content log. Built-ins: `pii_redact` (emails, phones, SSNs, Luhn-valid cards, API-key-like secrets → `[REDACTED:KIND]`), `pii_block` (422), `pii_detect` (audit only). Layers union: default + route + team. Responses carry `x-router-content-hooks` and `x-router-redactions` (counts only). | `privacy:` in routes.yaml |
| Custom hooks | `register_hook(name, fn)` in a module listed in `ROUTER_HOOK_MODULES`; unknown hook names fail config validation. A hook that raises fails the request closed (503, nothing sent). | `ROUTER_HOOK_MODULES` |
| Content logging | Off by default: usage rows, logs, spans and the audit trail hold metadata only. A team opts in with `log_content: true`; stored prompts and completions are then redacted with every detector and truncated. `GET /admin/content-log`. | `privacy.teams.<team>.log_content` |
| Audit trail | Every policy decision, every hook action (redact, block, detect, hook error), every content-log write, every MCP tool-call decision, and administrative changes (key creation and revocation, reloads, config applies, budget overrides, key pauses and anomaly overrides, circuit resets) are appended to `audit_log` with counts, never matched values. Rows are hash-chained; triggers refuse UPDATE/DELETE; `GET /admin/audit/verify` finds the first altered row. | always on |

Pattern-based detection misses PII it has no pattern for (names, street addresses, numbers spelled out) and can flag look-alikes; it reduces what reaches a provider or a log, and does not guarantee it.

The audit trail is distinct from **decision traces**: a trace ([router/traces.py](../router/traces.py)) is the per-request timeline of stages, held in a bounded in-memory buffer per worker; the audit trail is the durable, hash-chained record. See [operations](operations.md#observability).

## MCP tool gateway

`POST /mcp/{server}` places the same governance in front of [Model Context Protocol](https://modelcontextprotocol.io) tool servers ([router/mcp_gateway.py](../router/mcp_gateway.py), [router/mcp_policy.py](../router/mcp_policy.py), [ADR 0006](adr/0006-mcp-tool-gateway-scope.md)). It is configured from [config/mcp.example.yaml](../config/mcp.example.yaml):

```yaml
servers:
  tickets: { url: http://127.0.0.1:9101/mcp, headers_from_env: { Authorization: MCP_TICKETS_AUTH } }
teams:
  support:
    - { server: tickets, tool: "search_*" }
    - { server: tickets, tool: close_ticket, per_minute: 5, per_day: 200 }
```

| Control | Behaviour | Evidence |
|---|---|---|
| Default deny, per-team allow-lists | `tools/list` shows only allowed tools; other `tools/call`s get JSON-RPC `-32001` and never reach the server; a team with no entry for a server gets 403 | `test_denied_tools_never_reach_the_server`, `test_initialize_list_and_notifications_pass_through` |
| Velocity (fraud-style) | Per key per tool and per team per tool calls/minute; an identical-call limit (same tool and arguments) that stops agent loops; `-32002` + `Retry-After` | `test_velocity_identical_calls_and_daily_cap`, `test_check_call_allow_list_velocity_identical_and_caps` |
| Count and spend caps | Daily calls and USD per team per tool, read from the usage table (survive restarts); `-32003` | `test_velocity_identical_calls_and_daily_cap` |
| Argument redaction | Audit rows keep PII-redacted argument values (or keys only); `forward_redacted` sends redacted arguments upstream too | `test_allowed_call_is_forwarded_recorded_and_audited_with_redaction`, `test_sse_replies_and_forward_redacted` |
| Audit and accounting | Every `tools/call` decision is an audit row; allowed calls are usage rows (`provider: mcp`) priced at `cost_usd`, visible in showback; `router_mcp_tool_calls_total` | `test_allowed_call_is_forwarded_recorded_and_audited_with_redaction` |
| Fail closed | Upstream down, non-2xx, malformed, or its credential variable unset → `-32004`, nothing partial; unset credentials mean the server is not called at all | `test_upstream_failures_and_missing_credentials_fail_closed` |

**Scope.** The request/response part of the Streamable HTTP transport (JSON or SSE replies), with `initialize`, `ping`, `notifications/*`, `tools/list` and `tools/call`. Not proxied: stdio servers, batches, GET streams, DELETE, resumability, server-to-client requests, resources and prompts. Clients authenticate with their gateway key; upstream servers get static headers from environment variables (no OAuth). Tested against a fake MCP server over real HTTP, not third-party servers.

## Semantic cache

[router/semcache.py](../router/semcache.py) serves a cached answer when a new prompt's last user turn is similar enough to a cached one ([ADR 0005](adr/0005-semantic-cache-off-by-default-calibrated.md)). It is **off by default**; the measured calibration that keeps it off is in [evaluation](evaluation.md#semantic-cache-calibration).

- **Embedder:** offline deterministic hashing (word and character n-grams) by default; a provider embedding model with `embedder: provider`.
- **False-hit guards:** a hit also needs the same numbers and the same negation in both prompts.
- **Isolation:** entries are partitioned by team, alias, policy decision, content hooks, earlier messages, `max_tokens` and embedding space (`test_cache_never_crosses_teams_policies_or_context`). Per-team opt-in (`teams`) and per-team thresholds (`team_thresholds`).
- **Visibility:** `x-router-cache: semantic-hit`, `x-router-semantic-similarity`, `x-router-semantic-match` (`hit`, `below_threshold`, `guard-numbers-differ`, …); `router_semantic_cache_total`; `GET /admin/semantic-cache`.

The exact-match cache (`policies.cache_ttl_seconds`, `policies.cache_max_temperature`) is described in [ADR 0004](adr/0004-exact-match-cache-now-semantic-later.md).

## Shadow mode

[router/shadow.py](../router/shadow.py) mirrors a share of an alias's live traffic to a candidate alias in the background. Its guarantees and evidence are in [evaluation](evaluation.md#shadow-mode).

```yaml
shadow:
  smart-fast: { candidate: cheap-batch, percent: 10, billing: ledger, max_daily_usd: 5 }
```
