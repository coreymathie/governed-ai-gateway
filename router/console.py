# Corey Mathie, 2026
"""
Back end for the console's live mode (demo/ served at /console): config validation and apply,
policy dry runs and the overview roll-up. The HTTP endpoints live in main.py.

Config files the console can show, validate and (when ROUTER_ALLOW_CONFIG_WRITES is on) replace:

    policies   config/policies.yaml (or ROUTER_POLICIES_FILE)   validated by router/policy.py
    routes     config/routes.yaml (or ROUTER_ROUTES_FILE)       validated by router/models.py RouterConfig
    mcp        config/mcp.yaml (or ROUTER_MCP_FILE)             validated by router/mcp_policy.py

Validation uses the same loaders the gateway uses at startup and on POST /admin/reload, so a file
that validates here is a file the gateway accepts. Applying writes the file atomically and reloads
routes, policies and MCP config together; if the reload fails the previous file is restored.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import configcheck, policy, showback
from .config_loader import DEFAULT_MCP, DEFAULT_POLICIES, current, current_policies, reload_all, settings
from .configcheck import CONFIG_NAMES, ConfigRejected

__all__ = ["CONFIG_NAMES", "ConfigRejected", "apply", "config_path", "dry_run", "overview", "read_config", "validate"]


def config_path(name: str) -> Path:
    if name == "policies":
        return Path(settings.ROUTER_POLICIES_FILE) if settings.ROUTER_POLICIES_FILE else DEFAULT_POLICIES
    if name == "routes":
        return Path(settings.ROUTER_ROUTES_FILE)
    if name == "mcp":
        return Path(settings.ROUTER_MCP_FILE) if settings.ROUTER_MCP_FILE else DEFAULT_MCP
    raise KeyError(name)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_config(name: str) -> dict:
    path = config_path(name)
    text = path.read_text() if path.is_file() else ""
    return {
        "name": name,
        "path": str(path),
        "exists": path.is_file(),
        "text": text,
        "sha256": sha256(text),
        "writable": settings.ROUTER_ALLOW_CONFIG_WRITES,
    }


def parse(name: str, text: str):
    return configcheck.parse(name, text)


def validate(name: str, text: str) -> dict:
    return configcheck.validate(
        name,
        text,
        route_aliases=sorted(current().aliases),
        policy_aliases=sorted(current_policies().referenced_aliases()),
        opa_url=bool(settings.OPA_URL),
    )


def apply(name: str, text: str) -> dict:
    """Validate, write atomically, reload everything; restore the previous file if the reload fails."""
    configcheck.parse(name, text)  # raises ConfigRejected
    path = config_path(name)
    before = path.read_text() if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    try:
        reload_all()
    except Exception as e:  # noqa: BLE001 - put the old file back and keep the previous config
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before)
        reload_all()
        raise ConfigRejected([{"message": f"reload failed, previous file restored: {e}", "line": None}]) from None
    return {
        "status": "applied",
        "path": str(path),
        "sha256_before": sha256(before) if before is not None else None,
        "sha256": sha256(text),
    }


def dry_run(
    teams: list[str], aliases: list[str], max_tokens: int | None = None, candidate: policy.PolicySet | None = None
) -> list[dict]:
    """Policy decisions for team x alias pairs against the current routes, without calling anything."""
    return configcheck.decisions(candidate or current_policies(), current().aliases, teams, aliases, max_tokens)


def known_teams(extra: list[str] | None = None) -> list[str]:
    from .store import list_keys

    pol = current_policies()
    cfg = current()
    teams = {k.team for k in list_keys()} | set(pol.teams) | set(cfg.budgets.teams) | set(extra or [])
    return sorted(teams or {"default"})


# ---------- overview ----------


def overview() -> dict:
    from . import routing, traces
    from .anomaly import clear_cache, evaluate_all
    from .store import cache_stats_today, hourly_spend, list_keys, usage_rows

    today = datetime.now(UTC).date().isoformat()
    rows = usage_rows(today, today)
    llm_rows = [r for r in rows if r.get("provider") != "mcp"]
    tot = showback.totals(rows)
    clear_cache()
    labels = {k.id: (k.label, k.team) for k in list_keys()}
    flags = []
    for s in evaluate_all():
        if s.verdict != "ok":
            label, team = labels.get(s.key, ("?", "?"))
            flags.append({"label": label, "team": team, "multiple": round(s.multiple, 2), "verdict": s.verdict})
    breakers = routing.breakers.snapshot()
    return {
        "date": today,
        "kpis": {
            "spend_usd": tot.get("cost_usd", 0.0),
            "requests": sum(1 for r in llm_rows if not r.get("error")),
            "failed_attempts": sum(1 for r in llm_rows if r.get("error")),
            "tool_calls": sum(1 for r in rows if r.get("provider") == "mcp"),
            "cache": cache_stats_today(),
            "anomalies_flagged": sum(1 for f in flags if f["verdict"] == "flagged"),
            "anomalies_paused": sum(1 for f in flags if f["verdict"] == "pause"),
            "open_circuits": sum(1 for b in breakers if b.get("state") != "closed"),
        },
        "traces": traces.stats(),
        "by_team": showback.aggregate(rows, ("team",)),
        "by_model": showback.aggregate(llm_rows, ("provider", "model")),
        "hourly": hourly_spend(),
        "anomalies": sorted(flags, key=lambda f: -f["multiple"]),
        "breakers": breakers,
        "mock_providers": settings.ROUTER_MOCK_PROVIDERS,
    }
