# Corey Mathie, 2026
"""MCP tool gateway: policy core, and the HTTP proxy against a fake MCP server on a real local port."""

import json
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from router import config_loader, main, mcp_policy
from router.config_loader import settings

ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer sk-router-admin"}
TOOLS = [
    {"name": "search_cases", "description": "Search", "inputSchema": {"type": "object"}},
    {"name": "close_case", "description": "Close", "inputSchema": {"type": "object"}},
    {"name": "delete_everything", "description": "Danger", "inputSchema": {"type": "object"}},
]


# ---------- policy core ----------


def test_example_config_validates():
    pol = mcp_policy.load(yaml.safe_load((ROOT / "config" / "mcp.example.yaml").read_text()))
    assert set(pol.servers) == {"cases", "files"}
    assert pol.entry("member-services", "cases", "search_customers").tool == "search_*"
    assert pol.entry("member-services", "files", "read_file") is None
    assert pol.entry("risk-analytics", "files", "delete_file") is None
    assert pol.entry("risk-analytics", "files", "read_file").limits.cost_usd == 0.001


@pytest.mark.parametrize(
    "doc,match",
    [
        ({"servers": {"s": {"url": "ftp://x"}}}, "http"),
        ({"servers": {"s": {"url": "http://x", "token": "abc"}}}, "expected"),
        ({"servers": {"s": {"url": "http://x", "timeout_s": 0}}}, "timeout_s"),
        ({"servers": {"s": {"url": "http://x"}}, "teams": {"t": [{"server": "z", "tool": "a"}]}}, "unknown server"),
        (
            {"servers": {"s": {"url": "http://x"}}, "teams": {"t": [{"server": "s", "tool": "a", "per_minute": -1}]}},
            "non-negative",
        ),
        (
            {"servers": {"s": {"url": "http://x"}}, "teams": {"t": [{"server": "s", "tool": "a", "per_day": 1.5}]}},
            "integer",
        ),
        ({"defaults": {"redact_args": "yes"}}, "true/false"),
        ({"defaults": {"rate": 1}}, "unknown keys"),
        ({"teams": {"t": {"server": "s"}}}, "list of allow entries"),
        ({"other": 1}, "unknown top-level"),
    ],
)
def test_config_errors(doc, match):
    with pytest.raises(mcp_policy.McpConfigError, match=match):
        mcp_policy.load(doc)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


POLICY = {
    "servers": {"cases": {"url": "http://127.0.0.1:1/mcp"}},
    "defaults": {"per_minute": 3, "max_identical_per_minute": 2},
    "teams": {
        "member-services": [
            {"server": "cases", "tool": "search_*"},
            {"server": "cases", "tool": "close_case", "per_day": 2, "cost_usd": 0.5, "daily_usd": 0.75},
        ],
        "it-engineering": [{"server": "cases", "tool": "*", "per_minute": 100, "team_per_minute": 2}],
    },
}


def test_check_call_allow_list_velocity_identical_and_caps():
    pol, clock = mcp_policy.load(POLICY), Clock()
    lim = mcp_policy.VelocityLimiter(clock)

    def call(team="member-services", key="k1", tool="search_cases", args=None, used=(0, 0.0)):
        d = mcp_policy.check_call(pol, lim, team, key, "cases", tool, args if args is not None else {"q": key}, used)
        if d.allow:
            lim.record(d.scopes)
        return d

    assert call(tool="delete_everything").action == "deny" and call(team="risk-analytics").action == "deny"
    assert [call(args={"q": i}).action for i in range(4)] == ["allow", "allow", "allow", "rate_limited"]
    limited = call(args={"q": 9})
    assert limited.code == mcp_policy.ERR_RATE and "per-key" in limited.reason and 1 <= limited.retry_after <= 60
    assert call(key="k2", args={"q": 1}).allow  # another key has its own window
    assert [call(key="k3", args={"same": 1}).action for _ in range(3)][-1] == "rate_limited"  # agent-loop guard
    assert "identical-call" in call(key="k3", args={"same": 1}).reason
    clock.t += 61
    assert call(args={"q": "later"}).allow
    assert call(tool="close_case", used=(2, 0.0)).action == "cap_reached"
    spend = call(tool="close_case", used=(1, 0.5))
    assert spend.action == "cap_reached" and "spend cap" in spend.reason and spend.code == mcp_policy.ERR_CAP
    assert [call(team="it-engineering", key=f"o{i}", args={"i": i}).action for i in range(3)][-1] == "rate_limited"


