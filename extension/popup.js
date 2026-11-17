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

paint();
