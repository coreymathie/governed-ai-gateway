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

Latest local run: **308 passed, 1 skipped (309 tests)**, from 259 test functions in 22 files (pytest runs each case of a parametrized test separately, so it collects more tests than there are functions). The skipped test checks the tiktoken estimator and only runs when tiktoken can load its `cl100k_base` encoding (it downloads it on first use). Tests stub LiteLLM's completion call, so they run offline without provider keys. CI runs ruff, the format check and `pytest -q` on every push, and checks that the sample data and eval-case files are current (`scripts/generate_sample_company.py --check`, `scripts/sample_requests.py --check`, `scripts/build_eval_cases.py --check`, `scripts/build_semcache_pairs.py --check`).

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
| `test_demo_engine.py` | 21 | The browser demo's engine under CPython (including the governance panel's policy and content stages, that its rules equal `config/policies.yaml`, that its caps are `config/routes.yaml`'s divided by the stated scale, that its PII sample equals the page's, the semantic-cache panel and that in-browser calibration equals the script's, and the MCP panel with config equal to `config/mcp.example.yaml`), and that the modules it loads stay free of third-party imports |
| `test_console_api.py` | 17 | Live console endpoints: simulated providers (answers, outage fallback, 502 when all down, streaming, knob validation), trace stages and order and that traces hold no prompt text, refusals naming the stage that refused, budget overrides winning over routes.yaml, admin pause/unpause and the anomaly override, config read/validate/apply (YAML and schema errors with lines, writes off by default, atomic apply changes enforcement), policy dry runs on candidate text, overview roll-up, simulated MCP server, admin key file (0600, created once), version consistency |
| `test_demo_console.py` | 10 | The console's demo engine: same stage order and trace shape as the gateway, aliases equal `config/routes.yaml` and profiles equal `evals/sim_profiles.json`, fallback/breakers, residency and clamps, exact and semantic cache, keys/holds/revocation/budgets, runaway batch job paused then overridden, overview history, config screens using the gateway's loaders (pydantic for routes) |
| `test_demo_money.py` | 3 | Every `toLocale*` call in the console names `en-US`; KPI values and sub-lines, charts and numeric cells carry `translate="no"`; `usd()` prints `$1,100.00`, `$940`, `$5.40` and `figs()` wraps money in prose (run with Node when it is installed) |
| `test_console_data.py` | 2 | `demo/data/*.json` equal what the eval scripts produce now, and say what is simulated vs. measured |
| `test_sample_company.py`, `test_sample_requests.py` | 12 | The sample data: generator output is current and deterministic, daily totals equal the sum of the teams, labelled fictional with stated assumptions; request costs equal tokens times the price of the model that answered, each app's cost per request matching its route and token sizes, month-to-date budgets and key caps, on-prem routing for regulated apps, the activity-feed stories present in the log (the contractor refusal, the October 6 breaker opening, the October 5 on-prem timeout), no prompt or completion text stored |
| `test_sample_consistency.py` | 14 | One story across the console: spend by model equals what the request mix implies (within 1 point of share), the log's cost by model tracks it, one route per app in `config/routes.yaml`, the engine, the log and Spend; every alias and deployment in the log exists in the config; prices defined once; content hooks in the app table match the configs; the contractor refusal is `router/policy.py`'s decision; incident counts, breaker thresholds, the runaway and the August showback recomputed from the data; every day inside every org, team and key cap; volumes sized for the membership; eval cases are credit-union tasks with realistic prompt sizes, 24 or more for every app; every cited benchmark on the Spend sources panel has a URL and a quote and matches the prices used, and docs/console.md lists every row; MCP examples equal in engine and page; no generic shop, SaaS or healthcare strings in demo-facing files |
| `test_api.py`, `test_v04_streaming_cache.py`, `test_anomaly.py`, `test_config_and_store.py` | 26 | Fallback, clean 502s, spend caps, anomaly thresholds and auto-pause, streaming with LiteLLM's real chunk objects, cache scoping and expiry, metrics, admin auth, schema migration |

The console's **Evals** screen shows the test inventory from `demo/data/tests.json`, which counts test functions in the source (259; parametrized cases count once there) next to the last recorded pytest run (309 collected) and explains the difference.

## Console smoke test

`python scripts/demo_smoke.py --both` with Pyodide served from a local copy: **168 checks passed (119 demo, 44 live, 5 with the browser locale set to fr-FR)**, **measured**. The checks cover every screen's key interaction in both modes, no console errors, no failed or third-party requests, and no horizontal scroll at 390 px or 1366 px. Every mode also opens Spend with the browser locale set to fr-FR and checks that money still reads `$…` (for example `$943`, `$2.53`) with no `USD`, and that every KPI value and sub-line, chart, meter and numeric table cell carries `translate="no"`, so page translation leaves the figures alone. See [console](console.md#verification).

