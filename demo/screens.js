// Governed AI Gateway console: screens. Corey Mathie, 2026.
// Each screen renders into the view with render(view, ctx) and may return a cleanup function.
import { SAMPLE_PROMPTS, fetchJson, store } from "./adapters.js";
import {
  $, $$, busy, compactCharts, curves, download, empty, errorState, esc, hbars, hourlyChart, int, loading, measuredTag, ms, pct,
  realTag, scatter, simTag, stackedDaily, stageChips, statusPill, toast, usd,
} from "./ui.js";

const head = (title, sub, actions = "") =>
  `<div class="page-head"><div><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div><div class="row">${actions}</div></div>`;
const opt = (v, label, sel) => `<option value="${esc(v)}" ${sel ? "selected" : ""}>${esc(label ?? v)}</option>`;
const modeNote = (A) =>
  A.mode === "demo"
    ? `${realTag("real router/*.py")} ${simTag("simulated providers")}`
    : A.simulated ? `${realTag("live gateway")} ${simTag("simulated providers (ROUTER_MOCK_PROVIDERS)")}` : realTag("live gateway");

// =============================================================================================
// Overview
// =============================================================================================


// ---------- Overview › Business impact (sample company) ----------

const RANGES = [[7, "7 days"], [30, "30 days"], [90, "90 days"]];
const TEAM_LABELS = {
  "member-services": "Member services", "digital-banking": "Digital banking", "risk-analytics": "Risk analytics",
  lending: "Lending", compliance: "Compliance", "it-engineering": "IT and engineering", marketing: "Marketing",
};
const TEAM_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "#22d3ee", "#94a3b8"];
const money = (n) => {
  n = Number(n || 0);
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `$${(n / 1e3).toFixed(1)}K`;
  return "$" + Math.round(n).toLocaleString("en-US");
};
const big = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}K` : int(n));
const shortDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

function ovTabs(active) {
  const tabs = [["business", "Business impact", "#/overview"], ["session", "This session", "#/overview/session"]];
  return `<div class="tabs page-tabs" role="tablist" aria-label="Overview">${tabs.map(([k, label, href]) => `<a role="tab" href="${href}" aria-selected="${k === active}" id="ovtab-${k}">${label}</a>`).join("")}</div>`;
}

function spark(values, color = "var(--accent)") {
  if (!values.length) return "";
  const W = 110, H = 26, min = Math.min(...values), span = Math.max(...values) - min || 1;
  const pts = values.map((v, i) => [(i / Math.max(1, values.length - 1)) * (W - 4) + 2, H - 3 - ((v - min) / span) * (H - 6)]);
  const [lx, ly] = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><path d="${pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("")}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.3" fill="${color}"/></svg>`;
}

function delta(cur, prev, { better = "up", kind = "pct" } = {}) {
  if (prev == null || !isFinite(prev) || prev === 0) return '<span class="delta flat">no prior period</span>';
  const change = kind === "pts" ? (cur - prev) * 100 : ((cur - prev) / Math.abs(prev)) * 100;
  if (Math.abs(change) < 0.05) return '<span class="delta flat">no change</span>';
  const up = change > 0, good = (better === "up") === up;
  const label = kind === "pts" ? `${up ? "+" : "−"}${Math.abs(change).toFixed(2)} pts` : `${up ? "+" : "−"}${Math.abs(change).toFixed(1)}%`;
  return `<span class="delta ${good ? "good" : "bad"}" title="vs the previous period"><span aria-hidden="true">${up ? "▲" : "▼"}</span> ${label}</span>`;
}

function agg(days) {
  const t = { requests: 0, spend: 0, saved: 0, fallbacks: 0, failed: 0, denied: 0, pii: 0, regulated: 0, pauses: 0, teams: {} };
  for (const d of days) {
    t.requests += d.requests; t.spend += d.spend_usd; t.saved += d.saved_usd; t.fallbacks += d.fallbacks; t.failed += d.failed;
    t.denied += d.policy_denied; t.pii += d.pii_redacted; t.regulated += d.regulated_on_prem; t.pauses += d.anomaly_pauses;
    for (const [k, v] of Object.entries(d.teams)) {
      const x = (t.teams[k] ||= { requests: 0, spend: 0, saved: 0 });
      x.requests += v.requests; x.spend += v.spend_usd; x.saved += v.saved_usd;
    }
  }
  t.per1k = t.requests ? (t.spend / t.requests) * 1000 : 0;
  t.availability = t.requests ? 1 - t.failed / t.requests : 1;
  return t;
}

function kpi(label, value, sub, d, sp, cls = "") {
  return `<div class="kpi sample ${cls}"><div class="l">${esc(label)}</div><div class="v">${value}</div><div class="kpi-foot">${d}${sp}</div><div class="s">${sub}</div></div>`;
}

async function renderBusiness(view, app) {
  view.innerHTML = head("Overview", "What AI costs the business, and what the gateway controls.") + ovTabs("business") + loading("Loading the sample company…");
  if (!app.cache.company) app.cache.company = await fetchJson("./data/sample_company.json");
  const data = app.cache.company, co = data.company;
  const range = app.cache.range || 30;
  const days = data.days.slice(-range);
  const prevDays = data.days.length >= range * 2 ? data.days.slice(-range * 2, -range) : null;
  const t = agg(days), p = prevDays ? agg(prevDays) : null;
  const budget = data.teams.reduce((a, x) => a + x.monthly_budget_usd, 0) * (range / 30);
  const anomalyIn = days.some((d) => d.date === data.anomaly.date);
  const series = (f) => days.map(f);
  const period = `${shortDate(days[0].date)} – ${shortDate(days[days.length - 1].date)}, 2026`;
  const teamSeries = data.teams.map((x, i) => ({ key: x.team, label: TEAM_LABELS[x.team] || x.team, color: TEAM_COLORS[i % TEAM_COLORS.length] }));
  view.innerHTML = head(
    "Overview",
    `What AI costs <b>${esc(co.name)}</b> and what the gateway controls. ${`<span class="tag sample">sample company</span>`} Fictional data, generated for this demo, so the gateway can be judged at business scale.`,
    `<div class="seg" role="group" aria-label="Date range">${RANGES.map(([n, label]) => `<button type="button" data-range="${n}" aria-pressed="${n === range}">${label}</button>`).join("")}</div><a class="btn primary" href="#/playground">Send a request</a>`,
  ) + ovTabs("business") + `
  <div class="note sample" role="note"><b>Sample company data.</b> ${esc(co.name)} is fictional: ${int(co.employees)} employees, ${data.teams.length} teams, ${data.teams.reduce((a, x) => a + x.apps.length, 0)} AI applications, four model providers. These numbers come from <code>${esc(data.generated_by)}</code> (seed ${esc(data.seed)}), not from a real deployment. Measured results are on <a href="#/evals">Evals</a>; requests sent in this tab are on <a href="#/overview/session">This session</a>.</div>
  <p class="small muted period">${esc(period)} · ${range} days${p ? ` · compared with the ${range} days before` : ""}</p>
  <div class="kpis biz">
    ${kpi("AI spend", money(t.spend), `${Math.round((t.spend / budget) * 100)}% of ${money(budget)} in team budgets`, delta(t.spend, p?.spend, { better: "down" }), spark(series((d) => d.spend_usd)))}
    ${kpi("Requests served", big(t.requests), `${big(Math.round(t.requests / range))} a day across ${data.teams.reduce((a, x) => a + x.apps.length, 0)} apps`, delta(t.requests, p?.requests), spark(series((d) => d.requests)))}
    ${kpi("Cost per 1,000 requests", `$${t.per1k.toFixed(2)}`, "after caching and cheaper routes", delta(t.per1k, p?.per1k, { better: "down" }), spark(series((d) => (d.spend_usd / d.requests) * 1000)))}
    ${kpi("Saved by the cache", money(t.saved), "repeated questions answered at $0", delta(t.saved, p?.saved), spark(series((d) => d.saved_usd), "var(--chart-3)"))}
    ${kpi("Requests that succeeded", pct(t.availability, 3), `${int(t.fallbacks)} rescued by fallback · ${int(t.failed)} failed`, delta(t.availability, p?.availability, { kind: "pts" }), spark(series((d) => 1 - d.failed / d.requests), "var(--chart-3)"))}
    ${kpi("Spend stopped by anomaly pause", anomalyIn ? money(data.anomaly.prevented_usd) : "$0", anomalyIn ? `runaway ${esc(data.anomaly.key)} paused at ${data.anomaly.baseline_multiple}× baseline` : "no runaway keys in this period", anomalyIn ? '<span class="delta good">caught within the hour</span>' : '<span class="delta flat">none needed</span>', "", anomalyIn ? "warn" : "")}
    ${kpi("Policy decisions enforced", int(t.denied), "model, residency and size rules that said no", delta(t.denied, p?.denied, { better: "down" }), spark(series((d) => d.policy_denied), "var(--chart-4)"))}
    ${kpi("Regulated requests kept on-prem", big(t.regulated), "BSA and dispute evidence never left the network", delta(t.regulated, p?.regulated), spark(series((d) => d.regulated_on_prem)))}
  </div>
  <section class="card"><div class="card-head"><div><h2>Daily AI spend by team</h2><p>Every request is attributed to a team and an app before it reaches a provider.</p></div><span class="tag sample">sample</span></div>
    ${stackedDaily(days.map((d) => ({ label: shortDate(d.date), short: shortDate(d.date), values: Object.fromEntries(Object.entries(d.teams).map(([k, v]) => [k, v.spend_usd])) })), teamSeries, { compact: compactCharts(), ariaLabel: "Daily AI spend by team", format: (v) => "$" + Math.round(v).toLocaleString("en-US") })}
    <div class="legend">${teamSeries.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>
  </section>
  <div class="grid g2" style="margin-top:16px">
    <section class="card"><div class="card-head"><div><h2>Budgets by team</h2><p>Spend in the period against each team's budget for the same length of time.</p></div><span class="tag sample">sample</span></div>
      ${budgetTable(data.teams, t, range)}</section>
    <div class="stack">
      <section class="card"><div class="card-head"><div><h2>Spend by model</h2><p>Last 30 days. On-prem Llama serves the regulated work at near-zero marginal cost.</p></div><span class="tag sample">sample</span></div>
        ${hbars(data.models.map((m, i) => ({ name: m.model, value: m.spend_30d_usd * (range / 30), color: TEAM_COLORS[(i + 1) % TEAM_COLORS.length] })), { format: money })}</section>
      <section class="card"><div class="card-head"><div><h2>Provider incidents</h2><p>Outages the fallback chain absorbed.</p></div><span class="tag sample">sample</span></div>
        <ol class="events">${data.incidents.map((x) => `<li class="ev-reliability"><span class="ev-dot" aria-hidden="true"></span><div><div class="ev-meta">${esc(shortDate(x.date))} · ${esc(x.provider)} · ${x.duration_minutes} min</div><h3>${esc(x.what)}</h3><p>${esc(x.handled)}</p></div></li>`).join("")}</ol></section>
    </div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <section class="card"><div class="card-head"><div><h2>Governance</h2><p>What finance, risk and examiners ask the platform team.</p></div><span class="tag sample">sample</span></div>
      <ul class="checklist">
        <li><span class="ck" aria-hidden="true">✓</span><div><b>Requests attributed to a team and app</b><span class="small muted">Chargeback-ready; nothing reaches a provider unattributed</span></div><span class="cv">${pct(data.governance.requests_with_attribution, 0)}</span></li>
        <li><span class="ck" aria-hidden="true">✓</span><div><b>Audit trail verified intact</b><span class="small muted">Hash-chained admin and policy changes</span></div><span class="cv">${pct(data.governance.audit_chain_verified_rate, 0)}</span></li>
        <li><span class="ck" aria-hidden="true">✓</span><div><b>Prompts with personal data redacted</b><span class="small muted">Member-services hook replaces emails, phones and card numbers</span></div><span class="cv">${big(t.pii)}</span></li>
        <li><span class="ck" aria-hidden="true">✓</span><div><b>Prompt and completion logging</b><span class="small muted">Metadata only unless a team opts in</span></div><span class="cv small">${esc(data.governance.content_logging_default)}</span></li>
        <li><span class="ck" aria-hidden="true">✓</span><div><b>App keys rotated in the last 90 days</b><span class="small muted">Secrets shown once, stored hashed</span></div><span class="cv">${int(data.governance.keys_rotated_90d)}</span></li>
      </ul></section>
    <section class="card"><div class="card-head"><div><h2>Recent activity</h2></div><span class="tag sample">sample</span></div>
      <ol class="events">${data.notable.map((n) => `<li class="ev-${esc(n.kind)}"><span class="ev-dot" aria-hidden="true"></span><div><div class="ev-meta">${esc(shortDate(n.date))} · ${esc({ policy: "Policy", reliability: "Reliability", finops: "FinOps", risk: "Risk" }[n.kind] || n.kind)}</div><h3>${esc(n.title)}</h3><p>${esc(n.detail)}</p></div></li>`).join("")}</ol></section>
  </div>
  <p class="small faint" style="margin-top:12px">Assumptions: ${esc(data.assumptions.note)}</p>`;
  $$("[data-range]", view).forEach((b) => b.addEventListener("click", () => { app.cache.range = Number(b.dataset.range); renderBusiness(view, app); }));
}

