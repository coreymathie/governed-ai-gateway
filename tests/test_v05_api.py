# Corey Mathie, 2026
"""v0.5 end to end through the HTTP API, LiteLLM stubbed (no network, no provider keys)."""

import asyncio
import csv
import io
import textwrap

import litellm
import pytest
from fastapi.testclient import TestClient

from router import config_loader, costs, main, routing, store, telemetry
from router.models import ApiKey, ChatCompletionRequest, RouteTarget

ADMIN = {"Authorization": "Bearer sk-router-admin"}
BODY = {"model": "smart-fast", "messages": [{"role": "user", "content": "hi"}]}
REAL_ACOMPLETION = litellm.acompletion


class FakeResponse:
    def __init__(self, text="ok", pt=100, ct=50, model="m"):
        self._d = {
            "id": "x",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def anthropic_down(monkeypatch):
    calls = []

    async def acompletion(model, **kwargs):
        calls.append(model)
        if model.startswith("anthropic/"):
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="anthropic", model=model)
        return FakeResponse(model=model)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def _key(client, label="web", team="product"):
    r = client.post("/admin/keys", json={"label": label, "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _routes(router_env, extra: str, aliases: str | None = None):
    aliases = (
        aliases
        or """
    aliases:
      smart-fast:
        - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 5 }
        - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
    """
    )
    router_env.write_text(textwrap.dedent(aliases) + textwrap.dedent(extra))
    config_loader.load_routes()


# ---------- circuit breakers ----------


def test_breaker_opens_and_later_requests_skip_the_dead_provider(client, anthropic_down, router_env):
    _routes(router_env, "resilience:\n  circuit_breaker: { failure_threshold: 2, cooldown_seconds: 60 }\n")
    h = _key(client)
    for _ in range(2):
        r = client.post("/v1/chat/completions", json=BODY, headers=h)
        assert r.headers["x-router-fallback-from"] == "anthropic/claude-haiku-4-5"
    anthropic_down.clear()

    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200
    assert anthropic_down == ["openai/gpt-4.1-mini"]  # no wasted call to the dead provider
    assert r.headers["x-router-circuit-skipped"] == "anthropic/claude-haiku-4-5=open"
    assert r.headers["x-router-attempts"] == "1" and r.headers["x-router-strategy"] == "ordered"

    circuits = client.get("/admin/circuits", headers=ADMIN).json()
    state = {b["deployment"]: b["state"] for b in circuits["breakers"]}
    assert state["anthropic/claude-haiku-4-5"] == "open" and state["openai/gpt-4.1-mini"] == "closed"
    assert circuits["transitions"][0]["to"] == "open"

    text = client.get("/metrics", headers=ADMIN).text
    assert 'router_circuit_state{deployment="anthropic/claude-haiku-4-5"} 2' in text
    assert 'router_circuit_transitions_total{deployment="anthropic/claude-haiku-4-5",from="closed",to="open"} 1' in text
    assert 'router_requests_total{alias="smart-fast",provider="anthropic",outcome="skipped"} 1' in text


def test_half_open_probe_closes_the_breaker_when_provider_recovers(client, monkeypatch, router_env):
    _routes(router_env, "resilience:\n  circuit_breaker: { failure_threshold: 1, cooldown_seconds: 30 }\n")
    clock = {"t": 0.0}
    monkeypatch.setattr(routing.breakers, "clock", lambda: clock["t"])
    healthy = {"anthropic": False}

    async def acompletion(model, **kwargs):
        if model.startswith("anthropic/") and not healthy["anthropic"]:
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="anthropic", model=model)
        return FakeResponse(model=model)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    h = _key(client)
    client.post("/v1/chat/completions", json=BODY, headers=h)  # opens
    healthy["anthropic"] = True
    assert client.post("/v1/chat/completions", json=BODY, headers=h).headers["x-router-used-provider"] == "openai"
    clock["t"] = 31  # cooldown over -> half-open -> probe succeeds -> closed
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.headers["x-router-used-provider"] == "anthropic"
    moves = [(t["from"], t["to"]) for t in routing.breakers.recent_transitions()][::-1]
    assert moves == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_all_circuits_open_returns_503_with_retry_after(client, monkeypatch, router_env):
    _routes(router_env, "resilience:\n  circuit_breaker: { failure_threshold: 1, cooldown_seconds: 45 }\n")

    async def down(model, **kwargs):
        raise litellm.exceptions.ServiceUnavailableError(message="x", llm_provider="openai", model=model)

    monkeypatch.setattr(litellm, "acompletion", down)
    h = _key(client)
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 502
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 503 and "circuits open" in r.text
    assert 40 <= int(r.headers["retry-after"]) <= 45
    assert 'router_rejections_total{reason="circuit_open"} 1' in client.get("/metrics", headers=ADMIN).text


def test_provider_429_retry_after_is_honoured(client, monkeypatch):
    import httpx

    async def acompletion(model, **kwargs):
        if model.startswith("anthropic/"):
            resp = httpx.Response(429, headers={"retry-after": "20"}, request=httpx.Request("POST", "http://x"))
            raise litellm.exceptions.RateLimitError(
                message="slow down", llm_provider="anthropic", model=model, response=resp
            )
        return FakeResponse(model=model)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    client.post("/v1/chat/completions", json=BODY, headers=_key(client))
    b = routing.breakers.get("anthropic/claude-haiku-4-5")
    assert b.current_state() == "open" and 19 <= b.retry_in() <= 20


def test_bad_request_does_not_trip_breaker(client, monkeypatch, router_env):
    _routes(router_env, "resilience:\n  circuit_breaker: { failure_threshold: 1 }\n")

    async def acompletion(model, **kwargs):
        raise litellm.exceptions.BadRequestError(message="context too long", llm_provider="openai", model=model)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client)).status_code == 502
    assert all(b["state"] == "closed" for b in routing.breakers.snapshot())


