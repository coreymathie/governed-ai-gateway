# Corey Mathie, 2026
"""One story across the console: the sample company's usage file, request log, in-browser engine, route evals,
semantic-cache calibration and config/*.yaml all describe the same credit union, with the same numbers."""

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from demo import cypress_harbor as ch
from demo import engine
from router import pii, policy
from scripts import build_eval_cases, sample_requests
from scripts import generate_sample_company as company

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "demo" / "data"


@pytest.fixture(scope="module")
def usage():
    return json.loads((DATA / "sample_company.json").read_text())


@pytest.fixture(scope="module")
def log():
    return json.loads((DATA / "sample_requests.json").read_text())


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load((ROOT / "config" / "routes.yaml").read_text())


def _implied_model_spend(days: list[dict], routes: dict[str, list[str]]) -> dict[str, float]:
    """Spend by deployment that the request mix implies: uncached requests x route shares x token cost."""
    out: dict[str, float] = defaultdict(float)
    for d in days:
        for app, a in d["apps"].items():
            uncached = a["requests"] - a["cached"]
            for alias, share in ch.APPS[app]["routes"]:
                targets = routes[alias]
                for f, t in zip(ch.first_choice_shares(alias, targets), targets, strict=True):
                    out[t] += uncached * share * f * company.unit_cost(app, alias, t)
    return out


def test_spend_by_model_is_what_the_request_mix_implies(usage, cfg):
    routes = company.aliases(cfg)
    implied = _implied_model_spend(usage["days"][-30:], routes)
    shown = {m["model"]: m["spend_30d_usd"] for m in usage["models"]}
    total_i, total_s = sum(implied.values()), sum(shown.values())
    assert total_s == pytest.approx(total_i, rel=0.02)  # fallbacks and the paused loop move a little spend
    for dep in set(implied) | set(shown):
        assert shown.get(dep, 0) / total_s == pytest.approx(implied.get(dep, 0) / total_i, abs=0.01), dep
    assert shown["ollama/llama3.1:8b"] / total_s < 0.01  # "near-zero marginal cost" for the on-prem model
    by_day = {d["date"]: d for d in usage["days"]}
    assert sum(m["spend_30d_usd"] for m in usage["models"]) == pytest.approx(
        sum(by_day[k]["spend_usd"] for k in sorted(by_day)[-30:]), abs=1.0
    )


def test_the_log_prices_and_routes_match_spend_by_model(usage, log, cfg):
    """The log's cost share by model, reweighted to each app's share of traffic, tracks Spend by model."""
    routes = company.aliases(cfg)
    week = [d for d in usage["days"] if "2026-10-01" <= d["date"] <= "2026-10-07"]
    implied = _implied_model_spend(week, routes)
    per_app = defaultdict(lambda: defaultdict(float))
    rows = defaultdict(int)
    for r in log["requests"]:
        if r["outcome"] == "ok" and r["key"] == r["app"]:
            per_app[r["app"]][r["served_by"]] += r["cost_usd"]
            rows[r["app"]] += 1
    weighted: dict[str, float] = defaultdict(float)
    for app, models in per_app.items():
        uncached = sum(d["apps"][app]["requests"] - d["apps"][app]["cached"] for d in week)
        for dep, c in models.items():
            weighted[dep] += c / rows[app] * uncached
    tw, ti = sum(weighted.values()), sum(implied.values())
    for dep in ("anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-5"):
        assert weighted[dep] / tw == pytest.approx(implied[dep] / ti, abs=0.08), dep


