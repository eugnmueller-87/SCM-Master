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

   A row opens on click to say where its number comes from: how it is
   calculated, which tables it reads, what it excludes or assumes, why it
   matters and what the measurement needs. Those words are served with the
   value (/kpis: basis, calculation, reads, caveats, why, needs) from the same
   registry entry that computes it; this screen renders them and writes none
   of its own, so what it says can never drift from what the code does.
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
let KPI_OPEN = null;      // the row whose explanation is open; survives the live redraw

// The three words the backend uses for how far a number is a fact of the tables
// (services/kpis.py, KpiExplain). The hint is the legend for the word; the words
// about a particular KPI come from the API.
const KPI_BASIS = {
  measured:    { hint: "every input is a row, or a count of rows, in the tables named under 'where the data comes from'" },
  derived:     { hint: "an input is estimated by a rule in code because the data does not record it" },
  placeholder: { hint: "a design parameter with an owner enters the number; it changes when that person sets the real value" },
};

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

/* the row that opens under a KPI: where its number comes from, in the registry's own words,
   plus what this screen knows about the measurement and the target */
function kpiExplainHtml(r) {
  const basis = KPI_BASIS[r.basis] || { hint: "" };
  const at = r.measured_at
    ? new Date(r.measured_at).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })
    : "—";
  // A value measured before the simulation moved the calendar says so here as well as on
  // the value cell: the explanation must not read as if the number were today's.
  const when = r.stale_days > 0
    ? `<span class="kpi-stale">measured ${at}, ${r.stale_days} simulated day${r.stale_days === 1 ? "" : "s"} ago, before the dataset's calendar last moved; Measure again brings it current</span>`
    : `<span>measured ${at}, describing ${fmtDate(r.measured_on)}</span>`;
  // A placeholder row without a value has no seeded targets yet: say so, rather than
  // describing targets that are not there.
  const target = !r.placeholder
    ? `<span>targets set by ${esc(r.updated_by || "a person")}${r.owner ? `, owner ${esc(r.owner)}` : ""}${r.note ? ` (${esc(r.note)})` : ""}</span>`
    : r.target_y1 == null
      ? `<span class="kpi-explain__ph">no targets yet: a placeholder is seeded from the first measured value once there is one, and waits for an owner</span>`
      : `<span class="kpi-explain__ph">placeholder targets, derived from the first measured value (10/20/30 % better), waiting for ${r.owner ? esc(r.owner) : "an owner"} to set the real ones</span>`;
  const block = (label, text, cls = "") =>
    `<div class="kpi-explain__block${cls ? " " + cls : ""}"><div class="kpi-explain__label">${label}</div><div class="kpi-explain__text">${esc(text || "")}</div></div>`;
  // Not measurable: the reason and what would make it measurable, side by side, because
  // "no bill of materials yet" is only useful next to "this needs a BOM per product".
  const needs = r.current == null
    ? `<div class="kpi-explain__block kpi-explain__block--na"><div class="kpi-explain__label">Not measurable, and what would make it so</div><div class="kpi-explain__text"><b>${esc(r.reason || "")}.</b> ${esc(r.needs || "")}</div></div>`
    : block("What the measurement needs", r.needs);
  return `
    <tr class="kpi-explainrow">
      <td colspan="7">
        <div class="kpi-explain__head">
          <span class="kpi-basis kpi-basis--${esc(r.basis)}" title="${esc(basis.hint)}">${esc(r.basis)}</span>
          ${when}
          <span>·</span>
          ${target}
        </div>
        <div class="kpi-explain__grid">
          ${block("How it is calculated", r.calculation)}
          ${block("Where the data comes from", r.reads, "kpi-explain__reads")}
          ${block("What it excludes or assumes", r.caveats)}
          ${block("Why it matters", r.why)}
          ${needs}
        </div>
      </td>
    </tr>`;
}

