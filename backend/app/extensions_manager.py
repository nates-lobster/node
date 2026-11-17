"""
Custom Apps & Desktop Widgets Extension System + Resizable Desktop Layout Manager (Node v10.0).

Allows users and developers to build, install (`.nodeapp.json` or `.zip`), edit, export, and run
sandboxed Custom Apps (full tiled/floating OS windows) and Custom Desktop Widgets backed by the
`window.NodeOS` postMessage RPC SDK.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings


VALID_PERMISSIONS = {
    "storage",
    "tasks.read",
    "tasks.write",
    "calendar.read",
    "grades.read",
    "inbox.read",
    "notifications.send",
    "ai.ask",
}


NODEOS_BRIDGE_SCRIPT = r"""
<script>
(function() {
  const EXT_ID = window.__NODE_EXT_ID__ || 'custom-ext';
  let reqSeq = 0;
  const pending = new Map();
  const themeListeners = [];

  window.addEventListener('message', (ev) => {
    const msg = ev.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'NODEOS_RPC_RESULT' && pending.has(msg.reqId)) {
      const { resolve, reject } = pending.get(msg.reqId);
      pending.delete(msg.reqId);
      if (msg.ok) resolve(msg.data);
      else reject(new Error(msg.error || 'NodeOS RPC error'));
    } else if (msg.type === 'NODEOS_THEME_UPDATE') {
      applyHostTheme(msg.theme || {});
      for (const fn of themeListeners) { try { fn(msg.theme || {}); } catch (_) {} }
    }
  });

  function applyHostTheme(t) {
    const r = document.documentElement;
    if (t.accent) r.style.setProperty('--accent', t.accent);
    if (t.bg) r.style.setProperty('--bg', t.bg);
    if (t.ink) r.style.setProperty('--ink', t.ink);
    if (t.muted) r.style.setProperty('--muted', t.muted);
    if (t.border) r.style.setProperty('--border', t.border);
  }

  function callHost(method, params = {}) {
    return new Promise((resolve, reject) => {
      const reqId = `${EXT_ID}-${++reqSeq}-${Date.now()}`;
      pending.set(reqId, { resolve, reject });
      window.parent.postMessage({ type: 'NODEOS_RPC_CALL', extId: EXT_ID, reqId, method, params }, '*');
      setTimeout(() => {
        if (pending.has(reqId)) {
          pending.delete(reqId);
          reject(new Error(`NodeOS.${method} timed out`));
        }
      }, 25000);
    });
  }

  window.NodeOS = {
    extensionId: EXT_ID,
    getTheme: () => callHost('theme.get'),
    onThemeChange: (cb) => { if (typeof cb === 'function') themeListeners.push(cb); },
    storage: {
      get: () => callHost('storage.get'),
      set: (data, merge = true) => callHost('storage.set', { data, merge }),
    },
    tasks: {
      list: () => callHost('tasks.list'),
      create: (task) => callHost('tasks.create', task),
    },
    calendar: {
      list: () => callHost('calendar.list'),
    },
    grades: {
      list: () => callHost('grades.list'),
    },
    inbox: {
      list: () => callHost('inbox.list'),
    },
    ai: {
      ask: (prompt, thread_id = 'main') => callHost('ai.ask', { prompt, thread_id }),
    },
    notify: (message) => callHost('ui.notify', { message }),
    openWindow: (view) => callHost('ui.openWindow', { view }),
  };

  window.addEventListener('DOMContentLoaded', () => {
    callHost('theme.get').then(applyHostTheme).catch(() => {});
  });
})();
</script>
<style>
  :root {
    --accent: #2bfbae;
    --bg: transparent;
    --ink: #f1f5f9;
    --muted: #94a3b8;
    --border: rgba(255,255,255,0.09);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px;
  }
  button { cursor: pointer; font-family: inherit; transition: transform 0.08s ease, opacity 0.15s ease; }
  button:active { transform: scale(0.96); }
