"use strict";
/* ============================================================================
   SCM Master — Inventory & reorder planning. Loads after tracking.js.

   Answers, per item: how much is on hand vs warehouse capacity, where the
   reorder point sits, how much is already on order and WHEN it lands — so a
   buyer can see "reorder now / hold / don't overstock" at a glance.

   Reorder model (mirrors a classic min/max plan):
     daily_burn       — avg units consumed per day (from deployment history)
     days_of_cover    — on_hand / daily_burn
     reorder_point    — daily_burn × lead_time_days + safety_stock
     coverage_gap     — days until the next inbound lands vs days_of_cover
   To go live, replace INVENTORY[] with a read model joining current stock,
   the open inbound pipeline (ETA), and a consumption-rate view per product.
============================================================================ */

ICONS.stock = '<path d="M3 9l9-6 9 6v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>';

CRUMBS.inventory = "Inventory";

/* ── seed: per-item stock + reorder signals ────────────────────────── */
const INVENTORY = [
  { product: "R760",  name: "PowerEdge R760 · 2U server", category: "Servers",    location: "Frankfurt DC",
    on_hand: 6,  capacity: 60,  safety_stock: 4,  daily_burn: 0.55, lead_time_days: 21,
    on_order: 4,  next_eta: "2026-06-12", unit_price: 8420 },
  { product: "EPYC",  name: "EPYC 9554 · 64-core CPU",     category: "Processors", location: "Transit warehouse",
    on_hand: 1,  capacity: 40,  safety_stock: 6,  daily_burn: 0.42, lead_time_days: 35,
    on_order: 0,  next_eta: null,         unit_price: 7120 },
  { product: "DIMM",  name: "64GB DDR5-4800 RDIMM",        category: "Memory",     location: "Transit warehouse",
    on_hand: 22, capacity: 240, safety_stock: 32, daily_burn: 3.10, lead_time_days: 18,
    on_order: 16, next_eta: "2026-06-03", unit_price: 430 },
  { product: "SC847", name: "SC847 · 4U JBOD chassis",     category: "Storage",    location: "Frankfurt DC",
    on_hand: 9,  capacity: 24,  safety_stock: 2,  daily_burn: 0.18, lead_time_days: 25,
    on_order: 4,  next_eta: "2026-06-25", unit_price: 3180 },
  { product: "NIC",   name: "ConnectX-7 200G NIC",          category: "Networking", location: "Frankfurt DC",
    on_hand: 48, capacity: 80,  safety_stock: 12, daily_burn: 0.70, lead_time_days: 14,
    on_order: 0,  next_eta: null,         unit_price: 1180 },
  { product: "PSU",   name: "2400W Titanium PSU",           category: "Power",      location: "Transit warehouse",
    on_hand: 31, capacity: 120, safety_stock: 16, daily_burn: 1.20, lead_time_days: 16,
    on_order: 40, next_eta: "2026-06-08", unit_price: 540 },
];

const NOW_INV = new Date("2026-05-28T00:00:00Z");
const daysUntil = (iso) => iso ? Math.ceil((new Date(iso) - NOW_INV) / 86400000) : null;

function plan(it) {
  const cover = it.daily_burn > 0 ? Math.floor(it.on_hand / it.daily_burn) : 999;
  const rop = Math.ceil(it.daily_burn * it.lead_time_days + it.safety_stock);
  const etaDays = daysUntil(it.next_eta);
  const projected = it.on_hand + it.on_order;          // after the open order lands
  const overByOrder = projected > it.capacity;
  // will we run dry before the next order arrives?
  const stockoutBeforeEta = it.on_order > 0 && etaDays != null && cover < etaDays;
  const belowRop = it.on_hand <= rop;

  let status, tone, rec;
  if (cover <= it.lead_time_days && it.on_order === 0) {
    status = "Stock-out risk"; tone = "negative";
    rec = `Order now — ${cover}d cover vs ${it.lead_time_days}d lead. Suggest ${suggest(it, rop)} units.`;
  } else if (stockoutBeforeEta) {
    status = "Expedite"; tone = "negative";
    rec = `Runs dry in ${cover}d but next order lands in ${etaDays}d — expedite or add a bridge buy.`;
  } else if (belowRop && it.on_order === 0) {
    status = "Reorder"; tone = "warning";
    rec = `Below reorder point (${rop}). Place ${suggest(it, rop)} units with the preferred source.`;
  } else if (overByOrder) {
    status = "Overstock risk"; tone = "warning";
    rec = `On order would reach ${projected} vs ${it.capacity} capacity — trim the next PO by ${projected - it.capacity}.`;
  } else if (it.on_order > 0) {
    status = "On order"; tone = "info";
    rec = `Covered — ${it.on_order} units land in ${etaDays}d, bridging ${cover}d of stock.`;
  } else {
    status = "Healthy"; tone = "positive";
    rec = `${cover}d of cover, above reorder point. No action.`;
  }
  return { cover, rop, etaDays, projected, overByOrder, status, tone, rec };
}
// suggest to refill to ~85% of capacity, in MOQ-ish rounding
const suggest = (it, rop) => { const target = Math.round(it.capacity * 0.85); return Math.max(rop - it.on_hand, target - it.on_hand - it.on_order, it.safety_stock); };

