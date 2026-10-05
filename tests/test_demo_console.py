# Corey Mathie, 2026
"""The console's demo mode (demo/engine.py request path and api()), run under CPython with the same modules
Pyodide loads. Checks it follows the gateway's stage order and trace shape, and that its config screens use the
gateway's own loaders."""

import json
from pathlib import Path

import pytest
import yaml

from demo.engine import DEMO_ALIASES, NOW_HOUR, SIM_PROFILES, Engine, JsBridge, run_sync
from router import costs, traces
from router.models import RouterConfig

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "How do I export my invoices as CSV?"


@pytest.fixture(autouse=True)
def restore_prices():
    before = costs.price_overrides()
    yield
    costs.set_price_overrides(before)


def ask(e, key="web-app", alias="smart-fast", text=PROMPT, max_tokens=256, temperature=0.7):
    return run_sync(e.request(key, alias, text, max_tokens, temperature))


def stages(tr):
    return {s["stage"]: s for s in tr["stages"]}


def test_demo_aliases_and_profiles_match_the_repo_files():
    cfg = RouterConfig.model_validate(yaml.safe_load((ROOT / "config" / "routes.yaml").read_text()))
    shipped = {a: {"strategy": cfg.strategy(a), "targets": [f"{t.provider}/{t.model}" for t in ts]}
               for a, ts in cfg.aliases.items()}  # fmt: skip
    assert shipped == DEMO_ALIASES
    assert json.loads((ROOT / "evals" / "sim_profiles.json").read_text())["profiles"] == SIM_PROFILES


def test_request_runs_every_gateway_stage_in_order_with_the_live_trace_shape():
    e = Engine()
    tr = ask(e)
    assert [s["stage"] for s in tr["stages"]] == list(traces.STAGES)
    assert tr["status"] == 200 and tr["outcome"] == "ok" and tr["served_by"] == "anthropic/claude-haiku-4-5"
    assert tr["cost_usd"] > 0 and tr["simulated"] is True
    live = traces.Trace("id", "ts", "k", "fp", "t", "a").as_dict()
    assert set(live) <= set(tr)
    assert {s["label"] for s in tr["stages"]} == set(traces.LABELS.values())
    row = e.trace_rows()[0]
    assert "stages" not in row and "sent" not in row and e.trace(tr["id"])["stages"] == tr["stages"]


def test_outage_fallback_breaker_and_reset():
    e = Engine()
    e.set_provider("anthropic", {"outage": True})
    first = ask(e)
    assert first["served_by"] == "openai/gpt-4.1-mini" and first["fell_back"]
    assert [a["outcome"] for a in stages(first)["chain"]["detail"]["attempts"]] == ["error", "ok"]
    ask(e)
    ask(e)
    later = ask(e)
    assert stages(later)["chain"]["detail"]["attempts"][0]["outcome"] == "skipped"
    view = e.providers_view()
    anth = next(p for p in view["providers"] if p["provider"] == "anthropic")
    assert anth["outage"] and any(d["breaker"] == "open" for d in anth["deployments"])
    e.reset_breakers()
    e.set_provider("anthropic", {"outage": False})
    assert ask(e)["served_by"] == "anthropic/claude-haiku-4-5"
    with pytest.raises(KeyError):
        e.set_provider("mars", {"outage": True})


def test_policy_residency_deny_and_latency_route():
    e = Engine()
    e.create_key("reg-app", "regulated")
    denied = ask(e, "reg-app", "heavy-reasoning")
    assert denied["status"] == 403 and denied["stages"][-1]["stage"] == "policy"
    local = ask(e, "reg-app", "smart-fast", PROMPT + " Contact jordan@example.com", 8000)
    assert local["served_by"] == "ollama/llama3.1:8b"
    pol = stages(local)["policy"]
    assert (
        "removed anthropic/claude-haiku-4-5, openai/gpt-4.1-mini" in pol["summary"]
        and "max_tokens 2048" in pol["summary"]
    )
    assert "pii_redact" in stages(local)["pre_hooks"]["summary"]
    assert ask(e, alias="nope")["status"] == 400
    assert ask(e, "mobile-app", "fast-chat")["status"] == 200


def test_exact_and_semantic_cache():
    e = Engine()
    miss = ask(e, "support-bot", temperature=0)
    hit = ask(e, "support-bot", temperature=0)
    assert stages(miss)["cache"]["decision"] == "miss" and hit["outcome"] == "cache_hit"
    assert hit["cost_usd"] == 0 and hit["saved_usd"] == pytest.approx(miss["cost_usd"])
    e.set_semantic_team("support", True)
    ask(e, "support-bot", text="How do I rotate an API key in the dashboard?", temperature=0)
    sem = ask(e, "support-bot", text="how do i rotate an api key in the dashboard", temperature=0)
    assert sem["cache"] == "semantic-hit" and stages(sem)["semantic_cache"]["decision"] == "hit"
    e.set_cache(False)
    assert stages(ask(e, "support-bot", temperature=0))["cache"]["decision"] == "skip"
    ov = e.overview()
    assert ov["kpis"]["cache"]["hits"] == 2 and ov["kpis"]["cache"]["saved_usd"] > 0


