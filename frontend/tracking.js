"use strict";
/* ============================================================================
   SCM Master — order delivery tracking (control tower).
   Loads after features.js; shares global scope.

   Data mirrors the supplied seed (suppliers · purchase_orders · shipments ·
   shipment_events → the v_order_tracking read model). Embedded here so the view
   renders with no extra backend; to go live, point loadTracking() at
   GET /v_order_tracking  and  GET /shipment_events?shipment_id=eq.<id>&order=seq
   (PostgREST) and map the same fields.
============================================================================ */

ICONS.track = '<path d="M3 12h4l2.5-7 5 14 2.5-7H21"/>';
ICONS.ship  = '<path d="M3 13l1.8 6a1.5 1.5 0 0 0 1.45 1.1h11.5A1.5 1.5 0 0 0 19.2 19L21 13z"/><path d="M6 13V7h12v6"/><path d="M12 3v4"/>';
ICONS.plane = '<path d="M17.8 19.2 16 11l4-4a2 2 0 0 0-3-3l-4 4-8-1.6a.5.5 0 0 0-.5.8L8 10l-3 3-2-.4a.5.5 0 0 0-.4.85L6 16l1.6 3.5a.5.5 0 0 0 .85-.4L8 16l3-3 3.2 5.4a.5.5 0 0 0 .8.1z"/>';
ICONS.train = '<rect x="5" y="3" width="14" height="13" rx="2"/><path d="M5 11h14"/><path d="M8 19l-2 2M16 19l2 2"/>';
const MODE_ICON = { ocean: "ship", air: "plane", road: "truck", rail: "train" };

/* ── seed (v_order_tracking + shipment_events) ─────────────────────── */
const TRACKING = [
  { po: "PO-10288", supplier: "Shenzhen Optics Co.", country: "CN", mode: "ocean", line: "Camera modules ×5,000",
    current_status: "customs", progress_idx: 3, current_location: "Hamburg customs, DE",
    eta_original: "2026-04-30", eta_current: "2026-05-04", delay_days: 4, exception_flag: true, total_value: 84200,
    events: [
      { status: "placed",          ts: "2026-04-12", loc: "Shenzhen, CN",       note: "Order confirmed by supplier" },
      { status: "packed",          ts: "2026-04-18", loc: "Shenzhen, CN",       note: "Packed, awaiting vessel" },
      { status: "departed_origin", ts: "2026-04-22", loc: "Yantian Port, CN",   note: "Loaded on MV Hanjin (ETD)" },
      { status: "arrived_hub",     ts: "2026-04-28", loc: "Port of Hamburg, DE", note: "Container discharged" },
      { status: "customs",         ts: "2026-05-02", loc: "Hamburg customs, DE", note: "Held — HS code documentation query" },
    ] },
  { po: "PO-10310", supplier: "Pan-Asia Distribution", country: "TW", mode: "ocean", line: "MCU wafers ×30 lots",
    current_status: "departed_origin", progress_idx: 1, current_location: "Kaohsiung Port, TW",
    eta_original: "2026-05-19", eta_current: "2026-05-21", delay_days: 2, exception_flag: false, total_value: 210000,
    events: [
      { status: "placed",          ts: "2026-04-25", loc: "Hsinchu, TW",       note: "Order confirmed" },
      { status: "packed",          ts: "2026-05-09", loc: "Hsinchu, TW",       note: "Packed & sealed" },
      { status: "departed_origin", ts: "2026-05-13", loc: "Kaohsiung Port, TW", note: "At port — vessel congestion, ETD slipping" },
    ] },
  { po: "PO-10293", supplier: "Bosch Rexroth", country: "DE", mode: "road", line: "Hydraulic valves ×120",
    current_status: "in_transit", progress_idx: 2, current_location: "Frankfurt hub, DE",
    eta_original: "2026-05-05", eta_current: "2026-05-05", delay_days: 0, exception_flag: false, total_value: 31500,
    events: [
      { status: "placed",          ts: "2026-04-28", loc: "Lohr am Main, DE", note: "Order confirmed" },
      { status: "packed",          ts: "2026-04-30", loc: "Lohr am Main, DE", note: "Picked & packed" },
      { status: "departed_origin", ts: "2026-05-02", loc: "Würzburg, DE",     note: "Departed origin" },
      { status: "in_transit",      ts: "2026-05-03", loc: "Frankfurt hub, DE", note: "In transit — line haul" },
    ] },
  { po: "PO-10301", supplier: "Murata Mfg.", country: "JP", mode: "air", line: "Capacitors ×200,000",
    current_status: "out_for_delivery", progress_idx: 4, current_location: "Munich, DE",
    eta_original: "2026-05-04", eta_current: "2026-05-04", delay_days: 0, exception_flag: false, total_value: 18900,
    events: [
      { status: "placed",          ts: "2026-04-26", loc: "Kyoto, JP",        note: "Order confirmed" },
      { status: "departed_origin", ts: "2026-04-29", loc: "Kansai Airport, JP", note: "Air freight departed" },
      { status: "arrived_hub",     ts: "2026-05-01", loc: "Frankfurt FRA, DE", note: "Customs cleared at FRA" },
      { status: "out_for_delivery",ts: "2026-05-04", loc: "Munich, DE",       note: "Out for delivery" },
    ] },
  { po: "PO-10275", supplier: "Würth Group", country: "DE", mode: "road", line: "Fastener assortment",
    current_status: "delivered", progress_idx: 5, current_location: "Berlin DC, DE",
    eta_original: "2026-05-03", eta_current: "2026-05-03", delay_days: 0, exception_flag: false, total_value: 6420,
    events: [
      { status: "placed",          ts: "2026-04-28", loc: "Künzelsau, DE", note: "Order confirmed" },
      { status: "departed_origin", ts: "2026-04-30", loc: "Künzelsau, DE", note: "Dispatched" },
      { status: "delivered",       ts: "2026-05-03", loc: "Berlin DC, DE",  note: "Delivered — signed M. Krause" },
    ] },
  { po: "PO-10312", supplier: "Berliner Verpackung", country: "DE", mode: "road", line: "Pallet packaging",
    current_status: "placed", progress_idx: 0, current_location: "Berlin, DE",
    eta_original: "2026-05-12", eta_current: "2026-05-12", delay_days: 0, exception_flag: false, total_value: 2150,
    events: [
      { status: "placed", ts: "2026-05-03", loc: "Berlin, DE", note: "PO issued & confirmed" },
    ] },
];

