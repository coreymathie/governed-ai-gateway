# ADR 0005: Semantic cache ships off by default, with its calibration published as measured

## Status

Accepted (0.6.0). Builds on [ADR 0004](0004-exact-match-cache-now-semantic-later.md). Date: 2026-10.

## Context

ADR 0004 allowed a semantic cache only with a labelled replay set, a calibrated threshold with its false-hit rate published next to the hit rate, and a per-team switch that is off by default. A false hit returns a confident answer to a different question, so the false-hit rate matters more than the hit rate.

## Decision

- `router/semcache.py` implements the cache with a pluggable embedder. The default is an offline, deterministic hashing embedder (word unigrams and bigrams, character trigrams); a provider embedding model can be configured instead (`embedder: provider`).
- A hit needs similarity at or above the threshold **and** must pass two guards: both prompts contain the same numbers, and both or neither are negated.
- Entries are partitioned by team, alias, the policy decision fingerprint, the content-hook fingerprint, all earlier messages, `max_tokens`, and the embedding space. A lookup cannot return another team's or another policy's entry.
- `scripts/calibrate_semcache.py` splits a labelled pair set deterministically in half, picks the lowest threshold at which it and every higher threshold meet the target false-hit rate on the calibration half, and reports that threshold on the held-out half, with a 95% Wilson upper bound.
- The cache is **disabled by default**, with a per-team allow-list (`teams`) and per-team thresholds (`team_thresholds`).

### Measured result (bundled pairs, hashing embedder)

160 fictional, hand-written pairs (80 same-meaning, 80 different, many of them deliberately near-identical). Target: at most 1% of hits wrong.

| | Threshold | Hit rate | False-hit rate |
|---|---|---|---|
| Calibration half (80 pairs) | 0.88 (chosen) | 30.9% (13 of 42) | 0.0% (0 of 13 hits) |
| Held-out half (80 pairs) | 0.88 | 26.3% (10 of 38) | 9.1% (1 of 11 hits; 95% upper bound 37.7%) |
| Held-out, guards off | 0.88 | 26.3% | 28.6% (4 of 14 hits) |

The held-out false hit is "How do I rotate an API key in the dashboard?" vs. "How do I revoke an API key in the dashboard?": one word changes the intent, and the lexical embedder cannot see it. The guards removed three of four wrong hits at this threshold. Requiring the 95% upper bound to meet 1% selects no threshold at all: this set is too small to certify a 1% rate. These figures come from `test_bundled_pairs_calibration_numbers` and describe this embedder on this set, not production traffic.

## Consequences

### Positive

- The published result shows the control working as designed: the measured miss against the target is the reason the cache is off, not a hidden risk.
- An embedder failure skips the cache and routes normally (the cache is an optimisation), so the cache cannot cause an outage.

### Negative

- Turning the cache on is an explicit, per-team decision, made after calibrating on pairs labelled from that team's traffic (ideally with a provider embedding model).
- Cache entries are process-local and in memory; each worker has its own, and they are lost on restart.
- Embedding calls made with `embedder: provider` are not priced or recorded in usage (gap).

## Alternatives considered

- **Enable by default at the calibrated threshold.** Rejected: the held-out false-hit rate (9.1%) misses the 1% target by a wide margin.
- **A fixed, uncalibrated threshold.** Rejected in ADR 0004: the right number differs by embedding model and workload.
