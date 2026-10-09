# Corey Mathie, 2026
"""Policy-as-code: schema, layer combination, evaluation, OPA (fake server over real HTTP), and the API path."""

import json
import socket
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import litellm
import pytest
import yaml
from fastapi.testclient import TestClient

from router import config_loader, main, policy
from router.config_loader import settings
from router.models import RouteTarget
from router.pii import registered_hooks

ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer sk-router-admin"}
T = RouteTarget


def facts(team="digital-banking", alias="smart-fast", targets=None, **kw):
    targets = targets or [T(provider="anthropic", model="claude-haiku-4-5"), T(provider="ollama", model="llama3.1:8b")]
    return policy.RequestFacts(team=team, alias=alias, targets=targets, **kw)


# ---------- schema ----------


def test_shipped_policies_file_validates():
    ps = policy.load(yaml.safe_load((ROOT / "config" / "policies.yaml").read_text()), registered_hooks())
    assert ps.teams["regulated"].allowed_providers == ("ollama",)
    assert ps.teams["regulated"].required_hooks == ("pii_redact",)
    assert ps.defaults.max_tokens_ceiling == 4096 and not ps.opa.enabled


@pytest.mark.parametrize(
    "doc,match",
    [
        ({"nope": 1}, "unknown top-level keys"),
        ({"version": 2}, "unsupported version"),
        ({"defaults": {"max_tokenz": 5}}, "unknown keys"),
        ({"teams": {"t": {"allowed_providers": ["azure"]}}}, "unknown providers"),
        ({"defaults": {"max_tokens_mode": "truncate"}}, "'reject' or 'clamp'"),
        ({"defaults": {"max_tokens_ceiling": -1}}, "non-negative integer"),
        ({"defaults": {"max_messages": True}}, "non-negative integer"),
        ({"routes": {"a": {"deny_models": ["gpt-4.1"]}}}, "provider/model"),
        ({"teams": {"t": {"required_hooks": ["not_a_hook"]}}}, "unregistered hooks"),
        ({"teams": {"t": {"allow_aliases": "smart-fast"}}}, "list of non-empty strings"),
        ({"teams": ["t"]}, "expected a mapping"),
        ({"opa": {"timeout_s": 0}}, "between 0 and 10"),
        ({"opa": {"path": "/v1/data/x"}}, "relative data path"),
        ({"opa": {"url": "http://x"}}, "policies.opa"),
        ([1, 2], "top level must be a mapping"),
    ],
)
def test_schema_errors(doc, match):
    with pytest.raises(policy.PolicyConfigError, match=match):
        policy.load(doc, registered_hooks())


def test_empty_file_means_no_restrictions():
    d = policy.evaluate(policy.load(None), facts(max_tokens=100_000, request_bytes=10**9, messages=10**5))
    assert d.allow and len(d.targets) == 2 and d.max_tokens == 100_000 and d.rules == ["defaults"]


# ---------- combining layers ----------


def test_most_restrictive_layer_wins():
    ps = policy.load(
        {
            "defaults": {
                "max_tokens_ceiling": 4096,
                "max_tokens_mode": "clamp",
                "allowed_providers": ["anthropic", "ollama", "openai"],
            },
            "teams": {
                "t": {
                    "max_tokens_ceiling": 2048,
                    "allowed_providers": ["ollama", "openai"],
                    "deny_models": ["openai/x"],
                }
            },
            "routes": {
                "r": {
                    "max_tokens_ceiling": 8192,
                    "max_tokens_mode": "reject",
                    "allowed_providers": ["ollama", "anthropic"],
                    "deny_models": ["ollama/y"],
                    "required_hooks": ["pii_redact"],
                }
            },
        }
    )
    rule, layers = policy.effective_rule(ps, "t", "r")
    assert layers == ["defaults", "teams.t", "routes.r"]
    assert rule.max_tokens_ceiling == 2048  # a route can't raise the team's ceiling
    assert rule.max_tokens_mode == "reject"
    assert rule.allowed_providers == ("ollama",)  # intersection of all three
    assert rule.deny_models == ("openai/x", "ollama/y")
    assert rule.required_hooks == ("pii_redact",)
    rule, layers = policy.effective_rule(ps, "other", "other")
    assert layers == ["defaults"] and rule.max_tokens_mode == "clamp"


