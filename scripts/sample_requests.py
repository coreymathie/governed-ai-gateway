# Corey Mathie, 2026
"""
Write demo/data/sample_requests.json: a request log for the sample company, Cypress Harbor Credit
Union (fictional), so the console's Requests screen reads like a gateway in production.

500 requests from October 1 to 7, 2026, sampled in proportion to each app's traffic in
demo/data/sample_company.json and to the hour of day each app is used. Every request carries the
fields and decision stages the gateway records for a real one (router/traces.py): team, app, key,
alias, the deployment that answered and each fallback attempt, tokens, cost, latency, cache, the
policy decision and content hooks. Nothing here is a measurement, but nothing is defined twice:

- Apps, teams, aliases, token sizes, temperatures and hours come from demo/cypress_harbor.py, the same
  table the usage file and the in-browser engine read; deployments, budgets, per-key caps, content
  hooks and the rate limit come from config/routes.yaml.
- The policy stage is the decision router/policy.py makes for that team and alias under
  config/policies.yaml, so a refusal in the log is one the gateway would make.
- Cost is tokens times the list prices in demo/cypress_harbor.py (on-prem Llama at an assumed GPU
  chargeback rate). Budgets in each trace are the team's month-to-date spend and the key's spend so
  far that day, from the usage file, against the caps in config/routes.yaml.
- The October 6 Anthropic overload, the October 2 contractor-policy refusal and the October 5 on-prem
  timeout in the usage file's activity feed appear as the requests they describe.

    python scripts/sample_requests.py            # write the file
    python scripts/sample_requests.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo import cypress_harbor as ch  # noqa: E402
from router import pii, policy  # noqa: E402
from scripts import generate_sample_company as company  # noqa: E402

OUT = ROOT / "demo" / "data" / "sample_requests.json"
POLICIES_FILE = ROOT / "config" / "policies.yaml"
SEED = 20261008
COUNT = 500
FIRST, LAST = date(2026, 10, 1), date(2026, 10, 7)
EDT = timedelta(hours=-4)  # South Florida in October
PRICES = ch.PRICES
LATENCY = {  # first-token-ish base ms, ms per output token
    "anthropic/claude-haiku-4-5": (420, 6.0),
    "anthropic/claude-sonnet-4-5": (900, 16.0),
    "openai/gpt-4.1": (700, 12.0),
    "openai/gpt-4.1-mini": (380, 5.5),
    "openai/gpt-4.1-nano": (250, 3.5),
    "gemini/gemini-2.5-flash": (330, 4.5),
    "ollama/llama3.1:8b": (650, 22.0),
}
Target = namedtuple("Target", "provider model")
_OVERLOAD = next(i for i in ch.INCIDENTS if i["date"] == "2026-10-06")
_t0 = datetime.fromisoformat(f"{_OVERLOAD['date']}T{_OVERLOAD['start']}:00") - EDT
INCIDENT = (_t0.replace(tzinfo=UTC), (_t0 + timedelta(minutes=_OVERLOAD["minutes"])).replace(tzinfo=UTC))


def _fp(label: str) -> str:
    return hashlib.sha256(f"cypress-harbor:{label}".encode()).hexdigest()[:8]


def cost(model: str, tin: int, tout: int) -> float:
    return ch.cost(model, tin, tout)


def max_tokens(app: str, alias: str) -> int:
    """What the app asks for: twice its usual answer, rounded up to a multiple of 512."""
    return 512 * math.ceil(2 * ch.APPS[app]["tokens"][alias][1] / 512)


def hooks(cfg: dict, team: str, alias: str, required: list[str]) -> list[str]:
    """Content hooks for a request: routes.yaml privacy layers (default + route + team) plus any the policy requires."""
    p = cfg.get("privacy") or {}
    layers = [p.get("default") or {}, (p.get("routes") or {}).get(alias) or {}, (p.get("teams") or {}).get(team) or {}]
    names = [h for layer in layers for h in layer.get("pre_request") or []] + list(required)
    return list(dict.fromkeys(names))


def _pick_route(rng: random.Random, routes: list) -> str:
    r, acc = rng.random(), 0.0
    for alias, share in routes:
        acc += share
        if r < acc:
            return alias
    return routes[-1][0]


def _plan(rng: random.Random, data: dict) -> list[tuple[str, datetime]]:
    """(app, UTC time) for each request, in proportion to each app's daily requests and its hours of use."""
    days = {d["date"]: d for d in data["days"]}
    weights: list[tuple[str, date, int, float]] = []
    for i in range((LAST - FIRST).days + 1):
        day = FIRST + timedelta(days=i)
        apps = days[day.isoformat()]["apps"]
        for app in ch.APPS:
            hw = ch.hour_weights(app)
            total = sum(hw)
            weights += [(app, day, h, apps[app]["requests"] * w / total) for h, w in enumerate(hw) if w]
    # Small apps still get enough rows to read: at least 10 requests per app.
    picks: list[tuple[str, date, int]] = []
    for app in ch.APPS:
        rows = [w for w in weights if w[0] == app]
        picks += [(a, d, h) for a, d, h, _ in rng.choices(rows, weights=[w[3] for w in rows], k=10)]
    picks += [(a, d, h) for a, d, h, _ in rng.choices(weights, weights=[w[3] for w in weights], k=COUNT - len(picks))]
    out = []
    for app, day, hour in picks:
        local = datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60))
        out.append((app, (local - EDT).replace(tzinfo=UTC)))
    return out