function budgetTable(teams, t, range) {
  return `<div class="table-wrap"><table class="budgets"><thead><tr><th>Team</th><th class="num">Spend</th><th>Of budget</th></tr></thead><tbody>${teams.map((x) => {
    const spend = t.teams[x.team]?.spend || 0, cap = x.monthly_budget_usd * (range / 30), used = spend / cap;
    const cls = used >= 0.9 ? "bad" : used >= 0.75 ? "warn" : "ok";
    return `<tr><td>${esc(TEAM_LABELS[x.team] || x.team)}<span class="sub">${x.apps.map(esc).join(", ")}</span></td><td class="num">${money(spend)}</td><td><span class="meter ${cls}" role="img" aria-label="${Math.round(used * 100)}% of budget"><i style="width:${Math.min(100, used * 100).toFixed(1)}%"></i></span> <span class="nw small">${Math.round(used * 100)}% of ${money(cap)}</span></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

const overview = {
  id: "overview",
  title: "Overview",
  async render(view, { app, arg }) {
    if (arg !== "session") return renderBusiness(view, app);
    const A = app.adapter;
    view.innerHTML =
      head("This session", `Spend, traffic and risk signals for every team and model. ${modeNote(A)}`,
        `<button id="ovTraffic" type="button">Send 20 requests</button><button id="ovRefresh" type="button" class="ghost">Refresh</button>`) +
      ovTabs("session") + `<div id="ovBody">${loading("Loading overview…")}</div>`;
    const body = $("#ovBody");
    const paint = (ov) => {
      const k = ov.kpis, t = ov.traces;
      const demo = A.mode === "demo";
      const hasTraffic = t.requests > 0 || k.requests > 0;
      const flagged = k.anomalies_flagged + k.anomalies_paused;
      body.innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="l">${demo ? "Spend this session" : "Spend today (UTC)"}</div><div class="v">${usd(k.spend_usd)}</div><div class="s">${demo ? "simulated prices" : esc(ov.date)}</div></div>
        <div class="kpi"><div class="l">Requests served</div><div class="v">${int(k.requests)}</div><div class="s">${int(k.failed_attempts)} failed provider attempts</div></div>
        <div class="kpi ${t.fallback_rate > 0.2 ? "warn" : ""}"><div class="l">Fallback rate</div><div class="v">${pct(t.fallback_rate)}</div><div class="s">${int(t.fallbacks)} of ${int(t.served)} · ${esc(t.window)}</div></div>
        <div class="kpi"><div class="l">Cache savings</div><div class="v">${usd(k.cache.saved_usd)}</div><div class="s">${int(k.cache.hits)} hits · ${pct(k.cache.hit_rate)} hit rate</div></div>
        <div class="kpi ${k.anomalies_paused ? "bad" : flagged ? "warn" : ""}"><div class="l">Anomaly flags</div><div class="v">${int(flagged)}</div><div class="s">${int(k.anomalies_paused)} at pause level (10x)</div></div>
        <div class="kpi ${k.open_circuits ? "bad" : ""}"><div class="l">Open circuits</div><div class="v">${int(k.open_circuits)}</div><div class="s">${Object.entries(t.by_status || {}).map(([s, n]) => `${n}×${s}`).join(" · ") || "no refusals"}</div></div>
      </div>
      <div class="grid g-main">
        <section class="card" aria-labelledby="hourlyTitle">
          <div class="card-head"><div><h2 id="hourlyTitle">Spend per hour vs. 7-day average</h2>
            <p>${demo ? `Hours before ${String(ov.hourly.current_hour).padStart(2, "0")}:00 are simulated history; the highlighted hour is this session's real engine ledger.` : "UTC hours today; the dashed line is the average for that hour over the previous 7 days."}</p></div>
            ${demo ? simTag("simulated history") : ""}</div>
          ${hourlyChart({ today: ov.hourly.today, baseline: ov.hourly.baseline_7d, currentHour: ov.hourly.current_hour, simulatedBefore: demo ? ov.hourly.history_simulated_before : null, compact: compactCharts() })}
          <div class="legend"><span><i style="background:#a78bfa"></i>This hour</span>${demo ? '<span><i style="background:repeating-linear-gradient(45deg,#a78bfa 0 3px,#3b2f6b 3px 6px)"></i>Earlier today (simulated)</span>' : '<span><i style="background:#7c6bd6"></i>Earlier today</span>'}<span><i style="background:#fbbf24"></i>7-day average for the hour</span></div>
        </section>
        <section class="card" aria-labelledby="riskTitle">
          <div class="card-head"><h2 id="riskTitle">Risk signals</h2><a href="#/budgets" class="small">Budgets & Keys →</a></div>
          ${ov.anomalies.length ? `<div class="table-wrap"><table><thead><tr><th>Key</th><th>Team</th><th class="num">× baseline</th><th>Verdict</th></tr></thead><tbody>
            ${ov.anomalies.map((a) => `<tr><td>${esc(a.label)}</td><td>${esc(a.team)}</td><td class="num">${a.multiple.toFixed(1)}×</td><td><span class="pill ${esc(a.verdict)}">${esc(a.verdict)}</span></td></tr>`).join("")}
            </tbody></table></div>` : `<p class="muted small">No key is spending above 3× its 7-day hourly baseline.</p>`}
          <div class="hr"></div>
          <h3 style="margin-bottom:8px">Circuit breakers</h3>
          ${ov.breakers.length ? ov.breakers.map((b) => `<div class="dep"><code>${esc(b.deployment)}</code><span class="pill ${esc(b.state)}">${esc(b.state.replace("_", "-"))}</span></div>`).join("") : '<p class="muted small">No provider has been called yet.</p>'}
        </section>
      </div>
      <div class="grid g2" style="margin-top:16px">
        <section class="card"><div class="card-head"><h2>Spend by team</h2>${demo ? simTag() : ""}</div>
          ${ov.by_team.length ? hbars(ov.by_team.map((r) => ({ name: r.team, value: r.cost_usd, sub: `${r.requests} req` }))) : empty("No spend yet", "Send traffic from the Playground or the button above.")}</section>
        <section class="card"><div class="card-head"><h2>Spend by model</h2>${demo ? simTag() : ""}</div>
          ${ov.by_model.length ? hbars(ov.by_model.map((r) => ({ name: `${r.provider}/${r.model}`, value: r.cost_usd, sub: `${r.requests} req`, color: "var(--chart-2)" }))) : empty("No spend yet", "Model spend appears after the first request.")}</section>
      </div>
      <section class="card" style="margin-top:16px" aria-labelledby="tryTitle">
        <div class="card-head"><div><h2 id="tryTitle">What to try</h2><p>${hasTraffic ? "Each one takes under a minute." : "Start with some traffic, then break something."}</p></div></div>
        <div class="try-grid">
          <a class="try" href="#/playground?tab=resilience"><b>Take a provider down</b><span>Toggle an outage on Anthropic and watch its breaker open and requests fall back to OpenAI.</span><span class="go">Resilience →</span></a>
          <a class="try" href="#/playground?tab=chat&sample=pii"><b>Send a prompt with PII</b><span>As the support team, the redaction hook replaces the email, phone, card and API key before any provider sees them.</span><span class="go">Playground →</span></a>
          <a class="try" href="#/playground?tab=compare"><b>Compare two routes</b><span>Run the same prompt through smart-fast and cheap-batch: latency, cost and who answered, side by side.</span><span class="go">Compare →</span></a>
          <a class="try" href="#/budgets?spike=1"><b>Catch a runaway key</b><span>${demo ? "A batch job loops on the premium route: flagged at 3×, paused at 10× its 7-day baseline." : "Pause any key, or let an anomaly-paused key through for the rest of the hour."}</span><span class="go">Budgets & Keys →</span></a>
          <a class="try" href="#/policies"><b>Tighten a policy</b><span>Edit config/policies.yaml, see which team × route decisions change before you apply it.</span><span class="go">Policies →</span></a>
          <a class="try" href="#/traces"><b>Read a trace</b><span>Open any request to see each stage it passed: policy, budgets, hooks, caches, every fallback attempt.</span><span class="go">Traces →</span></a>
        </div>
      </section>`;
    };
    const load = async () => {
      try { paint(await A.overview()); } catch (e) { body.innerHTML = errorState(e, "ovRetry"); const r = $("#ovRetry"); if (r) r.onclick = load; }
    };
    $("#ovRefresh").onclick = (e) => busy(e.currentTarget, load);
    $("#ovTraffic").onclick = (e) =>
      busy(e.currentTarget, async () => {
        try {
          const r = await A.traffic(20, "mix");
          toast(`Sent ${r.sent} requests: ${Object.entries(r.outcomes).map(([k, v]) => `${v} ${k}`).join(", ")}`, "ok");
        } catch (err) { toast(err.message, "bad"); }
        await load();
      });
    await load();
    if (A.mode === "live") {
      const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
      return () => clearInterval(timer);
    }
  },
};

// =============================================================================================
// Playground
// =============================================================================================

// "provider/model" ids: allow a line break after the provider, never inside a hyphenated model name.
const breakAfterSlash = (id) => String(id).split("/").map((p) => `<span class="nw">${esc(p)}</span>`).join("/<wbr>");

function resultCard(r) {
  if (!r) return "";
  const meta = `
    <div class="result-meta">
      <div><b>${statusPill(r.status, r.ok ? (r.cache && r.cache !== "miss" ? "cache_hit" : "ok") : null)}</b><span>status</span></div>
      <div><b title="${esc(r.served_by || "")}">${breakAfterSlash(r.served_by || "–")}</b><span>served by${r.fell_back ? " (after fallback)" : ""}</span></div>
      <div><b>${ms(r.latency_ms)}</b><span>latency${r.simulated ? " (simulated)" : ""}</span></div>
      <div><b>${r.cost_usd == null ? "–" : usd(r.cost_usd)}</b><span>${r.saved_usd ? `saved ${usd(r.saved_usd)} by cache` : `cost · ${int(r.tokens)} tokens`}</span></div>
    </div>`;
  return `<div class="result">
    ${meta}
    ${stageChips(r.stages)}
    ${r.error ? `<div class="note bad">${esc(r.error)}</div>` : ""}
    ${r.sent != null ? `<div><div class="small muted" style="margin-bottom:4px">What the provider received ${r.sent ? "" : "(nothing was sent)"}</div><pre class="box">${esc(r.sent || "(nothing was sent)")}</pre></div>` : ""}
    ${r.response ? `<div><div class="small muted" style="margin-bottom:4px">Response ${r.simulated ? simTag() : ""}</div><pre class="box">${esc(r.response)}</pre></div>` : ""}
    ${r.trace_id ? `<div><button class="sm" data-trace="${esc(r.trace_id)}">Open trace ${esc(r.trace_id)}</button></div>` : ""}
  </div>`;
}

function wireTraceButtons(root, openTrace) {
  $$("[data-trace]", root).forEach((b) => (b.onclick = () => openTrace(b.dataset.trace)));
}

async function keyOptions(A, selected) {
  const keys = await A.playgroundKeys();
  return { keys, html: keys.map((k) => opt(k.label, `${k.label} (${k.team})`, k.label === selected)).join("") };
}

function liveKeyForm(teams) {
  return `<div class="note" style="margin-bottom:12px">Live mode sends real requests to this gateway with an <b>app key</b>. Create one per team here: the console keeps the secret in this tab's session storage only.</div>
    <div class="row"><label class="field" style="min-width:160px">Team<select id="pkTeam">${teams.map((t) => opt(t, t, t === "member-services")).join("")}</select></label>
    <button id="pkCreate" class="primary" style="align-self:flex-end">Create playground key</button></div>`;
}

const playground = {
  id: "playground",
  title: "Playground",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    const tabs = [["chat", "Chat"], ["compare", "Compare routes"], ["resilience", "Resilience"], ["cache", "Semantic cache"]];
    view.innerHTML =
      head("Playground", `Send requests through every gateway control. ${modeNote(A)}`) +
      `<div class="tabs" role="tablist">${tabs.map(([id, l]) => `<button role="tab" data-tab="${id}" aria-selected="false" id="tab-${id}">${l}</button>`).join("")}</div><div id="pgBody" role="tabpanel"></div>`;
    const state = { timer: null };
    const show = async (tab, params) => {
      clearInterval(state.timer);
      $$(".tabs [data-tab]", view).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
      const box = $("#pgBody");
      box.innerHTML = loading();
      try {
        if (tab === "compare") await renderCompare(box, A, ctx);
        else if (tab === "resilience") state.timer = await renderResilience(box, A, ctx);
        else if (tab === "cache") await renderCache(box, A);
        else await renderChat(box, A, ctx, params);
      } catch (e) { box.innerHTML = errorState(e); }
    };
    $$(".tabs [data-tab]", view).forEach((b) => (b.onclick = () => ctx.navigate(`#/playground?tab=${b.dataset.tab}`)));
    this.onParams = ({ params }) => show(params.get("tab") || "chat", params);
    await show(ctx.params.get("tab") || "chat", ctx.params);
    return () => clearInterval(state.timer);
  },
};

async function renderChat(box, A, ctx, params) {
  const sample = params && params.get("sample");
  const [routes, teams, keys] = await Promise.all([A.routes(), A.teams(), A.playgroundKeys()]);
  const defaultKey = A.mode === "demo" ? (sample === "pii" ? "member-assistant" : "online-banking") : keys[0] && keys[0].label;
  const html = keys.map((k) => opt(k.label, `${k.label} (${k.team})`, k.label === defaultKey)).join("");
  box.innerHTML = `<div class="grid g2">
    <section class="card"><div class="card-head"><div><h2>Request</h2><p>OpenAI-compatible: <code>POST /v1/chat/completions</code> with an app key.</p></div></div>
      ${A.mode === "live" && !keys.length ? liveKeyForm(teams) : ""}
      <div class="form-grid">
        <label class="field wide">App key<select id="chKey">${html}</select></label>
        <label class="field wide">Model alias<select id="chAlias">${Object.entries(routes).map(([a, r]) => opt(a, `${a} (${r.strategy})`, a === "smart-fast")).join("")}</select></label>
        <label class="field wide">Temperature<select id="chTemp">${opt("0.7", "0.7 (not cached)")}${opt("0", "0 (cacheable)")}</select></label>
        <label class="field wide">max_tokens<input id="chMax" type="number" min="1" placeholder="policy ceiling" /></label>
        <label class="field full">Prompt<textarea id="chText" rows="6">${esc(sample === "pii" ? SAMPLE_PROMPTS.pii : SAMPLE_PROMPTS.faq)}</textarea></label>
      </div>
      <div class="row" style="margin-top:10px"><span class="small muted">Samples:</span>
        <button class="sm ghost" data-sample="pii">Ticket with PII</button><button class="sm ghost" data-sample="faq">FAQ</button><button class="sm ghost" data-sample="release">Release note</button></div>
      <div class="row" style="margin-top:14px"><button class="primary" id="chSend" ${keys.length ? "" : "disabled"}>Send request</button>
        ${A.mode === "live" && keys.length ? `<span class="small muted">Need another team? <a href="#/budgets">create a key</a>.</span>` : ""}</div>
      ${A.mode === "demo" ? `<p class="small muted" style="margin-top:12px">Tip: <b>member-assistant</b> has redaction on; team <b>regulated</b> (create a key in Budgets & Keys) may only reach the local model.</p>` : ""}
    </section>
    <section class="card" aria-live="polite"><div class="card-head"><h2>Result</h2></div><div id="chOut">${empty("No request yet", "Send one to see who answered, what it cost and every stage it passed.")}</div></section>
  </div>`;
  $$("[data-sample]", box).forEach((b) => (b.onclick = () => ($("#chText").value = SAMPLE_PROMPTS[b.dataset.sample])));
  if ($("#pkCreate")) $("#pkCreate").onclick = (e) => busy(e.currentTarget, async () => {
    const team = $("#pkTeam").value;
    try { await A.createKey(`console-${team}-${Math.random().toString(36).slice(2, 6)}`, team); toast(`Created an app key for team ${team}.`, "ok"); await renderChat(box, A, ctx, params); }
    catch (err) { toast(err.message, "bad"); }
  });
  $("#chSend").onclick = (e) => busy(e.currentTarget, async () => {
    const out = $("#chOut");
    try {
      const r = await A.chat({ key: $("#chKey").value, alias: $("#chAlias").value, text: $("#chText").value, max_tokens: $("#chMax").value || null, temperature: $("#chTemp").value });
      out.innerHTML = resultCard(r);
      wireTraceButtons(out, ctx.openTrace);
    } catch (err) { out.innerHTML = errorState(err); }
  });
}

async function renderCompare(box, A, ctx) {
  const [routes, { keys, html }] = await Promise.all([A.routes(), keyOptions(A, "online-banking")]);
  const aliases = Object.keys(routes);
  const b0 = aliases.includes("cheap-batch") ? "cheap-batch" : aliases[1] || aliases[0];
  box.innerHTML = `<section class="card"><div class="card-head"><div><h2>Compare two routes</h2><p>The same prompt through two aliases, N times each. Each run is a real gateway request with its own trace.</p></div>${A.simulated ? simTag("simulated providers") : ""}</div>
    ${A.mode === "live" && !keys.length ? `<div class="note warn">Create a playground key on the Chat tab first.</div>` : ""}
    <div class="form-grid">
      <label class="field">Route A<select id="cmpA">${aliases.map((a) => opt(a, a, a === "smart-fast")).join("")}</select></label>
      <label class="field">Route B<select id="cmpB">${aliases.map((a) => opt(a, a, a === b0)).join("")}</select></label>
      <label class="field">App key<select id="cmpKey">${html}</select></label>
      <label class="field">Runs each<select id="cmpN">${opt("1")}${opt("5", "5", true)}${opt("10")}</select></label>
      <label class="field full">Prompt<textarea id="cmpText" rows="3">${esc(SAMPLE_PROMPTS.release)}</textarea></label>
    </div>
    <div class="row" style="margin-top:12px"><button class="primary" id="cmpRun" ${keys.length ? "" : "disabled"}>Run comparison</button><span class="small muted">Temperature 0.7, so the cache does not answer.</span></div>
  </section><div class="grid g2" style="margin-top:16px" id="cmpOut"></div>`;
  $("#cmpRun").onclick = (e) => busy(e.currentTarget, async () => {
    const n = Number($("#cmpN").value), key = $("#cmpKey").value, text = $("#cmpText").value;
    const sides = [$("#cmpA").value, $("#cmpB").value];
    const results = [[], []];
    const out = $("#cmpOut");
    out.innerHTML = loading("Running…");
    try {
      for (let i = 0; i < n; i++) for (let s = 0; s < 2; s++) results[s].push(await A.chat({ key, alias: sides[s], text, temperature: 0.7 }));
    } catch (err) { out.innerHTML = errorState(err); return; }
    out.innerHTML = sides.map((alias, s) => {
      const rs = results[s], ok = rs.filter((r) => r.ok);
      const avg = ok.length ? ok.reduce((a, r) => a + r.latency_ms, 0) / ok.length : null;
      const cost = rs.reduce((a, r) => a + (r.cost_usd || 0), 0);
      const by = {};
      ok.forEach((r) => (by[r.served_by] = (by[r.served_by] || 0) + 1));
      return `<section class="card compare-col"><div class="card-head"><h2>${esc(alias)}</h2><span class="muted small">${esc(routes[alias].targets.join(" → "))}</span></div>
        <div class="result-meta"><div><b>${ok.length}/${rs.length}</b><span>succeeded</span></div><div><b>${ms(avg)}</b><span>avg latency</span></div>
        <div><b>${usd(cost)}</b><span>total cost</span></div><div><b>${rs.filter((r) => r.fell_back).length}</b><span>fallbacks</span></div></div>
        <div class="small muted">Served by</div>${hbars(Object.entries(by).map(([name, v]) => ({ name, value: v })), { format: (v) => `${v}×`, color: "var(--chart-2)" }) || '<p class="small muted">none</p>'}
        ${rs.length ? `<div class="small muted">Last response</div><pre class="box">${esc(rs[rs.length - 1].response || rs[rs.length - 1].error || "")}</pre><div class="row">${rs.slice(-3).map((r) => r.trace_id ? `<button class="sm" data-trace="${esc(r.trace_id)}">${esc(r.trace_id)}</button>` : "").join("")}</div>` : ""}
      </section>`;
    }).join("");
    wireTraceButtons(out, ctx.openTrace);
  });
}

async function renderResilience(box, A, ctx) {
  const paint = async () => {
    const v = await A.providers();
    const can = A.caps.providerKnobs && v.enabled;
    box.innerHTML = `
      ${!can ? `<div class="note warn" style="margin-bottom:16px">Provider knobs need simulated providers. Start the gateway with <code>ROUTER_MOCK_PROVIDERS=true</code> (the docker compose file does) to take a provider down from here. Breaker states below are real.</div>` : ""}
      <section class="card"><div class="card-head"><div><h2>Traffic</h2><p>${A.mode === "demo" ? "Requests go through request() in demo/engine.py: the gateway's real breakers, fallback chain and latency ordering on a virtual clock." : "Requests go to this gateway with console app keys."}</p></div>${A.simulated ? simTag("simulated providers") : ""}</div>
        <div class="row"><button class="primary" id="rsOne">Send 1 request</button><button id="rsMix">Send 20 (mixed teams)</button>
        ${A.caps.virtualClock ? `<button id="rsWait" title="Advance the virtual clock past the 10 s cooldown">Wait 15 s (virtual)</button>` : ""}
        <button id="rsReset" class="ghost">Reset breakers</button><span class="small muted" id="rsMsg" aria-live="polite"></span></div>
      </section>
      <div class="prov-grid" style="margin-top:16px">${v.providers.map((p) => {
        const down = p.outage || p.rate_limited;
        const open = p.deployments.some((d) => d.breaker !== "closed");
        return `<article class="card prov ${down ? "down" : open ? "degraded" : ""}" data-prov="${esc(p.provider)}">
          <div class="row spread"><h3>${esc(p.label)}</h3>${down ? '<span class="pill bad">down</span>' : '<span class="pill ok">up</span>'}</div>
          <div style="margin:10px 0">${p.deployments.map((d) => `<div class="dep"><code title="${esc(d.id)}">${esc(d.id.split("/")[1])}</code><span><span class="pill ${esc(d.breaker)}">${esc(d.breaker.replace("_", "-"))}</span>${d.ewma_ms != null ? ` <span class="faint small">${ms(d.ewma_ms)}</span>` : ""}</span></div>`).join("")}</div>
          <label class="switch"><input type="checkbox" data-k="outage" ${p.outage ? "checked" : ""} ${can ? "" : "disabled"}/> Outage (timeouts)</label><br/>
          <label class="switch" style="margin-top:6px"><input type="checkbox" data-k="rate_limited" ${p.rate_limited ? "checked" : ""} ${can ? "" : "disabled"}/> 429 rate limited</label>
          <div class="knob"><span>Error rate</span><input type="range" min="0" max="1" step="0.05" value="${p.error_rate || 0}" data-k="error_rate" ${can ? "" : "disabled"} aria-label="${esc(p.label)} error rate"/><output>${pct(p.error_rate || 0, 0)}</output></div>
          <div class="knob"><span>Latency</span><input type="range" min="0" max="3000" step="50" value="${p.latency_ms ?? 0}" data-k="latency_ms" ${can ? "" : "disabled"} aria-label="${esc(p.label)} latency"/><output>${ms(p.latency_ms)}</output></div>
        </article>`;
      }).join("")}</div>
      <section class="card" style="margin-top:16px"><div class="card-head"><h2>Breaker transitions</h2><span class="small muted">newest first</span></div>
        ${v.transitions.length ? `<div class="table-wrap"><table><thead><tr><th>Deployment</th><th>Change</th><th>Why</th></tr></thead><tbody>${v.transitions.slice(0, 12).map((t) => `<tr><td><code>${esc(t.deployment)}</code></td><td><span class="pill ${esc(t.from)}">${esc(t.from)}</span> → <span class="pill ${esc(t.to)}">${esc(t.to)}</span></td><td class="small">${esc(t.reason)}</td></tr>`).join("")}</tbody></table></div>` : empty("No transitions yet", "Turn on an outage and send 20 requests: after 3 failures (5 on the default live config) the breaker opens and later requests skip that provider.")}
      </section>`;
    $$("[data-prov] input", box).forEach((inp) => {
      const prov = inp.closest("[data-prov]").dataset.prov;
      const k = inp.dataset.k;
      const ev = inp.type === "range" ? "change" : "change";
      if (inp.type === "range") inp.oninput = () => (inp.nextElementSibling.textContent = k === "error_rate" ? pct(inp.value, 0) : ms(inp.value));
      inp.addEventListener(ev, async () => {
        try { await A.setProvider(prov, { [k]: inp.type === "checkbox" ? inp.checked : Number(inp.value) }); } catch (e) { toast(e.message, "bad"); }
        await paint();
      });
    });
    const msg = (t) => ($("#rsMsg").textContent = t);
    const send = async (n) => {
      const r = await A.traffic(n, "mix");
      msg(`${r.sent} sent: ${Object.entries(r.outcomes).map(([k, c]) => `${c} ${k}`).join(", ")}`);
      await paint();
    };
    $("#rsOne").onclick = (e) => busy(e.currentTarget, () => send(1).catch((err) => toast(err.message, "bad")));
    $("#rsMix").onclick = (e) => busy(e.currentTarget, () => send(20).catch((err) => toast(err.message, "bad")));
    if ($("#rsWait")) $("#rsWait").onclick = async () => { await A.advance(15); msg("Virtual clock +15 s: open breakers are now half-open."); await paint(); };
    $("#rsReset").onclick = async () => { try { await A.resetBreakers(); msg("Breakers reset."); } catch (e) { toast(e.message, "bad"); } await paint(); };
  };
  await paint();
  return null;
}

async function renderCache(box, A) {
  if (!A.caps.semanticPlayground) {
    box.innerHTML = `<section class="card"><h2>Semantic cache</h2><p class="muted">On a live gateway the semantic cache is configured in <code>config/routes.yaml</code> under <code>semantic_cache:</code> (off by default; per-team opt-in). Hits show up in Traces as <b>Semantic cache: hit</b>, at $0, with the similarity. The calibration report is on the <a href="#/evals">Evals</a> screen.</p></section>`;
    return;
  }
  const view = await A.semanticView();
  const teams = (await A.teams()).filter((t) => ["digital-banking", "member-services", "risk-analytics"].includes(t));
  box.innerHTML = `<div class="grid g2"><section class="card"><div class="card-head"><div><h2>Ask through the semantic cache</h2><p><code>router/semcache.py</code>: hashing embedder, number and negation guards, partitions per team and policy.</p></div>${realTag()}</div>
    <div class="form-grid"><label class="field wide">Team<select id="smTeam">${teams.map((t) => opt(t, t, t === "digital-banking")).join("")}</select></label>
    <label class="field wide">Threshold <output id="smThrOut">${view.threshold}</output><input id="smThr" type="range" min="0.7" max="0.99" step="0.01" value="${view.threshold}"/></label>
    <label class="field full">Question<input id="smText" type="text" value="${esc(view.examples[0])}"/></label></div>
    <div class="chips" style="margin-top:10px">${view.examples.map((t, i) => `<button class="sm ghost" data-ex="${i}">${esc(t)}</button>`).join("")}</div>
    <div class="row" style="margin-top:12px"><button class="primary" id="smAsk">Ask</button></div>
    <div id="smOut" style="margin-top:12px" aria-live="polite"></div></section>
    <section class="card"><div class="card-head"><h2>Log</h2><span class="small muted" id="smCounts"></span></div><div id="smLog"></div></section></div>`;
  const paintLog = (v) => {
    $("#smCounts").textContent = `${v.counts.hit} hits · ${v.counts.miss} misses · ${v.counts.guard} guarded`;
    $("#smLog").innerHTML = v.log.length ? `<div class="table-wrap"><table><thead><tr><th>Team</th><th>Question</th><th>Result</th><th class="num">Similarity</th></tr></thead><tbody>${v.log.map((r) => `<tr><td>${esc(r.team)}</td><td>${esc(r.text)}<span class="sub">${esc(r.reason)}</span></td><td><span class="pill ${r.result === "hit" ? "hit" : "miss"}">${esc(r.result)}</span></td><td class="num">${r.similarity.toFixed(3)}</td></tr>`).join("")}</tbody></table></div>` : empty("Nothing asked yet", "Ask the first example, then the second: the reworded question is served from cache.");
  };
  paintLog(view);
  $("#smThr").oninput = () => ($("#smThrOut").textContent = $("#smThr").value);
  $$("[data-ex]", box).forEach((b) => (b.onclick = () => ($("#smText").value = view.examples[Number(b.dataset.ex)])));
  $("#smAsk").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await A.semanticAsk($("#smTeam").value, $("#smText").value, Number($("#smThr").value));
    $("#smOut").innerHTML = `<div class="note ${r.result === "hit" ? "" : "warn"}" id="smResult">${esc(r.result)} · ${esc(r.reason)} · similarity ${r.similarity.toFixed(3)}${r.nearest ? ` to “${esc(r.nearest)}”` : ""}${r.saved_usd ? ` · saved ${usd(r.saved_usd)}` : r.cost_usd ? ` · cost ${usd(r.cost_usd)}` : ""}</div>`;
    paintLog(await A.semanticView());
  });
}