# ---------- latency strategy ----------


def test_latency_route_prefers_the_faster_provider(client, router_env, monkeypatch):
    _routes(
        router_env,
        "resilience:\n  latency: { min_samples: 1 }\n",
        aliases="""
        aliases:
          smart-fast:
            strategy: latency
            targets:
              - { provider: anthropic, model: claude-haiku-4-5 }
              - { provider: openai, model: gpt-4.1-mini }
          ordered:
            - { provider: anthropic, model: claude-haiku-4-5 }
            - { provider: openai, model: gpt-4.1-mini }
        """,
    )

    async def ok(model, **kwargs):
        return FakeResponse(model=model)

    monkeypatch.setattr(litellm, "acompletion", ok)
    routing.sync_config()
    routing.latency.observe("anthropic/claude-haiku-4-5", 2.0)
    routing.latency.observe("openai/gpt-4.1-mini", 0.4)
    h = _key(client)
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.headers["x-router-used-provider"] == "openai" and r.headers["x-router-strategy"] == "latency"
    r = client.post("/v1/chat/completions", json={**BODY, "model": "ordered"}, headers=h)
    assert r.headers["x-router-used-provider"] == "anthropic"  # default behaviour unchanged
    assert "router_provider_latency_ewma_seconds" in client.get("/metrics", headers=ADMIN).text
    assert client.get("/admin/circuits", headers=ADMIN).json()["strategies"] == {
        "ordered": "ordered",
        "smart-fast": "latency",
    }


def test_streaming_records_ttft_and_routing_headers(client, monkeypatch):
    async def acompletion(model, **kwargs):
        if model.startswith("anthropic/"):
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="anthropic", model=model)
        return await REAL_ACOMPLETION(model=model, mock_response="streamed", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=_key(client)) as r:
        "".join(r.iter_text())
        assert r.headers["x-router-fallback-from"] == "anthropic/claude-haiku-4-5"
        assert r.headers["x-router-attempts"] == "2"
    assert routing.latency.samples("openai/gpt-4.1-mini", "ttft") == 1


def test_stream_closed_early_is_still_recorded():
    """Client disconnect closes the SSE generator; the finally block must still record usage."""
    key = store.create_key("s", team="product")
    api_key = ApiKey(**key.model_dump(exclude={"key"}))
    body = ChatCompletionRequest(**{**BODY, "stream": True})
    target = RouteTarget(provider="openai", model="gpt-4.1-mini")

    class Chunk(dict):
        def model_dump(self):
            return dict(self)

    def chunk(text):
        return Chunk(choices=[{"delta": {"content": text}}])

    async def more():
        for word in ["two ", "three ", "four "]:
            yield chunk(word)

    async def scenario():
        gen = routing._relay(api_key, body, target, chunk("one "), more().__aiter__())
        await gen.__anext__()
        await gen.__anext__()
        await gen.aclose()  # what Starlette does when the client goes away

    asyncio.run(scenario())
    row = store.recent_calls()[0]
    assert row["provider"] == "openai" and row["completion_tokens"] > 0 and row["error"] is None


# ---------- tokens per minute ----------


