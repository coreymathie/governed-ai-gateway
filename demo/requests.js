// Governed AI Gateway console: the request log and the request detail page. Corey Mathie, 2026.
// Renders the sample company's request log (demo/data/sample_requests.json, scripts/sample_requests.py) without
// waiting for the Python engine. Requests sent in this tab are on Requests › This session (the Traces screen).
import { fetchJson } from "./adapters.js";
import { $, $$, download, empty, esc, int, ms } from "./ui.js";

export const TEAM_LABELS = {
  "member-services": "Member services", "digital-banking": "Digital banking", "risk-analytics": "Risk analytics",
  lending: "Lending", compliance: "Compliance", "it-engineering": "IT and engineering", marketing: "Marketing",
};
const MODEL_NAMES = {
  "anthropic/claude-haiku-4-5": "Claude Haiku 4.5", "anthropic/claude-sonnet-4-5": "Claude Sonnet 4.5",
  "openai/gpt-4.1-mini": "GPT-4.1 mini", "gemini/gemini-2.5-flash": "Gemini 2.5 Flash", "ollama/llama3.1:8b": "Llama 3.1 8B (on-prem)",
};
export const modelName = (id) => MODEL_NAMES[id] || id || "–";
const PROVIDERS = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", ollama: "On-prem" };
const provider = (id) => PROVIDERS[String(id || "").split("/")[0]] || "";

// Stage names come from router/traces.py; the business view uses plain words for the same stages.
const STAGE_TECH = {
  auth: "Auth + RPM", policy: "Policy (YAML / OPA)", budgets: "Budgets", anomaly: "Anomaly pause",
  pre_hooks: "Content hooks (request)", cache: "Exact cache", semantic_cache: "Semantic cache", tpm: "TPM reservation",
  chain: "Fallback chain", settle: "Settle tokens + cost", post_hooks: "Content hooks (response)", content_log: "Content log",
};
const STAGE_PLAIN = {
  auth: "App identified", policy: "Policy check", budgets: "Budget check", anomaly: "Spending pattern",
  pre_hooks: "Personal data in the prompt", cache: "Cache", semantic_cache: "Similar-question cache", tpm: "Token rate cap",
  chain: "Model call", settle: "Cost recorded", post_hooks: "Personal data in the answer", content_log: "Prompt and answer text",
};
const DECISION_ICON = { pass: "✓", deny: "✕", error: "!", hit: "★", miss: "○", skip: "–" };

export function outcomeOf(r) {
  if (r.outcome === "cache_hit") return { key: "cache", label: "From cache", cls: "hit" };
  if (r.status === 403) return { key: "refused", label: "Refused by policy", cls: "rejected" };
  if (r.status === 429) return { key: "limited", label: "Rate limited", cls: "rejected" };
  if (r.status >= 500) return { key: "failed", label: "Failed", cls: "error" };
  if (r.fell_back) return { key: "fallback", label: "Answered after fallback", cls: "warn" };
  return { key: "ok", label: "Answered", cls: "ok" };
}
const OUTCOME_FILTERS = [
  ["", "All outcomes"], ["ok", "Answered"], ["fallback", "Answered after fallback"], ["cache", "From cache"],
  ["refused", "Refused by policy"], ["limited", "Rate limited"], ["failed", "Failed"], ["redacted", "Personal data redacted"],
];

