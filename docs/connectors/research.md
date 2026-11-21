# API-first connector research log

The connector strategy is per service, not “one universal scraper”. Prefer a
maintained official API/library; where student/managed accounts cannot use it,
prefer a free community protocol; use the signed-in browser session only as a
last resort. Never spoof a client id or store bearer/refresh tokens in Git or
plain connector state.

| Service | Candidate route | What we've confirmed | Status / next step |
|---|---|---|---|
| Outlook / Microsoft 365 | Graph, IMAP XOAUTH2 | Graph app consent failed for this student tenant; user reports MailCraft sign-in is blocked pending approval. Outlook Web's own session is used by the v0.16 connector and avoids a separate third-party client registration. IMAP is still policy-controlled and needs an accepted OAuth client. | Current OWA connector is read-only. See [Outlook capability report](outlook-capabilities.md). User confirming whether Outlook Web itself signs in is the key test. |
| Proton Mail | hydroxide local bridge → IMAP/SMTP/CardDAV | hydroxide is a free, unofficial bridge; it presents standard mail protocols to local clients after the user authenticates it. Proton's own free plan does not provide native IMAP. | Planned: add bridge config and a generic IMAP read-only connector. Don't assume bridge credentials are the Proton password; keep them local and out of API responses. |
| Teams | chatsvc + Assignments web API | Capture confirms activity feed and assignment detail/submission calls. Teams list-call capture is missing. | Existing connector; ask for a chat-list capture. These are private, unstable web endpoints. |
| Schoology | schoolopy / REST v1 | Community Python wrapper exists; access requires Schoology API key/secret provisioned by the institution. | Check whether the school exposes credentials before implementation. |
| PowerSchool | PublicPortalServiceJSON; student portal clients | A community client uses the student portal's legacy JSON/SOAP endpoints; district plugin APIs aren't generally available to students. | Capture the portal login and assignment/grade responses; implement read-only if the account can reach them. |
| Canvas | Canvas REST API; `canvasapi` | Official API is well-supported but usually requires a user access token or OAuth client; institution policies can restrict token creation. | Prefer official API if the user's school account can issue a token. Otherwise inspect the already authenticated browser API. |
| Gradescope | gradescopeapi | Community Python wrapper supports account/course/assignment reads; authentication and endpoint stability need verification against the user's account. | Evaluate only after obtaining a sanitized capture or testing account login without storing credentials. |
| Generic mail | IMAP4/IMAP IDLE | A standard protocol, implemented by Python's stdlib. Server support and OAuth/password policy vary; no special API client ID is universal. | Reuse for hydroxide and other configured IMAP servers. Keep per-account credentials in a protected secret store and use async thread offloading. |
| JMAP | RFC 8620/8621 | A modern standard for mail/calendar/contacts, supported by some providers but not a universal Outlook/Proton endpoint. | Add only when the provider explicitly advertises JMAP. |

## Outlook open-source leads reviewed

- [`weirdapps/outlook-access`](https://github.com/weirdapps/outlook-access) (MIT,
  TypeScript) supports Outlook Web session capture and read/write features for
  mail, folders, calendar and attachments. It demonstrates the same basic
  session-based strategy, but its CLI and dependencies aren't imported into
  Node. Its API contract still needs to be checked against the account before
  using any additional route.
- [`ghostwriteai/outlookjs`](https://github.com/ghostwriteai/outlookjs) is a
  page-hook style library; useful when a tab is open, not a background protocol.
- IMAP clients such as Thunderbird or MailCraft do not make a blocked OAuth
  app acceptable by changing wire protocol. Client registration and server
  IMAP enablement are separate tenant controls. No impersonation of Thunderbird
  or another app is planned.

## Data scope / safety

Start read-only. Pull only the data Node needs: mail body/metadata, assignment
and calendar facts. Avoid mirroring an entire mailbox, downloading attachments
by default, or importing Sent/Junk/Archive without an opt-in and dedupe plan.
Private/undocumented APIs may change; keep them behind connector adapters and
report shape errors clearly instead of silently creating incomplete items.