</style>
"""


# ---------------------------------------------------------------------------
# Built-In Starter Extension Gallery (Optional 1-Click Install — Not Forced!)
# ---------------------------------------------------------------------------
STARTER_EXTENSION_GALLERY: List[Dict[str, Any]] = [
    {
        "manifest": {
            "id": "pomodoro-studio",
            "name": "Pomodoro Deep-Work Timer",
            "version": "1.0.0",
            "author": "Node SDK Gallery",
            "description": "25/5 & 50/10 focus sprint timer with persistent session streak counter and OS toast notifications.",
            "icon": "clock",
            "permissions": ["storage", "notifications.send", "tasks.read"],
            "provides_app": True,
            "provides_widget": True,
            "default_widget_size": {"col_span": 3, "min_height": 206},
        },
        "html": """<!doctype html><html><body style="padding:18px;">
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
  <div>
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--accent);font-weight:700;">Focus Studio</div>
    <h2 style="margin:2px 0 0;font-size:18px;">Pomodoro Sprint Timer</h2>
  </div>
  <span id="streak" style="font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--border);">0 sprints</span>
</div>
<div style="text-align:center;padding:22px;border-radius:14px;border:1px solid var(--border);background:rgba(255,255,255,0.03);margin-bottom:14px;">
  <div id="clock" style="font-size:46px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-0.02em;">25:00</div>
  <div id="modeLbl" style="font-size:12px;color:var(--muted);margin-top:4px;">Ready for Deep Work</div>
  <div style="display:flex;gap:8px;justify-content:center;margin-top:14px;">
    <button id="toggleBtn" style="padding:8px 18px;border-radius:9px;border:none;background:var(--accent);color:#042015;font-weight:700;">Start</button>
    <button id="resetBtn" style="padding:8px 14px;border-radius:9px;border:1px solid var(--border);background:transparent;color:var(--ink);">Reset</button>
    <button id="p50Btn" style="padding:8px 14px;border-radius:9px;border:1px solid var(--border);background:transparent;color:var(--muted);">50m Mode</button>
  </div>
</div>
<div id="topTask" style="font-size:12px;color:var(--muted);"></div>
<script>
let total = 25 * 60, rem = total, timer = null, sprints = 0;
const fmt = (s) => String(Math.floor(s/60)).padStart(2,'0') + ':' + String(s%60).padStart(2,'0');
const render = () => { document.getElementById('clock').textContent = fmt(rem); document.getElementById('streak').textContent = sprints + ' sprints'; };
NodeOS.storage.get().then(st => { if (st && st.sprints) { sprints = st.sprints; render(); } }).catch(()=>{});
NodeOS.tasks.list().then(ts => { if (ts && ts.length) document.getElementById('topTask').textContent = 'Target Task: ' + ts[0].title; }).catch(()=>{});
document.getElementById('toggleBtn').onclick = () => {
  if (timer) { clearInterval(timer); timer = null; document.getElementById('toggleBtn').textContent = 'Resume'; return; }
  document.getElementById('toggleBtn').textContent = 'Pause';
  document.getElementById('modeLbl').textContent = 'Focusing…';
  timer = setInterval(() => {
    rem--; render();
    if (rem <= 0) {
      clearInterval(timer); timer = null; sprints++; rem = total; render();
      NodeOS.storage.set({ sprints });
      NodeOS.notify('Pomodoro sprint complete! Great focus.');
      document.getElementById('toggleBtn').textContent = 'Start';
    }
  }, 1000);
};
document.getElementById('resetBtn').onclick = () => { if (timer) clearInterval(timer); timer = null; rem = total; render(); document.getElementById('toggleBtn').textContent = 'Start'; };
document.getElementById('p50Btn').onclick = () => { total = total === 1500 ? 3000 : 1500; rem = total; render(); document.getElementById('p50Btn').textContent = total === 1500 ? '50m Mode' : '25m Mode'; };
</script></body></html>""",
        "widget_html": """<!doctype html><html><body style="padding:12px;display:flex;flex-direction:column;justify-content:space-between;height:100vh;">
