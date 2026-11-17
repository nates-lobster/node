# Architecture

How Node is put together, and where to look when something breaks.

---

## 1. Process model

One Python process, one SQLite file, one web page.

```
uvicorn app.main:app
   │
   ├── FastAPI app (backend/app/main.py)
   │     ├── middleware: auth gate → CORS → no-store cache headers
   │     ├── ~150 routes under /api/v1/*
   │     ├── WebSocket: /api/v1/stream (per-app browser windows)
   │     └── static: shell HTML, os_app.js, /wallpapers, /newtab
   │
   ├── background
   │     ├── cron_scheduler      (24/7 jobs: briefing, pre-bedtime windows, sync)
   │     ├── browser_stream mgr  (Playwright tabs, ping ingest, AI pass)
   │     └── AIJob worker        (detached asyncio tasks, survives disconnects)
   │
   └── data/hub.db (SQLite, WAL)
```

There is no Celery/Redis/broker. If you need background work, use
`asyncio.create_task` persisted to a table (`AIJob` is the model to copy) or the
cron scheduler.

---

## 2. Request lifecycle

1. **Auth middleware** (`main.py`, `auth_gate_and_cache_headers`).
   - `PUBLIC_EXACT_PATHS` (lock screen, login, `/newtab`, `/favicon*`) bypass auth.
   - `/wallpapers/*` and `/assets/*` are public so the lock screen can render
     ambient art — these expose zero personal data.
   - `GUARD_TOKEN_ALLOWED_PATHS` accept `X-Node-Guard-Token` instead of a session.
     This is how the **browser extension** and the **focus-guard extension** talk
     to the server without a cookie (different origin).
   - Everything else requires a valid session token (cookie or `X-Node-Session`).
2. Route handler → service module → SQLModel tables.
3. Response gets `Cache-Control: no-store` (API) or `no-store` (HTML shell) so a
   stale shell is never served after a deploy.

**Adding a route?** Put it in `main.py`, keep the handler thin, and put logic in a
service module so it is testable without HTTP.

---

## 3. The AI stack (read this before touching AI)

Three layers, each with a fallback.

### 3.1 Decision gate (`decision_gate.py`)
A small, fast, **typed** model (Liquid d1-2B). It does not chat. It answers
questions and returns calibrated confidence:

- `tag_notification(...)` → which tags apply (primary classification).
- `plan_tools(...)` → which tool families a chat turn needs + extracted slots
  (title, date, time, duration).

If the gate is offline, callers get `None` and **must** fall back deterministically.
`plan_tools` returning `None` means "no opinion" — the caller attaches a safe
default subset, not all 36 tools.

### 3.2 Router (`model_router.py`)
Chooses a model per task, in priority order: explicit provider → local Ollama →
remote Ollama → cloud. Handles think/tools retries and notification processing
(`process_incoming_notification`), which returns a normalized dict:
`{title, ai_summary, category, tags, is_important, priority, sender, dedupe_key,
confidence, engine}`.

### 3.3 Agent (`secretary.py`)
`SECRETARY_TOOLS` = 36 tool definitions. `stream_chat` gates the tool list through
`plan_tools`, applies gate-extracted slots, and streams deltas
(`thinking_delta | content_delta | turn_complete | unreachable`).

**Two executors:** `execute_secretary_tool` is **sync**; async service calls live
in `execute_secretary_tool_async`. Never `await` in the sync one.

### 3.4 Jobs
`POST /api/v1/ai/jobs/chat` starts a detached task and persists it; the client
reconnects via `GET /api/v1/ai/jobs/{uid}/stream`. This is what makes closing the
tab safe.

---

## 4. Notification pipeline

This is the most intricate path in the codebase. Follow it in order:

```
Playwright tab detects unread
   → POST /api/v1/ping  (or internal ingest)
   → browser_stream: normalize, scrub UI artifacts (scrub_notification_artifacts)
   → Notification row created (category="General", ai_processed=False)
   → _schedule_ai_notification_processing(ids)          [detached]
        → model_router.process_incoming_notification()  [gate → fallback]
        → write tags_csv / summary / importance
            ⚠ guarded by tag_corrected_by_user — user wins
   → merge_duplicate_notifications(since_ids=[...])
        → dedupe_key = hash(source, normalized sender, subject tokens, body fp)
        → keep oldest, UNION tags, bump merged_count, record dedupe_sources
   → Inbox UI reads via GET /api/v1/notifications (and the tag DSL via /inbox/query)
```

Two properties to preserve:
- **Idempotent.** Re-ingesting the same message must not create a second row.
- **Non-destructive.** Nothing automated may overwrite a user correction.

---

## 5. Inbox query language

One grammar, two implementations that must stay in sync:

