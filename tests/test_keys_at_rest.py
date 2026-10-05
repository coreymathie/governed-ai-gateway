# Corey Mathie, 2026
"""Application keys hashed at rest: storage, lookup, pepper, API surface, and migration of a v0.5 database."""

import hashlib
import sqlite3

import litellm
import pytest
from fastapi.testclient import TestClient

from router import main, showback, store
from router.config_loader import settings

ADMIN = {"Authorization": "Bearer sk-router-admin"}


def _all_values(db_path) -> str:
    with sqlite3.connect(db_path) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return " ".join(str(v) for t in tables for row in c.execute(f"SELECT * FROM {t}") for v in row)


def test_only_a_salted_hash_is_stored():
    created = store.create_key("web", team="product")
    secret = created.key
    assert secret.startswith("sk-router-") and created.id.startswith("key_")
    assert created.key_prefix == secret[:14] and created.fingerprint == hashlib.sha256(secret.encode()).hexdigest()[:8]
    with store.connect() as c:
        row = dict(c.execute("SELECT * FROM api_keys").fetchone())
    assert secret not in _all_values(settings.ROUTER_DB_PATH)
    assert row["key_hash"] != hashlib.sha256(secret.encode()).hexdigest() and len(row["salt"]) == 32
    other = store.create_key("web2")
    with store.connect() as c:
        salts = {r[0] for r in c.execute("SELECT salt FROM api_keys")}
    assert len(salts) == 2 and other.id != created.id


def test_lookup_by_secret_wrong_key_and_revocation():
    created = store.create_key("app", team="data", is_admin=True)
    found = store.get_active_key(created.key)
    assert found.id == created.id and found.team == "data" and found.is_admin and not hasattr(found, "key")
    assert store.get_active_key(created.key[:-1] + ("A" if created.key[-1] != "A" else "B")) is None  # same prefix
    assert store.get_active_key("sk-router-nothing") is None
    assert store.revoke_key(created.id) and store.get_active_key(created.key) is None
    second = store.create_key("app2")
    assert store.revoke_key(second.key)  # by the secret too
    assert not store.revoke_key("key_doesnotexist") and not store.revoke_key("sk-router-unknown")