class Context:
    """Everything a request needs from the usage file and the configs."""

    def __init__(self, data: dict):
        self.data = data
        self.cfg = company.routes_config()
        self.routes = company.aliases(self.cfg)
        self.policies = policy.load(yaml.safe_load(POLICIES_FILE.read_text()), pii.registered_hooks())
        self.days = {d["date"]: d for d in data["days"]}
        self.budgets = self.cfg["budgets"]
        self.defaults = self.cfg["policies"]
        self.baseline = {}  # app -> (average spend per active hour, active hours per day) on a weekday
        for app in ch.APPS:
            w = ch.hour_weights(app)
            self.baseline[app] = sum(1 for h in range(24) if w[h] / sum(w) * ch.APPS[app]["workday"] >= 1)

    def month_to_date(self, team: str, day: date) -> float:
        return round(
            sum(d["teams"][team]["spend_usd"] for k, d in self.days.items() if "2026-10-01" <= k < day.isoformat()), 2
        )

    def key_today(self, app: str, local: datetime) -> float:
        """The key's spend earlier the same local day, from its daily spend and hours of use."""
        spend = self.days[local.date().isoformat()]["apps"][app]["spend_usd"]
        return round(spend * company.hour_share(app, 0, local.hour + local.minute / 60), 2)

    def anomaly_multiple(self, app: str, local: datetime, rng: random.Random) -> float:
        """This hour's spend so far against the key's average spend per active hour."""
        w = ch.hour_weights(app)
        return w[local.hour] / sum(w) * self.baseline[app] * (local.minute + 1) / 60 * rng.uniform(0.85, 1.15)


def _caller(rng: random.Random, kind: str, n: int) -> str:
    label, role = ch.CALLERS[kind]
    if kind == "member":
        return f"{label} {1 + int(hashlib.sha256(f'{SEED}:{n}'.encode()).hexdigest()[:4], 16) % 0xFFFE:04x}"
    if kind == "batch":
        return label
    if kind == "agent" and rng.random() < 0.5:
        return f"{rng.choice(ch.BRANCHES)} branch"
    return f"{label}, {role} {rng.randrange(100, 999)}"


