# Changelog

Versions follow semver. Anything user-visible goes here, with a **manual step**
line when the server needs action beyond `git pull && docker compose up -d --build`.

---

## 0.15.0 — 2026-11-20

A tool for building connectors from the sites you're already logged into.

### Manual steps
- **Reload the extension** (`brave://extensions` → Node WebOS → ↻, or install
  the new zip). It asks for two new permissions, *scripting* and *webRequest*,
  which the recorder needs. They're only used while you're recording.
- Docker users: `git pull && docker compose up -d --build` also mounts
  `./extension`, so **Settings → Download extension** stops returning 404.

### Added
- **API Recorder** (extension popup → **Record this tab**). It records the
  current site's API calls and groups them one row per call type, so ~300
  requests become ~20 rows. It flags the likely-useful calls (lists of
  messages, `service.svc`, `chatsvc`, GraphQL…) and hides telemetry.
  - **Watch my next action:** do one thing in the site (open an email) and the
    panel highlights just the calls that fired. Name it, and they're marked.
  - **Save marked / Copy** gives you a JSON file with each call's method,
    redacted URL, request header *names*, cookie *names*, the auth style
    (bearer, OWA canary, Skype token, cookies) and the request/response JSON
    *shape*. Personal text becomes `<text N>`, emails `<email>`, and ids and
    tokens are masked. Header values are only kept for protocol headers
    (`action`, `content-type`…).
  - It catches the calls a page makes while loading (reload once after you
    start), calls made by the site's background workers, and OWA's
    `x-owa-urlpostdata` request bodies.
  - Nothing is uploaded. Data stays in the browser session until you save it
    and is cleared when the browser closes.

### Fixed
- `GET /api/v1/ext/package.zip` returned 404 under Docker because the image
  doesn't contain `extension/`. Compose now mounts it read-only
  (`EXTENSION_SOURCE_DIR` overrides the path). Test files are left out of the
  zip.
