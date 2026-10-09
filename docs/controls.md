# Controls mapping

This document maps each gateway control to the framework functions and risks it supports, with the code that enforces it and the test that provides evidence. It is written for governance, risk and audit reviewers who need to place each control against a framework they already report on. The two frameworks are:

- **NIST AI Risk Management Framework (AI RMF 1.0)** functions (Govern, Map, Measure, Manage), and the risks named in its **Generative AI Profile, NIST AI 600-1**.
- **FinOps Foundation Framework capabilities**, as the FinOps Foundation applies them to AI usage (FinOps for AI): allocation, unit economics, budgeting, anomaly management and so on.

This is a self-assessed mapping. It is not a certification, an attestation or an audit result, and it does not claim compliance with either framework. Every "implemented" row names the code and a test; anything else is marked **partial** or **roadmap**. Threats and residual risks are in the [threat model](threat-model.md).

## NIST AI RMF

| Function | Control in this repo | Status | Code / evidence |
|---|---|---|---|
| **Govern**: policies, roles, accountability | Routing, budgets, limits and breaker thresholds live in one reviewable YAML file with schema validation; bad reloads keep the previous config | implemented | `config/routes.yaml`, `router/models.py`; `test_bad_reload_keeps_previous_config`, `test_alias_mapping_form_and_validation` |
| Govern | Separate admin and application credentials; admin-only key management, usage, showback, circuits, budgets, metrics | implemented | `router/auth.py`; `test_admin_routes_require_admin` |
| Govern | Data residency per team: non-permitted providers removed from every route, fallback included | implemented | `config/policies.yaml` (`teams.regulated`), `config/regulated.yaml`; `test_regulated_team_blocked_from_external_provider`, `test_regulated_team_routes_local_with_hooks_and_ceiling` |
| Govern | Policy-as-code: declarative, schema-validated `config/policies.yaml` (model allow/deny, alias lists, data residency, max_tokens ceiling, request-size limits, required hooks), evaluated before routing, every decision audited and returned in headers; optional OPA with fail-closed behaviour | implemented | `router/policy.py`, `router/admission.py`, `router/opa.py`, `policies/router.rego`; `tests/test_policy.py` |
| Govern | SSO (OIDC) for the admin API and dashboard | **roadmap** | — |
| **Map**: context and risk identification | Threat model (STRIDE + OWASP LLM Top 10 2025) and ADRs recording trade-offs | implemented (docs) | `docs/threat-model.md`, `docs/adr/` |
| Map | Every call attributed to key, team, alias, provider and model | implemented | `router/store.py::record_call` |
| **Measure**: analyze, track | Per-attempt OpenTelemetry GenAI spans (no prompt content) | implemented when OpenTelemetry is installed | `router/telemetry.py`; `test_genai_spans_for_each_attempt` |
| Measure | Prometheus metrics: attempts by outcome, spend, tokens, cache savings, rejections by reason, breaker state and transitions, latency EWMA, TPM in use | implemented | `router/metrics.py`; `test_breaker_opens_and_later_requests_skip_the_dead_provider`, `test_team_tpm_limit_rejects_...` |
| Measure | Spend-velocity anomaly signal per key vs. its own baseline | implemented | `router/anomaly.py`; `tests/test_anomaly.py` |
| Measure | Route evaluation harness: offline replay with reference scoring and a judge hook, cost-vs-quality report, CI gate on quality drop / cost rise; shadow mode on a separate ledger with agreement metrics | implemented (harness verified with simulated providers only; no real-model evaluation has been run) | `router/evals.py`, `router/evalrun.py`, `router/shadow.py`, `scripts/eval_routes.py`, `scripts/eval_gate.py`; `tests/test_evals_shadow.py` |
| **Manage**: respond, recover | Circuit breakers with half-open recovery; ordered fallback; 503 + Retry-After when all circuits are open | implemented | `router/breaker.py`, `router/fallback.py`; `tests/test_breaker.py`, `test_all_circuits_open_returns_503_with_retry_after` |
| Manage | MCP tool gateway: default-deny per-team tool allow-lists, velocity and identical-call limits, daily count/spend caps, redacted argument audit, fail closed | implemented | `router/mcp_policy.py`, `router/mcp_gateway.py`; `tests/test_mcp_gateway.py` |
| Manage | Budget hierarchy, TPM limits, RPM limits, anomaly auto-pause, all enforced before the provider call | implemented | `router/budgets.py`, `router/tokens.py`, `router/auth.py`; tests listed in the threat model (LLM10) |
| Manage | PII redaction / block hooks before the provider call and on the response, per route and team; fail closed if a hook errors | implemented | `router/pii.py`, `router/privacy.py`; `test_route_redaction_reaches_provider_redacted_and_is_audited`, `test_team_block_refuses_before_any_provider_call`, `test_failing_hook_fails_closed` |
| Govern | Append-only, hash-chained audit trail of policy decisions, content-hook actions, MCP tool-call decisions and admin changes; per-request decision traces (metadata only) for explaining individual requests | implemented | `router/audit.py`, `router/traces.py`; `test_audit_log_is_append_only_and_tamper_evident`, `test_admin_actions_are_audited`, `test_mock_provider_answers_without_keys_and_traces_every_stage` |