def _request(rng: random.Random, ctx: Context, n: int, app: str, when: datetime, forced: dict | None = None) -> dict:
    spec = ch.APPS[app]
    forced = forced or {}
    team = forced.get("team") or spec["team"]
    alias = forced.get("alias") or _pick_route(rng, spec["routes"])
    key = forced.get("key") or app
    tin_base, tout_base = spec["tokens"].get(alias) or next(iter(spec["tokens"].values()))
    tin = max(80, int(rng.gauss(tin_base, tin_base * 0.22)))
    tout = max(12, int(rng.gauss(tout_base, tout_base * 0.3)))
    local = (when + EDT).replace(tzinfo=None)
    stages: list[dict] = []
    overhead = round(rng.uniform(7.0, 12.5), 1)
    rec = {
        "n": n,
        "when": when,
        "app": app,
        "team": team,
        "key": key,
        "alias": alias,
        "spec": spec,
        "tin": tin,
        "tout": 0,
        "overhead": overhead,
        "stages": stages,
    }

    def stage(name, decision, summary, ms=None, **detail):
        row = {"stage": name, "decision": decision, "summary": summary, "ms": None if ms is None else round(ms, 2)}
        detail = {k: v for k, v in detail.items() if v is not None}
        if detail:
            row["detail"] = detail
        stages.append(row)

    rpm = ctx.defaults["rate_limit_rpm"]
    stage("auth", "pass", f"key {key} ({_fp(key)}) · team {team}", None)
    if forced.get("rate_limited"):
        stages[-1].update(
            decision="deny",
            summary=f"{forced['rate_limited']} requests this minute; the limit is {rpm} per key",
            ms=0.4,
            detail={"limit_rpm": rpm},
        )
        return _finish(rec, 429, "rejected", f"Rate limit: {rpm} requests per minute per key. Retry-After 21 s.")

    # Policy: the decision router/policy.py makes under config/policies.yaml.
    targets = [Target(*d.split("/", 1)) for d in ctx.routes[alias]]
    mt = max_tokens(app, alias)
    dec = policy.evaluate(
        ctx.policies,
        policy.RequestFacts(
            team=team, alias=alias, targets=targets, max_tokens=mt, request_bytes=tin * 4, messages=2, key_label=key
        ),
    )
    if not dec.allow:
        stage(
            "policy",
            "deny",
            "; ".join(dec.reasons),
            0.6,
            layers=dec.rules,
            removed=[r["deployment"] for r in dec.removed] or None,
        )
        return _finish(rec, dec.status, "rejected", f"Policy: {'; '.join(dec.reasons)}.")
    names = hooks(ctx.cfg, team, alias, dec.required_hooks)
    stage(
        "policy",
        "pass",
        f"allowed: {alias}"
        + (" · on-prem only (allowed_providers: ollama)" if spec["regulated"] else "")
        + (f" · required hooks: {', '.join(dec.required_hooks)}" if dec.required_hooks else ""),
        rng.uniform(0.2, 0.7),
        layers=dec.rules,
        max_tokens=dec.max_tokens,
        clamped=dec.max_tokens_clamped or None,
        targets=[f"{t.provider}/{t.model}" for t in dec.targets],
    )

    # Budgets: the team's month so far and the key's day so far, against routes.yaml.
    tb = ctx.budgets["teams"].get(team, {})
    kb = ctx.budgets["keys"].get(key, {})
    key_cap = kb.get("daily_usd", ctx.defaults["per_key_daily_usd"])
    key_today = ctx.key_today(app, local) if key == app else 0.0
    if "monthly_usd" in tb:
        mtd = ctx.month_to_date(team, local.date())
        team_part = f"team {team}: ${mtd:,.2f} of ${tb['monthly_usd']:,.0f} this month"
        detail = {"team_month_usd": mtd, "team_monthly_budget_usd": tb["monthly_usd"]}
    else:
        team_part = f"team {team}: no monthly budget; ${ctx.defaults['per_team_daily_usd']:,.0f} a day by default"
        detail = {}
    stage(
        "budgets",
        "pass",
        f"{team_part} · key {key}: ${key_today:,.2f} of ${key_cap:,.2f} today",
        rng.uniform(0.3, 0.9),
        key_today_usd=key_today,
        key_daily_cap_usd=key_cap,
        **detail,
    )
    if key == app:
        multiple = ctx.anomaly_multiple(app, local, rng)
        stage(
            "anomaly",
            "pass",
            f"{multiple:.1f}x this key's 7-day hourly baseline (pauses at 10x)",
            rng.uniform(0.1, 0.3),
        )
    else:  # router/anomaly.py does not judge a key with less than a day of active hours
        stage("anomaly", "pass", "not judged: fewer than 24 active hours of history", rng.uniform(0.1, 0.3))

    redacted = forced.get("redacted") or {}
    if names:
        summary = (
            ("redacted " + ", ".join(f"{v} {k}" for k, v in redacted.items()))
            if redacted
            else f"{', '.join(names)}: nothing found"
        )
        stage("pre_hooks", "pass", summary, rng.uniform(0.4, 1.4), hooks=names, redacted=redacted or None)
    else:
        stage("pre_hooks", "skip", "no hooks for this team or route", 0.0)

    team_hit = spec["cache_hit"]
    cacheable = spec["temperature"] <= ctx.defaults["cache_max_temperature"]
    hit = cacheable and not forced.get("no_cache") and rng.random() < team_hit
    deployments = [f"{t.provider}/{t.model}" for t in dec.targets]
    mix = ch.LATENCY_MIX.get(alias)
    if mix:  # strategy: latency; whichever deployment has been fastest lately goes first
        first = rng.choices(deployments, weights=list(mix))[0]
        rest = sorted((d for d in deployments if d != first), key=lambda d: -mix[ctx.routes[alias].index(d)])
        deployments = [first, *rest]
    if not cacheable:
        stage(
            "cache",
            "skip",
            f"temperature {spec['temperature']} is above the {ctx.defaults['cache_max_temperature']} cache ceiling",
            0.0,
        )
    elif hit:
        saved = cost(deployments[0], tin, tout)
        stage(
            "cache",
            "hit",
            f"served from cache: saved ${saved:.4f} on {deployments[0]}",
            rng.uniform(0.8, 2.2),
            ttl_s=ctx.defaults["cache_ttl_seconds"],
            model=deployments[0],
        )
        rec.update(tout=tout, served=deployments[0], saved=saved, cache="hit", latency=overhead + rng.uniform(1, 3))
        return _finish(rec, 200, "cache_hit", "")
    else:
        stage("cache", "miss", "no entry for this prompt in the team's cache", rng.uniform(0.2, 0.6))
    stage("semantic_cache", "skip", "off (semantic_cache.enabled: false)", 0.0)
    stage("tpm", "skip", "no tokens-per-minute cap set", 0.0)

    # Fallback chain. During the October 6 overload Claude Haiku's breaker was open: the first requests got a 529,
    # later ones skipped it without waiting.
    attempts, served, seconds = [], None, 0.0
    in_incident = INCIDENT[0] <= when <= INCIDENT[1]
    for i, dep in enumerate(deployments):
        base, per = LATENCY[dep]
        took = (base + per * tout) * rng.uniform(0.85, 1.2) / 1000
        if in_incident and dep == _OVERLOAD["deployment"]:
            if forced.get("breaker") == "closed":
                t_err = rng.uniform(0.25, 0.9)
                attempts.append(
                    {"deployment": dep, "outcome": "error", "error": _OVERLOAD["error"], "seconds": round(t_err, 3)}
                )
                seconds += t_err
            else:
                attempts.append({"deployment": dep, "outcome": "skipped", "error": "circuit open", "seconds": 0.0})
            continue
        if forced.get("fail_all") or (i == 0 and forced.get("fallback")):
            err = forced.get("error") or "503 service unavailable"
            t_err = 60.0 if forced.get("fail_all") else rng.uniform(0.25, 0.9)
            attempts.append({"deployment": dep, "outcome": "error", "error": err, "seconds": round(t_err, 3)})
            seconds += t_err
            continue
        attempts.append({"deployment": dep, "outcome": "ok", "seconds": round(took, 3)})
        seconds += took
        served = dep
        break
    rec.update(attempts=attempts, latency=overhead + seconds * 1000)
    if served is None:
        stage(
            "chain",
            "error",
            "the only deployment timed out; regulated-fast has no cloud fallback by design",
            seconds * 1000,
            attempts=attempts,
        )
        reason = (
            f"Upstream timeout: {deployments[-1]} did not answer within 60 s. "
            "Regulated routes never fall back to a cloud provider."
        )
        return _finish(rec, 504, "error", reason)
    first = attempts[0]
    if len(attempts) > 1:
        how = "skipped (circuit open)" if first["outcome"] == "skipped" else f"failed ({first['error']})"
        summary = f"fell back: {first['deployment']} {how}, {served} answered"
    else:
        summary = f"{served} answered on the first attempt"
    stage("chain", "pass", summary, seconds * 1000, attempts=attempts, strategy="latency" if mix else "ordered")
    price = cost(served, tin, tout)
    stage(
        "settle",
        "pass",
        f"{tin:,} in / {tout:,} out · ${price:.4f}",
        rng.uniform(0.3, 0.8),
        input_usd_per_1m=PRICES[served][0],
        output_usd_per_1m=PRICES[served][1],
    )
    post = [
        h
        for layer in [(ctx.cfg["privacy"].get("teams") or {}).get(team) or {}]
        for h in layer.get("post_response") or []
    ]
    if post:
        stage("post_hooks", "pass", f"{', '.join(post)}: nothing found", rng.uniform(0.2, 0.8))
    else:
        stage("post_hooks", "skip", "no hooks", 0.0)
    stage("content_log", "skip", "metadata only: prompt and completion text are not stored", 0.0)
    rec.update(tout=tout, served=served, price=price, fell_back=len(attempts) > 1)
    return _finish(rec, 200, "ok", "")


