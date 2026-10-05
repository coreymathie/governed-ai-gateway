# Corey Mathie, 2026
"""The browser demo's engine, run under CPython: same router modules Pyodide loads, simulated providers."""

import ast
import csv
import io
import json
from pathlib import Path

import pytest
import yaml

from demo.engine import DEMO_MCP, DEMO_POLICIES, Engine, JsBridge, run_sync
from router import costs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def restore_prices():
    before = costs.price_overrides()
    yield
    costs.set_price_overrides(before)


def send(engine, key=None, gap=0.25, n=1):
    return [run_sync(engine.send(key, gap)) for _ in range(n)][-1]


def test_demo_only_imports_dependency_free_router_modules():
    """Pyodide gets router/*.py and nothing else: no FastAPI, pydantic, LiteLLM or SQLite at import time."""
    allowed_stdlib = {
        "__future__", "collections", "csv", "dataclasses", "datetime", "email", "hashlib", "io", "json",
        "fnmatch", "itertools", "logging", "math", "os", "random", "re", "time", "typing",
    }  # fmt: skip
    engine_src = (ROOT / "demo" / "engine.py").read_text()
    used = {n.module.split(".")[1] for n in ast.walk(ast.parse(engine_src)) if isinstance(n, ast.ImportFrom)
            and n.module and n.module.startswith("router.")}  # fmt: skip
    used |= {a.name for n in ast.walk(ast.parse(engine_src)) if isinstance(n, ast.ImportFrom)
             and n.module == "router" for a in n.names}  # fmt: skip
    page = (ROOT / "demo" / "index.html").read_text()
    for module in used:
        assert f'"{module}"' in page, f"demo page doesn't load router/{module}.py"
        tree = ast.parse((ROOT / "router" / f"{module}.py").read_text())
        for node in tree.body:  # module-level imports only; lazy imports inside functions are fine
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                assert name in allowed_stdlib, f"router/{module}.py imports {name} at module level"


def test_outage_opens_breaker_then_requests_skip_it():
    e = Engine()
    e.update_deployment("anthropic/claude-haiku-4-5", {"outage": True})
    first = send(e)
    assert first["served_by"] == "openai/gpt-4.1-mini" and first["path"][0]["outcome"] == "error"
    assert first["latency_ms"] >= 3000  # waited out the timeout
    send(e, n=2)
    later = send(e)
    assert later["path"][0]["outcome"] == "skipped" and later["latency_ms"] < 3000
    assert e.breakers.get("anthropic/claude-haiku-4-5").current_state() == "open"


def test_breaker_recovers_through_half_open():
    e = Engine()
    e.update_deployment("anthropic/claude-haiku-4-5", {"outage": True})
    send(e, n=3)
    e.update_deployment("anthropic/claude-haiku-4-5", {"outage": False})
    e.advance(15)
    probe = send(e)
    assert probe["served_by"] == "anthropic/claude-haiku-4-5"
    moves = [(t["from"], t["to"]) for t in e.breakers.recent_transitions()][::-1]
    assert moves == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_breakers_off_means_every_request_pays_the_timeout():
    e = Engine()
    e.set_breakers_enabled(False)
    e.update_deployment("anthropic/claude-haiku-4-5", {"outage": True})
    events = [send(e) for _ in range(6)]
    assert all(ev["latency_ms"] >= 3000 for ev in events)


def test_429_retry_after_opens_breaker():
    e = Engine()
    e.update_deployment("anthropic/claude-haiku-4-5", {"rate_limited": True})
    send(e)
    b = e.breakers.get("anthropic/claude-haiku-4-5")
    assert b.current_state() == "open" and "Retry-After 8s" in b.last_reason


def test_all_down_returns_503_without_calling_anyone():
    e = Engine()
    for d in e.deployments:
        e.update_deployment(d.id, {"rate_limited": True})
    assert send(e)["status"] == 502
    ev = send(e)
    assert ev["status"] == 503 and ev["reason"] == "circuit_open"


def test_latency_strategy_moves_traffic_to_faster_provider():
    e = Engine()
    e.set_strategy("latency")
    e.update_deployment("anthropic/claude-haiku-4-5", {"latency_ms": 2500})
    served = [send(e)["served_by"] for _ in range(30)]
    assert served[-10:].count("openai/gpt-4.1-mini") >= 8


def test_tpm_and_budget_rejections():
    e = Engine()
    e.set_team_tpm("product", 3000)
    events = [send(e, "web-app") for _ in range(4)]
    assert any(ev.get("reason") == "tpm_team" for ev in events)
    e2 = Engine()
    e2.set_team_cap("support", 0.002)
    events = [send(e2, "support-bot") for _ in range(5)]
    assert events[-1]["status"] == 402 and "per-team daily" in events[-1]["message"]


