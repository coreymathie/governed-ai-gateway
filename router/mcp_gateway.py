# Corey Mathie, 2026
"""
MCP tool gateway: a policy-enforcing proxy in front of Model Context Protocol
servers, at POST /mcp/{server}.

Scope (what is and isn't supported):
  - Transport: the request/response part of MCP's Streamable HTTP transport.
    Clients POST one JSON-RPC 2.0 message; the gateway POSTs it upstream and
    accepts either an application/json reply or a text/event-stream reply,
    from which it takes the JSON-RPC response with the matching id. Batches,
    GET (server-initiated SSE streams), DELETE (session end), resumability and
    server-to-client requests (sampling, elicitation) are not proxied; stdio
    servers can't be reached. Mcp-Session-Id and MCP-Protocol-Version headers
    are passed through.
  - Methods: initialize, ping, notifications/*, tools/list (filtered to the
    caller's allowed tools) and tools/call (policy-checked). Everything else
    (resources/*, prompts/*, completion/*, logging/*) is refused: -32601.
  - Auth: clients authenticate with their gateway key (bearer), which carries
    their team. The gateway authenticates to upstream servers with static
    headers read from environment variables (`headers_from_env`); there is no
    OAuth flow and no per-user upstream identity.

Every tools/call decision (allow, deny, rate_limited, cap_reached,
upstream_error) is an audit row with team, key fingerprint, server/tool, the
argument keys and, by default, PII-redacted argument values. Allowed calls are
recorded in the usage table (provider "mcp", model "server/tool", alias
"mcp:server") with the configured per-call cost, so they appear in showback and
daily caps survive restarts. Velocity windows are per process. Anything that
goes wrong between the gateway and the upstream server is a JSON-RPC error
(-32004) to the client, never a partial result.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import audit, metrics
from .config_loader import current, current_mcp
from .mcp_policy import ERR_UPSTREAM, Server, VelocityLimiter, check_call, filter_tools, redact_args
from .models import ApiKey
from .privacy import key_fp
from .store import record_call, tool_usage_today

log = logging.getLogger("router")
limiter = VelocityLimiter()
PASS_HEADERS = ("mcp-session-id", "mcp-protocol-version")
LOCAL_METHODS = {"initialize", "ping", "tools/list", "tools/call"}


class UpstreamError(Exception):
    pass


def _rpc_error(msg_id: Any, code: int, message: str, data: dict | None = None, status: int = 200, headers=None):
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": err}, status_code=status, headers=headers)


def _sse_response(text: str, msg_id: Any) -> dict:
    """Pick the JSON-RPC response with our id out of an SSE body (events separated by blank lines)."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == msg_id and ("result" in msg or "error" in msg):
            return msg
    raise UpstreamError("no response with a matching id in the event stream")


