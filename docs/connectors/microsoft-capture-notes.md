# Outlook & Teams: what the captures show (2026-11-20)

These notes come from console-recorder captures of the user's school account on
`outlook.cloud.microsoft` and `teams.cloud.microsoft`. They record structure
only. The raw files are kept out of git.

## Findings

**1. The data calls run in background workers.** The console hook saw calls
made from the main page:
- Outlook: `GetAttachmentPreviews`, `GetFolderChangeDigest`,
  `SubscribeToNotification`, `translateExchangeIds`, `signals`, search
  suggestions;
- Teams: presence and the MSAL token refresh.

It never saw the mail-list or chat calls (`FindConversation`/`FindItem`,
`GetConversationItems`, Teams `chatsvc`), even though the user switched
folders, opened emails and changed chats. They load from workers that a
pasted hook can't reach. `record.py` taps the network and covers page,
dedicated-worker, shared-worker (CDP tap) and service-worker requests. Use it
for the next capture.

**2. Outlook authenticates with a bearer token, not cookies.** Every
`/owa/service.svc` call sends:

```
authorization            (Bearer, MSAL access token)
action                   e.g. GetFolderChangeDigest
x-owa-urlpostdata        URL-encoded JSON request (instead of a body)
x-anchormailbox, x-tenantid, x-owa-sessionid, x-owa-hosted-ux,
x-owa-correlationid, x-owa-actionsource, x-req-source: Mail, ms-cv,
prefer: IdType="ImmutableId", ...
```

The REST endpoints (`/api/v2.0/me/...`, `/api/beta/me/translateExchangeIds`,
`/search/api/v1/...`) use the same token plus `x-anchormailbox`,
`x-routingparameter-sessionkey`, `owaappid`, `x-ms-appname: owa-reactmail`.

**So syncing cookies alone won't let the server call these.** The old plan
("OWA `service.svc` via synced cookies") needs a token source.

**3. Teams uses the MSAL SPA refresh flow.** The browser POSTs to
`login.microsoftonline.com/{tenant}/oauth2/v2.0/token` with
`grant_type=refresh_token` and gets back an `access_token` and a rotated
`refresh_token`. SPA refresh tokens are normally capped at 24 h, and the
`refresh_token_expires_in` key confirms it. Captures from 0.15.2 onward also
show `client_id`, `scope` (the audience) and `grant_type`. Presence
(`/ups/global/v1/presence/getpresence`) uses `authorization` +
`x-ms-client-*` headers.

## Token options for a server-side connector

| Option | How | Lasts | Notes |
|---|---|---|---|
| **A. On-demand mint** | The server's existing Playwright profile opens Outlook/Teams headless for ~10 s, captures one `authorization` header, then closes | as long as the Microsoft sign-in cookie (weeks, "stay signed in") | No tab left open. The rest is plain HTTP. Re-mint every ~50 min or on a 401 |
| B. Extension tap | The Node extension reads the `authorization` header of the user's own Outlook/Teams requests and forwards it (guard-token scope) | ~60–90 min per token | Only while the user has Outlook/Teams open. Good as a bonus source |
| C. Replay the SPA refresh grant | The server stores the SPA refresh token and refreshes like MSAL (needs an `Origin` header) | 24 h hard cap, rotating | Fragile. Daily re-login |

Leaning towards **A, with B as an opportunistic top-up**. This keeps the
"no always-on tabs" goal: the browser is only a token printer. The decision
waits for the `record.py` capture, which should show the list/chat calls and
each token's audience.

## `record.py` capture (second round)

The second round used `record.py`, and it recorded the data calls. Every call
below sends `authorization` (Bearer) and cookies. Shapes only; values were
never recorded.

### Outlook (`/owa/service.svc?action=X&app=Mail&n=N`, POST)

