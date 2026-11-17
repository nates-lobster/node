/**
 * ============================================================================
 * Node WebOS — Safe Customization & Preset Runtime (`os_customizer.js`)
 * ============================================================================
 * PURPOSE (AI & Developer Navigation Guide):
 *   Isolates all user- and AI-authored visual/behavioral customizations from
 *   core OS kernels (authentication, SQLite APIs, binary WebSocket streaming).
 *
 * WHAT LIVES HERE:
 *   1. `applyDesignTokens(values)` — Maps typed `osSettings` keys onto `:root`
 *      CSS custom properties (`--accent`, `--win-radius`, `--dock-scale`, etc.)
 *   2. `applyCustomCss(cssText)` — Mounts/updates `<style id="node-preset-css">`
 *   3. `runSandboxedJs(jsText, osApp)` — Runs Preset JS against a disposable
 *      `api` facade (`registerShortcut`, `addTopbarWidget`, `onWindowOpen`,
 *      `setCssVar`, `setSetting`, etc.) and automatically cleans up all side
 *      effects whenever another Preset is loaded or reset!
 * ============================================================================
 */
(function () {
  "use strict";

  const STYLE_TAG_ID = "node-preset-custom-css";

  // Active disposers from the currently loaded preset (cleaned before every re-run)
  let activeCleanupFns = [];
  let windowOpenCallbacks = [];

  function hexToRgb(hex) {
    const clean = String(hex || "#2bfbae").replace("#", "").trim();
    if (!/^[0-9a-fA-F]{6}$/.test(clean)) return { r: 43, g: 251, b: 174 };
    return {
      r: parseInt(clean.slice(0, 2), 16),
      g: parseInt(clean.slice(2, 4), 16),
      b: parseInt(clean.slice(4, 6), 16),
    };
  }

  function shadeRgb({ r, g, b }, factor) {
    const clamp = (n) => Math.max(0, Math.min(255, Math.round(n)));
    return `rgb(${clamp(r * factor)}, ${clamp(g * factor)}, ${clamp(b * factor)})`;
  }

  function applyDesignTokens(values) {
    if (!values || typeof values !== "object") return;
    const root = document.documentElement;

    // 1. Theme & Blur Class
    const isLight = values.theme === "light";
    root.classList.toggle("theme-light", isLight);
    root.classList.toggle("lite-glass", Boolean(values.lite_glass));

    // 2. Dynamic Accent Palette Derivation (ensures contrast on buttons & borders)
    const accentHex = values.accent_color || (isLight ? "#099e68" : "#2bfbae");
    const rgb = hexToRgb(accentHex);
    const luminance = (0.299 * rgb.r + 0.587 * rgb.g + 0.114 * rgb.b) / 255;
    root.style.setProperty("--accent", accentHex);
    root.style.setProperty("--accent-strong", shadeRgb(rgb, 0.86));
    root.style.setProperty("--accent-ink", luminance > 0.56 ? "#041f15" : "#ffffff");
    root.style.setProperty("--accent-soft", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.13)`);
    root.style.setProperty("--accent-border", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.36)`);
    root.style.setProperty("--accent-glow", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.18)`);

    // 3. Geometry, Opacity & Scale Tokens
    const radius = Number.isFinite(Number(values.win_radius)) ? Number(values.win_radius) : 11;
    const borderW = Number.isFinite(Number(values.tile_border_width)) ? Number(values.tile_border_width) : 2;
    const opacityPct = Number.isFinite(Number(values.win_opacity)) ? Number(values.win_opacity) : 96;
    const alpha = Math.max(0.75, Math.min(1, opacityPct / 100));
    const dockScale = (Number(values.dock_scale) || 100) / 100;
    const uiScale = (Number(values.ui_scale) || 100) / 100;

    root.style.setProperty("--win-radius", `${radius}px`);
    root.style.setProperty("--tile-border-width", `${borderW}px`);
    root.style.setProperty("--dock-scale", String(dockScale));
    root.style.setProperty("--ui-scale", String(uiScale));
    root.style.fontSize = `${Math.round(16 * uiScale * 100) / 100}px`;

    if (values.surface_bg && /^#[0-9a-fA-F]{6}$/.test(values.surface_bg)) {
      const srgb = hexToRgb(values.surface_bg);
      root.style.setProperty("--bg-win", `rgba(${srgb.r}, ${srgb.g}, ${srgb.b}, ${alpha})`);
      root.style.setProperty("--bg-win-focused", `rgba(${srgb.r}, ${srgb.g}, ${srgb.b}, ${Math.min(1, alpha + 0.03)})`);
      root.style.setProperty("--surface", values.surface_bg);
    } else if (isLight) {
      root.style.setProperty("--bg-win", `rgba(255, 255, 255, ${alpha})`);
      root.style.setProperty("--bg-win-focused", `rgba(255, 255, 255, ${Math.min(1, alpha + 0.02)})`);
      root.style.removeProperty("--surface");
    } else {
      root.style.setProperty("--bg-win", `rgba(13, 19, 29, ${alpha})`);
      root.style.setProperty("--bg-win-focused", `rgba(14, 21, 32, ${Math.min(1, alpha + 0.02)})`);
      root.style.removeProperty("--surface");
    }

    if (values.canvas_bg && /^#[0-9a-fA-F]{6}$/.test(values.canvas_bg)) {
      root.style.setProperty("--bg-base", values.canvas_bg);
      root.style.setProperty("--bg-canvas", values.canvas_bg);
      document.body && (document.body.style.backgroundColor = values.canvas_bg);
    } else {
      root.style.removeProperty("--bg-base");
      root.style.removeProperty("--bg-canvas");
      document.body && (document.body.style.backgroundColor = "");
    }

    if (values.card_bg && /^#[0-9a-fA-F]{6}$/.test(values.card_bg)) {
      const crgb = hexToRgb(values.card_bg);
      root.style.setProperty("--card-bg", values.card_bg);
      root.style.setProperty("--bg-card", `rgba(${crgb.r}, ${crgb.g}, ${crgb.b}, 0.94)`);
      root.style.setProperty("--bg-card-solid", values.card_bg);
    } else {
      root.style.removeProperty("--card-bg");
      root.style.removeProperty("--bg-card");
      root.style.removeProperty("--bg-card-solid");
    }

    if (values.text_color && /^#[0-9a-fA-F]{6}$/.test(values.text_color)) {
      root.style.setProperty("--ink-main", values.text_color);
      root.style.setProperty("--text-primary", values.text_color);
      document.body && (document.body.style.color = values.text_color);
    } else {
      root.style.removeProperty("--ink-main");
      root.style.removeProperty("--text-primary");
      document.body && (document.body.style.color = "");
    }

    const fontMap = {
      system: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      inter: "Inter, -apple-system, BlinkMacSystemFont, sans-serif",
      geist: "'Geist', Inter, -apple-system, sans-serif",
      "ibm-plex": "'IBM Plex Sans', Inter, sans-serif",
      serif: "Georgia, 'Iowan Old Style', 'Times New Roman', serif",
      "jetbrains-mono": "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
    };
    const chosenFont = fontMap[values.font_family] || fontMap.system;
    root.style.setProperty("--ui-font", chosenFont);
    if (document.body) document.body.style.fontFamily = chosenFont;

    // 4. Optional Hide-Window-Topbars-in-Tiling Toggle
    root.classList.toggle("keep-tiled-win-bars", values.hide_win_topbar_in_tiling === false);
  }

  function applyCustomCss(cssText) {
    let el = document.getElementById(STYLE_TAG_ID);
    const clean = String(cssText || "").trim();
    if (!clean) {
      if (el) el.textContent = "";
      return;
    }
    if (!el) {
      el = document.createElement("style");
      el.id = STYLE_TAG_ID;
      document.head.appendChild(el);
    }
    el.textContent = clean;
  }

  function disposeActiveHooks(osApp) {
    while (activeCleanupFns.length > 0) {
      const fn = activeCleanupFns.pop();
      try {
        fn();
      } catch (_e) {}
    }
    windowOpenCallbacks = [];
    if (osApp && Array.isArray(osApp.customTopbarWidgets)) {
      osApp.customTopbarWidgets = [];
    }
    if (osApp && Array.isArray(osApp.customShortcuts)) {
      osApp.customShortcuts = [];
    }
  }

  function parseShortcutCombo(combo) {
    const parts = String(combo || "")
      .toLowerCase()
      .split("+")
      .map((s) => s.trim())
      .filter(Boolean);
    return {
      alt: parts.includes("alt"),
      ctrl: parts.includes("ctrl") || parts.includes("control"),
      shift: parts.includes("shift"),
      meta: parts.includes("meta") || parts.includes("cmd"),
      key: parts[parts.length - 1] || "",
    };
  }

  function runSandboxedJs(jsText, osApp) {
    disposeActiveHooks(osApp);
    const code = String(jsText || "").trim();
    if (!code) {
      return { ok: true, error: "" };
    }

    const root = document.documentElement;
    const touchedVars = [];

    const api = Object.freeze({
      setSetting(key, value) {
        if (osApp && typeof osApp.setSingleOsSetting === "function") {
          osApp.setSingleOsSetting(String(key), value, false);
        }
      },
      getSetting(key) {
        return osApp && osApp.osSettings ? osApp.osSettings[key] : undefined;
      },
      setCssVar(varName, value) {
        const name = String(varName || "");
        if (!name.startsWith("--")) return;
        const prev = root.style.getPropertyValue(name);
        touchedVars.push([name, prev]);
        root.style.setProperty(name, String(value));
      },
      registerShortcut(combo, handler, label = "") {
        if (typeof handler !== "function") return;
        const spec = parseShortcutCombo(combo);
        if (!spec.key) return;
        const listener = (ev) => {
          const tag = (ev.target && ev.target.tagName) || "";
          if ((tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") && !spec.alt && !spec.ctrl && !spec.meta) {
            return;
          }
          if (
            Boolean(ev.altKey) === spec.alt &&
            Boolean(ev.ctrlKey) === spec.ctrl &&
            Boolean(ev.shiftKey) === spec.shift &&
            Boolean(ev.metaKey) === spec.meta &&
            String(ev.key || "").toLowerCase() === spec.key
          ) {
            ev.preventDefault();
            try {
              handler();
            } catch (err) {
              console.warn("[NodeCustomizer shortcut error]", err);
            }
          }
        };
        window.addEventListener("keydown", listener);
        activeCleanupFns.push(() => window.removeEventListener("keydown", listener));
        if (osApp && Array.isArray(osApp.customShortcuts)) {
          osApp.customShortcuts.push({ combo: String(combo), label: String(label || combo) });
        }
      },
      addTopbarWidget(widget) {
        if (!widget || !osApp || !Array.isArray(osApp.customTopbarWidgets)) return;
        const id = String(widget.id || `w-${osApp.customTopbarWidgets.length + 1}`);
        const item = {
          id,
          label: String(widget.label || "Custom").slice(0, 32),
          title: String(widget.title || widget.label || "Custom preset action").slice(0, 120),
          onClick: typeof widget.onClick === "function" ? widget.onClick : () => {},
        };
        osApp.customTopbarWidgets = [...osApp.customTopbarWidgets.filter((x) => x.id !== id), item];
      },
      onWindowOpen(cb) {
        if (typeof cb === "function") {
          windowOpenCallbacks.push(cb);
        }
      },
      openWindow(view) {
        if (osApp && typeof osApp.openWindow === "function") {
          osApp.openWindow(String(view || "tasks"));
        }
      },
      closeFocusedWindow() {
        if (osApp && osApp.focusedWinId) {
          osApp.closeWindow(osApp.focusedWinId);
        }
      },
      toggleTiling() {
        if (osApp && typeof osApp.toggleWmMode === "function") osApp.toggleWmMode();
      },
      equalizeTiles() {
        if (osApp && typeof osApp.equalizeTiles === "function") osApp.equalizeTiles();
      },
      toggleDockAutoHide() {
        if (osApp && typeof osApp.toggleDockAutoHide === "function") osApp.toggleDockAutoHide();
      },
      runCronCheck() {
        if (osApp && typeof osApp.runCronNow === "function") osApp.runCronNow();
      },
      startZen(seconds = 60) {
        if (osApp && typeof osApp.startZenBreather === "function") osApp.startZenBreather(Number(seconds) || 60);
      },
      notify(msg) {
        if (osApp && typeof osApp.notify === "function") osApp.notify(String(msg || ""));
      },
    });

    activeCleanupFns.push(() => {
      while (touchedVars.length > 0) {
        const [k, prev] = touchedVars.pop();
        if (prev) root.style.setProperty(k, prev);
        else root.style.removeProperty(k);
      }
    });

    try {
      // Execute in isolated strict scope with only `api` injected
      const runner = new Function("api", `"use strict";\n${code}`);
      runner(api);
      return { ok: true, error: "" };
    } catch (err) {
      disposeActiveHooks(osApp);
      return { ok: false, error: (err && err.message) ? String(err.message) : "Custom JS runtime error" };
    }
  }

  function emitWindowOpened(win) {
    for (const cb of windowOpenCallbacks) {
      try {
        cb({ id: win.id, kind: win.kind, view: win.view || null, title: win.title || "" });
      } catch (_e) {}
    }
  }

  window.NodeCustomizer = {
    applyDesignTokens,
    applyCustomCss,
    runSandboxedJs,
    disposeActiveHooks,
    emitWindowOpened,
  };
})();
