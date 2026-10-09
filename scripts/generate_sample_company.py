# Corey Mathie, 2026
"""
Write demo/data/sample_company.json: 90 days of AI traffic through the gateway at a fictional
credit union, so the console's Spend screen shows it at a realistic scale.

Cypress Harbor Credit Union does not exist. Every number in the file is generated here from a
fixed seed and stated assumptions; none of it is a measurement of this repo or of any real
institution. The console labels it "Sample company data" wherever it appears, and keeps it apart
from the measured eval results (Evals) and the requests simulated in the browser (This session).

    python scripts/generate_sample_company.py            # write the file
    python scripts/generate_sample_company.py --check    # exit 1 if the committed file differs

The model, in short:
- Applications, their teams, route aliases, token sizes, hours and volumes come from
  demo/cypress_harbor.py; deployments, budgets, per-key caps and the circuit-breaker settings come
  from config/routes.yaml. Nothing about the company is defined twice.
- Each day, every app's requests (less exact-cache hits) are served by its route's deployments in
  order; a small share falls back to the next deployment, and on incident days the requests that
  reached the failing deployment during the incident window fall back. Spend is tokens times price
  for the deployment that answered, so spend by team and by model follow from the request mix.
- One runaway (September 24: a coding agent looping on heavy-reasoning) is paused by the anomaly
  rule in router/anomaly.py (10x the key's 7-day hourly baseline); the spend it would have caused
  before the next business morning, within the key, team and org caps, is estimated.
- Regulated work (dispute triage, BSA case notes) runs only on the on-prem model.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta
from math import fsum
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo import cypress_harbor as ch  # noqa: E402
from router import anomaly  # noqa: E402

OUT = ROOT / "demo" / "data" / "sample_company.json"
ROUTES_FILE = ROOT / "config" / "routes.yaml"
SEED = 20261009
END = date(2026, 10, 7)
DAYS = 90
HOLIDAYS = {date(2026, 9, 7)}  # Labor Day: branches and back office closed
UTC_DAY_STARTS = 20  # local hour (Eastern daylight time) at which a new UTC day, and the daily caps, begin
STORY_FAILURES = {"2026-10-05": "bsa-case-notes"}  # the on-prem timeout shown in the request log

ASSUMPTIONS = {
    "note": "Request volumes, token sizes, cache hit rates and hours of use per app are illustrative assumptions "
    "(demo/cypress_harbor.py); budgets and per-key caps are the credit union's config/routes.yaml. Spend is tokens "
    "times list prices (on-prem Llama at an assumed GPU chargeback rate). Spend prevented by the anomaly pause is "
    "the runaway's rate until the next business morning, within the key, team and org caps (estimated, not "
    "measured).",
}


def routes_config() -> dict:
    return yaml.safe_load(ROUTES_FILE.read_text())


def aliases(cfg: dict | None = None) -> dict[str, list[str]]:
    """alias -> ["provider/model", ...] in configured order, from config/routes.yaml."""
    cfg = cfg or routes_config()
    out = {}
    for name, spec in cfg["aliases"].items():
        targets = spec["targets"] if isinstance(spec, dict) else spec
        out[name] = [f"{t['provider']}/{t['model']}" for t in targets]
    return out


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def clock(hhmm: str) -> int:
    """Minutes after local midnight."""
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def hour_share(app: str, h0: float, h1: float) -> float:
    """Share of an app's daily traffic between local hours h0 and h1 (fractions allowed)."""
    w = ch.hour_weights(app)
    total = fsum(w) or 1.0
    return fsum(w[h] * max(0.0, min(h1, h + 1) - max(h0, h)) for h in range(24)) / total


def window_share(app: str, start: str, minutes: int) -> float:
    """Share of an app's daily traffic in [start, start + minutes) local time."""
    m0 = clock(start)
    return hour_share(app, m0 / 60, (m0 + minutes) / 60)


def unit_cost(app: str, alias: str, dep: str) -> float:
    tin, tout = ch.APPS[app]["tokens"][alias]
    return ch.cost(dep, tin, tout)


