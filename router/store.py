# Corey Mathie, 2026
"""
SQLite persistence for API keys and per-call usage.

Timestamps are stored as UTC in SQLite's native "YYYY-MM-DD HH:MM:SS" format and
always compared through datetime(), so time-window queries (this hour, last 60
minutes, last 7 days) are correct. "Today" means the current UTC day.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config_loader import settings
from .models import ApiKey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT 'default',
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
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
"""

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


def init_db() -> None:
    with connect() as c:
        c.executescript(_SCHEMA)
        existing = {r["name"] for r in c.execute("PRAGMA table_info(usage)")}
        for column, ddl in _USAGE_MIGRATIONS.items():
            if column not in existing:
                c.execute(ddl)


def _row_to_key(r: sqlite3.Row) -> ApiKey:
    return ApiKey(
        key=r["key"],
        label=r["label"],
        team=r["team"],
        is_admin=bool(r["is_admin"]),
        created_at=r["created_at"],
        revoked_at=r["revoked_at"],
    )


def create_key(label: str, team: str = "default", is_admin: bool = False) -> ApiKey:
    key = "sk-router-" + secrets.token_urlsafe(32)
    created = now_utc()
    with connect() as c:
        c.execute(
            "INSERT INTO api_keys (key, label, team, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, label, team, int(is_admin), created),
        )
    return ApiKey(key=key, label=label, team=team, is_admin=is_admin, created_at=created)


def list_keys() -> list[ApiKey]:
    with connect() as c:
        rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [_row_to_key(r) for r in rows]


def get_active_key(key: str) -> ApiKey | None:
    with connect() as c:
        r = c.execute("SELECT * FROM api_keys WHERE key = ? AND revoked_at IS NULL", (key,)).fetchone()
    return _row_to_key(r) if r else None


def revoke_key(key: str) -> None:
    with connect() as c:
        c.execute("UPDATE api_keys SET revoked_at = ? WHERE key = ?", (now_utc(), key))


def record_call(
    key: str,
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
                key,
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


def calls_today() -> int:
    with connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM usage WHERE date(ts) = date('now')").fetchone()[0])


def recent_calls(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT u.ts, k.label, u.team, u.alias, u.provider, u.model, u.prompt_tokens, u.completion_tokens, "
            "u.cost_usd, u.error, u.cached, u.saved_usd FROM usage u LEFT JOIN api_keys k ON k.key = u.key "
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