def test_team_tpm_limit_rejects_with_retry_after_and_settles_to_real_usage(client, router_env, monkeypatch):
    _routes(router_env, "policies:\n  per_team_tpm: 1000\n  default_output_tokens_estimate: 200\n")

    async def ok(model, **kwargs):
        return FakeResponse(model=model, pt=100, ct=50)

    monkeypatch.setattr(litellm, "acompletion", ok)
    h = _key(client, team="product")
    big = {**BODY, "max_tokens": 600}
    assert client.post("/v1/chat/completions", json=big, headers=h).status_code == 200
    # Estimate (~600+) was corrected to the real 150 tokens, so the next request fits.
    assert routing.tpm.used("team:product") == 150
    assert client.post("/v1/chat/completions", json=big, headers=h).status_code == 200
    r = client.post("/v1/chat/completions", json={**BODY, "max_tokens": 900}, headers=h)
    assert r.status_code == 429 and "per-team token rate limit" in r.text
    assert 1 <= int(r.headers["retry-after"]) <= 60
    other_team = _key(client, label="d", team="data")
    assert client.post("/v1/chat/completions", json=big, headers=other_team).status_code == 200
    assert 'router_rejections_total{reason="tpm_team"} 1' in client.get("/metrics", headers=ADMIN).text
    assert "router_tokens_total" in client.get("/metrics", headers=ADMIN).text


def test_tpm_reservation_released_when_all_providers_fail(client, router_env, monkeypatch):
    _routes(router_env, "policies:\n  per_key_tpm: 5000\n")

    async def down(model, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(litellm, "acompletion", down)
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client)).status_code == 502
    assert sum(routing.tpm.snapshot().values()) == 0


def test_per_key_tpm_override_by_label(client, router_env, anthropic_down):
    _routes(router_env, "budgets:\n  keys:\n    tiny: { tpm: 10 }\n")
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client, label="tiny")).status_code == 429
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client, label="normal")).status_code == 200


# ---------- budget hierarchy ----------


def test_org_daily_cap_blocks_every_team(client, router_env, anthropic_down):
    _routes(router_env, "budgets:\n  org: { daily_usd: 0.0000001 }\n")
    a = _key(client, team="product")
    assert client.post("/v1/chat/completions", json=BODY, headers=a).status_code == 200
    r = client.post("/v1/chat/completions", json=BODY, headers=_key(client, label="x", team="data"))
    assert r.status_code == 402 and "org daily spend cap reached" in r.text
    assert 'router_rejections_total{reason="budget_org_daily"} 1' in client.get("/metrics", headers=ADMIN).text


def test_team_monthly_override(client, router_env, anthropic_down):
    _routes(router_env, "budgets:\n  teams:\n    data: { monthly_usd: 0.0000001 }\n")
    d = _key(client, team="data")
    client.post("/v1/chat/completions", json=BODY, headers=d)
    r = client.post("/v1/chat/completions", json=BODY, headers=d)
    assert r.status_code == 402 and "per-team monthly" in r.text
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client, team="product")).status_code == 200


def test_budgets_endpoint_reports_utilization(client, router_env, anthropic_down):
    _routes(router_env, "budgets:\n  org: { daily_usd: 10, monthly_usd: 100 }\n  teams:\n    ops: { daily_usd: 1 }\n")
    client.post("/v1/chat/completions", json=BODY, headers=_key(client, team="ops"))
    b = client.get("/admin/budgets", headers=ADMIN).json()
    assert b["org"]["daily"]["cap_usd"] == 10 and b["org"]["daily"]["spent_usd"] > 0
    assert b["teams"]["ops"]["daily"]["cap_usd"] == 1
    assert client.get("/admin/budgets", headers=_key(client, label="n")).status_code == 403


# ---------- showback ----------


def test_showback_json_and_csv(client, anthropic_down):
    h = _key(client, label="web", team="product")
    raw_key = h["Authorization"].split()[1]
    for _ in range(3):
        client.post("/v1/chat/completions", json=BODY, headers=h)
    client.post("/v1/chat/completions", json=BODY, headers=_key(client, label="etl", team="data"))

    j = client.get("/admin/showback", headers=ADMIN).json()
    assert j["group_by"] == ["team", "key", "provider", "model"] and j["period"]["timezone"] == "UTC"
    ok = next(r for r in j["rows"] if r["team"] == "product" and r["provider"] == "openai")
    assert ok["requests"] == 3 and ok["cost_usd"] > 0 and ok["cost_per_1k_requests"] > 0
    failed = next(r for r in j["rows"] if r["provider"] == "anthropic" and r["team"] == "product")
    assert failed["failed_attempts"] == 3 and failed["requests"] == 0
    assert j["totals"]["requests"] == 4

    by_team = client.get("/admin/showback?group_by=team&format=csv", headers=ADMIN)
    assert by_team.headers["content-type"].startswith("text/csv")
    assert "attachment" in by_team.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(by_team.text)))
    assert {r["team"] for r in rows} == {"product", "data"}
    assert raw_key not in by_team.text and raw_key not in str(j)


