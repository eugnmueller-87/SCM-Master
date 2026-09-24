"use strict";
/* ============================================================================
   SCM Master — Capacity plan (device-as-a-service). Loads after warehouse.js.

   The owner's question: what capacity do we assume per compartment, how fast
   must each turn to reach the fleet he wants, and when do we have to lease
   more room. The Warehouse tab derives a flow from stock and dwell; this tab
   runs Little's law the other way and shows every step: the fleet on the
   path, the returns it sends back, the share each compartment sees, the
   stock that flow needs at today's dwell, and the month it no longer fits.
   Two levers per compartment, neither picked for the planner: the dwell it
   would have to reach, and the extra places needed at today's dwell.

   Targets are the owner's and are edited here (PROCUREMENT or ADMIN), the
   station capacities too (WAREHOUSE or ADMIN). Every figure comes from
   /capacity-plan and is computed in the database; the dwell is derived and
   says so, and a figure without data says why instead of showing a zero.
============================================================================ */

CRUMBS.plan = "Capacity plan";

let CP = null;          // the plan as served
let CP_MS = 0;          // which milestone the table shows
let CP_EDIT = null;     // "ms:<date>" or "cap:<code>" while a form is open

const cpCanEditTargets = () => !!(window.ME && (window.ME.role === "ADMIN" || window.ME.role === "PROCUREMENT"));
const cpCanEditCapacity = () => !!(window.ME && (window.ME.role === "ADMIN" || window.ME.role === "WAREHOUSE"));
const cpNum1 = (v) => v == null ? "—" : Number(v).toLocaleString("de-DE", { maximumFractionDigits: 1 });
const cpDays = (v) => v == null ? "—" : cpNum1(v) + " d";
const cpPct = (v) => v == null ? "—" : cpNum1(v * 100) + " %";
const cpDate = (iso) => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : "—";
const CP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const cpMonth = (ym) => ym ? `${CP_MONTHS[Number(ym.slice(5, 7)) - 1]} ${ym.slice(0, 4)}` : "—";
const CP_STATE = {
  over_today:  { label: "Over capacity today", tone: "negative" },
  now:         { label: "Breaks now",          tone: "negative" },
  later:       { label: "Breaks",              tone: "warning" },
  fits:        { label: "Fits",                tone: "positive" },
  no_capacity: { label: "No capacity",         tone: "mute" },
  unknown:     { label: "No data",             tone: "mute" },
};

/* when a compartment breaks, as a label, a tone and one line of why */
function cpWhen(c) {
  const s = CP_STATE[c.breach_state] || CP_STATE.unknown;
  if (c.breach_state === "over_today") return { ...s, sub: `${num(c.on_hand)} in a ${num(c.capacity)}-place station${c.breach_month && !c.breach_now ? ` · model: ${cpMonth(c.breach_month)}` : ""}` };
  if (c.breach_state === "later") return { ...s, label: cpMonth(c.breach_month), sub: `at about ${num(c.breach_fleet)} devices at customers` };
  return { ...s, sub: c.breach_reason || "" };
}

/* the today card and one card per milestone on the path */
function cpTodayCard() {
  const m = CP.model;
  const row = (k, v) => `<div class="cp-ms__row"><span class="muted">${k}</span><b>${v}</b></div>`;
  return `<div class="cp-ms cp-ms--today">
    <div class="cp-ms__date">Today · ${cpDate(CP.as_of)}</div>
    <div class="cp-ms__n">${num(CP.fleet_now)}</div>
    <div class="cp-ms__owner">devices at customers, measured</div>
    <div class="cp-ms__rows">
      ${row("Returns", m.returns_per_month_now == null ? `<span class="wh-na">n/a</span>` : `${num(m.returns_per_month_now)} / month`)}
      ${row("New devices", m.first_rentals_per_month_measured == null ? `<span class="wh-na">n/a</span>` : `${num(m.first_rentals_per_month_measured)} / month`)}
      ${row("Mean term", m.term_months == null ? `<span class="wh-na">n/a</span>` : `${cpNum1(m.term_months)} months`)}
      ${row("Second rentals", cpPct(m.cycle2_share))}
    </div>
    <div class="wh-note">${m.first_rentals_per_month_measured == null ? esc(m.first_rentals_reason || "") : `new devices: first rentals started, mean of the last ${m.first_rentals_months} full months`}</div>
  </div>`;
}

