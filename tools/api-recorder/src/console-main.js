/* Node API Recorder — console edition (runner). Built into console-recorder.js
 * together with extension/recorder/shape.js; edit here, then run build.mjs. */
(function () {
  "use strict";
  if (window.nodeRec && window.nodeRec.__alive) {
    window.nodeRec.show();
    console.log("[node-rec] already running: panel shown again.");
    return;
  }
  const S = globalThis.NodeRecorderShape;
  const MAX_BODY = 3_000_000;
  const MAX_ENTRIES = 4000;
  const WATCH_SECONDS = 6;
  const entries = [];
  const marks = {}; // groupKey -> label ("" = marked without a label)
  const startedAt = Date.now();
  let watch = null;
  let showOther = false;
  let showNoise = false;
  const open = {};

  // ------------------------------------------------------------ capture
  function absUrl(u) {
    try { return new URL(String(u), location.href).href; } catch (_e) { return String(u); }
  }
  function headerPairs(h) {
    const out = [];
    try {
      if (!h) return out;
      if (typeof h.forEach === "function" && !Array.isArray(h)) h.forEach((v, k) => out.push({ name: k, value: v }));
      else if (Array.isArray(h)) h.forEach((p) => out.push({ name: p[0], value: p[1] }));
      else Object.keys(h).forEach((k) => out.push({ name: k, value: h[k] }));
    } catch (_e) {}
    return out;
  }
  function opName(body) {
    if (typeof body !== "string" || body.length > 200000) return "";
    const m = body.match(/"operationName"\s*:\s*"([A-Za-z0-9_]{1,60})"/);
    return m ? m[1] : "";
  }
  function textish(ctype) {
    const c = String(ctype || "").toLowerCase();
    return !c || c.includes("json") || c.includes("text/plain") || c.includes("javascript");
  }
  function add(e) {
    e.noise = S.isNoise(e.url);
    entries.push(e);
    if (entries.length > MAX_ENTRIES) entries.shift();
    scheduleRender();
  }
  function bodyShape(txt) {
    if (txt == null) return null;
    return txt.length <= MAX_BODY ? S.shapeBodyText(txt) : `<large ${txt.length}>`;
  }

  const origFetch = window.fetch;
  const XHR = window.XMLHttpRequest;
  const origOpen = XHR.prototype.open;
  const origSend = XHR.prototype.send;
  const origSetHeader = XHR.prototype.setRequestHeader;
  const OrigWS = window.WebSocket;

  window.fetch = function (input, init) {
    let method = "GET", url = "", pairs = [], reqBody = null;
    try {
      const isReq = typeof Request !== "undefined" && input instanceof Request;
      url = absUrl(isReq ? input.url : input);
      method = String((init && init.method) || (isReq && input.method) || "GET").toUpperCase();
      pairs = headerPairs((init && init.headers) || (isReq ? input.headers : null));
      if (init && typeof init.body === "string") reqBody = init.body;
    } catch (_e) {}
    const t = Date.now();
    const p = origFetch.apply(this, arguments);
    try {
      p.then((resp) => {
        try {
          const ctype = resp.headers.get("content-type") || "";
          const base = { t, via: "fetch", method, url, status: resp.status, headers: S.summarizeHeaders(pairs), reqText: reqBody, op: opName(reqBody) };
          if (!textish(ctype) || S.isNoise(url)) return add({ ...base, respShape: null });
          resp.clone().text().then((txt) => add({ ...base, respShape: bodyShape(txt) }), () => add({ ...base, respShape: null }));
        } catch (_e) {}
      }, (err) => add({ t, via: "fetch", method, url, status: "error", headers: S.summarizeHeaders(pairs), reqText: reqBody, op: opName(reqBody), error: String(err).slice(0, 80) }));
    } catch (_e) {}
    return p;
  };
  XHR.prototype.open = function (method, url) {
    try { this.__nodeRec = { method: String(method || "GET").toUpperCase(), url: absUrl(url), pairs: [] }; } catch (_e) {}
    return origOpen.apply(this, arguments);
  };
  XHR.prototype.setRequestHeader = function (name, value) {
    try { if (this.__nodeRec) this.__nodeRec.pairs.push({ name, value }); } catch (_e) {}
    return origSetHeader.apply(this, arguments);
  };
  XHR.prototype.send = function (body) {
    try {
      const rec = this.__nodeRec;
      if (rec) {
        rec.t = Date.now();
        const reqBody = typeof body === "string" ? body : null;
        this.addEventListener("loadend", () => {
          try {
            let respShape = null;
            const ctype = this.getResponseHeader("content-type") || "";
            if (!S.isNoise(rec.url) && textish(ctype)) {
              if (this.responseType === "" || this.responseType === "text") respShape = bodyShape(this.responseText || "");
              else if (this.responseType === "json" && this.response) respShape = S.shape(this.response);
            }
            add({ t: rec.t, via: "xhr", method: rec.method, url: rec.url, status: this.status, headers: S.summarizeHeaders(rec.pairs), reqText: reqBody, op: opName(reqBody), respShape });
          } catch (_e) {}
        });
      }
    } catch (_e) {}
    return origSend.apply(this, arguments);
  };
  // WebSockets (Teams pushes new messages this way): log the URL and the
  // shape of the first few JSON frames each way.
  if (OrigWS) {
    const Wrapped = function (url, protocols) {
      const ws = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      try {
        const e = { t: Date.now(), via: "websocket", method: "WS", url: absUrl(url), status: 101, headers: S.summarizeHeaders([]), reqText: null, op: "", respShape: null, frames: { in: 0, out: 0 } };
        add(e);
        const origWsSend = ws.send;
        ws.send = function (data) {
          try { e.frames.out += 1; if (!e.reqShape && typeof data === "string") e.reqShape = S.shapeBodyText(data); } catch (_e) {}
          return origWsSend.apply(this, arguments);
        };
        ws.addEventListener("message", (m) => {
          try {
            e.frames.in += 1;
            e.t2 = Date.now();
            if (!e.respShape && typeof m.data === "string") e.respShape = S.shapeBodyText(m.data.replace(/^\d+:+/, ""));
            scheduleRender();
          } catch (_e) {}
        });
      } catch (_e) {}
      return ws;
    };
    Wrapped.prototype = OrigWS.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach((k) => (Wrapped[k] = OrigWS[k]));
    window.WebSocket = Wrapped;
  }

  function stop() {
    window.fetch = origFetch;
    XHR.prototype.open = origOpen;
    XHR.prototype.send = origSend;
    XHR.prototype.setRequestHeader = origSetHeader;
    if (OrigWS) window.WebSocket = OrigWS;
    stopped = true;
    render();
    console.log("[node-rec] stopped. Your data is still here: nodeRec.save() or nodeRec.export().");
  }
  let stopped = false;

  // ------------------------------------------------------------ grouping
  function groups() {
    const map = new Map();
    for (const e of entries) {
      const key = S.groupKey(e.method, e.url, e.op);
      let g = map.get(key);
      if (!g) {
        g = { key, method: e.method, url: S.redactUrl(e.url), via: e.via, count: 0, ts: [], statuses: [], names: new Set(), safe: {}, reqShape: null, respShape: null, noise: true, frames: null };
        map.set(key, g);
      }
      g.count += 1;
      g.ts.push(e.t);
      if (e.t2) g.ts.push(e.t2);
      if (!g.statuses.includes(e.status)) g.statuses.push(e.status);
      (e.headers.names || []).forEach((n) => g.names.add(n));
      Object.assign(g.safe, e.headers.safe || {});
      if (!g.reqShape) g.reqShape = e.reqShape || (e.reqText ? S.shapeBodyText(e.reqText) : null) || e.headers.bodyFromHeader || null;
      if (!g.respShape && e.respShape) g.respShape = e.respShape;
      if (e.frames) g.frames = e.frames;
      g.noise = g.noise && !!e.noise;
    }
    const out = [];
    for (const g of map.values()) {
      const names = Array.from(g.names).sort();
      out.push({
        key: g.key, method: g.method, url: g.url, via: g.via, count: g.count, ts: g.ts, lastT: Math.max(...g.ts),
        statuses: g.statuses.slice(0, 6), requestHeaderNames: names, requestHeaders: g.safe,
        auth: authKind(names), requestBody: g.reqShape, responseBody: g.respShape, frames: g.frames,
        noise: g.noise, suggested: !g.noise && S.isSuggested(g.url, g.respShape),
        messageList: !!(g.respShape && S.looksLikeMessageList(g.respShape)),
        mark: Object.prototype.hasOwnProperty.call(marks, g.key) ? marks[g.key] : null,
      });
    }
    out.sort((a, b) => Number(b.suggested) - Number(a.suggested) || b.lastT - a.lastT);
    return out;
  }
  function authKind(names) {
    const has = (n) => names.includes(n);
    const bits = [];
    if (has("authorization")) bits.push("authorization header");
    if (has("authentication") || has("x-skypetoken")) bits.push("skype token header");
    if (has("x-owa-canary")) bits.push("OWA canary");
    if (has("x-csrf-token") || has("x-xsrf-token") || has("x-csrftoken")) bits.push("CSRF token");
    bits.push("cookies (if any are sent; the console can't see them)");
    return bits.join(" + ");
  }
  function cookieNames() {
    // Only the non-HttpOnly ones are visible to page JS; names only.
    return document.cookie.split(";").map((c) => c.split("=")[0].trim()).filter(Boolean).sort();
  }
  function exportData(all) {
    const gs = groups().filter((g) => all || g.mark !== null);
    return {
      tool: "node-api-recorder",
      version: "console-1",
      recordedAt: new Date().toISOString(),
      site: location.origin,
      privacy: "Structure only: header names (values only for protocol headers like action/content-type), cookie NAMES, redacted URLs and JSON shapes with personal text replaced by <text N>. No tokens, cookies or message content.",
      visibleCookieNames: cookieNames(),
      calls: gs.map((g) => ({
        label: g.mark || "", method: g.method, url: g.url, via: g.via, count: g.count, statuses: g.statuses, auth: g.auth,
        requestHeaderNames: g.requestHeaderNames, requestHeaders: g.requestHeaders, requestBody: g.requestBody,
        responseBody: g.responseBody, ...(g.frames ? { websocketFrames: g.frames } : {}),
      })),
    };
  }
  function save(all) {
    const data = exportData(all);
    if (!data.calls.length) return console.warn("[node-rec] nothing marked yet. Tick some rows, or nodeRec.save(true) for everything.");
    const text = JSON.stringify(data, null, 2);
    const host = location.hostname.replace(/[^a-z0-9.-]+/gi, "_");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    a.download = `node-api-capture-${host}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.json`;
    document.documentElement.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    return `${data.calls.length} call(s) saved`;
  }

  // ------------------------------------------------------------ panel
  // Built with createElement only: Outlook/Teams can enforce Trusted Types,
  // which blocks innerHTML from pasted code.
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "style") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
    return el;
  }
  const CSS = `
    :host { all: initial; }
    .box { position: fixed; right: 12px; bottom: 12px; z-index: 2147483647; width: 480px; max-height: 70vh; display: flex; flex-direction: column;
      background: #111; color: #eee; border: 1px solid #444; border-radius: 8px; font: 12px/1.4 system-ui, sans-serif; box-shadow: 0 8px 30px #0008; }
    .box.min .body, .box.min .bar, .box.min .msg { display: none; }
    .top, .bar { display: flex; gap: 6px; align-items: center; padding: 6px 8px; border-bottom: 1px solid #333; flex-wrap: wrap; }
    .top b { flex: 1; }
    button { font: inherit; background: #2a2a2a; color: #eee; border: 1px solid #555; border-radius: 5px; padding: 3px 8px; cursor: pointer; }
    button.go { background: #1d6b4c; border-color: #2bfbae; }
    button:disabled { opacity: .5; }
    .msg { padding: 6px 8px; background: #1b2b24; border-bottom: 1px solid #2bfbae55; }
    .msg input { width: 60%; }
    .body { overflow: auto; }
    .sec { padding: 6px 8px 2px; color: #999; font-size: 11px; text-transform: uppercase; display: flex; justify-content: space-between; }
    .sec a { color: #2bfbae; cursor: pointer; text-transform: none; }
    .row { padding: 4px 8px; border-top: 1px solid #222; }
    .row.hit { background: #17392b; }
    .line { display: flex; gap: 6px; align-items: center; cursor: pointer; }
    .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, monospace; font-size: 11px; }
    .tag { font-size: 10px; padding: 0 5px; border-radius: 8px; background: #333; white-space: nowrap; }
    .tag.good { background: #1d6b4c; }
    input[type=text] { font: inherit; background: #222; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 2px 5px; }
    .label { margin: 3px 0 0 22px; width: calc(100% - 26px); }
    pre { margin: 4px 0 0 22px; padding: 6px; background: #000; max-height: 200px; overflow: auto; font: 10.5px ui-monospace, monospace; white-space: pre-wrap; word-break: break-all; }
    .empty { padding: 10px 8px; color: #888; }`;
  const host = h("div", { id: "node-api-recorder-console" });
  const root = host.attachShadow({ mode: "open" });
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    root.adoptedStyleSheets = [sheet];
  } catch (_e) {
    root.append(h("style", { text: CSS }));
  }
  const title = h("b", { text: "API Recorder (console)" });
  const btnWatch = h("button", { class: "go", onclick: startWatch, text: "▶ Watch my next action" });
  const btnSave = h("button", { onclick: () => save(false), text: "Save marked (0)" });
  const btnCopy = h("button", { onclick: copy, text: "Copy" });
  const btnStop = h("button", { onclick: stop, text: "Stop" });
  const msg = h("div", { class: "msg", style: { display: "none" } });
  const body = h("div", { class: "body" });
  const box = h("div", { class: "box" },
    h("div", { class: "top" }, title, h("button", { onclick: () => box.classList.toggle("min"), text: "–" }), h("button", { onclick: () => host.remove(), title: "Hide (nodeRec.show() brings it back)", text: "×" })),
    h("div", { class: "bar" }, btnWatch, btnSave, btnCopy,
      h("button", { onclick: () => { entries.length = 0; for (const k in marks) delete marks[k]; watch = null; render(); }, text: "Clear" }), btnStop),
    msg, body);
  root.append(box);
  root.addEventListener("keydown", (e) => e.stopPropagation()); // keep the site's shortcuts out of our inputs

  function setMsg(...kids) {
    msg.replaceChildren(...kids);
    msg.style.display = kids.length ? "block" : "none";
  }
  async function copy() {
    const data = exportData(false);
    if (!data.calls.length) return;
    try { await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); btnCopy.textContent = "Copied ✓"; }
    catch (_e) { btnCopy.textContent = "Blocked, use Save"; }
    setTimeout(() => (btnCopy.textContent = "Copy"), 1800);
  }
  const hitIn = (g) => !!(watch && watch.done) && g.ts.some((t) => t >= watch.start && t <= watch.end);

  function startWatch() {
    const now = Date.now();
    watch = { start: now - 300, end: now + WATCH_SECONDS * 1000, done: false };
    let left = WATCH_SECONDS;
    const counter = h("span", { text: `${left}s` });
    setMsg(h("b", { text: "Do it now: " }), "open an email, a folder or a chat… ", counter);
    btnWatch.disabled = true;
    const tick = setInterval(() => {
      left -= 1;
      counter.textContent = `${left}s`;
      if (left > 0) return;
      clearInterval(tick);
      // Give responses that started in the window a moment to finish.
      setTimeout(finishWatch, 1200);
    }, 1000);
  }
  function finishWatch() {
    watch.done = true;
    btnWatch.disabled = stopped;
    const hits = groups().filter((g) => hitIn(g) && !g.noise);
    render();
    if (!hits.length) return setMsg("Nothing fired. Try another action (switch folder and back to re-load the list). ", h("button", { onclick: () => setMsg(), text: "ok" }));
    const input = h("input", { type: "text", placeholder: "What did you just do? e.g. open an email" });
    const apply = () => {
      const label = input.value.trim() || "action";
      const pick = hits.filter((g) => g.suggested || g.messageList);
      for (const g of pick.length ? pick : hits) marks[g.key] = g.mark ? `${g.mark}; ${label}` : label;
      setMsg();
      render();
    };
    input.addEventListener("keydown", (e) => e.key === "Enter" && apply());
    setMsg(h("b", { text: `${hits.length} call type(s) fired` }), " (highlighted). ", input, " ", h("button", { class: "go", onclick: apply, text: "Mark them" }), " ", h("button", { onclick: () => setMsg(), text: "skip" }));
    input.focus();
  }

  function row(g) {
    const marked = g.mark !== null;
    const cb = h("input", { type: "checkbox", checked: marked, title: "Include in the saved file", onclick: (e) => {
      e.stopPropagation();
      if (marked) delete marks[g.key]; else marks[g.key] = "";
      render();
    } });
    const ok = g.statuses.some((s) => (s >= 200 && s < 300) || s === 101);
    const line = h("div", { class: "line", onclick: () => { open[g.key] = !open[g.key]; render(); } },
      cb, h("span", { class: "tag", text: g.method }), h("span", { class: "name", title: g.url, text: g.key.replace(/^[A-Z]+ /, "") }),
      g.messageList && h("span", { class: "tag good", text: "list of items" }),
      h("span", { class: "tag", text: `×${g.count}` }), h("span", { class: "tag", style: ok ? {} : { background: "#6b1d2a" }, text: g.statuses.join(",") }));
    const el = h("div", { class: "row" + (hitIn(g) ? " hit" : "") }, line);
    if (marked) {
      const lbl = h("input", { type: "text", class: "label", placeholder: "What is this? e.g. inbox list", value: g.mark || "" });
      lbl.addEventListener("change", () => { marks[g.key] = lbl.value; render(); });
      el.append(lbl);
    }
    if (open[g.key]) {
      let txt = JSON.stringify({ url: g.url, auth: g.auth, requestHeaderNames: g.requestHeaderNames, requestHeaders: g.requestHeaders, requestBody: g.requestBody, responseBody: g.responseBody }, null, 1);
      if (txt.length > 6000) txt = txt.slice(0, 6000) + "\n… (full shape is in the saved file)";
      el.append(h("pre", { text: txt }));
    }
    return el;
  }
  function render() {
    renderPending = false;
    if (root.activeElement && root.activeElement.matches("input[type=text]")) return void scheduleRender(1500);
    const gs = groups();
    const n = gs.filter((g) => g.mark !== null).length;
    title.textContent = stopped ? "API Recorder: stopped (you can still save)" : `API Recorder: recording ${location.host}`;
    btnSave.textContent = `Save marked (${n})`;
    btnSave.disabled = btnCopy.disabled = !n;
    btnStop.disabled = stopped;
    btnWatch.disabled = stopped || !!(watch && !watch.done);
    const hitsFirst = (arr) => (watch && watch.done ? arr.slice().sort((a, b) => Number(hitIn(b)) - Number(hitIn(a))) : arr);
    const sug = hitsFirst(gs.filter((g) => g.suggested));
    const other = hitsFirst(gs.filter((g) => !g.suggested && !g.noise));
    const noise = gs.filter((g) => g.noise);
    if (!gs.length) return void body.replaceChildren(h("div", { class: "empty", text: "Waiting for requests… use the site (open a folder, an email, a chat)." }));
    const toggle = (label, on, flip) => h("a", { onclick: () => { flip(); render(); }, text: on ? "hide" : "show" });
    body.replaceChildren(
      h("div", { class: "sec" }, h("span", { text: `Likely useful (${sug.length})` })),
      ...(sug.length ? sug.map(row) : [h("div", { class: "empty", text: "None yet." })]),
      h("div", { class: "sec" }, h("span", { text: `Other calls (${other.length})` }), toggle("other", showOther, () => (showOther = !showOther))),
      ...(showOther ? other.map(row) : []),
      h("div", { class: "sec" }, h("span", { text: `Hidden as noise (${noise.length})` }), toggle("noise", showNoise, () => (showNoise = !showNoise))),
      ...(showNoise ? noise.map(row) : []),
    );
  }
  let renderPending = false;
  function scheduleRender(ms) {
    if (renderPending) return;
    renderPending = true;
    setTimeout(render, ms || 400);
  }

  function show() {
    if (!host.isConnected) (document.body || document.documentElement).append(host);
    box.classList.remove("min");
    render();
  }
  show();

  window.nodeRec = {
    __alive: true,
    show,
    stop,
    groups,
    /** The export object (marked calls; pass true for every call). */
    export: (all) => exportData(!!all),
    /** Download the export as a file (marked calls; pass true for every call). */
    save: (all) => save(!!all),
    mark: (key, label) => { marks[key] = label || ""; render(); },
    startedAt,
  };
  console.log(
    "%c[node-rec] recording " + location.host,
    "color:#2bfbae;font-weight:bold",
    "\n• Use the site. Calls made BEFORE you pasted this aren't caught, so switch folder/chat and back to re-trigger lists.",
    "\n• Panel bottom-right: ▶ Watch my next action → do one thing → name it. Then Save marked.",
    "\n• Console: nodeRec.save(), nodeRec.export(), nodeRec.stop(), nodeRec.show()",
  );
})();