def test_filter_tools_and_redact_args():
    pol = mcp_policy.load(POLICY)
    assert [t["name"] for t in mcp_policy.filter_tools(pol, "member-services", "cases", TOOLS)] == [
        "search_cases",
        "close_case",
    ]
    assert mcp_policy.filter_tools(pol, "nobody", "cases", TOOLS) == []
    red, found = mcp_policy.redact_args(
        {"to": "ana@example.com", "n": 3, "cc": ["bo@example.com", {"x": "202-555-0143"}]}
    )
    assert red == {"to": "[REDACTED:EMAIL]", "n": 3, "cc": ["[REDACTED:EMAIL]", {"x": "[REDACTED:PHONE]"}]}
    assert found == {"email": 2, "phone": 1}


# ---------- through the gateway ----------


class FakeMcp:
    """A minimal MCP server (Streamable HTTP, request/response) on a real local port."""

    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "json"  # json | sse | down
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"msg": msg, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if outer.mode == "down":
                    return self._send(500, b"boom", "text/plain")
                if "id" not in msg:
                    return self._send(202, b"", "text/plain")
                method = msg["method"]
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake-cases", "version": "0"}}  # fmt: skip
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    p = msg["params"]
                    text = f"ran {p['name']} with {json.dumps(p.get('arguments'))}"
                    bad = p["name"] == "close_case" and p.get("arguments", {}).get("id") == "bad"
                    result = {"content": [{"type": "text", "text": text}], "isError": bad}
                else:
                    result = {}
                body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                if outer.mode == "sse":
                    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
                    text = f"event: message\ndata: {note}\n\nevent: message\ndata: {json.dumps(body)}\n\n"
                    return self._send(200, text.encode(), "text/event-stream")
                self._send(200, json.dumps(body).encode(), "application/json", {"Mcp-Session-Id": "sess-123"})

            def _send(self, status, body, ctype, extra=None):
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def calls(self, method="tools/call"):
        return [r for r in self.requests if r["msg"].get("method") == method]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeMcp()
    yield f
    f.close()


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _mcp(fake, extra_team: str = ""):
    Path(settings.ROUTER_MCP_FILE).write_text(
        textwrap.dedent(
            f"""
            version: 1
            servers:
              cases: {{ url: "{fake.url}", timeout_s: 2, headers_from_env: {{ Authorization: TEST_MCP_AUTH }} }}
            defaults: {{ per_minute: 3, max_identical_per_minute: 2 }}
            teams:
              member-services:
                - {{ server: cases, tool: "search_*" }}
                - {{ server: cases, tool: close_case, per_day: 2, cost_usd: 0.25 }}
            """
        )
        + extra_team  # already indented under `teams:`
    )
    config_loader.reload_all()


