# Node (v0.15.0)

Self-hosted personal desktop WebOS, zero-Google **Brave** HD 1080p server-streamed web app workspace, multi-LMS academic tracker, universal social media focus guard, custom app/widget platform, and detached server-side multi-provider AI coordinator built for Linux hosts (`max@athlon`, Docker Compose / Dockge, Tailscale, and Cloudflare Tunnel).

---

## Read this first

| If you are… | Start here |
|---|---|
| **An AI agent maintaining this repo** | [`AGENT.md`](AGENT.md) — assumes zero context |
| **Deploying or updating the server** | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| **Making a product decision** | [`docs/PRODUCT_RULES.md`](docs/PRODUCT_RULES.md) — what was decided and what was rejected |
| **Touching backend code** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — process model, data flow, failure→cause table |
| **Curious about the last release** | [`docs/V10_2_STRUCTURAL_PASS.md`](docs/V10_2_STRUCTURAL_PASS.md), [`CHANGELOG.md`](CHANGELOG.md) |

**Quick start:**

```bash
cp .env.example .env                 # set credentials + TZ
docker compose up -d --build         # http://<server>:8080
```

**Golden rule:** code is tracked in git, **data is not**. Everything the server
writes lives in `data/` (SQLite DB, uploads, browser sessions, credentials). It is
gitignored and host-mounted, so `git pull` updates the code and never touches your
data.

---

## What's New in v0.15.0

**API Recorder, a tool for building connectors.** Instead of digging through
DevTools, click the extension → **Record this tab** on Outlook, Teams or any
site, use the site normally, and tick the calls that matter. The on-page panel
can also **Watch my next action** and highlight just the calls that fired.
**Save marked** writes a JSON file that describes the API's structure (URLs,
header names, cookie names, JSON keys and types) and leaves out messages,
tokens and cookie values. Settings → **Download extension** now works in Docker
too. See [`CHANGELOG.md`](CHANGELOG.md).

---

## What's New in v0.14.0

**One email, one row.** Copies of the same message (pop-up, page row, sender
glued to the subject, another app) are merged as they arrive, with or without
the AI; the decision model settles close calls. Merged emails no longer pile
up every copy's tags. Run **Inbox → Merge duplicates** once after updating to
clean up old copies. See [`CHANGELOG.md`](CHANGELOG.md).

---

## What's New in v0.13.0

**Two models, one Ollama.** See [`CHANGELOG.md`](CHANGELOG.md).

- **Decision model + Chat model** replace "model vs fast model". The decision
  model (LFM3 **d1**, `ollama pull d1`) tags the Inbox, sets priority and
  routes tools in one forward pass. The chat model does everything that
  writes text. Both are picked in **Settings → AI Models**.
- **Von is removed.** The decision model runs on the same Ollama server, so no
  extra container is needed.
- **Works on Ollama/llama.cpp:** where the runtime can't do multi-select or
  typed answers, d1 picks the main tag in one question and double-checks only
  the likely extra tags (0.13.1).
- **Auto-sort respects your corrections,** and CI now runs the tests on every PR.

---

## What's New in v0.12.0

A security and maintenance release. **One manual step if you installed apps.**
See [`CHANGELOG.md`](CHANGELOG.md).

- **Scoped device tokens.** The extension, the focus guard and remote workers
  each get their own guard token, limited to their own routes. Manage them in
  **Settings → System → Device tokens** or with `auth_cli guard-tokens`. Upgrades
  keep working in a *legacy* mode until you click **Finish migration**.
- **Installed apps are now user data** (`data/apps_config.yaml`), not a tracked
  file. The unused `scrapers/` container is gone.
- **Fixes:** the stream window no longer hangs when the server browser can't
  start, the system status reports the real version, and the extension's
  new-tab settings sync no longer depends on a session cookie.
- **Tests run anywhere:** no hardcoded paths, real-browser tests skip cleanly,
  and calendar fixtures no longer expire.

---

## What's New in v0.11.0

Versioned for git-based deployment. The product surface is the v10.2 structural
pass, plus a maintainer handoff:

- **LFM3 decision gate (d1-2B)** replaces the placeholder classifier. Tags are the
  primary classification; filters are tag-based. The gate also pre-selects only the
  tool families a chat turn needs and fills event slots correctly
  (`"band practice on friday"` → `Band Practice` on the right date).
