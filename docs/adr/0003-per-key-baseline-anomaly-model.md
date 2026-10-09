# ADR 0003: Spend anomalies against each key's own baseline, not fixed thresholds

## Status

Accepted. Date: 2026-10.

## Context

Daily and monthly caps (`router/budgets.py`) stop a scope once it has spent its allowance. They do not notice a key that is spending *abnormally* but is still under its cap: a retry loop in a batch job, a leaked key used from somewhere else, an agent stuck in a tool-call cycle. A fixed alert threshold ("alert above $5/hour") is wrong for most keys at once: a high-volume production key crosses it every hour, while a low-volume internal tool can spend 50x its normal amount and never reach it.

Transaction-fraud monitoring solves the same problem with **velocity checks against the account's own history**: what matters is how far this hour departs from what *this* account normally does. This decision applies that pattern from card-fraud and dispute operations to AI spend.

## Decision

`router/anomaly.py` compares each key's spend this hour with its own 7-day baseline:

- **Baseline** = total successful-call spend over the previous 7 days (excluding the current hour) ÷ the number of hours that had any successful call. Averaging over *active* hours avoids diluting a key that only runs during business hours or nightly.
- **Verdicts:** `ok` below 3x, `flagged` at 3x or more (dashboard), `pause` at 10x or more.
- **Cold start:** keys with fewer than 24 active hours of history are never judged.
- **Enforcement is opt-in:** with `policies.auto_pause_on_anomaly: true`, a key at `pause` gets HTTP 429 before any provider call. The default is off, so a fresh deployment does not pause keys before baselines exist.
- `classify()` and `baseline_from_hourly()` are pure functions; the SQL queries compute the same baseline, and the browser console runs the same thresholds against a simulated history.

## Consequences

### Positive

- One rule works across keys whose normal spend differs by orders of magnitude.
- The rule is explainable in an incident: "this key spent 14x its normal hour".
- A key whose usage grows legitimately (launch day) can be flagged; that is intended. Pausing is opt-in, and an administrator can raise the key's budget or wait for the baseline to adapt.

### Negative

- The current hour is partial, so the check is conservative early in an hour and can trigger late in a heavy hour; it compares spend so far, not a projection.
- Slow drift is invisible by design: a key that grows 2x per week never crosses 3x of its trailing baseline. Budgets and monthly caps cover that.
- Evaluations are cached for 30 s per key on the request path, so a key can keep spending for up to 30 s after crossing 10x.
- Thresholds are constants in code, not per-team config. Making them configurable, and adding a seasonality-aware baseline (same hour of week), are roadmap items; neither is implemented.

## Alternatives considered

- **Fixed per-key alert thresholds.** Need hand-tuning per key and go stale.
- **Statistical models (z-score, EWMA bands, isolation forest).** Better at subtle anomalies, harder to explain to the team being paused. Multiples of the key's own baseline are easy to state in an incident.
- **Provider-side budget alerts.** Arrive after the money is spent and cannot attribute spend to a key or team inside one provider account.
