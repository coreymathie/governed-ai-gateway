# Corey Mathie, 2026
"""
SQLite persistence for API keys and per-call usage.

Timestamps are stored as UTC in SQLite's native "YYYY-MM-DD HH:MM:SS" format and
always compared through datetime(), so time-window queries (this hour, last 60
minutes, last 7 days) are correct. "Today" means the current UTC day.

Application keys are never stored: each row keeps a random id (what usage rows
reference), a display prefix ("sk-router-" + 4 characters), a fingerprint (first
8 hex of SHA-256 of the key, used in exports and the audit trail), a random salt,
and HMAC-SHA256(pepper, salt + key). The key itself is returned once, at
creation. A lookup selects rows by prefix and compares hashes in constant time.
A fast hash is appropriate here because keys are 256-bit random tokens, not
passwords; ROUTER_KEY_PEPPER (optional) keeps a stolen database from being
checked against guessed keys without the process environment. Databases from
0.5.x and earlier are migrated by init_db(): plaintext keys are hashed, usage
rows are re-pointed from the key to its id, and the file is vacuumed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config_loader import settings
from .models import ApiKey, ApiKeyCreated

_API_KEYS_TABLE = """CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    key_prefix TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    salt TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    label TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT 'default',
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT
)"""
_API_KEYS_INDEX = "CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix)"

_SCHEMA = (
    _API_KEYS_TABLE
    + ";\n"
    + _API_KEYS_INDEX
    + """;
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    team TEXT NOT NULL,
    alias TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    error TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage(key, ts);
CREATE INDEX IF NOT EXISTS idx_usage_team_ts ON usage(team, ts);
CREATE TABLE IF NOT EXISTS cache (
    team TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    alias TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (team, key_hash)
);
-- Policy audit trail (router/audit.py). Each row carries the hash of the previous row, so an edit or a
-- deleted row breaks the chain; triggers refuse UPDATE and DELETE through SQLite.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT '',
    key_fp TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_cat_ts ON audit_log(category, ts);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
-- Prompt/response content, written only for teams that opt in (privacy.teams.<team>.log_content), always redacted.
CREATE TABLE IF NOT EXISTS content_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    team TEXT NOT NULL,
    key_fp TEXT NOT NULL,
    alias TEXT NOT NULL,
    request_redacted TEXT NOT NULL,
    response_redacted TEXT NOT NULL,
    redactions TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_content_team_ts ON content_log(team, ts);
-- Shadow traffic (router/shadow.py): a separate ledger, never counted toward budgets, anomalies or showback.
CREATE TABLE IF NOT EXISTS shadow_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    team TEXT NOT NULL,
    key_fp TEXT NOT NULL,
    alias TEXT NOT NULL,
    candidate TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    latency_s REAL,
    primary_deployment TEXT NOT NULL,
    primary_cost_usd REAL NOT NULL,
    primary_latency_s REAL NOT NULL,
    agreement REAL,
    exact_match INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_usage(ts);
