// Background recorder logic with chrome.* stubbed (no browser needed).
// Run: node --test extension/recorder/recorder-bg.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";

const L = {};
const on = (n) => ({ addListener: (f) => (L[n] = f) });
const store = {};
let registered = [];
let executed = [];
globalThis.chrome = {
  storage: {
    session: {
      get: async (k) => ({ [k]: store[k] }),
      set: async (o) => Object.assign(store, JSON.parse(JSON.stringify(o))),
    },
  },
  webRequest: { onBeforeRequest: on("before"), onSendHeaders: on("send"), onCompleted: on("done"), onErrorOccurred: on("err") },
  tabs: { onRemoved: on("removed") },
  runtime: { getManifest: () => ({ version: "9.9.9" }) },
  scripting: {
    getRegisteredContentScripts: async ({ ids }) => registered.filter((s) => ids.includes(s.id)),
    unregisterContentScripts: async ({ ids }) => {
      registered = registered.filter((s) => !ids.includes(s.id));
    },
    registerContentScripts: async (arr) => {
      registered.push(...arr);
    },
    executeScript: async (o) => {
      executed.push(o);
    },
  },
};

const { handleRecorderMessage } = await import("./recorder-bg.js");
const TAB = 7;
const msg = (m, tabId = TAB) => handleRecorderMessage(m, { tab: { id: tabId } });
const enc = (s) => new TextEncoder().encode(s).buffer;
let rid = 0;

function request({ method = "POST", url, tabId = TAB, initiator, body, headers = [], status = 200, ctype = "application/json" }) {
  const requestId = String(++rid);
  const t = Date.now();
  L.before({ requestId, tabId, initiator, method, url, type: "xmlhttprequest", timeStamp: t, requestBody: body ? { raw: [{ bytes: enc(body) }] } : undefined });
  L.send({ requestId, requestHeaders: headers });
  L.done({ requestId, statusCode: status, responseHeaders: [{ name: "Content-Type", value: ctype }] });
  return t;
}

test("ignores messages that are not recorder messages", async () => {
  assert.equal(await handleRecorderMessage({ type: "node:getConfig" }, {}), undefined);
});

test("refuses non-web pages", async () => {
  const res = await msg({ type: "rec:start", tabId: TAB, url: "chrome://extensions" });
  assert.equal(res.ok, false);
});

test("start registers a MAIN-world document_start hook for the site and injects now", async () => {
  const res = await msg({ type: "rec:start", tabId: TAB, url: "https://outlook.cloud.microsoft/mail/inbox" });
  assert.equal(res.ok, true);
  const main = registered.find((s) => s.world === "MAIN");
  assert.equal(main.runAt, "document_start");
  assert.deepEqual(main.matches, ["https://outlook.cloud.microsoft/*"]);
  assert.deepEqual(main.js, ["recorder/shape.js", "recorder/page-hook.js"]);
  assert.ok(executed.some((e) => e.world === "MAIN" && e.target.tabId === TAB));
  const st = await msg({ type: "rec:status" });
  assert.equal(st.active, true);
  assert.equal(st.thisTab, true);
});

test("network + page events merge into one redacted group; other tabs ignored", async () => {
  const url = "https://outlook.cloud.microsoft/owa/service.svc?action=FindConversation&app=Mail&n=12";
  const t = request({
    url,
    body: JSON.stringify({ __type: "FindConversationJsonRequest:#Exchange", Body: { QueryString: "from:Maria" } }),
    headers: [
      { name: "Authorization", value: "Bearer eyJSECRET" },
      { name: "action", value: "FindConversation" },
      { name: "x-owa-canary", value: "CANARYSECRET" },
      { name: "Cookie", value: "X-OWA-CANARY=CANARYSECRET; OpenIdConnect.token.v1=TOKSECRET" },
    ],
  });
  await msg({
    type: "rec:page",
    evt: { via: "fetch", method: "POST", url, t, status: 200, jsHeaderNames: ["action"], respShape: { Body: { Conversations: [{ ConversationTopic: "<text 9>", From: "<email>" }] } } },
  });
  request({ url: "https://evil.example/elsewhere?action=Leak", tabId: 99 });
  request({ method: "GET", url: "https://browser.events.data.microsoft.com/OneCollector/1.0/", ctype: "text/plain" });

  const { groups } = await msg({ type: "rec:groups" });
  assert.ok(!groups.some((g) => g.url.includes("evil.example")), "other tab's traffic must not be recorded");
  const g = groups.find((x) => x.key.includes("FindConversation"));
  assert.equal(g.count, 1, "page event attached to the network entry, not counted twice");
  assert.equal(g.suggested, true);
  assert.equal(g.messageList, true);
  assert.match(g.auth, /authorization header/);
  assert.match(g.auth, /OWA canary/);
  assert.deepEqual(g.cookieNames, ["OpenIdConnect.token.v1", "X-OWA-CANARY"]);
  assert.equal(g.requestHeaders.action, "FindConversation");
  assert.ok(g.responseBody, "response shape merged in from the page hook");
  const noise = groups.find((x) => x.url.includes("events.data.microsoft.com"));
  assert.equal(noise.noise, true);
  assert.equal(noise.suggested, false);
});

test("worker requests without a tab are attributed by initiator", async () => {
  request({ method: "GET", url: "https://outlook.cloud.microsoft/owa/startupdata.ashx?app=Mail", tabId: -1, initiator: "https://outlook.cloud.microsoft" });
  request({ method: "GET", url: "https://other.example/x", tabId: -1, initiator: "https://other.example" });
  const { groups } = await msg({ type: "rec:groups" });
  assert.ok(groups.some((g) => g.url.includes("startupdata.ashx")));
  assert.ok(!groups.some((g) => g.url.includes("other.example")));
});

test("only marked calls are exported, with labels, and no secrets", async () => {
  const { groups } = await msg({ type: "rec:groups" });
  const g = groups.find((x) => x.key.includes("FindConversation"));
  await msg({ type: "rec:mark", key: g.key, mark: "inbox list" });
  const { data } = await msg({ type: "rec:export" });
  assert.equal(data.tool, "node-api-recorder");
  assert.equal(data.version, "9.9.9");
  assert.equal(data.calls.length, 1);
  assert.equal(data.calls[0].label, "inbox list");
  assert.equal(data.calls[0].requestBody.__type, "FindConversationJsonRequest:#Exchange");
  const text = JSON.stringify(data);
  for (const secret of ["eyJSECRET", "CANARYSECRET", "TOKSECRET", "Maria"]) assert.ok(!text.includes(secret), `leaked ${secret}`);
  // Unmarking removes it.
  await msg({ type: "rec:mark", key: g.key, mark: null });
  assert.equal((await msg({ type: "rec:export" })).data.calls.length, 0);
});

test("stop unregisters the hooks; closing the recorded tab stops too", async () => {
  await msg({ type: "rec:stop" });
  assert.equal(registered.length, 0);
  assert.equal((await msg({ type: "rec:status" })).active, false);
  await msg({ type: "rec:start", tabId: TAB, url: "https://teams.microsoft.com/v2/" });
  assert.equal(registered.length, 2);
  await L.removed(TAB);
  assert.equal(registered.length, 0);
  assert.equal((await msg({ type: "rec:status" })).active, false);
});
