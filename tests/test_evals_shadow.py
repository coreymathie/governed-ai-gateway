# Corey Mathie, 2026
"""Eval harness (simulated providers only: no real model is called here) and shadow mode through the API."""

import asyncio
import json
import textwrap
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient

from router import config_loader, costs, evalrun, evals, main, routing, shadow
from router.config_loader import settings
from router.models import RouterConfig, RouteTarget
from scripts import eval_gate, eval_routes

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "sample_cases.jsonl"
ADMIN = {"Authorization": "Bearer sk-router-admin"}


@pytest.fixture(autouse=True)
def restore_prices():
    before = costs.price_overrides()
    yield
    costs.set_price_overrides(before)


# ---------- cases and scoring ----------


def test_bundled_cases_load_and_every_answer_passes_its_reference():
    cases = evals.load_cases(CASES.read_text().splitlines())
    assert len(cases) == 40 and len({c.id for c in cases}) == 40
    assert all(evals.score_reference(c.reference, c.answer) == 1.0 for c in cases)
    wrong = "I'm not certain; it may depend on details not given."
    assert all(evals.score_reference(c.reference, wrong) == 0.0 for c in cases)


@pytest.mark.parametrize(
    "line,match",
    [
        ('{"id": "a", "prompt": "x", "messages": []}', "exactly one of"),
        ('{"id": "a", "prompt": "x", "reference": {"type": "fuzzy", "value": "y"}}', "reference must be"),
        ('{"id": "a", "prompt": "x", "reference": {"type": "regex", "value": "("}}', "bad regex"),
        ('{"id": "a", "prompt": "x", "colour": "red"}', "unknown fields"),
        ('{"id": "", "prompt": "x"}', "unique non-empty"),
        ("{not json", "invalid JSON"),
        ("# only a comment", "no cases"),
    ],
)
def test_case_validation(line, match):
    with pytest.raises(evals.EvalConfigError, match=match):
        evals.load_cases([line])
    with pytest.raises(evals.EvalConfigError, match="unique"):
        evals.load_cases(['{"id": "a", "prompt": "x"}', '{"id": "a", "prompt": "y"}'])


def test_scoring_rules():
    assert evals.score_reference({"type": "exact", "value": "391"}, "  391. ") == 1.0
    assert evals.score_reference({"type": "exact", "value": "Ana"}, "ana") == 1.0
    assert evals.score_reference({"type": "exact", "value": "391"}, "391 apples") == 0.0
    assert evals.score_reference({"type": "contains", "value": "P2"}, "priority p2") == 1.0
    assert evals.score_reference({"type": "regex", "value": r"\b2027-03-09\b"}, "due 2027-03-09.") == 1.0


# ---------- simulated runs ----------


def _profiles(**q):
    return {d: evals.SimProfile(quality=v, price_in_per_1m=1.0, price_out_per_1m=2.0) for d, v in q.items()}


def _run(targets, profiles, cases, judge=None, seed="t"):
    evals.apply_sim_prices(profiles)
    return asyncio.run(evals.run_route("r", targets, cases, evals.simulated_provider(profiles, seed), judge))


def test_simulation_is_deterministic_and_follows_profiles():
    cases = evals.load_cases(CASES.read_text().splitlines())
    a, b = RouteTarget(provider="openai", model="a"), RouteTarget(provider="openai", model="b")
    profiles = {"openai/a": evals.SimProfile(quality=1.0), "openai/b": evals.SimProfile(quality=0.0)}
    assert evals.summarize(_run([a], profiles, cases))["quality"] == 1.0
    assert evals.summarize(_run([b], profiles, cases))["quality"] == 0.0
    mid = {"openai/a": evals.SimProfile(quality=0.5, latency_ms=100)}
    first, second = evals.summarize(_run([a], mid, cases)), evals.summarize(_run([a], mid, cases))
    assert first == second and 0.2 < first["quality"] < 0.8
    assert evals.summarize(_run([a], mid, cases, seed="other")) != first