function cpMilestoneCard(m, i) {
  const editing = CP_EDIT === `ms:${m.date}`;
  const row = (k, v) => `<div class="cp-ms__row"><span class="muted">${k}</span><b>${v}</b></div>`;
  const na = `<span class="wh-na">n/a</span>`;
  const form = !editing ? "" : `<form class="cp-form" data-cp-ms="${esc(m.date)}">
      <div class="field cp-form__wide"><label class="field__label">Devices at customers on ${cpDate(m.date)}</label><input class="input" name="target_fleet" type="number" min="0" step="1000" value="${m.target_fleet}" required /></div>
      <div class="field"><label class="field__label">Owner</label><input class="input" name="owner" value="${esc(m.owner || "")}" placeholder="left empty, the owner stays" /></div>
      <div class="field"><label class="field__label">Note</label><input class="input" name="note" value="${esc(m.note || "")}" /></div>
      <div class="cp-form__actions"><span class="kpi-form__hint">A request, not a deploy: the whole plan recomputes.</span>
        <span><button type="button" class="btn btn--ghost btn--sm" data-cp-cancel>Cancel</button> <button type="submit" class="btn btn--primary btn--sm">Save</button></span></div>
    </form>`;
  return `<div class="cp-ms${i === CP_MS ? " cp-ms--active" : ""}" data-ms="${i}" title="Show the compartments at this milestone">
    <div class="cp-ms__date">${cpDate(m.date)} · ${cpNum1(m.months_from_today)} months out</div>
    <div class="cp-ms__n">${num(m.target_fleet)}</div>
    <div class="cp-ms__owner"><span>${esc(m.owner || "no owner")}</span>${m.placeholder ? `<span class="kpi-placeholder-hint">placeholder</span>` : ""}${cpCanEditTargets() && !editing ? `<button class="btn btn--ghost btn--sm" data-cp-edit="ms:${esc(m.date)}">Edit</button>` : ""}</div>
    <div class="cp-ms__rows">
      ${row("Growth", `${num(m.growth_per_month)} / month`)}
      ${row("Returns", m.returns_per_month == null ? na : `${num(m.returns_per_month)} / month`)}
      ${row("New devices", m.placements_per_month == null ? na : `${num(m.placements_per_month)} / month`)}
      ${row("Second rentals", m.second_rentals_per_month == null ? na : `${num(m.second_rentals_per_month)} / month`)}
      ${row("Extra places", m.extra_places_total == null ? na : `<span style="color:${m.extra_places_total > 0 ? "var(--ts-negative)" : "var(--ts-positive)"}">${num(m.extra_places_total)}</span>`)}
    </div>
    ${m.note ? `<div class="wh-note">${esc(m.note)}</div>` : ""}
    ${form}
  </div>`;
}

