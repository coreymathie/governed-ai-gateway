// Governed AI Gateway console: shared UI helpers and inline-SVG charts (no chart library). Corey Mathie, 2026.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export function usd(v, digits) {
  const n = Number(v || 0);
  if (digits == null) digits = n !== 0 && Math.abs(n) < 0.01 ? 5 : n < 100 ? 4 : 2;
  return "$" + n.toFixed(digits);
}
export const pct = (v, d = 1) => (v == null ? "–" : (Number(v) * 100).toFixed(d) + "%");
export const int = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
export const ms = (v) => (v == null ? "–" : Number(v) >= 1000 ? (Number(v) / 1000).toFixed(2) + " s" : Math.round(Number(v)) + " ms");

export function toast(msg, kind = "") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

export function loading(label = "Loading…") {
  return `<div class="loading" role="status"><span class="spin" aria-hidden="true"></span><span>${esc(label)}</span></div>`;
}
export function empty(title, body = "", action = "") {
  return `<div class="empty"><h3>${esc(title)}</h3>${body ? `<p class="small">${body}</p>` : ""}${action}</div>`;
}
export function errorState(err, retryId = "") {
  const msg = err && err.message ? err.message : String(err);
  const isAuth = err && (err.status === 401 || err.status === 403);
  const action = isAuth ? `<a class="btn primary" href="#/settings">Open Settings</a>` : retryId ? `<button id="${retryId}" type="button">Try again</button>` : "";
  return `<div class="error-state" role="alert"><h3>${isAuth ? "Admin key needed" : "Something went wrong"}</h3><p class="small">${esc(msg)}</p>${action}</div>`;
}

export const simTag = (label = "simulated") => `<span class="tag sim" title="Simulated: not real provider traffic">${esc(label)}</span>`;
export const realTag = (label = "real code") => `<span class="tag real">${esc(label)}</span>`;
export const measuredTag = (label = "measured") => `<span class="tag measured">${esc(label)}</span>`;

export function statusPill(status, outcome) {
  const cls = outcome || (status < 400 ? "ok" : status >= 500 ? "error" : "rejected");
  return `<span class="pill ${esc(cls)}">${esc(status ?? "–")}${outcome === "cache_hit" ? " · cache" : ""}</span>`;
}

const DECISION_ICON = { pass: "✓", deny: "✕", error: "!", hit: "★", miss: "○", skip: "–" };
export const decisionIcon = (d) => DECISION_ICON[d] || "·";

export function stageChips(stages) {
  if (!stages || !stages.length) return "";
  return `<div class="timeline-mini">${stages
    .map((s) => `<span class="chip ${esc(s.decision)}" title="${esc(s.summary)}">${esc(decisionIcon(s.decision))} ${esc(s.label)}</span>`)
    .join("")}</div>`;
}

export function download(name, text, type = "text/csv") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function busy(btn, fn) {
  if (!btn || btn.disabled) return;
  const html = btn.innerHTML;
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.innerHTML = `<span class="spin" aria-hidden="true"></span>${html}`;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.innerHTML = html;
  }
}