def test_every_app_has_one_route_in_config_engine_log_and_spend(usage, log, cfg):
    routes = company.aliases(cfg)
    for app, spec in ch.APPS.items():
        for alias, _share in spec["routes"]:
            assert alias in routes, (app, alias)
            assert set(spec["tokens"]) == {a for a, _ in spec["routes"]}
        assert abs(sum(s for _, s in spec["routes"]) - 1) < 1e-9
    assert {a["app"]: [(r["alias"], r["share"]) for r in a["routes"]] for a in usage["apps"]} == {
        k: [tuple(r) for r in v["routes"]] for k, v in ch.APPS.items()
    }
    assert {k: v["routes"] for k, v in log["apps"].items()} == {
        k: [a for a, _ in v["routes"]] for k, v in ch.APPS.items()
    }
    for key, alias in engine.KEY_ALIAS.items():
        assert alias == ch.primary_alias(key) and alias in engine.DEMO_ALIASES
    assert {a: r["targets"] for a, r in engine.DEMO_ALIASES.items()} == routes
    assert engine.RUNAWAY["alias"] in [a for a, _ in ch.APPS[engine.RUNAWAY["key"]]["routes"]]
    assert usage["anomaly"]["key"] == engine.RUNAWAY["key"] and usage["anomaly"]["alias"] == engine.RUNAWAY["alias"]
    teams = {t["team"]: t["apps"] for t in usage["teams"]}
    assert teams == {t: ch.apps_of(t) for t in ch.TEAMS} and len(teams) == 7


def test_every_alias_and_deployment_in_the_log_exists_in_the_config(log, cfg):
    routes = company.aliases(cfg)
    assert log["aliases"] == routes
    for r in log["requests"]:
        assert r["alias"] in routes
        if r["served_by"]:
            assert r["served_by"] in routes[r["alias"]]
        chain = next((s for s in r["stages"] if s["stage"] == "chain"), None)
        if chain:
            assert all(a["deployment"] in routes[r["alias"]] for a in chain["detail"]["attempts"])


def test_prices_are_defined_once(cfg):
    profiles = json.loads((ROOT / "evals" / "sim_profiles.json").read_text())["profiles"]
    for dep, (pin, pout) in ch.PRICES.items():
        assert (profiles[dep]["price_in_per_1m"], profiles[dep]["price_out_per_1m"]) == (pin, pout)
    for dep, p in cfg["prices"].items():
        assert (p["input_usd_per_1m"], p["output_usd_per_1m"]) == ch.PRICES[dep]
    assert sample_requests.PRICES is ch.PRICES


def test_content_hooks_in_the_app_table_match_the_configs(cfg):
    """An app is marked pii in demo/cypress_harbor.py exactly when routes.yaml or policies.yaml runs pii_redact."""
    pol = policy.load(yaml.safe_load((ROOT / "config" / "policies.yaml").read_text()), pii.registered_hooks())
    for app, spec in ch.APPS.items():
        for alias, _share in spec["routes"]:
            required = policy.effective_rule(pol, spec["team"], alias)[0].required_hooks
            hooks = sample_requests.hooks(cfg, spec["team"], alias, list(required))
            assert ("pii_redact" in hooks) == spec["pii"], (app, alias)
            if spec["regulated"]:
                assert policy.effective_rule(pol, spec["team"], alias)[0].allowed_providers == ("ollama",)


def test_the_contractor_refusal_is_what_the_router_policy_decides(log, cfg):
    pol = policy.load(yaml.safe_load((ROOT / "config" / "policies.yaml").read_text()), pii.registered_hooks())
    routes = company.aliases(cfg)
    t = sample_requests.Target

    def decide(alias):
        targets = [t(*d.split("/", 1)) for d in routes[alias]]
        return policy.evaluate(pol, policy.RequestFacts(team=ch.CONTRACTOR["team"], alias=alias, targets=targets))

    refused, allowed = decide("heavy-reasoning"), decide("local-first")
    assert not refused.allow and refused.status == 403
    assert allowed.allow and [f"{x.provider}/{x.model}" for x in allowed.targets] == routes["local-first"]
    row = next(r for r in log["requests"] if r["key"] == ch.CONTRACTOR["key"] and r["status"] == 403)
    assert row["reason"] == f"Policy: {'; '.join(refused.reasons)}."
    # The same document drives the in-browser engine.
    assert policy.evaluate(policy.load(engine.DEMO_POLICIES, pii.registered_hooks()), policy.RequestFacts(
        team="contractors", alias="heavy-reasoning", targets=[t(*d.split("/", 1)) for d in routes["heavy-reasoning"]],
    )).status == 403  # fmt: skip


