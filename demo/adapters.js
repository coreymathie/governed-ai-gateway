// Governed AI Gateway console: data adapters. Corey Mathie, 2026.
//
// DemoAdapter runs the gateway's real router/*.py modules in Pyodide with simulated providers (demo/engine.py).
// LiveAdapter talks to a running gateway's HTTP API. Both expose the same async methods, so every screen works
// in either mode. Mode: ?mode=demo|live, else GET ./api-mode (served by the gateway at /console/api-mode).

export const PYODIDE_INDEX = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
// router/*.py files the demo loads unchanged (pure Python; tests/test_demo_engine.py checks this list).
export const ROUTER_MODULES = [
  "breaker", "fallback", "latency", "tokens", "budgets", "anomaly", "costs", "showback", "pii", "policy",
  "semcache", "mcp_policy", "configcheck", "traces",
];
// Loaded on demand with Pyodide's pydantic, only to validate routes.yaml with the gateway's RouterConfig.
export const LAZY_MODULES = ["models"];
export const REPO = "https://github.com/coreymathie/governed-ai-gateway";

export const SAMPLE_PROMPTS = {
  pii: "Summarize this case: member Jordan Example (jordan@example.com, 202-555-0143) says card 4111 1111 1111 1111 was double charged. Internal note: retry with key sk-test-abcdefghijklmnopqrstuvwx.",
  faq: "How does a member download statements as PDF?",
  release: "Draft a two-sentence in-app message announcing instant card lock in mobile banking.",
  triage: "A member says their debit card was charged twice this month. What should I check first?",
};
const TEAM_PROMPTS = {
  "digital-banking": [SAMPLE_PROMPTS.release, "Suggest three names for a round-up savings feature, one line each."],
  "member-services": [SAMPLE_PROMPTS.faq, SAMPLE_PROMPTS.triage, "How do I reset a member's online banking password?"],
  "risk-analytics": ["Tag these dispute cases as fraud, merchant error, duplicate or member error: unknown gas station charge; cancelled subscription renewed; restaurant charged twice; ATM cash not dispensed."],
};

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

const store = {
  get(k, session = false) {
    try { return (session ? sessionStorage : localStorage).getItem(k); } catch { return null; }
  },
  set(k, v, session = false) {
    try { (session ? sessionStorage : localStorage).setItem(k, v); } catch { /* storage unavailable */ }
  },
  del(k) {
    try { sessionStorage.removeItem(k); localStorage.removeItem(k); } catch { /* storage unavailable */ }
  },
};
export { store };

async function sha256(text) {
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(buf)).slice(0, 6).map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return "n/a";
  }
}

async function fetchText(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`could not fetch ${path} (HTTP ${r.status})`);
  return r.text();
}

export async function fetchJson(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`could not fetch ${path} (HTTP ${r.status})`);
  return r.json();
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (typeof window.loadPyodide === "function") return resolve();
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Could not load Pyodide from cdn.jsdelivr.net (offline or blocked)."));
    document.head.appendChild(s);
  });
}

export async function detectMode() {
  const q = new URLSearchParams(location.search).get("mode");
  if (q === "demo") return { mode: "demo" };
  // The gateway serves the console under /console/; anywhere else (GitHub Pages, a static server) it is the demo,
  // unless ?mode=live asks to look for a gateway.
  if (q !== "live" && !/\/console(\/|$)/.test(location.pathname)) return { mode: "demo" };
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 2500);
    const r = await fetch("./api-mode", { cache: "no-store", signal: ctl.signal });
    clearTimeout(timer);
    if (r.ok && (r.headers.get("content-type") || "").includes("json")) {
      const info = await r.json();
      if (info.mode === "live") return { mode: "live", info };
    }
  } catch { /* not served by a gateway */ }
  if (q === "live") return { mode: "live-unreachable" };
  return { mode: "demo" };
}

// ---------------------------------------------------------------------------------------------
// Demo mode: Pyodide + demo/engine.py
// ---------------------------------------------------------------------------------------------

export class DemoAdapter {
  constructor() {
    this.mode = "demo";
    this.simulated = true;
    this.label = "Demo · runs in your browser";
    this.files = [];
    this.caps = { virtualClock: true, providerKnobs: true, configWrites: true, semanticPlayground: true, mcpCalls: true, calibrate: true };
  }

