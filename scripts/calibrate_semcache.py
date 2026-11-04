# Corey Mathie, 2026
"""
Calibrate the semantic-cache similarity threshold on a labelled pair set.

    python scripts/calibrate_semcache.py                          # bundled pairs, hashing embedder, target 1%
    python scripts/calibrate_semcache.py --guards v1              # the guards before the opposite-action/entity checks
    python scripts/calibrate_semcache.py --target 0.02 --out reports/semcache

Splits the development pairs deterministically into calibration and holdout
halves, measures hit rate (share of same-meaning pairs that would be served
from cache) and false-hit rate (share of cache hits that would be the wrong
answer) at each threshold on the calibration half, picks the lowest threshold
at which it and every higher threshold meet the target, then reports the same
metrics for that threshold on the development holdout half and on a separate
held-out set (evals/semcache_heldout.jsonl), which played no part in the pick
or in the design of the guards.

The development pairs were read while the guards were designed, so the
development holdout half is no longer an independent test; the held-out set
is. The numbers describe this embedder on fictional pairs; production
traffic will differ, so re-run this on pairs labelled from your own logs
before trusting a threshold.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import semcache

ROOT = Path(__file__).resolve().parent.parent
GUARD_SETS = {"current": semcache.GUARDS, "v1": semcache.GUARDS_V1}
HELDOUT = "evals/semcache_heldout.jsonl"


def run(
    pairs_file: str,
    target: float,
    lo: float,
    hi: float,
    step: float,
    conservative: bool = False,
    heldout_file: str | None = HELDOUT,
    guards: str = "current",
) -> dict:
    pairs = semcache.load_pairs(Path(pairs_file).read_text().splitlines())
    heldout = None
    if heldout_file:
        path = Path(heldout_file) if Path(heldout_file).exists() else ROOT / heldout_file
        heldout = semcache.load_pairs(path.read_text().splitlines())
    rep = semcache.calibration_report(
        pairs, target, semcache.threshold_grid(lo, hi, step), conservative, heldout=heldout, guards=GUARD_SETS[guards]
    )
    return {"pairs_file": pairs_file, "heldout_file": heldout_file, "guard_set": guards, **rep}


def _line(name: str, r: dict, positives: int) -> str:
    return (
        f"| {name} | {r['hits']} | {r['false_hits']} | {r['false_hit_rate']:.1%} | {r['false_hit_rate_upper95']:.1%} "
        f"| {r['hit_rate']:.1%} ({r['true_hits']} of {positives}) |"
    )


def to_markdown(rep: dict) -> str:
    p = rep["pairs"]
    lines = [
        "# Semantic cache calibration",
        "",
        f"Embedder `{rep['embedder']}`, guards `{rep['guard_set']}` ({', '.join(rep['guards'])}). Development set "
        f"`{rep['pairs_file']}`: {p['total']} labelled pairs (fictional), {p['calibration']} for calibration "
        f"({rep['positives']['calibration']} same-meaning), {p['holdout']} in the development holdout half "
        f"({rep['positives']['holdout']} same-meaning).",
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
    t = rep["chosen"]["threshold"]
    h, g = rep["holdout_at_chosen"], rep["holdout_without_guards_at_chosen"]
    lines += [
        "",
        f"At the chosen threshold {t:.2f}:",
        "",
        "| Pair set | Hits | Wrong hits | False-hit rate | 95% upper bound | Hit rate |",
        "|---|---|---|---|---|---|",
        _line("Development holdout half", h, rep["positives"]["holdout"]),
        _line("Development holdout half, no guards", g, rep["positives"]["holdout"]),
    ]
    ho = rep.get("heldout")
    if ho and ho["at_chosen"]:
        lines += [
            _line(f"Held-out set ({ho['pairs']} pairs)", ho["at_chosen"], ho["positives"]),
            _line("Held-out set, no guards", ho["without_guards_at_chosen"], ho["positives"]),
        ]
    lines += [
        "",
        f"Holdout at {t:.2f}: hit rate {h['hit_rate']:.1%}, false-hit rate {h['false_hit_rate']:.1%} "
        f"({h['false_hits']} of {h['hits']} hits; 95% upper bound {h['false_hit_rate_upper95']:.1%}).",
    ]
    if ho and ho["at_chosen"]:
        a = ho["at_chosen"]
        lines.append(
            f"Held-out set at {t:.2f}: hit rate {a['hit_rate']:.1%}, false-hit rate {a['false_hit_rate']:.1%} "
            f"({a['false_hits']} of {a['hits']} hits; 95% upper bound {a['false_hit_rate_upper95']:.1%})."
        )
    lines += ["", "Measured on the bundled synthetic pair sets; not a prediction for production traffic."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Calibrate the semantic-cache threshold on labelled pairs.")
    ap.add_argument("--pairs", default="evals/semcache_pairs.jsonl", help="development pairs (pick and holdout half)")
    ap.add_argument("--heldout", default=HELDOUT, help="separate held-out pairs; '' to skip")
    ap.add_argument("--guards", choices=sorted(GUARD_SETS), default="current", help="guard set to measure")
    ap.add_argument("--target", type=float, default=0.01, help="max share of cache hits that may be wrong")
    ap.add_argument("--lo", type=float, default=0.50)
    ap.add_argument("--hi", type=float, default=0.99)
    ap.add_argument("--step", type=float, default=0.01)
    ap.add_argument("--conservative", action="store_true", help="require the 95%% upper bound to meet the target")
    ap.add_argument("--out", help="write <out>.json and <out>.md")
    args = ap.parse_args(argv)
    try:
        rep = run(
            args.pairs, args.target, args.lo, args.hi, args.step, args.conservative, args.heldout or None, args.guards
        )
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
