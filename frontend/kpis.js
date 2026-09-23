"use strict";
/* ============================================================================
   SCM Master — KPIs. Loads after requisitions.js.

   Where we stand, against where we want to be in one, two and three years.
   Every value on this screen is computed live by the backend from the same
   tables the other tabs read (assets, orders, requisitions, contracts,
   costing). Nothing is typed in. A KPI the data cannot measure says why,
   instead of showing a zero.

   Targets are owned: the backend seeds a placeholder (10/20/30 % better than
   today in the KPI's good direction) and marks it as such until a person with
   the PROCUREMENT or ADMIN role sets the real one here. The trend is measured:
   one snapshot per KPI per day, from the first day this tab was opened.
============================================================================ */

ICONS.target = '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>';

CRUMBS.kpis = "KPIs";

const KPI_STATUS = {
  met:            { label: "Target met",       tone: "positive" },
  on_track:       { label: "On track",         tone: "info" },
  behind:         { label: "Behind",           tone: "warning" },
  open:           { label: "Open",             tone: "neutral" },
  not_measurable: { label: "Not measurable",   tone: "mute" },
  no_target:      { label: "No target",        tone: "neutral" },
};

const KPI_GROUP_ORDER = ["fleet", "warehouse", "cost", "process", "suppliers"];
const KPI_GROUP_HINT = {
  fleet:     "The rental cycle: how much comes back, how fast it is ready again, what the second life and the sale bring.",
  warehouse: "The five the role names — availability, capital tied up, cover, aging, write-down risk — plus the flow around them.",
  cost:      "Money on the table and how much of the spend sits under a contract.",
  process:   "How much the system decides on its own, and how fast a person decides the rest.",
  suppliers: "Where a single supplier or an expiring contract is a risk.",
};

let KPI_ROWS = [];
let KPI_EDITING = null;

const kpiCanEdit = () => !!(window.ME && (window.ME.role === "ADMIN" || window.ME.role === "PROCUREMENT"));

function kpiFmt(v, unit) {
  if (v == null) return "—";
  const n = Number(v);
  switch (unit) {
    case "pct":   return n.toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " %";
    case "eur":   return euro(n);
    case "days":  return n.toLocaleString("de-DE", { maximumFractionDigits: 0 }) + " d";
    case "weeks": return n.toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " wk";
    case "hours": return n.toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " h";
    case "turns": return n.toLocaleString("de-DE", { maximumFractionDigits: 2 }) + " ×";
    default:      return n.toLocaleString("de-DE", { maximumFractionDigits: 0 });
  }
}

/* a small line of the daily snapshots; one point means "measured since today" */
function kpiSpark(history, direction) {
  const pts = (history || []).filter((h) => h.value != null);
  if (pts.length < 2) return `<span class="kpi-spark__none">since ${pts.length ? fmtDate(pts[0].as_of) : "today"}</span>`;
  const W = 84, H = 22, P = 2;
  const vals = pts.map((p) => Number(p.value));
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const xy = vals.map((v, i) => [P + (W - 2 * P) * i / (vals.length - 1), P + (H - 2 * P) * (1 - (v - lo) / span)]);
  const d = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const last = xy[xy.length - 1];
  const better = direction === "lower" ? vals[vals.length - 1] <= vals[0] : vals[vals.length - 1] >= vals[0];
  const color = better ? "var(--ts-positive)" : "var(--ts-negative)";
  return `<svg class="kpi-spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2" fill="${color}"/></svg>`;
}

function kpiTargetChip(v, unit, placeholder, active) {
  if (v == null) return `<span class="kpi-target kpi-target--none">—</span>`;
  return `<span class="kpi-target${placeholder ? " kpi-target--placeholder" : ""}${active ? " kpi-target--active" : ""}" title="${placeholder ? "placeholder, derived from today's value; set the real target" : "set by " + esc(arguments[4] || "a person")}">${kpiFmt(v, unit)}</span>`;
}

function kpiStatusCell(r) {
  const s = KPI_STATUS[r.status] || KPI_STATUS.no_target;
  const bar = (r.progress_pct == null || r.status === "open") ? "" :
    `<div class="kpi-progress" title="${r.progress_pct}% of the way to the one-year target"><div class="kpi-progress__fill" style="width:${Math.max(2, r.progress_pct)}%"></div></div>`;
  let gap = "";
  if (r.gap_to_y1 != null && r.status !== "met") {
    const sign = r.gap_to_y1 > 0 ? "+" : "−";
    gap = `<div class="kpi-gap">${sign}${kpiFmt(Math.abs(r.gap_to_y1), r.unit)} to the 1-year target</div>`;
  }
  return `${plainPill(s.label, s.tone)}${bar}${gap}`;
}

