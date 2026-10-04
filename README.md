# multi-provider-llm-router

[![ci](https://github.com/coreymathie/multi-provider-llm-router/actions/workflows/ci.yml/badge.svg)](https://github.com/coreymathie/multi-provider-llm-router/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A small, readable **LLM gateway** that puts OpenAI, Anthropic, Gemini, and a local Ollama behind one OpenAI-compatible endpoint. It routes each request through an ordered list of providers, falls back when one fails, enforces **hard daily spend caps**, and flags keys whose spend suddenly jumps above their own baseline.

Provider calls go through [LiteLLM](https://github.com/BerriAI/litellm). This repo adds the operating layer around them: a routing file you can review in a pull request, spend caps that are checked *before* a provider is called, fraud-style **spend-anomaly detection**, and a live dashboard.

---

## What it does

- **One OpenAI-compatible endpoint**: `POST /v1/chat/completions`. Change the base URL in your existing OpenAI client and keep your code.
- **Aliases instead of model names.** Your app asks for `smart-fast`; `config/routes.yaml` decides that means Claude Haiku, then GPT-4.1 mini, then a local Llama.
- **Fallback** on any provider error, timeout, or 429. The response headers `x-router-used-provider` and `x-router-used-model` tell you which one actually answered.
- **Spend caps per key and per team**, checked before any provider call. A runaway client stops spending at its cap.
- **Spend-anomaly detection.** Each key's spend this hour is compared with its own 7-day hourly baseline: flagged at 3x, paused at 10x if `auto_pause_on_anomaly` is on. It's the same velocity idea used in transaction fraud monitoring, applied to API spend.
- **Dashboard** at `/dashboard`: spend and calls today, provider error rates over the last hour, anomalies, spend by team and key, and recent calls.
- **Streaming** (`stream: true`) with fallback before the first byte. If a provider fails mid-answer, the client gets a clear error event, and the partial response is still billed, even if the client disconnects.
- **Response cache** for repeated deterministic requests (temperature 0 by default), scoped per team, with a TTL. Cache hits cost $0, carry an `x-router-cache: hit` header, and the dashboard shows the dollars saved.
- **Prometheus metrics** at `/metrics`: attempts by alias, provider, and outcome; spend; cache savings.
- **Hot reload** of `routes.yaml` (`POST /admin/reload`). An invalid file is rejected and the previous config stays active.
- **Pricing check at startup.** If a configured model has no price in LiteLLM's catalog, the router logs a warning and lists it under `/health`, because unpriced calls can't count toward spend caps.

---

## Quickstart

```bash
git clone https://github.com/coreymathie/multi-provider-llm-router.git
cd multi-provider-llm-router
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # provider keys + ROUTER_ADMIN_KEY
uvicorn router.main:app --port 4000
```

Create a key for an application:

```bash
curl -X POST http://localhost:4000/admin/keys \
  -H "Authorization: Bearer $ROUTER_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"label": "web-app", "team": "product"}'
```

Use it:

```python
from openai import OpenAI

client = OpenAI(api_key="sk-router-...", base_url="http://localhost:4000/v1")
r = client.chat.completions.create(
    model="smart-fast",
    messages=[{"role": "user", "content": "Draft a two-sentence product summary."}],
)
print(r.model)                     # e.g. "anthropic/claude-haiku-4-5", or the fallback that answered
print(r.choices[0].message.content)

# Streaming works the same way
for chunk in client.chat.completions.create(model="smart-fast", messages=[...], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

Dashboard: `http://localhost:4000/dashboard` (paste the admin key; it's kept for that browser tab only).

---

## Routes file

```yaml
aliases:
  smart-fast:
    - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 20 }
    - { provider: openai,    model: gpt-4.1-mini,     timeout_s: 20 }
    - { provider: ollama,    model: llama3.1:8b,      timeout_s: 60 }

policies:
  per_key_daily_usd: 50
  per_team_daily_usd: 500
  rate_limit_rpm: 120
  auto_pause_on_anomaly: false
  cache_ttl_seconds: 3600       # 0 turns the cache off
  cache_max_temperature: 0.0    # only cache deterministic requests
```

`provider` must be `openai`, `anthropic`, `gemini`, or `ollama`; a typo fails at startup instead of mid-request. `config/regulated.yaml` is a drop-in profile that routes only to on-prem models, with **no public fallback**, for workloads where data can't leave the network.

## Admin API

| Endpoint | Purpose |
|---|---|
| `POST /admin/keys`, `GET /admin/keys`, `DELETE /admin/keys/{key}` | Manage keys |
| `GET /admin/usage/today` | Spend and calls today, by key and team; provider error rates |
| `GET /admin/anomalies` | Per-key spend this hour vs. baseline, sorted worst first |
| `GET /admin/recent` | Recent calls, including failed fallback attempts |
| `POST /admin/reload` | Reload `routes.yaml` |
| `GET /metrics` | Prometheus metrics (admin key as bearer token) |

## Tests

```bash
pip install ruff && ruff check router tests && pytest -q
```

LiteLLM's completion call is stubbed, so tests run offline. They cover fallback order, streaming (using LiteLLM's real chunk objects), mid-stream failures, the response cache (hits, savings, team scoping, expiry), clean 502s that don't leak provider errors, spend caps, anomaly thresholds and auto-pause, time-window correctness, config validation and safe reload, metrics, admin authorization, and upgrading a v0.3 database in place.

## Limitations

- Fallback for streams only happens before the first byte; once text is flowing, a failure ends the stream with an error event.
- The cache matches exact requests. Semantic (similar-question) caching isn't implemented.
- Rate limits are in memory, per process. Multiple router instances need Redis.
- SQLite is fine for small and medium volume; move the store to Postgres for heavy concurrent writes.
- "Today" means the current UTC day, which is how most provider billing works. Spend caps reset at UTC midnight.
- If you outgrow this, LiteLLM's own proxy does far more, and the routes file maps over cleanly.

## License

MIT.
