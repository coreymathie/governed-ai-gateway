# Evaluation and evidence

This document records how the gateway's behaviour is verified and what each result does and does not show. Every figure is labelled **measured** (produced by running the repository's code), **simulated** (produced against simulated providers with invented profiles) or **sample** (generated data for the fictional Cypress Harbor Credit Union). No real-model evaluation has been run for this repository.

## Test suite

```bash
pip install -r requirements.txt ruff
ruff check . && ruff format --check .
python -m pytest -q
python scripts/demo_smoke.py --both     # optional: every console screen in headless Chromium, demo + live (needs playwright)
python scripts/build_console_data.py    # regenerate demo/data/*.json after changing evals, routes or tests
```

Latest local run: **289 passed, 1 skipped (290 tests)**. The skipped test checks the tiktoken estimator and only runs when tiktoken can load its `cl100k_base` encoding (it downloads it on first use). Tests stub LiteLLM's completion call, so they run offline without provider keys. CI runs ruff, the format check and `pytest -q` on every push, and checks that the sample data files are current (`scripts/generate_sample_company.py --check`, `scripts/sample_requests.py --check`).

| File | Tests | Covers |
|---|---|---|
| `test_breaker.py` | 17 | Every breaker transition on an injected clock; error-rate window; Retry-After (seconds and HTTP-date, capped); which errors count; probe slots |
| `test_latency_and_chain.py` | 14 | EWMA math, warm-up, tolerance ties, TTFT vs latency; fallback chain skips, 503 hints, cancellation releasing probes |
| `test_tokens_budgets_showback.py` | 21 | Heuristic and tiktoken estimates, TPM windows, settle/release, budget hierarchy order and inheritance, showback unit metrics, CSV formula escaping |
| `test_v05_api.py` | 22 | Breakers, headers and metrics through the HTTP API; half-open recovery; provider 429 Retry-After; latency routes; TPM 429; org/team caps; showback JSON/CSV; GenAI spans; config validation; early stream close |
| `test_evals_shadow.py` | 22 | Case validation and scoring, simulation determinism and profiles, fallback/errors/cost/latency in replays, judge hook, gate decisions, pinned simulated numbers for the shipped routes, real runs refused without keys, both CLI scripts; shadow mirroring off the books, failure isolation, candidate policy, redaction and required hooks, billing suppress, daily cap, sampling, streams, config validation |
| `test_mcp_gateway.py` | 20 | MCP config schema and example file, allow-list/velocity/identical-call/day-count/spend decisions, tools/list filtering, nested argument redaction; through the proxy with a fake MCP server: initialize and session/auth headers, notifications, filtered tools/list, allowed calls forwarded, recorded, priced and audited redacted, tool-level errors, denials never reaching the server, velocity and daily caps, upstream failures and missing credentials, SSE replies, forward_redacted, protocol edge cases |
| `test_semcache.py` | 12 | Embedder determinism and normalisation, number/negation guards, threshold/guard/partition/TTL/LRU, Wilson bound and pick rules, pinned bundled calibration numbers, calibration script, semantic hits through the API at $0, guard and threshold misses, isolation across teams/policies/context/parameters, disabled/creative/streaming, provider embedder and its failure, per-team opt-in and thresholds |
| `test_policy.py` | 39 | Policy schema errors, layer combination, alias/size/message limits, max_tokens reject/clamp/unset, provider and model lists, shipped regulated profile, OPA narrowing and fail-closed results, decision fingerprints; through the API: residency deny and local-only routing with no public fallback, streams, limits, body-size middleware, a fake OPA server (allow, deny, narrow, undefined, 500, garbage, down), OPA enabled without URL, engine crash, atomic reload, `/admin/policies`, cache isolation across policy changes |
| `test_privacy.py` | 30 | Every PII detector and its false-positive guards (Luhn, never-issued SSN ranges), hook order/block/failure, config validation, redaction through the API, team block, post-response hooks, opt-in redacted content log, fail-closed hooks, streaming rules, cache/policy isolation, custom hook modules, append-only hash-chained audit, admin actions audited |
| `test_keys_at_rest.py` | 7 | Application keys hashed at rest: storage, lookup, pepper, API surface, and migration of a v0.5 database with no plaintext left behind |
| `test_demo_engine.py` | 19 | The browser demo's engine under CPython (including the governance panel's policy and content stages, that its rules equal `config/policies.yaml`, the semantic-cache panel and that in-browser calibration equals the script's, and the MCP panel with config equal to `config/mcp.example.yaml`), and that the modules it loads stay free of third-party imports |
| `test_console_api.py` | 17 | Live console endpoints: simulated providers (answers, outage fallback, 502 when all down, streaming, knob validation), trace stages and order and that traces hold no prompt text, refusals naming the stage that refused, budget overrides winning over routes.yaml, admin pause/unpause and the anomaly override, config read/validate/apply (YAML and schema errors with lines, writes off by default, atomic apply changes enforcement), policy dry runs on candidate text, overview roll-up, simulated MCP server, admin key file (0600, created once), version consistency |
| `test_demo_console.py` | 10 | The console's demo engine: same stage order and trace shape as the gateway, aliases equal `config/routes.yaml` and profiles equal `evals/sim_profiles.json`, fallback/breakers, residency and clamps, exact and semantic cache, keys/holds/revocation/budgets, runaway batch job paused then overridden, overview history, config screens using the gateway's loaders (pydantic for routes) |
| `test_console_data.py` | 2 | `demo/data/*.json` equal what the eval scripts produce now, and say what is simulated vs. measured |
| `test_sample_company.py`, `test_sample_requests.py` | 12 | The sample data: generator output is current and deterministic, daily totals equal the sum of the teams, labelled fictional with stated assumptions; request costs equal tokens times the price of the model that answered, each team's cost per 1,000 requests within 30% of the usage file, month-to-date budgets, on-prem routing for regulated apps, the activity-feed stories present in the log, no prompt or completion text stored |
| `test_api.py`, `test_v04_streaming_cache.py`, `test_anomaly.py`, `test_config_and_store.py` | 26 | Fallback, clean 502s, spend caps, anomaly thresholds and auto-pause, streaming with LiteLLM's real chunk objects, cache scoping and expiry, metrics, admin auth, schema migration |