## Semantic cache calibration

**Measured** with `python scripts/calibrate_semcache.py` on the bundled pairs ([evals/semcache_pairs.jsonl](../evals/semcache_pairs.jsonl)) with the offline hashing embedder, target ≤1% of hits wrong; pinned by `test_bundled_pairs_calibration_numbers`. The file holds 1,324 fictional credit-union member questions: the 160 hand-written pairs of the earlier set ([evals/semcache_pairs_handwritten.jsonl](../evals/semcache_pairs_handwritten.jsonl), unchanged) and 1,164 written by [scripts/build_semcache_pairs.py](../scripts/build_semcache_pairs.py) (seeded; CI runs its `--check`). The generator fills question frames (a fee, an action and its opposite, a product and its rate type, a limit period, a closing day) in several phrasings. Same-meaning pairs are one phrasing with the noise members add (lower case, no question mark, a greeting, a typo, a contraction) or two phrasings of the same question; different-meaning pairs change one value: "turn on" and "turn off", an incoming and an outgoing wire, the APY and the dividend rate, Saturday and Sunday closing time. Some frames are long chat messages, where one changed word is a small share of the text. The calibration/holdout split hashes each pair id, so the hand-written pairs stay in the halves they were in.

```bash
python scripts/build_semcache_pairs.py --check     # the pair file is current
python scripts/calibrate_semcache.py               # pick on the calibration half, report the held-out half
```

| | Before: 160 pairs | After: 1,324 pairs |
|---|---|---|
| Pairs (same-meaning), calibration / held out | 80 (42) / 80 (38) | 678 (359) / 646 (321) |
| Threshold chosen on the calibration half | 0.90 (0 of 12 hits wrong) | 0.97 (0 of 90 hits wrong) |
| Held-out hit rate | 26.3% (10 of 38) | 24.3% (78 of 321) |
| Held-out false-hit rate | **0.0%** (0 of 10 hits; 95% upper bound 27.8%) | **2.5%** (2 of 80 hits; 95% upper bound 8.7%) |
| Held-out, guards off | 0.0% (0 of 10 hits) | 2.5% (2 of 80 hits) |
| Held-out at the previous default, 0.90 | 0.0% (0 of 10 hits) | 13.6% (19 of 140 hits; 95% upper bound 20.2%) |
| Threshold whose 95% upper bound meets 1% (`--conservative`) | none | none |

What the larger set supports: with this embedder, no threshold meets the 1% target on held-out data. The threshold picked on the calibration half (0.97, the lowest with no wrong hits there and above) had 2 wrong hits in 80 on the held-out half, both long messages that differ in one word ("turn on" and "turn off" biometric sign-in; "add" and "remove" a payable-on-death beneficiary, similarity 0.972). The previous default, 0.90, has a 13.6% held-out false-hit rate on this set. At 0.97 no number-change pair in the set reaches the threshold, so the number and negation guards change nothing here; they matter at looser thresholds. The cache therefore still ships disabled; `config/routes.yaml` and `router/models.py` now record 0.97, the calibration-half pick, as the threshold a team would start from. A team enabling it calibrates first on pairs labelled from its own traffic, preferably with a provider embedding model, and should expect lookalike requests to need more than a lexical threshold. These numbers describe this embedder on this synthetic set, not production traffic. Method and full result: [ADR 0005](adr/0005-semantic-cache-off-by-default-calibrated.md).

## Route evaluation

Routing changes are measured before they ship, offline and then on live traffic.

**Offline replay** ([router/evals.py](../router/evals.py), [scripts/eval_routes.py](../scripts/eval_routes.py)): a JSONL set of prompts with reference answers (`exact`, `contains`, `regex`; optional judge hook `module:function`) is replayed through each route with the gateway's own fallback chain and pricing, producing a cost-versus-quality report in Markdown and JSON. [evals/sample_cases.jsonl](../evals/sample_cases.jsonl) bundles 271 fictional credit-union tasks, 24 to 27 for each of the eleven apps, written by [scripts/build_eval_cases.py](../scripts/build_eval_cases.py) (seeded; CI runs its `--check`) and tagged with the app that does them: member, online-banking and mobile-banking questions answered from a knowledge base (fees, limits, menu paths, app requirements), agent-assist policy checks (fee waivers in a rolling 12 months, overdraft fees, card delivery), card-dispute tagging and Regulation E deadlines, extraction from five loan contracts, five BSA alerts, five fraud alerts (card testing, account takeover, lost or stolen card, first-party misuse), four regulatory notices, code review of a fee module and marketing rate checks. The first 40 cases are the earlier set, unchanged. Prompts carry the context the apps send (knowledge-base excerpts, procedures, loan documents, transaction histories), so they average 769 to 5,620 tokens per app, within 40% of the token sizes the sample company assumes (`test_eval_cases_are_credit_union_tasks_with_realistic_prompt_sizes`).

