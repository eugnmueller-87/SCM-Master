"use strict";
/* ============================================================================
   SCM Master — Device TCO (device-as-a-service). Loads after warehouse.js.

   What a device costs over its life, per month of service, and how much the
   second life gives back. Every figure comes from /tco/devices and is computed
   in the database from every serial. Quantities are measured (order lines,
   rental contracts, service events, compartments, sold serials); rates are
   placeholders with an owner and are marked as such on every row. Two
   populations: the finished lives (sold or recycled: the whole-life number a
   rental price has to cover) and the whole fleet to date. A layer the data
   cannot support says why instead of showing a zero.

   In the datacenter scenario the tab is not in the nav; the per-asset TCO
   stays on the analytics cockpit.
============================================================================ */

ICONS.tco = '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>';
CRUMBS.tco = "Device TCO";

const TCO_TONE = {
  acquisition: "var(--ts-brand-gold)", inbound: "var(--ts-info)", enrolment: "var(--ts-info-soft)", software: "var(--ts-positive)",
  support: "var(--ts-warning)", service: "var(--ts-negative)", warehouse: "var(--ts-line-strong)", eol: "var(--ts-ink-mute)",
};
const TCO_COHORTS = ["finished", "fleet"];

let TCO = null;               // the last /tco/devices answer
let TCO_COHORT = "finished";  // which population the screen shows
let TCO_PICK = null;          // "class:<key>" or "model:<key>" whose layers are open; null = the portfolio

const tcoEur = (v, d = 2) => v == null ? "—" : "€" + Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
const tcoPct = (v) => v == null ? "—" : Math.round(v * 100) + " %";
const tcoNum1 = (v) => v == null ? "—" : Number(v).toLocaleString("de-DE", { maximumFractionDigits: 1 });
const tcoRate = (id) => (TCO.rates || []).find((r) => r.id === id) || null;

function tcoPicked(C) {
  if (!TCO_PICK) return C.portfolio;
  const [kind, key] = TCO_PICK.split(/:(.*)/s);
  const list = kind === "class" ? C.classes : C.models;
  return list.find((g) => g.key === key) || C.portfolio;
}

/* the cost layers per device as one bar; the resale credit is not a cost and is shown next to it */
function tcoBar(g) {
  const gross = g.per_device && g.per_device.gross;
  if (!gross) return `<span class="wh-na">n/a</span>`;
  const segs = g.layers.filter((l) => l.id !== "eol" && l.per_device != null && l.per_device > 0)
    .map((l) => `<div class="tco-bar__seg" style="width:${l.per_device / gross * 100}%;background:${TCO_TONE[l.id]}" title="${esc(l.label)}: ${tcoEur(l.per_device)} per device"></div>`).join("");
  return `<div class="tco-bar">${segs}</div>`;
}

function tcoNa(reason) {
  return `<div class="wh-na">n/a</div><div class="wh-note">${esc(reason || "")}</div>`;
}

