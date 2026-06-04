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
(function () { const i = NAV.findIndex((n) => n.id === "capacity"); NAV.splice(i + 1, 0, { id: "inventory", label: "Inventory", icon: "stock" }); })();

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

/* ── render ────────────────────────────────────────────────────────── */
RENDER.inventory = async function () {
  if (!INV_LOADED) { await loadInventory(); }
  const plans = INVENTORY.map((it) => ({ it, p: plan(it) }));
  const reorder = plans.filter((x) => ["Reorder", "Stock-out risk", "Expedite"].includes(x.p.status)).length;
  const over = plans.filter((x) => x.p.status === "Overstock risk").length;

  const legend = `<div class="inv-legend">
    <div class="inv-legend__item"><span class="inv-legend__sw inv-legend__sw--onhand"></span>On hand</div>
    <div class="inv-legend__item"><span class="inv-legend__sw inv-legend__sw--onorder"></span>On order (inbound)</div>
    <div class="inv-legend__item"><span class="inv-legend__tick"></span>Reorder point</div>
    <div class="inv-legend__item" style="margin-left:auto;color:var(--ts-ink-faint)">Bar spans 0 → warehouse capacity per item</div>
  </div>`;

  const rows = plans.map(({ it, p }) => {
    const t = TONE[p.tone];
    const ohPct = Math.min(it.on_hand / it.capacity, 1) * 100;
    const ooW = Math.min(it.on_order / it.capacity, 1 - it.on_hand / it.capacity) * 100;
    const ropPct = Math.min(p.rop / it.capacity, 1) * 100;
    const ohColor = p.tone === "negative" ? "var(--ts-negative)" : p.tone === "warning" ? "var(--ts-warning)" : p.tone === "info" ? "var(--ts-info)" : "var(--ts-positive)";
    const coverColor = p.cover <= it.lead_time_days ? "var(--ts-negative)" : p.cover < it.lead_time_days * 1.6 ? "#8C6510" : "var(--ts-ink)";
    return `<tr>
      <td>${productCell(invPid(it))}</td>
      <td class="muted">${esc(it.location)}</td>
      <td>
        <div class="inv-bar-wrap">
          <div class="inv-bar">
            <div class="inv-bar__onhand" style="width:${ohPct}%;background:${ohColor}"></div>
            ${it.on_order > 0 ? `<div class="inv-bar__onorder" style="left:${ohPct}%;width:${Math.max(ooW, 0)}%"></div>` : ""}
            <div class="inv-bar__rop" style="left:${ropPct}%" title="Reorder point ${p.rop}"></div>
          </div>
          <div class="inv-scale"><span>${it.on_hand} on hand${it.on_order ? ` · +${it.on_order} inbound` : ""}</span><span>cap ${it.capacity}</span></div>
        </div>
      </td>
      <td class="num"><span class="inv-cover" style="color:${coverColor}">${p.cover > 365 ? "365+" : p.cover}d</span></td>
      <td>${demandCell(it)}</td>
      <td>${p.etaDays != null ? `<span class="inv-next">${icon("truck", 14)} ${p.etaDays}d · ${shortDate(it.next_eta)}</span>` : `<span class="inv-next inv-next--none">none scheduled</span>`}</td>
      <td>${plainPill(p.status, p.tone)}</td>
    </tr>
    <tr><td colspan="7" style="padding-top:0;border-bottom:1px solid var(--ts-line)"><div class="inv-rec" style="color:${t.fg}">${esc(p.rec)}${demandRec(it)}</div></td></tr>`;
  }).join("");

  $("#screen").innerHTML = `<div class="fade-in">
    ${pageHead("Planning", "Inventory & reorder", "Stock against warehouse capacity for every item, with the reorder point and the next delivery date — so you can see what to order, how much, and what not to overstock.")}
    <div class="tkpis">
      <div class="tkpi"><div class="tkpi__label">Items tracked</div><div class="tkpi__val">${INVENTORY.length}</div></div>
      <div class="tkpi"><div class="tkpi__label">Need reordering</div><div class="tkpi__val tkpi__val--neg">${reorder}</div></div>
      <div class="tkpi"><div class="tkpi__label">Overstock risk</div><div class="tkpi__val" style="color:#8C6510">${over}</div></div>
      <div class="tkpi"><div class="tkpi__label">On order, inbound</div><div class="tkpi__val tkpi__val--info">${INVENTORY.filter((i) => i.on_order > 0).length}</div></div>
    </div>
    ${legend}
    <div class="panel"><table class="tbl">
      <thead><tr><th>Item</th><th>Held at</th><th style="width:260px">Stock vs capacity</th><th class="num">Cover</th><th>90-day demand</th><th>Next delivery</th><th>Action</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </div>`;
};

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
