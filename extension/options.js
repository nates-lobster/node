const $ = (id) => document.getElementById(id);
let cfg = {};

async function load() {
  const res = await chrome.runtime.sendMessage({ type: "node:getConfig" });
  cfg = (res && res.config) || {};
  $("base").value = cfg.nodeBaseUrl || "http://127.0.0.1:8080";
  $("sync-enabled").checked = cfg.syncEnabled !== false;
  $("interval").value = cfg.syncIntervalMinutes || 15;
  $("mode").value = cfg.newtabMode || "custom";
  $("domains").value = (cfg.captureDomains || []).join("\n");
  $("token").value = cfg.guardToken || "";
}

async function save() {
  const patch = {
    nodeBaseUrl: $("base").value.trim().replace(/\/+$/, "") || "http://127.0.0.1:8080",
    guardToken: $("token").value.trim(),
    syncEnabled: $("sync-enabled").checked,
    syncIntervalMinutes: Math.min(240, Math.max(5, Number($("interval").value) || 15)),
    newtabMode: $("mode").value,
    captureDomains: $("domains").value
      .split(/\n+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean)
  };
  await chrome.runtime.sendMessage({ type: "node:setConfig", patch });
  // Keep the server's own setting in agreement with the extension.
  try {
    if (patch.newtabMode) {
      const headers = { "Content-Type": "application/json" };
      if (patch.guardToken) headers["X-Node-Guard-Token"] = patch.guardToken;
      await fetch(`${patch.nodeBaseUrl}/api/v1/newtab/config`, {
        method: "POST",
        headers,
        body: JSON.stringify({ mode: patch.newtabMode })
      });
    }
  } catch (_e) {}
  $("save-state").textContent = "Saved";
  setTimeout(() => { $("save-state").textContent = ""; }, 1800);
}

$("save").addEventListener("click", save);
$("sync-now").addEventListener("click", async () => {
  $("save-state").textContent = "Syncing…";
  const res = await chrome.runtime.sendMessage({ type: "node:syncNow" });
  $("save-state").textContent = res && res.ok ? `Synced ${res.count} cookie(s)` : `Failed: ${(res && res.error) || "unknown"}`;
  setTimeout(() => { $("save-state").textContent = ""; }, 3200);
});

load();