# ---------- evaluation ----------


def test_alias_size_and_message_limits():
    ps = policy.load(
        {"teams": {"t": {"allow_aliases": ["a"], "deny_aliases": ["b"], "max_request_bytes": 100, "max_messages": 2}}}
    )
    assert policy.evaluate(ps, facts("t", "z")).status == 403
    assert policy.evaluate(ps, facts("t", "b")).status == 403
    assert policy.evaluate(ps, facts("t", "a", request_bytes=101)).status == 413
    assert policy.evaluate(ps, facts("t", "a", messages=3)).status == 413
    ok = policy.evaluate(ps, facts("t", "a", request_bytes=100, messages=2))
    assert ok.allow and ok.rules == ["defaults", "teams.t"]


def test_max_tokens_ceiling_reject_clamp_and_unset():
    reject = policy.load({"defaults": {"max_tokens_ceiling": 1000}})
    d = policy.evaluate(reject, facts(max_tokens=1001))
    assert not d.allow and d.status == 400 and "ceiling of 1000" in d.reasons[0]
    unset = policy.evaluate(reject, facts(max_tokens=None))
    assert unset.allow and unset.max_tokens == 1000 and unset.max_tokens_clamped
    clamp = policy.load({"defaults": {"max_tokens_ceiling": 1000, "max_tokens_mode": "clamp"}})
    d = policy.evaluate(clamp, facts(max_tokens=5000))
    assert d.allow and d.max_tokens == 1000 and d.max_tokens_clamped
    under = policy.evaluate(clamp, facts(max_tokens=10))
    assert under.max_tokens == 10 and not under.max_tokens_clamped


def test_provider_and_model_lists_remove_targets_in_order():
    targets = [
        T(provider="anthropic", model="claude-haiku-4-5"),
        T(provider="openai", model="gpt-4.1"),
        T(provider="openai", model="gpt-4.1-mini"),
        T(provider="ollama", model="llama3.1:8b"),
    ]
    ps = policy.load(
        {
            "defaults": {"deny_models": ["openai/gpt-4.1"]},
            "teams": {"t": {"allowed_providers": ["openai", "ollama"], "allow_models": ["openai/*", "ollama/llama*"]}},
            "routes": {"r": {"allow_models": ["openai/gpt-4.1-mini", "ollama/*"]}},
        }
    )
    d = policy.evaluate(ps, facts("t", "r", targets))
    assert [f"{t.provider}/{t.model}" for t in d.targets] == ["openai/gpt-4.1-mini", "ollama/llama3.1:8b"]
    assert d.removed == [
        {"deployment": "anthropic/claude-haiku-4-5", "reason": "provider anthropic not in allowed_providers"},
        {"deployment": "openai/gpt-4.1", "reason": "matches deny_models"},
    ]
    # Every layer's allow_models must match: the route list excludes llama under a different pattern.
    narrow = policy.load(
        {"teams": {"t": {"allow_models": ["ollama/*"]}}, "routes": {"r": {"allow_models": ["openai/*"]}}}
    )
    d = policy.evaluate(narrow, facts("t", "r", targets))
    assert not d.allow and d.status == 403 and "no deployment" in d.reasons[0]


def test_regulated_profile_keeps_only_local_models():
    ps = policy.load(yaml.safe_load((ROOT / "config" / "policies.yaml").read_text()), registered_hooks())
    d = policy.evaluate(ps, facts("regulated", "smart-fast"))
    assert [t.provider for t in d.targets] == ["ollama"] and d.required_hooks == ["pii_redact"]
    assert d.max_tokens == 2048
    only_public = policy.evaluate(ps, facts("regulated", "x", [T(provider="openai", model="gpt-4.1-mini")]))
    assert only_public.status == 403
    assert policy.evaluate(ps, facts("contractors", "heavy-reasoning")).status == 403