CRUMBS.tracking = "Orders";

/* ── helpers ───────────────────────────────────────────────────────── */
const shortDate = (iso) => new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });

// pill: delivered > exception(customs hold) > at-risk(delay) > current_status
const STAT_LABEL = {
  placed: "Placed", confirmed: "Confirmed", packed: "Packed", departed_origin: "Departed",
  in_transit: "In transit", arrived_hub: "At hub", customs: "In customs", out_for_delivery: "Out for delivery", delivered: "Delivered",
};
function trackPill(o) {
  if (o.current_status === "delivered") return ["Delivered", "positive"];
  if (o.exception_flag) return [o.current_status === "customs" ? "Customs hold" : "Exception", "negative"];
  if (o.delay_days > 0) return ["At risk", "warning"];
  if (o.current_status === "out_for_delivery" || o.current_status === "in_transit" || o.current_status === "departed_origin" || o.current_status === "arrived_hub")
    return [STAT_LABEL[o.current_status], "info"];
  return [STAT_LABEL[o.current_status] || "Placed", "neutral"];
}

const trackHTML = (o) => {
  const STAGES = 6;
  let s = "";
  for (let i = 0; i < STAGES; i++) {
    let cls = "tnode";
    if (i < o.progress_idx) cls += " tnode--done";
    else if (i === o.progress_idx) cls += o.exception_flag ? " tnode--current tnode--exception" : " tnode--current";
    s += `<div class="tseg"><div class="${cls}"></div>${i < STAGES - 1 ? `<div class="tbar${i < o.progress_idx ? " tbar--done" : ""}"></div>` : ""}</div>`;
  }
  return `<div class="track">${s}</div>`;
};

/* ── render ────────────────────────────────────────────────────────── */
let trackSel = "PO-10288";

