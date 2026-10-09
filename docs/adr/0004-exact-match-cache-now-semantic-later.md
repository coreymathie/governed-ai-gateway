# ADR 0004: Exact-match response cache now; semantic cache only with a measured false-hit rate

## Status

Accepted. Exact match shipped in 0.4.0; the semantic cache was added in 0.6.0, off by default (see [ADR 0005](0005-semantic-cache-off-by-default-calibrated.md)). Date: 2026-10.

## Context

Repeated requests (classification prompts, templated extraction, CI runs, retries) cost money every time. A cache can serve them at $0. There are two kinds:

- **Exact match:** key = hash of alias, messages, temperature and max_tokens. A hit is, by construction, the same request.
- **Semantic:** embed the prompt and serve a cached answer when a previous prompt is "similar enough". It catches paraphrases, but a false hit serves a *confidently wrong answer to a different question*, which is worse than a cache miss. Whether it pays off depends on the similarity threshold, and that threshold is workload-specific.

## Decision

Ship exact match only (`router/routing.py`, `cache` table in `router/store.py`):

- Only deterministic requests are cached: `temperature <= policies.cache_max_temperature` (default 0.0).
- Entries are **scoped per team**, so one team never receives another team's cached completion.
- TTL via `policies.cache_ttl_seconds` (0 disables the cache).
- Hits are recorded at $0 with `saved_usd` = the original call's cost, marked `x-router-cache: hit`, counted in `router_cache_saved_usd_total`, and reported in showback (`cache_hits`, `saved_usd`).
- Hits do not consume TPM, since no provider is called.

A semantic cache is added only together with:

1. an **offline replay set** of real (or representative) prompt pairs labelled same-answer / different-answer;
2. a **threshold calibrated per alias or team** on that set, with the measured **false-hit rate** published next to the hit rate;
3. a per-team switch, off by default, and the same team scoping as the exact cache.

## Consequences

### Positive

- The exact-match cache cannot produce a false hit; savings are attributable per team in showback.
- The conditions for a semantic cache are stated in advance, so its introduction is a measured decision rather than a default (ADR 0005 records it).

### Negative

- Paraphrased repeats are missed.
- Cached completions are stored in plaintext in SQLite (`response_json`). That is sensitive data at rest (see [threat model](../threat-model.md), LLM02). Teams with that constraint keep `cache_ttl_seconds: 0` or put the store on encrypted storage.
- The cache key excludes the key ID, so keys in the same team share entries. The team is the intended unit, and this is documented.
- No exact-cache hit-rate or false-hit numbers are published, because none have been measured on real traffic.

## Alternatives considered

- **Semantic cache with a fixed threshold (e.g. cosine ≥ 0.95).** Rejected until calibrated: the right number differs by embedding model and workload, and an unmeasured threshold makes the error rate unknown.
- **Provider-side prompt caching.** Complementary: it discounts repeated prefixes inside a provider and does not replace response caching. It is not modelled in cost accounting yet.