<div style="display:flex;justify-content:space-between;align-items:center;">
  <span style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;">Pomodoro</span>
  <span id="wStreak" style="font-size:11px;color:var(--accent);font-weight:600;">0 done</span>
</div>
<div style="text-align:center;margin:8px 0;">
  <div id="wClock" style="font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;">25:00</div>
</div>
<div style="display:flex;gap:6px;">
  <button id="wStart" style="flex:1;padding:6px;border-radius:8px;border:none;background:var(--accent);color:#042015;font-weight:700;font-size:11.5px;">Start</button>
  <button id="wReset" style="padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--ink);font-size:11.5px;">↺</button>
</div>
<script>
let rem = 1500, t = null, sp = 0;
const draw = () => { document.getElementById('wClock').textContent = String(Math.floor(rem/60)).padStart(2,'0')+':'+String(rem%60).padStart(2,'0'); document.getElementById('wStreak').textContent = sp+' done'; };
NodeOS.storage.get().then(s => { if (s && s.sprints) { sp = s.sprints; draw(); } }).catch(()=>{});
document.getElementById('wStart').onclick = () => {
  if (t) { clearInterval(t); t = null; document.getElementById('wStart').textContent = 'Resume'; return; }
  document.getElementById('wStart').textContent = 'Pause';
  t = setInterval(() => { rem--; draw(); if (rem <= 0) { clearInterval(t); t = null; sp++; rem = 1500; draw(); NodeOS.storage.set({ sprints: sp }); NodeOS.notify('25m Sprint Complete!'); document.getElementById('wStart').textContent = 'Start'; } }, 1000);
};
document.getElementById('wReset').onclick = () => { if (t) clearInterval(t); t = null; rem = 1500; draw(); document.getElementById('wStart').textContent = 'Start'; };
</script></body></html>""",
    },
    {
        "manifest": {
            "id": "gpa-simulator",
            "name": "GPA What-If Simulator",
            "version": "1.0.0",
            "author": "Node SDK Gallery",
            "description": "Loads your live multi-LMS courses (PowerSchool, Canvas, Schoology) and lets you simulate hypothetical exam/project scores in real time.",
            "icon": "chart",
            "permissions": ["grades.read", "storage"],
            "provides_app": True,
            "provides_widget": True,
            "default_widget_size": {"col_span": 4, "min_height": 210},
        },
        "html": """<!doctype html><html><body style="padding:18px;">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
  <div>
    <div style="font-size:11px;text-transform:uppercase;color:var(--accent);font-weight:700;">Academic Sandbox</div>
    <h2 style="margin:2px 0 0;font-size:18px;">Live GPA What-If Simulator</h2>
  </div>
  <div id="gpaOut" style="font-size:22px;font-weight:800;color:var(--accent);">GPA —</div>
