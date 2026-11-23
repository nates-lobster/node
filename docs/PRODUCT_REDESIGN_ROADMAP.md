# Product redesign roadmap — API-first, MCP-addressable, user-controlled

**Status updated:** 2026-11-23. This is the working plan for the direction the
user described; it supersedes old assumptions where they conflict. Preserve the
tiled window manager. Do not turn Node into a Mac-style floating-window clone.

## Target principles

1. **A connection is a configured source, not a live window.** For every app,
   choose in this order: (1) official API/library, (2) free maintained community
   protocol/library, (3) app-owned web API with the user's authenticated session,
   (4) server-side headless browser to capture/replay the necessary API calls.
   Never use an LLM agent to reverse engineer a site at runtime. Browser use must
   be headless/server-side, bounded, observable, and an explicit fallback—not a
   visible stream window as the normal app integration.
2. **Connection setup is explicit and diagnosable.** Installing an app should
   offer available methods, initiate a safe connection test, show required
   account permissions and the exact result, then offer Sync now / pause / forget.
   A healthy cookie-import count is not a connected API: cookies, bearer tokens,
   OAuth scopes, local storage and app-specific endpoints are distinct.
3. **No silent data destruction.** The owner can reset data, but the UI must
   state scope, take/offer a backup, ask for a typed confirmation, preserve the
   deployment's login mechanism unless explicitly selected, and report what was
   removed. Retain the existing zero-preinstalled-app behavior for new installs.
4. **Complete user control.** Apps, displayed modules, settings, grade sources,
   course inclusion, weights, layouts and desktop profiles are editable. Defaults
   are useful, but a user can hide or reorder them. Tiling stays a first-class WM.
5. **AI actions have an API/MCP twin.** Every safe action offered in the UI must
   have a typed server operation callable by Node AI and by an authenticated MCP
   client. Destructive/write actions have explicit scopes and confirmation.
6. **Visual language:** macOS-inspired translucent glass, layered blur, pills and
   squircle controls, with Node's slate/emerald identity; preserve compact
   information density and the tiled-window interaction model.

## Current audit: what exists and why it feels inconsistent

### Apps / connections

- `apps_manager.py` owns installed apps in `data/apps_config.yaml`; a clean
  install intentionally starts empty. A code rebuild shouldn't erase that file.
- `main.py` / `os_app.js` Apps currently call many apps “monitors”; app templates
  include DOM selectors and actions that open server-streamed browser windows.
  This is the older architecture the owner wants faded out.
- The extension copies cookies for configured domains into the server's
  `api_bridge` cookie store. The reported “41 cookies” means 41 cookie records
  were captured/accepted, **not** 41 API integrations or successful sign-ins.
  Cookie-only auth does not satisfy APIs requiring bearer tokens, OAuth scopes,
  app-specific headers, IndexedDB/local-storage tokens or additional device
  state.
- An API connector exists for Outlook and Teams (v0.16); its OWA/Teams request
  auth is captured from Node's own authenticated profile. Other sites still use
  the legacy streamed-tab/DOM approach or remain unconnected. The connector
  framework needs a unified connection registry, protocol capability list,
  user-facing diagnostics, and a headless-only fallback path.
- Important resilience rule: don't change a visible `openStreamWindow` affordance
  into a silent background login. First make setup explicit, then migrate each
  service with tests and preserve the legacy integration as a temporary fallback.

### YouTube / social Focus Guard

- Server-side Focus Guard is present and its default state is enabled. It has
  platform rules, watched-time state, task/schedule-aware gates, justification,
  force-close response and generated browser-extension/userscript packages.
- **It is not bundled into the cookie/new-tab extension.** The Focus Guard is a
  separate generated extension download under Settings → Focus & Browser.
  Installing/updating only the new-tab/cookie extension will not install its
  content script. This split package is a likely reason the formerly integrated
  behavior appears disabled.
