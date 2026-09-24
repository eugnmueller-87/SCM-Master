"use strict";
/* ============================================================================
   SCM Master — Ordering mask (device-as-a-service). Loads after plan.js.

   The owner's instruction: "if we start to purchase stuff, it needs to open a
   mask which tells me how much I need and because of which factors. I want to
   simulate, for example, 100 Fairphones. What will it look like?" Pick a model,
   a manufacturer or a class, see what is needed and why, type a quantity, and
   see the consequence before anything is ordered: does it fit, does new stock
   cross its capacity and when, what it costs, what it covers, and what would
   make room when there is none.

   Every figure comes from /order-mask and is computed in the database from the
   reads that already exist (the forecast, the inventory plan, the compartments,
   the capacity plan, the over-order guard). Nothing here places an order; the
   purchasing gate stays on the Requisitions tab. A figure without data says
   why instead of showing a zero.
============================================================================ */

ICONS.mask = '<path d="M8 4h8l2 2v14H6V6z"/><path d="M9 4v3h6V4"/><path d="M9 12h6M9 16h4"/>';
CRUMBS.order = "Ordering";

let OM = { scopes: null, mask: null, sel: { product_code: "", manufacturer: "", family: "" }, qty: "", busy: false };

const omEur = (v) => v == null ? "—" : "€" + Number(v).toLocaleString("de-DE", { maximumFractionDigits: 0 });
const omN1 = (v) => v == null ? "—" : Number(v).toLocaleString("de-DE", { maximumFractionDigits: 1 });
const omTile = (label, val, hint, cls = "") =>
  `<div class="wh-cnt__tile"><div class="wh-cnt__tile-label">${esc(label)}</div><div class="wh-cnt__tile-val">${val}</div>${hint ? `<div class="wh-note ${cls}">${hint}</div>` : ""}</div>`;
const omDerived = (title) => `<span class="wh-derived" title="${esc(title)}">derived</span>`;
const omMeasured = (title) => `<span class="wh-derived om-measured" title="${esc(title)}">measured</span>`;
const omPlaceholder = (title) => `<span class="wh-derived" title="${esc(title)}">placeholder</span>`;

/* the owner's examples, straight from his words; manufacturers are the catalogue */
const OM_EXAMPLES = [
  { label: "100 Fairphone 5", sel: { product_code: "FPH-FP5-256" }, qty: 100 },
  { label: "Google phones", sel: { manufacturer: "Google", family: "Smartphone" }, qty: "" },
  { label: "Apple laptops", sel: { manufacturer: "Apple", family: "Laptop" }, qty: "" },
  { label: "5.000 iPhone 16", sel: { product_code: "APL-IP16-128" }, qty: 5000 },
];

function omQuery() {
  const q = new URLSearchParams();
  Object.entries(OM.sel).forEach(([k, v]) => { if (v) q.set(k, v); });
  if (OM.qty !== "" && OM.qty != null && Number(OM.qty) > 0) q.set("quantity", String(Math.round(Number(OM.qty))));
  return q.toString();
}

async function omLoad() {
  const host = $("#om-answer");
  if (!host || OM.busy) return;
  OM.busy = true;
  host.innerHTML = `<div class="state"><div class="state__icon">${icon("gauge", 22)}</div><div class="state__sub">Composing the mask… the forecast, the inventory plan, the compartments, the capacity plan and the guard, read again now.</div></div>`;
  try {
    OM.mask = await api(`/order-mask?${omQuery()}`);
    host.innerHTML = omAnswerHtml(OM.mask);
    $$("#om-answer [data-om-qty]").forEach((b) => b.addEventListener("click", () => { OM.qty = b.dataset.omQty; const i = $("#om-qty"); if (i) i.value = OM.qty; omLoad(); }));
  } catch (e) {
    host.innerHTML = errState((e && e.message) || "Could not compose the mask");
  } finally {
    OM.busy = false;
  }
}