function kpiRowHtml(r) {
  const editing = KPI_EDITING === r.id;
  const cur = r.current == null
    ? `<div class="kpi-val kpi-val--na">n/a</div><div class="kpi-reason">${esc(r.reason || "")}</div>`
    : `<div class="kpi-val">${kpiFmt(r.current, r.unit)}</div><div class="kpi-dir">${r.direction === "lower" ? "lower is better" : "higher is better"}</div>`;
  const targets = `
    <div class="kpi-targets">
      ${kpiTargetChip(r.target_y1, r.unit, r.placeholder, true, r.updated_by)}
      ${kpiTargetChip(r.target_y2, r.unit, r.placeholder, false, r.updated_by)}
      ${kpiTargetChip(r.target_y3, r.unit, r.placeholder, false, r.updated_by)}
    </div>
    ${r.placeholder ? `<div class="kpi-placeholder-hint">placeholder targets</div>` : ""}`;
  const owner = r.owner ? `<div class="kpi-owner">${esc(r.owner)}</div>` : `<div class="kpi-owner kpi-owner--none">no owner</div>`;
  const edit = kpiCanEdit()
    ? `<button class="btn btn--ghost btn--sm" data-kpi-edit="${r.id}">${editing ? "Close" : "Set targets"}</button>`
    : "";
  const main = `
    <tr class="${editing ? "is-open" : ""}">
      <td>
        <div class="kpi-name">${esc(r.name)}</div>
        <div class="kpi-def">${esc(r.definition)}</div>
        <div class="kpi-src">${icon("track", 11)} ${esc(r.source)}</div>
      </td>
      <td class="num">${cur}</td>
      <td class="num">${kpiSpark(r.history, r.direction)}</td>
      <td>${targets}</td>
      <td>${kpiStatusCell(r)}</td>
      <td>${owner}</td>
      <td class="num">${edit}</td>
    </tr>`;
  if (!editing) return main;
  const v = (x) => x == null ? "" : x;
  return main + `
    <tr class="kpi-editrow">
      <td colspan="7">
        <form class="kpi-form" data-kpi-form="${r.id}">
          <div class="field"><label class="field__label">In one year</label><input class="input" type="number" step="any" name="target_y1" value="${v(r.target_y1)}"></div>
          <div class="field"><label class="field__label">In two years</label><input class="input" type="number" step="any" name="target_y2" value="${v(r.target_y2)}"></div>
          <div class="field"><label class="field__label">In three years</label><input class="input" type="number" step="any" name="target_y3" value="${v(r.target_y3)}"></div>
          <div class="field kpi-form__wide"><label class="field__label">Owner</label><input class="input" type="text" name="owner" maxlength="128" placeholder="Head of Procurement" value="${esc(r.owner || "")}"></div>
          <div class="field kpi-form__wide"><label class="field__label">Note</label><input class="input" type="text" name="note" placeholder="why this number" value="${esc(r.placeholder ? "" : (r.note || ""))}"></div>
          <div class="kpi-form__actions">
            <span class="kpi-form__hint">Unit: ${esc(r.unit)} · ${r.direction === "lower" ? "lower is better" : "higher is better"}</span>
            <button type="submit" class="btn btn--primary btn--sm">Save targets</button>
          </div>
        </form>
      </td>
    </tr>`;
}

function kpiGroupHtml(g) {
  const rows = KPI_ROWS.filter((r) => r.group === g);
  if (!rows.length) return "";
  const label = rows[0].group_label;
  return `
    <div class="section">
      <div class="section__head">
        <div class="section__title">${esc(label)}</div>
        <span class="section__count">${rows.length}</span>
        <span class="section__hint">${esc(KPI_GROUP_HINT[g] || "")}</span>
      </div>
      <div class="panel">
        <table class="tbl kpi-tbl">
          <thead><tr>
            <th>KPI</th><th class="num">Today</th><th class="num">Trend</th><th>Target · 1 y / 2 y / 3 y</th><th>Status</th><th>Owner</th><th></th>
          </tr></thead>
          <tbody>${rows.map(kpiRowHtml).join("")}</tbody>
        </table>
      </div>
    </div>`;
}