  async init(progress = () => {}) {
    const t0 = performance.now();
    progress("Loading Python (Pyodide 0.26.4)…");
    await loadScript(PYODIDE_INDEX + "pyodide.js");
    const py = await window.loadPyodide({ indexURL: PYODIDE_INDEX });
    this.py = py;
    progress("Loading PyYAML…");
    await py.loadPackage("pyyaml");
    progress("Fetching router/*.py from this repository…");
    py.FS.mkdirTree("/home/pyodide/router");
    const init = await fetchText("../router/__init__.py");
    py.FS.writeFile("/home/pyodide/router/__init__.py", init);
    for (const m of ROUTER_MODULES) {
      const src = await fetchText(`../router/${m}.py`);
      py.FS.writeFile(`/home/pyodide/router/${m}.py`, src);
      this.files.push({ path: `router/${m}.py`, lines: src.split("\n").length, sha: await sha256(src) });
    }
    const engine = await fetchText("./engine.py");
    py.FS.writeFile("/home/pyodide/demo_engine.py", engine);
    this.files.push({ path: "demo/engine.py", lines: engine.split("\n").length, sha: await sha256(engine), sim: true });
    py.runPython("import demo_engine\nbridge = demo_engine.JsBridge()");
    this.bridge = py.globals.get("bridge");
    this.pyVersion = py.runPython("import sys; sys.version.split()[0]");
    progress("Reading config/*.yaml…");
    for (const [name, file] of [["policies", "policies.yaml"], ["routes", "routes.yaml"], ["mcp", "mcp.example.yaml"]]) {
      const text = await fetchText(`../config/${file}`);
      this.call("set_config_text", { name, text });
    }
    this.bootSeconds = (performance.now() - t0) / 1000;
  }

  call(method, args = {}) {
    return JSON.parse(this.bridge.api(method, JSON.stringify(args)));
  }

  async ensurePydantic() {
    if (this._pydantic) return;
    await this.py.loadPackage("pydantic");
    for (const m of LAZY_MODULES) {
      const src = await fetchText(`../router/${m}.py`);
      this.py.FS.writeFile(`/home/pyodide/router/${m}.py`, src);
      this.files.push({ path: `router/${m}.py`, lines: src.split("\n").length, sha: await sha256(src), lazy: true });
    }
    this._pydantic = true;
  }