/* ── live data: /planning/inventory + /planning/demand (sample fallback) ─ */
let INV_LOADED = false;
const DEMAND = {};   // name -> {usage_rate_per_day, projected_demand, projected_shortfall, recommended_order_qty, order_by}
async function loadInventory() {
  try {
    const rows = await api("/planning/inventory");
    if (Array.isArray(rows) && rows.length) {
      INVENTORY.length = 0;
      rows.forEach((r) => INVENTORY.push({
        product: r.product_code || r.product_id, name: r.name || r.product_code,
        category: r.category || "", location: "Warehouse",
        on_hand: r.on_hand, capacity: r.capacity, safety_stock: r.safety_stock,
        daily_burn: r.daily_burn, lead_time_days: r.lead_time_days,
        on_order: r.on_order, next_eta: r.next_eta, unit_price: r.unit_price,
      }));
    }
  } catch (e) { /* keep embedded sample on any error */ }
  try {
    const dem = await api("/planning/demand");
    (dem || []).forEach((d) => { DEMAND[d.name || d.product_code] = d; });
  } catch (e) { /* demand column degrades to "—" */ }
  INV_LOADED = true;
}

/* ── render — "control tower": action-queue KPIs + a Next-order hero column ─ */
RENDER.inventory = async function () {
  if (!INV_LOADED) { await loadInventory(); }
  document.body.classList.remove("ictw-ai-on");  // start with rationale rows hidden
  // Most-urgent first, so the table reads as an action queue.
  const ORDER = { negative: 0, warning: 1, info: 2, positive: 3 };
  const plans = INVENTORY.map((it) => ({ it, p: plan(it) }))
    .sort((a, b) => (ORDER[a.p.tone] - ORDER[b.p.tone]) || (a.p.cover - b.p.cover));

  const act = plans.filter((x) => ["Stock-out risk", "Expedite"].includes(x.p.status)).length;
  const reorder = plans.filter((x) => x.p.status === "Reorder").length;
  const inbound = plans.filter((x) => x.it.on_order > 0).length;
  const over = plans.filter((x) => x.p.status === "Overstock risk").length;

  const kpis = `<div class="ictw-kpis">
    ${ictwKpi("crit", "Act today", act, act ? `${plans.filter((x)=>x.p.status==="Expedite").length} expedite · ${plans.filter((x)=>x.p.status==="Stock-out risk").length} stock-out` : "nothing urgent")}
    ${ictwKpi("warn", "Reorder soon", reorder, "below reorder point")}
    ${ictwKpi("ok", "Inbound", inbound, "shipments in transit")}
    ${ictwKpi("idle", "Overstock risk", over, over ? "trim the next PO" : "none over capacity")}
  </div>`;

  const legend = `<div class="ictw-legend">
    <div class="ictw-li"><span class="ictw-sw-fill"></span>On hand</div>
    <div class="ictw-li"><span class="ictw-sw-in"></span>Inbound</div>
    <div class="ictw-li"><span class="ictw-sw-re"></span>Reorder point</div>
    <div class="ictw-li" style="margin-left:auto">bar spans 0 → capacity per item</div>
  </div>`;

  const rows = plans.map(({ it, p }, i) => ictwRow(it, p, i)).join("");

  $("#screen").innerHTML = `<div class="fade-in ictw">
    <div class="ictw-head">
      ${pageHead("Inventory · Control Tower", "What to order, how much, by when",
        "Stock and days-of-cover for every item, with the next order you need to place and anything already inbound.")}
      <button class="ictw-aitoggle" id="inv-reason" title="Run AI reasoning over the live demand forecast">
        <div><div class="ictw-aitoggle__lbl">AI demand reasoning</div><div class="ictw-aitoggle__sub">Risks the math misses</div></div>
        <span class="ictw-switch"></span>
      </button>
    </div>
    ${kpis}
    <div id="inv-reasoning"></div>
    ${legend}
    <div class="panel ictw-panel">
      <div class="ictw-thead">
        <span>Item</span><span>In stock</span><span class="num">90-day need</span>
        <span>Inbound</span><span class="num">Next order to place</span>
      </div>
      <div>${rows}</div>
    </div>
  </div>`;
  const rb = $("#inv-reason");
  if (rb) rb.addEventListener("click", () => reasonDemand(rb));
};

