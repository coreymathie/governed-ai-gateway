# Corey Mathie, 2026
"""
Write the JSON the console's Evals screen reads (demo/data/), using the repository's own eval code.

    python scripts/build_console_data.py               # route eval, gate example, calibration, test inventory
    python scripts/build_console_data.py --run-tests   # also run pytest -q and record its summary line

Outputs (deterministic except the optional pytest record):
    demo/data/eval_routes.json        scripts/eval_routes.py defaults: every alias, simulated providers
    demo/data/eval_gate.json          scripts/eval_gate.py logic: the member assistant's smart-fast vs. cheap-batch
    demo/data/semcache_calibration.json   scripts/calibrate_semcache.py defaults (bundled fictional pairs)
    demo/data/tests.json              test functions per file (counted from the source), plus the last
                                      recorded pytest summary and collected count when --run-tests is given

tests/test_console_data.py fails if the committed files drift from what this script produces.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from router import evalrun, evals  # noqa: E402
from scripts import calibrate_semcache  # noqa: E402

OUT = ROOT / "demo" / "data"


def route_eval() -> dict:
    rep = evalrun.run(ROOT / "config/routes.yaml", ROOT / "evals/sample_cases.jsonl", None, "sim",
                      ROOT / "evals/sim_profiles.json", "eval-v1")  # fmt: skip
    rep["case_count"] = len(next(iter(rep.pop("cases").values()), []))  # per-case rows aren't shown; summaries are
    rep["routes_file"] = "config/routes.yaml"
    rep["cases_source"] = "evals/sample_cases.jsonl"
    rep["command"] = "python scripts/eval_routes.py"
    rep["measured"] = "simulated: invented model profiles (evals/sim_profiles.json), deterministic"
    return rep


def gate_example(rep: dict) -> dict:
    base, cand = rep["routes"]["smart-fast"]["summary"], rep["routes"]["cheap-batch"]["summary"]
    result = evals.gate(base, cand, 0.02, 0.10)
    return {
        "question": "Would moving the member assistant from smart-fast to cheap-batch, the route the nightly "
        "fraud-alert batch uses, pass the CI gate?",
        "command": "python scripts/eval_gate.py --baseline-report r.json --candidate-report r.json "
        "--alias smart-fast --candidate-alias cheap-batch",
        "baseline": {"alias": "smart-fast", **{k: base[k] for k in ("quality", "total_cost_usd", "errors")}},
        "candidate": {"alias": "cheap-batch", **{k: cand[k] for k in ("quality", "total_cost_usd", "errors")}},
        "limits": {"max_quality_drop": 0.02, "max_cost_increase": 0.10},
        "measured": "simulated: same profiles and cases as eval_routes.json",
        **result.as_dict(),
    }


def calibration() -> dict:
    rep = calibrate_semcache.run(str(ROOT / "evals/semcache_pairs.jsonl"), 0.01, 0.50, 0.99, 0.01)
    rep["pairs_file"] = "evals/semcache_pairs.jsonl"
    rep["command"] = "python scripts/calibrate_semcache.py"
    rep["measured"] = (
        "measured: the hashing embedder on 160 bundled fictional credit-union pairs (not production traffic)"
    )
    return rep


def test_inventory() -> list[dict]:
    out = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(f.read_text())
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                 and n.name.startswith("test_")]  # fmt: skip
        doc = (ast.get_docstring(tree) or "").strip().split("\n")[0]
        out.append({"file": f"tests/{f.name}", "tests": len(names), "about": doc})
    return out


def pytest_summary() -> dict:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    lines = [ln for ln in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    line = lines[-1].strip("= ") if lines else "no summary"
    line = re.sub(r" in [\d.]+s.*$", "", line)
    collected = sum(int(n) for n in re.findall(r"(\d+) (?:passed|failed|skipped|error|errors|xfailed|xpassed)", line))
    return {
        "summary": line,
        "collected": collected,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "exit_code": proc.returncode,
    }


def build(run_tests: bool = False) -> dict[str, dict]:
    rep = route_eval()
    tests = {"files": test_inventory()}
    tests["total"] = sum(f["tests"] for f in tests["files"])
    tests["measured"] = "counted from the test sources; CI runs pytest -q on every push"
    previous = OUT / "tests.json"
    if run_tests:
        tests["last_run"] = pytest_summary()
    elif previous.is_file():
        tests["last_run"] = json.loads(previous.read_text()).get("last_run")
    return {
        "eval_routes.json": rep,
        "eval_gate.json": gate_example(rep),
        "semcache_calibration.json": calibration(),
        "tests.json": tests,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write demo/data/*.json for the console's Evals screen.")
    ap.add_argument("--run-tests", action="store_true", help="run pytest -q and record the summary line")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build(args.run_tests).items():
        (OUT / name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        print(f"wrote demo/data/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
