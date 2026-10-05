# Corey Mathie, 2026
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, audit, console, mcp_gateway, metrics, mock_provider, routing, shadow, showback, traces
from .anomaly import clear_cache, evaluate_all
from .auth import require_admin, require_key
from .budgets import utilization
from .config_loader import (
    current,
    current_mcp,
    current_policies,
    ensure_admin_key,
    load_mcp,
    load_policies,
    load_routes,
    reload_all,
    settings,
)
from .costs import is_priced, unpriced_models
from .models import ApiKey, ApiKeyCreate, ApiKeyCreated, ChatCompletionRequest
from .routing import open_stream, route
from .store import (
    budget_overrides,
    cache_stats_today,
    calls_today,
    clear_budget_override,
    clear_key_hold,
    content_log_rows,
    create_key,
    end_of_this_hour,
    init_db,
    key_holds,
    list_keys,
    per_provider_errors,
    recent_calls,
    revoke_key,
    set_budget_override,
    set_key_hold,
    shadow_summary,
    spend_for,
    spend_today,
    usage_rows,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")

KeyDep = Annotated[ApiKey, Depends(require_key)]
AdminDep = Annotated[ApiKey | None, Depends(require_admin)]


def _warn_unpriced() -> None:
    routing.sync_config()
    unknown = current_policies().referenced_aliases() - set(current().aliases)
    if unknown:
        log.warning("policies reference aliases not in the routes file: %s", sorted(unknown))
    if current_policies().opa.enabled and not settings.OPA_URL:
        log.warning("policies.opa.enabled is true but OPA_URL is not set: every request will be refused")
    for warning in routing.hierarchy(current()).warnings():
        log.warning("budget config: %s", warning)
    for alias, targets in current().aliases.items():
        for t in targets:
            if not is_priced(t.provider, t.model):
                log.warning("alias %s -> %s/%s has no price; spend caps won't see it", alias, t.provider, t.model)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    key_file = ensure_admin_key()
    if key_file:
        log.info("admin key read from %s (not logged)", key_file)
    if settings.ROUTER_MOCK_PROVIDERS:
        log.warning("ROUTER_MOCK_PROVIDERS is on: every provider call is answered by a simulated provider")
    traces.configure(settings.ROUTER_TRACE_BUFFER)
    init_db()
    load_routes()
    load_policies()  # a missing or invalid policies file stops startup (fail closed)
    load_mcp()  # likewise for the MCP tool gateway config
    _warn_unpriced()
    yield
    await shadow.drain()  # let in-flight shadow mirrors finish recording


class BodySizeLimit:
    """Refuse request bodies over ROUTER_MAX_BODY_BYTES with 413, before they're parsed (pure ASGI).

    A declared Content-Length over the limit is refused without reading the body. A chunked body is
    counted as it arrives; once it passes the limit the client gets 413, the app is told the client
    disconnected, and anything the app tries to send afterwards is dropped.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = settings.ROUTER_MAX_BODY_BYTES
        if scope["type"] != "http" or limit <= 0:
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await self._reject(send, limit)
        seen = 0
        started = rejected = False

        async def limited_receive():
            nonlocal seen, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    rejected = True
                    if not started:
                        await self._reject(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message):
            nonlocal started
            if rejected:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, limited_receive, tracking_send)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        metrics.rejection("body_too_large")
        body = f'{{"detail":"request body is larger than {limit} bytes"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="governed-ai-gateway", version=__version__, lifespan=lifespan)
app.add_middleware(BodySizeLimit)


@app.get("/health")
async def health():
    return {"status": "ok", "aliases": sorted(current().aliases), "unpriced_models": unpriced_models()}


@app.post("/v1/chat/completions")
async def chat(body: ChatCompletionRequest, response: Response, key: KeyDep):
    token = traces.start(key.label, key.fingerprint, key.team, body.model, body.stream)
    trace_id = traces.current().id
    try:
        if body.stream:
            target, events, routed = await open_stream(key, body)
            traces.finish(200)
            return StreamingResponse(
                events,
                media_type="text/event-stream",
                headers={
                    "x-router-used-provider": target.provider,
                    "x-router-used-model": target.model,
                    **routed,
                    "x-router-trace-id": trace_id,
                    "cache-control": "no-cache",
                },
            )
        outcome = await route(key, body)
        traces.finish(200)
        response.headers.update(outcome.headers())
        response.headers["x-router-trace-id"] = trace_id
        return outcome.data
    except HTTPException as e:
        traces.finish(e.status_code, str(e.detail))
        e.headers = {**(e.headers or {}), "x-router-trace-id": trace_id}
        raise
    except Exception as e:
        traces.finish(500, f"internal error ({type(e).__name__})")
        raise
    finally:
        traces.detach(token)


@app.post("/mcp/{server}")
async def mcp_proxy(server: str, request: Request, key: KeyDep):
    """MCP tool gateway (JSON-RPC over Streamable HTTP, request/response only). See router/mcp_gateway.py."""
    return await mcp_gateway.handle(server, request, key)


@app.api_route("/mcp/{server}", methods=["GET", "DELETE"])
async def mcp_unsupported(server: str, _key: KeyDep):
    """Server-initiated SSE streams and session deletion aren't proxied."""
    raise HTTPException(405, "only POST is supported by the MCP gateway", headers={"Allow": "POST"})


@app.get("/v1/models")
async def models(_key: KeyDep):
    """List aliases so OpenAI clients can discover them."""
    return {"object": "list", "data": [{"id": alias, "object": "model"} for alias in sorted(current().aliases)]}


# ---------- Admin ----------


def _actor(admin: ApiKey | None) -> str:
    return f"key:{admin.label}" if admin else "env-admin"


@app.post("/admin/reload")
async def admin_reload(_a: AdminDep):
    try:
        cfg, _pol = reload_all()
    except (ValidationError, ValueError, OSError) as e:
        audit.record("admin", "reload_rejected", subject=_actor(_a))
        raise HTTPException(400, f"routes or policies file rejected, previous config still active: {e}") from e
    _warn_unpriced()
    audit.record("admin", "reload", subject=_actor(_a), detail={"aliases": len(cfg.aliases)})
    return {"status": "reloaded", "aliases": sorted(cfg.aliases)}


@app.get("/admin/keys")
async def admin_list_keys(_a: AdminDep) -> list[ApiKey]:
    """Keys by id, display prefix and fingerprint. Secrets aren't stored, so they can't be listed."""
    return list_keys()


@app.post("/admin/keys")
async def admin_create_key(req: ApiKeyCreate, _a: AdminDep) -> ApiKeyCreated:
    """The response is the only time the key is shown: only a salted hash is stored."""
    created = create_key(label=req.label, team=req.team, is_admin=req.is_admin)
    audit.record(
        "admin", "key_created", team=req.team, subject=_actor(_a), detail={"label": req.label, "admin": req.is_admin}
    )
    return created


@app.delete("/admin/keys/{ident}")
async def admin_revoke(ident: str, _a: AdminDep):
    """Revoke by key id (key_...) or by the key itself."""
    if not revoke_key(ident):
        raise HTTPException(404, "no such key")
    audit.record("admin", "key_revoked", subject=_actor(_a))
    return {"status": "revoked"}


@app.get("/admin/usage/today")
async def admin_usage_today(_a: AdminDep):
    keys = list_keys()
    per_key = [{"label": k.label, "team": k.team, "spend_usd": round(spend_today(key=k.id), 4)} for k in keys]
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
    labels = {k.id: (k.label, k.team) for k in list_keys()}
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


@app.get("/admin/circuits")
async def admin_circuits(_a: AdminDep):
    """Circuit breaker state per deployment, recent transitions, and latency averages."""
    cfg = routing.sync_config()
    return {
        "breakers": routing.breakers.snapshot(),
        "transitions": routing.breakers.recent_transitions(50),
        "latency": routing.latency.snapshot(),
        "strategies": {alias: cfg.strategy(alias) for alias in sorted(cfg.aliases)},
        "config": cfg.resilience.model_dump(),
    }


@app.get("/admin/budgets")
async def admin_budgets(_a: AdminDep):
    """Org and team spend vs. daily/monthly caps, plus tokens in the current TPM window."""
    cfg = current()
    overrides = budget_overrides()
    teams = (
        [k.team for k in list_keys()] + list(cfg.budgets.teams) + [o["name"] for o in overrides if o["scope"] == "team"]
    )
    out = utilization(routing.hierarchy(cfg), teams, spend_for)
    out["tpm_in_use"] = routing.tpm.snapshot()
    out["overrides"] = overrides
    return out


class BudgetUpdate(BaseModel):
    daily_usd: float | None = Field(None, ge=0)
    monthly_usd: float | None = Field(None, ge=0)
    tpm: int | None = Field(None, ge=0)


def _budget_scope(scope: str, name: str) -> None:
    if scope not in ("org", "team", "key"):
        raise HTTPException(400, "scope must be org, team or key")
    if scope == "org" and name != "org":
        raise HTTPException(400, "the org scope's name is 'org'")
    if not name or len(name) > 100:
        raise HTTPException(400, "name must be 1-100 characters")


@app.put("/admin/budgets/{scope}/{name}")
async def admin_set_budget(scope: str, name: str, req: BudgetUpdate, _a: AdminDep):
    """Set caps for the org, a team or a key label; they win over routes.yaml `budgets:` (null = inherit)."""
    _budget_scope(scope, name)
    set_budget_override(scope, name, req.daily_usd, req.monthly_usd, req.tpm)
    routing.invalidate_budget_overrides()
    audit.record("admin", "budget_set", team=name if scope == "team" else "", subject=_actor(_a),
                 detail={"scope": scope, "name": name, **req.model_dump()})  # fmt: skip
    return {"status": "set", "scope": scope, "name": name, **req.model_dump()}


@app.delete("/admin/budgets/{scope}/{name}")
async def admin_clear_budget(scope: str, name: str, _a: AdminDep):
    _budget_scope(scope, name)
    if not clear_budget_override(scope, name):
        raise HTTPException(404, "no override for that scope and name")
    routing.invalidate_budget_overrides()
    audit.record("admin", "budget_cleared", team=name if scope == "team" else "", subject=_actor(_a),
                 detail={"scope": scope, "name": name})  # fmt: skip
    return {"status": "cleared"}


def _parse_day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(400, f"dates must be YYYY-MM-DD, got {value!r}") from e


@app.get("/admin/showback")
async def admin_showback(
    _a: AdminDep,
    start: str | None = None,
    end: str | None = None,
    group_by: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    """Showback / chargeback export: cost and unit metrics by team, key, model and provider (UTC dates)."""
    today = datetime.now(UTC).date()
    first, last = _parse_day(start, today.replace(day=1)), _parse_day(end, today)
    if first > last:
        raise HTTPException(400, "start must be on or before end")
    try:
        dims = showback.parse_group_by(group_by)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    rows = usage_rows(first.isoformat(), last.isoformat())
    grouped = showback.aggregate(rows, dims)
    if format == "csv":
        return PlainTextResponse(
            showback.to_csv(grouped, dims),
            media_type="text/csv",
            headers={"content-disposition": f'attachment; filename="showback_{first}_{last}.csv"'},
        )
    return {
        "period": {"start": first.isoformat(), "end": last.isoformat(), "timezone": "UTC"},
        "group_by": list(dims),
        "rows": grouped,
        "totals": showback.totals(rows),
    }


@app.get("/admin/policies")
async def admin_policies(_a: AdminDep, team: str | None = None, alias: str | None = None):
    """The active policy set; with team and alias, the effective (combined) rule for that pair."""
    from dataclasses import asdict

    from .policy import effective_rule

    pol = current_policies()
    out = {
        "defaults": asdict(pol.defaults),
        "teams": {t: asdict(r) for t, r in pol.teams.items()},
        "routes": {a: asdict(r) for a, r in pol.routes.items()},
        "opa": {**asdict(pol.opa), "url_set": bool(settings.OPA_URL)},
    }
    if team is not None and alias is not None:
        rule, layers = effective_rule(pol, team, alias)
        out["effective"] = {"team": team, "alias": alias, "layers": layers, "rule": asdict(rule)}
    return out


@app.get("/admin/mcp")
async def admin_mcp(_a: AdminDep):
    """MCP servers (no credentials) and each team's allowed tools with their limits."""
    from dataclasses import asdict

    pol = current_mcp()
    return {
        "servers": {
            n: {"url": s.url, "timeout_s": s.timeout_s, "auth_headers": sorted(s.headers_from_env)}
            for n, s in pol.servers.items()
        },
        "defaults": asdict(pol.defaults),
        "teams": {
            t: [{"server": e.server, "tool": e.tool, **asdict(e.limits)} for e in entries]
            for t, entries in pol.teams.items()
        },
    }


@app.get("/admin/semantic-cache")
async def admin_semantic_cache(_a: AdminDep):
    """Semantic-cache settings and size. Entries live in this worker's memory only."""
    cfg = routing.sync_config()
    return {
        "config": cfg.semantic_cache.model_dump(),
        "entries": routing.semantic.size(),
        "partitions": routing.semantic.partitions(),
        "scope": "process-local",
    }


@app.get("/admin/shadow")
async def admin_shadow(_a: AdminDep, days: int = 7):
    """Shadow-mode comparison per (alias, candidate): agreement, errors, cost and latency vs. the live route."""
    cfg = current()
    return {
        "config": {alias: sh.model_dump() for alias, sh in cfg.shadow.items()},
        "comparisons": shadow_summary(min(max(days, 1), 90)),
    }


@app.get("/admin/audit")
async def admin_audit(
    _a: AdminDep, category: str | None = None, team: str | None = None, limit: int = 100, exclude_allow: bool = False
):
    """Policy audit trail, newest first. Rows hold decisions, counts and key fingerprints, never content."""
    return audit.rows(category, team, min(max(limit, 1), 1000), exclude_allow)


@app.get("/admin/audit/verify")
async def admin_audit_verify(_a: AdminDep):
    """Recompute the audit hash chain; reports the first row that was altered, inserted or removed."""
    return audit.verify()


@app.get("/admin/content-log")
async def admin_content_log(_a: AdminDep, team: str | None = None, limit: int = 50):
    """Redacted prompts/completions for teams that opted in (privacy.teams.<team>.log_content). Empty by default."""
    return content_log_rows(team, min(max(limit, 1), 500))


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(_a: AdminDep):
    """Prometheus exposition format. Configure the scraper with the admin key as a bearer token."""
    routing.sync_config()
    text = metrics.render(
        breakers=routing.breakers.snapshot(), latency=routing.latency.snapshot(), tpm=routing.tpm.snapshot()
    )
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


# ---------- Console (live mode) ----------


def _key_by_ident(ident: str) -> ApiKey:
    for k in list_keys():
        if k.id == ident:
            return k
    raise HTTPException(404, "no such key")


@app.get("/admin/key-holds")
async def admin_key_holds(_a: AdminDep):
    """Keys an admin paused, or let through an anomaly pause for the rest of the hour."""
    return key_holds()


@app.post("/admin/keys/{ident}/pause")
async def admin_pause_key(ident: str, _a: AdminDep):
    """Refuse every request from this key (429) until it is unpaused. Usable on any key, flagged or not."""
    k = _key_by_ident(ident)
    set_key_hold(k.id, "paused", _actor(_a))
    audit.record("admin", "key_paused", team=k.team, key_fp=k.fingerprint, subject=_actor(_a))
    return {"status": "paused", "id": k.id}


@app.post("/admin/keys/{ident}/unpause")
async def admin_unpause_key(ident: str, _a: AdminDep):
    """Lift an admin pause. If the anomaly check would pause the key, let it through until the end of this UTC hour."""
    from .anomaly import evaluate

    k = _key_by_ident(ident)
    had_hold = clear_key_hold(k.id)
    signal = evaluate(k.id)
    if current().policies.auto_pause_on_anomaly and signal.verdict == "pause":
        until = end_of_this_hour()
        set_key_hold(k.id, "override", _actor(_a), until)
        audit.record("admin", "anomaly_override", team=k.team, key_fp=k.fingerprint, subject=_actor(_a),
                     detail={"until": until, "multiple": round(signal.multiple, 2)})  # fmt: skip
        return {"status": "override", "id": k.id, "until": until}
    if not had_hold:
        return {"status": "not_paused", "id": k.id}
    audit.record("admin", "key_unpaused", team=k.team, key_fp=k.fingerprint, subject=_actor(_a))
    return {"status": "unpaused", "id": k.id}


@app.get("/admin/routes")
async def admin_routes(_a: AdminDep):
    """Aliases with their strategy and deployments (what the console's playground offers)."""
    cfg = current()
    return {
        alias: {"strategy": cfg.strategy(alias), "targets": [f"{t.provider}/{t.model}" for t in targets]}
        for alias, targets in sorted(cfg.aliases.items())
    }


@app.get("/admin/overview")
async def admin_overview(_a: AdminDep):
    """Today's spend by team and model, hourly spend vs. the 7-day average, cache, anomalies, breakers."""
    return console.overview()


@app.get("/admin/traces")
async def admin_traces(
    _a: AdminDep, limit: int = 100, team: str | None = None, outcome: str | None = None, q: str | None = None
):
    """Recent request traces in this worker (metadata only), newest first."""
    return traces.rows(min(max(limit, 1), 1000), team or None, outcome or None, q)


@app.get("/admin/traces/{trace_id}")
async def admin_trace(trace_id: str, _a: AdminDep):
    tr = traces.get(trace_id)
    if tr is None:
        raise HTTPException(404, "no such trace (traces are kept in memory, per worker)")
    return tr


class ConfigText(BaseModel):
    text: str = Field(max_length=262_144)


def _config_name(name: str) -> str:
    if name not in console.CONFIG_NAMES:
        raise HTTPException(404, f"unknown config {name!r}; one of {list(console.CONFIG_NAMES)}")
    return name


@app.get("/admin/config")
async def admin_config_list(_a: AdminDep):
    return [{k: v for k, v in console.read_config(n).items() if k != "text"} for n in console.CONFIG_NAMES]


@app.get("/admin/config/{name}")
async def admin_config_read(name: str, _a: AdminDep):
    """The raw YAML of the policies, routes or MCP file (these files hold no secrets: credentials come from env)."""
    return console.read_config(_config_name(name))


@app.post("/admin/config/{name}/validate")
async def admin_config_validate(name: str, req: ConfigText, _a: AdminDep):
    """Validate YAML with the gateway's own loader without applying it."""
    return console.validate(_config_name(name), req.text)


@app.put("/admin/config/{name}")
async def admin_config_apply(name: str, req: ConfigText, _a: AdminDep):
    """Replace the file and reload routes, policies and MCP config together. Off unless ROUTER_ALLOW_CONFIG_WRITES."""
    _config_name(name)
    if not settings.ROUTER_ALLOW_CONFIG_WRITES:
        raise HTTPException(403, "config writes are disabled; set ROUTER_ALLOW_CONFIG_WRITES=true to allow them")
    try:
        result = console.apply(name, req.text)
    except console.ConfigRejected as e:
        audit.record("admin", "config_rejected", subject=_actor(_a), detail={"file": name})
        return JSONResponse({"ok": False, "errors": e.errors}, status_code=400)
    _warn_unpriced()
    audit.record("admin", "config_applied", subject=_actor(_a), detail={"file": name, "sha256": result["sha256"][:16],
                 "sha256_before": (result["sha256_before"] or "")[:16]})  # fmt: skip
    return {"ok": True, **result}


class DryRun(BaseModel):
    teams: list[str] | None = None
    aliases: list[str] | None = None
    max_tokens: int | None = Field(None, ge=1)
    text: str | None = Field(None, max_length=262_144)  # candidate policies.yaml; default: the active policy


@app.post("/admin/policies/dry-run")
async def admin_policies_dry_run(req: DryRun, _a: AdminDep):
    """Policy decisions for team x alias pairs, against the active policy or a candidate file. Calls nothing."""
    candidate = None
    if req.text is not None:
        try:
            candidate = console.parse("policies", req.text)
        except console.ConfigRejected as e:
            return JSONResponse({"ok": False, "errors": e.errors}, status_code=400)
    teams = req.teams or console.known_teams()
    aliases = req.aliases or sorted(current().aliases)
    if len(teams) * len(aliases) > 2500:
        raise HTTPException(400, "too many combinations")
    return {"ok": True, "results": console.dry_run(teams, aliases, req.max_tokens, candidate)}


@app.post("/admin/circuits/reset")
async def admin_circuits_reset(_a: AdminDep):
    """Close every breaker and forget their history (this worker)."""
    routing.breakers.reset()
    audit.record("admin", "circuits_reset", subject=_actor(_a))
    return {"status": "reset"}


@app.get("/admin/mock/providers")
async def admin_mock_providers(_a: AdminDep):
    """Simulated-provider knobs (only used when ROUTER_MOCK_PROVIDERS is on)."""
    return {"enabled": settings.ROUTER_MOCK_PROVIDERS, "providers": mock_provider.snapshot()}


@app.put("/admin/mock/providers/{provider}")
async def admin_mock_provider_update(provider: str, _a: AdminDep, changes: Annotated[dict, Body()]):
    if not settings.ROUTER_MOCK_PROVIDERS:
        raise HTTPException(409, "simulated providers are off (ROUTER_MOCK_PROVIDERS=false)")
    try:
        state = mock_provider.update(provider, changes)
    except KeyError:
        raise HTTPException(404, f"unknown provider {provider!r}") from None
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    return {"provider": provider, **state}


@app.post("/mock/mcp/{server}")
async def mock_mcp_server(server: str, request: Request):
    """A simulated MCP server (tools/list, tools/call) for the MCP gateway to call when providers are mocked."""
    if not settings.ROUTER_MOCK_PROVIDERS:
        raise HTTPException(404, "Not Found")
    try:
        msg = await request.json()
    except ValueError:
        raise HTTPException(400, "expected one JSON-RPC message") from None
    reply = mock_provider.mcp_response(server, msg if isinstance(msg, dict) else {})
    if reply is None:
        return Response(status_code=202)
    return reply


CONSOLE_DIR = Path(__file__).resolve().parent.parent / "demo"


@app.get("/console/api-mode")
async def console_api_mode():
    """Tells the console it is served by a gateway (live mode). Public: no counts, names or keys."""
    return {
        "mode": "live",
        "product": "Governed AI Gateway",
        "version": __version__,
        "mock_providers": settings.ROUTER_MOCK_PROVIDERS,
        "config_writes": settings.ROUTER_ALLOW_CONFIG_WRITES,
        "auth": "admin key (Settings)",
    }


# ---------- Dashboard ----------


@app.get("/dashboard")
async def dashboard():
    html = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
    return FileResponse(str(html), media_type="text/html")


if settings.ROUTER_CONSOLE and CONSOLE_DIR.is_dir():
    app.mount("/console", StaticFiles(directory=str(CONSOLE_DIR), html=True), name="console")
