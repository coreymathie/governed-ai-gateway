# Corey Mathie, 2026
from typing import Literal

from pydantic import BaseModel


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
    key: str
    label: str
    team: str = "default"
    is_admin: bool = False
    created_at: str
    revoked_at: str | None = None


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
    per_key_daily_usd: float = 50.0
    per_team_daily_usd: float = 500.0
    rate_limit_rpm: int = 120
    auto_pause_on_anomaly: bool = False  # block a key whose hourly spend hits 10x its 7-day baseline
    cache_ttl_seconds: int = 0  # 0 turns the response cache off
    cache_max_temperature: float = 0.0  # only cache requests at or below this temperature (deterministic ones)


class RouterConfig(BaseModel):
    aliases: dict[str, list[RouteTarget]]
    policies: RouterPolicies = RouterPolicies()