// =============================================================================================
// Traces
// =============================================================================================

const tracesScreen = {
  id: "traces",
  title: "Traces",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    const teams = await A.teams().catch(() => []);
    view.innerHTML =
      head("Traces", `Every request with its decision timeline. ${A.mode === "demo" ? "This browser session." : "Kept in memory by this gateway worker (ROUTER_TRACE_BUFFER); metadata only, never prompt text."}`,
        `<label class="switch small"><input type="checkbox" id="trAuto" ${A.mode === "live" ? "checked" : ""}/> Auto-refresh</label><button id="trRefresh" class="ghost">Refresh</button>`) +
      `<div class="card" style="margin-bottom:16px"><div class="form-grid">
        <label class="field wide">Search<input id="trQ" type="search" placeholder="trace id, key, alias, deployment, status…"/></label>
        <label class="field">Team<select id="trTeam"><option value="">All teams</option>${teams.map((t) => opt(t)).join("")}</select></label>
        <label class="field">Outcome<select id="trOutcome"><option value="">All outcomes</option>${["ok", "cache_hit", "rejected", "error"].map((o) => opt(o)).join("")}</select></label>
      </div></div><div id="trBody">${loading()}</div>`;
    const body = $("#trBody");
    let shown = 50;
    const load = async () => {
      try {
        let rows = await A.traces({ q: $("#trQ").value, team: $("#trTeam").value, outcome: $("#trOutcome").value, limit: 300 });
        if (!rows.length) {
          const filtered = $("#trQ").value || $("#trTeam").value || $("#trOutcome").value;
          body.innerHTML = filtered ? empty("No traces match these filters", "Clear the search or pick another team or outcome.") :
            empty("No requests yet", "Send one from the Playground and it shows up here with every stage it passed.", `<button id="trGen" class="primary">Send 20 requests</button>`);
          if ($("#trGen")) $("#trGen").onclick = (e) => busy(e.currentTarget, async () => { await A.traffic(20, "mix"); await load(); });
          return;
        }
        const total = rows.length;
        rows = rows.slice(0, shown);
        body.innerHTML = `<div class="table-wrap"><table class="dense"><thead><tr><th>Trace</th><th>Key · team</th><th>Alias</th><th>Status</th><th>Served by</th><th class="num">Attempts</th><th class="num">Latency</th><th class="num">Cost</th></tr></thead><tbody>
          ${rows.map((t) => `<tr class="click" tabindex="0" data-id="${esc(t.id)}"><td class="nw"><code>${esc(t.id)}</code><span class="sub">${esc(t.ts)}</span></td>
            <td class="nw">${esc(t.key)}<span class="sub">${esc(t.team)}</span></td><td class="nw">${esc(t.alias)}</td><td>${statusPill(t.status, t.outcome)}${t.reason && t.status >= 400 ? `<span class="sub" title="${esc(t.reason)}">${esc(t.reason.slice(0, 48))}${t.reason.length > 48 ? "…" : ""}</span>` : ""}</td>
            <td>${t.served_by ? `<code>${breakAfterSlash(t.served_by)}</code>` : '<span class="faint">–</span>'}${t.fell_back ? '<span class="sub">after fallback</span>' : ""}</td>
            <td class="num">${int(t.attempts)}</td><td class="num">${ms(t.latency_ms)}</td><td class="num">${t.saved_usd ? `<span title="saved ${usd(t.saved_usd)}">$0</span>` : usd(t.cost_usd)}</td></tr>`).join("")}
          </tbody></table></div><div class="row" style="margin-top:8px"><span class="small faint">Showing ${rows.length} of ${total} traces${A.simulated ? " · simulated providers" : ""}. Click a row for its timeline.</span>${total > rows.length ? '<button class="sm" id="trMore">Show 50 more</button>' : ""}</div>`;
        if ($("#trMore")) $("#trMore").onclick = () => { shown += 50; load(); };
        $$("tr[data-id]", body).forEach((tr) => {
          tr.onclick = () => ctx.openTrace(tr.dataset.id);
          tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ctx.openTrace(tr.dataset.id); } };
        });
      } catch (e) { body.innerHTML = errorState(e); }
    };
    let deb;
    $("#trQ").oninput = () => { clearTimeout(deb); deb = setTimeout(load, 200); };
    $("#trTeam").onchange = load;
    $("#trOutcome").onchange = load;
    $("#trRefresh").onclick = (e) => busy(e.currentTarget, load);
    await load();
    const timer = setInterval(() => { if ($("#trAuto") && $("#trAuto").checked && !document.hidden && !$("#drawer").classList.contains("open")) load(); }, 5000);
    return () => clearInterval(timer);
  },
};