function tcoClassRow(g) {
  const sel = TCO_PICK === `class:${g.key}`;
  if (!g.devices) {
    return `<tr class="tco-row"><td><div class="cell-prod"><span class="cell-prod__icon">${icon("box", 15)}</span><div><div class="cell-prod__name">${esc(g.label)}</div></div></div></td>
      <td colspan="5"><span class="wh-na">n/a</span> <span class="wh-note">${esc(g.reason || "")}</span></td></tr>`;
  }
  const counts = `${num(g.rented)} rented · ${num(g.on_hand)} on hand · ${num(g.sold)} sold · ${num(g.recycled)} recycled`;
  const months = g.months_per_device == null ? tcoNa(g.per_month_reason)
    : `<div><b>${tcoNum1(g.months_per_device)}</b> months per device</div><div class="wh-note">${num(Math.round(g.device_months))} device-months · ${tcoPct(g.second_life_share_of_months)} on a second rental</div>`;
  const per = g.per_device.gross == null ? tcoNa(g.reason)
    : `<div><b>${tcoEur(g.per_device.gross, 0)}</b></div><div class="wh-note">${tcoEur(g.per_device.net, 0)} net of resale</div>`;
  const r = g.resale;
  const credit = r.credit_share_of_acquisition == null ? tcoNa(r.reason)
    : `<div><b>${tcoPct(r.credit_share_of_acquisition)}</b> of acquisition</div><div class="wh-note">${tcoEur(r.proceeds / r.sold_priced, 0)} per sold device · ${num(r.sold_priced)} sold</div>`;
  const perMonth = g.per_month.net == null ? tcoNa(g.per_month_reason)
    : `<div class="tco-big">${tcoEur(g.per_month.net)}</div><div class="wh-note">${tcoEur(g.per_month.gross)} before resale</div>`;
  return `<tr class="clickable tco-row${sel ? " is-open" : ""}" data-pick="class:${esc(g.key)}">
    <td><div class="cell-prod"><span class="cell-prod__icon">${icon("box", 15)}</span><div><div class="cell-prod__name">${esc(g.label)}</div><div class="cell-prod__cat">${num(g.devices)} devices · ${counts}</div></div></div></td>
    <td>${months}</td>
    <td class="num">${per}</td>
    <td>${tcoBar(g)}</td>
    <td>${credit}</td>
    <td class="num">${perMonth}</td>
  </tr>`;
}

function tcoModelRow(g) {
  const sel = TCO_PICK === `model:${g.key}`;
  return `<tr class="clickable tco-row${sel ? " is-open" : ""}" data-pick="model:${esc(g.key)}">
    <td><div class="cell-prod__name">${esc(g.label)}</div><div class="cell-prod__cat">${esc(g.product_code || "")}</div></td>
    <td class="muted">${esc(g.family || "—")}</td>
    <td class="num">${num(g.devices)}<div class="wh-note">${num(g.contracts)} rentals</div></td>
    <td class="num">${g.months_per_device == null ? `<span class="wh-na">n/a</span>` : tcoNum1(g.months_per_device)}</td>
    <td class="num">${tcoEur(g.per_device.gross, 0)}</td>
    <td class="num">${g.resale.credit_share_of_acquisition == null ? `<span class="wh-na" title="${esc(g.resale.reason || "")}">n/a</span>` : tcoPct(g.resale.credit_share_of_acquisition)}</td>
    <td class="num">${g.per_month.net == null ? `<span class="wh-na" title="${esc(g.per_month_reason || "")}">n/a</span>` : `<b>${tcoEur(g.per_month.net)}</b>`}</td>
  </tr>`;
}

/* one component of a layer: where the number comes from, the quantity, the rate, the total */
function tcoPartRow(c, layerReason, mixed) {
  // A rate that differs by device class is a class average once the group spans classes,
  // and a component does not repeat a reason its whole layer already gives.
  const rate = tcoRate(c.rate_id);
  let basis, rateCell;
  if (c.basis === "measured") {
    basis = `<span class="tco-measured">measured</span>`;
    rateCell = c.rate == null ? "—" : `<span class="muted">${tcoEur(c.rate)} average</span>`;
  } else {
    basis = `<span class="tco-placeholder" title="${esc(rate ? `${rate.note}. Owner: ${rate.owner}` : "placeholder")}">placeholder · ${esc(rate ? rate.owner : "")}</span>`;
    const avg = rate && rate.by_family && mixed ? ` <span class="muted">(class average)</span>` : "";
    rateCell = c.rate == null ? "—" : (c.rate_id === "capital" ? `${tcoNum1(c.rate * 100)} % a year` : `${tcoEur(c.rate)} ${esc(rate ? rate.unit : "")}${avg}`);
  }
  const why = c.reason && c.reason !== layerReason ? `<div class="wh-note">${esc(c.reason)}</div>` : "";
  return `<tr class="tco-part">
    <td class="tco-part__label">${esc(c.label)}${c.note ? `<div class="wh-note">${esc(c.note)}</div>` : ""}</td>
    <td>${basis}</td>
    <td class="num">${c.quantity == null ? "—" : `${tcoNum1(c.quantity)} <span class="muted">${esc(c.unit)}</span>`}</td>
    <td class="num">${rateCell}</td>
    <td class="num">${c.total == null ? `<span class="wh-na">n/a</span>${why}` : tcoEur(c.total, 0)}</td>
    <td></td><td></td>
  </tr>`;
}