/* ── live data: /v_order_tracking + /shipment_events (sample fallback) ─ */
let TRK_LOADED = false;
let PO_ITEMS = {};   // order_number -> [line items], PROD_NAME -> product_id -> name
let PROD_NAME = {};
async function loadTracking() {
  // PO line-item contents + product names, so the timeline can show WHAT is in
  // each order (the backend already serves these; we just join by order_number).
  try {
    const [pos, prods] = await Promise.all([
      api("/purchase-orders?limit=500").catch(() => []),
      api("/products?limit=500").catch(() => []),
    ]);
    PROD_NAME = {}; (prods || []).forEach((p) => { PROD_NAME[p.id] = p.name || p.product_code; });
    PO_ITEMS = {}; (pos || []).forEach((o) => { PO_ITEMS[o.order_number] = o.items || []; });
  } catch (e) { /* contents degrade to unavailable */ }
  try {
    const rows = await api("/v_order_tracking");
    if (Array.isArray(rows) && rows.length) {
      const mapped = await Promise.all(rows.map(async (r) => {
        let events = [];
        try {
          const ev = await api(`/shipment_events?shipment_id=eq.${r.shipment_id}&order=seq`);
          events = (ev || []).map((e) => ({ status: e.status, ts: e.event_ts, loc: e.location_name, note: e.notes || e.status }));
        } catch (e) { /* leave events empty */ }
        return {
          po: r.po_id, supplier: r.supplier, country: r.country, mode: r.mode,
          line: r.shipment_id, current_status: r.current_status, progress_idx: r.progress_idx,
          current_location: r.current_location, eta_original: r.eta_original, eta_current: r.eta_current,
          delay_days: r.delay_days, exception_flag: r.exception_flag, total_value: r.total_value,
          events,
        };
      }));
      TRACKING.length = 0; mapped.forEach((m) => TRACKING.push(m));
      if (!TRACKING.find((o) => o.po === trackSel)) trackSel = TRACKING[0] && TRACKING[0].po;
    }
  } catch (e) { /* keep embedded sample on any error */ }
  TRK_LOADED = true;
}

RENDER.tracking = async function () {
  if (!TRK_LOADED) { await loadTracking(); }
  const open = TRACKING.length;
  const delayed = TRACKING.filter((o) => o.exception_flag || o.delay_days > 0).length;
  const ofd = TRACKING.filter((o) => o.current_status === "out_for_delivery").length;
  const delivered = TRACKING.filter((o) => o.current_status === "delivered").length;

  const cards = TRACKING.map((o) => {
    const [label, tone] = trackPill(o);
    const sel = trackSel === o.po;
    const delay = o.delay_days > 0 ? `<span class="tcard__delay tcard__delay--neg">+${o.delay_days} day${o.delay_days > 1 ? "s" : ""}</span>` : `<span class="tcard__delay tcard__delay--ok">On time</span>`;
    return `<div class="tcard${sel ? " tcard--sel" : ""}" data-po="${o.po}">
      <div class="tcard__head">
        <span class="tcard__mode">${icon(MODE_ICON[o.mode] || "box", 22)}</span>
        <div style="min-width:0">
          <div class="tcard__title">${o.po} · ${esc(o.supplier)} (${o.country})</div>
          <div class="tcard__sub">${esc(o.line)} · ${euro(o.total_value)}</div>
        </div>
        <span class="tcard__pill">${plainPill(label, tone)}</span>
      </div>
      ${trackHTML(o)}
      <div class="tcard__foot">
        <span class="tcard__loc">${icon("pin", 14)} ${esc(o.current_location)}</span>
        <span class="tcard__eta">ETA ${shortDate(o.eta_current)} ·${delay}</span>
      </div>
    </div>`;
  }).join("");

  $("#screen").innerHTML = `<div class="fade-in">
    ${pageHead("Control tower", "Tracking",
      "Live per-order delivery tracking — one milestone track per shipment. A delayed line flags the whole order; tap any order for its scan-by-scan timeline.",
      `<button class="btn btn--ink" id="new-order-btn">${icon("cart", 15)} New order</button>`)}
    <div id="order-modal-host"></div>
    <div class="tkpis">
      <div class="tkpi"><div class="tkpi__label">Open orders</div><div class="tkpi__val">${open}</div></div>
      <div class="tkpi"><div class="tkpi__label">Delayed / at risk</div><div class="tkpi__val tkpi__val--neg">${delayed}</div></div>
      <div class="tkpi"><div class="tkpi__label">Out for delivery</div><div class="tkpi__val tkpi__val--info">${ofd}</div></div>
      <div class="tkpi"><div class="tkpi__label">Delivered</div><div class="tkpi__val tkpi__val--pos">${delivered}</div></div>
    </div>
    <div class="track-split">
      <div id="track-cards">${cards}</div>
      <div id="track-timeline"></div>
    </div>
  </div>`;
  $$("#track-cards .tcard").forEach((c) => c.addEventListener("click", () => { trackSel = c.dataset.po; renderTimeline(); $$("#track-cards .tcard").forEach((x) => x.classList.toggle("tcard--sel", x.dataset.po === trackSel)); }));
  renderTimeline();
  const nob = $("#new-order-btn");
  if (nob) nob.addEventListener("click", openOrderModal);
};

