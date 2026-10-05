# Corey Mathie, 2026
from __future__ import annotations

import importlib
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import mcp_policy, policy
from .models import RouterConfig
from .pii import registered_hooks


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
    # Optional secret mixed into application-key hashes. Changing it invalidates every key.
    ROUTER_KEY_PEPPER: str = ""
    # Comma-separated Python modules imported before config validation; they call router.pii.register_hook().
    ROUTER_HOOK_MODULES: str = ""
    # Policy-as-code file. Empty: ./config/policies.yaml if it exists, else no policy restrictions.
    # A path that is set but missing or invalid stops the gateway from starting (fail closed).
    ROUTER_POLICIES_FILE: str = ""
    OPA_URL: str = ""  # e.g. http://127.0.0.1:8181 ; when set, OPA must also allow every request
    # Largest request body accepted on any endpoint (413 above it). 0 disables the check.
    ROUTER_MAX_BODY_BYTES: int = 1_048_576
    # MCP tool gateway config. Empty: ./config/mcp.yaml if it exists, else no MCP servers.
    ROUTER_MCP_FILE: str = ""


settings = Settings()

_cached: RouterConfig | None = None
_policies: policy.PolicySet | None = None
_mcp: mcp_policy.McpPolicy | None = None
DEFAULT_POLICIES = Path("./config/policies.yaml")
DEFAULT_MCP = Path("./config/mcp.yaml")


def import_hook_modules() -> None:
    for name in filter(None, (m.strip() for m in settings.ROUTER_HOOK_MODULES.split(","))):
        importlib.import_module(name)


def load_routes() -> RouterConfig:
    global _cached
    import_hook_modules()
    data = yaml.safe_load(Path(settings.ROUTER_ROUTES_FILE).read_text())
    _cached = RouterConfig.model_validate(data)
    return _cached


def current() -> RouterConfig:
    if _cached is None:
        return load_routes()
    return _cached


def policies_path() -> Path | None:
    if settings.ROUTER_POLICIES_FILE:
        return Path(settings.ROUTER_POLICIES_FILE)
    return DEFAULT_POLICIES if DEFAULT_POLICIES.exists() else None


def parse_policies() -> policy.PolicySet:
    path = policies_path()
    if path is None:
        return policy.PolicySet()
    return policy.load(yaml.safe_load(path.read_text()), registered_hooks())


def load_policies() -> policy.PolicySet:
    global _policies
    _policies = parse_policies()
    return _policies


def current_policies() -> policy.PolicySet:
    if _policies is None:
        return load_policies()
    return _policies


def parse_mcp() -> mcp_policy.McpPolicy:
    if settings.ROUTER_MCP_FILE:
        path: Path | None = Path(settings.ROUTER_MCP_FILE)
    else:
        path = DEFAULT_MCP if DEFAULT_MCP.exists() else None
    if path is None:
        return mcp_policy.McpPolicy()
    return mcp_policy.load(yaml.safe_load(path.read_text()))


def load_mcp() -> mcp_policy.McpPolicy:
    global _mcp
    _mcp = parse_mcp()
    return _mcp


def current_mcp() -> mcp_policy.McpPolicy:
    if _mcp is None:
        return load_mcp()
    return _mcp


def reload_all() -> tuple[RouterConfig, policy.PolicySet]:
    """Parse routes, policies and MCP config first, then swap them together; a bad file keeps the previous set."""
    global _cached, _policies, _mcp
    import_hook_modules()
    cfg = RouterConfig.model_validate(yaml.safe_load(Path(settings.ROUTER_ROUTES_FILE).read_text()))
    pol = parse_policies()
    mcp = parse_mcp()
    _cached, _policies, _mcp = cfg, pol, mcp
    return cfg, pol
