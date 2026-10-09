# Corey Mathie, 2026
"""Live-mode console endpoints: simulated providers, request traces, key holds, budget overrides, config
validate/apply, policy dry runs, the overview roll-up and the static console mount."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from router import anomaly, config_loader, main, mock_provider, routing, store, traces
from router.config_loader import settings

ADMIN = {"Authorization": "Bearer sk-router-admin"}
BODY = {
    "model": "smart-fast",
    "messages": [{"role": "user", "content": "Summarise case C-1042 for the card-disputes queue."}],
}


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_MOCK_PROVIDERS", True)
    mock_provider.reset()
    for p in mock_provider.PROVIDERS:
        mock_provider.update(p, {"latency_ms": 0})
    traces.reset()
    yield
    mock_provider.reset()


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _key(client, label="web", team="digital-banking") -> tuple[dict, str]:
    r = client.post("/admin/keys", json={"label": label, "team": team}, headers=ADMIN)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['key']}"}, r.json()["id"]


def _stages(trace: dict) -> dict:
    return {s["stage"]: s for s in trace["stages"]}


def test_api_mode_is_public_and_says_live(client, mock):
    r = client.get("/console/api-mode")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "live" and body["mock_providers"] is True and body["product"] == "Governed AI Gateway"
    assert "key" not in json.dumps(body).replace("admin key (Settings)", "")


def test_console_static_files_are_served(client):
    r = client.get("/console/")
    assert r.status_code == 200 and "Governed AI Gateway" in r.text
    assert client.get("/console/app.js").status_code == 200


def test_mock_provider_answers_without_keys_and_traces_every_stage(client, mock):
    h, _ = _key(client)
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["model"] == "anthropic/claude-haiku-4-5"
    assert data["choices"][0]["message"]["content"].startswith("[simulated anthropic/claude-haiku-4-5]")
    trace_id = r.headers["x-router-trace-id"]
    tr = client.get(f"/admin/traces/{trace_id}", headers=ADMIN).json()
    names = [s["stage"] for s in tr["stages"]]
    assert names == [
        "auth", "policy", "budgets", "anomaly", "pre_hooks", "cache", "semantic_cache", "tpm", "chain", "settle",
        "post_hooks", "content_log",
    ]  # fmt: skip
    assert tr["status"] == 200 and tr["outcome"] == "ok" and tr["served_by"] == "anthropic/claude-haiku-4-5"
    assert tr["prompt_tokens"] > 0 and tr["cost_usd"] > 0
    assert "Summarise" not in json.dumps(tr)  # traces never hold prompt text


def test_outage_falls_back_and_trace_shows_the_failed_attempt(client, mock):
    h, _ = _key(client)
    r = client.put("/admin/mock/providers/anthropic", json={"outage": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["outage"] is True
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200 and r.json()["model"] == "openai/gpt-4.1-mini"
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    chain = _stages(tr)["chain"]
    assert [a["outcome"] for a in chain["detail"]["attempts"]] == ["error", "ok"]
    assert tr["fell_back"] is True and "failed: anthropic/claude-haiku-4-5" in chain["summary"]
    for _ in range(5):
        client.post("/v1/chat/completions", json=BODY, headers=h)
    states = {b["deployment"]: b["state"] for b in client.get("/admin/circuits", headers=ADMIN).json()["breakers"]}
    assert states["anthropic/claude-haiku-4-5"] == "open"
    assert client.post("/admin/circuits/reset", headers=ADMIN).json() == {"status": "reset"}
    assert client.get("/admin/circuits", headers=ADMIN).json()["breakers"] == []


def test_everything_down_gives_502_with_a_trace(client, mock):
    h, _ = _key(client)
    for p in ("anthropic", "openai", "ollama"):
        client.put(f"/admin/mock/providers/{p}", json={"rate_limited": True}, headers=ADMIN)
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 502
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    assert tr["outcome"] == "error" and _stages(tr)["chain"]["decision"] == "error"
    assert "settle" not in _stages(tr)


def test_mock_provider_knobs_validation(client, mock, monkeypatch):
    assert client.put("/admin/mock/providers/nope", json={"outage": True}, headers=ADMIN).status_code == 404
    assert client.put("/admin/mock/providers/openai", json={"colour": 1}, headers=ADMIN).status_code == 400
    snap = client.get("/admin/mock/providers", headers=ADMIN).json()
    assert snap["enabled"] is True and set(snap["providers"]) == set(mock_provider.PROVIDERS)
    monkeypatch.setattr(settings, "ROUTER_MOCK_PROVIDERS", False)
    assert client.put("/admin/mock/providers/openai", json={"outage": True}, headers=ADMIN).status_code == 409
    assert client.get("/admin/mock/providers", headers=h_app(client)).status_code == 403


def h_app(client):
    return _key(client, label="app-only")[0]


def test_streaming_with_mock_provider_records_settle_in_the_trace(client, mock):
    h, _ = _key(client)
    with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=h) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
        trace_id = r.headers["x-router-trace-id"]
    assert "[simulated anthropic/claude-haiku-4-5]" in "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in body.splitlines()
        if line.startswith("data: {") and json.loads(line[6:]).get("choices")
    )
    tr = client.get(f"/admin/traces/{trace_id}", headers=ADMIN).json()
    assert tr["stream"] is True and _stages(tr)["settle"]["decision"] == "pass" and tr["completion_tokens"] > 0


def test_policy_and_budget_refusals_name_the_stage(client, mock, tmp_path, monkeypatch):
    pol = tmp_path / "pol.yaml"
    pol.write_text("version: 1\nteams:\n  regulated:\n    allowed_providers: [openai]\n")
    monkeypatch.setattr(settings, "ROUTER_POLICIES_FILE", str(pol))
    monkeypatch.setattr(config_loader, "_policies", None)
    h, _ = _key(client, "reg", "regulated")
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200 and r.json()["model"] == "openai/gpt-4.1-mini"
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    assert "removed anthropic/claude-haiku-4-5" in _stages(tr)["policy"]["summary"]
    assert _stages(tr)["chain"]["detail"]["attempts"][0]["deployment"] == "openai/gpt-4.1-mini"

    client.put("/admin/budgets/team/digital-banking", json={"daily_usd": 0.0000001}, headers=ADMIN)
    hp, _ = _key(client, "web", "digital-banking")
    store.record_call("x", "digital-banking", "smart-fast", "openai", "gpt-4.1-mini", 1, 1, 0.01)
    r = client.post("/v1/chat/completions", json=BODY, headers=hp)
    assert r.status_code == 402
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    assert tr["outcome"] == "rejected" and tr["stages"][-1]["stage"] == "budgets"
    assert tr["stages"][-1]["decision"] == "deny"
    rows = client.get("/admin/traces?outcome=rejected", headers=ADMIN).json()
    assert [t["status"] for t in rows] == [402]
    assert client.get("/admin/traces?q=regulated", headers=ADMIN).json()[0]["team"] == "regulated"
    assert client.get("/admin/traces/tr_missing", headers=ADMIN).status_code == 404


def test_budget_overrides_win_over_the_routes_file_and_clear(client, mock):
    h, _ = _key(client)
    assert client.put("/admin/budgets/galaxy/x", json={"daily_usd": 1}, headers=ADMIN).status_code == 400
    assert client.put("/admin/budgets/org/acme", json={"daily_usd": 1}, headers=ADMIN).status_code == 400
    assert client.put("/admin/budgets/team/digital-banking", json={"daily_usd": -1}, headers=ADMIN).status_code == 422
    r = client.put("/admin/budgets/team/digital-banking", json={"daily_usd": 0.001, "tpm": 50000}, headers=ADMIN)
    assert r.status_code == 200
    b = client.get("/admin/budgets", headers=ADMIN).json()
    assert b["overrides"][0]["name"] == "digital-banking" and b["overrides"][0]["daily_usd"] == 0.001
    assert b["teams"]["digital-banking"]["daily"]["cap_usd"] == 0.001 and b["teams"]["digital-banking"]["tpm"] == 50000
    store.record_call("x", "digital-banking", "smart-fast", "openai", "gpt-4.1-mini", 1, 1, 0.01)
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 402
    assert client.delete("/admin/budgets/team/digital-banking", headers=ADMIN).json() == {"status": "cleared"}
    assert client.delete("/admin/budgets/team/digital-banking", headers=ADMIN).status_code == 404
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    actions = [a["action"] for a in client.get("/admin/audit?category=admin", headers=ADMIN).json()]
    assert "budget_set" in actions and "budget_cleared" in actions


def test_pause_unpause_and_anomaly_override(client, mock, monkeypatch):
    h, key_id = _key(client, "runaway")
    assert client.post(f"/admin/keys/{key_id}/pause", headers=ADMIN).json()["status"] == "paused"
    assert client.get("/admin/key-holds", headers=ADMIN).json()[key_id]["state"] == "paused"
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 429 and "paused by an admin" in r.text
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    assert tr["stages"][-1]["stage"] == "anomaly" and tr["stages"][-1]["decision"] == "deny"
    assert client.post(f"/admin/keys/{key_id}/unpause", headers=ADMIN).json()["status"] == "unpaused"
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    assert client.post(f"/admin/keys/{key_id}/unpause", headers=ADMIN).json()["status"] == "not_paused"
    assert client.post("/admin/keys/key_nope/pause", headers=ADMIN).status_code == 404

    # A runaway key the anomaly check pauses can be let through for the rest of the hour.
    for hrs in range(2, 50):
        ts = (datetime.now(UTC) - timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
        store.record_call(key_id, "digital-banking", "smart-fast", "openai", "m", 1, 1, 0.01, ts=ts)
    store.record_call(key_id, "digital-banking", "smart-fast", "openai", "m", 1, 1, 0.50)
    monkeypatch.setattr(config_loader.current().policies, "auto_pause_on_anomaly", True)
    anomaly.clear_cache()
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 429
    r = client.post(f"/admin/keys/{key_id}/unpause", headers=ADMIN).json()
    assert r["status"] == "override" and r["until"]
    anomaly.clear_cache()
    r = client.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 200
    tr = client.get(f"/admin/traces/{r.headers['x-router-trace-id']}", headers=ADMIN).json()
    assert "admin override" in _stages(tr)["anomaly"]["summary"]
    actions = [a["action"] for a in client.get("/admin/audit?category=admin", headers=ADMIN).json()]
    assert {"key_paused", "key_unpaused", "anomaly_override"} <= set(actions)


def test_config_read_validate_and_apply(client, mock, monkeypatch):
    r = client.get("/admin/config/routes", headers=ADMIN).json()
    assert "smart-fast" in r["text"] and r["writable"] is False
    assert client.get("/admin/config/secrets", headers=ADMIN).status_code == 404
    assert [c["name"] for c in client.get("/admin/config", headers=ADMIN).json()] == ["policies", "routes", "mcp"]

    bad_yaml = client.post("/admin/config/policies/validate", json={"text": "teams:\n  a: [unclosed\n"}, headers=ADMIN)
    assert bad_yaml.json()["ok"] is False and bad_yaml.json()["errors"][0]["line"] is not None
    bad_schema = client.post(
        "/admin/config/policies/validate", json={"text": "version: 1\nteams:\n  x:\n    allowed_providers: [mars]\n"},
        headers=ADMIN,
    )  # fmt: skip
    assert "unknown providers ['mars']" in bad_schema.json()["errors"][0]["message"]
    bad_routes = client.post(
        "/admin/config/routes/validate", json={"text": "aliases:\n  a:\n    - { provider: nope, model: x }\n"},
        headers=ADMIN,
    )  # fmt: skip
    err = bad_routes.json()["errors"][0]
    assert err["message"].startswith("aliases.a.0.provider") and err["line"] == 3
    good = "version: 1\nteams:\n  digital-banking:\n    deny_aliases: [local-only]\n"
    v = client.post("/admin/config/policies/validate", json={"text": good}, headers=ADMIN).json()
    assert v["ok"] is True and v["summary"]["teams"] == ["digital-banking"]

    assert client.put("/admin/config/policies", json={"text": good}, headers=ADMIN).status_code == 403
    monkeypatch.setattr(settings, "ROUTER_ALLOW_CONFIG_WRITES", True)
    before = client.post(
        "/admin/policies/dry-run", json={"teams": ["digital-banking"], "aliases": ["local-only"]}, headers=ADMIN
    )
    assert before.json()["results"][0]["allow"] is True
    r = client.put("/admin/config/policies", json={"text": "teams: [broken"}, headers=ADMIN)
    assert r.status_code == 400 and r.json()["ok"] is False
    r = client.put("/admin/config/policies", json={"text": good}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["ok"] is True
    after = client.post(
        "/admin/policies/dry-run", json={"teams": ["digital-banking"], "aliases": ["local-only"]}, headers=ADMIN
    )
    assert after.json()["results"][0]["allow"] is False and after.json()["results"][0]["status"] == 403
    h, _ = _key(client)
    assert client.post("/v1/chat/completions", json={**BODY, "model": "local-only"}, headers=h).status_code == 403
    assert config_loader.policies_path().read_text() == good
    actions = [a["action"] for a in client.get("/admin/audit?category=admin", headers=ADMIN).json()]
    assert "config_applied" in actions and "config_rejected" in actions


def test_dry_run_matrix_and_candidate_policy(client, mock):
    _key(client, "a", "digital-banking")
    r = client.post("/admin/policies/dry-run", json={}, headers=ADMIN).json()
    assert {(x["team"], x["alias"]) for x in r["results"]} == {
        ("digital-banking", "local-only"),
        ("digital-banking", "smart-fast"),
    }
    cand = "version: 1\nteams:\n  digital-banking:\n    allowed_providers: [ollama]\n    max_tokens_ceiling: 100\n"
    r = client.post(
        "/admin/policies/dry-run",
        json={"teams": ["digital-banking"], "aliases": ["smart-fast"], "max_tokens": 50, "text": cand},
        headers=ADMIN,
    ).json()
    assert r["results"][0]["allow"] is False and r["results"][0]["status"] == 403, r
    r = client.post("/admin/policies/dry-run", json={"text": "version: 7"}, headers=ADMIN)
    assert r.status_code == 400 and "unsupported version" in r.json()["errors"][0]["message"]


def test_overview_rolls_up_today(client, mock):
    h, _ = _key(client)
    for _ in range(3):
        assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    ov = client.get("/admin/overview", headers=ADMIN).json()
    assert ov["kpis"]["requests"] == 3 and ov["kpis"]["spend_usd"] > 0
    assert ov["by_team"][0]["team"] == "digital-banking" and ov["by_model"][0]["model"] == "claude-haiku-4-5"
    assert len(ov["hourly"]["today"]) == 24 and sum(ov["hourly"]["today"]) == pytest.approx(ov["kpis"]["spend_usd"])
    assert ov["traces"]["served"] == 3 and ov["mock_providers"] is True
    routes = client.get("/admin/routes", headers=ADMIN).json()
    assert routes["smart-fast"]["targets"][0] == "anthropic/claude-haiku-4-5"


def test_mock_mcp_server(client, mock, monkeypatch):
    r = client.post("/mock/mcp/cases", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in r.json()["result"]["tools"]][:2] == ["search_cases", "get_case"]
    r = client.post(
        "/mock/mcp/cases",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_case", "arguments": {"id": 1}},
        },
    )
    assert "simulated cases server" in r.json()["result"]["content"][0]["text"]
    assert client.post("/mock/mcp/cases", json={"jsonrpc": "2.0", "method": "notifications/x"}).status_code == 202
    monkeypatch.setattr(settings, "ROUTER_MOCK_PROVIDERS", False)
    assert client.post("/mock/mcp/cases", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 404


def test_admin_key_file_is_created_once_with_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "secrets" / "admin-key.txt"
    monkeypatch.setattr(settings, "ROUTER_ADMIN_KEY", "")
    monkeypatch.setattr(settings, "ROUTER_ADMIN_KEY_FILE", str(path))
    assert config_loader.ensure_admin_key() == str(path)
    first = settings.ROUTER_ADMIN_KEY
    assert first.startswith("sk-admin-") and path.read_text().strip() == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    monkeypatch.setattr(settings, "ROUTER_ADMIN_KEY", "")
    config_loader.ensure_admin_key()
    assert settings.ROUTER_ADMIN_KEY == first  # read back, not regenerated
    monkeypatch.setattr(settings, "ROUTER_ADMIN_KEY", "explicit")
    assert config_loader.ensure_admin_key() is None and settings.ROUTER_ADMIN_KEY == "explicit"


def test_traces_are_a_no_op_outside_requests_and_bounded():
    traces.stage("policy", "pass")  # no active trace: nothing happens
    traces.reset()
    traces.configure(3)
    try:
        for i in range(5):
            tok = traces.start(f"k{i}", "fp", "t", "a")
            traces.finish(200)
            traces.detach(tok)
        assert [r["key"] for r in traces.rows()] == ["k4", "k3", "k2"]
        assert traces.stats()["served"] == 3
    finally:
        traces.configure(500)
        traces.reset()
    assert routing.hierarchy(config_loader.current()).org.daily_usd is None


def test_version_is_consistent():
    import tomllib
    from pathlib import Path

    from router import __version__

    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__ == main.app.version
    assert pyproject["project"]["name"] == "governed-ai-gateway"
