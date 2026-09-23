"use strict";
/* ============================================================================
   SCM Master — Returns (device-as-a-service). Loads after kpis.js.

   What comes back when. The calendar is built from the planned end of every
   running rental contract; the expected next step of a return (second rental,
   repair first, sale, recycling) is a rule over the grade mix and is labelled
   as expected, not as fact. The list below it is the concrete devices due in
   the next 30 days, one row each, so intake, MDM releases and refurbishment
   capacity can be planned against real serials.
============================================================================ */

CRUMBS.returns = "Returns";

RENDER.returns = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `<div class="state"><div class="state__icon">${icon("return", 22)}</div><div class="state__title">No rental fleet in this database</div><div class="state__sub">Returns exist in the device-as-a-service scenario. This database holds the datacenter operation.</div></div>`;
    return;
  }
  const [F, cal, up] = await Promise.all([
    api("/fleet/summary"),
    api("/fleet/returns/calendar?months=24"),
    api("/fleet/returns/upcoming?days=30&limit=300"),
  ]);
  const n = (v) => Number(v || 0).toLocaleString("de-DE");
  const monthLabel = (ym) => new Date(ym + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
  const maxTotal = Math.max(1, ...cal.map((m) => m.total));
  const sum = (k) => cal.reduce((a, m) => a + (m[k] || 0), 0);
  const stat = (label, ic, val, hint, hintCls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;

  const calRows = cal.map((m) => `
    <tr>
      <td><span class="ref">${monthLabel(m.month)}</span></td>
      <td class="num">${n(m.from_cycle1)}</td>
      <td class="num">${n(m.from_cycle2)}</td>
      <td class="num"><b>${n(m.total)}</b></td>
      <td><div style="display:flex;align-items:center;gap:8px"><div class="spendbar-track" style="width:80px"><div class="spendbar-fill" style="width:${m.total / maxTotal * 100}%"></div></div></div></td>
      <td class="num">${n(m.second_rental)}</td>
      <td class="num">${n(m.repair)}</td>
      <td class="num">${n(m.sale)}</td>
      <td class="num muted">${n(m.recycling)}</td>
      <td class="muted" style="white-space:nowrap;font-size:11.5px">${Object.entries(m.by_family || {}).sort((a, b) => b[1] - a[1]).map(([f, c]) => `${esc(f).slice(0, 5)} ${n(c)}`).join(" · ")}</td>
    </tr>`).join("");

  const upRows = up.length ? up.map((r) => `
    <tr>
      <td><span class="ref">${esc(r.serial_number)}</span></td>
      <td><div class="cell-prod__name">${esc(r.product)}</div><div class="cell-prod__cat">${esc(r.family || "")}${r.age_months != null ? ` · ${r.age_months} months old` : ""}</div></td>
      <td>${esc(r.customer)}</td>
      <td class="num">${r.cycle_no === 1 ? "1st" : "2nd"} · ${r.term_months} mo</td>
      <td class="muted">${fmtDate(r.planned_end)}${r.overdue ? ` ${plainPill("overdue", "negative")}` : ""}</td>
      <td class="muted">${esc(r.expected_next)}</td>
    </tr>`).join("")
    : `<tr><td colspan="6"><div class="state"><div class="state__sub">No contract ends in the next 30 days.</div></div></td></tr>`;

  screen.innerHTML = `
    ${pageHead("Rental cycle", "Returns", "What comes back when, from the planned end of every running contract. The next step of a return is expected from the grade mix: after a first rental most devices go into a second rental, grade C goes to repair first, everything after a second rental is sold.")}
    <div class="stats">
      ${stat("Due in 30 days", "return", n(F.returns_due_30d), `${n(F.returns_due_90d)} within 90 days`)}
      ${stat("Due in 12 months", "clock", n(F.returns_due_365d), `${n(F.rented)} rented today`)}
      ${stat("Overdue", "alert", n(F.returns_overdue), F.returns_overdue ? "planned end passed, device still out" : "every return on time", F.returns_overdue ? "stat__hint--neg" : "stat__hint--pos")}
      ${stat("Next 24 months", "box", n(sum("total")), `${n(sum("second_rental"))} to a second rental · ${n(sum("sale"))} to sale`)}
    </div>
    <div class="section">
      <div class="section__head"><span class="section__title">Return calendar</span><span class="section__count">24 months</span><span class="section__hint">expected next step from the grade mix A 35 · B 40 · C 20 · D 5 (placeholder, owner Head of Recommerce)</span></div>
      <div class="panel"><table class="tbl">
        <thead><tr><th>Month</th><th class="num">From 1st rental</th><th class="num">From 2nd rental</th><th class="num">Total</th><th></th><th class="num">→ 2nd rental</th><th class="num">→ Repair first</th><th class="num">→ Sale</th><th class="num">→ Recycling</th><th>By family</th></tr></thead>
        <tbody>${calRows}</tbody>
      </table></div>
    </div>
    <div class="section" style="margin-bottom:0">
      <div class="section__head"><span class="section__title">Due in the next 30 days</span><span class="section__count">${n(up.length)}</span><span class="section__hint">the concrete devices, planned end first</span></div>
      <div class="panel"><table class="tbl">
        <thead><tr><th>Serial</th><th>Device</th><th>Customer</th><th class="num">Rental</th><th>Planned end</th><th>Expected next</th></tr></thead>
        <tbody>${upRows}</tbody>
      </table></div>
    </div>`;
};
