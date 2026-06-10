"use strict";
/* ============================================================================
   SCM Master — operations UI. Dependency-free; talks to /api/v1.
   Token kept in localStorage and attached as a Bearer header on every call.
   Rendering follows the TrueSpend editorial design system (see app.css).
============================================================================ */

const API = "/api/v1";
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let token = localStorage.getItem("scm_token") || "";

// The executive analytics cockpit (separate service). Per-environment: the
// demo build points at the demo cockpit; a prod deploy overrides this by
// setting window.SCM_ANALYTICS_URL before app.js loads (or via a meta tag).
const ANALYTICS_URL =
  (typeof window !== "undefined" && window.SCM_ANALYTICS_URL) ||
  (document.querySelector('meta[name="scm-analytics-url"]') || {}).content ||
  "https://scm-power-bi-production.up.railway.app/";

/* ── Domain constants ──────────────────────────────────────────────── */
const LIFECYCLE = ["RECEIVED", "IN_STORAGE", "DEPLOYED", "MAINTENANCE", "DECOMMISSIONED", "DISPOSED"];

// Legal next states — mirrors backend services/lifecycle.py (UI hint only;
// the server is the source of truth and still enforces every transition).
const NEXT = {
  RECEIVED:       ["IN_STORAGE", "DEPLOYED"],
  IN_STORAGE:     ["DEPLOYED"],
  DEPLOYED:       ["MAINTENANCE", "DECOMMISSIONED"],
  MAINTENANCE:    ["DEPLOYED", "DECOMMISSIONED"],
  DECOMMISSIONED: ["DISPOSED"],
  DISPOSED:       [],
};

const STATUS = {
  RECEIVED:       { label: "Received",       tone: "info" },
  IN_STORAGE:     { label: "In storage",     tone: "neutral" },
  DEPLOYED:       { label: "Deployed",       tone: "positive" },
  MAINTENANCE:    { label: "Maintenance",    tone: "warning" },
  DECOMMISSIONED: { label: "Decommissioned", tone: "mute" },
  DISPOSED:       { label: "Disposed",       tone: "negative" },
};
const STEP_SHORT = ["Recv", "Store", "Deploy", "Maint", "Decom", "Disp"];

const TONE = {
  info:     { dot: "var(--ts-info)",        bg: "var(--ts-info-wash)",       fg: "var(--ts-info)" },
  positive: { dot: "var(--ts-positive)",    bg: "var(--ts-positive-wash)",   fg: "var(--ts-positive)" },
  warning:  { dot: "var(--ts-warning)",     bg: "var(--ts-warning-wash)",    fg: "#8C6510" },
  negative: { dot: "var(--ts-negative)",    bg: "var(--ts-negative-wash)",   fg: "var(--ts-negative)" },
  neutral:  { dot: "var(--ts-line-strong)", bg: "var(--ts-paper-deep)",      fg: "var(--ts-ink-soft)" },
  mute:     { dot: "var(--ts-ink-faint)",   bg: "var(--ts-paper-deep)",      fg: "var(--ts-ink-mute)" },
};

const NAV = [
  { id: "overview",     label: "Overview",     icon: "gauge" },
  { id: "inventory",    label: "Inventory",    icon: "stock" },
  { id: "requisitions", label: "Requisitions", icon: "cart",  countKey: "staged" },
  { id: "tracking",     label: "Orders",       icon: "track", countKey: "inbound" },
  { id: "assets",       label: "Assets",       icon: "box",   countKey: "assets" },
  { id: "capacity",     label: "Capacity",     icon: "layers" },
  { id: "contracts",    label: "Contracts",    icon: "contract" },
  { id: "spend",        label: "Spend",        icon: "euro" },
];

/* ── Icons (Lucide stroke language) ────────────────────────────────── */
const ICONS = {
  gauge:  '<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/><circle cx="12" cy="14" r="1.4" fill="currentColor" stroke="none"/>',
  box:    '<path d="M12 3l8 4v10l-8 4-8-4V7z"/><path d="M4 7l8 4 8-4"/><path d="M12 11v10"/>',
  truck:  '<path d="M3 6h11v9H3z"/><path d="M14 9h4l3 3v3h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17.5" cy="18" r="1.6"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  euro:   '<path d="M17 6.5a6 6 0 1 0 0 11"/><path d="M5 10h8"/><path d="M5 14h7"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  cpu:    '<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>',
  memory: '<rect x="2" y="7" width="20" height="10" rx="1.5"/><path d="M6 7v-2M10 7v-2M14 7v-2M18 7v-2M6 21v-4M10 21v-4M14 21v-4M18 21v-4"/>',
  disk:   '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="12" cy="12" r="4"/><path d="M16.5 16.5l2 2"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0 5.7 5.7l-9.4 9.4a2 2 0 0 1-2.8-2.8z"/><path d="M17 7l-2-2"/>',
  pin:    '<path d="M12 21s7-5.5 7-11a7 7 0 0 0-14 0c0 5.5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  check:  '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
  arrow:  '<path d="M5 12h14M13 6l6 6-6 6"/>',
  chev:   '<path d="M9 6l6 6-6 6"/>',
  clock:  '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert:  '<path d="M10.3 3.6L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  plus:   '<path d="M12 5v14M5 12h14"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.2 7.6-8 9-4.8-1.4-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
};
const icon = (name, size = 18) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;