// =============================================================================================
// Policies
// =============================================================================================

const CONFIG_TABS = [["policies", "policies.yaml"], ["routes", "routes.yaml"], ["mcp", "mcp.yaml"]];

const policies = {
  id: "policies",
  title: "Policies",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    view.innerHTML =
      head("Policies", `The gateway's real YAML, validated by its own loaders (<code>router/policy.py</code>, <code>router/models.py</code>, <code>router/mcp_policy.py</code> via <code>router/configcheck.py</code>). ${A.mode === "demo" ? "Applying changes this browser session only." : A.caps.configWrites ? "Applying writes the file and reloads the gateway." : "Read-only: config writes are off on this gateway."}`) +
      `<div class="tabs" role="tablist">${CONFIG_TABS.map(([id, l]) => `<button role="tab" data-cfg="${id}" aria-selected="false">${l}</button>`).join("")}</div><div id="pcBody"></div>`;
    const show = async (name) => {
      $$("[data-cfg]", view).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.cfg === name)));
      await renderConfig($("#pcBody"), A, ctx, name);
    };
    $$("[data-cfg]", view).forEach((b) => (b.onclick = () => ctx.navigate(`#/policies?file=${b.dataset.cfg}`)));
    this.onParams = ({ params }) => show(params.get("file") || "policies");
    await show(ctx.params.get("file") || "policies");
  },
};

