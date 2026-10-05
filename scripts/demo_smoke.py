# Corey Mathie, 2026
"""
Headless smoke test of the browser demo (demo/index.html) in Chromium.

Serves the repository over a local HTTP server, opens /demo/, waits for
Pyodide to boot and load router/*.py, then drives every panel and fails on any
page error, console error, failed request, or request to an unexpected host.

    pip install playwright            # plus a Chromium build (playwright install chromium)
    python scripts/demo_smoke.py
    python scripts/demo_smoke.py --pyodide-dir /path/to/pyodide-0.26.4/pyodide   # offline / CDN blocked

With --pyodide-dir, requests for the jsDelivr Pyodide URL are answered from a
local copy of the same release (the "pyodide" folder of the official
pyodide-0.26.4 release tarball). Without it, Pyodide loads from the CDN.
Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socket
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CDN = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"
TYPES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".json": "application/json",
    ".whl": "application/zip",
    ".tar": "application/x-tar",
}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the output to check results
        pass


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.passed = 0

    def __call__(self, cond: object, msg: str) -> None:
        print(("PASS " if cond else "FAIL ") + msg, flush=True)
        if cond:
            self.passed += 1
        else:
            self.failed.append(msg)


def idle(page, button: str, timeout: int = 60_000) -> None:
    page.wait_for_function(f"!document.getElementById('{button}').disabled", timeout=timeout)


def run(pyodide_dir: Path | None, shots: Path | None, headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    srv, port = serve(REPO)
    origin = f"http://127.0.0.1:{port}"
    check = Checks()
    errors: list[str] = []
    failures: list[str] = []
    foreign: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
            page = ctx.new_page()

            if pyodide_dir is not None:

                def from_disk(route):
                    name = route.request.url[len(CDN) :].split("?")[0]
                    f = pyodide_dir / name
                    if not f.is_file():
                        return route.fulfill(status=404, body="not in local pyodide copy")
                    ctype = TYPES.get(f.suffix, "application/octet-stream")
                    route.fulfill(path=str(f), content_type=ctype, headers={"access-control-allow-origin": "*"})

                page.route(CDN + "**", from_disk)

            def on_request(r):
                if not r.url.startswith((origin, CDN, "blob:", "data:")):
                    foreign.append(r.url)

            page.on("request", on_request)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("requestfailed", lambda r: failures.append(r.url))

            t0 = time.time()
            page.goto(f"{origin}/demo/")
            page.wait_for_function("window.__demoReady === true", timeout=180_000)
            check(True, f"Pyodide booted and the engine imported in {time.time() - t0:.1f}s")
            check(page.inner_text("#status").startswith("Ready. Python 3.12"), "status line reports ready")
            modules = page.locator("#modules li").all_inner_texts()
            expected = page.evaluate("ROUTER_MODULES.length") + 1
            check(len(modules) == expected and all("sha256" in m for m in modules), f"{len(modules)} files listed")
            check(page.locator("#providers article").count() == 3, "three provider cards")

            # --- traffic: fallback, breakers, half-open ---
            page.click("#btnSend")
            page.wait_for_timeout(200)
            check(page.locator("#events tr").count() == 1 and "✓" in page.inner_text("#events"), "one request served")
            page.locator('[data-dep="anthropic/claude-haiku-4-5"] input[data-field="outage"]').check()
            page.click("#btnBurst")
            idle(page, "btnBurst")
            check("3 consecutive failures" in page.inner_text("#transitions"), "breaker opened after 3 failures")
            check("skipped" in page.inner_text("#events"), "later requests skip the open breaker")
            page.locator('[data-dep="anthropic/claude-haiku-4-5"] input[data-field="outage"]').uncheck()
            page.click("#btnWait")
            page.wait_for_timeout(150)
            page.click("#btnSend")
            page.wait_for_timeout(200)
            check("probe succeeded" in page.inner_text("#transitions"), "half-open probe closes the breaker")

            # --- anomaly spike, budgets, showback ---
            page.click("#btnReset")
            page.wait_for_timeout(200)
            page.click("#btnSpike")
            idle(page, "btnSpike")
            check("pause" in page.inner_text("#anomalies"), "anomaly table shows a paused key")
            check("key paused" in page.inner_text("#events"), "paused key is refused with 429")
            check(page.locator("#budgets .budget").count() >= 4, "budget bars rendered")
            check(page.locator("#showback tr").count() >= 1, "showback rows rendered")
            with page.expect_download() as dl:
                page.click("#btnCsv")
            head = Path(dl.value.path()).read_text().splitlines()[0]
            check(head.startswith("team,model,requests"), f"showback CSV header: {head[:40]}…")

            # --- governance: PII hooks, content log, audit ---
            page.select_option("#govTeam", "support")
            page.select_option("#govMode", "redact")
            page.click("#btnPrompt")
            idle(page, "btnPrompt")
            sent = page.inner_text("#govSent")
            check("[REDACTED:EMAIL]" in sent and "[REDACTED:CARD]" in sent, "redaction on: provider gets placeholders")
            check("@example.com" not in sent, "redaction on: no email reaches the provider")
            check("redact" in page.inner_text("#govAudit"), "audit trail shows the redaction")
            page.select_option("#govMode", "off")
            page.click("#btnPrompt")
            idle(page, "btnPrompt")
            check("@example.com" in page.inner_text("#govSent"), "redaction off: provider gets the raw prompt")
            check("[REDACTED:EMAIL]" in page.inner_text("#govLogRows"), "content log entries are redacted")
            page.select_option("#govMode", "block")
            page.click("#btnPrompt")
            idle(page, "btnPrompt")
            check("422" in page.inner_text("#govTrace"), "block mode refuses the prompt with 422")
            check(page.inner_text("#govSent") == "(nothing was sent)", "block mode: nothing reaches a provider")

            # --- governance: policy-as-code ---
            page.select_option("#govTeam", "regulated")
            page.select_option("#govMode", "off")
            page.select_option("#govAlias", "public-only")
            page.click("#btnPrompt")
            idle(page, "btnPrompt")
            trace = page.inner_text("#govTrace")
            check("policy: deny 403" in trace, "regulated team on a public-only route is denied (403)")
            check(page.inner_text("#govSent") == "(nothing was sent)", "policy deny: nothing reaches a provider")
            page.select_option("#govAlias", "smart-fast")
            page.click("#btnPrompt")
            idle(page, "btnPrompt")
            trace = page.inner_text("#govTrace")
            check(
                "removed anthropic, openai" in trace and "max_tokens 2048" in trace, "public providers removed, clamp"
            )
            check("ollama" in trace.split("provider:")[-1], "regulated prompt served by the local model")
            check("[REDACTED:EMAIL]" in page.inner_text("#govSent"), "policy-required pii_redact runs with hooks off")
            check(
                "allow" in page.inner_text("#govAudit") and "deny" in page.inner_text("#govAudit"), "decisions audited"
            )
            if shots:
                page.locator("#govCard").screenshot(path=str(shots / "demo_governance.png"))

            # --- semantic cache ---
            def ask(team: str, text: str) -> str:
                page.select_option("#semTeam", team)
                page.fill("#semText", text)
                page.click("#btnSem")
                idle(page, "btnSem")
                return page.inner_text("#semResult")

            check("miss" in ask("product", "How do I export my invoices as CSV?"), "first question is a miss")
            check(ask("product", "how do I export my invoices as csv").startswith("hit"), "reworded question hits")
            check("miss" in ask("support", "how do I export my invoices as csv"), "other team never sees the entry")
            ask("product", "Is the 2025 model compatible with the charger?")
            res = ask("product", "Is the 2023 model compatible with the charger?")
            check("numbers differ" in res, "number guard blocks a near-identical prompt")
            page.click("#btnCal")
            page.wait_for_function("document.getElementById('calThr') !== null", timeout=60_000)
            check(page.inner_text("#calThr") == "0.88", "calibration in the browser picks 0.88")
            check(page.inner_text("#calHold").startswith("26.3% / 9.1%"), "held-out numbers match the script")
            if shots:
                page.locator("#semCard").screenshot(path=str(shots / "demo_semantic.png"))

            # --- MCP tool gateway ---
            def tool(team: str, server: str, name: str, burst: bool = False) -> str:
                page.select_option("#mcpTeam", team)
                page.select_option("#mcpServer", server)
                page.select_option("#mcpTool", name)
                page.click("#btnMcpBurst" if burst else "#btnMcp")
                idle(page, "btnMcpBurst" if burst else "btnMcp")
                return page.inner_text("#mcpResult")

            check(tool("support", "tickets", "search_tickets").startswith("allow"), "allowed tool call goes through")
            check("[REDACTED:EMAIL]" in page.inner_text("#mcpLog"), "tool arguments are audited redacted")
            check("not allowed" in tool("support", "tickets", "reassign_ticket"), "tool outside the allow-list denied")
            check("not allowed" in tool("product", "tickets", "search_tickets"), "team without tools denied")
            res = tool("support", "tickets", "close_ticket", burst=True)
            check("rate limited" in res and "velocity" in res, "burst hits the velocity limit")
            check(page.locator("#mcpTools .pill.skip").count() >= 1, "hidden tools shown struck through")
            if shots:
                page.locator("#mcpCard").screenshot(path=str(shots / "demo_mcp.png"))

            if shots:
                page.screenshot(path=str(shots / "demo_full.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            width = page.evaluate("document.documentElement.scrollWidth")
            check(width <= 390, f"no horizontal scroll at 390px (scrollWidth={width})")

            check(not errors, f"no page or console errors {errors[:3]}")
            check(not failures, f"no failed requests {failures[:3]}")
            check(not foreign, f"no requests to other hosts {foreign[:3]}")
            browser.close()
    finally:
        srv.shutdown()
    print(f"RESULT: {check.passed} passed, {len(check.failed)} failed")
    return 0 if not check.failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pyodide-dir", type=Path, help="local copy of Pyodide 0.26.4 (served instead of jsDelivr)")
    ap.add_argument("--screenshots", type=Path, help="directory to write screenshots into")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if args.pyodide_dir and not (args.pyodide_dir / "pyodide.js").is_file():
        ap.error(f"{args.pyodide_dir} has no pyodide.js")
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    try:
        return run(args.pyodide_dir, args.screenshots, args.headed)
    except ImportError:
        print("playwright is not installed: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