def _finish(rec: dict, status: int, outcome: str, reason: str) -> dict:
    when, app = rec["when"], rec["app"]
    rid = "req_" + hashlib.sha256(f"{SEED}:{rec['n']}:{when.isoformat()}:{app}".encode()).hexdigest()[:12]
    redacted = next(
        (
            s["detail"]["redacted"]
            for s in rec["stages"]
            if s["stage"] == "pre_hooks" and "redacted" in s.get("detail", {})
        ),
        None,
    )
    ok = outcome in ("ok", "cache_hit")
    return {
        "id": rid,
        "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "team": rec["team"],
        "app": app,
        "key": rec["key"],
        "key_fp": _fp(rec["key"]),
        "caller": "",
        "alias": rec["alias"],
        "status": status,
        "outcome": outcome,
        "reason": reason,
        "served_by": rec.get("served"),
        "fell_back": bool(rec.get("fell_back")),
        "attempts": len([a for a in rec.get("attempts", []) if a["outcome"] != "skipped"]),
        "prompt_tokens": rec["tin"],
        "completion_tokens": rec["tout"] if ok else 0,
        "cost_usd": round(rec.get("price", 0.0), 6),
        "saved_usd": round(rec.get("saved", 0.0), 6),
        "cache": rec.get("cache", "skip" if outcome == "rejected" else "miss"),
        "latency_ms": round(rec.get("latency", rec["overhead"]), 1),
        "overhead_ms": rec["overhead"],
        "regulated": bool(rec["spec"]["regulated"]),
        "redacted": redacted,
        "temperature": rec["spec"]["temperature"],
        "stages": rec["stages"],
    }