async function renderConfig(box, A, ctx, name) {
  box.innerHTML = loading("Loading file…");
  let cfg;
  try { cfg = await A.config(name); } catch (e) { box.innerHTML = errorState(e); return; }
  const original = cfg.text;
  const canApply = A.mode === "demo" || A.caps.configWrites;
  box.innerHTML = `<div class="grid g-main">
    <section class="card"><div class="card-head"><div><h2><code>${esc(cfg.path)}</code></h2><p>${cfg.exists ? `${original.split("\n").length} lines` : "This file does not exist yet; applying creates it."}${name === "routes" && A.mode === "demo" ? " · in the demo, applying updates aliases and strategies; budgets, breaker thresholds and prices keep the demo's scaled-down simulated values." : ""}</p></div>
      <div class="row"><button id="cfValidate" class="primary">Validate</button><button id="cfApply" ${canApply ? "" : "disabled"} title="${canApply ? "" : "Set ROUTER_ALLOW_CONFIG_WRITES=true on the gateway"}">Apply</button><button id="cfRevert" class="ghost">Revert</button></div></div>
      <div class="editor"><div class="gutter" id="cfGutter" aria-hidden="true"></div><textarea id="cfText" spellcheck="false" aria-label="${esc(cfg.path)} contents" wrap="off">${esc(original)}</textarea></div>
      <p class="small muted" id="cfStatus" style="margin:8px 0 0" aria-live="polite">Unchanged.</p>
    </section>
    <div class="stack">
      <section class="card"><div class="card-head"><h2>Validation</h2></div><div id="cfResult"><p class="small muted">Validate runs the same loader the gateway runs at startup and on <code>POST /admin/reload</code>.</p></div></section>
      ${name === "policies" ? `<section class="card"><div class="card-head"><div><h2>Re-run a scenario</h2><p>Send a real request through the applied policy.</p></div></div>
        <div class="form-grid"><label class="field wide">App key<select id="cfKey"></select></label><label class="field wide">Alias<select id="cfAlias"></select></label></div>
        <div class="row" style="margin-top:10px"><button id="cfRun">Send request</button></div><div id="cfRunOut" style="margin-top:10px"></div></section>` : ""}
    </div></div>
    ${name === "policies" ? `<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Decision preview</h2><p>Every team × route: the active policy vs. the text in the editor, evaluated by <code>router/policy.py</code> without calling anything. Cells that would change are outlined.</p></div>
      <div class="row"><label class="field" style="width:130px">max_tokens<input id="cfMax" type="number" min="1" value="3000"/></label><button id="cfPreview" style="align-self:flex-end">Preview</button></div></div>
      <div id="cfMatrix"></div></section>` : ""}`;
  const ta = $("#cfText"), gutter = $("#cfGutter");
  let errorLines = new Set();
  const paintGutter = () => {
    const n = ta.value.split("\n").length;
    gutter.innerHTML = Array.from({ length: n }, (_, i) => (errorLines.has(i + 1) ? `<span class="bad">${i + 1}</span>` : String(i + 1))).join("\n");
    gutter.scrollTop = ta.scrollTop;
  };
  ta.addEventListener("scroll", () => (gutter.scrollTop = ta.scrollTop));
  ta.addEventListener("input", () => { paintGutter(); $("#cfStatus").textContent = ta.value === original ? "Unchanged." : "Edited, not applied."; });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const s = ta.selectionStart;
      ta.setRangeText("  ", s, ta.selectionEnd, "end");
      paintGutter();
    }
  });
  paintGutter();
  const jump = (line) => {
    const lines = ta.value.split("\n");
    const start = lines.slice(0, line - 1).join("\n").length + (line > 1 ? 1 : 0);
    ta.focus();
    ta.setSelectionRange(start, start + (lines[line - 1] || "").length);
    ta.scrollTop = Math.max(0, (line - 4) * 20);
  };
  const showResult = (r, applied = false) => {
    errorLines = new Set((r.errors || []).map((e) => e.line).filter(Boolean));
    paintGutter();
    const out = $("#cfResult");
    if (r.ok) {
      const s = r.summary || {};
      out.innerHTML = `<div class="note" id="cfOk">${applied ? "Applied." : "Valid."} ${name === "policies" && s.teams ? `Teams: ${esc(s.teams.join(", ") || "none")}; route rules: ${esc((s.routes || []).join(", ") || "none")}.` : ""}${name === "routes" && s.aliases ? `${Object.keys(s.aliases).length} aliases: ${esc(Object.keys(s.aliases).join(", "))}.` : ""}${name === "mcp" && s.servers ? `Servers: ${esc(s.servers.join(", "))}; teams: ${esc(s.teams.join(", "))}.` : ""}</div>
        ${(r.warnings || []).map((w) => `<div class="note warn" style="margin-top:8px">${esc(w)}</div>`).join("")}`;
    } else {
      out.innerHTML = `<div class="errors" id="cfErrors">${(r.errors || []).map((e) => `<button type="button" data-line="${e.line || ""}">${e.line ? `Line ${e.line}: ` : ""}${esc(e.message)}</button>`).join("")}</div>`;
      $$("[data-line]", out).forEach((b) => (b.onclick = () => b.dataset.line && jump(Number(b.dataset.line))));
    }
  };
  $("#cfValidate").onclick = (e) => busy(e.currentTarget, async () => {
    try { showResult(await A.validateConfig(name, ta.value)); } catch (err) { $("#cfResult").innerHTML = errorState(err); }
  });
  $("#cfApply").onclick = (e) => busy(e.currentTarget, async () => {
    try {
      const r = await A.applyConfig(name, ta.value);
      showResult(r.ok ? { ...(await A.validateConfig(name, ta.value)), ok: true } : r, r.ok);
      if (r.ok) { $("#cfStatus").textContent = `Applied to ${cfg.path}.`; toast(`${cfg.path} applied`, "ok"); if (name === "policies") await preview(); }
    } catch (err) { $("#cfResult").innerHTML = errorState(err); }
  });
  $("#cfRevert").onclick = () => { ta.value = original; errorLines = new Set(); paintGutter(); $("#cfStatus").textContent = "Reverted to the file."; };
  if (name !== "policies") return;

  const preview = async () => {
    const box2 = $("#cfMatrix");
    box2.innerHTML = loading("Evaluating…");
    const max = Number($("#cfMax").value) || null;
    try {
      const [active, cand] = await Promise.all([A.dryRun({ max_tokens: max }), A.dryRun({ max_tokens: max, text: ta.value })]);
      if (!cand.ok) { box2.innerHTML = `<div class="note bad">The editor text does not validate: ${esc(cand.errors.map((x) => x.message).join("; "))}</div>`; return; }
      const teams = [...new Set(cand.results.map((r) => r.team))];
      const aliases = [...new Set(cand.results.map((r) => r.alias))];
      const find = (set, t, a) => set.results.find((r) => r.team === t && r.alias === a);
      const cell = (r) => !r ? "–" : r.allow ? `<span class="pill ok" title="${esc((r.targets || []).join(", "))}">allow${r.removed && r.removed.length ? ` −${r.removed.length}` : ""}</span>${r.max_tokens_clamped ? `<span class="sub">max ${r.max_tokens}</span>` : ""}` : `<span class="pill deny" title="${esc((r.reasons || []).join("; "))}">${r.status}</span>`;
      let changed = 0;
      box2.innerHTML = `<div class="table-wrap"><table class="matrix"><thead><tr><th>Team</th>${aliases.map((a) => `<th>${esc(a)}</th>`).join("")}</tr></thead><tbody>
        ${teams.map((t) => `<tr><td>${esc(t)}</td>${aliases.map((a) => {
          const c = find(cand, t, a), b = find(active, t, a);
          const diff = !b || b.allow !== c.allow || b.status !== c.status || JSON.stringify(b.targets) !== JSON.stringify(c.targets) || b.max_tokens !== c.max_tokens;
          if (diff) changed++;
          return `<td class="cell ${diff ? "changed" : ""}">${cell(c)}</td>`;
        }).join("")}</tr>`).join("")}</tbody></table></div>
        <p class="small ${changed ? "" : "muted"}" id="cfChanged" style="margin-top:8px">${changed ? `${changed} decision${changed > 1 ? "s" : ""} change if you apply the editor text.` : "No decision changes."} Hover a cell for permitted deployments or the reason. −N = deployments removed by the policy.</p>`;
    } catch (err) { box2.innerHTML = errorState(err); }
  };
  $("#cfPreview").onclick = (e) => busy(e.currentTarget, preview);
  const [{ html }, routes] = await Promise.all([keyOptions(A), A.routes()]);
  $("#cfKey").innerHTML = html || '<option value="">(create a key in Budgets & Keys)</option>';
  $("#cfAlias").innerHTML = Object.keys(routes).map((a) => opt(a)).join("");
  $("#cfRun").onclick = (e) => busy(e.currentTarget, async () => {
    try {
      const r = await A.chat({ key: $("#cfKey").value, alias: $("#cfAlias").value, text: SAMPLE_PROMPTS.faq, temperature: 0.7, max_tokens: Number($("#cfMax").value) || null });
      $("#cfRunOut").innerHTML = resultCard({ ...r, sent: null, response: null });
      wireTraceButtons($("#cfRunOut"), ctx.openTrace);
    } catch (err) { $("#cfRunOut").innerHTML = errorState(err); }
  });
  await preview();
}

// =============================================================================================
// Budgets & Keys
// =============================================================================================