def test_anomaly_spike_flags_then_pauses():
    e = Engine()
    verdicts = []
    for _ in range(40):
        ev = send(e, "nightly-batch", 0.1)
        verdicts.append(ev.get("anomaly") or ev.get("reason"))
        if ev.get("reason") == "anomaly_pause":
            break
    assert "flagged" in verdicts and verdicts[-1] == "anomaly_pause"
    sig = e.signal(e.key("nightly-batch"))
    assert sig.verdict == "pause" and sig.multiple >= 10 and sig.history_hours >= 24


def test_costs_and_showback_use_simulated_prices():
    e = Engine()
    ev = send(e, "web-app")
    d = e.deployment(ev["served_by"])
    assert ev["cost_usd"] > 0
    assert costs.price_overrides()[d.id] == pytest.approx((d.price_in_1k * 1000, d.price_out_1k * 1000))
    send(e, n=20)
    rows = list(csv.DictReader(io.StringIO(e.showback_csv("team,model"))))
    assert {r["team"] for r in rows} <= {"product", "support", "data"}
    total = sum(float(r["cost_usd"]) for r in rows)
    assert total == pytest.approx(e.snapshot()["org"]["spent_usd"], rel=1e-3)


def test_bridge_is_json_in_json_out():
    b = JsBridge()
    ev = json.loads(b.send("", 0.25))
    assert ev["outcome"] in ("ok", "rejected")
    snap = json.loads(b.snapshot())
    assert {"deployments", "teams", "keys", "events", "showback", "transitions"} <= set(snap)
    b.update_deployment("openai/gpt-4.1-mini", json.dumps({"latency_ms": 900, "outage": True}))
    assert b.engine.deployment("openai/gpt-4.1-mini").outage
    assert json.loads(b.showback("team"))["columns"] == ["team"]
    with pytest.raises(ValueError):
        b.call("__init__")
    b.call("reset")
    assert json.loads(b.snapshot())["stats"]["requests"] == 0


# ---------- governance panel (router/pii.py) ----------


def test_governance_redact_detect_block_and_content_log():
    e = Engine()
    sample = e.governance()["sample_prompt"]
    e.set_content_policy("product", "off", False)
    raw = run_sync(e.prompt("product", sample))
    assert raw["status"] == 200 and raw["sent"] == sample and "jordan@example.com" in raw["response"]

    e.set_content_policy("support", "redact", True)
    red = run_sync(e.prompt("support", sample))
    assert red["status"] == 200 and "[REDACTED:EMAIL]" in red["sent"] and "@example.com" not in red["sent"]
    assert "Jordan Example" in red["sent"]  # names are not detected; the demo says so
    assert red["trace"][0]["stage"] == "policy" and red["trace"][0]["outcome"] == "allow"
    assert red["trace"][1]["findings"] == {"secret": 1, "email": 1, "card": 1, "phone": 1}
    log = e.governance()["content_log"]
    assert len(log) == 1 and "@example.com" not in log[0]["request"] + log[0]["response"]

    e.set_content_policy("data", "block", False)
    blocked = run_sync(e.prompt("data", sample))
    assert blocked["status"] == 422 and blocked["sent"] is None and len(blocked["trace"]) == 2
    actions = [a["action"] for a in e.governance()["audit"] if a["category"] == "privacy"]
    assert actions == ["block", "content_logged", "redact"]  # newest first; "off" writes nothing

    e.set_content_policy("product", "detect", False)
    det = run_sync(e.prompt("product", sample))
    assert det["sent"] == sample and e.governance()["audit"][0]["action"] == "detect"  # after its policy "allow"
    with pytest.raises(ValueError):
        e.set_content_policy("product", "shred", False)


def test_bridge_governance_calls():
    b = JsBridge()
    g = json.loads(b.governance())
    b.call("set_content_policy", "product", "redact", False)
    r = json.loads(b.prompt("product", g["sample_prompt"]))
    assert "[REDACTED:CARD]" in r["sent"]
    assert json.loads(b.governance())["content"]["product"] == {"mode": "redact", "log_content": False}


# ---------- governance panel: policy-as-code (router/policy.py) ----------


def test_demo_policies_match_the_shipped_file():
    assert yaml.safe_load((ROOT / "config" / "policies.yaml").read_text()) == DEMO_POLICIES


