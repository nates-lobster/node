/**
 * Node v6.0 — Authenticated Desktop OS Controller
 * - Hyprland-style dwindle auto-tiling WM (topbar hidden in tiling, shown in floating)
 * - Mouse splitters, corner resize, tile drag-swap, and smooth tiling <-> floating transition
 * - Named AI Chat Contexts (editable names, plug any window into any context, prompt stacking)
 * - Gmail / ProtonMail-style Inbox (select all, presets, bulk read/unread/task/delete)
 * - Server-side persistent Chromium streaming (<canvas> WebSocket, instant frame reveal)
 * - Clean Zen Mode & Server-Validated Lock Screen integration
 */

const streamSockets = new Map();
let splitSeq = 1;

const MINIMAL_ICONS = {
  "node-logo": '<line x1="12" y1="12" x2="12" y2="3.8"/><line x1="12" y1="12" x2="19.4" y2="8.6"/><line x1="12" y1="12" x2="17.8" y2="18.2"/><line x1="12" y1="12" x2="6.2" y2="18.2"/><line x1="12" y1="12" x2="4.6" y2="8.6"/><circle cx="12" cy="12" r="3.1" fill="currentColor"/><circle cx="12" cy="3.8" r="1.75" fill="currentColor"/><circle cx="19.4" cy="8.6" r="1.65" fill="currentColor"/><circle cx="17.8" cy="18.2" r="1.65" fill="currentColor"/><circle cx="6.2" cy="18.2" r="1.65" fill="currentColor"/><circle cx="4.6" cy="8.6" r="1.65" fill="currentColor"/>',
  desk: '<rect x="3" y="4" width="18" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/>',
  secretary: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.5A8 8 0 1 1 21 12z"/><path d="M12 8v4m-2-2h4"/>',
  chat: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.5A8 8 0 1 1 21 12z"/><path d="M12 8v4m-2-2h4"/>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
  tasks: '<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/><polyline points="8.5 12.2 11 14.7 15.8 9.5"/>',
  grades: '<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12.5v4.5c0 1.5 2.7 3 6 3s6-1.5 6-3v-4.5"/><line x1="22" y1="10" x2="22" y2="16"/>',
  goals: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/>',
  apps: '<rect x="2.5" y="4" width="19" height="16" rx="2.2"/><line x1="2.5" y1="8.5" x2="21.5" y2="8.5"/><circle cx="6" cy="6.3" r="0.8" fill="currentColor"/><circle cx="9" cy="6.3" r="0.8" fill="currentColor"/>',
  stream: '<rect x="2.5" y="4" width="19" height="16" rx="2.2"/><line x1="2.5" y1="8.5" x2="21.5" y2="8.5"/><circle cx="12" cy="14" r="2.5"/>',
  memory: '<path d="M5 3.5h11l4 4V20a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 20V5A1.5 1.5 0 0 1 5.5 3.5z"/><polyline points="15.5 3.5 15.5 8 20 8"/><line x1="8" y1="12.5" x2="16" y2="12.5"/><line x1="8" y1="16.5" x2="13.5" y2="16.5"/>',
  files: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  gallery: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
  guard: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  zen: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.08a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.08a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  star: '<polygon points="12 2.5 15.09 8.76 22 9.77 17 14.64 18.18 21.52 12 18.27 5.82 21.52 7 14.64 2 9.77 8.91 8.76 12 2.5"/>',
  "star-filled": '<polygon points="12 2.5 15.09 8.76 22 9.77 17 14.64 18.18 21.52 12 18.27 5.82 21.52 7 14.64 2 9.77 8.91 8.76 12 2.5" fill="currentColor"/>',
  mute: '<circle cx="12" cy="12" r="9"/><line x1="5.7" y1="5.7" x2="18.3" y2="18.3"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/>',
  mail: '<rect x="2.5" y="4.5" width="19" height="15" rx="2"/><polyline points="21.5 6 12 13 2.5 6"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  "user-plus": '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="17" y1="11" x2="23" y2="11"/>',
  clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
  spark: '<path d="M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3z"/>',
  dock: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="15.5" x2="21" y2="15.5"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
  palette: '<circle cx="12" cy="12" r="9"/><circle cx="8.5" cy="10.5" r="1" fill="currentColor"/><circle cx="12" cy="7.5" r="1" fill="currentColor"/><circle cx="15.5" cy="10.5" r="1" fill="currentColor"/>',
  grid: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
  layers: '<rect x="3" y="6" width="13" height="13" rx="2"/><path d="M8 6V4.5A1.5 1.5 0 0 1 9.5 3h10A1.5 1.5 0 0 1 21 4.5v10a1.5 1.5 0 0 1-1.5 1.5H18"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><line x1="9" y1="2" x2="9" y2="5"/><line x1="15" y1="2" x2="15" y2="5"/><line x1="9" y1="19" x2="9" y2="22"/><line x1="15" y1="19" x2="15" y2="22"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
  search: '<circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.2" y2="16.2"/>',
  "arrow-up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="6 11 12 5 18 11"/>',
  "arrow-down": '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="6 13 12 19 18 13"/>',
  grip: '<circle cx="9" cy="6" r="1" fill="currentColor"/><circle cx="15" cy="6" r="1" fill="currentColor"/><circle cx="9" cy="12" r="1" fill="currentColor"/><circle cx="15" cy="12" r="1" fill="currentColor"/><circle cx="9" cy="18" r="1" fill="currentColor"/><circle cx="15" cy="18" r="1" fill="currentColor"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  bolt: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  puzzle: '<path d="M19.4 13a1.6 1.6 0 0 0 0 3.2h.6v2.8a2 2 0 0 1-2 2h-2.8v-.6a1.6 1.6 0 0 0-3.2 0v.6H9a2 2 0 0 1-2-2v-3h.6a1.6 1.6 0 0 0 0-3.2H7V9a2 2 0 0 1 2-2h3v-.6a1.6 1.6 0 0 1 3.2 0V7H18a2 2 0 0 1 2 2v4h-.6z"/>',
  extensions: '<path d="M19.4 13a1.6 1.6 0 0 0 0 3.2h.6v2.8a2 2 0 0 1-2 2h-2.8v-.6a1.6 1.6 0 0 0-3.2 0v.6H9a2 2 0 0 1-2-2v-3h.6a1.6 1.6 0 0 0 0-3.2H7V9a2 2 0 0 1 2-2h3v-.6a1.6 1.6 0 0 1 3.2 0V7H18a2 2 0 0 1 2 2v4h-.6z"/>',
  globe: '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14.5 14.5 0 0 0 0 18 14.5 14.5 0 0 0 0-18"/>',
  resize: '<polyline points="16 20 20 20 20 16"/><line x1="14" y1="14" x2="20" y2="20"/><line x1="10" y1="20" x2="20" y2="10"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
  "chevron-right": '<polyline points="9 18 15 12 9 6"/>',
};

