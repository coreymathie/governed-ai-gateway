# Corey Mathie, 2026
"""
Replay an evaluation set across routes and write a cost-versus-quality report.

    python scripts/eval_routes.py                                   # every alias, simulated providers
    python scripts/eval_routes.py --aliases smart-fast,cheap-batch --out reports/eval
    python scripts/eval_routes.py --provider real --aliases cheap-batch   # only if every key is set

Writes <out>.json and <out>.md. Simulated runs are deterministic (same seed,
profiles and cases -> same numbers) and labelled "simulated": they exercise the
routes, fallback and pricing with invented model profiles, not real models.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import evalrun, evals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay an eval set across routes (cost vs. quality).")
    ap.add_argument("--routes", default="config/routes.yaml")
    ap.add_argument("--cases", default="evals/sample_cases.jsonl")
    ap.add_argument("--aliases", help="comma-separated; default: every alias in the routes file")
    ap.add_argument("--provider", choices=["sim", "real"], default="sim")
    ap.add_argument("--profiles", default="evals/sim_profiles.json", help="simulation profiles (sim only)")
    ap.add_argument("--seed", default="eval-v1")
    ap.add_argument("--judge", help="optional judge hook, module:function -> score in [0, 1]")
    ap.add_argument("--out", default="reports/eval", help="output path prefix (.json and .md are added)")
    args = ap.parse_args(argv)
    try:
        rep = evalrun.run(
            args.routes, args.cases, args.aliases.split(",") if args.aliases else None,
            args.provider, args.profiles, args.seed, args.judge,
        )  # fmt: skip
    except (evals.EvalConfigError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rep, indent=2) + "\n")
    md = evals.to_markdown(rep)
    out.with_suffix(".md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