RENDER.kpis = async function (opts) {
  const screen = $("#screen");
  // A KPI is measured once a day (31 reads over 400,000 devices is not a page-load
  // job). The tab shows that measurement; "Measure again" takes a new one.
  const refresh = !!(opts && opts.refresh);
  if (refresh) screen.innerHTML = `<div class="state"><div class="state__title">Measuring</div><div class="state__sub">Reading 31 KPIs over the whole fleet. This takes a moment.</div></div>`;
  KPI_ROWS = await api("/kpis" + (refresh ? "?refresh=true" : ""));
  const n = (s) => KPI_ROWS.filter((r) => r.status === s).length;
  const asOf = KPI_ROWS.length ? fmtDate(KPI_ROWS[0].as_of) : "—";
  const placeholders = KPI_ROWS.filter((r) => r.placeholder).length;
  const stat = (label, val, hint, cls = "") =>
    `<div class="stat"><div class="stat__label">${label}</div><div class="stat__val ${cls}">${val}</div><div class="stat__hint">${hint}</div></div>`;
  screen.innerHTML = `
    ${pageHead("Steering", "KPIs", "Where we stand against where we want to be in one, two and three years. Every number is computed live from this system; a KPI the data cannot measure says why. Targets are owned; placeholders stay marked until a person sets them.")}
    <div class="stats">
      ${stat("Target met", n("met"), "already at the one-year target", "stat__val--gold")}
      ${stat("On track", n("on_track"), "past halfway to the one-year target")}
      ${stat("Behind or open", n("behind") + n("open"), "not at target; open = measured once, no movement yet")}
      ${stat("Not measurable", n("not_measurable"), "the data is missing, the reason is on the row")}
    </div>
    <div class="kpi-meta">
      <span>Measured ${asOf}</span>
      <span>·</span>
      <span>${KPI_ROWS.length} KPIs, ${placeholders} with placeholder targets</span>
      ${kpiCanEdit() ? "" : `<span>·</span><span>Read-only: PROCUREMENT or ADMIN sets targets</span>`}
      <span>·</span>
      <button class="btn btn--ghost btn--sm" id="kpi-refresh" title="A KPI is measured once a day. This measures again now.">Measure again</button>
    </div>
    ${KPI_GROUP_ORDER.map(kpiGroupHtml).join("")}`;

  const refreshBtn = $("#kpi-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => RENDER.kpis({ refresh: true }));
  $$("[data-kpi-edit]").forEach((b) => b.addEventListener("click", () => {
    KPI_EDITING = KPI_EDITING === b.dataset.kpiEdit ? null : b.dataset.kpiEdit;
    RENDER.kpis.rerender();
  }));
  $$("[data-kpi-form]").forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = f.dataset.kpiForm;
    const fd = new FormData(f);
    const numField = (k) => { const s = fd.get(k); return s === "" || s == null ? null : Number(s); };
    try {
      const updated = await api(`/kpis/${id}/target`, { method: "PUT", body: {
        target_y1: numField("target_y1"), target_y2: numField("target_y2"), target_y3: numField("target_y3"),
        owner: fd.get("owner") || null, note: fd.get("note") || null,
      } });
      KPI_ROWS = KPI_ROWS.map((r) => (r.id === id ? updated : r));
      KPI_EDITING = null;
      toast("Targets saved", "ok");
      RENDER.kpis.rerender();
    } catch (err) {
      toast("Could not save: " + ((err && err.message) || "error"), "err");
    }
  }));
};

/* re-draw without re-fetching (after an edit toggle or a save) */
RENDER.kpis.rerender = function () {
  const groups = $$(".section");
  // simplest correct thing: rebuild the grouped tables in place
  const host = groups.length ? groups[0].parentElement : $("#screen");
  groups.forEach((g) => g.remove());
  host.insertAdjacentHTML("beforeend", KPI_GROUP_ORDER.map(kpiGroupHtml).join(""));
  $$("[data-kpi-edit]").forEach((b) => b.addEventListener("click", () => {
    KPI_EDITING = KPI_EDITING === b.dataset.kpiEdit ? null : b.dataset.kpiEdit;
    RENDER.kpis.rerender();
  }));
  $$("[data-kpi-form]").forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = f.dataset.kpiForm;
    const fd = new FormData(f);
    const numField = (k) => { const s = fd.get(k); return s === "" || s == null ? null : Number(s); };
    try {
      const updated = await api(`/kpis/${id}/target`, { method: "PUT", body: {
        target_y1: numField("target_y1"), target_y2: numField("target_y2"), target_y3: numField("target_y3"),
        owner: fd.get("owner") || null, note: fd.get("note") || null,
      } });
      KPI_ROWS = KPI_ROWS.map((r) => (r.id === id ? updated : r));
      KPI_EDITING = null;
      toast("Targets saved", "ok");
      RENDER.kpis.rerender();
    } catch (err) {
      toast("Could not save: " + ((err && err.message) || "error"), "err");
    }
  }));
};
