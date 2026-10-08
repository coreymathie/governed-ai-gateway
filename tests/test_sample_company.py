# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
from pathlib import Path

from scripts import generate_sample_company as gen

ROOT = Path(__file__).resolve().parents[1]


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_generation_is_deterministic():
    assert gen.render(gen.build()) == gen.render(gen.build())


def test_daily_totals_are_the_sum_of_the_teams():
    data = gen.build()
    assert len(data["days"]) == gen.DAYS
    names = {t["team"] for t in data["teams"]}
    for d in data["days"]:
        assert set(d["teams"]) == names
        assert d["requests"] == sum(t["requests"] for t in d["teams"].values())
        assert abs(d["spend_usd"] - sum(t["spend_usd"] for t in d["teams"].values())) < 0.02
        assert all(t["cached"] <= t["requests"] for t in d["teams"].values())
        assert d["failed"] <= d["fallbacks"] <= d["requests"]


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    data = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())
    assert data["company"]["fictional"] is True
    assert "not measurements" in data["disclaimer"]
    assert "estimated" in data["assumptions"]["note"]
