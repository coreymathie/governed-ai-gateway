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
    # Answer every provider call with a simulated provider (router/mock_provider.py): no keys, no network.
    ROUTER_MOCK_PROVIDERS: bool = False
    # Let admins replace the policies / routes / MCP files from the console (PUT /admin/config/{name}).
    ROUTER_ALLOW_CONFIG_WRITES: bool = False
    # When ROUTER_ADMIN_KEY is empty: read the admin key from this file, or create one there on first start.
    ROUTER_ADMIN_KEY_FILE: str = ""
    # Request traces kept in memory for the console (per worker).
    ROUTER_TRACE_BUFFER: int = 500
    # Serve the console (demo/) at /console.
    ROUTER_CONSOLE: bool = True


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


def ensure_admin_key() -> str | None:
    """With ROUTER_ADMIN_KEY empty and ROUTER_ADMIN_KEY_FILE set, read the admin key from that file, creating it
    (random, mode 0600) on first start. Returns the file path used, or None. The key itself is never logged."""
    if settings.ROUTER_ADMIN_KEY or not settings.ROUTER_ADMIN_KEY_FILE:
        return None
    import os
    import secrets

    path = Path(settings.ROUTER_ADMIN_KEY_FILE)
    if path.is_file() and path.read_text().strip():
        settings.ROUTER_ADMIN_KEY = path.read_text().strip()
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "sk-admin-" + secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    settings.ROUTER_ADMIN_KEY = key
    return str(path)