async def forward(server: Server, payload: dict, request_headers: dict[str, str]) -> tuple[dict | None, dict]:
    """POST one JSON-RPC message upstream. Returns (response or None for 202, headers to pass back)."""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    for h in PASS_HEADERS:
        if request_headers.get(h):
            headers[h] = request_headers[h]
    for header, env in server.headers_from_env.items():
        value = os.environ.get(env)
        if not value:
            raise UpstreamError(f"credential variable {env} is not set")  # fail closed rather than call unauthenticated
        headers[header] = value
    try:
        async with httpx.AsyncClient(timeout=server.timeout_s) as client:
            r = await client.post(server.url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise UpstreamError(f"{type(e).__name__}") from e
    back = {h: r.headers[h] for h in PASS_HEADERS if h in r.headers}
    if r.status_code == 202:
        return None, back
    if r.status_code >= 400:
        raise UpstreamError(f"HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        return _sse_response(r.text, payload.get("id")), back
    try:
        body = r.json()
    except ValueError as e:
        raise UpstreamError("invalid JSON") from e
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise UpstreamError("not a JSON-RPC 2.0 response")
    return body, back


def _audit(key: ApiKey, server: str, tool: str, action: str, detail: dict) -> None:
    metrics.tool_call(server, tool, action)
    audit.record("mcp", action, team=key.team, key_fp=key_fp(key), subject=f"{server}/{tool}", detail=detail)


async def handle(server_name: str, request: Request, key: ApiKey) -> Response:
    policy = current_mcp()
    server = policy.servers.get(server_name)
    if server is None:
        raise HTTPException(404, f"no MCP server named {server_name!r} is configured")
    try:
        msg = json.loads(await request.body())
    except ValueError:
        return _rpc_error(None, -32700, "parse error", status=400)
    if isinstance(msg, list):
        return _rpc_error(None, -32600, "JSON-RPC batches are not supported by this gateway", status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(
            msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid JSON-RPC request", status=400
        )
    method, msg_id = msg["method"], msg.get("id")
    if not policy.team_can_use_server(key.team, server_name):
        _audit(key, server_name, "*", "deny", {"method": method, "reason": "no tools allowed on this server"})
        return _rpc_error(msg_id, -32001, f"team {key.team!r} has no access to MCP server {server_name!r}", status=403)
    req_headers = {k.lower(): v for k, v in request.headers.items()}

    if method.startswith("notifications/"):
        try:
            await forward(server, msg, req_headers)
        except UpstreamError as e:
            log.warning("MCP notification to %s failed: %s", server_name, e)
        return Response(status_code=202)
    if method not in LOCAL_METHODS:
        return _rpc_error(msg_id, -32601, f"method {method!r} is not available through the gateway")
    if method == "tools/call":
        return await _tool_call(server, msg, key, req_headers)

    try:
        body, back = await forward(server, msg, req_headers)
    except UpstreamError as e:
        metrics.tool_call(server_name, method, "upstream_error")
        log.warning("MCP %s to %s failed: %s", method, server_name, e)
        return _rpc_error(msg_id, ERR_UPSTREAM, "upstream MCP server unavailable")
    if body is None:
        return Response(status_code=202, headers=back)
    if method == "tools/list" and isinstance(body.get("result"), dict):
        tools = body["result"].get("tools") or []
        body["result"] = {**body["result"], "tools": filter_tools(policy, key.team, server_name, tools)}
    return JSONResponse(body, headers=back)


async def _tool_call(server: Server, msg: dict, key: ApiKey, req_headers: dict[str, str]) -> Response:
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    tool = params.get("name")
    if not isinstance(tool, str) or not tool:
        return _rpc_error(msg_id, -32602, "tools/call needs params.name")
    arguments = params.get("arguments") or {}
    policy = current_mcp()
    entry = policy.entry(key.team, server.name, tool)
    used = tool_usage_today(key.team, f"{server.name}/{tool}") if entry else (0, 0.0)
    decision = check_call(policy, limiter, key.team, key_fp(key), server.name, tool, arguments, used)
    lim = decision.entry.limits if decision.entry else policy.defaults
    shown, findings = redact_args(arguments, tuple(current().privacy.kinds)) if lim.redact_args else (arguments, {})
    detail = {
        "arg_keys": sorted(arguments) if isinstance(arguments, dict) else [],
        "args": shown if lim.redact_args else "(not logged)",
        "redactions": findings,
    }
    headers = {"x-router-mcp-decision": decision.action}
    if not decision.allow:
        if decision.retry_after:
            headers["Retry-After"] = str(int(decision.retry_after))
        _audit(key, server.name, tool, decision.action, {**detail, "reason": decision.reason})
        return _rpc_error(msg_id, decision.code, decision.reason, {"decision": decision.action}, headers=headers)

    limiter.record(decision.scopes)
    upstream_msg = msg
    if lim.forward_redacted:
        upstream_msg = {**msg, "params": {**params, "arguments": redact_args(arguments)[0]}}
    started = time.perf_counter()
    try:
        body, back = await forward(server, upstream_msg, req_headers)
        if body is None:
            raise UpstreamError("no response to tools/call")
    except UpstreamError as e:
        _audit(key, server.name, tool, "upstream_error", {**detail, "error": str(e)[:200]})
        record_call(key.id, key.team, f"mcp:{server.name}", "mcp", f"{server.name}/{tool}", 0, 0, 0.0,
                    error=f"upstream: {str(e)[:150]}")  # fmt: skip
        headers["x-router-mcp-decision"] = "upstream_error"
        return _rpc_error(msg_id, ERR_UPSTREAM, "upstream MCP server unavailable", headers=headers)
    latency = time.perf_counter() - started
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    is_error = bool(result.get("isError")) or "error" in body
    record_call(key.id, key.team, f"mcp:{server.name}", "mcp", f"{server.name}/{tool}", 0, 0, lim.cost_usd,
                error="tool returned an error" if is_error else None)  # fmt: skip
    _audit(key, server.name, tool, "allow",
           {**detail, "latency_s": round(latency, 4), "is_error": is_error, "cost_usd": lim.cost_usd})  # fmt: skip
    return JSONResponse(body, headers={**back, **headers})