def expected_cost(app: str, routes: dict[str, list[str]]) -> float:
    """Average cost of one uncached request with no fallback, from the route mix."""
    total = 0.0
    for alias, share in ch.APPS[app]["routes"]:
        targets = routes[alias]
        firsts = ch.first_choice_shares(alias, targets)
        total += share * fsum(f * unit_cost(app, alias, t) for f, t in zip(firsts, targets, strict=True))
    return total


def split(n: int, shares: list[float]) -> list[int]:
    """Split n into integer parts by share; the remainder goes to the largest share."""
    parts = [int(n * s) for s in shares]
    parts[max(range(len(shares)), key=lambda i: shares[i])] += n - sum(parts)
    return parts


def next_target(alias: str, targets: list[str], failing: str) -> str | None:
    """Where a request goes when `failing` errors or is skipped: the next target (latency routes: next fastest)."""
    rest = [t for t in targets if t != failing]
    mix = ch.LATENCY_MIX.get(alias)
    if mix:
        rest.sort(key=lambda t: -mix[targets.index(t)])
    return rest[0] if rest else None


def affected_aliases(dep: str, routes: dict[str, list[str]]) -> list[str]:
    """Aliases whose requests can reach `dep` first: ordered routes that start with it, latency routes listing it."""
    return sorted(a for a, t in routes.items() if t[0] == dep or (a in ch.LATENCY_MIX and dep in t))


def serve(app: str, uncached: int, d: date, rng: random.Random, routes: dict[str, list[str]]) -> dict:
    """Which deployment answered each uncached request of one app on one day, with fallbacks and failures."""
    spec = ch.APPS[app]
    served: dict[str, dict[str, int]] = {}  # alias -> deployment -> requests
    fallbacks = failed = 0
    incident_fallbacks: dict[str, int] = {}
    base_rate = rng.uniform(0.002, 0.005)  # ordinary provider errors and timeouts, retried on the next target
    today = [x for x in ch.INCIDENTS if x["date"] == d.isoformat()]
    for (alias, _share), m in zip(spec["routes"], split(uncached, [s for _, s in spec["routes"]]), strict=True):
        targets = routes[alias]
        row = served.setdefault(alias, {})
        for target, mj in zip(targets, split(m, ch.first_choice_shares(alias, targets)), strict=True):
            if not mj:
                continue
            affected = sum(
                round(mj * window_share(app, inc["start"], inc["minutes"]))
                for inc in today
                if inc["deployment"] == target
            )
            nxt = next_target(alias, targets, target)
            if nxt:
                base = round((mj - affected) * base_rate)
                row[nxt] = row.get(nxt, 0) + affected + base
                fallbacks += affected + base
                if affected:
                    incident_fallbacks[target] = incident_fallbacks.get(target, 0) + affected
            else:  # a single-target route (regulated-fast) has nowhere to go: the request fails
                base = int((mj - affected) * rng.uniform(0, 0.004))
                failed += base
            row[target] = row.get(target, 0) + mj - affected - base
    if STORY_FAILURES.get(d.isoformat()) == app and failed == 0:
        alias = spec["routes"][0][0]
        served[alias][routes[alias][0]] -= 1
        failed = 1
    return {"served": served, "fallbacks": fallbacks, "failed": failed, "incident_fallbacks": incident_fallbacks}


def app_day(app: str, d: date, i: int, rng: random.Random, routes: dict[str, list[str]]) -> dict:
    spec = ch.APPS[app]
    growth = 1 + 0.18 * (i / (DAYS - 1))
    n = int(spec["workday"] * growth * (1 if is_workday(d) else spec["weekend"]) * rng.uniform(0.92, 1.08))
    cached = int(n * spec["cache_hit"] * rng.uniform(0.9, 1.1)) if spec["temperature"] <= 0.0 else 0
    s = serve(app, n - cached, d, rng, routes)
    models: dict[str, list[float]] = {}
    for alias, row in s["served"].items():
        for dep, k in row.items():
            if k:
                acc = models.setdefault(dep, [0, 0.0])
                acc[0] += k
                acc[1] += k * unit_cost(app, alias, dep)
    return {
        "requests": n,
        "cached": cached,
        "spend_usd": round(fsum(v[1] for v in models.values()), 4),
        "saved_usd": round(cached * expected_cost(app, routes), 4),
        "fallbacks": s["fallbacks"],
        "failed": s["failed"],
        "incident_fallbacks": s["incident_fallbacks"],
        "models": {k: {"requests": v[0], "spend_usd": round(v[1], 4)} for k, v in sorted(models.items())},
    }