def test_fallback_errors_cost_and_latency():
    cases = evals.load_cases(CASES.read_text().splitlines())[:10]
    a, b = RouteTarget(provider="openai", model="a"), RouteTarget(provider="ollama", model="b")
    profiles = {
        "openai/a": evals.SimProfile(quality=1.0, error_rate=1.0),
        "ollama/b": evals.SimProfile(quality=1.0, latency_ms=1000, price_in_per_1m=1.0, price_out_per_1m=1.0),
    }
    s = evals.summarize(_run([a, b], profiles, cases))
    assert s["fallbacks"] == 10 and s["errors"] == 0 and s["served_by"] == {"ollama/b": 10}
    assert s["total_cost_usd"] > 0 and 0.8 <= s["latency_p50_s"] <= 1.2
    dead = evals.summarize(_run([a], profiles, cases))
    assert dead["errors"] == 10 and dead["quality"] == 0.0 and dead["total_cost_usd"] == 0.0


def test_judge_hook_scores_unreferenced_cases_and_is_clamped():
    cases = evals.load_cases(['{"id": "open", "prompt": "Write a haiku", "answer": "leaves fall"}'])
    a = RouteTarget(provider="openai", model="a")
    results = _run([a], {"openai/a": evals.SimProfile(quality=1.0)}, cases, judge=lambda case, out: 7.0)
    assert results[0].score == 1.0 and results[0].judge_score == 1.0
    assert evals.summarize(results)["judge_mean"] == 1.0


def test_gate_decisions():
    base = {"quality": 0.90, "total_cost_usd": 1.0}
    assert evals.gate(base, {"quality": 0.89, "total_cost_usd": 1.05}).ok
    drop = evals.gate(base, {"quality": 0.85, "total_cost_usd": 0.5})
    assert not drop.ok and "quality fell" in drop.reasons[0] and drop.quality_delta == -0.05
    pricey = evals.gate(base, {"quality": 0.95, "total_cost_usd": 1.2}, max_cost_increase=0.10)
    assert not pricey.ok and pricey.cost_change_pct == 0.2
    assert not evals.gate({"quality": 0.9, "total_cost_usd": 0.0}, {"quality": 0.9, "total_cost_usd": 0.1}).ok
    assert not evals.gate({"quality": None, "total_cost_usd": 1}, {"quality": 0.9, "total_cost_usd": 1}).ok


def test_shipped_routes_simulated_report_numbers():
    """The numbers quoted in the README come from this run (simulated providers, seed eval-v1)."""
    rep = evalrun.run(ROOT / "config/routes.yaml", CASES, None, "sim", ROOT / "evals/sim_profiles.json", "eval-v1")
    assert rep["provider"] == "simulated"
    q = {name: r["summary"]["quality"] for name, r in rep["routes"].items()}
    cost = {name: r["summary"]["total_cost_usd"] for name, r in rep["routes"].items()}
    assert q == {
        "cheap-batch": 0.625, "fast-chat": 0.85, "heavy-reasoning": 1.0, "local-first": 0.6, "regulated-fast": 0.6,
        "smart-fast": 0.85,
    }  # fmt: skip
    assert cost["smart-fast"] == pytest.approx(0.11055, abs=1e-6)
    assert cost["cheap-batch"] == pytest.approx(0.01215, abs=1e-6)
    assert cost["heavy-reasoning"] == pytest.approx(0.335109, abs=1e-6)
    # Realistic prompt sizes: per 1,000 requests, smart-fast costs about what the sample company's apps pay.
    assert 2.0 < rep["routes"]["smart-fast"]["summary"]["cost_per_1k_requests_usd"] < 4.0
    md = evals.to_markdown(rep)
    assert "Provider: **simulated** (deterministic simulation" in md and "| `smart-fast` |" in md


def test_real_runs_refuse_without_credentials(monkeypatch):
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(env, raising=False)
    with pytest.raises(
        evals.EvalConfigError, match=r"missing credentials.*openai/gpt-4\.1-nano \(needs OPENAI_API_KEY\)"
    ):
        evalrun.run(ROOT / "config/routes.yaml", CASES, ["cheap-batch"], "real")
    with pytest.raises(evals.EvalConfigError, match="not in the routes file"):
        evalrun.run(ROOT / "config/routes.yaml", CASES, ["nope"], "sim")