/* ── New Order modal: catalog OR package, capacity-aware, capacity-guarded ── */
let _orderLines = [];   // [{product_id, quantity}]

async function openOrderModal() {
  // Pull catalog, packages, and the live capacity-flow in parallel.
  const [products, packages, cap] = await Promise.all([
    api("/products?limit=500").catch(() => []),
    api("/requisitions/packages").catch(() => []),
    api("/planning/capacity-flow").catch(() => null),
  ]);
  _orderLines = [];
  const host = $("#order-modal-host");
  const prodOpts = (products || []).map((p) => `<option value="${p.id}">${esc(p.name)} (${esc(p.product_code)})</option>`).join("");
  const pkgOpts = (packages || []).map((p) => `<option value="${p.id}">${esc(p.name)} — ${p.lines.length} line(s)</option>`).join("");

  host.innerHTML = `<div class="ordm__veil" id="ordm-veil"></div>
    <div class="ordm" role="dialog" aria-label="New order">
      <div class="ordm__head">
        <div><div class="ordm__eyebrow">Procurement</div><h2 class="ordm__title">New order</h2></div>
        <button class="ordm__x" id="ordm-x">×</button>
      </div>
      <div class="ordm__cap" id="ordm-cap">${capLine(cap)}</div>
      <div class="ordm__body">
        <div class="ordm__row">
          <label>Package <span class="muted">(one-click bundle)</span></label>
          <div style="display:flex;gap:8px">
            <select id="ordm-pkg" style="flex:1"><option value="">— choose a package —</option>${pkgOpts}</select>
            <input id="ordm-packs" type="number" min="1" value="1" title="how many packs" style="width:70px"/>
            <button class="btn btn--ghost" id="ordm-addpkg">Add</button>
          </div>
        </div>
        <div class="ordm__or">or pick individual products</div>
        <div class="ordm__row">
          <div style="display:flex;gap:8px">
            <select id="ordm-prod" style="flex:1">${prodOpts}</select>
            <input id="ordm-qty" type="number" min="1" value="1" style="width:70px"/>
            <button class="btn btn--ghost" id="ordm-addprod">Add</button>
          </div>
        </div>
        <table class="ordm__lines" id="ordm-lines"><tbody></tbody></table>
      </div>
      <div class="ordm__foot">
        <span class="muted" id="ordm-summary">No lines yet.</span>
        <span style="flex:1"></span>
        <button class="btn btn--ghost" id="ordm-cancel">Cancel</button>
        <button class="btn btn--ink" id="ordm-place" disabled>${icon("check", 14)} Stage order</button>
      </div>
    </div>`;

  const close = () => { host.innerHTML = ""; };
  $("#ordm-x").onclick = close; $("#ordm-cancel").onclick = close; $("#ordm-veil").onclick = close;

  $("#ordm-addprod").onclick = () => {
    const sel = $("#ordm-prod"); const qty = parseInt($("#ordm-qty").value, 10) || 1;
    addOrderLine(sel.value, sel.options[sel.selectedIndex].text, qty);
  };
  $("#ordm-addpkg").onclick = async () => {
    const pid = $("#ordm-pkg").value; if (!pid) return;
    const packs = parseInt($("#ordm-packs").value, 10) || 1;
    const pkg = (packages || []).find((p) => p.id === pid);
    if (!pkg) return;
    pkg.lines.forEach((ln) => {
      const prod = (products || []).find((p) => p.id === ln.product_id);
      addOrderLine(ln.product_id, prod ? prod.name : ln.product_id, ln.quantity * packs);
    });
  };
  $("#ordm-place").onclick = () => placeOrder(close);
  renderOrderLines();
}

function capLine(cap) {
  if (!cap || cap.free_to_order == null) return `<span class="muted">No warehouse capacity defined — no storage limit applies.</span>`;
  const pct = cap.committed_pct != null ? Math.round(cap.committed_pct * 100) : 0;
  const cover = cap.weeks_of_cover != null ? `${cap.weeks_of_cover}w cover` : "—";
  return `<b>${cap.free_to_order}</b> units free to order · <b>${pct}%</b> committed of ${cap.capacity}
    · in ${cap.daily_in}/d, out ${cap.daily_out}/d · ${cover}`;
}

