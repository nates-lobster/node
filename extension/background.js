/**
 * Node WebOS — background service worker (MV3).
 *
 * Two jobs, both opt-in and scoped to the user's own server:
 *   1. Keep the server's session alive by copying the cookies Brave/Chromium
 *      already holds for the LMS / mail providers. The server never sees the
 *      whole jar, only the hostnames the user enabled.
 *   2. Run periodic "heartbeats" so the newtab page knows the server is up.
 *
 * Nothing is ever sent to a third party. `nodeBaseUrl` is user-configured
 * and must be a loopback/Tailscale/private address by default.
 */

const DEFAULTS = {
  nodeBaseUrl: "http://127.0.0.1:8080",
  guardToken: "",
  syncEnabled: true,
  syncIntervalMinutes: 15,
  captureDomains: [
    "powerschool.com",
    "schoology.com",
    "instructure.com",
    "classroom.google.com",
    "mail.google.com",
    "outlook.office.com",
    "outlook.live.com"
  ],
  lastSync: null,
  lastSyncStatus: "never",
  lastError: ""
};

async function getConfig() {
  const stored = await chrome.storage.local.get(null);
  return { ...DEFAULTS, ...stored };
}

async function setConfig(patch) {
  await chrome.storage.local.set(patch);
  return getConfig();
}

/** Strip a leading dot and any port from a hostname. */
function normalizeHost(host) {
  return String(host || "").replace(/^\./, "").replace(/:\d+$/, "").toLowerCase();
}

function matchesDomain(host, domains) {
  const h = normalizeHost(host);
  return domains.some((d) => {
    const dom = normalizeHost(d);
    return h === dom || h.endsWith("." + dom);
  });
}

/**
 * Collect cookies for every enabled domain the browser has a session for.
 * `partitionKey` is preserved so Brave's partitioned cookies keep working.
 */
async function collectCookies(domains) {
  const out = [];
  for (const domain of domains) {
    let cookies = [];
    try {
      cookies = await chrome.cookies.getAll({ domain: normalizeHost(domain) });
    } catch (err) {
      // A malformed domain should not abort the whole sync.
      console.warn("[node] cookie read failed for", domain, err);
      continue;
    }
    for (const c of cookies) {
      // Never forward HttpOnly *nothing* — we do want HttpOnly session cookies,
      // that is the entire point. But drop obviously third-party trackers.
      if (/^(_ga|_gid|_fbp|IDE|ANID)$/.test(c.name)) continue;
      out.push({
        domain: c.domain,
        name: c.name,
        value: c.value,
        path: c.path,
        secure: c.secure,
        httpOnly: c.httpOnly,
        sameSite: c.sameSite,
        expirationDate: c.expirationDate || null,
        hostOnly: c.hostOnly
      });
    }
  }
  return out;
}

async function syncSession(reason = "manual") {
  const cfg = await getConfig();
  if (!cfg.syncEnabled && reason !== "manual") {
    return { ok: false, skipped: "disabled" };
  }
  const base = String(cfg.nodeBaseUrl || "").replace(/\/+$/, "");
  if (!base) return { ok: false, skipped: "no_server" };

  try {
    const cookies = await collectCookies(cfg.captureDomains);
    const headers = { "Content-Type": "application/json" };
    if (cfg.guardToken) headers["X-Node-Guard-Token"] = cfg.guardToken;
    const res = await fetch(`${base}/api/v1/ext/cookies`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        source: "extension",
        reason,
        captured_at: new Date().toISOString(),
        cookies
      })
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${text.slice(0, 160)}`);
    }
    const body = await res.json().catch(() => ({}));
    await setConfig({
      lastSync: new Date().toISOString(),
      lastSyncStatus: "ok",
      lastError: "",
      lastCookieCount: cookies.length,
      lastAccepted: body.accepted ?? cookies.length
    });
    await updateBadge("ok");
    return { ok: true, count: cookies.length, body };
  } catch (err) {
    await setConfig({
      lastSync: new Date().toISOString(),
      lastSyncStatus: "error",
      lastError: String(err && err.message ? err.message : err)
    });
    await updateBadge("error");
    return { ok: false, error: String(err) };
  }
}

async function updateBadge(state) {
  try {
    const color = state === "ok" ? "#2bfbae" : state === "error" ? "#fb7185" : "#64748b";
    await chrome.action.setBadgeBackgroundColor({ color });
    await chrome.action.setBadgeText({ text: state === "ok" ? "✓" : state === "error" ? "!" : "" });
  } catch (_e) {}
}

/** Schedule the periodic sync (alarms survive service-worker restarts). */
async function ensureAlarm() {
  const cfg = await getConfig();
  const minutes = Math.max(5, Number(cfg.syncIntervalMinutes) || 15);
  const existing = await chrome.alarms.get("node-session-sync");
  if (!existing || existing.periodInMinutes !== minutes) {
    await chrome.alarms.create("node-session-sync", { periodInMinutes: minutes, delayInMinutes: 1 });
  }
}

chrome.runtime.onInstalled.addListener(async (details) => {
  await ensureAlarm();
  if (details.reason === "install") {
    // First run: open the options page so the server URL can be confirmed.
    chrome.runtime.openOptionsPage();
  }
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  syncSession("startup");
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "node-session-sync") syncSession("alarm");
});

/**
 * Capture opportunistically: whenever the user actually visits one of the
 * tracked domains, the cookie jar is freshest right then. Debounced to at
 * most one sync per 60s so a page with 30 requests doesn't spam the server.
 */
let lastSweep = 0;
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete" || !tab || !tab.url) return;
  const cfg = await getConfig();
  let host = "";
  try { host = new URL(tab.url).hostname; } catch (_e) { return; }
  if (!matchesDomain(host, cfg.captureDomains)) return;
  const now = Date.now();
  if (now - lastSweep < 60000) return;
  lastSweep = now;
  syncSession(`visit:${normalizeHost(host)}`);
});

// Message bridge for newtab.html / popup.html / options.html.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (!msg || typeof msg.type !== "string") return sendResponse({ ok: false });
      if (msg.type === "node:getConfig") return sendResponse({ ok: true, config: await getConfig() });
      if (msg.type === "node:setConfig") return sendResponse({ ok: true, config: await setConfig(msg.patch || {}), then: await ensureAlarm() });
      if (msg.type === "node:syncNow") return sendResponse(await syncSession("manual"));
      if (msg.type === "node:collectCookies") {
        const cfg = await getConfig();
        return sendResponse({ ok: true, cookies: await collectCookies(cfg.captureDomains) });
      }
      if (msg.type === "node:openServer") {
        const cfg = await getConfig();
        await chrome.tabs.create({ url: cfg.nodeBaseUrl });
        return sendResponse({ ok: true });
      }
      return sendResponse({ ok: false, error: "unknown message" });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return true; // async response
});