- **Implemented in this branch:** video pre-analysis and non-obvious typed
  justifications ask the `decision_gate` d1 service using typed choice
  questions and active task/schedule context. Deterministic fallback remains
  when d1 is offline; the hard-cap gate cannot be softened. Added a mock-backed
  test. Focus Guard settings now explicitly say this is a separate extension
  from New Tab/cookie sync. A single combined extension install path is still
  open.
- Immediate repair: provide a single current Focus Guard install/health state in
  Settings, make old/new package versions detectable, and test heartbeat → d1
  decision → nudge/close across a normal video and a short-form page.

### Inbox / tasks

- Inbox rows persist in SQLite under `data/`; pulling/rebuilding code never
  removes old rows, by design. The app is therefore not “clean” just because
  the image was rebuilt.
- v0.16.3 makes title-click content expand inline and removes incomplete tasks
  at least 7 days past due when the task list refreshes. Completed tasks stay.
- Still needed: cleanup/retention controls and a confirmed reset, plus ensure
  API-sourced message bodies make live-tab reading a fallback rather than the
  only way to read content.

### Grades

- Existing controls include course hide/restore, counts-toward-average, course
  groups (tabs) and scan-all. These are powerful but spread across controls and
  still encourage “sync all, then remove unwanted courses”.
- Missing: source-level inclusion before scan, explicit per-course weight/credit,
  visible weighted and unweighted views, and a single concise setup surface.

### New tab / desktop

- The MV3 new-tab override is `extension/newtab.html` + `newtab.js`. The separate
  `dashboard/newtab.html` is another mini dashboard; duplicated new-tab paths
  create mode drift and confusing fallback behavior.
- Server desktop widgets support ordering/size/toggle; settings include a
  new-tab mode. There is no independent “Work” vs “Display” desktop profile with
  its own widgets, layout and display-level element positions.
- `Customize Brave` text does not exist in this repository; owner confirmed that
  bar was a footer issue and fixed it locally. Don't reintroduce it.

### MCP / all-action parity

- `secretary.py` has an internal tool registry and tool executor; this is not a
  general authenticated MCP server exposing the whole application.
- `api_bridge.py` can talk to api-anything (which has its own REST/MCP), but that
  does not make Node's app operations MCP-accessible.
- Plan a single typed action registry behind HTTP and MCP adapters, with session
  auth / scoped device tokens, schemas, idempotency, confirmations and audit log.
  Build from existing route/service functions rather than duplicating business
  logic or exposing arbitrary SQL, Python, filesystem paths or bearer secrets.

## Progress in this session

- **Done and merged in v0.16.3 / PR #11:** Inbox content opens below the selected
  title; incomplete tasks overdue by 7 days are pruned on task-list refresh;
  new-tab mode falls back to dashboard when no saved mode can be read. Full
  backend suite: 70 passed, 6 browser-dependent skipped.
- **In this branch:** Focus Guard video pre-analysis and justification use d1
  typed choice when available, pass task/schedule context, and keep deterministic
  fallback/hard cap. The Apps window now shows per-app API connector health and
  Sync now; installing a supported Outlook/Teams app immediately runs its stable
  connector(s), rather than opening a visible browser window. Apps without an
  adapter say so and keep browser streaming as an explicitly labeled fallback.
  Updated `PRODUCT_RULES.md` for API-first, user control, MCP parity and the new
  visual direction. This code still needs tests and review.
- **Done in this report:** mapped app/connector architecture, identified the
  split Focus Guard/new-tab extension packages, documented this migration and
  safety contract.
- **Not implemented yet:** unified API-first Apps setup, automatic adapter
  selection/sync, app health/status, full reset, dual desktop profiles, grade
  source/weight controls, single-extension Focus Guard bundle and generic MCP
  server. These are grouped below for progression, not claimed as shipped.

## Proposed delivery sequence (small PRs, each usable)

### A. Connection foundation — next

- Add a normalized connection model/store: app slug, installed, connector type,
  connection status, last test/sync, safe diagnostic, user-selected strategy.
- Separate “installed app” from “connected data source”. Show capabilities
  (official API / community protocol / session API / headless adapter) and
  selection controls. Never imply that synced cookies mean connected.