/* when it breaks: one track per compartment across the path, filled to the breach month */
function cpTimeline() {
  const P = CP.path;
  if (P.length < 2) return "";
  const n = P.length - 1;
  const at = (iso) => { const i = P.findIndex((p) => p.date === iso); return i < 0 ? null : i / n; };
  const marks = CP.milestones.map((m) => `<div class="cp-track__mark" style="left:${(at(m.date) || 0) * 100}%"></div>`).join("");
  const ticks = [{ x: 0, t: cpDate(CP.as_of) }].concat(CP.milestones.map((m) => ({ x: at(m.date) || 0, t: `${cpDate(m.date)} · ${num(m.target_fleet)}` })));
  const axis = `<div class="cp-axis"><span>Compartment</span><div class="cp-axis__ticks">${ticks.map((k) => `<span class="cp-axis__tick" style="left:${k.x * 100}%">${esc(k.t)}</span>`).join("")}</div><span style="text-align:right">Breaks</span></div>`;
  const rows = CP.compartments.map((c) => {
    const w = cpWhen(c);
    const t = TONE[w.tone];
    let fill = "";
    if (c.breach_state === "over_today" || c.breach_state === "now") fill = `<div class="cp-track__fill" style="width:100%;background:${t.dot};opacity:.9"></div>`;
    else if (c.breach_state === "later") { const x = at(c.breach_date); fill = `<div class="cp-track__fill" style="width:${(x == null ? 1 : x) * 100}%;background:var(--ts-positive-wash)"></div><div class="cp-track__fill" style="left:${(x == null ? 1 : x) * 100}%;right:0;background:${t.dot};opacity:.85"></div>`; }
    else if (c.breach_state === "fits") fill = `<div class="cp-track__fill" style="width:100%;background:var(--ts-positive-wash)"></div>`;
    return `<div class="cp-tl"><span class="cp-tl__name">${c.step}. ${esc(c.name)}</span><div class="cp-track">${fill}${marks}</div><span class="cp-tl__when" style="color:${t.fg}">${esc(w.label)}</span></div>`;
  }).join("");
  return `<div class="cp-timeline">${axis}${rows}</div>`;
}