- **Inbox is a real mail client** — click-to-read with full body harvest, duplicate
  merging across mismatched senders, editable tag registry, corrections that train
  the gate, and a Gmail query language shared by you and the AI.
- **api-anything bridge** — learned HTTP APIs instead of browser-window streaming
  (verified live: `tier 1`, ~110 ms, JSON, no browser), with cookie import from the
  extension so server sessions stop expiring.
- **MV3 extension** — Brave/Chromium new-tab override in three modes, downloadable
  in-app as a zip, authenticated by a scoped guard token.
- **Grades you control** — hidden courses stay hidden across re-scans; course
  groups act as tabs with per-group averages.
- **Docs for continuity** — `AGENT.md`, `docs/ARCHITECTURE.md`,
  `docs/PRODUCT_RULES.md`, `docs/DEPLOYMENT.md`, `CHANGELOG.md`.

---

## What's New in v10.2 (Structural Pass)

Full write-up: [`docs/V10_2_STRUCTURAL_PASS.md`](docs/V10_2_STRUCTURAL_PASS.md)

### 1. LFM3 decision gate replaces the placeholder classifier
Every notification and chat turn runs through **Liquid AI d1-2B** (`d1.2` hosted / `LiquidAI/d1-2B` local, TypeSafe System One protocol). It answers typed questions in **one forward pass with calibrated confidence**, so classification costs no generation. It decides **tags first** (filters are tag-based, not category-based) and, for chat, attaches **only the tool families a turn needs** — ~6 tools instead of 36 — with dedicated slot questions so `"band practice on friday"` becomes `Band Practice` on the right date, not the whole sentence as a title. Offline, Node falls back to the chat model and then to deterministic heuristics.

### 2. Inbox rebuilt as a real mail client
Tag registry with editable descriptions (the description *is* the classifier criteria), rename that can never strand two tags, click-to-read with a full reading pane, on-demand body harvest from the live tab so you can drop the mail client entirely, duplicate merging across mismatched senders/services, a proper conversation list, user filters with an important-first default, and a **Gmail-style query language** (`is:unread`, `from:`, `tag:school OR tag:work`, `newer_than:7d`, `-tag:promo`) shared by the UI and the AI. Every correction is recorded and exportable as NDJSON for local fine-tuning.

### 3. api-anything replaces browser-window streaming
Learned HTTP APIs instead of a ~70%-reliable stream window: **verified live at `tier: 1`, 149 ms, JSON out, no browser.** Node registers the bridge, enumerates its operations, and calls them through `POST /api/v1/bridges/call`. The MV3 extension feeds cookies from your real local session into the bridge store, so server-side sessions stop expiring.

### 4. Browser extension + new-tab modes
`extension/` is a complete Manifest V3 extension, downloadable in-app as a zip. It can take over the Brave/Chromium new-tab page in **dashboard / minimal / wallpaper** mode, and it syncs session cookies for the domains you list — authenticated by a **guard token** that unlocks exactly three routes.

### 5. Grades you actually control
Removing a course **sticks** — the visibility row is written even for courses not yet in the DB, so scans can't resurrect them, and averages/GPA/briefing all recompute over the visible set. Course **groups** act as tabs with per-group averages (e.g. one per monitor).

### 6. Fixes worth calling out
User tag corrections are now authoritative and can no longer be overwritten by a later AI pass; offline quick-capture classification is correct without a model; event titles strip trailing dates/times; month names (`oct 3`) parse and roll to next year correctly; the new-tab lock scrim no longer blacks out the page.

---

## What's New in v10.0 (Production Release)

