# Corey Mathie, 2026
"""
Policy-as-code: a declarative admission and routing policy evaluated before
any budget check, cache lookup or provider call.

config/policies.yaml (schema validated here; unknown keys are errors):

    version: 1
    defaults:   { <rule> }          # applies to every request
    teams:      { <team>:  <rule> }
    routes:     { <alias>: <rule> }
    opa:        { enabled: false, path: router/decision, timeout_s: 0.5 }

A rule may set any of:

    allow_aliases:      [alias, ...]        only these aliases (absent = any)
    deny_aliases:       [alias, ...]
    allowed_providers:  [ollama, ...]       data residency: other providers are removed from the route
    allow_models:       ["openai/*", ...]   fnmatch globs on provider/model; others are removed
    deny_models:        ["openai/gpt-4.1"]
    max_tokens_ceiling: 4096                0 = none
    max_tokens_mode:    reject | clamp      over the ceiling: 400, or lower it to the ceiling
    max_request_bytes:  262144              UTF-8 size of the serialized messages; 413 above it
    max_messages:       200                 413 above it
    required_hooks:     [pii_redact]        content hooks (router/pii.py) that must run on the request

Layers combine so the most restrictive wins: allow-lists intersect, deny-lists
union, numeric ceilings take the smallest non-zero value, `reject` beats
`clamp`, required hooks union. A route can't loosen a team or default rule.

Targets a rule removes are never tried, so a fallback can't reach a provider
the policy excludes; if nothing is left the request is refused (403) rather
than routed somewhere else. Evaluation is deterministic and side-effect free.
Pure Python; the browser demo loads this file unchanged.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("openai", "anthropic", "gemini", "ollama")
RULE_KEYS = {
    "allow_aliases": "list",
    "deny_aliases": "list",
    "allowed_providers": "list",
    "allow_models": "list",
    "deny_models": "list",
    "max_tokens_ceiling": "int",
    "max_tokens_mode": "mode",
    "max_request_bytes": "int",
    "max_messages": "int",
    "required_hooks": "list",
}
TOP_KEYS = {"version", "defaults", "teams", "routes", "opa"}
OPA_KEYS = {"enabled", "path", "timeout_s"}


class PolicyConfigError(ValueError):
    """The policy file doesn't match the schema. The gateway refuses to start (or keeps the previous policy)."""


@dataclass(frozen=True)
class Rule:
    allow_aliases: tuple[str, ...] | None = None
    deny_aliases: tuple[str, ...] = ()
    allowed_providers: tuple[str, ...] | None = None
    allow_models: tuple[str, ...] | None = None
    deny_models: tuple[str, ...] = ()
    max_tokens_ceiling: int = 0
    max_tokens_mode: str | None = None  # None = not set in this layer
    max_request_bytes: int = 0
    max_messages: int = 0
    required_hooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpaSettings:
    enabled: bool = False
    path: str = "router/decision"
    timeout_s: float = 0.5


@dataclass(frozen=True)
class PolicySet:
    version: int = 1
    defaults: Rule = Rule()
    teams: Mapping[str, Rule] = field(default_factory=dict)
    routes: Mapping[str, Rule] = field(default_factory=dict)
    opa: OpaSettings = OpaSettings()

    def referenced_aliases(self) -> set[str]:
        out = set(self.routes)
        for r in (self.defaults, *self.teams.values(), *self.routes.values()):
            out |= set(r.allow_aliases or ()) | set(r.deny_aliases)
        return out


# ---------- loading / schema ----------


def _rule(raw: Any, where: str, known_hooks: Iterable[str] | None) -> Rule:
    if raw is None:
        return Rule()
    if not isinstance(raw, Mapping):
        raise PolicyConfigError(f"{where}: expected a mapping, got {type(raw).__name__}")
    unknown = set(raw) - set(RULE_KEYS)
    if unknown:
        raise PolicyConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(RULE_KEYS)}")
    kw: dict[str, Any] = {}
    for k, v in raw.items():
        kind = RULE_KEYS[k]
        if kind == "list":
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise PolicyConfigError(f"{where}.{k}: expected a list of non-empty strings")
            kw[k] = tuple(v)
        elif kind == "int":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise PolicyConfigError(f"{where}.{k}: expected a non-negative integer")
            kw[k] = v
        else:
            if v not in ("reject", "clamp"):
                raise PolicyConfigError(f"{where}.{k}: expected 'reject' or 'clamp'")
            kw[k] = v
    bad = sorted(set(kw.get("allowed_providers", ())) - set(PROVIDERS))
    if bad:
        raise PolicyConfigError(f"{where}.allowed_providers: unknown providers {bad}; known: {list(PROVIDERS)}")
    for g in (*kw.get("allow_models", ()), *kw.get("deny_models", ())):
        if "/" not in g:
            raise PolicyConfigError(f"{where}: model pattern {g!r} must be 'provider/model' (globs allowed)")
    if known_hooks is not None:
        missing = sorted(set(kw.get("required_hooks", ())) - set(known_hooks))
        if missing:
            raise PolicyConfigError(f"{where}.required_hooks: unregistered hooks {missing}")
    return Rule(**kw)


