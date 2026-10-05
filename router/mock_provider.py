# Corey Mathie, 2026
"""
Simulated providers, so the gateway runs end to end with no API keys (ROUTER_MOCK_PROVIDERS=true).

When the setting is on, every chat completion the gateway would send through LiteLLM is answered
here instead: an OpenAI-shaped response (or stream of chunks) whose text says it is simulated and
echoes what the provider would have received, after the gateway's content hooks. Nothing leaves
the process. Token counts are estimated from the text (about 4 characters per token) and priced
by the gateway as usual, so costs in this mode are simulated too.

Each provider has knobs an admin can change at runtime (GET/PUT /admin/mock/providers), used by
the console's Playground to take a provider down and watch the circuit breakers and fallback:

    latency_ms     simulated response time (slept, so latency routing sees it)
    error_rate     share of calls that fail with a simulated 503
    outage         every call fails as a connection timeout
    rate_limited   every call fails with a simulated 429 and Retry-After: 8

The same module serves a tiny simulated MCP server at /mock/mcp/{server} (see main.py), so the MCP
tool gateway can be exercised without real tool servers (config/mcp.mock.yaml points at it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

PROVIDERS = ("openai", "anthropic", "gemini", "ollama")
DEFAULT_LATENCY_MS = {"openai": 240.0, "anthropic": 320.0, "gemini": 260.0, "ollama": 650.0}
OUTAGE_DELAY_S = 0.3  # how long a simulated outage takes to fail (a short stand-in for a timeout)


class MockProviderError(Exception):
    """A simulated provider failure, shaped like a LiteLLM error (status_code, retry_after)."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class ProviderState:
    latency_ms: float
    error_rate: float = 0.0
    outage: bool = False
    rate_limited: bool = False
    calls: int = 0
    failures: int = 0


_state: dict[str, ProviderState] = {}


def reset() -> None:
    _state.clear()
    for p in PROVIDERS:
        _state[p] = ProviderState(DEFAULT_LATENCY_MS[p])


reset()


def snapshot() -> dict[str, dict]:
    return {p: asdict(s) for p, s in sorted(_state.items())}


def update(provider: str, changes: dict[str, Any]) -> dict:
    if provider not in _state:
        raise KeyError(provider)
    s = _state[provider]
    unknown = set(changes) - {"latency_ms", "error_rate", "outage", "rate_limited"}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    if "latency_ms" in changes:
        s.latency_ms = min(max(0.0, float(changes["latency_ms"])), 10_000.0)
    if "error_rate" in changes:
        s.error_rate = min(max(0.0, float(changes["error_rate"])), 1.0)
    for flag in ("outage", "rate_limited"):
        if flag in changes:
            setattr(s, flag, bool(changes[flag]))
    return asdict(s)


def _unit(*parts: str) -> float:
    """Deterministic number in [0, 1) from the inputs (no global random state)."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _reply(provider: str, model: str, messages: list[dict]) -> str:
    last = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
    words = len(str(last).split())
    excerpt = str(last)[:160] + ("…" if len(str(last)) > 160 else "")
    return (
        f"[simulated {provider}/{model}] No model was called (ROUTER_MOCK_PROVIDERS is on). "
        f'The provider received {len(messages)} message(s); the last one has {words} words: "{excerpt}"'
    )


async def _fail_or_wait(provider: str, model: str, messages: list[dict]) -> None:
    s = _state.get(provider) or ProviderState(DEFAULT_LATENCY_MS.get(provider, 300.0))
    s.calls += 1
    if s.outage:
        await asyncio.sleep(OUTAGE_DELAY_S)
        s.failures += 1
        raise MockProviderError(f"simulated outage: {provider} did not respond")
    if s.rate_limited:
        await asyncio.sleep(0.02)
        s.failures += 1
        raise MockProviderError("simulated 429: rate limited", status_code=429, retry_after=8)
    seed = f"{provider}/{model}|{s.calls}|{json.dumps(messages, sort_keys=True)[:200]}"
    jitter = 0.85 + 0.3 * _unit(seed, "latency")
    if s.error_rate and _unit(seed, "error") < s.error_rate:
        await asyncio.sleep(s.latency_ms / 1000 * 0.4 * jitter)
        s.failures += 1
        raise MockProviderError("simulated 503: upstream error", status_code=503)
    await asyncio.sleep(s.latency_ms / 1000 * jitter)


def _split_model(model: str) -> tuple[str, str]:
    provider, _, name = model.partition("/")
    return provider, name


async def acompletion(model: str, messages: list[dict], max_tokens: int | None = None, stream: bool = False, **_kw):
    """Drop-in for litellm.acompletion with the arguments routing.py passes."""
    provider, name = _split_model(model)
    await _fail_or_wait(provider, name, messages)
    text = _reply(provider, name, messages)
    if max_tokens:
        text = text[: max(1, int(max_tokens)) * 4]
    pt = sum(estimate_tokens(str(m.get("content") or "")) + 4 for m in messages)
    ct = estimate_tokens(text)
    created = int(time.time())
    rid = "chatcmpl-sim-" + hashlib.sha256(f"{model}{created}{pt}{text}".encode()).hexdigest()[:12]
    if stream:
        return _stream(rid, created, model, text, pt, ct)
    return {
        "id": rid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        "simulated": True,
    }


async def _stream(rid: str, created: int, model: str, text: str, pt: int, ct: int):
    words = text.split(" ")
    for i, w in enumerate(words):
        piece = w if i == 0 else " " + w
        yield {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        await asyncio.sleep(0)
    yield {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


# ---------- simulated MCP servers ----------

MCP_TOOLS = {
    "tickets": {
        "search_tickets": "Search support tickets (simulated)",
        "get_ticket": "Fetch one ticket by id (simulated)",
        "close_ticket": "Close a ticket with a note (simulated)",
        "reassign_ticket": "Move a ticket to another queue (simulated)",
    },
    "files": {
        "read_file": "Read a file from the shared drive (simulated)",
        "delete_file": "Delete a file (simulated)",
    },
}


def mcp_response(server: str, msg: dict) -> dict | None:
    """Answer one JSON-RPC message as a simulated MCP server. None for notifications."""
    msg_id = msg.get("id")
    method = msg.get("method")
    if msg_id is None:
        return None
    if server not in MCP_TOOLS:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"no simulated server {server}"}}
    if method == "initialize":
        result: dict = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"simulated-{server}", "version": "0.1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": n, "description": d, "inputSchema": {"type": "object"}} for n, d in MCP_TOOLS[server].items()
            ]
        }
    elif method == "tools/call":
        params = msg.get("params") or {}
        tool = params.get("name")
        if tool not in MCP_TOOLS[server]:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"unknown tool {tool}"}}
        keys = sorted((params.get("arguments") or {}).keys())
        text = f"(simulated {server} server) {tool} ok; argument keys: {', '.join(keys) or 'none'}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method {method} not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
