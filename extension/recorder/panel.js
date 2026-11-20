/**
 * Node API Recorder — on-page panel (extension's isolated world).
 *
 * Shows the recorded calls grouped by type, lets the user mark the ones that
 * matter, and saves a redacted JSON file. Also relays the page hook's
 * response-shape events to the background.
 *
 * The fastest workflow: click "Watch my next action", do one thing in the
 * site (open an email), and the panel highlights just the calls that fired.
 */
(function () {
  "use strict";
  if (window.__nodeRecorderPanel) return;
  window.__nodeRecorderPanel = true;

  const MARK = "__nodeApiRecorder";
  const WATCH_SECONDS = 6;

  // Relay page-hook events (same window, our marker only).
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || ev.data[MARK] !== true) return;
    try {
      chrome.runtime.sendMessage({ type: "rec:page", evt: ev.data.evt });
    } catch (_e) {
      /* extension reloaded */
    }
  });

  const host = document.createElement("div");
  host.id = "node-api-recorder";
  host.style.cssText = "all: initial; position: fixed; z-index: 2147483647; right: 14px; bottom: 14px;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  * { box-sizing: border-box; }
  .box { width: 470px; max-height: 74vh; display: flex; flex-direction: column; background: #0a1114; color: #e6f4ef;
    border: 1px solid rgba(43,251,174,.35); border-radius: 14px; box-shadow: 0 18px 60px rgba(0,0,0,.55);
    font: 12px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; overflow: hidden; }
  .box.min .body, .box.min .steps, .box.min .bar2 { display: none; }
  header { display: flex; align-items: center; gap: 8px; padding: 9px 11px; border-bottom: 1px solid rgba(255,255,255,.07); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #fb7185; box-shadow: 0 0 0 3px rgba(251,113,133,.2); flex: none; }
  .dot.off { background: #64748b; box-shadow: none; }
  .title { font-weight: 700; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .muted { color: #7f9a92; }
  button { font: inherit; font-size: 11.5px; font-weight: 700; border: 0; border-radius: 8px; padding: 5px 9px; cursor: pointer;
    background: rgba(255,255,255,.07); color: #d3e6e0; }
  button.accent { background: #2bfbae; color: #04120d; }
  button.warn { background: rgba(251,113,133,.16); color: #fda4af; }
  button:disabled { opacity: .5; cursor: default; }
  .bar2 { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 11px; border-bottom: 1px solid rgba(255,255,255,.07); }
  .steps { padding: 8px 11px; color: #a9c2ba; border-bottom: 1px solid rgba(255,255,255,.07); font-size: 11.5px; }
  .steps ol { margin: 0; padding-left: 18px; }
  .watch { padding: 9px 11px; background: rgba(43,251,174,.08); border-bottom: 1px solid rgba(43,251,174,.25); display: none; }
  .watch.on { display: block; }
  .watch input { width: 100%; margin: 6px 0; }
  .body { overflow: auto; padding: 4px 0 8px; }
  .sec { padding: 8px 11px 3px; font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: #6f8d84; font-weight: 700;
    display: flex; justify-content: space-between; align-items: center; }
  .row { padding: 6px 11px; border-top: 1px solid rgba(255,255,255,.04); }
  .row.hit { background: rgba(43,251,174,.10); }
  .line { display: flex; align-items: center; gap: 7px; cursor: pointer; }
  .line input[type=checkbox] { flex: none; accent-color: #2bfbae; width: 15px; height: 15px; margin: 0; }
  .m { font: 700 10px ui-monospace, monospace; padding: 1px 5px; border-radius: 5px; background: rgba(255,255,255,.08); flex: none; }
  .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, monospace; font-size: 11px; }
  .chip { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: rgba(255,255,255,.07); color: #a9c2ba; flex: none; }
  .chip.good { background: rgba(43,251,174,.16); color: #2bfbae; }
  .chip.bad { background: rgba(251,113,133,.16); color: #fda4af; }
  input[type=text] { font: inherit; font-size: 11.5px; background: rgba(255,255,255,.06); color: inherit; border: 1px solid rgba(255,255,255,.12);
    border-radius: 7px; padding: 4px 7px; outline: none; }
  .label { margin: 5px 0 0 22px; width: calc(100% - 22px); }
  pre { margin: 6px 0 0 22px; padding: 7px; background: rgba(0,0,0,.35); border-radius: 7px; max-height: 220px; overflow: auto;
    font: 10.5px/1.45 ui-monospace, monospace; color: #bcd3cc; white-space: pre-wrap; word-break: break-all; }
  .empty { padding: 14px 11px; color: #7f9a92; }
  a { color: #2bfbae; cursor: pointer; text-decoration: none; }
</style>
<div class="box">
  <header>
    <span class="dot" id="dot"></span>
    <span class="title" id="title">API Recorder</span>
    <button id="min" title="Minimise">–</button>
  </header>
  <div class="steps" id="steps">
    <ol>
      <li><b>Reload the page</b> once so startup calls (like the inbox list) are caught.</li>
      <li>Click <b>Watch my next action</b>, then do one thing in the site (open the inbox, open an email, open a chat).</li>
      <li>Name what you did and keep the highlighted rows ticked. Repeat for each action.</li>
      <li><b>Save marked</b> and send me the file. It holds structure only, no messages, tokens or cookies.</li>
    </ol>
  </div>
  <div class="bar2">
    <button class="accent" id="watch">▶ Watch my next action</button>
    <button id="save">Save marked (0)</button>
    <button id="copy">Copy</button>
    <button id="clear">Clear</button>
    <button class="warn" id="stop">Stop</button>
  </div>
  <div class="watch" id="watchbox"></div>
  <div class="body" id="list"><div class="empty">Waiting for requests… use the site normally.</div></div>
</div>`;

  const $ = (id) => root.getElementById(id);
  const open = {}; // key -> details expanded
  let showOther = false;
  let showNoise = false;
  let groups = [];
  let watch = null; // {start, end, done}
  let lastSig = "";

  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => resolve(res || {}));
      } catch (_e) {
        resolve({});
      }
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function shortName(g) {
    const k = g.key.replace(/^[A-Z]+ /, "");
    return k.length > 90 ? "…" + k.slice(-88) : k;
  }

  function hitIn(g, w) {
    return !!w && g.ts.some((t) => t >= w.start && t <= w.end);
  }

  function detail(g) {
    const brief = {
      url: g.url,
      auth: g.auth,
      requestHeaderNames: g.requestHeaderNames,
      requestHeaders: g.requestHeaders,
      cookieNames: g.cookieNames,
      requestBody: g.requestBody,
      responseBody: g.responseBody,
    };
    let txt = JSON.stringify(brief, null, 1);
    if (txt.length > 6000) txt = txt.slice(0, 6000) + "\n… (full shape is in the saved file)";
    return `<pre>${esc(txt)}</pre>`;
  }

  function rowHtml(g) {
    const marked = g.mark !== null;
    const status = g.statuses.length ? g.statuses.join(",") : "…";
    const okStatus = g.statuses.some((s) => s >= 200 && s < 300);
    return `<div class="row ${hitIn(g, watch && watch.done ? watch : null) ? "hit" : ""}" data-key="${esc(g.key)}">
      <div class="line" data-act="toggle">
        <input type="checkbox" data-act="mark" ${marked ? "checked" : ""} title="Include in the saved file" />
        <span class="m">${esc(g.method || "")}</span>
        <span class="name" title="${esc(g.url)}">${esc(shortName(g))}</span>
        ${g.messageList ? '<span class="chip good">list of items</span>' : ""}
        <span class="chip">×${g.count}</span>
        <span class="chip ${okStatus ? "" : "bad"}">${esc(status)}</span>
      </div>
      ${marked ? `<input class="label" type="text" data-act="label" placeholder="What is this? e.g. inbox list, open email" value="${esc(g.mark || "")}" />` : ""}
      ${open[g.key] ? detail(g) : ""}
    </div>`;
  }

  function render(st) {
    $("dot").className = "dot" + (st.active ? "" : " off");
    $("title").textContent = st.active ? `Recording ${String(st.origin || "").replace(/^https?:\/\//, "")}` : "Recording stopped (you can still save)";
    $("stop").disabled = !st.active;
    $("watch").disabled = !st.active || (watch && !watch.done);
    const markedCount = groups.filter((g) => g.mark !== null).length;
    $("save").textContent = `Save marked (${markedCount})`;
    $("save").disabled = !markedCount;

    const hitsFirst = (arr) =>
      watch && watch.done ? arr.slice().sort((a, b) => Number(hitIn(b, watch)) - Number(hitIn(a, watch))) : arr;
    const suggested = hitsFirst(groups.filter((g) => g.suggested));
    const other = hitsFirst(groups.filter((g) => !g.suggested && !g.noise));
    const noise = groups.filter((g) => g.noise);
    if (!groups.length) {
      $("list").innerHTML = '<div class="empty">Waiting for requests… use the site normally.</div>';
      return;
    }
    let html = `<div class="sec"><span>Likely useful (${suggested.length})</span></div>`;
    html += suggested.length ? suggested.map(rowHtml).join("") : '<div class="empty">None yet.</div>';
    html += `<div class="sec"><span>Other calls (${other.length})</span><a data-act="other">${showOther ? "hide" : "show"}</a></div>`;
    if (showOther) html += other.map(rowHtml).join("");
    html += `<div class="sec"><span>Hidden as noise (${noise.length})</span><a data-act="noise">${showNoise ? "hide" : "show"}</a></div>`;
    if (showNoise) html += noise.map(rowHtml).join("");
    $("list").innerHTML = html;
  }

  async function refresh(force) {
    const active = root.activeElement;
    if (!force && active && active.matches && active.matches("input[type=text]")) return; // don't steal focus mid-typing
    const res = await send({ type: "rec:groups" });
    if (!res || !res.ok) return;
    groups = res.groups || [];
    const sig = JSON.stringify([res.active, groups.map((g) => [g.key, g.count, g.mark, g.statuses.length, !!g.responseBody]), open, showOther, showNoise, watch]);
    if (!force && sig === lastSig) return;
    lastSig = sig;
    render(res);
  }

  function startWatch() {
    const now = Date.now();
    watch = { start: now - 300, end: now + WATCH_SECONDS * 1000, done: false };
    const box = $("watchbox");
    box.className = "watch on";
    let left = WATCH_SECONDS;
    box.innerHTML = `<b>Do it now:</b> open an email, a folder or a chat… <span id="left">${left}s</span>`;
    const tick = setInterval(() => {
      left -= 1;
      const el = root.getElementById("left");
      if (el) el.textContent = `${left}s`;
      if (left <= 0) {
        clearInterval(tick);
        finishWatch();
      }
    }, 1000);
    refresh(true);
  }

  async function finishWatch() {
    watch.done = true;
    await refresh(true);
    const hits = groups.filter((g) => hitIn(g, watch) && !g.noise);
    const box = $("watchbox");
    if (!hits.length) {
      box.innerHTML = 'Nothing new fired. Some sites load everything up front: try a different action, or reload the page. <a data-act="closewatch">close</a>';
      return;
    }
    const pre = hits.filter((g) => g.suggested || g.messageList);
    box.innerHTML = `<b>${hits.length} call type(s)</b> fired during that, highlighted below (${pre.length} pre-ticked).
      <input type="text" id="watchlabel" placeholder="What did you just do? e.g. open an email" />
      <button class="accent" data-act="applywatch">Mark them</button> <a data-act="closewatch">skip</a>`;
    const input = root.getElementById("watchlabel");
    if (input) input.focus();
  }

  async function applyWatch() {
    const input = root.getElementById("watchlabel");
    const label = (input && input.value.trim()) || "action";
    const hits = groups.filter((g) => hitIn(g, watch) && !g.noise);
    const pick = hits.filter((g) => g.suggested || g.messageList);
    for (const g of pick.length ? pick : hits) {
      await send({ type: "rec:mark", key: g.key, mark: g.mark ? `${g.mark}; ${label}` : label });
    }
    $("watchbox").className = "watch";
    await refresh(true);
  }

  async function exportData() {
    const res = await send({ type: "rec:export" });
    return res && res.ok ? res.data : null;
  }

  root.addEventListener("click", async (ev) => {
    const t = ev.target;
    const act = t && t.getAttribute && t.getAttribute("data-act");
    const row = t && t.closest ? t.closest(".row") : null;
    const key = row ? row.getAttribute("data-key") : null;
    if (t.id === "min") return $("min").closest(".box").classList.toggle("min");
    if (t.id === "watch") return startWatch();
    if (t.id === "stop") {
      await send({ type: "rec:stop" });
      return refresh(true);
    }
    if (t.id === "clear") {
      if (!confirm("Clear everything recorded so far?")) return;
      await send({ type: "rec:clear" });
      watch = null;
      return refresh(true);
    }
    if (t.id === "save" || t.id === "copy") {
      const data = await exportData();
      if (!data || !data.calls.length) return;
      const text = JSON.stringify(data, null, 2);
      if (t.id === "copy") {
        try {
          await navigator.clipboard.writeText(text);
          t.textContent = "Copied ✓";
        } catch (_e) {
          t.textContent = "Copy blocked: use Save";
        }
        setTimeout(() => (t.textContent = "Copy"), 1800);
        return;
      }
      const host = String(data.site || "site").replace(/^https?:\/\//, "").replace(/[^a-z0-9.-]+/gi, "_");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      a.download = `node-api-capture-${host}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.json`;
      document.documentElement.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
      return;
    }
    if (act === "other") { showOther = !showOther; return refresh(true); }
    if (act === "noise") { showNoise = !showNoise; return refresh(true); }
    if (act === "applywatch") return applyWatch();
    if (act === "closewatch") { $("watchbox").className = "watch"; return; }
    if (act === "mark" && key) {
      const g = groups.find((x) => x.key === key);
      await send({ type: "rec:mark", key, mark: g && g.mark !== null ? null : "" });
      return refresh(true);
    }
    if (act === "label") return;
    if (key && t.closest(".line")) {
      open[key] = !open[key];
      return refresh(true);
    }
  });

  root.addEventListener("change", async (ev) => {
    const t = ev.target;
    if (t && t.getAttribute("data-act") === "label") {
      const key = t.closest(".row").getAttribute("data-key");
      await send({ type: "rec:mark", key, mark: t.value });
    }
  });
  root.addEventListener("keydown", (ev) => {
    const t = ev.target;
    if (ev.key === "Enter" && t && t.id === "watchlabel") applyWatch();
    if (ev.key === "Enter" && t && t.getAttribute("data-act") === "label") t.blur();
    ev.stopPropagation(); // keep the site's keyboard shortcuts out of our inputs
  });

  function mount() {
    (document.body || document.documentElement).appendChild(host);
    refresh(true);
    setInterval(() => refresh(false), 1500);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount, { once: true });
})();