function ictwKpi(kind, label, num, meta) {
  return `<div class="ictw-kpi ictw-kpi--${kind}">
    <div class="ictw-kpi__lbl"><span class="ictw-dot"></span>${esc(label)}</div>
    <div class="ictw-kpi__num">${num}</div>
    <div class="ictw-kpi__meta">${esc(meta)}</div>
  </div>`;
}

// status -> chip label + tone for the hero "Next order" column
const ICTW_CHIP = {
  "Stock-out risk": ["STOCK-OUT", "crit"], "Expedite": ["EXPEDITE", "crit"],
  "Reorder": ["REORDER", "warn"], "Overstock risk": ["OVERSTOCK", "warn"],
  "On order": ["ON ORDER", "ok"], "Healthy": ["HEALTHY", "ok"],
};

function ictwRow(it, p, i) {
  const d = DEMAND[it.name] || {};
  const fillPct = Math.min(it.on_hand / it.capacity, 1) * 100;
  const inbW = Math.max(Math.min(it.on_order / it.capacity, 1 - fillPct / 100) * 100, 0);
  const roPct = Math.min(p.rop / it.capacity, 1) * 100;
  const coverCls = p.cover <= it.lead_time_days ? "c-crit" : p.cover <= it.lead_time_days * 1.6 ? "c-warn" : "c-ok";
  const [chipLbl, chipCls] = ICTW_CHIP[p.status] || ["—", "ok"];

  // demand: projected 90-day need + shortfall, from /planning/demand
  const need = d.projected_demand != null ? Math.round(d.projected_demand) : null;
  const short = d.projected_shortfall != null ? Math.round(d.projected_shortfall) : null;
  const rate = d.usage_rate_per_day != null ? `${d.usage_rate_per_day}/d` : "";

  // the hero "next order" cell — qty + when, driven by the plan() rec
  const recQty = d.recommended_order_qty || suggest(it, p.rop);
  const orderQty = (p.status === "Expedite") ? "Expedite inbound"
    : (p.status === "On order") ? `Order ${recQty}` : `Order ${recQty}`;
  const whenCls = chipCls === "crit" ? "now" : chipCls === "warn" ? "soon" : "future";
  const orderWhen = ictwWhen(it, p, d);

  const inboundCell = it.on_order
    ? `<div class="i-qty">+${it.on_order}</div><div class="i-date">${it.next_eta ? shortDate(it.next_eta) : "scheduled"}</div>${p.etaDays != null && p.etaDays < 0 ? `<div class="i-flag">overdue ${-p.etaDays}d</div>` : ""}`
    : `<span class="i-none">—</span>`;

  return `<div class="ictw-row" style="animation-delay:${i * 50}ms">
    <div class="ictw-item">${productCell(invPid(it))}</div>
    <div class="ictw-stock">
      <div class="ictw-stock__top">
        <span class="ictw-qty">${it.on_hand}</span><span class="ictw-unit">units</span>
        <span class="ictw-cover ${coverCls}">${p.cover > 365 ? "365+" : p.cover}d cover</span>
      </div>
      <div class="ictw-bar">
        <div class="ictw-bar__fill" style="width:${fillPct}%"></div>
        ${inbW > 0 ? `<div class="ictw-bar__inb" style="left:${fillPct}%;width:${inbW}%"></div>` : ""}
        <div class="ictw-bar__ro" style="left:${roPct}%" title="Reorder point ${p.rop}"></div>
      </div>
    </div>
    <div class="ictw-demand num">
      ${need != null ? `<div class="d-main">${need}</div>
        <div class="d-sub ${short > 0 ? "" : "zero"}">${short > 0 ? "short " + short : "covered"}</div>
        ${rate ? `<div class="d-rate">${rate}</div>` : ""}` : `<span class="i-none">—</span>`}
    </div>
    <div class="ictw-inbound">${inboundCell}</div>
    <div class="ictw-order num">
      <span class="ictw-ochip ${chipCls}">${chipLbl}</span>
      <div class="o-qty">${esc(orderQty)}</div>
      <div class="o-when ${whenCls}">${esc(orderWhen)}</div>
    </div>
    <div class="ictw-reason">${esc(p.rec)}${demandRec(it)}</div>
  </div>`;
}

