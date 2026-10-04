# Architecture

```
   ┌────────────┐    HTTPS      ┌──────────────────────┐
   │  Your app  │───────────────▶│  router (:4000)       │
   └────────────┘  POST /v1/..  │                       │
                                │  1. auth + rate limit │
                                │  2. spend caps +      │
                                │     anomaly pause     │
                                │  3. load alias        │
                                │  4. walk providers    │
                                │     (fallback loop)   │
                                │  5. record usage      │
                                └──────┬────────────────┘
                                       │
       ┌───────────────┬───────────────┼───────────────┬───────────────┐
       ▼               ▼               ▼               ▼               ▼
   ┌────────┐     ┌──────────┐    ┌─────────┐     ┌─────────┐    ┌─────────┐
   │ OpenAI │     │ Anthropic│    │  Gemini │     │ Ollama  │    │  ...    │
   └────────┘     └──────────┘    └─────────┘     └─────────┘    └─────────┘

  Any failure (error / timeout / 429)  →  next target in the alias list
```

## Design choices

**One alias, ordered targets.** No ML-based routing — a config file you can read says "try X first, then Y, then Z." Boring. Operations teams can diff it in a PR, roll it back, review it.

**Fail-closed on spend caps.** The daily per-key and per-team dollar caps are checked *before* a provider is called. A runaway loop in a client app pays nothing after it hits its cap.

**SQLite by default.** For most teams this gateway handles <1M calls/month and SQLite is plenty. The store module is small and self-contained; swapping in Postgres means rewriting one file behind the same functions.

**x-router-used-* headers.** On every response, the headers tell you which provider and model actually served the call. Debugging a weird answer? First check if the primary failed over.

**Reload without restart.** `POST /admin/reload` re-reads `routes.yaml`. Flip a flaky provider out of first position without downtime.

**Spend anomalies, fraud-style.** Each key's spend this hour is compared with its own 7-day hourly baseline. 3x is flagged on the dashboard; 10x is paused at the gateway when `auto_pause_on_anomaly` is on. A key with less than 24 hours of history isn't judged. This is the velocity logic used in transaction monitoring, pointed at API spend.