"""
)

# Columns added after 0.3.0. init_db() adds them to existing databases.
_USAGE_MIGRATIONS = {
    "cached": "ALTER TABLE usage ADD COLUMN cached INTEGER NOT NULL DEFAULT 0",
    "saved_usd": "ALTER TABLE usage ADD COLUMN saved_usd REAL NOT NULL DEFAULT 0",
}


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    """Open a connection to the router database (rows accessible by column name)."""
    Path(settings.ROUTER_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.ROUTER_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


PREFIX_LEN = 14  # "sk-router-" + 4 characters: enough to recognise a key, useless for guessing it


def init_db() -> None:
    migrate_plaintext_keys()  # before the schema script, which indexes columns the old table lacks
    with connect() as c:
        c.executescript(_SCHEMA)
        existing = {r["name"] for r in c.execute("PRAGMA table_info(usage)")}
        for column, ddl in _USAGE_MIGRATIONS.items():
            if column not in existing:
                c.execute(ddl)


def key_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def _hash(secret: str, salt: str) -> str:
    pepper = settings.ROUTER_KEY_PEPPER.encode()
    return hmac.new(pepper, bytes.fromhex(salt) + secret.encode(), hashlib.sha256).hexdigest()


def _new_record(secret: str) -> dict:
    salt = secrets.token_hex(16)
    return {
        "id": "key_" + secrets.token_hex(8),
        "key_prefix": secret[:PREFIX_LEN],
        "fingerprint": key_fingerprint(secret),
        "salt": salt,
        "key_hash": _hash(secret, salt),
    }


def migrate_plaintext_keys() -> int:
    """Hash keys stored in plaintext by 0.5.x and earlier. Returns how many keys were migrated (0 if none)."""
    c = connect()
    c.isolation_level = None
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(api_keys)")}
        if "key" not in cols or "key_hash" in cols:
            return 0
        c.execute("PRAGMA secure_delete = ON")  # overwrite the plaintext pages instead of just unlinking them
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM api_keys").fetchall()
        c.execute("ALTER TABLE api_keys RENAME TO api_keys_plaintext_v05")
        c.execute(_API_KEYS_TABLE)
        c.execute(_API_KEYS_INDEX)
        has_usage = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage'").fetchone()
        for r in old:
            rec = _new_record(r["key"])
            c.execute(
                "INSERT INTO api_keys (id, key_prefix, fingerprint, salt, key_hash, label, team, is_admin, created_at, "
                "revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*rec.values(), r["label"], r["team"], r["is_admin"], r["created_at"], r["revoked_at"]),
            )
            if has_usage:
                c.execute("UPDATE usage SET key = ? WHERE key = ?", (rec["id"], r["key"]))
        if has_usage:  # usage rows of keys that no longer exist: replace the secret with a stable placeholder
            for (raw,) in c.execute("SELECT DISTINCT key FROM usage WHERE key LIKE 'sk-router-%'").fetchall():
                c.execute("UPDATE usage SET key = ? WHERE key = ?", ("unknown_" + key_fingerprint(raw), raw))
        c.execute("DROP TABLE api_keys_plaintext_v05")
        c.execute("COMMIT")
        c.execute("VACUUM")  # rebuild the file so no free page still holds a plaintext key
        return len(old)
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _row_to_key(r: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=r["id"],
        key_prefix=r["key_prefix"],
        fingerprint=r["fingerprint"],
        label=r["label"],
        team=r["team"],
        is_admin=bool(r["is_admin"]),
        created_at=r["created_at"],
        revoked_at=r["revoked_at"],
    )


def create_key(label: str, team: str = "default", is_admin: bool = False) -> ApiKeyCreated:
    """Create a key. The returned object carries the secret (`key`); nothing that can recover it is stored."""
    secret = "sk-router-" + secrets.token_urlsafe(32)
    rec = _new_record(secret)
    created = now_utc()
    with connect() as c:
        c.execute(
            "INSERT INTO api_keys (id, key_prefix, fingerprint, salt, key_hash, label, team, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*rec.values(), label, team, int(is_admin), created),
        )
    fields = {k: rec[k] for k in ("id", "key_prefix", "fingerprint")}
    return ApiKeyCreated(key=secret, label=label, team=team, is_admin=is_admin, created_at=created, **fields)


def list_keys() -> list[ApiKey]:
    with connect() as c:
        rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [_row_to_key(r) for r in rows]


def _find(secret: str, active_only: bool) -> sqlite3.Row | None:
    sql = "SELECT * FROM api_keys WHERE key_prefix = ?" + (" AND revoked_at IS NULL" if active_only else "")
    with connect() as c:
        rows = c.execute(sql, (secret[:PREFIX_LEN],)).fetchall()
    found = None
    for r in rows:  # usually one row; compare every candidate so timing doesn't depend on which one matches
        if hmac.compare_digest(_hash(secret, r["salt"]), r["key_hash"]):
            found = r
    return found


def get_active_key(secret: str) -> ApiKey | None:
    r = _find(secret, active_only=True)
    return _row_to_key(r) if r else None


def revoke_key(ident: str) -> bool:
    """Revoke by key id (key_...) or by the key itself. Returns False when nothing matched."""
    if not ident.startswith("key_"):
        r = _find(ident, active_only=False)
        if r is None:
            return False
        ident = r["id"]
    with connect() as c:
        cur = c.execute("UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (now_utc(), ident))
    return cur.rowcount > 0


def record_call(
    key_id: str,
    team: str,
    alias: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    error: str | None = None,
    ts: str | None = None,
    cached: bool = False,
    saved_usd: float = 0.0,
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO usage (key, team, alias, provider, model, prompt_tokens, completion_tokens, "
            "cost_usd, error, ts, cached, saved_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                team,
                alias,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                error,
                ts or now_utc(),
                int(cached),
                saved_usd,
            ),
        )


def spend_today(key: str | None = None, team: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE date(ts) = date('now')"
    args: tuple = ()
    if key is not None:
        sql += " AND key = ?"
        args = (key,)
    elif team is not None:
        sql += " AND team = ?"
        args = (team,)
    with connect() as c:
        return float(c.execute(sql, args).fetchone()[0])


def spend_this_month(key: str | None = None, team: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')"
    args: tuple = ()
    if key is not None:
        sql += " AND key = ?"
        args = (key,)
    elif team is not None:
        sql += " AND team = ?"
        args = (team,)
    with connect() as c:
        return float(c.execute(sql, args).fetchone()[0])


def spend_for(scope: str, ident: str | None, period: str) -> float:
    """Spend lookup for the budget hierarchy: scope org|team|key, period daily|monthly (UTC)."""
    key = ident if scope == "key" else None
    team = ident if scope == "team" else None
    return spend_today(key=key, team=team) if period == "daily" else spend_this_month(key=key, team=team)


def usage_rows(start: str, end: str) -> list[dict]:
    """Usage between two UTC dates (inclusive, YYYY-MM-DD) with key labels and fingerprints (never secrets)."""
    with connect() as c:
        rows = c.execute(
            "SELECT u.key, COALESCE(k.label, '(deleted key)') AS key_label, k.fingerprint, u.team, u.alias, "
            "u.provider, u.model, u.prompt_tokens, u.completion_tokens, u.cost_usd, u.error, u.cached, u.saved_usd "
            "FROM usage u LEFT JOIN api_keys k ON k.id = u.key "
            "WHERE date(u.ts) >= date(?) AND date(u.ts) <= date(?)",
            (start, end),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        key_id = d.pop("key")
        d["key_fp"] = d.pop("fingerprint") or hashlib.sha256(key_id.encode()).hexdigest()[:8]
        out.append(d)
    return out


def calls_today() -> int:
    with connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM usage WHERE date(ts) = date('now')").fetchone()[0])


def recent_calls(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT u.ts, k.label, u.team, u.alias, u.provider, u.model, u.prompt_tokens, u.completion_tokens, "
            "u.cost_usd, u.error, u.cached, u.saved_usd FROM usage u LEFT JOIN api_keys k ON k.id = u.key "
            "ORDER BY u.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def per_provider_errors(window_minutes: int = 60) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """
            SELECT provider,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   COUNT(*) AS total
            FROM usage
            WHERE datetime(ts) >= datetime('now', ?) AND cached = 0
            GROUP BY provider
            """,
            (f"-{int(window_minutes)} minutes",),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Response cache ----------


def cache_get(team: str, key_hash: str, ttl_seconds: int) -> dict | None:
    with connect() as c:
        r = c.execute(
            "SELECT * FROM cache WHERE team = ? AND key_hash = ? AND datetime(created_at) > datetime('now', ?)",
            (team, key_hash, f"-{int(ttl_seconds)} seconds"),
        ).fetchone()
        if r:
            c.execute("UPDATE cache SET hits = hits + 1 WHERE team = ? AND key_hash = ?", (team, key_hash))
    return dict(r) if r else None


def cache_put(team: str, key_hash: str, alias: str, provider: str, model: str, response_json: str, cost: float) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (team, key_hash, alias, provider, model, response_json, cost_usd, "
            "created_at, hits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (team, key_hash, alias, provider, model, response_json, cost, now_utc()),
        )


def content_log_put(
    team: str, key_fp: str, alias: str, request_redacted: str, response_redacted: str, redactions: str
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO content_log (ts, team, key_fp, alias, request_redacted, response_redacted, redactions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_utc(), team, key_fp, alias, request_redacted, response_redacted, redactions),
        )


def content_log_rows(team: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM content_log"
    args: tuple = ()
    if team:
        sql += " WHERE team = ?"
        args = (team,)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as c:
        return [dict(r) for r in c.execute(sql, (*args, int(limit))).fetchall()]


def tool_usage_today(team: str, tool: str) -> tuple[int, float]:
    """(calls, USD) recorded today for an MCP tool ('server/tool') by a team; survives restarts."""
    with connect() as c:
        r = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE provider = 'mcp' AND model = ? AND team = ? AND date(ts) = date('now')",
            (tool, team),
        ).fetchone()
    return int(r[0]), float(r[1])


def shadow_record(row: dict) -> None:
    cols = (
        "team", "key_fp", "alias", "candidate", "provider", "model", "prompt_tokens", "completion_tokens", "cost_usd",
        "latency_s", "primary_deployment", "primary_cost_usd", "primary_latency_s", "agreement", "exact_match", "error",
    )  # fmt: skip
    with connect() as c:
        c.execute(
            f"INSERT INTO shadow_usage (ts, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
            (now_utc(), *(row.get(k, 0 if k.endswith("_tokens") else None) for k in cols)),
        )


def shadow_spend_today() -> float:
    with connect() as c:
        r = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM shadow_usage WHERE date(ts) = date('now')").fetchone()
    return float(r[0])


def shadow_summary(days: int = 7) -> list[dict]:
    """Per (alias, candidate): mirrored calls, errors, agreement and cost/latency of shadow vs. primary."""
    with connect() as c:
        rows = c.execute(
            """
            SELECT alias, candidate, COUNT(*) AS mirrored,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   AVG(agreement) AS mean_agreement,
                   AVG(exact_match) AS exact_match_rate,
                   SUM(cost_usd) AS shadow_cost_usd,
                   SUM(CASE WHEN error IS NULL THEN primary_cost_usd ELSE 0 END) AS primary_cost_usd,
                   AVG(latency_s) AS shadow_latency_s,
                   AVG(primary_latency_s) AS primary_latency_s,
                   SUM(CASE WHEN cost_usd IS NULL AND error IS NULL THEN 1 ELSE 0 END) AS unbilled
            FROM shadow_usage WHERE datetime(ts) >= datetime('now', ?)
            GROUP BY alias, candidate ORDER BY alias, candidate
            """,
            (f"-{int(days)} days",),
        ).fetchall()
    out = []
    for r in rows:
        d = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in dict(r).items()}
        p, sc = d["primary_cost_usd"] or 0.0, d["shadow_cost_usd"]
        d["cost_change_pct"] = round((sc - p) / p * 100, 2) if (p and sc is not None) else None
        out.append(d)
    return out


def cache_stats_today() -> dict:
    with connect() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(saved_usd), 0) AS saved, COALESCE(SUM(cached), 0) AS hits, COUNT(*) AS calls "
            "FROM usage WHERE date(ts) = date('now') AND error IS NULL"
        ).fetchone()
    calls = int(r["calls"])
    return {
        "saved_usd": round(float(r["saved"]), 6),
        "hits": int(r["hits"]),
        "hit_rate": round(int(r["hits"]) / calls, 4) if calls else 0.0,
    }
