"use strict";
/* ============================================================================
   SCM Master — Movements (device-as-a-service). Loads after ordering.js.

   The owner's instruction: "from which storage compartment or part of the
   warehouse is the piece being moved, so we understand the movement and the
   turnaround." The movement log: every move between compartments in a window,
   by pair, with how many and how long they had been in the compartment they
   left, measured from the moves themselves; per compartment the measured flow
   and the measured finished stay next to what the Warehouse tab derives from
   the stock still present; and one serial opened to its own path through the
   chain with the days in each station.

   Every figure comes from /movements and is measured from logged moves. The
   seeded fleet carries no history, and the screen says so: the log fills from
   the Simulation tab, the console's transition button and integrations.
============================================================================ */

ICONS.route = '<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 18h5a3 3 0 0 0 0-6h-3a3 3 0 0 1 0-6h5"/>';
CRUMBS.movements = "Movements";

let MV = { days: 30, view: null, serial: "", path: null };
const MV_WINDOWS = [7, 30, 90, 365];

const mvDays = (v) => v == null ? `<span class="wh-na">n/a</span>` : `${num(Math.round(v))} d`;
const mvMeasured = (title) => `<span class="wh-derived om-measured" title="${esc(title)}">measured</span>`;

function mvPairsHtml(V) {
  if (!V.pairs.length) return `<div class="muted" style="font-size:12.5px">${esc(V.reason || "No move in this window.")}</div>`;
  const rows = V.pairs.map((p) => `<tr>
      <td><div class="mv-pair"><span class="mv-pair__from">${esc(p.from_name)}</span>${icon("arrow", 12)}<span class="mv-pair__to">${esc(p.to_name)}</span></div>
        <div class="wh-note">${p.from_code ? esc(p.from_code) : "outside"} → ${p.to_code ? esc(p.to_code) : "outside"}</div></td>
      <td class="num" style="font-weight:600">${num(p.units)}</td>
      <td class="num">${num(p.per_week)}</td>
      <td class="num">${p.median_days == null ? `<span class="wh-na">n/a</span><div class="wh-note">${esc(p.dwell_reason || "")}</div>` : `<b>${mvDays(p.median_days)}</b><div class="wh-note">mean ${num(p.mean_days)} · p90 ${num(p.p90_days)} · longest ${num(p.max_days)}${p.unknown_units ? ` · ${num(p.unknown_units)} unknown` : ""}</div>`}</td>
    </tr>`).join("");
  return `<table class="tbl mv-pairs"><thead><tr><th>From → to</th><th class="num">Units</th><th class="num">A week</th><th class="num">Days in the compartment they left</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function mvCompartmentsHtml(V) {
  const rows = V.compartments.map((c) => {
    const flow = c.units_out ? `<b>${num(c.out_per_week)}</b> / week ${mvMeasured(c.flow_basis)}` : `<span class="wh-na">none</span>`;
    const derivedFlow = c.derived_units_per_week == null ? `<span class="wh-na">n/a</span>` : `${num(Math.round(c.derived_units_per_week))} / week <span class="wh-derived" title="${esc(c.derived_basis)}">derived</span>`;
    const stay = c.median_days == null ? `<span class="wh-na">n/a</span><div class="wh-note">${esc(c.dwell_reason || "")}</div>`
      : `<b>${mvDays(c.median_days)}</b> median <div class="wh-note">mean ${num(c.mean_days)} d · p90 ${num(c.p90_days)} d · ${num(c.dated_out)} stays${c.unknown_out ? `, ${num(c.unknown_out)} unknown` : ""} · target ${c.target_dwell_days} d</div>`;
    const derivedStay = c.derived_mean_days == null ? `<span class="wh-na">n/a</span>` : `${num(Math.round(c.derived_mean_days))} d mean age <span class="wh-derived" title="${esc(c.derived_basis)}">derived</span>`;
    return `<tr>
      <td><div class="cell-prod__name">${c.step}. ${esc(c.name)}</div><div class="wh-note">${num(c.on_hand)} on hand</div></td>
      <td class="num">${num(c.units_in)}</td>
      <td class="num">${num(c.units_out)}</td>
      <td class="num">${flow}<div class="wh-note">${derivedFlow}</div></td>
      <td>${stay}<div class="wh-note">${derivedStay}</div></td>
    </tr>`;
  }).join("");
  return `<table class="tbl mv-comp"><thead><tr><th>Compartment</th><th class="num">In</th><th class="num">Out</th><th class="num">Flow · measured vs derived</th><th>Finished stay · measured vs derived</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function mvPathHtml(p) {
  const head = `<div class="mv-path__head">
      <div><div class="sim-effect__title"><span class="ref">${esc(p.serial_number)}</span> · ${esc(p.product)}</div>
        <div class="wh-note">${esc(p.family || "")} · ${p.cycle_no === 0 ? "never rented" : `${p.cycle_no} rental${p.cycle_no === 1 ? "" : "s"}`}${p.grade ? ` · grade ${esc(p.grade)}` : ""}</div></div>
      <div class="mv-path__now">${statusPill(p.status)}<div class="wh-note">in <b>${esc(p.station_name)}</b>${p.since ? ` since ${fmtDate(p.since)}, <b>${num(p.days_so_far)} day${p.days_so_far === 1 ? "" : "s"}</b> so far` : ""}</div></div>
    </div>`;
  if (!p.steps.length) return head + `<div class="muted" style="font-size:12.5px;margin-top:10px">${esc(p.history_reason || "No move logged.")}</div>`;
  const steps = p.steps.map((s) => {
    const title = s.kind === "moved" ? "Moved within the warehouse" : `${esc(s.from_name || "On order")} ${icon("arrow", 11)} ${esc(s.to_name)}`;
    const stay = s.kind === "moved" ? "" : s.dwell_days == null ? (s.from_status ? `<span class="wh-na">stay unknown</span>` : "")
      : `<b>${num(s.dwell_days)} days</b> in ${esc(s.from_name)}${s.from_since ? ` (since ${fmtDate(s.from_since)})` : ""}`;
    return `<div class="log__entry"><div class="log__rail"><div class="log__dot" style="background:${s.to_code ? "var(--ts-brand-gold)" : "var(--ts-info)"}"></div><div class="log__line"></div></div>
      <div class="log__body"><div class="log__time">${s.effective_date ? fmtDate(s.effective_date) : "no day logged"}${s.actor ? " · " + esc(s.actor) : ""}</div>
        <div class="attn__title">${title}</div><div class="log__note">${stay}${s.note ? `${stay ? " · " : ""}${esc(s.note)}` : ""}</div></div></div>`;
  }).join("");
  return head + `<div class="log" style="margin-top:12px">${steps}</div>
    <div class="wh-note" style="margin-top:8px"><b>${num(p.warehouse_days_measured)} days</b> in the warehouse across the finished stays logged${p.customer_days_measured ? ` · ${num(p.customer_days_measured)} days at customers` : ""}${p.unknown_stays ? ` · ${num(p.unknown_stays)} stay${p.unknown_stays === 1 ? "" : "s"} of unknown length (entered before the log)` : ""} ${mvMeasured("the sum of the stays on the logged moves; the current stay is still open and not in this figure")}</div>`;
}

async function mvOpenSerial() {
  const host = $("#mv-path");
  const serial = ($("#mv-serial").value || "").trim();
  if (!host || !serial) return;
  MV.serial = serial;
  host.innerHTML = `<div class="muted" style="font-size:12.5px">Reading…</div>`;
  try {
    MV.path = await api(`/movements/serials/${encodeURIComponent(serial)}`);
    host.innerHTML = mvPathHtml(MV.path);
  } catch (e) {
    host.innerHTML = `<div class="muted" style="font-size:12.5px">${esc((e && e.message) || "Could not read")}</div>`;
  }
}

async function mvDraw(screen) {
  const V = await api(`/movements?days=${MV.days}`);
  MV.view = V;
  const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;
  const busiest = V.compartments.slice().sort((a, b) => b.units_out - a.units_out)[0];
  screen.innerHTML = `
    ${pageHead("Warehouse", "Movements", "Which compartment a device left, which it entered, when, and how long it had been there. Measured from the moves themselves, never from the stock still present: the movement log the Warehouse tab, the capacity plan and the device TCO said they needed. Open a serial to see its own way through the chain.")}
    ${V.moves === 0 && !V.last_move ? `<div class="sim-warn"><span class="sim-warn__icon">${icon("alert", 18)}</span><div><b>No move logged yet.</b> ${esc(V.history_note)}. Fire "A day of normal operation" on the Simulation tab and come back: the day's moves appear here with their stays.</div></div>` : ""}
    <div class="stats stats--5">
      ${stat(`Moves, last ${V.days} days`, "route", num(V.moves), V.moves ? `${num(V.pairs_count)} pairs of compartments` : esc(V.reason || ""), "", "stat__val--gold")}
      ${stat("Devices moved", "box", num(V.devices), V.devices ? `${num(Math.round(V.moves / V.devices * 10) / 10)} moves a device` : "")}
      ${stat("Busiest outflow", "layers", busiest && busiest.units_out ? esc(busiest.name) : "—", busiest && busiest.units_out ? `${num(busiest.units_out)} out · ${num(busiest.out_per_week)} a week` : "nothing left a compartment")}
      ${stat("First move logged", "clock", V.first_move ? fmtDate(V.first_move) : "—", V.last_move ? `last ${fmtDate(V.last_move)}` : "the log is empty")}
      ${stat("Without a day", "alert", num(V.undated_events), V.undated_events ? esc(V.undated_reason) : "every logged move carries its day", V.undated_events ? "stat__hint--neg" : "stat__hint--pos")}
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">Between compartments</span><span class="section__count">${V.pairs_count}</span>
        <div class="segmented" id="mv-window" style="margin-left:14px">${MV_WINDOWS.map((d) => `<button class="${d === MV.days ? "active" : ""}" data-days="${d}">${d} d</button>`).join("")}</div>
        <span class="section__hint" title="${esc(V.coverage_basis)}">since ${fmtDate(V.since)} · the log covers ${num(V.covered_days)} of the ${num(V.days)} days, a week's flow is over those · a stay is measured from the day a device entered the compartment to the day it left</span></div>
      <div class="panel" style="padding:${V.pairs.length ? 0 : 14}px 18px">${mvPairsHtml(V)}</div>
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">Per compartment</span><span class="section__count">${V.compartments.length}</span><span class="section__hint">measured from the moves, next to what the Warehouse tab derives from the stock still present</span></div>
      <div class="panel">${mvCompartmentsHtml(V)}</div>
    </div>
    <div class="section" style="margin-bottom:0">
      <div class="section__head"><span class="section__title">One device</span><span class="section__hint">a serial opens to its own path through the chain, with the days in each station</span></div>
      <div class="panel" style="padding:14px 18px">
        <form class="mv-form" id="mv-form"><input class="input" id="mv-serial" placeholder="Serial, e.g. DAAS-0000001" value="${esc(MV.serial)}" /><button type="submit" class="btn btn--ink btn--sm">${icon("route", 13)} Open</button></form>
        <div id="mv-path" style="margin-top:12px">${MV.path ? mvPathHtml(MV.path) : `<div class="muted" style="font-size:12.5px">Type a serial number.</div>`}</div>
      </div>
    </div>`;
  $$("#mv-window button").forEach((b) => b.addEventListener("click", () => { MV.days = Number(b.dataset.days); mvDraw(screen); }));
  $("#mv-form").addEventListener("submit", (e) => { e.preventDefault(); mvOpenSerial(); });
}

RENDER.movements = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `${pageHead("Warehouse", "Movements", "The movement log of the device fleet's warehouse.")}
      <div class="panel"><div class="state"><div class="state__icon">${icon("route", 22)}</div><div class="state__title">No compartments here</div>
      <div class="state__sub">This database holds the datacenter operation; the movement log follows the device fleet's compartments. Every asset's own event log is on the Assets tab.</div></div></div>`;
    return;
  }
  try {
    // A device can be linked to: /?serial=DAAS-0000001&days=90#movements opens its path.
    const q = new URLSearchParams(location.search);
    if (q.has("serial")) MV.serial = q.get("serial");
    if (MV_WINDOWS.includes(Number(q.get("days")))) MV.days = Number(q.get("days"));
    await mvDraw(screen);
    if (MV.serial && !MV.path) mvOpenSerial();
    // Live: a fleet event on the Simulation tab writes moves; one grouped read every 15 seconds while the tab is open.
    livePoll("movements", async () => {
      const V2 = await api(`/movements?days=${MV.days}`);
      if (MV.view && (V2.moves !== MV.view.moves || V2.devices !== MV.view.devices)) mvDraw(screen);
    }, 15000);
  } catch (e) { screen.innerHTML = errState(e.message); }
};
