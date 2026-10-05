# Corey Mathie, 2026
"""
Budget hierarchy: org -> team -> key, daily and monthly, in USD.

Every request is checked against all six caps *before* a provider is called,
broadest scope first; the first cap already reached rejects it (HTTP 402).
Team and key caps come from `policies` defaults, overridden per team / per key
label in the `budgets:` section of routes.yaml. A cap of 0 (or unset) means no cap.

What "enforced" means here, precisely: a request is admitted when recorded
spend is still below every cap. Spend is recorded when a call finishes, so
concurrent in-flight requests can carry a scope past its cap by at most their
own cost. Calls to models without a known price are recorded at $0 and don't
move any cap (see costs.py and /health).

Pure Python, no dependencies; the browser demo runs it unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

SCOPES = ("org", "team", "key")
PERIODS = ("daily", "monthly")

# spend(scope, ident, period) -> USD spent so far in the current UTC day / month
SpendFn = Callable[[str, str | None, str], float]


@dataclass(frozen=True)
class Limits:
    daily_usd: float | None = None
    monthly_usd: float | None = None
    tpm: int | None = None

    def over(self, base: Limits) -> Limits:
        """Fields set here win; unset fields inherit from base."""
        return Limits(
            self.daily_usd if self.daily_usd is not None else base.daily_usd,
            self.monthly_usd if self.monthly_usd is not None else base.monthly_usd,
            self.tpm if self.tpm is not None else base.tpm,
        )

    def cap(self, period: str) -> float:
        return float((self.daily_usd if period == "daily" else self.monthly_usd) or 0.0)


@dataclass
class Hierarchy:
    org: Limits = field(default_factory=Limits)
    team_default: Limits = field(default_factory=Limits)
    key_default: Limits = field(default_factory=Limits)
    teams: Mapping[str, Limits] = field(default_factory=dict)
    keys: Mapping[str, Limits] = field(default_factory=dict)  # by key label

    def team(self, name: str) -> Limits:
        return self.teams.get(name, Limits()).over(self.team_default)

    def key(self, label: str) -> Limits:
        return self.keys.get(label, Limits()).over(self.key_default)

    def warnings(self) -> list[str]:
        """Config smells: a child cap above its parent's can never be the binding one."""
        out = []
        for period in PERIODS:
            org_cap = self.org.cap(period)
            for name in sorted(self.teams):
                team_cap = self.team(name).cap(period)
                if org_cap and team_cap > org_cap:
                    out.append(f"team {name!r} {period} cap ${team_cap:g} exceeds the org cap ${org_cap:g}")
            for label, lim in sorted(self.keys.items()):
                key_cap = lim.over(self.key_default).cap(period)
                if org_cap and key_cap > org_cap:
                    out.append(f"key {label!r} {period} cap ${key_cap:g} exceeds the org cap ${org_cap:g}")
        return out


@dataclass(frozen=True)
class BudgetBreach:
    scope: str  # org | team | key
    period: str  # daily | monthly
    limit: float
    spent: float

    @property
    def message(self) -> str:
        who = "org" if self.scope == "org" else f"per-{self.scope}"
        return f"{who} {self.period} spend cap reached"

    @property
    def reason(self) -> str:
        return f"budget_{self.scope}_{self.period}"


def check(h: Hierarchy, team: str, key_label: str, key_id: str, spend: SpendFn) -> BudgetBreach | None:
    """First cap already reached, broadest scope first; None if the request may proceed."""
    scoped = (("org", None, h.org), ("team", team, h.team(team)), ("key", key_id, h.key(key_label)))
    for scope, ident, limits in scoped:
        for period in PERIODS:
            cap = limits.cap(period)
            if not cap:
                continue
            spent = spend(scope, ident, period)
            if spent >= cap:
                return BudgetBreach(scope, period, cap, spent)
    return None


def utilization(h: Hierarchy, teams: list[str], spend: SpendFn) -> dict:
    """Spend vs. caps for the org and each team (for dashboards)."""

    def row(limits: Limits, scope: str, ident: str | None) -> dict:
        out = {}
        for period in PERIODS:
            cap = limits.cap(period)
            spent = spend(scope, ident, period)
            out[period] = {
                "spent_usd": round(spent, 6),
                "cap_usd": cap or None,
                "used_pct": round(100 * spent / cap, 1) if cap else None,
            }
        out["tpm"] = limits.tpm or None
        return out

    return {
        "org": row(h.org, "org", None),
        "teams": {t: row(h.team(t), "team", t) for t in sorted(set(teams))},
        "warnings": h.warnings(),
    }