def test_pepper_is_part_of_the_hash(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_KEY_PEPPER", "pepper-one")
    created = store.create_key("peppered")
    assert store.get_active_key(created.key) is not None
    monkeypatch.setattr(settings, "ROUTER_KEY_PEPPER", "pepper-two")
    assert store.get_active_key(created.key) is None
    monkeypatch.setattr(settings, "ROUTER_KEY_PEPPER", "")
    assert store.get_active_key(created.key) is None


class FakeResponse:
    def model_dump(self):
        return {
            "id": "x",
            "object": "chat.completion",
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


@pytest.fixture
def client(monkeypatch):
    async def acompletion(model, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with TestClient(main.app) as c:
        yield c


BODY = {"model": "smart-fast", "messages": [{"role": "user", "content": "hi"}]}


def test_api_shows_the_secret_once_and_lists_prefixes_only(client):
    r = client.post("/admin/keys", json={"label": "web", "team": "product"}, headers=ADMIN).json()
    secret, key_id = r["key"], r["id"]
    assert r["key_prefix"] == secret[:14] and len(r["fingerprint"]) == 8
    listed = client.get("/admin/keys", headers=ADMIN).json()
    assert listed[0]["id"] == key_id and "key" not in listed[0] and secret not in str(listed)
    assert set(listed[0]) == {
        "id",
        "key_prefix",
        "fingerprint",
        "label",
        "team",
        "is_admin",
        "created_at",
        "revoked_at",
    }
    h = {"Authorization": f"Bearer {secret}"}
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    showback_rows = client.get("/admin/showback?group_by=key", headers=ADMIN).json()["rows"]
    assert showback_rows[0]["key_fp"] == r["fingerprint"] and secret not in str(showback_rows)
    assert client.delete(f"/admin/keys/{key_id}", headers=ADMIN).status_code == 200
    assert client.post("/v1/chat/completions", json=BODY, headers=h).status_code == 401
    assert client.delete("/admin/keys/key_nope", headers=ADMIN).status_code == 404
    admin = client.post("/admin/keys", json={"label": "ops", "is_admin": True}, headers=ADMIN).json()
    assert client.get("/admin/keys", headers={"Authorization": f"Bearer {admin['key']}"}).status_code == 200


# ---------- migration from 0.5.x ----------

OLD_SCHEMA = """
CREATE TABLE api_keys (key TEXT PRIMARY KEY, label TEXT NOT NULL, team TEXT NOT NULL DEFAULT 'default',
    is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, team TEXT NOT NULL, alias TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, error TEXT, ts TEXT NOT NULL);
"""
OLD_KEYS = [
    ("sk-router-OLDwebAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "web", "product", 0, None),
    ("sk-router-OLDopsBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", "ops", "platform", 1, None),
    ("sk-router-OLDoldCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC", "old", "product", 0, "2026-09-01 00:00:00"),
]
GONE = "sk-router-OLDgoneDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"  # usage for a key no longer in api_keys


@pytest.fixture
def v05_db(tmp_path, monkeypatch):
    path = tmp_path / "v05.db"
    with sqlite3.connect(path) as c:
        c.executescript(OLD_SCHEMA)
        for secret, label, team, admin, revoked in OLD_KEYS:
            c.execute(
                "INSERT INTO api_keys VALUES (?, ?, ?, ?, '2026-08-01 00:00:00', ?)",
                (secret, label, team, admin, revoked),
            )
        for secret in (OLD_KEYS[0][0], OLD_KEYS[0][0], OLD_KEYS[1][0], GONE):
            c.execute(
                "INSERT INTO usage (key, team, alias, provider, model, prompt_tokens, completion_tokens, cost_usd, ts) "
                "VALUES (?, 'product', 'smart-fast', 'openai', 'm', 1, 1, 0.5, datetime('now'))",
                (secret,),
            )
    monkeypatch.setattr(settings, "ROUTER_DB_PATH", str(path))
    return path


def test_migration_hashes_keys_repoints_usage_and_leaves_no_plaintext(v05_db):
    assert store.migrate_plaintext_keys() == 3
    store.init_db()  # also adds the 0.4 usage columns and the new tables; a second run is a no-op
    assert store.migrate_plaintext_keys() == 0
    web = store.get_active_key(OLD_KEYS[0][0])
    ops = store.get_active_key(OLD_KEYS[1][0])
    assert web.label == "web" and web.team == "product" and not web.is_admin and web.created_at == "2026-08-01 00:00:00"
    assert ops.is_admin and store.get_active_key(OLD_KEYS[2][0]) is None  # revoked stays revoked
    assert web.fingerprint == showback.fingerprint(OLD_KEYS[0][0])  # same fingerprint as 0.5 exports
    assert store.spend_today(key=web.id) == pytest.approx(1.0) and store.spend_today(key=ops.id) == pytest.approx(0.5)
    with store.connect() as c:
        keys = {r[0] for r in c.execute("SELECT DISTINCT key FROM usage")}
    assert keys == {web.id, ops.id, "unknown_" + showback.fingerprint(GONE)}
    raw = v05_db.read_bytes()
    for secret in [k[0] for k in OLD_KEYS] + [GONE]:
        assert secret.encode() not in raw, "plaintext key left in the database file"
    rows = store.usage_rows("2000-01-01", "2100-01-01")
    assert {r["key_fp"] for r in rows if r["key_label"] == "web"} == {web.fingerprint}


def test_migrated_keys_work_through_the_api(v05_db, monkeypatch):
    async def acompletion(model, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    with TestClient(main.app) as c:  # startup runs init_db, which migrates
        h = {"Authorization": f"Bearer {OLD_KEYS[0][0]}"}
        assert c.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
        assert c.get("/admin/keys", headers={"Authorization": f"Bearer {OLD_KEYS[1][0]}"}).status_code == 200
        assert (
            c.post("/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {OLD_KEYS[2][0]}"}).status_code
            == 401
        )
        listed = c.get("/admin/keys", headers=ADMIN).json()
        assert {k["label"] for k in listed} == {"web", "ops", "old"} and "OLD" not in str([k["id"] for k in listed])


def test_no_plaintext_left_even_where_sqlite_keeps_deleted_pages(v05_db, monkeypatch, tmp_path):
    """Some SQLite builds default secure_delete to OFF, leaving dropped rows in free pages. Simulate that."""
    real_connect = store.connect

    def connect():
        c = real_connect()
        c.execute("PRAGMA secure_delete = OFF")
        return c

    monkeypatch.setattr(store, "connect", connect)
    control = tmp_path / "control.db"  # with these settings a plain DROP TABLE leaves the secret in the file
    with sqlite3.connect(control) as c:
        c.execute("PRAGMA secure_delete = OFF")
        c.execute("CREATE TABLE t (k TEXT)")
        c.execute("INSERT INTO t VALUES (?)", (GONE,))
    with sqlite3.connect(control) as c:
        c.execute("PRAGMA secure_delete = OFF")
        c.execute("DROP TABLE t")
    assert GONE.encode() in control.read_bytes()
    assert store.migrate_plaintext_keys() == 3
    raw = v05_db.read_bytes()
    assert not [k for k, *_ in OLD_KEYS if k.encode() in raw] and GONE.encode() not in raw
