# Corey Mathie, 2026
"""
PII and secret detection, redaction, and pluggable content hooks.

Detectors (pattern-based, no ML, no network):
  email    RFC-5322-ish local@domain.tld
  card     13-19 digits, optionally grouped by spaces or dashes, that pass the Luhn check
  ssn      US SSN written ###-##-#### or ### ## ####, excluding never-issued ranges (000, 666, 9xx)
  phone    North American numbers with separators or parentheses, and +country international numbers
  secret   API-key-like tokens: sk-..., AKIA..., ghp_/gho_/ghs_..., xox?-..., AIza..., JWTs,
           "Bearer <token>", PEM private-key headers

Detection is a best-effort pattern match. It misses PII written in ways the
patterns don't cover (names, addresses, numbers spelled out, unusual formats)
and can flag look-alikes (an order number that passes Luhn). It reduces what
reaches a provider or a log; it is not a guarantee.

Hooks: a hook is a callable `(texts, ctx) -> HookOutcome` registered under a
name. The gateway runs the configured `pre_request` hooks over message
contents before any provider call and `post_response` hooks over the
completion text before it reaches the client, the cache or the content log.
Built-ins: `pii_redact` (replace matches with [REDACTED:KIND]), `pii_block`
(refuse the request if anything matches), `pii_detect` (count and audit,
change nothing). Register your own with `register_hook(name, fn)` from a module
listed in ROUTER_HOOK_MODULES.

Pure Python (stdlib only); the browser demo loads this file unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

KINDS = ("secret", "email", "card", "ssn", "phone")  # also the order detectors run in

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
_PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)|\d{1,4})(?:[\s.-]?\d{2,4}){2,4}"  # +44 20 7946 0958, +1-202-555-0143
    r"|(?:1[\s.-])?\(\d{3}\)\s?\d{3}[\s.-]\d{4}"  # (202) 555-0143
    r"|(?:1[\s.-])?\d{3}[\s.-]\d{3}[\s.-]\d{4}"  # 202-555-0143, 202.555.0143
    r")(?!\w)"
)
_SECRET = re.compile(
    r"(?:"
    r"\bsk-[A-Za-z0-9_-]{20,}"  # OpenAI / Anthropic style, also this gateway's own sk-router- keys
    r"|\bAKIA[0-9A-Z]{16}\b"  # AWS access key id
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"  # GitHub tokens
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|\bAIza[0-9A-Za-z_-]{35}"  # Google API keys
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWT
    r"|(?i:\bbearer\s+)[A-Za-z0-9._~+/-]{20,}=*"  # Authorization: Bearer <token>
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)


def luhn_valid(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _card_ok(match: str) -> bool:
    digits = re.sub(r"[ -]", "", match)
    return 13 <= len(digits) <= 19 and luhn_valid(digits)


_DETECTORS: dict[str, tuple[re.Pattern[str], Callable[[str], bool] | None]] = {
    "secret": (_SECRET, None),
    "email": (_EMAIL, None),
    "card": (_CARD, _card_ok),
    "ssn": (_SSN, None),
    "phone": (_PHONE, None),
}


def placeholder(kind: str) -> str:
    return f"[REDACTED:{kind.upper()}]"


def validate_kinds(kinds: Iterable[str]) -> tuple[str, ...]:
    out = tuple(kinds)
    unknown = [k for k in out if k not in KINDS]
    if unknown:
        raise ValueError(f"unknown PII kinds {unknown}; known: {list(KINDS)}")
    return out


def find(text: str, kinds: Sequence[str] = KINDS) -> dict[str, int]:
    """Count matches per kind without changing anything."""
    return redact(text, kinds)[1]


def redact(text: str, kinds: Sequence[str] = KINDS) -> tuple[str, dict[str, int]]:
    """Replace each match with [REDACTED:KIND]. Returns (new text, {kind: count}); matched values are never returned."""
    counts: dict[str, int] = {}
    for kind in KINDS:  # fixed order: secrets and cards before phones, so a card isn't half-matched as a phone
        if kind not in kinds:
            continue
        pattern, check = _DETECTORS[kind]
        n = 0

        def sub(m: re.Match[str], kind: str = kind, check=check) -> str:
            nonlocal n
            if check is not None and not check(m.group(0)):
                return m.group(0)
            n += 1
            return placeholder(kind)

        text = pattern.sub(sub, text)
        if n:
            counts[kind] = n
    return text, counts


# ---------- hooks ----------


@dataclass
class HookContext:
    stage: str  # "pre_request" | "post_response"
    team: str = ""
    alias: str = ""
    kinds: tuple[str, ...] = KINDS


@dataclass
class HookOutcome:
    texts: list[str]
    findings: dict[str, int] = field(default_factory=dict)  # kind -> count; never the matched values
    blocked: bool = False
    reason: str = ""
    action: str = "none"  # what the hook did: none | redact | block | detect


Hook = Callable[[list[str], HookContext], HookOutcome]
_HOOKS: dict[str, Hook] = {}


def register_hook(name: str, hook: Hook) -> None:
    """Make a hook available to `privacy:` config under `name` (call at import time of a ROUTER_HOOK_MODULES module)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        raise ValueError(f"hook name {name!r} must be lowercase letters, digits and underscores")
    _HOOKS[name] = hook


