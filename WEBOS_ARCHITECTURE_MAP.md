# Node WebOS Architecture & Safe Customization Map

This file provides a fast, unambiguous navigation guide for both **local AI (`qwen3.5:2b`)** and developers so custom UI/behavior changes always target the right layer and **never break core OS functionalities**.

---

## 1. Layered Separation of Concerns

| Layer | Files | Responsibility | Safe for Presets / Custom Code? |
| :--- | :--- | :--- | :--- |
| **Customization & Settings Layer** | `backend/app/os_settings.py`<br>`backend/app/ui/os_customizer.js`<br>`data/os_settings.json`<br>`data/os_presets.json` | Typed 34-key `SETTINGS_SCHEMA`, Preset CRUD, CSS variable derivation, scoped `<style>` injection, and disposable `api.*` JS hook sandbox. | **YES (Primary Target)** |
| **Visual Tokens & Layout Shell** | `dashboard/styles.css`<br>`backend/app/ui/os_shell.html`<br>`backend/app/ui/os_app.js` | CSS custom properties (`:root`), Topbar, Auto-Hiding Taskbar/Dock, Equal-Spaced + Focus-Aware Tiling WM, and Settings Studio UI. | **YES (via Schema & Tokens)** |
| **Protected Core Kernels** | `backend/app/auth.py`<br>`backend/app/browser_stream.py`<br>`backend/app/database.py` | Zero-leak PBKDF2-SHA256 Lock Screen auth, Brave Zero-Google binary JPEG WebSocket stream, and SQLite persistence. | **NO — Isolated & Guarded** |

---

## 2. What Changes What (Quick Reference)

### A. Bars & Window Manager (`category: "wm"`)
- **Hide Bottom Taskbar (Dock):** `dock_auto_hide` (`true` / `false`) + `dock_hide_on_desktop` (`true` / `false`) + `dock_scale` (`80–120%`). Also toggleable in 1 click from the OS Topbar (`Dock: Auto` / `Dock: Pin`).
- **Hide OS Topbar:** `topbar_auto_hide` (`true` / `false`) — reveals when cursor touches the top 10px sensor strip.
- **Hide Per-Window Titlebars in Tiling:** `hide_win_topbar_in_tiling` (`true` by default; `.node-win.is-tiled > .node-win-bar`).
- **Tiling Geometry:** `wm_mode` (`"tiling"` | `"floating"`), `tile_gap` (`0–24px`), `outer_gap` (`0–24px`), `split_target_aspect` (`0.9–2.2`), `tile_border_width` (`1–4px`).

### B. Appearance & Theme (`category: "appearance"`)
- **Palette & Glass:** `theme` (`"dark"` | `"light"`), `accent_color` (`#RRGGBB` -> auto-computes `--accent`, `--accent-soft`, `--accent-border`, `--accent-glow`), `lite_glass` (disables blur for Haswell CPU speed), `win_radius` (`--win-radius`), `win_opacity` (`75–100%`), `ui_scale` (`--ui-scale`).

### C. Schedule, Focus Guard & Stream (`category: "ai_cron"` & `"focus_stream"`)
- **Cron & Bedtime:** `bedtime_hhmm`, `wake_hhmm`, `sleep_buffer_minutes`, `cron_enabled`, `cron_interval_minutes`, `enable_thinking`.
- **YouTube Focus Guard & Brave Stream:** `fg_enabled`, `fg_hard_limit_minutes`, `fg_long_video_trigger_minutes`, `fg_study_allowance_minutes`, `fg_nudge_on_shorts`, `stream_fps`, `stream_jpeg_quality`.

---

## 3. Sandboxed Preset `custom_js` API (`os_customizer.js`)

Custom JavaScript inside a Preset executes in a `try/catch` sandbox receiving a single `api` object. Every listener, timer, CSS variable, and topbar button registered through `api` is **automatically disposed** when switching presets or clicking **Safe Reset**:

```js
// Set a validated setting or CSS variable
api.setSetting("dock_auto_hide", true);
api.setCssVar("--accent", "#38bdf8");

// Register a global shortcut (auto-cleaned on preset switch)
api.registerShortcut("Alt+C", () => api.openWindow("calendar"), "Open Calendar");

// Add a custom macro pill to the OS Topbar (auto-cleaned on preset switch)
api.addTopbarWidget({
  id: "morning-layout",
  label: "⚡ Study Split",
  title: "Open Tasks + Calendar and equalize tiles",
  onClick: () => {
    api.openWindow("tasks");
    api.openWindow("calendar");
    api.equalizeTiles();
  }
});
```
