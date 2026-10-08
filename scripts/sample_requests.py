# Corey Mathie, 2026
"""
Write demo/data/sample_requests.json: a request log for the sample company, Cypress Harbor Credit
Union (fictional), so the console's Requests screen reads like a gateway in production.

500 requests from October 1 to 7, 2026, sampled in proportion to each team's traffic in
demo/data/sample_company.json and to the hour of day each app is used. Every request carries the
fields and decision stages the gateway records for a real one (router/traces.py): team, app, key,
alias, the deployment that answered and each fallback attempt, tokens, cost, latency, cache, the
policy decision and content hooks. Nothing here is a measurement:

- Cost is tokens times the list prices below (on-prem Llama at an assumed GPU chargeback rate), and
  token sizes per app are chosen so each team's cost per 1,000 requests is close to the sample
  company's assumption (tests/test_sample_requests.py checks it).
- Budgets in each trace are the team's month-to-date spend from the sample company on that day.
- The October 6 Anthropic overload and the October 2 contractor-policy denial in the sample
  company's activity feed appear as the requests they describe.

    python scripts/sample_requests.py            # write the file
    python scripts/sample_requests.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import generate_sample_company as company  # noqa: E402

OUT = ROOT / "demo" / "data" / "sample_requests.json"
SEED = 20261008
COUNT = 500
FIRST, LAST = date(2026, 10, 1), date(2026, 10, 7)
EDT = timedelta(hours=-4)  # South Florida in October

# USD per 1M tokens (input, output): published list prices; on-prem is an assumed GPU chargeback.
PRICES = {
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "anthropic/claude-sonnet-4-5": (3.00, 15.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "gemini/gemini-2.5-flash": (0.30, 2.50),
    "ollama/llama3.1:8b": (0.05, 0.05),
}

# The sample company's routes (a config like config/routes.yaml plus config/regulated.yaml).
ALIASES = {
    "smart-fast": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini", "ollama/llama3.1:8b"],
    "fast-chat": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini", "gemini/gemini-2.5-flash"],
    "heavy-reasoning": ["anthropic/claude-sonnet-4-5", "openai/gpt-4.1-mini"],
    "cheap-batch": ["gemini/gemini-2.5-flash", "openai/gpt-4.1-mini"],
    "regulated-fast": ["ollama/llama3.1:8b"],
}

# app: team, [(alias, share)], (input tokens, output tokens) by alias, temperature, caller kind,
# hours (local) when it runs, PII hook
APPS = {
    "member-assistant": dict(team="member-services", routes=[("smart-fast", 1.0)], tokens={"smart-fast": (1800, 260)},
                             temp=0.0, caller="member", hours="always", pii=True),
    "agent-assist": dict(team="member-services", routes=[("smart-fast", 1.0)], tokens={"smart-fast": (2000, 230)},
                         temp=0.0, caller="staff", hours="branch", pii=True),
    "online-banking": dict(team="digital-banking", routes=[("fast-chat", 1.0)], tokens={"fast-chat": (2000, 300)},
                           temp=0.0, caller="member", hours="always"),
    "mobile-banking": dict(team="digital-banking", routes=[("fast-chat", 1.0)], tokens={"fast-chat": (1900, 280)},
                           temp=0.0, caller="member", hours="always"),
    "fraud-scoring-batch": dict(team="risk-analytics", routes=[("heavy-reasoning", 1.0)],
                                tokens={"heavy-reasoning": (2400, 260)}, temp=0.0, caller="batch", hours="night"),
    "dispute-triage": dict(team="risk-analytics", routes=[("regulated-fast", 1.0)],
                           tokens={"regulated-fast": (3000, 400)}, temp=0.0, caller="staff", hours="office", pii=True,
                           regulated=True),
    "loan-doc-extraction": dict(team="lending", routes=[("smart-fast", 0.78), ("heavy-reasoning", 0.22)],
                                tokens={"smart-fast": (3000, 350), "heavy-reasoning": (5200, 700)}, temp=0.0,
                                caller="staff", hours="office"),
    "reg-change-digest": dict(team="compliance", routes=[("heavy-reasoning", 1.0)],
                              tokens={"heavy-reasoning": (6000, 350)}, temp=0.0, caller="staff", hours="office"),
    "bsa-case-notes": dict(team="compliance", routes=[("regulated-fast", 1.0)], tokens={"regulated-fast": (2600, 350)},
                           temp=0.0, caller="staff", hours="office", pii=True, regulated=True),
    "code-assistant": dict(team="it-engineering", routes=[("smart-fast", 0.88), ("heavy-reasoning", 0.12)],
                           tokens={"smart-fast": (2000, 300), "heavy-reasoning": (3500, 500)}, temp=0.2,
                           caller="staff", hours="office"),
    "content-drafts": dict(team="marketing", routes=[("smart-fast", 1.0)], tokens={"smart-fast": (900, 1300)},
                           temp=0.7, caller="staff", hours="office"),
}  # fmt: skip

# Share of each team's requests per app (the rest of the team's traffic goes to its first app).
APP_SHARE = {"member-assistant": 0.62, "online-banking": 0.55, "fraud-scoring-batch": 0.6, "reg-change-digest": 0.65}

BRANCHES = ["Fort Lauderdale", "Plantation", "Coral Springs", "Pembroke Pines", "Davie", "Sunrise", "Weston",
            "Boca Raton", "Pompano Beach", "Hollywood", "Miramar"]  # fmt: skip
INCIDENT = (datetime(2026, 10, 6, 18, 5, tzinfo=UTC), datetime(2026, 10, 6, 18, 17, tzinfo=UTC))  # 2:05-2:17 pm EDT
LATENCY = {  # first-token-ish base ms, ms per output token
    "anthropic/claude-haiku-4-5": (420, 6.0),
    "anthropic/claude-sonnet-4-5": (900, 16.0),
    "openai/gpt-4.1-mini": (380, 5.5),
    "gemini/gemini-2.5-flash": (330, 4.5),
    "ollama/llama3.1:8b": (650, 22.0),
}


def _fp(label: str) -> str:
    return hashlib.sha256(f"cypress-harbor:{label}".encode()).hexdigest()[:8]


def cost(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICES[model]
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 6)


def _hour_weights(kind: str) -> list[float]:
    if kind == "night":
        return [6 if 1 <= h <= 4 else 0.2 for h in range(24)]
    if kind == "always":
        return [0.25 if h < 6 else 1.3 if 18 <= h <= 21 else 1.0 if h >= 8 else 0.5 for h in range(24)]
    start, end = (9, 18) if kind == "branch" else (8, 18)
    return [1.0 if start <= h < end else 0.04 for h in range(24)]


def _pick_route(rng: random.Random, routes: list[tuple[str, float]]) -> str:
    r, acc = rng.random(), 0.0
    for alias, share in routes:
        acc += share
        if r < acc:
            return alias
    return routes[-1][0]


def _plan(rng: random.Random, data: dict) -> list[tuple[str, datetime]]:
    """(app, UTC time) for each request, in proportion to team traffic, app share and hour of day."""
    days = {d["date"]: d for d in data["days"]}
    apps_by_team: dict[str, list[str]] = {}
    for app, spec in APPS.items():
        apps_by_team.setdefault(spec["team"], []).append(app)
    weights: list[tuple[str, date, int, float]] = []
    for i in range((LAST - FIRST).days + 1):
        day = FIRST + timedelta(days=i)
        teams = days[day.isoformat()]["teams"]
        for team, apps in apps_by_team.items():
            n = teams[team]["requests"]
            shares = [1.0] if len(apps) == 1 else [APP_SHARE[apps[0]], 1 - APP_SHARE[apps[0]]]
            for app, share in zip(apps, shares, strict=True):
                hw = _hour_weights(APPS[app]["hours"])
                total = sum(hw)
                for h, w in enumerate(hw):
                    weights.append((app, day, h, n * share * w / total))
    # Small teams still get enough rows to read: at least 10 requests per app.
    picks: list[tuple[str, date, int]] = []
    for app in APPS:
        rows = [w for w in weights if w[0] == app]
        picks += [(a, d, h) for a, d, h, _ in rng.choices(rows, weights=[w[3] for w in rows], k=10)]
    picks += [(a, d, h) for a, d, h, _ in rng.choices(weights, weights=[w[3] for w in weights], k=COUNT - len(picks))]
    out = []
    for app, day, hour in picks:
        local = datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60))
        out.append((app, (local - EDT).replace(tzinfo=UTC)))
    return out


def _mtd(data: dict, team: str, when: datetime) -> tuple[float, float]:
    """The team's month-to-date spend before this request's day, and its monthly budget."""
    day = (when + EDT).date()
    spend = sum(
        d["teams"][team]["spend_usd"]
        for d in data["days"]
        if d["date"][:7] == "2026-10" and d["date"] < day.isoformat()
    )
    budget = next(t["monthly_budget_usd"] for t in data["teams"] if t["team"] == team)
    return round(spend, 2), budget


def _caller(rng: random.Random, kind: str) -> str:
    if kind == "member":
        return f"member session {rng.randrange(16**4):04x}"
    if kind == "batch":
        return "nightly job"
    return f"{rng.choice(BRANCHES)} branch" if rng.random() < 0.5 else f"staff {rng.randrange(100, 999)}"


def _request(rng: random.Random, data: dict, n: int, app: str, when: datetime, forced: dict | None = None) -> dict:
    spec = APPS[app]
    forced = forced or {}
    team = spec["team"]
    alias = forced.get("alias") or _pick_route(rng, spec["routes"])
    key = forced.get("key") or app
    tin_base, tout_base = spec["tokens"].get(alias) or next(iter(spec["tokens"].values()))
    tin = max(80, int(rng.gauss(tin_base, tin_base * 0.22)))
    tout = max(12, int(rng.gauss(tout_base, tout_base * 0.3)))
    team_hit = next(t["cache_hit_rate"] for t in data["teams"] if t["team"] == team)
    stages: list[dict] = []
    status, outcome, reason = 200, "ok", ""
    served, attempts, fell_back = None, [], False
    price_usd = saved = 0.0
    cache = "miss"
    overhead = round(rng.uniform(7.0, 12.5), 1)

    def stage(name, label, decision, summary, ms=None, **detail):
        # The console labels stages from router/traces.py's LABELS; empty details are left out.
        row = {"stage": name, "decision": decision, "summary": summary, "ms": None if ms is None else round(ms, 2)}
        detail = {k: v for k, v in detail.items() if v is not None}
        if detail:
            row["detail"] = detail
        stages.append(row)

    profile = forced.get("profile")
    stage("auth", "Auth + RPM", "pass", f"key {key} ({_fp(key)}) · team {team}", None)
    if forced.get("rate_limited"):
        stage(
            "auth",
            "Auth + RPM",
            "deny",
            f"{forced['rate_limited']} requests this minute; the limit is 120 per key",
            0.4,
            limit_rpm=120,
        )
        status, outcome, reason = 429, "rejected", "Rate limit: 120 requests per minute per key. Retry-After 21 s."
        return _finish(
            n,
            when,
            app,
            team,
            key,
            alias,
            spec,
            stages,
            status,
            outcome,
            reason,
            None,
            [],
            False,
            tin,
            0,
            0.0,
            0.0,
            "skip",
            overhead,
            overhead,
            profile,
        )
    if profile == "contractors" and alias == "heavy-reasoning":
        stage(
            "policy",
            "Policy (YAML / OPA)",
            "deny",
            "contractors policy: anthropic/claude-sonnet-* and openai/gpt-4.1 are denied; no deployment left",
            0.6,
            layers=["defaults", "teams.contractors", "routes.heavy-reasoning"],
            removed=ALIASES["heavy-reasoning"],
        )
        status, outcome = 403, "rejected"
        reason = "Policy: every deployment of heavy-reasoning is denied for the contractors profile."
        return _finish(
            n,
            when,
            app,
            team,
            key,
            alias,
            spec,
            stages,
            status,
            outcome,
            reason,
            None,
            [],
            False,
            tin,
            0,
            0.0,
            0.0,
            "skip",
            overhead,
            overhead,
            profile,
        )
    layers = (
        ["defaults"]
        + ([f"teams.{profile}"] if profile else [])
        + ([f"routes.{alias}"] if alias == "heavy-reasoning" else [])
    )
    hooks = ["pii_redact"] if spec.get("pii") else []
    clamp = tout_base * 4 > 4096
    stage(
        "policy",
        "Policy (YAML / OPA)",
        "pass",
        f"allowed: {alias}"
        + (" · on-prem only (regulated)" if spec.get("regulated") else "")
        + (f" · required hooks: {', '.join(hooks)}" if hooks else ""),
        rng.uniform(0.2, 0.7),
        layers=layers,
        max_tokens_ceiling=2048 if spec.get("regulated") else 4096,
        clamped=clamp or None,
    )
    mtd, budget = _mtd(data, team, when)
    stage(
        "budgets",
        "Budgets",
        "pass",
        f"team {team}: ${mtd:,.2f} of ${budget:,.0f} this month · key under $50/day",
        rng.uniform(0.3, 0.9),
        team_month_usd=mtd,
        team_monthly_budget_usd=budget,
    )
    stage(
        "anomaly",
        "Anomaly pause",
        "pass",
        f"{rng.uniform(0.6, 1.6):.1f}x this key's 7-day hourly baseline",
        rng.uniform(0.1, 0.3),
    )
    redacted = forced.get("redacted") or {}
    if hooks:
        summary = (
            ("redacted " + ", ".join(f"{v} {k}" for k, v in redacted.items()))
            if redacted
            else "pii_redact: nothing found"
        )
        stage("pre_hooks", "Content hooks (request)", "pass", summary, rng.uniform(0.4, 1.4), redacted=redacted or None)
    else:
        stage("pre_hooks", "Content hooks (request)", "skip", "no hooks for this team or route", 0.0)
    cacheable = spec["temp"] <= 0.0
    hit = cacheable and not forced.get("no_cache") and rng.random() < team_hit
    if not cacheable:
        stage("cache", "Exact cache", "skip", f"temperature {spec['temp']} is above the 0.0 cache ceiling", 0.0)
        cache = "skip"
    elif hit:
        cache = "hit"
    else:
        stage("cache", "Exact cache", "miss", "no entry for this prompt in the team's cache", rng.uniform(0.2, 0.6))
    deployments = list(ALIASES[alias])
    if alias == "fast-chat":  # strategy: latency; whichever deployment has been fastest lately goes first
        first = rng.choices(deployments, weights=[0.45, 0.35, 0.2])[0]
        deployments = [first] + [d for d in deployments if d != first]
    if hit:
        model = deployments[0]
        saved = cost(model, tin, tout)
        stage(
            "cache",
            "Exact cache",
            "hit",
            f"served from cache: saved ${saved:.4f} on {model}",
            rng.uniform(0.8, 2.2),
            ttl_s=3600,
            model=model,
        )
        latency = overhead + rng.uniform(1.0, 3.0)
        return _finish(
            n,
            when,
            app,
            team,
            key,
            alias,
            spec,
            stages,
            200,
            "cache_hit",
            "",
            model,
            [],
            False,
            tin,
            tout,
            0.0,
            saved,
            "hit",
            latency,
            overhead,
            profile,
        )
    stage("semantic_cache", "Semantic cache", "skip", "off for this team", 0.0)
    stage("tpm", "TPM reservation", "skip", "no tokens-per-minute cap set", 0.0)
    in_incident = INCIDENT[0] <= when <= INCIDENT[1] and deployments[0] == "anthropic/claude-haiku-4-5"
    seconds = 0.0
    for i, dep in enumerate(deployments):
        base, per = LATENCY[dep]
        took = (base + per * tout) * rng.uniform(0.85, 1.2) / 1000
        if forced.get("fail_all") or (i == 0 and (in_incident or forced.get("fallback"))):
            err = forced.get("error") or "529 overloaded"
            t_err = rng.uniform(0.25, 0.9) if not forced.get("fail_all") else 60.0
            attempts.append({"deployment": dep, "outcome": "error", "error": err, "seconds": round(t_err, 3)})
            seconds += t_err
            continue
        attempts.append({"deployment": dep, "outcome": "ok", "seconds": round(took, 3)})
        seconds += took
        served = dep
        fell_back = i > 0
        break
    if served is None:
        stage(
            "chain",
            "Fallback chain",
            "error",
            "the only deployment timed out; regulated routes have no cloud fallback by design",
            seconds * 1000,
            attempts=attempts,
        )
        status, outcome = 504, "error"
        reason = (
            "Upstream timeout: ollama/llama3.1:8b did not answer within 60 s. "
            "Regulated routes never fall back to a cloud provider."
        )
        return _finish(
            n,
            when,
            app,
            team,
            key,
            alias,
            spec,
            stages,
            status,
            outcome,
            reason,
            None,
            attempts,
            False,
            tin,
            0,
            0.0,
            0.0,
            cache,
            overhead + seconds * 1000,
            overhead,
            profile,
        )
    stage(
        "chain",
        "Fallback chain",
        "pass",
        (
            f"fell back: {attempts[0]['deployment']} failed ({attempts[0]['error']}), {served} answered"
            if fell_back
            else f"{served} answered on the first attempt"
        ),
        seconds * 1000,
        attempts=attempts,
    )
    price_usd = cost(served, tin, tout)
    stage(
        "settle",
        "Settle tokens + cost",
        "pass",
        f"{tin:,} in / {tout:,} out · ${price_usd:.4f}",
        rng.uniform(0.3, 0.8),
        input_usd_per_1m=PRICES[served][0],
        output_usd_per_1m=PRICES[served][1],
    )
    if hooks:
        stage("post_hooks", "Content hooks (response)", "pass", "pii_redact: nothing found", rng.uniform(0.2, 0.8))
    else:
        stage("post_hooks", "Content hooks (response)", "skip", "no hooks", 0.0)
    stage("content_log", "Content log", "skip", "metadata only: prompt and completion text are not stored", 0.0)
    latency = overhead + seconds * 1000
    return _finish(
        n,
        when,
        app,
        team,
        key,
        alias,
        spec,
        stages,
        200,
        "ok",
        "",
        served,
        attempts,
        fell_back,
        tin,
        tout,
        price_usd,
        0.0,
        cache,
        latency,
        overhead,
        profile,
    )


def _finish(n, when, app, team, key, alias, spec, stages, status, outcome, reason, served, attempts, fell_back,
            tin, tout, price_usd, saved, cache, latency, overhead, profile) -> dict:  # fmt: skip
    rid = "req_" + hashlib.sha256(f"{SEED}:{n}:{when.isoformat()}:{app}".encode()).hexdigest()[:12]
    redacted = next(
        (s["detail"]["redacted"] for s in stages if s["stage"] == "pre_hooks" and "redacted" in s.get("detail", {})),
        None,
    )
    return {
        "id": rid,
        "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "team": team,
        "app": app,
        "key": key,
        "key_fp": _fp(key),
        "profile": profile,
        "caller": "",
        "alias": alias,
        "status": status,
        "outcome": outcome,
        "reason": reason,
        "served_by": served,
        "fell_back": fell_back,
        "attempts": len(attempts),
        "prompt_tokens": tin,
        "completion_tokens": tout,
        "cost_usd": round(price_usd, 6),
        "saved_usd": round(saved, 6),
        "cache": cache,
        "latency_ms": round(latency, 1),
        "overhead_ms": round(overhead, 1),
        "regulated": bool(spec.get("regulated")),
        "redacted": redacted,
        "temperature": spec["temp"],
        "stages": stages,
    }


def build() -> dict:
    rng = random.Random(SEED)
    data = company.build()
    plan = _plan(rng, data)
    # Requests the sample company's activity feed describes.
    stories: list[tuple[str, datetime, dict]] = [
        (
            "code-assistant",
            datetime(2026, 10, 2, 15, 42, 7, tzinfo=UTC),
            {"key": "vendor-code-assist", "profile": "contractors", "alias": "heavy-reasoning"},
        ),
        (
            "code-assistant",
            datetime(2026, 10, 2, 15, 42, 31, tzinfo=UTC),
            {"key": "vendor-code-assist", "profile": "contractors", "alias": "smart-fast", "no_cache": True},
        ),
        (
            "bsa-case-notes",
            datetime(2026, 10, 5, 14, 20, 3, tzinfo=UTC),
            {"fail_all": True, "error": "timeout after 60 s", "no_cache": True},
        ),
        ("mobile-banking", datetime(2026, 10, 3, 23, 4, 51, tzinfo=UTC), {"rate_limited": 131}),
        (
            "member-assistant",
            datetime(2026, 10, 4, 1, 12, 40, tzinfo=UTC),
            {"redacted": {"phone": 1}, "no_cache": True},
        ),
        ("agent-assist", datetime(2026, 10, 6, 15, 31, 9, tzinfo=UTC), {"redacted": {"card": 1}, "no_cache": True}),
        (
            "member-assistant",
            datetime(2026, 10, 7, 16, 2, 44, tzinfo=UTC),
            {"redacted": {"email": 1, "phone": 1}, "no_cache": True},
        ),
        ("dispute-triage", datetime(2026, 10, 1, 13, 18, 22, tzinfo=UTC), {"redacted": {"card": 2}, "no_cache": True}),
        (
            "online-banking",
            datetime(2026, 10, 2, 20, 44, 5, tzinfo=UTC),
            {"fallback": True, "error": "503 service unavailable", "no_cache": True},
        ),
    ]
    t0 = INCIDENT[0]
    for i in range(9):  # traffic during the October 6 overload
        app = ("member-assistant", "agent-assist", "online-banking", "mobile-banking")[i % 4]
        stories.append((app, t0 + timedelta(seconds=40 + i * 75 + rng.randrange(30)), {"no_cache": True}))
    plan = plan[: COUNT - len(stories)]
    rows = [(app, when, None) for app, when in plan] + [(a, w, f) for a, w, f in stories]
    rows.sort(key=lambda r: r[1])
    requests = []
    for n, (app, when, forced) in enumerate(rows):
        r = _request(rng, data, n, app, when, forced)
        r["caller"] = _caller(rng, APPS[app]["caller"]) if forced is None or "key" not in forced else "vendor developer"
        requests.append(r)
    requests.reverse()  # newest first, as a log reads
    return {
        "generated_by": "scripts/sample_requests.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated requests for demonstration, not measurements.",
        "period": {"start": FIRST.isoformat(), "end": LAST.isoformat(), "timezone": "America/New_York"},
        "sampled_from": sum(d["requests"] for d in data["days"] if FIRST.isoformat() <= d["date"] <= LAST.isoformat()),
        "prices_usd_per_1m": {k: {"input": v[0], "output": v[1]} for k, v in PRICES.items()},
        "aliases": ALIASES,
        "apps": {
            k: {"team": v["team"], "routes": [a for a, _ in v["routes"]], "regulated": bool(v.get("regulated"))}
            for k, v in APPS.items()
        },
        "requests": requests,
    }


def render(data: dict) -> str:
    return json.dumps(data, separators=(",", ":")) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/sample_requests.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.write_text(text)
    reqs = json.loads(text)["requests"]
    by = {}
    for r in reqs:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    print(f"wrote {OUT.relative_to(ROOT)}: {len(reqs)} requests, {by}, {len(text) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