/* ── the factors: a table a person can add up ─────────────────────── */
function omFactorsHtml(r, d) {
  const rows = r.factors.map((f) => `<tr>
      <td class="om-sign">${f.sign}</td>
      <td class="num om-val">${num(Math.round(f.value))}</td>
      <td><div class="cell-prod__name">${esc(f.label)}</div><div class="wh-note">${esc(f.basis)}</div></td>
    </tr>`).join("");
  const guard = r.guard;
  const room = guard.free_to_order == null ? "no storage limit is defined, so nothing is deferred"
    : `the warehouse is ${whShare(guard.committed_pct)} committed and can take ${num(guard.free_to_order)} more devices, so <b>${num(r.orderable_now)}</b> can be ordered now and <b>${num(r.deferred)}</b> would wait for room`;
  return `<table class="tbl om-factors"><tbody>${rows}
      <tr class="om-sum"><td class="om-sign">=</td><td class="num om-val">${num(r.gap)}</td><td><div class="cell-prod__name">The gap</div><div class="wh-note">${esc(r.gap_basis)}</div></td></tr>
      <tr class="om-sum om-sum--rec"><td class="om-sign">→</td><td class="num om-val">${num(r.recommended)}</td><td><div class="cell-prod__name">Recommended to order</div><div class="wh-note">${esc(r.recommended_basis)}${r.order_by ? ` · order by ${fmtDate(r.order_by)} to cover the horizon at a lead time of ${num(d.lead_time_days)} days` : ""}</div></td></tr>
    </tbody></table>
    <div class="wh-note" style="margin-top:8px">${room}. ${esc(r.guard_for === "what_if" ? "The guard above was asked about the what-if quantity; this line is the recommendation against the same free places." : "The guard was asked about this recommendation.")}</div>
    <div class="wh-note om-cross">Cross-check · the forecast alone, without the buffer, says <b>${num(r.forecast_recommended)}</b>. The purchasing agent stages from the same factors netted the same way (planning.inventory_position), so its figure is the gap above.</div>`;
}

/* ── the tiers: what we already own that could serve this demand ──── */
function omTierHtml(t) {
  const c = t.cost || {};
  let cost = "";
  if (c.kind === "none") cost = `<div class="wh-note">${esc(c.text)}</div>`;
  else if (c.kind === "rent_share") cost = `<div class="wh-note">${esc(c.text)} Rent at <b>${whShare(c.value)}</b> of a first rental's ${omPlaceholder(c.basis)}</div>`;
  else if (c.kind === "defect_cover") cost = `<div class="wh-note">${esc(c.text)}</div><div class="wh-note">${c.value == null ? esc(c.reason || "") : `Today's buffer covers <b>${omN1(c.value)} months</b> of defects at ${omN1(c.defects_per_month)} a month`} ${omMeasured(c.basis)}${c.per_rented != null ? ` · ${whShare(c.per_rented)} of the ${num(c.rented)} rented` : ""}</div>`;
  else if (c.kind === "forgone_proceeds") cost = `<div class="wh-note">${esc(c.text)}</div><div class="wh-note">${c.value == null ? esc(c.reason || "") : `Forgoes <b>${omEur(c.value)}</b> a device, ${omEur(c.forgone_total)} for all ${num(t.units)}`} ${omMeasured(c.basis)}${c.grade_ab_share != null ? ` · grade A or B ${whShare(c.grade_ab_share)}` : ""}</div>`;
  const cycles = (t.cycles || []).map((x) => `${esc(x.label)} ${num(x.units)}`).join(" · ");
  return `<div class="om-tier${t.counted ? " om-tier--counted" : ""}">
    <div class="om-tier__head"><span class="om-tier__name">${esc(t.name)}</span>${plainPill(t.counted ? "counted" : "not counted", t.counted ? "positive" : "neutral")}</div>
    <div class="om-tier__n">${num(t.units)}<small>serves a ${esc(t.serves)}</small></div>
    <div class="wh-note">${t.units ? `median ${whDays(t.median_days)} · oldest ${whDays(t.oldest_days)}${t.past_target_units ? ` · <span class="wh-note--warn">${num(t.past_target_units)} past the target of ${t.target_dwell_days} d</span>` : ""}${t.undated_units ? ` · ${num(t.undated_units)} undated` : ""}` : "nothing of this scope here"}</div>
    ${cycles ? `<div class="wh-note">${cycles}</div>` : ""}
    ${cost}
  </div>`;
}