### NIST AI 600-1 (Generative AI Profile) risks

| GAI risk | Relevant control | Status |
|---|---|---|
| Data Privacy | No prompt content in logs, usage rows, spans or audit rows; content logging opt-in per team and always redacted; PII redaction/block hooks; per-team cache scope keyed by content policy; on-prem-only route profile | implemented (`test_privacy.py`). Pattern-based detection only (names/addresses not detected). Plaintext cache at rest: **gap** |
| Information Security | Key auth, admin separation, constant-time admin compare, clean upstream errors, keys never in exports, request-size limits, fail-closed policy evaluation | implemented. Keys at rest: salted HMAC-SHA256 only, shown once at creation, plaintext databases migrated on start (`router/store.py`, `test_keys_at_rest.py`) |
| Value Chain and Component Integration | Multi-provider fallback reduces single-provider dependency; provider allowlist validated at load; LiteLLM dependency documented (ADR 0001); price overrides for contract rates | implemented. Pinned/hashed dependencies: **roadmap** |
| Confabulation | Semantic cache off by default, with number/negation guards, team/policy partitions and a published held-out false-hit rate (0.0%, 0 of 10 hits, on the bundled pairs at 0.90; 95% upper bound 27.8%, which cannot certify the 1% target); route changes gated on measured quality (eval harness) | implemented (`router/semcache.py`; `tests/test_semcache.py`, ADR 0005); the bundled calibration does **not** certify a 1% target |
| Human-AI Configuration | Breaker/fallback and policy decisions visible to callers via response headers; operators see state on the dashboard; agent tool use bounded by the MCP gateway's allow-lists and velocity limits | implemented |
| Environmental Impacts | Cache avoids repeated calls; local-model routes | partial: energy is not measured |
| Other 600-1 risks (CBRN information, dangerous or violent content, harmful bias, information integrity, intellectual property, obscene content) | Content-level risks; the gateway does not inspect content | out of scope (a policy/guardrail hook is **roadmap**) |

## FinOps for AI

| FinOps capability | Control in this repo | Status | Code / evidence |
|---|---|---|---|
| Allocation | Every call tagged with team and key, including MCP tool calls (`provider: mcp`); cache hits and failed attempts recorded separately | implemented | `router/store.py`; `test_showback_json_and_csv` |
| Invoicing & Chargeback / Reporting & Analytics | `GET /admin/showback` (JSON or CSV), grouped by any of team, key, alias, provider, model, for any UTC date range; CSV-injection-safe | implemented | `router/showback.py`, `router/main.py`; `test_showback_json_and_csv`, `test_csv_escapes_formulas_and_has_unit_columns` |
| Unit Economics | Cost per 1K requests, cost per 1K tokens, share of cost per group | implemented | `router/showback.py`; `test_aggregate_unit_metrics` |
| Budgeting | Org → team → key caps, daily and monthly, checked before the call; config warnings when a child cap exceeds the org cap | implemented | `router/budgets.py`; `test_first_breached_cap_broadest_first`, `test_hierarchy_warnings_and_utilization` |
| Anomaly Management | Per-key velocity vs. 7-day baseline, flag at 3x, optional pause at 10x | implemented | `router/anomaly.py`; ADR 0003 |
| Workload Optimization | Exact-match cache with dollars-saved accounting; local-first routes; latency strategy; cost-vs-quality route evaluation and shadow comparisons before switching routes | implemented | `router/routing.py`, `router/evals.py`, `router/shadow.py`; `test_deterministic_repeat_is_served_from_cache`, `test_cli_report_and_gate`, `test_shadow_mirrors_to_candidate_off_the_books` |
| Rate Optimization | Price overrides for negotiated or self-hosted rates | implemented (pricing input only) | `router/costs.py`; `test_price_overrides_take_precedence` |
| Forecasting | Spend forecasts by team | **partial**: the console's Spend screen projects each team's month-end spend at its current daily rate over the **sample** data; the gateway API has no forecast | `demo/screens.js` |
| Policy & Governance | Caps and limits in versioned config; policy-as-code with model allow/deny lists and max_tokens ceilings | implemented | `config/routes.yaml`, `config/policies.yaml`; `tests/test_policy.py` |

## Roadmap items referenced above

- SSO (OIDC) for the admin API and dashboard.
- Shared (multi-instance) limit and breaker state.
- Pinned, hashed dependencies.
- Spend forecasts by team in the gateway API.
- A policy or guardrail hook for content-level risks.

Application keys hashed at rest, previously listed here, shipped in 0.6.0 (`router/store.py`, `tests/test_keys_at_rest.py`).
