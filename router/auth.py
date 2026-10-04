# Corey Mathie, 2026
from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, status

from .config_loader import current, settings
from .store import get_active_key

_calls: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(key: str) -> None:
    rpm = current().policies.rate_limit_rpm
    now = time.monotonic()
    q = _calls[key]
    while q and q[0] < now - 60.0:
        q.popleft()
    if len(q) >= rpm:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit exceeded ({rpm}/min)",
        )
    q.append(now)


async def require_key(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    rec = get_active_key(key)
    if not rec:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked key")
    _rate_limit(key)
    return rec


async def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    # Admin can be either the env var (compared in constant time) or an admin-flagged DB key
    if settings.ROUTER_ADMIN_KEY and hmac.compare_digest(token, settings.ROUTER_ADMIN_KEY):
        return None
    rec = get_active_key(token)
    if not rec or not rec.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required")
    return rec
