# ADR 0004: Exact-match response cache now; semantic cache only with a measured false-hit rate

- Status: accepted (exact match shipped in 0.4.0; semantic cache added in 0.6.0, off by default: see [ADR 0005](0005-semantic-cache-off-by-default-calibrated.md))
- Date: 2026-10

## Context

Repeated requests (classification prompts, templated extraction, CI runs, retries) cost money every time. A cache can serve them at $0. There are two kinds:

- **Exact match:** key = hash of alias, messages, temperature and max_tokens. A hit is, by construction, the same request.
- **Semantic:** embed the prompt and serve a cached answer when a previous prompt is "similar enough". It catches paraphrases, but a false hit serves a *confidently wrong answer to a different question*, which is worse than a cache miss. Whether it pays off depends on the similarity threshold, and that threshold is workload-specific.

## Decision

Ship exact-match only (`router/routing.py`, `cache` table in `router/store.py`):

- Only deterministic requests are cached: `temperature <= policies.cache_max_temperature` (default 0.0).
- Entries are **scoped per team**, so one team never receives another team's cached completion.
- TTL via `policies.cache_ttl_seconds` (0 disables the cache).
- Hits are recorded at $0 with `saved_usd` = the original call's cost, marked `x-router-cache: hit`, counted in `router_cache_saved_usd_total`, and reported in showback (`cache_hits`, `saved_usd`).
- Hits do not consume TPM, since no provider is called.

A semantic cache will only be added together with:

1. an **offline replay set** of real (or representative) prompt pairs labelled same-answer / different-answer;
2. a **threshold calibrated per alias or team** on that set, with the measured **false-hit rate** published next to the hit rate;
3. a per-team switch, off by default, and the same team scoping as the exact cache.

## Consequences

- No false hits from the cache today, at the cost of missing paraphrased repeats.
- Cached completions are stored in plaintext in SQLite (`response_json`). That is sensitive data at rest (see [threat model](../threat-model.md), LLM02). Teams with that constraint should keep `cache_ttl_seconds: 0` or put the store on encrypted storage.
- The cache key excludes the key ID, so keys in the same team share entries. That's the intended unit (the team), and it's documented.
- No cache hit-rate or false-hit numbers are published for this repo because none have been measured on real traffic.

## Alternatives considered

- **Semantic cache with a fixed threshold (e.g. cosine ≥ 0.95).** Rejected until calibrated: the right number differs by embedding model and workload, and an unmeasured threshold makes the error rate unknown.
- **Provider-side prompt caching.** Complementary: it discounts repeated prefixes inside a provider and doesn't replace response caching. It isn't modelled in cost accounting yet.