def load(data: Any, known_hooks: Iterable[str] | None = None) -> PolicySet:
    """Validate a parsed policies.yaml. `None` or an empty file means no restrictions."""
    if data is None:
        return PolicySet()
    if not isinstance(data, Mapping):
        raise PolicyConfigError("policies: top level must be a mapping")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise PolicyConfigError(f"policies: unknown top-level keys {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}")
    version = data.get("version", 1)
    if version != 1:
        raise PolicyConfigError(f"policies: unsupported version {version!r} (expected 1)")
    hooks = list(known_hooks) if known_hooks is not None else None
    teams, routes = data.get("teams") or {}, data.get("routes") or {}
    for name, block in (("teams", teams), ("routes", routes)):
        if not isinstance(block, Mapping):
            raise PolicyConfigError(f"policies.{name}: expected a mapping")
    opa_raw = data.get("opa") or {}
    if not isinstance(opa_raw, Mapping) or set(opa_raw) - OPA_KEYS:
        raise PolicyConfigError(f"policies.opa: expected a mapping with keys {sorted(OPA_KEYS)}")
    timeout = opa_raw.get("timeout_s", 0.5)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= 10:
        raise PolicyConfigError("policies.opa.timeout_s: expected a number between 0 and 10")
    path = opa_raw.get("path", "router/decision")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path:
        raise PolicyConfigError("policies.opa.path: expected a relative data path like 'router/decision'")
    return PolicySet(
        version=1,
        defaults=_rule(data.get("defaults"), "policies.defaults", hooks),
        teams={str(t): _rule(r, f"policies.teams.{t}", hooks) for t, r in teams.items()},
        routes={str(a): _rule(r, f"policies.routes.{a}", hooks) for a, r in routes.items()},
        opa=OpaSettings(bool(opa_raw.get("enabled", False)), path, float(timeout)),
    )


# ---------- combining layers ----------


def _intersect(values: Sequence[tuple[str, ...] | None]) -> tuple[str, ...] | None:
    out: list[str] | None = None
    for v in values:
        if v is None:
            continue
        out = list(v) if out is None else [x for x in out if x in v]
    return tuple(out) if out is not None else None


def _union(values: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for v in values for x in v))


def _min_nonzero(values: Sequence[int]) -> int:
    nz = [v for v in values if v]
    return min(nz) if nz else 0


def effective_rule(policies: PolicySet, team: str, alias: str) -> tuple[Rule, list[str]]:
    """Combine defaults, the team's rule and the route's rule. Returns (rule, names of layers that applied)."""
    layers = [("defaults", policies.defaults)]
    if team in policies.teams:
        layers.append((f"teams.{team}", policies.teams[team]))
    if alias in policies.routes:
        layers.append((f"routes.{alias}", policies.routes[alias]))
    rules = [r for _, r in layers]
    modes = [r.max_tokens_mode for r in rules if r.max_tokens_mode]
    combined = Rule(
        allow_aliases=_intersect([r.allow_aliases for r in rules]),
        deny_aliases=_union([r.deny_aliases for r in rules]),
        allowed_providers=_intersect([r.allowed_providers for r in rules]),
        allow_models=None,  # checked layer by layer below: a target must match every layer that has a list
        deny_models=_union([r.deny_models for r in rules]),
        max_tokens_ceiling=_min_nonzero([r.max_tokens_ceiling for r in rules]),
        max_tokens_mode="reject" if (not modes or "reject" in modes) else "clamp",
        max_request_bytes=_min_nonzero([r.max_request_bytes for r in rules]),
        max_messages=_min_nonzero([r.max_messages for r in rules]),
        required_hooks=_union([r.required_hooks for r in rules]),
    )
    return combined, [name for name, _ in layers]


# ---------- evaluation ----------


@dataclass
class RequestFacts:
    team: str
    alias: str
    targets: Sequence[Any]  # objects with .provider and .model, in route order
    max_tokens: int | None = None
    request_bytes: int = 0
    messages: int = 0
    key_label: str = ""
    stream: bool = False


@dataclass
class Decision:
    allow: bool
    status: int = 200  # HTTP status when denied
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)  # layers that applied
    targets: list[Any] = field(default_factory=list)  # permitted targets, route order kept
    removed: list[dict] = field(default_factory=list)  # {"deployment", "reason"}
    max_tokens: int | None = None  # effective value to send
    max_tokens_clamped: bool = False
    required_hooks: list[str] = field(default_factory=list)
    source: str = "local"  # "local" or "local+opa"

    def as_dict(self) -> dict:
        return {
            "allow": self.allow,
            "status": self.status,
            "reasons": list(self.reasons),
            "rules": list(self.rules),
            "targets": [f"{t.provider}/{t.model}" for t in self.targets],
            "removed": list(self.removed),
            "max_tokens": self.max_tokens,
            "max_tokens_clamped": self.max_tokens_clamped,
            "required_hooks": list(self.required_hooks),
            "source": self.source,
        }

    def fingerprint(self) -> str:
        """Identifies what the policy permitted; part of cache partitions so entries never cross policies."""
        material = json.dumps(
            [sorted(f"{t.provider}/{t.model}" for t in self.targets), self.max_tokens, sorted(self.required_hooks)]
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]


