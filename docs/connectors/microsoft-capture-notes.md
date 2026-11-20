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

## Still needed (next capture, with `record.py`)

- Outlook: the inbox list (switch folder and back), open an email, scroll the
  list.
- Teams: open a chat, scroll up in it, switch chats, open Assignments.