function omChainHtml(rc) {
  if (!rc.units) return `<div class="muted" style="font-size:12.5px">${esc(rc.reason || "Nothing in the return chain.")}</div>`;
  const rows = rc.compartments.filter((c) => c.units).map((c) => `<tr>
      <td><div class="cell-prod__name">${esc(c.name)}</div><div class="wh-note">target ${c.target_dwell_days} d (${esc(c.target_owner)}, placeholder)</div></td>
      <td class="num" style="font-weight:600">${num(c.units)}</td>
      <td class="num">${(c.cycles || []).map((x) => `${x.cycle === "0" ? "new" : x.cycle === "1" ? "1st" : "2nd+"} ${num(x.units)}`).join(" · ") || "—"}</td>
      <td class="num">${whDays(c.median_days)}</td>
      <td class="num">${num(c.expected_second_life)}</td>
      <td class="num">${c.horizon_days_min == null ? "—" : c.horizon_days_min === c.horizon_days_max ? `${num(c.horizon_days_min)} d` : `${num(c.horizon_days_min)} to ${num(c.horizon_days_max)} d`}</td>
    </tr>`).join("");
  return `<div class="wh-note" style="margin:0 0 8px"><b>${num(rc.units)}</b> of this scope are in the return chain; about <b>${num(rc.expected_second_life)}</b> are expected to reach second-life stock and <b>${num(rc.expected_sale)}</b> to be sold ${omDerived(rc.rule_basis)}. The horizon is the target dwell ahead on the paths the state machine allows ${omPlaceholder(rc.horizon_basis)}.</div>
    <table class="tbl om-chain"><thead><tr><th>Compartment</th><th class="num">Units</th><th class="num">Rentals</th><th class="num">Median here</th><th class="num">To second life</th><th class="num">Horizon</th></tr></thead><tbody>${rows}</tbody></table>`;
}