/* one row of the compartment table at the selected milestone */
function cpRow(r, c) {
  // The flow this compartment sees, as the sub-line of its name: the share of returns under the
  // next-step rule, or what stands in for it. The blurb of what the compartment holds is on the
  // Warehouse tab and in the hover title here; eight columns and a wrapped blurb did not fit.
  const flow = c.code === "ST-SWAP"
    ? `reserve · ${cpPct(CP.model.swap_ratio)} of the fleet, today's ratio`
    : c.code === "ST-NEW"
      ? "growth + replacements: net growth on the path plus the returns that leave for good"
      : `${cpPct(c.share_of_returns)} of returns · next-step rule over the measured mix`;
  const thr = r.throughput_per_month == null
    ? `<div class="wh-na">n/a</div><div class="wh-note">${esc(r.throughput_reason || "")}</div>`
    : `<div><b>${num(r.throughput_per_month)}</b> / month <span class="wh-derived" title="${esc(c.flow_basis)}">derived</span></div>`;
  const dwell = c.dwell_days == null
    ? `<div class="wh-na">n/a</div><div class="wh-note">${esc(c.dwell_reason || "")}</div>`
    : `<div><b>${cpDays(c.dwell_days)}</b> mean <span class="wh-derived" title="${esc(CP.model.dwell_basis)}">derived</span></div><div class="wh-note">median ${cpDays(c.dwell_median_days)} · on hand ${num(c.on_hand)}${c.required_now != null ? ` · model needs today ${num(c.required_now)}` : ""}</div>`;
  const editing = CP_EDIT === `cap:${c.code}`;
  const capNote = c.capacity == null
    ? esc(c.capacity_reason || "no capacity")
    : c.capacity_placeholder
      ? `assumed: seed design parameter, owner ${esc(c.capacity_owner)}`
      : `set by ${esc(c.capacity_set_by)}${c.capacity_set_on ? ` on ${cpDate(c.capacity_set_on)}` : ""}`;
  const capForm = !editing ? "" : `<form class="cp-capform" data-cp-cap="${esc(c.code)}">
      <input class="input" name="capacity" type="number" min="0" step="100" value="${c.capacity == null ? "" : c.capacity}" required style="width:120px;padding:5px 8px" />
      <button type="submit" class="btn btn--primary btn--sm">Save</button> <button type="button" class="btn btn--ghost btn--sm" data-cp-cancel>Cancel</button></form>`;
  const u = (r.required_stock != null && c.capacity) ? r.required_stock / c.capacity : null;
  const tone = u == null ? "var(--ts-line-strong)" : capTone(u, u > 1);
  const stock = r.required_stock == null
    ? `<div class="wh-na">n/a</div><div class="wh-note">${esc(r.reason || "")}</div>`
    : `<div style="display:flex;align-items:center;gap:12px;justify-content:flex-end"><div class="cap-bar"><div class="cap-bar__fill" style="width:${Math.min(u == null ? 0 : u, 1) * 100}%;background:${tone}"></div></div><span class="cap-util" style="color:${tone}">${u == null ? "—" : Math.round(u * 100) + "%"}</span></div>
       <div class="wh-note"><b>${num(r.required_stock)}</b> needed of ${c.capacity == null ? "—" : num(c.capacity)}${r.gap == null ? "" : (r.gap < 0 ? ` · <span class="wh-note--warn">${num(-r.gap)} short</span>` : ` · ${num(r.gap)} spare`)}</div>`;
  const capCell = `${stock}<div class="wh-note">${capNote}${cpCanEditCapacity() && !editing ? ` <button class="btn btn--ghost btn--sm" data-cp-edit="cap:${esc(c.code)}">Edit</button>` : ""}</div>${capForm}`;
  const lever1 = r.required_dwell_days == null
    ? `<div class="cp-lever cp-lever--none">n/a</div><div class="wh-note">${esc(r.required_dwell_reason || "")}</div>`
    : `<div class="cp-lever${r.fits === false ? " cp-lever--hot" : ""}">${cpDays(r.required_dwell_days)}</div><div class="wh-note">${c.dwell_days == null ? "no dwell today to compare" : (r.required_dwell_days < c.dwell_days ? `from ${cpDays(c.dwell_days)} today` : `today's ${cpDays(c.dwell_days)} already fits`)}</div>`;
  const lever2 = r.extra_places == null
    ? `<div class="cp-lever cp-lever--none">n/a</div><div class="wh-note">${esc(r.reason || (c.capacity == null ? c.capacity_reason : "") || "")}</div>`
    : r.extra_places > 0
      ? `<div class="cp-lever cp-lever--hot">${num(r.extra_places)}</div><div class="wh-note">at today's dwell</div>`
      : `<div class="cp-lever cp-lever--ok">0</div><div class="wh-note">fits at today's dwell</div>`;
  const w = cpWhen(c);
  return `<tr class="cp-row" data-code="${esc(c.code)}" title="${esc(c.holds)}">
    <td><div class="cell-prod"><span class="cell-prod__icon">${icon("layers", 15)}</span><div><div class="cell-prod__name">${c.step}. ${esc(c.name)}</div><div class="cell-prod__cat">${esc(flow)}</div></div></div></td>
    <td class="num">${thr}</td>
    <td>${dwell}</td>
    <td class="num" style="width:230px">${capCell}</td>
    <td class="num">${lever1}</td>
    <td class="num">${lever2}</td>
    <td>${plainPill(w.label, w.tone)}<div class="wh-note">${esc(w.sub)}</div></td>
  </tr>`;
}

