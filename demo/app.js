// Governed AI Gateway console: app shell, hash router, trace drawer and guided tour. Corey Mathie, 2026.
import { DemoAdapter, LiveAdapter, REPO, detectMode, store } from "./adapters.js";
import { SCREENS } from "./screens.js";
import { crumbs, initShell, openKeys, setActive } from "./shell.js";

// Sidebar groups and sub-pages (the console shell is shared in design with the portfolio's other consoles).
const ROUTES = {
  overview: { group: "Monitor", label: "Overview", key: "o", subs: { session: "This session" } },
  traces: { group: "Monitor", label: "Traces", key: "t" },
  playground: { group: "Operate", label: "Playground", key: "p" },
  policies: { group: "Govern", label: "Policies", key: "y" },
  budgets: { group: "Govern", label: "Budgets & Keys", key: "b" },
  mcp: { group: "Govern", label: "MCP tools", key: "m" },
  evals: { group: "Govern", label: "Evals", key: "e" },
  settings: { group: "Configure", label: "Settings", key: "s" },
};
const GROUPS = ["Monitor", "Operate", "Govern", "Configure"];
import { $, $$, decisionIcon, esc, errorState, loading, ms, statusPill, toast, usd } from "./ui.js";

const ICONS = {
  overview: '<path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 8h6V4h-6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  playground: '<path d="M5 4l14 8-14 8z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  traces: '<path d="M4 6h16M4 12h10M4 18h13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="19" cy="12" r="2" fill="currentColor"/>',
  policies: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M9 12l2 2 4-4" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  budgets: '<rect x="3" y="6" width="18" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 10h18M7 15h4" stroke="currentColor" stroke-width="1.8"/>',
  mcp: '<path d="M14 7l3-3 3 3-3 3M10 17l-3 3-3-3 3-3M17 4L7 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  evals: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  settings: '<circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
};

const app = { adapter: null, screen: null, cleanup: null, cache: {} };
window.__console = app;

function renderNav() {
  $("#nav").innerHTML = GROUPS.map((g) => {
    const items = SCREENS.filter((s) => ROUTES[s.id]?.group === g);
    return `<div class="nav-group"><div class="nav-label" id="nl-${g.toLowerCase()}">${esc(g)}</div><div class="nav-items" role="list" aria-labelledby="nl-${g.toLowerCase()}">${items.map((s) => {
      const subs = Object.entries(ROUTES[s.id].subs || {});
      return `<div role="listitem"><a href="#/${s.id}" data-nav="${s.id}" data-route="${s.id}"><svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[s.id] || ""}</svg><span>${esc(s.title)}</span></a>${subs.length ? `<div class="sub">${subs.map(([k, label]) => `<a href="#/${s.id}/${k}" data-route="${s.id}" data-sub="${k}" class="sub-link"><span>${esc(label)}</span></a>`).join("")}</div>` : ""}</div>`;
    }).join("")}</div></div>`;
  }).join("");
}

function parseHash() {
  const raw = (location.hash || "#/overview").replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const [id, ...rest] = path.split("/");
  return { id: id || "overview", arg: rest.join("/") ? decodeURIComponent(rest.join("/")) : "", params: new URLSearchParams(query) };
}

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  if (!app.adapter) return;
  const { id, arg, params } = parseHash();
  const screen = SCREENS.find((s) => s.id === id) || SCREENS[0];
  const subs = ROUTES[screen.id]?.subs || {};
  const sub = subs[arg] ? arg : "";
  setActive(screen.id, sub);
  $("#pageTitle").innerHTML = crumbs(screen.id, sub);
  document.title = `${sub ? subs[sub] : screen.title} · Governed AI Gateway`;
  $("#sidebar").classList.remove("open");
  $("#menuBtn").setAttribute("aria-expanded", "false");
  const screenKey = Object.keys(subs).length ? `${screen.id}/${sub}` : screen.id;
  const sameScreen = app.screen === screenKey;
  if (!sameScreen) {
    if (app.cleanup) { try { app.cleanup(); } catch { /* ignore */ } }
    app.cleanup = null;
    app.screen = screenKey;
    const view = $("#view");
    view.innerHTML = loading(`Loading ${screen.title}…`);
    try {
      app.cleanup = (await screen.render(view, { app, arg, params, navigate, openTrace })) || null;
    } catch (e) {
      console.error(e);
      view.innerHTML = errorState(e, "retryScreen");
      const b = $("#retryScreen");
      if (b) b.onclick = () => { app.screen = null; route(); };
    }
    view.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  } else if (screen.onParams) {
    screen.onParams({ arg, params });
  }
  if (screen.id === "traces" && arg) openTrace(arg, { fromRoute: true });
  else if (screen.id === "traces" && !arg) closeDrawer({ fromRoute: true });
}