/* ── the what-if ──────────────────────────────────────────────────── */
function omWhatIfHtml(w, r) {
  const g = w.guard, i = w.intake, p = w.plan, c = w.cost, cv = w.covers;
  const verdict = g.verdict === "ok"
    ? { tone: "positive", title: `${num(w.quantity)} fit`, sub: `the warehouse can take ${num(g.free_to_order == null ? w.quantity : g.free_to_order)} more devices; with this order it stands at ${whShare(w.warehouse.committed_pct_with)} committed` }
    : g.verdict === "clamp"
      ? { tone: "warning", title: `Only ${num(g.allowed)} of ${num(w.quantity)} fit`, sub: `the warehouse is ${whShare(g.committed_pct)} committed and can take ${num(g.free_to_order)} more; the guard would clamp or refuse this order` }
      : { tone: "negative", title: "No room", sub: `the warehouse is ${whShare(g.committed_pct)} committed; the guard refuses any order until places are freed` };
  const t = TONE[verdict.tone];
  const bar = (val, cap) => cap ? `<div class="cap-bar" style="width:100%"><div class="cap-bar__fill" style="width:${Math.min(1, val / cap) * 100}%;background:${capTone(val / cap, val > cap)}"></div></div>` : "";
  const intake = i.capacity == null
    ? omTile("New stock, static", `${num(i.committed_with)}<small>committed</small>`, esc(i.capacity_reason || "no capacity"))
    : omTile("New stock, static", `${num(i.committed_with)}<small>of ${num(i.capacity)} · ${whShare(i.utilisation_with)}</small>`,
        `${bar(i.committed_with, i.capacity)}${num(i.on_hand)} on hand + ${num(i.inbound)} inbound + ${num(w.quantity)} = ${num(i.committed_with)}${i.over_with ? ` · <b>${num(i.over_with)} over</b>${i.over_now ? ` (${num(i.over_now)} over already)` : ""}` : " · fits"} <span class="wh-derived" title="${esc(i.static_basis)}">static</span>`,
        i.over_with ? "wh-note--warn" : "");
  const eta = i.stock_at_eta_with == null
    ? omTile(`On delivery, ${fmtDate(i.eta)}`, `<span class="wh-na">n/a</span>`, esc(i.outflow_basis || ""))
    : omTile(`On delivery, ${fmtDate(i.eta)}`, `${num(i.stock_at_eta_with)}<small>of ${num(i.capacity)}</small>`,
        `${bar(i.stock_at_eta_with, i.capacity)}${num(i.stock_at_eta_without)} left after ${num(i.lead_time_days)} days at ${omN1(i.outflow_per_day)} out a day + ${num(i.inbound_due_by_eta)} due by then + ${num(w.quantity)}${i.over_at_eta ? ` · <b>${num(i.over_at_eta)} over</b>` : " · fits"} ${omMeasured(i.outflow_basis)} <span class="wh-derived" title="${esc(i.drained_basis)}">drained</span>`,
        i.over_at_eta ? "wh-note--warn" : "");
  const planState = p.new_stock_state ? (CP_STATE[p.new_stock_state] || CP_STATE.unknown) : CP_STATE.unknown;
  const planLabel = p.new_stock_state === "later" ? cpMonth(p.new_stock_month) : planState.label;
  const withLabel = p.with_order_state === "breaks_at_delivery" ? `breaks ${cpMonth(p.with_order_month)}` : p.with_order_state === "fits_at_delivery" ? "fits at delivery" : "n/a";
  const plan = omTile("Capacity plan, new stock", `<span style="color:${TONE[planState.tone].fg}">${esc(planLabel)}</span><small>${icon("arrow", 11)} ${esc(withLabel)}</small>`,
    `${p.first_breach ? `first to break: ${esc(p.first_breach.name)} (${p.first_breach.over_capacity_today ? "over today" : cpMonth(p.first_breach.month)})` : "nothing breaks in the plan"}${p.earlier_than_plan ? " · <b>this order breaks new stock earlier than the plan</b>" : ""} <span class="wh-derived" title="${esc(p.basis)}">plan</span>`,
    p.earlier_than_plan ? "wh-note--warn" : "");
  const costV = { under_cap: "positive", human: "warning", escalate: "negative" }[c.verdict] || "mute";
  const cost = omTile("What it costs", c.verdict == null ? `<span class="wh-na">unpriced</span>` : `${omEur(c.total)}<small>${omEur(c.landed)} landed</small>`,
    `${plainPill(c.verdict === "under_cap" ? "under the auto-place cap" : c.verdict === "human" ? "a human approves" : c.verdict === "escalate" ? "escalates" : "unpriced", costV)} ${esc(c.reason)} ${omPlaceholder(c.adder_basis)}`);
  const covers = omTile("What it covers", cv.days_of_demand == null ? `<span class="wh-na">n/a</span>` : `${omN1(cv.days_of_demand)}<small>days of demand</small>`,
    cv.rate_reason ? esc(cv.rate_reason)
      : `${cv.verdict === "no_gap" ? `there is no gap: ${omN1(cv.cover_days_now)} days of cover already` : cv.verdict === "covers" ? `covers the recommended ${num(cv.recommended)}${cv.vs_gap > 0 ? `, ${num(cv.vs_gap)} beyond the need` : ""}` : `<b>${num(-cv.vs_gap)} short</b> of the recommended ${num(cv.recommended)}`} · cover after arrival ${omN1(cv.cover_days_after)} days (${omN1(cv.cover_days_now)} now)`,
    cv.verdict === "short" ? "wh-note--warn" : "");
  const room = w.room ? `<div class="om-room"><div class="wh-detail__head">What would make room · ${num(w.room.needed)} places short</div>
      <div class="om-room__grid">${w.room.levers.map((l) => `<div class="om-lever"><div class="om-lever__n">${num(l.units)}</div><div class="cell-prod__name">${esc(l.label)}</div><div class="wh-note">${esc(l.detail)}</div></div>`).join("")}</div>
      <div class="wh-note">${esc(w.room.basis)}</div></div>` : "";
  const split = w.split.length > 1 ? `<div class="wh-detail__head" style="margin-top:14px">How the quantity splits over the scope</div>
      <table class="tbl om-split"><thead><tr><th>Model</th><th class="num">Units</th><th class="num">Unit price</th><th class="num">Cost</th><th class="num">MOQ</th><th class="num">Lead time</th></tr></thead>
      <tbody>${w.split.map((s) => `<tr><td>${esc(s.name)}</td><td class="num" style="font-weight:600">${num(s.units)}</td><td class="num">${omEur(s.unit_price)}</td><td class="num">${omEur(s.cost)}</td><td class="num${s.moq_short ? " wh-note--warn" : ""}">${num(s.moq)}${s.moq_short ? " · below" : ""}</td><td class="num">${num(s.lead_time_days)} d</td></tr>`).join("")}</tbody></table>
      <div class="wh-note">in proportion to each model's gap; a model without a gap gets nothing</div>` : (w.split[0] && w.split[0].moq_short ? `<div class="wh-note wh-note--warn" style="margin-top:8px">${num(w.quantity)} is below the minimum order quantity of ${num(w.split[0].moq)}: the source would round it up.</div>` : "");
  return `<div class="om-verdict" style="background:${t.bg};color:${t.fg}"><span class="om-verdict__icon">${icon(verdict.tone === "positive" ? "check" : "alert", 18)}</span><div><b>${esc(verdict.title)}</b> · ${verdict.sub}</div></div>
    <div class="wh-cnt__strip om-strip">${intake}${eta}${plan}${cost}${covers}</div>
    ${room}${split}`;
}

