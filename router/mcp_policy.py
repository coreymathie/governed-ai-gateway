# Corey Mathie, 2026
"""
Tool-call policy for the MCP gateway: which team may call which tool on which
server, how fast, how often and for how much, and what of the arguments may
be logged. Pure Python (stdlib only); the browser demo loads it unchanged.

config/mcp.yaml (schema validated here; unknown keys are errors):

    version: 1
    servers:
      tickets:
        url: http://127.0.0.1:9101/mcp        # upstream MCP server, Streamable HTTP
        timeout_s: 10
        headers_from_env: { Authorization: MCP_TICKETS_AUTH }   # header -> env var read per call
    defaults:                                  # limits for every allow entry unless it sets its own
      per_minute: 60                           # calls per key per tool (sliding 60 s)
      team_per_minute: 0                       # calls per team per tool (0 = no limit)
      max_identical_per_minute: 5              # same tool + same arguments, per key: agent-loop guard
      per_day: 0                               # calls per team per tool per UTC day
      cost_usd: 0.0                            # price per call, recorded in usage and showback
      daily_usd: 0.0                           # spend cap per team per tool per UTC day
      redact_args: true                        # PII-redact argument values in the audit log
      forward_redacted: false                  # send redacted arguments upstream instead of the originals
    teams:
      support:
        - { server: tickets, tool: "search_*" }
        - { server: tickets, tool: close_ticket, per_minute: 5, per_day: 200 }

Nothing is allowed unless an entry allows it (default deny). Tool names match
entries with fnmatch globs; the first matching entry applies.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from . import pii

LIMIT_KEYS = {
    "per_minute": int,
    "team_per_minute": int,
    "max_identical_per_minute": int,
    "per_day": int,
    "cost_usd": float,
    "daily_usd": float,
    "redact_args": bool,
    "forward_redacted": bool,
}
SERVER_KEYS = {"url", "timeout_s", "headers_from_env"}


class McpConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    per_minute: int = 60
    team_per_minute: int = 0
    max_identical_per_minute: int = 5
    per_day: int = 0
    cost_usd: float = 0.0
    daily_usd: float = 0.0
    redact_args: bool = True
    forward_redacted: bool = False


@dataclass(frozen=True)
class AllowEntry:
    server: str
    tool: str  # fnmatch glob
    limits: Limits


@dataclass(frozen=True)
class Server:
    name: str
    url: str
    timeout_s: float = 10.0
    headers_from_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class McpPolicy:
    servers: Mapping[str, Server] = field(default_factory=dict)
    defaults: Limits = Limits()
    teams: Mapping[str, tuple[AllowEntry, ...]] = field(default_factory=dict)

    def entry(self, team: str, server: str, tool: str) -> AllowEntry | None:
        for e in self.teams.get(team, ()):
            if e.server == server and fnmatch.fnmatchcase(tool, e.tool):
                return e
        return None

    def team_can_use_server(self, team: str, server: str) -> bool:
        return any(e.server == server for e in self.teams.get(team, ()))


def _limits(raw: Mapping, where: str, base: Limits) -> Limits:
    unknown = set(raw) - set(LIMIT_KEYS)
    if unknown:
        raise McpConfigError(f"{where}: unknown keys {sorted(unknown)}")
    kw = {}
    for k, v in raw.items():
        typ = LIMIT_KEYS[k]
        if typ is bool:
            if not isinstance(v, bool):
                raise McpConfigError(f"{where}.{k}: expected true/false")
        elif isinstance(v, bool) or not isinstance(v, int | float) or v < 0 or (typ is int and v != int(v)):
            raise McpConfigError(f"{where}.{k}: expected a non-negative {'integer' if typ is int else 'number'}")
        kw[k] = typ(v)
    return replace(base, **kw)


def load(data: Any) -> McpPolicy:
    """Validate a parsed mcp.yaml. None means no MCP servers (the gateway's /mcp endpoints answer 404)."""
    if data is None:
        return McpPolicy()
    if not isinstance(data, Mapping):
        raise McpConfigError("mcp: top level must be a mapping")
    unknown = set(data) - {"version", "servers", "defaults", "teams"}
    if unknown:
        raise McpConfigError(f"mcp: unknown top-level keys {sorted(unknown)}")
    if data.get("version", 1) != 1:
        raise McpConfigError("mcp: unsupported version (expected 1)")
    servers = {}
    for name, raw in (data.get("servers") or {}).items():
        if not isinstance(raw, Mapping) or set(raw) - SERVER_KEYS or "url" not in raw:
            raise McpConfigError(f"mcp.servers.{name}: expected {{url, timeout_s?, headers_from_env?}}")
        url = raw["url"]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise McpConfigError(f"mcp.servers.{name}.url: expected an http(s) URL")
        hdrs = raw.get("headers_from_env") or {}
        if not isinstance(hdrs, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in hdrs.items()):
            raise McpConfigError(f"mcp.servers.{name}.headers_from_env: expected header: ENV_VAR pairs")
        timeout = raw.get("timeout_s", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= 120:
            raise McpConfigError(f"mcp.servers.{name}.timeout_s: expected 0 < seconds <= 120")
        servers[str(name)] = Server(str(name), url, float(timeout), dict(hdrs))
    defaults = _limits(data.get("defaults") or {}, "mcp.defaults", Limits())
    teams: dict[str, tuple[AllowEntry, ...]] = {}
    for team, entries in (data.get("teams") or {}).items():
        if not isinstance(entries, list):
            raise McpConfigError(f"mcp.teams.{team}: expected a list of allow entries")
        out = []
        for i, raw in enumerate(entries):
            where = f"mcp.teams.{team}[{i}]"
            if not isinstance(raw, Mapping) or "server" not in raw or "tool" not in raw:
                raise McpConfigError(f"{where}: expected {{server, tool, ...limits}}")
            if raw["server"] not in servers:
                raise McpConfigError(f"{where}: unknown server {raw['server']!r}")
            if not isinstance(raw["tool"], str) or not raw["tool"]:
                raise McpConfigError(f"{where}.tool: expected a tool name or glob")
            lim = _limits({k: v for k, v in raw.items() if k not in ("server", "tool")}, where, defaults)
            out.append(AllowEntry(raw["server"], raw["tool"], lim))
        teams[str(team)] = tuple(out)
    return McpPolicy(servers, defaults, teams)


# ---------- velocity ----------


class VelocityLimiter:
    """Sliding 60-second windows per scope, like card-fraud velocity rules. Process-local."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _window(self, scope: str) -> deque[float]:
        q = self._events[scope]
        cutoff = self.clock() - 60.0
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def check(self, limits: Mapping[str, int]) -> tuple[str, float] | None:
        """(scope, retry_after_s) for the first scope at its limit, else None. Doesn't record."""
        for scope, limit in limits.items():
            if limit and len(self._window(scope)) >= limit:
                q = self._events[scope]
                return scope, max(0.0, q[0] + 60.0 - self.clock())
        return None

    def record(self, scopes: Iterable[str]) -> None:
        now = self.clock()
        for s in scopes:
            self._events[s].append(now)

    def reset(self) -> None:
        self._events.clear()


def args_fingerprint(arguments: Any) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class CallDecision:
    allow: bool
    action: str  # allow | deny | rate_limited | cap_reached
    reason: str = ""
    code: int = 0  # JSON-RPC error code when refused
    retry_after: float = 0.0
    entry: AllowEntry | None = None
    scopes: list[str] = field(default_factory=list)  # velocity scopes to record when the call goes ahead


# JSON-RPC error codes the gateway uses (server-defined range -32000..-32099)
ERR_DENIED, ERR_RATE, ERR_CAP, ERR_UPSTREAM, ERR_POLICY = -32001, -32002, -32003, -32004, -32005


def check_call(
    policy: McpPolicy,
    limiter: VelocityLimiter,
    team: str,
    key_id: str,
    server: str,
    tool: str,
    arguments: Any,
    used_today: tuple[int, float] = (0, 0.0),  # (calls, USD) by this team on this tool today
) -> CallDecision:
    """Allow-list, then daily count/spend caps, then velocity. Refusals never reach the upstream server."""
    entry = policy.entry(team, server, tool)
    if entry is None:
        return CallDecision(False, "deny", f"tool {server}/{tool} is not allowed for team {team!r}", ERR_DENIED)
    lim = entry.limits
    calls, spent = used_today
    if lim.per_day and calls >= lim.per_day:
        return CallDecision(False, "cap_reached", f"daily limit of {lim.per_day} calls reached", ERR_CAP, entry=entry)
    if lim.daily_usd and spent + lim.cost_usd > lim.daily_usd + 1e-12:
        return CallDecision(False, "cap_reached", f"daily spend cap ${lim.daily_usd:g} reached", ERR_CAP, entry=entry)
    base = f"{server}/{tool}"
    scopes = {
        f"key:{key_id}:{base}": lim.per_minute,
        f"team:{team}:{base}": lim.team_per_minute,
        f"same:{key_id}:{base}:{args_fingerprint(arguments)}": lim.max_identical_per_minute,
    }
    hit = limiter.check(scopes)
    if hit:
        scope, wait = hit
        what = {"key": "per-key", "team": "per-team", "same": "identical-call"}[scope.split(":", 1)[0]]
        return CallDecision(
            False, "rate_limited", f"{what} velocity limit for {base}", ERR_RATE, max(1.0, math.ceil(wait)), entry
        )
    return CallDecision(True, "allow", entry=entry, scopes=list(scopes))


def filter_tools(policy: McpPolicy, team: str, server: str, tools: list) -> list:
    """tools/list result filtered to what the team may call (others aren't even shown)."""
    return [t for t in tools if isinstance(t, Mapping) and policy.entry(team, server, str(t.get("name", "")))]


def redact_args(value: Any, kinds: tuple[str, ...] = pii.KINDS) -> tuple[Any, dict[str, int]]:
    """Redact PII in every string inside a JSON-like value. Returns (copy, {kind: count})."""
    found: dict[str, int] = {}

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            new, counts = pii.redact(v, kinds)
            for k, n in counts.items():
                found[k] = found.get(k, 0) + n
            return new
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value), found
