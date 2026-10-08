# Corey Mathie, 2026
"""
Write demo/data/sample_company.json: 90 days of AI traffic through the gateway at a fictional
mid-size credit union, so the console's Business impact view shows it at a realistic scale.

Cypress Harbor Credit Union does not exist. Every number in the file is generated here from a
fixed seed and the assumptions below; none of it is a measurement of this repo or of any real
institution. The console labels it "Sample company data" wherever it appears, and keeps it apart
from the measured eval results (Evals) and the requests simulated in the browser (This session).

    python scripts/generate_sample_company.py            # write the file
    python scripts/generate_sample_company.py --check    # exit 1 if the committed file differs

The model, in short:
- Seven teams run AI features through one gateway, each with a monthly budget. Traffic follows
  the working week; the member assistant also runs nights and weekends.
- Spend per request depends on each team's model mix; the exact-match cache serves repeated
  questions at $0.
- Two provider incidents (September 16, October 6) are absorbed by fallback; one runaway batch job
  (September 24) is paused by spend-anomaly detection, and the spend it would have caused is estimated.
- Regulated work (dispute evidence in risk analytics, BSA case notes in compliance) runs only on the
  on-prem model.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "data" / "sample_company.json"
SEED = 20261009
END = date(2026, 10, 7)
DAYS = 90

COMPANY = {
    "name": "Cypress Harbor Credit Union",
    "short": "Cypress Harbor CU",
    "fictional": True,
    "industry": "Credit union (financial services)",
    "headquarters": "Fort Lauderdale, Florida",
    "members": 92400,
    "assets_usd": 1_400_000_000,
    "employees": 340,
    "branches": 11,
    "providers": ["Anthropic", "OpenAI", "Google Gemini", "On-prem Llama (Ollama)"],
    "regulators": ["NCUA", "CFPB", "Florida OFR"],
}

# team: (apps, workday requests, weekend share, dollars per 1K requests, monthly budget, cache hit rate)
TEAMS = [
    ("member-services", ["member-assistant", "agent-assist"], 31000, 0.55, 3.10, 3600, 0.21),
    ("digital-banking", ["online-banking", "mobile-banking"], 26000, 0.62, 2.40, 2600, 0.17),
    ("risk-analytics", ["fraud-scoring-batch", "dispute-triage"], 14000, 0.9, 6.80, 4200, 0.04),
    ("lending", ["loan-doc-extraction"], 5200, 0.08, 9.50, 2000, 0.06),
    ("compliance", ["reg-change-digest", "bsa-case-notes"], 900, 0.0, 14.0, 700, 0.09),
    ("it-engineering", ["code-assistant"], 4200, 0.05, 4.90, 1000, 0.12),
    ("marketing", ["content-drafts"], 1100, 0.02, 7.20, 500, 0.08),
]

MODELS = [
    ("anthropic/claude-haiku-4-5", 0.34),
    ("openai/gpt-4.1-mini", 0.24),
    ("anthropic/claude-sonnet-4-5", 0.14),
    ("gemini/gemini-2.5-flash", 0.11),
    ("ollama/llama3.1:8b (on-prem)", 0.17),
]

ASSUMPTIONS = {
    "note": "Request volumes, per-request prices by team and budgets are illustrative assumptions for the "
    "sample company. Spend prevented by the anomaly pause is the paused job's hourly rate times the hours it "
    "would have run until someone read the invoice (estimated, not measured).",
}


def _day(rng: random.Random, d: date, i: int) -> dict:
    weekday = d.weekday()
    workday = weekday < 5 and d != date(2026, 9, 7)
    growth = 1 + 0.18 * (i / (DAYS - 1))
    teams = {}
    for name, _apps, req, weekend, per_k, _budget, hit in TEAMS:
        n = int(req * growth * (1 if workday else weekend) * rng.uniform(0.92, 1.08))
        cached = int(n * hit * rng.uniform(0.9, 1.1))
        spend = round((n - cached) / 1000 * per_k * rng.uniform(0.95, 1.05), 2)
        saved = round(cached / 1000 * per_k, 2)
        teams[name] = {"requests": n, "cached": cached, "spend_usd": spend, "saved_usd": saved}
    if d == date(2026, 9, 24):  # the runaway batch: about two hours before the pause
        teams["risk-analytics"]["requests"] += 21000
        teams["risk-analytics"]["spend_usd"] = round(teams["risk-analytics"]["spend_usd"] + 192.40, 2)
    requests = sum(t["requests"] for t in teams.values())
    incident = d == date(2026, 9, 16)
    usual = rng.uniform(0.002, 0.006)
    # October 6: twelve minutes of Anthropic overload; smart-fast traffic fell back to gpt-4.1-mini.
    fallbacks = int(requests * (0.031 if incident else usual + 0.0045 if d == date(2026, 10, 6) else usual))
    failed = int(requests * (0.0004 if incident else rng.uniform(0.00002, 0.00008)))
    regulated = int(teams["risk-analytics"]["requests"] * 0.4 + teams["compliance"]["requests"] * 0.35)
    return {
        "date": d.isoformat(),
        "requests": requests,
        "spend_usd": round(sum(t["spend_usd"] for t in teams.values()), 2),
        "saved_usd": round(sum(t["saved_usd"] for t in teams.values()), 2),
        "fallbacks": fallbacks,
        "failed": failed,
        "policy_denied": rng.randint(4, 19) if workday else rng.randint(0, 4),
        "pii_redacted": int(teams["member-services"]["requests"] * rng.uniform(0.018, 0.026)),
        "regulated_on_prem": regulated,
        "anomaly_pauses": 1 if d == date(2026, 9, 24) else 0,
        "p50_overhead_ms": round(rng.uniform(8.5, 10.5), 1),
        "teams": teams,
    }


def build() -> dict:
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    days = [_day(rng, start + timedelta(days=i), i) for i in range(DAYS)]
    teams = [
        {"team": name, "apps": apps, "monthly_budget_usd": budget, "cache_hit_rate": hit}
        for name, apps, _r, _w, _p, budget, hit in TEAMS
    ]
    spend30 = sum(d["spend_usd"] for d in days[-30:])
    models = [{"model": m, "spend_30d_usd": round(spend30 * s * rng.uniform(0.95, 1.05), 2)} for m, s in MODELS]
    incidents = [
        {
            "date": "2026-10-06",
            "provider": "Anthropic",
            "duration_minutes": 12,
            "what": "Overloaded (529) responses on claude-haiku-4-5",
            "handled": "Each failed attempt fell through to gpt-4.1-mini within the same request; members saw "
            "about a second of extra latency and no errors.",
        },
        {
            "date": "2026-09-16",
            "provider": "OpenAI",
            "duration_minutes": 47,
            "what": "Elevated 5xx and latency on gpt-4.1-mini",
            "handled": "Circuit breaker opened after 3 failures; traffic fell back to Claude Haiku. "
            "99.6% of affected requests still succeeded.",
        },
        {
            "date": "2026-07-29",
            "provider": "Google Gemini",
            "duration_minutes": 12,
            "what": "Rate-limit (429) burst in us-east",
            "handled": "Breaker half-open probes recovered the route; no member-facing errors.",
        },
    ]
    notable = [
        {
            "date": "2026-10-06",
            "kind": "reliability",
            "title": "Twelve minutes of Anthropic overload, no member-facing errors",
            "detail": "claude-haiku-4-5 returned 529s from 2:05 to 2:17 pm; smart-fast requests fell back to "
            "gpt-4.1-mini within the same call.",
        },
        {
            "date": "2026-10-02",
            "kind": "policy",
            "title": "Contractor key blocked from Sonnet-class models",
            "detail": "A vendor's code-assistant key asked for claude-sonnet-4-5; the contractors policy denied it "
            "and the request fell to an allowed alias.",
        },
        {
            "date": "2026-09-24",
            "kind": "risk",
            "title": "Runaway fraud-scoring batch paused at 10.2x its baseline",
            "detail": "A retry loop multiplied requests; anomaly detection paused the key within the hour. "
            "Estimated spend prevented: $2,310 before the next business day.",
        },
        {
            "date": "2026-09-16",
            "kind": "reliability",
            "title": "Provider incident absorbed by fallback",
            "detail": "47 minutes of OpenAI errors; the member assistant and online banking chat kept answering "
            "through Claude Haiku.",
        },
        {
            "date": "2026-09-03",
            "kind": "finops",
            "title": "September showback sent to department heads",
            "detail": "CSV export by team and app; risk analytics came in 9% under budget after moving dispute "
            "triage to the cheap-batch route.",
        },
        {
            "date": "2026-08-11",
            "kind": "policy",
            "title": "Regulated workloads pinned to on-prem models",
            "detail": "BSA and dispute-evidence prompts now route only to the on-prem Llama deployment; "
            "fallback can't reach a cloud provider.",
        },
    ]
    return {
        "generated_by": "scripts/generate_sample_company.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated data for demonstration, not measurements.",
        "period": {"start": start.isoformat(), "end": END.isoformat(), "days": DAYS},
        "company": COMPANY,
        "assumptions": ASSUMPTIONS,
        "teams": teams,
        "days": days,
        "models": sorted(models, key=lambda x: -x["spend_30d_usd"]),
        "incidents": incidents,
        "anomaly": {"date": "2026-09-24", "team": "risk-analytics", "key": "fraud-scoring-batch",
                    "baseline_multiple": 10.2, "prevented_usd": 2310.0},
        "governance": {
            "requests_with_attribution": 1.0,
            "audit_chain_verified_rate": 1.0,
            "content_logging_default": "off (metadata only)",
            "keys_rotated_90d": 14,
        },
        "notable": notable,
    }  # fmt: skip


def render(data: dict) -> str:
    return json.dumps(data, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/generate_sample_company.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    d = json.loads(text)["days"][-30:]
    spend = sum(x["spend_usd"] for x in d)
    print(f"wrote {OUT.relative_to(ROOT)}: ${spend:,.0f} spend, {sum(x['requests'] for x in d):,} requests in 30 days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