/* ── the whole answer ─────────────────────────────────────────────── */
function omAnswerHtml(m) {
  if (m.scenario !== "daas") return errState(m.reason || "No ordering mask in this database.");
  const r = m.recommendation, d = m.demand, o = m.owned, ib = m.inbound, w = m.what_if;
  const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;
  const notCounted = Object.values(r.not_counted || {}).reduce((a, b) => a + b, 0);
  const g = r.guard;
  const stats = `<div class="stats stats--5">
    ${stat("Recommended to order", "mask", num(r.recommended), r.recommended ? `gap ${num(r.gap)}, rounded to the MOQ · ${num(r.orderable_now)} orderable now` : (d.gross ? "the stock and the inbound cover the need" : esc(d.reason || "no demand measured")), r.deferred ? "stat__hint--neg" : "stat__hint--pos", "stat__val--gold")}
    ${stat(`Needed over ${d.horizon_days} days`, "trend", num(r.need), `${num(d.gross)} demand + ${num(r.buffer)} buffer · ${omN1(d.rate_per_day)} a day`)}
    ${stat("Can go out next", "check", num(o.counted_total), `${num(r.new)} new · ${num(r.second_life)} second-life · ${r.cover_days_now == null ? "" : `${omN1(r.cover_days_now)} days of cover with the inbound`}`)}
    ${stat("Also owned, not counted", "layers", num(notCounted), Object.entries(r.not_counted || {}).map(([k, v]) => `${num(v)} ${k === "ST-SELL" ? "sellable" : k === "ST-SWAP" ? "swap buffer" : k}`).join(" · ") || "nothing")}
    ${stat("Room", "box", g.free_to_order == null ? "no limit" : num(g.free_to_order), g.free_to_order == null ? "no storage capacity defined" : `more devices fit · the warehouse is ${whShare(g.committed_pct)} committed`, g.committed_pct > 0.95 ? "stat__hint--neg" : "")}
  </div>`;
  const inboundRows = ib.lines.map((l) => `<tr>
      <td class="nowrap"><span class="ref">${esc(l.order_number)}</span></td><td>${esc(l.product)}</td>
      <td class="num" style="font-weight:600">${num(l.outstanding)}${l.received ? `<div class="wh-note" style="margin:0">${num(l.received)} of ${num(l.ordered)} in</div>` : ""}</td>
      <td class="num">${l.eta ? fmtDate(l.eta) : `<span class="wh-na">no ETA</span>`}</td>
      <td>${l.late == null ? "—" : l.late ? plainPill(`${num(-l.days_to_eta)} d late`, "negative") : plainPill(`in ${num(l.days_to_eta)} d`, "neutral")}</td></tr>`).join("");
  const products = r.products.length > 1 ? `<div class="section">
      <div class="section__head"><span class="section__title">Per model</span><span class="section__count">${r.products.length}</span><span class="section__hint">the same factors, one row per model in the scope</span></div>
      <div class="panel" style="overflow-x:auto"><table class="tbl om-products"><thead><tr><th>Model</th><th class="num">Usage</th><th class="num">End of life</th><th class="num">Buffer</th><th class="num">New</th><th class="num">Second-life</th><th class="num">Inbound</th><th class="num">Staged</th><th class="num">Gap</th><th class="num">MOQ</th><th class="num">Recommended</th><th class="num">Price</th></tr></thead>
      <tbody>${r.products.map((p) => `<tr><td><div class="cell-prod__name">${esc(p.name)}</div><div class="wh-note">${esc(p.manufacturer || "")} · ${esc(p.family || "")}${p.abc_class ? ` · class ${esc(p.abc_class)}` : ""}</div></td>
        <td class="num">${num(Math.round(p.usage))}</td><td class="num">${num(p.eol)}</td><td class="num">${num(p.buffer)}</td><td class="num">${num(p.new)}</td><td class="num">${num(p.second_life)}</td><td class="num">${num(p.inbound)}</td><td class="num">${num(p.staged)}</td>
        <td class="num" style="font-weight:600">${num(p.gap)}</td><td class="num">${num(p.moq)}</td><td class="num" style="font-weight:600">${num(p.recommended)}</td><td class="num">${omEur(p.unit_price)}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  return `${stats}
    ${w ? `<div class="section"><div class="section__head"><span class="section__title">What if ${num(w.quantity)}</span><span class="section__hint">the guard, the intake compartment now and on delivery, the capacity plan, the price, the cover · nothing is ordered</span></div><div class="panel" style="padding:16px 18px">${omWhatIfHtml(w, r)}</div></div>` : ""}
    <div class="content--rail om-rail">
      <div>
        <div class="section">
          <div class="section__head"><span class="section__title">Why this number</span><span class="section__hint">each factor with its basis; add them up</span></div>
          <div class="panel" style="padding:12px 18px 14px">${omFactorsHtml(r, d)}</div>
        </div>
        <div class="section">
          <div class="section__head"><span class="section__title">What we already own that could serve this demand</span><span class="section__count">${num(o.rentable_total)}</span><span class="section__hint" title="${esc(o.rentable_basis)}">tiers read off the state machine · counted: ${esc(o.counted_basis)}</span></div>
          <div class="om-tiers">${o.tiers.map(omTierHtml).join("")}</div>
        </div>
        <div class="section" style="margin-bottom:0">
          <div class="section__head"><span class="section__title">In the return chain, on a horizon</span><span class="section__count">${num(o.return_chain.units)}</span></div>
          <div class="panel" style="padding:14px 18px">${omChainHtml(o.return_chain)}</div>
        </div>
      </div>
      <aside class="rail">
        <div class="rail__head"><span class="rail__dot"></span> What is coming in</div>
        <div class="panel" style="padding:12px 16px;margin-bottom:18px">
          ${ib.lines.length ? `<div class="wh-note" style="margin:0 0 8px"><b>${num(ib.units)}</b> on ${num(ib.lines.length)} open line${ib.lines.length === 1 ? "" : "s"}${ib.late_units ? ` · <b>${num(ib.late_units)} late</b>` : ""} · next ${fmtDate(ib.next_eta)}</div>
             <table class="tbl om-lines"><thead><tr><th>Order</th><th>Model</th><th class="num">Open</th><th class="num">ETA</th><th></th></tr></thead><tbody>${inboundRows}</tbody></table>`
           : `<div class="muted" style="font-size:12.5px">${esc(ib.reason || "Nothing inbound.")}</div>`}
        </div>
        <div class="rail__head">Demand, measured</div>
        <div class="panel" style="padding:6px 16px;margin-bottom:18px">
          ${[["Rental starts a day", omN1(d.rate_per_day)], ["Over the horizon", num(Math.round(d.usage))], ["End-of-life replacements", num(d.eol)], ["Method", esc(d.method || "—")], ["Lead time", `${num(d.lead_time_days)} d`], ["Order by", d.order_by ? fmtDate(d.order_by) : "—"]]
            .map(([k, v], i, arr) => `<div class="prov__row"${i === arr.length - 1 ? ' style="border-bottom:none"' : ""}><span class="prov__k">${k}</span><span class="prov__v" style="font-weight:600">${v}</span></div>`).join("")}
        </div>
        <div class="wh-note">${esc(d.basis)}. Read in ${num(m.timing_ms.total)} ms: scope ${num(m.timing_ms.scope)}, demand ${num(m.timing_ms.demand)}, owned ${num(m.timing_ms.owned)}, guard and plan ${num(m.timing_ms.guard_and_plan)}.</div>
      </aside>
    </div>
    ${products}`;
}