const budgetsScreen = {
  id: "budgets",
  title: "Budgets & Keys",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    view.innerHTML =
      head("Budgets & Keys", `Org → team → key caps, app keys, anomaly pause, and showback. ${modeNote(A)}`,
        A.mode === "demo" ? `<button id="bkSpike">Simulate a runaway batch job</button>` : "") +
      `<div class="grid g2"><section class="card" id="bkBudgets">${loading()}</section><section class="card" id="bkAnom">${loading()}</section></div>
       <section class="card" id="bkKeys" style="margin-top:16px">${loading()}</section>
       <section class="card" id="bkShow" style="margin-top:16px">${loading()}</section>`;
    const paintBudgets = async () => {
      const el = $("#bkBudgets");
      try {
        const b = await A.budgets();
        const bar = (spent, cap) => {
          const p = cap ? Math.min(100, (100 * spent) / cap) : 0;
          return `<div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.toFixed(0)}"><i class="${p >= 100 ? "bad" : p >= 80 ? "warn" : ""}" style="width:${p}%"></i></div>`;
        };
        el.innerHTML = `<div class="card-head"><div><h2>Daily budgets</h2><p>${A.mode === "demo" ? "Scaled-down simulated caps so you can hit them." : "Caps set here are stored by the gateway and win over routes.yaml."}</p></div></div>
          <div class="budget" style="margin-bottom:14px"><div class="row spread small"><b>Org</b><span class="num">${usd(b.org.spent_usd)} of ${b.org.cap_usd ? usd(b.org.cap_usd, 2) : "no cap"}</span></div>${bar(b.org.spent_usd, b.org.cap_usd)}</div>
          ${b.teams.length ? b.teams.map((t) => `<div class="budget" style="margin-bottom:14px" data-team="${esc(t.team)}">
            <div class="row spread small"><b>${esc(t.team)}</b><span class="num">${usd(t.spent_usd)} of ${t.cap_usd ? usd(t.cap_usd) : "no cap"}${t.tpm ? ` · TPM ${int(t.tpm_used)}/${int(t.tpm)}` : ""}</span></div>${bar(t.spent_usd, t.cap_usd)}
            <div class="row" style="margin-top:6px"><span class="row nw"><label class="small muted" for="cap-${esc(t.team)}">Daily cap $</label><input id="cap-${esc(t.team)}" type="number" min="0" step="0.01" value="${t.cap_usd || ""}" style="width:110px" data-cap/></span>
            <span class="row nw"><label class="small muted" for="tpm-${esc(t.team)}">TPM</label><input id="tpm-${esc(t.team)}" type="number" min="0" step="1000" value="${t.tpm || ""}" style="width:110px" data-tpm/></span><button class="sm" data-save>Save</button></div></div>`).join("") : empty("No teams yet", "Teams appear when a key is created for them.")}
          ${(b.warnings || []).map((w) => `<div class="note warn">${esc(w)}</div>`).join("")}`;
        $$("[data-team]", el).forEach((row) => {
          $("[data-save]", row).onclick = (e) => busy(e.currentTarget, async () => {
            const cap = $("[data-cap]", row).value, tpm = $("[data-tpm]", row).value;
            try {
              await A.setBudget("team", row.dataset.team, { daily_usd: cap === "" ? null : Number(cap), tpm: tpm === "" ? null : Number(tpm) });
              toast(`Budget for ${row.dataset.team} saved`, "ok");
              await paintBudgets();
            } catch (err) { toast(err.message, "bad"); }
          });
        });
      } catch (e) { el.innerHTML = errorState(e); }
    };
    let lastSecret = null;
    const paintKeys = async () => {
      const el = $("#bkKeys"), an = $("#bkAnom");
      try {
        const [keys, teams] = await Promise.all([A.keys(), A.teams()]);
        const active = keys.filter((k) => !k.revoked_at);
        const flagged = active.filter((k) => k.verdict !== "ok" || k.hold);
        an.innerHTML = `<div class="card-head"><div><h2>Anomaly pause</h2><p>Spend this hour vs. each key's 7-day hourly baseline: flagged at 3×, paused at 10×.</p></div>${flagged.length ? `<span class="pill warn">${flagged.length} need attention</span>` : ""}</div>
          ${active.length ? hbars(active.map((k) => ({ name: k.label, value: Number(k.multiple) || 0, sub: k.hold || (k.verdict !== "ok" ? k.verdict : ""), color: k.verdict === "pause" ? "var(--bad)" : k.verdict === "flagged" ? "var(--warn)" : "var(--chart-1)" })), { format: (v) => `${v.toFixed(1)}×` }) : ""}
          ${flagged.length ? "" : `<p class="small muted" style="margin-top:8px">No flagged or paused keys.${A.mode === "live" ? " Baselines need at least 24 active hours of history per key." : ""}</p>`}
          <p class="small muted" style="margin-top:10px">Unpausing a key the anomaly check would pause lets it through until the end of the hour (an <b>override</b>), and is audited.</p>`;
        el.innerHTML = `<div class="card-head"><div><h2>App keys</h2><p>Secrets are shown once at creation; the gateway stores only a salted hash.</p></div></div>
          <div class="row" style="margin-bottom:12px"><label class="field" style="min-width:150px">Label<input id="nkLabel" type="text" placeholder="e.g. reporting-app"/></label>
          <label class="field" style="min-width:150px">Team<input id="nkTeam" type="text" list="teamList" placeholder="e.g. regulated"/><datalist id="teamList">${teams.map((t) => `<option value="${esc(t)}">`).join("")}</datalist></label>
          <button class="primary" id="nkCreate" style="align-self:flex-end">Create key</button></div>
          <div id="nkSecret">${lastSecret ? lastSecret : ""}</div>
          ${keys.length ? `<div class="table-wrap"><table><thead><tr><th>Label</th><th>Team</th><th>Fingerprint</th><th class="num">Spend today</th><th>Anomaly</th><th>State</th><th class="right">Actions</th></tr></thead><tbody>
            ${keys.map((k) => `<tr data-key="${esc(k.id)}"><td class="nw">${esc(k.label)}${k.is_admin ? ' <span class="pill info">admin</span>' : ""}<span class="sub"><code>${esc(k.key_prefix)}…</code></span></td><td>${esc(k.team)}</td><td><code>${esc(k.fingerprint)}</code></td>
              <td class="num">${usd(k.spent_today_usd)}</td><td><span class="pill ${esc(k.verdict)}">${esc(k.verdict)}</span>${k.multiple ? `<span class="sub">${Number(k.multiple).toFixed(1)}× baseline</span>` : ""}</td>
              <td>${k.revoked_at ? '<span class="pill neutral">revoked</span>' : k.hold ? `<span class="pill ${esc(k.hold)}">${esc(k.hold)}</span>` : '<span class="pill ok">active</span>'}</td>
              <td class="right">${k.revoked_at ? "" : `<div class="row" style="justify-content:flex-end;flex-wrap:nowrap">${k.hold === "paused" || k.verdict === "pause" ? `<button class="sm" data-act="unpause">Unpause</button>` : `<button class="sm" data-act="pause">Pause</button>`}<button class="sm danger" data-act="revoke">Revoke</button></div>`}</td></tr>`).join("")}
          </tbody></table></div>` : empty("No keys yet", "Create the first app key above.")}`;
        $("#nkCreate").onclick = (e) => busy(e.currentTarget, async () => {
          const label = $("#nkLabel").value.trim(), team = $("#nkTeam").value.trim();
          if (!label || !team) { toast("Give the key a label and a team.", "bad"); return; }
          try {
            const created = await A.createKey(label, team);
            lastSecret = `<div class="note" style="margin-bottom:12px" id="nkShown"><b>${esc(label)}</b> created. ${A.mode === "live" ? `Copy the key now, it is not shown again: <code>${esc(created.key)}</code> (also kept in this tab's session for the Playground).` : `Demo keys are simulated (<code>${esc(created.key)}</code>); pick <b>${esc(label)}</b> in the Playground.`}</div>`;
            await paintKeys();
            await paintBudgets();
          } catch (err) { toast(err.message, "bad"); }
        });
        $$("[data-key] [data-act]", el).forEach((b) => {
          const k = keys.find((x) => x.id === b.closest("[data-key]").dataset.key);
          b.onclick = (e) => busy(e.currentTarget, async () => {
            try {
              if (b.dataset.act === "revoke") {
                if (!confirm(`Revoke ${k.label}? Requests with it will get 401.`)) return;
                await A.revokeKey(k);
                toast(`${k.label} revoked`, "ok");
              } else if (b.dataset.act === "pause") { await A.pauseKey(k); toast(`${k.label} paused`, "ok"); }
              else { const r = await A.unpauseKey(k); toast(`${k.label}: ${r.status.replace("_", " ")}`, "ok"); }
              lastSecret = null;
              await paintKeys();
            } catch (err) { toast(err.message, "bad"); }
          });
        });
      } catch (e) { el.innerHTML = errorState(e); an.innerHTML = ""; }
    };
    const SHOWBACK_COLS = { team: "Team", model: "Model", provider: "Provider", key_label: "Key", key_fp: "Fingerprint" };
    const GROUPS = [["team", "Team"], ["team,model", "Team × model"], ["key", "Key"], ["provider,model", "Provider × model"]];
    let group = "team,model";
    const paintShowback = async () => {
      const el = $("#bkShow");
      try {
        const sb = await A.showback(group);
        const cols = sb.columns;
        el.innerHTML = `<div class="card-head"><div><h2>Showback</h2><p>Cost and unit metrics for chargeback (<code>router/showback.py</code>)${sb.period ? `, ${esc(sb.period.start)} to ${esc(sb.period.end)} UTC` : ", this session"}.</p></div>
          <div class="row"><div class="seg" role="group" aria-label="Group by">${GROUPS.map(([g, l]) => `<button data-g="${g}" aria-pressed="${g === group}">${l}</button>`).join("")}</div><button id="sbCsv">Download CSV</button></div></div>
          ${sb.rows.length ? `<div class="table-wrap"><table class="dense"><thead><tr>${cols.map((c) => `<th>${esc(SHOWBACK_COLS[c] || c)}</th>`).join("")}<th class="num">Requests</th><th class="num">Cache hits</th><th class="num">Failed</th><th class="num">Tokens</th><th class="num">Cost</th><th class="num">Saved</th><th class="num">$/1K req</th><th class="num">Share</th></tr></thead><tbody>
            ${sb.rows.slice(0, 50).map((r) => `<tr>${cols.map((c) => `<td class="nw">${c === "key_fp" ? `<code>${esc(r[c])}</code>` : esc(r[c])}</td>`).join("")}<td class="num">${int(r.requests)}</td><td class="num">${int(r.cache_hits)}</td><td class="num">${int(r.failed_attempts)}</td><td class="num">${int(r.input_tokens + r.output_tokens)}</td><td class="num">${usd(r.cost_usd)}</td><td class="num">${usd(r.saved_usd)}</td><td class="num">${usd(r.cost_per_1k_requests)}</td><td class="num">${pct(r.share_of_cost)}</td></tr>`).join("")}
          </tbody></table></div>` : empty("No usage yet", "Showback fills in as requests are served.")}`;
        $$("[data-g]", el).forEach((b) => (b.onclick = () => { group = b.dataset.g; paintShowback(); }));
        $("#sbCsv").onclick = (e) => busy(e.currentTarget, async () => {
          try { download(`showback_${group.replace(",", "-")}.csv`, await A.showbackCsv(group)); } catch (err) { toast(err.message, "bad"); }
        });
      } catch (e) { el.innerHTML = errorState(e); }
    };
    if ($("#bkSpike")) $("#bkSpike").onclick = (e) => busy(e.currentTarget, async () => {
      const r = await A.traffic(30, "spike");
      toast(`Batch job sent ${r.sent} requests: ${Object.entries(r.outcomes).map(([k, v]) => `${v} ${k}`).join(", ")}`, "ok");
      await Promise.all([paintKeys(), paintBudgets(), paintShowback()]);
    });
    await Promise.all([paintBudgets(), paintKeys(), paintShowback()]);
    if (ctx.params.get("spike") && $("#bkSpike")) $("#bkSpike").focus();
  },
};

// =============================================================================================
// MCP tools
// =============================================================================================

const LIVE_MCP_TOOLS = { tickets: ["search_tickets", "get_ticket", "close_ticket", "reassign_ticket"], files: ["read_file", "delete_file"] };
const MCP_ARGS = { search_tickets: { query: "refund requested by jordan@example.com" }, get_ticket: { id: "T-1042" }, close_ticket: { id: "T-1042", note: "refunded" }, reassign_ticket: { id: "T-1042", to: "tier-2" }, read_file: { path: "reports/q3-summary.txt" }, delete_file: { path: "reports/q3-summary.txt" } };