def make_day(rng: random.Random, d: date, i: int, routes: dict[str, list[str]]) -> dict:
    apps = {app: app_day(app, d, i, rng, routes) for app in ch.APPS}
    teams = {}
    for team in ch.TEAMS:
        rows = [apps[a] for a in ch.apps_of(team)]
        teams[team] = {
            "requests": sum(r["requests"] for r in rows),
            "cached": sum(r["cached"] for r in rows),
            "spend_usd": round(fsum(r["spend_usd"] for r in rows), 2),
            "saved_usd": round(fsum(r["saved_usd"] for r in rows), 2),
        }
    models: dict[str, dict] = {}
    for a in apps.values():
        for dep, v in a["models"].items():
            m = models.setdefault(dep, {"requests": 0, "spend_usd": 0.0})
            m["requests"] += v["requests"]
            m["spend_usd"] = round(m["spend_usd"] + v["spend_usd"], 4)
    workday = is_workday(d)
    pii_requests = sum(apps[a]["requests"] for a, s in ch.APPS.items() if s["pii"])
    incident = sorted({k for a in apps.values() for k in a["incident_fallbacks"]})
    return {
        "date": d.isoformat(),
        "requests": sum(t["requests"] for t in teams.values()),
        "spend_usd": round(fsum(t["spend_usd"] for t in teams.values()), 2),
        "saved_usd": round(fsum(t["saved_usd"] for t in teams.values()), 2),
        "fallbacks": sum(a["fallbacks"] for a in apps.values()),
        "failed": sum(a["failed"] for a in apps.values()),
        "policy_denied": rng.randint(4, 19) if workday else rng.randint(0, 4),
        "pii_redacted": int(pii_requests * rng.uniform(0.018, 0.026)),
        "regulated_on_prem": sum(apps[a]["requests"] for a, s in ch.APPS.items() if s["regulated"]),
        "anomaly_pauses": 0,
        "p50_overhead_ms": round(rng.uniform(8.5, 10.5), 1),
        "teams": teams,
        "apps": {
            k: {f: v[f] for f in ("requests", "cached", "spend_usd", "fallbacks", "failed")} for k, v in apps.items()
        },
        "models": dict(sorted(models.items())),
        "incident_fallbacks": {
            dep: sum(a["incident_fallbacks"].get(dep, 0) for a in apps.values()) for dep in incident
        },
    }


# ---------- the September 24 runaway ----------


def baseline(days: list[dict], app: str) -> tuple[float, int]:
    """router/anomaly.py's baseline over the 7 days before: spend per hour in which the key had any requests."""
    w = ch.hour_weights(app)
    total = fsum(w)
    hourly: list[float] = []
    for d in days[-7:]:
        a = d["apps"][app]
        uncached = a["requests"] - a["cached"]
        hourly += [a["spend_usd"] * w[h] / total for h in range(24) if uncached * w[h] / total >= 1]
    return anomaly.baseline_from_hourly(hourly)


def spend_between(day: dict, apps: list[str], h0: float, h1: float) -> float:
    """Normal spend of these apps between local hours h0 and h1 of a day, from each app's hours of use."""
    return fsum(day["apps"][a]["spend_usd"] * hour_share(a, h0, h1) for a in apps)


