# Threat model

Scope: the gateway process (`router/`, including the MCP tool gateway), its SQLite store (including the audit and content logs), its config files, the admin dashboard (`dashboard/index.html`) and the browser demo (`demo/`). Out of scope: the LLM providers themselves, the client applications, and the network in front of the gateway (TLS termination, WAF), which a deployment must supply.

Every mitigation below names the code or test that implements it. Gaps are marked **gap** and listed under [roadmap](#gaps-and-roadmap). This is a design review of a small open-source gateway, not the result of a penetration test.

## Assets

| Asset | Where it lives |
|---|---|
| Provider API keys | Process environment (`.env`, `router/config_loader.py`) |
| MCP server credentials | Environment variables named by `headers_from_env` in `config/mcp.yaml`; read per call, never logged or returned by `/admin/mcp` |
| Gateway API keys (one per app) | SQLite `api_keys` table: id, display prefix, fingerprint, salt and HMAC-SHA256(pepper, salt + key); the key is shown once at creation and never stored |
| Admin key | `ROUTER_ADMIN_KEY` env var, or keys flagged `is_admin` |
| Spend: budgets and provider invoices | Enforced by `router/budgets.py`, `router/tokens.py`, `router/anomaly.py` |
| Prompts and completions | In transit only by default. Also stored: completions in the response cache when it's enabled; redacted prompts and completions in `content_log` for teams that opt in (`privacy.teams.<team>.log_content`) |
| Audit trail of policy and admin actions | SQLite `audit_log`, hash-chained (`router/audit.py`) |
| Usage records (who called what, tokens, cost, error text) | SQLite `usage` table; mirrored (shadow) calls in `shadow_usage` |
| Routing and admission policy | `config/routes.yaml` (or `regulated.yaml`), `config/policies.yaml`, optional OPA policy (`policies/*.rego`) |

## Trust boundaries and data flow

```
 client app ──(1) bearer key──▶ gateway ──(3) provider key──▶ OpenAI / Anthropic / Gemini (internet)
                                 │   └──────────────────────▶ Ollama (OLLAMA_HOST, usually on-prem)
 operator ──(2) admin key──────▶ │
                                 ▼
                          SQLite file (keys, usage, cache)
```

1. Client → gateway: untrusted input (messages, model alias, temperature, max_tokens, stream). The caller's team comes from its key (set by an admin), never from the request, so a client can't pick a looser policy layer.
2. Operator → admin API / dashboard: privileged.
3. Gateway → providers: data leaves the trust boundary. A team with `allowed_providers: [ollama]` in `config/policies.yaml` has every other provider removed from any route before the fallback chain runs; for `config/regulated.yaml` aliases, only Ollama is configured at all.
4. Gateway → OPA (optional): request metadata only (team, key label, alias, permitted deployments, max_tokens, sizes), never message content.
5. Agent → gateway → MCP servers: tool calls cross the same boundary as model calls; the gateway decides before forwarding.

## STRIDE

| Threat | Example | Mitigation in this repo | Evidence |
|---|---|---|---|
| **S**poofing a client | Calling with a guessed or revoked key | Bearer key required on `/v1/*`; revoked keys rejected; keys are 32 bytes from `secrets.token_urlsafe` | `router/auth.py`; `tests/test_api.py::test_requires_key` |
| **S**poofing an admin | Using an app key on admin routes | Admin routes require `ROUTER_ADMIN_KEY` (constant-time compare) or an admin-flagged key | `router/auth.py`; `test_admin_routes_require_admin`, `test_showback_validation_and_auth`, `test_budgets_endpoint_reports_utilization` |
| **T**ampering with routing policy | Editing `routes.yaml` to route regulated traffic to a public provider | Residency is enforced by `policies.yaml` per team, independently of the routes file: a public provider added to a route is removed for that team. Both files are meant to be reviewed in PRs; schema validation rejects unknown keys, providers, strategies, modes and hooks; reloads swap routes and policies together or not at all | `router/models.py`, `router/policy.py`; `test_regulated_team_routes_local_with_hooks_and_ceiling`, `test_schema_errors`, `test_reload_is_atomic_and_missing_explicit_file_fails`, `test_shipped_route_files_validate`. No signature check on either file (**gap**) |
| **T**ampering with policy evaluation | OPA sidecar down or returning garbage so requests slip through | Any OPA error, timeout, non-200, undefined or malformed result denies (503); a policy-engine exception denies (503); `opa.enabled: true` without `OPA_URL` denies everything | `router/admission.py`; `test_opa_failures_fail_closed`, `test_policy_engine_crash_fails_closed`, `test_opa_enabled_without_url_refuses_everything` |
| **T**ampering with usage data | Editing SQLite to erase spend | File permissions of the host only for `usage` (**gap**). Policy and admin actions go to `audit_log`, which is append-only through SQLite (UPDATE/DELETE triggers) and hash-chained, so edits are detectable; not tamper-proof against someone who can rewrite the file | `router/audit.py`; `test_audit_log_is_append_only_and_tamper_evident` |
| **R**epudiation | "We never sent that customer's card number" / "no one changed that key" | Every content-hook action (redact, block, detect, hook error), content-log write and admin key/reload action is an audit row with team, key fingerprint, counts and time; `GET /admin/audit/verify` checks the chain | `router/privacy.py`, `router/main.py`; `test_route_redaction_reaches_provider_redacted_and_is_audited`, `test_admin_actions_are_audited` |
| **R**epudiation | "Our team didn't spend that" | Every provider attempt, failure and cache hit is a usage row with key, team, alias, provider, model, tokens, cost and timestamp; showback exports it by team/key | `router/store.py::record_call`; `test_showback_json_and_csv` |
| **I**nformation disclosure: upstream errors | Provider error text leaking internal hostnames to clients | Clients get a generic 502/503; details go to logs and the admin-only usage table | `test_all_providers_failing_returns_clean_502` |
| **I**nformation disclosure: keys in exports | Raw app keys in showback, metrics or headers | Showback shows label + 8-char SHA-256 fingerprint; TPM scopes use label + fingerprint; headers carry only deployment names | `router/store.py::usage_rows`; `test_showback_json_and_csv` asserts the raw key is absent |
| **I**nformation disclosure: cross-team cache | Team B receiving team A's cached completion | Exact and semantic caches are keyed by team, and by the policy decision and content hooks in force; the semantic cache also by earlier messages and parameters | `test_cache_skips_creative_requests_and_other_teams`, `test_cache_never_crosses_teams_policies_or_context`, `test_cache_does_not_cross_a_policy_change` |
| **I**nformation disclosure: PII to a provider | A support ticket with a customer's email and card number sent to a public model | `pre_request` hooks redact (or block, 422) emails, phones, SSNs, Luhn-valid cards and API-key-like secrets before any provider call; configurable per route and team; a failing hook fails closed (503) | `router/pii.py`, `router/privacy.py`; `test_route_redaction_reaches_provider_redacted_and_is_audited`, `test_team_block_refuses_before_any_provider_call`, `test_failing_hook_fails_closed` |
| **I**nformation disclosure: content logs | Prompts with PII sitting in a log table | Content logging is off by default; per-team opt-in stores redacted, truncated text only | `test_defaults_send_content_unchanged_and_store_no_content`, `test_content_log_is_opt_in_per_team_and_always_redacted` |
| **I**nformation disclosure: telemetry | Prompts exported to a tracing backend | Spans carry model, provider, token counts and cost only, never message content | `router/telemetry.py`; `test_genai_spans_for_each_attempt` |
| **D**enial of service / wallet | Runaway loop, leaked key, oversized prompts | See LLM10 below | — |
| **E**levation of privilege | App key creating keys | Key management is admin-only | `router/main.py` `AdminDep` on every `/admin/*` route |
| **E**levation of privilege | An agent calling a tool its team isn't cleared for (e.g. a delete tool) | MCP default deny: per-team allow-lists, hidden from `tools/list`, refused before the server is called, audited | `router/mcp_policy.py`, `router/mcp_gateway.py`; `test_denied_tools_never_reach_the_server` |
| **E**levation of privilege | A team calling a model or alias it isn't cleared for | Per-team alias allow/deny lists and model allow/deny globs, evaluated before routing; denied requests are audited | `router/policy.py`; `test_provider_and_model_lists_remove_targets_in_order`, `test_alias_size_and_message_limits` |

## OWASP Top 10 for LLM Applications (2025)

The gateway sits in front of models, so it's the natural enforcement point for three of the ten.

### LLM10: Unbounded Consumption (primary focus)

| Control | What it does | Evidence |
|---|---|---|
| Requests per minute per key | Sliding 60 s window | `router/auth.py::_rate_limit` |
| Tokens per minute per key and per team | Estimate reserved before the call (prompt + `max_tokens` or `default_output_tokens_estimate`), corrected to real usage after, released if all providers fail; 429 + `Retry-After` | `router/tokens.py`; `test_team_tpm_limit_rejects_with_retry_after_and_settles_to_real_usage`, `test_tpm_reservation_released_when_all_providers_fail` |
| Budget hierarchy org → team → key, daily and monthly | Checked before any provider call; 402 | `router/budgets.py`; `test_org_daily_cap_blocks_every_team`, `test_team_monthly_override`, `test_first_breached_cap_broadest_first` |
| Spend-velocity anomaly pause | Key at 10x its own 7-day baseline gets 429 (opt-in) | `router/anomaly.py`; `test_auto_pause_blocks_runaway_key` |
| Circuit breakers | Stop sending traffic to a failing or rate-limiting provider; honour `Retry-After` | `router/breaker.py`; `tests/test_breaker.py` |
| Shadow mode bounded | Sampled percentage, per-alias `max_daily_usd` on the shadow ledger, admitted by the candidate's policy, own breakers | `router/shadow.py`; `test_shadow_billing_suppress_cap_sampling_and_scope`, `test_shadow_respects_the_candidates_policy` |
| Streams billed when cut short | Usage recorded when the SSE generator closes, so disconnects still count toward caps | `test_stream_closed_early_is_still_recorded` |
| Unpriced models are visible | Startup warning and `/health` listing, because $0-priced calls don't move caps | `router/costs.py`, `router/main.py::_warn_unpriced` |
| `max_tokens` ceiling | Per default/team/route; over it → 400 or clamped; unset → the ceiling is sent | `router/policy.py`; `test_max_tokens_ceiling_reject_clamp_and_unset`, `test_max_tokens_reject_and_request_limits` |
| Request size | Body over `ROUTER_MAX_BODY_BYTES` → 413 before parsing (chunked bodies counted as they arrive); policy `max_request_bytes` / `max_messages` → 413 | `router/main.py::BodySizeLimit`; `test_body_size_middleware_refuses_before_parsing` |

Residual risk:
- Caps compare against **recorded** spend: concurrent in-flight requests can overshoot a cap by their own cost.
- All limits are **per process**: N workers allow up to N× the configured RPM/TPM and N× breaker failures (**gap**: shared store).
- Anomaly verdicts are cached for 30 s on the request path.
- Shadow spend is outside budgets and anomaly detection by design; its only bound is `percent` and `max_daily_usd` (a cap of 0 means unbounded).

### LLM02: Sensitive Information Disclosure

- **Data residency by policy:** `allowed_providers` per team removes other providers from every route, including fallback, so an outage of the permitted provider fails the request instead of sending data to a public API (`test_regulated_team_routes_local_with_hooks_and_ceiling`). `config/regulated.yaml` additionally ships routes with only Ollama configured.
- **No prompt logging by default:** the usage table, logs, spans and audit trail don't store prompts. Evidence: `record_call` has no content field; spans set no content attributes; audit rows carry counts (`test_route_redaction_reaches_provider_redacted_and_is_audited` asserts the raw values are absent). A team can opt in to a content log, which stores redacted, truncated text only (`test_content_log_is_opt_in_per_team_and_always_redacted`).
- **Cache contents:** cached completions are stored in plaintext SQLite (`cache.response_json`) when `cache_ttl_seconds > 0`. Scoped per team. Disable the cache or encrypt the volume for sensitive workloads (**gap**: no encryption at rest).
- **Error text:** up to 200 characters of provider error text are stored in `usage.error` and shown on the admin dashboard. Admin-only, but it can include provider-side detail.
- **App keys at rest:** salted HMAC-SHA256 with an optional `ROUTER_KEY_PEPPER`; lookups by display prefix with constant-time comparison; databases from 0.5.x are migrated on start with `secure_delete` and `VACUUM` so plaintext pages are overwritten (`tests/test_keys_at_rest.py`).
- **PII redaction:** `pre_request` / `post_response` hooks per route and team (`router/pii.py`, `router/privacy.py`). Detection is pattern-based: emails, phones, SSNs, Luhn-valid cards, API-key-like secrets. It does not find names, addresses or free-form identifiers (**residual risk**). Streams can't use `post_response` hooks (400) because chunk-wise redaction can miss a match split across chunks.
- **Cache and policy changes:** the cache key includes a fingerprint of the hooks in force, so tightening a policy doesn't serve answers cached under the old one (`test_cache_entries_do_not_cross_a_content_policy_change`).

### LLM03: Supply Chain

- **Provider adapter dependency:** LiteLLM is on the request path (ADR 0001). `requirements.txt` uses version floors, not pins (**gap**: pin with hashes or a lock file for production).
- **Model/price catalog:** costs come from LiteLLM's bundled catalog; `prices:` overrides let you pin negotiated or self-hosted prices explicitly.
- **Provider allowlist:** `provider` is a closed `Literal` validated at load time, so a config typo can't send traffic to an unexpected backend.
- **Browser demo:** loads Pyodide from jsDelivr without Subresource Integrity (**gap**). It holds no secrets and makes no provider calls, so the exposure is limited to the demo page itself.
- **CI:** GitHub Actions runs ruff and the offline test suite on every push; actions are referenced by major version tag, not SHA (**gap**).

### Other categories, briefly

| Category | Status |
|---|---|
| LLM09 Misinformation (cache) | A semantic-cache false hit returns a confident answer to a different question. Mitigations: off by default, per-team opt-in, number/negation guards, published held-out false-hit rate (9.1% on the bundled set; **residual risk**). `test_bundled_pairs_calibration_numbers` |
| LLM01 Prompt Injection | Not addressed: the gateway doesn't classify content. A custom content hook (`register_hook`) is the extension point; none ships. |
| LLM05 Improper Output Handling | Not addressed: outputs are relayed unchanged. The dashboard HTML-escapes everything it renders. |
| LLM06 Excessive Agency | MCP tool gateway: default-deny per-team tool allow-lists, per-key/per-team velocity, identical-call loop guard, daily count and spend caps, redacted argument audit, fail-closed upstream handling (`tests/test_mcp_gateway.py`, ADR 0006). Residual: request/response tools only; no per-user upstream identity; velocity windows are per process. |
| LLM04, LLM07, LLM08 | Out of scope for a routing gateway. |

## Gaps and roadmap

| Gap | Planned control |
|---|---|
| Per-process limits and breaker state | Shared store (Redis) for RPM/TPM windows and breaker state |
| Policy files are unsigned | Verify a signature or a pinned digest on load |
| MCP upstream auth is static headers | OAuth to upstream MCP servers; per-user identity |
| OPA policy isn't tested in CI | Add `opa test policies/` to CI (passes locally on OPA 1.4.2) |
| No SSO for admin | OIDC for the dashboard and admin API |
| Usage table is not append-only | Move usage rows into the hash chain, or ship them to an external append-only store |
| Plaintext cache at rest | Optional encryption, or keep the cache off for regulated aliases |
| Unpinned dependencies, no SRI on the demo's CDN script | Lock file with hashes; SRI once the published file hash is verified |