</div>
<div id="rows" style="display:flex;flex-direction:column;gap:8px;">Loading live LMS courses…</div>
<script>
const pctToGpa = (p) => p>=93?4.0:p>=90?3.7:p>=87?3.3:p>=83?3.0:p>=80?2.7:p>=77?2.3:p>=73?2.0:p>=70?1.7:p>=67?1.3:p>=60?1.0:0.0;
let courses = [];
function recalc() {
  const nums = courses.map(c => parseFloat(c.sim)).filter(n => !isNaN(n));
  if (!nums.length) { document.getElementById('gpaOut').textContent = 'No scored courses yet'; return; }
  const gpa = (nums.reduce((a,b)=>a+pctToGpa(b),0)/nums.length).toFixed(2);
  const avg = (nums.reduce((a,b)=>a+b,0)/nums.length).toFixed(1);
  document.getElementById('gpaOut').textContent = `${gpa} GPA (${avg}%)`;
}
NodeOS.grades.list().then(data => {
  const list = (data && data.courses) || [];
  if (!list.length) { document.getElementById('rows').innerHTML = '<div style="color:var(--muted);">Connect PowerSchool, Canvas, or Schoology in Grades to simulate scores!</div>'; return; }
  courses = list.map(c => ({ name: c.course_name, src: c.source_app, actual: c.score_percent ?? 92, sim: c.score_percent ?? 92 }));
  const box = document.getElementById('rows'); box.innerHTML = '';
  courses.forEach((c, idx) => {
    const r = document.createElement('div');
    r.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,0.02);';
    r.innerHTML = `<div style="flex:1;"><div style="font-weight:600;">${c.name}</div><div style="font-size:11px;color:var(--muted);">${c.src} • Actual: ${c.actual}%</div></div><input type="range" min="50" max="100" step="0.5" value="${c.sim}" style="width:130px;"><span id="v${idx}" style="width:52px;text-align:right;font-weight:700;">${c.sim}%</span>`;
    r.querySelector('input').oninput = (e) => { c.sim = parseFloat(e.target.value); r.querySelector('#v'+idx).textContent = c.sim.toFixed(1)+'%'; recalc(); };
    box.appendChild(r);
  });
  recalc();
});
</script></body></html>""",
        "widget_html": """<!doctype html><html><body style="padding:12px;">
<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;">GPA What-If</div>
<div id="wgpa" style="font-size:24px;font-weight:800;color:var(--accent);margin:8px 0 4px;">—</div>
<div id="wsub" style="font-size:11.5px;color:var(--muted);margin-bottom:10px;">Syncs with LMS Grades</div>
<button onclick="NodeOS.openWindow('ext:gpa-simulator')" style="width:100%;padding:6px;border-radius:8px;border:1px solid var(--border);background:rgba(255,255,255,0.04);color:var(--ink);font-size:11.5px;">Open Simulator →</button>
<script>
NodeOS.grades.list().then(d => {
  const s = d && d.summary;
  if (s && s.unweighted_gpa != null) {
    document.getElementById('wgpa').textContent = s.unweighted_gpa.toFixed(2) + ' GPA';
    document.getElementById('wsub').textContent = s.total_courses + ' courses • Avg ' + (s.average_percent||0).toFixed(1) + '%';
  }
}).catch(()=>{});
</script></body></html>""",
    },
    {
        "manifest": {
            "id": "milestone-countdown",
            "name": "Exam & Milestone Countdown",
            "version": "1.0.0",
            "author": "Node SDK Gallery",
            "description": "Track days & hours remaining until AP Exams, project launches, auditions, or semester finals.",
            "icon": "calendar",
            "permissions": ["storage"],
            "provides_app": True,
            "provides_widget": True,
            "default_widget_size": {"col_span": 3, "min_height": 206},
        },
        "html": """<!doctype html><html><body style="padding:18px;">
<h2 style="margin:0 0 12px;font-size:17px;">Exam & Milestone Countdowns</h2>
<div style="display:flex;gap:8px;margin-bottom:14px;">
  <input id="mTitle" placeholder="Milestone (e.g. Calc BC Final)" style="flex:1;padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:rgba(0,0,0,0.2);color:var(--ink);">
  <input id="mDate" type="date" style="padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:rgba(0,0,0,0.2);color:var(--ink);">
  <button id="addBtn" style="padding:8px 14px;border-radius:8px;border:none;background:var(--accent);color:#042015;font-weight:700;">+ Add</button>
