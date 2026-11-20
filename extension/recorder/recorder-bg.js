/**
 * Node API Recorder — background half (MV3 service worker module).
 *
 * While recording one tab it watches that tab's network traffic with
 * chrome.webRequest (sees every request, including ones made by the site's
 * service/web workers, plus header NAMES and request bodies) and merges in the
 * response-body shapes the page hook reports. Requests are grouped into one
 * row per call type, so a page that fires 300 requests shows ~20 rows.
 *
 * Nothing is uploaded anywhere. The user marks rows in the on-page panel and
 * saves a redacted JSON file locally.
 */
import "./shape.js";

const S = globalThis.NodeRecorderShape;
const STORE_KEY = "nodeApiRecorder";
const MAX_ENTRIES = 4000;
const SCRIPT_IDS = ["node-recorder-main", "node-recorder-panel"];

let state = blankState();
let loaded = false;
let saveTimer = null;

function blankState() {
  return { active: false, tabId: null, origin: "", startedAt: 0, entries: {}, order: [], marks: {}, seq: 0 };
}

async function load() {
  if (loaded) return;
  try {
    const got = await chrome.storage.session.get(STORE_KEY);
    if (got && got[STORE_KEY]) state = { ...blankState(), ...got[STORE_KEY] };
  } catch (_e) {}
  loaded = true;
}

function persistSoon() {
  if (saveTimer) return;
  saveTimer = setTimeout(async () => {
    saveTimer = null;
    try {
      await chrome.storage.session.set({ [STORE_KEY]: state });
    } catch (_e) {
      // Over quota: keep the newest half.
      state.order = state.order.slice(-Math.floor(MAX_ENTRIES / 2));
      const keep = new Set(state.order);
      for (const k of Object.keys(state.entries)) if (!keep.has(k)) delete state.entries[k];
      try { await chrome.storage.session.set({ [STORE_KEY]: state }); } catch (_e2) {}
    }
  }, 600);
}

function addEntry(id, entry) {
  if (!state.entries[id]) {
    state.order.push(id);
    if (state.order.length > MAX_ENTRIES) {
      const drop = state.order.shift();
      delete state.entries[drop];
    }
  }
  state.entries[id] = { ...(state.entries[id] || {}), ...entry };
  persistSoon();
}

function belongs(details) {
  if (!state.active) return false;
  if (details.tabId === state.tabId) return true;
  // Service-worker requests have no tab; attribute them by initiator.
  return details.tabId === -1 && !!state.origin && String(details.initiator || "") === state.origin;
}

function decodeRequestBody(rb) {
  if (!rb) return null;
  try {
    if (rb.formData) {
      const keys = {};
      for (const k of Object.keys(rb.formData)) keys[k] = "<value>";
      return { "<form>": keys };
    }
    if (rb.raw && rb.raw.length) {
      const chunks = rb.raw.filter((r) => r.bytes).map((r) => new Uint8Array(r.bytes));
      if (!chunks.length) return rb.raw.some((r) => r.file) ? "<file upload>" : null;
      const total = chunks.reduce((n, c) => n + c.length, 0);
      if (total > 3_000_000) return `<large body ${total} bytes>`;
      const buf = new Uint8Array(total);
      let off = 0;
      for (const c of chunks) { buf.set(c, off); off += c.length; }
      return S.shapeBodyText(new TextDecoder("utf-8", { fatal: false }).decode(buf));
    }
  } catch (_e) {}
  return null;
}

// ---------------------------------------------------------------- webRequest
// Listeners must be registered synchronously at startup (MV3), so they always
// exist and simply ignore traffic unless a recording is active.
const FILTER = { urls: ["<all_urls>"], types: ["xmlhttprequest", "websocket", "other", "ping"] };

chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    if (!loaded) return;
    if (!belongs(d)) return;
    addEntry(d.requestId, {
      t: d.timeStamp,
      method: d.method,
      url: d.url,
      type: d.type,
      noise: S.isNoise(d.url),
      reqShape: decodeRequestBody(d.requestBody),
    });
  },
  FILTER,
  ["requestBody"],
);