// ---------- trace drawer ----------

let lastFocus = null;
function renderAttempts(attempts) {
  return `<div class="attempts">${attempts
    .map((a) => {
      const icon = a.outcome === "ok" ? "✓" : a.outcome === "skipped" ? "–" : "✕";
      const cls = a.outcome === "ok" ? "pass" : a.outcome === "skipped" ? "skip" : "deny";
      const tail = a.outcome === "skipped" ? `breaker ${esc(a.breaker_state)}` : a.outcome === "error" ? `${esc(a.error || "error")}` : `${ms(a.seconds * 1000)}`;
      return `<div class="attempt"><span class="pill ${cls}" aria-hidden="true">${icon}</span><code>${esc(a.deployment)}</code><span class="small muted">${tail}</span></div>`;
    })
    .join("")}</div>`;
}

function drawerHtml(tr) {
  const stages = tr.stages || [];
  const total = stages.reduce((s, x) => s + (x.ms || 0), 0);
  return `
    <div class="drawer-head">
      <div style="min-width:0">
        <div class="small muted">Trace <code>${esc(tr.id)}</code>${tr.simulated ? ' · <span class="tag sim">simulated</span>' : ""}</div>
        <h2 id="drawerTitle" style="margin-top:4px">${esc(tr.alias)} · ${esc(tr.key)} <span class="muted">(${esc(tr.team)})</span></h2>
        <div class="row" style="margin-top:8px">${statusPill(tr.status, tr.outcome)}
          ${tr.served_by ? `<span class="chip">served by ${esc(tr.served_by)}</span>` : ""}
          ${tr.fell_back ? '<span class="chip">fell back</span>' : ""}
          <span class="chip">${ms(tr.latency_ms)}</span>
          <span class="chip">${usd(tr.cost_usd)}</span>
          ${tr.saved_usd ? `<span class="chip hit">saved ${usd(tr.saved_usd)}</span>` : ""}
        </div>
      </div>
      <button class="ghost" id="drawerClose" aria-label="Close trace">✕</button>
    </div>
    <div class="drawer-body">
      ${tr.reason ? `<div class="note ${tr.status >= 400 ? "bad" : ""}" style="margin-bottom:14px">${esc(tr.reason)}</div>` : ""}
      <p class="small muted">Every control the request passed through, in order. ${tr.simulated ? "Timings are on the demo's virtual clock." : "Timings are wall-clock in the gateway."} Traces hold metadata only: no prompt or completion text.</p>
      <ol class="tl">
        ${stages
          .map(
            (s) => `<li>
          <span class="ic ${esc(s.decision)}" aria-hidden="true">${esc(decisionIcon(s.decision))}</span>
          <div class="st"><b>${esc(s.label)}</b><span class="small faint num">${s.ms == null ? "" : ms(s.ms)}</span></div>
          <div class="sm"><span class="pill ${esc(s.decision)}">${esc(s.decision)}</span> ${esc(s.summary)}</div>
          ${s.detail && s.detail.attempts ? renderAttempts(s.detail.attempts) : ""}
          ${s.detail && Object.keys(s.detail).filter((k) => k !== "attempts").length ? `<details><summary>details</summary><pre class="box">${esc(JSON.stringify(Object.fromEntries(Object.entries(s.detail).filter(([k]) => k !== "attempts")), null, 2))}</pre></details>` : ""}
        </li>`,
          )
          .join("")}
      </ol>
      <div class="small faint">Stage time total ${ms(total)} · tokens ${esc(tr.prompt_tokens)} in / ${esc(tr.completion_tokens)} out · key fingerprint <code>${esc(tr.key_fp)}</code> · ${esc(tr.ts)}</div>
    </div>`;
}