const CAT_ICON = (cat = "") => {
  const c = cat.toLowerCase();
  if (c.includes("server")) return "server";
  if (c.includes("processor") || c.includes("cpu")) return "cpu";
  if (c.includes("memory") || c.includes("dimm") || c.includes("ram")) return "memory";
  if (c.includes("storage") || c.includes("disk") || c.includes("drive")) return "disk";
  return "box";
};

/* ── Formatters & helpers ──────────────────────────────────────────── */
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const euro = (n) => (n == null || n === "") ? "—" : "€" + Number(n).toLocaleString("de-DE", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) : "—";
const daysSince = (iso) => iso ? Math.floor((Date.now() - new Date(iso)) / 86400000) : null;
const pct = (u) => u == null ? "—" : Math.round(u * 100) + "%";
const pretty = (s) => String(s || "").replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());

const statusPill = (status) => {
  const m = STATUS[status] || { label: pretty(status), tone: "neutral" };
  const t = TONE[m.tone];
  return `<span class="pill" style="background:${t.bg};color:${t.fg}"><span class="pill__dot" style="background:${t.dot}"></span>${m.label}</span>`;
};
const plainPill = (label, tone) => {
  const t = TONE[tone] || TONE.neutral;
  return `<span class="pill" style="background:${t.bg};color:${t.fg}"><span class="pill__dot" style="background:${t.dot}"></span>${esc(label)}</span>`;
};
const capTone = (u, over) => over ? "var(--ts-negative)" : u >= 0.9 ? "var(--ts-warning)" : u >= 0.7 ? "var(--ts-brand-gold)" : "var(--ts-positive)";