def test_governance_policy_stage_residency_alias_list_and_clamp():
    e = Engine()
    text = e.governance()["sample_prompt"]
    denied = run_sync(e.prompt("regulated", text, "public-only", 8000))
    assert denied["status"] == 403 and denied["sent"] is None and denied["trace"][0]["outcome"] == "deny 403"
    local = run_sync(e.prompt("regulated", text, "smart-fast", 8000))
    assert local["status"] == 200 and local["served_by"] == "ollama/llama3.1:8b"
    assert local["policy"]["max_tokens"] == 2048 and len(local["policy"]["removed"]) == 2
    assert "[REDACTED:EMAIL]" in local["sent"]  # required by policy although the team's own hooks are off
    assert run_sync(e.prompt("contractors", text, "public-only"))["status"] == 403
    assert run_sync(e.prompt("product", text, "smart-fast", 8000))["policy"]["max_tokens"] == 4096
    # Residency holds under failure: Ollama down means 502, never a public fallback.
    e.update_deployment("ollama/llama3.1:8b", {"outage": True})
    down = run_sync(e.prompt("regulated", text, "smart-fast"))
    assert down["status"] == 502 and {a["deployment"] for a in down["path"]} == {"ollama/llama3.1:8b"}
    actions = [a["action"] for a in e.governance()["audit"] if a["category"] == "policy"]
    assert actions.count("deny") == 2 and actions.count("allow") == 3


# ---------- semantic cache panel (router/semcache.py) ----------


def test_semantic_panel_hit_guard_isolation_and_known_false_hit():
    e = Engine()
    ex = e.semantic_view()["examples"]
    assert run_sync(e.semantic_ask("product", ex[0]))["result"] == "miss"
    hit = run_sync(e.semantic_ask("product", ex[1]))
    assert hit["result"] == "hit" and hit["saved_usd"] > 0
    assert run_sync(e.semantic_ask("support", ex[1]))["reason"] == "empty"  # other team, other partition
    run_sync(e.semantic_ask("product", ex[2]))
    assert run_sync(e.semantic_ask("product", ex[3]))["reason"] == "guard: numbers differ"
    run_sync(e.semantic_ask("product", ex[4]))
    wrong = run_sync(e.semantic_ask("product", ex[5]))  # rotate vs. revoke: the documented false hit
    assert wrong["result"] == "hit" and wrong["nearest"] == ex[4]
    assert run_sync(e.semantic_ask("product", ex[5], 0.9))["result"] == "miss"  # a stricter threshold avoids it
    assert e.semantic_view()["counts"] == {"hit": 2, "miss": 5, "guard": 1}


def test_demo_calibration_matches_the_script():
    from scripts import calibrate_semcache

    pairs = ROOT / "evals" / "semcache_pairs.jsonl"
    demo = json.loads(JsBridge().calibrate(pairs.read_text(), 0.01))
    script = calibrate_semcache.run(str(pairs), 0.01, 0.50, 0.99, 0.01)
    assert demo["chosen"] == script["chosen"] and demo["holdout_at_chosen"] == script["holdout_at_chosen"]


# ---------- MCP tool gateway panel (router/mcp_policy.py) ----------


def test_demo_mcp_config_matches_the_example_file():
    assert yaml.safe_load((ROOT / "config" / "mcp.example.yaml").read_text()) == DEMO_MCP


def test_mcp_panel_allow_deny_velocity_redaction_and_showback():
    e = Engine()
    view = e.mcp_view()
    assert view["allowed"]["support"]["tickets"] == ["search_tickets", "get_ticket", "close_ticket"]
    assert view["allowed"]["product"] == {"tickets": [], "files": []}
    ok = e.mcp_call("support", "tickets", "search_tickets")
    assert ok["decision"] == "allow" and ok["args"] == {"query": "refund requested by [REDACTED:EMAIL]"}
    assert e.mcp_call("support", "tickets", "reassign_ticket")["decision"] == "deny"
    assert e.mcp_call("data", "files", "delete_file")["decision"] == "deny"
    burst = [e.mcp_call("support", "tickets", "close_ticket", {"id": f"T-{i}"})["decision"] for i in range(6)]
    assert burst == ["allow"] * 5 + ["rate_limited"]  # per_minute: 5 on close_ticket
    loop = [e.mcp_call("data", "files", "read_file", {"path": "same"})["decision"] for _ in range(6)]
    assert loop[-1] == "rate_limited" and "identical-call" in e.mcp_log[-1]["reason"]
    e.advance(61)
    assert e.mcp_call("support", "tickets", "close_ticket", {"id": "T-9"})["decision"] == "allow"
    tool_cost = sum(r["cost_usd"] for r in e.ledger if r["provider"] == "mcp")
    assert tool_cost == pytest.approx(5 * 0.001)  # read_file costs $0.001 per call; ticket tools are free
    assert {a["category"] for a in e.governance()["audit"]} == {"mcp"}
