# Corey Mathie, 2026
"""End-to-end API tests with LiteLLM's completion call stubbed (no network)."""

from datetime import UTC, datetime, timedelta

import litellm
import pytest
from fastapi.testclient import TestClient

from router import config_loader, main, store

ADMIN = {"Authorization": "Bearer sk-router-admin"}


class FakeResponse:
    def __init__(self, text: str, pt: int = 100, ct: int = 50):
        self._d = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def calls(monkeypatch):
    """Anthropic is 'down'; OpenAI answers."""
    seen = []

    async def fake_acompletion(model, **kwargs):
        seen.append(model)
        if model.startswith("anthropic/"):
            raise litellm.exceptions.APIConnectionError(
                message="internal upstream detail", llm_provider="anthropic", model=model
            )
        return FakeResponse("hello from openai")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _key(client, label="web", team="digital-banking", is_admin=False) -> dict:
    r = client.post("/admin/keys", json={"label": label, "team": team, "is_admin": is_admin}, headers=ADMIN)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['key']}"}


BODY = {"model": "smart-fast", "messages": [{"role": "user", "content": "hi"}]}


def test_requires_key(client):
    assert client.post("/v1/chat/completions", json=BODY).status_code == 401


def test_fallback_to_second_provider(client, calls):
    h = _key(client)
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200
    assert r.headers["x-router-used-provider"] == "openai"
    assert r.json()["choices"][0]["message"]["content"] == "hello from openai"
    assert calls == ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini"]

    rows = store.recent_calls()
    assert {(r["provider"], r["error"] is None) for r in rows} == {("anthropic", False), ("openai", True)}
    assert next(r for r in rows if r["provider"] == "openai")["cost_usd"] > 0  # priced via LiteLLM catalog


def test_all_providers_failing_returns_clean_502(client, monkeypatch):
    async def always_fail(model, **kwargs):
        raise RuntimeError("secret internal hostname db-7.prod")

    monkeypatch.setattr(litellm, "acompletion", always_fail)
    r = client.post("/v1/chat/completions", json=BODY, headers=_key(client))
    assert r.status_code == 502 and "db-7" not in r.text


def test_per_key_spend_cap(client, calls, monkeypatch):
    h = _key(client)
    monkeypatch.setattr(config_loader.current().policies, "per_key_daily_usd", 0.000001)
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200  # first call spends
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 402 and "per-key" in r.text


def test_auto_pause_blocks_runaway_key(client, calls, monkeypatch):
    h = _key(client, label="runaway")
    key = store.get_active_key(h["Authorization"].split()[1]).id  # usage rows reference the key id
    for hrs in range(2, 50):
        ts = (datetime.now(UTC) - timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
        store.record_call(key, "digital-banking", "smart-fast", "openai", "m", 1, 1, 0.01, ts=ts)
    store.record_call(key, "digital-banking", "smart-fast", "openai", "m", 1, 1, 0.50)  # 50x this hour

    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200  # policy off
    monkeypatch.setattr(config_loader.current().policies, "auto_pause_on_anomaly", True)
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 429 and "paused" in r.text

    anomalies = client.get("/admin/anomalies", headers=ADMIN).json()
    assert anomalies[0]["label"] == "runaway" and anomalies[0]["verdict"] == "pause"


def test_rejects_unknown_alias(client, calls):
    h = _key(client)
    r = client.post("/v1/chat/completions", json={**BODY, "model": "gpt-9"}, headers=h)
    assert r.status_code == 400 and "smart-fast" in r.text


def test_models_lists_aliases(client):
    r = client.get("/v1/models", headers=_key(client))
    assert [m["id"] for m in r.json()["data"]] == ["local-only", "smart-fast"]


def test_admin_routes_require_admin(client):
    h = _key(client)
    assert client.get("/admin/usage/today", headers=h).status_code == 403
    assert client.get("/admin/usage/today", headers=ADMIN).status_code == 200


def test_bad_reload_keeps_previous_config(client, router_env):
    router_env.write_text("aliases:\n  broken:\n    - { provider: nope, model: x }\n")
    r = client.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 400
    assert "smart-fast" in config_loader.current().aliases


def test_usage_summary(client, calls):
    h = _key(client, team="it-engineering")
    client.post("/v1/chat/completions", json=BODY, headers=h)
    u = client.get("/admin/usage/today", headers=ADMIN).json()
    assert u["calls_today"] == 2  # one failed anthropic attempt + one openai success
    assert u["per_team"]["it-engineering"] > 0 and u["total_spend_usd"] == u["per_team"]["it-engineering"]
