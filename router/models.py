# Corey Mathie, 2026
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False


class ApiKey(BaseModel):
    """An application key as stored: never the secret, only an id, a display prefix and a fingerprint."""

    id: str  # "key_..." (random, not derived from the secret); what usage rows reference
    key_prefix: str  # "sk-router-" + 4 characters, to recognise a key in a list
    fingerprint: str  # first 8 hex of SHA-256 of the secret; shown in showback and the audit trail
    label: str
    team: str = "default"
    is_admin: bool = False
    created_at: str
    revoked_at: str | None = None


class ApiKeyCreated(ApiKey):
    """Returned once, when a key is created: the only time the secret is available."""

    key: str


class ApiKeyCreate(BaseModel):
    label: str
    team: str = "default"
    is_admin: bool = False


Provider = Literal["openai", "anthropic", "gemini", "ollama"]


class RouteTarget(BaseModel):
    provider: Provider  # validated when routes.yaml loads, so a typo fails at startup, not mid-request
    model: str
    timeout_s: int = 30


class RouterPolicies(BaseModel):
    # Defaults for every key / team; override per team or key label under `budgets:`. 0 = no cap.
    per_key_daily_usd: float = 50.0
    per_team_daily_usd: float = 500.0
    per_key_monthly_usd: float = 0.0
    per_team_monthly_usd: float = 0.0
    per_key_tpm: int = 0  # tokens per minute (estimated before the call, corrected after)
    per_team_tpm: int = 0
    default_output_tokens_estimate: int = 256  # reserved when a request doesn't set max_tokens
    rate_limit_rpm: int = 120
    auto_pause_on_anomaly: bool = False  # block a key whose hourly spend hits 10x its 7-day baseline
    cache_ttl_seconds: int = 0  # 0 turns the response cache off
    cache_max_temperature: float = 0.0  # only cache requests at or below this temperature (deterministic ones)


Strategy = Literal["ordered", "latency"]


class CircuitBreakerSettings(BaseModel):
    enabled: bool = True
    failure_threshold: int = Field(5, ge=1)
    error_rate_threshold: float = Field(0.5, gt=0, le=1)
    min_requests: int = Field(10, ge=1)
    window_seconds: float = Field(60.0, gt=0)
    cooldown_seconds: float = Field(30.0, ge=0)
    half_open_max_probes: int = Field(1, ge=1)
    success_threshold: int = Field(1, ge=1)
    max_retry_after_seconds: float = Field(300.0, ge=0)


class LatencySettings(BaseModel):
    alpha: float = Field(0.3, gt=0, le=1)
    min_samples: int = Field(3, ge=1)
    tolerance: float = Field(0.10, ge=0)


class ResilienceSettings(BaseModel):
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()
    latency: LatencySettings = LatencySettings()


class LimitOverride(BaseModel):
    daily_usd: float | None = Field(None, ge=0)
    monthly_usd: float | None = Field(None, ge=0)
    tpm: int | None = Field(None, ge=0)


class BudgetSettings(BaseModel):
    org: LimitOverride = LimitOverride()
    teams: dict[str, LimitOverride] = {}
    keys: dict[str, LimitOverride] = {}  # by key label


class PriceOverride(BaseModel):
    input_usd_per_1m: float = Field(ge=0)
    output_usd_per_1m: float = Field(ge=0)


PiiKind = Literal["secret", "email", "card", "ssn", "phone"]


class HookSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pre_request: list[str] = []  # run over message contents before any provider call
    post_response: list[str] = []  # run over the completion before it reaches the client, cache or content log


class TeamPrivacy(HookSet):
    log_content: bool = False  # opt in to storing (redacted) prompts and completions for this team


class PrivacySettings(BaseModel):
    """Content hooks per route and team, and the content-logging opt-in. Layers are unioned: default + route + team."""

    model_config = ConfigDict(extra="forbid")
    kinds: list[PiiKind] = ["secret", "email", "card", "ssn", "phone"]
    default: HookSet = HookSet()
    routes: dict[str, HookSet] = {}
    teams: dict[str, TeamPrivacy] = {}
    content_log_max_chars: int = Field(4000, ge=100, le=100_000)

    @model_validator(mode="after")
    def _known_hooks(self) -> "PrivacySettings":
        from .pii import registered_hooks

        known = set(registered_hooks())
        layers = [self.default, *self.routes.values(), *self.teams.values()]
        unknown = sorted({h for s in layers for h in (*s.pre_request, *s.post_response)} - known)
        if unknown:
            raise ValueError(f"privacy: unknown hooks {unknown}; registered: {sorted(known)}")
        return self

    def hooks_for(self, team: str, alias: str, stage: str) -> list[str]:
        from .pii import resolve_hooks

        layers = [self.default, self.routes.get(alias, HookSet()), self.teams.get(team, HookSet())]
        return resolve_hooks(*(getattr(s, stage) for s in layers))

    def logs_content(self, team: str) -> bool:
        t = self.teams.get(team)
        return bool(t and t.log_content)


