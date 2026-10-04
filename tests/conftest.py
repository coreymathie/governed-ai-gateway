# Corey Mathie, 2026
import textwrap

import pytest

from router import anomaly, auth, config_loader, metrics, store
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
    monkeypatch.setattr(config_loader, "_cached", None)
    auth._calls.clear()
    anomaly.clear_cache()
    metrics.reset()
    store.init_db()
    config_loader.load_routes()
    return routes