/* ── the form ─────────────────────────────────────────────────────── */
function omFormHtml() {
  const s = OM.scopes;
  const opt = (list, cur, blank) => `<option value="">${esc(blank)}</option>` + list.map((v) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  return `<form class="om-form" id="om-form">
    <div class="field"><label class="field__label" for="om-product">Model</label>
      <select class="input" id="om-product" name="product_code"><option value="">any model</option>${s.products.map((p) => `<option value="${esc(p.code)}"${p.code === OM.sel.product_code ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>
    <div class="field"><label class="field__label" for="om-maker">Manufacturer</label><select class="input" id="om-maker" name="manufacturer">${opt(s.manufacturers, OM.sel.manufacturer, "any manufacturer")}</select></div>
    <div class="field"><label class="field__label" for="om-family">Class</label><select class="input" id="om-family" name="family">${opt(s.families, OM.sel.family, "any class")}</select></div>
    <div class="field om-form__qty"><label class="field__label" for="om-qty">What if we order</label><input class="input" id="om-qty" name="quantity" type="number" min="0" step="1" placeholder="devices" value="${esc(OM.qty)}" /></div>
    <button type="submit" class="btn btn--ink">${icon("mask", 14)} Open the mask</button>
    <div class="om-examples">${OM_EXAMPLES.map((e, i) => `<button type="button" class="btn btn--ghost btn--sm" data-om-example="${i}">${esc(e.label)}</button>`).join("")}</div>
  </form>`;
}

RENDER.order = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `${pageHead("Purchasing", "Ordering mask", "What is needed and why, before anything is ordered.")}
      <div class="panel"><div class="state"><div class="state__icon">${icon("mask", 22)}</div><div class="state__title">No ordering mask here</div>
      <div class="state__sub">This database holds the datacenter operation; the mask reads the device fleet's compartments. The New order dialog on the Requisitions tab serves the datacenter flow.</div></div></div>`;
    return;
  }
  try {
    OM.scopes = await api("/order-mask/scopes");
    // A mask can be linked to: /?product_code=FPH-FP5-256&quantity=100#order opens it filled in.
    const q = new URLSearchParams(location.search);
    if (q.has("product_code") || q.has("manufacturer") || q.has("family")) {
      OM.sel = { product_code: q.get("product_code") || "", manufacturer: q.get("manufacturer") || "", family: q.get("family") || "" };
      OM.qty = q.get("quantity") || "";
    }
    screen.innerHTML = `
      ${pageHead("Purchasing", "Ordering mask", "Pick a model, a manufacturer or a class. The mask says how much is needed and because of which factors, what we already own that could serve it and from which compartment, what is coming in, and, for a quantity you type, whether it fits, what it does to new stock and the capacity plan, what it costs and what it covers. Nothing here places an order.")}
      <div class="panel om-panel">${omFormHtml()}</div>
      <div id="om-answer"><div class="state"><div class="state__icon">${icon("mask", 22)}</div><div class="state__title">Open the mask</div><div class="state__sub">Choose a scope, or take one of the owner's examples.</div></div></div>`;
    const read = () => {
      OM.sel = { product_code: $("#om-product").value, manufacturer: $("#om-maker").value, family: $("#om-family").value };
      OM.qty = $("#om-qty").value;
    };
    $("#om-form").addEventListener("submit", (e) => { e.preventDefault(); read(); omLoad(); });
    $$("[data-om-example]").forEach((b) => b.addEventListener("click", () => {
      const ex = OM_EXAMPLES[Number(b.dataset.omExample)];
      OM.sel = { product_code: "", manufacturer: "", family: "", ...ex.sel };
      OM.qty = ex.qty;
      $("#om-product").value = OM.sel.product_code; $("#om-maker").value = OM.sel.manufacturer; $("#om-family").value = OM.sel.family; $("#om-qty").value = OM.qty;
      omLoad();
    }));
    if (OM.sel.product_code || OM.sel.manufacturer || OM.sel.family) omLoad();
  } catch (e) { screen.innerHTML = errState(e.message); }
};