- Add a connection test API and Settings → Apps status panel. Ensure each new
  connection does an initial sync immediately, within service rate limits.
- Replace automatic `openStreamWindow()` entry points with “Connect / Test”;
  only offer a headless fallback if the app explicitly lacks a supported API.

### B. Migrate adapters app by app

Priority: existing Outlook/Teams; Proton via free hydroxide + IMAP; Schoology
via schoolopy if institution credentials exist; PowerSchool student portal API;
Canvas official API where user token is allowed; Gradescope community client;
then Google Classroom. For each adapter: document auth requirements, add mock
contract tests, dedupe by stable source ID, preserve user tag corrections, expose
scopes and last error, and avoid writing credentials to Git/logs. Keep private
web APIs isolated behind an adapter and a shape-change alert.

### C. Restore Focus Guard as an integrated feature

- Reuse current D1 capability and typed choice/noul APIs. Ask d1 for
  `coursework_aligned`, `educational_general`, or `entertainment_distraction`,
  and match active task/course context. If d1 is offline, use deterministic
  heuristic + ask the user; never silently claim AI approval.
- Move Focus Guard content/background handlers into the one downloaded Node
  extension, or make the two required packages install/update atomically.
  Preserve opt-in per-platform toggles and explain the required browser
  permissions. Add a diagnostic (`extension installed/version`, last heartbeat,
  last policy verdict) to settings.
- Test generated ZIP manifest matches, permissions, content-script matches,
  heartbeats, shorts, SPA navigation, and force-close. Add no hidden stealth.

### D. Inbox / reset controls

- Add a visible retention and bulk cleanup panel, separate from the destructive
  full reset.
- Full reset has two explicit levels: “clear app data” (notifications, tasks,
  grades, calendar events, connector cursors) or “reset installation” (also app
  registry/settings/extension state). Show the exact list, create an on-host
  backup under ignored `data/`, require typed confirmation, keep login credentials
  by default, and provide a restart/first-run checklist. Never delete Docker
  mounts, `.env`, browser profiles or auth unless user selects each.

### E. Grade workflow

- Source selection at the top: enabled grade apps are selected once, before
  syncing; per-source status and connect method is visible there.
- Add course weight/credit and a calculation mode (unweighted / weighted / both).
  Do not invent school grading scales: offer default scale plus editable mappings.
- Make course include/ignore, group membership and weight editable inline in one
  grid; persist through scans. Show calculation assumptions and per-course
  contribution. Add fixtures for weighted averages and exclusion behavior.

### F. Two desktops / display mode

- Add persistent `work` and `display` desktop profiles; each owns widget set,
  layout, taskbar visibility, clock and arbitrary element geometry. Keep global
  tiled WM behavior for application windows. In display mode, no forced dock or
  settings; a configurable hotkey/button returns to work desktop.
- Add editor handles with snap/alignment guides, positions/size, opacity, blur,
  typography, visibility and lock state. Design responsive screen safe-areas and
  a clear restore-layout action. The extension new tab opens the selected display
  profile, not a separate hardcoded mini dashboard.

### G. MCP/action parity and design system

- Inventory every user-facing action vs existing internal AI tools vs public
  authenticated route; add missing operations. MCP should call the same service
  layer as HTTP/AI, with narrow read/write scopes and auditability.
- Replace legacy “Stream app” labels with source setup/sync; progressively remove
  forced modules and stale browser-window copy after their APIs replace them.
- Consolidate duplicate new-tab surfaces; adopt reusable glass panels, pills,
  squircle controls, accessible focus states, high contrast and reduced-motion.
  Keep customization controls discoverable and defaults user-editable.

## Review checkpoint

The order above respects the user's constraints and avoids a risky all-at-once
rewrite. Start with A and C (fix connection setup and the missing Focus Guard
integration), then D and E, then F and G. Keep API connectors and existing data
stable throughout. This document should be updated with each merged PR and
changed acceptance criteria.