  async overview() { return this.call("overview"); }
  async routes() { return this.call("routes_view"); }
  async chat({ key, alias, text, max_tokens, temperature }) {
    const tr = this.call("request", { key_label: key, alias, text, max_tokens: max_tokens || null, temperature: Number(temperature) });
    return {
      status: tr.status, ok: tr.status < 400, trace_id: tr.id, served_by: tr.served_by, latency_ms: tr.latency_ms,
      cost_usd: tr.cost_usd, saved_usd: tr.saved_usd, cache: tr.cache, tokens: tr.prompt_tokens + tr.completion_tokens,
      fell_back: tr.fell_back, response: tr.response || null, sent: tr.sent ?? null, error: tr.status >= 400 ? tr.reason : null,
      stages: tr.stages, simulated: true,
    };
  }
  async traffic(n, scenario = "mix") { return this.call("traffic", { n, scenario }); }
  async providers() { return { enabled: true, ...this.call("providers_view") }; }
  async setProvider(provider, changes) { this.call("set_provider", { provider, changes }); }
  async resetBreakers() { this.call("reset_breakers"); }
  async advance(seconds) { this.call("advance", { seconds }); }
  async traces(f = {}) { return this.call("trace_rows", { limit: f.limit || 200, team: f.team || "", outcome: f.outcome || "", q: f.q || "" }); }
  async trace(id) { return this.call("trace", { trace_id: id }); }
  async keys() { return this.call("keys_view"); }
  async teams() { return this.call("settings_view").teams; }
  async playgroundKeys() { return (await this.keys()).filter((k) => !k.revoked_at).map((k) => ({ label: k.label, team: k.team })); }
  async createKey(label, team) { return this.call("create_key", { label, team }); }
  async revokeKey(k) { this.call("revoke_key", { label: k.label }); }
  async pauseKey(k) { this.call("pause_key", { label: k.label }); return { status: "paused" }; }
  async unpauseKey(k) { return { status: this.call("unpause_key", { label: k.label }) }; }
  async budgets() { return this.call("budgets_view"); }
  async setBudget(scope, name, { daily_usd, tpm }) {
    if (scope === "org") this.call("set_org_cap", { daily_usd });
    else if (scope === "team") {
      if (daily_usd != null) this.call("set_team_cap", { team: name, daily_usd });
      if (tpm != null) this.call("set_team_tpm", { team: name, tpm });
    } else this.call("set_key_cap", { label: name, daily_usd });
  }
  async showback(groupBy) { return JSON.parse(this.bridge.showback(groupBy)); }
  async showbackCsv(groupBy) { return this.call("showback_csv", { group_by: groupBy }); }
  async config(name) { return this.call("config", { name }); }
  async validateConfig(name, text) {
    if (name === "routes") await this.ensurePydantic();
    return this.call("validate_config", { name, text });
  }
  async applyConfig(name, text) {
    if (name === "routes") await this.ensurePydantic();
    return this.call("apply_config", { name, text });
  }
  async dryRun(args) { return this.call("dry_run", { teams: args.teams || null, aliases: args.aliases || null, max_tokens: args.max_tokens || null, text: args.text ?? null }); }
  async mcp() {
    const v = this.call("mcp_view");
    return { ...v, log: v.log.map((r) => ({ ...r, time: `t+${r.t}s` })) };
  }
  async mcpCall(team, server, tool, args) { return this.call("mcp_call", { team, server, tool, arguments: args }); }
  async audit(category) {
    const rows = this.call("governance").audit;
    return category ? rows.filter((r) => r.category === category) : rows;
  }
  async settings() { return this.call("settings_view"); }
  async setSetting(name, value) {
    if (name === "breakers") this.call("set_breakers_enabled", { enabled: value });
    else if (name === "auto_pause") this.call("set_auto_pause", { enabled: value });
    else if (name === "cache") this.call("set_cache", { enabled: value });
    else if (name === "semantic") this.call("set_semantic_team", { team: value.team, enabled: value.enabled });
    else if (name === "content") this.call("set_content_policy", { team: value.team, mode: value.mode, log_content: value.log_content });
  }
  async semanticAsk(team, text, threshold) { return this.call("semantic_ask", { team, text, threshold }); }
  async semanticView() { return this.call("semantic_view"); }
  async calibrate(target) {
    const pairs = await fetchText("../evals/semcache_pairs.jsonl");
    return JSON.parse(this.bridge.calibrate(pairs, target));
  }
  async reset() { this.call("reset"); }
}

// ---------------------------------------------------------------------------------------------
// Live mode: the gateway's HTTP API
// ---------------------------------------------------------------------------------------------

export class LiveAdapter {
  constructor(info) {
    this.mode = "live";
    this.info = info || {};
    this.simulated = !!this.info.mock_providers;
    this.base = new URL("../", location.href).href.replace(/\/$/, "");
    this.host = location.host;
    this.label = `Live · connected to ${this.host}`;
    this.files = [];
    this.caps = {
      virtualClock: false, providerKnobs: !!this.info.mock_providers, configWrites: !!this.info.config_writes,
      semanticPlayground: false, mcpCalls: !!this.info.mock_providers, calibrate: false,
    };
  }

  async init() {}

  get adminKey() { return store.get("gag.adminKey", true) || store.get("gag.adminKey") || ""; }
  setAdminKey(value, remember) {
    store.del("gag.adminKey");
    if (value) store.set("gag.adminKey", value, !remember);
  }
  get appKeys() {
    try { return JSON.parse(store.get("gag.appKeys", true) || "{}"); } catch { return {}; }
  }
  saveAppKey(label, entry) {
    const all = this.appKeys;
    if (entry) all[label] = entry; else delete all[label];
    store.set("gag.appKeys", JSON.stringify(all), true);
  }

