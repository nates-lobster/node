# API connectors

Connectors fetch a source over plain HTTP and write Inbox rows (and Tasks)
without keeping a browser tab open. They live in `backend/app/connectors/`.

| Connector | App URL | What it reads | Token target |
|---|---|---|---|
| `outlook-mail` | Outlook on the web (`outlook.cloud.microsoft`, `outlook.office.com`, …) | Inbox conversations (`FindConversation`), plus bodies of new unread mail (`GetConversationItems`) | `outlook` |
| `teams-activity` | Teams (`teams.cloud.microsoft`, `teams.microsoft.com`, `teams.live.com`) | The activity feed: mentions, replies, assignment posts … (`48:notifications` on the chat service). Reactions are skipped | `teams-chatsvc` |
| `teams-chats` *(experimental)* | Teams | Recent chats; an unread last message becomes a row. Channels are left to the activity feed | `teams-chatsvc` |
| `teams-assignments` | Teams (school/work) | Teams Assignments: unsubmitted work goes to **Tasks**, new assignments go to the Inbox | `teams-assignments` |

They appear on their own when the matching app is installed and enabled
(Settings → System → **Connectors** shows them). Zero-knowledge apps get no
connector.

## How a run works

```
runner.run_one(key)
  token  = token_mint.get(target)          # cached in memory, or minted (below)
  result = connector.fetch_since(cursor, token)
  create, known = plan_items(result.items, seen_ids, first_run)
  create = connector.enrich(create, token) # e.g. fetch bodies for new mail
  write_items(...)                         # Inbox rows, read-sync, heartbeat, AI pass
  sync_lms_assignments_to_tasks(result.tasks)
  save cursor + seen ids (data/connectors.json; no secrets)
```

Ingest rules (`connectors/ingest.py`):
- **Already seen:** only "read" is synced, source → Node. Reading in Node never
  un-reads anything.
- **New reply in a known conversation:** the existing row is updated in place
  (one row per conversation) and the AI pass re-runs. User-corrected tags are
  untouched.
- **First sync:** read items older than `CONNECTOR_BACKFILL_DAYS` (3) are
  skipped. Unread ones always come in.
- **The same item already came in another way** (live tab, extension, pop-up):
  it is merged into that row with `inbox_ingest.absorb`.

Schedule and health (`connectors/runner.py`): every `CONNECTOR_POLL_SECONDS`
(300), and every `CONNECTOR_ASSIGNMENTS_POLL_SECONDS` (1800) for Assignments.
On a 401 the token is re-minted once. Failures back off:
- network: 5 min, doubling, up to 6 h;
- auth: 1 h, and the app shows *Sign-In Needed*;
- endpoint missing for this account: 24 h.

**Sync now** in the UI ignores the backoff.

## Tokens: the mint

Microsoft's own APIs authenticate with short-lived MSAL bearer tokens, and
student accounts can't register an app. So `connectors/token_mint.py` borrows
one from the server's own browser profile (the profile you sign into through
Node's browser viewer):

1. Open the app headless (`/mail/`, `/`, or the Teams Assignments deep link
   `/l/app/<TEAMS_ASSIGNMENTS_APP_ID>`).
2. Wait for the first request matching the target that carries
   `authorization`. Page, frame, dedicated-worker and SharedWorker requests all
   count; SharedWorkers are watched over CDP because Playwright doesn't report
   them.
3. Keep its replayable headers (`authorization`, `x-anchormailbox`,
   `x-owa-sessionid`, `clientinfo` …; per-request ids are dropped) plus the
   cookies for that URL. Close the page.

Tokens are held **in memory only**, until 3 minutes before the JWT `exp`
(45 min if the token isn't a JWT). A mint takes a few seconds, and at most
`CONNECTOR_MINT_TIMEOUT_SECONDS` (45). It only works while the profile's
Microsoft sign-in is alive, which is weeks with "stay signed in". If the page
lands on `login.microsoftonline.com`, the error says sign-in is needed.

`token_mint.put_external()` accepts headers from another source. It's for the
planned extension tap ("option B" in `microsoft-capture-notes.md`), and the
tests use it.

## Settings

| Env | Default | |
|---|---|---|
| `CONNECTORS_ENABLED` | `true` | Background loop on/off (manual **Sync now** still works) |
| `CONNECTOR_POLL_SECONDS` | `300` | Mail / feed / chats |
| `CONNECTOR_ASSIGNMENTS_POLL_SECONDS` | `1800` | Assignments |
| `CONNECTOR_BACKFILL_DAYS` | `3` | First-sync window for already-read items |
| `CONNECTOR_MINT_TIMEOUT_SECONDS` | `45` | How long a mint waits for a signed request |
| `TEAMS_ASSIGNMENTS_APP_ID` | `66aeee93-507d-479a-a3ef-8f494af43945` | Teams app id used to open Assignments |

## API

| Route | |
|---|---|
| `GET /api/v1/connectors` | Connectors, health, last error, token summary (never header values) |
| `POST /api/v1/connectors/{key}/run` | Run now, ignoring backoff |
| `POST /api/v1/connectors/{key}/enabled` | `{"enabled": bool}` |
| `POST /api/v1/connectors/{key}/mint` | Force a fresh token |

All routes are session-only. No guard token opens them.

## Adding a connector

1. Capture the site with `tools/api-recorder/record.py`. Mark the list call,
   the open-item call and the token refresh.
2. Subclass `connectors.base.Connector`:
   - set `key`, `label` and `auth_target`;
   - implement `fetch_since(cursor, auth)` to return `FetchResult(items, cursor, tasks)`;
   - raise `ConnectorError(kind, …)`. `raise_for_status()` maps HTTP codes to kinds.
3. If it needs a minted token, add a `MintTarget` (page to open, plus a URL
   regex of a signed request).
4. Map the app URL to it in `runner.connector_classes_for_url`.
5. Test it with `httpx.MockTransport` (`connector.transport = …`). See the
   `test_v016_*` tests.

## Unverified so far (needs a real run on the user's account)

These parts are built from the capture's structure, not from replies we've
actually seen:
- **Outlook:** the request uses `DistinguishedFolderId: inbox` and sorts by
  `ConversationLastDeliveryTime`. It sends `ShapeName:
  ReactConversationListView` and retries without it if that's rejected.
- **Teams chats:** the conversation-list endpoint wasn't in the capture.
- **Assignments:** the `me/work` filter was redacted, so it's omitted, and
  `me/classes` is used for class names. The deep link is the documented
  `/l/app/<id>` form.