function kpiRowHtml(r) {
  const editing = KPI_EDITING === r.id;
  const open = KPI_OPEN === r.id;
  // A value measured before the simulation moved the calendar says so in its own row, in plain words,
  // so nobody reads a forecast from before the last simulated days as today's.
  const stale = r.stale_days > 0 ? `<div class="kpi-stale">measured ${r.stale_days} simulated day${r.stale_days === 1 ? "" : "s"} ago</div>` : "";
  const cur = r.current == null
    ? `<div class="kpi-val kpi-val--na">n/a</div><div class="kpi-reason">${esc(r.reason || "")}</div>${stale}`
    : `<div class="kpi-val">${kpiFmt(r.current, r.unit)}</div><div class="kpi-dir">${r.direction === "lower" ? "lower is better" : "higher is better"}</div>${stale}`;
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
  // The row itself opens the explanation (the pattern the Warehouse and TCO tabs use for
  // their rows); the edit form keeps its button, so both can be open at once.
  const main = `
    <tr class="clickable${(editing || open) ? " is-open" : ""}" data-kpi-open="${r.id}" title="${open ? "Close" : "Where this number comes from"}">
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
    </tr>` + (open ? kpiExplainHtml(r) : "");
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
  // A KPI is measured once a day (32 reads over 400,000 devices is not a page-load
  // job). The tab shows that measurement; "Measure again" takes a new one.
  const refresh = !!(opts && opts.refresh);
  if (refresh) screen.innerHTML = `<div class="state"><div class="state__title">Measuring</div><div class="state__sub">Reading 32 KPIs over the whole fleet. This takes a moment.</div></div>`;
  KPI_ROWS = await api("/kpis" + (refresh ? "?refresh=true" : ""));
  // Has the simulation let days pass? One cheap row; the trend then holds one point per simulated day.
  const world = isDaas() ? await api("/simulation/status").then((s) => s.world).catch(() => null) : null;
  const n = (s) => KPI_ROWS.filter((r) => r.status === s).length;
  const asOf = KPI_ROWS.length ? fmtDate(KPI_ROWS[0].as_of) : "—";
  const placeholders = KPI_ROWS.filter((r) => r.placeholder).length;
  const staleN = KPI_ROWS.filter((r) => r.stale_days > 0).length;
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
      <span id="kpi-measured">Measured ${asOf}${kpiLastMeasured()}</span>
      ${world && world.days_advanced ? `<span>·</span><span title="The simulation moved every date in the dataset back by this many days; the trend holds one point per simulated day, each measured on the state of that day.">The dataset stands ${num(world.days_advanced)} day${world.days_advanced === 1 ? "" : "s"} later than its seed (simulation)</span>` : ""}
      <span>·</span>
      <span>${KPI_ROWS.length} KPIs, ${placeholders} with placeholder targets</span>
      ${staleN ? `<span>·</span><span class="kpi-stale">${staleN} measured before the last simulated days; Measure again brings them current</span>` : ""}
      ${kpiCanEdit() ? "" : `<span>·</span><span>Read-only: PROCUREMENT or ADMIN sets targets</span>`}
      <span>·</span>
      <button class="btn btn--ghost btn--sm" id="kpi-refresh" title="A KPI is measured once a day. This measures again now.">Measure again</button>
    </div>
    ${KPI_GROUP_ORDER.map(kpiGroupHtml).join("")}`;

  const refreshBtn = $("#kpi-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => RENDER.kpis({ refresh: true }));
  kpiBindRows();
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

  // Live: a fleet event on the Simulation tab measures the KPIs it moved again, and this
  // tab picks the new measurement up while it stays open. One cheap read every 15 seconds
  // (the day's snapshot, nothing is measured), a redraw only when a measurement changed,
  // and never while a target form is open.
  livePoll("kpis", async () => {
    if (KPI_EDITING) return;
    const rows = await api("/kpis");
    if (kpiSig(rows) !== kpiSig(KPI_ROWS)) RENDER.kpis();
  }, 15000);
};

/* when the latest measurement was taken; every row carries its own time */
function kpiLastMeasured() {
  const ts = KPI_ROWS.map((r) => r.measured_at).filter(Boolean).sort();
  if (!ts.length) return "";
  return ` · last measurement ${new Date(ts[ts.length - 1]).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })} · refreshed while this tab is open`;
}
const kpiSig = (rows) => (rows || []).map((r) => `${r.id}:${r.measured_at}:${r.current}`).join("|");

/* a row opens on click; the Set targets button sits inside that row, so its click must
   not bubble up and toggle the explanation as well */
function kpiBindRows() {
  $$("[data-kpi-open]").forEach((tr) => tr.addEventListener("click", () => {
    KPI_OPEN = KPI_OPEN === tr.dataset.kpiOpen ? null : tr.dataset.kpiOpen;
    RENDER.kpis.rerender();
  }));
  $$("[data-kpi-edit]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    KPI_EDITING = KPI_EDITING === b.dataset.kpiEdit ? null : b.dataset.kpiEdit;
    RENDER.kpis.rerender();
  }));
}

/* re-draw without re-fetching (after an edit toggle or a save) */
RENDER.kpis.rerender = function () {
  const groups = $$(".section");
  // simplest correct thing: rebuild the grouped tables in place
  const host = groups.length ? groups[0].parentElement : $("#screen");
  groups.forEach((g) => g.remove());
  host.insertAdjacentHTML("beforeend", KPI_GROUP_ORDER.map(kpiGroupHtml).join(""));
  kpiBindRows();
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