def test_activity_feed_and_incidents_match_the_daily_data(usage, cfg):
    by_date = {d["date"]: d for d in usage["days"]}
    breaker = cfg["resilience"]["circuit_breaker"]
    for inc in usage["incidents"]:
        day = by_date[inc["date"]]
        assert inc["fallback_requests"] == day["incident_fallbacks"][inc["deployment"]] > 0
        assert inc["fallback_requests"] <= day["fallbacks"]
        assert f"failure_threshold: {breaker['failure_threshold']}" in inc["handled"]
        assert f"after {breaker['failure_threshold']} consecutive failures" in inc["handled"]
        assert f"every {breaker['cooldown_seconds']:g} s" in inc["handled"]
        assert f"{inc['fallback_requests']:,} requests were answered" in inc["handled"]
        for alias in inc["routes_affected"]:
            assert inc["deployment"] in company.aliases(cfg)[alias]
    notes = {n["date"]: n for n in usage["notable"]}
    oct6 = next(i for i in usage["incidents"] if i["date"] == "2026-10-06")
    assert f"{oct6['fallback_requests']:,} requests" in notes["2026-10-06"]["title"]
    assert f"After {breaker['failure_threshold']} consecutive failures" in notes["2026-10-06"]["detail"]
    sep16 = next(i for i in usage["incidents"] if i["date"] == "2026-09-16")
    assert f"answered {sep16['fallback_requests']:,} requests" in notes["2026-09-16"]["detail"]
    # The runaway: every number in the story is the anomaly record's, and the record follows the data.
    a = usage["anomaly"]
    risk = notes[a["date"]]
    assert f"paused {a['seconds_to_pause']} seconds" in risk["title"]
    for value in (str(a["calls_before_pause"]), f"{a['baseline_multiple']}x", f"${a['prevented_usd']:,.2f}"):
        assert value in risk["detail"], value
    assert a["baseline_multiple"] >= 10 and a["hour_spend_at_pause_usd"] >= 10 * a["baseline_hourly_usd"]
    assert by_date[a["date"]]["anomaly_pauses"] == 1 and sum(d["anomaly_pauses"] for d in usage["days"]) == 1
    # The August showback, recomputed.
    aug = [d for d in usage["days"] if d["date"][:7] == "2026-08"]
    spent = {t: sum(d["teams"][t]["spend_usd"] for d in aug) for t in ch.TEAMS}
    budget = {t["team"]: t["monthly_budget_usd"] for t in usage["teams"]}
    show = notes["2026-09-03"]["detail"]
    assert "August" in notes["2026-09-03"]["title"]
    assert company.money(sum(spent.values())) in show
    best = max(ch.TEAMS, key=lambda t: 1 - spent[t] / budget[t])
    assert f"{ch.TEAMS[best]} finished {1 - spent[best] / budget[best]:.0%} under budget" in show
    # Regulated requests on-prem are exactly the regulated apps' requests, every day.
    regulated = [app for app, s in ch.APPS.items() if s["regulated"]]
    for d in usage["days"]:
        assert d["regulated_on_prem"] == sum(d["apps"][x]["requests"] for x in regulated)
        assert d["fallbacks"] == sum(x["fallbacks"] for x in d["apps"].values())
        assert d["failed"] == sum(x["failed"] for x in d["apps"].values())