function tcoLayerRows(g) {
  const defs = Object.fromEntries((TCO.layers || []).map((d) => [d.id, d.description]));
  const mixed = g.kind === "portfolio";
  return g.layers.map((l) => {
    const measured = l.components.filter((c) => c.basis === "measured").length;
    const basis = measured === l.components.length ? `<span class="tco-measured">measured</span>`
      : measured ? `<span class="muted">measured and placeholder</span>` : `<span class="muted">quantity measured, rate placeholder</span>`;
    const head = `<tr class="tco-layer">
      <td><span class="tco-swatch" style="background:${TCO_TONE[l.id]}"></span><b>${esc(l.label)}</b><div class="wh-note">${esc(defs[l.id] || "")}</div></td>
      <td>${basis}</td>
      <td></td><td></td>
      <td class="num">${l.total == null ? `<span class="wh-na">n/a</span><div class="wh-note">${esc(l.reason || "")}</div>` : `<b>${tcoEur(l.total, 0)}</b>`}</td>
      <td class="num">${l.per_device == null ? "—" : tcoEur(l.per_device)}</td>
      <td class="num">${l.per_month == null ? "—" : tcoEur(l.per_month)}</td>
    </tr>`;
    return head + l.components.map((c) => tcoPartRow(c, l.reason, mixed)).join("");
  }).join("");
}

function tcoRateFoot() {
  const one = (r) => {
    const v = r.by_family ? Object.entries(r.by_family).map(([f, x]) => `${f} ${tcoEur(x)}`).join(" / ")
      : (r.id === "capital" ? `${tcoNum1(r.value * 100)} %` : tcoEur(r.value));
    return `${esc(r.label)} ${v} ${esc(r.unit)} (${esc(r.owner)})`;
  };
  return (TCO.rates || []).map(one).join(" · ");
}

