# Node Apps & Desktop Widgets Developer SDK (`v10.0`)

Node includes a first-class, sandboxed **Custom Apps & Desktop Widgets** runtime inspired by browser extensions (`Manifest V3`) and Raycast/macOS widgets. Users and developers can package custom tools as a single `.nodeapp.json` file or a `.zip` archive (`manifest.json` + `app.html` + `widget.html`) and install them in one click from **Extensions Studio**.

---

## 1. Package Formats

### Option A — Single-File `.nodeapp.json` (Fastest)
```json
{
  "format": "nodeapp-v1",
  "manifest": {
    "id": "flashcard-deck",
    "name": "Spaced Repetition Deck",
    "version": "1.0.0",
    "author": "Your Name",
    "description": "Quick active-recall flashcards synced with Node storage & Tasks.",
    "icon": "layers",
    "permissions": ["storage", "tasks.read", "grades.read", "notifications.send", "ai.ask"],
    "provides_app": true,
    "provides_widget": true,
    "default_widget_size": {
      "col_span": 3,
      "min_height": 206
    }
  },
  "html": "<!doctype html><html><body>...full window app UI...</body></html>",
  "widget_html": "<!doctype html><html><body>...compact desktop widget UI...</body></html>"
}
```

### Option B — `.zip` Archive
Create a `.zip` file containing:
- `manifest.json` *(required)* — metadata, declared `permissions`, and default widget size (`col_span` `2`–`12`, `min_height` `140`–`680`).
- `app.html` *(optional if widget-only)* — HTML/CSS/JS rendered when opened as a tiled or floating OS window.
- `widget.html` *(optional)* — compact HTML/CSS/JS rendered inside the resizable 12-column Desktop Widget Grid (falls back to `app.html` if omitted).

---

## 2. Security & Sandboxing Model

1. **Origin Isolation:** Every custom app and desktop widget runs inside `<iframe sandbox="allow-scripts">` (`srcdoc`). It **cannot** read `document.cookie`, `localStorage` of the host OS, `X-Node-Session`, or touch `/api/v1/auth/*`.
2. **Permission-Checked RPC Bridge:** Node automatically injects `window.NodeOS` into `<head>`. Every `NodeOS.*` call sends a structured `postMessage` request (`NODEOS_RPC_CALL`) to the WebOS kernel, which verifies the extension's `manifest.permissions` before executing.
3. **Live Theme Synchronization:** Host CSS custom properties (`--accent`, `--bg`, `--ink`, `--muted`, `--border`) are automatically injected and updated via `NODEOS_THEME_UPDATE` whenever the user switches themes!

---

## 3. `window.NodeOS` JavaScript API Reference

All methods return native Promises:

### Persistent Extension Storage (`"storage"`)
Backed by `/data/extensions/<extension-id>/storage.json` on the Node server (persists across browsers, devices, and Tailscale sessions):
```js
// Read JSON object
const state = await NodeOS.storage.get();

// Merge patch into storage (pass `false` as 2nd arg to overwrite completely)
await NodeOS.storage.set({ count: (state.count || 0) + 1 }, true);
```

### Tasks (`"tasks.read"`, `"tasks.write"`)
```js
const tasks = await NodeOS.tasks.list();

await NodeOS.tasks.create({
  title: "Review Thermodynamics Ch. 4",
  priority: "high",          // "critical" | "high" | "normal" | "low"
  estimated_minutes: 30,
  due_date: "2026-10-28"
});
```

### Calendar, LMS Grades & Important Inbox (`"calendar.read"`, `"grades.read"`, `"inbox.read"`)
```js
const events = await NodeOS.calendar.list();
const gradesDashboard = await NodeOS.grades.list(); // PowerSchool + Canvas + Schoology + Classroom
const unreadItems = await NodeOS.inbox.list();
```

### Server-Side Background AI (`"ai.ask"`)
Submits a prompt to Node's **server-side `AIJob` queue** (runs on whichever local Ollama, remote Ollama, or Cloud API model is configured):
```js
const job = await NodeOS.ai.ask("Summarize my top 3 missing assignments into a 45m study plan");
```

### OS UI Helpers (`"notifications.send"`)
```js
await NodeOS.notify("Sprint finished — take a 5m breather!");
await NodeOS.openWindow("calendar");          // Open built-in OS app
await NodeOS.openWindow("ext:flashcard-deck"); // Open your full Custom App window from a Desktop Widget!
```

### Theme Hooks (Always Permitted)
```js
const theme = await NodeOS.getTheme(); // { accent, bg, ink, muted, border, isLight }
NodeOS.onThemeChange((nextTheme) => {
  console.log("Theme updated:", nextTheme.accent);
});
```

---

## 4. Installing & Testing via CLI or UI

- **In WebOS:** Open **Start Menu → Extensions & SDK** (or click **Customize Desktop → + Add Widget → Build / Install Extension**), upload a `.nodeapp.json` / `.zip` file, or paste HTML directly in the built-in Extension Builder.
- **Via REST API:**
  ```bash
  curl -X POST http://localhost:8080/api/v1/extensions/install \
    -H "Content-Type: application/json" \
    -H "X-Node-Session: <TOKEN>" \
    -d @my-extension.nodeapp.json
  ```
