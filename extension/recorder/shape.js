/**
 * Node API Recorder — redaction + shaping helpers.
 *
 * Plain script (no imports/exports) so the same file runs in three places:
 * the page (MAIN world hook), the background service worker, and Node tests.
 * It defines `globalThis.NodeRecorderShape`.
 *
 * Rule of thumb: keep the *structure* of a request (URL path, query keys,
 * header NAMES, JSON keys and types) and drop the *content* (header values,
 * cookies, tokens, subjects, names, ids). A few values that are protocol
 * vocabulary rather than personal data (e.g. `action=FindConversation`,
 * `"__type": "FindItemJsonRequest:#Exchange"`) are kept because a connector
 * can't be written without them.
 */
(function () {
  "use strict";

  // Header values that are protocol, not secrets.
  const SAFE_HEADER_VALUES = new Set([
    "accept",
    "action",
    "content-type",
    "prefer",
    "x-owa-actionname",
    "x-req-source",
    "x-requested-with",
    "x-ms-appname",
    "apollographql-client-name",
    "x-apollo-operation-name",
  ]);

  // Query parameters whose values are protocol vocabulary.
  const SAFE_QUERY_KEYS = new Set([
    "action", "app", "n", "ua", "api-version", "view", "type", "format", "$top", "$select",
    "$orderby", "$expand", "pagesize", "page_size", "limit", "per_page", "operationname", "version",
  ]);

  const NOISE_RE = new RegExp(
    [
      "telemetry", "analytics", "collector", "beacon", "/log(?:s|ging)?\\b", "clienttelemetry", "browser\\.events\\.data",
      "aria\\.microsoft", "events\\.data\\.microsoft", "/ev\\.owa", "/owa/ev", "ping\\.owa", "keepalive", "heartbeat",
      "sentry", "datadog", "newrelic", "segment\\.io", "google-analytics", "doubleclick", "hotjar", "fullstory",
      "\\.(?:js|mjs|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map)(?:\\?|$)", "/fonts?/", "/static/", "/cdn-cgi/",
      "/manifest\\.json", "/favicon",
    ].join("|"),
    "i",
  );

  const SUGGEST_URL_RE = new RegExp(
    [
      "service\\.svc", "find(?:conversation|item|folder)", "get(?:item|conversationitems|attachment)", "startupdata",
      "chatsvc", "/conversations", "/messages", "/threads", "/mail", "/inbox", "graphql", "/iapi", "/api/",
      "/v\\d(?:\\.\\d)?/", "/rest/", "/feed", "/notifications", "/assignments", "/grades",
    ].join("|"),
    "i",
  );

  // JSON keys that suggest a response is a list of messages / items.
  const MESSAGE_KEY_RE = /^(subject|title|body|bodypreview|preview|snippet|from|sender|message|content|topic|conversationtopic|displayname|due|dueat|duedate|score|grade)$/i;

  // Values under these keys are always personal content, however code-like
  // they look ("from:Maria", "j.smith", "McDonald").
  const CONTENT_KEY_RE = /(query|search|filter|subject|title|body|preview|snippet|text|content|message|topic|name|email|mail|address|sender|from|to|cc|bcc|recipient|user|login|upn|phone|note|comment|description|location|signature|attendee|organizer|owner|author)/i;

  function isNoise(url) {
    return NOISE_RE.test(String(url || ""));
  }

  /** "Protocol vocabulary" strings worth keeping; personal text is not. */
  function isIdentifierLike(s) {
    if (typeof s !== "string") return false;
    if (s.length === 0 || s.length > 40) return false;
    if (/@/.test(s)) return false; // email addresses
    if (/\s/.test(s)) return false; // sentences, subjects, full names
    if (/^[A-Za-z0-9+/=_-]{24,}$/.test(s)) return false; // tokens / base64 ids
    if (/^[0-9a-f-]{16,}$/i.test(s)) return false; // guids / hex ids
    if (!/^[A-Za-z][A-Za-z0-9_.:#-]*$/.test(s)) return false;
    // Exchange JSON type names: "FindItemJsonRequest:#Exchange".
    if (/^[A-Za-z]+:#[A-Za-z.]+$/.test(s)) return true;
    // Exchange item classes: "IPM.Note", "IPM.Schedule.Meeting.Request".
    if (/^[A-Z]{2,}(\.[A-Z][A-Za-z]+)+$/.test(s)) return true;
    // Otherwise ":" and "." are how people write queries and usernames
    // ("from:maria", "john.smith"), so they don't count as code.
    if (/[:.]/.test(s)) return false;
    // Single plain words could be a first name ("John"): keep only things that
    // look like code: CamelCase inside, or a separator / digit.
    return /[a-z][A-Z]/.test(s) || /[_#-]/.test(s) || (/\d/.test(s) && /[A-Za-z]/.test(s));
  }

  function describeString(s, key) {
    if (!(key && CONTENT_KEY_RE.test(key)) && isIdentifierLike(s)) return s;
    if (/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(s)) return "<email>";
    if (/^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(s)) return "<datetime>";
    if (/^https?:\/\//i.test(s)) return "<url>";
    return `<text ${s.length}>`;
  }

  /**
   * JSON value -> same structure with content replaced by type markers.
   * Arrays keep one example item and their length.
   */
  function shape(value, depth = 0, key = "") {
    if (depth > 9) return "<…>";
    if (value === null) return null;
    const t = typeof value;
    if (t === "boolean") return value;
    if (t === "number") return Number.isInteger(value) ? "<int>" : "<number>";
    if (t === "string") return describeString(value, key);
    if (Array.isArray(value)) {
      if (!value.length) return [];
      const first = value.find((v) => v !== null && v !== undefined);
      return [shape(first === undefined ? null : first, depth + 1, key), `<${value.length} items>`];
    }
    if (t === "object") {
      const out = {};
      const keys = Object.keys(value);
      for (const k of keys.slice(0, 60)) {
        // Keys that themselves look like ids/emails are collapsed.
        const safeKey = /@/.test(k) || /^[A-Za-z0-9+/=_-]{24,}$/.test(k) ? "<key>" : k;
        out[safeKey] = shape(value[k], depth + 1, k === "__type" ? "" : k);
      }
      if (keys.length > 60) out["<more keys>"] = keys.length - 60;
      return out;
    }
    return `<${t}>`;
  }

  function looksLikeMessageList(sh) {
    let hits = 0;
    const walk = (v, inArray, depth) => {
      if (depth > 9 || v === null || typeof v !== "object") return;
      if (Array.isArray(v)) {
        if (v.length) walk(v[0], true, depth + 1);
        return;
      }
      for (const [k, child] of Object.entries(v)) {
        if (inArray && MESSAGE_KEY_RE.test(k)) hits += 1;
        walk(child, inArray, depth + 1);
      }
    };
    walk(sh, false, 0);
    return hits >= 2;
  }

  /** Text body -> shape (JSON) or key list (form / urlencoded); null if empty/opaque. */
  function shapeBodyText(text) {
    if (text === null || text === undefined) return null;
    const s = String(text).trim();
    if (!s) return null;
    if ((s[0] === "{" || s[0] === "[") && s.length < 4_000_000) {
      try {
        return shape(JSON.parse(s));
      } catch (_e) {
        /* fall through */
      }
    }
    if (/^[\w.%[\]-]+=[^&]*(&[\w.%[\]-]+=[^&]*)*$/.test(s)) {
      const keys = {};
      for (const pair of s.split("&")) {
        const k = decodeURIComponentSafe(pair.split("=")[0]);
        keys[k] = "<value>";
      }
      return { "<form>": keys };
    }
    return `<non-JSON body ${s.length} chars>`;
  }

  function decodeURIComponentSafe(s) {
    try {
      return decodeURIComponent(String(s).replace(/\+/g, " "));
    } catch (_e) {
      return String(s);
    }
  }

  function redactPathSegment(seg) {
    if (!seg) return seg;
    const d = decodeURIComponentSafe(seg);
    if (/^\d+:[^/]+$/.test(d) || /@thread\./i.test(d)) return "{id}"; // Teams thread ids (contain @)
    if (/@/.test(d)) return "{email}";
    if (/^\d{5,}$/.test(d)) return "{number}";
    if (/^[0-9a-f-]{16,}$/i.test(d)) return "{id}";
    if (/^[A-Za-z0-9+/=_-]{24,}$/.test(d) && /\d/.test(d)) return "{id}";
    return seg;
  }

  /** Full URL -> redacted URL string (path ids and query values masked). */
  function redactUrl(raw) {
    let u;
    try {
      u = new URL(String(raw));
    } catch (_e) {
      return "<unparseable url>";
    }
    const path = u.pathname.split("/").map(redactPathSegment).join("/");
    const parts = [];
    for (const [k, v] of u.searchParams.entries()) {
      const safe = SAFE_QUERY_KEYS.has(k.toLowerCase()) || isIdentifierLike(v) || /^\d{1,4}$/.test(v) || v === "";
      parts.push(`${k}=${safe ? v : "…"}`);
    }
    return `${u.protocol}//${u.host}${path}${parts.length ? "?" + parts.join("&") : ""}`;
  }

  /**
   * Stable grouping key: method + host + redacted path + the "which call is
   * this" parameter (OWA ?action=, GraphQL operationName).
   */
  function groupKey(method, raw, opName) {
    let u;
    try {
      u = new URL(String(raw));
    } catch (_e) {
      return `${method} ${raw}`;
    }
    const path = u.pathname.split("/").map(redactPathSegment).join("/");
    const action = u.searchParams.get("action") || u.searchParams.get("operationName") || opName || "";
    return `${String(method || "GET").toUpperCase()} ${u.host}${path}${action ? " · " + action : ""}`;
  }

  /**
   * Header list -> {names, safe}. `names` is every header name (lowercased,
   * sorted); `safe` keeps values only for protocol headers. Cookie headers
   * contribute their cookie NAMES (never values) so we can tell cookie auth
   * from bearer auth.
   */
  function summarizeHeaders(headers) {
    const names = new Set();
    const safe = {};
    const cookieNames = new Set();
    let postData = null;
    for (const h of headers || []) {
      const name = String(h.name || h[0] || "").toLowerCase();
      const value = h.value !== undefined ? h.value : h[1];
      if (!name) continue;
      names.add(name);
      if (SAFE_HEADER_VALUES.has(name) && typeof value === "string" && value.length <= 120) {
        safe[name] = value;
      }
      if (name === "cookie" && typeof value === "string") {
        for (const part of value.split(";")) {
          const n = part.split("=")[0].trim();
          if (n) cookieNames.add(n);
        }
      }
      if (name === "x-owa-urlpostdata" && typeof value === "string") {
        // OWA sends the JSON body of some calls URL-encoded in this header.
        postData = shapeBodyText(decodeURIComponentSafe(value));
      }
    }
    return {
      names: Array.from(names).sort(),
      safe,
      cookieNames: Array.from(cookieNames).sort(),
      bodyFromHeader: postData,
    };
  }

  function isSuggested(url, respShape) {
    return SUGGEST_URL_RE.test(String(url || "")) || (respShape ? looksLikeMessageList(respShape) : false);
  }

  globalThis.NodeRecorderShape = {
    shape,
    shapeBodyText,
    redactUrl,
    groupKey,
    summarizeHeaders,
    isNoise,
    isSuggested,
    isIdentifierLike,
    looksLikeMessageList,
  };
})();