def test_showback_validation_and_auth(client):
    assert client.get("/admin/showback", headers=_key(client)).status_code == 403
    assert client.get("/admin/showback?group_by=password", headers=ADMIN).status_code == 400
    assert client.get("/admin/showback?start=2026-13-01", headers=ADMIN).status_code == 400
    assert client.get("/admin/showback?start=2026-05-02&end=2026-05-01", headers=ADMIN).status_code == 400
    empty = client.get("/admin/showback?start=2020-01-01&end=2020-01-31", headers=ADMIN).json()
    assert empty["rows"] == [] and empty["totals"]["requests"] == 0


# ---------- config ----------


def test_shipped_route_files_validate():
    from pathlib import Path

    import yaml

    from router.models import RouterConfig

    root = Path(__file__).resolve().parent.parent / "config"
    for name in ("routes.yaml", "regulated.yaml"):
        cfg = RouterConfig.model_validate(yaml.safe_load((root / name).read_text()))
        assert cfg.aliases


def test_alias_mapping_form_and_validation(router_env):
    _routes(
        router_env,
        "",
        aliases="aliases:\n  a: { strategy: latency, targets: [ { provider: openai, model: m } ] }\n  b:\n"
        "    - { provider: ollama, model: x }\n",
    )
    cfg = config_loader.current()
    assert cfg.strategy("a") == "latency" and cfg.strategy("b") == "ordered"
    assert [t.provider for t in cfg.aliases["a"]] == ["openai"]
    for bad in (
        "aliases:\n  a: { strategy: fastest, targets: [ { provider: openai, model: m } ] }\n",
        "aliases:\n  a: { strateg: latency, targets: [] }\n",
        "aliases:\n  a: []\n",
        "aliases:\n  a: [ { provider: openai, model: m } ]\nstrategies: { zzz: latency }\n",
        "aliases:\n  a: [ { provider: openai, model: m } ]\n"
        "prices: { gpt: { input_usd_per_1m: 1, output_usd_per_1m: 1 } }\n",
    ):
        router_env.write_text(bad)
        with pytest.raises(ValueError):
            config_loader.load_routes()


def test_price_overrides_take_precedence(client, router_env, monkeypatch):
    _routes(
        router_env,
        "prices:\n  ollama/llama3.1:8b: { input_usd_per_1m: 1.0, output_usd_per_1m: 2.0 }\n",
        aliases="aliases:\n  local:\n    - { provider: ollama, model: llama3.1:8b }\n",
    )

    async def ok(model, **kwargs):
        return FakeResponse(model=model, pt=1_000_000, ct=500_000)

    monkeypatch.setattr(litellm, "acompletion", ok)
    client.post("/v1/chat/completions", json={**BODY, "model": "local"}, headers=_key(client))
    assert store.recent_calls()[0]["cost_usd"] == pytest.approx(2.0)  # 1M*$1 + 0.5M*$2 per 1M
    assert costs.is_priced("ollama", "llama3.1:8b")


# ---------- OpenTelemetry ----------


@pytest.fixture
def spans():
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = memory.InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    telemetry.configure(provider)
    yield exporter
    telemetry.configure(None)


def test_genai_spans_for_each_attempt(client, anthropic_down, spans):
    client.post("/v1/chat/completions", json=BODY, headers=_key(client))
    got = {s.name: s for s in spans.get_finished_spans()}
    assert set(got) == {"chat claude-haiku-4-5", "chat gpt-4.1-mini"}
    failed, ok = got["chat claude-haiku-4-5"], got["chat gpt-4.1-mini"]
    assert failed.attributes["error.type"] == "APIConnectionError"
    assert failed.attributes["gen_ai.provider.name"] == "anthropic" and failed.attributes["router.attempt"] == 1
    a = ok.attributes
    assert a["gen_ai.operation.name"] == "chat" and a["gen_ai.provider.name"] == "openai"
    assert a["gen_ai.request.model"] == "gpt-4.1-mini" and a["router.alias"] == "smart-fast"
    assert a["gen_ai.usage.input_tokens"] == 100 and a["gen_ai.usage.output_tokens"] == 50
    assert a["router.cost_usd"] > 0 and a["router.attempt"] == 2
    assert ok.kind.name == "CLIENT"


def test_stream_span_ends_with_usage(client, monkeypatch, spans):
    async def acompletion(model, **kwargs):
        return await REAL_ACOMPLETION(model=model, mock_response="hello there", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=_key(client)) as r:
        "".join(r.iter_text())
    (span,) = spans.get_finished_spans()
    assert span.name == "chat claude-haiku-4-5" and span.attributes["router.stream"] is True
    assert span.attributes["gen_ai.usage.output_tokens"] > 0


def test_telemetry_is_a_noop_without_opentelemetry(client, anthropic_down, monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_trace", None)
    s = telemetry.start_chat_span("openai", "m", "a")
    s.set_usage(1, 2, 0.1)
    s.fail(RuntimeError())
    s.end()
    assert client.post("/v1/chat/completions", json=BODY, headers=_key(client)).status_code == 200
