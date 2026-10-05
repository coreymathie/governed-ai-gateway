# ADR 0002: Ordered fallback with per-deployment circuit breakers (latency ordering opt-in)

- Status: accepted (breakers and latency strategy added in 0.5.0)
- Date: 2026-10

## Context

Each alias maps to an ordered list of provider deployments (`provider/model`). Before 0.5.0 the gateway tried them in order on every request. When the primary was down, every request first waited for that provider to fail (up to `timeout_s`) and only then fell back. A provider outage therefore added its full timeout to every request, and the gateway kept sending traffic to a provider that was already rate-limiting it.

We also want the option to send traffic to whichever healthy deployment is currently fastest, without giving up a reviewable, deterministic route file.

## Decision

1. **Ordered fallback stays the default.** The route file is the policy: operators can read it, diff it in a PR and roll it back. Any exception from a provider moves to the next target.
2. **One circuit breaker per deployment** (`router/breaker.py`):
   - *closed → open* after `failure_threshold` consecutive failures, **or** an error rate ≥ `error_rate_threshold` once the sliding `window_seconds` holds `min_requests` calls, **or** immediately when a 429/503 carries `Retry-After` (opened for that long, capped at `max_retry_after_seconds`).
   - *open → half-open* when the cooldown (or Retry-After) elapses.
   - *half-open*: at most `half_open_max_probes` concurrent probe requests; `success_threshold` successes close it, any failure re-opens it with a fresh cooldown.
   - Only provider-health errors count: connection errors, timeouts, 401/403/408/429 and 5xx. Other 4xx (bad request, context too long) are the caller's problem and leave the breaker alone. A cancelled request releases its probe slot without judging the provider.
3. **Skipped deployments fall through** to the next target. If every target is skipped, the gateway returns **503 with `Retry-After`** (the soonest probe time) without calling anyone.
4. **Visibility:** response headers `x-router-attempts`, `x-router-fallback-from`, `x-router-circuit-skipped`, `x-router-strategy`; `GET /admin/circuits`; Prometheus `router_circuit_state`, `router_circuit_transitions_total`; WARN log on every transition.
5. **`strategy: latency` is opt-in per alias** (`router/latency.py`). Healthy deployments are ordered by an EWMA of successful-call latency (TTFT for streams). Deployments with fewer than `min_samples` observations go first, in configured order (bounded warm-up). Deployments within `tolerance` of the fastest keep their configured order, so a near-tie never displaces a preferred primary. Breakers still apply.

The chain itself (`router/fallback.py: run_chain`) has no HTTP or SDK code; the gateway and the browser demo run the same function.

## Consequences

- During an outage, requests stop paying the dead provider's timeout once its breaker opens (the demo shows the per-request latency difference with breakers on and off; those numbers are simulated, not measured against real providers).
- Breaker, latency and TPM state are **per process**. With N workers, a provider gets up to N × `failure_threshold` failures before every worker has opened. A shared store (Redis) is roadmap.
- Half-open probes are real user requests. With `half_open_max_probes: 1` at most one request per worker pays for a failed probe per cooldown period.
- Latency ordering only learns from successful calls on that worker, and a slow deployment that is never tried again keeps a stale average until the faster ones degrade. That is acceptable for an opt-in strategy; it's why the default stays ordered.
- Streaming fallback is still limited to before the first chunk: once text reaches the client, a provider failure ends the stream with an error event (see the `routing.py` module docstring).

## Alternatives considered

- **Retry with backoff on the same provider.** Adds latency during outages; LiteLLM's own retries can still be enabled per call if wanted.
- **Health-check pings.** Cost money on paid APIs and can disagree with real request outcomes; passive breakers use the traffic we already send.
- **Weighted / cost-aware / ML routing.** Harder to reason about and review. A router evaluation harness (offline replay + unbilled shadow mode) is on the roadmap before any such strategy ships.