def test_apply_opa_only_narrows_and_fails_closed():
    base = lambda: policy.evaluate(policy.load(None), facts())  # noqa: E731
    for bad in (None, {}, {"allow": "yes"}, [True], {"allow": True, "allowed_targets": "ollama/llama3.1:8b"}):
        d = policy.apply_opa(base(), bad)
        assert not d.allow and d.status == 503 and d.source == "local+opa"
    denied = policy.apply_opa(base(), {"allow": False, "reasons": ["no"]})
    assert denied.status == 403 and denied.reasons == ["no"]
    narrowed = policy.apply_opa(base(), {"allow": True, "allowed_targets": ["ollama/llama3.1:8b", "openai/never"]})
    assert [t.provider for t in narrowed.targets] == ["ollama"]
    assert narrowed.removed == [{"deployment": "anthropic/claude-haiku-4-5", "reason": "removed by OPA"}]
    assert policy.apply_opa(base(), {"allow": True, "allowed_targets": []}).status == 403
    doc = policy.opa_input(base(), facts(key_label="web"))
    assert set(doc) == {
        "team", "key_label", "alias", "targets", "max_tokens", "request_bytes", "messages", "stream",
        "required_hooks", "local_rules",
    }  # fmt: skip


def test_decision_fingerprint_tracks_what_was_permitted():
    ps = policy.load({"teams": {"reg": {"allowed_providers": ["ollama"]}}})
    a = policy.evaluate(ps, facts("digital-banking"))
    b = policy.evaluate(ps, facts("reg"))
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == policy.evaluate(ps, facts("other")).fingerprint()


# ---------- through the gateway ----------


class FakeResponse:
    def __init__(self, model):
        self._d = {
            "id": "x",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def calls(monkeypatch):
    seen = []
    fail = set()

    async def acompletion(model, messages, max_tokens=None, **kwargs):
        seen.append({"model": model, "max_tokens": max_tokens, "messages": [m["content"] for m in messages]})
        if model.split("/")[0] in fail:
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="x", model=model)
        return FakeResponse(model)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    seen_fail = seen, fail
    return seen_fail


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _setup(router_env, policies: str, routes_extra: str = ""):
    router_env.write_text(
        textwrap.dedent(
            """
            aliases:
              smart-fast:
                - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 5 }
                - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
                - { provider: ollama, model: llama3.1:8b, timeout_s: 5 }
              public-only:
                - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
            """
        )
        + textwrap.dedent(routes_extra)
    )
    Path(settings.ROUTER_POLICIES_FILE).write_text(textwrap.dedent(policies))
    config_loader.reload_all()


