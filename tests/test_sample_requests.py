# Corey Mathie, 2026
"""The console's sample request log: reproducible, priced from tokens, and consistent with the sample company."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from demo import cypress_harbor as ch
from scripts import generate_sample_company as company
from scripts import sample_requests as gen

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def log():
    return json.loads((ROOT / "demo" / "data" / "sample_requests.json").read_text())


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_requests_are_unique_newest_first_and_inside_the_period(log):
    reqs = log["requests"]
    assert len(reqs) == gen.COUNT
    assert len({r["id"] for r in reqs}) == len(reqs)
    times = [r["ts"] for r in reqs]
    assert times == sorted(times, reverse=True)
    first, last = datetime(2026, 10, 1, 4), datetime(2026, 10, 8, 4)  # midnight to midnight, EDT
    assert all(first <= datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") < last for t in times)
    assert "not measurements" in log["disclaimer"]


def test_cost_is_tokens_times_the_price_of_the_model_that_answered(log):
    for r in log["requests"]:
        if r["outcome"] == "ok":
            assert r["cost_usd"] == pytest.approx(gen.cost(r["served_by"], r["prompt_tokens"], r["completion_tokens"]))
            assert r["saved_usd"] == 0
        elif r["outcome"] == "cache_hit":
            assert r["cost_usd"] == 0 and r["saved_usd"] > 0
        else:
            assert r["cost_usd"] == 0 and r["status"] >= 400 and r["reason"]


def test_each_apps_cost_per_request_matches_its_route_and_token_sizes(log):
    """The log prices requests from the same app table as the usage file: per app and alias, within sampling noise."""
    cfg = company.routes_config()
    routes = company.aliases(cfg)
    spend, count = defaultdict(float), defaultdict(int)
    for r in log["requests"]:
        if r["outcome"] == "ok" and not r["fell_back"]:
            spend[(r["app"], r["alias"])] += r["cost_usd"]
            count[(r["app"], r["alias"])] += 1
    checked = 0
    for (app, alias), n in count.items():
        if n < 10:
            continue
        targets = routes[alias]
        firsts = ch.first_choice_shares(alias, targets)
        implied = sum(f * company.unit_cost(app, alias, t) for f, t in zip(firsts, targets, strict=True))
        assert 0.7 <= spend[(app, alias)] / n / implied <= 1.3, (app, alias, n)
        checked += 1
    assert checked >= 6


def test_apps_belong_to_their_team_and_regulated_work_stays_on_prem(log):
    for r in log["requests"]:
        spec = ch.APPS[r["app"]]
        assert r["team"] == (ch.CONTRACTOR["team"] if r["key"] == ch.CONTRACTOR["key"] else spec["team"])
        assert r["alias"] in [a for a, _ in spec["routes"]]
        assert r["temperature"] == spec["temperature"]
        if r["regulated"]:
            assert r["alias"] == "regulated-fast"
            assert r["served_by"] in (None, "ollama/llama3.1:8b")
            policy_stage = next(s for s in r["stages"] if s["stage"] == "policy")
            assert "routes.regulated-fast" in policy_stage["detail"]["layers"]


def test_budget_stage_shows_the_teams_month_to_date_spend_from_the_sample_company(log):
    days = company.build()["days"]
    for r in log["requests"][:40]:
        stage = next((s for s in r["stages"] if s["stage"] == "budgets"), None)
        if stage is None:
            continue
        local_day = gen.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") + gen.EDT
        expected = sum(
            d["teams"][r["team"]]["spend_usd"] for d in days if "2026-10-01" <= d["date"] < local_day.date().isoformat()
        )
        assert stage["detail"]["team_month_usd"] == pytest.approx(expected, abs=0.01)
        assert stage["detail"]["key_today_usd"] <= stage["detail"]["key_daily_cap_usd"]


def test_the_activity_feed_stories_are_in_the_log(log):
    reqs = log["requests"][::-1]  # oldest first
    vendor = [r for r in reqs if r["key"] == ch.CONTRACTOR["key"]]
    denied, retry = vendor
    assert denied["status"] == 403 and denied["alias"] == "heavy-reasoning" and denied["team"] == "contractors"
    deny = next(s for s in denied["stages"] if s["stage"] == "policy")
    assert (
        deny["decision"] == "deny"
        and deny["summary"] == "alias 'heavy-reasoning' is not permitted for team 'contractors'"
    )
    gap = datetime.strptime(retry["ts"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(denied["ts"], "%Y-%m-%dT%H:%M:%SZ")
    assert gap.total_seconds() == 24 and retry["alias"] == "local-first" and retry["status"] == 200  # as the feed says

    start, end = "2026-10-06T18:05:00Z", "2026-10-06T18:17:00Z"
    in_window = [r for r in reqs if start <= r["ts"] <= end and r["outcome"] == "ok"]
    haiku = [r for r in in_window if next(s for s in r["stages"] if s["stage"] == "chain")["detail"]["attempts"][0]
             ["deployment"] == "anthropic/claude-haiku-4-5"]  # fmt: skip
    assert len(haiku) >= 8
    firsts = [next(s for s in r["stages"] if s["stage"] == "chain")["detail"]["attempts"][0] for r in haiku]
    assert firsts[0]["outcome"] == "error" and firsts[0]["error"] == "529 overloaded"  # before the breaker opened
    assert all(f["outcome"] == "skipped" and f["error"] == "circuit open" for f in firsts[1:])  # after it opened
    assert all(r["fell_back"] and r["served_by"] == "openai/gpt-4.1-mini" for r in haiku)
    timeout = [r for r in reqs if r["status"] == 504]
    assert [r["app"] for r in timeout] == ["bsa-case-notes"] and timeout[0]["ts"].startswith("2026-10-05")
    assert any(r["redacted"] for r in reqs)


def test_no_prompt_or_completion_text_is_stored(log):
    text = json.dumps(log).lower()
    for field in ('"prompt"', '"completion"', '"messages"', '"content"'):
        assert field not in text