const mcpScreen = {
  id: "mcp",
  title: "MCP tools",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    view.innerHTML = head("MCP tools", `Default-deny tool allow-lists per team, velocity limits and argument redaction for agents calling MCP servers through <code>POST /mcp/{server}</code>. ${A.mode === "demo" ? `${realTag("router/mcp_policy.py")} ${simTag("simulated MCP servers")}` : modeNote(A)}`) + `<div id="mcBody">${loading()}</div>`;
    const body = $("#mcBody");
    const paint = async (last) => {
      const v = await A.mcp();
      const servers = Object.fromEntries(Object.entries(v.servers).map(([s, tools]) => [s, tools.length ? tools : LIVE_MCP_TOOLS[s] || []]));
      const allTools = Object.entries(servers).flatMap(([s, ts]) => ts.map((t) => [s, t]));
      const allowedFor = (team, s, t) => (v.allowed[team] && v.allowed[team][s] || []).some((p) => p === t || (p.endsWith("*") && t.startsWith(p.slice(0, -1))));
      const teams = v.teams.length ? v.teams : ["member-services", "risk-analytics"];
      body.innerHTML = `<div class="grid g-main">
        <section class="card"><div class="card-head"><div><h2>Allow-list</h2><p>What each team can see in <code>tools/list</code> and call. Anything not listed is denied and never reaches the server.</p></div></div>
          ${allTools.length ? `<div class="table-wrap"><table class="matrix"><thead><tr><th>Tool</th>${teams.map((t) => `<th>${esc(t)}</th>`).join("")}</tr></thead><tbody>
          ${allTools.map(([s, t]) => `<tr><td><code>${esc(s)}/${esc(t)}</code></td>${teams.map((team) => `<td class="cell">${allowedFor(team, s, t) ? '<span class="pill ok">allow</span>' : '<span class="pill skip">deny</span>'}</td>`).join("")}</tr>`).join("")}
          </tbody></table></div>` : empty("No MCP servers configured", "Copy config/mcp.example.yaml to config/mcp.yaml (or set ROUTER_MCP_FILE) and edit it on the Policies screen.")}
        </section>
        <section class="card"><div class="card-head"><div><h2>Try a tool call</h2><p>${A.caps.mcpCalls ? "Goes through the same policy check, limiter and audit as an agent's call." : "Calls need MCP servers: run the gateway with ROUTER_MOCK_PROVIDERS=true and ROUTER_MCP_FILE=config/mcp.mock.yaml."}</p></div></div>
          <div class="form-grid"><label class="field wide">Team<select id="mcTeam">${[...new Set([...teams, "digital-banking"])].map((t) => opt(t, t, t === "member-services")).join("")}</select></label>
          <label class="field wide">Tool<select id="mcTool">${allTools.map(([s, t]) => opt(`${s}/${t}`, `${s}/${t}`, t === "search_tickets")).join("")}</select></label>
          <label class="field full">Arguments (JSON)<textarea id="mcArgs" rows="3" class="mono"></textarea></label></div>
          <div class="row" style="margin-top:10px"><button class="primary" id="mcCall" ${A.caps.mcpCalls ? "" : "disabled"}>Call</button><button id="mcBurst" ${A.caps.mcpCalls ? "" : "disabled"}>Call 6× (velocity)</button></div>
          <div id="mcResult" style="margin-top:10px" aria-live="polite">${last || ""}</div>
        </section></div>
        <section class="card" style="margin-top:16px"><div class="card-head"><h2>Tool calls</h2><span class="small muted">newest first · arguments as audited (redacted)</span></div>
          ${v.log.length ? `<div class="table-wrap" id="mcLog"><table><thead><tr><th>When</th><th>Team</th><th>Tool</th><th>Decision</th><th>Reason</th><th>Arguments</th></tr></thead><tbody>
          ${v.log.map((r) => `<tr><td class="small">${esc(r.time || "")}</td><td>${esc(r.team)}</td><td><code>${esc(r.tool)}</code></td><td><span class="pill ${r.decision === "allow" ? "ok" : r.decision === "rate_limited" || r.decision === "cap_reached" ? "warn" : "bad"}">${esc(r.decision)}</span></td><td class="small">${esc(r.reason || "")}</td><td class="small"><code style="overflow-wrap:anywhere">${esc(r.args ? JSON.stringify(r.args) : "")}</code></td></tr>`).join("")}
          </tbody></table></div>` : empty("No tool calls yet", "Call an allowed tool, a denied one, then burst one past its per-minute limit.")}
        </section>`;
      const setArgs = () => { const t = $("#mcTool").value.split("/")[1]; $("#mcArgs").value = JSON.stringify(MCP_ARGS[t] || {}); };
      $("#mcTool").onchange = setArgs;
      setArgs();
      const call = async (n) => {
        let args;
        try { args = JSON.parse($("#mcArgs").value || "{}"); } catch { toast("Arguments must be JSON.", "bad"); return; }
        const [server, tool] = $("#mcTool").value.split("/");
        const team = $("#mcTeam").value;
        let r;
        for (let i = 0; i < n; i++) r = await A.mcpCall(team, server, tool, n > 1 ? { ...args, attempt: i } : args);
        const html = `<div class="note ${r.decision === "allow" ? "" : r.decision === "rate_limited" ? "warn" : "bad"}" id="mcOut"><b>${esc(r.decision)}</b>${r.reason ? ` · ${esc(r.reason)}` : ""}${r.result ? ` · ${esc(r.result)}` : ""}${r.retry_after ? ` · retry after ${esc(Number(r.retry_after).toFixed ? Number(r.retry_after).toFixed(0) : r.retry_after)} s` : ""}</div>`;
        const team0 = team, tool0 = $("#mcTool").value;
        await paint(html);
        $("#mcTeam").value = team0; $("#mcTool").value = tool0; setArgs();
      };
      $("#mcCall").onclick = (e) => busy(e.currentTarget, () => call(1).catch((err) => toast(err.message, "bad")));
      $("#mcBurst").onclick = (e) => busy(e.currentTarget, () => call(6).catch((err) => toast(err.message, "bad")));
    };
    try { await paint(); } catch (e) { body.innerHTML = errorState(e); }
  },
};

// =============================================================================================
// Evals
// =============================================================================================

const evalsScreen = {
  id: "evals",
  title: "Evals",
  async render(view, { app }) {
    const A = app.adapter;
    view.innerHTML = head("Evals", "Scorecards from the repository's own eval scripts, committed under <code>demo/data/</code> by <code>scripts/build_console_data.py</code> (CI checks they are current).") + `<div id="evBody">${loading()}</div>`;
    const body = $("#evBody");
    let routes, gate, cal, tests;
    try {
      [routes, gate, cal, tests] = await Promise.all(["eval_routes", "eval_gate", "semcache_calibration", "tests"].map((n) => fetchJson(`./data/${n}.json`)));
    } catch (e) { body.innerHTML = errorState(e); return; }
    const rows = Object.entries(routes.routes).map(([alias, r]) => ({ alias, ...r.summary, targets: r.targets }));
    const h = cal.holdout_at_chosen, g = cal.holdout_without_guards_at_chosen;
    body.innerHTML = `
      <section class="card"><div class="card-head"><div><h2>Route evaluation: cost vs. quality</h2><p>${esc(routes.cases_source)} (${rows[0] ? rows[0].cases : 0} fictional cases) replayed through every alias with the gateway's fallback chain and pricing. <code>${esc(routes.command)}</code></p></div>${simTag("simulated: invented model profiles")}</div>
        <div class="grid g2 stack-md"><div>${scatter(Object.values(rows.reduce((acc, r) => {
          const k = `${r.cost_per_1k_requests_usd}|${r.quality}`;
          acc[k] = acc[k] ? { ...acc[k], name: `${acc[k].name} · ${r.alias}` } : { name: r.alias, x: r.cost_per_1k_requests_usd, y: r.quality };
          return acc;
        }, {})), { compact: compactCharts() })}</div>
        <div class="table-wrap"><table><thead><tr><th>Route</th><th class="num">Quality</th><th class="num">$/1K req</th><th class="num">p50</th><th class="num">p95</th><th class="num">Fallbacks</th></tr></thead><tbody>
          ${rows.sort((a, b) => b.quality - a.quality).map((r) => `<tr><td><b>${esc(r.alias)}</b><span class="sub">${esc(r.targets.join(" → "))}</span></td><td class="num">${r.quality.toFixed(3)}</td><td class="num">${usd(r.cost_per_1k_requests_usd, 4)}</td><td class="num">${ms(r.latency_p50_s * 1000)}</td><td class="num">${ms(r.latency_p95_s * 1000)}</td><td class="num">${r.fallbacks}</td></tr>`).join("")}
        </tbody></table></div></div>
        <p class="small muted" style="margin-top:8px">Simulated numbers describe the configured profiles in <code>evals/sim_profiles.json</code>, not real models. Run <code>scripts/eval_routes.py --provider real</code> with keys for a measured report.</p>
      </section>
      <div class="grid g2 stack-md" style="margin-top:16px">
        <section class="card"><div class="card-head"><div><h2>CI gate example</h2><p>${esc(gate.question)}</p></div>${simTag()}</div>
          <div class="row" style="margin-bottom:10px"><span class="pill ${gate.ok ? "ok" : "bad"}" id="gateResult">${gate.ok ? "PASS" : "FAIL"}</span><span class="small muted">limits: quality drop ≤ ${gate.limits.max_quality_drop}, cost increase ≤ ${pct(gate.limits.max_cost_increase, 0)}</span></div>
          <div class="table-wrap"><table><thead><tr><th></th><th class="num">Quality</th><th class="num">Cost (40 cases)</th></tr></thead><tbody>
            <tr><td>${esc(gate.baseline.alias)} (baseline)</td><td class="num">${gate.baseline.quality}</td><td class="num">${usd(gate.baseline.total_cost_usd, 6)}</td></tr>
            <tr><td>${esc(gate.candidate.alias)} (candidate)</td><td class="num">${gate.candidate.quality}</td><td class="num">${usd(gate.candidate.total_cost_usd, 6)}</td></tr></tbody></table></div>
          <ul class="small">${gate.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
          <p class="small muted">Cheaper by ${pct(-gate.cost_change_pct, 0)}, but the quality drop fails the gate. <code>scripts/eval_gate.py</code> exits 1 on this in CI.</p>
        </section>
        <section class="card"><div class="card-head"><div><h2>Test suite</h2><p>${esc(tests.measured)}.</p></div>${measuredTag("counted")}</div>
          <div class="row" style="margin-bottom:10px"><b class="num" style="font-size:22px">${int(tests.total)}</b><span class="muted">test functions in ${tests.files.length} files</span>${tests.last_run ? `<span class="pill ${tests.last_run.exit_code === 0 ? "ok" : "bad"}">${esc(tests.last_run.summary)}</span><span class="small faint">last recorded run ${esc(tests.last_run.date)}</span>` : ""}</div>
          <div class="table-wrap scroll"><table><thead><tr><th>File</th><th class="num">Tests</th></tr></thead><tbody>${tests.files.map((f) => `<tr><td><code>${esc(f.file)}</code>${f.about ? `<span class="sub">${esc(f.about)}</span>` : ""}</td><td class="num">${f.tests}</td></tr>`).join("")}</tbody></table></div>
        </section>
      </div>
      <section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Semantic cache calibration</h2><p>${esc(cal.pairs.total)} labelled pairs (fictional), threshold chosen on ${esc(cal.pairs.calibration)}, reported on ${esc(cal.pairs.holdout)} held out. Target false-hit rate ${pct(cal.target_false_hit_rate)}. <code>${esc(cal.command)}</code></p></div>${measuredTag("measured on bundled pairs")}</div>
        <div class="grid g2"><div>${curves(cal.calibration_grid, cal.chosen && cal.chosen.threshold, { compact: compactCharts() })}<div class="legend"><span><i style="background:#a78bfa"></i>hit rate</span><span><i style="background:#f87171"></i>false-hit rate</span><span><i style="background:#fbbf24"></i>chosen threshold</span></div></div>
        <div>
          <div class="kpis" style="grid-template-columns:repeat(2,minmax(0,1fr))">
            <div class="kpi"><div class="l">Chosen threshold</div><div class="v" id="calThr">${cal.chosen ? cal.chosen.threshold : "none"}</div><div class="s">${esc(cal.rule)}</div></div>
            <div class="kpi"><div class="l">Holdout hit rate</div><div class="v">${pct(h.hit_rate)}</div><div class="s">${h.true_hits} of ${cal.positives.holdout} same-meaning pairs</div></div>
            <div class="kpi ${h.false_hit_rate > cal.target_false_hit_rate ? "warn" : ""}"><div class="l">Holdout false-hit rate</div><div class="v" id="calHold">${pct(h.false_hit_rate)}</div><div class="s">${h.false_hits} of ${h.hits} hits · 95% upper ${pct(h.false_hit_rate_upper95)}</div></div>
            <div class="kpi"><div class="l">Without guards</div><div class="v">${pct(g.false_hit_rate)}</div><div class="s">false-hit rate at the same threshold</div></div>
          </div>
          <p class="small muted">The held-out false-hit rate misses the 1% target, which is why the semantic cache ships off. Calibrate on pairs labelled from your own traffic before turning it on.</p>
          ${A.caps.calibrate ? `<div class="row"><button id="calRun">Re-run calibration in your browser</button><span class="small muted" id="calMsg" aria-live="polite"></span></div>` : ""}
        </div></div>
      </section>`;
    if ($("#calRun")) $("#calRun").onclick = (e) => busy(e.currentTarget, async () => {
      const r = await A.calibrate(cal.target_false_hit_rate);
      const same = r.chosen && cal.chosen && r.chosen.threshold === cal.chosen.threshold && r.holdout_at_chosen.false_hit_rate === h.false_hit_rate;
      $("#calMsg").textContent = `In-browser result: threshold ${r.chosen ? r.chosen.threshold : "none"}, holdout ${pct(r.holdout_at_chosen.hit_rate)} hit / ${pct(r.holdout_at_chosen.false_hit_rate)} false-hit. ${same ? "Identical to the committed report." : "Differs from the committed report."}`;
    });
  },
};

