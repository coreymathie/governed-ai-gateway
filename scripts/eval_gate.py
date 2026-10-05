# Corey Mathie, 2026
"""
CI gate for route changes: fail when quality drops or cost rises beyond a budget.

Compare one alias under two routes files (runs both evaluations, simulated by default):

    git show origin/main:config/routes.yaml > /tmp/routes_main.yaml
    python scripts/eval_gate.py --routes-before /tmp/routes_main.yaml --routes-after config/routes.yaml \\
        --alias smart-fast --max-quality-drop 0.02 --max-cost-increase 0.10

or two existing reports from scripts/eval_routes.py:

    python scripts/eval_gate.py --baseline-report base.json --candidate-report cand.json --alias smart-fast

Exit 0 = pass, 1 = gate failed, 2 = usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import evalrun, evals


def _summary(rep: dict, alias: str) -> dict:
    if alias not in rep.get("routes", {}):
        raise evals.EvalConfigError(f"alias {alias!r} not in report (has {sorted(rep.get('routes', {}))})")
    return rep["routes"][alias]["summary"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail a route change on a quality drop or cost rise.")
    ap.add_argument("--alias", required=True)
    ap.add_argument("--candidate-alias", help="compare against a different alias in the candidate (default: --alias)")
    ap.add_argument("--routes-before")
    ap.add_argument("--routes-after")
    ap.add_argument("--baseline-report")
    ap.add_argument("--candidate-report")
    ap.add_argument("--cases", default="evals/sample_cases.jsonl")
    ap.add_argument("--provider", choices=["sim", "real"], default="sim")
    ap.add_argument("--profiles", default="evals/sim_profiles.json")
    ap.add_argument("--seed", default="eval-v1")
    ap.add_argument("--max-quality-drop", type=float, default=0.02, help="absolute, on a 0-1 scale")
    ap.add_argument("--max-cost-increase", type=float, default=0.10, help="fraction, 0.10 = +10%%")
    ap.add_argument("--out", help="write the gate result (.json and .md) to this prefix")
    args = ap.parse_args(argv)
    cand_alias = args.candidate_alias or args.alias
    try:
        if args.routes_before and args.routes_after:
            base = evalrun.run(args.routes_before, args.cases, [args.alias], args.provider, args.profiles, args.seed)
            cand = evalrun.run(args.routes_after, args.cases, [cand_alias], args.provider, args.profiles, args.seed)
        elif args.baseline_report and args.candidate_report:
            base = json.loads(Path(args.baseline_report).read_text())
            cand = json.loads(Path(args.candidate_report).read_text())
        else:
            ap.error("give --routes-before/--routes-after or --baseline-report/--candidate-report")
        if base.get("provider") != cand.get("provider"):
            raise evals.EvalConfigError("baseline and candidate came from different providers (simulated vs real)")
        result = evals.gate(
            _summary(base, args.alias), _summary(cand, cand_alias), args.max_quality_drop, args.max_cost_increase
        )
    except (evals.EvalConfigError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    b, c = _summary(base, args.alias), _summary(cand, cand_alias)
    lines = [
        f"# Route gate: `{args.alias}`" + (f" vs `{cand_alias}`" if cand_alias != args.alias else ""),
        "",
        f"Provider: **{base.get('provider')}**",
        "",
        "| | Quality | Cost (USD) | Errors |",
        "|---|---|---|---|",
        f"| baseline | {b['quality']} | {b['total_cost_usd']:.6f} | {b['errors']} |",
        f"| candidate | {c['quality']} | {c['total_cost_usd']:.6f} | {c['errors']} |",
        "",
        f"Limits: quality drop ≤ {args.max_quality_drop}, cost increase ≤ {args.max_cost_increase:.0%}",
        f"Result: **{'PASS' if result.ok else 'FAIL'}**",
        *[f"- {r}" for r in result.reasons],
    ]
    md = "\n".join(lines) + "\n"
    print(md)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(
            json.dumps({"provider": base.get("provider"), **result.as_dict()}, indent=2)
        )
        out.with_suffix(".md").write_text(md)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
