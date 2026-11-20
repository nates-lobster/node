// Run: node --test extension/recorder/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

createRequire(import.meta.url)("./shape.js");
const S = globalThis.NodeRecorderShape;

test("header values are dropped except protocol headers; cookie values never kept", () => {
  const h = S.summarizeHeaders([
    { name: "Authorization", value: "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOi.secret.sig" },
    { name: "action", value: "FindConversation" },
    { name: "Content-Type", value: "application/json; charset=utf-8" },
    { name: "Cookie", value: "X-OWA-CANARY=abc123secret; ClientId=XYZ987; MSFPC=GUID=1" },
    { name: "x-owa-canary", value: "abc123secret" },
  ]);
  assert.deepEqual(h.names, ["action", "authorization", "content-type", "cookie", "x-owa-canary"]);
  assert.deepEqual(h.safe, { action: "FindConversation", "content-type": "application/json; charset=utf-8" });
  assert.deepEqual(h.cookieNames, ["ClientId", "MSFPC", "X-OWA-CANARY"]);
  const dump = JSON.stringify(h);
  assert.ok(!dump.includes("secret") && !dump.includes("eyJ0") && !dump.includes("XYZ987"));
});

test("OWA body carried in x-owa-urlpostdata is shaped, not copied", () => {
  const body = { __type: "FindConversationJsonRequest:#Exchange", Header: { RequestServerVersion: "V2018_01_08" }, Body: { QueryString: "from:Ms Lee" } };
  const h = S.summarizeHeaders([{ name: "x-owa-urlpostdata", value: encodeURIComponent(JSON.stringify(body)) }]);
  assert.equal(h.bodyFromHeader.__type, "FindConversationJsonRequest:#Exchange");
  assert.equal(h.bodyFromHeader.Header.RequestServerVersion, "V2018_01_08");
  assert.equal(h.bodyFromHeader.Body.QueryString, "<text 11>");
});

test("JSON shapes keep structure and protocol words, drop personal content", () => {
  const sh = S.shape({
    Body: {
      ResponseMessages: {
        Items: [
          {
            Conversation: [
              {
                ConversationTopic: "Lab report due Friday",
                ConversationId: { Id: "AAQkAGI2NGVhZTVlLTI3ZjUtNDQ1Ni1hYjE0LWIyMjY3ZmE5" },
                UniqueSenders: ["Maria Lee"],
                From: { Mailbox: { EmailAddress: "mlee@thedps.org", Name: "John" } },
                UnreadCount: 1,
                HasAttachments: false,
                LastDeliveryTime: "2026-11-19T14:02:11Z",
                Importance: "Normal",
                ItemClasses: ["IPM.Note"],
              },
            ],
          },
        ],
      },
    },
  });
  const conv = sh.Body.ResponseMessages.Items[0].Conversation[0];
  assert.equal(conv.ConversationTopic, "<text 21>");
  assert.equal(conv.ConversationId.Id, "<text 48>");
  assert.deepEqual(conv.UniqueSenders, ["<text 9>", "<1 items>"]);
  assert.equal(conv.From.Mailbox.EmailAddress, "<email>");
  assert.equal(conv.From.Mailbox.Name, "<text 4>"); // single plain word could be a name
  assert.equal(conv.UnreadCount, "<int>");
  assert.equal(conv.HasAttachments, false);
  assert.equal(conv.LastDeliveryTime, "<datetime>");
  assert.deepEqual(conv.ItemClasses, ["IPM.Note", "<1 items>"]);
  assert.ok(S.looksLikeMessageList(sh));
  const dump = JSON.stringify(sh);
  for (const secret of ["Lab report", "Maria", "mlee", "AAQkAGI2", "John"]) assert.ok(!dump.includes(secret), secret);
});

test("URLs keep path and protocol query values, mask ids and personal values", () => {
  assert.equal(
    S.redactUrl("https://outlook.cloud.microsoft/owa/service.svc?action=GetConversationItems&UA=0&app=Mail&n=58"),
    "https://outlook.cloud.microsoft/owa/service.svc?action=GetConversationItems&UA=0&app=Mail&n=58",
  );
  assert.equal(
    S.redactUrl("https://teams.microsoft.com/api/chatsvc/amer/v1/users/ME/conversations/19:abc123def456@thread.v2/messages?startTime=1&pageSize=200&syncState=eyJsYXN0U3luYyI6MTIzNDU2Nzg5MH0abcdef"),
    "https://teams.microsoft.com/api/chatsvc/amer/v1/users/ME/conversations/{id}/messages?startTime=1&pageSize=200&syncState=…",
  );
  assert.equal(S.redactUrl("https://x.schoology.com/iapi/user/12345678/inbox?q=lee%40thedps.org"), "https://x.schoology.com/iapi/user/{number}/inbox?q=…");
});

test("grouping collapses repeats into one row per call type", () => {
  const a = S.groupKey("post", "https://outlook.cloud.microsoft/owa/service.svc?action=GetItem&n=55");
  const b = S.groupKey("POST", "https://outlook.cloud.microsoft/owa/service.svc?action=GetItem&n=61");
  const c = S.groupKey("POST", "https://outlook.cloud.microsoft/owa/service.svc?action=FindConversation&n=12");
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.equal(a, "POST outlook.cloud.microsoft/owa/service.svc · GetItem");
});