const TZ = "America/New_York";
const when = (iso, opts) => new Date(iso).toLocaleString("en-US", { timeZone: TZ, ...opts });
const stamp = (iso) => when(iso, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const longStamp = (iso) => when(iso, { weekday: "long", month: "long", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" }) + " ET";
const dayKey = (iso) => when(iso, { year: "numeric", month: "2-digit", day: "2-digit" });
const dayLabel = (iso) => when(iso, { weekday: "short", month: "short", day: "numeric" });
const money = (n) => (n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(n < 1 ? 3 : 2)}`);
const total = (n) => `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const head = (title, sub, actions = "") =>
  `<div class="page-head"><div><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div><div class="row">${actions}</div></div>`;
const tabs = (active) => `<div class="tabs page-tabs" role="tablist" aria-label="Requests">${[["log", "Sample company", "#/requests"], ["session", "This session", "#/traces"]]
  .map(([k, label, href]) => `<a role="tab" href="${href}" aria-selected="${k === active}" id="rqtab-${k}">${label}</a>`).join("")}</div>`;

let LOG = null;
export async function loadLog() {
  if (!LOG) LOG = await fetchJson("./data/sample_requests.json");
  return LOG;
}

function matches(r, f) {
  if (f.team && r.team !== f.team) return false;
  if (f.model && r.served_by !== f.model) return false;
  if (f.day && dayKey(r.ts) !== f.day) return false;
  if (f.outcome === "redacted") { if (!r.redacted) return false; }
  else if (f.outcome && outcomeOf(r).key !== f.outcome) return false;
  if (f.q) {
    const hay = `${r.id} ${r.app} ${r.key} ${r.caller} ${r.alias} ${r.served_by || ""} ${modelName(r.served_by)} ${TEAM_LABELS[r.team] || r.team} ${r.status}`.toLowerCase();
    if (!f.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

const filterState = { q: "", team: "", outcome: "", model: "", day: "", shown: 50 };

function csv(rows) {
  const cols = ["id", "ts", "team", "app", "key", "caller", "alias", "served_by", "status", "outcome", "fell_back", "prompt_tokens", "completion_tokens", "cost_usd", "saved_usd", "latency_ms"];
  const cell = (v) => (/[",\n]/.test(String(v ?? "")) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? ""));
  return [cols.join(","), ...rows.map((r) => cols.map((c) => cell(r[c])).join(","))].join("\n") + "\n";
}

async function renderLog(view, ctx) {
  const data = await loadLog();
  const reqs = data.requests;
  const days = [...new Set(reqs.map((r) => dayKey(r.ts)))];
  const models = [...new Set(reqs.map((r) => r.served_by).filter(Boolean))];
  const p = ctx.params || new URLSearchParams();
  for (const k of ["q", "team", "outcome", "model", "day"]) if (p.has(k)) filterState[k] = p.get(k);
  const f = filterState;
  view.innerHTML = head("Requests",
    `Every call the gateway handled for <b>Cypress Harbor Credit Union</b>: which app made it, which model answered, what it cost and every control it passed. <span class="tag sample">sample</span>`,
    `<button type="button" id="rqCsv" class="ghost">Export CSV</button>`) + tabs("log") + `
    <div class="card filters" style="margin-bottom:14px"><div class="form-grid rq-filters">
      <label class="field wide">Search<input id="rqQ" type="search" value="${esc(f.q)}" placeholder="App, caller, model or request ID"/></label>
      <label class="field">Team<select id="rqTeam"><option value="">All teams</option>${Object.entries(TEAM_LABELS).map(([k, v]) => `<option value="${k}" ${k === f.team ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <label class="field">Outcome<select id="rqOutcome">${OUTCOME_FILTERS.map(([k, v]) => `<option value="${k}" ${k === f.outcome ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <label class="field">Model<select id="rqModel"><option value="">All models</option>${models.map((m) => `<option value="${esc(m)}" ${m === f.model ? "selected" : ""}>${esc(modelName(m))}</option>`).join("")}</select></label>
      <label class="field">Day<select id="rqDay"><option value="">Oct 1 – 7</option>${days.map((d) => `<option value="${d}" ${d === f.day ? "selected" : ""}>${esc(dayLabel(reqs.find((r) => dayKey(r.ts) === d).ts))}</option>`).join("")}</select></label>
    </div></div>
    <div id="rqSummary" class="rq-summary"></div><div id="rqBody"></div>
    <p class="small faint" style="margin-top:10px">Times are Eastern. A sample of ${int(reqs.length)} of the ${int(data.sampled_from)} requests the sample company sent from October 1 to 7, generated by <code>${esc(data.generated_by)}</code><span class="tech-only"> (seed ${esc(data.seed)}; prices are list prices per 1M tokens, on-prem at an assumed GPU chargeback)</span>. Requests hold metadata only: the gateway never stores prompt or answer text unless a team opts in.</p>`;
  const paint = () => {
    if (!$("#rqSummary", view)) return; // a debounced search can land after the user has moved on
    const rows = reqs.filter((r) => matches(r, f));
    const spend = rows.reduce((a, r) => a + r.cost_usd, 0), saved = rows.reduce((a, r) => a + r.saved_usd, 0);
    const refused = rows.filter((r) => r.status >= 400).length, fb = rows.filter((r) => r.fell_back).length;
    $("#rqSummary", view).innerHTML = `<div><b>${int(rows.length)}</b> ${rows.length === 1 ? "request" : "requests"}</div><div><b>${total(spend)}</b> spent on these requests</div><div><b>${total(saved)}</b> saved by the cache</div><div><b>${int(fb)}</b> rescued by fallback</div><div><b>${int(refused)}</b> refused or failed</div>`;
    if (!rows.length) {
      $("#rqBody", view).innerHTML = empty("No requests match these filters", "Clear the search or choose another team, outcome, model or day.", '<button type="button" id="rqClear">Clear filters</button>');
      $("#rqClear", view).onclick = () => { Object.assign(f, { q: "", team: "", outcome: "", model: "", day: "" }); renderLog(view, { ...ctx, params: new URLSearchParams() }); };
      return;
    }
    const shown = rows.slice(0, f.shown);
    $("#rqBody", view).innerHTML = `<div class="table-wrap"><table class="dense rq-table"><thead><tr>
      <th>Time</th><th>App</th><th class="hide-narrow">Caller</th><th>Model</th><th>Outcome</th><th class="num">Latency</th><th class="num">Cost</th>
      <th class="tech-only">Key · alias</th><th class="tech-only num">Tokens</th><th class="tech-only">Request</th></tr></thead><tbody>
      ${shown.map((r) => {
        const o = outcomeOf(r);
        return `<tr class="click" tabindex="0" data-id="${esc(r.id)}"><td class="nw">${esc(stamp(r.ts))}</td>
          <td><a href="#/requests/${esc(r.id)}" class="row-link">${esc(r.app)}</a><span class="sub">${esc(TEAM_LABELS[r.team] || r.team)}</span></td>
          <td class="hide-narrow small">${esc(r.caller)}</td>
          <td>${r.served_by ? esc(modelName(r.served_by)) : '<span class="faint">–</span>'}<span class="sub">${esc(provider(r.served_by))}</span></td>
          <td><span class="pill ${o.cls}">${esc(o.label)}</span>${r.redacted ? '<span class="sub">personal data redacted</span>' : ""}</td>
          <td class="num">${r.outcome === "rejected" ? '<span class="faint">–</span>' : ms(r.latency_ms)}</td>
          <td class="num">${r.saved_usd ? `<span title="saved ${money(r.saved_usd)}">$0</span>` : money(r.cost_usd)}</td>
          <td class="tech-only nw small"><code>${esc(r.key)}</code><span class="sub">${esc(r.alias)}</span></td>
          <td class="tech-only num small">${int(r.prompt_tokens)} / ${int(r.completion_tokens)}</td>
          <td class="tech-only"><code class="small">${esc(r.id)}</code></td></tr>`;
      }).join("")}</tbody></table></div>
      <div class="row" style="margin-top:8px"><span class="small faint" id="rqCount">Showing ${int(shown.length)} of ${int(rows.length)}</span>${rows.length > shown.length ? '<button type="button" class="sm" id="rqMore">Show 50 more</button>' : ""}</div>`;
    if ($("#rqMore", view)) $("#rqMore", view).onclick = () => { f.shown += 50; paint(); };
    $$("tr[data-id]", view).forEach((tr) => {
      tr.onclick = (e) => { if (!e.target.closest("a")) ctx.navigate(`#/requests/${tr.dataset.id}`); };
      tr.onkeydown = (e) => { if (e.key === "Enter") ctx.navigate(`#/requests/${tr.dataset.id}`); };
    });
  };
  const bind = (id, key) => { $(id, view).onchange = (e) => { f[key] = e.target.value; f.shown = 50; paint(); }; };
  let deb;
  $("#rqQ", view).oninput = (e) => { clearTimeout(deb); deb = setTimeout(() => { f.q = e.target.value.trim(); f.shown = 50; paint(); }, 150); };
  bind("#rqTeam", "team"); bind("#rqOutcome", "outcome"); bind("#rqModel", "model"); bind("#rqDay", "day");
  $("#rqCsv", view).onclick = () => download("cypress-harbor-requests.csv", csv(reqs.filter((r) => matches(r, f))));
  paint();
}

// ---------- request detail ----------

function story(r) {
  const o = outcomeOf(r);
  const who = esc(`${r.app} (${TEAM_LABELS[r.team] || r.team})`);
  if (o.key === "refused") return `${who} asked for ${r.alias === "heavy-reasoning" ? "a premium reasoning model" : esc(r.alias)}. The policy for this key doesn't allow it, so the gateway refused before any provider saw the prompt. Nothing was charged.`;
  if (o.key === "limited") return `${who} sent more requests in one minute than its key allows. The gateway turned this one away and told the app when to retry. Nothing was charged.`;
  if (o.key === "failed") return `${who} sent a regulated request. The on-prem model didn't answer in time, and regulated work never falls back to a cloud provider, so the request failed instead of leaving the network.`;
  if (o.key === "cache") return `${who} asked a question the team had asked recently. The gateway answered from its cache in ${ms(r.latency_ms)}, so no model was called and the ${money(r.saved_usd)} the call would have cost was saved.`;
  const fb = r.fell_back ? `The first model didn't answer (${esc(firstError(r))}), so the gateway retried on ${modelName(r.served_by)} within the same request. ` : "";
  const pii = r.redacted ? `Personal data was replaced before the prompt left the gateway (${Object.entries(r.redacted).map(([k, v]) => `${v} ${k}`).join(", ")}). ` : "";
  return `${who} sent a request${r.regulated ? " that must stay on-prem" : ""}. ${pii}${fb}${modelName(r.served_by)} answered in ${ms(r.latency_ms)} and it cost ${money(r.cost_usd)}, charged to ${TEAM_LABELS[r.team] || r.team}.`;
}
const firstError = (r) => (r.stages.find((s) => s.stage === "chain")?.detail?.attempts || []).find((a) => a.outcome === "error")?.error || "error";

// Business-view wording for each stage; the technical view shows the gateway's own summary.
function plain(r, s) {
  const team = TEAM_LABELS[r.team] || r.team;
  const d = s.detail || {};
  const redacted = (x) => Object.entries(x).map(([k, v]) => `${v} ${k === "card" ? "card number" : k === "phone" ? "phone number" : k === "email" ? "email address" : k}${v > 1 ? "s" : ""}`).join(" and ");
  switch (s.stage) {
    case "auth": return s.decision === "deny" ? "Too many requests from this app in one minute." : `The ${r.app} app's key; usage is charged to ${team}.`;
    case "policy":
      if (s.decision === "deny") return "Refused: this key's policy doesn't allow premium reasoning models.";
      return r.regulated ? "Allowed, on the condition that it stays on the on-prem model." : `Allowed on the ${r.alias} route${s.summary.includes("pii_redact") ? ", with personal-data redaction required" : ""}.`;
    case "budgets": return d.team_month_usd != null ? `${team} has used ${total(d.team_month_usd)} of its ${total(d.team_monthly_budget_usd)} budget this month.` : s.summary;
    case "anomaly": return "Normal for this app at this time of day.";
    case "pre_hooks": return s.decision === "skip" ? "Not checked for this app." : d.redacted ? `Replaced ${redacted(d.redacted)} before the prompt left the gateway.` : "Checked; none found.";
    case "cache": return s.decision === "hit" ? `Asked recently, so answered from the cache instead of calling a model.` : s.decision === "skip" ? "Not cached: this app asks for varied answers." : "Not asked recently, so sent to a model.";
    case "semantic_cache": return "Off for this team.";
    case "tpm": return "No token-rate cap on this app.";
    case "chain": {
      const a = d.attempts || [];
      if (s.decision === "error") return "The on-prem model timed out. Regulated work has no cloud fallback, so the request failed.";
      return a.length > 1 ? `${modelName(a[0].deployment)} failed (${a[0].error}); ${modelName(r.served_by)} answered instead.` : `${modelName(r.served_by)} answered on the first try.`;
    }
    case "settle": return `${int(r.prompt_tokens)} tokens in and ${int(r.completion_tokens)} out cost ${money(r.cost_usd)}.`;
    case "post_hooks": return s.decision === "skip" ? "Not checked for this app." : "Checked; none found.";
    case "content_log": return "Not stored. The gateway keeps only who asked, what it cost and how it was handled.";
    default: return s.summary;
  }
}

function timeline(r) {
  return `<ol class="tl rq-tl">${r.stages.map((s) => {
    const attempts = s.detail?.attempts;
    const rest = s.detail ? Object.fromEntries(Object.entries(s.detail).filter(([k]) => k !== "attempts")) : {};
    return `<li><span class="ic ${esc(s.decision)}" aria-hidden="true">${DECISION_ICON[s.decision] || "·"}</span>
      <div class="st"><b><span class="biz-only">${esc(STAGE_PLAIN[s.stage] || s.stage)}</span><span class="tech-only">${esc(STAGE_TECH[s.stage] || s.stage)}</span></b><span class="small faint num tech-only">${s.ms == null ? "" : ms(s.ms)}</span></div>
      <div class="sm"><span class="biz-only">${esc(plain(r, s))}</span><span class="tech-only">${esc(s.summary)}</span></div>
      ${attempts ? `<div class="attempts">${attempts.map((a) => `<div class="attempt"><span class="pill ${a.outcome === "ok" ? "pass" : "deny"}" aria-hidden="true">${a.outcome === "ok" ? "✓" : "✕"}</span><span>${esc(modelName(a.deployment))}</span><span class="small muted">${a.outcome === "ok" ? ms(a.seconds * 1000) : esc(a.error)}</span></div>`).join("")}</div>` : ""}
      ${Object.keys(rest).length ? `<details class="tech-only"><summary>details</summary><pre class="box">${esc(JSON.stringify(rest, null, 2))}</pre></details>` : ""}</li>`;
  }).join("")}</ol>`;
}

async function renderDetail(view, ctx, id) {
  const data = await loadLog();
  const r = data.requests.find((x) => x.id === id);
  if (!r) {
    view.innerHTML = head("Request not found") + empty("There's no request with that ID", "It may be from another session. The sample log keeps October 1 to 7.", '<a class="btn primary" href="#/requests">All requests</a>');
    return;
  }
  const o = outcomeOf(r);
  const budget = r.stages.find((s) => s.stage === "budgets")?.detail;
  const settle = r.stages.find((s) => s.stage === "settle")?.detail;
  const sameApp = data.requests.filter((x) => x.app === r.app && x.id !== r.id).slice(0, 5);
  const idx = data.requests.indexOf(r);
  const newer = data.requests[idx - 1], older = data.requests[idx + 1];
  const inIncident = r.ts >= "2026-10-06T18:05:00Z" && r.ts <= "2026-10-06T18:17:00Z";
  const used = budget ? budget.team_month_usd / budget.team_monthly_budget_usd : 0;
  view.innerHTML = `<div class="rq-nav"><a href="#/requests" class="back">← All requests</a><span class="row">${newer ? `<a class="btn sm ghost" href="#/requests/${esc(newer.id)}" aria-label="Newer request">← Newer</a>` : ""}${older ? `<a class="btn sm ghost" href="#/requests/${esc(older.id)}" aria-label="Older request">Older →</a>` : ""}</span></div>
    <div class="page-head rq-head"><div><div class="small muted">${esc(longStamp(r.ts))}</div>
      <h1>${esc(r.app)} <span class="muted">·</span> ${r.served_by ? esc(modelName(r.served_by)) : esc(o.label)}</h1>
      <div class="row" style="margin-top:6px"><span class="pill ${o.cls}">${esc(o.label)}</span><span class="chip">${esc(TEAM_LABELS[r.team] || r.team)}</span><span class="chip">${esc(r.caller)}</span>${r.redacted ? '<span class="chip">personal data redacted</span>' : ""}${r.regulated ? '<span class="chip">on-prem only</span>' : ""}<span class="tag sample">sample</span></div></div></div>
    ${inIncident ? `<div class="note" role="note">Sent during the <a href="#/overview">October 6 Anthropic overload</a> (2:05 to 2:17 pm). <a href="#/requests?day=${encodeURIComponent(dayKey(r.ts))}&outcome=fallback">Other requests rescued by fallback that day</a>.</div>` : ""}
    <section class="card rq-story"><h2>What happened</h2><p>${story(r)}</p>${r.reason ? `<p class="small muted">Gateway response: ${r.status} · ${esc(r.reason)}</p>` : ""}</section>
    <div class="kpis rq-kpis">
      <div class="kpi"><div class="l">Cost</div><div class="v">${r.saved_usd ? "$0" : money(r.cost_usd)}</div><div class="s">${r.saved_usd ? `saved ${money(r.saved_usd)} by the cache` : r.prompt_tokens && r.completion_tokens && settle ? `${int(r.prompt_tokens)} tokens in, ${int(r.completion_tokens)} out` : "not charged"}</div></div>
      <div class="kpi"><div class="l">Response time</div><div class="v">${r.outcome === "rejected" ? "–" : ms(r.latency_ms)}</div><div class="s">${ms(r.overhead_ms)} of it in the gateway</div></div>
      <div class="kpi"><div class="l">Model</div><div class="v sm">${esc(r.served_by ? modelName(r.served_by) : "none")}</div><div class="s">${r.fell_back ? `after ${esc(modelName(r.stages.find((s) => s.stage === "chain").detail.attempts[0].deployment))} failed` : `route <code>${esc(r.alias)}</code>`}</div></div>
      ${budget ? `<div class="kpi"><div class="l">${esc(TEAM_LABELS[r.team] || r.team)} budget</div><div class="v sm">${total(budget.team_month_usd)} <span class="muted small">of ${total(budget.team_monthly_budget_usd)}</span></div><div class="s"><span class="meter ${used >= 0.9 ? "bad" : used >= 0.75 ? "warn" : "ok"}" role="img" aria-label="${Math.round(used * 100)}% of the month's budget"><i style="width:${Math.min(100, used * 100).toFixed(1)}%"></i></span> October so far, before this day</div></div>` : ""}
    </div>
    <div class="grid g-main" style="margin-top:16px">
      <section class="card"><div class="card-head"><div><h2>Every control it passed</h2><p>In order, as the gateway recorded them.</p></div></div>${timeline(r)}</section>
      <div class="stack">
        <section class="card"><h2>Details</h2><dl class="kv">
          <dt>App</dt><dd>${esc(r.app)}</dd><dt>Team</dt><dd>${esc(TEAM_LABELS[r.team] || r.team)}</dd><dt>Caller</dt><dd>${esc(r.caller)}</dd>
          <dt>Route</dt><dd><code>${esc(r.alias)}</code></dd>
          <dt>Answered by</dt><dd>${r.served_by ? `${esc(modelName(r.served_by))} <span class="muted small">(${esc(provider(r.served_by))})</span>` : "–"}</dd>
          ${settle ? `<dt>Price</dt><dd>$${settle.input_usd_per_1m} in / $${settle.output_usd_per_1m} out per 1M tokens</dd>` : ""}
          <dt class="tech-only">Key</dt><dd class="tech-only"><code>${esc(r.key)}</code> · <code>${esc(r.key_fp)}</code>${r.profile ? ` · policy profile <code>${esc(r.profile)}</code>` : ""}</dd>
          <dt class="tech-only">Request ID</dt><dd class="tech-only"><code>${esc(r.id)}</code></dd>
          <dt class="tech-only">Status</dt><dd class="tech-only">${r.status} · ${esc(r.outcome)} · temperature ${r.temperature}</dd>
        </dl></section>
        ${sameApp.length ? `<section class="card"><h2>Recent from ${esc(r.app)}</h2><ul class="rq-related">${sameApp.map((x) => `<li><a href="#/requests/${esc(x.id)}">${esc(stamp(x.ts))}</a><span class="pill ${outcomeOf(x).cls}">${esc(outcomeOf(x).label)}</span><span class="num small">${x.saved_usd ? "$0" : money(x.cost_usd)}</span></li>`).join("")}</ul>
          <a class="small" href="#/requests?q=${encodeURIComponent(r.app)}">All requests from ${esc(r.app)} →</a></section>` : ""}
        <details class="card tech-only"><summary>Raw record</summary><pre class="box">${esc(JSON.stringify({ ...r, stages: undefined }, null, 2))}</pre></details>
      </div>
    </div>`;
}

export const requestsScreen = {
  id: "requests",
  title: "Requests",
  static: true,
  async render(view, ctx) {
    if (ctx.arg && ctx.arg !== "session") return renderDetail(view, ctx, ctx.arg);
    return renderLog(view, ctx);
  },
};

export async function latestRequests(n = 6) {
  const data = await loadLog();
  return data.requests.slice(0, n);
}
export { stamp, money, outcomeOf as outcome, tabs as requestTabs };
