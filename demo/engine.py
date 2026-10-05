# Corey Mathie, 2026
"""
Browser demo engine: the gateway's real decision code, driven by simulated providers.

What is real: the circuit breakers (router/breaker.py), the fallback chain and
latency ordering (router/fallback.py, router/latency.py), token estimation and
TPM limits (router/tokens.py), the org -> team -> key budget check
(router/budgets.py), anomaly thresholds (router/anomaly.py), per-call cost
(router/costs.py, price overrides), the showback roll-up (router/showback.py),
the PII detectors and content hooks (router/pii.py), the policy-as-code
evaluator (router/policy.py, with the same rules as config/policies.yaml) and
the semantic cache with its calibration (router/semcache.py), and the MCP
tool-gateway policy: allow-lists, velocity limits, caps and argument
redaction (router/mcp_policy.py, with the same config as config/mcp.example.yaml).
The MCP servers behind it are simulated; the gateway's HTTP proxy
(router/mcp_gateway.py) is not loaded in the browser.
In the browser these files are fetched from this repository and run in Pyodide.

What is simulated: the providers (latency, errors, outages, 429s and prices are
knobs in the UI), the request mix, the 7-day spend history behind each key's
anomaly baseline, and time itself (a virtual clock, so a "30 s cooldown" doesn't
make you wait). Nothing here calls a network or needs an API key.

The request path mirrors router/routing.py: budgets -> anomaly pause -> TPM
reservation -> fallback chain -> settle tokens -> record cost. The governance
panel (`prompt()`) adds the governance stages: policy decision -> pre_request
hooks (plus any the policy requires) -> provider (only deployments the policy
permits) -> post_response hooks -> opt-in redacted content log, with an audit
entry for every decision and every hook that acted. The optional OPA check is
not part of the demo. The audit list here lives in memory; the gateway writes
the same entries to a hash-chained SQLite table (router/audit.py).
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass

from router import anomaly, costs, mcp_policy, pii, policy, semcache, showback
from router.breaker import BreakerConfig, BreakerRegistry
from router.budgets import Hierarchy, Limits
from router.budgets import check as check_budgets
from router.fallback import ChainExhausted, run_chain
from router.latency import LatencyConfig, LatencyTracker
from router.tokens import TokenLimitExceeded, TokenRateLimiter, estimate_request_tokens, estimator_name

ALIAS = "smart-fast"
REAL_MODULES = [
    "breaker", "fallback", "latency", "tokens", "budgets", "anomaly", "costs", "showback", "pii", "policy", "semcache",
    "mcp_policy",
]  # fmt: skip
# Same document as config/mcp.example.yaml (tests/test_demo_engine.py fails if they drift).
DEMO_MCP = {
    "version": 1,
    "servers": {
        "tickets": {
            "url": "http://127.0.0.1:9101/mcp", "timeout_s": 10,
            "headers_from_env": {"Authorization": "MCP_TICKETS_AUTH"},
        },
        "files": {"url": "http://127.0.0.1:9102/mcp", "timeout_s": 5},
    },
    "defaults": {
        "per_minute": 60, "team_per_minute": 0, "max_identical_per_minute": 5, "per_day": 0, "cost_usd": 0.0,
        "daily_usd": 0.0, "redact_args": True, "forward_redacted": False,
    },
    "teams": {
        "support": [
            {"server": "tickets", "tool": "search_*"},
            {"server": "tickets", "tool": "get_ticket"},
            {"server": "tickets", "tool": "close_ticket", "per_minute": 5, "per_day": 200},
        ],
        "data": [{"server": "files", "tool": "read_file", "per_minute": 30, "cost_usd": 0.001, "daily_usd": 2.0}],
    },
}  # fmt: skip
# What the simulated MCP servers expose (tools/list before the gateway filters it).
DEMO_MCP_TOOLS = {
    "tickets": ["search_tickets", "get_ticket", "close_ticket", "reassign_ticket"],
    "files": ["read_file", "delete_file"],
}
DEMO_MCP_ARGS = {
    "search_tickets": {"query": "refund requested by jordan@example.com"},
    "get_ticket": {"id": "T-1042"},
    "close_ticket": {"id": "T-1042", "note": "refunded"},
    "reassign_ticket": {"id": "T-1042", "to": "tier-2"},
    "read_file": {"path": "reports/q3-summary.txt"},
    "delete_file": {"path": "reports/q3-summary.txt"},
}
SEMANTIC_EXAMPLES = [
    "How do I export my invoices as CSV?",
    "how do I export my invoices as csv",
    "Is the 2025 model compatible with the charger?",
    "Is the 2023 model compatible with the charger?",
    "How do I rotate an API key in the dashboard?",
    "How do I revoke an API key in the dashboard?",
]
# Same document as config/policies.yaml (tests/test_demo_engine.py fails if they drift); the browser has no YAML parser.
DEMO_POLICIES = {
    "version": 1,
    "defaults": {
        "max_tokens_ceiling": 4096,
        "max_tokens_mode": "clamp",
        "max_request_bytes": 262144,
        "max_messages": 200,
    },
    "teams": {
        "regulated": {"allowed_providers": ["ollama"], "required_hooks": ["pii_redact"], "max_tokens_ceiling": 2048},
        "contractors": {
            "allow_aliases": ["smart-fast", "cheap-batch", "local-first"],
            "deny_models": ["anthropic/claude-sonnet-*", "openai/gpt-4.1"],
        },
    },
    "routes": {"heavy-reasoning": {"max_tokens_mode": "reject"}},
    "opa": {"enabled": False, "path": "router/decision", "timeout_s": 0.5},
}
DEMO_ROUTES = {"smart-fast": "anthropic -> openai -> ollama", "public-only": "openai only"}
SAMPLE_PROMPT = (
    "Summarize this ticket: customer Jordan Example (jordan@example.com, 202-555-0143) says card "
    "4111 1111 1111 1111 was double charged. Internal note: retry with key sk-test-abcdefghijklmnopqrstuvwx."
)  # fictional: reserved example domain, 555-01xx number, a published test card, a fake key
HOOK_MODES = {"off": [], "detect": ["pii_detect"], "redact": ["pii_redact"], "block": ["pii_block"]}


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class SimulatedProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class Deployment:
    provider: str
    model: str
    label: str
    latency_ms: float
    error_rate: float = 0.0
    outage: bool = False
    rate_limited: bool = False
    price_in_1k: float = 0.0  # simulated USD per 1K input tokens
    price_out_1k: float = 0.0
    timeout_s: float = 3.0
    served: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class DemoKey:
    label: str
    team: str
    weight: float  # share of normal traffic
    baseline_per_hour: float  # synthetic 7-day average, USD per active hour
    active_hours_per_day: int
    prompt_tokens: int
    max_tokens: int

    @property
    def key_id(self) -> str:
        return f"demo-{self.label}"  # not a secret; the demo has no real keys


def default_deployments() -> list[Deployment]:
    return [
        Deployment("anthropic", "claude-haiku-4-5", "Anthropic", 650, 0.02, price_in_1k=0.0010, price_out_1k=0.0050),
        Deployment("openai", "gpt-4.1-mini", "OpenAI", 480, 0.02, price_in_1k=0.0004, price_out_1k=0.0016),
        Deployment("ollama", "llama3.1:8b", "Ollama (local)", 1300, 0.0, price_in_1k=0.0001, price_out_1k=0.0001),
    ]


def default_keys() -> list[DemoKey]:
    return [
        DemoKey("web-app", "product", 0.40, 0.030, 14, 700, 400),
        DemoKey("mobile-app", "product", 0.25, 0.020, 16, 500, 300),
        DemoKey("support-bot", "support", 0.30, 0.025, 24, 900, 350),
        DemoKey("nightly-batch", "data", 0.05, 0.004, 6, 1200, 500),
    ]


class Engine:
    def __init__(self, seed: int = 7) -> None:
        self.seed = seed
        self.reset()

    # ---------- setup ----------

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.clock = VirtualClock()
        self.deployments = default_deployments()
        self.keys = default_keys()
        self.strategy = "ordered"
        self.auto_pause = True
        self.breaker_config = BreakerConfig(
            failure_threshold=3, error_rate_threshold=0.5, min_requests=8, window_seconds=30, cooldown_seconds=10
        )
        self.breakers = BreakerRegistry(self.breaker_config, clock=self.clock)
        self.latency = LatencyTracker(LatencyConfig(alpha=0.3, min_samples=3, tolerance=0.10))
        self.tpm = TokenRateLimiter(clock=self.clock)
        self.org = Limits(daily_usd=0.40)
        self.team_limits = {
            "product": Limits(daily_usd=0.15, tpm=40_000),
            "support": Limits(daily_usd=0.12, tpm=25_000),
            "data": Limits(daily_usd=0.15, tpm=30_000),
        }
        self.key_limits = {"nightly-batch": Limits(daily_usd=0.10)}
        self.key_default = Limits(daily_usd=0.08)
        self.ledger: list[dict] = []
        self.events: list[dict] = []
        self.counter = 0
        # Governance panel state (router/privacy.py config, simplified to one setting per team).
        self.content = {
            "product": {"mode": "off", "log_content": False},
            "support": {"mode": "redact", "log_content": True},
            "data": {"mode": "block", "log_content": False},
            "regulated": {"mode": "off", "log_content": False},
            "contractors": {"mode": "off", "log_content": False},
        }
        self.policies = policy.load(DEMO_POLICIES, pii.registered_hooks())
        self.embedder = semcache.HashingEmbedder()
        self.semantic = semcache.SemanticCache(threshold=0.88, ttl_seconds=3600, clock=self.clock)
        self.semantic_log: list[dict] = []
        self.mcp = mcp_policy.load(DEMO_MCP)
        self.mcp_limiter = mcp_policy.VelocityLimiter(clock=self.clock)
        self.mcp_log: list[dict] = []
        self.audit: list[dict] = []
        self.content_log: list[dict] = []
        self.prompts: list[dict] = []
        self.baselines = {k.label: self._synthetic_history(k) for k in self.keys}
        self._apply_prices()

    def _synthetic_history(self, k: DemoKey) -> list[float]:
        """Simulated: one spend total per active hour over the previous 7 days."""
        rng = random.Random(f"{self.seed}-{k.label}")
        return [
            max(0.0, rng.gauss(k.baseline_per_hour, k.baseline_per_hour * 0.25))
            for _ in range(7 * k.active_hours_per_day)
        ]

    def _apply_prices(self) -> None:
        costs.set_price_overrides({d.id: (d.price_in_1k * 1000, d.price_out_1k * 1000) for d in self.deployments})

    def hierarchy(self) -> Hierarchy:
        return Hierarchy(
            org=self.org,
            team_default=Limits(daily_usd=0.0),
            key_default=self.key_default,
            teams=dict(self.team_limits),
            keys=dict(self.key_limits),
        )

    # ---------- controls ----------

    def deployment(self, dep_id: str) -> Deployment:
        for d in self.deployments:
            if d.id == dep_id:
                return d
        raise KeyError(dep_id)

    def update_deployment(self, dep_id: str, changes: dict) -> None:
        d = self.deployment(dep_id)
        for field in ("latency_ms", "error_rate", "price_in_1k", "price_out_1k", "timeout_s"):
            if field in changes:
                setattr(d, field, max(0.0, float(changes[field])))
        for field in ("outage", "rate_limited"):
            if field in changes:
                setattr(d, field, bool(changes[field]))
        d.error_rate = min(d.error_rate, 1.0)
        self._apply_prices()

    def set_strategy(self, strategy: str) -> None:
        if strategy not in ("ordered", "latency"):
            raise ValueError(strategy)
        self.strategy = strategy

    def set_breakers_enabled(self, enabled: bool) -> None:
        self.breaker_config.enabled = bool(enabled)

    def set_auto_pause(self, enabled: bool) -> None:
        self.auto_pause = bool(enabled)

    def set_team_cap(self, team: str, daily_usd: float) -> None:
        cur = self.team_limits.get(team, Limits())
        self.team_limits[team] = Limits(max(0.0, float(daily_usd)), cur.monthly_usd, cur.tpm)

    def set_team_tpm(self, team: str, tpm: int) -> None:
        cur = self.team_limits.get(team, Limits())
        self.team_limits[team] = Limits(cur.daily_usd, cur.monthly_usd, max(0, int(tpm)))

    def set_org_cap(self, daily_usd: float) -> None:
        self.org = Limits(max(0.0, float(daily_usd)))

    def advance(self, seconds: float) -> None:
        self.clock.advance(float(seconds))

    # ---------- ledger queries (what store.py does in SQL) ----------

    def _spend(self, scope: str, ident: str | None, period: str) -> float:
        # The whole simulation happens on one UTC day, so daily == monthly-to-date here.
        rows = self.ledger
        if scope == "team":
            rows = [r for r in rows if r["team"] == ident]
        elif scope == "key":
            rows = [r for r in rows if r["key_id"] == ident]
        return sum(r["cost_usd"] for r in rows)

    def _hour_spend(self, k: DemoKey) -> float:
        hour = int(self.clock() // 3600)
        return sum(
            r["cost_usd"] for r in self.ledger if r["key_id"] == k.key_id and not r["error"] and r["hour"] == hour
        )

    def signal(self, k: DemoKey) -> anomaly.SpendSignal:
        baseline, hours = anomaly.baseline_from_hourly(self.baselines[k.label])
        return anomaly.classify(k.label, self._hour_spend(k), baseline, hours)

    def _record(self, k: DemoKey, d: Deployment, pt: int, ct: int, cost: float, error: str | None) -> None:
        self.ledger.append(
            {
                "key_id": k.key_id,
                "key_label": k.label,
                "key_fp": showback.fingerprint(k.key_id),
                "team": k.team,
                "alias": ALIAS,
                "provider": d.provider,
                "model": d.model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cost_usd": cost,
                "error": error,
                "cached": 0,
                "saved_usd": 0.0,
                "hour": int(self.clock() // 3600),
            }
        )

    # ---------- the request path ----------

    def pick_key(self) -> DemoKey:
        return self.rng.choices(self.keys, weights=[k.weight for k in self.keys])[0]

    def key(self, label: str) -> DemoKey:
        for k in self.keys:
            if k.label == label:
                return k
        raise KeyError(label)

    async def _call(self, target: Deployment) -> tuple[int, int]:
        d = target
        if d.outage:
            self.clock.advance(d.timeout_s)
            d.failed += 1
            raise SimulatedProviderError(f"timed out after {d.timeout_s:g}s")
        if d.rate_limited:
            self.clock.advance(0.05)
            d.failed += 1
            raise SimulatedProviderError("429 rate limited", status_code=429, retry_after=8)
        jitter = self.rng.uniform(0.8, 1.3)
        if self.rng.random() < d.error_rate:
            self.clock.advance(d.latency_ms / 1000 * 0.4 * jitter)
            d.failed += 1
            raise SimulatedProviderError("503 upstream error", status_code=503)
        self.clock.advance(d.latency_ms / 1000 * jitter)
        d.served += 1
        k = self._current_key
        pt = int(k.prompt_tokens * self.rng.uniform(0.9, 1.1))
        ct = int(k.max_tokens * self.rng.uniform(0.4, 0.9))
        return pt, ct

    def _messages(self, k: DemoKey) -> list[dict]:
        # Synthetic prompt sized to the key's typical request (about 4 characters per token).
        return [{"role": "user", "content": "lorem ipsum " * (k.prompt_tokens * 4 // 12)}]

    async def send(self, key_label: str | None = None, gap_s: float = 0.25) -> dict:
        self.clock.advance(gap_s)
        k = self.key(key_label) if key_label else self.pick_key()
        self._current_key = k
        self.counter += 1
        event = {"n": self.counter, "t": round(self.clock(), 2), "key": k.label, "team": k.team}
        started = self.clock()

        # 1. budgets: org -> team -> key
        breach = check_budgets(self.hierarchy(), k.team, k.label, k.key_id, self._spend)
        if breach:
            return self._reject(event, 402, breach.message, breach.reason)

        # 2. anomaly auto-pause
        sig = self.signal(k)
        if self.auto_pause and sig.verdict == "pause":
            return self._reject(event, 429, f"key paused: {sig.multiple:.1f}x its 7-day baseline", "anomaly_pause")

        # 3. TPM reservation (estimate now, settle later)
        estimate = estimate_request_tokens(self._messages(k), k.max_tokens, 256)
        limits = {f"team:{k.team}": self.hierarchy().team(k.team).tpm or 0}
        try:
            reservation = self.tpm.reserve(limits, estimate)
        except TokenLimitExceeded as e:
            return self._reject(event, 429, f"{e} (retry in {e.retry_after:.0f}s)", "tpm_team")

        # 4. fallback chain with breakers and (optionally) latency ordering
        def on_attempt(target: Deployment, a) -> None:
            if a.outcome == "skipped":
                target.skipped += 1
            elif a.outcome == "error":
                self._record(k, target, 0, 0, 0.0, a.error)

        try:
            result = await run_chain(
                self.deployments,
                self._call,
                strategy=self.strategy,
                breakers=self.breakers,
                latency=self.latency,
                clock=self.clock,
                on_attempt=on_attempt,
            )
        except ChainExhausted as e:
            reservation.release()
            event["path"] = [a.as_dict() for a in e.attempts]
            event["latency_ms"] = round((self.clock() - started) * 1000)
            if e.all_skipped:
                return self._reject(event, 503, "all circuits open", "circuit_open", keep_path=True)
            return self._reject(event, 502, "all providers failed", "all_failed", keep_path=True)

        # 5. settle tokens, price the call, record it
        pt, ct = result.value
        d = result.target
        reservation.settle(pt + ct)
        cost = costs.dollars_for(d.provider, d.model, pt, ct)
        self._record(k, d, pt, ct, cost, None)
        event.update(
            outcome="ok",
            status=200,
            served_by=d.id,
            fell_back=result.fell_back,
            path=[a.as_dict() for a in result.attempts],
            latency_ms=round((self.clock() - started) * 1000),
            estimated_tokens=estimate,
            tokens=pt + ct,
            cost_usd=cost,
            anomaly=self.signal(k).verdict,
        )
        return self._log(event)

    def _reject(self, event: dict, status: int, message: str, reason: str, keep_path: bool = False) -> dict:
        event.update(outcome="rejected", status=status, reason=reason, message=message)
        if not keep_path:
            event["path"] = []
            event["latency_ms"] = 0
        return self._log(event)

    def _log(self, event: dict) -> dict:
        self.events.append(event)
        del self.events[:-200]
        return event

    # ---------- governance: content hooks, content log, audit ----------

    def set_content_policy(self, team: str, mode: str, log_content: bool) -> None:
        if mode not in HOOK_MODES:
            raise ValueError(mode)
        self.content[team] = {"mode": mode, "log_content": bool(log_content)}

    def _audit(self, category: str, action: str, team: str, detail: dict) -> None:
        self.audit.append({"t": round(self.clock(), 2), "category": category, "action": action, "team": team, **detail})
        del self.audit[:-100]

    def _hooks(self, stage: str, team: str, texts: list[str], required: list[str] | None = None) -> pii.HookRun:
        names = pii.resolve_hooks(HOOK_MODES[self.content.get(team, {"mode": "off"})["mode"]], required or [])
        run = pii.run_hooks(names, texts, pii.HookContext(stage, team, ALIAS))
        for a in run.actions:
            self._audit("privacy", a["action"], team, {"stage": stage, "hook": a["hook"], "findings": a["findings"]})
        return run

    def route_targets(self, alias: str) -> list[Deployment]:
        if alias == "smart-fast":
            return list(self.deployments)
        if alias == "public-only":
            return [self.deployment("openai/gpt-4.1-mini")]
        raise ValueError(f"unknown demo alias {alias!r}")

    async def prompt(self, team: str, text: str, alias: str = "smart-fast", max_tokens: int | None = 512) -> dict:
        """One prompt through the governed path, with a trace of every stage."""
        self.clock.advance(0.5)
        k = DemoKey(f"{team}-console", team, 0.0, 0.01, 8, 0, 300)
        self._current_key = k
        out: dict = {"team": team, "alias": alias, "trace": [], "sent": None, "response": None}
        trace = out["trace"]

        # 0. policy-as-code (router/policy.py): before budgets, cache and any provider call
        msgs = [{"role": "user", "content": text}]
        facts = policy.RequestFacts(
            team=team, alias=alias, targets=self.route_targets(alias), max_tokens=max_tokens,
            request_bytes=len(json.dumps(msgs, ensure_ascii=False).encode()), messages=len(msgs),
            key_label=k.label,
        )  # fmt: skip
        dec = policy.evaluate(self.policies, facts)
        detail = {
            "rules": dec.rules,
            "removed": [r["deployment"] for r in dec.removed],
            "reason": "; ".join(dec.reasons),
        }
        self._audit("policy", "allow" if dec.allow else "deny", team, detail)
        trace.append(
            {
                "stage": "policy",
                "outcome": "allow" if dec.allow else f"deny {dec.status}",
                "rules": dec.rules,
                "removed": dec.removed,
                "max_tokens": dec.max_tokens,
                "required_hooks": dec.required_hooks,
            }
        )
        out["policy"] = dec.as_dict()
        if not dec.allow:
            out.update(status=dec.status, message="policy: " + "; ".join(dec.reasons))
            return self._prompt_done(out)

        pre = self._hooks("pre_request", team, [text], dec.required_hooks)
        mode = self.content.get(team, {}).get("mode", "off")
        pre_label = f"{mode} + required {','.join(dec.required_hooks)}" if dec.required_hooks else mode
        trace.append({"stage": "pre_request hooks", "outcome": pre_label, "findings": pre.findings()})
        if pre.blocked:
            out.update(status=422, message=f"blocked by content policy ({pre.blocked_by}): {pre.reason}")
            return self._prompt_done(out)
        sent = pre.texts[0]
        out["sent"] = sent

        try:
            result = await run_chain(
                dec.targets, self._call, strategy=self.strategy, breakers=self.breakers,
                latency=self.latency, clock=self.clock,
            )  # fmt: skip
        except ChainExhausted as e:
            out.update(status=502, message="all permitted providers failed", path=[a.as_dict() for a in e.attempts])
            trace.append({"stage": "provider", "outcome": "all permitted deployments failed"})
            return self._prompt_done(out)
        d = result.target
        pt = max(1, len(sent) // 4)  # simulated provider: tokens from the text actually sent
        reply = f"Summary of what I received: {sent[:150]}"
        ct = max(1, len(reply) // 4)
        cost = costs.dollars_for(d.provider, d.model, pt, ct)
        self._record(k, d, pt, ct, cost, None)
        trace.append({"stage": "provider", "outcome": d.id, "cost_usd": cost})

        post = self._hooks("post_response", team, [reply])
        trace.append({"stage": "post_response hooks", "outcome": mode, "findings": post.findings()})
        if post.blocked:
            out.update(status=422, message=f"response withheld by content policy: {post.reason}")
            return self._prompt_done(out)
        out["response"] = post.texts[0]

        if self.content.get(team, {}).get("log_content"):
            req, rc = pii.redact(text)
            resp, sc = pii.redact(out["response"])
            self.content_log.append({"t": round(self.clock(), 2), "team": team, "request": req, "response": resp})
            del self.content_log[:-20]
            self._audit("privacy", "content_logged", team, {"findings": {**rc, **sc}})
            trace.append({"stage": "content log", "outcome": "stored redacted"})
        else:
            trace.append({"stage": "content log", "outcome": "off (metadata only)"})
        out.update(status=200, served_by=d.id, cost_usd=cost)
        return self._prompt_done(out)

    def _prompt_done(self, out: dict) -> dict:
        self.prompts.append(out)
        del self.prompts[:-20]
        return out

    # ---------- semantic cache (router/semcache.py) ----------

    def _semantic_partition(self, team: str) -> str:
        # The gateway also folds in content hooks, earlier turns and generation parameters; here a single turn.
        facts = policy.RequestFacts(team=team, alias=ALIAS, targets=self.deployments)
        return semcache.partition_key(team, ALIAS, policy.evaluate(self.policies, facts).fingerprint())

    async def semantic_ask(self, team: str, text: str, threshold: float = 0.88) -> dict:
        self.clock.advance(0.5)
        self.semantic.threshold = max(0.0, min(1.0, float(threshold)))
        part = self._semantic_partition(team)
        found = self.semantic.lookup(part, text, self.embedder.embed(text))
        out = {
            "team": team,
            "text": text,
            "threshold": self.semantic.threshold,
            "similarity": round(found.similarity, 4),
            "nearest": found.entry.text if found.entry else None,
            "reason": found.reason,
        }
        if found.hit:
            v = found.entry.value
            out.update(result="hit", answer=v["answer"], saved_usd=v["cost_usd"], served_by=v["served_by"])
        else:
            k = DemoKey(f"{team}-console", team, 0.0, 0.01, 8, max(1, len(text) // 4), 120)
            self._current_key = k
            try:
                res = await run_chain(self.deployments, self._call, strategy=self.strategy, breakers=self.breakers,
                                      latency=self.latency, clock=self.clock)  # fmt: skip
            except ChainExhausted:
                out.update(result="error", answer=None)
                return self._semantic_done(out)
            pt, ct = res.value
            d = res.target
            cost = costs.dollars_for(d.provider, d.model, pt, ct)
            self._record(k, d, pt, ct, cost, None)
            answer = f"(simulated answer #{len(self.semantic_log) + 1} from {d.label})"
            self.semantic.put(
                part, text, self.embedder.embed(text), {"answer": answer, "cost_usd": cost, "served_by": d.id}
            )
            out.update(result="miss", answer=answer, cost_usd=cost, served_by=d.id)
        return self._semantic_done(out)

    def _semantic_done(self, out: dict) -> dict:
        self.semantic_log.append(out)
        del self.semantic_log[:-30]
        return out

    def semantic_view(self) -> dict:
        entries = []
        for team in sorted({e["team"] for e in self.semantic_log}):
            part = self.semantic._live(self._semantic_partition(team))
            entries += [{"team": team, "text": e.text, "hits": e.hits} for e in part.values()]
        counts = {"hit": 0, "miss": 0, "guard": 0}
        for e in self.semantic_log:
            counts["guard" if e["reason"].startswith("guard") else ("hit" if e["result"] == "hit" else "miss")] += 1
        return {
            "threshold": self.semantic.threshold,
            "entries": entries,
            "log": self.semantic_log[-12:][::-1],
            "counts": counts,
            "examples": SEMANTIC_EXAMPLES,
        }

    def calibrate(self, pairs_jsonl: str, target: float = 0.01) -> dict:
        """The calibration in scripts/calibrate_semcache.py, on the bundled pairs the page fetched."""
        pairs = semcache.load_pairs(pairs_jsonl.splitlines())
        return semcache.calibration_report(pairs, float(target))

    # ---------- MCP tool gateway (router/mcp_policy.py) ----------

    def mcp_list(self, team: str, server: str) -> dict:
        upstream = [{"name": t} for t in DEMO_MCP_TOOLS.get(server, [])]
        visible = [t["name"] for t in mcp_policy.filter_tools(self.mcp, team, server, upstream)]
        return {"server": server, "team": team, "upstream": [t["name"] for t in upstream], "visible": visible}

    def mcp_call(self, team: str, server: str, tool: str, arguments: dict | None = None) -> dict:
        self.clock.advance(0.2)
        args = DEMO_MCP_ARGS.get(tool, {}) if arguments is None else arguments
        model = f"{server}/{tool}"
        today = [r for r in self.ledger if r["provider"] == "mcp" and r["model"] == model and r["team"] == team]
        used = (len(today), sum(r["cost_usd"] for r in today))
        key_id = f"demo-{team}-agent"
        d = mcp_policy.check_call(self.mcp, self.mcp_limiter, team, key_id, server, tool, args, used)
        lim = d.entry.limits if d.entry else self.mcp.defaults
        shown, found = mcp_policy.redact_args(args) if lim.redact_args else (args, {})
        out = {"t": round(self.clock(), 2), "team": team, "tool": model, "decision": d.action, "reason": d.reason,
               "args": shown, "redactions": found, "retry_after": d.retry_after}  # fmt: skip
        if d.allow:
            self.mcp_limiter.record(d.scopes)
            k = DemoKey(f"{team}-agent", team, 0.0, 0.0, 8, 0, 0)
            self.ledger.append({
                "key_id": k.key_id, "key_label": k.label, "key_fp": showback.fingerprint(k.key_id), "team": team,
                "alias": f"mcp:{server}", "provider": "mcp", "model": model, "prompt_tokens": 0,
                "completion_tokens": 0, "cost_usd": lim.cost_usd, "error": None, "cached": 0, "saved_usd": 0.0,
                "hour": int(self.clock() // 3600),
            })  # fmt: skip
            out["result"] = f"(simulated {server} server) {tool} ok"
            out["cost_usd"] = lim.cost_usd
        self._audit("mcp", d.action, team, {"stage": model, "findings": found, "reason": d.reason})
        self.mcp_log.append(out)
        del self.mcp_log[:-40]
        return out

    def mcp_view(self) -> dict:
        teams = sorted({*self.mcp.teams, "product"})
        return {
            "servers": {s: DEMO_MCP_TOOLS[s] for s in self.mcp.servers},
            "teams": teams,
            "allowed": {t: {s: self.mcp_list(t, s)["visible"] for s in self.mcp.servers} for t in teams},
            "log": self.mcp_log[-15:][::-1],
            "default_args": DEMO_MCP_ARGS,
        }

    def governance(self) -> dict:
        return {
            "content": self.content,
            "audit": self.audit[-40:][::-1],
            "content_log": self.content_log[-10:][::-1],
            "sample_prompt": SAMPLE_PROMPT,
            "routes": DEMO_ROUTES,
            "policies": DEMO_POLICIES,
        }

    # ---------- views ----------

    def snapshot(self) -> dict:
        breakers = {b["deployment"]: b for b in self.breakers.snapshot()}
        deployments = []
        for d in self.deployments:
            br = breakers.get(d.id) or self.breakers.get(d.id).snapshot()
            ewma = self.latency.ewma(d.id)
            deployments.append(
                {
                    **{k: v for k, v in asdict(d).items()},
                    "id": d.id,
                    "breaker": br,
                    "ewma_ms": round(ewma * 1000) if ewma is not None else None,
                    "samples": self.latency.samples(d.id),
                }
            )
        h = self.hierarchy()
        teams = []
        for team in sorted({k.team for k in self.keys}):
            lim = h.team(team)
            teams.append(
                {
                    "team": team,
                    "spent_usd": round(self._spend("team", team, "daily"), 6),
                    "cap_usd": lim.daily_usd or 0.0,
                    "tpm_used": self.tpm.used(f"team:{team}"),
                    "tpm_limit": lim.tpm or 0,
                }
            )
        keys = []
        for k in self.keys:
            s = self.signal(k)
            keys.append(
                {
                    "label": k.label,
                    "team": k.team,
                    "hour_spend_usd": round(s.hour_spend, 6),
                    "baseline_hourly_usd": round(s.baseline_hourly, 6),
                    "history_hours": s.history_hours,
                    "multiple": round(s.multiple, 2),
                    "verdict": s.verdict,
                    "spent_today_usd": round(self._spend("key", k.key_id, "daily"), 6),
                    "cap_usd": h.key(k.label).daily_usd or 0.0,
                }
            )
        ok = [e for e in self.events if e.get("outcome") == "ok"]
        rejected: dict[str, int] = {}
        for e in self.events:
            if e.get("outcome") == "rejected":
                rejected[e["reason"]] = rejected.get(e["reason"], 0) + 1
        return {
            "clock_s": round(self.clock(), 2),
            "strategy": self.strategy,
            "breakers_enabled": self.breaker_config.enabled,
            "auto_pause": self.auto_pause,
            "estimator": estimator_name(),
            "deployments": deployments,
            "org": {"spent_usd": round(self._spend("org", None, "daily"), 6), "cap_usd": self.org.daily_usd or 0.0},
            "teams": teams,
            "keys": keys,
            "events": self.events[-60:][::-1],
            "transitions": self.breakers.recent_transitions(30),
            "showback": showback.aggregate(self.ledger, ("team", "model")),
            "totals": showback.totals(self.ledger),
            "stats": {
                "requests": len(self.events),
                "ok": len(ok),
                "fallbacks": sum(1 for e in ok if e.get("fell_back")),
                "rejected": rejected,
            },
            "budget_warnings": h.warnings(),
        }

    def showback_csv(self, group_by: str = "team,model") -> str:
        dims = showback.parse_group_by(group_by)
        return showback.to_csv(showback.aggregate(self.ledger, dims), dims)


# ---------- synchronous wrappers for JavaScript ----------


def run_sync(coro):
    """Drive a coroutine that never truly suspends (simulated providers don't do I/O)."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("simulated call suspended unexpectedly")


class JsBridge:
    """JSON in, JSON out, so the page never handles Python objects."""

    def __init__(self, seed: int = 7) -> None:
        self.engine = Engine(seed)

    def send(self, key_label: str = "", gap_s: float = 0.25) -> str:
        return json.dumps(run_sync(self.engine.send(key_label or None, gap_s)))

    def snapshot(self) -> str:
        return json.dumps(self.engine.snapshot())

    def update_deployment(self, dep_id: str, changes_json: str) -> None:
        self.engine.update_deployment(dep_id, json.loads(changes_json))

    def prompt(self, team: str, text: str, alias: str = "smart-fast", max_tokens: int = 512) -> str:
        return json.dumps(run_sync(self.engine.prompt(team, text, alias, int(max_tokens) if max_tokens else None)))

    def governance(self) -> str:
        return json.dumps(self.engine.governance())

    def semantic_ask(self, team: str, text: str, threshold: float = 0.88) -> str:
        return json.dumps(run_sync(self.engine.semantic_ask(team, text, threshold)))

    def semantic_view(self) -> str:
        return json.dumps(self.engine.semantic_view())

    def calibrate(self, pairs_jsonl: str, target: float = 0.01) -> str:
        return json.dumps(self.engine.calibrate(pairs_jsonl, target))

    def mcp_call(self, team: str, server: str, tool: str, args_json: str = "") -> str:
        args = json.loads(args_json) if args_json.strip() else None
        return json.dumps(self.engine.mcp_call(team, server, tool, args))

    def mcp_view(self) -> str:
        return json.dumps(self.engine.mcp_view())

    def call(self, method: str, *args) -> None:
        allowed = {
            "set_strategy",
            "set_breakers_enabled",
            "set_auto_pause",
            "set_team_cap",
            "set_team_tpm",
            "set_org_cap",
            "set_content_policy",
            "advance",
            "reset",
        }
        if method not in allowed:
            raise ValueError(method)
        getattr(self.engine, method)(*args)

    def showback(self, group_by: str = "team,model") -> str:
        dims = showback.parse_group_by(group_by)
        return json.dumps({"columns": list(dims), "rows": showback.aggregate(self.engine.ledger, dims)})

    def showback_csv(self, group_by: str = "team,model") -> str:
        return self.engine.showback_csv(group_by)