class SemanticGuards(BaseModel):
    model_config = ConfigDict(extra="forbid")
    numbers: bool = True  # both prompts must contain the same numbers
    negation: bool = True  # ...and both or neither must be negated


class SemanticCacheSettings(BaseModel):
    """Embedding-similarity cache (router/semcache.py). Off by default; calibrate the threshold first."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    threshold: float = Field(0.88, gt=0, le=1)  # from scripts/calibrate_semcache.py on the bundled pairs
    ttl_seconds: int = Field(3600, ge=1)
    max_entries_per_partition: int = Field(1000, ge=1)
    max_temperature: float = Field(0.0, ge=0)  # only deterministic requests are served from or stored in it
    embedder: Literal["hashing", "provider"] = "hashing"
    embedding_model: str = ""  # provider/model when embedder is "provider", e.g. openai/text-embedding-3-small
    guards: SemanticGuards = SemanticGuards()
    teams: list[str] = []  # when non-empty, only these teams use it (per-team opt-in)
    team_thresholds: dict[str, float] = {}  # per-team calibrated thresholds, overriding `threshold`

    def threshold_for(self, team: str) -> float:
        return self.team_thresholds.get(team, self.threshold)

    def applies_to(self, team: str) -> bool:
        return self.enabled and (not self.teams or team in self.teams)

    @model_validator(mode="after")
    def _model_for_provider(self) -> "SemanticCacheSettings":
        bad = {t: v for t, v in self.team_thresholds.items() if not 0 < v <= 1}
        if bad:
            raise ValueError(f"semantic_cache.team_thresholds must be in (0, 1]: {bad}")
        if self.embedder == "provider" and "/" not in self.embedding_model:
            raise ValueError("semantic_cache.embedding_model must be 'provider/model' when embedder is 'provider'")
        return self


class ShadowSettings(BaseModel):
    """Mirror a share of an alias's live traffic to a candidate alias. Never returned to the client."""

    model_config = ConfigDict(extra="forbid")
    candidate: str  # alias whose deployments receive the mirrored request
    percent: float = Field(0.0, ge=0, le=100)
    billing: Literal["ledger", "suppress"] = "ledger"  # ledger: cost in shadow_usage; suppress: cost not recorded
    max_daily_usd: float = Field(0.0, ge=0)  # stop mirroring once today's shadow ledger reaches this (0 = no cap)


class RouterConfig(BaseModel):
    aliases: dict[str, list[RouteTarget]]
    strategies: dict[str, Strategy] = {}  # alias -> strategy; filled from the mapping form below
    policies: RouterPolicies = RouterPolicies()
    resilience: ResilienceSettings = ResilienceSettings()
    budgets: BudgetSettings = BudgetSettings()
    prices: dict[str, PriceOverride] = {}  # "provider/model" -> USD per 1M tokens
    privacy: PrivacySettings = PrivacySettings()
    shadow: dict[str, ShadowSettings] = {}  # live alias -> candidate mirror
    semantic_cache: SemanticCacheSettings = SemanticCacheSettings()

    @model_validator(mode="before")
    @classmethod
    def _alias_mapping_form(cls, data: Any) -> Any:
        """Accept `alias: [targets]` (ordered) or `alias: {strategy: latency, targets: [...]}`."""
        if not isinstance(data, dict) or not isinstance(data.get("aliases"), dict):
            return data
        data = dict(data)
        strategies = dict(data.get("strategies") or {})
        aliases = {}
        for name, spec in data["aliases"].items():
            if isinstance(spec, dict):
                unknown = set(spec) - {"strategy", "targets"}
                if unknown:
                    raise ValueError(f"alias {name!r}: unknown keys {sorted(unknown)}")
                strategies[name] = spec.get("strategy", "ordered")
                aliases[name] = spec.get("targets") or []
            else:
                aliases[name] = spec
        data["aliases"] = aliases
        data["strategies"] = strategies
        return data

    @model_validator(mode="after")
    def _check_references(self) -> "RouterConfig":
        for name, targets in self.aliases.items():
            if not targets:
                raise ValueError(f"alias {name!r} has no targets")
        unknown = set(self.strategies) - set(self.aliases)
        if unknown:
            raise ValueError(f"strategies set for unknown aliases: {sorted(unknown)}")
        for key in self.prices:
            if "/" not in key:
                raise ValueError(f"price key {key!r} must be 'provider/model'")
        for alias, sh in self.shadow.items():
            if alias not in self.aliases or sh.candidate not in self.aliases:
                raise ValueError(f"shadow.{alias}: alias and candidate must both be defined aliases")
            if sh.candidate == alias:
                raise ValueError(f"shadow.{alias}: candidate must differ from the alias")
        unknown_routes = set(self.privacy.routes) - set(self.aliases)
        if unknown_routes:
            raise ValueError(f"privacy.routes set for unknown aliases: {sorted(unknown_routes)}")
        return self

    def strategy(self, alias: str) -> str:
        return self.strategies.get(alias, "ordered")