def test_keys_holds_revocation_and_budgets():
    e = Engine()
    e.pause_key("web-app")
    paused = ask(e)
    assert paused["status"] == 429 and paused["stages"][-1]["stage"] == "anomaly"
    assert e.unpause_key("web-app") == "unpaused" and ask(e)["status"] == 200
    assert e.unpause_key("web-app") == "not_paused"
    e.revoke_key("web-app")
    assert ask(e)["status"] == 401
    created = e.create_key("new-app", "product")
    assert created["key"].startswith("sk-demo-") and created["simulated"]
    with pytest.raises(ValueError):
        e.create_key("new-app", "product")
    e.set_team_cap("product", 0.0000001)
    capped = ask(e, "new-app")
    assert capped["status"] == 402 and capped["stages"][-1]["stage"] == "budgets"
    b = e.budgets_view()
    assert next(t for t in b["teams"] if t["team"] == "product")["cap_usd"] == 0.0000001
    keys = {k["label"]: k for k in e.keys_view()}
    assert keys["web-app"]["revoked_at"] and keys["new-app"]["team"] == "product"


def test_spike_is_flagged_then_paused_and_can_be_overridden():
    e = Engine()
    summary = run_sync(e.traffic(40, "spike"))
    assert summary["outcomes"].get("rejected", 0) >= 1
    paused = [t for t in e.traces if t["status"] == 429]
    assert paused and paused[0]["stages"][-1]["stage"] == "anomaly"
    ov = e.overview()
    assert ov["anomalies"][0]["label"] == "nightly-batch" and ov["anomalies"][0]["verdict"] == "pause"
    assert e.unpause_key("nightly-batch") == "override"
    tr = ask(e, "nightly-batch", "cheap-batch")
    assert tr["status"] == 200 and "admin override" in stages(tr)["anomaly"]["summary"]


def test_overview_chart_history_is_simulated_and_current_hour_is_live():
    e = Engine()
    run_sync(e.traffic(20))
    ov = e.overview()
    today, base = ov["hourly"]["today"], ov["hourly"]["baseline_7d"]
    assert len(today) == len(base) == 24 and ov["hourly"]["current_hour"] == NOW_HOUR
    assert all(v > 0 for v in today[:NOW_HOUR]) and all(v == 0 for v in today[NOW_HOUR + 1 :])
    assert today[NOW_HOUR] >= ov["kpis"]["spend_usd"]
    assert ov["kpis"]["requests"] >= 1 and ov["traces"]["requests"] == 20
    assert {r["team"] for r in ov["by_team"]} <= {"product", "support", "data"}


def test_config_screens_use_the_gateway_loaders():
    e = Engine()
    for name, file in (("policies", "policies.yaml"), ("routes", "routes.yaml"), ("mcp", "mcp.example.yaml")):
        text = (ROOT / "config" / file).read_text()
        e.set_config_text(name, text)
        assert e.validate_config(name, text)["ok"], name
    bad = e.validate_config("policies", "version: 1\nteams:\n  x:\n    allowed_providers: [mars]\n")
    assert not bad["ok"] and "mars" in bad["errors"][0]["message"] and bad["errors"][0]["line"] == 4
    assert e.validate_config("routes", "aliases: [")["errors"][0]["line"] is not None
    assert e.apply_config("routes", "aliases:\n  a:\n    - { provider: nope, model: x }\n")["ok"] is False

    before = e.dry_run(["product"], ["local-first"])["results"][0]
    assert before["allow"] is True
    tighter = e.config("policies")["text"] + "\n"
    tighter = tighter.replace("teams:\n", "teams:\n  product:\n    deny_aliases: [local-first]\n", 1)
    preview = e.dry_run(["product"], ["local-first"], text=tighter)["results"][0]
    assert preview["allow"] is False and preview["status"] == 403
    assert e.apply_config("policies", tighter)["ok"]
    assert ask(e, alias="local-first")["status"] == 403

    tiny = "aliases:\n  tiny:\n    - { provider: gemini, model: gemini-2.5-flash, timeout_s: 5 }\n"
    routes = e.config("routes")["text"].replace("aliases:\n", tiny, 1)
    assert e.apply_config("routes", routes)["ok"]
    assert e.routes_view()["tiny"]["targets"] == ["gemini/gemini-2.5-flash"]
    assert ask(e, alias="tiny")["served_by"] == "gemini/gemini-2.5-flash"


def test_api_dispatch_is_an_allow_list_and_json_in_json_out():
    b = JsBridge()
    tr = json.loads(b.api("request", json.dumps({"key_label": "web-app", "alias": "smart-fast", "text": PROMPT})))
    assert tr["status"] == 200
    rows = json.loads(b.api("trace_rows", json.dumps({"limit": 5})))
    assert rows[0]["id"] == tr["id"]
    assert json.loads(b.api("traffic", json.dumps({"n": 3})))["sent"] == 3
    assert "providers" in json.loads(b.api("providers_view"))
    with pytest.raises(ValueError):
        b.api("_deny")
    with pytest.raises(ValueError):
        b.api("__init__")
