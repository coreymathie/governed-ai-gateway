# Corey Mathie, 2026
"""v0.4: streaming (with real LiteLLM mock chunks), response cache, metrics, schema migration."""

import json
import sqlite3

import litellm
import pytest
from fastapi.testclient import TestClient

from router import config_loader, main, store
from router.config_loader import settings

ADMIN = {"Authorization": "Bearer sk-router-admin"}
BODY = {"model": "smart-fast", "messages": [{"role": "user", "content": "hi"}]}
REAL_ACOMPLETION = litellm.acompletion


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def providers(monkeypatch):
    """Anthropic is down; OpenAI answers through LiteLLM's own mock mode (real response objects)."""
    calls = []

    async def acompletion(model, **kwargs):
        calls.append(model)
        if model.startswith("anthropic/"):
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="anthropic", model=model)
        return await REAL_ACOMPLETION(model=model, mock_response="Hello from the router stream", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def _key(client, label="web", team="product"):
    r = client.post("/admin/keys", json={"label": label, "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _sse(text: str) -> list:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


# ---------- Streaming ----------


def test_stream_falls_back_and_bills(client, providers):
    h = _key(client)
    with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=h) as r:
        assert r.status_code == 200
        assert r.headers["x-router-used-provider"] == "openai"
        events = _sse("".join(r.iter_text()))
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c.get("choices"))
    assert text == "Hello from the router stream"
    assert all(c["model"] == "openai/gpt-4.1-mini" for c in chunks)
    assert providers == ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini"]

    ok = next(r for r in store.recent_calls() if r["provider"] == "openai")
    assert ok["error"] is None and ok["completion_tokens"] > 0 and ok["cost_usd"] > 0


def test_mid_stream_failure_sends_error_event_and_records_it(client, monkeypatch):
    async def acompletion(model, **kwargs):
        real = await REAL_ACOMPLETION(model=model, mock_response="partial answer here", **kwargs)

        async def broken():
            n = 0
            async for chunk in real:
                n += 1
                if n == 2:
                    raise RuntimeError("connection reset by provider")
                yield chunk

        return broken()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=_key(client)) as r:
        events = _sse("".join(r.iter_text()))
    assert any("error" in e for e in events if e != "[DONE]") and events[-1] == "[DONE]"
    row = store.recent_calls()[0]
    assert row["error"] and "stream interrupted" in row["error"]


def test_stream_respects_spend_cap(client, providers, monkeypatch):
    monkeypatch.setattr(config_loader.current().policies, "per_key_daily_usd", 0.0000001)
    h = _key(client)
    client.post("/v1/chat/completions", json=BODY, headers=h)
    r = client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=h)
    assert r.status_code == 402


# ---------- Cache ----------


@pytest.fixture
def cache_on(monkeypatch):
    pol = config_loader.current().policies
    monkeypatch.setattr(pol, "cache_ttl_seconds", 3600)
    monkeypatch.setattr(pol, "cache_max_temperature", 0.0)


def test_deterministic_repeat_is_served_from_cache(client, providers, cache_on):
    h = _key(client)
    body = {**BODY, "temperature": 0}
    first = client.post("/v1/chat/completions", json=body, headers=h)
    providers.clear()
    second = client.post("/v1/chat/completions", json=body, headers=h)
    assert first.headers["x-router-cache"] == "miss" and second.headers["x-router-cache"] == "hit"
    assert providers == []  # no provider was called the second time
    assert second.json()["choices"][0]["message"]["content"] == first.json()["choices"][0]["message"]["content"]

    stats = client.get("/admin/usage/today", headers=ADMIN).json()["cache"]
    assert stats["hits"] == 1 and stats["saved_usd"] > 0


def test_cache_skips_creative_requests_and_other_teams(client, providers, cache_on):
    body0 = {**BODY, "temperature": 0}
    client.post("/v1/chat/completions", json=body0, headers=_key(client, team="product"))
    other_team = client.post("/v1/chat/completions", json=body0, headers=_key(client, label="b", team="data"))
    assert other_team.headers["x-router-cache"] == "miss"  # cache is scoped per team
    warm = {**BODY, "temperature": 0.7}
    h = _key(client, label="c", team="product")
    client.post("/v1/chat/completions", json=warm, headers=h)
    assert client.post("/v1/chat/completions", json=warm, headers=h).headers["x-router-cache"] == "miss"


def test_cache_entries_expire(client, providers, cache_on):
    h = _key(client)
    body = {**BODY, "temperature": 0}
    client.post("/v1/chat/completions", json=body, headers=h)
    with store.connect() as c:
        c.execute("UPDATE cache SET created_at = datetime('now', '-2 hours')")
    assert client.post("/v1/chat/completions", json=body, headers=h).headers["x-router-cache"] == "miss"


# ---------- Metrics ----------


def test_metrics_require_admin_and_count_outcomes(client, providers):
    client.post("/v1/chat/completions", json=BODY, headers=_key(client))
    assert client.get("/metrics", headers=_key(client, label="x")).status_code == 403
    text = client.get("/metrics", headers=ADMIN).text
    assert 'router_requests_total{alias="smart-fast",provider="anthropic",outcome="error"} 1' in text
    assert 'router_requests_total{alias="smart-fast",provider="openai",outcome="ok"} 1' in text
    assert "router_spend_usd_total" in text


# ---------- Migration ----------


def test_v03_database_is_upgraded_in_place(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, team TEXT, alias TEXT, provider TEXT, "
            "model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, error TEXT, ts TEXT)"
        )
        c.execute("INSERT INTO usage VALUES (1,'k','t','a','openai','m',1,1,0.5,NULL,datetime('now'))")
    monkeypatch.setattr(settings, "ROUTER_DB_PATH", str(db))
    store.init_db()
    with store.connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(usage)")}
    assert {"cached", "saved_usd"} <= cols
    assert store.spend_today() == pytest.approx(0.5)  # old rows still count