def runaway(days: list[dict], today: dict, cfg: dict) -> dict:
    """When the anomaly check pauses the loop, and what it would have spent before the next business morning."""
    r = ch.RUNAWAY
    app, alias = r["key"], r["alias"]
    team = ch.APPS[app]["team"]
    routes = aliases(cfg)
    base, hours = baseline(days, app)
    call = ch.cost(routes[alias][0], *r["tokens"])
    start = clock(r["start"]) * 60  # seconds after local midnight
    gap = 3600 / r["calls_per_hour"]
    hour = start // 3600
    calls = 0
    while True:  # the gateway checks every request before it is sent
        t = start + calls * gap
        sig = anomaly.classify(app, spend_between(today, [app], hour, t / 3600) + calls * call, base, hours)
        if sig.verdict == "pause":
            break
        calls += 1
    paused = start + calls * gap
    loop = calls * call

    b = cfg["budgets"]
    caps = {
        "key daily cap": (b["keys"][app]["daily_usd"], [app]),
        "team daily cap": (b["teams"][team]["daily_usd"], ch.apps_of(team)),
        "org daily cap": (b["org"]["daily_usd"], list(ch.APPS)),
    }
    month = [x for x in days if x["date"][:7] == r["date"][:7]] + [today]
    monthly = {
        "team monthly budget": b["teams"][team]["monthly_usd"] - fsum(x["teams"][team]["spend_usd"] for x in month),
        "org monthly budget": b["org"]["monthly_usd"] - fsum(x["spend_usd"] for x in month),
    }
    # Spend already in the current UTC day for each daily cap: last evening after 8 pm, today until the pause.
    used = {
        k: spend_between(days[-1], apps, UTC_DAY_STARTS, 24) + spend_between(today, apps, 0, paused / 3600) + loop
        for k, (_cap, apps) in caps.items()
    }
    d0 = date.fromisoformat(r["date"])
    morning = d0 + timedelta(days=1)
    while not is_workday(morning):
        morning += timedelta(days=1)
    stop = datetime.combine(morning, datetime.min.time()) + timedelta(minutes=clock(r["noticed"]))
    now = datetime.combine(d0, datetime.min.time()) + timedelta(seconds=paused)
    rate = r["calls_per_hour"] / 60 * call  # per minute
    prevented, binding, utc_days = 0.0, None, 1
    while now < stop:
        if now.hour == UTC_DAY_STARTS and now.minute == 0:  # a new UTC day: daily caps start again
            used = dict.fromkeys(used, 0.0)
            utc_days += 1
        h = now.hour + now.minute / 60
        for k, (_cap, apps) in caps.items():  # normal traffic keeps using the same caps (today's shape)
            used[k] += spend_between(today, apps, h, h + 1 / 60)
        room = {k: cap - used[k] for k, (cap, _apps) in caps.items()}
        room |= {k: v - prevented for k, v in monthly.items()}
        tightest = min(room, key=room.get)
        step = max(0.0, min(rate, room[tightest]))
        if step < rate and binding is None:
            binding = tightest
        prevented += step
        for k in used:
            used[k] += step
        now += timedelta(minutes=1)
    return {
        "date": r["date"],
        "team": team,
        "key": app,
        "alias": alias,
        "deployment": routes[alias][0],
        "started": r["start"],
        "paused_at": f"{int(paused // 3600):02d}:{int(paused % 3600 // 60):02d}:{int(paused % 60):02d}",
        "seconds_to_pause": round(paused - start),
        "calls_before_pause": calls,
        "cost_per_call_usd": round(call, 4),
        "loop_spend_usd": round(loop, 2),
        "baseline_hourly_usd": round(base, 4),
        "baseline_hours": hours,
        "hour_spend_at_pause_usd": round(sig.hour_spend, 4),
        "baseline_multiple": round(sig.multiple, 1),
        "rate_usd_per_hour": round(rate * 60, 2),
        "prevented_usd": round(prevented, 2),
        "binding_cap": binding,
        "binding_cap_usd": (
            caps[binding][0]
            if binding in caps
            else b["teams"][team]["monthly_usd"]
            if binding == "team monthly budget"
            else b["org"]["monthly_usd"]
        )
        if binding
        else None,
        "utc_days": utc_days,
        "key_daily_cap_usd": b["keys"][app]["daily_usd"],
        "until": stop.strftime("%Y-%m-%dT%H:%M"),
    }


def book_runaway(day: dict, info: dict) -> None:
    """Book the loop's calls before the pause on the key, its team, the deployment and the day."""
    n, extra = info["calls_before_pause"], info["loop_spend_usd"]
    a, t = day["apps"][info["key"]], day["teams"][info["team"]]
    m = day["models"].setdefault(info["deployment"], {"requests": 0, "spend_usd": 0.0})
    for row, digits in ((a, 4), (t, 2), (m, 4), (day, 2)):
        row["requests"] += n
        row["spend_usd"] = round(row["spend_usd"] + extra, digits)
    day["anomaly_pauses"] = 1