def test_cli_report_and_gate(tmp_path, capsys):
    out = tmp_path / "rep"
    assert eval_routes.main(["--aliases", "smart-fast,cheap-batch", "--out", str(out)]) == 0
    rep = json.loads(out.with_suffix(".json").read_text())
    assert set(rep["routes"]) == {"smart-fast", "cheap-batch"} and rep["provider"] == "simulated"
    assert "| `cheap-batch` |" in out.with_suffix(".md").read_text()

    routes = (ROOT / "config/routes.yaml").read_text()
    cheaper = tmp_path / "cheaper.yaml"
    cheaper.write_text(
        routes.replace(
            "  smart-fast:\n    - { provider: anthropic, model: claude-haiku-4-5,",
            "  smart-fast:\n    - { provider: openai,    model: gpt-4.1-nano,",
            1,
        )
    )
    assert "gpt-4.1-nano" in cheaper.read_text().split("heavy-reasoning")[0]
    args = ["--routes-before", str(ROOT / "config/routes.yaml"), "--alias", "smart-fast"]
    assert eval_gate.main([*args, "--routes-after", str(cheaper), "--out", str(tmp_path / "gate")]) == 1
    assert "quality fell" in capsys.readouterr().out
    assert json.loads((tmp_path / "gate.json").read_text())["ok"] is False
    assert eval_gate.main([*args, "--routes-after", str(ROOT / "config/routes.yaml")]) == 0
    pricier = tmp_path / "pricier.yaml"
    pricier.write_text(
        routes.replace(
            "  smart-fast:\n    - { provider: anthropic, model: claude-haiku-4-5,",
            "  smart-fast:\n    - { provider: anthropic, model: claude-sonnet-4-5,",
            1,
        )
    )
    assert eval_gate.main([*args, "--routes-after", str(pricier)]) == 1
    assert "cost rose 203.1%" in capsys.readouterr().out
    rep_json = str(out.with_suffix(".json"))
    pair = ["--baseline-report", rep_json, "--candidate-report", rep_json]
    assert eval_gate.main([*pair, "--alias", "smart-fast", "--candidate-alias", "cheap-batch"]) == 1
    missing = ["--baseline-report", "/nonexistent.json", "--candidate-report", "/nonexistent.json"]
    assert eval_gate.main(["--alias", "smart-fast", *missing]) == 2


# ---------- shadow mode ----------


