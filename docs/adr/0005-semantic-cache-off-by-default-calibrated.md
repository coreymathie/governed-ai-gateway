# ADR 0005: Semantic cache ships off by default, with its calibration published as measured

## Status

Accepted (0.6.0); measured result revised with the credit-union pair set, again with the larger generated set, and again with the opposite-action and entity guards measured on a separate held-out set (Unreleased). Builds on [ADR 0004](0004-exact-match-cache-now-semantic-later.md). Date: 2026-10.

## Context

ADR 0004 allowed a semantic cache only with a labelled replay set, a calibrated threshold with its false-hit rate published next to the hit rate, and a per-team switch that is off by default. A false hit returns a confident answer to a different question, so the false-hit rate matters more than the hit rate.

## Decision

- `router/semcache.py` implements the cache with a pluggable embedder. The default is an offline, deterministic hashing embedder (word unigrams and bigrams, character trigrams); a provider embedding model can be configured instead (`embedder: provider`).
- A hit needs similarity at or above the threshold **and** must pass every guard, at any threshold, on every candidate at or above it (best first): both prompts contain the same numbers (digits or number words); both or neither are negated; no action meets its opposite (turn on/off, add/remove, open/close, lock/unlock, start/stop, increase/decrease, approve/deny, before/after, more/less, send/receive, deposit/withdraw, and un-/dis- forms); and no entity value is swapped within one kind (account, card, loan, payment rail, setting, role, period, weekday, month, file format, a capitalised name). The lexicon is written from general antonym and entity classes. A refusal is a cache miss, never an error. Each guard can be switched off in `semantic_cache.guards`; all are on by default.
- Entries are partitioned by team, alias, the policy decision fingerprint, the content-hook fingerprint, all earlier messages, `max_tokens`, and the embedding space. A lookup cannot return another team's or another policy's entry.
- `scripts/calibrate_semcache.py` splits the development pair set deterministically in half, picks the lowest threshold at which it and every higher threshold meet the target false-hit rate on the calibration half, and reports that threshold on the development holdout half and on a separate held-out set, with a 95% Wilson upper bound. The held-out set is written apart from the development set and is never used to choose a threshold or design a guard; once a fix has been designed on a set, that set becomes development data and a new held-out set measures the fix.
- The cache may ship on by default only if the held-out 95% upper bound meets the 1% target.
- The cache is **disabled by default**, with a per-team allow-list (`teams`) and per-team thresholds (`team_thresholds`).

### Measured result (bundled pairs, hashing embedder)

Target: at most 1% of hits wrong. Development set: 1,324 fictional pairs of credit-union member questions (680 same-meaning, 644 different), the 160 hand-written pairs of the earlier set and 1,164 written by `scripts/build_semcache_pairs.py`, many deliberately near-identical (an incoming vs an outgoing wire, the APY vs the dividend rate, "turn on" vs "turn off", including in long chat messages). Held-out set: 2,800 pairs (1,400 same-meaning) written by `scripts/build_semcache_heldout.py` with its own seed, frames, phrasings, entities and noise, before the opposite-action and entity guards were added.

| Each version at the threshold it picks on the development calibration half | Before: numbers and negation, 0.97 | After: + opposites and entities, 0.89 |
|---|---|---|
| Development calibration half | 0 of 90 hits wrong; hit rate 25.1% | 1 of 156 hits wrong; hit rate 43.2% |
| Development holdout half | 2.5% wrong (2 of 80 hits; 95% upper bound 8.7%); hit rate 24.3% | 0.8% wrong (1 of 130 hits; 95% upper bound 4.2%); hit rate 40.2% |
| Held-out set | 6.5% wrong (4 of 62 hits; 95% upper bound 15.4%); hit rate 4.1% | 5.3% wrong (28 of 531 hits; 95% upper bound 7.5%); hit rate 35.9% |
| Held-out set, guards off | 6.5% (4 of 62 hits) | 25.2% (170 of 675 hits) |

Before the fix, the wrong hits at the calibrated threshold were long messages that differ in one word ("turn on" vs "turn off" biometric sign-in; "add" vs "remove" a payable-on-death beneficiary, similarity 0.972): the lexical embedder cannot see that one word reverses the request, and the number and negation guards never fired at 0.97. After the fix those are refused at any threshold, the calibration picks 0.89, and on the held-out set the guards refuse 142 of 170 wrong hits for 2 of 505 correct ones. The held-out set lags the development holdout half (5.3% against 0.8% wrong): that gap is the measured overfitting of the fix to the pairs it was designed on. The 28 held-out wrong hits left are lookalikes the lexicon does not describe (a different share of a paycheck, "activate" vs "replace" a card, "over" vs "under" $10,000, an unlisted role or loan type, a typo in the differing word, "iPad" vs "iPhone"); they stay as known gaps. Requiring the 95% upper bound to meet 1% selects no threshold. These figures come from `test_bundled_pairs_calibration_numbers` and `test_before_fix_figures_reproduce` and describe this embedder on these sets, not production traffic. The default threshold in `config/routes.yaml` and `router/models.py` is the calibration-half pick (0.97 before the fix, 0.89 after); neither held-out set played a part in the choice.

The 160-pair set replaced an earlier generic one (shopping and SaaS questions) in the sample-data revision; on that set the same method chose 0.88 and measured 9.1% (1 of 11) held out. On the 160 pairs it chose 0.90 and measured 0.0% (0 of 10 hits; 95% upper bound 27.8%). The decision below did not depend on any one of these results.

## Consequences

### Positive

- The published result shows the control working as designed: the held-out false-hit rate (5.3%, 95% upper bound 7.5%) misses the target, and that is why the cache is off, not a hidden risk.
- A guard refusal costs a cache miss, never a wrong answer, so guards can be conservative: on the held-out set they refuse 2 of 505 correct hits.
- An embedder failure skips the cache and routes normally (the cache is an optimisation), so the cache cannot cause an outage.

### Negative

- Turning the cache on is an explicit, per-team decision, made after calibrating on pairs labelled from that team's traffic (ideally with a provider embedding model).
- Cache entries are process-local and in memory; each worker has its own, and they are lost on restart.
- Embedding calls made with `embedder: provider` are not priced or recorded in usage (gap).
- The guards are a word list: they catch the lookalikes someone wrote down and miss the rest (the 28 held-out wrong hits). The lexicon is English and tuned to retail-banking vocabulary.

## Alternatives considered

- **Enable by default at the calibrated threshold.** Rejected: on the held-out set the calibrated threshold (0.89) is wrong 5.3% of the time (95% upper bound 7.5%), above the 1% target. A default has to hold for traffic nobody has labelled.
- **Extend the lexicon until the held-out wrong hits are gone.** Rejected for this revision: tuning the guards on the held-out set would turn it into development data and leave no honest estimate. The next lexicon change is measured on a newly written held-out set.
- **A fixed, uncalibrated threshold.** Rejected in ADR 0004: the right number differs by embedding model and workload.