54 cases carry a second tag, `hard`: boundary values (an account exactly $10.00 overdrawn, exactly $10,000 in cash, a waiver one day inside the 12 months), deadlines that cross federal holidays (Labor Day, Columbus Day, Thanksgiving), lookalike figures (a certificate's 4.07% dividend rate against its 4.15% APY), questions the context cannot answer (the expected answer hands the member to member services), and code whose behaviour differs from its docstring (`round_cents` quantizes to three places, so `overdraft_fees` returns `75.000`). Every expected answer is checked against its reference, and the fixed wrong answer fails every reference (`test_bundled_cases_load_and_every_answer_passes_its_reference`).

```bash
python scripts/build_eval_cases.py --check                                               # the case file is current
python scripts/eval_routes.py --out reports/eval                                         # every alias, simulated (default)
python scripts/eval_routes.py --provider real --aliases cheap-batch                     # refused unless every key is set
```

The default provider is a **deterministic simulation**: each deployment answers a case correctly with a probability from [evals/sim_profiles.json](../evals/sim_profiles.json) (invented numbers, not measurements of any model). It tests the harness, the routes' fallback behaviour and their pricing; it says nothing about how real models perform. The simulation does not know which cases are hard, so hard cases score like the rest (`smart-fast` 0.926 on them); they measure something only with `--provider real`. **Simulated** result for the shipped routes, before and after the larger set (seed `eval-v1`, pinned by `test_shipped_routes_simulated_report_numbers`):

| Route (simulated providers) | Before: quality, 40 cases | Before: per 1,000 requests | After: quality, 271 cases | After: cost for 271 cases | After: per 1,000 requests | After: p50 latency |
|---|---|---|---|---|---|---|
| `heavy-reasoning` (sonnet → gpt-4.1) | 1.000 | $8.38 | 0.974 | $2.101042 | $7.75 | 1.419 s |
| `smart-fast` (haiku → gpt-4.1-mini → llama3.1) | 0.850 | $2.76 | 0.878 | $0.695661 | $2.57 | 0.636 s |
| `fast-chat` (latency order over haiku, gpt-4.1-mini, gemini flash) | 0.850 | $2.76 | 0.878 | $0.695661 | $2.57 | 0.636 s |
| `cheap-batch` (gpt-4.1-nano → gemini flash) | 0.625 | $0.30 | 0.705 | $0.074439 | $0.27 | 0.305 s |
| `local-first` (llama3.1 → haiku) | 0.600 | $0.14 | 0.668 | $0.035042 | $0.13 | 1.300 s |
| `regulated-fast` (llama3.1 only) | 0.600 | $0.14 | 0.668 | $0.035042 | $0.13 | 1.300 s |

The replay runs targets in configured order, so `fast-chat` scores as `smart-fast` (same first deployment) and `regulated-fast` as `local-first`. With 271 cases the simulated quality of each route lies closer to its first deployment's profile (Claude Haiku 0.90, Sonnet 0.96, GPT-4.1 nano 0.72, Llama 0.70) than with 40, where one case was 0.025 of quality; now one case is about 0.004. Per 1,000 requests, `smart-fast` costs about what the sample company's apps pay ($2.53 per 1,000 across all apps on the Spend screen).

### Route-eval gate

[scripts/eval_gate.py](../scripts/eval_gate.py) exits 1 when an alias's quality drops more than `--max-quality-drop` (default 0.02) or its cost rises more than `--max-cost-increase` (default 10%) between two routes files or two reports, and 2 on configuration errors. Locally, swapping `smart-fast`'s first target to `gpt-4.1-nano` fails it on quality, and swapping it to `claude-sonnet-4-5` fails it on cost (+200.5% on 271 cases; +203.1% on the earlier 40) (**simulated**; `test_cli_report_and_gate`). The console's example asks whether the member assistant could move from `smart-fast` to `cheap-batch`, the route the nightly fraud-alert batch uses: 89% cheaper, but quality falls 0.173 (0.878 → 0.705; on the earlier 40 cases 0.225, 0.850 → 0.625), so the gate fails. Example for a pull-request job (not wired into this repository's CI):

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

The console's usage file (90 days) and 500-request log are **sample** data for the fictional Cypress Harbor Credit Union, generated from fixed seeds and stated assumptions in [demo/cypress_harbor.py](../demo/cypress_harbor.py) and [config/routes.yaml](../config/routes.yaml). They are checked for internal consistency (above), not against any real institution. The Spend screen's sources panel lists each headline figure with the published source it is checked against or the assumption it rests on; two figures (the Claude Haiku 4.5 and Sonnet 4.5 list prices) are cited, the rest are assumptions. See [console](console.md#how-the-sample-numbers-were-set).
