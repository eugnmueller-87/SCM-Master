"use strict";
/* ============================================================================
   SCM Master — Customers (device-as-a-service). Loads after returns.js.

   The rented fleet and when it comes back, so a reorder can be timed. At the top,
   how many devices are out and at how many customers. Then one month grid: what
   comes back from customers (the planned end of every running contract, overdue
   ones in their own row) beside what open purchase orders bring in the same month.
   Then every customer, most devices first; a row opens to its devices by model,
   its returns by month and the contracts ending in the next 90 days. Last, the
   defects and repairs the data records, month by month.

   Every figure is counted by the backend (services/rented_fleet.py); nothing on
   this page is estimated, and what the data cannot tell is said, not padded.

   Two reads feed the page: /fleet/customers (the customer and contract tiles are
   its totals, counted over the rows beneath them; rented out now is every RENTED
   device, at a listed customer or not) and /fleet/inflow (the inbound tile, the
   month grid, which counts every running contract whichever organisation holds
   it, and the defects). Each can fail on its own and leaves the other half
   standing, with a way to load it again. A render the user has left, or that a
   newer one replaced, paints nothing.
============================================================================ */

CRUMBS.customers = "Customers";
ICONS.users = '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18.5 14.2a6.5 6.5 0 0 1 3 5.8"/>';

const CU_ENDING_SHOWN = 50;    // contracts listed in an opened customer; the count above the list is the full one
const cuMonth = (ym) => new Date(ym + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
const cuMonthShort = (ym) => new Date(ym + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "2-digit" });

/* the latest render of this tab; an older one that finishes late must not paint over it */
let cuRenderSeq = 0;
const cuLeft = () => currentTab !== "customers";

/* the categories present in a list of {category: n} maps, largest first */
function cuCats(maps) {
  const tot = {};
  maps.forEach((m) => Object.entries(m || {}).forEach(([k, v]) => { tot[k] = (tot[k] || 0) + v; }));
  return Object.keys(tot).sort((a, b) => tot[b] - tot[a] || a.localeCompare(b));
}

