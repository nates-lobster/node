const $ = (id) => document.getElementById(id);

function relTime(iso) {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60000) return "just now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return `${Math.floor(diff / 86400000)}d ago`;
}

async function paint() {
  const res = await chrome.runtime.sendMessage({ type: "node:getConfig" });
  const cfg = (res && res.config) || {};
  $("base").textContent = cfg.nodeBaseUrl || "—";
  const status = cfg.lastSyncStatus || "never";
  const pill = $("status");
  pill.textContent = status === "ok" ? "Synced" : status === "error" ? "Error" : "Idle";
  pill.className = "pill " + (status === "ok" ? "ok" : status === "error" ? "err" : "idle");
  $("last").textContent = relTime(cfg.lastSync);
  $("count").textContent = String(cfg.lastCookieCount ?? 0);
  if (cfg.lastError) $("last").title = cfg.lastError;
}

$("sync").addEventListener("click", async () => {
  $("sync").textContent = "Syncing…";
  const res = await chrome.runtime.sendMessage({ type: "node:syncNow" });
  $("sync").textContent = res && res.ok ? `Synced ${res.count} cookie(s)` : "Sync failed — retry";
  setTimeout(() => { $("sync").textContent = "Sync session now"; paint(); }, 1600);
  paint();
});
$("open").addEventListener("click", () => chrome.runtime.sendMessage({ type: "node:openServer" }));
$("opts").addEventListener("click", () => chrome.runtime.openOptionsPage());

// ---------------------------------------------------------- API Recorder
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function paintRecorder() {
  const st = await chrome.runtime.sendMessage({ type: "rec:status" });
  const tab = await activeTab();
  const here = st && st.active && tab && st.tabId === tab.id;
  const pill = $("rec-status");
  pill.textContent = st && st.active ? (here ? `Recording · ${st.total} calls` : "Recording another tab") : "Off";
  pill.className = "pill " + (st && st.active ? "ok" : "idle");
  $("rec-toggle").textContent = st && st.active ? "Stop recording" : "Record this tab";
  $("rec-reload").style.display = here ? "block" : "none";
}

$("rec-toggle").addEventListener("click", async () => {
  const st = await chrome.runtime.sendMessage({ type: "rec:status" });
  if (st && st.active) {
    await chrome.runtime.sendMessage({ type: "rec:stop" });
  } else {
    const tab = await activeTab();
    const res = await chrome.runtime.sendMessage({ type: "rec:start", tabId: tab.id, url: tab.url });
    $("rec-hint").textContent = res && res.ok ? (res.warning || "Recording. Reload the tab once, then use the panel on the page.") : (res && res.error) || "Could not start.";
  }
  paintRecorder();
});
$("rec-reload").addEventListener("click", async () => {
  const tab = await activeTab();
  if (tab) chrome.tabs.reload(tab.id);
  window.close();
});

paint();
paintRecorder();