### 1. Resizable Desktop Widgets, App Library & Custom Apps / Widgets SDK
- **Resizable & Customizable Desktop Widgets:** Every widget on the 12-column Desktop grid can be resized (`3–12` columns × `1–4` height rows via corner drag handle or quick size controls), reordered, hidden/removed, or restored via the `+ Widgets` picker — plus pinned web apps and sandboxed custom widgets installed from the Extension Studio.
- **Zero-Preinstalled App Library:** Fresh installations ship completely clean (the installed-apps registry in `data/apps_config.yaml` starts empty — no hardcoded MS Teams or Outlook defaults). Open **Monitors → App Library** for 1-click installation across 17+ templates: **Gmail, ProtonMail, Outlook, Microsoft Teams, Slack, Discord, GroupMe, WhatsApp Web, PowerSchool, Schoology, Canvas LMS, Google Classroom, AP Classroom, Infinite Campus, Gradescope, GitHub, and Notion** — or add any custom web URL with isolated multi-account browser profiles.
- **Installable Custom Apps & Desktop Widgets (`window.NodeOS` SDK):** Build, install, export, and upload `.nodeapp.json` / `.zip` packages (`manifest.json` + HTML/CSS/JS) that run as native Node windows and/or live Desktop widgets via a capability-gated `postMessage` bridge (`storage`, `tasks`, `calendar`, `grades`, `notify`, `ai`). Full developer guide: [`docs/APPS_AND_WIDGETS_SDK.md`](docs/APPS_AND_WIDGETS_SDK.md) (also viewable in-OS under **Settings → Custom Apps & SDK**).

### 2. Generalized Multi-LMS Grades & Assignments (PowerSchool + Schoology + Canvas + Classroom)
- **Generalized Grades App:** Replaces single-LMS assumptions with unified multi-portal tracking across **PowerSchool**, **Schoology**, **Canvas LMS**, **Google Classroom**, **Infinite Campus**, **Gradescope**, and manual/custom courses, complete with per-portal filter pills, GPA/average rollups, score deltas, sparkline history, and inline portal setup.
- **Schoology Native Interception (`leftathome/schoology-go` Architecture + `.ics` Fallback):** Extracts `Drupal.settings.s_common` CSRF credentials (`X-CSRF-Token` / `X-CSRF-Key`) inside authenticated Brave sessions to query `/iapi/parent/info`, `/iapi2/site-navigation/courses`, `/iapi/parent/overdue_submissions/{uid}`, student `/home` `.upcoming-events`, and `/grades/grades` — gracefully falling back to courses + overdue/upcoming assignments + Schoology `.ics` feeds on SIS-locked (`403`) parent tenants.
- **Canvas LMS REST Interception:** Queries authenticated same-origin Canvas endpoints (`/api/v1/courses?include[]=total_scores`, `/api/v1/courses/:id/assignments?include[]=submission`, `/api/v1/planner/items`, `/api/v1/users/self/todo` with automatic `while(1);` prefix stripping) to pull course percentages, letter grades, missing/unsubmitted items, and planner deadlines.
- **PowerSchool Full-Course & XTE Assignment Sync:** Expands `colspan` cells on `table#tblgrades`, traverses `/guardian/scores.html` + `POST /ws/xte/assignment/lookup`, preserves cached SQLite grades across OIDC `max_age=600` re-auth walls, and bridges missing/upcoming LMS assignments directly into **Tasks**.

### 3. Detached Server-Side AI Queue, Multi-Provider Models, Shared Agent Logs & Web Search
- **Dynamic Local/Remote Ollama + Cloud API Providers:** Configure local Ollama, remote LAN/Tailscale Ollama servers, OpenAI-compatible endpoints (OpenAI, OpenRouter, Groq, Together, vLLM), Anthropic, and Google Gemini. The decision model (`d1`) is served by the same Ollama over `/v1/systemone`. Remote Ollama workers can also self-register via `POST /api/v1/models/ping`. Every model exposed by Ollama (`qwen3.5:2b`, `qwen3.5:0.8b`, `gemma4:e2b`, Llama, Mistral, DeepSeek, etc.) is discovered dynamically with automatic thinking/tool-capability fallback (`OLLAMA_MODEL=auto`).
- **100% Server-Side AI Execution Queue (`AIJob`):** Every prompt runs inside a detached server-side `asyncio.Task` persisted to SQLite (`POST /api/v1/ai/jobs/chat` + live reconnectable `GET /api/v1/ai/jobs/{job_uid}/stream`). Fire off multiple prompts across contexts, close or reload your browser tab mid-generation with **zero network errors**, and return anytime to completed responses and executed OS actions.
- **Shared Multi-Agent Workspace Logbook:** AI instances share persistent read/write/edit/append memory (`agent_logs` SQLite table + `/data/ai_shared_log.md`) so reasoning models, fast triage models, and 24/7 Cron passes actively leave discoveries, user preferences, and operational notes for one another.
- **Web Search + Exact Weekday/Date Grounding:** Built-in `web_search` + `fetch_webpage` tools plus server-side 14-day calendar lookup (`verify_and_fix_weekday_date`) that eliminates off-by-one weekday scheduling mistakes.