  async req(path, { method = "GET", body, key, raw = false } = {}) {
    const token = key ?? this.adminKey;
    if (!token && (path.startsWith("/admin") || path === "/metrics")) {
      throw new ApiError(401, "Enter the admin key in Settings to load this screen.");
    }
    const headers = {};
    if (token) headers.Authorization = "Bearer " + token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let r;
    try {
      r = await fetch(this.base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new ApiError(0, `Could not reach the gateway at ${this.host} (${e.message}).`);
    }
    if (raw) return r;
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) {
      let msg = (data && (data.detail || (data.errors && data.errors.map((e) => e.message).join("; ")))) || `HTTP ${r.status}`;
      if (typeof msg !== "string") msg = JSON.stringify(msg);
      if (r.status === 401 || r.status === 403) msg = this.adminKey ? `The gateway refused the key (${r.status}): ${msg}` : "Enter the admin key in Settings to load this screen.";
      throw new ApiError(r.status, msg, data);
    }
    return data;
  }

  async overview() { return this.req("/admin/overview"); }
  async routes() { return this.req("/admin/routes"); }

  async chat({ key, alias, text, max_tokens, temperature }) {
    const entry = this.appKeys[key];
    const secret = entry ? entry.key : key;
    const body = { model: alias, messages: [{ role: "user", content: text }], temperature: Number(temperature) };
    if (max_tokens) body.max_tokens = Number(max_tokens);
    const t0 = performance.now();
    const r = await this.req("/v1/chat/completions", { method: "POST", body, key: secret, raw: true });
    const data = await r.json().catch(() => null);
    const traceId = r.headers.get("x-router-trace-id");
    let trace = null;
    if (traceId) { try { trace = await this.trace(traceId); } catch { trace = null; } }
    const out = {
      status: r.status, ok: r.ok, trace_id: traceId, latency_ms: trace ? trace.latency_ms : Math.round(performance.now() - t0),
      served_by: r.headers.get("x-router-used-provider") ? `${r.headers.get("x-router-used-provider")}/${r.headers.get("x-router-used-model")}` : null,
      cache: r.headers.get("x-router-cache") || (trace && trace.cache) || null, stages: trace ? trace.stages : [],
      cost_usd: trace ? trace.cost_usd : null, saved_usd: trace ? trace.saved_usd : 0, fell_back: trace ? trace.fell_back : false,
      tokens: trace ? trace.prompt_tokens + trace.completion_tokens : (data && data.usage ? data.usage.total_tokens : null),
      response: r.ok && data && data.choices ? data.choices.map((c) => c.message && c.message.content).join("\n") : null,
      sent: null, error: r.ok ? null : (data && data.detail) || `HTTP ${r.status}`, simulated: this.simulated,
    };
    return out;
  }

  async ensureTeamKey(team) {
    const label = `console-${team}`;
    if (this.appKeys[label]) return label;
    const created = await this.createKey(label, team);
    return created.label;
  }

