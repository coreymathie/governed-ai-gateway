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

The console (`request()`, used by every screen of demo/) follows router/routing.py
stage by stage: auth -> policy -> budgets -> anomaly pause -> pre_request hooks ->
exact cache -> semantic cache -> TPM reservation -> fallback chain -> settle ->
post_response hooks -> content log, and records a trace in the same shape as the
gateway's GET /admin/traces. The console's Policies screen validates YAML with the
gateway's loaders (router/configcheck.py: router/policy.py, router/mcp_policy.py, and
router/models.py with Pyodide's pydantic for routes.yaml).

The older `send()` path (kept for its tests) mirrors router/routing.py's core:
budgets -> anomaly pause -> TPM reservation -> fallback chain -> settle tokens ->
record cost. The governance
panel (`prompt()`) adds the governance stages: policy decision -> pre_request
hooks (plus any the policy requires) -> provider (only deployments the policy
permits) -> post_response hooks -> opt-in redacted content log, with an audit
entry for every decision and every hook that acted. The optional OPA check is
not part of the demo. The audit list here lives in memory; the gateway writes
the same entries to a hash-chained SQLite table (router/audit.py).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass

from router import anomaly, configcheck, costs, mcp_policy, pii, policy, semcache, showback
from router.breaker import BreakerConfig, BreakerRegistry
from router.budgets import Hierarchy, Limits
from router.budgets import check as check_budgets
from router.fallback import ChainExhausted, run_chain
from router.latency import LatencyConfig, LatencyTracker
from router.tokens import TokenLimitExceeded, TokenRateLimiter, estimate_request_tokens, estimator_name
from router.traces import LABELS as STAGE_LABELS

