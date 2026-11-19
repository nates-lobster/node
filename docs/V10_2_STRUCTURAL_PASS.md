# Node WebOS v10.2 — Structural Pass

This release replaces the placeholder decision layer with a real one, rebuilds the
Inbox around tags, makes Grades fully user-controlled, and replaces unreliable
browser-window scraping with learned HTTP APIs.

---

## 1. The decision gate (LFM3 / d1-2B)

Every notification and every chat turn passes through a **typed decision model**
before anything else happens. It replaces the `Von`-like placeholder as the
primary classifier and router.

**Why a decision model and not a chat model.** d1-2B answers a set of typed
questions — choice, multi-choice, score, text, date, number — in a **single
forward pass**, and returns a **calibrated probability** with each answer. That
means Node can decide "is this important?" and "which tags apply?" without
generating a single token, which is what makes per-notification classification
affordable.

| | |
|---|---|
| Default endpoint | `POST {DECISION_BASE_URL}/v1/systemone` |
| Hosted model alias | `d1.2` (Liquid API) |
| Local model | `LiquidAI/d1-2B` (~1.92B, 128K ctx) via llama.cpp / vLLM / MLX |
| Protocol | TypeSafe System One (`state` + `questions`) |
| Falls back to | smallest/fastest discovered Ollama model, then heuristics |

Set it up with:

```bash
# local
DECISION_BASE_URL=http://localhost:8010

# hosted
DECISION_BASE_URL=https://api.liquid.ai/decisions
DECISION_API_KEY=...
```

`GET /api/v1/decision-gate/status` returns `{engine, online, configured, enabled,
model, protocol, confidence_threshold}`. The Inbox shows the result as a **Gate**
pill — click it to run a self-test. Everything still works when the gate is
offline; only the quality of the classification changes.

### Two jobs

**Tagging (primary).** The gate scores a notification against every tag's
`description`. Those descriptions are editable in the Inbox → `+ Tag`, so
**writing a clearer description literally makes the classifier better** — no
retraining, no prompt files.

**Routing tools (secondary).** Before a chat turn, the gate reads the user's
message and picks only the tool families that turn needs (`calendar`, `tasks`,
`inbox`, `grades`, `files`, `bridges`, …). The chat model then sees ~6 tools
instead of 36, which is what stops it from forgetting tools or mis-filling
arguments.

```
"I have band practice on friday"
  → groups: ["calendar"]      tools: [create_calendar_event]
  → slots:  title="Band Practice", date=<friday>, minutes=60
```

Title/date/slot extraction is its own focused question set, which is why the
event is `Band Practice` and not the whole sentence. `POST /api/v1/ai/tool-plan`
exposes this directly for debugging.

---

## 2. Inbox — a real mail client

The Inbox is no longer a notification list. It is a tag-first mail client.

### Tags, not categories
Tags are the primary classification; filters operate on tags. `category` is kept
as a compatibility field but is no longer what the UI or the query language use.

- **Create** — `+ Tag` opens the editor. Name + description (the classifier
  criteria) + color + important flag.
- **Rename** — cascades onto every message carrying it. Renaming can never leave
  two tags behind (the registry rejects a collision before writing).
- **Delete** — built-in tags are protected; custom tags remove cleanly.
- **Corrections train the gate** — moving a message from tag A to tag B records
  a `sender_key` + `subject_tokens` → `tag` hint. Later messages from that sender
  get B automatically, at higher priority than the model.

### Gmail-style query language
The same syntax works for the user (search bar) and for the AI (`inbox_search`
tool), evaluated client-side for instant filtering and server-side as the source
of truth:

```
tag:school  tag:school,work     is:unread  is:read  is:important  is:starred
is:archived  is:unimportant     from:prof@school.edu   subject:"band practice"
source:canvas                   body:rubric            priority:high
newer_than:7d  older_than:30d   has:attachment         -tag:promo
tag:school OR tag:work
```

`GET /api/v1/inbox/query?q=...` returns `{query, explained, count, notifications}`
— `explained` is a plain-English rendering, which the AI uses to confirm it
understood before answering.

### Reading pane
Click any row to open it. The pane shows sender, timestamp, tags, and the **full
body**. If the body hasn't been captured yet, **Refetch body** pulls it from the
live app tab (`POST /api/v1/inbox/harvest`) so you can read mail without opening
Gmail at all.

Opening a message marks it read after a short grace period, with an **Undo**
in the toast — the same contract as Gmail.

### Duplicate merging
The same real-world event arrives from several feeds with different sender
strings ("PowerSchool", "powerschool-noreply@…", "Schooldriver"). The merge pass
computes a `dedupe_key` from source + normalized sender + subject tokens + body
fingerprint, then keeps the oldest row, **unions the tags**, and records
`merged_count` / `dedupe_sources`. Run it on demand with **Merge duplicates** or
`POST /api/v1/inbox/dedupe?dry_run=true`.

### Everything is archived for fine-tuning
`GET /api/v1/inbox/learning/export` streams NDJSON: one record per correction,
with the sender, subject, body excerpt, and the from→to tag transition. That is
directly consumable as a local fine-tuning set.

---

## 3. Grades — full user control

### Hidden courses stay hidden
`POST /api/v1/grades/hide` writes a `CourseVisibility` row. It is written **even
when the course isn't in the database yet**, so a future scan cannot resurrect a
course you removed. `GET /api/v1/grades` filters through that table and
recomputes the summary over the *visible* set — averages, GPA, and the briefing
all agree with what you see.

Toggle everything from the eye icon on a course card; the **Hidden courses (n)**
panel lists them with one-click **Restore**, plus **Restore all**.