def _key(client, team):
    r = client.post("/admin/keys", json={"label": f"{team}-key", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _chat(client, h, alias="smart-fast", **extra):
    body = {"model": alias, "messages": [{"role": "user", "content": "mail a@example.com"}], **extra}
    return client.post("/v1/chat/completions", json=body, headers=h)


REGULATED = """
version: 1
teams:
  regulated: { allowed_providers: [ollama], required_hooks: [pii_redact], max_tokens_ceiling: 512 }
"""


def test_regulated_team_blocked_from_external_provider(client, calls, router_env):
    seen, _ = calls
    _setup(router_env, REGULATED)
    h = _key(client, "regulated")
    r = _chat(client, h, "public-only")
    assert r.status_code == 403 and "no deployment of 'public-only' is permitted" in r.json()["detail"]
    assert r.headers["x-router-policy"] == "deny" and r.headers["x-router-policy-removed"] == "openai/gpt-4.1-mini"
    assert seen == []
    rows = client.get("/admin/audit?category=policy", headers=ADMIN).json()
    assert rows[0]["action"] == "deny" and rows[0]["detail"]["status"] == 403 and rows[0]["team"] == "regulated"
    _chat(client, h, "smart-fast")
    assert client.get("/admin/audit?category=policy", headers=ADMIN).json()[0]["action"] == "allow"
    quiet = client.get("/admin/audit?exclude_allow=true", headers=ADMIN).json()
    assert "allow" not in {a["action"] for a in quiet} and "deny" in {a["action"] for a in quiet}


def test_regulated_team_routes_local_with_hooks_and_ceiling(client, calls, router_env):
    seen, fail = calls
    _setup(router_env, REGULATED)
    h = _key(client, "regulated")
    r = _chat(client, h)
    assert r.status_code == 200 and r.headers["x-router-used-provider"] == "ollama"
    assert r.headers["x-router-policy-removed"] == "anthropic/claude-haiku-4-5,openai/gpt-4.1-mini"
    assert r.headers["x-router-policy-rules"] == "defaults,teams.regulated"
    assert r.headers["x-router-policy-max-tokens"] == "512"
    assert seen == [{"model": "ollama/llama3.1:8b", "max_tokens": 512, "messages": ["mail [REDACTED:EMAIL]"]}]
    # Fallback never reaches a removed provider: with Ollama down the request fails instead.
    fail.add("ollama")
    r = _chat(client, h)
    assert r.status_code == 502 and [c["model"].split("/")[0] for c in seen[1:]] == ["ollama"]
    # Other teams keep the full route and their content untouched.
    fail.clear()
    r = _chat(client, _key(client, "digital-banking"))
    assert r.headers["x-router-used-provider"] == "anthropic" and seen[-1]["messages"] == ["mail a@example.com"]
    assert r.headers["x-router-policy"] == "allow" and "x-router-policy-removed" not in r.headers


def test_streams_are_admitted_by_the_same_policy(client, calls, router_env):
    _setup(router_env, REGULATED)
    r = _chat(client, _key(client, "regulated"), "public-only", stream=True)
    assert r.status_code == 403 and calls[0] == []


def test_max_tokens_reject_and_request_limits(client, calls, router_env):
    seen, _ = calls
    _setup(router_env, "defaults: { max_tokens_ceiling: 100, max_request_bytes: 200, max_messages: 2 }\n")
    h = _key(client, "digital-banking")
    r = _chat(client, h, max_tokens=101)
    assert r.status_code == 400 and "ceiling of 100" in r.json()["detail"]
    big = {"model": "smart-fast", "messages": [{"role": "user", "content": "x" * 300}]}
    assert client.post("/v1/chat/completions", json=big, headers=h).status_code == 413
    many = {"model": "smart-fast", "messages": [{"role": "user", "content": "x"}] * 3}
    assert client.post("/v1/chat/completions", json=many, headers=h).status_code == 413
    assert seen == []
    assert _chat(client, h, max_tokens=100).status_code == 200


def test_body_size_middleware_refuses_before_parsing(client, calls, router_env, monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_MAX_BODY_BYTES", 500)
    h = _key(client, "digital-banking")
    r = client.post(
        "/v1/chat/completions", content=b"{" + b" " * 600 + b"}", headers={**h, "content-type": "application/json"}
    )
    assert r.status_code == 413 and "larger than 500 bytes" in r.json()["detail"]

    def chunks():
        yield b'{"model": "smart-fast", "messages": ['
        yield b" " * 600
        yield b"]}"

    r = client.post("/v1/chat/completions", content=chunks(), headers={**h, "content-type": "application/json"})
    assert r.status_code == 413  # no content-length: counted while streaming in
    assert _chat(client, h).status_code == 200


class FakeOpa:
    """A stand-in OPA sidecar on a real local port: records inputs, answers with `self.result`."""

    def __init__(self):
        self.inputs: list[dict] = []
        self.result: object = {"allow": True}
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.inputs.append(
                    {"path": self.path, **json.loads(self.rfile.read(int(self.headers["content-length"])))}
                )
                body = b"not json" if outer.result == "garbage" else json.dumps({"result": outer.result}).encode()
                if outer.result is None:
                    body = b"{}"  # undefined decision
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def opa(monkeypatch):
    fake = FakeOpa()
    monkeypatch.setattr(settings, "OPA_URL", fake.url)
    yield fake
    fake.close()


def test_opa_allow_deny_narrow_and_input_has_no_content(client, calls, router_env, opa):
    seen, _ = calls
    _setup(router_env, "version: 1\nopa: { path: router/decision, timeout_s: 2 }\n")
    h = _key(client, "digital-banking")
    r = _chat(client, h)
    assert r.status_code == 200 and r.headers["x-router-policy-source"] == "local+opa"
    sent = opa.inputs[0]
    assert sent["path"] == "/v1/data/router/decision"
    assert sent["input"]["team"] == "digital-banking" and sent["input"]["targets"][0] == "anthropic/claude-haiku-4-5"
    assert "a@example.com" not in json.dumps(sent) and "messages" in sent["input"]  # a count, not content

    opa.result = {"allow": False, "reasons": ["outside business hours"]}
    r = _chat(client, h)
    assert r.status_code == 403 and "outside business hours" in r.json()["detail"]

    opa.result = {"allow": True, "allowed_targets": ["openai/gpt-4.1-mini"]}
    r = _chat(client, h)
    assert r.headers["x-router-used-provider"] == "openai"
    assert r.headers["x-router-policy-removed"] == "anthropic/claude-haiku-4-5,ollama/llama3.1:8b"
    assert len(seen) == 2


@pytest.mark.parametrize("mode", ["undefined", "http_500", "garbage", "down"])
def test_opa_failures_fail_closed(client, calls, router_env, opa, mode, monkeypatch):
    seen, _ = calls
    _setup(router_env, "version: 1\nopa: { timeout_s: 1 }\n")
    if mode == "undefined":
        opa.result = None
    elif mode == "http_500":
        opa.status = 500
    elif mode == "garbage":
        opa.result = "garbage"
    else:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            monkeypatch.setattr(settings, "OPA_URL", f"http://127.0.0.1:{s.getsockname()[1]}")
    r = _chat(client, _key(client, "digital-banking"))
    assert r.status_code == 503 and r.headers["x-router-policy"] == "deny"
    assert seen == []
    action = client.get("/admin/audit?category=policy", headers=ADMIN).json()[0]["action"]
    assert action == ("deny" if mode == "undefined" else "error")


def test_opa_enabled_without_url_refuses_everything(client, calls, router_env):
    _setup(router_env, "version: 1\nopa: { enabled: true }\n")
    r = _chat(client, _key(client, "digital-banking"))
    assert r.status_code == 503 and "OPA_URL is not set" in r.json()["detail"]


def test_policy_engine_crash_fails_closed(client, calls, router_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr("router.admission.evaluate", boom)
    r = _chat(client, _key(client, "digital-banking"))
    assert r.status_code == 503 and r.json()["detail"] == "policy decision unavailable; request refused"
    assert calls[0] == []


def test_reload_is_atomic_and_missing_explicit_file_fails(client, calls, router_env, monkeypatch, tmp_path):
    _setup(router_env, REGULATED)
    Path(settings.ROUTER_POLICIES_FILE).write_text("teams: { regulated: { allowed_providers: [azure] } }\n")
    router_env.write_text(router_env.read_text() + "  extra:\n    - { provider: openai, model: gpt-4.1 }\n")
    r = client.post("/admin/reload", headers=ADMIN)
    assert r.status_code == 400 and "unknown providers" in r.json()["detail"]
    assert "extra" not in config_loader.current().aliases  # routes weren't swapped either
    assert config_loader.current_policies().teams["regulated"].allowed_providers == ("ollama",)
    monkeypatch.setattr(settings, "ROUTER_POLICIES_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        config_loader.load_policies()


def test_admin_policies_shows_effective_rule(client, router_env):
    _setup(router_env, REGULATED)
    out = client.get("/admin/policies?team=regulated&alias=smart-fast", headers=ADMIN).json()
    assert out["effective"]["layers"] == ["defaults", "teams.regulated"]
    assert out["effective"]["rule"]["allowed_providers"] == ["ollama"] and out["opa"]["url_set"] is False
    assert client.get("/admin/policies").status_code == 401


def test_cache_does_not_cross_a_policy_change(client, calls, router_env):
    routes = "policies:\n  cache_ttl_seconds: 600\n"
    _setup(router_env, "version: 1\n", routes)
    h = _key(client, "digital-banking")
    assert _chat(client, h, temperature=0).headers["x-router-cache"] == "miss"
    assert _chat(client, h, temperature=0).headers["x-router-cache"] == "hit"
    _setup(router_env, "teams: { digital-banking: { allowed_providers: [ollama] } }\n", routes)
    r = _chat(client, h, temperature=0)
    assert r.headers["x-router-cache"] == "miss" and r.headers["x-router-used-provider"] == "ollama"
