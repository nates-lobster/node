# Product Rules — Decisions Already Made

Every line here was chosen by the human, usually after rejecting the obvious
alternative. **Do not "improve" these without asking.** When a new request seems
to contradict one, surface the conflict instead of silently picking a side.

---

## Apps & installation

- **Zero preinstalled apps.** The installed-apps registry (`data/apps_config.yaml`)
  starts empty. No MS Teams, no Outlook, no demo data. A fresh install is empty by
  design. Demo seeds (`SEED_DEMO_DATA`) stay off.
- Apps install from the **App Library** (17+ templates) or as a **custom URL**.
- Custom Apps and Desktop Widgets are installable packages (`.nodeapp.json` /
  zip) with a documented `window.NodeOS` SDK. See `docs/APPS_AND_WIDGETS_SDK.md`.
- LMS setup lives in **Grades / App Library**. Never call
  `openNewAppModal('PowerSchool')` from the shell.

## Inbox

- **Tags, not categories.** Filters operate on tags. `category` is a legacy
  compatibility field.
- **Rename must never leave two tags behind.** The registry rejects collisions
  before writing; the rename cascades onto every message.
- Gmail/ProtonMail **bulk** aesthetics: no app-name prefix, no `.sr-only` junk, no
  Outlook UI chrome.
- **Read-on-open**, with a short grace period and an Undo affordance.
- **Identical messages merge** — same event, even with mismatched sender or
  service name. One real event = one row with unioned tags.
- Default filter is **important-first**.
- **Recategorisation is training data.** Every correction is recorded and
  exportable (NDJSON) for local fine-tuning.
- **Gmail-style operators** are first-class for both the user and the AI:
  `is:unread`, `is:important`, `from:`, `tag:`, `subject:`, `newer_than:7d`,
  `-tag:promo`, `OR`.
- The user must be able to **ditch the original mail client**: the reading pane
  pulls the full body from the live tab.

## AI

- **All processing is server-side.** Closing the client never aborts a job.
- **Model discovery is dynamic** (`/api/v1/models/ping`, Ollama `/api/tags`).
  Never hardcode a model list.
- **Decision gate first**, then fall back to the smallest/fastest available model.
  The gate tags notifications and attaches only the tools a turn needs.
- **Tool arguments must be correct.** "I have band practice on friday" produces
  the title `Band Practice` and the right date — not the sentence, not today's
  date. Slot questions exist specifically to prevent this.
- AIs **log and reflect** through the shared log/memory files — without spamming
  files and without overfitting to one conversation.
- AIs can perform **every** OS action through tools. If a feature is user-doable
  but not AI-doable, that is a gap.
- There is exactly **one `AI Instructions`** surface. Never "Notebook". No time
  estimates in that window.
- Web search and weekday/date grounding are mandatory before scheduling.

## Tasks

- A **clean to-do list**. No coaching, no free-time budget bar, no gamification.
- Priority, drag order, duration parsing, sync from LMS and Inbox.

## Grades

- **Removing a course must stick** across visits and re-scans. Intent is stored
  even for courses not yet in the DB.
- The user has **complete control** over which courses appear and which count
  toward averages/GPA.
- **Course groups act as tabs** with their own averages — this is the
  multi-monitor story (e.g. one group per monitor).
- `Next Up` is **chronological only** — no hardcoded Bed/Wake times.

## Files

- A mini file explorer / **shared drive** for the user, the AIs, and the server.

## Calendar

- Clicking a date **highlights and expands** it with quick event creation.
- Interactive; `.ics` feeds supported alongside native events.

## Desktop & chrome

- Widgets are **resizable** (and reorderable, hideable, restorable).
- **Headerless equal-split tiling** plus drag-to-swap; floating mode via pull.
- **Settings / Bedtime / Tiled-Floating are out of the top bar by default.** Help,
  Keybinds and Taskbar/OS-Bar editing live **inside Settings**.
- Start Menu is searchable and keybound.
- Buttons feel tactile; icons are `1.8px` stroke.
- Favicon is the branching-circle mark.
- Default theme: **dark slate with `#2bfbae` accent**.
- Lock screen must leak **zero** data before authentication.

## New tab / browser

- A Brave/Chromium **new-tab override** with switchable modes: full dashboard,
  wallpaper-only lock, and a custom minimal page (clock, to-do, urgent items).
- **Zero-Google Brave** is the streaming engine.
- Prefer **learned APIs** (api-anything) over browser-window streaming where
  reliable; the extension may read cookies from the user's local session so
  server-side sessions don't expire.

## Focus guard

- **Multi-platform**, with per-site daily caps, short-form nudges, AI
  accountability prompts (non-pausing), and force-close on hard cap.

## Things explicitly rejected — do not reintroduce

| Rejected | Instead |
|---|---|
| Coaching / free-budget bar in Tasks | plain to-do list |
| "Notebook" naming for AI instructions | single `AI Instructions` section |
| Hardcoded Bed/Wake entries in Next Up | chronological only |
| Apps preinstalled "to be helpful" | empty registry, App Library |
| The separate `scrapers/` container | in-process browser tabs + api-anything (removed in 0.12) |
| One guard token shared by every client | per-client scoped tokens (0.12) |
| Category-based inbox filters | tag-based |
| Overwriting user tag corrections | corrections are authoritative |
| Time estimates inside the instructions window | omit them |
| Seeded demo data | empty, clean install |
