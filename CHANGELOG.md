# Changelog

## [Unreleased]

The console now presents the gateway in the setting it is designed for: a sample credit union, Cypress Harbor Credit Union (fictional), whose seven teams run eleven AI applications through the gateway. It opens on Spend, the FinOps view, with a 500-request log beside it in which every request shows the controls it passed. Evidence for this release: 305 tests and a 159-check browser smoke test across both console modes.

### Highlights

- **Spend** leads with what finance asks first: each team's month-to-date spend, its month-end forecast and its budget, with the gateway's interventions (fallback rescues, anomaly pauses, policy refusals) linked to the requests they describe.
- **Requests** makes every decision explainable per request: a **sample** log of 500 requests, each opening on a page with its cost, the model that answered, the team's budget and every control it passed in order.
- **Business and technical views** let one console serve a department head and an engineer without separate tools.

### Added

- **Spend** (formerly Overview) adds *October so far*: each team's month-to-date spend, the month-end forecast at its current daily rate and its budget, with a note when a team is on track to go over; the latest requests; and activity-feed items linked to the requests they describe.
- **Spend** for the sample company (introduced as **Overview › Business impact**; fictional: seven teams, four providers, monthly budgets). Over 7, 30 or 90 days: AI spend against team budgets, requests served, cost per 1,000 requests, cache savings, success rate with fallback rescues, spend stopped by an anomaly pause, policy decisions enforced and regulated requests kept on-prem, each against the previous period with trend lines; daily spend by team; budgets by team; spend by model; provider incidents; governance checks; recent activity. The previous Overview became **Overview › This session**, now **Spend › This session**.
- **Requests**: the sample company's request log (`demo/data/sample_requests.json`): search, filters by team, outcome, model and day, paging, CSV export. Each request opens a page with what happened in plain words, cost, response time and gateway overhead, the model that answered and any failed attempt, the team's month-to-date budget, every control it passed, recent requests from the same app, and (technical view) key fingerprint, stage details and the raw record. Requests sent in the tab are under **Requests › This session** (the former Traces screen, same URLs).
- **Business and technical views** (header switch or `?view=technical`): the business view names apps, teams, models and outcomes in plain language; the technical view adds keys, aliases, tokens, stage names and timings, and raw data.
- **Light and dark themes**, following the system, with a header toggle.
- **Navigation**: screens grouped by job (Monitor, Operate, Govern, Configure) with sub-pages, breadcrumbs in the header, a command palette (Ctrl/Cmd+K or `/`) over screens and actions, `g` + letter shortcuts with a `?` sheet, a workspace label for the sample company, and a collapsible sidebar (`demo/shell.js`, shared in design with the portfolio's other consoles).
- A not-found page for unknown routes.
- New chart: stacked daily columns (`stackedDaily` in `demo/ui.js`).
- **Request log data**: `scripts/sample_requests.py` writes 500 requests from October 1 to 7, sampled in proportion to each app's traffic and hours, with the fields and stages of `router/traces.py`. Costs are tokens times list prices (on-prem at an assumed GPU chargeback). CI checks the file is current; `tests/test_sample_requests.py` checks prices, each app's cost per request against its route and token sizes, month-to-date budgets, on-prem routing for regulated apps, the October 6 overload and October 2 refusal, and that no prompt or answer text is stored.
- **Usage data**: `scripts/generate_sample_company.py` writes `demo/data/sample_company.json` from a fixed seed and stated assumptions; CI checks it is current, and `tests/test_sample_company.py` checks that daily totals are the sum of the teams and that it is labelled fictional.
- The usage file gains compliance's on-prem `bsa-case-notes` app (bringing the sample to eleven AI applications, from ten), the October 6 Anthropic overload (incident, fallbacks, activity item), and a regulated-request count consistent with the on-prem apps.
- Documentation: `docs/console.md`, `docs/configuration.md`, `docs/operations.md` and `docs/evaluation.md` hold the console, control configuration, operations and evidence detail previously in the README.

### Sample data and evaluation

The sample company now tells one story everywhere it appears: the Spend screen, the request log, the in-browser engine, the route evals, the semantic-cache calibration and `config/*.yaml` describe the same credit union with the same numbers.

- **One definition of the company.** `demo/cypress_harbor.py` defines each application's team, route alias, token sizes, temperature, hours and volume once. `config/routes.yaml` is now the credit union's route config: it adds `regulated-fast` (on-prem only), team monthly and daily caps, a daily cap on every application key, member-services and regulated-route redaction hooks, the on-prem GPU chargeback price, and the calibrated semantic-cache threshold. The usage generator, the request-log generator and the engine read both, so nothing is defined twice.
- **Spend by model follows the request mix.** Spend per team and per model is computed from each app's route, token sizes and list prices rather than from fixed shares. On-prem Llama now carries under 1% of spend, consistent with its chargeback rate.
- **Routes agree everywhere.** Each app calls one alias in config, engine, log and Spend: online and mobile banking on `fast-chat`, the code assistant on `local-first` and `heavy-reasoning`, regulated work on `regulated-fast`. `fraud-scoring-batch` is renamed `fraud-alert-narratives` and runs nightly on `cheap-batch`.
- **Stories match the data.** Incident counts are computed from the daily data; breakers open after `failure_threshold` (5) failures and probe every `cooldown_seconds` (30), as configured; the September 16 OpenAI incident affects `fast-chat`, which fell back to Claude Haiku; the July 29 Gemini 429s appear in that day's fallbacks; the request log shows the October 6 breaker opening (first a 529, then skipped attempts). The September 24 runaway is now a coding agent looping on `heavy-reasoning`, paused by the anomaly rule 69 seconds after it started at 10.2x the key's baseline; spend prevented ($12.72) is bounded by the key's daily cap. The September 3 item is the August showback, with figures recomputed from the data, and the August 11 item records moving on-prem residency into policy.
- **Contractor refusal is the policy's decision.** The vendor key belongs to team `contractors`; its `heavy-reasoning` request is refused because the contractors policy allows only `smart-fast`, `cheap-batch` and `local-first`, the reason `router/policy.py` returns. The developer's follow-up runs on `local-first`. `config/policies.yaml` adds a `regulated-fast` route rule (ollama only, `pii_redact` required, 2,048-token ceiling).
- **Volumes and budgets at the credit union's size.** About 12,000 requests a day for 92,400 members (from about 84,000). Over the 30 days to October 7: $943 of AI spend against $1,099 in team budgets (was $9,311 against $14,600), 373,000 requests (was 2.52 million), $2.53 per 1,000 requests (was $3.69), $177 saved by the cache, 1,239 requests rescued by fallback and 4 failed, 8,498 regulated requests kept on-prem. Regulators are NCUA and the Florida Office of Financial Regulation; CFPB rules apply.
- **Engine.** Keys, aliases, prompt sizes, temperatures, hours and the runaway scenario come from the same table. Caps and 7-day anomaly baselines are the credit union's divided by 25, stated on Budgets & Keys; simulated traffic repeats only at each app's cache hit rate.
- **Credit-union content.** The MCP example is a member-case system (`cases` server: `search_cases`, `get_case`, `close_case`, `reassign_case`; case C-1042) and a procedures share (`policies/bsa-procedures.txt`); `MCP_TICKETS_AUTH` becomes `MCP_CASES_AUTH`. Semantic-cache examples, the PII sample ("Case with PII"), the "What to try" cards and request callers (departments for back-office apps) use credit-union language. `config/regulated.yaml` names `member-npi` and the `bsa` profile instead of healthcare terms.
- **Route evals.** `evals/sample_cases.jsonl` holds 40 credit-union tasks (knowledge-base answers, dispute tagging, Regulation E deadlines, loan-document extraction, BSA review, fraud-alert narratives, a regulatory digest, code review, rate checks) written by `scripts/build_eval_cases.py`, with prompts the size the apps send; `regulated-fast` is evaluated too. Simulated results: `smart-fast` 0.850 quality and $2.76 per 1,000 requests (was 0.975 and $0.03 on 20-token prompts), `heavy-reasoning` 1.000 (was 0.950), `cheap-batch` 0.625 (was 0.775). The CI gate example asks whether the member assistant could move from `smart-fast` to `cheap-batch`: it fails on quality (0.850 → 0.625). A Sonnet-first `smart-fast` fails on cost (+203%, was +216%).
- **Semantic-cache calibration.** `evals/semcache_pairs.jsonl` holds 160 credit-union question pairs, including near-duplicates and hard negatives (certificate terms, loan numbers, "turn on" vs "turn off"). Measured: threshold 0.90 (was 0.88); held-out hit rate 26.3%, false-hit rate 0.0% (0 of 10 hits, 95% upper bound 27.8%; was 9.1%, 1 of 11); also 0.0% without guards (was 28.6%). Ten hits cannot certify a 1% rate, so the cache stays off by default; the default threshold in `router/models.py` and `config/routes.yaml` is now 0.90.
- **Consistency tests.** `tests/test_sample_consistency.py` checks that Spend by model equals what the request mix implies, that each app's route is the same in config, engine, log and Spend, that every alias in the log exists in the config, that activity-feed and incident numbers and thresholds match the data and config, that every day stays inside every cap, that eval cases are credit-union tasks of realistic size, and that demo-facing files contain no generic shop, SaaS or healthcare strings.
- **Test suite card.** The Evals screen explains that pytest collects more tests (305) than there are test functions (255) because parametrized cases run separately; every test file has a description.

### Changed

- Spend and Requests render from the committed files before Pyodide has started; screens that need the engine say they are starting, and explain what still works if it cannot load.
- The guided tour is offered on the first visit instead of opening by itself.
- The demo's teams and app keys are named for the setting: `product` → `digital-banking` (`web-app` → `online-banking`, `mobile-app` → `mobile-banking`), `support` → `member-services` (`support-bot` → `member-assistant`), `data` → `risk-analytics` (`nightly-batch` → `fraud-alert-narratives`); sample prompts are credit-union work (card disputes, statements, member messages). `config/mcp.example.yaml` and `config/mcp.mock.yaml` use the same team names. Test fixtures use the same team names.
- The README is restructured as a reference architecture: executive summary, problem and context, design principles, decisions and trade-offs, controls and risk mapping, evidence, operations, and residual risk. ADRs use a common Status, Context, Decision, Consequences and Alternatives format.

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