### Course groups as tabs
Create a group (`+ Group`), then tick the courses that belong to it. Each tab
shows its own average. This is the multi-monitor story: "Monitor 2 — core only"
is a group whose average excludes electives. Groups persist in `grade_tabs` and
mirror onto `CourseVisibility.tab_id` so they survive re-scans.

---

## 4. api-anything — learned APIs instead of browser windows

Browser-window streaming is roughly 70% reliable and dies whenever a session
expires. api-anything replaces it for services that have no public API: an agent
learns the site's **own frontend requests** once, and they become replayable
operations over plain HTTP with JSON results.

**Measured in this build:** `hacker-news/search` → `tier: 1`, **149 ms**, 30
structured records, no browser involved.

### Setup

```bash
# needs Node >= 22.13
npx -y api-anything serve --host 127.0.0.1 --port 8787
export API_ANYTHING_TOKEN=<token>      # bearer auth for every route but /v1/health
```

Then in Node: **Settings → System → Bridges**, or

```bash
curl -X POST /api/v1/bridges/config \
  -d '{"base_url":"http://127.0.0.1:8787","token":"<token>"}'
```

`GET /api/v1/bridges/status` reports `reachable`, `detail`, and the installed
operations, and distinguishes **"not running"** from **"listening but rejected
the probe — set the token"** using a TCP probe.

### Calling
`POST /api/v1/bridges/call  {"operation": "site/op", "args": {...}}`
(the server nests inputs under `args`; Node handles that for you and hoists
`data` / `tier` / `ms` into the response). The AI reaches it through the
`manage_api_bridge` tool.

### Cookies from your local session
The extension (below) pushes cookies for your enabled domains to
`POST /api/v1/ext/cookies`. Node groups them per host
(`canvas.instructure.com → canvas`) and injects them into the bridge's session
store, so server-side operations reuse the login your browser already has. That
is the fix for "server sessions keep expiring": the browser is the source of
truth for the session, and the server mirrors it.

---

## 5. Browser extension (MV3)

`extension/` — a real Manifest V3 extension, downloadable from
**Settings → Browser Extension** as a zip (built from the live folder, so it
always matches the running server).

- **New-tab override** (`chrome_url_overrides.newtab`) with three modes:
  *full dashboard* (iframe of Node), *custom minimal* (clock, quick add, tasks,
  urgent mail), *wallpaper + clock*. The mode lives in Node's settings and is
  mirrored to `chrome.storage`, so the in-app toggle and the browser agree.
- **Session capture** — copies cookies for user-listed domains only
  (PowerSchool, Schoology, Instructure, Classroom, Gmail, Outlook by default) and
  POSTs them to your server. Debounced to one sweep per 60 s, plus a periodic
  alarm.
- **Guard token** *(0.12 note: tokens are now scoped per client — see
  `docs/ARCHITECTURE.md` §2)* — the extension talks to exactly three routes
  (`/api/v1/ext/bootstrap`, `/api/v1/ext/quick-capture`, `/api/v1/ext/cookies`)
  authenticated with `X-Node-Guard-Token`. Nothing else is exposed, and a
  cookieless caller is still rejected.

Install: unzip → `brave://extensions` → Developer mode → Load unpacked → paste
the token from Settings.

---

## 6. Reliability fixes in this pass

- A **user tag correction is authoritative.** A later AI pass or re-scan no
  longer overwrites `tags_csv` / `category` once `tag_corrected_by_user` is set.
  (This was silently undoing corrections.)
- **Offline event classification.** With no decision model reachable, quick
  capture still distinguishes "band practice friday 4pm" (event) from "buy rosin"
  (task) using cue + time + day heuristics, task-verb vetoes included.
- **Titles are cleaned deterministically**, trailing dates and times included:
  `band practice friday 4pm` → `band practice`, `standup 9:30` → `standup`.
- **Month names parse** (`oct 3`, `3 Nov`) and roll to next year once the date has
  passed, instead of silently becoming today.
- **Grades defaults** no longer seed empty "Core"/"Electives" groups that render
  as dead chrome.
- The **new-tab scrim** was blacking out the page behind the login card; it is now
  a tunable legibility veil, and the clock stays readable while locked.

---

## Route reference

**Decision gate** `GET /api/v1/decision-gate/status` · `POST …/selftest` ·
`POST /api/v1/ai/tool-plan` · `POST /api/v1/ai/quick-capture`

**Inbox** `GET|POST|PATCH /api/v1/inbox/tags` · `DELETE /api/v1/inbox/tags/{name}` ·
`GET|POST /api/v1/inbox/filters` · `DELETE /api/v1/inbox/filters/{id}` ·
`GET /api/v1/inbox/query` · `POST /api/v1/inbox/harvest` ·
`POST /api/v1/inbox/dedupe` · `POST /api/v1/inbox/{id}/tags` ·
`POST /api/v1/inbox/bulk-tags` · `GET /api/v1/inbox/learning` ·
`GET /api/v1/inbox/learning/export`

**Grades** `GET /api/v1/grades/control` · `POST …/visibility` · `POST …/hide` ·
`GET|POST /api/v1/grades/tabs` · `DELETE /api/v1/grades/tabs/{id}`

**Bridges** `GET /api/v1/bridges/status` · `POST …/config|learn|serve|call|session`
· `GET /api/v1/bridges/session/{app_slug}`

**Extension** `GET /api/v1/ext/info` · `GET /api/v1/ext/package.zip` ·
`GET /api/v1/ext/bootstrap` · `POST /api/v1/ext/quick-capture` ·
`POST /api/v1/ext/cookies`

**New tab** `GET|POST /api/v1/newtab/config` · `GET /newtab`