const cuMix = (m) => Object.entries(m || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="cu-chip">${esc(k)} ${num(v)}</span>`).join(" ") || "—";

/* a part of the page whose read failed: say which, and offer to load the tab again */
const cuFailed = (what, err) => `<div class="panel" style="padding:14px 16px">
    <span class="muted">Could not load ${esc(what)}: ${esc((err && err.message) || "the request failed")}.</span>
    <button type="button" class="btn btn--ghost btn--sm cu-reload" style="margin-left:8px">Retry</button>
  </div>`;

/* the month grid: returns from customers beside inbound from suppliers */
function cuGrid(O) {
  const G = O.grid || [];
  const rc = cuCats(G.map((g) => g.returns_by_category).concat([O.returns_overdue_by_category]));
  // every bucket with a category split names its columns, the one after the grid included
  const ic = cuCats(G.map((g) => g.inbound_by_category)
    .concat([O.inbound_late.by_category, O.inbound_no_eta.by_category, O.inbound_after_window.by_category]));
  // the bar compares single months; the bucket after the grid spans years and gets none
  const maxLanding = Math.max(1, ...G.map((g) => g.landing), O.returns_overdue + O.inbound_late.units);
  const cells = (m, cats) => cats.map((c) => `<td class="num muted">${m && m[c] ? num(m[c]) : "—"}</td>`).join("");
  const bar = (v) => v == null ? `<td></td>`
    : `<td><div class="spendbar-track" style="width:90px"><div class="spendbar-fill" style="width:${Math.min(100, v / maxLanding * 100)}%"></div></div></td>`;
  const row = (label, back, backBy, inb, inbBy, cls = "", withBar = true) => `
    <tr class="${cls}">
      <td>${label}</td>
      <td class="num"><b>${back == null ? "—" : num(back)}</b></td>${cells(backBy, rc)}
      <td class="num"><b>${inb == null ? "—" : num(inb)}</b></td>${cells(inbBy, ic)}
      <td class="num"><b>${num((back || 0) + (inb || 0))}</b></td>${bar(withBar ? (back || 0) + (inb || 0) : null)}
    </tr>`;
  const last = G.length ? cuMonth(G[G.length - 1].month) : "";
  const body = [
    row(plainPill("overdue / late", "negative"), O.returns_overdue, O.returns_overdue_by_category,
        O.inbound_late.units, O.inbound_late.by_category, "cu-bucket"),
    ...G.map((g) => row(`<span class="ref">${cuMonth(g.month)}</span>`, g.returns, g.returns_by_category, g.inbound, g.inbound_by_category)),
    row(`<span class="muted">after ${esc(last)}</span>`, O.returns_after_window, null, O.inbound_after_window.units,
        O.inbound_after_window.by_category, "cu-bucket", false),
  ];
  if (O.inbound_no_eta.units) body.push(row(`<span class="muted">no delivery date</span>`, null, null, O.inbound_no_eta.units, O.inbound_no_eta.by_category, "cu-bucket", false));
  return `<div class="panel cu-scroll"><table class="tbl cu-grid">
    <thead>
      <tr><th></th><th class="cu-group" colspan="${1 + rc.length}">Back from customers</th><th class="cu-group" colspan="${1 + ic.length}">From suppliers, open POs</th><th class="cu-group" colspan="2">Landing</th></tr>
      <tr><th>Month</th><th class="num">Devices</th>${rc.map((c) => `<th class="num">${esc(c)}</th>`).join("")}<th class="num">Units</th>${ic.map((c) => `<th class="num">${esc(c)}</th>`).join("")}<th class="num">Total</th><th></th></tr>
    </thead>
    <tbody>${body.join("")}</tbody>
  </table></div>`;
}

/* one customer, opened */
function cuDetailHtml(d) {
  const models = d.by_model.length
    ? `<table class="tbl"><thead><tr><th>Model</th><th>Category</th><th class="num">Devices</th></tr></thead><tbody>
        ${d.by_model.map((m) => `<tr><td><div class="cell-prod__name">${esc(m.product)}</div><div class="cell-prod__cat">${esc(m.product_code)}</div></td><td class="muted">${esc(m.category || "")}</td><td class="num" style="font-weight:600">${num(m.devices)}</td></tr>`).join("")}
      </tbody></table>`
    : `<div class="muted">No device at this customer now.</div>`;
  const cats = cuCats(d.returns_schedule.map((m) => m.by_category));
  const months = `<table class="tbl"><thead><tr><th>Month</th><th class="num">Back</th>${cats.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr></thead><tbody>
      ${d.returns_overdue ? `<tr class="cu-bucket"><td>${plainPill("overdue", "negative")}</td><td class="num" style="font-weight:600">${num(d.returns_overdue)}</td>${cats.map(() => `<td></td>`).join("")}</tr>` : ""}
      ${d.returns_schedule.map((m) => `<tr><td><span class="ref">${cuMonth(m.month)}</span></td><td class="num" style="font-weight:600">${m.devices ? num(m.devices) : "—"}</td>${cats.map((c) => `<td class="num muted">${m.by_category[c] ? num(m.by_category[c]) : "—"}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;
  const ending = d.contracts_ending.length
    ? `<table class="tbl"><thead><tr><th>Serial</th><th>Device</th><th class="num">Rental</th><th class="num">Planned end</th><th class="num">Days left</th></tr></thead><tbody>
        ${d.contracts_ending.map((c) => `<tr><td><span class="ref">${esc(c.serial_number)}</span></td><td>${esc(c.product)}<div class="cell-prod__cat">${esc(c.category || "")}</div></td><td class="num muted">${c.cycle_no >= 2 ? "2nd" : "1st"} · ${num(c.term_months)} mo</td><td class="num">${fmtDate(c.planned_end)}</td><td class="num" style="font-weight:600">${num(c.days_left)}</td></tr>`).join("")}
      </tbody></table>
      ${d.contracts_ending_90d > d.contracts_ending_shown ? `<div class="wh-note">The first ${num(d.contracts_ending_shown)} of ${num(d.contracts_ending_90d)}, soonest first; the month table beside counts them all.</div>` : ""}`
    : `<div class="muted">No contract of this customer ends in the next 90 days.</div>`;
  return `<div class="wh-cnt">
    <div class="wh-cnt__grid">
      <div class="wh-cnt__col"><div class="wh-cnt__block"><div class="wh-detail__head">Devices by model · ${num(d.devices_at_customer)} on ${num(d.by_model.length)} model${d.by_model.length === 1 ? "" : "s"}</div>${models}</div></div>
      <div class="wh-cnt__col"><div class="wh-cnt__block"><div class="wh-detail__head">Back by month</div>${months}</div></div>
    </div>
    <div class="wh-cnt__block"><div class="wh-detail__head">Contracts ending in the next 90 days · ${num(d.contracts_ending_90d)}</div>${ending}</div>
  </div>`;
}

/* Load one customer into its opened row. Marked loaded only when it arrived: after a
   failure, the Retry button and closing and opening the row both try again. */
async function cuLoadDetail(code, host) {
  host.dataset.loading = "1";
  host.innerHTML = `<span class="muted">Loading…</span>`;
  let html = null, err = null;
  try {
    html = cuDetailHtml(await api(`/fleet/customers/${encodeURIComponent(code)}?limit=${CU_ENDING_SHOWN}`));
  } catch (e) { err = e; }
  delete host.dataset.loading;
  if (cuLeft() || !host.isConnected) return;        // the user moved on while it loaded
  if (html != null) { host.innerHTML = html; host.dataset.loaded = "1"; return; }
  host.innerHTML = `<span class="muted">${esc((err && err.message) || "Could not load")}</span>
    <button type="button" class="btn btn--ghost btn--sm cu-retry" style="margin-left:8px">Retry</button>`;
  host.querySelector(".cu-retry").addEventListener("click", () => cuLoadDetail(code, host));
}

RENDER.customers = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `<div class="state"><div class="state__icon">${icon("users", 22)}</div><div class="state__title">No rented fleet in this database</div><div class="state__sub">Customers holding devices exist in the device-as-a-service scenario. This database holds the datacenter operation.</div></div>`;
    return;
  }
  const seq = ++cuRenderSeq;
  // The two reads are independent: one that fails must not blank the other's half of the page.
  const [rO, rC] = await Promise.allSettled([api("/fleet/inflow"), api("/fleet/customers")]);
  if (seq !== cuRenderSeq || cuLeft()) return;      // the user moved on, or a newer render took over
  const O = rO.status === "fulfilled" ? rO.value : null;
  const C = rC.status === "fulfilled" ? rC.value : null;

  const stat = (label, ic, val, hint, hintCls = "", title = "") =>
    `<div class="stat"${title ? ` title="${esc(title)}"` : ""}><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;

  // The customer and contract tiles are the customers table's totals: a tile and the column beneath it are one
  // count. Rented out now is every RENTED device, at a listed customer or not.
  const T = C ? C.totals : null;
  const notLoaded = "not loaded, see below";
  // The grid's overdue row counts every running contract, the tile the customers below. An organisation flagged as
  // a supplier or a manufacturer can hold a running contract; the tile then says how many more the grid holds.
  const overdueElsewhere = T && O ? Math.max(0, O.returns_overdue - T.returns_overdue) : 0;
  const overdueHint = !T ? notLoaded
    : (T.returns_overdue ? "contract ended, device still out, at the customers below" : "every return on time at the customers below")
      + (overdueElsewhere ? ` · ${num(overdueElsewhere)} more at organisations not listed` : "");
  const overdueCls = !T ? "" : (T.returns_overdue || overdueElsewhere ? "stat__hint--neg" : "stat__hint--pos");
  const stats = `<div class="stats stats--5">
      ${stat("Rented out now", "box", num(T ? T.rented_total : null), T ? "status RENTED: at a customer, under a running contract" : notLoaded, "", C ? C.definition : "")}
      ${stat("Customers holding them", "users", num(T ? T.customers_holding_devices : null), T ? `of ${num(T.customers)} customers${T.devices_without_customer ? ` · ${num(T.devices_without_customer)} devices not at a listed customer` : ""}` : notLoaded)}
      ${stat("Back in 90 days", "return", num(T ? T.contracts_ending_90d : null), T ? "planned contract ends at the customers below, today to day 90" : notLoaded)}
      ${stat("Overdue returns", "alert", num(T ? T.returns_overdue : null), overdueHint, overdueCls)}
      ${stat("Inbound on open POs", "truck", num(O ? O.inbound_open_total : null), O ? `${num(O.inbound_open_lines)} open lines${O.inbound_late.units ? ` · ${num(O.inbound_late.units)} late` : ""}` : notLoaded, O && O.inbound_late.units ? "stat__hint--neg" : "")}
    </div>`;

  const grid = O ? `${cuGrid(O)}
      <div class="wh-foot"><b>Landing</b> is the volume that physically arrives at the warehouse, not stock that covers demand. The ordering mask subtracts stock on hand and open orders, not devices still to come back from customers; the capacity plan does count returns, as rented out again unless they are sold or recycled. This table does not say how many of the returns will be rented out again.</div>
      <div class="wh-foot">A return lands when the customer sends the device back; its month is the contract's planned end, and an overdue device is counted once, in the first row. The grid counts every running contract, whichever organisation holds it. From suppliers: ordered minus received on open order lines; a line is late when its estimated delivery date has passed. Staged requisitions are not orders and are not counted.</div>`
    : cuFailed("the month grid", rO.reason);

  let customers;
  if (C) {
    const rows = C.rows || [];
    const maxDev = Math.max(1, ...rows.map((r) => r.devices_at_customer));
    const next = (r) => Object.entries(r.returns_by_month || {}).filter(([, v]) => v > 0).slice(0, 3)
      .map(([m, v]) => `${cuMonthShort(m)} <b>${num(v)}</b>`).join(" · ") || `<span class="muted">none in 12 months</span>`;
    // only a row with a customer code can open: the detail is addressed by the code
    const custRows = rows.map((r, i) => {
      const opens = !!r.customer_code;
      return `
      <tr class="cu-row${opens ? " clickable" : ""}" data-code="${esc(r.customer_code || "")}" data-i="${i}"${opens ? ` title="Open: devices by model, returns by month"` : ""}>
        <td class="cu-name"><div class="cell-prod__name">${esc(r.customer)}</div><div class="cell-prod__cat">${esc(r.customer_code || "no code")}</div></td>
        <td class="muted cu-nowrap">${r.since ? fmtDate(r.since) : "—"}</td>
        <td class="num"><b>${num(r.devices_at_customer)}</b><div class="spendbar-track cu-bar"><div class="spendbar-fill" style="width:${r.devices_at_customer / maxDev * 100}%"></div></div></td>
        <td class="num">${num(r.contracts_active)}</td>
        <td class="num">${num(r.contracts_ending_90d)}</td>
        <td class="num">${r.returns_overdue ? plainPill(num(r.returns_overdue), "negative") : `<span class="muted">0</span>`}</td>
        <td class="cu-next">${next(r)}</td>
        <td class="cu-mix">${cuMix(r.by_category)}</td>
      </tr>${opens ? `
      <tr class="cu-detail hidden" data-detail="${i}"><td colspan="8"><div class="wh-detail__inner" id="cu-detail-${i}"><span class="muted">Loading…</span></div></td></tr>` : ""}`;
    }).join("") || `<tr><td colspan="8"><div class="state"><div class="state__sub">No customer in this database.</div></div></td></tr>`;
    customers = `<div class="panel cu-scroll"><table class="tbl cu-tbl">
          <thead><tr><th>Customer</th><th>Since</th><th class="num">Devices</th><th class="num">Contracts</th><th class="num">Ending 90 d</th><th class="num">Overdue</th><th>Next returns</th><th>By category</th></tr></thead>
          <tbody>${custRows}</tbody>
        </table></div>
        <div class="wh-foot">Since: the start of the customer's first rental contract; the system stores no onboarding date for a customer.</div>`;
  } else {
    customers = cuFailed("the customers", rC.reason);
  }

  let defects, repairHint = "";
  if (O) {
    const D = O.defects;
    const current = D.months.length ? D.months[D.months.length - 1].month : null;   // oldest first: the last is this month
    const defRows = D.months.map((m) => `<tr>
        <td><span class="ref">${cuMonth(m.month)}</span>${m.month === current ? ` <span class="muted">to date</span>` : ""}</td>
        <td class="num">${num(m.reported)}</td><td class="num">${num(m.in_repair)}</td>
        <td class="num">${num(m.repairs_invoiced)}</td><td class="num">${num(m.written_off)}</td></tr>`).join("");
    const rest = [D.in_repair_before_window ? `${num(D.in_repair_before_window)} went in before this window` : "",
                  D.in_repair_undated ? `${num(D.in_repair_undated)} carry no date: in the count above, in no month` : ""].filter(Boolean).join("; ");
    repairHint = `${num(D.in_repair_now)} devices in repair now`;
    defects = `<div class="panel cu-scroll"><table class="tbl cu-grid">
          <thead><tr><th>Month</th><th class="num">Defect returns</th><th class="num">In repair now, entered</th><th class="num">Repairs invoiced</th><th class="num">Written off</th></tr></thead>
          <tbody>${defRows}</tbody>
        </table></div>
        <div class="wh-foot">Defect returns: rentals that ended for a defect or a swap, by the day they ended. In the seeded demo data such an end exists only for the devices in repair on the day the data was seeded (app/seed_daas.py), and the simulation ends rentals as planned or early only, so every other month reads 0: those zeros are a property of the simulated data, not of the business. In repair now: every device at the repair partner today, a defect or a grade C return alike, by the month it went in${rest ? ` (${rest})` : ""}. Repairs invoiced: the repair partner's invoices, by invoice date. In the seeded data a repair invoice exists only for a device that went on to a second rental, dated at least eight days before that rental began, and the devices in repair now have none yet, so a current month that began less than eight days before the data was seeded reads 0 until the simulation finishes repairs: again a property of the simulated data. Written off: devices recycled, the exit without proceeds. Not shown: the replacement device a defect used up. No record links a defect return to the swap-buffer device that replaced it, and the movement log has no history from before the simulation, so those months would show zeros that are not real.</div>`;
  } else {
    defects = cuFailed("defects and repairs", rO.reason);
  }

  screen.innerHTML = `
    ${pageHead("Rented fleet", "Customers and returns", "How many devices are out, at which customer, and how many land back at the warehouse in which month, beside what open purchase orders bring in the same month. Counted from every rental contract and order line; nothing here is estimated.")}
    ${stats}
    <div class="section">
      <div class="section__head"><span class="section__title">What lands at the warehouse</span><span class="section__count">12 months</span><span class="section__hint">back: planned end of each running contract · in: open order lines by estimated delivery</span></div>
      ${grid}
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">Customers</span>${T ? `<span class="section__count">${num(T.customers)}</span>` : ""}<span class="section__hint">most devices first · open a row for its devices by model and its returns by month</span></div>
      ${customers}
    </div>
    <div class="section" style="margin-bottom:0">
      <div class="section__head"><span class="section__title">Defects and repairs</span><span class="section__count">last 12 months</span>${repairHint ? `<span class="section__hint">${repairHint}</span>` : ""}</div>
      ${defects}
    </div>`;

  const open = (i) => {
    const detail = $(`#screen [data-detail="${i}"]`);
    const row = $(`#screen .cu-row[data-i="${i}"]`);
    if (!detail || !row || !row.dataset.code) return;
    const wasHidden = detail.classList.contains("hidden");
    detail.classList.toggle("hidden");
    row.classList.toggle("is-open", wasHidden);
    const host = $(`#cu-detail-${i}`);
    if (wasHidden && host && !host.dataset.loaded && !host.dataset.loading) cuLoadDetail(row.dataset.code, host);
  };
  $$("#screen .cu-row.clickable").forEach((r) => r.addEventListener("click", () => open(r.dataset.i)));
  $$("#screen .cu-reload").forEach((b) => b.addEventListener("click", () => showTab("customers")));
};
