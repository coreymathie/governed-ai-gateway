# Corey Mathie, 2026
"""
Optional Open Policy Agent check, after the local policy (router/policy.py).

When OPA_URL is set, every request the local policy allows is also sent to
`POST {OPA_URL}/v1/data/{policies.opa.path}` with `{"input": ...}` (request
metadata only: team, key label, alias, permitted deployments, max_tokens,
sizes; never message content). The expected result is

    {"allow": true|false, "reasons": [...], "allowed_targets": ["provider/model", ...]}

`allowed_targets` is optional and can only narrow the route. Any failure
(timeout, connection error, non-2xx, undefined or malformed result) denies the
request: the gateway fails closed. See policies/router.rego for an example.
"""

from __future__ import annotations

from typing import Any

import httpx


class OpaError(Exception):
    pass


async def query(base_url: str, path: str, document: dict, timeout_s: float) -> Any:
    url = f"{base_url.rstrip('/')}/v1/data/{path.strip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, json={"input": document})
    except httpx.HTTPError as e:
        raise OpaError(f"OPA unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise OpaError(f"OPA returned HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as e:
        raise OpaError("OPA returned invalid JSON") from e
    return body.get("result") if isinstance(body, dict) else None
