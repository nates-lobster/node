# Changelog

Versions follow semver. Anything user-visible goes here, with a **manual step**
line when the server needs action beyond `git pull && docker compose up -d --build`.

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