ALIAS = "smart-fast"
REAL_MODULES = [
    "breaker", "fallback", "latency", "tokens", "budgets", "anomaly", "costs", "showback", "pii", "policy", "semcache",
    "mcp_policy", "configcheck", "traces",
]  # fmt: skip
# Same aliases as config/routes.yaml (tests/test_demo_engine.py fails if they drift).
DEMO_ALIASES = {
    "smart-fast": {
        "strategy": "ordered",
        "targets": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini", "ollama/llama3.1:8b"],
    },
    "heavy-reasoning": {"strategy": "ordered", "targets": ["anthropic/claude-sonnet-4-5", "openai/gpt-4.1"]},
    "cheap-batch": {"strategy": "ordered", "targets": ["openai/gpt-4.1-nano", "gemini/gemini-2.5-flash"]},
    "local-first": {"strategy": "ordered", "targets": ["ollama/llama3.1:8b", "anthropic/claude-haiku-4-5"]},
    "fast-chat": {
        "strategy": "latency",
        "targets": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1-mini", "gemini/gemini-2.5-flash"],
    },
}
# Same invented profiles as evals/sim_profiles.json (latency, error rate, USD per 1M tokens), for deployments the
# three original demo providers don't cover. Simulated: not measurements of any real model.
SIM_PROFILES = {
    "anthropic/claude-haiku-4-5": {
        "quality": 0.90,
        "latency_ms": 650,
        "error_rate": 0.02,
        "price_in_per_1m": 1.00,
        "price_out_per_1m": 5.00,
    },
    "anthropic/claude-sonnet-4-5": {
        "quality": 0.96,
        "latency_ms": 1400,
        "error_rate": 0.01,
        "price_in_per_1m": 3.00,
        "price_out_per_1m": 15.00,
    },
    "openai/gpt-4.1": {
        "quality": 0.95,
        "latency_ms": 1200,
        "error_rate": 0.01,
        "price_in_per_1m": 2.00,
        "price_out_per_1m": 8.00,
    },
    "openai/gpt-4.1-mini": {
        "quality": 0.88,
        "latency_ms": 480,
        "error_rate": 0.02,
        "price_in_per_1m": 0.40,
        "price_out_per_1m": 1.60,
    },
    "openai/gpt-4.1-nano": {
        "quality": 0.72,
        "latency_ms": 300,
        "error_rate": 0.02,
        "price_in_per_1m": 0.10,
        "price_out_per_1m": 0.40,
    },
    "gemini/gemini-2.5-flash": {
        "quality": 0.86,
        "latency_ms": 520,
        "error_rate": 0.02,
        "price_in_per_1m": 0.30,
        "price_out_per_1m": 2.50,
    },
    "ollama/llama3.1:8b": {
        "quality": 0.70,
        "latency_ms": 1300,
        "error_rate": 0.00,
        "price_in_per_1m": 0.05,
        "price_out_per_1m": 0.05,
    },
    "ollama/qwen2.5:14b": {
        "quality": 0.78,
        "latency_ms": 2100,
        "error_rate": 0.00,
        "price_in_per_1m": 0.08,
        "price_out_per_1m": 0.08,
    },
}
PROVIDER_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Gemini", "ollama": "Ollama (local)"}
NOW_HOUR = 14  # the simulated time of day the overview chart is drawn at
# Which hours of the day each demo key is active (simulated history for the overview chart).
ACTIVE_HOURS = {
    "online-banking": range(8, 22),
    "mobile-banking": range(7, 23),
    "member-assistant": range(0, 24),
    "fraud-scoring-batch": range(0, 6),
}
KEY_ALIAS = {
    "online-banking": "smart-fast",
    "mobile-banking": "fast-chat",
    "member-assistant": "smart-fast",
    "fraud-scoring-batch": "cheap-batch",
}
SAMPLE_PROMPTS = {
    "digital-banking": [
        "Draft a two-sentence in-app message announcing instant card lock in mobile banking.",
        "Rewrite this error so a member understands it: 'E_TIMEOUT 504 core banking'.",
        "Suggest three names for a round-up savings feature, one line each.",
        "Turn these bullet points into short release notes: faster login, Zelle limits shown, bug fixes.",
    ],
    "member-services": [
        "How does a member download statements as PDF?",
        "A member says their debit card was charged twice this month. What should I check first?",
        "Summarize case C-1042 for the card-services team in three bullets.",
        "How do I reset a member's online banking password?",
    ],
    "risk-analytics": [
        "Summarize each of these 40 card-dispute cases in one line and tag it fraud, merchant error, "
        "duplicate or member error. "
        + " ".join(
            f"Case C-{1000 + i}: {s}. The member called twice and uploaded the statement from online banking."
            for i, s in enumerate(
                [
                    "card charged at a gas station the member never visited",
                    "subscription renewed after the member cancelled it",
                    "same restaurant charge posted twice on the statement",
                    "online purchase never arrived and the merchant stopped replying",
                    "foreign transaction while the card was in the member's wallet",
                    "hotel kept the deposit after checkout",
                    "ATM withdrawal posted but the cash was not dispensed",
                    "member forgot a family member used the card",
                ]
                * 5
            )
        )
    ],
}  # fmt: skip
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
        "member-services": [
            {"server": "tickets", "tool": "search_*"},
            {"server": "tickets", "tool": "get_ticket"},
            {"server": "tickets", "tool": "close_ticket", "per_minute": 5, "per_day": 200},
        ],
        "risk-analytics": [
            {"server": "files", "tool": "read_file", "per_minute": 30, "cost_usd": 0.001, "daily_usd": 2.0},
        ],
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
        DemoKey("online-banking", "digital-banking", 0.40, 0.030, 14, 700, 400),
        DemoKey("mobile-banking", "digital-banking", 0.25, 0.020, 16, 500, 300),
        DemoKey("member-assistant", "member-services", 0.30, 0.025, 24, 900, 350),
        DemoKey("fraud-scoring-batch", "risk-analytics", 0.05, 0.004, 6, 1200, 500),
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
            "digital-banking": Limits(daily_usd=0.15, tpm=40_000),
            "member-services": Limits(daily_usd=0.12, tpm=25_000),
            "risk-analytics": Limits(daily_usd=0.15, tpm=30_000),
        }
        self.key_limits = {"fraud-scoring-batch": Limits(daily_usd=0.10)}
        self.key_default = Limits(daily_usd=0.08)
        self.ledger: list[dict] = []
        self.events: list[dict] = []
        self.counter = 0
        # Governance panel state (router/privacy.py config, simplified to one setting per team).
        self.content = {
            "digital-banking": {"mode": "off", "log_content": False},
            "member-services": {"mode": "redact", "log_content": True},
            "risk-analytics": {"mode": "block", "log_content": False},
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
        self._reset_console()
        self._apply_prices()

    def _synthetic_history(self, k: DemoKey) -> list[float]:
        """Simulated: one spend total per active hour over the previous 7 days."""
        rng = random.Random(f"{self.seed}-{k.label}")
        return [
            max(0.0, rng.gauss(k.baseline_per_hour, k.baseline_per_hour * 0.25))
            for _ in range(7 * k.active_hours_per_day)
        ]

    def _apply_prices(self) -> None:
        deps = list(getattr(self, "pool", {}).values()) or self.deployments
        costs.set_price_overrides({d.id: (d.price_in_1k * 1000, d.price_out_1k * 1000) for d in deps})

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
        if dep_id in getattr(self, "pool", {}):
            return self.pool[dep_id]
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

    def _record(
        self, k: DemoKey, d: Deployment, pt: int, ct: int, cost: float, error: str | None, alias: str = ALIAS,
        cached: bool = False, saved: float = 0.0,
    ) -> None:  # fmt: skip
        self.ledger.append(
            {
                "key_id": k.key_id,
                "key_label": k.label,
                "key_fp": showback.fingerprint(k.key_id),
                "team": k.team,
                "alias": alias,
                "provider": d.provider,
                "model": d.model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cost_usd": cost,
                "error": error,
                "cached": int(cached),
                "saved_usd": saved,
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
        teams = sorted({*self.mcp.teams, "digital-banking"})
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

    # ---------- console (demo/): state ----------

    def _reset_console(self) -> None:
        self.pool: dict[str, Deployment] = {d.id: d for d in self.deployments}
        self.routes: dict[str, dict] = {}
        self._set_routes(DEMO_ALIASES)
        self.traces: list[dict] = []
        self.trace_seq = 0
        self.revoked: set[str] = set()
        self.holds: dict[str, str] = {}  # key label -> "paused" | "override"
        self.cache_on = True  # exact cache for temperature-0 requests (cache_ttl_seconds > 0 in routes.yaml)
        self.exact_cache: dict[str, dict] = {}
        self.semantic_teams: set[str] = set()  # semantic cache off by default, as in the gateway
        self.config_text: dict[str, str] = {}
        self.history = self._history_today()

    def _deployment_for(self, dep_id: str) -> Deployment:
        if dep_id not in self.pool:
            provider, _, model = dep_id.partition("/")
            prof = SIM_PROFILES.get(dep_id, {"latency_ms": 800, "error_rate": 0.01, "price_in_per_1m": 1.0,
                                             "price_out_per_1m": 4.0})  # fmt: skip
            self.pool[dep_id] = Deployment(
                provider, model, PROVIDER_LABELS.get(provider, provider), float(prof["latency_ms"]),
                float(prof["error_rate"]), price_in_1k=prof["price_in_per_1m"] / 1000,
                price_out_1k=prof["price_out_per_1m"] / 1000,
            )  # fmt: skip
        return self.pool[dep_id]

    def _set_routes(self, aliases: dict[str, dict]) -> None:
        self.routes = {a: {"strategy": r["strategy"], "targets": list(r["targets"])} for a, r in aliases.items()}
        for r in self.routes.values():
            for dep_id in r["targets"]:
                self._deployment_for(dep_id)
        self._apply_prices()

    def _history_today(self) -> list[float]:
        """Simulated spend for each hour of today before NOW_HOUR, around each key's 7-day baseline."""
        rng = random.Random(f"{self.seed}-today")
        out = [0.0] * 24
        for k in self.keys:
            for h in ACTIVE_HOURS.get(k.label, range(0)):
                if h < NOW_HOUR:
                    out[h] += max(0.0, rng.gauss(k.baseline_per_hour, k.baseline_per_hour * 0.25))
        return [round(v, 6) for v in out]

    def _baseline_by_hour(self) -> list[float]:
        out = [0.0] * 24
        for k in self.keys:
            for h in ACTIVE_HOURS.get(k.label, range(0)):
                out[h] += k.baseline_per_hour
        return [round(v, 6) for v in out]

    # ---------- console: the governed request path (mirrors router/routing.py route()) ----------

    def _new_trace(self, k: DemoKey, alias: str, source: str) -> dict:
        self.trace_seq += 1
        return {
            "id": f"tr_demo{self.trace_seq:05d}", "ts": f"{NOW_HOUR:02d}:{int(self.clock() // 60) % 60:02d}:"
            f"{int(self.clock()) % 60:02d}", "t": round(self.clock(), 2), "key": k.label,
            "key_fp": showback.fingerprint(k.key_id), "team": k.team, "alias": alias, "stream": False,
            "status": None, "outcome": "pending", "reason": "", "served_by": None, "fell_back": False, "attempts": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "saved_usd": 0.0, "cache": "miss",
            "latency_ms": 0.0, "simulated": True, "source": source, "stages": [],
        }  # fmt: skip

    def _stage(self, tr: dict, name: str, decision: str, summary: str, ms: float | None = None, **detail) -> None:
        tr["stages"].append({"stage": name, "label": STAGE_LABELS.get(name, name), "decision": decision,
                             "summary": summary, "ms": None if ms is None else round(ms, 2),
                             "detail": {k: v for k, v in detail.items() if v is not None}})  # fmt: skip

    def _finish(self, tr: dict, started: float, status: int, reason: str = "") -> dict:
        tr["status"], tr["reason"] = status, reason
        if status < 400:
            tr["outcome"] = "cache_hit" if tr["cache"] in ("hit", "semantic-hit") else "ok"
        else:
            tr["outcome"] = "error" if status >= 500 else "rejected"
        tr["latency_ms"] = round((self.clock() - started) * 1000, 1)
        self.traces.append(tr)
        del self.traces[:-500]
        return tr

    def _deny(self, tr: dict, started: float, stage: str, status: int, reason: str, **detail) -> dict:
        self._stage(tr, stage, "error" if status >= 500 else "deny", reason, **detail)
        return self._finish(tr, started, status, reason)

    def find_key(self, label: str) -> DemoKey | None:
        return next((k for k in self.keys if k.label == label), None)

    async def _simulate(self, d: Deployment, prompt_tokens: int, max_tokens: int | None) -> tuple[int, int]:
        """A simulated provider call: same failure modes as _call, tokens from the request text."""
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
        ct = int(self.rng.uniform(60, 260))
        return prompt_tokens, min(ct, max_tokens or ct)

    def _cache_key(self, team: str, alias: str, text: str, max_tokens: int | None, fp: str) -> str:
        return hashlib.sha256(json.dumps([team, alias, text, max_tokens, fp]).encode()).hexdigest()

    async def request(
        self, key_label: str, alias: str, text: str, max_tokens: int | None = None, temperature: float = 0.7,
        gap_s: float = 0.5, source: str = "playground",
    ) -> dict:  # fmt: skip
        """One chat request through every gateway stage, with a trace like GET /admin/traces/{id}."""
        self.clock.advance(gap_s)
        started = self.clock()
        k = self.find_key(key_label)
        if k is None or k.label in self.revoked:
            k = k or DemoKey(key_label or "unknown", "-", 0.0, 0.0, 1, 0, 0)
            tr = self._new_trace(k, alias, source)
            return self._deny(tr, started, "auth", 401, "invalid or revoked key")
        tr = self._new_trace(k, alias, source)
        self._stage(tr, "auth", "pass", f"key {k.label} ({tr['key_fp']}) · team {k.team}")

        # policy (router/admission.py -> router/policy.py)
        if alias not in self.routes:
            return self._deny(tr, started, "policy", 400, f"unknown model alias {alias!r}; available: "
                              f"{sorted(self.routes)}")  # fmt: skip
        route = self.routes[alias]
        targets = [self.pool[t] for t in route["targets"]]
        msgs = [{"role": "user", "content": text}]
        facts = policy.RequestFacts(
            team=k.team, alias=alias, targets=targets, max_tokens=max_tokens,
            request_bytes=len(json.dumps(msgs, ensure_ascii=False).encode()), messages=1, key_label=k.label,
        )  # fmt: skip
        dec = policy.evaluate(self.policies, facts)
        self._audit("policy", "allow" if dec.allow else "deny", k.team,
                    {"rules": dec.rules, "removed": [r["deployment"] for r in dec.removed],
                     "reason": "; ".join(dec.reasons), "stage": alias})  # fmt: skip
        if not dec.allow:
            return self._deny(tr, started, "policy", dec.status, "policy: " + "; ".join(dec.reasons),
                              rules=dec.rules, removed=dec.removed or None)  # fmt: skip
        summary = f"allow ({', '.join(dec.rules)})"
        if dec.removed:
            summary += f"; removed {', '.join(r['deployment'] for r in dec.removed)}"
        if dec.max_tokens_clamped:
            summary += f"; max_tokens {dec.max_tokens}"
        self._stage(tr, "policy", "pass", summary, rules=dec.rules, removed=dec.removed or None,
                    targets=[d.id for d in dec.targets], max_tokens=dec.max_tokens,
                    required_hooks=dec.required_hooks or None, source="local")  # fmt: skip

        # budgets: org -> team -> key
        h = self.hierarchy()
        breach = check_budgets(h, k.team, k.label, k.key_id, self._spend)
        if breach:
            return self._deny(tr, started, "budgets", 402, breach.message)
        cap = h.team(k.team).daily_usd or 0.0
        spent = self._spend("team", k.team, "daily")
        self._stage(tr, "budgets", "pass", f"team {k.team} ${spent:,.4f} of " + (f"${cap:,.2f}" if cap else "no cap")
                    + " today", team_spent_usd=round(spent, 6))  # fmt: skip

        # anomaly pause and admin holds
        hold = self.holds.get(k.label)
        if hold == "paused":
            return self._deny(tr, started, "anomaly", 429, "key paused by an admin. Contact an admin.")
        sig = self.signal(k) if k.label in self.baselines else anomaly.classify(k.label, 0.0, 0.0, 0)
        if self.auto_pause and sig.verdict == "pause" and hold != "override":
            return self._deny(tr, started, "anomaly", 429, f"key paused: this hour's spend is {sig.multiple:.0f}x "
                              "its 7-day baseline. Contact an admin.", verdict=sig.verdict)  # fmt: skip
        if self.auto_pause:
            note = " (admin override this hour)" if hold == "override" and sig.verdict == "pause" else ""
            self._stage(tr, "anomaly", "pass", f"{sig.verdict}: {sig.multiple:.1f}x the 7-day baseline{note}",
                        verdict=sig.verdict)  # fmt: skip
        else:
            self._stage(tr, "anomaly", "skip", "auto-pause is off")

        # pre_request content hooks (team mode plus any the policy requires)
        pre = self._hooks("pre_request", k.team, [text], dec.required_hooks)
        names = pii.resolve_hooks(HOOK_MODES[self.content.get(k.team, {"mode": "off"})["mode"]], dec.required_hooks)
        if pre.blocked:
            return self._deny(tr, started, "pre_hooks", 422, f"blocked by content policy ({pre.blocked_by}): "
                              f"{pre.reason}", hooks=names, findings=pre.findings() or None)  # fmt: skip
        found = pre.findings()
        if names:
            what = ", ".join(f"{a}={b}" for a, b in sorted(found.items())) or "nothing found"
            self._stage(tr, "pre_hooks", "pass", f"{', '.join(names)}: {what}", hooks=names, findings=found or None)
        else:
            self._stage(tr, "pre_hooks", "skip", "no hooks configured")
        sent = pre.texts[0]
        tr["sent"] = sent  # what the simulated provider received (shown in the playground, not in the trace list)

        # exact cache (temperature 0 only), then the semantic cache if the team opted in
        fp = dec.fingerprint() + "|" + ",".join(names)
        ck = self._cache_key(k.team, alias, sent, dec.max_tokens, fp)
        if self.cache_on and temperature <= 0:
            hit = self.exact_cache.get(ck)
            if hit:
                self._stage(tr, "cache", "hit", f"exact match for team {k.team}")
                return self._serve_cached(tr, started, k, alias, hit, "hit")
            self._stage(tr, "cache", "miss", "no cached answer")
        else:
            self._stage(tr, "cache", "skip", "cache off" if not self.cache_on else "temperature above 0")
        sem_part = None
        if k.team in self.semantic_teams and temperature <= 0:
            sem_part = semcache.partition_key(k.team, alias, fp)
            found_sem = self.semantic.lookup(sem_part, sent, self.embedder.embed(sent))
            if found_sem.hit:
                self._stage(tr, "semantic_cache", "hit", f"similarity {found_sem.similarity:.3f}",
                            similarity=round(found_sem.similarity, 4))  # fmt: skip
                return self._serve_cached(tr, started, k, alias, found_sem.entry.value, "semantic-hit")
            self._stage(tr, "semantic_cache", "miss", f"{found_sem.reason} (similarity {found_sem.similarity:.3f})",
                        similarity=round(found_sem.similarity, 4))  # fmt: skip
        else:
            self._stage(tr, "semantic_cache", "skip", "off for this team" if k.team not in self.semantic_teams
                        else "not eligible (temperature above 0)")  # fmt: skip

        # TPM reservation
        estimate = estimate_request_tokens(msgs, dec.max_tokens, 256)
        limits = {f"team:{k.team}": h.team(k.team).tpm or 0, f"key:{k.label}": h.key(k.label).tpm or 0}
        reservation = None
        if any(limits.values()):
            try:
                reservation = self.tpm.reserve(limits, estimate)
            except TokenLimitExceeded as e:
                return self._deny(tr, started, "tpm", 429, f"{e} (retry in {e.retry_after:.0f}s)")
            self._stage(tr, "tpm", "pass", f"reserved {estimate} estimated tokens", estimate=estimate)
        else:
            self._stage(tr, "tpm", "skip", "no TPM limit for this key or team")

        # fallback chain
        pt_est = max(1, len(sent) // 4)
        t0 = self.clock()

        def on_attempt(target: Deployment, a) -> None:
            if a.outcome == "skipped":
                target.skipped += 1
            elif a.outcome == "error":
                self._record(k, target, 0, 0, 0.0, a.error, alias)

        try:
            result = await run_chain(
                dec.targets, lambda d: self._simulate(d, pt_est, dec.max_tokens), strategy=route["strategy"],
                breakers=self.breakers, latency=self.latency, clock=self.clock, on_attempt=on_attempt,
            )  # fmt: skip
        except ChainExhausted as e:
            if reservation:
                reservation.release()
            self._chain_stage(tr, route["strategy"], e.attempts, False, (self.clock() - t0) * 1000)
            if e.all_skipped:
                return self._finish(tr, started, 503, f"no provider available for alias {alias!r}: all circuits open")
            return self._finish(tr, started, 502, f"all providers failed for alias {alias!r}")
        self._chain_stage(tr, route["strategy"], result.attempts, True, (self.clock() - t0) * 1000)

        # settle, price, record
        d = result.target
        pt, ct = result.value
        if reservation:
            reservation.settle(pt + ct)
        cost = costs.dollars_for(d.provider, d.model, pt, ct)
        self._record(k, d, pt, ct, cost, None, alias)
        tr.update(prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)
        self._stage(tr, "settle", "pass", f"{pt} in + {ct} out tokens · ${cost:.6f}", cost_usd=round(cost, 8))
        reply = f"(simulated {d.label} reply) Received {len(sent.split())} words: {sent[:140]}"

        # post_response hooks, caches, content log
        post = self._hooks("post_response", k.team, [reply])
        post_names = HOOK_MODES[self.content.get(k.team, {"mode": "off"})["mode"]]
        if post.blocked:
            return self._deny(tr, started, "post_hooks", 422, f"response withheld by content policy: {post.reason}")
        if post_names:
            pf = post.findings()
            what = ", ".join(f"{a}={b}" for a, b in sorted(pf.items())) or "nothing found"
            self._stage(tr, "post_hooks", "pass", f"{', '.join(post_names)}: {what}", hooks=post_names)
        else:
            self._stage(tr, "post_hooks", "skip", "no hooks configured")
        tr["response"] = post.texts[0]
        value = {"answer": tr["response"], "cost_usd": cost, "served_by": d.id, "pt": pt, "ct": ct}
        if self.cache_on and temperature <= 0:
            self.exact_cache[ck] = value
        if sem_part is not None:
            self.semantic.put(sem_part, sent, self.embedder.embed(sent), value)
        self._content_log_stage(tr, k.team, text, tr["response"])
        return self._finish(tr, started, 200)

    def _chain_stage(self, tr: dict, strategy: str, attempts: list, ok: bool, ms: float) -> None:
        tried = [a for a in attempts if a.outcome != "skipped"]
        failed = [a.deployment for a in attempts if a.outcome == "error"]
        skipped = [a.deployment for a in attempts if a.outcome == "skipped"]
        served = next((a.deployment for a in attempts if a.outcome == "ok"), None)
        parts = [f"{strategy} order"]
        if served:
            parts.append(f"served by {served}")
        if failed:
            parts.append(f"failed: {', '.join(failed)}")
        if skipped:
            parts.append(f"skipped (circuit open): {', '.join(skipped)}")
        self._stage(tr, "chain", "pass" if ok else "error", "; ".join(parts), ms,
                    attempts=[a.as_dict() for a in attempts], strategy=strategy)  # fmt: skip
        tr.update(attempts=len(tried), fell_back=len(attempts) > 1, served_by=served)

    def _content_log_stage(self, tr: dict, team: str, text: str, response: str) -> None:
        if self.content.get(team, {}).get("log_content"):
            req, rc = pii.redact(text)
            resp, sc = pii.redact(response)
            self.content_log.append({"t": round(self.clock(), 2), "team": team, "request": req, "response": resp})
            del self.content_log[:-20]
            self._audit("privacy", "content_logged", team, {"findings": {**rc, **sc}})
            self._stage(tr, "content_log", "pass", "stored redacted (team opted in)")
        else:
            self._stage(tr, "content_log", "skip", "off: metadata only")

    def _serve_cached(self, tr: dict, started: float, k: DemoKey, alias: str, hit: dict, kind: str) -> dict:
        d = self.deployment(hit["served_by"])
        self._record(k, d, int(hit.get("pt", 0)), int(hit.get("ct", 0)), 0.0, None, alias, True, hit["cost_usd"])
        tr.update(cache=kind, served_by=hit["served_by"], saved_usd=hit["cost_usd"],
                  prompt_tokens=int(hit.get("pt", 0)), completion_tokens=int(hit.get("ct", 0)))  # fmt: skip
        tr["response"] = hit["answer"]
        self._stage(tr, "settle", "pass", f"served from cache at $0; saved ${hit['cost_usd']:.6f}")
        self._content_log_stage(tr, k.team, tr.get("sent", ""), hit["answer"])
        return self._finish(tr, started, 200)

    # ---------- console: traffic generator ----------

    async def traffic(self, n: int = 20, scenario: str = "mix") -> dict:
        """Simulated request mix (or a runaway batch job) through request(). Returns a summary."""
        out = []
        for i in range(max(1, min(int(n), 200))):
            if scenario == "spike":  # a batch job stuck in a retry loop on the premium route
                k, gap = self.key("fraud-scoring-batch"), 4.0
            else:
                k, gap = self.pick_key(), 0.25
            prompts = SAMPLE_PROMPTS.get(k.team, SAMPLE_PROMPTS["digital-banking"])
            text = prompts[(self.counter + i) % len(prompts)]
            temp = 0.0 if k.team == "member-services" else 0.7
            alias = "smart-fast" if scenario == "spike" else KEY_ALIAS.get(k.label, "smart-fast")
            out.append(await self.request(k.label, alias, text, k.max_tokens, temp, gap, source=scenario))
        self.counter += len(out)
        counts: dict[str, int] = {}
        for t in out:
            counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
        return {"sent": len(out), "outcomes": counts, "last": out[-1]["id"]}

    # ---------- console: views ----------

    def overview(self) -> dict:
        chat = [t for t in self.traces]
        served = [t for t in chat if t["outcome"] in ("ok", "cache_hit")]
        rows = [r for r in self.ledger if r["provider"] != "mcp"]
        flags = []
        for k in self.keys:
            if k.label in self.baselines:
                s = self.signal(k)
                if s.verdict != "ok":
                    flags.append({"label": k.label, "team": k.team, "multiple": round(s.multiple, 2),
                                  "verdict": s.verdict})  # fmt: skip
        today = list(self.history)
        today[NOW_HOUR] = round(today[NOW_HOUR] + sum(r["cost_usd"] for r in self.ledger), 6)
        rejected: dict[str, int] = {}
        for t in chat:
            if t["outcome"] in ("rejected", "error"):
                rejected[str(t["status"])] = rejected.get(str(t["status"]), 0) + 1
        breakers = [self.breakers.get(d).snapshot() for d in sorted(self.pool)]
        lat = sorted(t["latency_ms"] for t in served)
        return {
            "date": "simulated day",
            "kpis": {
                "spend_usd": round(sum(r["cost_usd"] for r in self.ledger), 6),
                "requests": sum(1 for r in rows if not r["error"]),
                "failed_attempts": sum(1 for r in rows if r["error"]),
                "tool_calls": sum(1 for r in self.ledger if r["provider"] == "mcp"),
                "cache": {
                    "saved_usd": round(sum(r["saved_usd"] for r in self.ledger), 6),
                    "hits": sum(r["cached"] for r in rows),
                    "hit_rate": round(sum(r["cached"] for r in rows) / len([r for r in rows if not r["error"]]), 4)
                    if any(not r["error"] for r in rows) else 0.0,
                },
                "anomalies_flagged": sum(1 for f in flags if f["verdict"] == "flagged"),
                "anomalies_paused": sum(1 for f in flags if f["verdict"] == "pause"),
                "open_circuits": sum(1 for b in breakers if b["state"] != "closed"),
            },
            "traces": {
                "requests": len(chat), "served": len(served),
                "fallbacks": sum(1 for t in served if t["fell_back"]),
                "fallback_rate": round(sum(1 for t in served if t["fell_back"]) / len(served), 4) if served else 0.0,
                "by_status": rejected, "latency_p50_ms": lat[len(lat) // 2] if lat else None,
                "window": "this browser session (simulated)",
            },
            "by_team": showback.aggregate(self.ledger, ("team",)),
            "by_model": showback.aggregate(rows, ("provider", "model")),
            "hourly": {"today": today, "baseline_7d": self._baseline_by_hour(), "current_hour": NOW_HOUR,
                       "history_simulated_before": NOW_HOUR},
            "anomalies": sorted(flags, key=lambda f: -f["multiple"]),
            "breakers": breakers,
            "mock_providers": True,
        }  # fmt: skip

    def trace_rows(self, limit: int = 100, team: str = "", outcome: str = "", q: str = "") -> list[dict]:
        needle = q.lower().strip()
        out = []
        for t in reversed(self.traces):
            if team and t["team"] != team:
                continue
            if outcome and t["outcome"] != outcome:
                continue
            if needle:
                hay = " ".join(str(t.get(f)) for f in ("id", "key", "key_fp", "team", "alias", "served_by", "reason",
                                                       "status")).lower()  # fmt: skip
                if needle not in hay:
                    continue
            out.append({k: v for k, v in t.items() if k not in ("stages", "sent", "response")})
            if len(out) >= limit:
                break
        return out

    def trace(self, trace_id: str) -> dict | None:
        for t in self.traces:
            if t["id"] == trace_id:
                return {k: v for k, v in t.items() if k not in ("sent", "response")}
        return None

    def routes_view(self) -> dict:
        return {a: dict(r) for a, r in sorted(self.routes.items())}

    def providers_view(self) -> dict:
        out: dict[str, dict] = {}
        for d in sorted(self.pool.values(), key=lambda x: x.id):
            p = out.setdefault(d.provider, {"provider": d.provider, "label": d.label, "deployments": [],
                                             "outage": False, "rate_limited": False})  # fmt: skip
            br = self.breakers.get(d.id).snapshot()
            ewma = self.latency.ewma(d.id)
            p["deployments"].append({"id": d.id, "latency_ms": d.latency_ms, "error_rate": d.error_rate,
                                     "served": d.served, "failed": d.failed, "skipped": d.skipped,
                                     "breaker": br["state"], "consecutive_failures": br["consecutive_failures"],
                                     "ewma_ms": round(ewma * 1000) if ewma is not None else None})  # fmt: skip
            p["outage"] = p["outage"] or d.outage
            p["rate_limited"] = p["rate_limited"] or d.rate_limited
            p.setdefault("latency_ms", d.latency_ms)
            p["error_rate"] = max(p.get("error_rate", 0.0), d.error_rate)
        return {"providers": list(out.values()), "transitions": self.breakers.recent_transitions(30),
                "clock_s": round(self.clock(), 2)}  # fmt: skip

    def set_provider(self, provider: str, changes: dict) -> None:
        deps = [d for d in self.pool.values() if d.provider == provider]
        if not deps:
            raise KeyError(provider)
        for d in deps:
            self.update_deployment(d.id, changes)

    def reset_breakers(self) -> None:
        self.breakers.reset()

    # ---------- console: keys, holds and budgets ----------

    def keys_view(self) -> list[dict]:
        out = []
        for k in self.keys:
            s = self.signal(k) if k.label in self.baselines else None
            out.append({
                "id": k.key_id, "label": k.label, "team": k.team, "key_prefix": "sk-demo-" + k.label[:4],
                "fingerprint": showback.fingerprint(k.key_id), "is_admin": False, "created_at": "simulated",
                "revoked_at": "revoked" if k.label in self.revoked else None, "hold": self.holds.get(k.label),
                "verdict": s.verdict if s else "ok", "multiple": round(s.multiple, 2) if s else 0.0,
                "spent_today_usd": round(self._spend("key", k.key_id, "daily"), 6),
                "cap_usd": self.hierarchy().key(k.label).daily_usd or 0.0,
            })  # fmt: skip
        return out

    def create_key(self, label: str, team: str) -> dict:
        label, team = label.strip(), team.strip()
        if not label or not team or self.find_key(label):
            raise ValueError("label must be new and non-empty; team must be non-empty")
        k = DemoKey(label, team, 0.0, 0.01, 8, 300, 300)
        self.keys.append(k)
        if team not in self.content:
            self.content[team] = {"mode": "off", "log_content": False}
        self._audit("admin", "key_created", team, {"stage": label})
        return {"id": k.key_id, "label": label, "team": team, "key": "sk-demo-" + hashlib.sha256(
            f"{self.seed}{label}".encode()).hexdigest()[:24], "simulated": True}  # fmt: skip

    def revoke_key(self, label: str) -> None:
        self.key(label)
        self.revoked.add(label)
        self._audit("admin", "key_revoked", self.key(label).team, {"stage": label})

    def pause_key(self, label: str) -> None:
        self.holds[label] = "paused"
        self._audit("admin", "key_paused", self.key(label).team, {"stage": label})

    def unpause_key(self, label: str) -> str:
        k = self.key(label)
        had = self.holds.pop(label, None)
        sig = self.signal(k) if label in self.baselines else None
        if self.auto_pause and sig is not None and sig.verdict == "pause":
            self.holds[label] = "override"
            self._audit("admin", "anomaly_override", k.team, {"stage": label})
            return "override"
        if had:
            self._audit("admin", "key_unpaused", k.team, {"stage": label})
            return "unpaused"
        return "not_paused"

    def budgets_view(self) -> dict:
        h = self.hierarchy()
        teams = sorted({k.team for k in self.keys} | set(self.team_limits))
        return {
            "org": {"spent_usd": round(self._spend("org", None, "daily"), 6), "cap_usd": self.org.daily_usd or 0.0},
            "teams": [{"team": t, "spent_usd": round(self._spend("team", t, "daily"), 6),
                       "cap_usd": h.team(t).daily_usd or 0.0, "tpm": h.team(t).tpm or 0,
                       "tpm_used": self.tpm.used(f"team:{t}")} for t in teams],
            "keys": {lbl: lim.daily_usd for lbl, lim in self.key_limits.items()},
            "key_default_usd": self.key_default.daily_usd,
            "warnings": h.warnings(),
        }  # fmt: skip

    def set_key_cap(self, label: str, daily_usd: float) -> None:
        self.key_limits[label] = Limits(max(0.0, float(daily_usd)))

    # ---------- console: config files (router/configcheck.py) ----------

    def set_config_text(self, name: str, text: str) -> None:
        self.config_text[name] = text

    def config(self, name: str) -> dict:
        if name not in configcheck.CONFIG_NAMES:
            raise KeyError(name)
        text = self.config_text.get(name, "")
        return {"name": name, "path": {"policies": "config/policies.yaml", "routes": "config/routes.yaml",
                                       "mcp": "config/mcp.example.yaml"}[name],
                "exists": bool(text), "text": text, "writable": True}  # fmt: skip

    def validate_config(self, name: str, text: str) -> dict:
        return configcheck.validate(name, text, sorted(self.routes),
                                    sorted(self.policies.referenced_aliases()))  # fmt: skip

    def apply_config(self, name: str, text: str) -> dict:
        try:
            loaded = configcheck.parse(name, text)
        except configcheck.ConfigRejected as e:
            return {"ok": False, "errors": e.errors}
        if name == "policies":
            self.policies = loaded
        elif name == "mcp":
            self.mcp = loaded
        else:  # aliases and strategies; budgets, breaker thresholds and prices keep the demo's simulated values
            self._set_routes({a: {"strategy": loaded.strategy(a), "targets": [f"{t.provider}/{t.model}" for t in ts]}
                              for a, ts in loaded.aliases.items()})  # fmt: skip
        self.config_text[name] = text
        self._audit("admin", "config_applied", "", {"stage": name})
        return {"ok": True, "status": "applied", "path": self.config(name)["path"]}

    def dry_run(self, teams: list[str] | None = None, aliases: list[str] | None = None,
                max_tokens: int | None = None, text: str | None = None) -> dict:  # fmt: skip
        pol = self.policies
        if text is not None:
            try:
                pol = configcheck.parse("policies", text)
            except configcheck.ConfigRejected as e:
                return {"ok": False, "errors": e.errors}
        teams = teams or sorted({k.team for k in self.keys} | set(pol.teams))
        aliases = aliases or sorted(self.routes)
        routes = {a: [self.pool[t] for t in r["targets"]] for a, r in self.routes.items()}
        return {"ok": True, "results": configcheck.decisions(pol, routes, teams, aliases, max_tokens)}

    # ---------- console: settings ----------

    def settings_view(self) -> dict:
        return {
            "breakers_enabled": self.breaker_config.enabled, "auto_pause": self.auto_pause, "cache": self.cache_on,
            "semantic_teams": sorted(self.semantic_teams), "semantic_threshold": self.semantic.threshold,
            "content": self.content, "teams": sorted({k.team for k in self.keys} | set(self.content)),
            "breaker_config": asdict(self.breaker_config), "estimator": estimator_name(),
        }  # fmt: skip

    def set_cache(self, enabled: bool) -> None:
        self.cache_on = bool(enabled)
        if not self.cache_on:
            self.exact_cache.clear()

    def set_semantic_team(self, team: str, enabled: bool) -> None:
        (self.semantic_teams.add if enabled else self.semantic_teams.discard)(team)

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

    # The console (demo/app.js) calls everything through api(): one method name, JSON args, JSON result.
    API = frozenset({
        "overview", "trace_rows", "trace", "routes_view", "providers_view", "set_provider", "reset_breakers",
        "advance", "keys_view", "create_key", "revoke_key", "pause_key", "unpause_key", "budgets_view",
        "set_team_cap", "set_team_tpm", "set_org_cap", "set_key_cap", "config", "set_config_text",
        "validate_config", "apply_config", "dry_run", "settings_view", "set_cache", "set_semantic_team",
        "set_breakers_enabled", "set_auto_pause", "set_content_policy", "governance", "mcp_view", "mcp_call",
        "semantic_view", "showback_csv", "reset",
    })  # fmt: skip
    ASYNC_API = frozenset({"request", "traffic", "semantic_ask"})

    def api(self, method: str, args_json: str = "{}") -> str:
        kwargs = json.loads(args_json or "{}")
        if method in self.ASYNC_API:
            result = run_sync(getattr(self.engine, method)(**kwargs))
        elif method in self.API:
            result = getattr(self.engine, method)(**kwargs)
        else:
            raise ValueError(method)
        return json.dumps(result)
