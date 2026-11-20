/**
 * Node API Recorder — page hook (runs in the page's own JS world).
 *
 * Wraps fetch() and XMLHttpRequest so the recorder can see *response bodies*,
 * which the browser's network API doesn't expose to extensions. Bodies are
 * reduced to their shape (keys and types, see shape.js) right here in the
 * page; raw content never leaves it. Header VALUES are never read.
 *
 * Only active while recording (the extension injects it on demand). Every
 * wrapper is fail-safe: if anything here throws, the page's request still
 * proceeds untouched.
 */
(function () {
  "use strict";
  if (window.__nodeRecorderHooked) return;
  window.__nodeRecorderHooked = true;
  const S = globalThis.NodeRecorderShape;
  if (!S) return;

  const MAX_BODY = 3_000_000;
  const MARK = "__nodeApiRecorder";

  function post(evt) {
    try {
      window.postMessage({ [MARK]: true, evt }, window.location.origin);
    } catch (_e) {
      /* never break the page */
    }
  }

  function absUrl(u) {
    try {
      return new URL(String(u), window.location.href).href;
    } catch (_e) {
      return String(u);
    }
  }

  function headerNames(h) {
    const out = [];
    try {
      if (!h) return out;
      if (typeof h.forEach === "function" && !Array.isArray(h)) h.forEach((_v, k) => out.push(String(k).toLowerCase()));
      else if (Array.isArray(h)) h.forEach((p) => out.push(String(p[0]).toLowerCase()));
      else Object.keys(h).forEach((k) => out.push(k.toLowerCase()));
    } catch (_e) {}
    return out;
  }

  function opNameFromBody(body) {
    if (typeof body !== "string" || body.length > 200000) return "";
    const m = body.match(/"operationName"\s*:\s*"([A-Za-z0-9_]{1,60})"/);
    return m ? m[1] : "";
  }

  function interesting(ctype) {
    const c = String(ctype || "").toLowerCase();
    return !c || c.includes("json") || c.includes("text/plain") || c.includes("javascript");
  }

  // ---------------------------------------------------------------- fetch
  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      let method = "GET";
      let url = "";
      let names = [];
      let reqBody = null;
      try {
        const isReq = typeof Request !== "undefined" && input instanceof Request;
        url = absUrl(isReq ? input.url : input);
        method = String((init && init.method) || (isReq && input.method) || "GET").toUpperCase();
        names = headerNames((init && init.headers) || (isReq ? input.headers : null));
        if (init && typeof init.body === "string") reqBody = init.body;
      } catch (_e) {}
      const started = Date.now();
      const p = origFetch.apply(this, arguments);
      try {
        if (!S.isNoise(url)) {
          p.then((resp) => {
            try {
              const ctype = resp.headers.get("content-type") || "";
              const len = Number(resp.headers.get("content-length") || 0);
              const base = {
                via: "fetch", method, url, t: started, status: resp.status, jsHeaderNames: names,
                reqShape: reqBody ? S.shapeBodyText(reqBody) : null, opName: opNameFromBody(reqBody),
              };
              if (!interesting(ctype) || len > MAX_BODY) return post({ ...base, respShape: null, ctype });
              resp.clone().text().then(
                (txt) => post({ ...base, ctype, respShape: txt.length <= MAX_BODY ? S.shapeBodyText(txt) : `<large ${txt.length}>` }),
                () => post({ ...base, ctype, respShape: null }),
              );
            } catch (_e) {}
          }, () => {});
        }
      } catch (_e) {}
      return p;
    };
  }

  // ------------------------------------------------------------------ XHR
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const origOpen = XHR.prototype.open;
    const origSend = XHR.prototype.send;
    const origSetHeader = XHR.prototype.setRequestHeader;
    XHR.prototype.open = function (method, url) {
      try {
        this.__nodeRec = { method: String(method || "GET").toUpperCase(), url: absUrl(url), names: [] };
      } catch (_e) {}
      return origOpen.apply(this, arguments);
    };
    XHR.prototype.setRequestHeader = function (name) {
      try {
        if (this.__nodeRec) this.__nodeRec.names.push(String(name).toLowerCase());
      } catch (_e) {}
      return origSetHeader.apply(this, arguments);
    };
    XHR.prototype.send = function (body) {
      try {
        const rec = this.__nodeRec;
        if (rec && !S.isNoise(rec.url)) {
          rec.t = Date.now();
          const reqBody = typeof body === "string" ? body : null;
          this.addEventListener("loadend", () => {
            try {
              const ctype = this.getResponseHeader("content-type") || "";
              let respShape = null;
              if (interesting(ctype) && (this.responseType === "" || this.responseType === "text")) {
                const txt = this.responseText || "";
                respShape = txt.length <= MAX_BODY ? S.shapeBodyText(txt) : `<large ${txt.length}>`;
              } else if (this.responseType === "json" && this.response) {
                respShape = S.shape(this.response);
              }
              post({
                via: "xhr", method: rec.method, url: rec.url, t: rec.t, status: this.status, jsHeaderNames: rec.names,
                reqShape: reqBody ? S.shapeBodyText(reqBody) : null, opName: opNameFromBody(reqBody), ctype, respShape,
              });
            } catch (_e) {}
          });
        }
      } catch (_e) {}
      return origSend.apply(this, arguments);
    };
  }
})();