function tcoDraw(screen) {
  const C = TCO.cohorts[TCO_COHORT];
  const P = C.portfolio;
  const G = tcoPicked(C);
  const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;
  const toggle = `<div class="segmented" id="tco-cohort">${TCO_COHORTS.map((id) => `<button class="${TCO_COHORT === id ? "active" : ""}" data-cohort="${id}">${esc(TCO.cohorts[id].label)}</button>`).join("")}</div>`;
  const r = P.resale;
  const top = screen.scrollTop;

  screen.innerHTML = `
    ${pageHead("Analytics", "Device TCO", "What a device costs over its life, per month of service, and how much the second life gives back. Quantities are measured from the order lines, the rental contracts, the service events, the compartments and the sold serials; rates are placeholders with an owner. Computed in the database from every serial.")}
    <div class="tco-toolbar">${toggle}<span class="tco-toolbar__desc">${esc(C.description)}</span><span class="tco-toolbar__asof">as of ${fmtDate(TCO.as_of)}</span></div>
    <div class="stats stats--5">
      ${stat("Net cost per device-month", "euro", P.per_month.net == null ? "—" : tcoEur(P.per_month.net), P.per_month.net == null ? esc(P.per_month_reason || P.reason || "") : `${tcoEur(P.per_month.gross)} before the resale credit`, "", "stat__val--gold")}
      ${stat("Cost per device", "box", P.per_device.gross == null ? "—" : tcoEur(P.per_device.gross, 0), P.per_device.gross == null ? esc(P.reason || "") : `${tcoEur(P.per_device.net, 0)} net of resale · ${tcoNum1(P.months_per_device)} months in service`)}
      ${stat("Second life gives back", "return", r.credit_share_of_acquisition == null ? "—" : tcoPct(r.credit_share_of_acquisition), r.credit_share_of_acquisition == null ? esc(r.reason || "") : `of acquisition, over ${num(r.sold_priced)} sold devices`, r.credit_share_of_acquisition == null ? "" : "stat__hint--pos")}
      ${stat("Devices", "layers", num(P.devices), P.devices ? `${num(P.rented)} rented · ${num(P.on_hand)} on hand · ${num(P.sold)} sold · ${num(P.recycled)} recycled` : esc(P.reason || ""))}
      ${stat("Device-months measured", "clock", num(Math.round(P.device_months)), `${num(P.contracts)} rentals · ${tcoPct(P.second_life_share_of_months)} of the months on a second rental`)}
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">By device class</span><span class="section__count">${C.classes.length}</span><span class="section__hint">open a row for its layers · the bar is the cost per device by layer, the resale credit stands beside it</span></div>
      <div class="panel"><table class="tbl tco-tbl">
        <thead><tr><th>Class</th><th>In service</th><th class="num">Cost per device</th><th style="width:220px">Layers per device</th><th>Second life gives back</th><th class="num">Net per device-month</th></tr></thead>
        <tbody>${C.classes.map(tcoClassRow).join("")}</tbody>
      </table></div>
      <div class="tco-legend">${(TCO.layers || []).filter((d) => d.id !== "eol").map((d) => `<span><span class="tco-swatch" style="background:${TCO_TONE[d.id]}"></span>${esc(d.label)}</span>`).join("")}</div>
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">Cost layers · ${esc(G.label)}</span><span class="section__count">${G.layers.length}</span><span class="section__hint">${G.devices ? `${num(G.devices)} devices · ${num(Math.round(G.device_months))} device-months` : esc(G.reason || "")} · every quantity is measured, every rate says who owns it</span></div>
      <div class="panel"><table class="tbl tco-tbl">
        <thead><tr><th>Layer</th><th>Basis</th><th class="num">Quantity</th><th class="num">Rate</th><th class="num">Total</th><th class="num">Per device</th><th class="num">Per month</th></tr></thead>
        <tbody>${tcoLayerRows(G)}</tbody>
      </table></div>
    </div>
    <div class="section" style="margin-bottom:0">
      <div class="section__head"><span class="section__title">By model</span><span class="section__count">${C.models.length}</span><span class="section__hint">open a row for its layers</span></div>
      <div class="panel"><table class="tbl tco-tbl">
        <thead><tr><th>Model</th><th>Class</th><th class="num">Devices</th><th class="num">Months per device</th><th class="num">Cost per device</th><th class="num">Second life gives back</th><th class="num">Net per device-month</th></tr></thead>
        <tbody>${C.models.map(tcoModelRow).join("") || `<tr><td colspan="7"><div class="state"><div class="state__sub">${esc(P.reason || "No model in this population.")}</div></div></td></tr>`}</tbody>
      </table></div>
      <div class="wh-foot">Rates, placeholders until the named role sets them: ${tcoRateFoot()}. ${esc(TCO.basis)} Months in service are the sum of every rental's days, running ones up to today; the warehouse layer is the stock on hand and its dwell to date, because a device's whole-life warehouse days need the movement log.</div>
    </div>`;

  $$("#tco-cohort button").forEach((b) => b.addEventListener("click", () => { TCO_COHORT = b.dataset.cohort; tcoDraw(screen); }));
  $$("#screen .tco-row[data-pick]").forEach((row) => row.addEventListener("click", () => {
    TCO_PICK = TCO_PICK === row.dataset.pick ? null : row.dataset.pick;
    tcoDraw(screen);
  }));
  screen.scrollTop = top;
}

RENDER.tco = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `<div class="state"><div class="state__icon">${icon("tco", 22)}</div><div class="state__title">No rental fleet in this database</div><div class="state__sub">The device TCO exists in the device-as-a-service scenario. This database holds the datacenter operation, whose per-asset TCO is on the analytics cockpit.</div></div>`;
    return;
  }
  try {
    TCO = await api("/tco/devices");
    if (!TCO.cohorts || !TCO.cohorts[TCO_COHORT]) { screen.innerHTML = errState(TCO.reason || "No device TCO in this database."); return; }
    tcoDraw(screen);
  } catch (e) { screen.innerHTML = errState(e.message); }
};