chrome.webRequest.onSendHeaders.addListener(
  (d) => {
    if (!loaded || !state.entries[d.requestId]) return;
    const h = S.summarizeHeaders(d.requestHeaders || []);
    const cur = state.entries[d.requestId];
    addEntry(d.requestId, {
      reqHeaderNames: h.names,
      safeHeaders: h.safe,
      cookieNames: h.cookieNames,
      reqShape: cur.reqShape || h.bodyFromHeader,
    });
  },
  FILTER,
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onCompleted.addListener(
  (d) => {
    if (!loaded || !state.entries[d.requestId]) return;
    const h = S.summarizeHeaders(d.responseHeaders || []);
    addEntry(d.requestId, { status: d.statusCode, respHeaderNames: h.names, ctype: h.safe["content-type"] || "" });
  },
  FILTER,
  ["responseHeaders", "extraHeaders"],
);

chrome.webRequest.onErrorOccurred.addListener(
  (d) => {
    if (!loaded || !state.entries[d.requestId]) return;
    addEntry(d.requestId, { status: "error", error: String(d.error || "").slice(0, 80) });
  },
  FILTER,
);

load();

// ------------------------------------------------------------- page events
function onPageEvent(evt, senderTabId) {
  if (!state.active || senderTabId !== state.tabId || !evt || !evt.url) return;
  const method = String(evt.method || "GET").toUpperCase();
  // Attach to the matching network entry (same method + URL, close in time).
  let match = null;
  for (let i = state.order.length - 1, seen = 0; i >= 0 && seen < 400; i--, seen++) {
    const e = state.entries[state.order[i]];
    if (e && !e.respShape && !e.pageSeen && e.method === method && e.url === evt.url && Math.abs((e.t || 0) - (evt.t || 0)) < 60000) {
      match = state.order[i];
      break;
    }
  }
  const patch = {
    pageSeen: true,
    respShape: evt.respShape || null,
    jsHeaderNames: evt.jsHeaderNames || [],
    opName: evt.opName || "",
  };
  if (match) {
    const cur = state.entries[match];
    addEntry(match, { ...patch, reqShape: cur.reqShape || evt.reqShape || null });
  } else {
    state.seq += 1;
    addEntry(`page-${state.seq}`, {
      ...patch,
      t: evt.t || Date.now(),
      method,
      url: evt.url,
      type: evt.via,
      status: evt.status,
      noise: S.isNoise(evt.url),
      reqShape: evt.reqShape || null,
    });
  }
}

// ------------------------------------------------------------------ groups
function buildGroups() {
  const groups = new Map();
  for (const id of state.order) {
    const e = state.entries[id];
    if (!e || !e.url) continue;
    const key = S.groupKey(e.method, e.url, e.opName);
    let g = groups.get(key);
    if (!g) {
      g = {
        key, method: e.method, url: S.redactUrl(e.url), count: 0, ts: [], statuses: [], type: e.type,
        reqHeaderNames: new Set(), jsHeaderNames: new Set(), respHeaderNames: new Set(), cookieNames: new Set(),
        safeHeaders: {}, reqShape: null, respShape: null, noise: !!e.noise,
      };
      groups.set(key, g);
    }
    g.count += 1;
    g.ts.push(e.t || 0);
    if (e.status !== undefined && !g.statuses.includes(e.status)) g.statuses.push(e.status);
    (e.reqHeaderNames || []).forEach((n) => g.reqHeaderNames.add(n));
    (e.jsHeaderNames || []).forEach((n) => g.jsHeaderNames.add(n));
    (e.respHeaderNames || []).forEach((n) => g.respHeaderNames.add(n));
    (e.cookieNames || []).forEach((n) => g.cookieNames.add(n));
    Object.assign(g.safeHeaders, e.safeHeaders || {});
    if (!g.reqShape && e.reqShape) g.reqShape = e.reqShape;
    if (!g.respShape && e.respShape) g.respShape = e.respShape;
    g.noise = g.noise && !!e.noise;
  }
  const out = [];
  for (const g of groups.values()) {
    const reqNames = Array.from(new Set([...g.reqHeaderNames, ...g.jsHeaderNames])).sort();
    out.push({
      key: g.key,
      method: g.method,
      url: g.url,
      type: g.type,
      count: g.count,
      firstT: Math.min(...g.ts),
      lastT: Math.max(...g.ts),
      ts: g.ts.slice(-40),
      statuses: g.statuses.slice(0, 6),
      requestHeaderNames: reqNames,
      requestHeaders: g.safeHeaders,
      cookieNames: Array.from(g.cookieNames).sort(),
      responseHeaderNames: Array.from(g.respHeaderNames).sort(),
      requestBody: g.reqShape,
      responseBody: g.respShape,
      auth: authKind(reqNames, g.cookieNames.size),
      noise: g.noise,
      suggested: !g.noise && S.isSuggested(g.url, g.respShape),
      messageList: !!(g.respShape && S.looksLikeMessageList(g.respShape)),
      mark: Object.prototype.hasOwnProperty.call(state.marks, g.key) ? state.marks[g.key] : null,
    });
  }
  out.sort((a, b) => Number(b.suggested) - Number(a.suggested) || b.lastT - a.lastT);
  return out;
}

function authKind(names, cookieCount) {
  const has = (n) => names.includes(n);
  const bits = [];
  if (has("authorization")) bits.push("authorization header");
  if (has("authentication") || has("x-skypetoken")) bits.push("skype token header");
  if (has("x-owa-canary")) bits.push("OWA canary");
  if (has("x-csrf-token") || has("x-xsrf-token") || has("x-csrftoken")) bits.push("CSRF token");
  if (cookieCount) bits.push("cookies");
  return bits.join(" + ") || "none seen";
}

function buildExport() {
  const groups = buildGroups().filter((g) => g.mark !== null);
  return {
    tool: "node-api-recorder",
    version: chrome.runtime.getManifest().version,
    recordedAt: new Date().toISOString(),
    site: state.origin,
    privacy:
      "Structure only: header names (values only for protocol headers like action/content-type), cookie NAMES, " +
      "redacted URLs and JSON shapes with personal text replaced by <text N>. No tokens, cookies or message content.",
    calls: groups.map((g) => ({
      label: g.mark || "",
      method: g.method,
      url: g.url,
      count: g.count,
      statuses: g.statuses,
      auth: g.auth,
      requestHeaderNames: g.requestHeaderNames,
      requestHeaders: g.requestHeaders,
      cookieNames: g.cookieNames,
      responseHeaderNames: g.responseHeaderNames,
      requestBody: g.requestBody,
      responseBody: g.responseBody,
    })),
  };
}

// ----------------------------------------------------------- start / stop
function originPattern(origin) {
  return `${origin}/*`;
}

async function unregisterScripts() {
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids: SCRIPT_IDS });
    if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: existing.map((s) => s.id) });
  } catch (_e) {}
}

