# Corey Mathie, 2026
"""
Append-only, hash-chained audit trail of policy actions.

Every row stores the SHA-256 of the previous row (`prev_hash`) and its own
hash over (prev_hash, ts, category, action, team, key_fp, subject, detail).
`verify()` walks the chain and reports the first row that doesn't match, so an
edited, inserted or deleted row is detectable. SQLite triggers refuse UPDATE
and DELETE on the table. This makes tampering *evident*, not impossible:
anyone with write access to the database file can rebuild the whole chain.
Ship rows to an external store (SIEM, WORM bucket) for stronger guarantees.

Categories and actions written by the gateway:
  privacy  redact | block | detect | content_logged | hook_error
  policy   allow | deny | error          (one row per request that reaches policy evaluation)
  admin    key_created | key_revoked | reload | reload_rejected
  mcp      allow | deny | rate_limited | cap_reached | upstream_error
  shadow   skipped_cap | skipped_policy   (mirrored calls themselves go to shadow_usage)

Rows never contain prompt or completion text, tool arguments in clear, or raw
keys: only counts, names, decisions and key fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import logging

from . import metrics
from .store import connect, now_utc

log = logging.getLogger("router")
GENESIS = "0" * 64


def _row_hash(prev: str, ts: str, category: str, action: str, team: str, key_fp: str, subject: str, detail: str) -> str:
    material = "\x1f".join((prev, ts, category, action, team, key_fp, subject, detail))
    return hashlib.sha256(material.encode()).hexdigest()


def record(
    category: str, action: str, *, team: str = "", key_fp: str = "", subject: str = "", detail: dict | None = None
) -> None:
    """Append one audit row. The previous hash is read inside the same write transaction."""
    payload = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"), default=str)
    ts = now_utc()
    c = connect()
    c.isolation_level = None  # manage the transaction explicitly
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["row_hash"] if row else GENESIS
        h = _row_hash(prev, ts, category, action, team, key_fp, subject, payload)
        c.execute(
            "INSERT INTO audit_log (ts, category, action, team, key_fp, subject, detail, prev_hash, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, category, action, team, key_fp, subject, payload, prev, h),
        )
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    metrics.policy_action(category, action)


def rows(
    category: str | None = None, team: str | None = None, limit: int = 100, exclude_allow: bool = False
) -> list[dict]:
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if exclude_allow:  # routine policy "allow" decisions, one per request
        sql += " AND NOT (category = 'policy' AND action = 'allow')"
    if category:
        sql += " AND category = ?"
        args.append(category)
    if team:
        sql += " AND team = ?"
        args.append(team)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with connect() as c:
        out = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in out:
        r["detail"] = json.loads(r["detail"])
    return out


def verify() -> dict:
    """Recompute the chain. Returns {"ok", "rows", "first_bad_id"}."""
    prev = GENESIS
    n = 0
    with connect() as c:
        for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
            n += 1
            expected = _row_hash(
                prev, r["ts"], r["category"], r["action"], r["team"], r["key_fp"], r["subject"], r["detail"]
            )
            if r["prev_hash"] != prev or r["row_hash"] != expected:
                return {"ok": False, "rows": n, "first_bad_id": r["id"]}
            prev = r["row_hash"]
    return {"ok": True, "rows": n, "first_bad_id": None}
