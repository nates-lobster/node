"""
Node WebOS Comprehensive Settings, Preset Manager, & AI-Navigable Code Customizer.

Architectural Contract:
- Separates protected core kernels (Auth, SQLite DB, Binary Browser Stream transport)
  from user/AI customizable WebOS surfaces (Settings Schema, CSS Tokens/Selectors,
  and Disposable Sandboxed `api.*` JS Hooks).
- Custom changes (settings patches + custom CSS + custom JS hooks) are validated for
  core safety and persisted into named Presets that can be loaded, inspected, or edited.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .browser_stream import browser_stream_manager
from .config import settings
from .cron_scheduler import cron_scheduler
from .database import engine
from .focus_guard import focus_guard_service
from .ollama_client import ollama_client
from sqlmodel import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Self-Documenting WebOS Settings Schema (Single Source of Truth)
# ---------------------------------------------------------------------------
SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    # --- Category: appearance ---
    {
        "key": "theme",
        "category": "appearance",
        "label": "Color Mode",
        "type": "enum",
        "options": ["dark", "light"],
        "default": "dark",
        "description": "Switches global WebOS surface palette between Dark Slate and Crisp Light.",
    },
    {
        "key": "accent_color",
        "category": "appearance",
        "label": "Accent Color",
        "type": "color",
        "default": "#2bfbae",
        "css_var": "--accent",
        "description": "Primary highlight color for buttons, active window borders, badges, and dock indicators.",
    },
    {
        "key": "canvas_bg",
        "category": "appearance",
        "label": "Desktop Base Color",
        "type": "color",
        "default": "",
        "css_var": "--bg-base",
        "description": "Optional custom hex background for the desktop canvas (leave empty to follow Dark/Light mode).",
    },
    {
        "key": "surface_bg",
        "category": "appearance",
        "label": "Window Surface Color",
        "type": "color",
        "default": "",
        "css_var": "--bg-win",
        "description": "Optional custom hex surface color for OS windows and drawers.",
    },
    {
        "key": "card_bg",
        "category": "appearance",
        "label": "Card & Widget Surface Color",
        "type": "color",
        "default": "",
        "css_var": "--card-bg",
        "description": "Optional custom hex color for desktop widgets and inner cards.",
    },
    {
        "key": "text_color",
        "category": "appearance",
        "label": "Primary Text Color",
        "type": "color",
        "default": "",
        "css_var": "--ink-main",
        "description": "Optional custom hex color for primary interface typography.",
    },
    {
        "key": "font_family",
        "category": "appearance",
        "label": "Interface Font Family",
        "type": "enum",
        "options": ["system", "inter", "geist", "ibm-plex", "serif", "jetbrains-mono"],
        "default": "system",
        "description": "Global UI font stack across desktop widgets, windows, and chat.",
    },
    {
        "key": "lite_glass",
        "category": "appearance",
        "label": "Performance Glass (Disable Blur)",
        "type": "bool",
        "default": False,
        "description": "Disables backdrop-filter blur on bars and modals for zero GPU/compositor overhead.",
    },
    {
        "key": "win_radius",
        "category": "appearance",
        "label": "Window Corner Radius (px)",
        "type": "int",
        "min": 0,
        "max": 24,
        "default": 11,
        "css_var": "--win-radius",
        "description": "Roundness of tiled and floating window corners in pixels (0 = sharp tiling, 16+ = rounded).",
    },
    {
        "key": "win_opacity",
        "category": "appearance",
        "label": "Window Surface Opacity (%)",
        "type": "int",
        "min": 75,
        "max": 100,
        "default": 96,
        "description": "Opacity of desktop window backgrounds so the wallpaper subtly shines through.",
    },
    {
        "key": "ui_scale",
        "category": "appearance",
        "label": "UI Scale (%)",
        "type": "int",
        "min": 85,
        "max": 115,
        "default": 100,
        "css_var": "--ui-scale",
        "description": "Global interface text and spacing density scale.",
    },
    {
        "key": "wallpaper_id",
        "category": "appearance",
        "label": "Desktop Wallpaper",
        "type": "string",
        "default": "aurora-mesh",
        "description": "Active desktop background ID (built-in CSS mesh or scenic wallpaper).",
    },
    {
        "key": "wallpaper_dim",
        "category": "appearance",
        "label": "Wallpaper Dimming (%)",
        "type": "int",
        "min": 15,
        "max": 85,
        "default": 48,
        "description": "Darkness of the contrast veil above the desktop wallpaper.",
    },
    {
        "key": "lock_live_wallpaper",
        "category": "appearance",
        "label": "Lock Screen Live Wallpaper Stream",
        "type": "bool",
        "default": True,
        "description": "Streams crossfading Ken Burns artwork + 30fps aurora mist on the lock screen.",
    },
    # --- Category: wm (Window Manager & Taskbar) ---
    {
        "key": "wm_mode",
        "category": "wm",
        "label": "Window Manager Mode",
        "type": "enum",
        "options": ["tiling", "floating"],
        "default": "tiling",
        "description": "Default layout engine: 'tiling' (equal-spaced / focus-aware split) or 'floating' (free-drag windows).",
    },
    {
        "key": "dock_auto_hide",
        "category": "wm",
        "label": "Auto-Hide Bottom Taskbar (Dock)",
        "type": "bool",
        "default": False,
        "description": "Hides the bottom taskbar/dock automatically until cursor touches the bottom edge of the screen.",
    },
    {
        "key": "dock_hide_on_desktop",
        "category": "wm",
        "label": "Hide Taskbar on Empty Desktop Too",
        "type": "bool",
        "default": True,
        "description": "When Auto-Hide Taskbar is enabled, keeps the dock hidden even when all windows are closed/minimized.",
    },
    {
        "key": "dock_scale",
        "category": "wm",
        "label": "Taskbar / Dock Size (%)",
        "type": "int",
        "min": 80,
        "max": 120,
        "default": 100,
        "css_var": "--dock-scale",
        "description": "Scales the bottom dock icons and container.",
    },
    {
        "key": "dock_peek_ms",
        "category": "wm",
        "label": "Taskbar Peek Duration (ms)",
        "type": "int",
        "min": 500,
        "max": 4000,
        "default": 1600,
        "description": "How long the auto-hidden taskbar stays visible after minimizing a window.",
    },
    {
        "key": "topbar_auto_hide",
        "category": "wm",
        "label": "Auto-Hide Top OS Menubar",
        "type": "bool",
        "default": False,
        "description": "Optionally auto-hides the 36px top OS status bar until cursor hovers the top screen edge.",
    },
    {
        "key": "topbar_hidden_items",
        "category": "wm",
        "label": "Hidden Top Bar Items (CSV)",
        "type": "string",
        "allow_empty": True,
        "default": "",
        "description": "Comma-separated top bar items to hide for a cleaner bar (chat_btn, window_chips, cron_btn, alerts_btn, dock_btn, zen_btn, theme_btn, lock_btn).",
    },
    {
        "key": "dock_hidden_items",
        "category": "wm",
        "label": "Hidden Bottom Dock Icons (CSV)",
        "type": "string",
        "allow_empty": True,
        "default": "",
        "description": "Comma-separated dock app IDs to hide from the bottom taskbar while keeping them in Start Menu (e.g. gallery, extensions, goals, zen).",
    },
    {
        "key": "inbox_categories_csv",
        "category": "ai_cron",
        "label": "Global Inbox Auto-Sort Categories",
        "type": "string",
        "allow_empty": False,
        "default": "Urgent, School, Work, Personal, Finance, Updates, Promo",
        "description": "Global categories the decision model (falling back to the chat model) uses to auto-sort Inbox notifications.",
    },
    {
        "key": "kb_start_menu",
        "category": "wm",
        "label": "Keybind: Start Menu",
        "type": "string",
        "default": "Ctrl+K",
        "description": "Shortcut to open the searchable Start Menu (Alt+Space also works).",
    },
    {
        "key": "kb_toggle_tiling",
        "category": "wm",
        "label": "Keybind: Toggle Tiled / Floating",
        "type": "string",
        "default": "Alt+T",
        "description": "Shortcut to switch between Tiled and Floating window modes.",
    },
    {
        "key": "kb_equalize_tiles",
        "category": "wm",
        "label": "Keybind: Equalize Tiled Panes",
        "type": "string",
        "default": "Alt+E",
        "description": "Shortcut to reset all tiled panes back to equal spacing.",
    },
    {
        "key": "kb_new_chat",
        "category": "wm",
        "label": "Keybind: Open AI Chat",
        "type": "string",
        "default": "Alt+C",
        "description": "Shortcut to open a new AI Chat window.",
    },
    {
        "key": "kb_zen_mode",
        "category": "wm",
        "label": "Keybind: Zen Breather Mode",
        "type": "string",
        "default": "Alt+Z",
        "description": "Shortcut to enter/exit Zen focus breather.",
    },
    {
        "key": "kb_lock_screen",
        "category": "wm",
        "label": "Keybind: Lock Screen",
        "type": "string",
        "default": "Alt+L",
        "description": "Shortcut to lock the OS session immediately.",
    },
    # ---- v10.2: New Tab / start page mode (extension replaces Brave new tab) ----
    {
        "key": "newtab_mode",
        "category": "wm",
        "label": "New Tab Screen Mode",
        "type": "enum",
        "default": "dashboard",
        "options": ["dashboard", "lock", "wallpaper", "custom", "off"],
        "description": "What a new browser tab shows: full dashboard, lock screen wallpaper, bare wallpaper, a custom mini-page, or nothing (extension off).",
    },
    {
        "key": "newtab_show_on_open",
        "category": "wm",
        "label": "Auto-Show New Tab Page",
        "type": "bool",
        "default": True,
        "description": "When on, the extension replaces Brave's new-tab page. When off, the page is still reachable at /newtab.",
    },
    {
        "key": "newtab_custom_blocks_csv",
        "category": "wm",
        "label": "Custom New Tab Blocks (CSV)",
        "type": "string",
        "allow_empty": True,
        "default": "clock,quick_add,urgent_tasks,next_event,inbox_digest",
        "description": "Blocks shown in custom new-tab mode (clock, urgent_tasks, next_event, inbox_digest, quick_add, grades).",
    },
    {
        "key": "newtab_wallpaper_id",
        "category": "wm",
        "label": "New Tab Wallpaper",
        "type": "string",
        "allow_empty": True,
        "default": "",
        "description": "Wallpaper filename for lock/wallpaper new-tab modes. Empty = active desktop wallpaper.",
    },
    {
        "key": "newtab_greeting_name",
        "category": "wm",
        "label": "New Tab Greeting Name",
        "type": "string",
        "allow_empty": True,
        "default": "",
        "description": "Name shown in the new-tab greeting (e.g. 'Good evening, Max').",
    },
    {
        "key": "newtab_overlay_strength",
        "category": "wm",
        "label": "New Tab Wallpaper Overlay (%)",
        "type": "int",
        "min": 0,
        "max": 90,
        "default": 35,
        "description": "Darkening overlay over the wallpaper on the new-tab page so text stays readable.",
    },

    # ---- v10.2: Inbox behaviour ----
    {
        "key": "inbox_mark_read_on_open",
        "category": "ai_cron",
        "label": "Mark Read When Opened",
        "type": "bool",
        "default": True,
        "description": "Gmail-style: opening a message marks it read (with a 3s grace period so a mis-click can be undone).",
    },
    {
        "key": "inbox_auto_fetch_body",
        "category": "ai_cron",
        "label": "Auto-Fetch Message Bodies",
        "type": "bool",
        "default": True,
        "description": "When a message is opened, read its full body from the live tab so you never need the mail client.",
    },
    {
        "key": "inbox_dedupe_window_hours",
        "category": "ai_cron",
        "label": "Duplicate Merge Window (hours)",
        "type": "int",
        "min": 1,
        "max": 720,
        "default": 72,
        "description": "Identical items (same event, same content) arriving within this window are merged into one row with combined tags.",
    },
    {
        "key": "inbox_default_filter",
        "category": "ai_cron",
        "label": "Default Inbox Filter",
        "type": "string",
        "allow_empty": True,
        "default": "is:unread is:important",
        "description": "Gmail-style query applied when the Inbox opens (e.g. 'tag:school is:unread').",
    },
    {
        "key": "inbox_page_size",
        "category": "ai_cron",
        "label": "Inbox Page Size",
        "type": "int",
        "min": 20,
        "max": 300,
        "default": 60,
        "description": "Rows loaded per Inbox page (scrolling loads more).",
    },

    # ---- v10.2: Decision gate (LFM3 d1 / System One) ----
    {
        "key": "decision_gate_enabled",
        "category": "ai_cron",
        "label": "Decision Gate Enabled",
        "type": "bool",
        "default": True,
        "description": "Route every notification tag decision and every chat tool plan through the LFM3 d1 / System One decision model.",
    },
    {
        "key": "decision_gate_prefer_for_tools",
        "category": "ai_cron",
        "label": "Use Decision Gate for Tool Routing",
        "type": "bool",
        "default": True,
        "description": "Attach only the tool schemas relevant to each chat turn instead of all tools at once.",
    },
    {
        "key": "grades_show_scoped_tabs",
        "category": "ai_cron",
        "label": "Show Grades Tabs (Core / Electives)",
        "type": "bool",
        "default": True,
        "description": "Display course-group tabs with per-group averages at the top of Grades.",
    },
    {
        "key": "grades_counts_hidden_in_briefing",
        "category": "ai_cron",
        "label": "Include Hidden Courses in Briefing",
        "type": "bool",
        "default": False,
        "description": "When off, courses you removed from the tracker are also excluded from AI briefings and GPA.",
    },

    {
        "key": "hide_win_topbar_in_tiling",
        "category": "wm",
        "label": "Hide Window Titlebars in Tiling Mode",
        "type": "bool",
        "default": True,
        "description": "Hides per-window close/minimize/maximize bars when tiled and restores them in Floating mode.",
    },
    {
        "key": "tile_gap",
        "category": "wm",
        "label": "Inner Tile Gap (px)",
        "type": "int",
        "min": 0,
        "max": 24,
        "default": 8,
        "description": "Spacing in pixels between adjacent tiled windows.",
    },
    {
        "key": "outer_gap",
        "category": "wm",
        "label": "Outer Screen Margin (px)",
        "type": "int",
        "min": 0,
        "max": 24,
        "default": 8,
        "description": "Outer margin in pixels between tiled windows and viewport edges.",
    },
    {
        "key": "split_target_aspect",
        "category": "wm",
        "label": "Focused Split Aspect Target",
        "type": "float",
        "min": 0.9,
        "max": 2.2,
        "default": 1.38,
        "description": "Ideal width/height aspect ratio used to choose horizontal vs vertical split when a window is manually focused.",
    },
    {
        "key": "tile_border_width",
        "category": "wm",
        "label": "Tiled Window Border Width (px)",
        "type": "int",
        "min": 1,
        "max": 4,
        "default": 2,
        "css_var": "--tile-border-width",
        "description": "Border thickness around tiled panes.",
    },
    # --- Category: ai_cron (AI & Bedtime Schedule) ---
    {
        "key": "enable_thinking",
        "category": "ai_cron",
        "label": "AI Chain-of-Thought Thinking",
        "type": "bool",
        "default": True,
        "description": "Enables qwen3.5:2b reasoning traces before final responses in AI Chat.",
    },
    {
        "key": "cron_enabled",
        "category": "ai_cron",
        "label": "24/7 Background Auto-Sync (Cron)",
        "type": "bool",
        "default": True,
        "description": "Runs continuously 24/7 (day & night) to scan grades, AI-classify Inbox arrivals, and sync Schoology/calendar deadlines.",
    },
    {
        "key": "cron_interval_minutes",
        "category": "ai_cron",
        "label": "Auto-Sync Interval (min)",
        "type": "int",
        "min": 15,
        "max": 240,
        "default": 60,
        "description": "Minutes between 24/7 background auto-sync passes.",
    },
    {
        "key": "bedtime_hhmm",
        "category": "ai_cron",
        "label": "Daily End-of-Day Cutoff Time",
        "type": "time",
        "default": "22:00",
        "description": "Evening cutoff (24h HH:MM) used to calculate remaining free time today for Briefing & YouTube Focus Guard.",
    },
    {
        "key": "wake_hhmm",
        "category": "ai_cron",
        "label": "Morning Day-Start Time",
        "type": "time",
        "default": "07:00",
        "description": "Morning start time (24h HH:MM) for daily time-block planning.",
    },
    {
        "key": "sleep_buffer_minutes",
        "category": "ai_cron",
        "label": "Evening Quiet Buffer (min)",
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 15,
        "description": "Minutes before your End-of-Day Cutoff excluded from heavy task scheduling so your day doesn't pack work up to the final minute.",
    },
    # --- Category: focus_stream (Focus Guard & Server Browser) ---
    {
        "key": "fg_enabled",
        "category": "focus_stream",
        "label": "Social & Media Focus Guard Enabled",
        "type": "bool",
        "default": True,
        "description": "Enables schedule-aware accountability across YouTube, Instagram, TikTok, Facebook, X, Reddit, and custom sites.",
    },
    {
        "key": "fg_hard_limit_minutes",
        "category": "focus_stream",
        "label": "Daily Social Media Hard Cap (min)",
        "type": "int",
        "min": 10,
        "max": 240,
        "default": 45,
        "description": "Total daily social/entertainment minutes after which guarded tabs force-close with no continue choice.",
    },
    {
        "key": "fg_long_video_trigger_minutes",
        "category": "focus_stream",
        "label": "Long Video (>30m) Trigger (min)",
        "type": "int",
        "min": 3,
        "max": 45,
        "default": 10,
        "description": "Minutes watched before triggering an accountability check on videos longer than 30 minutes.",
    },
    {
        "key": "fg_study_allowance_minutes",
        "category": "focus_stream",
        "label": "Approved Reasoning Pass (min)",
        "type": "int",
        "min": 5,
        "max": 90,
        "default": 20,
        "description": "Minutes granted when AI approves concrete coursework/study reasoning.",
    },
    {
        "key": "fg_nudge_on_shorts",
        "category": "focus_stream",
        "label": "Immediate Check on YouTube Shorts",
        "type": "bool",
        "default": True,
        "description": "Triggers accountability immediately when opening YouTube Shorts.",
    },
    {
        "key": "zen_default_seconds",
        "category": "focus_stream",
        "label": "Default Zen Breather (sec)",
        "type": "int",
        "min": 60,
        "max": 3600,
        "default": 60,
        "description": "Default countdown duration when launching Zen Mode from the topbar or dock.",
    },
    {
        "key": "stream_fps",
        "category": "focus_stream",
        "label": "Brave Stream Max FPS",
        "type": "int",
        "min": 4,
        "max": 15,
        "default": 10,
        "description": "Target framerate for server-side Brave zero-Google binary JPEG streaming.",
    },
    {
        "key": "stream_jpeg_quality",
        "category": "focus_stream",
        "label": "Brave Stream JPEG Quality",
        "type": "int",
        "min": 40,
        "max": 85,
        "default": 62,
        "description": "JPEG compression quality (lower = faster over Tailscale/WAN, higher = crisper text).",
    },
]

SCHEMA_BY_KEY: Dict[str, Dict[str, Any]] = {item["key"]: item for item in SETTINGS_SCHEMA}


# ---------------------------------------------------------------------------
# 2. AI-Navigable WebOS Code & Hook Map (Explains What Changes What Safely)
# ---------------------------------------------------------------------------
WEBOS_CODE_MAP: Dict[str, Any] = {
    "overview": (
        "Node WebOS separates Protected Core Kernels (Auth, SQLite DB, Binary WS Transport) "
        "from the Customizable Surface Layer (Settings Schema, CSS Tokens/Selectors, and "
        "Sandboxed Disposable `api.*` JS Hooks in `os_customizer.js`). Custom changes never "
        "mutate core files directly — they compile into self-contained Presets."
    ),
    "files": [
        {
            "path": "backend/app/os_settings.py",
            "role": "Settings Schema, Preset Storage, Code Safety Validator, & AI Settings/Code Compiler.",
            "safe_to_extend": True,
        },
        {
            "path": "backend/app/ui/os_customizer.js",
            "role": "Dedicated Frontend Customization Runtime: applies CSS tokens, scoped `<style>`, and disposable `api.*` JS hooks with automatic teardown & error isolation.",
            "safe_to_extend": True,
        },
        {
            "path": "dashboard/styles.css",
            "role": "Design tokens (`:root` `--*` variables) and component classes (`.os-menubar`, `.os-dock`, `.node-win`).",
            "safe_to_extend": True,
        },
        {
            "path": "backend/app/ui/os_shell.html",
            "role": "Authenticated WebOS markup (Topbar, Tiled/Floating Windows, Settings Studio, Dock).",
            "safe_to_extend": True,
        },
        {
            "path": "backend/app/auth.py & backend/app/browser_stream.py",
            "role": "PROTECTED CORE: Zero-leak PBKDF2-SHA256 session auth & Brave binary JPEG WebSocket pipe. Never altered by presets.",
            "safe_to_extend": False,
        },
    ],
    "css_tokens_and_selectors": [
        {"target": ":root", "tokens": ["--accent", "--accent-soft", "--accent-border", "--accent-glow", "--win-radius", "--tile-border-width", "--dock-scale", "--ui-scale", "--bar-blur"], "changes": "Global colors, corner roundness, border thickness, dock scale, UI density, and blur."},
        {"target": ".os-menubar / .os-menubar-pill", "changes": "Top 36px OS status bar background, pill styling, height, and typography."},
        {"target": ".os-dock-wrap / .os-dock / .os-dock-item", "changes": "Bottom taskbar/dock glass container, icon size, hover lift, and active dots."},
        {"target": ".node-win / .node-win.is-tiled / .node-win.is-focused", "changes": "Desktop windows, tiled pane borders, shadows, and focused window glow."},
        {"target": ".node-win-bar / .node-tile-grip", "changes": "Floating window titlebars and tiled hover drag/close grip pill."},
        {"target": ".studio-card / .studio-subcard / .btn-accent", "changes": "Inner panel cards, inputs, and primary action buttons."},
    ],
    "js_sandbox_api": [
        {"call": "api.setSetting(key, value)", "changes": "Updates any validated WebOS setting key (e.g. api.setSetting('dock_auto_hide', true))."},
        {"call": "api.setCssVar('--name', value)", "changes": "Sets a root CSS custom property (auto-reverted when switching presets)."},
        {"call": "api.registerShortcut('Alt+Key', () => { ... }, 'Label')", "changes": "Binds a global keyboard shortcut (auto-removed when switching presets)."},
        {"call": "api.addTopbarWidget({ id, label, title, onClick })", "changes": "Adds an interactive pill button into the OS topbar (auto-removed on preset switch)."},
        {"call": "api.onWindowOpen((win) => { ... })", "changes": "Runs a callback whenever a window opens (auto-removed on preset switch)."},
        {"call": "api.openWindow(view) / api.closeFocusedWindow()", "changes": "Opens 'tasks', 'calendar', 'inbox', 'secretary', 'apps', 'memory', 'files', 'gallery', 'guard', 'settings'."},
        {"call": "api.toggleTiling() / api.equalizeTiles() / api.toggleDockAutoHide()", "changes": "Controls window manager layout and taskbar auto-hiding."},
        {"call": "api.runCronCheck() / api.startZen(seconds) / api.notify(msg)", "changes": "Triggers schedule cron check, Zen breather, or toast notification."},
    ],
}


# ---------------------------------------------------------------------------
# 3. Built-In Starter Presets
# ---------------------------------------------------------------------------
BUILTIN_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "default-mint",
        "name": "Node Default (Mint Slate)",
        "description": "Balanced #2bfbae mint accent, pinned taskbar, 8px equal-spaced tiling, clean defaults.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "accent_color": "#2bfbae",
            "lite_glass": False,
            "win_radius": 11,
            "win_opacity": 96,
            "ui_scale": 100,
            "dock_auto_hide": False,
            "topbar_auto_hide": False,
            "hide_win_topbar_in_tiling": True,
            "tile_gap": 8,
            "outer_gap": 8,
            "tile_border_width": 2,
        },
        "custom_css": "",
        "custom_js": "",
    },
    {
        "id": "hyprland-max",
        "name": "Hyprland Max Screen (Auto-Hide Dock)",
        "description": "Auto-hides bottom taskbar for full vertical height, crisp 6px gaps, neon focused border + Alt+1/2/3 shortcuts.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "accent_color": "#2bfbae",
            "dock_auto_hide": True,
            "dock_hide_on_desktop": True,
            "topbar_auto_hide": False,
            "hide_win_topbar_in_tiling": True,
            "win_radius": 6,
            "tile_gap": 6,
            "outer_gap": 6,
            "tile_border_width": 2,
        },
        "custom_css": (
            "/* Crisp Hyprland active border glow */\n"
            ".node-win.is-tiled.is-focused {\n"
            "  box-shadow: 0 0 0 1px var(--accent), 0 0 22px rgba(43, 251, 174, 0.18);\n"
            "}\n"
        ),
        "custom_js": (
            "// Quick workspace shortcuts (auto-cleaned on preset switch)\n"
            "api.registerShortcut('Alt+1', () => api.openWindow('tasks'), 'Open Tasks');\n"
            "api.registerShortcut('Alt+2', () => api.openWindow('calendar'), 'Open Calendar');\n"
            "api.registerShortcut('Alt+3', () => api.openWindow('secretary'), 'Open AI Chat');\n"
        ),
    },
    {
        "id": "athlon-perf",
        "name": "Zero-Blur Low-CPU Performance",
        "description": "Disables GPU backdrop blur, sharp 4px corners, 8 FPS low-bandwidth stream for maximum CPU headroom.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "canvas_bg": "",
            "surface_bg": "",
            "card_bg": "",
            "text_color": "",
            "lite_glass": True,
            "win_radius": 4,
            "win_opacity": 100,
            "tile_gap": 4,
            "outer_gap": 4,
            "tile_border_width": 1,
            "stream_fps": 8,
            "stream_jpeg_quality": 55,
        },
        "custom_css": (
            ".node-win { box-shadow: none !important; }\n"
        ),
        "custom_js": "",
    },
    {
        "id": "cyber-cyan",
        "name": "Midnight Cyber Cyan",
        "description": "Sky-cyan #38bdf8 palette, auto-hidden dock, translucent obsidian windows, and a custom Topbar Focus Macro pill.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "accent_color": "#38bdf8",
            "canvas_bg": "#070b14",
            "surface_bg": "#0b1220",
            "card_bg": "#111a2e",
            "text_color": "#f1f5f9",
            "dock_auto_hide": True,
            "win_radius": 14,
            "win_opacity": 90,
            "tile_gap": 10,
            "outer_gap": 10,
        },
        "custom_css": (
            ".os-menubar {\n"
            "  border-bottom: 1px solid rgba(56, 189, 248, 0.28);\n"
            "}\n"
            ".node-win.is-focused {\n"
            "  box-shadow: 0 18px 48px rgba(2, 8, 20, 0.72), 0 0 18px rgba(56, 189, 248, 0.16);\n"
            "}\n"
        ),
        "custom_js": (
            "api.addTopbarWidget({\n"
            "  id: 'focus-macro',\n"
            "  label: '⚡ Work Split',\n"
            "  title: 'Open Tasks + Calendar side-by-side and equalize',\n"
            "  onClick: () => {\n"
            "    api.openWindow('tasks');\n"
            "    api.openWindow('calendar');\n"
            "    api.equalizeTiles();\n"
            "  }\n"
            "});\n"
        ),
    },
    {
        "id": "nordic-frost",
        "name": "Nordic Frost Slate",
        "description": "Cool arctic slate surfaces (#1a202c / #232b3a) with soft ice-blue #88c0d0 highlights and relaxed 12px geometry.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "accent_color": "#88c0d0",
            "canvas_bg": "#1a202c",
            "surface_bg": "#232b3a",
            "card_bg": "#2b3547",
            "text_color": "#eceff4",
            "win_radius": 12,
            "win_opacity": 96,
            "tile_gap": 10,
        },
        "custom_css": "",
        "custom_js": "",
    },
    {
        "id": "warm-espresso",
        "name": "Warm Espresso Studio",
        "description": "Low-blue-light warm charcoal surfaces (#161311 / #201b18) with amber-gold #f59e0b accents for late-evening study.",
        "builtin": True,
        "settings_patch": {
            "theme": "dark",
            "accent_color": "#f59e0b",
            "canvas_bg": "#161311",
            "surface_bg": "#201b18",
            "card_bg": "#2a231f",
            "text_color": "#f5efe9",
            "win_radius": 12,
            "win_opacity": 97,
        },
        "custom_css": "",
        "custom_js": "",
    },
    {
        "id": "crisp-daylight",
        "name": "Crisp Daylight Paper",
        "description": "High-contrast warm paper light mode (#f1f5f9 / #ffffff) with forest-emerald #059669 accents.",
        "builtin": True,
        "settings_patch": {
            "theme": "light",
            "accent_color": "#059669",
            "canvas_bg": "#f1f5f9",
            "surface_bg": "#ffffff",
            "card_bg": "#f8fafc",
            "text_color": "#0f172a",
            "win_radius": 12,
            "win_opacity": 98,
        },
        "custom_css": "",
        "custom_js": "",
    },
]


# ---------------------------------------------------------------------------
# 4. Code Safety Validator (Prevents Breaking Core OS Auth / DB / Transport)
# ---------------------------------------------------------------------------
FORBIDDEN_JS_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"document\.cookie", re.I), "Accessing document.cookie is blocked to protect session isolation."),
    (re.compile(r"/api/v1/auth", re.I), "Touching /api/v1/auth endpoints from custom preset code is blocked."),
    (re.compile(r"X-Node-Session|__nodeGetSessionHeaders|__nodeLockSession", re.I), "Accessing internal session headers is blocked."),
    (re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(", re.I), "Nested eval/Function constructors are disallowed; use `api.*` hooks directly."),
    (re.compile(r"\bXMLHttpRequest\b|\bsendBeacon\b", re.I), "Raw network transports are blocked in custom presets; use `api.*` methods."),
    (re.compile(r"window\.location\s*=|location\.replace\s*\(|location\.href\s*=", re.I), "Navigating away from the WebOS shell is blocked."),
    (re.compile(r"node-os-mount", re.I), "Mutating #node-os-mount root container is blocked to protect core rendering."),
    (re.compile(r"while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;\s*\)", re.I), "Unbounded infinite loops are blocked to prevent UI freezes."),
]

FORBIDDEN_CSS_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"@import\b", re.I), "External @import rules are disallowed in offline/sandboxed WebOS CSS."),
    (re.compile(r"url\s*\(\s*['\"]?https?://", re.I), "External remote URLs in CSS are disallowed; use built-in tokens or data URIs."),
]


def validate_custom_code(custom_css: str = "", custom_js: str = "") -> Tuple[bool, List[str]]:
    errors: List[str] = []
    css_txt = custom_css or ""
    js_txt = custom_js or ""

    for pat, msg in FORBIDDEN_CSS_PATTERNS:
        if pat.search(css_txt):
            errors.append(f"CSS safety guard: {msg}")

    for pat, msg in FORBIDDEN_JS_PATTERNS:
        if pat.search(js_txt):
            errors.append(f"JS safety guard: {msg}")

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# 5. Settings Validation & Service Implementation
# ---------------------------------------------------------------------------
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

NAMED_COLORS: Dict[str, str] = {
    "mint": "#2bfbae",
    "emerald": "#10b981",
    "green": "#22c55e",
    "cyan": "#38bdf8",
    "sky": "#0ea5e9",
    "blue": "#3b82f6",
    "indigo": "#6366f1",
    "purple": "#a855f7",
    "violet": "#8b5cf6",
    "pink": "#ec4899",
    "rose": "#f43f5e",
    "red": "#ef4444",
    "orange": "#f97316",
    "amber": "#f59e0b",
    "gold": "#eab308",
    "yellow": "#facc15",
    "teal": "#14b8a6",
    "white": "#e2e8f0",
}


class WebOSSettingsService:
    def _settings_path(self) -> Path:
        p = Path(settings.os_settings_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _presets_path(self) -> Path:
        p = Path(settings.os_presets_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def default_values(self) -> Dict[str, Any]:
        return {item["key"]: item["default"] for item in SETTINGS_SCHEMA}

    def sanitize_patch(self, raw_patch: Dict[str, Any]) -> Dict[str, Any]:
        clean: Dict[str, Any] = {}
        if not isinstance(raw_patch, dict):
            return clean

        for key, val in raw_patch.items():
            meta = SCHEMA_BY_KEY.get(key)
            if not meta or val is None:
                continue
            t = meta["type"]
            try:
                if t == "bool":
                    if isinstance(val, str):
                        clean[key] = val.strip().lower() in ("1", "true", "yes", "on")
                    else:
                        clean[key] = bool(val)
                elif t == "int":
                    iv = int(round(float(val)))
                    iv = max(int(meta["min"]), min(int(meta["max"]), iv))
                    clean[key] = iv
                elif t == "float":
                    fv = round(float(val), 2)
                    fv = max(float(meta["min"]), min(float(meta["max"]), fv))
                    clean[key] = fv
                elif t == "enum":
                    sv = str(val).strip().lower()
                    if sv in meta["options"]:
                        clean[key] = sv
                elif t == "color":
                    sv = str(val).strip().lower()
                    if sv in ("", "auto", "default") and meta.get("default") == "":
                        clean[key] = ""
                    elif sv in NAMED_COLORS:
                        clean[key] = NAMED_COLORS[sv]
                    elif HEX_COLOR_RE.match(sv):
                        clean[key] = sv
                elif t == "time":
                    sv = str(val).strip()
                    if TIME_HHMM_RE.match(sv):
                        clean[key] = sv
                elif t == "string":
                    sv = str(val).strip()[:600]
                    if sv or meta.get("allow_empty"):
                        clean[key] = sv
            except (ValueError, TypeError):
                continue
        return clean

    def load_state(self) -> Dict[str, Any]:
        values = self.default_values()
        # Pull live cron & focus_guard values so Settings always reflects true backend state
        try:
            c_cfg = cron_scheduler.load_settings()
            values["cron_enabled"] = bool(c_cfg.get("enabled", True))
            values["cron_interval_minutes"] = int(c_cfg.get("interval_minutes", 60))
            values["bedtime_hhmm"] = str(c_cfg.get("bedtime_hhmm", "22:00"))
            values["wake_hhmm"] = str(c_cfg.get("wake_hhmm", "07:00"))
            values["sleep_buffer_minutes"] = int(c_cfg.get("sleep_buffer_minutes", 15))
        except Exception:
            pass

        try:
            fg_cfg = focus_guard_service._load_state().get("settings", {})
            values["fg_enabled"] = bool(fg_cfg.get("enabled", True))
            values["fg_hard_limit_minutes"] = int(fg_cfg.get("hard_limit_minutes", 45))
            values["fg_long_video_trigger_minutes"] = int(fg_cfg.get("long_video_trigger_minutes", 10))
            values["fg_study_allowance_minutes"] = int(fg_cfg.get("study_allowance_minutes", 20))
            values["fg_nudge_on_shorts"] = bool(fg_cfg.get("nudge_on_shorts_immediately", True))
        except Exception:
            pass

        custom_css = ""
        custom_js = ""
        active_preset_id = "default-mint"

        sp = self._settings_path()
        if sp.is_file():
            try:
                stored = json.loads(sp.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    saved_vals = self.sanitize_patch(stored.get("values") or {})
                    values.update(saved_vals)
                    custom_css = str(stored.get("custom_css") or "")
                    custom_js = str(stored.get("custom_js") or "")
                    active_preset_id = str(stored.get("active_preset_id") or "default-mint")
            except Exception as exc:
                logger.warning("Could not parse os_settings.json: %s", exc)

        # Keep stream manager synced
        browser_stream_manager.stream_fps = int(values.get("stream_fps", 10))
        browser_stream_manager.stream_jpeg_quality = int(values.get("stream_jpeg_quality", 62))

        return {
            "values": values,
            "custom_css": custom_css,
            "custom_js": custom_js,
            "active_preset_id": active_preset_id,
        }

    def _sync_linked_services(self, values: Dict[str, Any]) -> None:
        # 1. Sync Hourly AI Cron & Bedtime engine
        try:
            cron_scheduler.save_settings(
                {
                    "enabled": values["cron_enabled"],
                    "interval_minutes": values["cron_interval_minutes"],
                    "bedtime_hhmm": values["bedtime_hhmm"],
                    "wake_hhmm": values["wake_hhmm"],
                    "sleep_buffer_minutes": values["sleep_buffer_minutes"],
                }
            )
        except Exception as exc:
            logger.warning("Cron sync warning: %s", exc)

        # 2. Sync YouTube Focus Guard
        try:
            with Session(engine) as db:
                focus_guard_service.update_settings(
                    {
                        "enabled": values["fg_enabled"],
                        "hard_limit_minutes": values["fg_hard_limit_minutes"],
                        "long_video_trigger_minutes": values["fg_long_video_trigger_minutes"],
                        "study_allowance_minutes": values["fg_study_allowance_minutes"],
                        "nudge_on_shorts_immediately": values["fg_nudge_on_shorts"],
                    },
                    session=db,
                )
        except Exception as exc:
            logger.warning("Focus guard sync warning: %s", exc)

        # 3. Sync Server Brave Browser Streamer
        browser_stream_manager.stream_fps = int(values.get("stream_fps", 10))
        browser_stream_manager.stream_jpeg_quality = int(values.get("stream_jpeg_quality", 62))

    def update_state(
        self,
        settings_patch: Optional[Dict[str, Any]] = None,
        custom_css: Optional[str] = None,
        custom_js: Optional[str] = None,
        active_preset_id: Optional[str] = None,
        replace_defaults: bool = False,
    ) -> Dict[str, Any]:
        cur = self.load_state()
        next_values = self.default_values() if replace_defaults else dict(cur["values"])
        if settings_patch:
            next_values.update(self.sanitize_patch(settings_patch))

        next_css = cur["custom_css"] if custom_css is None else str(custom_css)
        next_js = cur["custom_js"] if custom_js is None else str(custom_js)

        ok, safety_errors = validate_custom_code(next_css, next_js)
        if not ok:
            raise ValueError(" | ".join(safety_errors))

        next_preset = active_preset_id if active_preset_id is not None else cur["active_preset_id"]
        payload = {
            "values": next_values,
            "custom_css": next_css,
            "custom_js": next_js,
            "active_preset_id": next_preset,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._settings_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._sync_linked_services(next_values)
        return self.full_payload()

    # -----------------------------------------------------------------------
    # Preset Storage (Built-in + User/AI Editable Presets)
    # -----------------------------------------------------------------------
    def _load_custom_presets(self) -> List[Dict[str, Any]]:
        pp = self._presets_path()
        if not pp.is_file():
            return []
        try:
            raw = json.loads(pp.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [p for p in raw if isinstance(p, dict) and p.get("id") and p.get("name")]
        except Exception:
            pass
        return []

    def _write_custom_presets(self, items: List[Dict[str, Any]]) -> None:
        self._presets_path().write_text(json.dumps(items, indent=2), encoding="utf-8")

    def list_presets(self) -> List[Dict[str, Any]]:
        custom = self._load_custom_presets()
        return [*BUILTIN_PRESETS, *custom]

    def save_preset(
        self,
        name: str,
        description: str = "",
        settings_patch: Optional[Dict[str, Any]] = None,
        custom_css: str = "",
        custom_js: str = "",
        preset_id: Optional[str] = None,
        activate: bool = True,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        ok, safety_errors = validate_custom_code(custom_css, custom_js)
        if not ok:
            raise ValueError(" | ".join(safety_errors))

        clean_patch = self.sanitize_patch(settings_patch or {})
        custom_list = self._load_custom_presets()

        builtin_ids = {b["id"] for b in BUILTIN_PRESETS}
        pid = (preset_id or "").strip()
        if not pid or pid in builtin_ids:
            slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:36] or "preset"
            pid = f"custom-{slug}-{uuid.uuid4().hex[:6]}"

        now_iso = datetime.now(timezone.utc).isoformat()
        record = {
            "id": pid,
            "name": name.strip()[:120],
            "description": description.strip()[:400],
            "builtin": False,
            "settings_patch": clean_patch,
            "custom_css": custom_css,
            "custom_js": custom_js,
            "updated_at": now_iso,
        }

        replaced = False
        for i, existing in enumerate(custom_list):
            if existing.get("id") == pid:
                custom_list[i] = record
                replaced = True
                break
        if not replaced:
            custom_list.insert(0, record)

        self._write_custom_presets(custom_list)

        if activate:
            full = self.update_state(
                settings_patch=clean_patch,
                custom_css=custom_css,
                custom_js=custom_js,
                active_preset_id=pid,
                replace_defaults=False,
            )
        else:
            full = self.full_payload()
        return record, full

    def load_preset(self, preset_id: str) -> Dict[str, Any]:
        all_p = self.list_presets()
        target = next((p for p in all_p if p["id"] == preset_id), None)
        if not target:
            raise KeyError(f"Preset '{preset_id}' not found")

        # Built-in presets reset cleanly onto defaults + preset patch so switching feels deterministic;
        # Custom presets apply their stored patch + custom CSS/JS cleanly as well.
        return self.update_state(
            settings_patch=target.get("settings_patch") or {},
            custom_css=target.get("custom_css") or "",
            custom_js=target.get("custom_js") or "",
            active_preset_id=target["id"],
            replace_defaults=True,
        )

    def delete_preset(self, preset_id: str) -> Dict[str, Any]:
        if any(b["id"] == preset_id for b in BUILTIN_PRESETS):
            raise ValueError("Built-in starter presets cannot be deleted (duplicate or edit to create a custom preset).")
        custom_list = [p for p in self._load_custom_presets() if p.get("id") != preset_id]
        self._write_custom_presets(custom_list)
        cur = self.load_state()
        if cur["active_preset_id"] == preset_id:
            return self.load_preset("default-mint")
        return self.full_payload()

    def full_payload(self) -> Dict[str, Any]:
        st = self.load_state()
        return {
            "values": st["values"],
            "custom_css": st["custom_css"],
            "custom_js": st["custom_js"],
            "active_preset_id": st["active_preset_id"],
            "presets": self.list_presets(),
            "schema": SETTINGS_SCHEMA,
            "code_map": WEBOS_CODE_MAP,
        }

    # -----------------------------------------------------------------------
    # 6. AI Settings Finder & Direct Safe Code / Preset Generator
    # -----------------------------------------------------------------------
    def _heuristic_compile(self, prompt: str, current_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deterministic natural-language compiler for WebOS settings, CSS tokens,
        and sandboxed JS hooks — guarantees instant, accurate results even when
        local Ollama is offline, and serves as safety fallback.
        """
        low = prompt.lower().strip()
        is_pure_question = (
            any(low.startswith(w) for w in ("where ", "which ", "how do i ", "how can i ", "what setting", "explain "))
            and not any(w in low for w in ("set ", "make ", "enable ", "turn on", "turn off", "change ", "add ", "write ", "create ", "apply "))
        )

        patch: Dict[str, Any] = {}
        css_blocks: List[str] = []
        js_blocks: List[str] = []
        notes: List[str] = []
        target_tab = "appearance"

        # 1. Taskbar / Dock Auto-Hide
        if any(k in low for k in ("taskbar", "dock", "bottom bar")):
            target_tab = "wm"
            if any(k in low for k in ("pin", "always show", "don't hide", "do not hide", "disable auto", "unhide", "show dock", "keep visible")):
                patch["dock_auto_hide"] = False
                notes.append("Pinned the bottom taskbar (`dock_auto_hide = false`) in **Window Manager & Bars**.")
            elif any(k in low for k in ("hide", "auto-hide", "autohide", "auto hide", "collapsible")):
                patch["dock_auto_hide"] = True
                patch["dock_hide_on_desktop"] = True
                notes.append("Enabled **Auto-Hide Bottom Taskbar (Dock)** (`dock_auto_hide = true`) in **Window Manager & Bars** (hover bottom edge to reveal, or toggle `Dock: Auto` in the OS topbar).")
            else:
                notes.append("Taskbar visibility is controlled in **Settings → Window Manager & Bars → Auto-Hide Bottom Taskbar (Dock)** (`dock_auto_hide`), or via the `Dock: Auto / Pin` button in the OS topbar.")

        # 2. Top OS Menubar vs Window Topbars
        if any(k in low for k in ("top bar", "topbar", "menubar", "menu bar", "status bar", "titlebar", "title bar")):
            target_tab = "wm"
            if "window" in low or "title" in low:
                want_hide = not any(x in low for x in ("show", "restore", "keep", "visible", "enable"))
                patch["hide_win_topbar_in_tiling"] = want_hide
                notes.append(f"Set **Hide Window Titlebars in Tiling Mode** (`hide_win_topbar_in_tiling = {str(want_hide).lower()}`).")
            elif any(x in low for x in ("hide", "auto-hide", "autohide", "auto hide")):
                patch["topbar_auto_hide"] = True
                notes.append("Enabled **Auto-Hide Top OS Menubar** (`topbar_auto_hide = true`) — hover top screen edge to reveal.")
            elif any(x in low for x in ("show", "pin", "always")):
                patch["topbar_auto_hide"] = False
                notes.append("Pinned the **Top OS Menubar** (`topbar_auto_hide = false`).")

        # 3. Tiling / Gaps / Corners / Borders
        if any(k in low for k in ("gap", "spacing", "margin", "padding")):
            target_tab = "wm"
            m_px = re.search(r"(\d{1,2})\s*(?:px|pixel)?", low)
            val = int(m_px.group(1)) if m_px else (0 if "zero" in low or "no gap" in low else (14 if "wide" in low or "large" in low or "increase" in low else 4))
            patch["tile_gap"] = max(0, min(24, val))
            patch["outer_gap"] = max(0, min(24, val))
            notes.append(f"Adjusted **Inner Tile Gap** & **Outer Screen Margin** to `{patch['tile_gap']}px` in **Window Manager & Bars**.")

        if any(k in low for k in ("corner", "radius", "rounded", "sharp", "square")):
            target_tab = "appearance"
            if "sharp" in low or "square" in low or "0px" in low:
                patch["win_radius"] = 0
            else:
                m_r = re.search(r"(\d{1,2})\s*px", low)
                patch["win_radius"] = int(m_r.group(1)) if m_r else (18 if "very" in low or "more" in low else 12)
            notes.append(f"Updated **Window Corner Radius** (`--win-radius`) to `{patch['win_radius']}px`.")

        if "floating" in low and ("mode" in low or "default" in low or "switch" in low):
            target_tab = "wm"
            patch["wm_mode"] = "floating"
            notes.append("Switched **Window Manager Mode** to `floating`.")
        elif "tiling" in low and ("mode" in low or "default" in low or "switch" in low):
            target_tab = "wm"
            patch["wm_mode"] = "tiling"
            notes.append("Switched **Window Manager Mode** to `tiling`.")

        # 4. Theme & Accent Colors
        if "light mode" in low or "light theme" in low:
            target_tab = "appearance"
            patch["theme"] = "light"
            notes.append("Switched **Color Mode** to `light`.")
        elif "dark mode" in low or "dark theme" in low:
            target_tab = "appearance"
            patch["theme"] = "dark"
            notes.append("Switched **Color Mode** to `dark`.")

        hex_match = re.search(r"(#[0-9a-fA-F]{6})\b", prompt)
        if hex_match:
            target_tab = "appearance"
            patch["accent_color"] = hex_match.group(1).lower()
            notes.append(f"Set **Accent Color** (`--accent`) to `{patch['accent_color']}`.")
        else:
            for cname, chex in NAMED_COLORS.items():
                if re.search(rf"\b{cname}\b", low):
                    target_tab = "appearance"
                    patch["accent_color"] = chex
                    notes.append(f"Set **Accent Color** (`--accent`) to **{cname}** (`{chex}`).")
                    break

        if any(k in low for k in ("blur", "performance", "fast", "low cpu", "lite glass")):
            target_tab = "appearance"
            if any(k in low for k in ("no blur", "disable blur", "performance", "fast", "low cpu")):
                patch["lite_glass"] = True
                notes.append("Enabled **Performance Glass** (`lite_glass = true`, disables GPU blur).")
            elif "enable blur" in low or "more blur" in low:
                patch["lite_glass"] = False
                notes.append("Enabled frosted glass blur (`lite_glass = false`).")

        # 5. Bedtime / Cron / Focus Guard / Stream
        if any(k in low for k in ("bedtime", "sleep", "cron")):
            target_tab = "ai_cron"
            tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
            if tm and ("bed" in low or "sleep" in low):
                hh = int(tm.group(1))
                mm = int(tm.group(2) or 0)
                ampm = tm.group(3)
                if ampm == "pm" and hh < 12:
                    hh += 12
                elif ampm == "am" and hh == 12:
                    hh = 0
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    patch["bedtime_hhmm"] = f"{hh:02d}:{mm:02d}"
                    notes.append(f"Updated **Scheduled Bedtime** to `{patch['bedtime_hhmm']}` in **AI & Schedule**.")
            elif not notes:
                notes.append("Bedtime and hourly cron intervals are in **Settings → AI & Schedule** (`bedtime_hhmm`, `cron_interval_minutes`).")

        if any(k in low for k in ("youtube", "focus guard", "video limit", "hard cap")):
            target_tab = "focus_stream"
            m_min = re.search(r"(\d{1,3})\s*(?:m|min|minutes)", low)
            if m_min:
                patch["fg_hard_limit_minutes"] = max(10, min(240, int(m_min.group(1))))
                notes.append(f"Updated **Daily YouTube Hard Cap** to `{patch['fg_hard_limit_minutes']}m` in **Focus & Browser**.")
            elif not notes:
                notes.append("YouTube limits and video triggers live in **Settings → Focus & Browser**.")

        # 6. Custom CSS Generation Requests (glow, neon, transparent/glass, compact, monospace/terminal, etc.)
        if any(k in low for k in ("glow", "neon", "cyber")):
            target_tab = "presets"
            css_blocks.append(
                "/* Custom Focused Window Neon Glow */\n"
                ".node-win.is-focused {\n"
                "  border-color: var(--accent) !important;\n"
                "  box-shadow: 0 0 0 1px var(--accent), 0 0 26px var(--accent-glow), 0 18px 45px rgba(0, 0, 0, 0.65) !important;\n"
                "}"
            )
            notes.append("Added custom CSS neon glow on `.node-win.is-focused`.")

        if any(k in low for k in ("translucent", "glassmorphic", "transparent", "frosted")):
            target_tab = "presets"
            patch["win_opacity"] = 86
            css_blocks.append(
                "/* Translucent Frosted Window Surfaces */\n"
                ".node-win {\n"
                "  backdrop-filter: blur(16px) saturate(140%);\n"
                "  -webkit-backdrop-filter: blur(16px) saturate(140%);\n"
                "}"
            )
            notes.append("Added frosted glass backdrop-filter CSS on `.node-win` and set window opacity to `86%`.")

        if any(k in low for k in ("monospace", "terminal", "hacker", "retro")):
            target_tab = "presets"
            css_blocks.append(
                "/* Monospace Terminal Typography Override */\n"
                "body, .node-win, .os-menubar, .os-dock {\n"
                "  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;\n"
                "}"
            )
            notes.append("Applied monospace terminal typography across WebOS surfaces via Custom CSS.")

        # 7. Custom Sandboxed JS Hook Generation (shortcuts & topbar buttons)
        sc_match = re.search(r"(alt\+[a-z0-9])", low)
        target_win_map = {
            "calendar": "calendar",
            "task": "tasks",
            "goal": "goals",
            "inbox": "inbox",
            "monitor": "apps",
            "browser": "apps",
            "note": "memory",
            "file": "files",
            "guard": "guard",
            "setting": "settings",
            "chat": "secretary",
        }
        matched_view = next((v for k, v in target_win_map.items() if k in low), None)

        if ("shortcut" in low or "hotkey" in low or "keybind" in low or sc_match) and matched_view:
            target_tab = "presets"
            combo = sc_match.group(1).title() if sc_match else "Alt+K"
            js_blocks.append(
                f"// Custom keyboard shortcut registered via safe Node Customizer API\n"
                f"api.registerShortcut('{combo}', () => api.openWindow('{matched_view}'), 'Open {matched_view.title()}');"
            )
            notes.append(f"Generated sandboxed JS hook `api.registerShortcut('{combo}', ...)` to open `{matched_view}`.")

        if ("topbar" in low or "top bar" in low or "button" in low or "widget" in low) and any(
            w in low for w in ("add ", "create ", "button", "widget", "macro")
        ):
            target_tab = "presets"
            if "equalize" in low or "cron" in low or "schedule" in low:
                js_blocks.append(
                    "api.addTopbarWidget({\n"
                    "  id: 'custom-sync-equalize',\n"
                    "  label: '⚡ Sync & Tile',\n"
                    "  title: 'Equalize tiled windows and run AI schedule check',\n"
                    "  onClick: () => { api.equalizeTiles(); api.runCronCheck(); }\n"
                    "});"
                )
                notes.append("Added custom Topbar widget `⚡ Sync & Tile` via `api.addTopbarWidget(...)`.")
            elif matched_view:
                js_blocks.append(
                    f"api.addTopbarWidget({{\n"
                    f"  id: 'quick-{matched_view}',\n"
                    f"  label: '★ {matched_view.title()}',\n"
                    f"  title: 'Quick open {matched_view.title()}',\n"
                    f"  onClick: () => api.openWindow('{matched_view}')\n"
                    f"}});"
                )
                notes.append(f"Added custom Topbar button `★ {matched_view.title()}` via `api.addTopbarWidget(...)`.")

        if is_pure_question and not css_blocks and not js_blocks:
            return {
                "is_question_only": True,
                "target_tab": target_tab,
                "explanation": " ".join(notes)
                if notes
                else (
                    "Use **Appearance** for theme/accent/radius/blur, **Window Manager & Bars** for hiding the taskbar (`dock_auto_hide`) "
                    "or tile gaps, **AI & Schedule** for bedtime & hourly cron, **Focus & Browser** for YouTube Guard & Brave FPS, "
                    "or **Presets & Code** to edit custom CSS/JS."
                ),
                "settings_patch": {},
                "custom_css": "",
                "custom_js": "",
                "suggested_preset_name": "",
            }

        # Build concise preset name
        words = [w.capitalize() for w in re.findall(r"[a-zA-Z0-9]+", prompt) if w.lower() not in ("make", "the", "and", "to", "a", "in", "set", "change", "please", "add", "with")][:4]
        preset_title = " ".join(words) if words else "AI Custom Workspace"

        return {
            "is_question_only": False,
            "target_tab": target_tab,
            "explanation": " ".join(notes) if notes else "Prepared WebOS configuration update.",
            "settings_patch": patch,
            "custom_css": "\n\n".join(css_blocks),
            "custom_js": "\n\n".join(js_blocks),
            "suggested_preset_name": f"AI: {preset_title}"[:60],
        }

    async def ai_assist(
        self,
        prompt: str,
        save_as_preset: bool = True,
        preset_name: Optional[str] = None,
        apply_immediately: bool = True,
    ) -> Dict[str, Any]:
        cur = self.load_state()
        fallback = self._heuristic_compile(prompt, cur)

        result_data = dict(fallback)
        # Attempt live local qwen3.5:2b enrichment when Ollama is reachable
        try:
            health = await ollama_client.check_health()
            if health.get("reachable"):
                compact_keys = [
                    f"{s['key']} ({s['type']}, default={s['default']}): {s['description']}"
                    for s in SETTINGS_SCHEMA
                ]
                sys_prompt = (
                    "You are the Node WebOS Settings & Safe Code Architect.\n"
                    "Return STRICT JSON ONLY with keys:\n"
                    '{"is_question_only": bool, "target_tab": "appearance|wm|ai_cron|focus_stream|presets|system", '
                    '"explanation": "concise markdown answer", "settings_patch": {}, "custom_css": "", "custom_js": "", "suggested_preset_name": ""}\n\n'
                    "SETTINGS KEYS:\n" + "\n".join(compact_keys) + "\n\n"
                    "SAFE CSS SELECTORS: :root (--accent, --win-radius, --tile-border-width, --dock-scale, --ui-scale), "
                    ".os-menubar, .os-dock, .node-win, .node-win.is-tiled, .node-win.is-focused, .studio-card.\n"
                    "SAFE JS `api` METHODS ONLY (never touch document.cookie, /api/v1/auth, or window.location):\n"
                    "- api.setSetting(key, val)\n"
                    "- api.setCssVar('--var', val)\n"
                    "- api.registerShortcut('Alt+Key', () => { ... }, 'Label')\n"
                    "- api.addTopbarWidget({ id: '...', label: '...', title: '...', onClick: () => { ... } })\n"
                    "- api.openWindow('tasks'|'calendar'|'inbox'|'secretary'|'apps'|'memory'|'files'|'gallery'|'guard'|'settings')\n"
                    "- api.equalizeTiles() / api.toggleTiling() / api.toggleDockAutoHide() / api.runCronCheck() / api.notify(msg)\n"
                )
                llm_res = await ollama_client.chat_json(
                    system_prompt=sys_prompt,
                    user_prompt=f"Current settings: {json.dumps(cur['values'])}\n\nUser request: {prompt}",
                    think=False,
                    max_tokens=900,
                )
                if isinstance(llm_res, dict) and ("explanation" in llm_res or "settings_patch" in llm_res):
                    cand_css = str(llm_res.get("custom_css") or "").strip()
                    cand_js = str(llm_res.get("custom_js") or "").strip()
                    ok, _errs = validate_custom_code(cand_css, cand_js)
                    if ok:
                        cand_patch = self.sanitize_patch(llm_res.get("settings_patch") or {})
                        # Merge with deterministic patch if LLM missed an obvious key
                        merged_patch = {**fallback["settings_patch"], **cand_patch}
                        result_data = {
                            "is_question_only": bool(llm_res.get("is_question_only", False)) and not merged_patch and not cand_css and not cand_js,
                            "target_tab": str(llm_res.get("target_tab") or fallback["target_tab"]),
                            "explanation": str(llm_res.get("explanation") or fallback["explanation"]),
                            "settings_patch": merged_patch,
                            "custom_css": cand_css or fallback["custom_css"],
                            "custom_js": cand_js or fallback["custom_js"],
                            "suggested_preset_name": str(llm_res.get("suggested_preset_name") or fallback["suggested_preset_name"]),
                        }
        except Exception as exc:
            logger.debug("Settings AI assist used deterministic compiler: %s", exc)

        has_changes = bool(
            result_data["settings_patch"]
            or result_data["custom_css"].strip()
            or result_data["custom_js"].strip()
        )

        saved_preset: Optional[Dict[str, Any]] = None
        if has_changes and not result_data["is_question_only"]:
            merged_css = result_data["custom_css"].strip() or cur["custom_css"]
            merged_js = result_data["custom_js"].strip() or cur["custom_js"]
            if save_as_preset:
                final_name = (preset_name or result_data["suggested_preset_name"] or "AI Custom Preset").strip()
                saved_preset, full = self.save_preset(
                    name=final_name,
                    description=f"Generated from: {prompt[:160]}",
                    settings_patch={**cur["values"], **result_data["settings_patch"]},
                    custom_css=merged_css,
                    custom_js=merged_js,
                    activate=apply_immediately,
                )
            elif apply_immediately:
                full = self.update_state(
                    settings_patch=result_data["settings_patch"],
                    custom_css=merged_css,
                    custom_js=merged_js,
                )
            else:
                full = self.full_payload()
        else:
            full = self.full_payload()

        return {
            "assist": {
                **result_data,
                "has_changes": has_changes,
                "saved_preset": saved_preset,
            },
            "state": full,
        }


os_settings_service = WebOSSettingsService()
