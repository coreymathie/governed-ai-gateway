# Changelog

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