</div>
<div id="list" style="display:flex;flex-direction:column;gap:8px;"></div>
<script>
let items = [];
function render() {
  const box = document.getElementById('list'); box.innerHTML = '';
  if (!items.length) { box.innerHTML = '<div style="color:var(--muted);">Add your first milestone above.</div>'; return; }
  const now = new Date();
  items.sort((a,b) => a.date.localeCompare(b.date)).forEach((it, i) => {
    const days = Math.ceil((new Date(it.date + 'T23:59:59') - now) / 86400000);
    const el = document.createElement('div');
    el.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-radius:10px;border:1px solid var(--border);';
    el.innerHTML = `<div><div style="font-weight:600;">${it.title}</div><div style="font-size:11px;color:var(--muted);">${it.date}</div></div><div style="display:flex;align-items:center;gap:10px;"><span style="font-weight:800;color:var(--accent);font-size:15px;">${days >= 0 ? days + 'd left' : 'Passed'}</span><button data-i="${i}" style="border:none;background:transparent;color:var(--muted);">✕</button></div>`;
    el.querySelector('button').onclick = () => { items.splice(i,1); NodeOS.storage.set({ items }, false); render(); };
    box.appendChild(el);
  });
}
NodeOS.storage.get().then(s => { items = (s && s.items) || []; render(); }).catch(render);
document.getElementById('addBtn').onclick = () => {
  const t = document.getElementById('mTitle').value.trim(), d = document.getElementById('mDate').value;
  if (!t || !d) return;
  items.push({ title: t, date: d }); document.getElementById('mTitle').value = '';
  NodeOS.storage.set({ items }, false); render();
};
</script></body></html>""",
        "widget_html": """<!doctype html><html><body style="padding:12px;">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
  <span style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;">Countdowns</span>
  <button onclick="NodeOS.openWindow('ext:milestone-countdown')" style="border:none;background:transparent;color:var(--accent);font-size:11px;font-weight:600;">Edit</button>