function cpDraw(screen) {
  const P = CP;
  if (P.scenario !== "daas" || !P.model) { screen.innerHTML = errState(P.reason || "No capacity plan in this database."); return; }
  const M = P.milestones;
  if (CP_MS >= M.length) CP_MS = 0;
  const m = P.model;
  const fb = P.first_breach;
  const C = P.compartments;
  const byCode = Object.fromEntries(C.map((c) => [c.code, c]));
  const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;
  const ms1 = M[0], ms2 = M[M.length - 1];
  const fbCard = !fb
    ? stat("First to break", "alert", "—", esc(P.reason || "every compartment fits through the last milestone at today's dwell"), "stat__hint--pos")
    : stat("First to break", "alert", esc(fb.name), fb.over_capacity_today
        ? `over capacity today · ${num(byCode[fb.code].on_hand)} in ${num(byCode[fb.code].capacity)} places`
        : (fb.breach_now ? "breaks now: the model at today's fleet already exceeds the capacity" : `${cpMonth(fb.month)} · at about ${num(byCode[fb.code].breach_fleet)} devices at customers`), "stat__hint--neg");
  const fleetHint = M.length ? M.map((x) => `${num(x.target_fleet)} by ${cpDate(x.date)}`).join(" → ") : esc(P.reason || "no milestone");
  const returnsVal = m.returns_per_month_now == null ? "—" : num(m.returns_per_month_now);
  const returnsHint = m.term_months == null ? esc(m.term_reason || "") : `mean term ${cpNum1(m.term_months)} months over ${num(m.term_contracts)} running contracts${ms1 && ms1.returns_per_month != null ? ` · ${num(ms1.returns_per_month)} at ${num(ms1.target_fleet)}` : ""}${ms2 && ms2 !== ms1 && ms2.returns_per_month != null ? ` · ${num(ms2.returns_per_month)} at ${num(ms2.target_fleet)}` : ""}`;
  const placeVal = ms1 && ms1.placements_per_month != null ? num(ms1.placements_per_month) : "—";
  const placeHint = ms1 ? `new devices a month to reach ${num(ms1.target_fleet)} by ${cpDate(ms1.date)} · measured today ${m.first_rentals_per_month_measured == null ? "n/a" : num(m.first_rentals_per_month_measured)}` : "no milestone";
  const extraVal = ms1 && ms1.extra_places_total != null ? num(ms1.extra_places_total) : "—";
  const extraHint = ms1 ? `at today's dwell by ${cpDate(ms1.date)}${ms2 && ms2 !== ms1 && ms2.extra_places_total != null ? ` · ${num(ms2.extra_places_total)} by ${cpDate(ms2.date)}` : ""} · or the dwell lever` : "";
  const sel = M[CP_MS];
  const shares = m.next_step_share || {};
  const rule = (k) => shares[k] ? Object.entries(shares[k]).map(([s, v]) => `${s.replace("_", " ")} ${cpPct(v)}`).join(", ") : "";

  screen.innerHTML = `
    ${pageHead("Warehouse", "Capacity plan", "How fast each compartment must turn for the fleet the owner wants, and when it runs out of room. Little's law inverted: a target fleet implies a return flow, the flow at today's dwell implies a stock, and a stock above the capacity is the month a lease has to be signed. Two levers per compartment, the planner picks.")}
    <div class="stats stats--5">
      ${fbCard}
      ${stat("Devices at customers", "box", num(P.fleet_now), fleetHint)}
      ${stat("Returns a month", "return", returnsVal, returnsHint)}
      ${stat("New devices a month", "truck", placeVal, placeHint)}
      ${stat("Extra places", "layers", extraVal, extraHint, ms1 && ms1.extra_places_total > 0 ? "stat__hint--neg" : "")}
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">The path</span><span class="section__count">${M.length}</span><span class="section__hint">the fleet is interpolated month by month between the milestones · targets are owned, click a milestone to see its compartments</span></div>
      <div class="panel cp-path">${cpTodayCard()}${M.map((x, i) => `<div class="wh-step__arrow">${icon("arrow", 14)}</div>${cpMilestoneCard(x, i)}`).join("")}</div>
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">When it breaks</span><span class="section__count">${C.filter((c) => c.breach_state === "over_today" || c.breach_state === "now" || c.breach_state === "later").length}</span><span class="section__hint">the first month a compartment's required stock crosses its capacity at today's dwell · the lead time for a lease or a shift plan</span></div>
      <div class="panel">${cpTimeline() || `<div class="state"><div class="state__sub">${esc(P.reason || "No milestone ahead of today.")}</div></div>`}</div>
    </div>
    <div class="section" style="margin-bottom:0">
      <div class="section__head"><span class="section__title">Compartments</span><span class="section__count">${C.length}</span>
        ${M.length ? `<div class="segmented" id="cp-ms" style="margin-left:14px">${M.map((x, i) => `<button class="${i === CP_MS ? "active" : ""}" data-ms="${i}">${cpDate(x.date)} · ${num(x.target_fleet)}</button>`).join("")}</div>` : ""}
        <span class="section__hint">${sel ? `${num(sel.compartments_short)} of ${C.length} short at ${num(sel.target_fleet)} devices · required stock = throughput x today's mean dwell` : ""}</span></div>
      <div class="panel" style="overflow-x:auto"><table class="tbl cp-tbl">
        <thead><tr><th>Compartment · flow</th><th class="num">Required throughput</th><th>Dwell today</th><th class="num">Required stock vs capacity</th><th class="num">Lever 1 · dwell to fit</th><th class="num">Lever 2 · extra places</th><th>Breaks</th></tr></thead>
        <tbody>${sel ? sel.rows.map((r) => cpRow(r, byCode[r.code])).join("") : `<tr><td colspan="7"><div class="state"><div class="state__sub">${esc(P.reason || "No milestone ahead of today.")}</div></div></td></tr>`}</tbody>
      </table></div>
      <div class="wh-foot">
        <b>The model.</b> A fleet of F devices with a mean rental term of ${m.term_months == null ? "T" : cpNum1(m.term_months)} months (measured over ${num(m.term_contracts)} running contracts) returns F / T a month.
        Every return walks the chain; the split is the fleet's next-step rule (after a first rental: ${rule("1")}; after a second: ${rule("2")}) over the measured mix of ${cpPct(m.cycle2_share)} second rentals, so ${cpPct(m.exit_share)} of returns leave for good and have to be replaced by new devices${m.exit_share_measured_12m != null ? ` (measured over the last twelve months: ${cpPct(m.exit_share_measured_12m)}, ${num(m.gone_12m)} sold or recycled against ${num(m.returns_12m)} contracts ended)` : ` (${esc(m.exit_share_measured_reason || "")})`}.
        The swap buffer is a reserve, not a queue: it is scaled with the fleet at today's ${cpPct(m.swap_ratio)}.
        <b>The dwell is derived</b>: ${esc(m.dwell_basis)}. Every row therefore shows what the model says today's fleet needs next to what is on hand.
        Capacities marked assumed are the seed's design parameters until ${esc((C[0] || {}).capacity_owner || "the owner")} sets them.
      </div>
    </div>`;

  const redraw = () => cpDraw(screen);
  $$("#cp-ms button").forEach((b) => b.addEventListener("click", () => { CP_MS = Number(b.dataset.ms); CP_EDIT = null; redraw(); }));
  $$("#screen .cp-ms[data-ms]").forEach((card) => card.addEventListener("click", (e) => {
    if (e.target.closest("button, input, form")) return;
    CP_MS = Number(card.dataset.ms); redraw();
  }));
  $$("[data-cp-edit]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); CP_EDIT = CP_EDIT === b.dataset.cpEdit ? null : b.dataset.cpEdit; redraw(); }));
  $$("[data-cp-cancel]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); CP_EDIT = null; redraw(); }));
  $$("[data-cp-ms]").forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    try {
      CP = await api(`/capacity-plan/milestones/${encodeURIComponent(f.dataset.cpMs)}`, { method: "PUT", body: {
        target_fleet: Number(fd.get("target_fleet")), owner: fd.get("owner") || null, note: fd.get("note") || null,
      } });
      CP_EDIT = null;
      toast("Milestone saved, plan recomputed", "ok");
      redraw();
    } catch (err) {
      toast("Could not save: " + ((err && err.message) || "error"), "err");
    }
  }));
  $$("[data-cp-cap]").forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    try {
      CP = await api(`/capacity-plan/compartments/${encodeURIComponent(f.dataset.cpCap)}/capacity`, { method: "PUT", body: { capacity: Number(fd.get("capacity")) } });
      CP_EDIT = null;
      toast("Capacity saved, plan recomputed", "ok");
      redraw();
    } catch (err) {
      toast("Could not save: " + ((err && err.message) || "error"), "err");
    }
  }));
}

RENDER.plan = async function () {
  // The datacenter operation has no compartments to plan; it keeps its location capacity view.
  if (!isDaas()) return RENDER.capacity();
  const screen = $("#screen");
  try {
    CP = await api("/capacity-plan");
    CP_EDIT = null;
    cpDraw(screen);
  } catch (e) { screen.innerHTML = errState(e.message); }
};