def _deny(d: Decision, status: int, reason: str) -> Decision:
    d.allow, d.status = False, status
    d.reasons.append(reason)
    d.targets = []
    return d


def evaluate(policies: PolicySet, facts: RequestFacts) -> Decision:
    rule, names = effective_rule(policies, facts.team, facts.alias)
    d = Decision(allow=True, rules=names, required_hooks=list(rule.required_hooks), max_tokens=facts.max_tokens)

    if facts.alias in rule.deny_aliases or (rule.allow_aliases is not None and facts.alias not in rule.allow_aliases):
        return _deny(d, 403, f"alias {facts.alias!r} is not permitted for team {facts.team!r}")
    if rule.max_request_bytes and facts.request_bytes > rule.max_request_bytes:
        return _deny(d, 413, f"request is {facts.request_bytes} bytes; the limit is {rule.max_request_bytes}")
    if rule.max_messages and facts.messages > rule.max_messages:
        return _deny(d, 413, f"request has {facts.messages} messages; the limit is {rule.max_messages}")

    ceiling = rule.max_tokens_ceiling
    if ceiling:
        if facts.max_tokens is None:
            d.max_tokens, d.max_tokens_clamped = ceiling, True  # unset would mean "provider default", unbounded here
        elif facts.max_tokens > ceiling:
            if rule.max_tokens_mode == "reject":
                return _deny(d, 400, f"max_tokens {facts.max_tokens} is above the ceiling of {ceiling}")
            d.max_tokens, d.max_tokens_clamped = ceiling, True

    layers = [(n, r) for n, r in zip(names, _layer_rules(policies, facts), strict=True)]
    for t in facts.targets:
        dep = f"{t.provider}/{t.model}"
        why = None
        if rule.allowed_providers is not None and t.provider not in rule.allowed_providers:
            why = f"provider {t.provider} not in allowed_providers"
        elif any(fnmatch.fnmatchcase(dep, g) for g in rule.deny_models):
            why = "matches deny_models"
        else:
            for name, r in layers:
                if r.allow_models is not None and not any(fnmatch.fnmatchcase(dep, g) for g in r.allow_models):
                    why = f"not in {name}.allow_models"
                    break
        if why:
            d.removed.append({"deployment": dep, "reason": why})
        else:
            d.targets.append(t)
    if not d.targets:
        detail = "; ".join(f"{r['deployment']}: {r['reason']}" for r in d.removed)
        return _deny(d, 403, f"no deployment of {facts.alias!r} is permitted for team {facts.team!r} ({detail})")
    return d


def _layer_rules(policies: PolicySet, facts: RequestFacts) -> list[Rule]:
    rules = [policies.defaults]
    if facts.team in policies.teams:
        rules.append(policies.teams[facts.team])
    if facts.alias in policies.routes:
        rules.append(policies.routes[facts.alias])
    return rules


def apply_opa(decision: Decision, result: Any) -> Decision:
    """Combine a local allow with an OPA result ({"allow": bool, "reasons": [...], "allowed_targets": [...]}).

    Anything other than a mapping with a boolean `allow` is treated as a deny (fail closed). OPA can only
    narrow what the local policy permitted, never widen it.
    """
    decision.source = "local+opa"
    if not decision.allow:
        return decision
    if not isinstance(result, Mapping) or not isinstance(result.get("allow"), bool):
        return _deny(decision, 503, "OPA returned no decision (undefined or malformed result)")
    reasons = [str(r) for r in (result.get("reasons") or []) if r]
    if not result["allow"]:
        decision.reasons.extend(reasons or ["denied by OPA policy"])
        decision.allow, decision.status, decision.targets = False, 403, []
        return decision
    allowed = result.get("allowed_targets")
    if allowed is not None:
        if not isinstance(allowed, list):
            return _deny(decision, 503, "OPA allowed_targets must be a list")
        keep = [t for t in decision.targets if f"{t.provider}/{t.model}" in allowed]
        for t in decision.targets:
            if t not in keep:
                decision.removed.append({"deployment": f"{t.provider}/{t.model}", "reason": "removed by OPA"})
        decision.targets = keep
        if not keep:
            return _deny(decision, 403, "OPA permitted none of the route's deployments")
    return decision


def opa_input(decision: Decision, facts: RequestFacts) -> dict:
    """The document sent to OPA: request metadata and the local decision. Never message content."""
    return {
        "team": facts.team,
        "key_label": facts.key_label,
        "alias": facts.alias,
        "targets": [f"{t.provider}/{t.model}" for t in decision.targets],
        "max_tokens": decision.max_tokens,
        "request_bytes": facts.request_bytes,
        "messages": facts.messages,
        "stream": facts.stream,
        "required_hooks": list(decision.required_hooks),
        "local_rules": list(decision.rules),
    }
