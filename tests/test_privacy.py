# Corey Mathie, 2026
"""PII detection, content hooks, the opt-in content log and the hash-chained audit trail. All data is fictional."""

import sqlite3
import sys
import textwrap

import litellm
import pytest
from fastapi.testclient import TestClient

from router import audit, config_loader, main, metrics, pii, store
from router.config_loader import settings
from router.models import RouterConfig

ADMIN = {"Authorization": "Bearer sk-router-admin"}
PII_TEXT = (
    "Customer jane.doe@example.com, phone (202) 555-0143, SSN 123-45-6789, "
    "card 4111 1111 1111 1111, key sk-test-abcdefghijklmnopqrstuvwxyz0123"
)


# ---------- detectors ----------


@pytest.mark.parametrize(
    "text,kind",
    [
        ("write to ops.team+alerts@example.co.uk today", "email"),
        ("call (202) 555-0143", "phone"),
        ("call 202-555-0143", "phone"),
        ("call +44 20 7946 0958", "phone"),
        ("ssn 123-45-6789", "ssn"),
        ("ssn 123 45 6789", "ssn"),
        ("card 4111-1111-1111-1111", "card"),
        ("card 5555555555554444", "card"),
        ("aws AKIAABCDEFGHIJKLMNOP", "secret"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "secret"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwx", "secret"),
        ("-----BEGIN RSA PRIVATE KEY-----", "secret"),
    ],
)
def test_each_detector_finds_its_kind(text, kind):
    redacted, counts = pii.redact(text)
    assert counts == {kind: 1}
    assert pii.placeholder(kind) in redacted


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1112",  # fails Luhn
        "ssn 000-12-3456 or 666-12-3456 or 900-12-3456",  # never-issued areas
        "order 12345, version 1.2.3, total 199.99, date 2026-10-04, ip 10.0.0.1, id 1234567890",
        "the sk- prefix alone is fine, so is user@localhost",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert pii.redact(text) == (text, {})


def test_redact_respects_kinds_and_never_returns_values():
    text, counts = pii.redact(PII_TEXT, ("email", "card"))
    assert counts == {"email": 1, "card": 1}
    assert "555-0143" in text and "jane.doe" not in text
    _, all_counts = pii.redact(PII_TEXT)
    assert all_counts == {"email": 1, "phone": 1, "ssn": 1, "card": 1, "secret": 1}
    assert pii.luhn_valid("4111111111111111") and not pii.luhn_valid("4111111111111112")
    with pytest.raises(ValueError):
        pii.validate_kinds(["email", "dna"])


def test_run_hooks_order_block_and_failures():
    ctx = pii.HookContext("pre_request")
    run = pii.run_hooks(["pii_detect", "pii_redact"], [PII_TEXT, "nothing here"], ctx)
    assert [a["action"] for a in run.actions] == ["detect", "redact"]
    assert "[REDACTED:EMAIL]" in run.texts[0] and run.texts[1] == "nothing here"
    blocked = pii.run_hooks(["pii_block", "pii_redact"], [PII_TEXT], ctx)
    assert blocked.blocked and blocked.blocked_by == "pii_block" and len(blocked.actions) == 1
    assert "request contains" in blocked.reason

    pii.register_hook("boom_hook", lambda texts, ctx: 1 / 0)
    pii.register_hook("bad_shape_hook", lambda texts, ctx: pii.HookOutcome([]))
    try:
        for name in ("boom_hook", "bad_shape_hook"):
            with pytest.raises(pii.HookError):
                pii.run_hooks([name], ["x"], ctx)
    finally:
        pii._HOOKS.pop("boom_hook")
        pii._HOOKS.pop("bad_shape_hook")
    assert pii.resolve_hooks(["a", "b"], ["b", "c"], []) == ["a", "b", "c"]
    with pytest.raises(ValueError):
        pii.register_hook("Bad-Name", lambda t, c: pii.HookOutcome(t))


def test_privacy_config_validation():
    base = {"aliases": {"a": [{"provider": "ollama", "model": "m"}]}}
    with pytest.raises(ValueError, match="unknown hooks"):
        RouterConfig.model_validate({**base, "privacy": {"default": {"pre_request": ["nope"]}}})
    with pytest.raises(ValueError, match="unknown aliases"):
        RouterConfig.model_validate({**base, "privacy": {"routes": {"zzz": {"pre_request": ["pii_redact"]}}}})
    with pytest.raises(ValueError):
        RouterConfig.model_validate({**base, "privacy": {"kinds": ["dna"]}})
    cfg = RouterConfig.model_validate(
        {
            **base,
            "privacy": {
                "default": {"pre_request": ["pii_detect"]},
                "routes": {"a": {"pre_request": ["pii_redact"]}},
                "teams": {"t": {"pre_request": ["pii_block"], "log_content": True}},
            },
        }
    )
    assert cfg.privacy.hooks_for("t", "a", "pre_request") == ["pii_detect", "pii_redact", "pii_block"]
    assert cfg.privacy.hooks_for("other", "b", "pre_request") == ["pii_detect"]
    assert cfg.privacy.logs_content("t") and not cfg.privacy.logs_content("other")


# ---------- through the gateway ----------


class FakeResponse:
    def __init__(self, text="ok"):
        self._d = {
            "id": "x",
            "object": "chat.completion",
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def provider(monkeypatch):
    """Records the messages each provider call received; replies with `reply`."""
    seen = {"calls": [], "reply": "ok"}

    async def acompletion(model, messages, **kwargs):
        seen["calls"].append([m["content"] for m in messages])
        return FakeResponse(seen["reply"])

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return seen


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _key(client, team="product"):
    r = client.post("/admin/keys", json={"label": f"{team}-app", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _privacy(router_env, block: str, policies: str = ""):
    router_env.write_text(
        textwrap.dedent(
            """
            aliases:
              smart-fast:
                - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
            """
        )
        + textwrap.dedent(policies)
        + textwrap.dedent(block)
    )
    config_loader.load_routes()


def _chat(client, h, content=PII_TEXT, **extra):
    return client.post(
        "/v1/chat/completions",
        json={"model": "smart-fast", "messages": [{"role": "user", "content": content}], **extra},
        headers=h,
    )


def test_defaults_send_content_unchanged_and_store_no_content(client, provider, router_env):
    _privacy(router_env, "")
    r = _chat(client, _key(client))
    assert r.status_code == 200
    assert provider["calls"] == [[PII_TEXT]]
    assert "x-router-content-hooks" not in r.headers
    assert client.get("/admin/content-log", headers=ADMIN).json() == []
    assert client.get("/admin/audit?category=privacy", headers=ADMIN).json() == []


def test_route_redaction_reaches_provider_redacted_and_is_audited(client, provider, router_env):
    _privacy(router_env, "privacy:\n  routes:\n    smart-fast: { pre_request: [pii_redact] }\n")
    r = _chat(client, _key(client))
    assert r.status_code == 200
    sent = provider["calls"][0][0]
    for raw in ("jane.doe@example.com", "555-0143", "123-45-6789", "4111 1111", "sk-test-"):
        assert raw not in sent
    assert sent.count("[REDACTED:") == 5
    assert r.headers["x-router-content-hooks"] == "pii_redact"
    assert r.headers["x-router-redactions"] == "card=1,email=1,phone=1,secret=1,ssn=1"
    rows = client.get("/admin/audit?category=privacy", headers=ADMIN).json()
    assert len(rows) == 1 and rows[0]["action"] == "redact" and rows[0]["team"] == "product"
    assert rows[0]["detail"]["findings"]["email"] == 1 and rows[0]["key_fp"]
    raw_dump = str(rows)
    assert "jane.doe" not in raw_dump and "sk-test" not in raw_dump and "sk-router" not in raw_dump
    chain = client.get("/admin/audit/verify", headers=ADMIN).json()
    assert chain["ok"] and chain["rows"] == 3  # key creation (admin), policy allow, redaction
    assert 'router_policy_actions_total{category="privacy",action="redact"} 1' in metrics.render()


def test_team_block_refuses_before_any_provider_call(client, provider, router_env):
    _privacy(router_env, "privacy:\n  teams:\n    support: { pre_request: [pii_block] }\n")
    r = _chat(client, _key(client, "support"))
    assert r.status_code == 422 and "blocked by content policy" in r.json()["detail"]
    assert "jane.doe" not in r.text
    assert provider["calls"] == []
    assert _chat(client, _key(client, "product")).status_code == 200  # other teams unaffected
    assert _chat(client, _key(client, "support"), content="no personal data here").status_code == 200
    assert 'router_rejections_total{reason="privacy_block_request"} 1' in metrics.render()


def test_post_response_redact_and_block(client, provider, router_env):
    _privacy(router_env, "privacy:\n  default: { post_response: [pii_redact] }\n")
    provider["reply"] = "Sure, contact jane.doe@example.com"
    r = _chat(client, _key(client), content="who do I contact?")
    assert r.json()["choices"][0]["message"]["content"] == "Sure, contact [REDACTED:EMAIL]"
    assert r.headers["x-router-redactions"] == "email=1"

    _privacy(router_env, "privacy:\n  default: { post_response: [pii_block] }\n")
    h = _key(client, "data")
    r = _chat(client, h, content="who do I contact?")
    assert r.status_code == 422 and "response withheld" in r.json()["detail"]
    # The provider was paid, so the call is still on the ledger.
    assert any(c["team"] == "data" and c["cost_usd"] >= 0 for c in client.get("/admin/recent", headers=ADMIN).json())


def test_content_log_is_opt_in_per_team_and_always_redacted(client, provider, router_env):
    _privacy(router_env, "privacy:\n  teams:\n    support: { log_content: true }\n")
    provider["reply"] = "Reach me at agent@example.com"
    _chat(client, _key(client, "product"))
    assert client.get("/admin/content-log", headers=ADMIN).json() == []
    _chat(client, _key(client, "support"))
    # No redaction hook is configured, so the provider saw the raw text, but the stored copy is redacted.
    assert provider["calls"][-1] == [PII_TEXT]
    rows = client.get("/admin/content-log?team=support", headers=ADMIN).json()
    assert len(rows) == 1
    assert "jane.doe" not in rows[0]["request_redacted"] and "[REDACTED:EMAIL]" in rows[0]["request_redacted"]
    assert rows[0]["response_redacted"] == "Reach me at [REDACTED:EMAIL]"
    assert '"email": 1' in rows[0]["redactions"]
    assert [a["action"] for a in client.get("/admin/audit?category=privacy", headers=ADMIN).json()] == [
        "content_logged"
    ]


def test_failing_hook_fails_closed(client, provider, router_env):
    pii.register_hook("flaky_hook", lambda texts, ctx: 1 / 0)
    try:
        _privacy(router_env, "privacy:\n  default: { pre_request: [flaky_hook] }\n")
        r = _chat(client, _key(client))
        assert r.status_code == 503 and "content policy hook failed" in r.json()["detail"]
        assert provider["calls"] == []
        assert client.get("/admin/audit?category=privacy", headers=ADMIN).json()[0]["action"] == "hook_error"
    finally:
        pii._HOOKS.pop("flaky_hook")


def test_streaming_with_pre_hooks_and_refused_with_post_hooks(client, provider, router_env, monkeypatch):
    async def stream(model, messages, **kwargs):
        provider["calls"].append([m["content"] for m in messages])

        async def gen():
            yield {"choices": [{"delta": {"content": "hi"}}]}
            yield {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return gen()

    monkeypatch.setattr(litellm, "acompletion", stream)
    _privacy(router_env, "privacy:\n  default: { pre_request: [pii_redact] }\n")
    r = _chat(client, _key(client), stream=True)
    assert r.status_code == 200 and "[DONE]" in r.text
    assert "jane.doe" not in provider["calls"][0][0]
    assert r.headers["x-router-redactions"].startswith("card=1")

    _privacy(router_env, "privacy:\n  default: { post_response: [pii_redact] }\n")
    r = _chat(client, _key(client), stream=True)
    assert r.status_code == 400 and "post_response" in r.json()["detail"]
    assert len(provider["calls"]) == 1


def test_cache_entries_do_not_cross_a_content_policy_change(client, provider, router_env):
    cache = "policies:\n  cache_ttl_seconds: 600\n  cache_max_temperature: 0.0\n"
    _privacy(router_env, "", cache)
    h = _key(client)
    assert _chat(client, h, content="plain question", temperature=0).headers["x-router-cache"] == "miss"
    assert _chat(client, h, content="plain question", temperature=0).headers["x-router-cache"] == "hit"
    _privacy(router_env, "privacy:\n  default: { post_response: [pii_redact] }\n", cache)
    assert _chat(client, h, content="plain question", temperature=0).headers["x-router-cache"] == "miss"


def test_hook_modules_are_imported_from_env(router_env, tmp_path, monkeypatch):
    (tmp_path / "my_hooks_mod.py").write_text(
        "from router.pii import HookOutcome, register_hook\n"
        "register_hook('upper_hook', lambda texts, ctx: HookOutcome([t.upper() for t in texts], action='redact'))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "ROUTER_HOOK_MODULES", "my_hooks_mod")
    try:
        _privacy(router_env, "privacy:\n  default: { pre_request: [upper_hook] }\n")
        assert "upper_hook" in pii.registered_hooks()
    finally:
        pii._HOOKS.pop("upper_hook", None)
        sys.modules.pop("my_hooks_mod", None)


def test_audit_log_is_append_only_and_tamper_evident():
    for i in range(3):
        audit.record("privacy", "redact", team="t", subject="a", detail={"n": i})
    assert audit.verify() == {"ok": True, "rows": 3, "first_bad_id": None}
    with store.connect() as c, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE audit_log SET action = 'none' WHERE id = 2")
    with store.connect() as c, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM audit_log WHERE id = 2")
    # Someone with file access drops the trigger and edits a row: the chain shows where.
    with store.connect() as c:
        c.execute("DROP TRIGGER audit_log_no_update")
        c.execute("UPDATE audit_log SET detail = '{\"n\":99}' WHERE id = 2")
    assert audit.verify() == {"ok": False, "rows": 2, "first_bad_id": 2}
    store.init_db()  # recreates the trigger


def test_admin_actions_are_audited(client, router_env):
    r = client.post("/admin/keys", json={"label": "ops", "team": "data"}, headers=ADMIN)
    client.delete(f"/admin/keys/{r.json()['key']}", headers=ADMIN)
    router_env.write_text("aliases:\n  a: [{ provider: nope, model: m }]\n")
    assert client.post("/admin/reload", headers=ADMIN).status_code == 400
    rows = client.get("/admin/audit?category=admin", headers=ADMIN).json()
    assert [a["action"] for a in rows] == ["reload_rejected", "key_revoked", "key_created"]
    assert rows[-1]["detail"] == {"label": "ops", "admin": False} and rows[-1]["subject"] == "env-admin"
    assert "sk-router-" not in str(rows)
