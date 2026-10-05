# Corey Mathie, 2026
"""
Calibrate the semantic-cache similarity threshold on a labelled pair set.

    python scripts/calibrate_semcache.py                          # bundled pairs, hashing embedder, target 1%
    python scripts/calibrate_semcache.py --target 0.02 --out reports/semcache

Splits the pairs deterministically into calibration and holdout halves, measures
hit rate (share of same-meaning pairs that would be served from cache) and
false-hit rate (share of cache hits that would be the wrong answer) at each
threshold on the calibration half, picks the lowest threshold at which it and
every higher threshold meet the target, then reports the same metrics for that
threshold on the holdout half, which played no part in the choice.

The numbers describe this embedder on this pair set. The bundled set is 160
short, fictional, hand-written pairs; production traffic will differ, so
re-run this on pairs labelled from your own logs before trusting a threshold.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import semcache


def run(pairs_file: str, target: float, lo: float, hi: float, step: float, conservative: bool = False) -> dict:
    pairs = semcache.load_pairs(Path(pairs_file).read_text().splitlines())
    rep = semcache.calibration_report(pairs, target, semcache.threshold_grid(lo, hi, step), conservative)
    return {"pairs_file": pairs_file, **rep}


def to_markdown(rep: dict) -> str:
    p = rep["pairs"]
    lines = [
        "# Semantic cache calibration",
        "",
        f"Embedder `{rep['embedder']}` on `{rep['pairs_file']}`: {p['total']} labelled pairs (fictional), "
        f"{p['calibration']} for calibration ({rep['positives']['calibration']} same-meaning), "
        f"{p['holdout']} held out ({rep['positives']['holdout']} same-meaning).",
        f"Target false-hit rate: {rep['target_false_hit_rate']:.1%} of cache hits ({rep['rule']}).",
        "",
        "| Threshold | Hits | Wrong hits | Hit rate | False-hit rate | 95% upper bound |",
        "|---|---|---|---|---|---|",
    ]
    for r in rep["calibration_grid"]:
        if round(r["threshold"] * 100) % 5 == 0 or (rep["chosen"] and r["threshold"] == rep["chosen"]["threshold"]):
            mark = " **(chosen)**" if rep["chosen"] and r["threshold"] == rep["chosen"]["threshold"] else ""
            lines.append(
                f"| {r['threshold']:.2f}{mark} | {r['hits']} | {r['false_hits']} | {r['hit_rate']:.1%} | "
                f"{r['false_hit_rate']:.1%} | {r['false_hit_rate_upper95']:.1%} |"
            )
    if rep["chosen"] is None:
        lines += ["", "No threshold meets the target on the calibration half."]
        return "\n".join(lines) + "\n"
    h, g = rep["holdout_at_chosen"], rep["holdout_without_guards_at_chosen"]
    lines += [
        "",
        f"Holdout at {rep['chosen']['threshold']:.2f}: hit rate {h['hit_rate']:.1%} "
        f"({h['true_hits']} of {rep['positives']['holdout']} same-meaning pairs), "
        f"false-hit rate {h['false_hit_rate']:.1%} ({h['false_hits']} of {h['hits']} hits; "
        f"95% upper bound {h['false_hit_rate_upper95']:.1%}).",
        f"Same threshold without the number/negation guards: hit rate {g['hit_rate']:.1%}, "
        f"false-hit rate {g['false_hit_rate']:.1%} ({g['false_hits']} of {g['hits']} hits).",
        "",
        "Measured on the bundled synthetic pair set; not a prediction for production traffic.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Calibrate the semantic-cache threshold on labelled pairs.")
    ap.add_argument("--pairs", default="evals/semcache_pairs.jsonl")
    ap.add_argument("--target", type=float, default=0.01, help="max share of cache hits that may be wrong")
    ap.add_argument("--lo", type=float, default=0.50)
    ap.add_argument("--hi", type=float, default=0.99)
    ap.add_argument("--step", type=float, default=0.01)
    ap.add_argument("--conservative", action="store_true", help="require the 95%% upper bound to meet the target")
    ap.add_argument("--out", help="write <out>.json and <out>.md")
    args = ap.parse_args(argv)
    try:
        rep = run(args.pairs, args.target, args.lo, args.hi, args.step, args.conservative)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    md = to_markdown(rep)
    print(md)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(json.dumps(rep, indent=2) + "\n")
        out.with_suffix(".md").write_text(md)
    return 0 if rep["chosen"] else 1


if __name__ == "__main__":
    sys.exit(main())