def build() -> dict:
    rng = random.Random(SEED)
    data = company.build()
    ctx = Context(data)
    plan = _plan(rng, data)
    c = ch.CONTRACTOR
    vendor = {"key": c["key"], "team": c["team"], "no_cache": True}
    # Requests the sample company's activity feed describes.
    stories: list[tuple[str, datetime, dict]] = [
        (c["app"], datetime(2026, 10, 2, 15, 42, 7, tzinfo=UTC), {**vendor, "alias": "heavy-reasoning"}),
        (c["app"], datetime(2026, 10, 2, 15, 42, 31, tzinfo=UTC), {**vendor, "alias": "local-first"}),
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
        ("online-banking", datetime(2026, 10, 2, 20, 44, 5, tzinfo=UTC), {"fallback": True, "no_cache": True}),
    ]
    t0 = INCIDENT[0]
    stories.append(("member-assistant", t0 + timedelta(seconds=2), {"no_cache": True, "breaker": "closed"}))
    for i in range(8):  # traffic during the October 6 overload, after the breaker opened
        app = ("member-assistant", "agent-assist", "loan-doc-extraction", "content-drafts")[i % 4]
        stories.append(
            (app, t0 + timedelta(seconds=40 + i * 80 + rng.randrange(30)), {"no_cache": True, "alias": "smart-fast"})
        )
    plan = plan[: COUNT - len(stories)]
    rows = [(app, when, None) for app, when in plan] + list(stories)
    rows.sort(key=lambda r: r[1])
    requests = []
    for n, (app, when, forced) in enumerate(rows):
        r = _request(rng, ctx, n, app, when, forced)
        r["caller"] = (
            c["caller"] if forced and forced.get("key") == c["key"] else _caller(rng, ch.APPS[app]["caller"], n)
        )
        requests.append(r)
    requests.reverse()  # newest first, as a log reads
    return {
        "generated_by": "scripts/sample_requests.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated requests for demonstration, not measurements.",
        "period": {"start": FIRST.isoformat(), "end": LAST.isoformat(), "timezone": "America/New_York"},
        "sampled_from": sum(d["requests"] for d in data["days"] if FIRST.isoformat() <= d["date"] <= LAST.isoformat()),
        "prices_usd_per_1m": {k: {"input": v[0], "output": v[1]} for k, v in PRICES.items()},
        "aliases": ctx.routes,
        "apps": {
            k: {"team": v["team"], "routes": [a for a, _ in v["routes"]], "regulated": v["regulated"]}
            for k, v in ch.APPS.items()
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
    by: dict[str, int] = {}
    for r in reqs:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    print(f"wrote {OUT.relative_to(ROOT)}: {len(reqs)} requests, {by}, {len(text) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