def registered_hooks() -> list[str]:
    return sorted(_HOOKS)


def get_hook(name: str) -> Hook:
    try:
        return _HOOKS[name]
    except KeyError:
        raise KeyError(f"no hook registered as {name!r}") from None


def _merge(into: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        into[k] = into.get(k, 0) + v


def _pii_redact(texts: list[str], ctx: HookContext) -> HookOutcome:
    found: dict[str, int] = {}
    out = []
    for t in texts:
        new, counts = redact(t, ctx.kinds)
        out.append(new)
        _merge(found, counts)
    return HookOutcome(out, found, action="redact" if found else "none")


def _pii_block(texts: list[str], ctx: HookContext) -> HookOutcome:
    found: dict[str, int] = {}
    for t in texts:
        _merge(found, find(t, ctx.kinds))
    if found:
        kinds = ", ".join(sorted(found))
        where = "request" if ctx.stage == "pre_request" else "response"
        return HookOutcome(list(texts), found, True, f"{where} contains {kinds}", action="block")
    return HookOutcome(list(texts))


def _pii_detect(texts: list[str], ctx: HookContext) -> HookOutcome:
    found: dict[str, int] = {}
    for t in texts:
        _merge(found, find(t, ctx.kinds))
    return HookOutcome(list(texts), found, action="detect" if found else "none")


register_hook("pii_redact", _pii_redact)
register_hook("pii_block", _pii_block)
register_hook("pii_detect", _pii_detect)


@dataclass
class HookRun:
    """Result of running a list of hooks in order: final texts plus one record per hook that acted."""

    texts: list[str]
    actions: list[dict] = field(default_factory=list)  # {"hook", "action", "findings"}
    blocked_by: str = ""
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    def findings(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for a in self.actions:
            _merge(total, a["findings"])
        return total


class HookError(Exception):
    """A hook raised or returned something invalid. The gateway fails closed on this."""


def run_hooks(names: Sequence[str], texts: list[str], ctx: HookContext) -> HookRun:
    run = HookRun(list(texts))
    for name in names:
        try:
            outcome = get_hook(name)(list(run.texts), ctx)
        except Exception as e:
            raise HookError(f"hook {name!r} failed: {type(e).__name__}") from e
        if not isinstance(outcome, HookOutcome) or len(outcome.texts) != len(run.texts):
            raise HookError(f"hook {name!r} returned an invalid result")
        run.texts = list(outcome.texts)
        if outcome.action != "none" or outcome.blocked:
            run.actions.append({"hook": name, "action": outcome.action, "findings": dict(outcome.findings)})
        if outcome.blocked:
            run.blocked_by, run.reason = name, outcome.reason or "blocked by policy hook"
            break
    return run


def resolve_hooks(*layers: Iterable[str]) -> list[str]:
    """Union of hook lists from several config layers (default, route, team, policy), first occurrence wins order."""
    out: list[str] = []
    for layer in layers:
        for name in layer:
            if name not in out:
                out.append(name)
    return out