def _key(client, team="member-services"):
    r = client.post("/admin/keys", json={"label": f"{team}-agent", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _rpc(client, h, method, params=None, msg_id=1, server="cases", headers=None):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": msg_id} if msg_id is not None else {})}
    if params is not None:
        msg["params"] = params
    return client.post(f"/mcp/{server}", json=msg, headers={**h, **(headers or {})})


@pytest.fixture(autouse=True)
def mcp_auth(monkeypatch):
    monkeypatch.setenv("TEST_MCP_AUTH", "Bearer upstream-test-token")


def test_initialize_list_and_notifications_pass_through(client, fake):
    _mcp(fake)
    h = _key(client)
    r = _rpc(
        client, h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}
    )
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "fake-cases"
    assert r.headers["mcp-session-id"] == "sess-123"
    up = fake.requests[0]["headers"]
    assert up["authorization"] == "Bearer upstream-test-token" and "sk-router" not in json.dumps(up)
    n = _rpc(client, h, "notifications/initialized", msg_id=None, headers={"Mcp-Session-Id": "sess-123"})
    assert n.status_code == 202 and fake.requests[-1]["headers"]["mcp-session-id"] == "sess-123"
    tools = _rpc(client, h, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_cases", "close_case"]  # delete_everything is hidden
    assert _rpc(client, h, "ping").json()["result"] == {}


def test_allowed_call_is_forwarded_recorded_and_audited_with_redaction(client, fake):
    _mcp(fake)
    h = _key(client)
    r = _rpc(client, h, "tools/call", {"name": "search_cases", "arguments": {"q": "dispute for ana@example.com"}})
    assert r.status_code == 200 and r.headers["x-router-mcp-decision"] == "allow"
    assert "ana@example.com" in r.json()["result"]["content"][0]["text"]  # upstream got the original by default
    row = client.get("/admin/audit?category=mcp", headers=ADMIN).json()[0]
    assert row["action"] == "allow" and row["subject"] == "cases/search_cases" and row["team"] == "member-services"
    assert row["detail"]["args"] == {"q": "dispute for [REDACTED:EMAIL]"} and row["detail"]["redactions"] == {
        "email": 1
    }
    assert "ana@example.com" not in json.dumps(row)
    _rpc(client, h, "tools/call", {"name": "close_case", "arguments": {"id": "C-1"}})
    recent = client.get("/admin/recent", headers=ADMIN).json()
    assert recent[0]["provider"] == "mcp" and recent[0]["model"] == "cases/close_case" and recent[0]["cost_usd"] == 0.25
    failed = _rpc(client, h, "tools/call", {"name": "close_case", "arguments": {"id": "bad"}})
    assert failed.json()["result"]["isError"] is True  # a tool-level error is relayed, and billed
    assert client.get("/admin/recent", headers=ADMIN).json()[0]["error"] == "tool returned an error"
    assert client.get("/admin/audit?category=mcp", headers=ADMIN).json()[0]["detail"]["is_error"] is True
    rows = client.get("/admin/showback?group_by=team,provider", headers=ADMIN).json()["rows"]
    assert any(r["provider"] == "mcp" and r["cost_usd"] == 0.5 for r in rows)
    assert (
        'router_mcp_tool_calls_total{server="cases",tool="search_cases",decision="allow"} 1'
        in client.get("/metrics", headers=ADMIN).text
    )


def test_denied_tools_never_reach_the_server(client, fake):
    _mcp(fake, "  risk-analytics:\n    - { server: cases, tool: search_cases }\n")
    h = _key(client)
    r = _rpc(client, h, "tools/call", {"name": "delete_everything", "arguments": {}})
    body = r.json()
    assert (
        r.status_code == 200
        and body["error"]["code"] == mcp_policy.ERR_DENIED
        and "not allowed" in body["error"]["message"]
    )
    assert r.headers["x-router-mcp-decision"] == "deny" and fake.calls() == []
    outsider = _rpc(client, _key(client, "digital-banking"), "tools/list")
    assert outsider.status_code == 403 and outsider.json()["error"]["code"] == -32001 and fake.calls("tools/list") == []
    actions = [a["action"] for a in client.get("/admin/audit?category=mcp", headers=ADMIN).json()]
    assert actions == ["deny", "deny"]


def test_velocity_identical_calls_and_daily_cap(client, fake):
    _mcp(fake)
    h = _key(client)
    res = [_rpc(client, h, "tools/call", {"name": "search_cases", "arguments": {"q": str(i)}}) for i in range(4)]
    assert [r.headers["x-router-mcp-decision"] for r in res] == ["allow", "allow", "allow", "rate_limited"]
    assert res[-1].json()["error"]["code"] == mcp_policy.ERR_RATE and int(res[-1].headers["retry-after"]) >= 1
    h2 = _key(client)
    same = [_rpc(client, h2, "tools/call", {"name": "close_case", "arguments": {"id": "C-9"}}) for _ in range(3)]
    assert [r.headers["x-router-mcp-decision"] for r in same] == ["allow", "allow", "cap_reached"]  # per_day: 2
    h3 = _key(client)
    assert (
        _rpc(client, h3, "tools/call", {"name": "close_case", "arguments": {"id": "C-10"}}).json()["error"]["code"]
        == mcp_policy.ERR_CAP
    )  # the cap is per team per day, persisted in usage
    loop = [_rpc(client, h3, "tools/call", {"name": "search_cases", "arguments": {"q": "x"}}) for _ in range(3)]
    assert loop[-1].json()["error"]["message"].startswith("identical-call velocity limit")
    assert len(fake.calls()) == 3 + 2 + 2


def test_upstream_failures_and_missing_credentials_fail_closed(client, fake, monkeypatch):
    _mcp(fake)
    h = _key(client)
    fake.mode = "down"
    r = _rpc(client, h, "tools/call", {"name": "search_cases", "arguments": {"q": "a"}})
    assert r.json()["error"]["code"] == mcp_policy.ERR_UPSTREAM and "result" not in r.json()
    assert r.headers["x-router-mcp-decision"] == "upstream_error"
    assert _rpc(client, h, "tools/list").json()["error"]["code"] == mcp_policy.ERR_UPSTREAM
    fake.mode = "json"
    monkeypatch.delenv("TEST_MCP_AUTH")
    n = len(fake.requests)
    r = _rpc(client, h, "tools/call", {"name": "search_cases", "arguments": {"q": "b"}})
    assert (
        r.json()["error"]["code"] == mcp_policy.ERR_UPSTREAM and len(fake.requests) == n
    )  # never called unauthenticated
    row = client.get("/admin/audit?category=mcp", headers=ADMIN).json()[0]
    assert row["action"] == "upstream_error" and "TEST_MCP_AUTH" in row["detail"]["error"]


def test_sse_replies_and_forward_redacted(client, fake):
    _mcp(fake, "  risk-analytics:\n    - { server: cases, tool: search_cases, forward_redacted: true }\n")
    fake.mode = "sse"
    r = _rpc(client, _key(client), "tools/call", {"name": "search_cases", "arguments": {"q": "ok"}}, msg_id=7)
    assert r.json()["id"] == 7 and "ran search_cases" in r.json()["result"]["content"][0]["text"]
    fake.mode = "json"
    _rpc(
        client,
        _key(client, "risk-analytics"),
        "tools/call",
        {"name": "search_cases", "arguments": {"q": "bo@example.com"}},
    )
    assert fake.calls()[-1]["msg"]["params"]["arguments"] == {"q": "[REDACTED:EMAIL]"}


def test_protocol_edges(client, fake):
    _mcp(fake)
    h = _key(client)
    assert client.post("/mcp/nope", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h).status_code == 404
    batch = client.post("/mcp/cases", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=h)
    assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
    bad = client.post("/mcp/cases", content=b"{not json", headers={**h, "content-type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    assert _rpc(client, h, "resources/list").json()["error"]["code"] == -32601
    assert _rpc(client, h, "tools/call", {"arguments": {}}).json()["error"]["code"] == -32602
    assert client.get("/mcp/cases", headers=h).status_code == 405
    assert client.post("/mcp/cases", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    view = client.get("/admin/mcp", headers=ADMIN).json()
    assert view["servers"]["cases"]["auth_headers"] == ["Authorization"] and "upstream-test-token" not in json.dumps(
        view
    )
    assert fake.calls() == []