export async function openTrace(id, { fromRoute = false } = {}) {
  const drawer = $("#drawer");
  if (!fromRoute) {
    const { id: screen } = parseHash();
    if (screen === "traces") { app.pushedTrace = true; navigate(`#/traces/${encodeURIComponent(id)}`); return; }
  }
  lastFocus = document.activeElement;
  drawer.innerHTML = `<div class="drawer-body">${loading("Loading trace…")}</div>`;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  try {
    const tr = await app.adapter.trace(id);
    drawer.innerHTML = tr ? drawerHtml(tr) : `<div class="drawer-body">${errorState(new Error("No such trace (traces are kept in memory)."))}</div><div style="padding:0 18px"><button id="drawerClose">Close</button></div>`;
  } catch (e) {
    drawer.innerHTML = `<div class="drawer-body">${errorState(e)}<p><button id="drawerClose">Close</button></p></div>`;
  }
  const close = $("#drawerClose");
  if (close) { close.onclick = () => closeDrawer(); close.focus(); }
}

export function closeDrawer({ fromRoute = false } = {}) {
  const drawer = $("#drawer");
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  if (!fromRoute && parseHash().id === "traces" && parseHash().arg) {
    if (app.pushedTrace) history.back();
    else history.replaceState(null, "", "#/traces");
  }
  app.pushedTrace = false;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

// ---------- guided tour ----------

const TOUR = [
  { nav: "overview", title: "Overview", body: "Spend today by team and model, fallback rate, cache savings and anomaly flags, with spend per hour against each hour's 7-day average." },
  { nav: "playground", title: "Playground", body: "Send a request as any key, compare two routes side by side, or take a simulated provider down and watch the circuit breakers and fallback." },
  { nav: "traces", title: "Traces", body: "Every request with its full decision timeline: auth, policy, budgets, anomaly pause, PII hooks, caches, TPM, each fallback attempt and the cost settled." },
  { nav: "policies", title: "Policies", body: "Edit the real config/policies.yaml and routes.yaml, validate them with the gateway's own loaders, preview the decisions, then apply." },
  { nav: "budgets", title: "Budgets & Keys", body: "Team caps, keys, anomaly pause and unpause, and the showback CSV your finance team asks for." },
];
let tourStep = 0;

function showTour(step = 0) {
  tourStep = step;
  const t = TOUR[step];
  const el = $("#tour");
  $$(".tour-target").forEach((x) => x.classList.remove("tour-target"));
  const target = $(`#nav a[data-nav="${t.nav}"]`);
  const narrow = window.innerWidth <= 860;
  if (target && !narrow) target.classList.add("tour-target");
  const rect = target && !narrow ? target.getBoundingClientRect() : null;
  const top = rect ? Math.min(window.innerHeight - 240, Math.max(16, rect.top - 10)) : 90;
  const left = rect ? rect.right + 16 : 16;
  el.innerHTML = `<div class="scrim2"></div><div class="bubble" role="dialog" aria-modal="true" aria-labelledby="tourTitle" style="top:${top}px;left:${left}px">
    <div class="step">Step ${step + 1} of ${TOUR.length}</div>
    <h2 id="tourTitle">${esc(t.title)}</h2>
    <p class="muted" style="margin:0">${esc(t.body)}</p>
    <div class="row spread"><button class="ghost sm" id="tourSkip">Skip tour</button>
      <div class="row">${step > 0 ? '<button class="sm" id="tourBack">Back</button>' : ""}<button class="primary sm" id="tourNext">${step === TOUR.length - 1 ? "Done" : "Next"}</button></div></div>
  </div>`;
  el.classList.add("open");
  $("#tourNext").focus();
  $("#tourNext").onclick = () => (step === TOUR.length - 1 ? endTour() : showTour(step + 1));
  $("#tourSkip").onclick = endTour;
  if ($("#tourBack")) $("#tourBack").onclick = () => showTour(step - 1);
  el.querySelector(".scrim2").onclick = endTour;
}

function endTour() {
  $("#tour").classList.remove("open");
  $("#tour").innerHTML = "";
  $$(".tour-target").forEach((x) => x.classList.remove("tour-target"));
  store.set("gag.tourDone", "1");
}

// ---------- boot ----------

function setBadge(text, cls = "") {
  $("#modeBadge").className = "mode-badge " + cls;
  $("#modeText").textContent = text;
  $("#footMode").textContent = text;
}

async function boot() {
  renderNav();
  initShell({
    home: "Cypress Harbor CU",
    storageKey: "gag",
    navLinks: "#nav a[data-route]",
    routes: ROUTES,
    go: navigate,
    commands: () => [
      { section: "Actions", label: "Send 20 requests from every team", hint: "Overview › This session", hash: "#/overview/session" },
      { section: "Actions", label: "Take a provider down", hint: "Playground › Resilience", hash: "#/playground?tab=resilience" },
      { section: "Actions", label: "Send a prompt with personal data", hint: "Playground › PII redaction", hash: "#/playground?tab=chat&sample=pii" },
      { section: "Actions", label: "Compare two routes", hint: "Playground › Compare", hash: "#/playground?tab=compare" },
      { section: "Actions", label: "Catch a runaway key", hint: "Budgets & Keys › anomaly", hash: "#/budgets?spike=1" },
      { section: "Actions", label: "Preview a policy change", hint: "Policies › dry run", hash: "#/policies" },
      { section: "Actions", label: "Export showback by team", hint: "Budgets & Keys", hash: "#/budgets" },
      { section: "Actions", label: "Take the guided tour", hint: "Help", run: () => showTour(0) },
      { section: "Actions", label: "Show keyboard shortcuts", hint: "Help", run: openKeys },
    ],
  });
  $("#keys-btn").onclick = openKeys;
  $("#repoLink").href = REPO;
  $("#menuBtn").onclick = () => {
    const open = $("#sidebar").classList.toggle("open");
    $("#menuBtn").setAttribute("aria-expanded", String(open));
  };
  $("#tourBtn").onclick = () => showTour(0);
  $("#scrim").onclick = () => closeDrawer();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("#tour").classList.contains("open")) endTour();
      else closeDrawer();
    }
  });
  window.addEventListener("hashchange", route);

  const detected = await detectMode();
  if (detected.mode === "live") {
    app.adapter = new LiveAdapter(detected.info);
    setBadge(app.adapter.label, "live");
  } else {
    if (detected.mode === "live-unreachable") toast("No gateway answered at ./api-mode; running the in-browser demo instead.", "bad");
    app.adapter = new DemoAdapter();
    setBadge("Demo · starting Python…");
    try {
      await app.adapter.init((msg) => ($("#bootText").textContent = msg));
    } catch (e) {
      console.error(e);
      setBadge("Demo · Python failed to load", "err");
      $("#view").innerHTML = errorState(new Error(`${e.message} The same code runs in CI: see tests/test_demo_engine.py and tests/test_demo_console.py.`));
      return;
    }
    setBadge(app.adapter.label);
  }
  document.body.dataset.mode = app.adapter.mode;
  window.__consoleReady = true;
  await route();
  if (!store.get("gag.tourDone") && !new URLSearchParams(location.search).has("notour")) showTour(0);
}

boot();