def test_spend_stays_inside_every_cap(usage, log, cfg):
    b = cfg["budgets"]
    for t in usage["teams"]:  # Spend's budgets are routes.yaml's monthly caps
        assert (t["monthly_budget_usd"], t["daily_cap_usd"]) == (
            b["teams"][t["team"]]["monthly_usd"],
            b["teams"][t["team"]]["daily_usd"],
        )
    assert {a["app"]: a["key_daily_cap_usd"] for a in usage["apps"]} == {
        k: v["daily_usd"] for k, v in b["keys"].items()
    }
    for d in usage["days"]:
        assert d["spend_usd"] <= b["org"]["daily_usd"]
        for team, t in d["teams"].items():
            assert t["spend_usd"] <= b["teams"][team]["daily_usd"], (d["date"], team)
        for app, x in d["apps"].items():
            assert x["spend_usd"] <= b["keys"][app]["daily_usd"], (d["date"], app)
    months = defaultdict(float)
    for d in usage["days"]:
        months[d["date"][:7]] += d["spend_usd"]
    assert max(months.values()) <= b["org"]["monthly_usd"]
    for r in log["requests"]:
        stage = next((s for s in r["stages"] if s["stage"] == "budgets"), None)
        if stage:
            assert stage["detail"]["key_today_usd"] <= stage["detail"]["key_daily_cap_usd"]
            assert f"of ${stage['detail']['key_daily_cap_usd']:,.2f} today" in stage["summary"]
    assert usage["anomaly"]["binding_cap"] and usage["anomaly"]["prevented_usd"] > 0


def test_volumes_are_believable_for_the_credit_unions_size(usage):
    last30 = usage["days"][-30:]
    per_day = sum(d["requests"] for d in last30) / 30
    assert per_day / usage["company"]["members"] < 0.25  # well under one AI request per member per day
    assert usage["company"]["regulators"] == ["NCUA", "Florida OFR"] and "CFPB" in usage["company"]["consumer_rules"]


def test_eval_cases_are_credit_union_tasks_with_realistic_prompt_sizes():
    assert build_eval_cases.main(["--check"]) == 0
    sizes = defaultdict(list)
    for c in build_eval_cases.build():
        assert len(c["tags"]) == 1 and c["tags"][0] in ch.APPS
        sizes[c["tags"][0]].append(sum(len(m["content"]) for m in c["messages"]) // 4)  # the simulator's estimate
    for app, values in sizes.items():
        app_tokens = min(t[0] for t in ch.APPS[app]["tokens"].values())
        assert 0.6 <= sum(values) / len(values) / app_tokens <= 1.4, app
    gate = json.loads((DATA / "eval_gate.json").read_text())
    assert gate["baseline"]["alias"] == ch.primary_alias("member-assistant")
    assert gate["candidate"]["alias"] == ch.primary_alias("fraud-alert-narratives")


def test_mcp_examples_are_the_same_in_the_engine_and_the_page():
    page = (ROOT / "demo" / "screens.js").read_text()
    for tool, args in engine.DEMO_MCP_ARGS.items():
        assert tool in page
        for value in args.values():
            assert json.dumps(value) in page, (tool, value)
    assert set(engine.DEMO_MCP_TOOLS) == set(yaml.safe_load((ROOT / "config" / "mcp.mock.yaml").read_text())["servers"])


# Words from the generic shop / SaaS / healthcare story the console used to tell. None belong in what a visitor sees.
DENYLIST = [
    "ticket", "invoice", "kettle", "bakery", "larkspur", "copperleaf", "nimbus", "riverton", "charger", "glims",
    "rotate an api key", "support team", "fraud-scoring", "phi-safe", "hipaa", "t-1042", "q3-summary",
    "subscription costs", "ferngully", "bluefin", "customer",
]  # fmt: skip


def test_no_generic_strings_in_demo_facing_data():
    files = [*DATA.glob("*.json"), *(ROOT / "evals").glob("*.jsonl"), *(ROOT / "config").glob("*.yaml"),
             *(ROOT / "demo").glob("*.js"), ROOT / "demo" / "engine.py", ROOT / "demo" / "cypress_harbor.py",
             ROOT / "demo" / "index.html"]  # fmt: skip
    for f in files:
        text = f.read_text().lower()
        for word in DENYLIST:
            assert not re.search(re.escape(word), text), f"{f.relative_to(ROOT)} mentions {word!r}"