### 4. Universal Social & Media Focus Guard + Full Theme Customization
- **Universal Social & Media Focus Guard:** Expands YT Guard into a cross-platform Manifest V3 extension (`/api/v1/focus-guard/extension.zip`) & userscript (`/api/v1/focus-guard/userscript.user.js`) covering **YouTube (Shorts + Watch), Instagram (Reels + Feed), TikTok, Facebook, X / Twitter, Reddit, Twitch, Snapchat, Netflix**, plus user-defined custom distraction domains — with per-platform daily caps, Shorts/Reels nudges, non-pausing AI accountability prompts, and tab force-close on hard cap.
- **Complete Settings & Theme Customization:** Full control over accent, desktop canvas, window surface, card surface, primary text color, font family (`Inter`, `Geist`, `JetBrains Mono`, `System`, `Serif`), glass blur, border radius, UI/dock scaling, tiling behavior, 24/7 Cron cadence, AI provider routing, and custom CSS/JS presets.

---

## Server-Only Credential Setup (SSH / `.env` Only)

Credentials can **only** be configured on the server (never from the browser), and the unauthenticated Lock Screen leaks zero user, app, or schedule data:

### Option 1 — Interactive SSH CLI (`recommended` — stores PBKDF2-SHA256 hash in `/data/auth_credentials.json`)
```bash
docker exec -it hub-backend python -m app.auth_cli set max
# Prompts securely for password (hidden input) and writes PBKDF2-HMAC-SHA256 hash.

# Inspect active credential source & active sessions:
docker exec -it hub-backend python -m app.auth_cli status

# Revoke all active browser sessions immediately:
docker exec -it hub-backend python -m app.auth_cli revoke-sessions

# List / rotate the scoped device tokens (extension, focus_guard, worker):
docker exec -it hub-backend python -m app.auth_cli guard-tokens
docker exec -it hub-backend python -m app.auth_cli guard-tokens rotate focus_guard
```

### Option 2 — `.env` / Dockge Environment Variables
Both `NODE_AUTH_*` and `AUTH_*` prefixes are supported seamlessly:
```env
NODE_AUTH_ENABLED=true
NODE_AUTH_USERNAME=max
NODE_AUTH_PASSWORD_HASH=pbkdf2_sha256$600000$...
# Or plaintext server env fallback:
NODE_AUTH_PASSWORD=your-strong-server-password
```
*(Default fallback before configuring `.env` or `auth_cli` is `admin` / `changeme`.)*

---

## Deploy / Upgrade (`max@athlon` • Docker Compose / Dockge • Tailscale • Cloudflare Tunnel)

```bash
cp -n .env.example .env
# On the Ollama box: the decision model plus any chat model (Node discovers both):
ollama pull d1
ollama pull qwen3.5:2b  # any chat model; Node lists everything Ollama has
docker compose up -d --build
```

### Remote Ollama Worker Ping (Optional)
Any secondary machine running Ollama on your LAN or Tailscale tailnet can register its live models with Node:
```bash
MODELS=$(curl -fsSL http://127.0.0.1:11434/api/tags | jq -c '[.models[].name]')
curl -X POST "http://<node-host>:8080/api/v1/models/ping" \
  -H "Content-Type: application/json" \
  -H "X-Node-Guard-Token: <Workers token from Settings → System → Device tokens>" \
  -d "{\"provider_id\":\"remote-ollama\",\"provider_name\":\"Remote Ollama\",\"protocol\":\"ollama\",\"base_url\":\"http://<worker-tailscale-ip>:11434\",\"models\":$MODELS}"
```

- **Tailscale:** Open `http://<athlon-tailscale-ip>:8080`
- **Cloudflare Tunnel (`cloudflared`):** Point your public hostname in Zero Trust → Tunnels to `http://127.0.0.1:8080` (or `http://hub-dashboard:80` on the shared Docker network). Nginx disables proxy buffering on streaming endpoints (`proxy_buffering off`) and forwards `X-Forwarded-Proto: https` automatically.
- **Custom Apps & Widgets SDK Documentation:** [`docs/APPS_AND_WIDGETS_SDK.md`](docs/APPS_AND_WIDGETS_SDK.md)
