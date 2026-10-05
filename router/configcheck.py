# Corey Mathie, 2026
"""
Validate the gateway's YAML config files with the gateway's own loaders, for the console's Policies
screen. Used by the live API (router/console.py) and, unchanged, by the browser demo in Pyodide.

    policies   router/policy.py load()          (pure Python)
    mcp        router/mcp_policy.py load()      (pure Python)
    routes     router/models.py RouterConfig    (pydantic; the demo loads Pyodide's pydantic on demand)

Errors come back as [{"message", "line"}]; the line is exact for YAML syntax errors and a best-effort
pointer for schema errors. PyYAML and pydantic are imported inside the functions that need them, so
this module imports with the standard library alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from . import mcp_policy, policy
from .pii import registered_hooks

CONFIG_NAMES = ("policies", "routes", "mcp")


class ConfigRejected(ValueError):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def _yaml_errors(e: Exception) -> list[dict]:
    mark = getattr(e, "problem_mark", None)
    line = mark.line + 1 if mark is not None else None
    problem = getattr(e, "problem", None) or str(e)
    return [{"message": f"YAML syntax: {problem}", "line": line}]


def line_of(text: str, path: Sequence[Any]) -> int | None:
    """Best effort: the line where the last named key of an error location appears, searching in order."""
    keys = [str(p) for p in path if not isinstance(p, int)]
    lines = text.splitlines()
    start, found = 0, None
    for k in keys:
        pat = re.compile(r"(^|[\s{,])[\"']?" + re.escape(k) + r"[\"']?\s*:")
        for i in range(start, len(lines)):
            if pat.search(lines[i]):
                start, found = i, i + 1
                break
        else:
            break
    return found


def parse(name: str, text: str) -> Any:
    """Load config text with the gateway's own loader. Returns the loaded object; raises ConfigRejected."""
    if name not in CONFIG_NAMES:
        raise KeyError(name)
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigRejected(_yaml_errors(e)) from None
    if name == "policies":
        try:
            return policy.load(data, registered_hooks())
        except policy.PolicyConfigError as e:
            raise ConfigRejected([{"message": str(e), "line": _line_for_message(text, str(e))}]) from None
    if name == "mcp":
        try:
            return mcp_policy.load(data)
        except mcp_policy.McpConfigError as e:
            raise ConfigRejected([{"message": str(e), "line": _line_for_message(text, str(e))}]) from None
    from pydantic import ValidationError

    from .models import RouterConfig

    try:
        return RouterConfig.model_validate(data)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = tuple(err.get("loc") or ())
            where = ".".join(str(p) for p in loc) or "routes"
            errors.append({"message": f"{where}: {err.get('msg')}", "line": line_of(text, loc)})
        raise ConfigRejected(errors) from None


def _line_for_message(text: str, message: str) -> int | None:
    """Loader messages start with a dotted path such as policies.teams.regulated.allowed_providers."""
    head = message.split(":", 1)[0].strip()
    parts = head.split(".")[1:] if "." in head else []
    return line_of(text, parts) if parts else None


def summary(name: str, loaded: Any) -> dict:
    if name == "policies":
        return {
            "teams": sorted(loaded.teams),
            "routes": sorted(loaded.routes),
            "defaults": asdict(loaded.defaults),
            "opa_enabled": loaded.opa.enabled,
        }
    if name == "mcp":
        return {"servers": sorted(loaded.servers), "teams": sorted(loaded.teams)}
    return {
        "aliases": {a: [f"{t.provider}/{t.model}" for t in ts] for a, ts in sorted(loaded.aliases.items())},
        "strategies": {a: loaded.strategy(a) for a in sorted(loaded.aliases)},
    }


def warnings_for(
    name: str, loaded: Any, route_aliases: Sequence[str] = (), policy_aliases: Sequence[str] = (), opa_url: bool = False
) -> list[str]:
    out = []
    if name == "policies":
        unknown = loaded.referenced_aliases() - set(route_aliases)
        if unknown:
            out.append(f"references aliases not in the routes file: {sorted(unknown)}")
        if loaded.opa.enabled and not opa_url:
            out.append("opa.enabled is true but OPA_URL is not set: every request would be refused")
    elif name == "routes":
        unknown = set(policy_aliases) - set(loaded.aliases)
        if unknown:
            out.append(f"the active policies reference aliases this file drops: {sorted(unknown)}")
    return out


def validate(
    name: str, text: str, route_aliases: Sequence[str] = (), policy_aliases: Sequence[str] = (), opa_url: bool = False
) -> dict:
    try:
        loaded = parse(name, text)
    except ConfigRejected as e:
        return {"ok": False, "errors": e.errors, "warnings": []}
    return {
        "ok": True,
        "errors": [],
        "warnings": warnings_for(name, loaded, route_aliases, policy_aliases, opa_url),
        "summary": summary(name, loaded),
    }


def decisions(
    pol: policy.PolicySet,
    routes: Mapping[str, Sequence[Any]],
    teams: Sequence[str],
    aliases: Sequence[str],
    max_tokens: int | None = None,
) -> list[dict]:
    """Policy decisions for team x alias pairs (targets: objects with .provider and .model). Calls nothing."""
    out = []
    for team in teams:
        for alias in aliases:
            if alias not in routes:
                out.append({"team": team, "alias": alias, "allow": False, "status": 400, "reasons": ["unknown alias"]})
                continue
            facts = policy.RequestFacts(team=team, alias=alias, targets=list(routes[alias]), max_tokens=max_tokens)
            facts.messages = 1
            out.append({"team": team, "alias": alias, **policy.evaluate(pol, facts).as_dict()})
    return out
