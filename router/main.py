# Corey Mathie, 2026
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from . import metrics
from .anomaly import clear_cache, evaluate_all
from .auth import require_admin, require_key
from .config_loader import current, load_routes
from .costs import is_priced, unpriced_models
from .models import ApiKey, ApiKeyCreate, ChatCompletionRequest
from .routing import open_stream, route
from .store import (
    cache_stats_today,
    calls_today,
    create_key,
    init_db,
    list_keys,
    per_provider_errors,
    recent_calls,
    revoke_key,
    spend_today,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")

KeyDep = Annotated[ApiKey, Depends(require_key)]
AdminDep = Annotated[ApiKey | None, Depends(require_admin)]


def _warn_unpriced() -> None:
    for alias, targets in current().aliases.items():
        for t in targets:
            if not is_priced(t.provider, t.model):
                log.warning("alias %s -> %s/%s has no price; spend caps won't see it", alias, t.provider, t.model)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    load_routes()
    _warn_unpriced()
    yield


app = FastAPI(title="multi-provider-llm-router", version="0.4.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "aliases": sorted(current().aliases), "unpriced_models": unpriced_models()}


@app.post("/v1/chat/completions")
async def chat(body: ChatCompletionRequest, response: Response, key: KeyDep):
    if body.stream:
        target, events = await open_stream(key, body)
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={
                "x-router-used-provider": target.provider,
                "x-router-used-model": target.model,
                "cache-control": "no-cache",
            },
        )
    data, provider, model, cached = await route(key, body)
    response.headers["x-router-used-provider"] = provider
    response.headers["x-router-used-model"] = model
    response.headers["x-router-cache"] = "hit" if cached else "miss"
    return data


@app.get("/v1/models")
async def models(_key: KeyDep):
    """List aliases so OpenAI clients can discover them."""
    return {"object": "list", "data": [{"id": alias, "object": "model"} for alias in sorted(current().aliases)]}


# ---------- Admin ----------


@app.post("/admin/reload")
async def admin_reload(_a: AdminDep):
    try:
        cfg = load_routes()
    except (ValidationError, ValueError, OSError) as e:
        raise HTTPException(400, f"routes file rejected, previous config still active: {e}") from e
    _warn_unpriced()
    return {"status": "reloaded", "aliases": sorted(cfg.aliases)}


@app.get("/admin/keys")
async def admin_list_keys(_a: AdminDep) -> list[ApiKey]:
    return list_keys()


@app.post("/admin/keys")
async def admin_create_key(req: ApiKeyCreate, _a: AdminDep) -> ApiKey:
    return create_key(label=req.label, team=req.team, is_admin=req.is_admin)


@app.delete("/admin/keys/{key}")
async def admin_revoke(key: str, _a: AdminDep):
    revoke_key(key)
    return {"status": "revoked"}


@app.get("/admin/usage/today")
async def admin_usage_today(_a: AdminDep):
    keys = list_keys()
    per_key = [{"label": k.label, "team": k.team, "spend_usd": round(spend_today(key=k.key), 4)} for k in keys]
    teams = sorted({k.team for k in keys})
    return {
        "total_spend_usd": round(spend_today(), 4),
        "calls_today": calls_today(),
        "cache": cache_stats_today(),
        "per_key": per_key,
        "per_team": {t: round(spend_today(team=t), 4) for t in teams},
        "provider_errors_60m": per_provider_errors(60),
    }


@app.get("/admin/recent")
async def admin_recent(_a: AdminDep, limit: int = 50):
    return recent_calls(min(limit, 500))


@app.get("/admin/anomalies")
async def admin_anomalies(_a: AdminDep):
    clear_cache()
    labels = {k.key: (k.label, k.team) for k in list_keys()}
    out = []
    for s in evaluate_all():
        label, team = labels.get(s.key, ("?", "?"))
        out.append(
            {
                "label": label,
                "team": team,
                "hour_spend_usd": round(s.hour_spend, 5),
                "baseline_hourly_usd": round(s.baseline_hourly, 5),
                "history_hours": s.history_hours,
                "multiple": round(s.multiple, 2),
                "verdict": s.verdict,
            }
        )
    order = {"pause": 0, "flagged": 1, "ok": 2}
    return sorted(out, key=lambda r: (order[r["verdict"]], -r["multiple"]))


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(_a: AdminDep):
    """Prometheus exposition format. Configure the scraper with the admin key as a bearer token."""
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


# ---------- Dashboard ----------


@app.get("/dashboard")
async def dashboard():
    html = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
    return FileResponse(str(html), media_type="text/html")