async function start(tabId, url) {
  await load();
  let origin = "";
  try { origin = new URL(url).origin; } catch (_e) {}
  if (!/^https?:/.test(origin)) return { ok: false, error: "Open the site you want to record first (an http/https page)." };
  await unregisterScripts();
  // Re-inject on every load of this site while recording, at document_start so
  // the calls a page makes while it starts (the inbox list!) are caught too.
  await chrome.scripting.registerContentScripts([
    { id: SCRIPT_IDS[0], matches: [originPattern(origin)], js: ["recorder/shape.js", "recorder/page-hook.js"], runAt: "document_start", world: "MAIN", persistAcrossSessions: false },
    { id: SCRIPT_IDS[1], matches: [originPattern(origin)], js: ["recorder/panel.js"], runAt: "document_idle", persistAcrossSessions: false },
  ]);
  const fresh = state.origin !== origin;
  state = { ...(fresh ? blankState() : state), active: true, tabId, origin, startedAt: state.startedAt && !fresh ? state.startedAt : Date.now() };
  persistSoon();
  // And right now, without a reload.
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["recorder/shape.js", "recorder/page-hook.js"], world: "MAIN" });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["recorder/panel.js"] });
  } catch (err) {
    return { ok: true, warning: `Recording, but the panel could not be shown on this page: ${err}` };
  }
  return { ok: true };
}

async function stop() {
  await load();
  state.active = false;
  persistSoon();
  await unregisterScripts();
  return { ok: true };
}

function status() {
  const total = state.order.length;
  return { ok: true, active: state.active, tabId: state.tabId, origin: state.origin, startedAt: state.startedAt, total };
}

/** Handle `rec:*` messages. Returns undefined for messages that aren't ours. */
export async function handleRecorderMessage(msg, sender) {
  if (!msg || typeof msg.type !== "string" || !msg.type.startsWith("rec:")) return undefined;
  await load();
  const senderTab = sender && sender.tab ? sender.tab.id : null;
  switch (msg.type) {
    case "rec:start":
      return start(msg.tabId, msg.url);
    case "rec:stop":
      return stop();
    case "rec:status":
      return { ...status(), thisTab: senderTab !== null && senderTab === state.tabId };
    case "rec:page":
      onPageEvent(msg.evt, senderTab);
      return { ok: true };
    case "rec:groups":
      return { ...status(), groups: buildGroups() };
    case "rec:mark":
      if (msg.mark === null) delete state.marks[msg.key];
      else state.marks[msg.key] = String(msg.mark || "").slice(0, 80);
      persistSoon();
      return { ok: true };
    case "rec:clear":
      state = { ...blankState(), active: state.active, tabId: state.tabId, origin: state.origin, startedAt: Date.now() };
      persistSoon();
      return { ok: true };
    case "rec:export":
      return { ok: true, data: buildExport() };
    default:
      return { ok: false, error: "unknown recorder message" };
  }
}

// Recording follows the tab: if it closes, stop.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await load();
  if (state.active && tabId === state.tabId) await stop();
});