test("noise is hidden and likely-useful calls are suggested", () => {
  assert.ok(S.isNoise("https://browser.events.data.microsoft.com/OneCollector/1.0/"));
  assert.ok(S.isNoise("https://outlook.cloud.microsoft/owa/ev.owa2?ns=PerfMark"));
  assert.ok(S.isNoise("https://res.cdn.office.net/owamail/app.js?v=1"));
  assert.ok(!S.isNoise("https://outlook.cloud.microsoft/owa/service.svc?action=GetItem"));
  assert.ok(S.isSuggested("https://outlook.cloud.microsoft/owa/service.svc?action=GetItem"));
  assert.ok(S.isSuggested("https://example.com/whatever", { items: [{ subject: "<text 3>", from: "<email>" }, "<3 items>"] }));
  assert.ok(!S.isSuggested("https://example.com/whatever", { ok: true }));
});

test("form bodies keep keys (plus OAuth protocol values); opaque bodies are summarized", () => {
  assert.deepEqual(S.shapeBodyText("grant_type=refresh_token&refresh_token=SECRET&client_id=abc&name=Maria"), {
    "<form>": { grant_type: "refresh_token", refresh_token: "<value>", client_id: "<text 3>", name: "<value>" },
  });
  assert.equal(S.shapeBodyText("\u0000binary"), "<non-JSON body 7 chars>");
  assert.equal(S.shapeBodyText(""), null);
});

test("search strings, usernames and names under content keys are masked even if code-like", () => {
  const out = S.shape({
    Body: { QueryString: "from:Maria", Folder: "Inbox", Paging: { BasePoint: "Beginning" } },
    Owner: { Name: "McDonald", Upn: "john.smith", Id: "jsmith_2024" },
    ItemShape: { BaseShape: "IdOnly" },
    RequestServerVersion: "V2018_01_08",
    __type: "FindConversationJsonRequest:#Exchange",
    note: "john.smith",
  });
  assert.equal(out.Body.QueryString, "<text 10>");
  assert.equal(out.Owner.Name, "<text 8>");
  assert.equal(out.Owner.Upn, "<text 10>");
  assert.equal(out.note, "<text 10>");
  assert.equal(out.Owner.Id, "jsmith_2024", "non-content keys keep code-like values");
  assert.equal(out.ItemShape.BaseShape, "IdOnly");
  assert.equal(out.RequestServerVersion, "V2018_01_08");
  assert.equal(out.__type, "FindConversationJsonRequest:#Exchange");
  assert.equal(S.isIdentifierLike("john.smith"), false);
  assert.equal(S.isIdentifierLike("from:maria"), false);
  assert.equal(S.isIdentifierLike("John.Smith"), false);
  assert.equal(S.isIdentifierLike("IPM.Schedule.Meeting.Request"), true);
});

test("OAuth protocol values are kept, tokens and personal values are not", () => {
  const form = S.shapeBodyText(
    "client_id=5e3ce6c0-2b1f-4285-8d4b-75ee78787346&redirect_uri=https%3A%2F%2Fteams.cloud.microsoft%2Fv2&" +
      "scope=https%3A%2F%2Fic3.teams.office.com%2F.default%20openid%20profile%20offline_access&grant_type=refresh_token&" +
      "refresh_token=0.ARoAsecret&X-AnchorMailbox=Oid%3Aabc%40tenant&client_info=1",
  )["<form>"];
  assert.equal(form.client_id, "5e3ce6c0-2b1f-4285-8d4b-75ee78787346");
  assert.equal(form.grant_type, "refresh_token");
  assert.equal(form.scope, "https://ic3.teams.office.com/.default openid profile offline_access");
  assert.equal(form.refresh_token, "<value>");
  assert.equal(form.redirect_uri, "<value>");
  assert.equal(form["X-AnchorMailbox"], "<value>");
  const resp = S.shape({
    token_type: "Bearer",
    scope: "https://outlook.office.com/Mail.Read https://outlook.office.com/.default Mail.ReadWrite https://x.example/?u=me@x.org",
    expires_in: 4235,
    refresh_token_expires_in: 86399,
    access_token: "eyJ0eXAiOiJKV1QiLCJhbGciOi",
    refresh_token: "0.ARoA",
    AAQkAGI2NGVhZTVlLTI3ZjUtNDQ1Ni1hYjE0: { x: 1 },
  });
  assert.equal(resp.token_type, "Bearer");
  assert.equal(resp.scope, "https://outlook.office.com/Mail.Read https://outlook.office.com/.default Mail.ReadWrite <scope>");
  assert.equal(resp.expires_in, "<int>");
  assert.equal(resp.refresh_token_expires_in, "<int>", "snake_case names are not id-like keys");
  assert.equal(resp.access_token, "<text 26>");
  assert.equal(resp.refresh_token, "<text 6>");
  assert.ok("<key>" in resp, "base64 id keys are still masked");
});

