# AGENT.md — Start Here

You are picking up **Node**, a self-hosted personal WebOS. This file is written
for an AI agent with **no prior context**. Read it fully before touching code.
It tells you what the project is, what you may and may not touch, how to verify
your work, and where the deeper docs live.

- **Version:** 0.12.0
- **Human:** one person (their host is `max@athlon`). You are their maintainer.
- **Language of the product:** English. Code, comments and commits: English.
- **Read next:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (map + data model),
  [`docs/V10_2_STRUCTURAL_PASS.md`](docs/V10_2_STRUCTURAL_PASS.md) (why the recent
  release is shaped the way it is).

---

## 1. What this thing is

A single-user personal operating system that runs as a web app on a home server.
It does five jobs:

1. **Desktop shell** — tiling/floating windows, resizable widgets, themes, a Start
   menu, a lock screen. Alpine.js + Tailwind in one HTML shell, no build step.
2. **Inbox** — a real mail client over notifications scraped from web apps
   (Gmail/Proton, LMS portals, messaging). Tag-first, Gmail query syntax.
3. **Grades** — multi-LMS aggregation (PowerSchool, Schoology, Canvas, Classroom),
   with user-defined course groups.
4. **Calendar + Tasks** — a local calendar, plus task sync from calendar and inbox.
5. **AI** — a 24/7 secretary that can perform *any* OS action through ~36 tools,
   backed by local/remote Ollama, cloud providers, and a small decision model.

Everything is **server-side**. Closing the browser never cancels an AI job.

---

## 2. Architecture in one picture

```
                     ┌───────────────────────────────────────────────┐
  Brave/Chromium ───►│  FastAPI  (backend/app/main.py)               │
  (WebOS shell,      │   • auth gate (lock screen, zero-leak)        │
   Alpine.js)        │   • ~150 REST routes  /api/v1/*               │
                     │   • WebSocket stream for app windows          │
                     └───────────────┬───────────────────────────────┘
                                     │
        ┌────────────────┬───────────┼────────────┬─────────────────┐
        ▼                ▼           ▼            ▼                 ▼
   SQLite hub.db    secretary.py  model_router  browser_stream   api_bridge
   (single file)    36 AI tools   decision gate  Playwright tabs  api-anything
                                   (d1-2B)                        (learned APIs)
```

- **One process.** `uvicorn app.main:app`. No Celery, no Redis, no message bus.
- **One database.** SQLite at `data/hub.db`. Schema is created/migrated in
  `database.py` on boot (idempotent `ALTER TABLE` + index + seed block).
- **No frontend build step.** `ui/os_app.js` and `ui/os_shell.html` are served
  directly. Editing them and reloading is the whole loop.

---

## 3. The five rules that matter most

These were chosen deliberately by the human. Breaking them is a regression even
if the tests pass.

### 3.1 Code is tracked, data is not — never blur the line
`data/` holds the SQLite DB, credentials, uploaded files, and browser cookie
profiles. It is gitignored **and** mounted outside the container's code path.
**Never** commit anything from `data/`, never hardcode a path into it, and never
write runtime state to a tracked file. Every runtime path is derived from
`settings.*_path` (see `backend/app/config.py`) — use those, don't invent paths.

### 3.2 Zero preinstalled apps
The installed-apps registry is runtime data (`data/apps_config.yaml`, via
`settings.apps_config_path`) and starts empty. Nothing in the repo ships an app
list. Users install from the App Library or add a custom URL. Never seed demo apps, demo grades, demo notifications, or
fake data. `SEED_DEMO_DATA` must stay `false` in production.

### 3.3 All AI runs server-side
Prompts execute inside a detached `asyncio` task persisted as an `AIJob`. A
client disconnect must never abort generation. If you add an AI feature, route it
through the job queue, not a request-scoped coroutine.

### 3.4 One instructions surface
There is exactly one `AI Instructions` section (composable markdown files). It is
never called "Notebook". No time estimates in that window.

### 3.5 The human's corrections win
If the user tags something by hand (`tag_corrected_by_user`), no AI pass, re-scan
or model fallback may overwrite it. This rule was learned the hard way — see
§7.3.

---

## 4. How to run and verify

### 4.1 Local (this sandbox or any Linux box)

