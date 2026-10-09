# Corey Mathie, 2026
"""
Headless smoke test of the console (demo/) in Chromium, in demo mode and live mode.

Demo mode serves the repository over a local HTTP server, opens /demo/, waits for Pyodide to boot and
load router/*.py, then visits every screen, performs its key interaction and asserts the result.
Live mode starts the gateway with uvicorn on a free port (simulated providers, temporary database and
config copies, a random admin key), opens /console/?mode=live and exercises the screens against the real
HTTP API. Both fail on any page error, console error, failed request or request to an unexpected host,
and check there is no horizontal scroll at 390 px or 1366 px.

    pip install playwright            # plus a Chromium build (playwright install chromium)
    python scripts/demo_smoke.py                              # demo mode, Pyodide from jsDelivr
    python scripts/demo_smoke.py --pyodide-dir /path/to/pyodide-0.26.4/pyodide   # offline / CDN blocked
    python scripts/demo_smoke.py --live                       # live mode only (no network needed)
    python scripts/demo_smoke.py --both --screenshots shots/  # both, with desktop + mobile screenshots

With --pyodide-dir, requests for the jsDelivr Pyodide URL are answered from a local copy of the same
release (the "pyodide" folder of the official pyodide-0.26.4 release tarball).
Exit code 0 when every check passes, 1 on a failed check, 2 when Playwright is missing.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
SCREENS = [
    "overview",
    "overview/session",
    "requests",
    "playground",
    "traces",
    "policies",
    "budgets",
    "mcp",
    "evals",
    "settings",
]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the output to check results
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class Checks:
    def __init__(self, label: str) -> None:
        self.label = label
        self.failed: list[str] = []
        self.passed = 0

    def __call__(self, cond: object, msg: str) -> None:
        print(("PASS " if cond else "FAIL ") + f"[{self.label}] " + msg, flush=True)
        if cond:
            self.passed += 1
        else:
            self.failed.append(msg)


class Browser:
    """One page with the error, failure and foreign-host listeners every check relies on."""

    def __init__(self, p, origin: str, pyodide_dir: Path | None, headed: bool, ignore_http_errors: bool = False):
        self.browser = p.chromium.launch(headless=not headed)
        self.ctx = self.browser.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.failures: list[str] = []
        self.foreign: list[str] = []
        if pyodide_dir is not None:

            def from_disk(route):
                name = route.request.url[len(CDN) :].split("?")[0]
                f = pyodide_dir / name
                if not f.is_file():
                    return route.fulfill(status=404, body="not in local pyodide copy")
                ctype = TYPES.get(f.suffix, "application/octet-stream")
                route.fulfill(path=str(f), content_type=ctype, headers={"access-control-allow-origin": "*"})

            self.page.route(CDN + "**", from_disk)
        http_error = re.compile(r"Failed to load resource: the server responded with a status of [45]\d\d")

        def on_console(m):
            if m.type == "error" and not (ignore_http_errors and http_error.search(m.text)):
                self.errors.append(m.text)

        self.page.on("console", on_console)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("requestfailed", lambda r: self.failures.append(f"{r.url} {r.failure}"))
        allowed = (origin, CDN, "blob:", "data:")
        self.page.on("request", lambda r: None if r.url.startswith(allowed) else self.foreign.append(r.url))

    def close(self) -> None:
        self.browser.close()


def wait_idle(page, timeout: int = 60_000) -> None:
    page.wait_for_function("!document.querySelector('button[aria-busy=true]')", timeout=timeout)


def goto(page, base: str, hash_: str) -> None:
    page.evaluate(f"location.hash = {hash_!r}")
    page.wait_for_function(
        "(h) => location.hash === h && !document.querySelector('#view > .loading')", arg=hash_, timeout=60_000
    )
    page.wait_for_timeout(250)


def no_hscroll(page) -> int:
    return page.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)")


def layout_checks(check: Checks, b: Browser, base: str, shots: Path | None, prefix: str) -> None:
    page = b.page
    page.add_style_tag(content="#toasts { display: none !important; }")  # keep notifications out of screenshots
    for s in SCREENS:
        goto(page, base, f"#/{s}")
        width = no_hscroll(page)
        check(width <= 1366, f"{s}: no horizontal scroll at 1366px (scrollWidth={width})")
        if shots:
            page.screenshot(path=str(shots / f"{prefix}_{s.replace('/', '-')}_desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    for s in SCREENS:
        goto(page, base, f"#/{s}")
        width = no_hscroll(page)
        check(width <= 390, f"{s}: no horizontal scroll at 390px (scrollWidth={width})")
        if shots:
            page.screenshot(path=str(shots / f"{prefix}_{s.replace('/', '-')}_mobile.png"), full_page=True)
    page.click("#menuBtn")
    check(page.is_visible("#nav a[data-nav='requests']"), "390px: the menu button opens the navigation")
    page.click("#nav a[data-nav='overview']")
    page.wait_for_timeout(300)
    check(not page.is_visible("#nav a[data-nav='requests']"), "390px: choosing a screen closes the menu")
    page.set_viewport_size({"width": 1366, "height": 900})


# ---------------------------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------------------------


def run_demo(p, pyodide_dir: Path | None, shots: Path | None, headed: bool) -> Checks:
    check = Checks("demo")
    srv, port = serve(REPO)
    origin = f"http://127.0.0.1:{port}"
    base = f"{origin}/demo/"
    b = Browser(p, origin, pyodide_dir, headed)
    page = b.page
    try:
        t0 = time.time()
        page.goto(base)
        page.wait_for_selector(".kpi.sample", timeout=30_000)
        check(
            page.evaluate("window.__consoleReady !== true"),
            "Spend renders from sample data before the engine has started",
        )
        page.wait_for_function("window.__consoleReady === true", timeout=180_000)
        check(True, f"Pyodide booted and the engine imported in {time.time() - t0:.1f}s")
        check(page.inner_text("#modeText") == "Demo workspace", "header badge says demo workspace (business view)")

        # --- guided tour: offered on first visit, five steps, dismissal remembered ---
        page.wait_for_selector("#tourInvite:not([hidden])", timeout=10_000)
        check(not page.is_visible("#tour .bubble"), "tour is offered, not forced")
        page.click("#tourInviteStart")
        page.wait_for_selector("#tour.open .bubble")
        check("step 1 of 5" in page.inner_text("#tour").lower(), "tour starts from the invite")
        for _ in range(4):
            page.click("#tourNext")
        check("step 5 of 5" in page.inner_text("#tour").lower(), "tour walks through five steps")
        page.click("#tourNext")
        check(not page.is_visible("#tour .bubble"), "tour closes on Done")
        page.reload()
        page.wait_for_function("window.__consoleReady === true", timeout=180_000)
        page.wait_for_timeout(500)
        check(
            not page.is_visible("#tourInvite") and not page.is_visible("#tour .bubble"), "tour dismissal is remembered"
        )

        # --- views and theme ---
        page.click("[data-view='technical']")
        check(page.evaluate("document.body.classList.contains('tech')"), "technical view switches on")
        check(page.inner_text("#modeText") == "Demo · runs in your browser", "technical view: badge names the runtime")
        page.click("[data-view='business']")
        page.click("#themeBtn")
        theme = page.evaluate("document.documentElement.dataset.theme")
        page.click("#themeBtn")
        check(
            theme in ("light", "dark") and page.evaluate("document.documentElement.dataset.theme") != theme,
            "theme toggle switches light and dark",
        )

        # --- spend: the sample company ---
        goto(page, base, "#/overview")
        check("fictional" in page.inner_text(".note.sample"), "spend: sample company labelled fictional")
        check(page.locator(".kpi.sample").count() == 8, "spend: eight KPI tiles")
        check(page.locator("#view svg.chart rect").count() >= 150, "spend: daily spend chart by team")
        check(page.locator(".month-card table.budgets tbody tr").count() == 7, "spend: October budgets for seven teams")
        check("Forecast for October" in page.inner_text(".month-card"), "spend: month-end forecast against budget")
        check(page.locator(".latest li").count() == 7, "spend: latest requests listed")
        check("Monitor" in page.inner_text("#pageTitle .crumbs"), "spend: breadcrumb in the header")
        before = page.inner_text(".kpi.sample .v >> nth=0")
        page.click("[data-range='7']")
        page.wait_for_selector("[data-range='7'][aria-pressed='true']")
        check(page.inner_text(".kpi.sample .v >> nth=0") != before, "spend: date range changes the totals")
        page.click("[data-range='30']")
        page.wait_for_selector("[data-range='30'][aria-pressed='true']")
        if shots:
            page.screenshot(path=str(shots / "business_desktop.png"), full_page=True)
            page.screenshot(path=str(shots / "console.png"))

        # --- requests: the sample log, filters, detail page ---
        page.click(".latest li a >> nth=0")
        page.wait_for_selector(".rq-story")
        check("What happened" in page.inner_text("#view"), "requests: a latest request opens its detail page")
        goto(page, base, "#/requests")
        check(page.locator("#rqBody tr[data-id]").count() == 50, "requests: first 50 of the log listed")
        check("500 requests" in page.inner_text("#rqSummary"), "requests: 500 sample requests")
        page.click("#rqMore")
        check(page.locator("#rqBody tr[data-id]").count() == 100, "requests: show more")
        page.select_option("#rqOutcome", "refused")
        check(
            page.locator("#rqBody tr[data-id]").count() == 1 and "code-assistant" in page.inner_text("#rqBody"),
            "requests: refused filter finds the contractor denial",
        )
        page.click("#rqBody tr[data-id] >> nth=0")
        page.wait_for_selector(".rq-story")
        check(
            "Refused by policy" in page.inner_text("#view") and "premium reasoning" in page.inner_text(".rq-story"),
            "requests: refusal explained in plain language",
        )
        check("$0" in page.inner_text(".rq-kpis"), "requests: refused request cost nothing")
        page.go_back()
        page.wait_for_selector("#rqOutcome")
        page.select_option("#rqOutcome", "fallback")
        page.select_option("#rqDay", "10/06/2026")
        n = page.locator("#rqBody tr[data-id]").count()
        check(n >= 6, f"requests: October 6 fallbacks listed ({n})")
        # The oldest is the first request of the overload, sent before the breaker opened.
        page.click(f"#rqBody tr[data-id] >> nth={n - 1}")
        page.wait_for_selector(".rq-story")
        check(
            "529 overloaded" in page.inner_text(".rq-tl") and "Anthropic overload" in page.inner_text("#view"),
            "requests: failed attempt and the incident shown",
        )
        page.go_back()
        page.wait_for_selector("#rqBody tr[data-id]")
        page.click("#rqBody tr[data-id] >> nth=0")  # a later one: the breaker was open, so Claude Haiku was skipped
        page.wait_for_selector(".rq-story")
        check(
            "circuit open" in page.inner_text(".rq-tl") and "circuit breaker was open" in page.inner_text(".rq-story"),
            "requests: skipped attempt (open breaker) shown",
        )
        page.click("[data-view='technical']")
        check(
            page.is_visible("text=Fallback chain") and page.is_visible(".rq-tl details"),
            "requests: technical view shows stage names and details",
        )
        page.click("[data-view='business']")
        page.click("text=Older →")
        page.wait_for_selector(".rq-story")
        check("What happened" in page.inner_text("#view"), "requests: older / newer navigation")
        goto(page, base, "#/requests")
        page.select_option("#rqOutcome", "")
        page.select_option("#rqDay", "")
        page.fill("#rqQ", "bsa-case-notes")
        page.wait_for_timeout(300)
        check(0 < page.locator("#rqBody tr[data-id]").count() < 50, "requests: search narrows the log")
        with page.expect_download() as dl:
            page.click("#rqCsv")
        check(dl.value.suggested_filename == "cypress-harbor-requests.csv", "requests: CSV export of the filtered log")
        page.fill("#rqQ", "")
        goto(page, base, "#/requests/req_nope")
        check("no request with that ID" in page.inner_text("#view"), "requests: unknown ID gets a friendly page")
        goto(page, base, "#/nowhere")
        check("no page here" in page.inner_text("#view"), "unknown route gets a not-found page")

        # --- navigation: palette, shortcuts, collapsible sidebar ---
        page.keyboard.press("Control+k")
        page.wait_for_selector("#palette:not([hidden])")
        page.fill("#palette-input", "traces")
        page.keyboard.press("Enter")
        page.wait_for_function("location.hash === '#/traces'")
        check(page.is_hidden("#palette"), "palette: jumps to a screen and closes")
        page.keyboard.press("g")
        page.keyboard.press("e")
        page.wait_for_function("location.hash === '#/evals'")
        check(True, "shortcut: g e opens Evals")
        page.keyboard.press("?")
        check(page.is_visible("#keys"), "shortcut: ? shows the shortcuts sheet")
        page.keyboard.press("Escape")
        check(page.is_hidden("#keys"), "shortcuts sheet closes with Esc")
        page.click("#nav-collapse")
        check(page.evaluate("document.body.classList.contains('nav-collapsed')"), "sidebar collapses")
        page.click("#nav-collapse")

        # --- overview: this session ---
        goto(page, base, "#/overview/session")
        check(page.locator("#nav a[data-sub='session'][aria-current='page']").count() == 1, "session: sub-page in nav")
        check(page.locator(".kpi").count() == 6, "overview: six KPI tiles")
        bars = page.locator("#ovBody svg.chart rect").count()
        check(bars >= 14, f"overview: hourly chart drawn with simulated history ({bars} bars)")
        page.click("#ovTraffic")
        wait_idle(page)
        page.wait_for_timeout(300)
        served = int(page.locator(".kpi").nth(1).locator(".v").inner_text().replace(",", ""))
        check(served >= 15, f"overview: 20 simulated requests served ({served})")
        check(page.locator("#ovBody .hbar").count() >= 3, "overview: spend by team and model bars")
        check(page.inner_text(".kpi >> nth=0").find("$0.0000") == -1, "overview: spend is non-zero")

        # --- playground: chat with PII redaction and the trace drawer ---
        goto(page, base, "#/playground?tab=chat&sample=pii")
        page.select_option("#chKey", "member-assistant")
        page.click("#chSend")
        wait_idle(page)
        out = page.inner_text("#chOut")
        redacted = "[REDACTED:EMAIL]" in out and "jordan@example.com" not in out
        check(redacted, "playground: the provider gets redacted text")
        check("200" in out and "Fallback chain" in out, "playground: status 200 with stage chips")
        page.click("#chOut [data-trace]")
        page.wait_for_selector("#drawer.open .tl li")
        stages = page.locator("#drawer .tl li").count()
        check(stages == 12, f"trace drawer shows all 12 stages ({stages})")
        check("pii_redact" in page.inner_text("#drawer"), "trace drawer shows the redaction hook")
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        check(not page.is_visible("#drawer .tl"), "Escape closes the drawer")

        # --- playground: compare ---
        goto(page, base, "#/playground?tab=compare")
        page.select_option("#cmpN", "1")
        page.click("#cmpRun")
        wait_idle(page)
        cols = page.locator("#cmpOut .compare-col").count()
        check(cols == 2 and "succeeded" in page.inner_text("#cmpOut"), "compare: two routes side by side")

        # --- playground: resilience (outage -> breaker opens -> half-open probe closes it) ---
        goto(page, base, "#/playground?tab=resilience")
        page.locator('[data-prov="anthropic"] input[data-k="outage"]').check()
        page.wait_for_timeout(300)
        page.click("#rsMix")
        wait_idle(page)
        trans = page.inner_text("#pgBody")
        check("consecutive failures" in trans and "open" in trans, "resilience: anthropic breaker opened")
        check("down" in page.inner_text('[data-prov="anthropic"]'), "resilience: provider card shows the outage")
        page.locator('[data-prov="anthropic"] input[data-k="outage"]').uncheck()
        page.wait_for_timeout(300)
        page.click("#rsWait")
        page.wait_for_timeout(300)
        page.click("#rsMix")
        wait_idle(page)
        check("probe succeeded" in page.inner_text("#pgBody"), "resilience: half-open probe closes the breaker")

        # --- playground: semantic cache ---
        goto(page, base, "#/playground?tab=cache")
        page.click("[data-ex='0']")
        page.click("#smAsk")
        wait_idle(page)
        check(page.inner_text("#smResult").startswith("miss"), "semantic cache: first question misses")
        page.click("[data-ex='1']")
        page.click("#smAsk")
        wait_idle(page)
        check(page.inner_text("#smResult").startswith("hit"), "semantic cache: reworded question hits")

        # --- traces: list, filters, drawer via the URL, back button ---
        goto(page, base, "#/traces")
        rows = page.locator("#trBody tr[data-id]").count()
        check(rows == 50 and "of " in page.inner_text("#trBody"), f"traces: first 50 listed ({rows})")
        page.click("#trMore")
        page.wait_for_timeout(400)
        rows = page.locator("#trBody tr[data-id]").count()
        check(rows > 50, f"traces: show more ({rows})")
        page.select_option("#trOutcome", "ok")
        page.wait_for_timeout(400)
        check(
            all(t.startswith("200") for t in page.locator("#trBody tr[data-id] td:nth-child(4)").all_inner_texts()),
            "traces: outcome filter",
        )
        page.select_option("#trOutcome", "")
        page.fill("#trQ", "gpt-4.1-mini")
        page.wait_for_timeout(500)
        filtered = page.locator("#trBody tr[data-id]").count()
        check(0 < filtered < rows, f"traces: search narrows the list ({filtered})")
        page.locator("#trBody tr[data-id]").first.click()
        page.wait_for_selector("#drawer.open .tl li")
        check(re.search(r"#/traces/tr_demo\d+", page.url) is not None, "traces: drawer is deep-linkable")
        check("Fallback chain" in page.inner_text("#drawer"), "traces: drawer shows the chain stage")
        page.go_back()
        page.wait_for_timeout(400)
        check(page.url.endswith("#/traces") and not page.is_visible("#drawer .tl"), "traces: browser back closes it")

        # --- policies: validate errors, preview, apply, re-run ---
        goto(page, base, "#/policies")
        page.click("#cfValidate")
        wait_idle(page)
        check(page.inner_text("#cfResult").startswith("Valid"), "policies: shipped policies.yaml validates")
        text = page.input_value("#cfText")
        page.fill("#cfText", text.replace("allowed_providers: [ollama]", "allowed_providers: [mars]"))
        page.click("#cfValidate")
        wait_idle(page)
        err = page.inner_text("#cfErrors")
        check("Line 21" in err and "mars" in err, f"policies: schema error shown inline with its line ({err[:60]}…)")
        tighter = text.replace("teams:\n", "teams:\n  digital-banking:\n    deny_aliases: [local-first]\n", 1)
        page.fill("#cfText", tighter)
        page.click("#cfPreview")
        wait_idle(page)
        check("1 decision change" in page.inner_text("#cfChanged"), "policies: preview shows the one changed decision")
        check(page.locator("#cfMatrix td.changed").count() == 1, "policies: changed cell outlined")
        page.click("#cfApply")
        wait_idle(page)
        check("Applied" in page.inner_text("#cfResult"), "policies: apply")
        page.select_option("#cfKey", "online-banking")
        page.select_option("#cfAlias", "local-first")
        page.click("#cfRun")
        wait_idle(page)
        check("403" in page.inner_text("#cfRunOut"), "policies: re-run scenario is refused by the new rule")
        goto(page, base, "#/policies?file=routes")
        page.click("#cfValidate")
        wait_idle(page, 120_000)
        check("6 aliases" in page.inner_text("#cfResult"), "policies: routes.yaml validates with pydantic RouterConfig")

        # --- budgets & keys ---
        goto(page, base, "#/budgets")
        page.fill("#nkLabel", "reg-app")
        page.fill("#nkTeam", "regulated")
        page.click("#nkCreate")
        wait_idle(page)
        check("reg-app" in page.inner_text("#bkKeys") and "simulated" in page.inner_text("#nkShown"), "keys: create")
        page.locator('#bkKeys tr:has-text("mobile-banking") [data-act="pause"]').click()
        wait_idle(page)
        check("paused" in page.inner_text('#bkKeys tr:has-text("mobile-banking")'), "keys: pause")
        page.locator('#bkKeys tr:has-text("mobile-banking") [data-act="unpause"]').click()
        wait_idle(page)
        check("active" in page.inner_text('#bkKeys tr:has-text("mobile-banking")'), "keys: unpause")
        page.click("#bkSpike")
        wait_idle(page)
        page.wait_for_timeout(300)
        check("pause" in page.inner_text("#bkAnom"), "anomaly: looping coding agent reaches pause level")
        page.locator('#bkKeys tr:has-text("code-assistant") [data-act="unpause"]').click()
        wait_idle(page)
        check(
            "override" in page.inner_text('#bkKeys tr:has-text("code-assistant")'),
            "anomaly: unpause is an override",
        )
        check("divided by 25" in page.inner_text("#bkBudgets"), "budgets: the scale-down from routes.yaml is stated")
        check(page.locator("#bkShow tbody tr").count() >= 3, "showback: rows")
        with page.expect_download() as dl:
            page.click("#sbCsv")
        head = Path(dl.value.path()).read_text().splitlines()[0]
        check(head.startswith("team,model,requests"), f"showback CSV header: {head[:40]}…")
        budget_row = page.locator('[data-team="digital-banking"]')
        budget_row.locator("[data-cap]").fill("0.0001")
        budget_row.locator("[data-save]").click()
        wait_idle(page)
        check("of $0.00010" in page.inner_text('[data-team="digital-banking"]'), "budgets: team cap saved")
        goto(page, base, "#/playground?tab=chat")
        page.select_option("#chKey", "online-banking")
        page.click("#chSend")
        wait_idle(page)
        check("402" in page.inner_text("#chOut"), "budgets: a request over the new team cap gets 402")

        # --- MCP ---
        goto(page, base, "#/mcp")
        page.select_option("#mcTeam", "member-services")
        page.select_option("#mcTool", "cases/search_cases")
        page.click("#mcCall")
        wait_idle(page)
        check(page.inner_text("#mcOut").startswith("allow"), "mcp: allowed call")
        check("[REDACTED:EMAIL]" in page.inner_text("#mcLog"), "mcp: arguments audited redacted")
        page.select_option("#mcTeam", "digital-banking")
        page.click("#mcCall")
        wait_idle(page)
        check("not allowed" in page.inner_text("#mcOut"), "mcp: tool outside the allow-list denied")
        page.select_option("#mcTeam", "member-services")
        page.select_option("#mcTool", "cases/close_case")
        page.click("#mcBurst")
        wait_idle(page)
        check("rate_limited" in page.inner_text("#mcOut"), "mcp: burst hits the velocity limit")

        # --- evals ---
        goto(page, base, "#/evals")
        check(page.inner_text("#calThr") == "0.90" and page.inner_text("#calHold") == "0.0%", "evals: calibration")
        check(page.inner_text("#gateResult") == "FAIL", "evals: CI gate example fails on quality")
        page.click("#calRun")
        wait_idle(page)
        same = "Identical to the committed report" in page.inner_text("#calMsg")
        check(same, "evals: in-browser calibration matches the committed report")

        # --- settings ---
        goto(page, base, "#/settings")
        files = page.locator("#stFiles li").count()
        modules = page.evaluate("import('./adapters.js').then(m => m.ROUTER_MODULES.length)")
        expected = modules + 3  # the company table, the engine and models
        check(files == expected, f"settings: {files} files listed with hashes")
        page.locator('tr[data-team="digital-banking"] [data-mode]').select_option("redact")
        page.wait_for_timeout(300)
        goto(page, base, "#/playground?tab=chat&sample=pii")
        page.select_option("#chKey", "reg-app")
        page.click("#chSend")
        wait_idle(page)
        out = page.inner_text("#chOut")
        check("ollama/llama3.1:8b" in out and "[REDACTED:CARD]" in out, "settings + policy: regulated key stays local")

        layout_checks(check, b, base, shots, "demo")
        check(not b.errors, f"no page or console errors {b.errors[:3]}")
        check(not b.failures, f"no failed requests {b.failures[:3]}")
        check(not b.foreign, f"no requests to other hosts {b.foreign[:3]}")
    finally:
        b.close()
        srv.shutdown()
    return check


# ---------------------------------------------------------------------------------------------
# Live mode
# ---------------------------------------------------------------------------------------------


def start_gateway(tmp: Path) -> tuple[subprocess.Popen, int, str]:
    port = free_port()
    admin = "sk-admin-smoke-" + secrets.token_urlsafe(12)
    for f in ("routes.yaml", "policies.yaml"):
        shutil.copy(REPO / "config" / f, tmp / f)
    mcp = (REPO / "config" / "mcp.mock.yaml").read_text().replace("127.0.0.1:4000", f"127.0.0.1:{port}")
    (tmp / "mcp.yaml").write_text(mcp)
    env = {
        **os.environ,
        "ROUTER_MOCK_PROVIDERS": "true",
        "ROUTER_ALLOW_CONFIG_WRITES": "true",
        "ROUTER_ADMIN_KEY": admin,
        "ROUTER_DB_PATH": str(tmp / "router.db"),
        "ROUTER_ROUTES_FILE": str(tmp / "routes.yaml"),
        "ROUTER_POLICIES_FILE": str(tmp / "policies.yaml"),
        "ROUTER_MCP_FILE": str(tmp / "mcp.yaml"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "router.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return proc, port, admin
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("gateway did not start:\n" + (proc.stdout.read().decode() if proc.stdout else ""))


def run_live(p, shots: Path | None, headed: bool) -> Checks:
    check = Checks("live")
    tmp = Path(tempfile.mkdtemp(prefix="console-smoke-"))
    proc, port, admin = start_gateway(tmp)
    origin = f"http://127.0.0.1:{port}"
    base = f"{origin}/console/"
    b = Browser(p, origin, None, headed, ignore_http_errors=True)
    page = b.page
    try:
        page.goto(base + "?mode=live&notour")
        page.wait_for_function("window.__consoleReady === true", timeout=30_000)
        check(page.inner_text("#modeText") == f"Live · connected to 127.0.0.1:{port}", "header badge says live mode")
        page.wait_for_selector(".kpi.sample")
        check(page.locator(".kpi.sample").count() == 8, "business impact loads without a key (static sample data)")
        goto(page, base, "#/overview/session")
        page.wait_for_selector(".error-state")
        check("Admin key needed" in page.inner_text("#view"), "overview asks for the admin key first")

        goto(page, base, "#/settings")
        page.fill("#stKey", admin)
        page.click("#stSave")
        wait_idle(page)
        check(page.inner_text("#stMsg") == "Connected.", "settings: admin key accepted")

        goto(page, base, "#/overview/session")
        page.click("#ovTraffic")
        wait_idle(page, 120_000)
        page.wait_for_timeout(300)
        served = int(page.locator(".kpi").nth(1).locator(".v").inner_text().replace(",", ""))
        check(served >= 18, f"overview: requests served through the real API ({served})")
        check(page.locator("#ovBody .hbar").count() >= 3, "overview: spend by team and model from showback")

        goto(page, base, "#/playground?tab=chat&sample=pii")
        page.select_option("#chKey", "console-risk-analytics")
        page.click("#chSend")
        wait_idle(page)
        check("[simulated" in page.inner_text("#chOut"), "playground: answered by the simulated provider")
        goto(page, base, "#/budgets")
        page.fill("#nkLabel", "reg-live")
        page.fill("#nkTeam", "regulated")
        page.click("#nkCreate")
        wait_idle(page)
        check("sk-router-" in page.inner_text("#nkShown"), "keys: the secret is shown once")
        goto(page, base, "#/playground?tab=chat&sample=pii")
        page.select_option("#chKey", "reg-live")
        page.click("#chSend")
        wait_idle(page)
        out = page.inner_text("#chOut")
        check("ollama/llama3.1:8b" in out and "[REDACTED:EMAIL]" in out, "playground: regulated team local + redacted")

        goto(page, base, "#/playground?tab=resilience")
        page.locator('[data-prov="anthropic"] input[data-k="outage"]').check()
        page.wait_for_timeout(500)
        page.click("#rsMix")
        wait_idle(page, 120_000)
        check("open" in page.inner_text("#pgBody") and "down" in page.inner_text('[data-prov="anthropic"]'),
              "resilience: real breaker opens on the simulated outage")  # fmt: skip
        page.locator('[data-prov="anthropic"] input[data-k="outage"]').uncheck()
        page.wait_for_timeout(300)
        page.click("#rsReset")
        page.wait_for_timeout(500)

        goto(page, base, "#/traces")
        rows = page.locator("#trBody tr[data-id]").count()
        check(rows >= 40, f"traces: listed from GET /admin/traces ({rows})")
        page.fill("#trQ", "anthropic/claude-haiku-4-5")
        page.wait_for_timeout(600)
        check(0 < page.locator("#trBody tr[data-id]").count() <= rows, "traces: search through the API")
        page.locator("#trBody tr[data-id]").first.click()
        page.wait_for_selector("#drawer.open .tl li")
        n = page.locator("#drawer .tl li").count()
        check(n >= 7 and "Policy (YAML / OPA)" in page.inner_text("#drawer"), f"traces: drawer timeline ({n} stages)")
        page.keyboard.press("Escape")

        goto(page, base, "#/policies")
        page.click("#cfValidate")
        wait_idle(page)
        check(page.inner_text("#cfResult").startswith("Valid"), "policies: validated by the gateway")
        check(page.locator("#cfMatrix table").count() == 1, "policies: dry-run matrix from the gateway")

        goto(page, base, "#/budgets")
        page.locator('#bkKeys tr:has-text("reg-live") [data-act="pause"]').click()
        wait_idle(page)
        check("paused" in page.inner_text('#bkKeys tr:has-text("reg-live")'), "keys: pause through the API")

        goto(page, base, "#/mcp")
        page.select_option("#mcTeam", "member-services")
        page.select_option("#mcTool", "cases/search_cases")
        page.click("#mcCall")
        wait_idle(page)
        check(page.inner_text("#mcOut").startswith("allow"), "mcp: call through the gateway to the simulated server")
        check("[REDACTED:EMAIL]" in page.inner_text("#mcLog"), "mcp: audit row shows redacted arguments")

        goto(page, base, "#/evals")
        check(page.inner_text("#calThr") == "0.90", "evals: committed reports load")

        layout_checks(check, b, base, shots, "live")
        check(not b.errors, f"no page or console errors {b.errors[:3]}")
        check(not b.failures, f"no failed requests {b.failures[:3]}")
        check(not b.foreign, f"no requests to other hosts {b.foreign[:3]}")
    finally:
        b.close()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    return check


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pyodide-dir", type=Path, help="local copy of Pyodide 0.26.4 (served instead of jsDelivr)")
    ap.add_argument("--screenshots", type=Path, help="directory to write desktop and mobile screenshots into")
    ap.add_argument("--headed", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="live mode only")
    mode.add_argument("--both", action="store_true", help="demo mode, then live mode")
    args = ap.parse_args()
    if args.pyodide_dir and not (args.pyodide_dir / "pyodide.js").is_file():
        ap.error(f"{args.pyodide_dir} has no pyodide.js")
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2
    results = []
    with sync_playwright() as p:
        if not args.live:
            results.append(run_demo(p, args.pyodide_dir, args.screenshots, args.headed))
        if args.live or args.both:
            results.append(run_live(p, args.screenshots, args.headed))
    passed = sum(c.passed for c in results)
    failed = [f"[{c.label}] {m}" for c in results for m in c.failed]
    print(f"RESULT: {passed} passed, {len(failed)} failed")
    for f in failed:
        print("  FAILED " + f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