function addOrderLine(productId, label, qty) {
  if (!productId || qty <= 0) return;
  const existing = _orderLines.find((l) => l.product_id === productId);
  if (existing) existing.quantity += qty;
  else _orderLines.push({ product_id: productId, label, quantity: qty });
  renderOrderLines();
}

function renderOrderLines() {
  const tb = $("#ordm-lines").querySelector("tbody");
  tb.innerHTML = _orderLines.map((l, i) => `<tr>
    <td>${esc(l.label)}</td>
    <td class="num"><input type="number" min="1" value="${l.quantity}" data-i="${i}" class="ordm-qedit" style="width:64px"/></td>
    <td><button class="ordm__rm" data-i="${i}">remove</button></td></tr>`).join("");
  const total = _orderLines.reduce((s, l) => s + l.quantity, 0);
  $("#ordm-summary").textContent = _orderLines.length ? `${_orderLines.length} line(s) · ${total} units` : "No lines yet.";
  $("#ordm-place").disabled = _orderLines.length === 0;
  tb.querySelectorAll(".ordm-qedit").forEach((inp) => inp.onchange = () => {
    const v = parseInt(inp.value, 10) || 1; _orderLines[+inp.dataset.i].quantity = Math.max(1, v); renderOrderLines();
  });
  tb.querySelectorAll(".ordm__rm").forEach((b) => b.onclick = () => { _orderLines.splice(+b.dataset.i, 1); renderOrderLines(); });
}

async function placeOrder(close) {
  const btn = $("#ordm-place"); btn.disabled = true;
  try {
    const res = await api("/requisitions/manual", { method: "POST",
      body: { lines: _orderLines.map((l) => ({ product_id: l.product_id, quantity: l.quantity })) } });
    const bits = [`${res.requisition_ids.length} requisition(s) staged`];
    if (res.orphans && res.orphans.length) bits.push(`${res.orphans.length} need a supplier`);
    toast(bits.join(" · "), "ok");
    close();
  } catch (e) {
    // The capacity guard (422) and any other refusal land here.
    toast(e.message || "Order refused", "err");
    btn.disabled = false;
  }
}

function renderTimeline() {
  const o = TRACKING.find((x) => x.po === trackSel) || TRACKING[0];
  const items = o.events.map((e, i) => {
    const last = i === o.events.length - 1;
    const exc = e.status === "customs" || e.status === "exception";
    const dot = exc ? "var(--ts-negative)" : "var(--ts-ink-soft)";
    return `<div class="log__entry">
      <div class="log__rail"><div class="log__dot" style="background:${dot}"></div><div class="log__line"></div></div>
      <div class="log__body">
        <div class="log__note" style="font-weight:600;color:${exc ? "var(--ts-negative)" : "var(--ts-ink)"};font-size:13px">${esc(e.note)}</div>
        <div class="log__time">${shortDate(e.ts)} · ${esc(e.loc)}</div>
      </div></div>`;
  }).join("");
  // Order contents — the actual PO line items (what's in this order).
  const lines = PO_ITEMS[o.po] || [];
  const contents = lines.length
    ? `<table class="po-contents"><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Line</th></tr></thead><tbody>${
        lines.map((l) => `<tr><td>${esc(PROD_NAME[l.product_id] || l.product_id)}</td>`
          + `<td class="num">${l.quantity}</td>`
          + `<td class="num">${l.unit_price != null ? euro(l.unit_price) : "—"}</td>`
          + `<td class="num">${l.unit_price != null ? euro(l.quantity * Number(l.unit_price)) : "—"}</td></tr>`).join("")
      }</tbody></table>`
    : `<div class="muted" style="font-size:12px;padding:4px 0">Line items unavailable for this order.</div>`;

  $("#track-timeline").innerHTML = `<div class="tlpanel">
    <div class="tlpanel__head">
      <div class="tlpanel__title">${o.po} — event timeline</div>
      <div class="tlpanel__promise">Promised <b>${shortDate(o.eta_original)}</b> → now <b>${shortDate(o.eta_current)}</b></div>
    </div>
    <div class="po-contents-h">Order contents</div>
    ${contents}
    <div class="log">${items}</div>
    <div class="tlpanel__escalate"><button class="btn btn--secondary btn--sm" id="track-escalate">Escalate this order ${icon("arrow", 13)}</button></div>
  </div>`;
  const esb = $("#track-escalate");
  if (esb) esb.addEventListener("click", () => toast(`Escalation opened for ${o.po}`, "ok"));
}