| Action | Count | Notes |
|---|---|---|
| `FindConversation` | 11× | The inbox list. The request is JSON in the `x-owa-urlpostdata` header: `ParentFolderId`, `ConversationShape` (IdOnly + extras), `ShapeName`, `Paging` (`SeekToConditionPageView` on `ConversationInstanceKey`, ~50 rows), `ViewFilter`, `SortOrder`, `FocusedViewFilter`. The response `Body.Conversations[]` has `ConversationId`, `ConversationTopic`, `UniqueSenders`, `LastSender.Mailbox`, `LastDeliveryTime`, `MessageCount`, `UnreadCount`, `HasAttachments`, `ItemIds`, `Preview`, `Importance` |
| `GetConversationItems` | 21× | Opening an email. The JSON is in the body. `ItemShape.BodyShape` = `UniqueFragment`. Response: `ConversationNodes[].Items[]` with `From`, `ToRecipients`, `Subject`, `DateTimeReceived`, `UniqueBody{BodyType,Value}`, `IsRead`, `HasAttachments` |
| `FindItem` | 2× | Drafts: `IndexedPageView`, `RootFolder.Items[]` |
| `GetFolderChangeDigest` | 4× | Cheap "anything new?" check: `Watermark`, `UnseenCount` |
| also | | `GetItem`, `ApplyConversationAction` (mark read), `GetFolder`, `SubscribeToNotification`, `GetCalendarView`, `GetOwaUserConfiguration` |

Headers: `action`, `x-anchormailbox`, `x-owa-sessionid`, `x-owa-hosted-ux`,
`x-owa-correlationid`, `x-req-source: Mail`, `prefer: IdType="ImmutableId", …`,
`ms-cv`. The only token refresh captured was for
`titles.prod.mos.microsoft.com` (client `9199bf20-…`). OWA's own mail token
came from cache, so its audience wasn't seen. That doesn't matter for the mint
(below).

### Teams

- **Chat service** (`teams.cloud.microsoft/api/chatsvc/amer/v1/…`):
  - `users/ME/conversations/{id}/messages?view=msnp24Equivalent|supportsMessageProperties&pageSize=200&startTime=1`.
    The sampled conversation id has 16 characters: it's **`48:notifications`,
    the activity feed**.
  - Each feed message has `properties.activity` with `activityType`,
    `sourceUserImDisplayName`, `messagePreview` and `sourceThreadTopic`, plus
    `properties.isread`.
  - Also seen: `users/ME/conversations/{id}` and `threads/{id}/consumptionhorizons`.
  - **No chat-list call was captured.**
- **Token:** `login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, client
  `5e3ce6c0-…`, scope `https://api.spaces.skype.com/.default`. It refreshed
  24× in a few minutes.
- **Assignments** (`assignments.edu.cloud.microsoft/api/v1.0/edu/…`, from the
  app's frame):
  - `me/work?$filter=…&$orderby=dueDateTime desc&$expand=submissions(...)`;
  - `classes/{id}/assignments/{id}` (`displayName`, `dueDateTime`, `status`,
    `instructions`, `grading.maxPoints`, `webUrl`);
  - `…/submissions` (`status`, `submittedDateTime`, `outcomes`);
  - `classes/{id}/members`.
  - Headers: `prefer: AssignmentStatusV2`, `x-aui-*`, `ms-int-appid`.

## Decision: option A (on-demand mint)

Built in 0.16.0 (`backend/app/connectors/`, design in [README.md](README.md)).
The mint copies the headers of the first matching request instead of asking
MSAL for a token. Unknown audiences don't matter: whatever the app itself sent
is what we replay. SharedWorker requests are watched over CDP, like
`record.py` does.

## Still open

- A Teams chat-list call. `teams-chats` uses the chat service's standard list
  endpoint and is marked experimental.
- Outlook write-back (mark read in Outlook when read in Node), using
  `ApplyConversationAction`.
- Option B (the extension forwards headers) as a top-up source:
  `token_mint.put_external` is ready for it.
