# Corey Mathie, 2026
import textwrap

import pytest

from router import anomaly, auth, config_loader, mcp_gateway, metrics, routing, store
from router.config_loader import settings

ROUTES = textwrap.dedent(
    """
    aliases:
      smart-fast:
        - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 5 }
        - { provider: openai, model: gpt-4.1-mini, timeout_s: 5 }
      local-only:
        - { provider: ollama, model: llama3.1:8b, timeout_s: 5 }
    policies:
      per_key_daily_usd: 50
      per_team_daily_usd: 500
      rate_limit_rpm: 120
      auto_pause_on_anomaly: false
    """
)


@pytest.fixture(autouse=True)
def router_env(tmp_path, monkeypatch):
    routes = tmp_path / "routes.yaml"
    routes.write_text(ROUTES)
    monkeypatch.setattr(settings, "ROUTER_DB_PATH", str(tmp_path / "router.db"))
    monkeypatch.setattr(settings, "ROUTER_ROUTES_FILE", str(routes))
    monkeypatch.setattr(settings, "ROUTER_ADMIN_KEY", "sk-router-admin")
    policies = tmp_path / "policies.yaml"
    policies.write_text("version: 1\n")  # no restrictions unless a test writes its own
    monkeypatch.setattr(settings, "ROUTER_POLICIES_FILE", str(policies))
    monkeypatch.setattr(settings, "OPA_URL", "")
    monkeypatch.setattr(config_loader, "_cached", None)
    monkeypatch.setattr(config_loader, "_policies", None)
    mcp = tmp_path / "mcp.yaml"
    mcp.write_text("version: 1\n")  # no MCP servers unless a test writes its own
    monkeypatch.setattr(settings, "ROUTER_MCP_FILE", str(mcp))
    monkeypatch.setattr(config_loader, "_mcp", None)
    auth._calls.clear()
    anomaly.clear_cache()
    metrics.reset()
    routing.reset_state()  # circuit breakers, latency averages, TPM windows are process-wide
    mcp_gateway.limiter.reset()
    store.init_db()
    config_loader.load_routes()
    return routes