  async traffic(n, scenario = "mix") {
    const teams = scenario === "spike" ? ["risk-analytics"] : ["digital-banking", "member-services", "risk-analytics", "digital-banking", "member-services"];
    const aliasFor = { "digital-banking": "smart-fast", "member-services": "smart-fast", "risk-analytics": "cheap-batch" };
    const routes = await this.routes();
    const keys = {};
    for (const t of new Set(teams)) keys[t] = await this.ensureTeamKey(t);
    const outcomes = {};
    let i = 0;
    const worker = async () => {
      while (i < n) {
        const j = i++;
        const team = teams[j % teams.length];
        const prompts = TEAM_PROMPTS[team];
        let alias = scenario === "spike" ? "smart-fast" : aliasFor[team];
        if (!routes[alias]) alias = Object.keys(routes)[0];
        const res = await this.chat({ key: keys[team], alias, text: prompts[j % prompts.length], max_tokens: 256, temperature: team === "member-services" ? 0 : 0.7 });
        const o = res.ok ? (res.cache && res.cache !== "miss" ? "cache_hit" : "ok") : res.status >= 500 ? "error" : "rejected";
        outcomes[o] = (outcomes[o] || 0) + 1;
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
    return { sent: n, outcomes };
  }

  async providers() {
    const [mock, circuits, routes] = await Promise.all([this.req("/admin/mock/providers"), this.req("/admin/circuits"), this.routes()]);
    const states = Object.fromEntries((circuits.breakers || []).map((b) => [b.deployment, b]));
    const deps = [...new Set(Object.values(routes).flatMap((r) => r.targets))].sort();
    const names = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Gemini", ollama: "Ollama (local)" };
    const byProvider = {};
    for (const d of deps) {
      const p = d.split("/")[0];
      const knobs = (mock.providers || {})[p] || {};
      const b = states[d] || { state: "closed", consecutive_failures: 0 };
      const entry = byProvider[p] || (byProvider[p] = { provider: p, label: names[p] || p, deployments: [], outage: !!knobs.outage, rate_limited: !!knobs.rate_limited, latency_ms: knobs.latency_ms ?? null, error_rate: knobs.error_rate ?? 0 });
      const lat = (circuits.latency || []).find((l) => l.deployment === d && l.kind === "latency");
      entry.deployments.push({ id: d, breaker: b.state, consecutive_failures: b.consecutive_failures, ewma_ms: lat ? Math.round(lat.ewma_s * 1000) : null });
    }
    return { enabled: !!mock.enabled, providers: Object.values(byProvider), transitions: circuits.transitions || [] };
  }
  async setProvider(provider, changes) { await this.req(`/admin/mock/providers/${encodeURIComponent(provider)}`, { method: "PUT", body: changes }); }
  async resetBreakers() { await this.req("/admin/circuits/reset", { method: "POST" }); }
  async advance() {}
  async traces(f = {}) {
    const q = new URLSearchParams({ limit: String(f.limit || 200) });
    if (f.team) q.set("team", f.team);
    if (f.outcome) q.set("outcome", f.outcome);
    if (f.q) q.set("q", f.q);
    return this.req(`/admin/traces?${q}`);
  }
  async trace(id) { return this.req(`/admin/traces/${encodeURIComponent(id)}`); }

  async keys() {
    const [keys, holds, anomalies, usage, budgets] = await Promise.all([
      this.req("/admin/keys"), this.req("/admin/key-holds"), this.req("/admin/anomalies"), this.req("/admin/usage/today"), this.req("/admin/budgets"),
    ]);
    const verdict = Object.fromEntries(anomalies.map((a) => [`${a.label}|${a.team}`, a]));
    const spend = Object.fromEntries((usage.per_key || []).map((u) => [`${u.label}|${u.team}`, u.spend_usd]));
    return keys.map((k) => {
      const a = verdict[`${k.label}|${k.team}`] || {};
      return { ...k, hold: (holds[k.id] || {}).state || null, verdict: a.verdict || "ok", multiple: a.multiple || 0, spent_today_usd: spend[`${k.label}|${k.team}`] || 0, cap_usd: null, _budgets: budgets };
    });
  }
  async teams() {
    const keys = await this.req("/admin/keys");
    const pol = await this.req("/admin/policies").catch(() => ({ teams: {} }));
    return [...new Set([...keys.map((k) => k.team), ...Object.keys(pol.teams || {}), "digital-banking", "member-services", "risk-analytics"])].sort();
  }
  async playgroundKeys() {
    return Object.entries(this.appKeys).map(([label, e]) => ({ label, team: e.team }));
  }
  async createKey(label, team) {
    const created = await this.req("/admin/keys", { method: "POST", body: { label, team } });
    this.saveAppKey(label, { key: created.key, team, id: created.id });
    return created;
  }
  async revokeKey(k) {
    await this.req(`/admin/keys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
    const stored = Object.entries(this.appKeys).find(([, e]) => e.id === k.id);
    if (stored) this.saveAppKey(stored[0], null);
  }
  async pauseKey(k) { return this.req(`/admin/keys/${encodeURIComponent(k.id)}/pause`, { method: "POST" }); }
  async unpauseKey(k) { return this.req(`/admin/keys/${encodeURIComponent(k.id)}/unpause`, { method: "POST" }); }
  async budgets() {
    const b = await this.req("/admin/budgets");
    const used = b.tpm_in_use || {};
    return {
      org: { spent_usd: b.org.daily.spent_usd, cap_usd: b.org.daily.cap_usd || 0, monthly: b.org.monthly },
      teams: Object.entries(b.teams || {}).map(([team, r]) => ({ team, spent_usd: r.daily.spent_usd, cap_usd: r.daily.cap_usd || 0, tpm: r.tpm || 0, tpm_used: used[`team:${team}`] || 0, monthly: r.monthly })),
      overrides: b.overrides || [], warnings: b.warnings || [],
    };
  }
  async setBudget(scope, name, { daily_usd, tpm }) {
    const body = { daily_usd: daily_usd ?? null, tpm: tpm ?? null };
    await this.req(`/admin/budgets/${scope}/${encodeURIComponent(name)}`, { method: "PUT", body });
  }
  async showback(groupBy) {
    const r = await this.req(`/admin/showback?group_by=${encodeURIComponent(groupBy)}`);
    const cols = groupBy.split(",").flatMap((d) => (d === "key" ? ["key_label", "key_fp"] : [d]));
    return { columns: cols, rows: r.rows, period: r.period };
  }
  async showbackCsv(groupBy) {
    const r = await this.req(`/admin/showback?group_by=${encodeURIComponent(groupBy)}&format=csv`, { raw: true });
    if (!r.ok) throw new ApiError(r.status, `HTTP ${r.status}`);
    return r.text();
  }
  async config(name) { return this.req(`/admin/config/${name}`); }
  async validateConfig(name, text) { return this.req(`/admin/config/${name}/validate`, { method: "POST", body: { text } }); }
  async applyConfig(name, text) {
    try {
      return await this.req(`/admin/config/${name}`, { method: "PUT", body: { text } });
    } catch (e) {
      if (e.status === 400 && e.body && e.body.errors) return e.body;
      throw e;
    }
  }
  async dryRun(args) {
    const body = {};
    for (const k of ["teams", "aliases", "max_tokens", "text"]) if (args[k] != null) body[k] = args[k];
    try {
      return await this.req("/admin/policies/dry-run", { method: "POST", body });
    } catch (e) {
      if (e.status === 400 && e.body && e.body.errors) return e.body;
      throw e;
    }
  }
  async mcp() {
    const [cfg, audit] = await Promise.all([this.req("/admin/mcp"), this.req("/admin/audit?category=mcp&limit=40")]);
    const servers = Object.fromEntries(Object.keys(cfg.servers).map((s) => [s, []]));
    const teams = Object.keys(cfg.teams).sort();
    const allowed = {};
    for (const t of teams) {
      allowed[t] = {};
      for (const s of Object.keys(cfg.servers)) allowed[t][s] = cfg.teams[t].filter((e) => e.server === s).map((e) => e.tool);
    }
    const log = audit.map((a) => {
      let d = {};
      try { d = typeof a.detail === "string" ? JSON.parse(a.detail) : a.detail || {}; } catch { d = {}; }
      return { time: a.ts, team: a.team, tool: a.subject, decision: a.action, reason: d.reason || "", args: d.args || d.arguments || null, redactions: d.redactions || d.findings || {} };
    });
    return { servers, teams, allowed, log, config: cfg, patterns: true };
  }
  async mcpCall(team, server, tool, args) {
    const label = await this.ensureTeamKey(team);
    const secret = this.appKeys[label].key;
    const msg = { jsonrpc: "2.0", id: Date.now(), method: "tools/call", params: { name: tool, arguments: args || {} } };
    const r = await this.req(`/mcp/${encodeURIComponent(server)}`, { method: "POST", body: msg, key: secret, raw: true });
    const data = await r.json().catch(() => ({}));
    const err = data.error;
    return {
      team, tool: `${server}/${tool}`, decision: r.headers.get("x-router-mcp-decision") || (err ? "deny" : "allow"),
      reason: err ? err.message : "", result: data.result ? (data.result.content || []).map((c) => c.text).join(" ") : null,
      retry_after: r.headers.get("retry-after"),
    };
  }
  async audit(category) { return this.req(`/admin/audit?limit=60${category ? `&category=${category}` : ""}`); }
  async settings() {
    const [pol, sem, circuits] = await Promise.all([this.req("/admin/policies"), this.req("/admin/semantic-cache"), this.req("/admin/circuits")]);
    return { policies: pol, semantic: sem, breaker_config: circuits.config, strategies: circuits.strategies, live: true };
  }
  async setSetting() { throw new Error("In live mode these settings live in config/routes.yaml: edit it on the Policies screen."); }
  async calibrate() { throw new Error("Calibration re-runs in demo mode; live mode shows the committed report."); }
  async reset() {}
}
