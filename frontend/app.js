"use strict";
// Minimal dependency-free operations UI over the /api/v1 surface.
// Token is kept in localStorage; every call attaches it as a Bearer header.

const API = "/api/v1";
const $ = (sel) => document.querySelector(sel);
let token = localStorage.getItem("scm_token") || "";

// Legal next states per current asset status (mirrors the backend state machine,
// only to decide which action buttons to show — the server still enforces it).
const NEXT = {
  RECEIVED: ["IN_STORAGE", "DEPLOYED"],
  IN_STORAGE: ["DEPLOYED"],
  DEPLOYED: ["MAINTENANCE", "DECOMMISSIONED"],
  MAINTENANCE: ["DEPLOYED", "DECOMMISSIONED"],
  DECOMMISSIONED: ["DISPOSED"],
  DISPOSED: [],
};

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) { payload = new URLSearchParams(form); }
  else if (body) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(API + path, { method, headers, body: payload });
  if (res.status === 401) { logout(); throw new Error("Session expired"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 2600);
}

// --- auth ---------------------------------------------------------------
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
  $("#whoami").textContent = "";
}

// --- rendering ----------------------------------------------------------
let LOCATIONS = {};

async function loadAssets() {
  const status = $("#asset-status-filter").value;
  const assets = await api("/assets" + (status ? `?status=${status}` : ""));
  const tbody = $("#assets-table tbody");
  tbody.innerHTML = "";
  for (const a of assets) {
    const tr = document.createElement("tr");
    const loc = LOCATIONS[a.current_location_id] || "—";
    tr.innerHTML = `
      <td><code>${a.serial_number}</code></td>
      <td><span class="badge ${a.status}">${a.status}</span></td>
      <td>${loc}</td>
      <td><button class="ghost" data-prov="${a.id}">trace</button></td>
      <td><div class="actions"></div></td>`;
    const actions = tr.querySelector(".actions");
    for (const next of NEXT[a.status] || []) {
      const b = document.createElement("button");
      b.textContent = "→ " + next;
      b.onclick = () => transition(a.id, next);
      actions.appendChild(b);
    }
    tr.querySelector("[data-prov]").onclick = () => showProvenance(a.id);
    tbody.appendChild(tr);
  }
}

async function transition(assetId, target) {
  // Deploying wants a rack; pick the first RACK location for this demo UI.
  let location_id;
  if (target === "DEPLOYED") {
    location_id = Object.entries(LOCATIONS).find(([, name]) => /rack/i.test(name))?.[0];
  }
  try {
    await api(`/assets/${assetId}/transition`, { method: "POST", body: { target, location_id } });
    toast(`Asset → ${target}`);
    loadAssets();
  } catch (e) { toast(e.message, true); }
}

async function showProvenance(assetId) {
  try {
    const p = await api(`/assets/${assetId}/provenance`);
    toast(`${p.serial_number}: ${p.order_number || "—"} · ${p.supplier_name || "—"} · €${p.unit_price || "?"}`);
  } catch (e) { toast(e.message, true); }
}

async function loadInbound() {
  const rows = await api("/planning/inbound");
  const tbody = $("#inbound-table tbody");
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td>${r.order_number}</td>
      <td><span class="badge">${r.order_status}</span></td>
      <td>${r.ordered}</td><td>${r.received}</td><td>${r.outstanding}</td>
      <td>${r.estimated_delivery_date || "—"}</td>
      <td>${r.overdue ? '<span class="over">overdue</span>' : ""}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="hint">Nothing inbound.</td></tr>`;
}

async function loadCapacity() {
  const rows = await api("/planning/capacity");
  $("#capacity-table tbody").innerHTML = rows.map((r) => `
    <tr>
      <td><code>${r.code}</code></td><td>${r.name}</td><td>${r.location_type}</td>
      <td class="${r.over_capacity ? "over" : ""}">${r.used}</td>
      <td>${r.capacity ?? "—"}</td>
      <td>${r.utilisation != null ? Math.round(r.utilisation * 100) + "%" : "—"}</td>
    </tr>`).join("");
}

async function loadSpend() {
  const s = await api("/analytics/spend");
  $("#spend-summary").innerHTML = `
    <div><div class="n">${s.total_units}</div>units received</div>
    <div><div class="n">€${s.total_spend}</div>total spend</div>`;
  $("#spend-supplier-table tbody").innerHTML = s.by_supplier.map((r) =>
    `<tr><td>${r.supplier_name || "—"}</td><td>${r.units}</td><td>€${r.spend}</td></tr>`).join("")
    || `<tr><td colspan="3" class="hint">No spend yet.</td></tr>`;
  $("#spend-category-table tbody").innerHTML = s.by_category.map((r) =>
    `<tr><td>${r.category}</td><td>${r.units}</td><td>€${r.spend}</td></tr>`).join("");
}

const LOADERS = { assets: loadAssets, inbound: loadInbound, capacity: loadCapacity, spend: loadSpend };

function showTab(name) {
  document.querySelectorAll(".tabs button[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  $(`#tab-${name}`).classList.remove("hidden");
  LOADERS[name]?.();
}

// --- boot ---------------------------------------------------------------
async function boot() {
  try {
    const me = await api("/auth/me");
    $("#whoami").textContent = `${me.email} · ${me.role}`;
    const locs = await api("/locations");
    LOCATIONS = Object.fromEntries(locs.map((l) => [l.id, l.name]));
    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    showTab("assets");
  } catch {
    logout();
  }
}

// --- wiring -------------------------------------------------------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try { await login($("#login-email").value, $("#login-password").value); }
  catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", logout);
$("#asset-refresh").addEventListener("click", loadAssets);
$("#asset-status-filter").addEventListener("change", loadAssets);
document.querySelectorAll(".tabs button[data-tab]").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

if (token) boot(); else logout();