# ---------- stories ----------


def money(x: float) -> str:
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def when(hhmm: str) -> str:
    h, m = (int(p) for p in hhmm.split(":")[:2])
    return f"{(h - 1) % 12 + 1}:{m:02d} {'am' if h < 12 else 'pm'}"


def plus(hhmm: str, minutes: int) -> str:
    t = clock(hhmm) + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def model_name(dep: str) -> str:
    return ch.MODEL_NAMES[dep]


def stories(days: list[dict], cfg: dict, info: dict) -> tuple[list[dict], list[dict]]:
    """Incidents and the activity feed, with every number taken from the generated days and the config."""
    by_date = {d["date"]: d for d in days}
    breaker = cfg["resilience"]["circuit_breaker"]
    threshold, cooldown = breaker["failure_threshold"], breaker["cooldown_seconds"]
    routes = aliases(cfg)
    incidents, facts = [], {}
    for inc in ch.INCIDENTS:
        dep = inc["deployment"]
        n = by_date[inc["date"]]["incident_fallbacks"].get(dep, 0)
        hit = affected_aliases(dep, routes)
        to = " and ".join(sorted({model_name(next_target(a, routes[a], dep)) for a in hit}))
        end = plus(inc["start"], inc["minutes"])
        incidents.append(
            {
                "date": inc["date"],
                "provider": inc["provider"],
                "deployment": dep,
                "duration_minutes": inc["minutes"],
                "window": f"{when(inc['start'])} to {when(end)}",
                "what": f"{inc['error'][:1].upper()}{inc['error'][1:]} responses on {model_name(dep)}",
                "routes_affected": hit,
                "fallback_requests": n,
                "failed_requests": 0,
                "handled": f"The circuit breaker on {model_name(dep)} opened after {threshold} consecutive failures "
                f"(failure_threshold: {threshold}); later requests skipped it and went straight to {to}, and half-open "
                f"probes every {cooldown:g} s closed it again at {when(end)}. {n:,} requests were answered by the "
                "fallback; none failed.",
            }
        )
        facts[inc["date"]] = {"n": n, "to": to, "end": end, "inc": inc}

    aug = [d for d in days if d["date"][:7] == "2026-08"]
    budget = {t: cfg["budgets"]["teams"][t]["monthly_usd"] for t in ch.TEAMS}
    spent = {t: fsum(d["teams"][t]["spend_usd"] for d in aug) for t in ch.TEAMS}
    under = {t: 1 - spent[t] / budget[t] for t in ch.TEAMS}
    most, least = max(under, key=under.get), min(under, key=under.get)
    o6, s16 = facts["2026-10-06"], facts["2026-09-16"]
    c = ch.CONTRACTOR
    notable = [
        {
            "date": "2026-10-06",
            "kind": "reliability",
            "title": f"Twelve minutes of Anthropic overload, {o6['n']:,} requests answered by the fallback",
            "detail": f"{model_name(o6['inc']['deployment'])} returned {o6['inc']['error']} from "
            f"{when(o6['inc']['start'])} to {when(o6['end'])}. After {threshold} consecutive failures its breaker "
            f"opened, and smart-fast and fast-chat requests went straight to {o6['to']}; no member-facing errors.",
        },
        {
            "date": "2026-10-02",
            "kind": "policy",
            "title": "Contractor key refused the heavy-reasoning route",
            "detail": f"A vendor developer's {c['key']} key (team {c['team']}) asked for heavy-reasoning. The "
            "contractors policy allows only smart-fast, cheap-batch and local-first, so the gateway refused it with "
            "403 before any provider saw the prompt; the developer resent it on local-first 24 seconds later.",
        },
        {
            "date": info["date"],
            "kind": "risk",
            "title": f"Looping coding agent paused {info['seconds_to_pause']} seconds after it started",
            "detail": f"From {when(info['started'])} an engineer's coding agent called {info['alias']} in a "
            f"fix-and-retest loop on the {info['key']} key ({money(info['rate_usd_per_hour'])} an hour). After "
            f"{info['calls_before_pause']} calls the key's spend that hour reached {info['baseline_multiple']}x its "
            f"7-day hourly baseline and the gateway paused it. Estimated spend prevented before 8 am the next business "
            f"day: {money(info['prevented_usd'])}, the most the {info['binding_cap']} "
            f"({money(info['binding_cap_usd'])}) allowed across the {info['utc_days']} UTC days the loop would have "
            "run.",
        },
        {
            "date": "2026-09-16",
            "kind": "reliability",
            "title": "OpenAI incident absorbed by fallback",
            "detail": f"{s16['inc']['minutes']} minutes of {s16['inc']['error']} on "
            f"{model_name(s16['inc']['deployment'])}. fast-chat ranked the remaining deployments by latency and "
            f"{s16['to']} answered {s16['n']:,} requests; online and mobile banking chat kept answering, and the "
            "member assistant (smart-fast, Claude Haiku first) was not affected.",
        },
        {
            "date": "2026-09-03",
            "kind": "finops",
            "title": "August showback sent to department heads",
            "detail": f"CSV export by team, app and model: {money(fsum(spent.values()))} in August against "
            f"{money(fsum(budget.values()))} in team budgets ({fsum(spent.values()) / fsum(budget.values()):.0%}). "
            f"{ch.TEAMS[most]} finished {under[most]:.0%} under budget; {ch.TEAMS[least].lower()} came closest, at "
            f"{1 - under[least]:.0%} of its budget.",
        },
        {
            "date": "2026-08-11",
            "kind": "policy",
            "title": "On-prem residency for regulated work moved into policy",
            "detail": "regulated-fast already listed only the on-prem Llama deployment. config/policies.yaml now also "
            "sets allowed_providers: [ollama] and a required pii_redact hook for that route, so a later edit to "
            "routes.yaml cannot add a cloud fallback for dispute or BSA work.",
        },
    ]
    return incidents, notable