```bash
pip install -r backend/requirements.txt
PYTHONPATH=backend python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. Default login comes from `NODE_AUTH_USERNAME` /
`NODE_AUTH_PASSWORD` (defaults `admin` / `changeme` — the human must change this
on a real deployment). In this sandbox it is `admin` / `changeme`.

The convenience launcher `start_preview.sh` installs deps, installs Brave if
missing, and execs uvicorn. It is sandbox tooling, not the production entrypoint.

### 4.2 Production (Docker Compose / Dockge)

```bash
cp .env.example .env      # then edit: credentials, TZ, ports
docker compose up -d --build
```

`docker-compose.yml` mounts `./data/*` from the host. That is the whole
persistence story: **redeploy the code, keep `data/`.**

### 4.3 Tests — run this before you claim anything works

```bash
# from the repo root (a venv is easiest: python3 -m venv .venv && . .venv/bin/activate)
pip install -r backend/requirements.txt pytest pytest-timeout
PYTHONPATH=backend python3 -m pytest backend/tests/test_api.py -q --timeout=120
```

**Expected: 49 passed** with a launchable browser, or **44 passed, 5 skipped**
without one (~20 s). The 5 real-browser tests skip with the actual launch error
when Playwright Chromium (or, for one test, Brave) isn't available. Wherever a
browser *should* exist, run with `NODE_TESTS_REQUIRE_BROWSER=1` so a missing
browser fails loudly instead of skipping. If you add a feature, add a test. The suite is a
single file by design; it uses a temp dir per run and env-var overrides, so it
never touches the real DB. Do not "fix" a failing test by weakening it — three
of the failures found this way were real product bugs (see §7).

Some tests need Playwright + a browser:

```bash
python3 -m playwright install chromium
sudo python3 -m playwright install-deps chromium
```

The Arena sandbox can only reach GitHub, npm and PyPI, so this download fails
there. Browser tests skip in that environment; verify them on a real host.

---

## 5. Codebase map (where to look)

All paths relative to repo root.

| Area | File | Notes |
|---|---|---|
| HTTP routes | `backend/app/main.py` | ~150 endpoints. Huge but flat; search by `@app.` |
| AI agent (tools) | `backend/app/secretary.py` | `SECRETARY_TOOLS` = 36 tools; `stream_chat`, tool executors |
| Decision model | `backend/app/decision_gate.py` | d1-2B client, `tag_notification`, `plan_tools` |
| Model routing | `backend/app/model_router.py` | pick model, notification pipeline, fallbacks |
| Browser tabs | `backend/app/browser_stream.py` | Playwright managers, ping ingest, AI pass, dedupe |
| Inbox tags/DSL | `backend/app/inbox_tags.py` | registry, Gmail parser, learning, filters |
| Inbox harvest/dedupe | `backend/app/inbox_harvest.py` | body extraction, `merge_duplicate_notifications` |
| Inbox ingest dedupe | `backend/app/inbox_ingest.py` | `absorb_or_none` (every row-creating path must call it first), decision-model check for borderline pairs, cleanup |
| Grades | `backend/app/grades_service.py`, `grades_control.py` | dashboards, visibility, groups |
| Learned APIs | `backend/app/api_bridge.py` | api-anything client, cookie import |
| Tasks sync | `backend/app/task_intelligence.py` | calendar+inbox → tasks |
| Settings | `backend/app/os_settings.py` | 100+ keys, `sanitize_patch` whitelist |
| Schema | `backend/app/models.py`, `database.py` | SQLModel tables + boot migrations |
| UI | `backend/app/ui/os_shell.html`, `os_app.js` | shell markup (Alpine `x-show` views) + methods/state |
| Extension | `extension/` | MV3, served as a zip by the server (compose mounts it at `/extension`) |
| API Recorder (standalone) | `tools/api-recorder/` | `console-recorder.js` (GENERATED by `build.mjs` from `shape.js` + `src/console-main.js`, paste into DevTools; no `innerHTML`, so it works under Trusted Types), `record.py` (Playwright + stdlib marking page on 127.0.0.1; redaction runs `shape.js` in its own tab; `SharedWorkerTap` adds SharedWorker traffic over CDP, which Playwright doesn't report). Tests: `python -m pytest tools/api-recorder` |
| API Recorder | `extension/recorder/` | Records one tab's API calls as a redacted, structure-only file: `shape.js` (redaction, shared by every part + Node tests), `page-hook.js` (MAIN-world fetch/XHR wrapper, shapes response bodies in-page), `recorder-bg.js` (webRequest capture + grouping, `rec:*` messages), `panel.js` (on-page marking UI). Tests: `node --test extension/recorder/*.test.mjs` |
| Auth + guard tokens | `backend/app/auth.py`, `auth_cli.py` | sessions, `GUARD_SCOPES` (which token opens which route), CLI |
| Apps registry | `backend/app/apps_manager.py` | installed apps (`data/apps_config.yaml`), App Library catalog |

### 5.1 UI conventions
- Views are `<section x-show="win.view === '<name>'">` blocks inside
  `os_shell.html`. State + methods live in the single Alpine component in
  `os_app.js`.
- **Server-side filters need a matching client-side getter** (e.g. the Gmail query
  parser exists in both `inbox_tags.py` and as `matchesInboxQuery` in JS) so
  typing filters instantly.
- Styling is Tailwind + custom classes (`studio-card`, `studio-subcard`,
  `badge-accent`, `win-tool-btn`). Accent is `#2bfbae` on dark slate. Icons are
  inline SVG via `nodeSvg(name, size)`, stroke width `1.8`.
- **Tailwind is precompiled** (`dashboard/assets/tailwind.css`, no build step), so
  only utility classes that already appear in that file work. A new class like
  `bg-amber-400/10` silently does nothing. Grep the CSS (escape `/` and `[`)
  before using a class you haven't seen elsewhere in the shell.

---

## 6. Data model (the tables you'll actually touch)

| Table | Meaning |
|---|---|
| `Notification` | One scraped message. Key v0.11 columns: `tags_csv`, `sender`, `body_text`, `dedupe_key`, `merged_count`, `starred`, `folder`, `tag_confidence`, `tag_engine`, `tag_corrected_by_user` |
| `CourseGrade` | One course from one LMS |
| `CourseVisibility` | Per-course user intent: `hidden`, `tracked`, `counts_toward_gpa`, `tab_id` |
| `GradeTab` | A user-defined course group ("monitor") |
| `InboxLearningHint` | A correction, exportable as fine-tuning data |
| `CalendarEvent`, `Task`, `Goal` | Self-evident |
| `AIJob` | Detached server-side AI run |
| `AuthSession`, `MonitoredApp`, `AgentLog`… | See `docs/ARCHITECTURE.md` |

Migrations: append an idempotent block to `database.py` (create table, `ALTER
TABLE ... ADD COLUMN` guarded by a column check, index, seed). Never destructively
migrate a user's existing `hub.db`.

---

## 7. Hard-won knowledge — do not re-learn these

### 7.1 Sandbox / environment
- The repo lives at `/home/user/node`, not `/home/user` (it moved once already and
  left hardcoded paths behind). Derive paths from `__file__` or `settings`, never
  from an absolute prefix.
- Playwright browsers and Brave are **not** in the snapshot; reinstall after any
  environment reset.
- **Real-browser UI checks in the Arena sandbox** (Playwright's CDN is blocked,
  npm is not): `npm i @sparticuz/chromium playwright-core` in a scratch dir
  outside the repo, call `executablePath()` once (writes `/tmp/chromium`), unpack
  its bundled libs (`node -e` brotli-decompress `bin/al2023.tar.br`, then
  `tar -x` into `/tmp/al2023`), and launch with
  `LD_LIBRARY_PATH=/tmp/al2023/lib` plus the package's `args`. Good enough to
  drive the shell; not used by the pytest browser probe.
- The container clock can differ from the human's wall clock. Tests that depend on
  "now" must pin time through settings, not assume it.

### 7.2 Test isolation trap (cost hours)
`inbox_tags.py` used to bind file paths at import time, so a module-level
singleton leaked real user state (`data/inbox_*.json`) into the test DB and broke
rename with a confusing `400 Tag not found`. Fix: paths resolve lazily through
`_ensure_paths()` keyed on `settings.database_url`. **Never reintroduce
import-time path binding.** If tests act strangely, `rm -f data/inbox_*.json`.

### 7.3 User corrections were being silently undone
The AI notification pass overwrote `tags_csv` on every sweep, reverting hand-made
tags. Fixed with the `tag_corrected_by_user` guard in
`browser_stream.process_notifications_with_ai`. If you add another write path to
notifications, honour that flag.

### 7.4 A reusable tool executor cannot `await`
`execute_secretary_tool` is sync. Async service calls must go in
`execute_secretary_tool_async`. Putting an `await` in the sync one is a
`SyntaxError` at import.

### 7.5 Settings silently drop unknown types
`os_settings.sanitize_patch` only accepts declared keys; a `"type": "string"` that
the sanitizer doesn't understand is discarded without error. Use the declared
types (`enum` + `options`, `bool`, `int`, `range`) and verify the round-trip.

### 7.6 `.ics` past-date blind spot
`parse_ics_content` drops events older than 14 days *before* they reach the DB, so
a fixture with fixed dates silently stops working weeks later. **Always use
relative dates in calendar fixtures** (`_ics_stamp(days, "HHMMSS")` in the test
file). This already bit once: `test_v90` hardcoded 2026-10-26 and started
failing in mid-November.

### 7.7 External API contracts (verified, don't guess)
- **Liquid d1**: `POST {base}/v1/systemone` with `state` + `questions` (TypeSafe
  System One). Hosted model alias is `d1.2`; Ollama serves it at the same path
  after `ollama pull d1`. **llama.cpp/Ollama answer only noul/choice/score** —
  multi-select and text/date need Transformers or MLX, hence the capability
  probe. Docs: `docs.liquid.ai/api/guides/decisions`.
- **Two model roles only:** decision + chat. Don't reintroduce a "fast" role;
  use `model_router.chat_json` for small JSON jobs.
- **api-anything REST**: `POST /v1/call/{site}/{op}` with body
  `{"args": {...}}` — arguments **nest under `args`**. OpenAPI at `/openapi.json`,
  auth `Authorization: Bearer $API_ANYTHING_TOKEN`. Needs Node ≥ 22.13.
- **Ollama**: `/api/tags`, `/api/ps`, and think/tools retries.
- **PowerSchool / Schoology / Canvas**: contracts are documented inline in
  `lms_connector.py`.

### 7.8 Guard tokens are scoped — keep them that way
Clients without a session cookie send `X-Node-Guard-Token`. There are three
tokens (`extension`, `focus_guard`, `worker`). `auth.GUARD_SCOPES` maps each to
its routes, and the auth middleware only accepts a token on its own scope's
routes. To expose a route to a token client, add it to exactly one scope, and
never to a scope whose token is embedded somewhere less trusted (the
focus-guard userscript runs on third-party sites). The token-management API
(`/api/v1/auth/guard-tokens*`) is session-only and deliberately **not** an AI
tool, so secrets never land in chat logs or go to cloud providers. Installs
upgraded from ≤0.11 may be in "legacy shared" mode until the user clicks
*Finish migration* (Settings → System → Device tokens).

### 7.9 The stream websocket must close on failure
`handle_stream_websocket` reports errors as a `{"type":"error"}` frame and then
closes with `1011`. Before 0.12 it never closed, so clients (and a test) hung
forever when the browser couldn't launch. Keep the close.

---

## 8. Definition of done

Before you report a change as complete:

1. `pytest backend/tests/test_api.py -q` → **0 failed** (49 passed with a browser,
   44 passed + 5 skipped without one; more as tests are added).
2. `python3 -c "import app.main"` with `PYTHONPATH=backend` imports cleanly.
3. `node --check backend/app/ui/os_app.js` (and any extension JS) passes.
4. If you touched UI, load it in a browser: no console errors, no `pageerror`.
5. If you touched UI or the extension, run against a **real browser**, not just
   the DOM string.
6. No new file under `data/`, no new hardcoded absolute path, no secret committed.
7. You updated the relevant doc — a feature that isn't documented is half-built.

---

## 9. Git workflow (you open and merge PRs, the human pulls)

The human decided (2026-11): the maintainer agent works on its own branch, opens a
pull request into `main`, checks it's green, and **merges it itself**. The human
then pulls on the server. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the
update commands.

Branch and commit style used in this repo:

```
feat(inbox): merge duplicates across mismatched senders
fix(grades): keep hidden courses hidden across re-scan
docs: add agent handoff
chore: bump to v0.11.0
```

Rules:
- One logical change per commit. Per release, bump `settings.app_version` in
  `config.py` (everything server-side reads it), `extension/manifest.json`, the
  README title and `.env.example` header. A release is due whenever a change
  needs a manual server step.
- Update `CHANGELOG.md` for anything user-visible.
- Never force-push. Never commit `data/` or `.env`.
- If a change needs a manual server step (new env var, Node upgrade, a migration
  the user must run), say so explicitly in the commit body **and** in `CHANGELOG.md`.

---

## 10. Where the human's opinions live

They are strong and specific. Read `docs/PRODUCT_RULES.md` before making product
decisions — it is the distilled set of "we decided X, not Y" that isn't obvious
from the code. If a request seems to conflict with it, ask rather than assume.