| Where | Function |
|---|---|
| Python (source of truth, used by AI) | `inbox_tags.parse_filter` → AST, `matches(payload, ast)` |
| JavaScript (instant filtering in UI) | `matchesInboxQuery(notif, query)` in `os_app.js` |

Grammar: `key:value` tokens ANDed; `-token` negates; `OR` (or `|`) joins into an
any-group; quoted values allow spaces; a bare token searches all text fields.

Keys: `tag|label|category`, `is` (unread/read/important/unimportant/starred/
archived), `from|to`, `subject`, `source`, `body`, `priority`, `has:attachment`,
`newer_than`, `older_than`.

If you extend the grammar, extend **both** and add a test asserting they agree.

---

## 6. Grades

```
lms_connector  → scrapes portal (Playwright) or REST (Canvas/Schoology)
   → CourseGrade rows
   → grades_control.apply_to_courses()   ← filters by CourseVisibility
   → grades_service.get_dashboard()      ← summary recomputed over VISIBLE set
   → GET /api/v1/grades returns {courses, summary, hidden_courses, tabs, scoped}
```

`CourseVisibility` is the user's intent and is written even when the course does
not exist yet, which is what makes "removed stays removed" true. `GradeTab` groups
courses; membership is mirrored onto `CourseVisibility.tab_id` so it survives a
re-scan.

**Never compute averages from all `CourseGrade` rows.** Always go through
`apply_to_courses`.

---

## 7. Learned APIs (api-anything bridge)

```
extension (background.js) ──cookies──► POST /api/v1/ext/cookies
                                          → api_bridge.import_session_cookies()
                                          → per-host bridge stores

api-anything serve (127.0.0.1:8787)
   ▲ Authorization: Bearer $API_ANYTHING_TOKEN
   │
api_bridge.status()  → GET /openapi.json → operation list
api_bridge.call()    → POST /v1/call/{site}/{op}  body {"args": {...}}
```

Notes that cost time to learn:
- Arguments **nest under `args`**. A flat body returns
  `400 Unrecognized key: "query"`.
- There is no `--token` flag; the token is `API_ANYTHING_TOKEN` (or
  `~/.api-anything/serve-token`).
- `status()` distinguishes "not running" from "listening but rejected the probe"
  with a raw TCP connect, so the user gets an actionable message.

---

## 8. Settings

`os_settings.py` holds a declarative list of keys with type, default, category and
description. `sanitize_patch` **drops unknown keys silently** — if a setting
doesn't stick, it is almost always because the type wasn't understood
(`enum` needs `options`).

Keys worth knowing: `newtab_mode`, `inbox_mark_read_on_open`,
`inbox_auto_fetch_body`, `inbox_dedupe_window_hours`, `decision_gate_enabled`,
`decision_gate_prefer_for_tools`, `grades_show_scoped_tabs`,
`grades_counts_hidden_in_briefing`, plus theme/WM keys.

---

## 9. Database & migrations

`database.init_db()` runs on boot and is idempotent:
1. `SQLModel.metadata.create_all()`
2. a block of `ALTER TABLE ... ADD COLUMN` guarded by a column-existence check
3. index creation
4. seeding (settings defaults, tag registry) — only when empty

To add a column: add it to the model, then add a guarded `ALTER` in that block.
**Never** drop or rewrite a user's column.

---

## 10. Extension

`extension/` is a Manifest V3 bundle served as a zip by
`GET /api/v1/ext/package.zip` (built from the folder on demand, so it always
matches the running server).

- `chrome_url_overrides.newtab` → `newtab.html`
- Three surfaces: `newtab.html` (modes), `popup.html` (status), `options.html`
  (server URL, guard token, cookie domains)
- Talks to exactly three server routes via `X-Node-Guard-Token`:
  `/api/v1/ext/bootstrap`, `/api/v1/ext/quick-capture`, `/api/v1/ext/cookies`

Anything you add to the extension must not widen that token's blast radius.

---

## 11. Common failure → cause

| Symptom | Likely cause |
|---|---|
| `401` on an API call that works elsewhere | path not in `PUBLIC_EXACT_PATHS` / guard allowlist, or expired session |
| Setting doesn't persist | wrong `type` in `os_settings` (silently dropped) |
| Tag rename 400 "not found" | stale `data/inbox_*.json` from an older run, or import-time path binding |
| Event gets today's date | month/day not parsed, or gate offline and no deterministic fallback |
| User's tag reverts | a new write path ignores `tag_corrected_by_user` |
| Hidden course reappears | code path bypassed `grades_control.apply_to_courses` |
| Bridge "reachable" but calls fail | token mismatch (server and Node must share `API_ANYTHING_TOKEN`) |
| New tab shows a black page | scrim/overlay opacity, or mode is `dashboard` while the server is unreachable |
| Shell shows old code after edit | browser cache — responses are `no-store`, so hard-reload |