def build() -> dict:
    cfg = routes_config()
    routes = aliases(cfg)
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    days: list[dict] = []
    info: dict = {}
    for i in range(DAYS):
        d = start + timedelta(days=i)
        day = make_day(rng, d, i, routes)
        if d.isoformat() == ch.RUNAWAY["date"]:
            info = runaway(days, day, cfg)
            book_runaway(day, info)
        days.append(day)
    b = cfg["budgets"]
    teams = [
        {
            "team": name,
            "label": label,
            "apps": ch.apps_of(name),
            "monthly_budget_usd": b["teams"][name]["monthly_usd"],
            "daily_cap_usd": b["teams"][name]["daily_usd"],
            "cache_hit_rate": round(
                sum(d["teams"][name]["cached"] for d in days) / sum(d["teams"][name]["requests"] for d in days), 3
            ),
        }
        for name, label in ch.TEAMS.items()
    ]
    apps = [
        {
            "app": app,
            "team": spec["team"],
            "routes": [{"alias": a, "share": s} for a, s in spec["routes"]],
            "key_daily_cap_usd": b["keys"][app]["daily_usd"],
            "regulated": spec["regulated"],
        }
        for app, spec in ch.APPS.items()
    ]
    models: dict[str, dict] = {}
    for d in days[-30:]:
        for dep, v in d["models"].items():
            m = models.setdefault(dep, {"model": dep, "requests_30d": 0, "spend_30d_usd": 0.0})
            m["requests_30d"] += v["requests"]
            m["spend_30d_usd"] += v["spend_usd"]
    for m in models.values():
        m["spend_30d_usd"] = round(m["spend_30d_usd"], 2)
    incidents, notable = stories(days, cfg, info)
    return {
        "generated_by": "scripts/generate_sample_company.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated data for demonstration, not measurements.",
        "period": {"start": start.isoformat(), "end": END.isoformat(), "days": DAYS},
        "company": ch.COMPANY,
        "assumptions": ASSUMPTIONS,
        "teams": teams,
        "apps": apps,
        "org_budget": {**b["org"], "source": "config/routes.yaml"},
        "days": days,
        "models": sorted(models.values(), key=lambda x: -x["spend_30d_usd"]),
        "incidents": incidents,
        "anomaly": info,
        "governance": {
            "requests_with_attribution": 1.0,
            "audit_chain_verified_rate": 1.0,
            "content_logging_default": "off (metadata only)",
            "keys_rotated_90d": 14,
        },
        "notable": notable,
    }


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
