/**
 * newtab.js — controls which of the three new-tab modes renders.
 *
 *   dashboard  → full Node WebOS dashboard in an iframe
 *   custom     → this local minimal page (clock, quick add, tasks, urgent)
 *   wallpaper  → clock over the wallpaper only, nothing else
 *
 * The mode lives in the server's settings (`newtab_mode`) so it stays in sync
 * with the in-app toggle, and is mirrored to chrome.storage for instant boot.
 */

const DEFAULT_BASE = "http://127.0.0.1:8080";
const MODES = ["dashboard", "custom", "wallpaper"];

const $ = (id) => document.getElementById(id);

let baseUrl = DEFAULT_BASE;
let mode = "dashboard";

let guardToken = "";

async function readConfig() {
  try {
    const res = await chrome.runtime.sendMessage({ type: "node:getConfig" });
    if (res && res.ok && res.config) {
      baseUrl = String(res.config.nodeBaseUrl || DEFAULT_BASE).replace(/\/+$/, "");
      guardToken = String(res.config.guardToken || "");
      if (MODES.includes(res.config.newtabMode)) mode = res.config.newtabMode;
      return res.config;
    }
  } catch (_e) {}
  return {};
}

async function serverFetch(path, opts = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), opts.timeoutMs || 3500);
  try {
    const headers = { ...(opts.headers || {}) };
    if (guardToken) headers["X-Node-Guard-Token"] = guardToken;
    const res = await fetch(`${baseUrl}${path}`, { ...opts, headers, signal: ctrl.signal, credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(t);
  }
}

/** Server mode wins when reachable; the stored mirror is only the fast path. */
async function resolveMode() {
  try {
    const cfg = await serverFetch("/api/v1/newtab/config");
    if (cfg && MODES.includes(cfg.mode)) mode = cfg.mode;
    if (cfg && cfg.greeting_name) {
      $("greet").textContent = greeting(cfg.greeting_name);
    } else {
      $("greet").textContent = greeting("");
    }
    chrome.storage.local.set({ newtabMode: mode, serverOnline: true });
  } catch (_e) {
    chrome.storage.local.set({ serverOnline: false });
    $("hint").textContent = `Server unreachable at ${baseUrl} — showing offline clock.`;
  }
}

function greeting(name) {
  const h = new Date().getHours();
  const part = h < 5 ? "Late night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : h < 21 ? "Good evening" : "Good night";
  return name ? `${part}, ${name}` : part;
}

function tick() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("date").textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

function applyMode() {
  const showFrame = mode === "dashboard";
  document.body.classList.toggle("has-frame", showFrame);
  if (showFrame) {
    const frame = $("frame");
    const target = `${baseUrl}/`;
    if (frame.src !== target) frame.src = target;
  }
  // wallpaper = clock only; custom = clock + cards
  $("card-tasks").style.display = mode === "wallpaper" ? "none" : "";
  $("card-urgent").style.display = mode === "wallpaper" ? "none" : "";
  $("quick").style.display = mode === "wallpaper" ? "none" : "flex";
  $("hint").style.display = mode === "wallpaper" ? "none" : "";
  $("mode-chip").textContent = mode === "wallpaper" ? "" : `new tab · ${mode}`;
  chrome.storage.local.set({ newtabMode: mode });
}

async function cycleMode() {
  const idx = MODES.indexOf(mode);
  mode = MODES[(idx + 1) % MODES.length];
  applyMode();
  // Best-effort write-back so the in-app Settings toggle agrees.
  try {
    await serverFetch("/api/v1/newtab/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
  } catch (_e) {}
}

function prioRank(p) {
  return { critical: 0, urgent: 0, high: 1, medium: 2, low: 3 }[String(p || "").toLowerCase()] ?? 2;
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const days = Math.round((d - new Date()) / 86400000);
  if (Number.isNaN(days)) return "";
  if (days < 0) return `${Math.abs(days)}d overdue`;
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return `in ${days}d`;
}

async function loadPanels() {
  if (mode === "wallpaper") return;
  try {
    const boot = await serverFetch("/api/v1/ext/bootstrap");
    const list = (boot.tasks || [])
      .filter((t) => !t.completed)
      .sort((a, b) => prioRank(a.priority) - prioRank(b.priority))
      .slice(0, 5);
    $("tasks").innerHTML = list.length
      ? list.map((t) =>
          `<div class="row"><span class="dot ${prioRank(t.priority) === 0 ? "hot" : prioRank(t.priority) === 1 ? "warn" : ""}"></span>` +
          `<span>${escapeHtml(t.title)}</span>` +
          `${t.due_date ? `<span class="tag">${escapeHtml(relTime(t.due_date) || t.due_date)}</span>` : ""}</div>`
        ).join("")
      : `<div class="row muted">Nothing queued. Enjoy it.</div>`;
    const items = (boot.urgent || []);
    $("urgent").innerHTML = items.length
      ? items.map((n) =>
          `<div class="row"><span class="dot hot"></span>` +
          `<span>${escapeHtml(n.title || "")}</span>` +
          `<span class="tag">${escapeHtml((n.tags_csv || n.category || "").split(",")[0] || "")}</span></div>`
        ).join("")
      : `<div class="row muted">No urgent mail.</div>`;
  } catch (_e) {
    $("tasks").innerHTML = `<div class="row muted">Server unavailable</div>`;
    $("urgent").innerHTML = `<div class="row muted">Server unavailable</div>`;
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function quickAdd(text) {
  const value = String(text || "").trim();
  if (!value) return;
  $("hint").textContent = "Adding…";
  try {
    const res = await serverFetch("/api/v1/ext/quick-capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: value }),
      timeoutMs: 20000,
    });
    $("hint").textContent = res && res.reply ? res.reply : `Added as ${res && res.kind ? res.kind : "item"}.`;
    $("quick-input").value = "";
    loadPanels();
  } catch (err) {
    $("hint").textContent = `Could not reach Node (${String(err.message || err)}). Saved nothing.`;
  }
}

async function boot() {
  tick();
  setInterval(tick, 15000);
  await readConfig();
  await resolveMode();
  applyMode();
  loadPanels();
  setInterval(loadPanels, 60000);

  $("btn-dash").addEventListener("click", async () => {
    mode = "dashboard";
    applyMode();
    try {
      await serverFetch("/api/v1/newtab/config", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }),
      });
    } catch (_e) {}
  });
  $("btn-focus").addEventListener("click", cycleMode);
  $("btn-options").addEventListener("click", () => chrome.runtime.openOptionsPage());

  $("quick").addEventListener("submit", (e) => {
    e.preventDefault();
    quickAdd($("quick-input").value);
  });

  // Ctrl/Cmd+K focuses quick add, same as the in-app palette.
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      $("quick-input").focus();
    }
  });
}

boot();
