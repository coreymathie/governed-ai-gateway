# Corey Mathie, 2026
"""The console's sample request log: reproducible, priced from tokens, and consistent with the sample company."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

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


def test_each_teams_cost_per_thousand_is_close_to_the_sample_companys_assumption(log):
    assumed = {name: per_k for name, _apps, _req, _we, per_k, _b, _hit in company.TEAMS}
    spend, count = defaultdict(float), defaultdict(int)
    for r in log["requests"]:
        if r["outcome"] == "ok":
            spend[r["team"]] += r["cost_usd"]
            count[r["team"]] += 1
    assert set(count) == set(assumed)
    for team, n in count.items():
        per_k = spend[team] / n * 1000
        assert 0.7 <= per_k / assumed[team] <= 1.3, (team, round(per_k, 2), assumed[team])


def test_apps_belong_to_their_team_and_regulated_work_stays_on_prem(log):
    teams = {t["team"]: set(t["apps"]) for t in company.build()["teams"]}
    for r in log["requests"]:
        assert r["app"] in teams[r["team"]]
        if r["regulated"]:
            assert r["alias"] == "regulated-fast"
            assert r["served_by"] in (None, "ollama/llama3.1:8b")


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


def test_the_activity_feed_stories_are_in_the_log(log):
    reqs = log["requests"]
    denied = [r for r in reqs if r["status"] == 403]
    assert any(r["key"] == "vendor-code-assist" and r["ts"].startswith("2026-10-02") for r in denied)
    deny = next(s for s in denied[0]["stages"] if s["stage"] == "policy")
    assert deny["decision"] == "deny" and "contractors" in deny["summary"]
    start, end = "2026-10-06T18:05:00Z", "2026-10-06T18:17:00Z"
    in_window = [r for r in reqs if start <= r["ts"] <= end and r["outcome"] == "ok"]
    assert len(in_window) >= 8
    for r in in_window:
        first = next(s for s in r["stages"] if s["stage"] == "chain")["detail"]["attempts"][0]
        if first["deployment"] == "anthropic/claude-haiku-4-5":  # haiku was overloaded: every such request fell back
            assert first["outcome"] == "error" and first["error"] == "529 overloaded"
            assert r["fell_back"] and r["served_by"] == "openai/gpt-4.1-mini"
    assert sum(r["fell_back"] for r in in_window) >= 6
    assert any(r["redacted"] for r in reqs)


def test_no_prompt_or_completion_text_is_stored(log):
    text = json.dumps(log).lower()
    for field in ('"prompt"', '"completion"', '"messages"', '"content"'):
        assert field not in text