// ---------- charts ----------

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/** Hourly spend bars for today with the 7-day average per hour as a line. */
export function hourlyChart({ today, baseline, currentHour, simulatedBefore = null }) {
  const W = 720, H = 240, L = 54, R = 10, T = 12, B = 30;
  const max = niceMax(Math.max(...today, ...baseline, 0.000001) * 1.08);
  const bw = (W - L - R) / 24;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const fmt = (v) => (max < 0.1 ? "$" + v.toFixed(3) : max < 10 ? "$" + v.toFixed(2) : "$" + Math.round(v));
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Spend per hour today compared with the 7-day average for each hour">`;
  svg += `<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#a78bfa" opacity="0.45"/><line x1="0" y1="0" x2="0" y2="6" stroke="#0e0a1d" stroke-width="2" opacity="0.5"/></pattern></defs>`;
  for (const t of ticks) svg += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${fmt(t)}</text>`;
  today.forEach((v, h) => {
    if (h > currentHour) return;
    const x = L + h * bw + 2;
    const fill = h === currentHour ? "#a78bfa" : simulatedBefore != null && h < simulatedBefore ? "url(#hatch)" : "#7c6bd6";
    const hgt = Math.max(v > 0 ? 1.5 : 0, H - B - y(v));
    svg += `<rect x="${x}" y="${H - B - hgt}" width="${bw - 4}" height="${hgt}" rx="2" fill="${fill}"><title>${String(h).padStart(2, "0")}:00 · ${usd(v)}${h === currentHour ? " (this hour)" : ""}</title></rect>`;
  });
  const pts = baseline.map((v, h) => `${L + h * bw + bw / 2},${y(v)}`).join(" ");
  svg += `<polyline points="${pts}" fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="5 4"/>`;
  for (let h = 0; h < 24; h += 3) svg += `<text x="${L + h * bw + bw / 2}" y="${H - 10}" text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`;
  svg += `<line x1="${L + currentHour * bw + bw / 2}" x2="${L + currentHour * bw + bw / 2}" y1="${T}" y2="${H - B}" stroke="#a78bfa" stroke-width="1" opacity="0.5"/>`;
  return svg + "</svg>";
}

/** Horizontal bars: [{name, value, sub}] */
export function hbars(items, { format = usd, color = "var(--chart-1)" } = {}) {
  if (!items.length) return "";
  const max = Math.max(...items.map((i) => i.value), 0.0000001);
  return items
    .map(
      (i) => `<div class="hbar"><span class="name" title="${esc(i.name)}">${esc(i.name)}${i.sub ? `<span class="faint small"> ${esc(i.sub)}</span>` : ""}</span>
      <span class="track" aria-hidden="true"><span class="fill" style="display:block;width:${Math.max(2, (100 * i.value) / max).toFixed(1)}%;background:${i.color || color}"></span></span>
      <span class="num right">${esc(format(i.value))}</span></div>`,
    )
    .join("");
}

/** Quality vs cost scatter for route evals. points: [{name, x: cost, y: quality}] */
export function scatter(points, { xLabel = "Cost per 1K requests (USD)", yLabel = "Quality" } = {}) {
  const W = 560, H = 260, L = 50, R = 20, T = 14, B = 40;
  const xs = points.map((p) => p.x);
  const xmax = niceMax(Math.max(...xs) * 1.1);
  const ymin = Math.max(0, Math.floor(Math.min(...points.map((p) => p.y)) * 10) / 10 - 0.1);
  const x = (v) => L + (W - L - R) * (v / xmax);
  const y = (v) => T + (H - T - B) * (1 - (v - ymin) / (1 - ymin));
  const colors = ["#a78bfa", "#60a5fa", "#4ade80", "#fbbf24", "#f472b6", "#f87171"];
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Route quality against cost">`;
  for (let i = 0; i <= 4; i++) {
    const yv = ymin + ((1 - ymin) * i) / 4;
    svg += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(yv)}" y2="${y(yv)}"/><text x="${L - 6}" y="${y(yv) + 4}" text-anchor="end">${yv.toFixed(2)}</text>`;
    const xv = (xmax * i) / 4;
    svg += `<text x="${x(xv)}" y="${H - B + 16}" text-anchor="middle">$${xv.toFixed(xv < 0.1 ? 3 : 2)}</text>`;
  }
  svg += `<text x="${(L + W - R) / 2}" y="${H - 4}" text-anchor="middle">${esc(xLabel)}</text>`;
  svg += `<text x="12" y="${(T + H - B) / 2}" text-anchor="middle" transform="rotate(-90 12 ${(T + H - B) / 2})">${esc(yLabel)}</text>`;
  points.forEach((p, i) => {
    const c = colors[i % colors.length];
    svg += `<circle cx="${x(p.x)}" cy="${y(p.y)}" r="7" fill="${c}" stroke="#0e0a1d" stroke-width="2"><title>${esc(p.name)}: quality ${p.y}, ${usd(p.x, 4)} per 1K requests</title></circle>`;
    const right = x(p.x) > W - 150;
    svg += `<text x="${x(p.x) + (right ? -11 : 11)}" y="${y(p.y) + 4 + (i % 2 ? 10 : -4)}" text-anchor="${right ? "end" : "start"}" style="fill:${c};font-weight:600">${esc(p.name)}</text>`;
  });
  return svg + "</svg>";
}

/** Two lines over a threshold grid (semantic-cache calibration). */
export function curves(grid, chosen) {
  const W = 560, H = 230, L = 46, R = 16, T = 12, B = 34;
  const xs = grid.map((g) => g.threshold);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const x = (v) => L + (W - L - R) * ((v - xmin) / (xmax - xmin || 1));
  const y = (v) => T + (H - T - B) * (1 - v);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Hit rate and false-hit rate by similarity threshold">`;
  for (const t of [0, 0.25, 0.5, 0.75, 1]) svg += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${Math.round(t * 100)}%</text>`;
  for (let v = Math.ceil(xmin * 10) / 10; v <= xmax + 1e-9; v += 0.1) svg += `<text x="${x(v)}" y="${H - 12}" text-anchor="middle">${v.toFixed(1)}</text>`;
  const line = (key, color, dash = "") =>
    `<polyline points="${grid.map((g) => `${x(g.threshold)},${y(g[key])}`).join(" ")}" fill="none" stroke="${color}" stroke-width="2.2" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`;
  svg += line("hit_rate", "#a78bfa") + line("false_hit_rate", "#f87171", "5 4");
  if (chosen != null) svg += `<line x1="${x(chosen)}" x2="${x(chosen)}" y1="${T}" y2="${H - B}" stroke="#fbbf24" stroke-width="1.5"/><text x="${x(chosen) - 4}" y="${T + 10}" text-anchor="end" style="fill:#fbbf24">chosen ${chosen}</text>`;
  return svg + "</svg>";
}
