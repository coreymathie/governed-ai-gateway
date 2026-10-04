# Corey Mathie, 2026
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import RouterConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    OLLAMA_HOST: str = "http://127.0.0.1:11434"

    ROUTER_HOST: str = "0.0.0.0"
    ROUTER_PORT: int = 4000
    ROUTER_ROUTES_FILE: str = "./config/routes.yaml"
    ROUTER_DB_PATH: str = "./router.db"
    ROUTER_ADMIN_KEY: str = ""


settings = Settings()

_cached: RouterConfig | None = None


def load_routes() -> RouterConfig:
    global _cached
    data = yaml.safe_load(Path(settings.ROUTER_ROUTES_FILE).read_text())
    _cached = RouterConfig.model_validate(data)
    return _cached


def current() -> RouterConfig:
    if _cached is None:
        return load_routes()
    return _cached