// =============================================================================================
// Settings
// =============================================================================================

const settingsScreen = {
  id: "settings",
  title: "Settings",
  async render(view, ctx) {
    const A = ctx.app.adapter;
    const live = A.mode === "live";
    view.innerHTML = head("Settings", live ? "Connection, credentials kept by this browser tab, and the gateway's runtime settings." : "Gateway controls for the in-browser demo (in memory; Reset restores the defaults).") +
      `<div class="grid g2"><section class="card" id="stConn"></section><section class="card" id="stRoles"></section></div>
       <section class="card" id="stGateway" style="margin-top:16px">${loading()}</section>
       <section class="card" id="stAbout" style="margin-top:16px"></section>`;
    const conn = $("#stConn");
    if (live) {
      const keys = A.appKeys;
      conn.innerHTML = `<div class="card-head"><div><h2>Connection</h2><p>Live · ${esc(A.host)} · gateway ${esc(A.info.version || "")}${A.info.mock_providers ? " · simulated providers" : ""}</p></div><span class="mode-badge live"><span class="dot"></span>Live</span></div>
        <label class="field">Admin key<input id="stKey" type="password" autocomplete="off" placeholder="sk-admin-… or ROUTER_ADMIN_KEY" value="${A.adminKey ? "••••••••" : ""}"/></label>
        <div class="row" style="margin-top:8px"><label class="switch small"><input type="checkbox" id="stRemember" ${store.get("gag.adminKey") ? "checked" : ""}/> Remember on this device</label></div>
        <div class="row" style="margin-top:10px"><button class="primary" id="stSave">Save and test</button><button class="ghost" id="stForget">Forget</button><span class="small" id="stMsg" aria-live="polite"></span></div>
        <p class="small muted" style="margin-top:10px">Kept in this tab's session storage (or this device's local storage if you tick Remember), sent only to this gateway as a bearer token. With docker compose: <code>docker compose exec gateway cat /data/admin-key.txt</code>.</p>
        <div class="hr"></div><h3>Playground app keys in this tab</h3>
        ${Object.keys(keys).length ? Object.entries(keys).map(([l, e]) => `<div class="dep"><span>${esc(l)} <span class="faint small">${esc(e.team)}</span></span><button class="sm ghost" data-forget="${esc(l)}">Forget</button></div>`).join("") : '<p class="small muted">None yet: the Playground creates them.</p>'}`;
      $("#stSave").onclick = (e) => busy(e.currentTarget, async () => {
        const v = $("#stKey").value;
        if (v && v !== "••••••••") A.setAdminKey(v.trim(), $("#stRemember").checked);
        try { await A.overview(); $("#stMsg").textContent = "Connected."; $("#stMsg").className = "small"; toast("Admin key accepted", "ok"); }
        catch (err) { $("#stMsg").textContent = err.message; $("#stMsg").className = "small"; $("#stMsg").style.color = "var(--bad)"; }
      });
      $("#stForget").onclick = () => { A.setAdminKey(""); $("#stKey").value = ""; $("#stMsg").textContent = "Forgotten."; };
      $$("[data-forget]", conn).forEach((b) => (b.onclick = () => { A.saveAppKey(b.dataset.forget, null); b.closest(".dep").remove(); }));
    } else {
      conn.innerHTML = `<div class="card-head"><div><h2>Connection</h2><p>Demo · runs in your browser. Nothing is sent anywhere.</p></div><span class="mode-badge"><span class="dot"></span>Demo</span></div>
        <p class="small">To connect to a real gateway: <code>docker compose up</code>, then open <code>http://localhost:4000/console/</code>. The console detects the gateway and switches to live mode; enter the admin key here.</p>
        <p class="small muted">Python ${esc(A.pyVersion)} in Pyodide 0.26.4, started in ${A.bootSeconds ? A.bootSeconds.toFixed(1) : "?"} s.</p>
        <div class="row"><button id="stReset" class="danger">Reset demo state</button></div>`;
      $("#stReset").onclick = async () => { await A.reset(); toast("Demo reset to defaults", "ok"); ctx.app.screen = null; ctx.navigate("#/settings"); };
    }
    $("#stRoles").innerHTML = `<div class="card-head"><h2>Roles</h2></div>
      <div class="table-wrap"><table><thead><tr><th>Credential</th><th>Can</th></tr></thead><tbody>
      <tr><td><b>Admin key</b><span class="sub">ROUTER_ADMIN_KEY or an admin-flagged key</span></td><td class="small">Every <code>/admin/*</code> endpoint: keys, budgets, config, traces, showback, audit; <code>/metrics</code>.</td></tr>
      <tr><td><b>App key</b><span class="sub">POST /admin/keys</span></td><td class="small"><code>/v1/chat/completions</code>, <code>/v1/models</code> and <code>/mcp/{server}</code> as its team; subject to that team's policy, budgets and tool allow-list.</td></tr>
      <tr><td><b>No key</b></td><td class="small"><code>/health</code>, <code>/console/</code> static files, <code class="nw">/console/api-mode</code>.</td></tr>
      </tbody></table></div>`;
    const gw = $("#stGateway");
    try {
      const s = await A.settings();
      if (s.live) {
        const bc = s.breaker_config.circuit_breaker || {};
        gw.innerHTML = `<div class="card-head"><div><h2>Gateway settings</h2><p>Read from the running gateway. Change them in <a href="#/policies?file=routes">routes.yaml</a> or <a href="#/policies">policies.yaml</a>.</p></div></div>
          <div class="grid g3"><div><h3>Circuit breakers</h3><p class="small muted">${bc.enabled ? "on" : "off"} · open after ${esc(bc.failure_threshold)} failures or ${pct(bc.error_rate_threshold, 0)} errors · cooldown ${esc(bc.cooldown_seconds)} s</p></div>
          <div><h3>Semantic cache</h3><p class="small muted">${s.semantic.config.enabled ? "on" : "off"} · threshold ${esc(s.semantic.config.threshold)} · ${esc(s.semantic.entries)} entries (${esc(s.semantic.scope)})</p></div>
          <div><h3>OPA</h3><p class="small muted">${s.policies.opa.enabled ? "enabled" : "off"}${s.policies.opa.url_set ? " · OPA_URL set" : ""}</p></div></div>
          <h3 style="margin-top:12px">Route strategies</h3><div class="chips" style="margin-top:6px">${Object.entries(s.strategies).map(([a, st]) => `<span class="chip">${esc(a)}: ${esc(st)}</span>`).join("")}</div>`;
      } else {
        const modes = ["off", "detect", "redact", "block"];
        gw.innerHTML = `<div class="card-head"><div><h2>Gateway controls</h2><p>The same switches the gateway reads from routes.yaml, applied to the in-browser engine.</p></div>${realTag()}</div>
          <div class="row" style="gap:18px;margin-bottom:14px"><label class="switch"><input type="checkbox" data-set="breakers" ${s.breakers_enabled ? "checked" : ""}/> Circuit breakers</label>
          <label class="switch"><input type="checkbox" data-set="auto_pause" ${s.auto_pause ? "checked" : ""}/> Anomaly auto-pause</label>
          <label class="switch"><input type="checkbox" data-set="cache" ${s.cache ? "checked" : ""}/> Exact cache (temperature 0)</label></div>
          <div class="table-wrap"><table><thead><tr><th>Team</th><th>PII hooks</th><th>Content log (redacted)</th><th>Semantic cache</th></tr></thead><tbody>
          ${s.teams.map((t) => { const c = s.content[t] || { mode: "off", log_content: false }; return `<tr data-team="${esc(t)}"><td>${esc(t)}</td><td><select data-mode aria-label="PII hook mode for ${esc(t)}">${modes.map((m) => opt(m, m, m === c.mode)).join("")}</select></td>
            <td><label class="switch"><input type="checkbox" data-log ${c.log_content ? "checked" : ""} aria-label="Content log for ${esc(t)}"/> <span data-log-label>${c.log_content ? "on" : "off"}</span></label></td>
            <td><label class="switch"><input type="checkbox" data-sem ${s.semantic_teams.includes(t) ? "checked" : ""} aria-label="Semantic cache for ${esc(t)}"/> threshold ${esc(s.semantic_threshold)}</label></td></tr>`; }).join("")}
          </tbody></table></div>
          <p class="small muted" style="margin-top:8px">Breakers in the demo open after ${esc(s.breaker_config.failure_threshold)} consecutive failures with a ${esc(s.breaker_config.cooldown_seconds)} s cooldown on a virtual clock (the shipped routes.yaml uses 5 and 30 s). Token estimates: ${esc(s.estimator)}.</p>`;
        $$("[data-set]", gw).forEach((i) => (i.onchange = async () => { await A.setSetting(i.dataset.set, i.checked); toast("Saved", "ok"); }));
        $$("tr[data-team]", gw).forEach((row) => {
          const team = row.dataset.team;
          const save = async () => {
            await A.setSetting("content", { team, mode: $("[data-mode]", row).value, log_content: $("[data-log]", row).checked });
            $("[data-log-label]", row).textContent = $("[data-log]", row).checked ? "on" : "off";
            toast(`Content policy for ${team} saved`, "ok");
          };
          $("[data-mode]", row).onchange = save;
          $("[data-log]", row).onchange = save;
          $("[data-sem]", row).onchange = async (e) => { await A.setSetting("semantic", { team, enabled: e.target.checked }); toast(`Semantic cache ${e.target.checked ? "on" : "off"} for ${team}`, "ok"); };
        });
      }
    } catch (e) { gw.innerHTML = errorState(e); }
    $("#stAbout").innerHTML = `<div class="card-head"><div><h2>About this console</h2><p>One codebase, two modes: Pyodide demo (GitHub Pages) and live (served by the gateway at <code>/console/</code>).</p></div><button id="stTour">Restart the guided tour</button></div>
      ${A.files.length ? `<h3>Files running in your browser</h3><ul class="small mono" id="stFiles">${A.files.map((f) => `<li>${esc(f.path)} · ${f.lines} lines · sha256 ${esc(f.sha)}…${f.sim ? " (simulation harness)" : ""}${f.lazy ? " (loaded on demand)" : ""}</li>`).join("")}</ul>
        <p class="small muted">Fetched from this repository and run unchanged. Pyodide and its PyYAML / pydantic packages load from cdn.jsdelivr.net. The FastAPI app, LiteLLM and SQLite are not loaded in the browser.</p>` : ""}`;
    $("#stTour").onclick = () => { store.del("gag.tourDone"); $("#tourBtn").click(); };
  },
};

export const SCREENS = [overview, playground, tracesScreen, policies, budgetsScreen, mcpScreen, evalsScreen, settingsScreen];