- Cookie sync now also covers `outlook.cloud.microsoft`, `teams.microsoft.com`
  and `teams.cloud.microsoft` (new installs; existing installs keep their
  saved domain list, so add these in the extension's Settings).

### Developer
- CI runs `node --check` on the extension and `node --test` on the recorder
  (redaction + background logic with `chrome.*` stubbed).

---

## 0.14.0 — 2026-11-20

Fixes "one message shows up five times".

### Manual steps
- After updating, open **Inbox → Merge duplicates** once. It cleans up the
  copies already stored from the last 14 days.

### Fixed
- **Duplicates stop at the door.** The tab code can report one email five ways
  (page row, JSON, websocket, desktop pop-up, unread counter), often as
  "Sender — subject" or with the sender as the title. Every path now checks
  recent rows first and folds a look-alike into the existing row. This needs
  no AI, so it works while the models are offline. Word order, sender labels
  and preview length don't matter, but numbers do (Unit 5 ≠ Unit 6).
- **The decision model judges close calls**, e.g. "essay feedback posted" vs.
  "essay due tomorrow". It's asked "same message?" and only a confident yes
  merges.
- **A merged email no longer shows under every copy's tag.** Merges used to
  combine the tags of all copies. Now your correction wins, otherwise the
  merged row is re-tagged once from its fuller content.
- A duplicate arriving later no longer marks a read message unread again.
- Unread-counter pings update one row per app instead of adding a row each
  time the count changes.
- Intercepted items keep the sender in the sender field instead of gluing it
  onto the subject.

---

## 0.13.1 — 2026-11-19

### Changed
- **Faster tagging on Ollama.** Instead of one yes/no question per tag, d1
  now picks the main tag in a single `choice` question and double-checks only
  the 3–4 runners-up for extra tags. With 20 tags that's about 5 questions
  instead of 20, and the result is the same or better.

---

## 0.13.0 — 2026-11-19

Two AI models instead of "model vs fast model", and Von is gone.

### Manual steps
- **Pull the decision model on your Ollama box:** `ollama pull d1`. Node finds
  it automatically on the same `OLLAMA_BASE_URL` (`/v1/systemone`), so no new
  server or container is needed. Optional: `OLLAMA_KEEP_ALIVE=-1` on the Ollama
  server keeps both models loaded.
- **If you used Von:** delete `VON_BASE_URL` / `VON_API_KEY` from `.env` and
  stop the old container (`docker rm -f hub-von`). The `von` compose profile no
  longer exists.
- Optional: set `CHAT_MODEL` in `.env`. `OLLAMA_MODEL` / `OLLAMA_FAST_MODEL`
  still work as fallbacks, and saved Settings routing migrates on its own.

### Changed
- **AI roles are now Decision model + Chat model** (Settings → AI Models). The
  decision model (LFM3 d1) tags the Inbox, picks priority and routes tools; the
  chat model handles chat, tools, briefings and every other generation. The old
  Primary / Fast / Briefing pickers merge into the chat model.
- **One Ollama for both.** The decision model defaults to the Ollama server's
  `/v1/systemone` endpoint; `DECISION_BASE_URL` is only needed for a separate
  System One server. With routing on *auto*, Node picks the d1 tag Ollama has
  installed.
- **Decision models never get picked as the chat model,** even if they're the
  only model loaded in RAM. Choosing one as the chat model is rejected.
- **Auto chat-model choice** prefers models already loaded in RAM, then the
  smallest, so the first reply doesn't wait on a cold load.
- **Inbox fallback chain** is now decision model → chat model → rules. The
  categories card shows which one is active.
- `DECISION_TIMEOUT_SECONDS` default raised from 6 to 12 to cover Ollama
  cold-loading d1.

### Fixed
- **Works on Ollama / llama.cpp, not only Transformers.** Node probes what the
  server's System One answers. Where multi-select isn't available, tagging and
  tool routing ask one yes/no question per tag or tool family instead of
  failing, and skip the text/date reads.
- **Auto-sort no longer overwrites tags you corrected by hand**
  (`tag_corrected_by_user` is authoritative).
- Online status dots in Settings were invisible (the green class wasn't in the
  prebuilt CSS).

### Removed
- Von: `VON_*` settings, its probes and status fields, the `von` compose
  profile, and the `primary_chat` / `fast_router` / `von_router` keys in
  `/api/v1/models/status` (now `chat_model` / `decision_model`).

### Developer
- GitHub Actions runs the test suite on Python 3.12 for every PR and push to
  `main`.

---

## 0.12.0 — 2026-11-19

Security and maintenance. First release managed by the maintainer agent through
pull requests.

### Manual steps
- **Only if you installed apps from the App Library:** the installed-apps list
  moved from the tracked `scrapers/apps_config.yaml` into `data/`. Your server's
  copy has local edits, so `git pull --ff-only` will refuse until you move it.
  Run this in the repo **before** pulling:
  ```bash
  mkdir -p data/sqlite
  cp -n scrapers/apps_config.yaml data/sqlite/apps_config.yaml   # Docker: /data = ./data/sqlite
  # (plain systemd/venv installs: cp -n scrapers/apps_config.yaml data/apps_config.yaml)
  git checkout -- scrapers/apps_config.yaml
  git pull --ff-only && docker compose up -d --build
  ```
  If you never installed an app, just pull.
- **Device tokens (no action required to keep working):** after upgrading,
  open **Settings → System → Device tokens**. Re-download the focus guard
  (Focus Guard window) and point worker scripts at the **Workers** token, then
  click **Finish migration**. Until then the old shared token keeps working
  everywhere it used to.

### Security
- **Guard tokens are scoped per client.** One shared `X-Node-Guard-Token` used to
  open 12 routes for every token holder. A token embedded in the focus-guard
  userscript (which runs on third-party sites) could therefore read inbox
  titles and tasks, and push cookies into the bridge store. There are now three
  tokens, each limited to its own routes:
  `extension` (`/ext/*`, `/newtab/config`), `focus_guard`
  (`/focus-guard/{heartbeat,respond,justify,status}`) and `worker`
  (`/models/ping`, `/ping`). They can be rotated individually. No token can read
  or rotate tokens, and token management is deliberately not an AI tool.

### Added
- Settings → System → **Device tokens**: list, copy, rotate (two-click
  confirm), finish migration.
- `auth_cli status`, `auth_cli revoke-sessions` (documented before, but missing)
  and `auth_cli guard-tokens [list|rotate <scope>|end-legacy]`.

### Changed
- The installed-apps registry is runtime data at `data/apps_config.yaml`
  (`APPS_CONFIG_PATH=/data/apps_config.yaml` in compose). On boot, a non-empty
  legacy registry is copied over if the new one is missing (never overwritten,
  source never deleted).
- Removed `scrapers/` (standalone scraper container no longer used by compose).
- Version is defined once (`settings.app_version`). The FastAPI app and the
  status endpoints read it.

### Fixed
- The stream websocket closes (code 1011) after reporting an error, so a window
  whose server browser can't start shows "Error" instead of hanging forever.
- `/api/v1/health` and `/api/v1/system/status` reported version `7.0.0`.
- The extension's `/api/v1/newtab/config` calls were not reachable with its
  token (they only worked with a session cookie). Its options page now sends
  the token.
- Tests: no hardcoded `/home/user` paths. Real-browser tests skip with the
  launch error when no browser exists (`NODE_TESTS_REQUIRE_BROWSER=1` makes that
  fail instead). Calendar fixtures use relative dates (`test_v90` had expired).
- `start_preview.sh` no longer assumes the repo is at `/home/user`.

### Verified
- 49 tests: 44 passed + 5 skipped (no browser in the agent sandbox); import and
  `node --check` clean. The real-browser tests still need a run on athlon:
  `NODE_TESTS_REQUIRE_BROWSER=1 pytest backend/tests/test_api.py -q`.

---

## 0.11.0 — 2026-10-22

First git-versioned release. Renumbered from the internal `v10.x` line; the
product surface is unchanged from v10.2 plus the handoff work below.

### Added
- **`AGENT.md`** — self-contained handoff for an AI maintainer with no prior
  context: architecture, rules, verification commands, hard-won traps.
- **`docs/ARCHITECTURE.md`** — process model, request lifecycle, AI stack,
  notification pipeline, query grammar, grades flow, bridge, settings, migrations,
  and a symptom → cause table.
- **`docs/PRODUCT_RULES.md`** — the decisions already made, including a table of
  explicitly rejected approaches.
- **`docs/DEPLOYMENT.md`** — first commit, pull-based updates, backups, systemd,
  credentials, and how to hand control to another agent.
- **`CHANGELOG.md`** — this file.

### Changed
- `.gitignore` hardened: `.cache/` (≈760 MB of Playwright browsers), `.npm/`,
  `.local/`, runtime JSON state, uploads, and sandbox noise are excluded. A first
  commit is ~84 files / ~4 MB instead of gigabytes.
- `data/` confirmed as the single runtime root — gitignored and host-mounted, so
  `git pull` can never touch user data.

### Fixed
- **User tag corrections are authoritative.** A later AI pass or re-scan no longer
  overwrites `tags_csv` / `category` once the user has corrected a tag.
- **Offline quick-capture classification.** With no decision model reachable,
  "band practice friday 4pm" is still an event, not a task.
- **Event titles** strip trailing dates and clock times
  (`band practice friday 4pm` → `band practice`).
- **Month-name dates** (`oct 3`, `3 Nov`) parse, and roll to next year once the
  date has passed, instead of silently becoming today.
- **Grades** no longer seed empty "Core"/"Electives" groups that render as dead
  tabs; only the virtual "All Courses" tab exists by default.
- **New-tab lock scrim** no longer blacks out the clock and cards behind the login
  card; it is a tunable legibility veil.
- **api-anything bridge** now speaks the real REST contract
  (`POST /v1/call/{site}/{op}` with `{"args": {...}}`), distinguishes "not
  running" from "listening but rejected the probe" via a TCP check, and starts the
  server with `API_ANYTHING_TOKEN` (there is no `--token` flag).

### Verified
- 45/45 tests passing.
- Live bridged call through Node → api-anything → web: `tier 1`, ~110 ms,
  30 structured records, no browser.
- End-to-end in a real browser: inbox query filtering, reading pane, Grades
  groups/hidden-courses, Settings → Browser Extension, and the standalone new-tab
  page — zero console errors, zero 5xx.

### Manual steps
- None for existing installs. Rebuild the container
  (`docker compose up -d --build`) to pick up the version bump.
- For the API bridge: **Node ≥ 22.13**, then
  `npx -y api-anything serve --port 8787` and set a matching
  `API_ANYTHING_TOKEN` in `.env`.

---

## 0.10.2 — internal (unreleased, folded into 0.11.0)

- LFM3 decision gate (d1-2B) replaces the placeholder classifier; tagging is
  primary, categories secondary.
- Inbox rebuilt as a tag-first mail client: reading pane, body harvest, duplicate
  merge, Gmail query language, learning export.
- Grades: persistent course visibility + course groups as tabs.
- api-anything bridge + extension cookie import.
- MV3 browser extension: new-tab override (3 modes) + session cookie sync.
- Decision model pre-selects tools per turn and fills event slots correctly.

---

## 0.10.1 — internal

- Unified `AI Instructions` surface, chat rating/self-improvement, mini file
  explorer (shared drive), calendar date expansion, Settings reorganised
  (Help / Keybinds / Bars), named chat contexts.

## 0.10.0 — internal

- Resizable desktop widgets, zero-preinstalled App Library, Custom Apps & Widgets
  SDK (`window.NodeOS`), generalized multi-LMS Grades, detached server-side
  `AIJob` queue, multi-provider model discovery, universal Focus Guard.