class FakeResponse:
    def __init__(self, text):
        self._d = {
            "id": "x",
            "object": "chat.completion",
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def provider(monkeypatch):
    state = {"calls": [], "fail": set(), "replies": {}}

    async def acompletion(model, messages, **kwargs):
        state["calls"].append({"model": model, "messages": [m["content"] for m in messages]})
        if model.split("/")[0] in state["fail"]:
            raise litellm.exceptions.APIConnectionError(message="down", llm_provider="x", model=model)
        return FakeResponse(state["replies"].get(model.split("/")[0], "the answer is 42"))

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return state


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


ROUTES = """
aliases:
  smart-fast:
    - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 5 }
  candidate:
    - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
  local-candidate:
    - { provider: ollama, model: llama3.1:8b, timeout_s: 5 }
prices:
  "anthropic/claude-haiku-4-5": { input_usd_per_1m: 1.0, output_usd_per_1m: 5.0 }
  "openai/gpt-4.1-mini": { input_usd_per_1m: 0.4, output_usd_per_1m: 1.6 }
"""


def _shadow(router_env, block: str, policies: str = "version: 1\n"):
    router_env.write_text(textwrap.dedent(ROUTES) + textwrap.dedent(block))
    Path(settings.ROUTER_POLICIES_FILE).write_text(textwrap.dedent(policies))
    config_loader.reload_all()


def _key(client, team="digital-banking"):
    r = client.post("/admin/keys", json={"label": f"{team}-k", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _chat(client, h, content="what is six times seven?", **extra):
    body = {"model": "smart-fast", "messages": [{"role": "user", "content": content}], **extra}
    return client.post("/v1/chat/completions", json=body, headers=h)


def _settle(client):
    client.portal.call(shadow.drain)
    return client.get("/admin/shadow", headers=ADMIN).json()


def test_shadow_mirrors_to_candidate_off_the_books(client, provider, router_env):
    _shadow(router_env, "shadow:\n  smart-fast: { candidate: candidate, percent: 100 }\n")
    provider["replies"]["openai"] = "the answer is 42 probably"
    h = _key(client)
    r = _chat(client, h)
    assert r.status_code == 200 and r.headers["x-router-used-provider"] == "anthropic"
    assert r.json()["choices"][0]["message"]["content"] == "the answer is 42"
    out = _settle(client)
    assert [c["model"] for c in provider["calls"]] == ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini"]
    (cmp,) = out["comparisons"]
    assert cmp["alias"] == "smart-fast" and cmp["candidate"] == "candidate" and cmp["mirrored"] == 1
    assert cmp["mean_agreement"] == pytest.approx(0.8) and cmp["exact_match_rate"] == 0
    assert cmp["shadow_cost_usd"] == pytest.approx(0.00012) and cmp["primary_cost_usd"] == pytest.approx(0.00035)
    assert cmp["cost_change_pct"] == pytest.approx(-65.71, abs=0.01)
    # Off the books: the usage ledger, showback and budgets only see the live call.
    recent = client.get("/admin/recent", headers=ADMIN).json()
    assert [c["provider"] for c in recent] == ["anthropic"]
    totals = client.get("/admin/showback", headers=ADMIN).json()["totals"]
    assert totals["cost_usd"] == pytest.approx(0.00035)
    assert "router_shadow_requests_total" in client.get("/metrics", headers=ADMIN).text


def test_shadow_failure_never_touches_the_live_path(client, provider, router_env):
    _shadow(router_env, "shadow:\n  smart-fast: { candidate: candidate, percent: 100 }\n")
    provider["fail"].add("openai")
    h = _key(client)
    for _ in range(6):
        assert _chat(client, h).status_code == 200
    out = _settle(client)
    assert out["comparisons"][0]["errors"] == 6
    live = {b["deployment"] for b in routing.breakers.snapshot()}
    assert "openai/gpt-4.1-mini" not in live  # the live registry never saw the candidate
    assert shadow.breakers.get("openai/gpt-4.1-mini").current_state() == "open"


def test_shadow_respects_the_candidates_policy(client, provider, router_env):
    _shadow(
        router_env,
        "shadow:\n  smart-fast: { candidate: candidate, percent: 100 }\n",
        "teams:\n  regulated: { allowed_providers: [ollama, anthropic] }\n",
    )
    assert _chat(client, _key(client, "regulated")).status_code == 200
    out = _settle(client)
    assert out["comparisons"] == [] and [c["model"] for c in provider["calls"]] == ["anthropic/claude-haiku-4-5"]
    rows = client.get("/admin/audit?category=shadow", headers=ADMIN).json()
    assert (
        rows[0]["action"] == "skipped_policy"
        and "provider openai not in allowed_providers" in rows[0]["detail"]["reasons"][0]
    )


def test_shadow_gets_redacted_content_and_required_hooks(client, provider, router_env):
    _shadow(
        router_env,
        "shadow:\n  smart-fast: { candidate: local-candidate, percent: 100 }\n",
        "routes:\n  local-candidate: { required_hooks: [pii_redact] }\n",
    )
    _chat(client, _key(client), content="mail ops@example.com")
    _settle(client)
    assert provider["calls"][0]["messages"] == ["mail ops@example.com"]  # live route has no hook
    assert provider["calls"][1] == {"model": "ollama/llama3.1:8b", "messages": ["mail [REDACTED:EMAIL]"]}


def test_shadow_billing_suppress_cap_sampling_and_scope(client, provider, router_env):
    _shadow(router_env, "shadow:\n  smart-fast: { candidate: candidate, percent: 100, billing: suppress }\n")
    h = _key(client)
    _chat(client, h)
    cmp = _settle(client)["comparisons"][0]
    assert cmp["shadow_cost_usd"] is None and cmp["unbilled"] == 1

    _shadow(router_env, "shadow:\n  smart-fast: { candidate: candidate, percent: 100, max_daily_usd: 0.0001 }\n")
    _chat(client, h)
    _settle(client)  # this mirror is billed and passes the cap
    _chat(client, h)
    _settle(client)
    assert client.get("/admin/audit?category=shadow", headers=ADMIN).json()[0]["action"] == "skipped_cap"

    _shadow(router_env, "shadow:\n  smart-fast: { candidate: candidate, percent: 0 }\n")
    n = len(provider["calls"])
    _chat(client, h)
    _chat(client, h, stream=True)
    _settle(client)
    assert all(c["model"].startswith("anthropic/") for c in provider["calls"][n:])  # no mirror, streams never


def test_shadow_config_validation():
    base = {"aliases": {"a": [{"provider": "ollama", "model": "m"}], "b": [{"provider": "ollama", "model": "n"}]}}
    for bad in ({"a": {"candidate": "zzz"}}, {"a": {"candidate": "a"}}, {"a": {"candidate": "b", "percent": 101}},
                {"a": {"candidate": "b", "billing": "free"}}):  # fmt: skip
        with pytest.raises(ValueError):
            RouterConfig.model_validate({**base, "shadow": bad})
    assert shadow.agreement("", "") == 1.0 and shadow.agreement("a b", "c d") == 0.0