function renderMinimalSvg(name, size = 18, extraClass = "") {
  const inner = MINIMAL_ICONS[name] || MINIMAL_ICONS.apps;
  const cls = extraClass ? `node-svg-icon ${extraClass}` : "node-svg-icon";
  return `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

function safeStorageGet(key, fallback = null) {
  try {
    const val = window.localStorage.getItem(key);
    return val !== null ? val : fallback;
  } catch (_e) {
    return fallback;
  }
}

function safeStorageSet(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (_e) {}
}

document.addEventListener("alpine:init", () => {
  if (window.marked && typeof window.marked.setOptions === "function") {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  Alpine.data("webosStudio", () => ({
    currentUsername: "admin",

    // Preferences & Comprehensive WebOS Settings (zero credentials ever stored client-side!)
    theme: "dark",
    liteGlass: false,
    dockAutoHide: false,
    dockHovered: false,
    topbarHovered: false,
    dockPeekUntil: 0,
    enableThinking: true,
    wmMode: "tiling",

    osSettings: {
      theme: "dark",
      accent_color: "#2bfbae",
      lite_glass: false,
      win_radius: 11,
      win_opacity: 96,
      ui_scale: 100,
      wallpaper_id: "aurora-mesh",
      wallpaper_dim: 48,
      lock_live_wallpaper: true,
      wm_mode: "tiling",
      dock_auto_hide: false,
      dock_hide_on_desktop: true,
      dock_scale: 100,
      dock_peek_ms: 1600,
      topbar_auto_hide: false,
      hide_win_topbar_in_tiling: true,
      tile_gap: 8,
      outer_gap: 8,
      split_target_aspect: 1.38,
      tile_border_width: 2,
      enable_thinking: true,
      cron_enabled: true,
      cron_interval_minutes: 60,
      bedtime_hhmm: "22:00",
      wake_hhmm: "07:00",
      sleep_buffer_minutes: 15,
      fg_enabled: true,
      fg_hard_limit_minutes: 45,
      fg_long_video_trigger_minutes: 10,
      fg_study_allowance_minutes: 20,
      fg_nudge_on_shorts: true,
      zen_default_seconds: 60,
      stream_fps: 10,
      stream_jpeg_quality: 62,
      topbar_hidden_items: "",
      dock_hidden_items: "",
      inbox_categories_csv: "Urgent, School, Work, Personal, Finance, Updates, Promo",
      kb_start_menu: "Ctrl+K",
      kb_toggle_tiling: "Alt+T",
      kb_equalize_tiles: "Alt+E",
      kb_new_chat: "Alt+C",
      kb_zen_mode: "Alt+Z",
      kb_lock_screen: "Alt+L",
    },
    osPresets: [],
    activePresetId: "default-mint",
    osCodeMap: {},
    showCodeMap: false,
    settingsTab: "appearance",
    settingsAiPrompt: "",
    settingsAiLoading: false,
    settingsAiSavePreset: true,
    settingsAiResult: null,
    presetEditor: {
      id: "default-mint",
      builtin: true,
      name: "My Custom Workspace",
      description: "",
      custom_css: "",
      custom_js: "",
    },
    customJsError: "",
    customTopbarWidgets: [],
    customShortcuts: [],
    _settingsSaveTimer: null,

    // Wallpapers & Zen Mode
    wallpapers: [
      { id: "aurora-mesh", title: "Node Mint Mesh (Zero-RAM CSS)", kind: "css", credit: "Built-in GPU gradient" },
      { id: "minimal-slate", title: "Pure Obsidian Slate (Zero-RAM CSS)", kind: "css", credit: "Built-in minimal dark" },
    ],
    activeWallpaperId: "aurora-mesh",
    wallpaperDim: 48,
    zenMode: false,
    zenSecondsLeft: 60,
    zenInitialSeconds: 60,
    zenUntimed: false,
    zenInterval: null,

    // Clock
    clockTime: "--:--",
    clockSeconds: "00",
    clockAmPm: "",
    clockDate: "Loading…",
    topBarClock: "--:--",
    todayIso: new Date().toISOString().slice(0, 10),

    // Window Manager + Equal-Spaced / Focus-Aware Multi-Way Split Tree
    windows: [],
    focusedWinId: null,
    manualTileFocusId: null,
    hasCustomTileTree: false,
    tiledOrder: [],
    nextZ: 120,
    chatWinCounter: 1,
    tileTree: null,
    tileRects: {},
    tileSplitters: [],
    draggingSplitId: null,
    swapTargetWinId: null,
    pullToFloatActive: false,
    isPointerInteracting: false,

    // Toast
    toastActionLabel: "",
    toastAction: null,
    toastMessage: "",
    toastType: "ok",
    toastTimer: null,

    // System status
    system: {
      status: "ok",
      unread_notifications: 0,
      active_tasks: 0,
      upcoming_events: 0,
      active_goals: 0,
      scraper_runtime: { live_warm_sessions: 0, detail: "Checking server browser…" },
      tailscale: { connected: false, ip: null, hostname: null, daemon_state: "Checking", detail: "" },
      ollama: { reachable: false, configured_model: "qwen3.5:2b", model_ready: false, available_models: [], detail: "" },
    },

    // Core data
    monitoredApps: [],
    notifications: [],
    inboxTab: "all", // 'all' | 'important' | 'unread'
    selectedAppFilter: "All",
    unreadOnlyFilter: false,
    inboxSearch: "",
    selectedNotificationIds: [],
    expandedNotificationIds: {},
    enrichingNotifIds: {},
    importanceStats: { trusted_senders: [], muted_senders: [], total_feedback_events: 0 },

    // PowerSchool / SIS Grades Tracker
    gradesData: {
      courses: [],
      recent_changes: [],
      summary: {
        total_courses: 0,
        average_percent: null,
        unweighted_gpa: null,
        total_assignments: 0,
        total_missing: 0,
        last_checked_at: null,
      },
    },
    gradesScanning: false,
    expandedCourseKeys: {},
    courseAssignmentFilter: {},

    // Multi-Account Duplicate Modal
    duplicateModalOpen: false,
    duplicateSourceApp: null,
    duplicateAccountLabel: "",
    duplicateAccountProfile: "",

    // Start Menu & Multi-Model Router Status
    startMenuOpen: false,
    startMenuQuery: "",
    startMenuSelectedIdx: 0,
    modelsStatus: {
      primary_chat: { model: "qwen3.5:2b", ready: false },
      fast_router: { model: "qwen3.5:0.8b", ready: false, active_model: "qwen3.5:2b" },
      von_router: { configured: false, reachable: false, model: "von-1.3.7" },
    },
    inboxAiProcessing: false,

    tasks: [],
    taskFilter: "focus",
    newTask: { title: "", due_date: "", category: "General", priority: "normal", estimated_minutes: "" },
    draggingTaskId: null,
    dragOverTaskId: null,
    taskSyncingSources: false,
    taskAiModalOpen: false,
    taskAiRawText: "",
    taskAiExtracting: false,

    // Calendar
    calendarMode: "month",
    calendarCursor: new Date().toISOString().slice(0, 10),
    calendarRangeStart: "",
    calendarRangeEnd: "",
    calendarEvents: [],
    calendarFeeds: [],
    calendarFreeWindows: [],
    calendarCoordinating: false,
    calSyncModalOpen: false,
    eventModalOpen: false,
    eventEditingId: null,
    eventForm: {
      title: "",
      date: new Date().toISOString().slice(0, 10),
      all_day: false,
      start_time: "15:00",
      end_time: "16:00",
      category: "Event",
      color: "sky",
      location: "",
      notes: "",
      repeat_enabled: false,
      repeat_days: [],
      repeat_weeks: 4,
    },
    newFeed: { name: "", url: "", color: "violet" },
    feedSubmitting: false,
    icsImportText: "",
    icsImportSourceName: "Imported .ics",

    // Goals
    goals: [],
    goalModalOpen: false,
    goalForm: {
      title: "",
      category: "General",
      target_date: "",
      minutes_per_day: 25,
      preferred_slot: "afternoon",
      daily_habit: "",
      milestones: [],
    },

    // Hourly AI Cron & Bedtime Priority State
    cronState: {
      settings: {
        enabled: true,
        interval_minutes: 60,
        bedtime_hhmm: "22:00",
        wake_hhmm: "07:00",
        sleep_buffer_minutes: 15,
      },
      budget: {
        bedtime_hhmm: "22:00",
        bedtime_label: "10:00 PM",
        free_minutes_until_bedtime: 0,
        due_tonight_minutes: 0,
        youtube_slack_minutes: 0,
        youtube_allowed: true,
        on_schedule_status: "on_track",
      },
      last_run: null,
      history: [],
    },
    cronRunning: false,

    // YouTube Focus Guard (v7.0 Shorts + Full Videos + AI Justification)
    focusGuard: {
      enabled: true,
      earned_break_limit_minutes: 20,
      hard_limit_minutes: 45,
      long_video_threshold_minutes: 30,
      long_video_trigger_minutes: 10,
      study_allowance_minutes: 20,
      quick_break_minutes: 10,
      nudge_on_shorts_immediately: true,
      session: {
        active: false,
        should_nudge: false,
        force_close: false,
        prompt_mode: "none",
        video_title: "",
        channel: "",
        session_seconds: 0,
        ai_video_analysis: null,
      },
      workload: { urgent_tasks_count: 0, unchecked_goals_count: 0, top_items: [], all_urgent_clear: true },
      total_youtube_minutes_today: 0,
    },
    justifyDraft: "",
    justifyLoading: false,
    justifyFeedback: "",
    justifyApproved: false,

    // Built-in Focus Timer
    focusTimer: {
      visible: false,
      running: false,
      label: "Focus Session",
      durationSeconds: 1500,
      remainingSeconds: 1500,
      startedAtMs: null,
      taskId: null,
      goalId: null,
      intervalId: null,
    },

    // Briefing + Named AI Chat Contexts
    briefing: null,
    aiFocusNote: "",
    aiLoading: false,
    deskQuickPrompt: "",
    deskContextId: "chat-1",
    chatThreadsList: [], // [{ thread_id, name, count, updated_at }]
    chatThreads: {},     // { [threadId]: { messages: [], input: '', loading: false, queue: [] } }
    openThinkingIds: {},

    // Memory & Multi-file Notebook
    memoryTab: "notes",
    memoryDoc: { content: "", updated_at: null },
    notesDoc: { slug: "scratchpad", filename: "scratchpad.md", content: "", updated_at: null },
    notebookFiles: [],
    activeNotebookSlug: "scratchpad",
    newNotebookName: "",
    timeEstimates: { archetypes: { chat_reply: 5, email_triage: 10, assignment: 45, deep_project: 50 }, recent_Factor: 1.0 },
    notesEditMode: false,
    quickNoteInput: "",
    memorySaving: false,

    // Uploads
    uploadedFiles: [],
    uploadsDir: "/data/uploads",
    uploadIngestMode: "store",
    uploadingFile: false,
    ingestingFileMap: {},
    extractedTaskFileMap: {},
    dragOverFiles: false,
    pasteFilename: "notes.md",
    pasteContent: "",

    // App modal
    appModalOpen: false,
    appModalEditingSlug: null,
    aiAssistQuery: "",
    appForm: {
      name: "",
      url: "",
      icon: "globe",
      category: "General",
      account_profile: "default",
      enabled: true,
      zero_knowledge: false,
      indicator_type: "dom_badge",
      title_regex: "\\((\\d+)\\)",
      badge_selectors_text: "",
      notes: "",
    },

    // Diagnostics test ping
    simPing: {
      source_app: "System Monitor",
      indicator_type: "dom_badge",
      title: "Test Notification",
      message: "End-to-end alert check",
    },
    pingSubmitting: false,

    // v10.0 Resizable Desktop Widgets, App Library, Multi-Provider AI, Shared Logs & Extensions SDK
    desktopWidgets: [
      { id: "w-calendar", kind: "builtin", type: "calendar", title: "Schedule", col_span: 3, min_height: 206 },
      { id: "w-tasks", kind: "builtin", type: "tasks", title: "Priority Tasks", col_span: 3, min_height: 206 },
      { id: "w-grades", kind: "builtin", type: "grades", title: "Grades", col_span: 3, min_height: 206 },
      { id: "w-apps", kind: "builtin", type: "apps", title: "Web Apps", col_span: 3, min_height: 206 },
      { id: "w-briefing", kind: "builtin", type: "briefing", title: "Briefing", col_span: 12, min_height: 150 },
    ],
    builtinWidgetCatalog: [],
    desktopEditMode: false,
    showAddWidgetDrawer: false,
    resizingWidgetId: null,

    // Multi-Provider AI & Server-Side Job Queue
    selectedChatModel: "auto",
    aiProvidersState: { providers: [], routing: { primary_model: "auto", fast_model: "auto", searxng_url: "" } },
    serverAiJobs: [],
    attachedJobStreams: {},
    providerFormOpen: false,
    providerSaving: false,
    providerForm: {
      provider_id: "",
      name: "",
      protocol: "ollama",
      base_url: "http://192.168.1.50:11434",
      api_key: "",
      models_csv: "",
      auto_discover: true,
      enabled: true,
    },

    // Shared Multi-Agent Logbook
    sharedAgentLogs: [],
    sharedLogCategoryFilter: "all",
    sharedLogSearch: "",
    sharedLogForm: {
      log_id: null,
      title: "",
      category: "insight",
      tags_csv: "",
      pinned: false,
      content: "",
    },

    // App Library Catalog (Zero Preinstalled Apps on Fresh Setup)
    appCatalog: [],
    appCatalogCategory: "All",
    appCatalogSearch: "",
    showAppLibraryDrawer: true,
    installingTemplateId: null,
    templateUrlDrafts: {},

    // Generalized Social & Media Focus Guard Custom Site Form
    newPlatformForm: {
      name: "",
      domain: "",
      daily_limit_minutes: 30,
      short_form_immediate: true,
    },

    // ===== v10.2 Tag-first Gmail-style Inbox =====
    inboxQuery: "",
    inboxSavedFilters: [],
    inboxActiveFilter: "important",
    activeGradeTab: "all",
    gradesShowTabManager: false,
    gradesShowHidden: false,
    newGradeTabName: "",
    inboxFilterDraftName: "",
    inboxFilterGrammar: "",
    inboxTags: [],
    inboxShowQueryHelp: false,
    inboxShowTagManager: false,
    inboxShowDetail: false,
    inboxDetailId: null,
    inboxDrafts: {},          // { [tag]: "new tag name" }
    inboxNewTagForm: { name: "", description: "", color: "violet", important: true },
    inboxSelectorCache: {},   // { [notifId]: { selectors, ts } }
    inboxReadGraceId: null,   // id pending auto mark-read (gmail behaviour)
    inboxReadGraceTimer: null,
    inboxGroupsCollapsed: {},
    inboxAutoMerging: false,
    inboxHarvesting: false,
    inboxMoveMenuId: null,
    decisionGateStatus: { online: false, engine: "none", detail: "", model: "", protocol: "typesafe-systemone" },
    decisionGateTesting: false,
    decisionGateResult: null,

    // Inbox Sort, Custom Categories & Von-First Auto-Sorter
    inboxSortBy: "priority", // priority | newest | oldest | category | app | title
    selectedCategoryFilter: "All",
    inboxCategoriesState: {
      global_categories: ["Urgent", "School", "Work", "Personal", "Finance", "Updates", "Promo"],
      per_app_categories: {},
      all_categories: ["Urgent", "School", "Work", "Personal", "Finance", "Updates", "Promo"],
      autosort_engine: { primary: "von", von_online: false, active_label: "Fast AI" },
    },
    showInboxCategoryManager: false,
    newInboxCategoryInput: "",
    newInboxCategoryScope: "global",
    inboxAutoSorting: false,

    // Unified AI Instructions & Chat Feedback Self-Improvement
    unifiedInstructionsList: [],
    activeInstructionSlug: "persona",
    activeInstructionDoc: { slug: "persona", filename: "persona.md", title: "Persona & Standing Instructions", role_badge: "Core Persona", content: "", deletable: false },
    instructionEditMode: true,
    instructionSaving: false,
    newInstructionFileName: "",
    chatFeedbackMap: {}, // { [msgKey]: 'good' | 'poor' }
    poorFeedbackTargetMsg: null,
    poorFeedbackReason: "",
    selfImprovingNow: false,

    // Mini File Explorer / Shared Workspace Drive
    drivePath: "",
    driveSearch: "",
    driveBreadcrumbs: [{ name: "Drive", path: "" }],
    driveItems: [],
    driveStats: { total_files: 0, total_folders: 0, total_bytes: 0 },
    selectedDriveEntry: null,
    driveFileContent: "",
    driveFileEditable: false,
    drivePreviewMode: false,
    driveSaving: false,
    driveNewFolderOpen: false,
    driveNewFolderName: "",
    driveNewFileOpen: false,
    driveNewFileName: "notes.md",

    // Calendar Interactive Day Inspector & Quick-Add
    calendarDayInspectorOpen: true,
    calendarQuickAdd: {
      title: "",
      start_hhmm: "16:00",
      end_hhmm: "17:00",
      all_day: false,
      color: "emerald",
    },

    // Custom Apps & Desktop Widgets Extension SDK
    installedExtensions: [],
    extensionGallery: [],
    extensionsTab: "gallery", // 'gallery' | 'installed' | 'builder' | 'docs'
    extSdkDocsMarkdown: "",
    extBuilderSaving: false,
    extBuilderForm: {
      id: "study-flashcards",
      name: "Study Flashcards",
      version: "1.0.0",
      description: "Custom flashcard deck powered by NodeOS storage & Tasks.",
      icon: "layers",
      provides_app: true,
      provides_widget: true,
      col_span: 3,
      min_height: 206,
      permissions: ["storage", "tasks.read", "tasks.write", "notifications.send"],
      html: "<!doctype html><html><body style='margin:0;padding:16px;font-family:sans-serif;color:#e2e8f0;background:transparent'><h3 style='margin:0 0 8px;color:var(--accent,#2bfbae)'>Custom Node App</h3><p style='font-size:12px;color:#94a3b8'>Uses <code>window.NodeOS.storage</code> &amp; <code>NodeOS.tasks</code> inside a sandboxed iframe.</p><button onclick=\"NodeOS.notify('Hello from your custom extension!')\" style='padding:8px 14px;border-radius:10px;border:0;background:var(--accent,#2bfbae);color:#041f15;font-weight:700;cursor:pointer'>Test NodeOS Bridge</button></body></html>",
      widget_html: "",
    },

    // =========================================================================
    // Lifecycle
    // =========================================================================
    async init() {
      this.loadPreferences();
      this.setupExtensionRpcBridge();
      this.updateClock();
      setInterval(() => this.updateClock(), 1000);

      window.addEventListener("resize", () => {
        if (this.wmMode === "tiling") {
          this.layoutTiles(true);
        }
      });

      await this.fetchSessionIdentity();
      await Promise.all([
        this.loadOsSettings(true),
        this.loadDesktopWidgets(),
        this.loadWallpapers(),
        this.refreshCoreData(),
        this.loadAppCatalog(),
        this.loadAiProviders(false),
        this.loadExtensions(),
        this.loadSharedAgentLogs(),
        this.loadCronStatus(true),
        this.loadCalendarFeeds(),
        this.refreshCalendarRange(),
        this.loadGoals(),
        this.loadFocusGuardStatus(),
        this.loadLatestBriefing(),
        this.loadChatThreads(),
        this.loadMemoryFiles(),
        this.loadUnifiedInstructions(),
        this.loadInboxCategories(),
        this.loadInboxTags(),
        this.loadDecisionGateStatus(),
        this.loadDrive(""),
        this.loadUploads(),
      ]);
      await this.syncServerAiJobs();

      setInterval(() => {
        this.refreshCoreData(true);
        this.loadFocusGuardStatus(true);
        this.syncServerAiJobs();
      }, 12000);
    },

    async fetchSessionIdentity() {
      try {
        const s = await this.apiFetch("/api/v1/auth/session");
        if (s && s.username) this.currentUsername = s.username;
      } catch (_e) {}
    },

    lockSession() {
      for (const [, sock] of streamSockets.entries()) {
        try { sock.close(); } catch (_e) {}
      }
      streamSockets.clear();
      if (typeof window.__nodeLockSession === "function") {
        window.__nodeLockSession();
      } else {
        fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }).finally(() => {
          window.location.replace("/");
        });
      }
    },

    loadPreferences() {
      const savedTheme = safeStorageGet("node_theme");
      if (savedTheme === "light" || savedTheme === "dark") this.theme = savedTheme;
      this.applyTheme();

      this.liteGlass = safeStorageGet("node_lite_glass") === "true";
      document.documentElement.classList.toggle("lite-glass", this.liteGlass);

      const savedDock = safeStorageGet("node_dock_autohide");
      if (savedDock !== null) this.dockAutoHide = savedDock === "true";

      const savedWm = safeStorageGet("node_wm_mode");
      if (savedWm === "tiling" || savedWm === "floating") this.wmMode = savedWm;

      const savedThink = safeStorageGet("node_thinking");
      if (savedThink !== null) this.enableThinking = savedThink === "true";

      const savedWp = safeStorageGet("node_wallpaper");
      if (savedWp) this.activeWallpaperId = savedWp;

      const savedDim = Number(safeStorageGet("node_wallpaper_dim"));
      if (!Number.isNaN(savedDim) && savedDim >= 15 && savedDim <= 85) {
        this.wallpaperDim = savedDim;
      }
    },

    applyTheme() {
      document.documentElement.classList.toggle("theme-light", this.theme === "light");
      if (window.NodeCustomizer) {
        window.NodeCustomizer.applyDesignTokens(this.osSettings);
      }
    },

    toggleTheme() {
      const next = this.theme === "dark" ? "light" : "dark";
      this.setSingleOsSetting("theme", next);
    },

    toggleLiteGlass() {
      this.setSingleOsSetting("lite_glass", !this.liteGlass);
    },

    toggleDockAutoHide() {
      const next = !this.dockAutoHide;
      this.setSingleOsSetting("dock_auto_hide", next);
      this.notify(next ? "Taskbar set to Auto-Hide (hover bottom edge to reveal)" : "Taskbar pinned");
    },

    toggleThinkingMode() {
      this.setSingleOsSetting("enable_thinking", !this.enableThinking);
    },

    _matchesKeyCombo(ev, comboStr) {
      const raw = String(comboStr || "").trim();
      if (!raw) return false;
      const parts = raw.split("+").map((p) => p.trim().toLowerCase()).filter(Boolean);
      if (!parts.length) return false;
      const keyTarget = parts[parts.length - 1];
      const wantAlt = parts.includes("alt");
      const wantCtrl = parts.includes("ctrl") || parts.includes("cmd") || parts.includes("meta");
      const wantShift = parts.includes("shift");
      if (Boolean(ev.altKey) !== wantAlt) return false;
      if (Boolean(ev.ctrlKey || ev.metaKey) !== wantCtrl) return false;
      if (Boolean(ev.shiftKey) !== wantShift) return false;
      const pressed = (ev.key || "").toLowerCase();
      const code = (ev.code || "").toLowerCase();
      if (keyTarget === "space") return pressed === " " || code === "space";
      return pressed === keyTarget || code === `key${keyTarget}`;
    },

    onGlobalKeyDown(ev) {
      if (ev.key === "Escape") {
        if (this.startMenuOpen) {
          ev.preventDefault();
          this.closeStartMenu();
          return;
        }
        if (this.zenMode) {
          ev.preventDefault();
          this.exitZenMode();
          return;
        }
      }
      const inInput = ev.target && ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable='true']");
      if (
        (ev.altKey && ev.code === "Space") ||
        this._matchesKeyCombo(ev, this.osSettings.kb_start_menu || "Ctrl+K")
      ) {
        ev.preventDefault();
        this.toggleStartMenu();
        return;
      }
      if (inInput && !ev.altKey) return;

      if (this._matchesKeyCombo(ev, this.osSettings.kb_toggle_tiling || "Alt+T")) {
        ev.preventDefault();
        this.toggleWmMode();
      } else if (ev.altKey && (ev.key === "q" || ev.key === "Q")) {
        if (this.focusedWinId) {
          ev.preventDefault();
          this.closeWindow(this.focusedWinId);
        }
      } else if (this._matchesKeyCombo(ev, this.osSettings.kb_equalize_tiles || "Alt+E")) {
        if (this.wmMode === "tiling") {
          ev.preventDefault();
          this.equalizeTiles();
        }
      } else if (this._matchesKeyCombo(ev, this.osSettings.kb_new_chat || "Alt+C")) {
        ev.preventDefault();
        this.openChatWindow(this.defaultContextId());
      } else if (ev.altKey && (ev.key === "," || ev.key === "<")) {
        ev.preventDefault();
        this.openSettings();
      } else if (this._matchesKeyCombo(ev, this.osSettings.kb_zen_mode || "Alt+Z")) {
        ev.preventDefault();
        if (this.zenMode) this.exitZenMode();
        else this.startZenBreather(this.osSettings.zen_default_seconds || 60);
      } else if (this._matchesKeyCombo(ev, this.osSettings.kb_lock_screen || "Alt+L")) {
        ev.preventDefault();
        this.lockSession();
      }
    },

    isTopbarItemVisible(itemKey) {
      const hidden = String(this.osSettings.topbar_hidden_items || "")
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean);
      return !hidden.includes(String(itemKey || "").toLowerCase());
    },

    toggleTopbarItem(itemKey) {
      const key = String(itemKey || "").trim().toLowerCase();
      const set = new Set(
        String(this.osSettings.topbar_hidden_items || "")
          .split(",")
          .map((s) => s.trim().toLowerCase())
          .filter(Boolean)
      );
      if (set.has(key)) set.delete(key);
      else set.add(key);
      this.setSingleOsSetting("topbar_hidden_items", Array.from(set).join(", "));
    },

    isDockItemVisible(itemKey) {
      const hidden = String(this.osSettings.dock_hidden_items || "")
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean);
      return !hidden.includes(String(itemKey || "").toLowerCase());
    },

    toggleDockItem(itemKey) {
      const key = String(itemKey || "").trim().toLowerCase();
      const set = new Set(
        String(this.osSettings.dock_hidden_items || "")
          .split(",")
          .map((s) => s.trim().toLowerCase())
          .filter(Boolean)
      );
      if (set.has(key)) set.delete(key);
      else set.add(key);
      this.setSingleOsSetting("dock_hidden_items", Array.from(set).join(", "));
    },

    // =========================================================================
    // Taskbar (Dock), Topbar & Wallpaper Helpers
    // =========================================================================
    get isDockHidden() {
      if (this.zenMode) return true;
      if (!this.dockAutoHide) return false;
      if (!this.osSettings.dock_hide_on_desktop && this.visibleWindows.length === 0) {
        return false;
      }
      if (this.dockHovered || Date.now() < this.dockPeekUntil) return false;
      return true;
    },

    get isTopbarHidden() {
      if (this.zenMode) return true;
      if (!this.osSettings.topbar_auto_hide) return false;
      return !this.topbarHovered;
    },

    onDockEnter() {
      this.dockHovered = true;
    },

    onDockLeave() {
      this.dockHovered = false;
    },

    peekDock(ms = null) {
      const hold = ms !== null ? ms : Number(this.osSettings.dock_peek_ms) || 1600;
      this.dockPeekUntil = Date.now() + hold;
    },

    get activeWallpaperObj() {
      return this.wallpapers.find((w) => w.id === this.activeWallpaperId) || this.wallpapers[0];
    },

    get wallpaperClass() {
      const wp = this.activeWallpaperObj;
      if (!wp || wp.kind === "css") {
        return wp && wp.id === "minimal-slate" ? "os-wallpaper-minimal" : "os-wallpaper-mesh";
      }
      return "";
    },

    decorateAssetUrl(url) {
      if (!url) return "";
      return typeof window.__nodeDecorateUrl === "function" ? window.__nodeDecorateUrl(url) : url;
    },

    get wallpaperInlineStyle() {
      const wp = this.activeWallpaperObj;
      if (wp && wp.kind === "image" && wp.url) {
        return `background-image: url('${this.decorateAssetUrl(wp.url)}');`;
      }
      return "";
    },

    get wallpaperVeilStyle() {
      if (this.zenMode) return "opacity: 0.08;";
      const alpha = Math.min(0.9, Math.max(0.12, this.wallpaperDim / 100));
      return `opacity: ${alpha};`;
    },

    async loadWallpapers() {
      try {
        const res = await this.apiFetch("/api/v1/wallpapers");
        if (res && Array.isArray(res.wallpapers) && res.wallpapers.length > 0) {
          this.wallpapers = res.wallpapers;
          if (!this.wallpapers.some((w) => w.id === this.activeWallpaperId)) {
            const firstPhoto = this.wallpapers.find((w) => w.kind === "image");
            this.activeWallpaperId = firstPhoto ? firstPhoto.id : this.wallpapers[0].id;
          }
        }
      } catch (_e) {}
    },

    selectWallpaper(wp) {
      this.activeWallpaperId = wp.id;
      safeStorageSet("node_wallpaper", wp.id);
      this.notify(`Wallpaper: ${wp.title}`);
    },

    cycleWallpaper(step = 1) {
      if (!this.wallpapers.length) return;
      const idx = Math.max(0, this.wallpapers.findIndex((w) => w.id === this.activeWallpaperId));
      const next = (idx + step + this.wallpapers.length) % this.wallpapers.length;
      this.activeWallpaperId = this.wallpapers[next].id;
      safeStorageSet("node_wallpaper", this.activeWallpaperId);
    },

    updateWallpaperDim(val) {
      this.wallpaperDim = Number(val);
      safeStorageSet("node_wallpaper_dim", String(this.wallpaperDim));
    },

    startZenBreather(seconds = 60) {
      this.zenMode = true;
      this.setZenDuration(seconds);
    },

    setZenDuration(seconds) {
      if (this.zenInterval) {
        clearInterval(this.zenInterval);
        this.zenInterval = null;
      }
      if (!seconds || seconds <= 0) {
        this.zenUntimed = true;
        this.zenInitialSeconds = 0;
        this.zenSecondsLeft = 0;
        return;
      }
      this.zenUntimed = false;
      this.zenInitialSeconds = seconds;
      this.zenSecondsLeft = seconds;
      this.zenInterval = setInterval(() => {
        this.zenSecondsLeft = Math.max(0, this.zenSecondsLeft - 1);
        if (this.zenSecondsLeft <= 0) this.exitZenMode();
      }, 1000);
    },

    exitZenMode() {
      this.zenMode = false;
      if (this.zenInterval) {
        clearInterval(this.zenInterval);
        this.zenInterval = null;
      }
      if (this.wmMode === "tiling") {
        this.$nextTick(() => this.layoutTiles(true));
      }
    },

    // =========================================================================
    // Hyprland Auto-Tiling + Floating Window Manager
    // =========================================================================
    get visibleWindows() {
      return this.windows.filter((w) => !w.minimized);
    },

    get activeTileSplitters() {
      if (this.wmMode !== "tiling" || this.zenMode) return [];
      if (this.visibleWindows.some((w) => w.maximized)) return [];
      return this.tileSplitters;
    },

    windowDisplayTitle(win) {
      if (!win) return "";
      if (win.kind === "chat") {
        return `AI Chat — ${this.threadName(win.threadId)}`;
      }
      return win.title;
    },

    nodeSvg(name, size = 18, extraClass = "") {
      return renderMinimalSvg(name, size, extraClass);
    },

    windowSvg(win, size = 15) {
      if (!win) return renderMinimalSvg("apps", size);
      if (win.kind === "chat") return renderMinimalSvg("secretary", size);
      if (win.kind === "extension") return renderMinimalSvg(win.icon || "puzzle", size);
      if (win.kind === "stream") {
        const nm = (win.title || "").toLowerCase();
        if (nm.includes("mail") || nm.includes("gmail") || nm.includes("outlook")) return renderMinimalSvg("mail", size);
        if (nm.includes("powerschool") || nm.includes("schoology") || nm.includes("grade")) return renderMinimalSvg("grades", size);
        if (nm.includes("classroom") || nm.includes("canvas")) return renderMinimalSvg("tasks", size);
        return renderMinimalSvg("stream", size);
      }
      if (win.view === "extensions") return renderMinimalSvg("puzzle", size);
      return renderMinimalSvg(win.view || "apps", size);
    },

    monitorSvg(app, size = 16) {
      const nm = ((app && app.name) || "").toLowerCase();
      const ind = (app && app.indicator_type) || "";
      if (ind === "grades_tracker" || nm.includes("powerschool") || nm.includes("grade")) return renderMinimalSvg("grades", size);
      if (ind === "assignment_tracker" || nm.includes("classroom") || nm.includes("canvas")) return renderMinimalSvg("tasks", size);
      if (nm.includes("mail") || nm.includes("gmail") || nm.includes("outlook") || (app && app.icon === "mail")) return renderMinimalSvg("mail", size);
      if (nm.includes("teams") || nm.includes("slack") || nm.includes("discord")) return renderMinimalSvg("chat", size);
      return renderMinimalSvg("apps", size);
    },

    formatCompactTime24(hhmm) {
      const m = String(hhmm || "").match(/^(\d{1,2}):(\d{2})/);
      if (!m) return hhmm || "";
      let h = parseInt(m[1], 10);
      const mins = m[2];
      const suffix = h >= 12 ? "pm" : "am";
      h = h % 12 || 12;
      return mins === "00" ? `${h}${suffix}` : `${h}:${mins}${suffix}`;
    },

    // =========================================================================
    // Keybound Searchable Start Menu + Clean Event-Only "Next Up"
    // =========================================================================
    toggleStartMenu() {
      this.startMenuOpen = !this.startMenuOpen;
      if (this.startMenuOpen) {
        this.startMenuQuery = "";
        this.startMenuSelectedIdx = 0;
        this.$nextTick(() => {
          const inp = document.getElementById("node-start-search");
          if (inp) inp.focus();
        });
      }
    },

    closeStartMenu() {
      this.startMenuOpen = false;
      this.startMenuQuery = "";
    },

    get filteredStartMenuItems() {
      const baseItems = [
        { id: "app-desk", group: "OS Apps", label: "Show Desktop", sub: "Resizable widgets & briefing", icon: "desk", kbd: "", run: () => this.showDesktop() },
        { id: "app-secretary", group: "OS Apps", label: "AI Chat (Secretary)", sub: "Server-queued AI + web search + OS tools", icon: "secretary", kbd: "", run: () => this.openChatWindow(this.defaultContextId()) },
        { id: "app-inbox", group: "OS Apps", label: "Inbox", sub: `${this.importantUnreadCount} important unread`, icon: "inbox", kbd: "", run: () => this.openWindow("inbox") },
        { id: "app-calendar", group: "OS Apps", label: "Calendar", sub: "Schedule, .ics feeds & free slots", icon: "calendar", kbd: "", run: () => this.openWindow("calendar") },
        { id: "app-tasks", group: "OS Apps", label: "Tasks", sub: `${this.deskTopTasks.length} active tasks`, icon: "tasks", kbd: "", run: () => this.openWindow("tasks") },
        { id: "app-grades", group: "OS Apps", label: "Grades", sub: "PowerSchool, Schoology, Canvas & GPA", icon: "grades", kbd: "", run: () => this.openWindow("grades") },
        { id: "app-goals", group: "OS Apps", label: "Goals", sub: "Long-term habits & milestones", icon: "goals", kbd: "", run: () => this.openWindow("goals") },
        { id: "app-apps", group: "OS Apps", label: "App Library & Monitors", sub: "1-click install Gmail, Proton, LMS & web tabs", icon: "apps", kbd: "", run: () => this.openWindow("apps") },
        { id: "app-extensions", group: "OS Apps", label: "Extensions & Widgets SDK", sub: "Install or build custom apps & desktop widgets", icon: "puzzle", kbd: "", run: () => this.openWindow("extensions") },
        { id: "app-memory", group: "OS Apps", label: "AI Instructions", sub: "Unified persona, instruction files, logs & self-reflection", icon: "memory", kbd: "", run: () => this.openWindow("memory") },
        { id: "app-files", group: "OS Apps", label: "Files & Drive", sub: "Shared file explorer for you, AI models & server", icon: "files", kbd: "", run: () => this.openWindow("files") },
        { id: "app-guard", group: "OS Apps", label: "Social Focus Guard", sub: "YouTube, IG, TikTok, X, Reddit & custom caps", icon: "guard", kbd: "", run: () => this.openWindow("guard") },
        { id: "app-settings", group: "OS Apps", label: "Settings, Bars & Help", sub: "Themes, bars, keybinds, AI providers & feature guide", icon: "settings", kbd: "Alt+,", run: () => this.openSettings() },
      ];

      for (const ext of this.installedExtensions || []) {
        if (ext.provides_app !== false) {
          baseItems.push({
            id: `ext-${ext.id}`,
            group: "Custom Apps",
            label: ext.name,
            sub: ext.description || `Custom App v${ext.version || "1.0.0"}`,
            icon: ext.icon || "puzzle",
            kbd: "EXT",
            run: () => this.openExtensionWindow(ext.id),
          });
        }
      }

      for (const app of this.monitoredApps || []) {
        baseItems.push({
          id: `stream-${app.slug}`,
          group: "Streamed Web Apps",
          label: `Stream: ${app.name}`,
          sub: `${app.category || "Web"} • ${app.unread_count || 0} unread`,
          icon: "stream",
          kbd: "HD",
          run: () => this.openStreamWindow(app),
        });
      }

      baseItems.push(
        { id: "act-customize-desk", group: "Quick Actions", label: "Customize Desktop Widgets", sub: "Resize, add, or remove desktop widgets", icon: "resize", kbd: "", run: () => { this.showDesktop(); this.desktopEditMode = true; this.showAddWidgetDrawer = true; } },
        { id: "act-sync-tasks", group: "Quick Actions", label: "Sync Tasks from LMS & Inbox", sub: "Scan Schoology/Canvas/PowerSchool + important pings", icon: "spark", kbd: "", run: () => { this.openWindow("tasks"); this.syncTasksFromSources(); } },
        { id: "act-briefing", group: "Quick Actions", label: "Refresh AI Daily Briefing", sub: "Recompute workload vs free time", icon: "bolt", kbd: "", run: () => { this.showDesktop(); this.refreshBriefing(); } },
        { id: "act-wm", group: "Quick Actions", label: `Switch to ${this.wmMode === "tiling" ? "Floating" : "Tiling"} Mode`, sub: "Toggle window manager layout", icon: "grid", kbd: "Alt+T", run: () => this.toggleWmMode() },
        { id: "act-zen", group: "Quick Actions", label: "Start 60s Zen Breather", sub: "Calm fullscreen focus pause", icon: "zen", kbd: "Alt+Z", run: () => this.startZenBreather(60) },
        { id: "act-lock", group: "Quick Actions", label: "Lock Node Session", sub: "Return to zero-leak lock screen", icon: "lock", kbd: "Alt+L", run: () => this.lockSession() }
      );

      const q = (this.startMenuQuery || "").trim().toLowerCase();
      if (!q) return baseItems;
      return baseItems.filter((it) => `${it.label} ${it.sub} ${it.group}`.toLowerCase().includes(q));
    },

    onStartMenuKeyDown(ev) {
      const items = this.filteredStartMenuItems;
      if (!items.length) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        this.startMenuSelectedIdx = (this.startMenuSelectedIdx + 1) % items.length;
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        this.startMenuSelectedIdx = (this.startMenuSelectedIdx - 1 + items.length) % items.length;
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        const picked = items[Math.min(this.startMenuSelectedIdx, items.length - 1)];
        if (picked) this.launchStartMenuItem(picked);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        this.closeStartMenu();
      }
    },

    launchStartMenuItem(item) {
      if (!item) return;
      this.closeStartMenu();
      if (typeof item.run === "function") item.run();
    },

    // Chronological "Next Up" next to Desktop Clock — strictly upcoming Calendar events
    // (next 2–3 events / next few hours; NO hardcoded Bed Time or Wake Up anchors!)
    get desktopNextUpItems() {
      const now = new Date();
      const nowMins = now.getHours() * 60 + now.getMinutes();
      const today = this.todayIso;
      const tomorrow = this.addDaysIso(today, 1);

      const nearEvents = [];
      const laterEvents = [];

      for (const ev of this.calendarEvents || []) {
        if (ev.completed) continue;
        const d = (ev.start_at || "").slice(0, 10);
        if (d !== today && d !== tomorrow) continue;

        if (ev.all_day) {
          if (d === today) {
            laterEvents.push({
              id: `ev-${ev.id}`,
              kind: "event",
              icon: "calendar",
              label: ev.title,
              timeLabel: "All day",
              sortScore: nowMins + 240,
              targetView: "calendar",
            });
          }
          continue;
        }

        const hhmm = (ev.start_at || "").slice(11, 16);
        const endHhmm = (ev.end_at || "").slice(11, 16);
        const [sh, sm] = hhmm.split(":").map(Number);
        const [eh, em] = (endHhmm || hhmm).split(":").map(Number);
        if (!Number.isFinite(sh)) continue;

        const startM = sh * 60 + (sm || 0) + (d === tomorrow ? 1440 : 0);
        const endM = (Number.isFinite(eh) ? eh * 60 + (em || 0) : startM + 60) + (d === tomorrow ? 1440 : 0);
        if (endM <= nowMins) continue; // Already ended

        const minsUntil = startM - nowMins;
        // Only show tomorrow events if they are within the next 3 hours (e.g. late night looking at midnight/early morning)
        if (d === tomorrow && minsUntil > 180) continue;

        const entry = {
          id: `ev-${ev.id}`,
          kind: "event",
          icon: "calendar",
          label: ev.title,
          timeLabel: startM <= nowMins ? "Now" : this.formatCompactTime24(hhmm),
          sortScore: Math.max(nowMins, startM),
          targetView: "calendar",
        };

        if (minsUntil <= 180) {
          nearEvents.push(entry);
        } else {
          laterEvents.push(entry);
        }
      }

      nearEvents.sort((a, b) => a.sortScore - b.sortScore);
      laterEvents.sort((a, b) => a.sortScore - b.sortScore);
      const merged = nearEvents.length > 0 ? [...nearEvents, ...laterEvents] : laterEvents;
      return merged.slice(0, 3);
    },

    onClickNextUpItem(item) {
      if (!item) return;
      if (item.targetView === "settings") {
        this.openSettings(item.targetTab || "ai");
      } else if (item.targetView) {
        this.openWindow(item.targetView);
      }
    },

    toggleWmMode(anchorWinId = null) {
      if (this.wmMode === "tiling") {
        // Seed floating coordinates from current tiled rectangles for a smooth visual transition!
        this.windows.forEach((w, idx) => {
          const r = this.tileRects[w.id];
          if (r && !w.maximized) {
            const inset = Math.min(24, 10 + idx * 6);
            w.x = Math.max(16, r.x + inset);
            w.y = Math.max(46, r.y + 8 + (idx % 3) * 10);
            w.w = Math.max(360, r.w - inset * 2);
            w.h = Math.max(260, r.h - 16);
          }
        });
        this.wmMode = "floating";
        this.osSettings.wm_mode = "floating";
        safeStorageSet("node_wm_mode", "floating");
        if (anchorWinId) this.focusWindow(anchorWinId, false);
        this.syncAllVisibleStreamViewports();
        this.scheduleSettingsSync({ wm_mode: "floating" });
        this.notify("Floating mode • Window topbars shown");
      } else {
        this.wmMode = "tiling";
        this.osSettings.wm_mode = "tiling";
        safeStorageSet("node_wm_mode", "tiling");
        this.windows.forEach((w) => {
          w.maximized = false;
        });
        this.layoutTiles(true);
        if (anchorWinId) this.focusWindow(anchorWinId, false);
        this.scheduleSettingsSync({ wm_mode: "tiling" });
        this.notify("Tiling mode • Window topbars hidden");
      }
    },

    isViewFocused(view) {
      if (view === "desk") return this.visibleWindows.length === 0;
      const w = this.windows.find((it) => !it.minimized && it.id === this.focusedWinId);
      if (!w) return false;
      if (view === "secretary") return w.kind === "chat";
      return w.view === view;
    },

    isViewVisible(view) {
      if (view === "secretary") {
        return this.windows.some((w) => w.kind === "chat" && !w.minimized);
      }
      return this.windows.some((w) => w.view === view && !w.minimized);
    },

    showDesktop() {
      this.windows.forEach((w) => {
        w.minimized = true;
      });
      this.focusedWinId = null;
      this.manualTileFocusId = null;
      this.layoutTiles(false);
    },

    // Taskbar / Dock quick-tap: tap closed/hidden app -> open & tile; tap open app -> close/hide & re-tile!
    toggleDockWindow(view) {
      if (view === "secretary") {
        const visibleChats = this.windows.filter((w) => w.kind === "chat" && !w.minimized);
        if (visibleChats.length > 0) {
          if (this.wmMode === "tiling" || visibleChats.some((w) => w.id === this.focusedWinId)) {
            visibleChats.forEach((w) => { w.minimized = true; });
            this.pickNextFocus();
            this.layoutTiles(true);
            return;
          }
          this.focusWindow(visibleChats[visibleChats.length - 1].id);
          return;
        }
        const hiddenChat = this.windows.find((w) => w.kind === "chat" && w.minimized);
        if (hiddenChat) {
          hiddenChat.minimized = false;
          this.focusWindow(hiddenChat.id);
          this.layoutTiles(true);
          return;
        }
        this.openChatWindow(this.defaultContextId());
        return;
      }

      const winId = `panel-${view}`;
      const existing = this.windows.find((w) => w.id === winId);
      if (existing && !existing.minimized) {
        if (this.wmMode === "tiling" || this.focusedWinId === winId) {
          this.minimizeWindow(winId);
          return;
        }
        this.focusWindow(winId);
        return;
      }
      this.openWindow(view);
    },

    pickNextFocus() {
      const vis = this.visibleWindows;
      if (!vis.length) {
        this.focusedWinId = null;
        return;
      }
      const top = vis.reduce((a, b) => (a.z >= b.z ? a : b));
      this.focusedWinId = top.id;
    },

    defaultGeometry(idx = 0) {
      const vw = window.innerWidth || 1280;
      const vh = window.innerHeight || 800;
      const w = Math.min(980, Math.max(400, Math.round(vw * 0.72)));
      const h = Math.min(680, Math.max(360, Math.round(vh * 0.76)));
      const offset = (idx % 5) * 24;
      const x = Math.max(16, Math.min(vw - w - 16, Math.round((vw - w) / 2) + offset - 24));
      const y = Math.max(46, Math.min(vh - h - 20, 56 + offset));
      return { x, y, w, h };
    },

    openSettings(tab = null) {
      if (tab) {
        this.settingsTab = tab;
      }
      this.openWindow("settings");
    },

    openWindow(view, opts = {}) {
      if (view === "desk") {
        this.showDesktop();
        return;
      }
      if (typeof view === "string" && view.startsWith("ext:")) {
        this.openExtensionWindow(view.slice(4));
        return;
      }
      if (view === "secretary") {
        this.openChatWindow(opts.threadId || this.defaultContextId(), opts.initialPrompt || "");
        return;
      }
      if (view === "diagnostics") {
        this.settingsTab = "system";
        view = "settings";
      }
      if (opts.tab && view === "settings") {
        this.settingsTab = opts.tab;
      }
      if (opts.tab && view === "extensions") {
        this.extensionsTab = opts.tab;
      }
      if (opts.tab && view === "memory") {
        this.memoryTab = opts.tab;
      }
      if (opts.filter) {
        this.selectedAppFilter = opts.filter;
      }

      const titles = {
        calendar: "Calendar",
        tasks: "Tasks",
        grades: "Grades",
        goals: "Goals",
        inbox: "Inbox",
        apps: "App Library & Monitors",
        extensions: "Extensions & Widgets SDK",
        memory: "AI Instructions",
        files: "Files & Drive",
        gallery: "Wallpapers",
        guard: "Social Focus Guard",
        settings: "Settings & Help",
      };

      const winId = `panel-${view}`;
      const existing = this.windows.find((w) => w.id === winId);
      if (existing) {
        existing.minimized = false;
        this.focusWindow(winId, false);
        this.layoutTiles(true);
        if (view === "apps" || view === "inbox" || view === "settings" || view === "grades") this.refreshCoreData(true);
        if (view === "inbox") this.loadInboxCategories();
        if (view === "apps") this.loadAppCatalog();
        if (view === "extensions") this.loadExtensions();
        if (view === "memory") { this.loadUnifiedInstructions(); this.loadSharedAgentLogs(); }
        if (view === "files") this.loadDrive(this.drivePath || "");
        return;
      }

      const geo = this.defaultGeometry(this.windows.length);
      const createdWin = {
        id: winId,
        kind: "panel",
        view,
        title: titles[view] || view,
        minimized: false,
        maximized: false,
        z: ++this.nextZ,
        ...geo,
      };
      this.windows.push(createdWin);
      this.focusWindow(winId, false);
      this.layoutTiles(true);
      if (view === "inbox") this.loadInboxCategories();
      if (view === "memory") this.loadUnifiedInstructions();
      if (view === "files") this.loadDrive(this.drivePath || "");
      if (window.NodeCustomizer) {
        window.NodeCustomizer.emitWindowOpened(createdWin);
      }
    },

    // =========================================================================
    // Equal-Spaced + Focus-Aware Multi-Way Tiling Engine & Drag-to-Swap
    // =========================================================================
    workspaceBounds() {
      const vw = window.innerWidth || 1280;
      const vh = window.innerHeight || 800;
      const pad = Number.isFinite(Number(this.osSettings.outer_gap)) ? Number(this.osSettings.outer_gap) : 8;
      const innerGap = Number.isFinite(Number(this.osSettings.tile_gap)) ? Number(this.osSettings.tile_gap) : 8;
      // Persistent 36px OS topbar (unless topbar_auto_hide is enabled in Settings)
      const top = this.osSettings.topbar_auto_hide ? Math.max(8, pad) : 36 + Math.max(4, pad);
      const bottomReserve = this.dockAutoHide ? Math.max(12, pad + 6) : 76;
      return {
        x: pad,
        y: top,
        w: Math.max(300, vw - pad * 2),
        h: Math.max(220, vh - top - bottomReserve),
        gap: innerGap,
      };
    },

    _bestSplitDirForRect(rect) {
      // Pick left/right ('row') vs top/bottom ('col') based on which produces
      // child aspect ratios closest to the configured desktop aspect target!
      const targetAspect = Number(this.osSettings.split_target_aspect) || 1.38;
      const rowChildAspect = (rect.w / 2) / Math.max(1, rect.h);
      const colChildAspect = rect.w / Math.max(1, rect.h / 2);
      const rowPenalty = Math.abs(Math.log(Math.max(0.05, rowChildAspect) / targetAspect));
      const colPenalty = Math.abs(Math.log(Math.max(0.05, colChildAspect) / targetAspect));
      return rowPenalty <= colPenalty ? "row" : "col";
    },

    _normalizeWeights(weights, n) {
      if (!Array.isArray(weights) || weights.length !== n || n <= 0) {
        return Array.from({ length: Math.max(1, n) }, () => 1 / Math.max(1, n));
      }
      const clamped = weights.map((w) => Math.max(0.08, Number(w) || 1 / n));
      const sum = clamped.reduce((a, b) => a + b, 0) || 1;
      return clamped.map((w) => w / sum);
    },

    _buildEqualBalancedTree(winIds, bounds) {
      const n = winIds.length;
      if (n === 0) return null;
      if (n === 1) return { type: "leaf", winId: winIds[0] };
      if (n === 2) {
        const dir = this._bestSplitDirForRect(bounds);
        return {
          type: "split",
          id: `sp-${splitSeq++}`,
          dir,
          weights: [0.5, 0.5],
          children: winIds.map((id) => ({ type: "leaf", winId: id })),
        };
      }
      if (n === 3) {
        const threeColAspect = (bounds.w / 3) / Math.max(1, bounds.h);
        const dir = threeColAspect >= 0.45 ? "row" : "col";
        return {
          type: "split",
          id: `sp-${splitSeq++}`,
          dir,
          weights: [1 / 3, 1 / 3, 1 / 3],
          children: winIds.map((id) => ({ type: "leaf", winId: id })),
        };
      }
      // For N >= 4 (e.g. 4 -> 2x2 equal 25% grid, 6 -> 3x2 equal grid):
      // Distribute evenly across balanced rows of equal-width columns!
      const rowsCount = Math.max(2, Math.floor(Math.sqrt(n)));
      const rowChildren = [];
      let cursor = 0;
      for (let r = 0; r < rowsCount; r++) {
        const remItems = n - cursor;
        const remRows = rowsCount - r;
        const countInRow = Math.ceil(remItems / remRows);
        const slice = winIds.slice(cursor, cursor + countInRow);
        cursor += countInRow;
        if (slice.length === 1) {
          rowChildren.push({ type: "leaf", winId: slice[0] });
        } else {
          rowChildren.push({
            type: "split",
            id: `sp-${splitSeq++}`,
            dir: "row",
            weights: Array.from({ length: slice.length }, () => 1 / slice.length),
            children: slice.map((id) => ({ type: "leaf", winId: id })),
          });
        }
      }
      return {
        type: "split",
        id: `sp-${splitSeq++}`,
        dir: "col",
        weights: Array.from({ length: rowChildren.length }, () => 1 / rowChildren.length),
        children: rowChildren,
      };
    },

    _pruneTree(node, validSet) {
      if (!node) return null;
      if (node.type === "leaf") {
        return validSet.has(node.winId) ? node : null;
      }
      const kept = [];
      const keptWeights = [];
      const wArr = this._normalizeWeights(node.weights, node.children.length);
      node.children.forEach((ch, idx) => {
        const p = this._pruneTree(ch, validSet);
        if (p) {
          kept.push(p);
          keptWeights.push(wArr[idx]);
        }
      });
      if (kept.length === 0) return null;
      if (kept.length === 1) return kept[0];
      node.children = kept;
      node.weights = this._normalizeWeights(keptWeights, kept.length);
      return node;
    },

    _collectLeafIds(node, out = new Set()) {
      if (!node) return out;
      if (node.type === "leaf") {
        out.add(node.winId);
        return out;
      }
      for (const ch of node.children) {
        this._collectLeafIds(ch, out);
      }
      return out;
    },

    _childRects(node, rect, gap) {
      const k = node.children.length;
      const weights = this._normalizeWeights(node.weights, k);
      node.weights = weights;
      const out = [];
      if (node.dir === "row") {
        const availW = Math.max(100, rect.w - gap * (k - 1));
        let curX = rect.x;
        for (let i = 0; i < k; i++) {
          const cw = i === k - 1 ? Math.max(80, rect.x + rect.w - curX) : availW * weights[i];
          out.push({ x: curX, y: rect.y, w: cw, h: rect.h });
          curX += cw + gap;
        }
      } else {
        const availH = Math.max(100, rect.h - gap * (k - 1));
        let curY = rect.y;
        for (let i = 0; i < k; i++) {
          const ch = i === k - 1 ? Math.max(80, rect.y + rect.h - curY) : availH * weights[i];
          out.push({ x: rect.x, y: curY, w: rect.w, h: ch });
          curY += ch + gap;
        }
      }
      return out;
    },

    _largestLeafId(node, rect, gap) {
      const tempRects = {};
      this._computeTileGeometry(node, rect, gap, tempRects, []);
      let bestId = null;
      let bestArea = -1;
      for (const [wid, r] of Object.entries(tempRects)) {
        const area = r.w * r.h;
        if (area > bestArea) {
          bestArea = area;
          bestId = wid;
        }
      }
      return bestId;
    },

    _splitLeafBestAspect(node, targetLeafId, newWinId, rect, gap) {
      if (!node) {
        return { type: "leaf", winId: newWinId };
      }
      if (node.type === "leaf") {
        if (!targetLeafId || node.winId === targetLeafId) {
          const dir = this._bestSplitDirForRect(rect);
          return {
            type: "split",
            id: `sp-${splitSeq++}`,
            dir,
            weights: [0.5, 0.5],
            children: [node, { type: "leaf", winId: newWinId }],
          };
        }
        return node;
      }

      const cRects = this._childRects(node, rect, gap);
      for (let i = 0; i < node.children.length; i++) {
        const ch = node.children[i];
        if (this._collectLeafIds(ch).has(targetLeafId)) {
          // If splitting a direct leaf in the SAME direction as its parent, insert as an equal sibling!
          if (ch.type === "leaf" && ch.winId === targetLeafId) {
            const bestDir = this._bestSplitDirForRect(cRects[i]);
            if (bestDir === node.dir) {
              const oldW = node.weights[i];
              node.children.splice(i + 1, 0, { type: "leaf", winId: newWinId });
              node.weights.splice(i, 1, oldW / 2, oldW / 2);
              node.weights = this._normalizeWeights(node.weights, node.children.length);
              return node;
            }
          }
          node.children[i] = this._splitLeafBestAspect(ch, targetLeafId, newWinId, cRects[i], gap);
          return node;
        }
      }
      return node;
    },

    _swapLeaves(node, id1, id2) {
      if (!node || id1 === id2) return;
      if (node.type === "leaf") {
        if (node.winId === id1) node.winId = id2;
        else if (node.winId === id2) node.winId = id1;
        return;
      }
      for (const ch of node.children) {
        this._swapLeaves(ch, id1, id2);
      }
    },

    _findAncestorSplitsForLeaf(node, winId, rect, gap, ancestors = []) {
      if (!node) return null;
      if (node.type === "leaf") {
        return node.winId === winId ? ancestors : null;
      }
      const cRects = this._childRects(node, rect, gap);
      for (let i = 0; i < node.children.length; i++) {
        const hit = this._findAncestorSplitsForLeaf(node.children[i], winId, cRects[i], gap, [
          ...ancestors,
          { split: node, index: i, parentRect: rect },
        ]);
        if (hit) return hit;
      }
      return null;
    },

    _computeTileGeometry(node, rect, gap, outRects, outSplitters) {
      if (!node) return;
      if (node.type === "leaf") {
        outRects[node.winId] = {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.max(200, Math.round(rect.w)),
          h: Math.max(150, Math.round(rect.h)),
        };
        return;
      }
      const cRects = this._childRects(node, rect, gap);
      for (let i = 0; i < node.children.length; i++) {
        if (i < node.children.length - 1) {
          const rA = cRects[i];
          if (node.dir === "row") {
            outSplitters.push({
              id: `${node.id}-${i}`,
              node,
              index: i,
              dir: "row",
              parentRect: { ...rect },
              x: Math.round(rA.x + rA.w),
              y: Math.round(rect.y),
              w: gap,
              h: Math.round(rect.h),
            });
          } else {
            outSplitters.push({
              id: `${node.id}-${i}`,
              node,
              index: i,
              dir: "col",
              parentRect: { ...rect },
              x: Math.round(rect.x),
              y: Math.round(rA.y + rA.h),
              w: Math.round(rect.w),
              h: gap,
            });
          }
        }
        this._computeTileGeometry(node.children[i], cRects[i], gap, outRects, outSplitters);
      }
    },

    equalizeTiles() {
      this.hasCustomTileTree = false;
      this.manualTileFocusId = null;
      const bounds = this.workspaceBounds();
      const ids = this.tiledOrder.filter((id) =>
        this.windows.some((w) => w.id === id && !w.minimized && !w.maximized)
      );
      this.tileTree = this._buildEqualBalancedTree(ids, bounds);
      this.layoutTiles(true);
      this.notify("Equalized all tiled windows");
    },

    layoutTiles(syncStreams = false) {
      const tiledWins = this.windows.filter((w) => !w.minimized && !w.maximized);
      const validIds = new Set(tiledWins.map((w) => w.id));

      // Keep stable tiledOrder for equal-grid placement & drag-swaps
      this.tiledOrder = this.tiledOrder.filter((id) => validIds.has(id));
      const bounds = this.workspaceBounds();

      const prevInTree = this._collectLeafIds(this.tileTree);
      this.tileTree = this._pruneTree(this.tileTree, validIds);
      const inTree = this._collectLeafIds(this.tileTree);

      const newlyAdded = [];
      for (const w of tiledWins) {
        if (!this.tiledOrder.includes(w.id)) {
          this.tiledOrder.push(w.id);
        }
        if (!inTree.has(w.id)) {
          newlyAdded.push(w.id);
        }
      }

      if (newlyAdded.length > 0) {
        for (const newId of newlyAdded) {
          // If the user explicitly focused a window to split, split THAT window along best aspect ratio!
          if (
            this.manualTileFocusId &&
            inTree.has(this.manualTileFocusId) &&
            this.manualTileFocusId !== newId
          ) {
            if (!this.tileTree) {
              this.tileTree = this._buildEqualBalancedTree(this.tiledOrder, bounds);
            }
            this.tileTree = this._splitLeafBestAspect(
              this.tileTree,
              this.manualTileFocusId,
              newId,
              bounds,
              bounds.gap
            );
            this.hasCustomTileTree = true;
            this.manualTileFocusId = null;
            inTree.add(newId);
          } else if (this.hasCustomTileTree && this.tileTree) {
            // Preserve custom split ratios, splitting the LARGEST tile along best aspect ratio (never golden-ratio corner!)
            const largestId = this._largestLeafId(this.tileTree, bounds, bounds.gap);
            this.tileTree = this._splitLeafBestAspect(this.tileTree, largestId, newId, bounds, bounds.gap);
            inTree.add(newId);
          } else {
            // Default: clean equal-spaced balanced layout!
            this.tileTree = this._buildEqualBalancedTree(this.tiledOrder, bounds);
            this._collectLeafIds(this.tileTree, inTree);
          }
        }
      } else if (!this.hasCustomTileTree && prevInTree.size !== validIds.size) {
        // Window closed while in default equal mode -> rebalance remaining windows equally!
        this.tileTree = this._buildEqualBalancedTree(this.tiledOrder, bounds);
      }

      const nextRects = {};
      const nextSplitters = [];
      if (this.tileTree) {
        this._computeTileGeometry(this.tileTree, bounds, bounds.gap, nextRects, nextSplitters);
      }
      this.tileRects = nextRects;
      this.tileSplitters = nextSplitters;

      if (syncStreams && this.wmMode === "tiling") {
        this.syncAllVisibleStreamViewports();
      }
    },

    syncAllVisibleStreamViewports() {
      clearTimeout(this._streamSyncTimer);
      this._streamSyncTimer = setTimeout(() => {
        this.visibleWindows.forEach((w) => {
          if (w.kind === "stream") this.syncStreamViewport(w);
        });
      }, 140);
    },

    startSplitResize(ev, sp) {
      if (ev.button !== 0) return;
      this.isPointerInteracting = true;
      this.hasCustomTileTree = true;
      this.draggingSplitId = sp.id;
      const pRect = sp.parentRect;
      const idx = sp.index;
      const startX = ev.clientX;
      const startY = ev.clientY;
      const initWeights = [...this._normalizeWeights(sp.node.weights, sp.node.children.length)];
      const pairSum = initWeights[idx] + initWeights[idx + 1];

      const onMove = (me) => {
        const deltaFrac =
          sp.dir === "row"
            ? (me.clientX - startX) / Math.max(1, pRect.w)
            : (me.clientY - startY) / Math.max(1, pRect.h);
        const minW = Math.min(0.12, pairSum * 0.25);
        const wA = Math.min(pairSum - minW, Math.max(minW, initWeights[idx] + deltaFrac));
        const wB = pairSum - wA;
        const next = [...initWeights];
        next[idx] = wA;
        next[idx + 1] = wB;
        sp.node.weights = next;
        this.layoutTiles(false);
      };

      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        this.draggingSplitId = null;
        this.isPointerInteracting = false;
        this.syncAllVisibleStreamViewports();
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    windowStyle(win) {
      const transitionCss = this.isPointerInteracting
        ? "transition: none;"
        : "transition: left 0.17s cubic-bezier(0.22,1,0.36,1), top 0.17s cubic-bezier(0.22,1,0.36,1), width 0.17s cubic-bezier(0.22,1,0.36,1), height 0.17s cubic-bezier(0.22,1,0.36,1);";
      if (win.maximized) {
        return `z-index: ${win.z + 200}; ${transitionCss}`;
      }
      if (this.wmMode === "tiling") {
        const r = this.tileRects[win.id] || this.workspaceBounds();
        return `left: ${r.x}px; top: ${r.y}px; width: ${r.w}px; height: ${r.h}px; z-index: ${win.z}; ${transitionCss}`;
      }
      return `left: ${win.x}px; top: ${win.y}px; width: ${win.w}px; height: ${win.h}px; z-index: ${win.z}; ${transitionCss}`;
    },

    focusWindow(winId, isManualUserFocus = false) {
      const w = this.windows.find((it) => it.id === winId);
      if (!w) return;
      w.minimized = false;
      w.z = ++this.nextZ;
      this.focusedWinId = winId;
      if (isManualUserFocus && this.wmMode === "tiling") {
        this.manualTileFocusId = winId;
      }
    },

    onWindowPointerDown(ev, win) {
      this.focusWindow(win.id, true);
      // Support Alt + Left-Drag anywhere inside a tiled window to swap positions Hyprland-style!
      if (this.wmMode === "tiling" && ev.altKey && ev.button === 0 && !win.maximized) {
        ev.preventDefault();
        this.startDragWindow(ev, win, true);
      }
    },

    minimizeWindow(winId) {
      const w = this.windows.find((it) => it.id === winId);
      if (!w) return;
      w.minimized = true;
      if (this.manualTileFocusId === winId) this.manualTileFocusId = null;
      if (this.focusedWinId === winId) this.pickNextFocus();
      this.layoutTiles(true);
      this.peekDock(1600);
    },

    toggleMaximizeWindow(winId) {
      const w = this.windows.find((it) => it.id === winId);
      if (!w) return;
      this.focusWindow(winId);
      w.maximized = !w.maximized;
      this.layoutTiles(true);
      if (w.kind === "stream") {
        setTimeout(() => this.syncStreamViewport(w), 120);
      }
    },

    snapWindow(winId, side) {
      const w = this.windows.find((it) => it.id === winId);
      if (!w) return;
      if (this.wmMode === "tiling") {
        this.toggleWmMode(winId);
      }
      this.focusWindow(winId);
      w.maximized = false;
      const vw = window.innerWidth || 1280;
      const vh = window.innerHeight || 800;
      const top = 40;
      const usableH = vh - top - 8;
      w.y = top;
      w.h = usableH;
      w.w = Math.floor((vw - 12) / 2);
      w.x = side === "left" ? 4 : vw - w.w - 4;
      if (w.kind === "stream") {
        setTimeout(() => this.syncStreamViewport(w), 120);
      }
    },

    closeWindow(winId) {
      const idx = this.windows.findIndex((it) => it.id === winId);
      if (idx === -1) return;
      const w = this.windows[idx];
      if (w.kind === "stream") {
        this.disconnectStreamSocket(winId);
      }
      this.windows.splice(idx, 1);
      if (this.manualTileFocusId === winId) this.manualTileFocusId = null;
      if (this.focusedWinId === winId) this.pickNextFocus();
      this.layoutTiles(true);
      this.refreshCoreData(true);
    },

    startDragWindow(ev, win, forceHandle = false) {
      if (ev.button !== 0) return;
      if (!forceHandle && ev.target.closest("button, input, select, textarea, a")) return;
      this.focusWindow(win.id, true);
      if (win.maximized) return;

      // In Tiling mode: drag onto another tile to swap apps (keeping exact window layout!),
      // OR pull to top edge (y <= 14) to pop into Floating mode!
      if (this.wmMode === "tiling") {
        const startX = ev.clientX;
        const startY = ev.clientY;
        const onMove = (me) => {
          const moved = Math.hypot(me.clientX - startX, me.clientY - startY);
          if (moved < 8) return;
          this.pullToFloatActive = me.clientY <= 14;
          if (this.pullToFloatActive) {
            this.swapTargetWinId = null;
            return;
          }
          let found = null;
          for (const other of this.visibleWindows) {
            if (other.id === win.id) continue;
            const r = this.tileRects[other.id];
            if (r && me.clientX >= r.x && me.clientX <= r.x + r.w && me.clientY >= r.y && me.clientY <= r.y + r.h) {
              found = other.id;
              break;
            }
          }
          this.swapTargetWinId = found;
        };
        const onUp = (ue) => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          const shouldFloat = this.pullToFloatActive;
          const targetId = this.swapTargetWinId;
          this.pullToFloatActive = false;
          this.swapTargetWinId = null;

          if (shouldFloat) {
            this.toggleWmMode(win.id);
            const vw = window.innerWidth || 1280;
            win.w = Math.min(860, Math.max(420, Math.round(vw * 0.62)));
            win.h = Math.min(620, Math.max(340, Math.round((window.innerHeight || 800) * 0.68)));
            win.x = Math.max(12, Math.min(vw - win.w - 12, Math.round(ue.clientX - win.w / 2)));
            win.y = 44;
          } else if (targetId) {
            // Swap positions in BOTH the split tree and tiledOrder while keeping exact window sizes!
            this._swapLeaves(this.tileTree, win.id, targetId);
            const idxA = this.tiledOrder.indexOf(win.id);
            const idxB = this.tiledOrder.indexOf(targetId);
            if (idxA !== -1 && idxB !== -1) {
              const tmp = this.tiledOrder[idxA];
              this.tiledOrder[idxA] = this.tiledOrder[idxB];
              this.tiledOrder[idxB] = tmp;
            }
            this.layoutTiles(true);
          }
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        return;
      }

      // Floating mode drag
      this.isPointerInteracting = true;
      const startX = ev.clientX;
      const startY = ev.clientY;
      const origX = win.x;
      const origY = win.y;

      const onMove = (me) => {
        const vw = window.innerWidth || 1280;
        const vh = window.innerHeight || 800;
        win.x = Math.max(-win.w + 120, Math.min(vw - 80, origX + (me.clientX - startX)));
        win.y = Math.max(36, Math.min(vh - 42, origY + (me.clientY - startY)));
      };
      const onUp = () => {
        this.isPointerInteracting = false;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    startResizeWindow(ev, win) {
      if (ev.button !== 0 || win.maximized) return;
      ev.stopPropagation();
      this.focusWindow(win.id);
      this.isPointerInteracting = true;

      // In Tiling mode: corner resize adjusts nearest horizontal & vertical multi-way split weights!
      if (this.wmMode === "tiling") {
        this.hasCustomTileTree = true;
        const bounds = this.workspaceBounds();
        const ancestors = this._findAncestorSplitsForLeaf(this.tileTree, win.id, bounds, bounds.gap) || [];
        let rowAnc = null;
        let colAnc = null;
        for (let i = ancestors.length - 1; i >= 0; i--) {
          if (!rowAnc && ancestors[i].split.dir === "row" && ancestors[i].split.children.length >= 2) rowAnc = ancestors[i];
          if (!colAnc && ancestors[i].split.dir === "col" && ancestors[i].split.children.length >= 2) colAnc = ancestors[i];
        }
        const startX = ev.clientX;
        const startY = ev.clientY;
        const initRowWeights = rowAnc ? [...this._normalizeWeights(rowAnc.split.weights, rowAnc.split.children.length)] : null;
        const initColWeights = colAnc ? [...this._normalizeWeights(colAnc.split.weights, colAnc.split.children.length)] : null;

        const adjustPair = (anc, initW, deltaFrac) => {
          const k = initW.length;
          const idx = anc.index < k - 1 ? anc.index : k - 2;
          const dirSign = anc.index < k - 1 ? 1 : -1;
          const pairSum = initW[idx] + initW[idx + 1];
          const minW = Math.min(0.12, pairSum * 0.25);
          const wA = Math.min(pairSum - minW, Math.max(minW, initW[idx] + dirSign * deltaFrac));
          const next = [...initW];
          next[idx] = wA;
          next[idx + 1] = pairSum - wA;
          anc.split.weights = next;
        };

        const onMove = (me) => {
          if (rowAnc && initRowWeights) {
            adjustPair(rowAnc, initRowWeights, (me.clientX - startX) / Math.max(1, rowAnc.parentRect.w));
          }
          if (colAnc && initColWeights) {
            adjustPair(colAnc, initColWeights, (me.clientY - startY) / Math.max(1, colAnc.parentRect.h));
          }
          this.layoutTiles(false);
        };
        const onUp = () => {
          this.isPointerInteracting = false;
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          this.syncAllVisibleStreamViewports();
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        return;
      }

      // Floating mode resize
      const startX = ev.clientX;
      const startY = ev.clientY;
      const origW = win.w;
      const origH = win.h;

      const onMove = (me) => {
        win.w = Math.max(300, origW + (me.clientX - startX));
        win.h = Math.max(220, origH + (me.clientY - startY));
      };
      const onUp = () => {
        this.isPointerInteracting = false;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        if (win.kind === "stream") {
          this.syncStreamViewport(win);
        }
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    // =========================================================================
    // Server-Side Interactive Browser Streaming (Fixed Alpine Proxy Reactivity!)
    // =========================================================================
    getReactiveWindow(winId) {
      return this.windows.find((w) => w.id === winId) || null;
    },

    openStreamWindow(app) {
      const winId = `stream-${app.slug}`;
      let existing = this.getReactiveWindow(winId);
      if (existing) {
        existing.minimized = false;
        this.focusWindow(winId);
        this.layoutTiles(true);
        if (!streamSockets.has(winId)) {
          this.connectStreamSocket(winId);
        }
        return;
      }

      const geo = this.defaultGeometry(this.windows.length);
      geo.w = Math.min(1120, Math.max(520, geo.w + 80));
      geo.h = Math.min(740, Math.max(420, geo.h + 40));

      this.windows.push({
        id: winId,
        kind: "stream",
        appSlug: app.slug,
        title: app.name,
        minimized: false,
        maximized: false,
        z: ++this.nextZ,
        streamUrl: app.url,
        streamUrlInput: app.url,
        streamTitle: app.name,
        streamConnected: false,
        streamHasFrame: false,
        streamStatus: "Connecting…",
        streamError: "",
        streamCalibrating: false,
        streamQuality: "hd",
        viewportW: 1600,
        viewportH: 900,
        ...geo,
      });

      this.focusWindow(winId, false);
      this.layoutTiles(false);
      this.$nextTick(() => this.connectStreamSocket(winId));
    },

    connectStreamSocket(winId) {
      this.disconnectStreamSocket(winId);
      const rWin = this.getReactiveWindow(winId);
      if (!rWin) return;

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const rawWsUrl = `${proto}//${window.location.host}/api/v1/stream/ws/${encodeURIComponent(rWin.appSlug)}`;
      const wsUrl = this.decorateAssetUrl(rawWsUrl);

      rWin.streamStatus = "Opening tab…";
      rWin.streamError = "";

      const ws = new WebSocket(wsUrl);
      ws.binaryType = "blob";
      streamSockets.set(winId, ws);

      ws.onopen = () => {
        const w = this.getReactiveWindow(winId);
        if (!w) return;
        w.streamConnected = true;
        w.streamStatus = "Live";
        setTimeout(() => this.syncStreamViewport(w), 60);
      };

      ws.onmessage = (ev) => {
        const w = this.getReactiveWindow(winId);
        if (!w) return;

        // Fast Path: Raw Binary JPEG Frames (off-main-thread GPU decode via createImageBitmap!)
        if (ev.data instanceof Blob) {
          w.streamHasFrame = true;
          w.streamConnected = true;
          w.streamStatus = "Live";
          this.drawBinaryStreamBlob(winId, ev.data);
          return;
        }

        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (_e) {
          return;
        }

        if (msg.type === "frame" && msg.data) {
          w.streamHasFrame = true;
          w.streamConnected = true;
          w.streamStatus = "Live";
          if (msg.url) {
            w.streamUrl = msg.url;
            if (document.activeElement?.dataset?.winUrl !== winId) {
              w.streamUrlInput = msg.url;
            }
          }
          this.drawStreamFrame(winId, msg.data, msg);
        } else if (msg.type === "status") {
          w.streamConnected = true;
          if (msg.width) w.viewportW = msg.width;
          if (msg.height) w.viewportH = msg.height;
          if (msg.url) {
            w.streamUrl = msg.url;
            if (document.activeElement?.dataset?.winUrl !== winId) {
              w.streamUrlInput = msg.url;
            }
          }
          if (msg.title || msg.name) w.streamTitle = msg.title || msg.name;
          w.streamStatus = msg.status === "auth_required" ? "Sign in" : "Live";
          if (typeof msg.unread_count === "number" && msg.unread_count !== w._lastSyncedUnread) {
            w._lastSyncedUnread = msg.unread_count;
            this.refreshCoreData(true);
          }
        } else if (msg.type === "intercepted") {
          this.refreshCoreData(true);
          if (msg.added_count > 0) {
            const label = msg.titles && msg.titles[0] ? msg.titles[0] : `${msg.added_count} new item(s)`;
            this.notify(msg.added_count > 1 ? `${label} (+${msg.added_count - 1} more)` : label);
          }
        } else if (msg.type === "error") {
          w.streamError = msg.message || "Browser stream error";
          w.streamStatus = "Error";
        }
      };

      ws.onclose = (ev) => {
        streamSockets.delete(winId);
        const w = this.getReactiveWindow(winId);
        if (!w) return;
        w.streamConnected = false;
        if (ev.code === 4401) {
          this.lockSession();
          return;
        }
        // Keep a server-reported error visible instead of masking it as "Paused".
        if (w.streamStatus !== "Error") w.streamStatus = "Paused";
      };

      ws.onerror = () => {
        const w = this.getReactiveWindow(winId);
        if (!w) return;
        w.streamConnected = false;
        w.streamStatus = "Disconnected";
      };
    },

    disconnectStreamSocket(winId) {
      const ws = streamSockets.get(winId);
      if (ws) {
        try { ws.close(); } catch (_e) {}
        streamSockets.delete(winId);
      }
    },

    drawBinaryStreamBlob(winId, blob) {
      const w = this.getReactiveWindow(winId);
      if (!w) return;
      // Coalesce so if a newer frame arrives while decoding, skip intermediate frame
      w._pendingBlob = blob;
      if (w._decodingBlob) return;
      w._decodingBlob = true;

      const pump = async () => {
        while (w._pendingBlob) {
          const curBlob = w._pendingBlob;
          w._pendingBlob = null;
          const canvas = document.getElementById(`stream-canvas-${winId}`);
          if (!canvas) break;
          if (typeof window.createImageBitmap === "function") {
            try {
              const bmp = await window.createImageBitmap(curBlob);
              if (canvas.width !== bmp.width) canvas.width = bmp.width;
              if (canvas.height !== bmp.height) canvas.height = bmp.height;
              w.viewportW = bmp.width;
              w.viewportH = bmp.height;
              const ctx = canvas.getContext("2d", { alpha: false });
              if (ctx) ctx.drawImage(bmp, 0, 0);
              bmp.close();
              continue;
            } catch (_e) {}
          }
          // Fallback URL decode
          await new Promise((resolve) => {
            const objUrl = URL.createObjectURL(curBlob);
            const img = new Image();
            img.onload = () => {
              if (canvas.width !== img.naturalWidth) canvas.width = img.naturalWidth;
              if (canvas.height !== img.naturalHeight) canvas.height = img.naturalHeight;
              const ctx = canvas.getContext("2d", { alpha: false });
              if (ctx) ctx.drawImage(img, 0, 0);
              URL.revokeObjectURL(objUrl);
              resolve();
            };
            img.onerror = () => {
              URL.revokeObjectURL(objUrl);
              resolve();
            };
            img.src = objUrl;
          });
        }
        w._decodingBlob = false;
      };
      pump();
    },

    drawStreamFrame(winId, b64Data, meta) {
      const canvas = document.getElementById(`stream-canvas-${winId}`);
      if (!canvas) return;
      const w = this.getReactiveWindow(winId);
      const img = new Image();
      img.onload = () => {
        const cw = (meta && (meta.width || meta.deviceWidth)) || img.naturalWidth || (w ? w.viewportW : 1240);
        const ch = (meta && (meta.height || meta.deviceHeight)) || img.naturalHeight || (w ? w.viewportH : 760);
        if (canvas.width !== cw) canvas.width = cw;
        if (canvas.height !== ch) canvas.height = ch;
        if (w) {
          w.viewportW = cw;
          w.viewportH = ch;
        }
        const ctx = canvas.getContext("2d", { alpha: false });
        if (ctx) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      };
      img.src = `data:image/jpeg;base64,${b64Data}`;
    },

    syncStreamViewport(win) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const wrap = document.getElementById(`stream-wrap-${win.id}`);
      if (!wrap) return;
      const rect = wrap.getBoundingClientRect();
      if (rect.width < 120 || rect.height < 120) return;

      const isSaver = win.streamQuality === "saver";
      // In HD mode, render the remote desktop browser at crisp 1080p-class resolution
      // while matching the exact aspect ratio of the local pane (zero letterbox bars + crisp text)!
      let scale = 1.0;
      if (!isSaver) {
        const dpr = Math.min(2.0, window.devicePixelRatio || 1.25);
        const minTargetW = 1600;
        scale = Math.max(dpr, minTargetW / Math.max(320, rect.width));
      }
      let width = Math.round(rect.width * scale);
      let height = Math.round(rect.height * scale);
      const maxW = isSaver ? 1280 : 1920;
      const maxH = isSaver ? 800 : 1080;
      if (width > maxW) {
        height = Math.round(height * (maxW / width));
        width = maxW;
      }
      if (height > maxH) {
        width = Math.round(width * (maxH / height));
        height = maxH;
      }
      width = Math.max(800, width);
      height = Math.max(500, height);
      win.viewportW = width;
      win.viewportH = height;
      ws.send(JSON.stringify({ type: "resize", width, height }));
    },

    toggleStreamQuality(win) {
      if (!win) return;
      win.streamQuality = win.streamQuality === "saver" ? "hd" : "saver";
      const ws = streamSockets.get(win.id);
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "set_quality", mode: win.streamQuality }));
        this.syncStreamViewport(win);
      }
      this.notify(
        win.streamQuality === "hd"
          ? "HD 1080p stream enabled (smart frame dedup + crisp idle keyframes)"
          : "Wi-Fi Saver stream enabled (low-bandwidth capture)"
      );
    },

    canvasCoordsToServer(ev, win) {
      const canvas = ev.currentTarget;
      const rect = canvas.getBoundingClientRect();
      const targetW = canvas.width || win.viewportW || 1240;
      const targetH = canvas.height || win.viewportH || 760;

      const scale = Math.min(rect.width / targetW, rect.height / targetH);
      const renderedW = targetW * scale;
      const renderedH = targetH * scale;
      const padX = (rect.width - renderedW) / 2;
      const padY = (rect.height - renderedH) / 2;

      const x = Math.max(0, Math.min(targetW, Math.round((ev.clientX - rect.left - padX) / scale)));
      const y = Math.max(0, Math.min(targetH, Math.round((ev.clientY - rect.top - padY) / scale)));
      return { x, y };
    },

    onStreamMouse(ev, win, eventType) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

      if (eventType === "mouseMoved") {
        const { x, y } = this.canvasCoordsToServer(ev, win);
        win._pendingMove = { x, y };
        if (win._moveTimer) return;
        win._moveTimer = setTimeout(() => {
          win._moveTimer = null;
          if (win._pendingMove && ws.readyState === WebSocket.OPEN) {
            const pt = win._pendingMove;
            win._pendingMove = null;
            ws.send(JSON.stringify({ type: "mouse", action: "mouseMoved", x: pt.x, y: pt.y, button: "none", clickCount: 0 }));
          }
        }, 40);
        return;
      }

      if (eventType === "mousePressed") {
        ev.currentTarget.focus();
      }
      const { x, y } = this.canvasCoordsToServer(ev, win);
      const btn = ev.button === 2 ? "right" : ev.button === 1 ? "middle" : "left";
      ws.send(JSON.stringify({
        type: "mouse",
        action: eventType,
        x,
        y,
        button: btn,
        clickCount: ev.detail || 1,
      }));
    },

    onStreamWheel(ev, win) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ev.preventDefault();
      const { x, y } = this.canvasCoordsToServer(ev, win);
      win._wheelAccX = (win._wheelAccX || 0) + Math.round(ev.deltaX);
      win._wheelAccY = (win._wheelAccY || 0) + Math.round(ev.deltaY);
      win._wheelPt = { x, y };
      if (win._wheelTimer) return;
      win._wheelTimer = setTimeout(() => {
        win._wheelTimer = null;
        if (ws.readyState === WebSocket.OPEN && win._wheelPt) {
          ws.send(JSON.stringify({
            type: "mouse",
            action: "mouseWheel",
            x: win._wheelPt.x,
            y: win._wheelPt.y,
            deltaX: win._wheelAccX || 0,
            deltaY: win._wheelAccY || 0,
          }));
        }
        win._wheelAccX = 0;
        win._wheelAccY = 0;
      }, 45);
    },

    onStreamKeyDown(ev, win) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "v") return;
      ev.preventDefault();
      if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        ws.send(JSON.stringify({ type: "key", action: "insertText", text: ev.key }));
      } else {
        ws.send(JSON.stringify({ type: "key", action: "press", key: ev.key }));
      }
    },

    onStreamPaste(ev, win) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const text = ev.clipboardData?.getData("text/plain");
      if (text) {
        ev.preventDefault();
        ws.send(JSON.stringify({ type: "key", action: "insertText", text }));
      }
    },

    navigateStream(win, action) {
      const ws = streamSockets.get(win.id);
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        this.connectStreamSocket(win.id);
        return;
      }
      ws.send(JSON.stringify({
        type: "navigate",
        action,
        url: win.streamUrlInput,
      }));
    },

    async calibrateStreamApp(win) {
      win.streamCalibrating = true;
      try {
        const res = await this.apiFetch(`/api/v1/apps/${encodeURIComponent(win.appSlug)}/ai-calibrate`, {
          method: "POST",
        });
        await this.refreshCoreData(true);
        const summary = res?.telemetry?.notes || "Monitor rules updated";
        this.notify(summary);
      } catch (err) {
        this.notify(err.message || "Calibration failed", "error");
      } finally {
        win.streamCalibrating = false;
      }
    },

    async killServerTab(slug, winId) {
      this.disconnectStreamSocket(winId);
      try {
        await this.apiFetch(`/api/v1/stream/tabs/${encodeURIComponent(slug)}`, { method: "DELETE" });
        this.closeWindow(winId);
        await this.refreshCoreData(true);
        this.notify("Closed server tab");
      } catch (err) {
        this.notify(err.message || "Could not close tab", "error");
      }
    },

    // =========================================================================
    // Named AI Chat Contexts + Multi-Window Plugging + Prompt Stacking
    // =========================================================================
    defaultContextId() {
      if (this.chatThreadsList.length > 0) {
        return this.chatThreadsList[0].thread_id;
      }
      return "chat-1";
    },

    threadName(threadId) {
      const found = this.chatThreadsList.find((t) => t.thread_id === threadId);
      return found ? found.name : threadId;
    },

    ensureChatThread(threadId) {
      const tid = threadId || "chat-1";
      if (!this.chatThreads[tid]) {
        this.chatThreads[tid] = {
          messages: [],
          input: "",
          loading: false,
          queue: [],
          loadedFromServer: false,
        };
      }
      return this.chatThreads[tid];
    },

    async loadChatThreads() {
      try {
        const res = await this.apiFetch("/api/v1/ai/threads");
        const list = Array.isArray(res) ? res : (res && res.threads) || [];
        if (list.length > 0) {
          this.chatThreadsList = list;
          if (!this.chatThreadsList.some((t) => t.thread_id === this.deskContextId)) {
            this.deskContextId = this.chatThreadsList[0].thread_id;
          }
          await this.loadThreadMessages(this.chatThreadsList[0].thread_id);
        }
      } catch (_e) {}
    },

    async loadThreadMessages(threadId) {
      const th = this.ensureChatThread(threadId);
      try {
        const msgs = await this.apiFetch(`/api/v1/ai/chat?thread_id=${encodeURIComponent(threadId)}`);
        if (Array.isArray(msgs)) {
          th.messages = msgs;
          th.loadedFromServer = true;
        }
      } catch (_e) {}
    },

    openChatWindow(threadId = null, initialPrompt = "", awaitingContextPick = false) {
      const tid = threadId || this.defaultContextId();
      const th = this.ensureChatThread(tid);
      if (!th.loadedFromServer) {
        this.loadThreadMessages(tid);
      }

      // Reuse an existing chat window already plugged into `tid` unless opening a picker window
      if (!awaitingContextPick) {
        const existing = this.windows.find((w) => w.kind === "chat" && w.threadId === tid);
        if (existing) {
          existing.minimized = false;
          this.focusWindow(existing.id);
          this.layoutTiles(true);
          if (initialPrompt && initialPrompt.trim()) {
            this.enqueueChatPrompt(tid, initialPrompt.trim());
          }
          return;
        }
      }

      const winId = `chatwin-${this.chatWinCounter++}`;
      const geo = this.defaultGeometry(this.windows.length);
      geo.w = Math.min(680, Math.max(400, geo.w - 100));

      this.windows.push({
        id: winId,
        kind: "chat",
        view: "secretary",
        threadId: tid,
        title: "AI Chat",
        renamingContext: false,
        renameDraft: this.threadName(tid),
        awaitingContextPick: Boolean(awaitingContextPick),
        minimized: false,
        maximized: false,
        z: ++this.nextZ,
        ...geo,
      });
      this.focusWindow(winId, false);
      this.layoutTiles(true);

      if (initialPrompt && initialPrompt.trim()) {
        this.enqueueChatPrompt(tid, initialPrompt.trim());
      }
      this.scrollChatWindowsForThread(tid);
    },

    openNewChatWindowPicker() {
      this.openChatWindow(this.defaultContextId(), "", true);
    },

    async plugWindowIntoContext(winId, newThreadId) {
      const w = this.getReactiveWindow(winId);
      if (!w || !newThreadId) return;
      w.threadId = newThreadId;
      w.awaitingContextPick = false;
      w.renamingContext = false;
      const th = this.ensureChatThread(newThreadId);
      if (!th.loadedFromServer) {
        await this.loadThreadMessages(newThreadId);
      }
      this.scrollChatWindowsForThread(newThreadId);
    },

    async createAndPlugNewContext(winId = null, initialPrompt = "") {
      try {
        const created = await this.apiFetch("/api/v1/ai/threads", {
          method: "POST",
          body: JSON.stringify({}),
        });
        await this.loadChatThreads();
        this.ensureChatThread(created.thread_id).messages = [];
        this.ensureChatThread(created.thread_id).loadedFromServer = true;

        if (winId) {
          const w = this.getReactiveWindow(winId);
          if (w) {
            w.threadId = created.thread_id;
            w.awaitingContextPick = false;
            w.renameDraft = created.name;
            w.renamingContext = true;
            this.$nextTick(() => {
              const inp = document.getElementById(`rename-input-${w.id}`);
              if (inp) {
                inp.focus();
                inp.select();
              }
            });
          }
        } else {
          this.deskContextId = created.thread_id;
          this.openChatWindow(created.thread_id, initialPrompt, false);
        }
        return created;
      } catch (err) {
        this.notify(err.message || "Could not create context", "error");
        return null;
      }
    },

    startContextRename(win) {
      const rWin = this.getReactiveWindow(win.id);
      if (!rWin) return;
      rWin.renameDraft = this.threadName(rWin.threadId);
      rWin.renamingContext = true;
      this.$nextTick(() => {
        const inp = document.getElementById(`rename-input-${rWin.id}`);
        if (inp) {
          inp.focus();
          inp.select();
        }
      });
    },

    async commitContextRename(win) {
      const rWin = this.getReactiveWindow(win.id);
      if (!rWin) return;
      const trimmed = (rWin.renameDraft || "").trim();
      rWin.renamingContext = false;
      if (!trimmed) return;
      try {
        const updated = await this.apiFetch(`/api/v1/ai/threads/${encodeURIComponent(rWin.threadId)}`, {
          method: "PATCH",
          body: JSON.stringify({ name: trimmed }),
        });
        const item = this.chatThreadsList.find((t) => t.thread_id === rWin.threadId);
        if (item && updated && updated.name) item.name = updated.name;
        else await this.loadChatThreads();
        this.notify(`Context renamed to "${trimmed}"`);
      } catch (err) {
        this.notify(err.message || "Could not rename context", "error");
      }
    },

    async deleteContextFromWindow(win) {
      const tid = win.threadId;
      if (this.chatThreadsList.length <= 1) {
        return this.resetConversation(tid);
      }
      try {
        await this.apiFetch(`/api/v1/ai/threads/${encodeURIComponent(tid)}`, { method: "DELETE" });
        delete this.chatThreads[tid];
        await this.loadChatThreads();
        const fallbackId = this.defaultContextId();
        this.windows.forEach((w) => {
          if (w.kind === "chat" && w.threadId === tid) {
            w.threadId = fallbackId;
          }
        });
        this.notify("Context deleted");
      } catch (err) {
        this.notify(err.message || "Could not delete context", "error");
      }
    },

    async submitDeskPrompt() {
      const q = (this.deskQuickPrompt || "").trim();
      if (!q) return;
      this.deskQuickPrompt = "";
      if (this.deskContextId === "__new__") {
        await this.createAndPlugNewContext(null, q);
      } else {
        this.openChatWindow(this.deskContextId || this.defaultContextId(), q, false);
      }
    },

    submitWindowChat(win) {
      const th = this.ensureChatThread(win.threadId);
      const text = (th.input || "").trim();
      if (!text) return;
      th.input = "";
      win.awaitingContextPick = false;
      this.enqueueChatPrompt(win.threadId, text);
    },

    async enqueueChatPrompt(threadId, text, modelOverride = null) {
      const cleanText = String(text || "").trim();
      if (!cleanText) return null;
      const th = this.ensureChatThread(threadId);
      const chosenModel = modelOverride || this.selectedChatModel || "auto";

      // Optimistic user bubble while server job enqueues (<15ms)
      const tempUserId = `temp-user-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      th.messages.push({
        id: tempUserId,
        role: "user",
        content: cleanText,
        actions: [],
        created_at: new Date().toISOString(),
      });

      const streamMsgId = `stream-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      th.messages.push({
        id: streamMsgId,
        jobUid: null,
        role: "assistant",
        content: "",
        thinking: "",
        liveTools: [],
        actions: [],
        streaming: true,
        serverQueued: true,
        thinkingActive: Boolean(this.enableThinking),
        created_at: new Date().toISOString(),
      });
      this.scrollChatWindowsForThread(threadId);

      try {
        const submitted = await this.apiFetch("/api/v1/ai/jobs/chat", {
          method: "POST",
          body: JSON.stringify({
            message: cleanText,
            enable_thinking: this.enableThinking,
            thread_id: threadId,
            model: chosenModel,
          }),
        });

        const jobUid = submitted && submitted.job_uid;
        if (submitted && submitted.user_message) {
          const uIdx = th.messages.findIndex((m) => m.id === tempUserId);
          if (uIdx !== -1) th.messages.splice(uIdx, 1, submitted.user_message);
        }
        const liveMsg = th.messages.find((m) => m.id === streamMsgId);
        if (liveMsg && jobUid) {
          liveMsg.jobUid = jobUid;
        }
        if (submitted && submitted.job) {
          this.upsertLocalServerJob(submitted.job);
        }
        if (jobUid) {
          this.attachToServerJobStream(jobUid, threadId, streamMsgId);
        }
        return submitted;
      } catch (err) {
        const liveMsg = th.messages.find((m) => m.id === streamMsgId);
        if (liveMsg) {
          liveMsg.streaming = false;
          liveMsg.thinkingActive = false;
          liveMsg.serverQueued = false;
          liveMsg.content = `Could not enqueue prompt on server: ${err.message || "Connection offline"}`;
        }
        return null;
      }
    },

    removeQueuedPrompt(threadId, qid) {
      const th = this.ensureChatThread(threadId);
      th.queue = th.queue.filter((q) => q.qid !== qid);
    },

    upsertLocalServerJob(jobObj) {
      if (!jobObj || !jobObj.job_uid) return;
      const idx = (this.serverAiJobs || []).findIndex((j) => j.job_uid === jobObj.job_uid);
      if (idx >= 0) {
        this.serverAiJobs.splice(idx, 1, jobObj);
      } else {
        this.serverAiJobs.unshift(jobObj);
      }
    },

    activeJobsForThread(threadId) {
      return (this.serverAiJobs || []).filter(
        (j) => j.thread_id === threadId && (j.status === "queued" || j.status === "running")
      );
    },

    get totalActiveServerJobs() {
      return (this.serverAiJobs || []).filter((j) => j.status === "queued" || j.status === "running").length;
    },

    executeReturnedAction(act) {
      if (!act || !act.type) return;
      if ((act.type === "open_window" || act.type === "navigate") && act.view) {
        const mappedView = act.view === "focus_guard" ? "guard" : act.view;
        this.openWindow(mappedView, act);
      } else if (act.type === "open_settings") {
        this.openSettings(act.tab || "appearance");
      } else if (act.type === "open_stream" && act.slug) {
        const app = (this.monitoredApps || []).find((a) => a.slug === act.slug);
        if (app) this.openStreamWindow(app);
        else this.openWindow("apps");
      } else if (act.type === "os_settings_updated") {
        const patch = act.settings || act.settings_patch || {};
        this.osSettings = { ...this.osSettings, ...patch };
        if (patch.theme) {
          this.theme = patch.theme;
        }
        if (typeof patch.lite_glass === "boolean") {
          this.liteGlass = patch.lite_glass;
          document.documentElement.classList.toggle("lite-glass", this.liteGlass);
        }
        if (patch.wm_mode) {
          this.wmMode = patch.wm_mode;
          this.layoutTiles(true);
        }
        this.applyTheme();
      }
    },

    async attachToServerJobStream(jobUid, threadId, existingMsgId = null) {
      if (!jobUid || this.attachedJobStreams[jobUid]) return;
      this.attachedJobStreams[jobUid] = true;

      const th = this.ensureChatThread(threadId);
      th.loading = true;

      let streamMsgId = existingMsgId;
      let existingBubble = th.messages.find((m) => (streamMsgId && m.id === streamMsgId) || m.jobUid === jobUid);
      if (!existingBubble) {
        streamMsgId = `stream-${jobUid}`;
        th.messages.push({
          id: streamMsgId,
          jobUid,
          role: "assistant",
          content: "",
          thinking: "",
          liveTools: [],
          actions: [],
          streaming: true,
          serverQueued: true,
          thinkingActive: Boolean(this.enableThinking),
          created_at: new Date().toISOString(),
        });
      } else {
        streamMsgId = existingBubble.id;
      }

      const getLiveMsg = () => th.messages.find((m) => m.id === streamMsgId || m.jobUid === jobUid);
      let completedCleanly = false;

      try {
        const extraAuthHeaders = typeof window.__nodeGetSessionHeaders === "function"
          ? window.__nodeGetSessionHeaders()
          : {};
        const resp = await fetch(`/api/v1/ai/jobs/${encodeURIComponent(jobUid)}/stream`, {
          method: "GET",
          credentials: "same-origin",
          headers: { ...extraAuthHeaders },
        });

        if (resp.status === 401) {
          this.lockSession();
          return;
        }
        if (resp.ok && resp.body) {
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";

          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let nl;
            while ((nl = buf.indexOf("\n")) >= 0) {
              const line = buf.slice(0, nl).trim();
              buf = buf.slice(nl + 1);
              if (!line) continue;
              let ev;
              try {
                ev = JSON.parse(line);
              } catch (_e) {
                continue;
              }
              const liveMsg = getLiveMsg();
              if (!liveMsg) continue;

              if (ev.type === "job_status") {
                liveMsg.serverQueued = ev.status === "queued";
                this.upsertLocalServerJob({
                  job_uid: jobUid,
                  thread_id: threadId,
                  status: ev.status || "running",
                });
              } else if (ev.type === "thinking_delta" && ev.delta) {
                liveMsg.serverQueued = false;
                liveMsg.thinkingActive = true;
                liveMsg.thinking = (liveMsg.thinking || "") + ev.delta;
                this.scrollChatWindowsForThread(threadId);
              } else if (ev.type === "tool_start") {
                liveMsg.serverQueued = false;
                liveMsg.thinkingActive = false;
                liveMsg.liveTools.push({
                  name: ev.tool,
                  status: "running",
                  summary: "",
                });
                this.scrollChatWindowsForThread(threadId);
              } else if (ev.type === "tool_done") {
                const running = [...liveMsg.liveTools].reverse().find((t) => t.name === ev.tool && t.status === "running");
                if (running) {
                  running.status = ev.ok ? "done" : "error";
                  running.summary = ev.summary || "";
                } else {
                  liveMsg.liveTools.push({
                    name: ev.tool,
                    status: ev.ok ? "done" : "error",
                    summary: ev.summary || "",
                  });
                }
                if (Array.isArray(ev.actions)) {
                  ev.actions.forEach((a) => {
                    if (a.type === "os_settings_updated" || a.auto) {
                      this.executeReturnedAction(a);
                    }
                  });
                }
              } else if (ev.type === "content_delta" && ev.delta) {
                liveMsg.serverQueued = false;
                liveMsg.thinkingActive = false;
                liveMsg.content = (liveMsg.content || "") + ev.delta;
                this.scrollChatWindowsForThread(threadId);
              } else if (ev.type === "content_replace") {
                liveMsg.serverQueued = false;
                liveMsg.thinkingActive = false;
                liveMsg.content = ev.content || "";
                this.scrollChatWindowsForThread(threadId);
              } else if (ev.type === "done") {
                completedCleanly = true;
                const fin = ev.result || ev;
                if (fin && fin.assistant_message) {
                  const savedMsg = {
                    ...fin.assistant_message,
                    liveTools: liveMsg.liveTools || [],
                    streaming: false,
                    serverQueued: false,
                    thinkingActive: false,
                  };
                  const idx = th.messages.findIndex((m) => m.id === streamMsgId || m.jobUid === jobUid);
                  if (idx !== -1) {
                    th.messages.splice(idx, 1, savedMsg);
                  }
                }
                const finActions = fin.actions || ev.actions;
                if (Array.isArray(finActions)) {
                  finActions.forEach((a) => {
                    if (a.type === "os_settings_updated" || a.auto) {
                      this.executeReturnedAction(a);
                    }
                  });
                }
                this.upsertLocalServerJob({ job_uid: jobUid, thread_id: threadId, status: "completed" });
              } else if (ev.type === "error") {
                completedCleanly = true;
                liveMsg.streaming = false;
                liveMsg.serverQueued = false;
                liveMsg.thinkingActive = false;
                liveMsg.content = `Server AI notice: ${ev.detail || "Model unavailable"}`;
                this.upsertLocalServerJob({ job_uid: jobUid, thread_id: threadId, status: "failed" });
              }
            }
          }
        }

        if (completedCleanly) {
          await Promise.all([
            this.refreshCoreData(true),
            this.refreshCalendarRange(),
            this.loadGoals(),
            this.loadMemoryFiles(),
            this.loadSharedAgentLogs(),
            this.loadChatThreads(),
          ]);
        }
      } catch (_disconnectErr) {
        // NEVER overwrite with "NetworkError"! Server-side AIJob continues running in background.
        const liveMsg = getLiveMsg();
        if (liveMsg && !liveMsg.content) {
          liveMsg.serverQueued = true;
        }
      } finally {
        delete this.attachedJobStreams[jobUid];
        th.loading = this.activeJobsForThread(threadId).length > 0;
        this.scrollChatWindowsForThread(threadId);
        if (!completedCleanly) {
          setTimeout(() => this.syncServerAiJobs(), 2000);
        }
      }
    },

    async processChatQueue(threadId) {
      const th = this.ensureChatThread(threadId);
      while (th.queue.length > 0) {
        const nextItem = th.queue.shift();
        await this.enqueueChatPrompt(threadId, nextItem.text);
      }
    },

    scrollChatWindowsForThread(threadId) {
      this.$nextTick(() => {
        this.windows
          .filter((w) => w.kind === "chat" && w.threadId === threadId)
          .forEach((w) => {
            const el = document.getElementById(`chat-scroll-${w.id}`);
            if (el) el.scrollTop = el.scrollHeight;
          });
      });
    },

    async resetConversation(threadId) {
      try {
        await this.apiFetch(`/api/v1/ai/chat?thread_id=${encodeURIComponent(threadId)}`, { method: "DELETE" });
        const th = this.ensureChatThread(threadId);
        th.messages = [];
        await this.loadChatThreads();
        this.notify("Cleared messages in context");
      } catch (_e) {}
    },

    toggleMessageThinking(msgId) {
      this.openThinkingIds[msgId] = !this.openThinkingIds[msgId];
    },

    // =========================================================================
    // Clock & API Helper
    // =========================================================================
    updateClock() {
      const now = new Date();
      let h = now.getHours() % 12;
      if (h === 0) h = 12;
      const m = String(now.getMinutes()).padStart(2, "0");
      this.clockTime = `${h}:${m}`;
      this.clockSeconds = String(now.getSeconds()).padStart(2, "0");
      this.clockAmPm = now.getHours() >= 12 ? "PM" : "AM";
      this.topBarClock = now.toLocaleTimeString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
      this.clockDate = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
      this.todayIso = this.localDateIso(now);
    },

    localDateIso(d) {
      const yr = d.getFullYear();
      const mo = String(d.getMonth() + 1).padStart(2, "0");
      const da = String(d.getDate()).padStart(2, "0");
      return `${yr}-${mo}-${da}`;
    },

    async apiFetch(path, options = {}) {
      const extraAuthHeaders = typeof window.__nodeGetSessionHeaders === "function"
        ? window.__nodeGetSessionHeaders()
        : {};
      const mergedHeaders = {
        "Content-Type": "application/json",
        ...extraAuthHeaders,
        ...(options.headers || {}),
      };
      const resp = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers: mergedHeaders,
      });
      if (resp.status === 401 && !path.startsWith("/api/v1/auth/")) {
        this.lockSession();
        throw new Error("Session locked");
      }
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const errJson = await resp.json();
          detail = errJson.detail || JSON.stringify(errJson);
        } catch (_e) {}
        throw new Error(detail);
      }
      if (resp.status === 204) return null;
      return resp.json();
    },

    notify(message, type = "ok", opts = null) {
      this.toastMessage = message;
      this.toastType = type;
      this.toastActionLabel = (opts && opts.actionLabel) || "";
      this.toastAction = (opts && typeof opts.action === "function") ? opts.action : null;
      if (this.toastTimer) clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => {
        this.toastMessage = "";
        this.toastAction = null;
        this.toastActionLabel = "";
      }, opts && opts.duration ? opts.duration : 4200);
    },

    runToastAction() {
      const fn = this.toastAction;
      this.toastAction = null;
      this.toastActionLabel = "";
      this.toastMessage = "";
      if (typeof fn === "function") fn();
    },

    renderMarkdown(md) {
      if (!md) return "";
      if (window.marked && typeof window.marked.parse === "function") {
        return window.marked.parse(md);
      }
      return String(md).replace(/</g, "&lt;").replace(/\n/g, "<br/>");
    },

    // =========================================================================
    // Core Data, Gmail/ProtonMail Bulk Inbox, & Tasks
    // =========================================================================
    async refreshCoreData(silent = false) {
      try {
        const [sys, apps, notifs, tasks, grades, impStats, modelsSt] = await Promise.all([
          this.apiFetch("/api/v1/system/status"),
          this.apiFetch("/api/v1/apps"),
          this.apiFetch("/api/v1/notifications?limit=150"),
          this.apiFetch("/api/v1/tasks"),
          this.apiFetch("/api/v1/grades").catch(() => null),
          this.apiFetch("/api/v1/notifications/importance-stats").catch(() => null),
          this.apiFetch("/api/v1/models/status").catch(() => null),
        ]);
        this.system = sys;
        if (sys && sys.cron) {
          this.cronState = { ...this.cronState, ...sys.cron };
        }
        if (modelsSt) {
          this.modelsStatus = modelsSt;
        }
        this.monitoredApps = Array.isArray(apps) ? apps : (apps && apps.apps) || [];
        this.notifications = Array.isArray(notifs) ? notifs : [];
        this.tasks = Array.isArray(tasks) ? tasks : [];
        if (grades && Array.isArray(grades.courses)) {
          this.gradesData = grades;
        }
        if (impStats) {
          this.importanceStats = impStats;
        }
        this.pruneInboxSelection();
      } catch (err) {
        if (!silent && err.message !== "Session locked") {
          this.notify(`Sync error: ${err.message}`, "error");
        }
      }
    },

    async runInboxAISweep() {
      if (this.inboxAiProcessing) return;
      this.inboxAiProcessing = true;
      try {
        const res = await this.apiFetch("/api/v1/notifications/ai-process?limit=30", { method: "POST" });
        await this.refreshCoreData(true);
        this.notify(`AI cleaned & tagged ${res?.processed || 0} notification(s)`);
      } catch (err) {
        this.notify(err.message || "AI inbox sweep failed", "error");
      } finally {
        this.inboxAiProcessing = false;
      }
    },

    // Visual Grades Widget Ring + Distribution + Trend Sparklines + Assignments Computed Model
    get gradesChartSummary() {
      const courses = this.gradesData?.courses || [];
      const sum = this.gradesData?.summary || {};
      const avg = typeof sum.average_percent === "number" ? sum.average_percent : null;
      const gpa = typeof sum.unweighted_gpa === "number" ? sum.unweighted_gpa : null;
      const circumference = 2 * Math.PI * 28; // r = 28 -> ~175.93
      const clampedPct = avg !== null ? Math.max(0, Math.min(100, avg)) : 0;
      const dashOffset = circumference * (1 - clampedPct / 100);

      const dist = { A: 0, B: 0, C: 0, low: 0 };
      const enrichedCourses = courses.map((c) => {
        const hasScore = typeof c.score_percent === "number";
        const pct = hasScore
          ? Math.max(0, Math.min(100, c.score_percent))
          : (c.letter_grade?.startsWith("A") ? 95 : c.letter_grade?.startsWith("B") ? 85 : c.letter_grade?.startsWith("C") ? 75 : (c.letter_grade ? 65 : 0));
        const letter = (c.letter_grade || "").toUpperCase();
        if (hasScore || letter) {
          if (letter.startsWith("A") || pct >= 90) dist.A++;
          else if (letter.startsWith("B") || pct >= 80) dist.B++;
          else if (letter.startsWith("C") || pct >= 70) dist.C++;
          else dist.low++;
        }

        let barColor = "var(--accent)";
        if (!hasScore && !letter) barColor = "rgba(148,163,184,0.35)";
        else if (pct < 75) barColor = "#fb7185";
        else if (pct < 85) barColor = "#fbbf24";
        else if (pct < 90) barColor = "#38bdf8";

        const prevPct =
          typeof c.previous_score_percent === "number"
            ? c.previous_score_percent
            : (typeof c.prev_score_percent === "number" ? c.prev_score_percent : null);
        const prevLetter = c.previous_letter_grade || c.prev_letter_grade || "";
        const delta =
          typeof c.delta_percent === "number"
            ? c.delta_percent
            : (hasScore && prevPct !== null ? Math.round((c.score_percent - prevPct) * 100) / 100 : null);

        const hasTrendChange =
          (delta !== null && Math.abs(delta) >= 0.05) ||
          Boolean(prevLetter && c.letter_grade && prevLetter !== c.letter_grade);
        const trendIsUp = delta !== null ? delta >= 0 : c.trend !== "down";

        let shortDelta = "";
        let fullTrendText = c.trend_label || "";
        if (hasTrendChange) {
          const arrow = trendIsUp ? "↑" : "↓";
          if (delta !== null && Math.abs(delta) >= 0.05) {
            const sign = delta > 0 ? "+" : "";
            shortDelta = `${arrow} ${sign}${delta.toFixed(1)}%`;
            const wasPart = prevLetter && prevPct !== null ? `${prevLetter} (${prevPct}%)` : (prevPct !== null ? `${prevPct}%` : prevLetter);
            fullTrendText = `${arrow} ${sign}${delta.toFixed(1)}% • was ${wasPart}`;
          } else if (prevLetter) {
            shortDelta = `${arrow} ${prevLetter}→${c.letter_grade}`;
            fullTrendText = `${arrow} moved from ${prevLetter} to ${c.letter_grade}`;
          }
        }

        // Build inline SVG sparkline coordinates (`viewBox="0 0 76 22"`) from persistent `history`
        const rawHist = Array.isArray(c.history) ? c.history : [];
        const numericHist = rawHist
          .map((h) => (typeof h?.score_percent === "number" ? h.score_percent : null))
          .filter((v) => v !== null);
        let hasSparkline = false;
        let sparklinePoints = "";
        let sparklineLastX = 72;
        let sparklineLastY = 11;
        if (numericHist.length >= 2) {
          hasSparkline = true;
          const pts = numericHist.slice(-14);
          let minV = Math.min(...pts);
          let maxV = Math.max(...pts);
          if (maxV - minV < 2) {
            minV -= 1;
            maxV += 1;
          }
          const coords = pts.map((v, idx) => {
            const x = 4 + (idx / (pts.length - 1)) * 68;
            const y = 18 - ((v - minV) / (maxV - minV)) * 14;
            return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
          });
          sparklinePoints = coords.map(([x, y]) => `${x},${y}`).join(" ");
          sparklineLastX = coords[coords.length - 1][0];
          sparklineLastY = coords[coords.length - 1][1];
        }

        // Build compact term pills list (`Q1: A- 91%`, `Q2: A 94%`)
        const tgObj = c.term_grades && typeof c.term_grades === "object" ? c.term_grades : {};
        const termOrder = ["Q1", "T1", "MP1", "Q2", "T2", "MP2", "S1", "Q3", "T3", "MP3", "Q4", "MP4", "S2", "Y1", "F1"];
        const termKeys = Object.keys(tgObj).sort((a, b) => {
          const ia = termOrder.indexOf(a.toUpperCase());
          const ib = termOrder.indexOf(b.toUpperCase());
          if (ia === -1 && ib === -1) return a.localeCompare(b);
          if (ia === -1) return 1;
          if (ib === -1) return -1;
          return ia - ib;
        });
        const termList = termKeys
          .map((tk) => {
            const entry = tgObj[tk] || {};
            const lg = entry.letter_grade || "";
            const sp = typeof entry.score_percent === "number" ? `${entry.score_percent}%` : "";
            const val = [lg, sp].filter(Boolean).join(" ");
            return val ? { term: tk, display: val, isActive: tk.toUpperCase() === String(c.term || "").toUpperCase() } : null;
          })
          .filter(Boolean);

        const assignments = Array.isArray(c.assignments) ? c.assignments : [];

        return {
          ...c,
          period_label: c.period_label || c.period || "",
          teacher_name: c.teacher_name || c.teacher || "",
          term_label: c.term_label || c.term || "Current",
          previous_score_percent: prevPct,
          previous_letter_grade: prevLetter,
          barPercent: Math.round(pct),
          barColor,
          hasTrendChange,
          trendIsUp,
          shortDelta,
          fullTrendText,
          hasSparkline,
          sparklinePoints,
          sparklineLastX,
          sparklineLastY,
          termList,
          assignments,
          assignmentsCount: assignments.length,
        };
      });

      const gradedTotal = Math.max(1, dist.A + dist.B + dist.C + dist.low);
      const totalAssignments =
        typeof sum.total_assignments === "number"
          ? sum.total_assignments
          : enrichedCourses.reduce((acc, item) => acc + item.assignmentsCount, 0);
      const totalMissing =
        typeof sum.total_missing === "number"
          ? sum.total_missing
          : enrichedCourses.reduce((acc, item) => acc + (item.missing_count || 0), 0);

      return {
        hasCourses: courses.length > 0,
        totalCourses: courses.length,
        totalAssignments,
        totalMissing,
        avgDisplay: avg !== null ? `${avg.toFixed(1)}%` : "—",
        gpaDisplay: gpa !== null ? gpa.toFixed(2) : "—",
        circumference: circumference.toFixed(2),
        dashOffset: dashOffset.toFixed(2),
        dist,
        distShares: {
          A: Math.round((dist.A / gradedTotal) * 100),
          B: Math.round((dist.B / gradedTotal) * 100),
          C: Math.round((dist.C / gradedTotal) * 100),
          low: Math.round((dist.low / gradedTotal) * 100),
        },
        courses: enrichedCourses,
      };
    },

    get importantUnreadCount() {
      return this.notifications.filter((n) => !n.read_state && n.is_important !== false).length;
    },

    get promoFilteredCount() {
      return this.notifications.filter((n) => !n.read_state && n.is_important === false).length;
    },

    get deskTopTasks() {
      return this.tasks.filter((t) => !t.completed);
    },

    get deskTodayEvents() {
      return this.eventsForDate(this.todayIso);
    },

    _slugifyAppKey(raw) {
      return String(raw || "")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
    },

    get inboxAppFilters() {
      // Canonicalize by slug so renamed accounts or case differences NEVER split into 2 tags!
      const slugToCanonical = new Map();
      for (const a of this.monitoredApps || []) {
        const k = a.slug || this._slugifyAppKey(a.name);
        if (k) slugToCanonical.set(k, a.name);
      }
      const counts = {};
      for (const n of this.notifications || []) {
        const k = this._slugifyAppKey(n.source_app);
        const canon = slugToCanonical.get(k) || n.source_app;
        if (!slugToCanonical.has(k) && canon) slugToCanonical.set(k, canon);
        if (!n.read_state && canon) counts[canon] = (counts[canon] || 0) + 1;
      }
      const out = [{ name: "All", unread: (this.notifications || []).filter((n) => !n.read_state).length }];
      const seenNames = new Set(["all"]);
      for (const canon of slugToCanonical.values()) {
        if (!canon || seenNames.has(canon.toLowerCase())) continue;
        seenNames.add(canon.toLowerCase());
        out.push({ name: canon, unread: counts[canon] || 0 });
      }
      return out;
    },

    get inboxCategoryPills() {
      const counts = {};
      const seenLower = new Map();
      const configured = (this.inboxCategoriesState && this.inboxCategoriesState.all_categories) || [
        "Urgent", "School", "Work", "Personal", "Finance", "Updates", "Promo",
      ];
      for (const c of configured) {
        if (c && !seenLower.has(c.toLowerCase())) seenLower.set(c.toLowerCase(), c);
      }
      for (const n of this.notifications || []) {
        const rawC = String(n.category || "General").trim();
        if (!rawC) continue;
        const canon = seenLower.get(rawC.toLowerCase()) || rawC;
        if (!seenLower.has(rawC.toLowerCase())) seenLower.set(rawC.toLowerCase(), canon);
        if (!n.read_state) counts[canon] = (counts[canon] || 0) + 1;
      }
      const pills = [{ name: "All", count: (this.notifications || []).filter((n) => !n.read_state).length }];
      for (const canon of seenLower.values()) {
        pills.push({ name: canon, count: counts[canon] || 0 });
      }
      return pills;
    },

    get filteredNotifications() {
      const q = (this.inboxSearch || "").trim().toLowerCase();
      const targetAppKey = this.selectedAppFilter === "All" ? null : this._slugifyAppKey(this.selectedAppFilter);
      const targetCat = (this.selectedCategoryFilter || "All").toLowerCase();

      const filtered = (this.notifications || []).filter((n) => {
        if (this.inboxTab === "important" && n.is_important === false) return false;
        if ((this.inboxTab === "unread" || this.unreadOnlyFilter) && n.read_state) return false;
        if (targetAppKey && this._slugifyAppKey(n.source_app) !== targetAppKey) return false;
        if (targetCat !== "all" && String(n.category || "General").trim().toLowerCase() !== targetCat) return false;
        if (q) {
          const hay = `${n.source_app} ${n.category || ""} ${n.title} ${n.ai_summary || ""} ${n.message || ""}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });

      const prioRank = { critical: 0, high: 1, normal: 2, low: 3 };
      const mode = this.inboxSortBy || "priority";
      return filtered.slice().sort((a, b) => {
        if (mode === "newest") return (b.id || 0) - (a.id || 0);
        if (mode === "oldest") return (a.id || 0) - (b.id || 0);
        if (mode === "category") {
          const c = String(a.category || "General").localeCompare(String(b.category || "General"));
          return c !== 0 ? c : (b.id || 0) - (a.id || 0);
        }
        if (mode === "app") {
          const c = String(a.source_app || "").localeCompare(String(b.source_app || ""));
          return c !== 0 ? c : (b.id || 0) - (a.id || 0);
        }
        if (mode === "title") {
          return String(a.title || "").localeCompare(String(b.title || ""));
        }
        // Default: priority (unread + importance_score + priority rank + newest)
        const ua = a.read_state ? 1 : 0;
        const ub = b.read_state ? 1 : 0;
        if (ua !== ub) return ua - ub;
        const pa = prioRank[String(a.priority || "normal").toLowerCase()] ?? 2;
        const pb = prioRank[String(b.priority || "normal").toLowerCase()] ?? 2;
        if (pa !== pb) return pa - pb;
        const sa = Number(a.importance_score ?? 0.5);
        const sb = Number(b.importance_score ?? 0.5);
        if (Math.abs(sb - sa) > 0.05) return sb - sa;
        return (b.id || 0) - (a.id || 0);
      });
    },

    notificationSummaryLine(n) {
      if (!n) return "";
      if (n.ai_summary && n.ai_summary.trim()) return n.ai_summary.trim();
      const msg = (n.message || "").trim();
      if (msg && msg.toLowerCase() !== (n.title || "").trim().toLowerCase()) return msg;
      return "";
    },

    async onClickNotificationRow(n) {
      if (!n) return;
      this.toggleExpandNotification(n.id);
      const isExpanded = Boolean(this.expandedNotificationIds[n.id]);
      if (!isExpanded) return;

      // Auto-fetch deeper content & 1-line AI summary if this was a generic ping or lacks an AI summary!
      const isBarePing = /\bping\b/i.test(n.title || "") || /new activity detected/i.test(n.message || "");
      if (n.indicator_type !== "ping_only" && (isBarePing || !n.ai_summary)) {
        await this.enrichNotification(n, true);
      }
    },

    async enrichNotification(n, silent = false) {
      if (!n || !n.id || this.enrichingNotifIds[n.id]) return;
      this.enrichingNotifIds[n.id] = true;
      try {
        const res = await this.apiFetch(`/api/v1/notifications/${n.id}/enrich`, { method: "POST" });
        const idx = this.notifications.findIndex((it) => it.id === n.id);
        if (idx !== -1 && res) {
          this.notifications[idx] = { ...this.notifications[idx], ...res };
        }
        if (!silent) {
          this.notify("Fetched 1-line content summary");
        }
      } catch (err) {
        if (!silent) this.notify(err.message || "Could not summarize message", "error");
      } finally {
        this.enrichingNotifIds[n.id] = false;
      }
    },

    async setNotificationImportance(n, markImportant) {
      if (!n || !n.id) return;
      try {
        const res = await this.apiFetch(`/api/v1/notifications/${n.id}/importance`, {
          method: "POST",
          body: JSON.stringify({ is_important: Boolean(markImportant) }),
        });
        await this.refreshCoreData(true);
        const sender = res?.learner?.sender_key;
        if (markImportant) {
          this.notify(sender ? `Marked Important • Prioritizing "${sender}"` : "Marked Important • AI filter updated");
        } else {
          this.notify(sender ? `Marked Not Important • Muting promos like "${sender}"` : "Marked Not Important • AI filter updated");
        }
      } catch (err) {
        this.notify(err.message || "Could not update importance", "error");
      }
    },

    get allVisibleNotificationsSelected() {
      const vis = this.filteredNotifications;
      if (!vis.length) return false;
      const sel = new Set(this.selectedNotificationIds);
      return vis.every((n) => sel.has(n.id));
    },

    isNotificationSelected(id) {
      return this.selectedNotificationIds.includes(id);
    },

    toggleSelectNotification(id) {
      if (this.selectedNotificationIds.includes(id)) {
        this.selectedNotificationIds = this.selectedNotificationIds.filter((x) => x !== id);
      } else {
        this.selectedNotificationIds = [...this.selectedNotificationIds, id];
      }
    },

    toggleSelectAllNotifications() {
      if (this.allVisibleNotificationsSelected) {
        this.selectedNotificationIds = [];
      } else {
        this.selectedNotificationIds = this.filteredNotifications.map((n) => n.id);
      }
    },

    selectInboxByPreset(preset) {
      const vis = this.filteredNotifications;
      if (preset === "none") {
        this.selectedNotificationIds = [];
      } else if (preset === "all") {
        this.selectedNotificationIds = vis.map((n) => n.id);
      } else if (preset === "unread") {
        this.selectedNotificationIds = vis.filter((n) => !n.read_state).map((n) => n.id);
      } else if (preset === "read") {
        this.selectedNotificationIds = vis.filter((n) => n.read_state).map((n) => n.id);
      }
    },

    pruneInboxSelection() {
      const valid = new Set(this.notifications.map((n) => n.id));
      this.selectedNotificationIds = this.selectedNotificationIds.filter((id) => valid.has(id));
    },

    toggleExpandNotification(id) {
      this.expandedNotificationIds[id] = !this.expandedNotificationIds[id];
    },

    async runInboxBulkAction(action) {
      const ids = [...this.selectedNotificationIds];
      if (!ids.length) return;
      try {
        const res = await this.apiFetch("/api/v1/notifications/bulk", {
          method: "POST",
          body: JSON.stringify({ ids, action }),
        });
        if (action === "delete") {
          this.selectedNotificationIds = [];
        }
        await this.refreshCoreData(true);
        const labels = {
          mark_read: `Marked ${res.affected} read`,
          mark_unread: `Marked ${res.affected} unread`,
          mark_important: `Marked ${res.affected} important (AI filter trained)`,
          mark_not_important: `Marked ${res.affected} not important (AI filter trained)`,
          delete: `Deleted ${res.affected} item(s)`,
          add_to_tasks: `Added ${res.tasks_created ?? res.created_tasks ?? res.affected} task(s)`,
        };
        this.notify(labels[action] || "Updated inbox");
      } catch (err) {
        this.notify(err.message || "Bulk action failed", "error");
      }
    },

    async toggleRead(notif) {
      try {
        const updated = await this.apiFetch(`/api/v1/notifications/${notif.id}`, {
          method: "PATCH",
          body: JSON.stringify({ read_state: !notif.read_state }),
        });
        const idx = this.notifications.findIndex((n) => n.id === notif.id);
        if (idx !== -1) this.notifications[idx] = updated;
        await this.refreshCoreData(true);
      } catch (_e) {}
    },

    // Method alias for consistency
    async toggleNotificationRead(notif) {
      return this.toggleRead(notif);
    },

    async dismissNotification(id) {
      try {
        await this.apiFetch(`/api/v1/notifications/${id}`, { method: "DELETE" });
        this.notifications = this.notifications.filter((n) => n.id !== id);
        this.selectedNotificationIds = this.selectedNotificationIds.filter((x) => x !== id);
        await this.refreshCoreData(true);
      } catch (_e) {}
    },

    async promoteNotificationToTask(notif) {
      try {
        await this.apiFetch("/api/v1/tasks", {
          method: "POST",
          body: JSON.stringify({
            title: `${notif.source_app}: ${notif.title}`,
            category: "General",
            priority: 2,
            source_notification_id: notif.id,
          }),
        });
        await this.refreshCoreData(true);
        this.notify("Added to Tasks");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async convertNotificationToTask(notif) {
      return this.promoteNotificationToTask(notif);
    },

    get filteredTasks() {
      return this.taskFilter === "focus" ? this.tasks.filter((t) => !t.completed) : this.tasks;
    },

    get taskCompletionPercent() {
      if (!this.tasks.length) return 100;
      const done = this.tasks.filter((t) => t.completed).length;
      return Math.round((done / this.tasks.length) * 100);
    },

    async submitNewTask() {
      const cleanTitle = (this.newTask.title || "").trim();
      if (!cleanTitle) return;
      const minsVal = Number(this.newTask.estimated_minutes);
      try {
        await this.apiFetch("/api/v1/tasks", {
          method: "POST",
          body: JSON.stringify({
            title: cleanTitle,
            due_date: this.newTask.due_date || null,
            category: this.newTask.category || "General",
            priority: String(this.newTask.priority || "normal").toLowerCase(),
            estimated_minutes: Number.isFinite(minsVal) && minsVal >= 2 ? Math.round(minsVal) : null,
          }),
        });
        this.newTask.title = "";
        this.newTask.due_date = "";
        this.newTask.estimated_minutes = "";
        this.newTask.priority = "normal";
        await this.refreshCoreData(true);
        this.notify("Task added");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    taskPriorityPillClass(priority) {
      const p = String(priority || "normal").toLowerCase();
      if (p === "critical") return "prio-pill-critical";
      if (p === "high") return "prio-pill-high";
      if (p === "low") return "prio-pill-low";
      return "prio-pill-normal";
    },

    async cycleTaskPriority(task) {
      if (!task) return;
      const order = ["critical", "high", "normal", "low"];
      const cur = order.indexOf(String(task.priority || "normal").toLowerCase());
      const nextPrio = order[(cur + 1) % order.length];
      try {
        await this.apiFetch(`/api/v1/tasks/${task.id}`, {
          method: "PATCH",
          body: JSON.stringify({ priority: nextPrio }),
        });
        await this.refreshCoreData(true);
        this.notify(`Priority → ${nextPrio}`);
      } catch (err) {
        this.notify(err.message || "Could not update priority", "error");
      }
    },

    async persistTaskOrder(orderedIds) {
      try {
        const res = await this.apiFetch("/api/v1/tasks/reorder", {
          method: "POST",
          body: JSON.stringify({ ordered_ids: orderedIds }),
        });
        if (res && Array.isArray(res.tasks)) {
          this.tasks = res.tasks;
        } else {
          await this.refreshCoreData(true);
        }
      } catch (err) {
        this.notify(err.message || "Reorder failed", "error");
      }
    },

    async moveTaskOrder(task, delta) {
      if (!task) return;
      const list = [...this.filteredTasks];
      const idx = list.findIndex((t) => t.id === task.id);
      const targetIdx = idx + delta;
      if (idx < 0 || targetIdx < 0 || targetIdx >= list.length) return;
      const target = list[targetIdx];
      // Ensure swapped items share priority tier if dragging/moving across tiers, or reorder within list
      if (target.priority !== task.priority) {
        await this.apiFetch(`/api/v1/tasks/${task.id}`, {
          method: "PATCH",
          body: JSON.stringify({ priority: target.priority }),
        });
      }
      const [moved] = list.splice(idx, 1);
      list.splice(targetIdx, 0, moved);
      const otherIds = this.tasks.filter((t) => !list.some((x) => x.id === t.id)).map((t) => t.id);
      await this.persistTaskOrder([...list.map((t) => t.id), ...otherIds]);
    },

    onTaskDragStart(ev, task) {
      this.draggingTaskId = task.id;
      if (ev.dataTransfer) {
        ev.dataTransfer.effectAllowed = "move";
        try { ev.dataTransfer.setData("text/plain", String(task.id)); } catch (_e) {}
      }
    },

    onTaskDragOver(ev, task) {
      ev.preventDefault();
      if (this.draggingTaskId && this.draggingTaskId !== task.id) {
        this.dragOverTaskId = task.id;
      }
    },

    async onTaskDrop(ev, targetTask) {
      ev.preventDefault();
      const srcId = this.draggingTaskId;
      this.draggingTaskId = null;
      this.dragOverTaskId = null;
      if (!srcId || !targetTask || srcId === targetTask.id) return;

      const list = [...this.filteredTasks];
      const srcIdx = list.findIndex((t) => t.id === srcId);
      const dstIdx = list.findIndex((t) => t.id === targetTask.id);
      if (srcIdx < 0 || dstIdx < 0) return;

      const srcTask = list[srcIdx];
      if (srcTask.priority !== targetTask.priority) {
        await this.apiFetch(`/api/v1/tasks/${srcTask.id}`, {
          method: "PATCH",
          body: JSON.stringify({ priority: targetTask.priority }),
        });
      }
      list.splice(srcIdx, 1);
      list.splice(dstIdx, 0, srcTask);
      const otherIds = this.tasks.filter((t) => !list.some((x) => x.id === t.id)).map((t) => t.id);
      await this.persistTaskOrder([...list.map((t) => t.id), ...otherIds]);
    },

    onTaskDragEnd() {
      this.draggingTaskId = null;
      this.dragOverTaskId = null;
    },

    async syncTasksFromSources() {
      if (this.taskSyncingSources) return;
      this.taskSyncingSources = true;
      try {
        const res = await this.apiFetch("/api/v1/tasks/sync-sources", {
          method: "POST",
          body: JSON.stringify({ include_calendar: true, include_inbox: true, days_ahead: 14 }),
        });
        await this.refreshCoreData(true);
        if (res && res.created_count > 0) {
          this.notify(`Added ${res.created_count} task(s) (${res.from_calendar || 0} Schoology/Cal, ${res.from_inbox || 0} Inbox)`);
        } else {
          this.notify(`Up to date • ${res?.skipped_duplicates || 0} existing item(s) already in Tasks`);
        }
      } catch (err) {
        this.notify(err.message || "Could not sync sources", "error");
      } finally {
        this.taskSyncingSources = false;
      }
    },

    async submitTaskAiExtract() {
      const raw = (this.taskAiRawText || "").trim();
      if (!raw || this.taskAiExtracting) return;
      this.taskAiExtracting = true;
      try {
        const res = await this.apiFetch("/api/v1/tasks/ai-extract", {
          method: "POST",
          body: JSON.stringify({ text: raw, source_name: "AI Note Extract" }),
        });
        this.taskAiRawText = "";
        this.taskAiModalOpen = false;
        await this.refreshCoreData(true);
        if (res && res.created_count > 0) {
          this.notify(`AI extracted ${res.created_count} task(s) with smart durations`);
        } else {
          this.notify(`No new tasks (${res?.skipped_duplicates || 0} duplicate(s) skipped)`);
        }
      } catch (err) {
        this.notify(err.message || "AI task extraction failed", "error");
      } finally {
        this.taskAiExtracting = false;
      }
    },

    async toggleTaskComplete(task) {
      try {
        const updated = await this.apiFetch(`/api/v1/tasks/${task.id}`, {
          method: "PATCH",
          body: JSON.stringify({ completed: !task.completed }),
        });
        const idx = this.tasks.findIndex((t) => t.id === task.id);
        if (idx !== -1) this.tasks[idx] = updated;
        await Promise.all([this.refreshCoreData(true), this.loadFocusGuardStatus(true)]);
      } catch (_e) {}
    },

    async toggleTask(task) {
      return this.toggleTaskComplete(task);
    },

    async removeTask(id) {
      try {
        await this.apiFetch(`/api/v1/tasks/${id}`, { method: "DELETE" });
        this.tasks = this.tasks.filter((t) => t.id !== id);
        await this.refreshCoreData(true);
      } catch (_e) {}
    },

    async deleteTask(id) {
      return this.removeTask(id);
    },

    // =========================================================================
    // Calendar Engine (Month 7-col grid, Week, Day, Recurring, .ics)
    // =========================================================================
    parseIsoDate(iso) {
      const [y, m, d] = iso.split("-").map(Number);
      return new Date(y, m - 1, d);
    },

    addDaysIso(iso, delta) {
      const dt = this.parseIsoDate(iso);
      dt.setDate(dt.getDate() + delta);
      return this.localDateIso(dt);
    },

    mondayOfWeekIso(iso) {
      const dt = this.parseIsoDate(iso);
      const day = dt.getDay();
      const diff = day === 0 ? -6 : 1 - day;
      dt.setDate(dt.getDate() + diff);
      return this.localDateIso(dt);
    },

    get calendarHeaderLabel() {
      const dt = this.parseIsoDate(this.calendarCursor);
      if (this.calendarMode === "month") {
        return dt.toLocaleDateString([], { month: "long", year: "numeric" });
      }
      if (this.calendarMode === "day") {
        return dt.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
      }
      const mon = this.parseIsoDate(this.mondayOfWeekIso(this.calendarCursor));
      const sun = this.parseIsoDate(this.addDaysIso(this.mondayOfWeekIso(this.calendarCursor), 6));
      return `${mon.toLocaleDateString([], { month: "short", day: "numeric" })} – ${sun.toLocaleDateString([], { month: "short", day: "numeric" })}`;
    },

    computeVisibleBounds() {
      const dt = this.parseIsoDate(this.calendarCursor);
      const firstOfMonth = new Date(dt.getFullYear(), dt.getMonth(), 1);
      const startIso = this.mondayOfWeekIso(this.localDateIso(firstOfMonth));
      const endIso = this.addDaysIso(startIso, 41);
      return { startIso, endIso };
    },

    async refreshCalendarRange() {
      const { startIso, endIso } = this.computeVisibleBounds();
      this.calendarRangeStart = startIso;
      this.calendarRangeEnd = endIso;
      try {
        const [events, freeRes] = await Promise.all([
          this.apiFetch(`/api/v1/calendar/events?start_date=${startIso}&end_date=${endIso}`),
          this.apiFetch(`/api/v1/calendar/free-slots?target_date=${this.calendarCursor}`),
        ]);
        this.calendarEvents = Array.isArray(events) ? events : [];
        this.calendarFreeWindows = (freeRes && (freeRes.windows || freeRes.free_windows)) || [];
      } catch (_e) {}
    },

    eventsForDate(dateIso) {
      return this.calendarEvents
        .filter((ev) => ev.start_at.slice(0, 10) <= dateIso && ev.end_at.slice(0, 10) >= dateIso)
        .sort((a, b) => a.start_at.localeCompare(b.start_at));
    },

    dueTasksForDate(dateIso) {
      return this.tasks.filter((t) => !t.completed && t.due_date === dateIso);
    },

    get calendarWeekDays() {
      const mon = this.mondayOfWeekIso(this.calendarCursor);
      const days = [];
      for (let i = 0; i < 7; i++) {
        const iso = this.addDaysIso(mon, i);
        const dt = this.parseIsoDate(iso);
        days.push({
          date: iso,
          shortName: dt.toLocaleDateString([], { weekday: "short" }),
          dayNumber: dt.getDate(),
          isToday: iso === this.todayIso,
          events: this.eventsForDate(iso),
          dueTasks: this.dueTasksForDate(iso),
        });
      }
      return days;
    },

    get calendarMonthCells() {
      const dt = this.parseIsoDate(this.calendarCursor);
      const curMonth = dt.getMonth();
      const firstOfMonth = new Date(dt.getFullYear(), curMonth, 1);
      const startIso = this.mondayOfWeekIso(this.localDateIso(firstOfMonth));
      const cells = [];
      for (let i = 0; i < 42; i++) {
        const iso = this.addDaysIso(startIso, i);
        const cdt = this.parseIsoDate(iso);
        cells.push({
          date: iso,
          dayNumber: cdt.getDate(),
          inCurrentMonth: cdt.getMonth() === curMonth,
          isToday: iso === this.todayIso,
          events: this.eventsForDate(iso),
          dueTasks: this.dueTasksForDate(iso),
        });
      }
      return cells;
    },

    get selectedDayEvents() {
      return this.eventsForDate(this.calendarCursor);
    },

    stepCalendar(dir) {
      if (this.calendarMode === "day") {
        this.calendarCursor = this.addDaysIso(this.calendarCursor, dir);
      } else if (this.calendarMode === "week") {
        this.calendarCursor = this.addDaysIso(this.calendarCursor, dir * 7);
      } else {
        const dt = this.parseIsoDate(this.calendarCursor);
        dt.setMonth(dt.getMonth() + dir);
        this.calendarCursor = this.localDateIso(dt);
      }
      this.refreshCalendarRange();
    },

    jumpCalendarToday() {
      this.calendarCursor = this.todayIso;
      this.refreshCalendarRange();
    },

    selectCalendarDay(dateIso, switchToDay = false) {
      this.calendarCursor = dateIso;
      this.calendarDayInspectorOpen = true;
      if (switchToDay) this.calendarMode = "day";
      this.refreshCalendarRange();
    },

    async submitCalendarQuickAdd() {
      const title = (this.calendarQuickAdd.title || "").trim();
      if (!title) return;
      const d = this.calendarCursor || this.todayIso;
      const allDay = Boolean(this.calendarQuickAdd.all_day);
      const startAt = allDay ? `${d}T00:00` : `${d}T${this.calendarQuickAdd.start_hhmm || "16:00"}`;
      const endAt = allDay ? `${d}T23:59` : `${d}T${this.calendarQuickAdd.end_hhmm || "17:00"}`;
      try {
        await this.apiFetch("/api/v1/calendar/events", {
          method: "POST",
          body: JSON.stringify({
            title,
            start_at: startAt,
            end_at: endAt,
            all_day: allDay,
            category: "General",
            color: this.calendarQuickAdd.color || "emerald",
          }),
        });
        this.calendarQuickAdd.title = "";
        await this.refreshCalendarRange();
        this.notify(`Added "${title}" on ${d}`);
      } catch (err) {
        this.notify(err.message || "Could not add event", "error");
      }
    },

    formatEventTimeSpan(ev) {
      if (ev.all_day) return "All day";
      return `${ev.start_at.slice(11, 16)}–${ev.end_at.slice(11, 16)}`;
    },

    eventColorClasses(color) {
      const map = {
        sky: "bg-sky-500/15 border-sky-500/35 text-sky-200",
        violet: "bg-violet-500/15 border-violet-500/35 text-violet-200",
        amber: "badge-accent",
        emerald: "bg-emerald-500/15 border-emerald-500/35 text-emerald-200",
        rose: "bg-rose-500/15 border-rose-500/35 text-rose-200",
        teal: "bg-teal-500/15 border-teal-500/35 text-teal-200",
      };
      return map[color] || map.sky;
    },

    openNewEventModal(dateIso, startTime = "15:00", endTime = "16:00") {
      this.eventEditingId = null;
      const targetDate = dateIso || this.calendarCursor || this.todayIso;
      const dt = this.parseIsoDate(targetDate);
      const jsDay = dt.getDay();
      const monZeroIdx = jsDay === 0 ? 6 : jsDay - 1;
      this.eventForm = {
        title: "",
        date: targetDate,
        all_day: false,
        start_time: startTime,
        end_time: endTime,
        category: "Event",
        color: "sky",
        location: "",
        notes: "",
        repeat_enabled: false,
        repeat_days: [monZeroIdx],
        repeat_weeks: 4,
      };
      this.eventModalOpen = true;
    },

    openEditEventModal(ev) {
      this.eventEditingId = ev.id;
      this.eventForm = {
        title: ev.title,
        date: ev.start_at.slice(0, 10),
        all_day: ev.all_day,
        start_time: ev.start_at.slice(11, 16) || "09:00",
        end_time: ev.end_at.slice(11, 16) || "10:00",
        category: ev.category || "Event",
        color: ev.color || "sky",
        location: ev.location || "",
        notes: ev.notes || "",
        repeat_enabled: false,
        repeat_days: [],
        repeat_weeks: 4,
      };
      this.eventModalOpen = true;
    },

    onEventCategoryChange() {
      const defaults = {
        Event: "sky",
        Work: "violet",
        Class: "violet",
        "Focus Block": "amber",
        "Goal Block": "emerald",
        Personal: "teal",
      };
      if (defaults[this.eventForm.category]) {
        this.eventForm.color = defaults[this.eventForm.category];
      }
    },

    toggleRepeatWeekday(dayIdx) {
      const cur = Array.isArray(this.eventForm.repeat_days) ? [...this.eventForm.repeat_days] : [];
      const pos = cur.indexOf(dayIdx);
      if (pos >= 0) cur.splice(pos, 1);
      else cur.push(dayIdx);
      this.eventForm.repeat_days = cur.sort((a, b) => a - b);
    },

    async saveEventForm() {
      const f = this.eventForm;
      const startAt = f.all_day ? `${f.date}T00:00` : `${f.date}T${f.start_time}`;
      const endAt = f.all_day ? `${f.date}T23:59` : `${f.date}T${f.end_time}`;
      const payload = {
        title: f.title.trim(),
        start_at: startAt,
        end_at: endAt,
        all_day: f.all_day,
        category: f.category,
        color: f.color,
        location: f.location.trim() || null,
        notes: f.notes.trim() || null,
      };
      try {
        if (this.eventEditingId) {
          await this.apiFetch(`/api/v1/calendar/events/${this.eventEditingId}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
          this.notify("Event updated");
        } else if (f.repeat_enabled && f.repeat_days && f.repeat_days.length > 0) {
          const created = await this.apiFetch("/api/v1/calendar/events", {
            method: "POST",
            body: JSON.stringify({
              ...payload,
              repeat_days: f.repeat_days,
              repeat_weeks: Number(f.repeat_weeks) || 4,
            }),
          });
          this.notify(`Scheduled ${Array.isArray(created) ? created.length : 1} recurring blocks`);
        } else {
          await this.apiFetch("/api/v1/calendar/events", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          this.notify("Event added");
        }
        this.eventModalOpen = false;
        await Promise.all([this.refreshCalendarRange(), this.refreshCoreData(true)]);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async deleteCalendarEvent(eventId) {
      try {
        await this.apiFetch(`/api/v1/calendar/events/${eventId}`, { method: "DELETE" });
        this.eventModalOpen = false;
        await Promise.all([this.refreshCalendarRange(), this.refreshCoreData(true)]);
        this.notify("Event deleted");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async runAICoordinateDay(dateIso) {
      this.calendarCoordinating = true;
      try {
        const res = await this.apiFetch("/api/v1/calendar/coordinate", {
          method: "POST",
          body: JSON.stringify({ target_date: dateIso || this.todayIso, include_tasks: true, include_goals: true }),
        });
        await Promise.all([this.refreshCalendarRange(), this.refreshCoreData(true)]);
        this.notify(`Coordinated ${res.created_count} focus block(s)`);
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.calendarCoordinating = false;
      }
    },

    async loadCalendarFeeds() {
      try {
        this.calendarFeeds = await this.apiFetch("/api/v1/calendar/feeds");
      } catch (_e) {}
    },

    async subscribeCalendarFeed() {
      if (!this.newFeed.name.trim() || !this.newFeed.url.trim()) return;
      this.feedSubmitting = true;
      try {
        await this.apiFetch("/api/v1/calendar/feeds", {
          method: "POST",
          body: JSON.stringify(this.newFeed),
        });
        this.newFeed = { name: "", url: "", color: "violet" };
        await Promise.all([this.loadCalendarFeeds(), this.refreshCalendarRange()]);
        this.notify("Subscribed & synced .ics feed");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.feedSubmitting = false;
      }
    },

    handleICSFileSelected(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      this.icsImportSourceName = file.name.replace(/\.ics$/i, "");
      const reader = new FileReader();
      reader.onload = () => {
        this.icsImportText = String(reader.result || "");
      };
      reader.readAsText(file);
    },

    async submitICSImport() {
      if (!this.icsImportText.trim()) return;
      try {
        const res = await this.apiFetch("/api/v1/calendar/import-ics", {
          method: "POST",
          body: JSON.stringify({
            ics_content: this.icsImportText,
            source_name: this.icsImportSourceName || "Imported .ics",
            color: "teal",
          }),
        });
        this.icsImportText = "";
        this.calSyncModalOpen = false;
        await this.refreshCalendarRange();
        this.notify(`Imported ${(res.added || 0) + (res.updated || 0)} events`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    get exportICSUrl() {
      return this.decorateAssetUrl(`${window.location.origin}/api/v1/calendar/export.ics`);
    },

    async copyExportICSUrl() {
      try {
        await navigator.clipboard.writeText(this.exportICSUrl);
        this.notify("Copied .ics URL");
      } catch (_e) {}
    },

    // =========================================================================
    // Goals
    // =========================================================================
    async loadGoals() {
      try {
        this.goals = await this.apiFetch("/api/v1/goals");
      } catch (_e) {}
    },

    openNewGoalModal() {
      this.goalForm = {
        title: "",
        category: "General",
        target_date: "",
        minutes_per_day: 25,
        preferred_slot: "afternoon",
        daily_habit: "",
        milestones: [],
      };
      this.goalModalOpen = true;
    },

    async previewGoalBreakdown() {
      if (!this.goalForm.title.trim()) return;
      try {
        const plan = await this.apiFetch("/api/v1/goals/breakdown", {
          method: "POST",
          body: JSON.stringify({
            title: this.goalForm.title.trim(),
            category: this.goalForm.category,
            minutes_per_day: Number(this.goalForm.minutes_per_day) || 25,
          }),
        });
        this.goalForm.daily_habit = plan.daily_habit || this.goalForm.daily_habit;
        this.goalForm.milestones = plan.milestones || [];
        this.notify("Generated habit & milestones");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveNewGoal() {
      if (!this.goalForm.title.trim()) return;
      try {
        await this.apiFetch("/api/v1/goals", {
          method: "POST",
          body: JSON.stringify({
            ...this.goalForm,
            target_date: this.goalForm.target_date || null,
            minutes_per_day: Number(this.goalForm.minutes_per_day) || 25,
          }),
        });
        this.goalModalOpen = false;
        await Promise.all([this.loadGoals(), this.refreshCoreData(true)]);
        this.notify("Goal saved");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async checkinGoalToday(goal) {
      try {
        await this.apiFetch(`/api/v1/goals/${goal.id}/checkin`, { method: "POST", body: JSON.stringify({ minutes_logged: goal.minutes_per_day }) });
        await Promise.all([this.loadGoals(), this.loadFocusGuardStatus(true)]);
        this.notify(`Checked in: ${goal.title}`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async scheduleGoalBlockToday(goal) {
      try {
        const res = await this.apiFetch("/api/v1/calendar/coordinate", {
          method: "POST",
          body: JSON.stringify({ target_date: this.todayIso, include_tasks: false, include_goals: true }),
        });
        await this.refreshCalendarRange();
        this.notify(`Scheduled ${res.created_count} goal block(s)`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async toggleGoalMilestone(goal, msIdx) {
      const updated = (goal.milestones || []).map((m, i) => (i === msIdx ? { ...m, done: !m.done } : m));
      const doneCount = updated.filter((m) => m.done).length;
      const pct = updated.length ? Math.round((doneCount / updated.length) * 100) : goal.progress_percent;
      try {
        await this.apiFetch(`/api/v1/goals/${goal.id}`, {
          method: "PATCH",
          body: JSON.stringify({ milestones: updated, progress_percent: pct }),
        });
        await this.loadGoals();
      } catch (_e) {}
    },

    async removeGoal(goalId) {
      try {
        await this.apiFetch(`/api/v1/goals/${goalId}`, { method: "DELETE" });
        await Promise.all([this.loadGoals(), this.refreshCoreData(true)]);
      } catch (_e) {}
    },

    // =========================================================================
    // Hourly AI Cron & Bedtime Priority Engine + YouTube Focus Guard v7.0
    // =========================================================================
    async loadCronStatus(silent = false) {
      try {
        const res = await this.apiFetch("/api/v1/cron/status");
        if (res) this.cronState = { ...this.cronState, ...res };
      } catch (err) {
        if (!silent && err.message !== "Session locked") this.notify(err.message, "error");
      }
    },

    async runCronNow() {
      this.cronRunning = true;
      try {
        const res = await this.apiFetch("/api/v1/cron/run", {
          method: "POST",
          body: JSON.stringify({ trigger: "manual" }),
        });
        if (res) this.cronState = { ...this.cronState, ...res };
        await Promise.all([
          this.refreshCoreData(true),
          this.refreshCalendarRange(),
          this.loadFocusGuardStatus(true),
        ]);
        this.notify("Schedule & priorities updated before bedtime");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.cronRunning = false;
      }
    },

    async saveCronSettings() {
      try {
        const res = await this.apiFetch("/api/v1/cron/settings", {
          method: "PATCH",
          body: JSON.stringify({
            enabled: Boolean(this.cronState.settings.enabled),
            interval_minutes: Number(this.cronState.settings.interval_minutes) || 60,
            bedtime_hhmm: this.cronState.settings.bedtime_hhmm || "22:00",
            wake_hhmm: this.cronState.settings.wake_hhmm || "07:00",
          }),
        });
        if (res) this.cronState = { ...this.cronState, ...res };
        await this.loadFocusGuardStatus(true);
        this.notify(`Bedtime set to ${this.cronState.budget.bedtime_label}`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    get extensionDownloadUrl() {
      return this.decorateAssetUrl(`${window.location.origin}/api/v1/focus-guard/extension.zip?origin=${encodeURIComponent(window.location.origin)}`);
    },

    get userscriptDownloadUrl() {
      return this.decorateAssetUrl(`${window.location.origin}/api/v1/focus-guard/youtube-guard.user.js?origin=${encodeURIComponent(window.location.origin)}`);
    },

    async loadFocusGuardStatus(silent = false) {
      try {
        this.focusGuard = await this.apiFetch("/api/v1/focus-guard/status");
      } catch (err) {
        if (!silent && err.message !== "Session locked") this.notify(err.message, "error");
      }
    },

    async saveFocusGuardSettings() {
      try {
        this.focusGuard = await this.apiFetch("/api/v1/focus-guard/settings", {
          method: "PATCH",
          body: JSON.stringify({
            enabled: Boolean(this.focusGuard.enabled),
            earned_break_limit_minutes: Number(this.focusGuard.earned_break_limit_minutes) || 20,
            hard_limit_minutes: Number(this.focusGuard.hard_limit_minutes) || 45,
            long_video_trigger_minutes: Number(this.focusGuard.long_video_trigger_minutes) || 10,
            study_allowance_minutes: Number(this.focusGuard.study_allowance_minutes) || 20,
            quick_break_minutes: Number(this.focusGuard.quick_break_minutes) || 10,
            nudge_on_shorts_immediately: Boolean(this.focusGuard.nudge_on_shorts_immediately),
          }),
        });
        this.notify("Focus Guard v7 rules saved");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async simulateYoutubeScenario(scenario) {
      this.justifyFeedback = "";
      this.justifyDraft = "";
      const map = {
        short_video_end: {
          url: "https://www.youtube.com/watch?v=shortvid1",
          video_title: "Top 10 Gaming Stream Highlights",
          channel: "Highlights Daily",
          is_short: false,
          duration_seconds: 720,
          current_time_seconds: 719,
          video_ended: true,
          override_session_minutes: 12,
          override_today_minutes: 15,
        },
        long_video_10m: {
          url: "https://www.youtube.com/watch?v=longvid1",
          video_title: "2-Hour Retrospective Documentary",
          channel: "Deep Dive Essays",
          is_short: false,
          duration_seconds: 7200,
          current_time_seconds: 605,
          video_ended: false,
          override_session_minutes: 10,
          override_today_minutes: 22,
        },
        hard_cap_exhausted: {
          url: "https://www.youtube.com/watch?v=overcap1",
          video_title: "Funny Clips Compilation",
          channel: "Viral Clips",
          is_short: false,
          duration_seconds: 900,
          current_time_seconds: 120,
          video_ended: false,
          override_session_minutes: 50,
          override_today_minutes: 55,
        },
        shorts_trap: {
          url: "https://www.youtube.com/shorts/shorttrap1",
          video_title: "Quick Viral Short",
          channel: "Shorts Feed",
          is_short: true,
          duration_seconds: 45,
          current_time_seconds: 15,
          override_session_minutes: 3,
          override_today_minutes: 12,
        },
      };
      const p = map[scenario] || map.short_video_end;
      try {
        this.focusGuard = await this.apiFetch("/api/v1/focus-guard/heartbeat", {
          method: "POST",
          body: JSON.stringify({ ...p, playing: true, clear_allowance: true }),
        });
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async submitFocusJustification() {
      const txt = (this.justifyDraft || "").trim();
      if (!txt) {
        this.justifyApproved = false;
        this.justifyFeedback = "Type concrete reasons first — or click Close YouTube & Work.";
        return;
      }
      this.justifyLoading = true;
      this.justifyFeedback = "✦ Node AI is evaluating your reasoning while playback continues…";
      try {
        const res = await this.apiFetch("/api/v1/focus-guard/justify", {
          method: "POST",
          body: JSON.stringify({
            reasoning: txt,
            video_title: this.focusGuard.session?.video_title || "YouTube Video",
            channel: this.focusGuard.session?.channel || "",
            duration_seconds: this.focusGuard.session?.duration_seconds || 600,
            url: this.focusGuard.session?.url || "https://www.youtube.com",
          }),
        });
        this.focusGuard = res;
        const jr = res && res.justification_result;
        if (jr && jr.approved) {
          this.justifyApproved = true;
          this.justifyFeedback = "";
          this.justifyDraft = "";
          this.notify(`Approved (+${jr.granted_minutes}m): ${jr.explanation}`);
        } else {
          this.justifyApproved = false;
          this.justifyFeedback = (jr && jr.explanation) || "Reasoning insufficient — force-closing tab.";
          this.notify("Reasoning rejected — tab force-closed", "error");
        }
        await this.refreshCoreData(true);
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.justifyLoading = false;
      }
    },

    async respondFocusGuard(decision) {
      try {
        await this.apiFetch("/api/v1/focus-guard/respond", {
          method: "POST",
          body: JSON.stringify({ action: decision }),
        });
        await this.loadFocusGuardStatus(true);
        if (decision === "stop_watching") {
          const top = this.focusGuard.workload?.top_items?.[0];
          if (top) this.startFocusTimer(top.minutes || 25, top.title);
        }
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    startFocusTimer(minutes = 25, label = "Focus Session", meta = {}) {
      if (this.focusTimer.intervalId) clearInterval(this.focusTimer.intervalId);
      const total = Math.max(60, Math.round(Number(minutes) * 60));
      this.focusTimer = {
        visible: true,
        running: true,
        label,
        durationSeconds: total,
        remainingSeconds: total,
        startedAtMs: Date.now(),
        taskId: meta.taskId || null,
        goalId: meta.goalId || null,
        intervalId: setInterval(() => this.tickFocusTimer(), 1000),
      };
    },

    tickFocusTimer() {
      if (!this.focusTimer.running) return;
      if (this.focusTimer.remainingSeconds > 0) {
        this.focusTimer.remainingSeconds -= 1;
      } else {
        this.focusTimer.running = false;
        this.notify(`Focus timer complete: ${this.focusTimer.label}`);
      }
    },

    toggleFocusTimerPause() {
      this.focusTimer.running = !this.focusTimer.running;
    },

    async completeFocusSession() {
      const elapsedSec = Math.max(60, this.focusTimer.durationSeconds - this.focusTimer.remainingSeconds);
      const actualMins = Math.max(1, Math.round(elapsedSec / 60));
      try {
        await this.apiFetch("/api/v1/ai/time-estimates/learn", {
          method: "POST",
          body: JSON.stringify({
            archetype: "assignment",
            minutes: actualMins,
            feedback: `Completed "${this.focusTimer.label}" in ${actualMins}m`,
          }),
        });
        if (this.focusTimer.taskId) {
          const t = this.tasks.find((x) => x.id === this.focusTimer.taskId);
          if (t && !t.completed) await this.toggleTaskComplete(t);
        }
        await this.loadMemoryFiles();
        this.notify(`Logged ${actualMins}m`);
      } catch (_e) {}
      this.closeFocusTimer();
    },

    closeFocusTimer() {
      if (this.focusTimer.intervalId) clearInterval(this.focusTimer.intervalId);
      this.focusTimer.visible = false;
      this.focusTimer.running = false;
    },

    get focusTimerFormatted() {
      const s = Math.max(0, this.focusTimer.remainingSeconds);
      return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    },

    get focusTimerProgressPct() {
      if (!this.focusTimer.durationSeconds) return 0;
      return Math.min(100, Math.round(((this.focusTimer.durationSeconds - this.focusTimer.remainingSeconds) / this.focusTimer.durationSeconds) * 100));
    },

    // =========================================================================
    // Briefing, Notebook, & Uploads
    // =========================================================================
    async loadLatestBriefing() {
      try {
        const b = await this.apiFetch("/api/v1/ai/latest");
        const md = b && (b.markdown || b.content_markdown || b.content_md);
        if (md) {
          this.briefing = { ...b, renderedHtml: this.renderMarkdown(md) };
        }
      } catch (_e) {}
    },

    async generateDailyPlan() {
      this.aiLoading = true;
      try {
        const b = await this.apiFetch("/api/v1/ai/summarize", {
          method: "POST",
          body: JSON.stringify({ focus_note: this.aiFocusNote.trim() || null }),
        });
        const md = b && (b.markdown || b.content_markdown || b.content_md);
        this.briefing = { ...b, renderedHtml: this.renderMarkdown(md || "") };
        this.aiFocusNote = "";
        await this.refreshCoreData(true);
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.aiLoading = false;
      }
    },

    async executeActionChip(act) {
      if (!act || !act.type) return;
      if (act.type === "navigate") {
        this.openWindow(act.view || "calendar", { filter: act.filter || null });
      } else if (act.type === "start_timer") {
        this.startFocusTimer(act.minutes || 25, act.title || "Focus Session", {
          taskId: act.task_id || null,
          goalId: act.goal_id || null,
        });
      } else if (act.type === "coordinate_day") {
        await this.runAICoordinateDay(act.date || this.todayIso);
        this.openWindow("calendar");
      }
    },

    async loadMemoryFiles() {
      try {
        const [mem, nbList] = await Promise.all([
          this.apiFetch("/api/v1/ai/memory"),
          this.apiFetch("/api/v1/ai/notebook"),
        ]);
        this.memoryDoc = mem;
        this.notebookFiles = (nbList && nbList.files) || [];
        if (nbList && nbList.time_estimates) {
          this.timeEstimates = nbList.time_estimates;
        }
        await this.selectNotebookFile(this.activeNotebookSlug || "scratchpad");
      } catch (_e) {}
    },

    async selectNotebookFile(slug) {
      this.activeNotebookSlug = slug;
      try {
        const doc = await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(slug)}`);
        this.notesDoc = doc;
      } catch (_e) {}
    },

    async createNotebookFile() {
      const name = (this.newNotebookName || "").trim();
      if (!name) return;
      try {
        const doc = await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(name)}`, {
          method: "PUT",
          body: JSON.stringify({ content: `# ${name}\n\n- ` }),
        });
        this.newNotebookName = "";
        await this.loadMemoryFiles();
        this.activeNotebookSlug = doc.slug;
        this.notesDoc = doc;
        this.notesEditMode = true;
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async deleteNotebookFile(slug) {
      try {
        await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(slug)}`, { method: "DELETE" });
        this.activeNotebookSlug = "scratchpad";
        await this.loadMemoryFiles();
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveMemoryFile() {
      this.memorySaving = true;
      try {
        this.memoryDoc = await this.apiFetch("/api/v1/ai/memory", {
          method: "PUT",
          body: JSON.stringify({ content: this.memoryDoc.content }),
        });
        this.notify("Persona saved");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.memorySaving = false;
      }
    },

    async saveNotesFile() {
      try {
        this.notesDoc = await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(this.activeNotebookSlug)}`, {
          method: "PUT",
          body: JSON.stringify({ content: this.notesDoc.content }),
        });
        this.notesEditMode = false;
        await this.loadMemoryFiles();
        this.notify("Saved");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async appendQuickNote() {
      if (!this.quickNoteInput.trim()) return;
      try {
        this.notesDoc = await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(this.activeNotebookSlug)}/append`, {
          method: "POST",
          body: JSON.stringify({ text: this.quickNoteInput.trim(), category: "Quick Notes" }),
        });
        this.quickNoteInput = "";
        await this.loadMemoryFiles();
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    applyPersonaPreset(preset) {
      if (preset === "warm_secretary") {
        this.memoryDoc.content = `# Node — AI Persona & Rules\n\n## Tone & Style\n- Clear, concise, practical daily coordinator.\n- Confirm real database actions in 1-2 lines.\n`;
      } else {
        this.memoryDoc.content = `# Node — Concise Mode\n\n## Tone & Style\n- Ultra-concise bullet points only. Zero fluff.\n`;
      }
    },

    async loadUploads() {
      try {
        const res = await this.apiFetch("/api/v1/files");
        this.uploadedFiles = res.files || [];
        this.uploadsDir = res.uploads_dir || "/data/uploads";
      } catch (_e) {}
    },

    handleFileDrop(ev) {
      this.dragOverFiles = false;
      const files = ev.dataTransfer?.files;
      if (files && files.length) this.uploadFileList(files);
    },

    handleFileInputChange(ev, overrideMode = null) {
      const files = ev.target?.files;
      if (files && files.length) this.uploadFileList(files, overrideMode);
      if (ev.target) ev.target.value = "";
    },

    readFileAsBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const s = String(reader.result || "");
          const comma = s.indexOf(",");
          resolve(comma >= 0 ? s.slice(comma + 1) : s);
        };
        reader.onerror = () => reject(new Error("Could not read file"));
        reader.readAsDataURL(file);
      });
    },

    async uploadFileList(fileList, overrideMode = null) {
      const mode = overrideMode || this.uploadIngestMode || "store";
      this.uploadingFile = true;
      try {
        for (const file of Array.from(fileList)) {
          const b64 = await this.readFileAsBase64(file);
          const data = await this.apiFetch("/api/v1/files/upload", {
            method: "POST",
            body: JSON.stringify({
              filename: file.name,
              content_base64: b64,
              ingest_mode: mode === "wallpaper" ? "store" : mode,
            }),
          });
          if (mode === "wallpaper" && data.file?.kind === "image") {
            await this.loadWallpapers();
            this.activeWallpaperId = `upload:${data.file.filename}`;
            safeStorageSet("node_wallpaper", this.activeWallpaperId);
          }
        }
        await Promise.all([this.loadUploads(), this.loadWallpapers(), this.refreshCoreData(true), this.refreshCalendarRange(), this.loadMemoryFiles()]);
        this.notify("Upload complete");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.uploadingFile = false;
      }
    },

    async submitPastedFile() {
      if (!this.pasteContent.trim()) return;
      this.uploadingFile = true;
      try {
        const mode = this.uploadIngestMode || "store";
        const res = await this.apiFetch("/api/v1/files/upload", {
          method: "POST",
          body: JSON.stringify({
            filename: this.pasteFilename.trim() || "notes.md",
            text_content: this.pasteContent,
            ingest_mode: mode,
          }),
        });
        this.pasteContent = "";
        if (mode === "extract_tasks" && res?.file?.filename) {
          this.extractedTaskFileMap[res.file.filename] = true;
        }
        await Promise.all([this.loadUploads(), this.refreshCoreData(true), this.loadMemoryFiles()]);
        this.notify(mode === "extract_tasks" ? `Saved & extracted ${res?.ingest?.created_tasks ?? 0} task(s)` : "Saved file");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.uploadingFile = false;
      }
    },

    async runFileIngest(file, action) {
      if (!file || !file.filename) return;
      const busyKey = `${file.filename}:${action}`;
      if (this.ingestingFileMap[busyKey]) return;
      this.ingestingFileMap[busyKey] = true;
      try {
        const res = await this.apiFetch(`/api/v1/files/${encodeURIComponent(file.filename)}/ingest`, {
          method: "POST",
          body: JSON.stringify({ mode: action }),
        });
        if (action === "extract_tasks") {
          this.extractedTaskFileMap[file.filename] = true;
        }
        await Promise.all([this.refreshCoreData(true), this.refreshCalendarRange(), this.loadMemoryFiles()]);
        if (action === "extract_tasks") {
          const added = res?.tasks_created ?? res?.created_tasks ?? 0;
          const dup = res?.skipped_duplicates ?? 0;
          this.notify(
            added > 0
              ? `AI extracted ${added} task(s)${dup ? ` (${dup} already added)` : ""}`
              : `All tasks from ${file.filename} are already in Tasks (${dup} skipped)`
          );
        } else {
          this.notify("Processed file");
        }
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.ingestingFileMap[busyKey] = false;
      }
    },

    async setUploadedPhotoAsWallpaper(file) {
      await this.loadWallpapers();
      const id = `upload:${file.filename}`;
      this.activeWallpaperId = id;
      safeStorageSet("node_wallpaper", id);
      this.notify(`Wallpaper set: ${file.filename}`);
    },

    async deleteUploadedFile(file) {
      try {
        await this.apiFetch(`/api/v1/files/${encodeURIComponent(file.filename)}`, { method: "DELETE" });
        await Promise.all([this.loadUploads(), this.loadWallpapers()]);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    formatFileSize(bytes) {
      const n = Number(bytes) || 0;
      if (n < 1024) return `${n} B`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
      return `${(n / (1024 * 1024)).toFixed(2)} MB`;
    },

    // =========================================================================
    // Monitored Apps Management & Diagnostics
    // =========================================================================
    runtimeBadgeClass(status) {
      if (status === "warm") return "badge-accent";
      if (status === "needs_auth") return "bg-rose-500/15 text-rose-300 border-rose-500/35";
      return "studio-subcard text-muted-ink";
    },

    openNewAppModal(presetName = "") {
      this.appModalEditingSlug = null;
      this.aiAssistQuery = presetName || "";
      const key = String(presetName || "").toLowerCase();
      const lmsDefaults = {
        powerschool: { url: "https://powerschool.com/public/home.html", icon: "grades", category: "School", indicator_type: "grades_tracker", selectors: 'a[href*="scores.html"], td.bold' },
        schoology: { url: "https://app.schoology.com", icon: "grades", category: "School", indicator_type: "grades_tracker", selectors: ".upcoming-event, .s-edge-feed" },
        canvas: { url: "https://canvas.instructure.com", icon: "grades", category: "School", indicator_type: "grades_tracker", selectors: ".ic-DashboardCard, .PlannerItem" },
        "google classroom": { url: "https://classroom.google.com", icon: "grades", category: "School", indicator_type: "assignment_tracker", selectors: "[data-course-id], a[href*='/c/']" },
      };
      const matched = lmsDefaults[key] || null;
      this.appForm = {
        name: presetName || "",
        url: matched ? matched.url : "",
        icon: matched ? matched.icon : "globe",
        category: matched ? matched.category : "General",
        account_profile: "default",
        enabled: true,
        zero_knowledge: false,
        indicator_type: matched ? matched.indicator_type : "dom_badge",
        title_regex: "\\((\\d+)\\)",
        badge_selectors_text: matched ? matched.selectors : '[aria-label*="unread" i], .badge, .unread-count',
        inbox_categories_text: "",
        notes: "",
      };
      this.appModalOpen = true;
    },

    openEditAppModal(app) {
      this.appModalEditingSlug = app.slug;
      this.aiAssistQuery = "";
      this.appForm = {
        name: app.name,
        url: app.url,
        icon: app.icon || "globe",
        category: app.category || "General",
        account_profile: app.account_profile || "default",
        enabled: app.enabled !== false,
        zero_knowledge: Boolean(app.zero_knowledge),
        indicator_type: app.indicator_type || "dom_badge",
        title_regex: app.title_regex || "\\((\\d+)\\)",
        badge_selectors_text: (app.badge_selectors || []).join(", "),
        inbox_categories_text: (app.inbox_categories || []).join(", "),
        notes: app.notes || "",
      };
      this.appModalOpen = true;
    },

    openDuplicateAccountModal(app) {
      if (!app) return;
      const base = app.name.replace(/\s*\([^)]+\)\s*$/, "").trim();
      const count = this.monitoredApps.filter((a) => a.name.toLowerCase().startsWith(base.toLowerCase())).length + 1;
      this.duplicateSourceApp = app;
      this.duplicateAccountLabel = `${base} (${count})`;
      this.duplicateAccountProfile = `${app.slug.replace(/-\d+$/, "")}-${count}`;
      this.duplicateModalOpen = true;
    },

    async confirmDuplicateAccount(openStreamAfter = true) {
      if (!this.duplicateSourceApp) return;
      try {
        const res = await this.apiFetch(`/api/v1/apps/${encodeURIComponent(this.duplicateSourceApp.slug)}/duplicate`, {
          method: "POST",
          body: JSON.stringify({
            label: (this.duplicateAccountLabel || "").trim(),
            account_profile: (this.duplicateAccountProfile || "").trim(),
          }),
        });
        this.duplicateModalOpen = false;
        await this.refreshCoreData(true);
        const created = res && res.app;
        if (created) {
          this.notify(`Added isolated account: ${created.name} (profile: ${created.account_profile})`);
          if (openStreamAfter) {
            this.openStreamWindow(created);
          }
        }
      } catch (err) {
        this.notify(err.message || "Could not create isolated account", "error");
      }
    },

    // =========================================================================
    // Multi-LMS Grades Tracker (PowerSchool, Schoology, Canvas, Classroom)
    // =========================================================================
    get lmsPortalApps() {
      const lmsKeywords = ["powerschool", "schoology", "canvas", "instructure", "classroom.google", "google classroom", "infinitecampus", "skyward", "aeries"];
      return (this.monitoredApps || []).filter((a) => {
        if (!a) return false;
        if (a.indicator_type === "grades_tracker" || a.indicator_type === "assignment_tracker") return true;
        const hay = `${a.name || ""} ${a.url || ""}`.toLowerCase();
        return lmsKeywords.some((k) => hay.includes(k));
      });
    },

    get gradesPortalApp() {
      return this.lmsPortalApps[0] || null;
    },

    courseRowKey(course) {
      if (!course) return "";
      return String(course.course_key || course.id || course.course_name || "");
    },

    isCourseExpanded(course) {
      const k = this.courseRowKey(course);
      return Boolean(k && this.expandedCourseKeys[k]);
    },

    toggleCourseAssignments(course) {
      const k = this.courseRowKey(course);
      if (!k) return;
      this.expandedCourseKeys = {
        ...this.expandedCourseKeys,
        [k]: !this.expandedCourseKeys[k],
      };
    },

    getCourseAssignmentFilter(course) {
      const k = this.courseRowKey(course);
      return (k && this.courseAssignmentFilter[k]) || "all";
    },

    setCourseAssignmentFilter(course, mode) {
      const k = this.courseRowKey(course);
      if (!k) return;
      this.courseAssignmentFilter = {
        ...this.courseAssignmentFilter,
        [k]: mode || "all",
      };
    },

    filteredCourseAssignments(course) {
      const list = Array.isArray(course?.assignments) ? course.assignments : [];
      const mode = this.getCourseAssignmentFilter(course);
      if (mode === "flagged") {
        return list.filter(
          (a) => a.is_missing || a.is_late || a.is_incomplete || (typeof a.score_percent === "number" && a.score_percent < 80)
        );
      }
      return list;
    },

    openCourseGradesDetail(course) {
      if (course) {
        const k = this.courseRowKey(course);
        if (k) {
          this.expandedCourseKeys = { ...this.expandedCourseKeys, [k]: true };
        }
      }
      this.openWindow("grades");
    },

    async scanGradesNow(slug = null) {
      if (this.gradesScanning) return;
      this.gradesScanning = true;
      try {
        const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
        const res = await this.apiFetch(`/api/v1/grades/scan${qs}`, { method: "POST" });
        if (res && Array.isArray(res.courses)) {
          this.gradesData = {
            courses: res.courses,
            recent_changes: res.recent_changes || [],
            summary: res.summary || this.gradesData.summary,
          };
        }
        await this.refreshCoreData(true);
        const changes = res?.scan?.changes_detected?.length || 0;
        const scannedCount = res?.scan?.courses_scanned || 0;
        const assignCount = res?.scan?.assignments_scanned || 0;
        const assignPart = assignCount > 0 ? ` • ${assignCount} assignment(s)` : "";
        if (changes > 0) {
          this.notify(`Grades updated • ${changes} change(s) detected${assignPart}!`);
        } else if (scannedCount > 0) {
          this.notify(`Scanned ${scannedCount} course(s)${assignPart} • All remembered`);
        } else if (!this.gradesPortalApp) {
          this.notify("Add PowerSchool URL first to scan live grades");
          this.openNewAppModal("PowerSchool");
        } else if (res?.scan?.auth_required) {
          this.notify("PowerSchool SSO session expired — sign in inside stream to sync all courses & assignments");
          this.openGradesPortalStream();
        } else {
          this.notify("Open PowerSchool stream & sign in so grades can be scanned");
        }
      } catch (err) {
        this.notify(err.message || "Grade scan failed", "error");
      } finally {
        this.gradesScanning = false;
      }
    },

    async openGradesPortalStream() {
      const existing = this.gradesPortalApp;
      if (existing) {
        this.openStreamWindow(existing);
      } else {
        this.openNewAppModal("PowerSchool");
      }
    },

    async deleteCourseGrade(course) {
      if (!course || !course.id) return;
      try {
        await this.apiFetch(`/api/v1/grades/${course.id}`, { method: "DELETE" });
        await this.refreshCoreData(true);
        this.notify(`Removed ${course.course_name}`);
      } catch (err) {
        this.notify(err.message || "Could not remove course", "error");
      }
    },

    // =========================================================================
    // v10.2: Grades course groups (tabs) + persistent visibility control
    // =========================================================================
    gradeTabHasCourse(tab, course) {
      const list = (tab && (tab.course_keys || [])) || [];
      return list.includes(this.gradeCourseKey(course));
    },

    gradeCourseKey(course) {
      return String((course && (course.course_key || course.id)) || "");
    },

    get visibleGradeCourses() {
      const all = (this.gradesChartSummary && this.gradesChartSummary.courses) || [];
      const active = String(this.activeGradeTab || "all");
      if (active === "all" || !active.startsWith("tab")) return all;
      const tabId = Number(active.slice(3));
      const tab = (this.gradesData.tabs || []).find((t) => Number(t.id) === tabId);
      if (!tab) return all;
      const keys = new Set((tab.course_keys || []).map(String));
      return all.filter((c) => keys.has(this.gradeCourseKey(c)));
    },

    tabScopedAverage(tab) {
      if (!tab || !Array.isArray(tab.course_keys) || !tab.course_keys.length) return null;
      const keys = new Set(tab.course_keys.map(String));
      const rows = ((this.gradesChartSummary && this.gradesChartSummary.courses) || []).filter(
        (c) => keys.has(this.gradeCourseKey(c)) && Number.isFinite(Number(c.score_percent))
      );
      if (!rows.length) return null;
      const sum = rows.reduce((acc, c) => acc + Number(c.score_percent), 0);
      return Math.round(sum / rows.length * 10) / 10;
    },

    async createGradeTab() {
      const name = (this.newGradeTabName || "").trim();
      if (!name) return;
      try {
        await this.apiFetch("/api/v1/grades/tabs", {
          method: "POST",
          body: JSON.stringify({ name, course_keys: [] }),
        });
        this.newGradeTabName = "";
        await this.refreshCoreData(true);
        this.notify(`Group "${name}" created`);
      } catch (err) {
        this.notify(err.message || "Could not create group", "error");
      }
    },

    async toggleCourseInGradeTab(tab, course) {
      if (!tab || !course) return;
      const keys = new Set((tab.course_keys || []).map(String));
      const key = this.gradeCourseKey(course);
      if (keys.has(key)) keys.delete(key); else keys.add(key);
      try {
        await this.apiFetch("/api/v1/grades/tabs", {
          method: "POST",
          body: JSON.stringify({ name: tab.name, course_keys: [...keys] }),
        });
        await this.refreshCoreData(true);
        // Local echo so the pill flips instantly even if the refresh is slow.
        const local = (this.gradesData.tabs || []).find((t) => Number(t.id) === Number(tab.id));
        if (local) local.course_keys = [...keys];
      } catch (err) {
        this.notify(err.message || "Could not update group", "error");
      }
    },

    async deleteGradeTab(tab) {
      if (!tab) return;
      try {
        await this.apiFetch(`/api/v1/grades/tabs/${tab.id}`, { method: "DELETE" });
        if (String(this.activeGradeTab) === `tab${tab.id}`) this.activeGradeTab = "all";
        await this.refreshCoreData(true);
        this.notify(`Deleted group "${tab.name}"`);
      } catch (err) {
        this.notify(err.message || "Could not delete group", "error");
      }
    },

    async hideGradeCourse(course) {
      if (!course) return;
      try {
        await this.apiFetch("/api/v1/grades/hide", {
          method: "POST",
          body: JSON.stringify({ course_key: this.gradeCourseKey(course), hidden: true }),
        });
        await this.refreshCoreData(true);
        this.notify(`Hid ${course.course_name} — it stays hidden across scans`, "ok", {
          actionLabel: "Undo",
          action: () => this.restoreHiddenCourse({ course_key: this.gradeCourseKey(course), course_name: course.course_name }),
        });
      } catch (err) {
        this.notify(err.message || "Could not hide course", "error");
      }
    },

    async restoreHiddenCourse(hc) {
      if (!hc) return;
      try {
        await this.apiFetch("/api/v1/grades/hide", {
          method: "POST",
          body: JSON.stringify({ course_key: hc.course_key || this.gradeCourseKey(hc), hidden: false }),
        });
        await this.refreshCoreData(true);
        this.notify(`Restored ${hc.course_name || "course"}`);
      } catch (err) {
        this.notify(err.message || "Could not restore course", "error");
      }
    },

    async restoreAllHiddenCourses() {
      const list = (this.gradesData && this.gradesData.hidden_courses) || [];
      for (const hc of list) {
        try {
          await this.apiFetch("/api/v1/grades/hide", {
            method: "POST",
            body: JSON.stringify({ course_key: hc.course_key, hidden: false }),
          });
        } catch (_e) {}
      }
      await this.refreshCoreData(true);
      this.notify(`Restored ${list.length} course(s)`);
    },

    async runAppAIAssist() {
      if (!this.aiAssistQuery.trim()) return;
      try {
        const res = await this.apiFetch("/api/v1/apps/ai-assist", {
          method: "POST",
          body: JSON.stringify({ query: this.aiAssistQuery.trim(), zero_knowledge: Boolean(this.appForm.zero_knowledge) }),
        });
        const s = res.config || res.suggestion || {};
        this.appForm.name = s.name || this.appForm.name;
        this.appForm.url = s.url || this.appForm.url;
        this.appForm.category = s.category || "General";
        this.appForm.zero_knowledge = Boolean(s.zero_knowledge);
        this.appForm.indicator_type = s.indicator_type || "dom_badge";
        this.appForm.title_regex = s.title_regex || "\\((\\d+)\\)";
        this.appForm.badge_selectors_text = (s.badge_selectors || []).join(", ");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveAppForm() {
      const selectors = (this.appForm.badge_selectors_text || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const inboxCats = (this.appForm.inbox_categories_text || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const payload = {
        name: this.appForm.name.trim(),
        url: this.appForm.url.trim(),
        icon: this.appForm.icon,
        category: this.appForm.category,
        account_profile: (this.appForm.account_profile || "default").trim() || "default",
        enabled: Boolean(this.appForm.enabled),
        zero_knowledge: Boolean(this.appForm.zero_knowledge),
        indicator_type: this.appForm.indicator_type,
        title_regex: this.appForm.title_regex,
        badge_selectors: selectors,
        inbox_categories: inboxCats,
        notes: this.appForm.notes,
      };
      try {
        if (this.appModalEditingSlug) {
          const res = await this.apiFetch(`/api/v1/apps/${this.appModalEditingSlug}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
          if (res && res.app) {
            const oldSlug = res.previous_slug || this.appModalEditingSlug;
            const oldName = res.previous_name || "";
            if (oldName && this.selectedAppFilter === oldName) {
              this.selectedAppFilter = res.app.name;
            }
            for (const w of this.windows) {
              if (w.kind === "stream" && w.appSlug === oldSlug) {
                w.appSlug = res.app.slug;
                w.title = res.app.name;
              }
            }
          }
        } else {
          await this.apiFetch("/api/v1/apps", {
            method: "POST",
            body: JSON.stringify(payload),
          });
        }
        this.appModalOpen = false;
        await Promise.all([this.refreshCoreData(true), this.loadInboxCategories()]);
        this.notify("App saved");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async toggleAppEnabled(app) {
      try {
        await this.apiFetch(`/api/v1/apps/${app.slug}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: !app.enabled }),
        });
        await this.refreshCoreData(true);
      } catch (_e) {}
    },

    async deleteMonitoredApp(app) {
      try {
        await this.apiFetch(`/api/v1/apps/${app.slug}`, { method: "DELETE" });
        await this.refreshCoreData(true);
      } catch (_e) {}
    },

    async fireSimulatedPing() {
      this.pingSubmitting = true;
      try {
        await this.apiFetch("/api/v1/ping", {
          method: "POST",
          body: JSON.stringify({
            source_app: this.simPing.source_app,
            indicator_type: this.simPing.indicator_type,
            unread_count: 1,
            title: this.simPing.title,
            message: this.simPing.message,
          }),
        });
        await this.refreshCoreData(true);
        this.notify("Test notification received");
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.pingSubmitting = false;
      }
    },

    formatRelativeTime(isoString) {
      if (!isoString) return "";
      const normalized = isoString.endsWith("Z") || isoString.includes("+") ? isoString : `${isoString}Z`;
      const diffSec = Math.max(0, Math.floor((Date.now() - new Date(normalized).getTime()) / 1000));
      if (diffSec < 60) return "now";
      if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
      if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
      return `${Math.floor(diffSec / 86400)}d`;
    },

    // =========================================================================
    // Comprehensive WebOS Settings, Presets, & Safe AI Code Studio
    // =========================================================================
    get activePresetName() {
      const found = this.osPresets.find((p) => p.id === this.activePresetId);
      return found ? found.name : "Custom";
    },

    applyOsSettingsPayload(payload, updateEditor = false) {
      if (!payload || typeof payload !== "object") return;
      if (payload.values && typeof payload.values === "object") {
        this.osSettings = { ...this.osSettings, ...payload.values };
      }
      if (Array.isArray(payload.presets)) {
        this.osPresets = payload.presets;
      }
      if (payload.active_preset_id) {
        this.activePresetId = payload.active_preset_id;
      }
      if (payload.code_map) {
        this.osCodeMap = payload.code_map;
      }

      // Mirror core reactive flags
      this.theme = this.osSettings.theme || "dark";
      this.liteGlass = Boolean(this.osSettings.lite_glass);
      this.dockAutoHide = Boolean(this.osSettings.dock_auto_hide);
      this.enableThinking = Boolean(this.osSettings.enable_thinking);
      if (this.osSettings.wm_mode === "tiling" || this.osSettings.wm_mode === "floating") {
        this.wmMode = this.osSettings.wm_mode;
      }
      if (this.osSettings.wallpaper_id) {
        this.activeWallpaperId = this.osSettings.wallpaper_id;
      }
      if (Number.isFinite(Number(this.osSettings.wallpaper_dim))) {
        this.wallpaperDim = Number(this.osSettings.wallpaper_dim);
      }

      safeStorageSet("node_theme", this.theme);
      safeStorageSet("node_lite_glass", this.liteGlass ? "true" : "false");
      safeStorageSet("node_dock_autohide", this.dockAutoHide ? "true" : "false");
      safeStorageSet("node_wm_mode", this.wmMode);
      safeStorageSet("node_thinking", this.enableThinking ? "true" : "false");

      // Sync linked Cron & Focus Guard local UI models
      this.cronState.settings.enabled = Boolean(this.osSettings.cron_enabled);
      this.cronState.settings.interval_minutes = Number(this.osSettings.cron_interval_minutes) || 60;
      this.cronState.settings.bedtime_hhmm = this.osSettings.bedtime_hhmm || "22:00";
      this.cronState.settings.wake_hhmm = this.osSettings.wake_hhmm || "07:00";
      this.cronState.settings.sleep_buffer_minutes = Number(this.osSettings.sleep_buffer_minutes) || 15;

      this.focusGuard.enabled = Boolean(this.osSettings.fg_enabled);
      this.focusGuard.hard_limit_minutes = Number(this.osSettings.fg_hard_limit_minutes) || 45;
      this.focusGuard.long_video_trigger_minutes = Number(this.osSettings.fg_long_video_trigger_minutes) || 10;
      this.focusGuard.study_allowance_minutes = Number(this.osSettings.fg_study_allowance_minutes) || 20;
      this.focusGuard.nudge_on_shorts_immediately = Boolean(this.osSettings.fg_nudge_on_shorts);

      // Apply CSS tokens, scoped custom CSS, and sandboxed disposable JS hooks
      if (window.NodeCustomizer) {
        window.NodeCustomizer.applyDesignTokens(this.osSettings);
        if (typeof payload.custom_css === "string") {
          window.NodeCustomizer.applyCustomCss(payload.custom_css);
        }
        if (typeof payload.custom_js === "string") {
          const jsRes = window.NodeCustomizer.runSandboxedJs(payload.custom_js, this);
          this.customJsError = jsRes.ok ? "" : jsRes.error;
        }
      }

      if (updateEditor) {
        const activePr = this.osPresets.find((p) => p.id === this.activePresetId);
        this.presetEditor = {
          id: activePr ? activePr.id : null,
          builtin: activePr ? Boolean(activePr.builtin) : false,
          name: activePr && !activePr.builtin ? activePr.name : "My Custom Workspace",
          description: activePr ? activePr.description || "" : "",
          custom_css: payload.custom_css || "",
          custom_js: payload.custom_js || "",
        };
      }

      if (this.wmMode === "tiling") {
        this.layoutTiles(true);
      }
    },

    async loadOsSettings(silent = false) {
      try {
        const res = await this.apiFetch("/api/v1/settings");
        this.applyOsSettingsPayload(res, true);
      } catch (err) {
        if (!silent && err.message !== "Session locked") this.notify(err.message, "error");
      }
    },

    scheduleSettingsSync(patchObj) {
      clearTimeout(this._settingsSaveTimer);
      this._settingsSaveTimer = setTimeout(async () => {
        try {
          const res = await this.apiFetch("/api/v1/settings", {
            method: "PATCH",
            body: JSON.stringify({ settings_patch: patchObj }),
          });
          this.applyOsSettingsPayload(res, false);
          this.loadCronStatus(true);
        } catch (err) {
          this.notify(err.message, "error");
        }
      }, 180);
    },

    setSingleOsSetting(key, value, persist = true) {
      this.osSettings[key] = value;

      if (key === "theme") this.theme = value;
      else if (key === "lite_glass") this.liteGlass = Boolean(value);
      else if (key === "dock_auto_hide") this.dockAutoHide = Boolean(value);
      else if (key === "enable_thinking") this.enableThinking = Boolean(value);
      else if (key === "wm_mode" && (value === "tiling" || value === "floating")) {
        if (this.wmMode !== value) {
          this.toggleWmMode();
          return;
        }
      }

      if (window.NodeCustomizer) {
        window.NodeCustomizer.applyDesignTokens(this.osSettings);
      }
      if (this.wmMode === "tiling") {
        this.layoutTiles(true);
      }
      if (persist) {
        this.scheduleSettingsSync({ [key]: value });
      }
    },

    openPresetInEditor(pr) {
      if (!pr) return;
      this.settingsTab = "presets";
      this.presetEditor = {
        id: pr.id,
        builtin: Boolean(pr.builtin),
        name: pr.builtin ? `${pr.name} (Copy)` : pr.name,
        description: pr.description || "",
        custom_css: pr.custom_css || "",
        custom_js: pr.custom_js || "",
      };
    },

    async loadOsPreset(presetId) {
      try {
        const res = await this.apiFetch(`/api/v1/settings/presets/${encodeURIComponent(presetId)}/load`, {
          method: "POST",
        });
        this.applyOsSettingsPayload(res, true);
        await this.loadCronStatus(true);
        this.notify(`Loaded preset: ${this.activePresetName}`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async applyEditorLiveOnly() {
      try {
        const res = await this.apiFetch("/api/v1/settings", {
          method: "PATCH",
          body: JSON.stringify({
            settings_patch: this.osSettings,
            custom_css: this.presetEditor.custom_css || "",
            custom_js: this.presetEditor.custom_js || "",
          }),
        });
        this.applyOsSettingsPayload(res, false);
        if (!this.customJsError) {
          this.notify("Applied custom CSS & sandboxed JS live");
        }
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveEditorAsPreset(overwriteExisting = false) {
      const name = (this.presetEditor.name || "").trim() || "Custom Workspace";
      try {
        const res = await this.apiFetch("/api/v1/settings/presets", {
          method: "POST",
          body: JSON.stringify({
            preset_id: overwriteExisting && !this.presetEditor.builtin ? this.presetEditor.id : null,
            name,
            description: this.presetEditor.description || "",
            settings_patch: this.osSettings,
            custom_css: this.presetEditor.custom_css || "",
            custom_js: this.presetEditor.custom_js || "",
            activate: true,
          }),
        });
        if (res && res.state) {
          this.applyOsSettingsPayload(res.state, true);
        }
        this.notify(`Saved preset: ${name}`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async deleteOsPreset(presetId) {
      try {
        const res = await this.apiFetch(`/api/v1/settings/presets/${encodeURIComponent(presetId)}`, {
          method: "DELETE",
        });
        this.applyOsSettingsPayload(res, true);
        this.notify("Preset deleted");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async resetAllOsSettings() {
      try {
        const res = await this.apiFetch("/api/v1/settings/reset", { method: "POST" });
        this.applyOsSettingsPayload(res, true);
        this.settingsAiResult = null;
        this.notify("Reset to Node Default (all custom code cleared)");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async runSettingsAiExample(exampleText) {
      this.settingsAiPrompt = exampleText;
      await this.submitSettingsAiPrompt();
    },

    async submitSettingsAiPrompt() {
      const q = (this.settingsAiPrompt || "").trim();
      if (!q) return;
      this.settingsAiLoading = true;
      try {
        const res = await this.apiFetch("/api/v1/settings/ai-assist", {
          method: "POST",
          body: JSON.stringify({
            prompt: q,
            save_as_preset: Boolean(this.settingsAiSavePreset),
            apply_immediately: true,
          }),
        });
        if (res && res.state) {
          this.applyOsSettingsPayload(res.state, true);
        }
        if (res && res.assist) {
          this.settingsAiResult = res.assist;
          if (res.assist.target_tab) {
            this.settingsTab = res.assist.target_tab;
          }
          if (res.assist.saved_preset) {
            this.notify(`Saved & loaded preset: ${res.assist.saved_preset.name}`);
          } else if (res.assist.has_changes) {
            this.notify("Settings updated");
          }
        }
        this.settingsAiPrompt = "";
        await this.loadCronStatus(true);
      } catch (err) {
        this.notify(err.message, "error");
      } finally {
        this.settingsAiLoading = false;
      }
    },

    // =========================================================================
    // v10.0 — Resizable, Addable & Removable Desktop Widgets
    // =========================================================================
    async loadDesktopWidgets() {
      try {
        const res = await this.apiFetch("/api/v1/desktop/widgets");
        if (Array.isArray(res?.widgets) && res.widgets.length > 0) {
          this.desktopWidgets = res.widgets.map((w) => ({
            ...w,
            type: w.type || w.widget_type || (w.kind === "extension" ? "extension" : "tasks"),
            widget_type: w.widget_type || w.type || "tasks",
          }));
        }
        if (Array.isArray(res?.builtin_catalog)) {
          this.builtinWidgetCatalog = res.builtin_catalog.map((b) => ({
            ...b,
            type: b.type || b.widget_type,
            widget_type: b.widget_type || b.type,
          }));
        }
        if (Array.isArray(res?.installed_extensions)) {
          this.installedExtensions = res.installed_extensions;
        }
      } catch (_e) {}
    },

    widgetGridStyle(w) {
      const col = Math.max(2, Math.min(12, Number(w?.col_span) || 3));
      const mdCol = col <= 5 ? 6 : 12;
      const minH = Math.max(140, Math.min(680, Number(w?.min_height) || 206));
      return `--widget-col-lg: ${col}; --widget-col-md: ${mdCol}; min-height: ${minH}px;`;
    },

    async persistDesktopWidgets() {
      try {
        const res = await this.apiFetch("/api/v1/desktop/widgets", {
          method: "PUT",
          body: JSON.stringify({ widgets: this.desktopWidgets }),
        });
        if (Array.isArray(res?.widgets)) {
          this.desktopWidgets = res.widgets;
        }
      } catch (err) {
        this.notify(err.message || "Could not save widget layout", "error");
      }
    },

    startWidgetResize(ev, widget) {
      const cell = ev.currentTarget.closest(".node-desk-widget-cell");
      const grid = cell && cell.closest(".node-desktop-grid-v10");
      if (!cell || !grid) return;

      const startX = ev.clientX;
      const startY = ev.clientY;
      const startCol = Math.max(2, Math.min(12, Number(widget.col_span) || 3));
      const rect = cell.getBoundingClientRect();
      const gridRect = grid.getBoundingClientRect();
      const startH = Math.max(140, Math.round(rect.height || Number(widget.min_height) || 206));
      const colUnitPx = Math.max(60, gridRect.width / 12);

      this.resizingWidgetId = widget.id;

      const onMove = (me) => {
        const dx = me.clientX - startX;
        const dy = me.clientY - startY;
        const deltaCols = Math.round(dx / colUnitPx);
        const nextCol = Math.max(2, Math.min(12, startCol + deltaCols));
        const nextH = Math.max(140, Math.min(680, Math.round((startH + dy) / 8) * 8));
        widget.col_span = nextCol;
        widget.min_height = nextH;
      };

      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        this.resizingWidgetId = null;
        this.persistDesktopWidgets();
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    async nudgeWidgetSpan(widget, deltaCols) {
      const cur = Math.max(2, Math.min(12, Number(widget.col_span) || 3));
      widget.col_span = Math.max(2, Math.min(12, cur + deltaCols));
      await this.persistDesktopWidgets();
    },

    async moveDesktopWidget(idx, dir) {
      const target = idx + dir;
      if (target < 0 || target >= this.desktopWidgets.length) return;
      const copy = [...this.desktopWidgets];
      const [moved] = copy.splice(idx, 1);
      copy.splice(target, 0, moved);
      this.desktopWidgets = copy;
      await this.persistDesktopWidgets();
    },

    async removeDesktopWidget(widgetId) {
      this.desktopWidgets = (this.desktopWidgets || []).filter((w) => w.id !== widgetId);
      await this.persistDesktopWidgets();
      this.notify("Removed widget from Desktop");
    },

    async addBuiltinDesktopWidget(catItem) {
      if (!catItem) return;
      const wType = catItem.type || catItem.widget_type || "tasks";
      const newW = {
        id: `w-${wType}-${Date.now().toString(36).slice(-4)}`,
        kind: "builtin",
        type: wType,
        widget_type: wType,
        title: catItem.title,
        col_span: catItem.default_col_span || 3,
        min_height: catItem.default_min_height || 206,
      };
      this.desktopWidgets.push(newW);
      await this.persistDesktopWidgets();
      this.notify(`Added "${catItem.title}" to Desktop`);
    },

    async addExtensionDesktopWidget(ext) {
      if (!ext) return;
      const size = ext.default_widget_size || {};
      const newW = {
        id: `w-ext-${ext.id}-${Date.now().toString(36).slice(-4)}`,
        kind: "extension",
        type: "extension",
        extension_id: ext.id,
        title: ext.name,
        col_span: Number(size.col_span) || 3,
        min_height: Number(size.min_height) || 206,
      };
      this.desktopWidgets.push(newW);
      await this.persistDesktopWidgets();
      this.notify(`Pinned "${ext.name}" widget to Desktop`);
    },

    async resetDesktopWidgetsLayout() {
      try {
        const res = await this.apiFetch("/api/v1/desktop/widgets/reset", { method: "POST" });
        if (Array.isArray(res?.widgets)) {
          this.desktopWidgets = res.widgets;
        }
        this.notify("Reset Desktop widgets to default layout");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    getExtensionById(extId) {
      return (this.installedExtensions || []).find((e) => e.id === extId) || null;
    },

    // =========================================================================
    // v10.0 — Multi-Provider AI Models & Server-Side Job Sync
    // =========================================================================
    get allAvailableChatModels() {
      const out = [{ id: "auto", label: "Auto (Best Online Model)" }];
      const seen = new Set(["auto"]);
      for (const p of this.aiProvidersState?.providers || []) {
        if (!p.enabled) continue;
        const models = Array.isArray(p.discovered_models) && p.discovered_models.length > 0
          ? p.discovered_models
          : (p.configured_models || []);
        for (const m of models) {
          const mid = String(m || "").trim();
          if (!mid) continue;
          const fullId = `${p.id}::${mid}`;
          if (!seen.has(fullId)) {
            seen.add(fullId);
            out.push({ id: fullId, label: `${mid} (${p.name})` });
          }
        }
      }
      if (out.length === 1 && Array.isArray(this.system?.ollama?.available_models)) {
        for (const m of this.system.ollama.available_models) {
          if (m && !seen.has(m)) {
            seen.add(m);
            out.push({ id: m, label: m });
          }
        }
      }
      return out;
    },

    async loadAiProviders(forceRefresh = false) {
      try {
        const res = await this.apiFetch(`/api/v1/models/providers?refresh=${forceRefresh ? "true" : "false"}`);
        if (res && Array.isArray(res.providers)) {
          this.aiProvidersState = res;
        }
        if (forceRefresh) {
          await this.refreshCoreData(true);
          this.notify("Discovered live models across all providers");
        }
      } catch (_e) {}
    },

    openAddProviderPreset(presetType) {
      const presets = {
        ollama_remote: { name: "Remote Ollama Server", protocol: "ollama", base_url: "http://192.168.1.50:11434", models_csv: "", auto_discover: true },
        openai: { name: "OpenAI Cloud", protocol: "openai", base_url: "https://api.openai.com/v1", models_csv: "gpt-4o-mini, gpt-4o", auto_discover: true },
        anthropic: { name: "Anthropic Claude", protocol: "anthropic", base_url: "https://api.anthropic.com", models_csv: "claude-3-5-haiku-latest, claude-3-7-sonnet-latest", auto_discover: false },
        gemini: { name: "Google Gemini", protocol: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", models_csv: "gemini-2.5-flash, gemini-2.5-pro", auto_discover: false },
        openrouter: { name: "OpenRouter", protocol: "openai", base_url: "https://openrouter.ai/api/v1", models_csv: "meta-llama/llama-3.3-70b-instruct", auto_discover: true },
        groq: { name: "Groq Cloud", protocol: "openai", base_url: "https://api.groq.com/openai/v1", models_csv: "llama-3.3-70b-versatile", auto_discover: true },
      };
      const p = presets[presetType] || presets.ollama_remote;
      this.providerForm = {
        provider_id: "",
        name: p.name,
        protocol: p.protocol,
        base_url: p.base_url,
        api_key: "",
        models_csv: p.models_csv,
        auto_discover: p.auto_discover,
        enabled: true,
      };
      this.providerFormOpen = true;
    },

    editExistingProvider(prov) {
      this.providerForm = {
        provider_id: prov.id,
        name: prov.name,
        protocol: prov.protocol,
        base_url: prov.base_url,
        api_key: "",
        models_csv: (prov.configured_models || []).join(", "),
        auto_discover: prov.auto_discover !== false,
        enabled: prov.enabled !== false,
      };
      this.providerFormOpen = true;
    },

    async saveAiProviderForm() {
      this.providerSaving = true;
      try {
        const models = (this.providerForm.models_csv || "")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        const payload = {
          provider_id: this.providerForm.provider_id || null,
          name: this.providerForm.name.trim() || "AI Provider",
          protocol: this.providerForm.protocol,
          base_url: this.providerForm.base_url.trim(),
          api_key: this.providerForm.api_key ? this.providerForm.api_key.trim() : null,
          models,
          enabled: Boolean(this.providerForm.enabled),
          auto_discover: Boolean(this.providerForm.auto_discover),
        };
        const res = await this.apiFetch("/api/v1/models/providers", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        if (res?.state) this.aiProvidersState = res.state;
        this.providerFormOpen = false;
        await this.refreshCoreData(true);
        this.notify(`Saved provider: ${payload.name}`);
      } catch (err) {
        this.notify(err.message || "Could not save AI provider", "error");
      } finally {
        this.providerSaving = false;
      }
    },

    async deleteAiProvider(providerId) {
      try {
        const res = await this.apiFetch(`/api/v1/models/providers/${encodeURIComponent(providerId)}`, {
          method: "DELETE",
        });
        if (res?.state) this.aiProvidersState = res.state;
        await this.refreshCoreData(true);
        this.notify("Removed AI provider");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveModelRoleRouting() {
      try {
        const r = this.aiProvidersState?.routing || {};
        const res = await this.apiFetch("/api/v1/models/routing", {
          method: "PATCH",
          body: JSON.stringify({
            primary_model: r.primary_model || "auto",
            fast_model: r.fast_model || "auto",
            searxng_url: r.searxng_url || "",
          }),
        });
        if (res?.routing && this.aiProvidersState) {
          this.aiProvidersState.routing = res.routing;
        }
        await this.refreshCoreData(true);
        this.notify("Updated AI model routing");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async syncServerAiJobs() {
      try {
        const res = await this.apiFetch("/api/v1/ai/jobs?limit=25");
        const jobs = Array.isArray(res?.jobs) ? res.jobs : [];
        const hadRunningBefore = this.totalActiveServerJobs > 0;
        this.serverAiJobs = jobs;

        for (const j of jobs) {
          if (j.status === "queued" || j.status === "running") {
            this.attachToServerJobStream(j.job_uid, j.thread_id || "main");
          }
        }
        if (hadRunningBefore && this.totalActiveServerJobs === 0) {
          await this.loadChatThreads();
        }
      } catch (_e) {}
    },

    // =========================================================================
    // v10.0 — Shared Multi-Agent AI Logbook
    // =========================================================================
    get filteredSharedLogs() {
      const q = (this.sharedLogSearch || "").trim().toLowerCase();
      return (this.sharedAgentLogs || []).filter((item) => {
        if (this.sharedLogCategoryFilter !== "all" && item.category !== this.sharedLogCategoryFilter) {
          return false;
        }
        if (!q) return true;
        return `${item.title} ${item.content} ${(item.tags || []).join(" ")} ${item.model_id}`.toLowerCase().includes(q);
      });
    },

    async loadSharedAgentLogs() {
      try {
        const res = await this.apiFetch("/api/v1/ai/shared-logs?limit=80");
        if (Array.isArray(res?.logs)) {
          this.sharedAgentLogs = res.logs;
        }
      } catch (_e) {}
    },

    editSharedLogItem(entry) {
      this.sharedLogForm = {
        log_id: entry.id,
        title: entry.title || "",
        category: entry.category || "insight",
        tags_csv: (entry.tags || []).join(", "),
        pinned: Boolean(entry.pinned),
        content: entry.content || "",
      };
    },

    resetSharedLogForm() {
      this.sharedLogForm = {
        log_id: null,
        title: "",
        category: "insight",
        tags_csv: "",
        pinned: false,
        content: "",
      };
    },

    async saveSharedLogForm() {
      if (!this.sharedLogForm.title.trim() || !this.sharedLogForm.content.trim()) return;
      try {
        const tags = (this.sharedLogForm.tags_csv || "")
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean);
        await this.apiFetch("/api/v1/ai/shared-logs", {
          method: "POST",
          body: JSON.stringify({
            log_id: this.sharedLogForm.log_id,
            title: this.sharedLogForm.title.trim(),
            content: this.sharedLogForm.content.trim(),
            category: this.sharedLogForm.category,
            tags,
            pinned: Boolean(this.sharedLogForm.pinned),
            agent_name: this.currentUsername || "user",
            model_id: "human+os",
          }),
        });
        this.resetSharedLogForm();
        await this.loadSharedAgentLogs();
        this.notify("Saved entry to Shared Multi-Agent Logbook");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async deleteSharedLogItem(logId) {
      try {
        await this.apiFetch(`/api/v1/ai/shared-logs/${logId}`, { method: "DELETE" });
        await this.loadSharedAgentLogs();
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    // =========================================================================
    // v10.0 — App Library Catalog (1-Click Install; Zero Preinstalled Defaults)
    // =========================================================================
    get appCatalogCategories() {
      const cats = new Set(["All"]);
      for (const item of this.appCatalog || []) {
        if (item.category) cats.add(item.category);
      }
      return Array.from(cats);
    },

    get filteredAppCatalog() {
      const q = (this.appCatalogSearch || "").trim().toLowerCase();
      return (this.appCatalog || []).filter((item) => {
        if (this.appCatalogCategory !== "All" && item.category !== this.appCatalogCategory) return false;
        if (!q) return true;
        return `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(q);
      });
    },

    async loadAppCatalog() {
      try {
        const res = await this.apiFetch("/api/v1/apps/catalog");
        if (Array.isArray(res?.catalog)) {
          this.appCatalog = res.catalog;
        }
      } catch (_e) {}
    },

    async installCatalogApp(tpl, openStreamImmediately = false) {
      if (!tpl || !tpl.template_id) return;
      this.installingTemplateId = tpl.template_id;
      try {
        const customUrl = (this.templateUrlDrafts[tpl.template_id] || "").trim() || null;
        const res = await this.apiFetch("/api/v1/apps/install-template", {
          method: "POST",
          body: JSON.stringify({
            template_id: tpl.template_id,
            custom_url: customUrl,
          }),
        });
        if (Array.isArray(res?.catalog)) this.appCatalog = res.catalog;
        await this.refreshCoreData(true);
        this.notify(`Installed ${res?.app?.name || tpl.name}`);
        if (openStreamImmediately && res?.app) {
          this.openStreamWindow(res.app);
        }
      } catch (err) {
        this.notify(err.message || "Could not install app", "error");
      } finally {
        this.installingTemplateId = null;
      }
    },

    // =========================================================================
    // v10.0 — Generalized Social & Media Focus Guard Platform Rules
    // =========================================================================
    get focusGuardPlatformsList() {
      const raw = this.focusGuard?.platforms;
      const minsMap = this.focusGuard?.platform_minutes_today || {};
      if (Array.isArray(raw)) {
        return raw.map((cfg) => ({
          id: cfg.id,
          name: cfg.name || cfg.id,
          domains: cfg.domains || [],
          enabled: cfg.enabled !== false,
          daily_limit_minutes: Number(cfg.daily_limit_minutes ?? 30),
          short_form_immediate: Boolean(cfg.short_form_immediate),
          builtin: cfg.builtin !== false && !String(cfg.id || "").includes("-com") && !String(cfg.id || "").includes("-org"),
          minutes_today: Number(cfg.minutes_today ?? minsMap[cfg.id] ?? 0),
        }));
      }
      const pmap = raw || {};
      return Object.entries(pmap).map(([pid, cfg]) => ({
        id: pid,
        name: cfg.name || pid,
        domains: cfg.domains || [],
        enabled: cfg.enabled !== false,
        daily_limit_minutes: Number(cfg.daily_limit_minutes ?? 30),
        short_form_immediate: Boolean(cfg.short_form_immediate),
        builtin: Boolean(cfg.builtin),
        minutes_today: Number(minsMap[pid] || 0),
      }));
    },

    async toggleFocusPlatform(plat) {
      if (!plat) return;
      try {
        const domain = (plat.domains && plat.domains[0]) || plat.id;
        const res = await this.apiFetch("/api/v1/focus-guard/platforms", {
          method: "POST",
          body: JSON.stringify({
            domain,
            name: plat.name,
            enabled: !plat.enabled,
            daily_limit_minutes: plat.daily_limit_minutes,
            short_form_immediate: plat.short_form_immediate,
          }),
        });
        this.focusGuard = res;
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async saveFocusPlatformLimit(plat, minutesVal) {
      if (!plat) return;
      try {
        const domain = (plat.domains && plat.domains[0]) || plat.id;
        const res = await this.apiFetch("/api/v1/focus-guard/platforms", {
          method: "POST",
          body: JSON.stringify({
            domain,
            name: plat.name,
            enabled: plat.enabled,
            daily_limit_minutes: Math.max(0, Number(minutesVal) || 0),
            short_form_immediate: plat.short_form_immediate,
          }),
        });
        this.focusGuard = res;
        this.notify(`Updated ${plat.name} daily cap`);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async addCustomFocusPlatform() {
      const dom = (this.newPlatformForm.domain || "").trim();
      if (!dom) return;
      try {
        const res = await this.apiFetch("/api/v1/focus-guard/platforms", {
          method: "POST",
          body: JSON.stringify({
            domain: dom,
            name: (this.newPlatformForm.name || "").trim() || null,
            enabled: true,
            daily_limit_minutes: Number(this.newPlatformForm.daily_limit_minutes) || 30,
            short_form_immediate: Boolean(this.newPlatformForm.short_form_immediate),
          }),
        });
        this.focusGuard = res;
        this.newPlatformForm = { name: "", domain: "", daily_limit_minutes: 30, short_form_immediate: true };
        this.notify("Added custom site to Focus Guard");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async removeCustomFocusPlatform(platformId) {
      try {
        const res = await this.apiFetch(`/api/v1/focus-guard/platforms/${encodeURIComponent(platformId)}`, {
          method: "DELETE",
        });
        this.focusGuard = res;
        this.notify("Removed custom site rule");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    // =========================================================================
    // v10.0 — Custom Apps & Widgets SDK + Sandboxed `window.NodeOS` RPC Host
    // =========================================================================
    async loadExtensions() {
      try {
        const [extRes, docRes] = await Promise.all([
          this.apiFetch("/api/v1/extensions"),
          this.apiFetch("/api/v1/extensions/sdk-docs"),
        ]);
        if (Array.isArray(extRes?.extensions)) this.installedExtensions = extRes.extensions;
        if (Array.isArray(extRes?.gallery)) this.extensionGallery = extRes.gallery;
        if (docRes?.markdown) this.extSdkDocsMarkdown = docRes.markdown;
      } catch (_e) {}
    },

    openExtensionWindow(extId) {
      const ext = this.getExtensionById(extId);
      if (!ext) {
        this.openWindow("extensions");
        return;
      }
      const winId = `ext-win-${ext.id}`;
      const existing = this.windows.find((w) => w.id === winId);
      if (existing) {
        existing.minimized = false;
        this.focusWindow(winId, false);
        this.layoutTiles(true);
        return;
      }
      const geo = this.defaultGeometry(this.windows.length);
      const createdWin = {
        id: winId,
        kind: "extension",
        extId: ext.id,
        icon: ext.icon || "puzzle",
        title: ext.name,
        minimized: false,
        maximized: false,
        z: ++this.nextZ,
        ...geo,
      };
      this.windows.push(createdWin);
      this.focusWindow(winId, false);
      this.layoutTiles(true);
    },

    async installGalleryExtension(galleryId, pinWidget = true) {
      try {
        const res = await this.apiFetch(`/api/v1/extensions/gallery/${encodeURIComponent(galleryId)}/install`, {
          method: "POST",
        });
        if (Array.isArray(res?.extensions)) this.installedExtensions = res.extensions;
        if (Array.isArray(res?.gallery)) this.extensionGallery = res.gallery;
        if (pinWidget && res?.extension?.provides_widget) {
          const alreadyOnDesk = (this.desktopWidgets || []).some((w) => w.extension_id === res.extension.id);
          if (!alreadyOnDesk) {
            await this.addExtensionDesktopWidget(res.extension);
          }
        }
        this.notify(`Installed "${res?.extension?.name || galleryId}"`);
      } catch (err) {
        this.notify(err.message || "Could not install extension", "error");
      }
    },

    async saveBuilderExtension() {
      this.extBuilderSaving = true;
      try {
        const f = this.extBuilderForm;
        const manifest = {
          id: (f.id || "custom-ext").trim(),
          name: (f.name || "Custom Extension").trim(),
          version: (f.version || "1.0.0").trim(),
          description: (f.description || "").trim(),
          icon: f.icon || "puzzle",
          permissions: Array.isArray(f.permissions) ? f.permissions : ["storage", "notifications.send"],
          provides_app: Boolean(f.provides_app),
          provides_widget: Boolean(f.provides_widget),
          default_widget_size: {
            col_span: Number(f.col_span) || 3,
            min_height: Number(f.min_height) || 206,
          },
        };
        const res = await this.apiFetch("/api/v1/extensions/install", {
          method: "POST",
          body: JSON.stringify({
            manifest,
            html: f.html || "",
            widget_html: f.widget_html || f.html || "",
            overwrite: true,
          }),
        });
        if (Array.isArray(res?.extensions)) this.installedExtensions = res.extensions;
        await this.loadDesktopWidgets();
        this.notify(`Built & installed "${manifest.name}"`);
        this.extensionsTab = "installed";
      } catch (err) {
        this.notify(err.message || "Extension build failed", "error");
      } finally {
        this.extBuilderSaving = false;
      }
    },

    async handleExtensionPackageUpload(ev) {
      const file = ev?.target?.files?.[0];
      if (!file) return;
      try {
        const buf = await file.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
        const b64 = btoa(binary);
        const res = await this.apiFetch("/api/v1/extensions/upload", {
          method: "POST",
          body: JSON.stringify({ filename: file.name, content_base64: b64 }),
        });
        if (Array.isArray(res?.extensions)) this.installedExtensions = res.extensions;
        this.notify(`Installed package: ${res?.extension?.name || file.name}`);
      } catch (err) {
        this.notify(err.message || "Invalid .nodeapp.json / .zip package", "error");
      } finally {
        ev.target.value = "";
      }
    },

    async exportInstalledExtension(extId) {
      try {
        const pkg = await this.apiFetch(`/api/v1/extensions/${encodeURIComponent(extId)}/export`);
        const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${extId}.nodeapp.json`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    async uninstallExtensionById(extId) {
      try {
        const res = await this.apiFetch(`/api/v1/extensions/${encodeURIComponent(extId)}`, {
          method: "DELETE",
        });
        if (Array.isArray(res?.extensions)) this.installedExtensions = res.extensions;
        if (Array.isArray(res?.gallery)) this.extensionGallery = res.gallery;
        if (Array.isArray(res?.widgets)) this.desktopWidgets = res.widgets;
        this.windows = this.windows.filter((w) => !(w.kind === "extension" && w.extId === extId));
        this.layoutTiles(true);
        this.notify("Uninstalled extension");
      } catch (err) {
        this.notify(err.message, "error");
      }
    },

    setupExtensionRpcBridge() {
      if (window.__nodeExtRpcBound) return;
      window.__nodeExtRpcBound = true;

      window.addEventListener("message", async (ev) => {
        const data = ev && ev.data;
        if (!data || data.type !== "NODEOS_RPC_CALL" || !data.extId || !data.reqId) return;

        const ext = this.getExtensionById(data.extId);
        const perms = new Set((ext && ext.permissions) || []);
        const reply = (ok, resultOrErr) => {
          try {
            if (ev.source && typeof ev.source.postMessage === "function") {
              ev.source.postMessage(
                ok
                  ? { type: "NODEOS_RPC_RESULT", reqId: data.reqId, ok: true, result: resultOrErr }
                  : { type: "NODEOS_RPC_RESULT", reqId: data.reqId, ok: false, error: String(resultOrErr) },
                "*"
              );
            }
          } catch (_e) {}
        };

        try {
          const method = String(data.method || "");
          const params = data.params || {};

          if (method === "theme.get") {
            return reply(true, {
              accent: this.osSettings.accent_color || "#2bfbae",
              isLight: this.theme === "light",
            });
          }
          if (method === "ui.openWindow") {
            this.openWindow(String(params.view || "desk"));
            return reply(true, { opened: true });
          }
          if (method === "storage.get") {
            if (!perms.has("storage")) return reply(false, "Missing 'storage' permission");
            const s = await this.apiFetch(`/api/v1/extensions/${encodeURIComponent(data.extId)}/storage`);
            return reply(true, s?.storage || {});
          }
          if (method === "storage.set") {
            if (!perms.has("storage")) return reply(false, "Missing 'storage' permission");
            const s = await this.apiFetch(`/api/v1/extensions/${encodeURIComponent(data.extId)}/storage`, {
              method: "POST",
              body: JSON.stringify({ data: params.data || {}, merge: params.merge !== false }),
            });
            return reply(true, s?.storage || {});
          }
          if (method === "tasks.list") {
            if (!perms.has("tasks.read")) return reply(false, "Missing 'tasks.read' permission");
            return reply(true, this.tasks || []);
          }
          if (method === "tasks.create") {
            if (!perms.has("tasks.write")) return reply(false, "Missing 'tasks.write' permission");
            const created = await this.apiFetch("/api/v1/tasks", {
              method: "POST",
              body: JSON.stringify({
                title: String(params.title || "Extension Task"),
                priority: String(params.priority || "normal"),
                estimated_minutes: Number(params.estimated_minutes || 25),
                due_date: params.due_date || null,
              }),
            });
            await this.refreshCoreData(true);
            return reply(true, created);
          }
          if (method === "calendar.list") {
            if (!perms.has("calendar.read")) return reply(false, "Missing 'calendar.read' permission");
            return reply(true, this.calendarEvents || []);
          }
          if (method === "grades.list") {
            if (!perms.has("grades.read")) return reply(false, "Missing 'grades.read' permission");
            return reply(true, this.gradesData || {});
          }
          if (method === "inbox.list") {
            if (!perms.has("inbox.read")) return reply(false, "Missing 'inbox.read' permission");
            return reply(true, (this.notifications || []).filter((n) => !n.is_read).slice(0, 20));
          }
          if (method === "notifications.send") {
            if (!perms.has("notifications.send")) return reply(false, "Missing 'notifications.send' permission");
            this.notify(String(params.message || "Extension alert"));
            return reply(true, { ok: true });
          }
          if (method === "ai.ask") {
            if (!perms.has("ai.ask")) return reply(false, "Missing 'ai.ask' permission");
            const job = await this.enqueueChatPrompt(this.defaultContextId(), String(params.prompt || ""));
            return reply(true, { job_uid: job?.job_uid || null });
          }
          return reply(false, `Unknown NodeOS method: ${method}`);
        } catch (err) {
          return reply(false, err?.message || "RPC error");
        }
      });
    },

    // =========================================================================
    // v10.1: Inbox Custom Categories + Von-First Auto-Sorter
    // =========================================================================
    async loadInboxCategories() {
      try {
        const res = await this.apiFetch("/api/v1/notifications/categories");
        if (res && Array.isArray(res.all_categories)) {
          this.inboxCategoriesState = res;
        }
      } catch (_e) {}
    },

    async addCustomInboxCategory() {
      const cat = (this.newInboxCategoryInput || "").trim();
      if (!cat) return;
      const scopeSlug = this.newInboxCategoryScope === "global" ? null : this.newInboxCategoryScope;
      try {
        const res = await this.apiFetch("/api/v1/notifications/categories", {
          method: "POST",
          body: JSON.stringify({ action: "add", category: cat, app_slug: scopeSlug }),
        });
        this.inboxCategoriesState = res;
        this.newInboxCategoryInput = "";
        await this.refreshCoreData(true);
        this.notify(`Added "${cat}" category (${scopeSlug || "Global"})`);
      } catch (err) {
        this.notify(err.message || "Could not add category", "error");
      }
    },

    async removeCustomInboxCategory(catName, appSlug = null) {
      try {
        const res = await this.apiFetch("/api/v1/notifications/categories", {
          method: "POST",
          body: JSON.stringify({ action: "remove", category: catName, app_slug: appSlug }),
        });
        this.inboxCategoriesState = res;
        if ((this.selectedCategoryFilter || "").toLowerCase() === String(catName).toLowerCase()) {
          this.selectedCategoryFilter = "All";
        }
        await this.refreshCoreData(true);
      } catch (err) {
        this.notify(err.message || "Could not remove category", "error");
      }
    },

    async runInboxAutoSort() {
      this.inboxAutoSorting = true;
      try {
        const q = this.selectedAppFilter && this.selectedAppFilter !== "All"
          ? `?app_filter=${encodeURIComponent(this.selectedAppFilter)}`
          : "";
        const res = await this.apiFetch(`/api/v1/notifications/autosort${q}`, { method: "POST" });
        await Promise.all([this.refreshCoreData(true), this.loadInboxCategories()]);
        const engs = Object.keys(res?.engines_used || {}).join(", ") || "auto";
        this.notify(`Sorted ${res?.sorted_count || 0} message(s) via ${engs}`);
      } catch (err) {
        this.notify(err.message || "Auto-sort failed", "error");
      } finally {
        this.inboxAutoSorting = false;
      }
    },

    // =========================================================================
    // v10.2: Tag-first Gmail-style Inbox (tags, filters, dedupe, body harvest)
    // =========================================================================
    async loadInboxTags() {
      try {
        const res = await this.apiFetch("/api/v1/inbox/tags");
        this.inboxTags = (res && res.tags) || [];
        const cfg = await this.apiFetch("/api/v1/inbox/filters");
        this.inboxSavedFilters = (cfg && cfg.filters) || [];
        this.inboxFilterGrammar = (cfg && cfg.grammar) || "";
        if (!this.inboxQuery && this.inboxActiveFilter === "important") {
          const def = this.inboxSavedFilters.find((f) => f.id === "important");
          if (def) this.inboxQuery = def.query;
        }
      } catch (_e) {}
    },

    extensionInfo: { installed: false, version: "", guard_token: "", zip_url: "", bridge_detail: "", newtab_mode: "custom" },

    async loadExtensionInfo() {
      try {
        this.extensionInfo = await this.apiFetch("/api/v1/ext/info");
      } catch (err) {
        this.notify(err.message || "Could not read extension info", "error");
      }
    },

    async copyExtensionToken() {
      const tok = this.extensionInfo && this.extensionInfo.guard_token;
      if (!tok) return;
      try {
        await navigator.clipboard.writeText(tok);
        this.notify("Guard token copied — paste it into the extension settings");
      } catch (_e) {
        this.notify("Copy blocked by the browser — select the token manually", "error");
      }
    },

    async patchOsSetting(key, value) {
      // Mirror into local state immediately so the button flips before the round trip.
      this.osSettings = { ...(this.osSettings || {}), [key]: value };
      this.notify("Saved");
      this.scheduleSettingsSync({ [key]: value });
    },

    async loadDecisionGateStatus() {
      try {
        this.decisionGateStatus = await this.apiFetch("/api/v1/decision-gate/status");
      } catch (_e) {}
    },

    async testDecisionGate() {
      this.decisionGateTesting = true;
      this.decisionGateResult = null;
      try {
        const res = await this.apiFetch("/api/v1/decision-gate/selftest", {
          method: "POST",
          body: JSON.stringify({ text: "Quiz 3 is due tonight at 11:59pm, worth 15% of the final grade." }),
        });
        this.decisionGateResult = res;
        this.decisionGateStatus = res.status || this.decisionGateStatus;
        this.notify(res.ok ? `Decision gate OK — ${res.latency_ms}ms` : "Decision gate offline (fallback active)", res.ok ? "success" : "error");
      } catch (err) {
        this.notify(err.message || "Decision gate self-test failed", "error");
      } finally {
        this.decisionGateTesting = false;
      }
    },

    parseInboxTags(raw) {
      if (Array.isArray(raw)) return raw.map((t) => String(t).trim()).filter(Boolean);
      return String(raw || "")
        .split(/[,;|]+/)
        .map((t) => t.trim())
        .filter(Boolean);
    },

    get inboxTagMap() {
      const map = {};
      for (const t of this.inboxTags || []) map[String(t.name).toLowerCase()] = t;
      return map;
    },

    tagColor(tagName) {
      const t = this.inboxTagMap[String(tagName || "").toLowerCase()];
      return (t && t.color) || "slate";
    },

    /**
     * Gmail-style query evaluation, mirrored client-side so every keystroke
     * filters instantly (the server endpoint is the source of truth for the AI).
     */
    matchesInboxQuery(n, query) {
      const q = String(query || "").trim();
      if (!q) return true;
      const tokens = q.match(/"[^"]*"|'[^']*'|\S+/g) || [];
      let anyGroup = [];
      const positives = [];
      const negatives = [];
      let inOr = false;
      for (const rawTok of tokens) {
        if (rawTok.toLowerCase() === "or" || rawTok === "|") { inOr = true; continue; }
        const negated = rawTok.startsWith("-");
        const tok = negated ? rawTok.slice(1) : rawTok;
        const item = { tok, negated };
        if (negated) negatives.push(item);
        else if (inOr) anyGroup.push(item);
        else positives.push(item);
      }
      const tags = this.parseInboxTags(n.tags_csv || n.category).map((t) => t.toLowerCase());
      const hay = `${n.title || ""} ${n.message || ""} ${n.ai_summary || ""} ${n.source_app || ""} ${(n.sender || "")} ${n.tags_csv || ""} ${n.category || ""}`.toLowerCase();
      const ts = n.timestamp ? new Date(n.timestamp).getTime() : 0;

      const ok = (item) => {
        const { tok } = item;
        const idx = tok.indexOf(":");
        const key = idx > 0 ? tok.slice(0, idx).toLowerCase() : "";
        const val = idx > 0 ? tok.slice(idx + 1).replace(/^["']|["']$/g, "").toLowerCase() : tok.toLowerCase();
        if (key === "tag" || key === "label" || key === "category") {
          if (val.endsWith("*")) return tags.some((t) => t.startsWith(val.slice(0, -1)));
          return tags.includes(val) || String(n.category || "").toLowerCase() === val;
        }
        if (key === "is") {
          if (val === "unread") return !n.read_state;
          if (val === "read") return !!n.read_state;
          if (val === "important") return n.is_important !== false;
          if (val === "unimportant") return n.is_important === false;
          if (val === "starred") return !!n.starred;
          if (val === "archived") return String(n.folder || "inbox") === "archive";
          return true;
        }
        if (key === "from" || key === "to") {
          return String(n.sender || "").toLowerCase().includes(val) || String(n.source_app || "").toLowerCase().includes(val);
        }
        if (key === "subject") return String(n.title || "").toLowerCase().includes(val);
        if (key === "source") return String(n.source_app || "").toLowerCase().includes(val);
        if (key === "body") return String(n.message || "").toLowerCase().includes(val) || String(n.ai_summary || "").toLowerCase().includes(val);
        if (key === "priority") return String(n.priority || "").toLowerCase() === val;
        if (key === "has") return val.startsWith("attach") ? !!n.has_attachment : true;
        if (key === "newer_than" || key === "older_than") {
          const m = val.match(/^(\d+)\s*([hdwmy])$/);
          if (!m || !ts) return true;
          const mult = { h: 3600e3, d: 86400e3, w: 604800e3, m: 2592e6, y: 31536e6 }[m[2]] || 86400e3;
          const delta = Date.now() - ts;
          const limit = Number(m[1]) * mult;
          return key === "newer_than" ? delta <= limit : delta >= limit;
        }
        return hay.includes(val || tok.toLowerCase());
      };

      if (!positives.every(ok)) return false;
      if (anyGroup.length && !anyGroup.some(ok)) return false;
      if (negatives.some(ok)) return false;
      return true;
    },

    get inboxStage() {
      // inbox | archive
      return String(this.inboxActiveFilter || "").toLowerCase() === "archive" ? "archive" : "inbox";
    },

    get inboxFilteredByQuery() {
      const stage = this.inboxStage;
      return (this.notifications || []).filter((n) => {
        const folder = String(n.folder || "inbox");
        if (stage === "archive" && folder !== "archive") return false;
        if (stage !== "archive" && folder === "archive" && !String(this.inboxQuery || "").includes("is:archived")) return false;
        return this.matchesInboxQuery(n, this.inboxQuery);
      });
    },

    /** Group by sender+topic so a thread reads as one line (Gmail conversation view). */
    get inboxGroups() {
      const rows = this.inboxFilteredByQuery.slice().sort((a, b) => {
        const ua = a.read_state ? 1 : 0;
        const ub = b.read_state ? 1 : 0;
        if (ua !== ub) return ua - ub;
        const sa = Number(a.importance_score ?? 0.5);
        const sb = Number(b.importance_score ?? 0.5);
        if (Math.abs(sb - sa) > 0.08) return sb - sa;
        return (b.id || 0) - (a.id || 0);
      });
      const groups = new Map();
      for (const n of rows) {
        const senderKey = String(n.sender || n.source_app || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
        const subjectKey = String(n.title || "")
          .toLowerCase()
          .replace(/^(re|fw|fwd)\s*:\s*/g, "")
          .replace(/[^a-z0-9]+/g, " ")
          .split(" ")
          .filter((w) => w.length > 2)
          .slice(0, 5)
          .join("-");
        const key = n.dedupe_key || `${senderKey}::${subjectKey}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(n);
      }
      return Array.from(groups.entries()).map(([key, items]) => ({
        key,
        items,
        latest: items[0],
        count: items.reduce((acc, it) => acc + (Number(it.merged_count) || 1), 0),
        unread: items.some((it) => !it.read_state),
        tags: Array.from(new Set(items.flatMap((it) => this.parseInboxTags(it.tags_csv || it.category)))),
      }));
    },

    get inboxTagFilterPills() {
      const counts = {};
      for (const n of this.notifications || []) {
        if (String(n.folder || "inbox") === "archive") continue;
        for (const t of this.parseInboxTags(n.tags_csv || n.category)) {
          const key = t.toLowerCase();
          counts[key] = counts[key] || { name: t, unread: 0, total: 0 };
          counts[key].total += 1;
          if (!n.read_state) counts[key].unread += 1;
        }
      }
      const ordered = (this.inboxTags || []).map((t) => {
        const c = counts[String(t.name).toLowerCase()] || { unread: 0, total: 0 };
        return { name: t.name, color: t.color, important: t.important, unread: c.unread, total: c.total };
      });
      const known = new Set(ordered.map((o) => o.name.toLowerCase()));
      for (const [k, v] of Object.entries(counts)) {
        if (!known.has(k)) ordered.push({ name: v.name, color: "slate", important: true, unread: v.unread, total: v.total });
      }
      return ordered.filter((o) => o.total > 0 || o.important);
    },

    applyInboxFilter(f) {
      this.inboxActiveFilter = f.id;
      this.inboxQuery = f.query || "";
      this.inboxShowDetail = false;
      this.pruneInboxSelection();
      this.expandedNotificationIds = {};
    },

    setInboxTagFilter(tagName) {
      const base = `tag:${String(tagName).toLowerCase()}`;
      const cur = String(this.inboxQuery || "");
      this.inboxQuery = cur.includes(base) ? cur.replace(base, "").trim() : (cur ? `${cur} ${base}` : base);
      this.inboxActiveFilter = "custom";
    },

    inboxBadgeCount() {
      return (this.notifications || []).filter((n) => !n.read_state && n.is_important !== false && String(n.folder || "inbox") !== "archive").length;
    },

    // ------------------------------------------------- open / read / detail
    async openInboxItem(n) {
      if (!n) return;
      this.inboxDetailId = n.id;
      this.inboxShowDetail = true;
      this.toggleExpandNotification(n.id);
      // Gmail behaviour: opening marks read after a short grace period so a
      // mis-click can be undone with the "Undo" toast.
      if (!n.read_state && this.osSettingsValue("inbox_mark_read_on_open", true)) {
        if (this.inboxReadGraceTimer) clearTimeout(this.inboxReadGraceTimer);
        this.inboxReadGraceId = n.id;
        this.inboxReadGraceTimer = setTimeout(() => this.commitInboxRead(n.id), 2600);
      }
      // Pull the real message body from the live tab (so the mail client is optional).
      const needsBody = !n.body_text || (n.body_text || "").length < 40;
      if (needsBody && this.osSettingsValue("inbox_auto_fetch_body", true)) {
        await this.harvestInboxBodies([n.id]);
      }
    },

    osSettingsValue(key, fallback = null) {
      const bag = this.osSettings || {};
      const v = bag.values && typeof bag.values === "object" && key in bag.values ? bag.values[key] : bag[key];
      return v === undefined || v === null ? fallback : v;
    },

    async commitInboxRead(id) {
      const row = (this.notifications || []).find((n) => n.id === id);
      if (!row || row.read_state) return;
      try {
        const updated = await this.apiFetch(`/api/v1/notifications/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ read_state: true }),
        });
        const idx = this.notifications.findIndex((n) => n.id === id);
        if (idx !== -1 && updated) this.notifications[idx] = updated;
        this.inboxReadGraceId = null;
        this.notify("Marked as read · Undo", "info", { actionLabel: "Undo", action: () => this.undoInboxRead(id) });
      } catch (_e) {}
    },

    async undoInboxRead(id) {
      try {
        const updated = await this.apiFetch(`/api/v1/notifications/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ read_state: false }),
        });
        const idx = this.notifications.findIndex((n) => n.id === id);
        if (idx !== -1 && updated) this.notifications[idx] = updated;
      } catch (_e) {}
    },

    closeInboxDetail() {
      this.inboxShowDetail = false;
      this.inboxDetailId = null;
    },

    get inboxDetailItem() {
      return (this.notifications || []).find((n) => n.id === this.inboxDetailId) || null;
    },

    get inboxDetailHasSelectors() {
      const cache = this.inboxSelectorCache[this.inboxDetailId];
      return Boolean(cache && cache.selectors);
    },

    async harvestInboxBodies(ids) {
      if (this.inboxHarvesting) return;
      this.inboxHarvesting = true;
      try {
        const res = await this.apiFetch("/api/v1/inbox/harvest", {
          method: "POST",
          body: JSON.stringify({ ids, limit: Math.max(1, Math.min(25, ids.length || 10)) }),
        });
        await this.refreshCoreData(true);
        if (res && res.harvested) this.notify(`Fetched ${res.harvested} message body(ies)`);
        else this.notify("Could not read that message from its live tab", "error");
      } catch (err) {
        this.notify(err.message || "Body fetch failed", "error");
      } finally {
        this.inboxHarvesting = false;
      }
    },

    // ------------------------------------------------------------- tagging
    async moveInboxItemToTag(n, tagName) {
      if (!n || !tagName) return;
      try {
        const res = await this.apiFetch(`/api/v1/inbox/${n.id}/tags`, {
          method: "POST",
          body: JSON.stringify({ tag: tagName, learn: true }),
        });
        const idx = this.notifications.findIndex((x) => x.id === n.id);
        if (idx !== -1 && res.notification) this.notifications[idx] = res.notification;
        this.inboxMoveMenuId = null;
        this.notify(`Moved to ${tagName} · the AI learned from this`);
        // Tag counts drift after a correction, so refresh the pill totals.
        setTimeout(() => this.refreshCoreData(true), 400);
      } catch (err) {
        this.notify(err.message || "Could not move message", "error");
      }
    },

    async bulkTagSelected(addTags, removeTags, setTags) {
      const ids = [...this.selectedNotificationIds];
      if (!ids.length) return;
      try {
        await this.apiFetch("/api/v1/inbox/bulk-tags", {
          method: "POST",
          body: JSON.stringify({ ids, add_tags: addTags || [], remove_tags: removeTags || [], set_tags: setTags }),
        });
        await this.refreshCoreData(true);
        this.notify(`Updated ${ids.length} message(s)`);
      } catch (err) {
        this.notify(err.message || "Bulk tag failed", "error");
      }
    },

    async archiveInboxItems(ids, archive = true) {
      try {
        await this.apiFetch("/api/v1/inbox/bulk-tags", {
          method: "POST",
          body: JSON.stringify({ ids, archive }),
        });
        await this.refreshCoreData(true);
        this.selectedNotificationIds = [];
        this.notify(archive ? `Archived ${ids.length}` : `Moved ${ids.length} to Inbox`);
      } catch (err) {
        this.notify(err.message || "Archive failed", "error");
      }
    },

    async starInboxItem(n) {
      if (!n) return;
      try {
        await this.apiFetch("/api/v1/inbox/bulk-tags", {
          method: "POST",
          body: JSON.stringify({ ids: [n.id], star: !n.starred }),
        });
        const idx = this.notifications.findIndex((x) => x.id === n.id);
        if (idx !== -1) this.notifications[idx] = { ...this.notifications[idx], starred: !n.starred };
      } catch (_e) {}
    },

    async dedupeInbox() {
      this.inboxAutoMerging = true;
      try {
        const res = await this.apiFetch("/api/v1/inbox/dedupe", { method: "POST" });
        await this.refreshCoreData(true);
        this.notify(`Merged ${res.merged_groups || 0} duplicate group(s), removed ${res.removed || 0}`);
      } catch (err) {
        this.notify(err.message || "Dedupe failed", "error");
      } finally {
        this.inboxAutoMerging = false;
      }
    },

    async saveInboxTag() {
      const f = this.inboxNewTagForm;
      if (!f.name || !f.name.trim()) return;
      try {
        const res = await this.apiFetch("/api/v1/inbox/tags", {
          method: "POST",
          body: JSON.stringify({
            name: f.name.trim(),
            description: f.description,
            color: f.color,
            important: Boolean(f.important),
          }),
        });
        this.inboxTags = (res && res.tags) || this.inboxTags;
        this.inboxNewTagForm = { name: "", description: "", color: "violet", important: true };
        this.notify(`Tag "${f.name}" added — the gate can identify it immediately`);
      } catch (err) {
        this.notify(err.message || "Could not save tag", "error");
      }
    },

    async updateInboxTag(tag, patch) {
      try {
        const res = await this.apiFetch("/api/v1/inbox/tags", {
          method: "POST",
          body: JSON.stringify({
            name: tag.name,
            description: patch.description !== undefined ? patch.description : tag.description,
            color: patch.color !== undefined ? patch.color : tag.color,
            important: patch.important !== undefined ? patch.important : tag.important,
          }),
        });
        this.inboxTags = (res && res.tags) || this.inboxTags;
      } catch (err) {
        this.notify(err.message || "Could not update tag", "error");
      }
    },

    async deleteInboxTag(tagName) {
      try {
        const res = await this.apiFetch(`/api/v1/inbox/tags/${encodeURIComponent(tagName)}`, { method: "DELETE" });
        this.inboxTags = (res && res.tags) || this.inboxTags.filter((t) => t.name !== tagName);
        this.notify(`Deleted tag ${tagName}`);
      } catch (err) {
        this.notify(err.message || "Cannot delete that tag", "error");
      }
    },

    async saveInboxFilter(name, query) {
      if (!name || !query) return;
      try {
        const res = await this.apiFetch("/api/v1/inbox/filters", {
          method: "POST",
          body: JSON.stringify({ name, query }),
        });
        this.inboxSavedFilters = (res && res.filters) || this.inboxSavedFilters;
        this.notify(`Saved filter "${name}"`);
      } catch (err) {
        this.notify(err.message || "Could not save filter", "error");
      }
    },

    async deleteInboxFilter(filterId) {
      try {
        const res = await this.apiFetch(`/api/v1/inbox/filters/${encodeURIComponent(filterId)}`, { method: "DELETE" });
        this.inboxSavedFilters = (res && res.filters) || this.inboxSavedFilters;
      } catch (err) {
        this.notify(err.message || "Cannot delete that filter", "error");
      }
    },

    inboxSenderLabel(n) {
      if (!n) return "";
      const s = String(n.sender || "").trim();
      if (s) {
        const local = s.includes("@") ? s.split("@")[0].replace(/[._]/g, " ") : s;
        return local.replace(/\b\w/g, (c) => c.toUpperCase()).slice(0, 28);
      }
      return String(n.source_app || "Unknown").slice(0, 28);
    },

    inboxTimeLabel(n) {
      if (!n || !n.timestamp) return "";
      const d = new Date(n.timestamp);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      if (sameDay) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      const days = Math.floor((now - d) / 86400000);
      if (days < 7) return d.toLocaleDateString([], { weekday: "short" });
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    },

    // =========================================================================
    // v10.1: Unified AI Instructions & Chat Rating Self-Improvement
    // =========================================================================
    async loadUnifiedInstructions(selectSlug = null) {
      try {
        const res = await this.apiFetch("/api/v1/ai/instructions");
        const files = (res && res.files) || [];
        this.unifiedInstructionsList = files;
        const target = selectSlug || this.activeInstructionSlug || "persona";
        await this.selectInstructionFile(target, true);
      } catch (_e) {}
    },

    async selectInstructionFile(slug, silent = false) {
      const clean = String(slug || "persona").trim();
      this.activeInstructionSlug = clean;
      try {
        const doc = await this.apiFetch(`/api/v1/ai/instructions/${encodeURIComponent(clean)}`);
        if (doc) {
          this.activeInstructionDoc = doc;
        }
      } catch (err) {
        if (!silent) this.notify(err.message || "Could not load instruction file", "error");
      }
    },

    async saveActiveInstructionFile() {
      const slug = this.activeInstructionSlug || "persona";
      this.instructionSaving = true;
      try {
        const saved = await this.apiFetch(`/api/v1/ai/instructions/${encodeURIComponent(slug)}`, {
          method: "PUT",
          body: JSON.stringify({ content: this.activeInstructionDoc.content || "" }),
        });
        this.activeInstructionDoc = saved;
        await Promise.all([this.loadUnifiedInstructions(slug), this.loadMemoryFiles()]);
        this.notify(`Saved ${saved.filename}`);
      } catch (err) {
        this.notify(err.message || "Save failed", "error");
      } finally {
        this.instructionSaving = false;
      }
    },

    async createUnifiedInstructionFile() {
      const raw = (this.newInstructionFileName || "").trim();
      if (!raw) return;
      const slug = raw.toLowerCase().replace(/\.md$/i, "").replace(/[^a-z0-9_-]+/g, "_");
      if (!slug) return;
      try {
        await this.apiFetch(`/api/v1/ai/instructions/${encodeURIComponent(slug)}`, {
          method: "PUT",
          body: JSON.stringify({
            content: `# ${raw.replace(/\.md$/i, "")}\n\n- Add standing instructions, workflows, or AI observations here.\n`,
          }),
        });
        this.newInstructionFileName = "";
        this.instructionEditMode = true;
        await this.loadUnifiedInstructions(slug);
        this.notify(`Created ${slug}.md`);
      } catch (err) {
        this.notify(err.message || "Could not create file", "error");
      }
    },

    async deleteUnifiedInstructionFile(fileItem) {
      if (!fileItem || !fileItem.deletable) return;
      try {
        await this.apiFetch(`/api/v1/ai/notebook/${encodeURIComponent(fileItem.slug)}`, { method: "DELETE" });
        this.activeInstructionSlug = "persona";
        await this.loadUnifiedInstructions("persona");
        this.notify(`Deleted ${fileItem.filename}`);
      } catch (err) {
        this.notify(err.message || "Could not delete file", "error");
      }
    },

    async rateChatMessage(msg, rating, threadId = "main") {
      if (!msg) return;
      const key = String(msg.id || msg.content?.slice(0, 40));
      if (rating === "poor" && this.poorFeedbackTargetMsg !== key) {
        this.poorFeedbackTargetMsg = key;
        this.poorFeedbackReason = "";
        return;
      }
      await this.submitChatFeedback(msg, rating, "", threadId);
    },

    async submitChatFeedback(msg, rating, customReason = "", threadId = "main") {
      const key = String(msg?.id || msg?.content?.slice(0, 40));
      try {
        await this.apiFetch("/api/v1/ai/feedback", {
          method: "POST",
          body: JSON.stringify({
            rating,
            message_id: typeof msg?.id === "number" ? msg.id : null,
            thread_id: threadId || "main",
            reason: customReason || this.poorFeedbackReason || "",
            assistant_reply: (msg?.content || "").slice(0, 1500),
          }),
        });
        this.chatFeedbackMap = { ...this.chatFeedbackMap, [key]: rating };
        this.poorFeedbackTargetMsg = null;
        this.poorFeedbackReason = "";
        if (rating === "poor") {
          await this.loadUnifiedInstructions(this.activeInstructionSlug);
          this.notify("Logged feedback & distilled lesson into self_improvement.md");
        } else {
          this.notify("Thanks — positive signal logged for next Cron reflection");
        }
      } catch (err) {
        this.notify(err.message || "Feedback failed", "error");
      }
    },

    async triggerSelfImproveNow() {
      this.selfImprovingNow = true;
      try {
        const res = await this.apiFetch("/api/v1/ai/self-improve", { method: "POST" });
        await this.loadUnifiedInstructions("self_improvement");
        this.notify(res?.processed_count ? `Distilled ${res.processed_count} feedback signal(s)` : "Self-improvement rules refreshed");
      } catch (err) {
        this.notify(err.message || "Reflection failed", "error");
      } finally {
        this.selfImprovingNow = false;
      }
    },

    // =========================================================================
    // v10.1: Mini File Explorer / Shared Workspace Drive
    // =========================================================================
    async loadDrive(relPath = null) {
      const targetPath = relPath !== null ? relPath : (this.drivePath || "");
      try {
        const q = `?path=${encodeURIComponent(targetPath)}&search=${encodeURIComponent(this.driveSearch || "")}`;
        const res = await this.apiFetch(`/api/v1/drive/tree${q}`);
        if (res) {
          this.drivePath = res.current_path || "";
          this.driveBreadcrumbs = res.breadcrumbs || [{ name: "Drive", path: "" }];
          this.driveItems = res.items || [];
          this.driveStats = res.stats || { total_files: 0, total_folders: 0, total_bytes: 0 };
        }
      } catch (_e) {}
    },

    async openDriveEntry(item) {
      if (!item) return;
      if (item.is_dir) {
        this.driveSearch = "";
        this.selectedDriveEntry = null;
        await this.loadDrive(item.path);
        return;
      }
      try {
        const res = await this.apiFetch(`/api/v1/drive/file?path=${encodeURIComponent(item.path)}`);
        if (res && res.file) {
          this.selectedDriveEntry = res.file;
          this.driveFileContent = res.file.content || "";
          this.driveFileEditable = Boolean(res.file.editable);
          this.drivePreviewMode = res.file.extension === ".md" || res.file.kind === "image" || res.file.kind === "pdf";
        }
      } catch (err) {
        this.notify(err.message || "Could not open file", "error");
      }
    },

    async saveDriveFile() {
      if (!this.selectedDriveEntry || !this.driveFileEditable) return;
      this.driveSaving = true;
      try {
        const res = await this.apiFetch("/api/v1/drive/file", {
          method: "PUT",
          body: JSON.stringify({
            path: this.selectedDriveEntry.path,
            content: this.driveFileContent,
          }),
        });
        if (res && res.file) this.selectedDriveEntry = res.file;
        await this.loadDrive(this.drivePath);
        this.notify(`Saved ${this.selectedDriveEntry.name}`);
      } catch (err) {
        this.notify(err.message || "Could not save file", "error");
      } finally {
        this.driveSaving = false;
      }
    },

    async submitNewDriveFolder() {
      const name = (this.driveNewFolderName || "").trim();
      if (!name) return;
      const fullPath = this.drivePath ? `${this.drivePath}/${name}` : name;
      try {
        await this.apiFetch("/api/v1/drive/folder", {
          method: "POST",
          body: JSON.stringify({ path: fullPath }),
        });
        this.driveNewFolderName = "";
        this.driveNewFolderOpen = false;
        await this.loadDrive(this.drivePath);
        this.notify(`Created folder "${name}"`);
      } catch (err) {
        this.notify(err.message || "Could not create folder", "error");
      }
    },

    async submitNewDriveFile() {
      const name = (this.driveNewFileName || "").trim();
      if (!name) return;
      const fullPath = this.drivePath ? `${this.drivePath}/${name}` : name;
      const starter = name.endsWith(".md") ? `# ${name.replace(/\.md$/i, "")}\n\n` : "";
      try {
        const res = await this.apiFetch("/api/v1/drive/file", {
          method: "PUT",
          body: JSON.stringify({ path: fullPath, content: starter }),
        });
        this.driveNewFileOpen = false;
        await this.loadDrive(this.drivePath);
        if (res && res.file) {
          this.selectedDriveEntry = res.file;
          this.driveFileContent = res.file.content || "";
          this.driveFileEditable = true;
          this.drivePreviewMode = false;
        }
        this.notify(`Created ${name}`);
      } catch (err) {
        this.notify(err.message || "Could not create file", "error");
      }
    },

    async handleDriveFileUpload(ev) {
      const files = ev?.target?.files || ev?.dataTransfer?.files;
      if (!files || !files.length) return;
      this.uploadingFile = true;
      try {
        for (const f of Array.from(files)) {
          const buf = await f.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let bin = "";
          const chunk = 8192;
          for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          await this.apiFetch("/api/v1/drive/upload", {
            method: "POST",
            body: JSON.stringify({
              folder: this.drivePath || "",
              filename: f.name,
              content_base64: btoa(bin),
            }),
          });
        }
        await Promise.all([this.loadDrive(this.drivePath), this.loadUploads()]);
        this.notify(`Uploaded ${files.length} file(s) to /${this.drivePath || "Drive"}`);
      } catch (err) {
        this.notify(err.message || "Upload failed", "error");
      } finally {
        this.uploadingFile = false;
        if (ev?.target && "value" in ev.target) ev.target.value = "";
      }
    },

    async renameDriveItem(item) {
      if (!item) return;
      const nextName = window.prompt("Rename or move path inside Drive:", item.path);
      if (!nextName || nextName.trim() === item.path) return;
      try {
        const res = await this.apiFetch("/api/v1/drive/move", {
          method: "POST",
          body: JSON.stringify({ src_path: item.path, dest_path: nextName.trim() }),
        });
        await this.loadDrive(this.drivePath);
        if (this.selectedDriveEntry && this.selectedDriveEntry.path === item.path && res?.entry) {
          await this.openDriveEntry(res.entry);
        }
        this.notify(`Moved to ${res?.entry?.path || nextName.trim()}`);
      } catch (err) {
        this.notify(err.message || "Rename/move failed", "error");
      }
    },

    async deleteDriveEntry(item) {
      if (!item) return;
      try {
        await this.apiFetch(`/api/v1/drive/item?path=${encodeURIComponent(item.path)}`, { method: "DELETE" });
        if (this.selectedDriveEntry && this.selectedDriveEntry.path === item.path) {
          this.selectedDriveEntry = null;
        }
        await Promise.all([this.loadDrive(this.drivePath), this.loadUploads()]);
        this.notify(`Deleted ${item.name}`);
      } catch (err) {
        this.notify(err.message || "Delete failed", "error");
      }
    },

    askAiAboutDriveFile(item) {
      if (!item) return;
      const tid = this.defaultContextId();
      this.openChatWindow(tid);
      this.ensureThreadState(tid);
      this.chatThreads[tid].input = `Read Drive file "${item.path}" using drive_explorer and summarize or act on it: `;
    },
  }));
});
