# Corey Mathie, 2026
"""The console's Evals screen reads demo/data/*.json; they must match what the repo's eval code produces now."""

import json
from pathlib import Path

from scripts import build_console_data

DATA = Path(__file__).resolve().parent.parent / "demo" / "data"


def test_committed_console_data_matches_the_eval_scripts():
    fresh = build_console_data.build(run_tests=False)
    for name, data in fresh.items():
        committed = json.loads((DATA / name).read_text())
        if name == "tests.json":
            committed.pop("last_run", None)
            data = {k: v for k, v in data.items() if k != "last_run"}
        stale = f"demo/data/{name} is stale: run scripts/build_console_data.py"
        assert _same(committed, json.loads(json.dumps(data))), stale


def _same(a, b, key: str = "") -> bool:
    """Equal, except latencies (they include real microseconds spent on simulated failures) within 1 ms."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], k) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y, key) for x, y in zip(a, b, strict=True))
    if "latency" in key and isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-3
    return a == b


def test_console_data_labels_what_is_simulated_and_what_is_measured():
    routes = json.loads((DATA / "eval_routes.json").read_text())
    assert routes["provider"] == "simulated" and routes["measured"].startswith("simulated")
    cal = json.loads((DATA / "semcache_calibration.json").read_text())
    assert cal["measured"].startswith("measured") and cal["chosen"]["threshold"] == 0.90
    gate = json.loads((DATA / "eval_gate.json").read_text())
    assert gate["ok"] is False and gate["measured"].startswith("simulated")