</div>
<div id="wList" style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted);">No milestones yet</div>
<script>
NodeOS.storage.get().then(s => {
  const items = ((s && s.items) || []).sort((a,b)=>a.date.localeCompare(b.date)).slice(0,3);
  if (!items.length) return;
  const now = new Date();
  document.getElementById('wList').innerHTML = items.map(it => {
    const d = Math.ceil((new Date(it.date + 'T23:59:59') - now) / 86400000);
    return `<div style="display:flex;justify-content:space-between;color:var(--ink);"><span>${it.title}</span><strong style="color:var(--accent);">${d}d</strong></div>`;
  }).join('');
}).catch(()=>{});
</script></body></html>""",
    },
]


DEFAULT_DESKTOP_WIDGETS: List[Dict[str, Any]] = [
    {"id": "w-grades", "kind": "builtin", "type": "grades", "widget_type": "grades", "title": "Grades", "col_span": 3, "min_height": 206},
    {"id": "w-inbox", "kind": "builtin", "type": "inbox", "widget_type": "inbox", "title": "Inbox", "col_span": 3, "min_height": 206},
    {"id": "w-tasks", "kind": "builtin", "type": "tasks", "widget_type": "tasks", "title": "Tasks", "col_span": 3, "min_height": 206},
    {"id": "w-calendar", "kind": "builtin", "type": "calendar", "widget_type": "calendar", "title": "Calendar", "col_span": 3, "min_height": 206},
    {"id": "w-briefing", "kind": "builtin", "type": "briefing", "widget_type": "briefing", "title": "Briefing", "col_span": 12, "min_height": 175},
]

BUILTIN_WIDGET_CATALOG: List[Dict[str, Any]] = [
    {"type": "grades", "widget_type": "grades", "title": "Grades & LMS Chart", "description": "Multi-LMS course averages, GPA donut & grade deltas.", "default_col_span": 3, "default_min_height": 206},
    {"type": "inbox", "widget_type": "inbox", "title": "Important Inbox", "description": "Priority-filtered unread messages across your monitored apps.", "default_col_span": 3, "default_min_height": 206},
    {"type": "tasks", "widget_type": "tasks", "title": "Priority Tasks", "description": "Top tasks synced from LMS portals, Calendar & Inbox.", "default_col_span": 3, "default_min_height": 206},
    {"type": "calendar", "widget_type": "calendar", "title": "Today's Schedule", "description": "Chronological timeline of today's upcoming events.", "default_col_span": 3, "default_min_height": 206},
    {"type": "briefing", "widget_type": "briefing", "title": "AI Executive Briefing", "description": "Workload-aware daily briefing with free-time math.", "default_col_span": 12, "default_min_height": 175},
    {"type": "ai_prompt", "widget_type": "ai_prompt", "title": "Server AI Prompt Bar", "description": "Fire off background server-side AI prompts straight from the Desktop.", "default_col_span": 6, "default_min_height": 185},
    {"type": "focus_guard", "widget_type": "focus_guard", "title": "Social Focus Guard", "description": "Daily usage meter & quick status across guarded social platforms.", "default_col_span": 4, "default_min_height": 195},
    {"type": "shared_logs", "widget_type": "shared_logs", "title": "Shared AI Logbook", "description": "Live feed of shared multi-agent workspace logs across models.", "default_col_span": 6, "default_min_height": 195},
    {"type": "apps", "widget_type": "apps", "title": "App Launcher", "description": "Quick launch tiles for your installed server-streamed web apps.", "default_col_span": 4, "default_min_height": 195},
    {"type": "goals", "widget_type": "goals", "title": "Daily Habit Goals", "description": "Streak check-ins for active long-term daily habits.", "default_col_span": 4, "default_min_height": 195},
]


def _slugify_ext_id(raw: str) -> str:
    s = re.sub(r"[^a-z0-9\-]+", "-", (raw or "custom-app").strip().lower()).strip("-")
    return s[:64] or "custom-app"


class ExtensionsManager:
    def __init__(self, root_dir: Optional[str] = None, widgets_path: Optional[str] = None) -> None:
        self.root_dir = Path(root_dir or settings.extensions_dir)
        self.widgets_path = Path(widgets_path or settings.desktop_widgets_path)

    def _inject_bridge(self, ext_id: str, raw_html: str) -> str:
        boot = f"<script>window.__NODE_EXT_ID__ = {json.dumps(ext_id)};</script>\n{NODEOS_BRIDGE_SCRIPT}"
        if "<head>" in raw_html:
            return raw_html.replace("<head>", f"<head>\n{boot}", 1)
        if "<body" in raw_html:
            return f"<!doctype html><html><head>{boot}</head>{raw_html}"
        return f"<!doctype html><html><head>{boot}</head><body>{raw_html}</body></html>"

    def list_installed(self) -> List[Dict[str, Any]]:
        if not self.root_dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for sub in sorted(self.root_dir.iterdir()):
            if not sub.is_dir():
                continue
            mf_path = sub / "manifest.json"
            if not mf_path.is_file():
                continue
            try:
                manifest = json.loads(mf_path.read_text(encoding="utf-8"))
                ext_id = manifest["id"]
                app_html = (sub / "app.html").read_text(encoding="utf-8") if (sub / "app.html").is_file() else ""
                wid_html = (sub / "widget.html").read_text(encoding="utf-8") if (sub / "widget.html").is_file() else app_html
                out.append(
                    {
                        "id": ext_id,
                        "name": manifest.get("name", ext_id),
                        "version": manifest.get("version", "1.0.0"),
                        "description": manifest.get("description", ""),
                        "permissions": manifest.get("permissions", []),
                        "provides_app": manifest.get("provides_app", True),
                        "provides_widget": manifest.get("provides_widget", True),
                        "default_col_span": manifest.get("default_col_span", 4),
                        "default_min_height": manifest.get("default_min_height", 220),
                        "manifest": manifest,
                        "app_srcdoc": self._inject_bridge(ext_id, app_html) if app_html else "",
                        "widget_srcdoc": self._inject_bridge(ext_id, wid_html) if wid_html else "",
                        "raw_html": app_html,
                        "raw_widget_html": wid_html,
                    }
                )
            except Exception:
                continue
        return out

    def list_gallery(self) -> List[Dict[str, Any]]:
        installed_ids = {e["id"] for e in self.list_installed()}
        return [
            {
                **item.get("manifest", {}),
                **item,
                "id": item["manifest"]["id"],
                "installed": item["manifest"]["id"] in installed_ids,
            }
            for item in STARTER_EXTENSION_GALLERY
        ]

    def install_extension(
        self,
        *,
        manifest: Dict[str, Any],
        html: str = "",
        widget_html: str = "",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        if not isinstance(manifest, dict) or not manifest.get("name"):
            raise ValueError("Extension manifest must include `name`.")
        ext_id = _slugify_ext_id(str(manifest.get("id") or manifest["name"]))
        perms = [str(p).strip() for p in (manifest.get("permissions") or ["storage"]) if str(p).strip() in VALID_PERMISSIONS]
        clean_manifest = {
            "id": ext_id,
            "name": str(manifest["name"]).strip()[:80],
            "version": str(manifest.get("version") or "1.0.0").strip()[:24],
            "author": str(manifest.get("author") or "User").strip()[:80],
            "description": str(manifest.get("description") or "").strip()[:400],
            "icon": str(manifest.get("icon") or "box").strip()[:40],
            "permissions": perms,
            "provides_app": bool(manifest.get("provides_app", True)),
            "provides_widget": bool(manifest.get("provides_widget", bool(widget_html or html))),
            "default_widget_size": manifest.get("default_widget_size") or {"col_span": 3, "min_height": 206},
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }

        ext_dir = self.root_dir / ext_id
        if ext_dir.exists() and not overwrite:
            raise ValueError(f"Extension '{ext_id}' is already installed.")
        ext_dir.mkdir(parents=True, exist_ok=True)
        (ext_dir / "manifest.json").write_text(json.dumps(clean_manifest, indent=2), encoding="utf-8")
        app_body = html or widget_html or "<div style='padding:16px;'>Custom Node Extension</div>"
        wid_body = widget_html or html or app_body
        (ext_dir / "app.html").write_text(app_body, encoding="utf-8")
        (ext_dir / "widget.html").write_text(wid_body, encoding="utf-8")
        if not (ext_dir / "storage.json").exists():
            (ext_dir / "storage.json").write_text("{}", encoding="utf-8")

        return next(e for e in self.list_installed() if e["id"] == ext_id)

    def install_from_gallery(self, gallery_id: str) -> Optional[Dict[str, Any]]:
        gid = _slugify_ext_id(gallery_id)
        item = next((g for g in STARTER_EXTENSION_GALLERY if g["manifest"]["id"] == gid), None)
        if not item:
            return None
        return self.install_extension(
            manifest=item["manifest"],
            html=item.get("html") or "",
            widget_html=item.get("widget_html") or "",
            overwrite=True,
        )

    def install_from_package_bytes(self, raw_bytes: bytes, filename: str = "") -> Dict[str, Any]:
        fname = (filename or "").lower()
        if fname.endswith(".zip") or raw_bytes[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                names = zf.namelist()
                mf_name = next((n for n in names if n.endswith("manifest.json")), None)
                if not mf_name:
                    raise ValueError("Extension .zip must contain `manifest.json`.")
                manifest = json.loads(zf.read(mf_name).decode("utf-8"))
                app_name = next((n for n in names if n.endswith("app.html") or n.endswith("index.html")), None)
                wid_name = next((n for n in names if n.endswith("widget.html")), None)
                html = zf.read(app_name).decode("utf-8") if app_name else ""
                wid_html = zf.read(wid_name).decode("utf-8") if wid_name else html
                return self.install_extension(manifest=manifest, html=html, widget_html=wid_html, overwrite=True)

        pkg = json.loads(raw_bytes.decode("utf-8"))
        manifest = pkg.get("manifest") if isinstance(pkg.get("manifest"), dict) else pkg
        html = str(pkg.get("html") or pkg.get("app_html") or "")
        wid_html = str(pkg.get("widget_html") or html)
        return self.install_extension(manifest=manifest, html=html, widget_html=wid_html, overwrite=True)

    def export_package(self, ext_id: str) -> Optional[Dict[str, Any]]:
        cid = _slugify_ext_id(ext_id)
        ext = next((e for e in self.list_installed() if e["id"] == cid), None)
        if not ext:
            return None
        return {
            "format": "nodeapp-v1",
            "manifest": ext["manifest"],
            "html": ext["raw_html"],
            "widget_html": ext["raw_widget_html"],
        }

    def uninstall_extension(self, ext_id: str) -> bool:
        cid = _slugify_ext_id(ext_id)
        ext_dir = self.root_dir / cid
        if not ext_dir.exists() or not ext_dir.is_dir():
            return False
        shutil.rmtree(ext_dir, ignore_errors=True)
        # Also remove any desktop widget instances of this extension
        widgets = self.get_desktop_widgets()
        filtered = [w for w in widgets if w.get("extension_id") != cid]
        if len(filtered) != len(widgets):
            self.save_desktop_widgets(filtered)
        return True

    def get_storage(self, ext_id: str) -> Dict[str, Any]:
        cid = _slugify_ext_id(ext_id)
        sp = self.root_dir / cid / "storage.json"
        if not sp.is_file():
            return {}
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def set_storage(self, ext_id: str, data: Dict[str, Any], merge: bool = True) -> Dict[str, Any]:
        cid = _slugify_ext_id(ext_id)
        ext_dir = self.root_dir / cid
        if not ext_dir.is_dir():
            raise KeyError(f"Extension '{cid}' not found")
        cur = self.get_storage(cid) if merge else {}
        if isinstance(data, dict):
            cur.update(data)
        sp = ext_dir / "storage.json"
        sp.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        return cur

    # ------------------------------------------------------------------
    # Resizable / Customizable Desktop Widget Grid Persistence
    # ------------------------------------------------------------------
    def get_desktop_widgets(self) -> List[Dict[str, Any]]:
        if not self.widgets_path.is_file():
            return json.loads(json.dumps(DEFAULT_DESKTOP_WIDGETS))
        try:
            raw = json.loads(self.widgets_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        wt = str(item.get("type") or item.get("widget_type") or "tasks")
                        item["type"] = wt
                        item["widget_type"] = wt
                return raw
        except Exception:
            pass
        return json.loads(json.dumps(DEFAULT_DESKTOP_WIDGETS))

    def save_desktop_widgets(self, widgets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for idx, w in enumerate(widgets[:24]):
            if not isinstance(w, dict):
                continue
            col_span = max(2, min(12, int(w.get("col_span") or 3)))
            min_height = max(140, min(680, int(w.get("min_height") or 206)))
            wt = str(w.get("type") or w.get("widget_type") or ("extension" if w.get("kind") == "extension" else "tasks"))[:64]
            sanitized.append(
                {
                    "id": str(w.get("id") or f"w-{idx}-{int(datetime.now().timestamp())}")[:64],
                    "kind": "extension" if w.get("kind") == "extension" else "builtin",
                    "type": wt,
                    "widget_type": wt,
                    "extension_id": str(w.get("extension_id") or "")[:64] if w.get("kind") == "extension" else None,
                    "title": str(w.get("title") or "Widget")[:80],
                    "col_span": col_span,
                    "min_height": min_height,
                }
            )
        self.widgets_path.parent.mkdir(parents=True, exist_ok=True)
        self.widgets_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
        return sanitized

    def reset_desktop_widgets(self) -> List[Dict[str, Any]]:
        default = json.loads(json.dumps(DEFAULT_DESKTOP_WIDGETS))
        return self.save_desktop_widgets(default)


extensions_manager = ExtensionsManager()
