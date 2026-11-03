# ADR 0005: Semantic cache ships off by default, with its calibration published as measured

## Status

Accepted (0.6.0); measured result revised with the credit-union pair set and again with the larger generated set (Unreleased). Builds on [ADR 0004](0004-exact-match-cache-now-semantic-later.md). Date: 2026-10.

## Context

ADR 0004 allowed a semantic cache only with a labelled replay set, a calibrated threshold with its false-hit rate published next to the hit rate, and a per-team switch that is off by default. A false hit returns a confident answer to a different question, so the false-hit rate matters more than the hit rate.

## Decision

- `router/semcache.py` implements the cache with a pluggable embedder. The default is an offline, deterministic hashing embedder (word unigrams and bigrams, character trigrams); a provider embedding model can be configured instead (`embedder: provider`).
- A hit needs similarity at or above the threshold **and** must pass two guards: both prompts contain the same numbers, and both or neither are negated.
- Entries are partitioned by team, alias, the policy decision fingerprint, the content-hook fingerprint, all earlier messages, `max_tokens`, and the embedding space. A lookup cannot return another team's or another policy's entry.
- `scripts/calibrate_semcache.py` splits a labelled pair set deterministically in half, picks the lowest threshold at which it and every higher threshold meet the target false-hit rate on the calibration half, and reports that threshold on the held-out half, with a 95% Wilson upper bound.
- The cache is **disabled by default**, with a per-team allow-list (`teams`) and per-team thresholds (`team_thresholds`).

### Measured result (bundled pairs, hashing embedder)

1,324 fictional pairs of credit-union member questions (680 same-meaning, 644 different): the 160 hand-written pairs of the earlier set and 1,164 written by `scripts/build_semcache_pairs.py` from question frames, many of them deliberately near-identical (an incoming vs an outgoing wire, the APY vs the dividend rate, "turn on" vs "turn off", including in long chat messages). Target: at most 1% of hits wrong.

| | Before: 160 pairs | After: 1,324 pairs |
|---|---|---|
| Calibration half: chosen threshold | 0.90 (0 of 12 hits wrong; hit rate 28.6%) | 0.97 (0 of 90 hits wrong; hit rate 25.1%) |
| Held-out half: hit rate | 26.3% (10 of 38) | 24.3% (78 of 321) |
| Held-out half: false-hit rate | 0.0% (0 of 10 hits; 95% upper bound 27.8%) | 2.5% (2 of 80 hits; 95% upper bound 8.7%) |
| Held-out, guards off | 0.0% (0 of 10 hits) | 2.5% (2 of 80 hits) |
| Held-out at 0.90 | 0.0% (0 of 10 hits) | 13.6% (19 of 140 hits) |

On the larger set the threshold picked on the calibration half misses the target on the held-out half: 2 of 80 hits are wrong, both long messages that differ in one word ("turn on" vs "turn off" biometric sign-in; "add" vs "remove" a payable-on-death beneficiary), at similarity 0.972. The lexical embedder cannot see that one word reverses the request, and the negation guard does not cover antonyms. At 0.97 no number-change pair in the set reaches the threshold, so the guards remove no wrong hits there. Requiring the 95% upper bound to meet 1% selects no threshold on either set. These figures come from `test_bundled_pairs_calibration_numbers` and describe this embedder on this set, not production traffic. The default threshold in `config/routes.yaml` and `router/models.py` moved from 0.90 to 0.97, the calibration-half pick; the held-out half played no part in that choice.

The 160-pair set replaced an earlier generic one (shopping and SaaS questions) in the sample-data revision; on that set the same method chose 0.88 and measured 9.1% (1 of 11) held out. The decision below did not depend on that particular miss.

## Consequences

### Positive

- The published result shows the control working as designed: the held-out false-hit rate (2.5%) misses the target, and that is why the cache is off, not a hidden risk.
- An embedder failure skips the cache and routes normally (the cache is an optimisation), so the cache cannot cause an outage.

### Negative

- Turning the cache on is an explicit, per-team decision, made after calibrating on pairs labelled from that team's traffic (ideally with a provider embedding model).
- Cache entries are process-local and in memory; each worker has its own, and they are lost on restart.
- Embedding calls made with `embedder: provider` are not priced or recorded in usage (gap).

## Alternatives considered

- **Enable by default at the calibrated threshold.** Rejected: the held-out false-hit rate at the calibrated threshold (2.5%, 95% upper bound 8.7%) misses the 1% target, and the previous default (0.90) is wrong 13.6% of the time on the held-out half. A default has to hold for traffic nobody has labelled.
- **A fixed, uncalibrated threshold.** Rejected in ADR 0004: the right number differs by embedding model and workload.