/* ── API layer ─────────────────────────────────────────────────────── */
async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) { payload = new URLSearchParams(form); }
  else if (body) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(API + path, { method, headers, body: payload });
  if (res.status === 401) { logout(); throw new Error("Session expired — sign in again"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast " + kind; }, 2800);
}

/* ── Caches ────────────────────────────────────────────────────────── */
let PRODUCTS = {};   // id → {name, category, product_code}
let LOCATIONS = {};  // id → {name, code, location_type, capacity}
let ORGS = {};       // id → {name, is_supplier, is_manufacturer}
let COUNTS = { assets: null, inbound: null, overdue: 0 };
let RACK_ID = null;  // a rack location to deploy into
let openAssetId = null;

/* ── Auth ──────────────────────────────────────────────────────────── */
async function login(email, password) {
  const data = await api("/auth/login", { method: "POST", form: { username: email, password } });
  token = data.access_token;
  localStorage.setItem("scm_token", token);
  await boot();
}
function logout() {
  token = "";
  localStorage.removeItem("scm_token");
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}

async function boot() {
  try {
    const me = await api("/auth/me");
    $("#user-name").textContent = me.full_name || me.email;
    $("#user-sub").innerHTML = `<span class="role-pill">${esc(me.role)}</span>`;
    $("#user-avatar").textContent = initials(me.full_name || me.email);

    const [products, locations, orgs] = await Promise.all([
      api("/products").catch(() => []),
      api("/locations").catch(() => []),
      api("/organizations").catch(() => []),
    ]);
    PRODUCTS = Object.fromEntries(products.map((p) => [p.id, p]));
    LOCATIONS = Object.fromEntries(locations.map((l) => [l.id, l]));
    ORGS = Object.fromEntries(orgs.map((o) => [o.id, o]));
    const rack = locations.find((l) => /rack/i.test(l.location_type || "") || /rack/i.test(l.name || ""));
    RACK_ID = rack ? rack.id : null;

    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    renderNav();
    primeCounts();
    showTab("overview");
  } catch (e) {
    logout();
  }
}
const initials = (s) => s.replace(/@.*/, "").split(/[ ._-]+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase() || "··";

async function primeCounts() {
  try {
    const [assets, inbound] = await Promise.all([
      api("/assets?limit=1000").catch(() => []),
      api("/planning/inbound").catch(() => []),
    ]);
    COUNTS.assets = assets.length;
    COUNTS.inbound = inbound.length;
    COUNTS.overdue = inbound.filter((r) => r.overdue).length;
    renderNav();
  } catch (e) {}
}

/* ── Navigation ────────────────────────────────────────────────────── */
let currentTab = "overview";
function renderNav() {
  $("#nav").innerHTML = NAV.map((n) => {
    const active = currentTab === n.id;
    const c = n.countKey ? COUNTS[n.countKey] : null;
    const urgent = n.id === "tracking" && COUNTS.overdue > 0;
    const badge = c != null ? `<span class="navlink__count${urgent ? " navlink__count--urgent" : ""}">${c}</span>` : "";
    return `<button class="navlink${active ? " navlink--active" : ""}" data-tab="${n.id}">
      <span class="navlink__icon">${icon(n.icon, 16)}</span><span>${n.label}</span>${badge}</button>`;
  }).join("");
  $$("#nav .navlink").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
}

const CRUMBS = { overview: "Overview", assets: "Assets", inbound: "Inbound", capacity: "Capacity", spend: "Spend" };
const RENDER = {};
function showTab(name) {
  currentTab = name;
  openAssetId = null;
  renderNav();
  // A tab's label + renderer live in its own feature script (e.g. inventory.js
  // sets CRUMBS.inventory and RENDER.inventory). If that script didn't load —
  // almost always a stale browser cache after a deploy — CRUMBS[name] is
  // undefined and RENDER[name]() throws, leaving the page stuck on "Loading…"
  // with an "undefined" crumb. Fail loud and recoverable instead of hanging.
  const label = CRUMBS[name] || name;
  $("#crumbs").innerHTML = `<span>SCM Master</span>${icon("chev", 12)}<strong>${esc(label)}</strong>`;
  const screen = $("#screen");
  const renderer = RENDER[name];
  if (typeof renderer !== "function") {
    screen.innerHTML = `<div class="state">
      <div class="state__icon">${icon("gauge", 22)}</div>
      <div class="state__sub">This view didn't finish loading — your browser may be holding a stale copy.</div>
      <button class="btn btn--primary" style="margin-top:12px" onclick="location.reload(true)">Reload</button>
    </div>`;
    return;
  }
  screen.innerHTML = `<div class="state"><div class="state__icon">${icon("gauge", 22)}</div><div class="state__sub">Loading…</div></div>`;
  // A throw inside the renderer must not leave the screen stuck on "Loading…".
  Promise.resolve().then(renderer).catch((e) => {
    screen.innerHTML = `<div class="state">
      <div class="state__icon">${icon("gauge", 22)}</div>
      <div class="state__sub">Couldn't load this view. ${esc((e && e.message) || "")}</div>
      <button class="btn btn--primary" style="margin-top:12px" onclick="location.reload(true)">Reload</button>
    </div>`;
  });
}

/* ── Reusable bits ─────────────────────────────────────────────────── */
const pageHead = (eyebrow, title, sub, actions = "") => `
  <div class="pagehead">
    <div>
      <div class="pagehead__eyebrow">${esc(eyebrow)}</div>
      <h1 class="pagehead__title">${esc(title)}</h1>
      ${sub ? `<p class="pagehead__sub">${esc(sub)}</p>` : ""}
    </div>
    ${actions ? `<div class="pagehead__actions">${actions}</div>` : ""}
  </div>`;

const errState = (msg) => `<div class="state"><div class="state__icon">${icon("alert", 24)}</div>
  <div class="state__title">Couldn’t load this view</div><div class="state__sub">${esc(msg)}</div></div>`;

const productCell = (productId, big = true) => {
  const p = PRODUCTS[productId] || {};
  const name = p.name || productId || "—";
  const cat = p.category || "";
  return `<div class="cell-prod">
    <span class="cell-prod__icon">${icon(CAT_ICON(cat), big ? 17 : 16)}</span>
    <div><div class="cell-prod__name">${esc(name)}</div>${big && cat ? `<div class="cell-prod__cat">${esc(cat)}</div>` : ""}</div>
  </div>`;
};
const locName = (id) => id && LOCATIONS[id] ? LOCATIONS[id].name : "—";

/* ── Overview ──────────────────────────────────────────────────────── */
RENDER.overview = async function () {
  try {
    const [assets, inbound, capacity, spend, forecast] = await Promise.all([
      api("/assets?limit=1000"),
      api("/planning/inbound").catch(() => []),
      api("/planning/capacity").catch(() => []),
      api("/analytics/spend").catch(() => null),
      api("/planning/forecast").catch(() => null),
    ]);

    const dist = LIFECYCLE.map((s) => ({ status: s, n: assets.filter((a) => a.status === s).length }));
    const total = assets.length;
    const deployed = dist.find((d) => d.status === "DEPLOYED").n;
    const outstanding = inbound.reduce((s, r) => s + (r.outstanding || 0), 0);
    const overdue = inbound.filter((r) => r.overdue);
    const overCap = capacity.filter((r) => r.over_capacity);
    const inMaint = assets.filter((a) => a.status === "MAINTENANCE");

    // Needs-attention items, derived from real signals
    const attn = [];
    overdue.forEach((r) => attn.push({
      tone: "negative", ic: "clock",
      title: `${r.order_number} is overdue — ${r.outstanding}× ${(PRODUCTS[r.product_id] || {}).name || "units"} outstanding`,
      sub: `ETA ${fmtDate(r.estimated_delivery_date)} has passed. Chase the supplier or re-source the line.`,
      go: "inbound",
    }));
    overCap.forEach((r) => attn.push({
      tone: "negative", ic: "alert",
      title: `${r.name} is over capacity`,
      sub: `${r.used} units in a ${r.capacity}-unit location. Re-stage incoming stock.`,
      go: "capacity",
    }));
    if (inMaint.length) attn.push({
      tone: "warning", ic: "wrench",
      title: `${inMaint.length} unit${inMaint.length > 1 ? "s" : ""} in maintenance`,
      sub: `Decide: return to service or decommission. ${inMaint.slice(0, 3).map((a) => a.serial_number).join(", ")}${inMaint.length > 3 ? "…" : ""}`,
      go: "assets",
    });

    const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
      `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div>
       <div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;

    const distBar = dist.map((d) => {
      if (d.n === 0) return "";
      const t = TONE[STATUS[d.status].tone];
      const wide = total && d.n / total > 0.06;
      return `<div class="dist__seg" title="${STATUS[d.status].label}: ${d.n}" style="flex:${d.n};background:${t.bg};border-right:1px solid var(--ts-surface)">${wide ? `<span class="dist__seg-n" style="color:${t.fg}">${d.n}</span>` : ""}</div>`;
    }).join("");
    const legend = dist.map((d) => {
      const t = TONE[STATUS[d.status].tone];
      return `<div class="dist-legend__item"><span class="dist-legend__dot" style="background:${t.dot}"></span>${STATUS[d.status].label}<span class="dist-legend__n">${d.n}</span></div>`;
    }).join("");

    const attnHTML = attn.length ? `<div class="panel" style="padding:4px 22px"><div class="attn">${attn.map((a) => {
      const t = TONE[a.tone];
      return `<div class="attn__item">
        <div class="attn__icon" style="background:${t.bg};color:${t.fg}">${icon(a.ic, 16)}</div>
        <div><div class="attn__title">${esc(a.title)}</div><div class="attn__sub">${esc(a.sub)}</div></div>
        <button class="btn btn--ghost btn--sm attn__action" data-go="${a.go}">Open</button>
      </div>`;
    }).join("")}</div></div>`
      : `<div class="panel"><div class="state"><div class="state__icon">${icon("check", 22)}</div><div class="state__title">Nothing needs you right now</div><div class="state__sub">No overdue orders, no capacity breaches, nothing stuck in service.</div></div></div>`;

    const railHTML = forecast ? `
      <aside class="rail">
        <div class="rail__head"><span class="rail__dot"></span> Deployment forecast</div>
        <div class="panel" style="padding:6px 20px;margin-bottom:18px">
          ${[["On hand", forecast.on_hand], ["Still inbound", forecast.inbound], ["Deployed", forecast.deployed], ["Forecast deployable", forecast.forecast_deployable]]
            .map(([k, v], i, arr) => `<div class="prov__row"${i === arr.length - 1 ? ' style="border-bottom:none"' : ""}><span class="prov__k">${k}</span><span class="prov__v" style="font-variant-numeric:tabular-nums;font-weight:600">${v}</span></div>`).join("")}
        </div>
        <div class="rail__head">Capacity snapshot</div>
        <div class="panel" style="padding:6px 20px">
          ${capacity.slice(0, 5).map((r, i, arr) => {
            const u = r.utilisation != null ? r.utilisation : (r.capacity ? r.used / r.capacity : 0);
            const tone = capTone(u, r.over_capacity);
            return `<div class="prov__row"${i === arr.length - 1 ? ' style="border-bottom:none"' : ""}><span class="prov__k">${esc(r.name)}</span><span class="cap-util" style="color:${tone}">${pct(u)}</span></div>`;
          }).join("")}
        </div>
      </aside>` : "";

    $("#screen").innerHTML = `<div class="content--rail fade-in"><div>
      ${pageHead("Operations", "Operations overview", "What needs you, and where the fleet stands — composed live from the asset register, the inbound pipeline and capacity.")}
      <div class="stats">
        ${stat("Under management", "box", total, "serialised units")}
        ${stat("Deployed", "check", deployed, total ? Math.round(deployed / total * 100) + "% of fleet in racks" : "", "stat__hint--pos")}
        ${stat("Inbound outstanding", "truck", outstanding, overdue.length ? `${overdue.length} order${overdue.length > 1 ? "s" : ""} overdue` : "all on track", overdue.length ? "stat__hint--neg" : "stat__hint--pos")}
        ${stat("Spend tracked", "euro", spend ? euro(spend.total_spend) : "—", "via asset provenance", "", "stat__val--gold")}
      </div>
      <div class="section">
        <div class="section__head"><span class="section__title">Lifecycle distribution</span><span class="section__count">${total} units</span><span class="section__hint">received → in storage → deployed → service → retired</span></div>
        <div class="dist">${distBar}</div>
        <div class="dist-legend">${legend}</div>
      </div>
      <div class="section" style="margin-bottom:0">
        <div class="section__head"><span class="section__title">Needs you this week</span><span class="section__count">${attn.length}</span></div>
        ${attnHTML}
      </div>
    </div>${railHTML}</div>`;

    $$("#screen [data-go]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.go)));
  } catch (e) {
    $("#screen").innerHTML = errState(e.message);
  }
};

/* ── Assets ────────────────────────────────────────────────────────── */
const FILTERS = [
  { id: "all", label: "All" }, { id: "RECEIVED", label: "Received" }, { id: "IN_STORAGE", label: "In storage" },
  { id: "DEPLOYED", label: "Deployed" }, { id: "MAINTENANCE", label: "Maintenance" }, { id: "retired", label: "Retired" },
];
let assetFilter = "all";
let assetCache = [];

RENDER.assets = async function () {
  $("#screen").innerHTML = `
    ${pageHead("Asset lifecycle", "Assets", "Every serialised unit, followed from receipt to disposal. Open a row to trace its provenance and move it along the lifecycle.", `<button class="btn btn--ink" disabled title="Receiving runs against a purchase order">${icon("box", 15)} Receive units</button>`)}
    <div class="toolbar">
      <div class="segmented" id="asset-filter">${FILTERS.map((f) => `<button class="${assetFilter === f.id ? "active" : ""}" data-f="${f.id}">${f.label}</button>`).join("")}</div>
      <div class="toolbar__spacer"></div>
      <span class="toolbar__count" id="asset-count"></span>
    </div>
    <div class="panel"><table class="tbl">
      <thead><tr><th>Serial</th><th>Product</th><th>Status</th><th>Location</th><th>Received</th><th style="width:32px"></th></tr></thead>
      <tbody id="asset-rows"><tr><td colspan="6"><div class="state"><div class="state__sub">Loading…</div></div></td></tr></tbody>
    </table></div>`;
  $$("#asset-filter button").forEach((b) => b.addEventListener("click", () => { assetFilter = b.dataset.f; openAssetId = null; loadAssets(); }));
  loadAssets();
};

async function loadAssets() {
  $$("#asset-filter button").forEach((b) => b.classList.toggle("active", b.dataset.f === assetFilter));
  try {
    const q = assetFilter === "all" || assetFilter === "retired" ? "?limit=1000" : `?status=${assetFilter}&limit=1000`;
    let rows = await api("/assets" + q);
    if (assetFilter === "retired") rows = rows.filter((a) => a.status === "DECOMMISSIONED" || a.status === "DISPOSED");
    assetCache = rows;
    COUNTS.assets = assetFilter === "all" ? rows.length : COUNTS.assets;
    $("#asset-count").textContent = `${rows.length} unit${rows.length === 1 ? "" : "s"}`;
    const tb = $("#asset-rows");
    if (!rows.length) { tb.innerHTML = `<tr><td colspan="6"><div class="state"><div class="state__icon">${icon("box", 22)}</div><div class="state__sub">No units in this state.</div></div></td></tr>`; return; }
    tb.innerHTML = rows.map((a) => `
      <tr class="clickable" data-id="${a.id}">
        <td><span class="ref">${esc(a.serial_number)}</span></td>
        <td>${productCell(a.product_id)}</td>
        <td>${statusPill(a.status)}</td>
        <td class="muted">${esc(locName(a.current_location_id))}</td>
        <td class="muted">${fmtDate(a.received_date)}</td>
        <td style="color:var(--ts-ink-faint)"><span class="chev" style="display:inline-flex;transition:transform 160ms">${icon("chev", 15)}</span></td>
      </tr>
      <tr class="brief-host" data-host="${a.id}" hidden><td colspan="6"></td></tr>`).join("");
    $$("#asset-rows tr.clickable").forEach((tr) => tr.addEventListener("click", () => toggleAsset(tr.dataset.id)));
  } catch (e) {
    $("#asset-rows").innerHTML = `<tr><td colspan="6">${errState(e.message)}</td></tr>`;
  }
}

async function toggleAsset(id) {
  const host = $(`#asset-rows tr[data-host="${id}"]`);
  const row = $(`#asset-rows tr.clickable[data-id="${id}"]`);
  const wasOpen = openAssetId === id;
  // close any open
  $$('#asset-rows tr.brief-host').forEach((h) => { h.hidden = true; h.firstElementChild.innerHTML = ""; });
  $$('#asset-rows tr.clickable').forEach((r) => { r.classList.remove("is-open"); const c = r.querySelector(".chev"); if (c) c.style.transform = "none"; });
  if (wasOpen) { openAssetId = null; return; }
  openAssetId = id;
  row.classList.add("is-open");
  const chev = row.querySelector(".chev"); if (chev) chev.style.transform = "rotate(90deg)";
  host.hidden = false;
  host.firstElementChild.className = "";
  host.firstElementChild.innerHTML = `<div class="brief__inner"><div class="state__sub" style="padding:18px 4px">Tracing…</div></div>`;
  host.classList.add("brief");
  const asset = assetCache.find((a) => a.id === id);
  try {
    const [prov, events] = await Promise.all([
      api(`/assets/${id}/provenance`).catch(() => null),
      api(`/assets/${id}/events`).catch(() => []),
    ]);
    host.firstElementChild.innerHTML = renderBrief(asset, prov, events);
    $$(`#asset-rows [data-transition]`).forEach((b) => b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      doTransition(id, b.dataset.transition);
    }));
  } catch (e) {
    host.firstElementChild.innerHTML = `<div class="brief__inner">${errState(e.message)}</div>`;
  }
}

function renderBrief(asset, prov, events) {
  const idx = LIFECYCLE.indexOf(asset.status);
  const stepper = `<div class="stepper">${LIFECYCLE.map((s, i) => {
    const cls = i < idx ? "step--done" : i === idx ? "step--current" : "";
    return `<div class="step ${cls}"><div class="step__node"><div class="step__dot"></div></div>${i < LIFECYCLE.length - 1 ? '<div class="step__bar"></div>' : ""}</div>`;
  }).join("")}</div>
  <div class="steplabels">${STEP_SHORT.map((s, i) => `<div class="steplabel${i === idx ? " steplabel--current" : ""}">${s}</div>`).join("")}</div>`;

  const EVTONE = { RECEIVED: "var(--ts-info)", IN_STORAGE: "var(--ts-line-strong)", DEPLOYED: "var(--ts-positive)", MAINTENANCE: "var(--ts-warning)", DECOMMISSIONED: "var(--ts-ink-faint)", DISPOSED: "var(--ts-negative)" };
  const log = (events && events.length) ? `<div class="log">${events.map((e) => {
    const dot = EVTONE[e.to_status] || "var(--ts-line-strong)";
    const title = e.note || (e.to_status ? `${pretty(e.from_status || "—")} → ${pretty(e.to_status)}` : pretty(e.event_type));
    return `<div class="log__entry"><div class="log__rail"><div class="log__dot" style="background:${dot}"></div><div class="log__line"></div></div>
      <div class="log__body"><div class="log__time">${fmtDate(e.date_created)}${e.actor ? " · " + esc(e.actor) : ""}</div><div class="log__note">${esc(title)}</div></div></div>`;
  }).join("")}</div>` : `<div class="log__note" style="color:var(--ts-ink-faint)">No events recorded for this unit yet.</div>`;

  const nexts = NEXT[asset.status] || [];
  const actions = nexts.length
    ? `<span class="brief__actions-label">Transition to</span>${nexts.map((t) => `<button class="btn btn--secondary btn--sm" data-transition="${t}">${STATUS[t].label} ${icon("arrow", 13)}</button>`).join("")}`
    : `<span class="brief__actions-label">End of lifecycle — unit disposed.</span>`;

  const p = prov || {};
  const provRows = [
    ["Order", p.order_number ? `<span class="ref">${esc(p.order_number)}</span>` : "—"],
    ["Product", esc((PRODUCTS[asset.product_id] || {}).name || p.product_name || asset.product_id)],
    ["SKU", `<span class="ref">${esc((PRODUCTS[asset.product_id] || {}).product_code || "—")}</span>`],
    ["Source supplier", esc(p.supplier_name || "—")],
    ["Unit price", `<span class="prov__v money">${euro(p.unit_price)}</span>`],
    ["Received", fmtDate(asset.received_date)],
    ["Age in service", daysSince(asset.received_date) != null ? daysSince(asset.received_date) + " days" : "—"],
    ["Current location", esc(locName(asset.current_location_id))],
  ];

  return `<div class="brief__inner fade-in">
    <div>
      <div class="brief__h">Lifecycle</div>${stepper}
      <div class="brief__h" style="margin-top:22px">Event log</div>${log}
      <div class="brief__actions">${actions}</div>
    </div>
    <div>
      <div class="brief__h">Provenance · never broken</div>
      <div class="prov">${provRows.map(([k, v], i) => `<div class="prov__row"${i === provRows.length - 1 ? ' style="border-bottom:none"' : ""}><span class="prov__k">${k}</span><span class="prov__v">${v}</span></div>`).join("")}</div>
    </div>
  </div>`;
}

async function doTransition(id, target) {
  const location_id = target === "DEPLOYED" ? RACK_ID : undefined;
  try {
    await api(`/assets/${id}/transition`, { method: "POST", body: { target, location_id } });
    toast(`${assetCache.find((a) => a.id === id)?.serial_number || "Asset"} → ${STATUS[target].label}`, "ok");
    openAssetId = null;
    await loadAssets();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ── Inbound ───────────────────────────────────────────────────────── */
RENDER.inbound = async function () {
  try {
    const rows = await api("/planning/inbound");
    const totalOut = rows.reduce((s, r) => s + (r.outstanding || 0), 0);
    const body = rows.length ? rows.map((r) => `
      <tr>
        <td><span class="ref">${esc(r.order_number)}</span></td>
        <td>${productCell(r.product_id, false)}</td>
        <td class="num">${r.ordered}</td>
        <td class="num muted">${r.received}</td>
        <td class="num" style="font-weight:600">${r.outstanding}</td>
        <td class="muted">${fmtDate(r.estimated_delivery_date)}</td>
        <td>${r.overdue ? plainPill("Overdue", "negative") : plainPill("On track", "info")}</td>
      </tr>`).join("")
      : `<tr><td colspan="7"><div class="state"><div class="state__icon">${icon("truck", 22)}</div><div class="state__title">Nothing inbound</div><div class="state__sub">Every open order line has been fully received.</div></div></td></tr>`;
    $("#screen").innerHTML = `
      ${pageHead("Procurement", "Inbound pipeline", "Open order lines with quantity still outstanding. The overdue flag fires the moment ETA passes with units undelivered.")}
      <div class="toolbar"><div class="toolbar__spacer"></div><span class="toolbar__count">${totalOut} units outstanding across ${rows.length} order${rows.length === 1 ? "" : "s"}</span></div>
      <div class="panel"><table class="tbl">
        <thead><tr><th>Order</th><th>Product</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Outstanding</th><th>ETA</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table></div>`;
  } catch (e) { $("#screen").innerHTML = errState(e.message); }
};

/* ── Capacity ──────────────────────────────────────────────────────── */
// recommended-action -> button label + tone (over-capacity is placement, never a buy)
const CAP_ACTION = {
  rebalance:   { label: "Rebalance", tone: "negative", title: "Move the overflow to a location with free space" },
  hold_inbound:{ label: "Hold inbound", tone: "warning", title: "Defer the incoming delivery — it has nowhere to land" },
  add_capacity:{ label: "Add capacity", tone: "negative", title: "No room to move and over capacity — an infrastructure decision" },
  watch:       { label: "Watch", tone: "info", title: "Approaching capacity" },
};

RENDER.capacity = async function () {
  try {
    const [rows, diag, headroom] = await Promise.all([
      api("/planning/capacity"),
      api("/planning/capacity/diagnosis").catch(() => []),
      api("/planning/storage-headroom").catch(() => null),
    ]);
    const DIAG = {};
    (diag || []).forEach((d) => { DIAG[d.location_id] = d; });

    const body = rows.map((r) => {
      const u = r.utilisation != null ? r.utilisation : (r.capacity ? r.used / r.capacity : 0);
      const tone = capTone(u, r.over_capacity);
      const d = DIAG[r.location_id];
      const act = d ? (CAP_ACTION[d.recommended_action] || CAP_ACTION.watch) : null;
      const mainRow = `<tr${d ? ` class="cap-row cap-row--alert"` : ""}>
        <td><div class="cell-prod"><span class="cell-prod__icon">${icon("pin", 15)}</span>
          <div><div class="cell-prod__name">${esc(r.name)}</div><div class="cell-prod__cat ref">${esc(r.code)}</div></div></div></td>
        <td class="muted">${pretty(r.location_type)}</td>
        <td class="num" style="font-weight:600">${r.used}</td>
        <td class="num muted">${r.capacity ?? "—"}</td>
        <td><div style="display:flex;align-items:center;gap:12px"><div class="cap-bar"><div class="cap-bar__fill" style="width:${Math.min(u, 1) * 100}%;background:${tone}"></div></div><span class="cap-util" style="color:${tone}">${pct(u)}</span></div></td>
        <td style="display:flex;align-items:center;gap:8px;justify-content:flex-end">
          ${r.over_capacity ? plainPill("Over capacity", "negative") : (d ? plainPill("Near capacity", "warning") : "")}
          ${d && d.recommended_action === "rebalance" ? `<button class="btn btn--sm cap-resolve" data-loc="${r.location_id}" data-code="${esc(r.code)}" title="${esc(act.title)}">${act.label}</button>`
            : d ? `<span class="cap-actionhint" title="${esc(act.title)}">${act.label}</span>` : ""}
        </td>
      </tr>`;
      // cause + recommended fix expansion row
      const causeRow = d ? `<tr class="cap-cause"><td colspan="6">
        <div class="cap-cause__inner">
          <div class="cap-cause__why">${esc(d.summary)}</div>
          <div class="cap-cause__bits">
            ${d.by_source_po.length ? `<span class="cap-tag">filled by ${d.by_source_po.slice(0,3).map((p)=>`${esc(p.order_number)} (${p.units})`).join(", ")}</span>` : ""}
            ${d.inbound_units > 0 ? `<span class="cap-tag cap-tag--warn">+${d.inbound_units} inbound · ${d.inbound_pos.map(esc).join(", ")}</span>` : ""}
            ${d.by_product.length ? `<span class="cap-tag">${d.by_product.slice(0,3).map((p)=>`${p.units}× ${esc(p.name)}`).join(", ")}</span>` : ""}
          </div>
        </div></td></tr>` : "";
      return mainRow + causeRow;
    }).join("");

    const hr = headroom && headroom.storable_max != null
      ? `<div class="cap-headroom"><strong>${headroom.storable_max}</strong> units max we can still store
         <span class="muted">(${headroom.free_now} free now − ${headroom.committed_inbound} already inbound)</span>
         — any order is capped to this so nothing arrives with nowhere to go.</div>`
      : "";

    $("#screen").innerHTML = `
      ${pageHead("Warehouse flow", "Capacity", "Used against capacity per location. When a location nears its limit we show what's filling it and the right fix — rebalance, hold inbound, or add capacity. Over-capacity is a placement problem, never a reason to buy.")}
      ${hr}
      <div class="panel"><table class="tbl">
        <thead><tr><th>Location</th><th>Type</th><th class="num">Used</th><th class="num">Capacity</th><th style="width:200px">Utilisation</th><th></th></tr></thead>
        <tbody>${body || `<tr><td colspan="6"><div class="state"><div class="state__sub">No locations defined.</div></div></td></tr>`}</tbody>
      </table></div>`;
    $$(".cap-resolve").forEach((b) => b.addEventListener("click", () => resolveCapacity(b.dataset.loc, b.dataset.code, b)));
  } catch (e) { $("#screen").innerHTML = errState(e.message); }
};

async function resolveCapacity(locationId, code, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Resolving…"; }
  try {
    const res = await api(`/planning/capacity/${locationId}/rebalance`, { method: "POST" });
    toast(res.message || `Rebalanced ${code}`, res.moved > 0 ? "ok" : "");
    RENDER.capacity();   // refresh the table — utilisation + pills update
  } catch (e) {
    toast(e.message || "Could not rebalance", "err");
    if (btn) { btn.disabled = false; btn.textContent = "Resolve"; }
  }
}

/* ── Spend ─────────────────────────────────────────────────────────── */
RENDER.spend = async function () {
  try {
    const s = await api("/analytics/spend");
    const bars = (data, head) => {
      const max = Math.max(1, ...data.map((d) => Number(d.spend)));
      const rows = data.map((d) => `<tr>
        <td><span class="cell-prod__name">${esc(d.supplier_name || d.category || "—")}</span></td>
        <td><div style="display:flex;align-items:center"><div class="spendbar-track"><div class="spendbar-fill" style="width:${Number(d.spend) / max * 100}%"></div></div></div></td>
        <td class="num muted">${d.units}</td>
        <td class="num"><span class="prov__v money">${euro(d.spend)}</span></td>
      </tr>`).join("");
      return `<div class="panel"><table class="tbl">
        <thead><tr><th>${head}</th><th style="width:280px">Share</th><th class="num">Units</th><th class="num">Spend</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4"><div class="state"><div class="state__sub">No spend recorded yet.</div></div></td></tr>`}</tbody></table></div>`;
    };
    const avg = s.total_units ? Math.round(Number(s.total_spend) / s.total_units) : 0;
    $("#screen").innerHTML = `
      ${pageHead("Analytics", "Spend", "Computed from received assets via the never-broken asset → order → supplier link — so every euro traces to a physical unit.")}
      <div class="spend-summary">
        <div><div class="stat__label">Total spend tracked</div><div class="stat__val stat__val--gold">${euro(s.total_spend)}</div></div>
        <div><div class="stat__label">Units received</div><div class="stat__val">${s.total_units}</div></div>
        <div><div class="stat__label">Average per unit</div><div class="stat__val">${euro(avg)}</div></div>
      </div>
      <div class="section"><div class="section__head"><span class="section__title">By supplier</span><span class="section__count">${s.by_supplier.length}</span></div>${bars(s.by_supplier, "Supplier")}</div>
      <div class="section" style="margin-bottom:0"><div class="section__head"><span class="section__title">By category</span><span class="section__count">${s.by_category.length}</span></div>${bars(s.by_category, "Category")}</div>`;
  } catch (e) { $("#screen").innerHTML = errState(e.message); }
};

/* ── Wiring ────────────────────────────────────────────────────────── */
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try { await login($("#login-email").value, $("#login-password").value); }
  catch (err) { $("#login-error").textContent = err.message; }
});
// One-click read-only tour for casual visitors (seeded VIEWER account).
$("#guest-login").addEventListener("click", async (e) => {
  const btn = e.currentTarget; const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Signing in…";
  $("#login-error").textContent = "";
  try { await login("guest@example.com", "guest"); }
  catch (err) { $("#login-error").textContent = err.message; btn.disabled = false; btn.textContent = label; }
});
$("#logout").addEventListener("click", logout);
// Point the sidebar's SCM Analytics link at this environment's cockpit.
(() => { const a = $("#analytics-link"); if (a) a.href = ANALYTICS_URL; })();

// On production, hide demo-only login affordances (guest button + sample
// credentials hint). The backend reports this via the public /health endpoint.
fetch("/health").then((r) => r.json()).then((h) => {
  if (h && h.is_production) $$(".demo-only").forEach((el) => el.remove());
}).catch(() => {});

// Boot is triggered by the host page AFTER features.js has registered its
// nav items / agent button (see the init script in index.html).
window.__scmInit = function () { if (token) boot(); else logout(); };