The console's **Evals** screen shows the test inventory from `demo/data/tests.json`, which counts test functions in the source (parametrized cases count once there, so its total is lower than pytest's item count).

## Console smoke test

`python scripts/demo_smoke.py --both` with Pyodide served from a local copy: **157 checks passed (113 demo, 44 live)**, **measured**. The checks cover every screen's key interaction in both modes, no console errors, no failed or third-party requests, and no horizontal scroll at 390 px or 1366 px. See [console](console.md#verification).

## Semantic cache calibration

**Measured** with `python scripts/calibrate_semcache.py` on the bundled fictional pairs with the hashing embedder, target ≤1% of hits wrong; pinned by `test_bundled_pairs_calibration_numbers`.

| 160 labelled pairs, split in half | Threshold | Hit rate | False-hit rate |
|---|---|---|---|
| Calibration half | 0.88 (chosen) | 30.9% | 0.0% (0 of 13 hits) |
| Held-out half | 0.88 | 26.3% | **9.1%** (1 of 11 hits; 95% upper bound 37.7%) |
| Held-out half, guards off | 0.88 | 26.3% | 28.6% (4 of 14 hits) |

The chosen threshold misses the 1% target on held-out pairs: a dozen hits cannot certify 1%, and the lexical embedder cannot tell "rotate" from "revoke" an API key. For that reason the cache ships disabled. A team enabling it calibrates first on pairs labelled from its own traffic, preferably with a provider embedding model. These numbers describe this embedder on this small synthetic set, not production traffic. Method and full result: [ADR 0005](adr/0005-semantic-cache-off-by-default-calibrated.md).

## Route evaluation

Routing changes are measured before they ship, offline and then on live traffic.

**Offline replay** ([router/evals.py](../router/evals.py), [scripts/eval_routes.py](../scripts/eval_routes.py)): a JSONL set of prompts with reference answers (`exact`, `contains`, `regex`; optional judge hook `module:function`) is replayed through each route with the gateway's own fallback chain and pricing, producing a cost-versus-quality report in Markdown and JSON. [evals/sample_cases.jsonl](../evals/sample_cases.jsonl) bundles 40 fictional cases (math, extraction, classification, formatting, reasoning, summary).