// human "when to order" line for the hero column
function ictwWhen(it, p, d) {
  if (p.status === "Expedite") return `lands in ${p.etaDays}d · add bridge buy`;
  if (p.status === "Stock-out risk") return "now";
  if (p.status === "Reorder") return `now · below reorder point (${p.rop})`;
  if (p.status === "On order") {
    const by = d.order_by ? ` · reorder by ${fmtDate(d.order_by)}` : "";
    return `${p.etaDays != null ? p.etaDays + "d to land" : "inbound"}${by}`;
  }
  if (p.status === "Overstock risk") return `trim next PO by ${p.projected - it.capacity}`;
  return "no action";
}

async function reasonDemand(btn) {
  const box = $("#inv-reasoning");
  btn.disabled = true;
  btn.classList.add("ictw-aitoggle--on");
  document.body.classList.add("ictw-ai-on");   // reveal the per-row PLAN rationale
  const subEl = btn.querySelector(".ictw-aitoggle__sub");
  const subWas = subEl ? subEl.textContent : "";
  if (subEl) subEl.textContent = "Reasoning…";
  box.innerHTML = `<div class="state"><div class="state__sub">The agent is reasoning over the live forecast…</div></div>`;
  try {
    const res = await api("/planning/demand/reason", { method: "POST" });
    const U = { urgent: "negative", soon: "warning", routine: "info" };
    const ADJ = { raise: "↑ raise", lower: "↓ lower", hold: "= hold", defer: "⏸ defer" };
    const cards = (res.items || []).map((it) => `
      <div class="panel" style="padding:14px 16px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <strong>${esc(it.name || it.product_id)}</strong>
          ${plainPill((it.urgency || "routine").toUpperCase(), U[it.urgency] || "info")}
          <span class="muted" style="font-size:12px">computed short ${Math.round(it.computed_shortfall)} · AI: ${esc(ADJ[it.adjustment] || it.adjustment)} → order ${it.recommended_qty}</span>
          <span style="margin-left:auto;font-size:12px;color:var(--ts-ink-mute)">confidence ${Math.round((it.confidence || 0) * 100)}%</span>
        </div>
        <div style="margin-top:8px;font-size:14px;color:var(--ts-ink-soft);line-height:1.5">${esc(it.rationale)}</div>
        ${(it.risks || []).length ? `<ul style="margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--ts-negative)">${it.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      </div>`).join("");
    box.innerHTML = `<div class="fade-in" style="margin-bottom:16px">
      <div style="font-size:13px;color:var(--ts-ink-soft);margin-bottom:10px;font-style:italic">${esc(res.summary || "")}</div>
      ${cards || `<div class="muted">No products to analyse.</div>`}
    </div>`;
  } catch (e) {
    box.innerHTML = errState(e.message || "Reasoning unavailable");
    btn.classList.remove("ictw-aitoggle--on");
  } finally {
    btn.disabled = false;
    if (subEl) subEl.textContent = subWas;
  }
}

// forward-demand cell: projected 90-day demand + shortfall flag (from /planning/demand)
function demandCell(it) {
  const d = DEMAND[it.name];
  if (!d) return `<span class="inv-next inv-next--none">—</span>`;
  const short = d.projected_shortfall > 0;
  const rate = d.usage_rate_per_day ? `${d.usage_rate_per_day}/d` : "0/d";
  return `<div style="line-height:1.35">
    <div style="font-weight:600">${Math.round(d.projected_demand)} <span style="font-weight:400;color:var(--ts-ink-mute)">in ${d.horizon_days}d</span></div>
    <div style="font-size:12px;color:${short ? "var(--ts-negative)" : "var(--ts-ink-mute)"}">${short ? `short ${Math.round(d.projected_shortfall)}` : "covered"} · ${rate}</div>
  </div>`;
}

function demandRec(it) {
  const d = DEMAND[it.name];
  if (!d || d.projected_shortfall <= 0 || !d.recommended_order_qty) return "";
  const by = d.order_by ? ` by ${fmtDate(d.order_by)}` : "";
  return ` <span style="color:var(--ts-ink-mute)">· Forecast: order ${d.recommended_order_qty}${by} to cover ${d.horizon_days}-day demand.</span>`;
}

// map our inventory product keys onto the PRODUCTS cache when present, else synth a cell
function invPid(it) {
  const hit = Object.keys(PRODUCTS).find((id) => (PRODUCTS[id].name || "") === it.name);
  if (hit) return hit;
  PRODUCTS["_inv_" + it.product] = { name: it.name, category: it.category, product_code: it.product };
  return "_inv_" + it.product;
}