```bash
python scripts/eval_routes.py --aliases smart-fast,cheap-batch --out reports/eval        # simulated (default)
python scripts/eval_routes.py --provider real --aliases cheap-batch                     # refused unless every key is set
```

The default provider is a **deterministic simulation**: each deployment answers a case correctly with a probability from [evals/sim_profiles.json](../evals/sim_profiles.json) (invented numbers, not measurements of any model). It tests the harness, the routes' fallback behaviour and their pricing; it says nothing about how real models perform. **Simulated** result for the shipped routes (seed `eval-v1`, pinned by `test_shipped_routes_simulated_report_numbers`):

| Route (simulated providers) | Quality | Cost for 40 cases | p50 latency |
|---|---|---|---|
| `smart-fast` (haiku → gpt-4.1-mini → llama3.1) | 0.975 | $0.001200 | 0.659 s |
| `heavy-reasoning` (sonnet → gpt-4.1) | 0.950 | $0.003789 | 1.411 s |
| `cheap-batch` (gpt-4.1-nano → gemini flash) | 0.775 | $0.000155 | 0.305 s |
| `local-first` (llama3.1 → haiku) | 0.650 | $0.000050 | 1.243 s |

With 40 cases one case is 0.025 of quality, so a profile of 0.96 can score below one of 0.90 by chance (as `heavy-reasoning` vs `smart-fast` does here). Case sets and gate thresholds need sizing with that granularity in mind.

### Route-eval gate

[scripts/eval_gate.py](../scripts/eval_gate.py) exits 1 when an alias's quality drops more than `--max-quality-drop` (default 0.02) or its cost rises more than `--max-cost-increase` (default 10%) between two routes files or two reports, and 2 on configuration errors. Locally, swapping `smart-fast`'s first target to `gpt-4.1-nano` fails it on quality (0.975 → 0.775), and swapping it to `claude-sonnet-4-5` fails it on cost (+216%) (**simulated**; `test_cli_report_and_gate`). Example for a pull-request job (not wired into this repository's CI):

```bash
git show origin/main:config/routes.yaml > /tmp/routes_main.yaml
python scripts/eval_gate.py --routes-before /tmp/routes_main.yaml --routes-after config/routes.yaml --alias smart-fast
```

### Shadow mode

[router/shadow.py](../router/shadow.py) mirrors a share of an alias's live traffic to a candidate alias in the background (configuration: [configuration](configuration.md#shadow-mode)).

| Guarantee | Evidence |
|---|---|
| The client only ever gets the live answer; shadow failures do not affect it | `test_shadow_mirrors_to_candidate_off_the_books`, `test_shadow_failure_never_touches_the_live_path` |
| Shadow cost goes to a separate `shadow_usage` ledger (or is not recorded with `billing: suppress`), never to usage, budgets, anomaly baselines or showback | `test_shadow_mirrors_to_candidate_off_the_books`, `test_shadow_billing_suppress_cap_sampling_and_scope` |
| The mirror is admitted by the same policy as a live request for the candidate (residency holds) and gets the already-redacted messages plus any hooks the candidate's policy requires | `test_shadow_respects_the_candidates_policy`, `test_shadow_gets_redacted_content_and_required_hooks` |
| Separate circuit breakers and latency averages; `max_daily_usd` cap; streams and cache hits are not mirrored | `test_shadow_failure_never_touches_the_live_path`, `test_shadow_billing_suppress_cap_sampling_and_scope` |

`GET /admin/shadow` compares each pair: mirrored calls, errors, word-level agreement, exact-match rate, and cost and latency of shadow vs. live. Shadow calls are real provider calls; `billing: suppress` hides the cost from the ledger, not from the invoice.

## OPA policy

`opa test policies/` passed 8/8 locally on OPA 1.4.2, and the gateway was checked against a real `opa run --server` with [policies/router.rego](../policies/router.rego). CI does not run OPA.

## Sample data

The console's usage file (90 days) and 500-request log are **sample** data for the fictional Cypress Harbor Credit Union, generated from fixed seeds and stated assumptions. They are checked for internal consistency (above), not against any real institution. See [console](console.md#sample-business).
