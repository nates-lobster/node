import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from .apps_manager import AppsConfigManager, slugify_app_name
from .auth import SESSION_COOKIE_NAME, auth_service
from .config import settings
from .database import engine
from .grades_service import grades_service
from .importance_filter import importance_service, strip_html_tags
from .model_router import model_router, scrub_notification_artifacts
from .models import Notification, ScraperHeartbeat
from .ollama_client import ollama_client
from .seed import reconcile_app_unread_counts

logger = logging.getLogger("node.browser_stream")

INIT_INTERCEPTOR_JS = """
(() => {
  if (window.__nodeInterceptorInstalled) return;
  window.__nodeInterceptorInstalled = true;

  const emit = (payload) => {
    try {
      if (typeof window.__nodeReportNotification === 'function') {
        window.__nodeReportNotification(JSON.stringify(payload));
      }
    } catch (_) {}
  };

  class NodeNotification {
    static get permission() { return 'granted'; }
    static requestPermission(cb) {
      if (typeof cb === 'function') cb('granted');
      return Promise.resolve('granted');
    }
    constructor(title, options = {}) {
      this.title = String(title || '');
      this.body = String((options && options.body) || '');
      this.tag = String((options && options.tag) || '');
      this.onclick = null;
      this.onclose = null;
      emit({
        kind: 'web_notification',
        title: this.title.slice(0, 240),
        body: this.body.slice(0, 600),
        tag: this.tag.slice(0, 120),
        href: location.href
      });
    }
    close() {
      if (typeof this.onclose === 'function') {
        try { this.onclose(); } catch (_) {}
      }
    }
    addEventListener() {}
    removeEventListener() {}
  }

  try {
    window.Notification = NodeNotification;
  } catch (_) {}

  try {
    if (window.ServiceWorkerRegistration && ServiceWorkerRegistration.prototype) {
      const origShow = ServiceWorkerRegistration.prototype.showNotification;
      ServiceWorkerRegistration.prototype.showNotification = function(title, options = {}) {
        emit({
          kind: 'sw_notification',
          title: String(title || '').slice(0, 240),
          body: String((options && options.body) || '').slice(0, 600),
          href: location.href
        });
        if (origShow) {
          try { return origShow.call(this, title, options); } catch (_) {}
        }
        return Promise.resolve();
      };
    }
  } catch (_) {}

  // Throttled DOM & <title> MutationObserver so SPA hydration (already-unread messages /
  // active assignments rendering 2-8s after page load) and silent DOM updates trigger Python scans.
  let domSyncTimer = null;
  let lastDomSyncTs = 0;
  const scheduleDomSync = (delayMs = 1400) => {
    if (domSyncTimer) return;
    const elapsed = Date.now() - lastDomSyncTs;
    const wait = Math.max(delayMs, elapsed < 2200 ? 2200 - elapsed : delayMs);
    domSyncTimer = setTimeout(() => {
      domSyncTimer = null;
      lastDomSyncTs = Date.now();
      try {
        if (typeof window.__nodeReportDomMutation === 'function') {
          window.__nodeReportDomMutation(location.href || '');
        }
      } catch (_) {}
    }, wait);
  };

  const installObservers = () => {
    try {
      if (!document.documentElement) return;
      const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
          if (m.target && m.target.nodeName === 'TITLE') {
            scheduleDomSync(350);
            return;
          }
          if (m.addedNodes && m.addedNodes.length > 0) {
            scheduleDomSync(1400);
            return;
          }
          if (m.type === 'attributes') {
            scheduleDomSync(1600);
            return;
          }
        }
      });
      obs.observe(document.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['class', 'aria-label', 'data-unread', 'data-testid']
      });
    } catch (_) {}
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installObservers, { once: true });
  } else {
    installObservers();
  }
})();
"""

# Executed inside Brave/Chromium page context to extract:
# 1. Existing/new unread message/mail threads rendered in the DOM
# 2. Existing/new active assignments (e.g., AP Classroom, Canvas, Google Classroom, Gradescope, Schoology)
EXTRACT_DOM_STATE_JS = """
(cfg) => {
  const customPreviewSel = (cfg && cfg.previewSelector) || '';
  const isAssignmentMode = Boolean(cfg && cfg.assignmentMode);
  const includeDigest = Boolean(cfg && cfg.includeDigest);

  const UI_NOISE_RE = /^(mark\\s+as\\s+(un)?read|unread|read|inbox|all\\s+mail|filter|sort|compose|new\\s+message|sign\\s+in|log\\s+in|settings|menu|skip\\s+to|search|load\\s+more|show\\s+more|dismiss|close|view\\s+all|s?star\\s+conversation|unstar\\s+conversation|select\\s+(?:message|conversation|item|email)|unread\\s+email|read\\s+email)$/i;
  const SR_ONLY_SEL = '.sr-only, .visually-hidden, [class*="sr-only" i], [class*="visually-hidden" i], [class*="screen-reader" i], [aria-hidden="true"], button, input, svg, style, script';
  const ACTIVE_ASSIGNMENT_RE = /\\b(assigned|in\\s+progress|not\\s+started|not\\s+submitted|no\\s+submission|unsubmitted|missing|overdue|incomplete|pending\\s+submission|due\\s*(?:date|:|by)?\\s*(?:today|tomorrow|tonight|mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\\d{1,2}[\\/\\-]\\d{1,2}))\\b/i;
  const COMPLETED_ASSIGNMENT_RE = /\\b(completed|submitted|turned\\s+in|graded|scored\\s+\\d+|100%\\s*complete|closed\\s+for\\s+submission)\\b/i;
  const PORTAL_HOST_OR_DOM_RE = /(collegeboard|classroom|canvas|instructure|schoology|gradescope|blackboard|brightspace|moodle|deltamath|webassign|assignment|coursework|assessment)/i;

  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.closest('nav, footer, script, style, noscript, [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 24 && r.height > 12;
  };

  // Space-preserving visible text-node walker that skips screen-reader/button noise!
  const extractCleanText = (rootEl) => {
    if (!rootEl) return '';
    const parts = [];
    const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent) continue;
      if (parent.closest(SR_ONLY_SEL)) continue;
      const st = window.getComputedStyle(parent);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.fontSize || '12') < 2) continue;
      const txt = String(node.nodeValue || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
      if (!txt || UI_NOISE_RE.test(txt)) continue;
      parts.push(txt);
    }
    return parts.join(' ').replace(/\\s+/g, ' ').trim();
  };

  const cleanLines = (el) => {
    const out = [];
    const pushClean = (str) => {
      for (const part of String(str || '').replace(/\\u00a0/g, ' ').split(/\\n+/)) {
        const s = part.replace(/\\s+/g, ' ').trim();
        if (s.length >= 2 && !/^\\d+$/.test(s) && !UI_NOISE_RE.test(s) && !/^(?:S|U)$/i.test(s) && !out.includes(s)) {
          out.push(s);
        }
      }
    };

    // 1. Semantic webmail / chat extraction first (ProtonMail, Gmail, Outlook, Teams, Slack)
    const senderEl = el.querySelector(
      '[data-testid*="message-column:sender" i], [data-testid*="sender" i], .item-senders, .yX.xY, [data-tid*="message-author" i], [class*="sender" i]:not(button)'
    );
    const subjectEl = el.querySelector(
      '[data-testid*="message-column:subject" i], [data-testid*="subject" i], .item-subject, .bog, [data-tid*="subject" i], [class*="subject" i]:not(button)'
    );
    const snippetEl = el.querySelector(
      '.y2, [data-testid*="snippet" i], [data-testid*="preview" i], [data-tid*="preview" i], [class*="snippet" i], [class*="preview" i]:not(button)'
    );
    const semSender = extractCleanText(senderEl);
    const semSubj = extractCleanText(subjectEl);
    const semSnip = extractCleanText(snippetEl);
    if (semSubj || semSender) {
      if (semSubj && semSender && semSubj.toLowerCase() !== semSender.toLowerCase()) {
        pushClean(semSubj);
        pushClean(semSender);
      } else {
        pushClean(semSubj || semSender);
      }
      if (semSnip && semSnip !== semSubj) pushClean(semSnip);
      if (out.length > 0) return out;
    }

    // 2. General leaf-node walker (no arbitrary <=24 cap, skips sr-only & buttons!)
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    let tnode;
    while ((tnode = walker.nextNode()) && out.length < 10) {
      const p = tnode.parentElement;
      if (!p || p.closest(SR_ONLY_SEL)) continue;
      const st = window.getComputedStyle(p);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      pushClean(tnode.nodeValue || '');
    }
    if (out.length === 0) {
      pushClean(extractCleanText(el));
    }
    return out;
  };

  const unreadItems = [];
  const seenNodes = new Set();

  // --- A. Unread Messages / Threads / Emails ---
  const unreadSelectors = [];
  if (customPreviewSel && !isAssignmentMode) unreadSelectors.push(customPreviewSel);
  unreadSelectors.push(
    '[data-shortcut-target="item-container"].unread',
    '[data-testid*="message-item" i].unread',
    '[data-tid*="unread" i]',
    '[data-tid*="chat-list-item" i][aria-label*="unread" i]',
    '[role="treeitem"][aria-label*="unread" i]',
    '[role="listitem"][aria-label*="unread" i]',
    'tr.zE',
    '.chat-list-item.unread',
    '.item-container.unread',
    '[data-unread="true"]',
    '[aria-label*="unread" i]',
    '[class*="unread" i]'
  );

  const rawUnreadEls = [];
  for (const sel of unreadSelectors) {
    try {
      for (const el of document.querySelectorAll(sel)) {
        rawUnreadEls.push(el);
      }
    } catch (_) {}
  }

  for (const el of rawUnreadEls) {
    if (unreadItems.length >= 15) break;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'button' || tag === 'input' || el.getAttribute('role') === 'tab' || el.getAttribute('role') === 'button' && (el.innerText || '').trim().length < 18) {
      continue;
    }
    if (!isVisible(el)) continue;

    // Prefer row/listitem container if `el` is just an inner unread dot/badge
    let row = el;
    if ((el.innerText || '').trim().length < 6) {
      const parentRow = el.closest('tr, li, article, [role="listitem"], [role="row"], [class*="item" i], [class*="thread" i], [class*="row" i]');
      if (parentRow && isVisible(parentRow)) row = parentRow;
    }
    if (seenNodes.has(row)) continue;
    // Skip giant wrapper containers that hold multiple rows
    if ((row.innerText || '').length > 900 && row.querySelectorAll('tr, li, [role="listitem"], [role="row"]').length > 2) {
      continue;
    }
    seenNodes.add(row);

    const aria = (row.getAttribute('aria-label') || el.getAttribute('aria-label') || row.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
    const lines = cleanLines(row);
    let title = '';
    let detail = '';

    if (lines.length >= 2) {
      if (lines[0].length <= 48 && lines[1].length <= 140) {
        title = `${lines[0]} — ${lines[1]}`.slice(0, 160);
        detail = lines.slice(2, 5).join(' • ').slice(0, 320);
      } else {
        title = lines[0].slice(0, 140);
        detail = lines.slice(1, 5).join(' • ').slice(0, 320);
      }
    } else if (lines.length === 1 && lines[0].length >= 4) {
      title = lines[0].slice(0, 160);
    } else if (aria.length >= 6 && !UI_NOISE_RE.test(aria) && !/^\\d+\\s+unread$/i.test(aria)) {
      title = aria.slice(0, 160);
    }

    // Enrich detail from aria-label (e.g. Microsoft Teams / Outlook / Slack put full unread message body in aria-label!)
    if (aria.length >= 14 && !/^\\d+\\s+unread$/i.test(aria) && aria.toLowerCase() !== title.toLowerCase()) {
      const cleanedAria = aria.replace(/^unread\\s+(?:message|chat|email|item)\\s*(?:from\\s*)?/i, '').trim();
      if (!detail || (cleanedAria.length > detail.length + 12 && !detail.includes(cleanedAria.slice(0, 20)))) {
        detail = cleanedAria.slice(0, 340);
      }
    }

    if (!title || UI_NOISE_RE.test(title) || /^\\d+$/.test(title)) continue;
    unreadItems.push({ kind: 'unread_message', title, detail });
  }

  // --- B. Active / Incomplete Assignments (AP Classroom, Canvas, Classroom, Gradescope, etc.) ---
  const assignmentItems = [];
  const pageSignalsPortal =
    isAssignmentMode ||
    PORTAL_HOST_OR_DOM_RE.test(location.hostname + ' ' + location.pathname + ' ' + document.title) ||
    Boolean(document.querySelector('[data-testid*="assignment" i], [class*="assignment" i], [class*="assessment" i], [class*="coursework" i]'));

  if (pageSignalsPortal) {
    const assignSelectors = [];
    if (customPreviewSel && isAssignmentMode) assignSelectors.push(customPreviewSel);
    assignSelectors.push(
      '[data-testid*="assignment" i]',
      '[data-testid*="assessment" i]',
      '[class*="assignment-card" i]',
      '[class*="assignment-row" i]',
      '[class*="assignment-item" i]',
      '[class*="assignment" i]',
      '[class*="assessment" i]',
      '[class*="todo-list-item" i]',
      'tbody tr',
      '[role="row"]',
      '[role="listitem"]',
      'article',
      'li'
    );

    const seenAssignNodes = new Set();
    for (const sel of assignSelectors) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(sel)); } catch (_) { continue; }
      for (const node of nodes) {
        if (assignmentItems.length >= 20) break;
        if (seenAssignNodes.has(node) || !isVisible(node)) continue;
        const fullText = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
        if (fullText.length < 8 || fullText.length > 750) continue;
        // Skip wrapper lists that contain child rows also matching
        if (node.querySelectorAll('tr, [role="row"], [role="listitem"], article, [data-testid*="assignment" i]').length > 2) {
          continue;
        }

        // Check if this card/row represents an ACTIVE (unsubmitted/assigned/due) assignment
        const isExplicitAssignCard = /assignment|assessment/i.test(
          (node.className && typeof node.className === 'string' ? node.className : '') + ' ' + (node.getAttribute('data-testid') || '')
        );
        const hasActiveSignal = ACTIVE_ASSIGNMENT_RE.test(fullText) || (isAssignmentMode && isExplicitAssignCard);
        if (!hasActiveSignal) continue;
        if (COMPLETED_ASSIGNMENT_RE.test(fullText)) continue;

        seenAssignNodes.add(node);
        const lines = cleanLines(node);
        if (lines.length === 0) continue;

        // Pick best assignment title (heading/link inside row, or first non-status line)
        let title = '';
        const headingEl = node.querySelector('h1, h2, h3, h4, h5, a, [class*="title" i], [data-testid*="title" i], strong');
        if (headingEl) {
          const hTxt = (headingEl.innerText || headingEl.textContent || '').replace(/\\s+/g, ' ').trim();
          if (hTxt.length >= 3 && !ACTIVE_ASSIGNMENT_RE.test(hTxt) && !UI_NOISE_RE.test(hTxt)) {
            title = hTxt.slice(0, 160);
          }
        }
        if (!title) {
          const nonStatusLine = lines.find((l) => l.length >= 3 && !/^(assigned|in progress|not started|missing|unsubmitted|overdue)$/i.test(l));
          title = (nonStatusLine || lines[0]).slice(0, 160);
        }
        if (!title || UI_NOISE_RE.test(title)) continue;

        const detailParts = lines.filter((l) => l !== title).slice(0, 3);
        const detail = detailParts.join(' • ').slice(0, 260) || 'Active assignment';
        assignmentItems.push({ kind: 'active_assignment', title, detail });
      }
    }
  }

  // --- C. Optional Visible Row Digest for On-Demand / AI (`qwen3.5:2b`) Page Scan ---
  const digest = [];
  if (includeDigest) {
    const candidates = document.querySelectorAll('main tr, main li, main article, [role="row"], [role="listitem"], tr, article');
    for (const el of Array.from(candidates)) {
      if (digest.length >= 25) break;
      if (!isVisible(el)) continue;
      const lines = cleanLines(el);
      if (lines.length === 0) continue;
      const joined = lines.slice(0, 3).join(' | ');
      if (joined.length >= 6 && joined.length <= 240 && !digest.includes(joined)) {
        digest.push(joined);
      }
    }
  }

  return {
    unread_items: unreadItems,
    assignment_items: assignmentItems,
    digest
  };
}
"""

CANDIDATE_UNREAD_JSON_KEYS = (
    "unread_count",
    "unreadCount",
    "unseen_count",
    "unseenCount",
    "badge_count",
    "badgeCount",
    "UnreadItemCount",
    "notification_count",
    "mentions_count",
    "Unread",
)

READABLE_SNIPPET_KEYS = (
    "Subject",
    "subject",
    "SenderName",
    "sender_name",
    "preview",
    "snippet",
)

COMPLETED_STATUS_VALUES = {
    "completed",
    "complete",
    "submitted",
    "turned_in",
    "turnedin",
    "graded",
    "scored",
    "done",
    "closed",
    "archived",
}

ACTIVE_ASSIGNMENT_STATUS_VALUES = {
    "assigned",
    "in_progress",
    "in progress",
    "not_started",
    "not started",
    "unsubmitted",
    "not_submitted",
    "no_submission",
    "missing",
    "overdue",
    "pending",
    "open",
    "active",
    "todo",
}

PGP_OR_CIPHERTEXT_RE = re.compile(
    r"(-----BEGIN PGP|-----END PGP|^[A-Za-z0-9+/=\-_]{44,}$|^[0-9a-fA-F]{40,}$)",
    re.IGNORECASE,
)
LONG_OPAQUE_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{48,}")


def is_intelligible_text(text: Optional[str]) -> bool:
    """
    Detect whether intercepted notification/traffic text is readable human text
    versus PGP armor, base64/hex ciphertext, or binary junk.
    """
    if not text:
        return False
    s = " ".join(str(text).split()).strip()
    if not s:
        return False
    if PGP_OR_CIPHERTEXT_RE.search(s):
        return False
    if LONG_OPAQUE_TOKEN_RE.search(s):
        return False
    if "\ufffd" in s:
        return False
    printable = sum(1 for ch in s if ch.isprintable())
    if printable / max(1, len(s)) < 0.92:
        return False
    alpha_or_digit = sum(1 for ch in s if ch.isalnum())
    if alpha_or_digit == 0:
        return False
    if len(s) > 36 and " " not in s and not s.startswith(("http://", "https://")):
        return False
    return True


def format_readable_or_ping_fallback(
    app_name: str,
    raw_title: Optional[str],
    raw_message: Optional[str],
    unread_count: int = 1,
    strict_zero_knowledge: bool = False,
) -> Tuple[str, str]:
    """
    Clean readable notification titles & previews WITHOUT prepending redundant `<AppName>:`
    (since `source_app` is already rendered as its own tag in the Inbox UI!), while scrubbing
    accessibility/screen-reader artifacts like `SStar conversation` or `Unread email`.
    """
    if strict_zero_knowledge:
        return (
            f"{app_name} ping",
            f"Count updated ({unread_count}).",
        )

    t_ok = is_intelligible_text(raw_title)
    m_ok = is_intelligible_text(raw_message)
    raw_t_str = " ".join(str(raw_title or "").split()).strip() if t_ok else ""
    raw_m_str = " ".join(str(raw_message or "").split()).strip() if m_ok else ""

    if raw_t_str or raw_m_str:
        scrubbed = scrub_notification_artifacts(app_name, raw_t_str, raw_m_str)
        clean_t = scrubbed["title"]
        clean_m = scrubbed["preview"]
        if clean_t and clean_t.lower() != "new message":
            return clean_t[:280], clean_m[:1200]
        if clean_m:
            return clean_m[:160], clean_m[:1200]

    suffix = f" ({unread_count} unread)" if unread_count > 1 else ""
    return f"{app_name} ping{suffix}", f"New activity detected in {app_name}."


def compute_item_fingerprint(app_name: str, title: str) -> str:
    """Stable normalized fingerprint so already-unread items & active assignments never duplicate."""
    norm_app = re.sub(r"[^a-z0-9]+", "", (app_name or "").lower())
    clean_title = (title or "").strip().lower()
    # Strip leading '<app_name>:' prefix if already formatted
    app_prefix = (app_name or "").strip().lower() + ":"
    if clean_title.startswith(app_prefix):
        clean_title = clean_title[len(app_prefix) :].strip()
    norm_title = re.sub(r"[^a-z0-9]+", " ", clean_title).strip()
    digest = hashlib.sha1(f"{norm_app}::{norm_title}".encode("utf-8")).hexdigest()
    return digest[:16]


def _extract_sender_label(obj: Dict[str, Any]) -> str:
    for k in ("Sender", "sender", "from", "author", "user", "creator"):
        val = obj.get(k)
        if isinstance(val, dict):
            name = str(val.get("Name") or val.get("name") or val.get("displayName") or val.get("Address") or val.get("email") or "").strip()
            if is_intelligible_text(name):
                return name[:80]
        elif isinstance(val, str) and is_intelligible_text(val):
            return val.strip()[:80]
    for k in ("SenderName", "sender_name", "from_name", "authorName"):
        val = obj.get(k)
        if isinstance(val, str) and is_intelligible_text(val):
            return val.strip()[:80]
    return ""


def _is_explicitly_unread_dict(obj: Dict[str, Any]) -> bool:
    if obj.get("Unread") in (1, True) or obj.get("unread") in (1, True):
        return True
    if obj.get("is_unread") is True or obj.get("isUnread") is True or obj.get("unseen") is True:
        return True
    if "isRead" in obj and obj.get("isRead") is False:
        return True
    if "read" in obj and obj.get("read") is False:
        return True
    if "seen" in obj and obj.get("seen") is False:
        return True
    return False


def _scan_json_for_unread_and_snippets(
    obj: Any,
    custom_key: Optional[str] = None,
    depth: int = 0,
) -> Tuple[Dict[str, int], List[str], List[Dict[str, str]]]:
    """
    Inspect JSON payloads for:
      1. Integer unread counters (`unread_count`, `Unread`, etc.)
      2. Readable text snippets
      3. Structured unread message items AND active/incomplete assignment items!
    """
    found: Dict[str, int] = {}
    snippets: List[str] = []
    structured_items: List[Dict[str, str]] = []
    if depth > 6:
        return found, snippets, structured_items

    watch_keys = set(CANDIDATE_UNREAD_JSON_KEYS)
    if custom_key:
        watch_keys.add(custom_key)

    if isinstance(obj, dict):
        # 1. Check if this dict is an individual UNREAD message/conversation/notification
        sender_name = _extract_sender_label(obj)
        raw_t_val = (
            obj.get("Subject")
            or obj.get("subject")
            or obj.get("conversation_topic")
            or obj.get("chat_name")
        )
        raw_b_val = (
            obj.get("preview")
            or obj.get("snippet")
            or obj.get("body_preview")
            or obj.get("BodyPreview")
            or obj.get("text")
            or obj.get("content")
        )
        msg_title = strip_html_tags(str(raw_t_val)) if isinstance(raw_t_val, str) else ""
        msg_body = strip_html_tags(str(raw_b_val)) if isinstance(raw_b_val, str) else ""

        if _is_explicitly_unread_dict(obj):
            primary_txt = str(msg_title or msg_body or "").strip()
            if is_intelligible_text(primary_txt):
                item_title = f"{sender_name} — {primary_txt}" if (sender_name and sender_name not in primary_txt) else primary_txt
                detail_txt = str(msg_body or "").strip() if (msg_title and msg_body and msg_body != msg_title and is_intelligible_text(str(msg_body))) else ""
                structured_items.append(
                    {
                        "kind": "unread_message",
                        "title": item_title[:180],
                        "detail": detail_txt[:340],
                    }
                )
                if item_title[:200] not in snippets:
                    snippets.append(item_title[:200])

        # 2. Check if this dict is an ACTIVE ASSIGNMENT (AP Classroom, Canvas, Classroom, Gradescope, etc.)
        has_assign_key = any(
            k in obj
            for k in (
                "assignmentId",
                "assignment_id",
                "assessmentId",
                "assessment_id",
                "courseworkId",
                "plannable_id",
                "submissionStatus",
                "submission_status",
                "dueDate",
                "due_at",
                "dueAt",
            )
        ) or str(obj.get("type") or obj.get("itemType") or obj.get("plannable_type") or "").lower() in (
            "assignment",
            "assessment",
            "quiz",
            "homework",
            "practice",
            "progress_check",
        )

        if has_assign_key:
            a_title = str(
                obj.get("title")
                or obj.get("assessmentTitle")
                or obj.get("assignmentTitle")
                or obj.get("name")
                or ""
            ).strip()
            raw_status = str(
                obj.get("status")
                or obj.get("submissionStatus")
                or obj.get("submission_status")
                or obj.get("workflow_state")
                or obj.get("state")
                or ""
            ).strip().lower()
            is_completed_flag = (
                obj.get("completed") is True
                or obj.get("submitted") is True
                or obj.get("isCompleted") is True
                or raw_status in COMPLETED_STATUS_VALUES
            )
            is_active_status = (not raw_status) or (raw_status in ACTIVE_ASSIGNMENT_STATUS_VALUES)
            if not is_completed_flag and is_active_status and is_intelligible_text(a_title):
                due_raw = str(obj.get("dueDate") or obj.get("due_at") or obj.get("dueAt") or obj.get("endDate") or "").strip()
                course_raw = str(obj.get("courseName") or obj.get("course_name") or obj.get("subjectName") or "").strip()
                meta_bits: List[str] = []
                if course_raw and is_intelligible_text(course_raw):
                    meta_bits.append(course_raw[:60])
                if raw_status:
                    meta_bits.append(raw_status.replace("_", " ").title())
                else:
                    meta_bits.append("Assigned")
                if due_raw and len(due_raw) <= 40:
                    meta_bits.append(f"Due {due_raw}")
                structured_items.append(
                    {
                        "kind": "active_assignment",
                        "title": a_title[:180],
                        "detail": " • ".join(meta_bits)[:260],
                    }
                )

        # 3. Walk nested fields & numeric unread counters
        for k, v in obj.items():
            if k in watch_keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                iv = int(v)
                if 0 <= iv <= 9999:
                    found[k] = found.get(k, 0) + iv
            elif k in READABLE_SNIPPET_KEYS and isinstance(v, str) and is_intelligible_text(v):
                val = v.strip()[:200]
                if val not in snippets:
                    snippets.append(val)
            elif isinstance(v, (dict, list)):
                sub_c, sub_s, sub_items = _scan_json_for_unread_and_snippets(v, custom_key, depth + 1)
                for sk, sv in sub_c.items():
                    found[sk] = found.get(sk, 0) + sv
                for s_item in sub_s:
                    if len(snippets) < 6 and s_item not in snippets:
                        snippets.append(s_item)
                for st_it in sub_items:
                    if len(structured_items) < 20:
                        structured_items.append(st_it)
    elif isinstance(obj, list):
        for item in obj[:40]:
            sub_c, sub_s, sub_items = _scan_json_for_unread_and_snippets(item, custom_key, depth + 1)
            for sk, sv in sub_c.items():
                found[sk] = found.get(sk, 0) + sv
            for s_item in sub_s:
                if len(snippets) < 6 and s_item not in snippets:
                    snippets.append(s_item)
            for st_it in sub_items:
                if len(structured_items) < 20:
                    structured_items.append(st_it)

    return found, snippets, structured_items


@dataclass
class ManagedAppTab:
    slug: str
    app_name: str
    target_url: str
    page: Any
    account_profile: str = "default"
    zero_knowledge: bool = False
    indicator_type: str = "dom_badge"
    last_unread: int = 0
    active_viewers: int = 0
    status: str = "warm"
    opened_at: float = field(default_factory=time.time)
    last_checked_at: float = 0.0
    recent_json_traffic: List[Dict[str, Any]] = field(default_factory=list)
    recent_ws_frames: int = 0
    last_notif_fingerprints: Dict[str, float] = field(default_factory=dict)
    seen_item_fingerprints: Optional[Set[str]] = None
    viewer_callbacks: Set[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = field(default_factory=set)
    settle_task: Optional[asyncio.Task] = None
    last_jpeg_bytes: Optional[bytes] = None
    last_jpeg_dims: Tuple[int, int] = (1600, 900)


ZERO_GOOGLE_PRIVACY_ARGS: List[str] = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--safebrowsing-disable-auto-update",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-pings",
    "--disable-features=OptimizationHints,MediaRouter,Translate,AutofillServerCommunication,InterestFeedContentSuggestions,CertificateTransparencyComponentUpdater,HighEfficiencyModeAvailable,MemorySaverModeAggressiveness,WebContentsDiscard,CalculateNativeWinOcclusion",
]


def resolve_browser_binary() -> Tuple[Optional[str], str]:
    """
    Prefer Brave Browser (`brave-browser`, zero Google telemetry + native Rust Shields)
    and fall back to hardened open-source Chromium with all Google endpoints disabled.
    """
    candidates: List[str] = []
    if settings.browser_executable_path:
        candidates.append(settings.browser_executable_path.strip())
    candidates.extend(
        [
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/opt/brave.com/brave/brave-browser",
        ]
    )
    which_brave = shutil.which("brave-browser") or shutil.which("brave-browser-stable")
    if which_brave and which_brave not in candidates:
        candidates.append(which_brave)

    for cand in candidates:
        if cand and Path(cand).is_file():
            label = "Brave Browser (Shields • Zero-Google)" if "brave" in cand.lower() else f"Custom ({Path(cand).name})"
            return cand, label

    return None, "Hardened Chromium (Zero-Google Flags)"


class ServerBrowserStreamManager:
    """
    Runs a persistent Brave / zero-Google Chromium profile on the Node server (`max@athlon`)
    so monitored web apps execute 100% server-side, stream interactively over binary WebSocket
    frames, and intercept both live pings AND initial-load unreads/assignments 24/7.
    """

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Any = None
        self._browser_loop: Any = None
        self._playwright: Any = None
        self._context: Any = None
        self._contexts: Dict[str, Any] = {}
        self._active_engine_label: str = resolve_browser_binary()[1]
        self._tabs: Dict[str, ManagedAppTab] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._apps_mgr = AppsConfigManager()
        self.stream_fps: int = 12
        self.stream_jpeg_quality: int = 78

    def _get_lock(self) -> asyncio.Lock:
        cur_loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not cur_loop:
            self._lock = asyncio.Lock()
            self._tab_lock = asyncio.Lock()
            self._lock_loop = cur_loop
        return self._lock

    def _get_tab_lock(self) -> asyncio.Lock:
        self._get_lock()
        return self._tab_lock

    async def shutdown(self) -> None:
        cur_loop = asyncio.get_running_loop()
        for tab in list(self._tabs.values()):
            if tab.settle_task and not tab.settle_task.done():
                tab.settle_task.cancel()
        self._tabs.clear()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None
        if self._browser_loop is cur_loop:
            for ctx in list(self._contexts.values()):
                try:
                    await ctx.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
        self._contexts.clear()
        self._context = None
        self._playwright = None
        self._browser_loop = None

    def get_engine_info(self) -> Dict[str, Any]:
        exe, label = resolve_browser_binary()
        return {
            "engine": self._active_engine_label or label,
            "executable": exe or "playwright-chromium",
            "zero_google_telemetry": True,
            "is_brave": bool(exe and "brave" in exe.lower()),
            "active_profiles": list(self._contexts.keys()),
        }

    def _resolve_profile_dir(self, account_profile: str) -> Path:
        norm = slugify_app_name(account_profile or "default")
        if norm == "default":
            return Path(settings.browser_profile_dir)
        return Path(settings.browser_profiles_root) / norm

    def _clean_stale_singleton_locks(self, profile_dir: Path, profile_key: str) -> None:
        if profile_key in self._contexts:
            return
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = profile_dir / lock_name
            if p.exists() or p.is_symlink():
                try:
                    p.unlink()
                except OSError:
                    pass

    async def ensure_browser(self, account_profile: str = "default") -> Any:
        prof_key = slugify_app_name(account_profile or "default")
        cur_loop = asyncio.get_running_loop()
        async with self._get_lock():
            if self._browser_loop is not cur_loop:
                self._contexts.clear()
                self._context = None
                self._playwright = None
                self._tabs.clear()

            if prof_key in self._contexts:
                return self._contexts[prof_key]

            from playwright.async_api import async_playwright

            profile_dir = self._resolve_profile_dir(prof_key)
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._clean_stale_singleton_locks(profile_dir, prof_key)

            exe_path, engine_label = resolve_browser_binary()
            if self._playwright is None:
                self._playwright = await async_playwright().start()

            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": True,
                "viewport": {
                    "width": int(settings.stream_default_width or 1600),
                    "height": int(settings.stream_default_height or 900),
                },
                "user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "args": list(ZERO_GOOGLE_PRIVACY_ARGS),
            }
            if exe_path:
                launch_kwargs["executable_path"] = exe_path

            try:
                ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                self._active_engine_label = engine_label
            except Exception as exc:
                if exe_path:
                    logger.warning("Preferred browser %s failed (%s); falling back to hardened Chromium", exe_path, exc)
                    launch_kwargs.pop("executable_path", None)
                    ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                    self._active_engine_label = "Hardened Chromium (Zero-Google Flags)"
                else:
                    raise

            await ctx.add_init_script(INIT_INTERCEPTOR_JS)
            self._contexts[prof_key] = ctx
            if prof_key == "default" or self._context is None:
                self._context = ctx

            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._background_monitor_loop())

            self._browser_loop = cur_loop
            logger.info("Server %s persistent context [%s] active at %s", self._active_engine_label, prof_key, profile_dir)
            return ctx

    def _lookup_app_config(self, slug: str) -> Dict[str, Any]:
        _, apps = self._apps_mgr.get_global_and_apps()
        norm = slugify_app_name(slug)
        for a in apps:
            if a["slug"] == norm:
                return a
        raise KeyError(f"App '{slug}' not found in registry")

    def _ensure_tab_fingerprints_loaded(self, tab: ManagedAppTab) -> Set[str]:
        """Seed known item fingerprints from SQLite so existing Inbox items never duplicate across restarts."""
        if tab.seen_item_fingerprints is not None:
            return tab.seen_item_fingerprints

        fps: Set[str] = set()
        with Session(engine) as session:
            existing_rows = session.exec(
                select(Notification).where(Notification.source_app == tab.app_name)
            ).all()
            for row in existing_rows:
                if row.raw_payload:
                    try:
                        rp = json.loads(row.raw_payload)
                        if isinstance(rp, dict) and rp.get("fp"):
                            fps.add(str(rp["fp"]))
                    except Exception:
                        pass
                fps.add(compute_item_fingerprint(tab.app_name, row.title))

        tab.seen_item_fingerprints = fps
        return fps

    async def _broadcast_to_viewers(self, tab: ManagedAppTab, payload: Dict[str, Any]) -> None:
        if not tab.viewer_callbacks:
            return
        for cb in list(tab.viewer_callbacks):
            try:
                await cb(payload)
            except Exception:
                tab.viewer_callbacks.discard(cb)

    def _ingest_extracted_items(
        self,
        tab: ManagedAppTab,
        items: List[Dict[str, str]],
        indicator: str,
    ) -> int:
        """
        Persist newly discovered unread messages or active assignments into SQLite Inbox,
        deduplicating by stable item fingerprint so initial page loads capture everything
        once and subsequent polls never spam duplicates.
        """
        if not items or tab.zero_knowledge:
            return 0

        known_fps = self._ensure_tab_fingerprints_loaded(tab)
        added_titles: List[str] = []
        added_ids: List[int] = []
        now_utc = datetime.now(timezone.utc)

        with Session(engine) as session:
            new_notifs: List[Notification] = []
            for item in items:
                raw_t = str(item.get("title") or "").strip()
                raw_d = str(item.get("detail") or "").strip()
                kind = str(item.get("kind") or "unread_message")
                if not is_intelligible_text(raw_t):
                    continue

                fp = compute_item_fingerprint(tab.app_name, raw_t)
                if fp in known_fps:
                    continue
                known_fps.add(fp)

                formatted_title, formatted_msg = format_readable_or_ping_fallback(
                    app_name=tab.app_name,
                    raw_title=raw_t,
                    raw_message=raw_d,
                    unread_count=1,
                    strict_zero_knowledge=False,
                )
                eff_ind = kind if kind == "active_assignment" else indicator
                is_imp, imp_score, imp_reason = importance_service.classify(
                    tab.app_name, formatted_title, formatted_msg, eff_ind
                )
                one_line = importance_service.build_one_line_summary(
                    tab.app_name, formatted_title, formatted_msg, eff_ind
                )
                prio = "high" if (kind == "active_assignment" and any(w in raw_d.lower() for w in ("today", "tonight", "overdue", "missing"))) else "normal"
                notif = Notification(
                    source_app=tab.app_name,
                    title=formatted_title,
                    message=formatted_msg,
                    ai_summary=one_line,
                    status="active",
                    timestamp=now_utc,
                    priority=prio,
                    read_state=False,
                    is_important=is_imp,
                    importance_score=imp_score,
                    importance_reason=imp_reason,
                    category="School" if kind == "active_assignment" else "General",
                    ai_processed=False,
                    indicator_type=eff_ind,
                    raw_payload=json.dumps({"fp": fp, "kind": kind}),
                )
                session.add(notif)
                new_notifs.append(notif)
                added_titles.append(formatted_title)

            if added_titles:
                session.flush()
                added_ids = [n.id for n in new_notifs if n.id is not None]
                hb = session.get(ScraperHeartbeat, tab.app_name)
                if not hb:
                    hb = ScraperHeartbeat(
                        source_app=tab.app_name,
                        indicator_type=indicator,
                        status=tab.status,
                        unread_count=len(added_titles),
                        app_url=tab.target_url,
                        zero_knowledge=tab.zero_knowledge,
                        total_pings=len(added_titles),
                        last_seen=now_utc,
                    )
                else:
                    hb.status = tab.status
                    hb.indicator_type = indicator
                    hb.total_pings = (hb.total_pings or 0) + len(added_titles)
                    hb.last_seen = now_utc
                session.add(hb)
                session.commit()
                reconcile_app_unread_counts(session)

        if added_titles:
            tab.last_unread = max(tab.last_unread, len(added_titles))
            self._schedule_ai_notification_processing(added_ids)
            asyncio.create_task(
                self._broadcast_to_viewers(
                    tab,
                    {
                        "type": "intercepted",
                        "slug": tab.slug,
                        "added_count": len(added_titles),
                        "titles": added_titles[:4],
                        "unread_count": tab.last_unread,
                    },
                )
            )
        return len(added_titles)

    def _schedule_post_navigation_scans(self, tab: ManagedAppTab) -> None:
        """
        SPAs (ProtonMail, Teams, GroupMe, AP Classroom, Canvas) hydrate 1.5s–8s AFTER
        `domcontentloaded`. Schedule staggered post-hydration scans so existing unread
        messages and active assignments are reliably captured on launch and after login!
        """
        if tab.settle_task and not tab.settle_task.done():
            tab.settle_task.cancel()

        async def _run_staggered() -> None:
            for delay in (1.4, 3.6, 7.5):
                await asyncio.sleep(delay)
                if tab.page.is_closed():
                    return
                try:
                    await self._poll_single_tab(tab)
                except Exception:
                    pass

        tab.settle_task = asyncio.create_task(_run_staggered())

    def list_live_tabs(self) -> List[Dict[str, Any]]:
        out = []
        for slug, tab in list(self._tabs.items()):
            if tab.page.is_closed():
                continue
            try:
                cur_url = tab.page.url
            except Exception:
                cur_url = tab.target_url
            out.append(
                {
                    "slug": slug,
                    "name": tab.app_name,
                    "url": cur_url,
                    "account_profile": tab.account_profile,
                    "status": tab.status,
                    "last_unread": tab.last_unread,
                    "active_viewers": tab.active_viewers,
                    "zero_knowledge": tab.zero_knowledge,
                    "recent_api_signals": len(tab.recent_json_traffic),
                    "recent_ws_frames": tab.recent_ws_frames,
                }
            )
        return out

    def live_tab_count(self) -> int:
        return sum(1 for t in self._tabs.values() if not t.page.is_closed())

    async def get_or_open_tab(
        self,
        slug: str,
        custom_url: Optional[str] = None,
        wait_for_dom: bool = True,
    ) -> ManagedAppTab:
        app_cfg = self._lookup_app_config(slug)
        norm_slug = app_cfg["slug"]
        prof_id = str(app_cfg.get("account_profile") or "default")

        async with self._get_tab_lock():
            existing = self._tabs.get(norm_slug)
            if existing and not existing.page.is_closed():
                existing.zero_knowledge = bool(app_cfg["zero_knowledge"])
                existing.indicator_type = str(app_cfg["indicator_type"])
                existing.account_profile = prof_id
                if custom_url and custom_url != existing.page.url:
                    try:
                        await existing.page.goto(
                            custom_url,
                            wait_until="domcontentloaded" if wait_for_dom else "commit",
                            timeout=25000,
                        )
                        self._schedule_post_navigation_scans(existing)
                    except Exception:
                        pass
                return existing

            ctx = await self.ensure_browser(prof_id)
            page = await ctx.new_page()
            tab = ManagedAppTab(
                slug=norm_slug,
                app_name=app_cfg["name"],
                target_url=custom_url or app_cfg["url"],
                page=page,
                account_profile=prof_id,
                zero_knowledge=bool(app_cfg["zero_knowledge"]),
                indicator_type=str(app_cfg["indicator_type"]),
            )
            self._ensure_tab_fingerprints_loaded(tab)

            async def _on_js_notification(raw_json: str) -> None:
                try:
                    payload = json.loads(raw_json)
                    await self._record_intercepted_ping(tab, payload)
                except Exception as exc:
                    logger.debug("JS notification parse error: %s", exc)

            async def _on_dom_mutation(_href: str = "") -> None:
                try:
                    await self._poll_single_tab(tab)
                except Exception as exc:
                    logger.debug("DOM mutation scan error: %s", exc)

            await page.expose_function("__nodeReportNotification", _on_js_notification)
            await page.expose_function("__nodeReportDomMutation", _on_dom_mutation)

            def _on_response(response: Any) -> None:
                asyncio.create_task(self._inspect_network_response(tab, response))

            def _on_websocket(ws: Any) -> None:
                def _on_ws_frame(payload: Any) -> None:
                    if isinstance(payload, str) and len(payload) >= 2 and len(payload) <= 65536:
                        asyncio.create_task(self._inspect_websocket_frame(tab, payload))

                ws.on("framereceived", _on_ws_frame)

            def _on_framenavigated(frame: Any) -> None:
                if frame == page.main_frame:
                    self._schedule_post_navigation_scans(tab)

            page.on("response", _on_response)
            page.on("websocket", _on_websocket)
            page.on("framenavigated", _on_framenavigated)
            self._tabs[norm_slug] = tab

        if wait_for_dom or tab.target_url.startswith("data:"):
            try:
                await page.goto(tab.target_url, wait_until="domcontentloaded", timeout=28000)
            except Exception as exc:
                logger.warning("Initial navigation warning on %s (%s): %s", norm_slug, tab.target_url, exc)
            await self._poll_single_tab(tab)
            self._schedule_post_navigation_scans(tab)
        else:
            async def _bg_nav() -> None:
                try:
                    await page.goto(tab.target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                try:
                    await self._poll_single_tab(tab)
                    self._schedule_post_navigation_scans(tab)
                except Exception:
                    pass

            asyncio.create_task(_bg_nav())

        return tab

    async def rename_tab(self, old_slug: str, new_slug: str, new_name: str) -> bool:
        """
        Re-key a live ManagedAppTab in-place when an app/account is renamed so the open
        Brave page stays mounted without reloading and never emits under the old name.
        """
        norm_old = slugify_app_name(old_slug)
        norm_new = slugify_app_name(new_slug)
        async with self._get_tab_lock():
            tab = self._tabs.get(norm_old)
            if not tab:
                return False
            tab.app_name = new_name
            tab.slug = norm_new
            if norm_old != norm_new:
                self._tabs.pop(norm_old, None)
                self._tabs[norm_new] = tab
            return True

    async def close_tab(self, slug: str) -> bool:
        norm_slug = slugify_app_name(slug)
        tab = self._tabs.pop(norm_slug, None)
        if not tab:
            return False
        if tab.settle_task and not tab.settle_task.done():
            tab.settle_task.cancel()
        try:
            if not tab.page.is_closed():
                await tab.page.close()
        except Exception:
            pass
        return True

    async def _inspect_websocket_frame(self, tab: ManagedAppTab, raw_frame: str) -> None:
        """
        Inspect live WebSocket push frames (Slack, Discord, GroupMe Faye/Push, Teams JSON frames,
        Notion, etc.) for real-time incoming messages and notifications.
        """
        try:
            tab.recent_ws_frames += 1
            cleaned = raw_frame.strip().rstrip("\x1e")
            # Strip Socket.IO numeric packet prefix (e.g. '42["message", {...}]')
            j_idx_brace = cleaned.find("{")
            j_idx_bracket = cleaned.find("[")
            candidates = [i for i in (j_idx_brace, j_idx_bracket) if i != -1]
            if not candidates:
                return
            start = min(candidates)
            if start > 6:
                return
            data = json.loads(cleaned[start:])

            # Look for explicit live push event envelopes or structured unread items
            app_cfg = self._lookup_app_config(tab.slug)
            counters, snippets, structured_items = _scan_json_for_unread_and_snippets(
                data,
                custom_key=app_cfg.get("api_unread_key"),
            )

            # Also check push event envelopes like {"type": "MESSAGE_CREATE", "d": {"content": "...", "author": {"username": "..."}}}
            if isinstance(data, dict):
                ev_name = str(data.get("type") or data.get("t") or data.get("event") or data.get("channel") or "").lower()
                payload_obj = data.get("d") if isinstance(data.get("d"), dict) else (data.get("data") if isinstance(data.get("data"), dict) else data)
                if any(tok in ev_name for tok in ("message", "notification", "chat", "alert", "mention", "assignment")):
                    sender = _extract_sender_label(payload_obj)
                    body = str(
                        payload_obj.get("content")
                        or payload_obj.get("text")
                        or payload_obj.get("body")
                        or payload_obj.get("subject")
                        or payload_obj.get("title")
                        or ""
                    ).strip()
                    if is_intelligible_text(body):
                        title_str = f"{sender} — {body[:120]}" if sender else body[:140]
                        structured_items.append({"kind": "unread_message", "title": title_str, "detail": ""})

            if structured_items and not tab.zero_knowledge:
                self._ingest_extracted_items(tab, structured_items, indicator="websocket_push")
            elif counters:
                primary_key = next(iter(counters))
                new_count = int(counters[primary_key])
                if new_count > tab.last_unread:
                    tab.last_unread = new_count
                    top_s = snippets[0] if snippets else ""
                    title, message = format_readable_or_ping_fallback(
                        app_name=tab.app_name,
                        raw_title=top_s,
                        raw_message="",
                        unread_count=new_count,
                        strict_zero_knowledge=tab.zero_knowledge,
                    )
                    self._persist_ping_to_db(
                        tab=tab,
                        unread_count=new_count,
                        title=title,
                        message=message,
                        indicator="websocket_push",
                    )
        except Exception:
            pass

    async def _inspect_network_response(self, tab: ManagedAppTab, response: Any) -> None:
        """Inspect HTTP/Fetch/GraphQL JSON responses for unread counters AND existing unread/assignment items."""
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype or response.status >= 400:
                return
            url = response.url
            if any(skip in url for skip in (".js", ".css", "telemetry", "analytics", "sentry")):
                return
            body_bytes = await response.body()
            if not body_bytes or len(body_bytes) > 750_000:
                return
            data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
            app_cfg = self._lookup_app_config(tab.slug)
            custom_key = app_cfg.get("api_unread_key")
            counters, snippets, structured_items = _scan_json_for_unread_and_snippets(data, custom_key=custom_key)
            if not counters and not snippets and not structured_items:
                return

            parsed = urlparse(url)
            entry = {
                "ts": round(time.time(), 1),
                "endpoint": f"{parsed.netloc}{parsed.path}"[:140],
                "counters": counters,
                "items_found": len(structured_items),
            }
            tab.recent_json_traffic = (tab.recent_json_traffic + [entry])[-15:]

            # 1. Ingest structured unread messages or active assignments directly (solves initial-load JSON!)
            added_from_items = 0
            if structured_items and not tab.zero_knowledge:
                added_from_items = self._ingest_extracted_items(tab, structured_items, indicator="network_json")

            # 2. Also handle pure counter increases (or count-only zero_knowledge mode)
            if counters:
                primary_key = custom_key if (custom_key and custom_key in counters) else next(iter(counters))
                new_count = int(counters[primary_key])
                if new_count > tab.last_unread:
                    tab.last_unread = new_count
                    if added_from_items == 0:
                        top_snippet = snippets[0] if snippets else ""
                        title, message = format_readable_or_ping_fallback(
                            app_name=tab.app_name,
                            raw_title=top_snippet,
                            raw_message="; ".join(snippets[1:3]) if len(snippets) > 1 else "",
                            unread_count=new_count,
                            strict_zero_knowledge=tab.zero_knowledge,
                        )
                        self._persist_ping_to_db(
                            tab=tab,
                            unread_count=new_count,
                            title=title,
                            message=message,
                            indicator="network_json",
                        )
        except Exception:
            pass

    async def _record_intercepted_ping(self, tab: ManagedAppTab, payload: Dict[str, Any]) -> None:
        now = time.time()
        raw_title = str(payload.get("title") or "").strip()
        raw_body = str(payload.get("body") or "").strip()
        fp = f"{raw_title}|{raw_body}"[:160]
        if fp in tab.last_notif_fingerprints and (now - tab.last_notif_fingerprints[fp]) < 25:
            return
        tab.last_notif_fingerprints[fp] = now

        if not tab.zero_knowledge and is_intelligible_text(raw_title or raw_body):
            item_fp = compute_item_fingerprint(tab.app_name, raw_title or raw_body)
            self._ensure_tab_fingerprints_loaded(tab).add(item_fp)

        tab.last_unread = max(1, tab.last_unread + 1)
        safe_title, safe_body = format_readable_or_ping_fallback(
            app_name=tab.app_name,
            raw_title=raw_title,
            raw_message=raw_body,
            unread_count=tab.last_unread,
            strict_zero_knowledge=tab.zero_knowledge,
        )
        ind = "ping_only" if tab.zero_knowledge else "web_notification"

        self._persist_ping_to_db(
            tab=tab,
            unread_count=tab.last_unread,
            title=safe_title,
            message=safe_body,
            indicator=ind,
        )
        await self._broadcast_to_viewers(
            tab,
            {
                "type": "intercepted",
                "slug": tab.slug,
                "added_count": 1,
                "titles": [safe_title],
                "unread_count": tab.last_unread,
            },
        )

    def _persist_ping_to_db(
        self,
        tab: ManagedAppTab,
        unread_count: int,
        title: str,
        message: str,
        indicator: str,
        emit_notification: bool = True,
    ) -> None:
        now_utc = datetime.now(timezone.utc)
        created_id: Optional[int] = None
        with Session(engine) as session:
            if emit_notification:
                fp = compute_item_fingerprint(tab.app_name, title)
                eff_ind = "ping_only" if tab.zero_knowledge else indicator
                is_imp, imp_score, imp_reason = importance_service.classify(
                    tab.app_name, title, message, eff_ind
                )
                one_line = (
                    None
                    if tab.zero_knowledge
                    else importance_service.build_one_line_summary(tab.app_name, title, message, eff_ind)
                )
                from .decision_gate import compute_dedupe_key

                notif = Notification(
                    source_app=tab.app_name,
                    title=title,
                    message=message,
                    ai_summary=one_line,
                    status="active",
                    timestamp=now_utc,
                    priority="high" if unread_count >= 3 else "normal",
                    read_state=False,
                    is_important=is_imp,
                    importance_score=imp_score,
                    importance_reason=imp_reason,
                    category="General",
                    tags_csv="",
                    dedupe_key=compute_dedupe_key(tab.app_name, title, body=message or ""),
                    ai_processed=False,
                    indicator_type=eff_ind,
                    raw_payload=json.dumps({"fp": fp, "kind": indicator}),
                )
                session.add(notif)
                session.flush()
                created_id = notif.id

            hb = session.get(ScraperHeartbeat, tab.app_name)
            if not hb:
                hb = ScraperHeartbeat(
                    source_app=tab.app_name,
                    indicator_type="ping_only" if tab.zero_knowledge else indicator,
                    status=tab.status,
                    unread_count=0,
                    app_url=tab.target_url,
                    zero_knowledge=tab.zero_knowledge,
                    total_pings=1 if emit_notification else 0,
                    last_seen=now_utc,
                )
            else:
                hb.status = tab.status
                hb.zero_knowledge = tab.zero_knowledge
                hb.indicator_type = "ping_only" if tab.zero_knowledge else indicator
                if emit_notification:
                    hb.total_pings = (hb.total_pings or 0) + 1
                hb.last_seen = now_utc
            session.add(hb)
            session.commit()
            reconcile_app_unread_counts(session)

        if created_id is not None and not tab.zero_knowledge:
            self._schedule_ai_notification_processing([created_id])

    def _schedule_ai_notification_processing(self, notif_ids: List[int]) -> None:
        if not notif_ids:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.process_notifications_with_ai(notif_ids))
        except RuntimeError:
            pass

    async def process_notifications_with_ai(
        self,
        notif_ids: Optional[List[int]] = None,
        limit: int = 20,
    ) -> int:
        """
        Immediately refine newly arrived (or unprocessed cron) notifications through
        Decision model (`/v1/systemone`) with chat-model fallback (`think=False`):
        cleans accessibility artifacts, removes redundant app prefixes, writes 1-line
        previews, and assigns `category`, `is_important`, and `priority`.
        """
        with Session(engine) as session:
            if notif_ids:
                rows = [session.get(Notification, nid) for nid in notif_ids]
                targets = [r for r in rows if r is not None]
            else:
                targets = list(
                    session.exec(
                        select(Notification)
                        .where(Notification.ai_processed == False)  # noqa: E712
                        .order_by(Notification.timestamp.desc())
                        .limit(limit)
                    ).all()
                )
            snapshots = [
                {
                    "id": n.id,
                    "source_app": n.source_app,
                    "title": n.title,
                    "message": n.message,
                    "indicator_type": n.indicator_type,
                    "user_override": n.user_importance_override,
                    "sender": n.sender or "",
                    "body": n.body_text or "",
                }
                for n in targets
                if n.id is not None
            ]

        if not snapshots:
            return 0

        updated_count = 0
        for snap in snapshots:
            try:
                res = await model_router.process_incoming_notification(
                    source_app=snap["source_app"],
                    raw_title=snap["title"],
                    raw_message=snap["message"],
                    indicator_type=snap["indicator_type"],
                    sender=snap.get("sender") or "",
                    body=snap.get("body") or "",
                )
                with Session(engine) as session:
                    n = session.get(Notification, snap["id"])
                    if not n:
                        continue
                    n.title = res["title"]
                    n.ai_summary = res["ai_summary"]
                    # A user correction is authoritative: never let a re-scan or a
                    # later AI pass undo the tag the user chose by hand.
                    if not n.tag_corrected_by_user:
                        n.category = res["category"]
                    if res.get("tags") and not n.tag_corrected_by_user:
                        n.tags_csv = str(res["tags"])[:400]
                    if res.get("sender") and not n.sender:
                        n.sender = str(res["sender"])[:200]
                    if res.get("dedupe_key"):
                        n.dedupe_key = str(res["dedupe_key"])[:64]
                    n.tag_confidence = float(res.get("confidence") or 0.0)
                    n.tag_engine = str(res.get("engine") or "")[:40]
                    if n.user_importance_override is None:
                        # Blend learned sender/keyword override with the AI decision
                        learned_imp, learned_score, learned_reason = importance_service.classify(
                            n.source_app, n.title, n.message, n.indicator_type
                        )
                        if "user-learned" in (learned_reason or ""):
                            n.is_important = learned_imp
                            n.importance_score = learned_score
                        else:
                            n.is_important = bool(res["is_important"])
                            n.priority = res["priority"]
                    n.ai_processed = True
                    session.add(n)
                    session.commit()
                    updated_count += 1
            except Exception as exc:
                logger.debug("AI notification processing skipped id=%s: %s", snap["id"], exc)

        # Collapse duplicates produced by multiple feeds / sender variants BEFORE
        # the Inbox UI ever sees them. One real event = one row with merged tags.
        if updated_count:
            try:
                from .inbox_harvest import merge_duplicate_notifications

                with Session(engine) as session:
                    merge_duplicate_notifications(
                        session,
                        since_ids=[s["id"] for s in snapshots if s.get("id")],
                    )
            except Exception as exc:  # pragma: no cover - never break ingest
                logger.debug("Dedupe merge skipped: %s", exc)
        return updated_count

    async def _poll_single_tab(self, tab: ManagedAppTab, include_digest: bool = False) -> Dict[str, Any]:
        if tab.page.is_closed():
            return {"status": "closed"}

        try:
            app_cfg = self._lookup_app_config(tab.slug)
        except KeyError:
            return {"status": "removed"}

        tab.zero_knowledge = bool(app_cfg["zero_knowledge"])
        tab.indicator_type = str(app_cfg["indicator_type"])
        cur_url = tab.page.url or ""
        lower_url = cur_url.lower()

        auth_patterns = [p.lower() for p in (app_cfg.get("auth_wall_patterns") or []) if p]
        is_auth_wall = any(pat in lower_url for pat in auth_patterns)

        try:
            page_title = await tab.page.title()
        except Exception:
            page_title = ""

        detected_count = 0
        title_rx = app_cfg.get("title_regex") or r"^\((\d+)\)"
        try:
            m = re.search(title_rx, page_title)
            if m and m.groups():
                detected_count = max(detected_count, int(m.group(1)))
        except Exception:
            pass

        selectors = list(app_cfg.get("badge_selectors") or [])
        if selectors and not is_auth_wall:
            try:
                dom_res = await tab.page.evaluate(
                    """(sels) => {
                        let best = 0;
                        for (const s of sels) {
                          try {
                            const els = document.querySelectorAll(s);
                            for (const el of els) {
                              const tag = (el.tagName || '').toLowerCase();
                              if (tag === 'button' || el.getAttribute('role') === 'tab') continue;
                              const txt = (el.textContent || el.getAttribute('aria-label') || '').trim();
                              // Only parse short badge-like numeric strings (avoid matching random dates/IDs inside huge paragraphs)
                              if (txt.length <= 28) {
                                const m = txt.match(/^\\(?(\\d{1,4})\\)?$/) || txt.match(/(\\d{1,4})\\s*(?:unread|new|assigned|missing)/i);
                                if (m) best = Math.max(best, parseInt(m[1], 10));
                              }
                            }
                          } catch (_) {}
                        }
                        return best;
                    }""",
                    selectors,
                )
                if isinstance(dom_res, (int, float)):
                    detected_count = max(detected_count, int(dom_res))
            except Exception:
                pass

        dom_added = 0
        dom_items_total = 0
        page_digest: List[str] = []

        # Extract LMS course grades & assignments (PowerSchool, Schoology, Canvas, Classroom, SIS)
        from .lms_connector import is_lms_app

        is_grades_portal = (
            tab.indicator_type == "grades_tracker"
            or is_lms_app(tab.app_name, lower_url)
            or "/guardian/home" in lower_url
            or "/guardian/scores" in lower_url
        )
        if is_grades_portal and not is_auth_wall and not tab.zero_knowledge:
            try:
                extracted_courses = await grades_service.extract_grades_from_page(tab.page, source_app=tab.app_name)
                if extracted_courses:
                    with Session(engine) as g_session:
                        g_res = grades_service.ingest_courses(
                            g_session,
                            source_app=tab.app_name,
                            raw_courses=extracted_courses,
                            emit_pings=True,
                        )
                    grade_changes = g_res.get("changes_detected") or []
                    if grade_changes:
                        dom_added += len(grade_changes)
                        asyncio.create_task(
                            self._broadcast_to_viewers(
                                tab,
                                {
                                    "type": "intercepted",
                                    "slug": tab.slug,
                                    "added_count": len(grade_changes),
                                    "titles": [c["summary"] for c in grade_changes[:3]],
                                    "unread_count": len(grade_changes),
                                },
                            )
                        )
                    # Do not emit generic unread pings on grade-table rows
                    dom_items_total = max(dom_items_total, len(extracted_courses))
            except Exception as exc:
                logger.debug("Grade scan warning (%s): %s", tab.slug, exc)

        # Extract rendered unread items AND active assignments directly from the hydrated DOM!
        if not is_grades_portal and not is_auth_wall and not tab.zero_knowledge:
            try:
                dom_state = await tab.page.evaluate(
                    EXTRACT_DOM_STATE_JS,
                    {
                        "previewSelector": app_cfg.get("preview_selector") or "",
                        "assignmentMode": tab.indicator_type == "assignment_tracker",
                        "includeDigest": include_digest,
                    },
                )
                if isinstance(dom_state, dict):
                    u_items = list(dom_state.get("unread_items") or [])
                    a_items = list(dom_state.get("assignment_items") or [])
                    page_digest = list(dom_state.get("digest") or [])
                    combined = a_items + u_items
                    dom_items_total = len(combined)
                    if combined:
                        detected_count = max(detected_count, len(combined))
                        dom_added = self._ingest_extracted_items(
                            tab,
                            combined,
                            indicator="active_assignment" if a_items else app_cfg.get("indicator_type", "dom_badge"),
                        )
            except Exception as exc:
                logger.debug("DOM item state extraction warning (%s): %s", tab.slug, exc)

        preview_snippet = ""
        if (
            not tab.zero_knowledge
            and not is_auth_wall
            and dom_added == 0
            and detected_count > tab.last_unread
            and app_cfg.get("preview_selector")
        ):
            try:
                preview_snippet = await tab.page.evaluate(
                    """(sel) => {
                        try {
                          const el = document.querySelector(sel);
                          return el ? (el.innerText || el.textContent || '').trim().slice(0, 260) : '';
                        } catch (_) { return ''; }
                    }""",
                    app_cfg["preview_selector"],
                )
            except Exception:
                preview_snippet = ""

        tab.status = "auth_required" if is_auth_wall else "warm"
        tab.last_checked_at = time.time()

        # Emit aggregate badge ping ONLY if count jumped AND individual DOM items weren't already extracted
        emit_new = (not is_auth_wall) and (detected_count > tab.last_unread) and (dom_added == 0) and (dom_items_total == 0)
        tab.last_unread = max(detected_count, dom_items_total)

        title, message = format_readable_or_ping_fallback(
            app_name=tab.app_name,
            raw_title=preview_snippet,
            raw_message=page_title if (page_title and not preview_snippet) else "",
            unread_count=detected_count,
            strict_zero_knowledge=tab.zero_knowledge,
        )

        self._persist_ping_to_db(
            tab=tab,
            unread_count=detected_count,
            title=title,
            message=message,
            indicator="ping_only" if tab.zero_knowledge else app_cfg.get("indicator_type", "dom_badge"),
            emit_notification=emit_new,
        )
        if emit_new:
            asyncio.create_task(
                self._broadcast_to_viewers(
                    tab,
                    {
                        "type": "intercepted",
                        "slug": tab.slug,
                        "added_count": 1,
                        "titles": [title],
                        "unread_count": detected_count,
                    },
                )
            )

        return {
            "slug": tab.slug,
            "status": tab.status,
            "url": cur_url,
            "title": page_title if is_intelligible_text(page_title) else tab.app_name,
            "unread_count": detected_count,
            "dom_items_detected": dom_items_total,
            "newly_registered": dom_added + (1 if emit_new else 0),
            "digest": page_digest,
        }

    async def _background_monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for tab in list(self._tabs.values()):
                if tab.page.is_closed():
                    continue
                try:
                    await self._poll_single_tab(tab)
                except Exception as exc:
                    logger.debug("Background poll error (%s): %s", tab.slug, exc)

    async def calibrate_app_with_ai(self, slug: str) -> Dict[str, Any]:
        """
        Deep-scan the live server browser tab right now:
          1. Captures all currently rendered unread messages & active assignments (e.g. AP Classroom)
          2. Inspects DOM badge candidates + observed JSON/WebSocket traffic
          3. Uses `qwen3.5:2b` (when reachable) both to extract custom portal items from the visible
             digest AND to calibrate CSS/API selectors for future background checks.
        """
        tab = await self.get_or_open_tab(slug, wait_for_dom=True)
        app_cfg = self._lookup_app_config(slug)
        zk = bool(app_cfg["zero_knowledge"])

        # Step 1: Immediate live DOM + assignment + unread scan (with visible row digest for Qwen)
        poll_now = await self._poll_single_tab(tab, include_digest=not zk)
        ai_extracted_count = 0

        dom_candidates = await tab.page.evaluate(
            """(isZk) => {
                const results = [];
                const nodes = document.querySelectorAll(
                  '[aria-label*="unread" i], [aria-label*="notification" i], [aria-label*="assigned" i], [class*="badge" i], [class*="unread" i], [class*="counter" i], [class*="assignment" i], [data-testid*="badge" i], [data-testid*="counter" i], [data-testid*="assignment" i]'
                );
                for (const el of Array.from(nodes).slice(0, 18)) {
                  const tag = el.tagName.toLowerCase();
                  if (tag === 'button') continue;
                  const cls = (typeof el.className === 'string' ? el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '');
                  const testid = el.getAttribute('data-testid') || '';
                  const aria = el.getAttribute('aria-label') || '';
                  const rawTxt = (el.textContent || '').trim();
                  const numMatch = rawTxt.match(/^\\d+$/);
                  let selector = tag;
                  if (testid) selector = `[data-testid='${testid}']`;
                  else if (cls) selector = `${tag}.${cls}`;
                  results.push({
                    selector,
                    has_integer: Boolean(numMatch),
                    sample: isZk ? (numMatch ? numMatch[0] : '') : rawTxt.slice(0, 40),
                    aria: isZk ? '' : aria.slice(0, 48)
                  });
                }
                return {
                  title: document.title.slice(0, 80),
                  url_host: location.host + location.pathname,
                  candidates: results
                };
            }""",
            zk,
        )

        traffic_summary = tab.recent_json_traffic[-8:]
        chosen_selectors: List[str] = []
        for c in dom_candidates.get("candidates", []):
            sel = c.get("selector")
            if sel and sel not in chosen_selectors and (
                c.get("has_integer") or "badge" in sel.lower() or "counter" in sel.lower() or "assignment" in sel.lower()
            ):
                chosen_selectors.append(sel)
            if len(chosen_selectors) >= 4:
                break

        best_api_key: Optional[str] = app_cfg.get("api_unread_key")
        for t in reversed(traffic_summary):
            counters = t.get("counters") or {}
            if counters:
                best_api_key = next(iter(counters.keys()))
                break

        ai_note = ""
        try:
            digest_lines = poll_now.get("digest") or []
            prompt = (
                "Inspect this live web app page from our server browser and return ONLY JSON with keys:\n"
                '{"badge_selectors": ["..."], "api_unread_key": "...", '
                '"active_items": [{"kind": "active_assignment"|"unread_message", "title": "...", "detail": "..."}], '
                '"notes": "..."}\n'
                "Rules: Only include genuinely unread messages or active/unsubmitted assignments in `active_items` "
                "(ignore completed/submitted items and navigation links).\n"
                f"App: {app_cfg['name']} (mode={app_cfg['indicator_type']})\n"
                f"DOM Candidates: {json.dumps(dom_candidates)}\n"
                f"Visible Row Digest: {json.dumps(digest_lines[:18])}\n"
                f"Observed JSON Traffic: {json.dumps(traffic_summary)}"
            )
            res = await ollama_client.chat_json_or_text(
                system_prompt="You extract unread messages/active assignments and calibrate web monitor selectors. Return concise valid JSON only.",
                user_prompt=prompt,
                enable_thinking=False,
            )
            raw_text = (res.get("content") or "").strip()
            j_start, j_end = raw_text.find("{"), raw_text.rfind("}")
            if j_start != -1 and j_end != -1:
                parsed = json.loads(raw_text[j_start : j_end + 1])
                if isinstance(parsed.get("badge_selectors"), list) and parsed["badge_selectors"]:
                    chosen_selectors = [str(s) for s in parsed["badge_selectors"][:4] if str(s).strip()]
                if parsed.get("api_unread_key"):
                    best_api_key = str(parsed["api_unread_key"]).strip()
                if parsed.get("notes"):
                    ai_note = str(parsed["notes"]).strip()
                if not zk and isinstance(parsed.get("active_items"), list) and parsed["active_items"]:
                    ai_extracted_count = self._ingest_extracted_items(
                        tab,
                        [it for it in parsed["active_items"] if isinstance(it, dict)],
                        indicator="ai_page_scan",
                    )
        except Exception:
            pass

        if not chosen_selectors:
            chosen_selectors = list(app_cfg.get("badge_selectors") or ["[class*='badge']", "[data-testid*='counter']"])

        total_newly_captured = int(poll_now.get("newly_registered", 0)) + ai_extracted_count
        if total_newly_captured > 0:
            summary_note = f"Captured {total_newly_captured} item(s) into Inbox • watching {len(chosen_selectors)} selector(s)"
        else:
            summary_note = ai_note or (
                f"Scanned page ({poll_now.get('dom_items_detected', 0)} active/unread visible, 0 new) • {len(chosen_selectors)} selector(s)"
                + (f" + JSON `{best_api_key}`" if best_api_key else "")
            )

        updated = self._apps_mgr.update_app(
            slug,
            {
                "badge_selectors": chosen_selectors,
                "api_unread_key": best_api_key,
                "calibration_notes": summary_note,
            },
        )
        poll_now.pop("digest", None)
        poll_now["newly_registered"] = total_newly_captured
        return {
            "app": updated,
            "telemetry": {
                "dom_candidates_inspected": len(dom_candidates.get("candidates", [])),
                "json_endpoints_observed": traffic_summary,
                "ws_frames_observed": tab.recent_ws_frames,
                "selected_selectors": chosen_selectors,
                "selected_api_unread_key": best_api_key,
                "notes": summary_note,
                "live_poll": poll_now,
            },
        }

    async def scan_grades_tabs(
        self,
        target_slug: Optional[str] = None,
        reload_idle_tabs: bool = False,
        open_if_configured: bool = False,
    ) -> Dict[str, Any]:
        """
        Scan active PowerSchool / grade-tracker server browser tabs, compare against
        recorded grades in SQLite, update the Grades dashboard, and emit pings on changes.
        """
        from .lms_connector import is_lms_app

        _, all_apps = self._apps_mgr.get_global_and_apps()
        grade_apps = [
            a
            for a in all_apps
            if a.get("enabled", True)
            and (
                (target_slug and a["slug"] == slugify_app_name(target_slug))
                or (not target_slug and is_lms_app(a))
            )
        ]

        scanned_apps: List[str] = []
        all_changes: List[Dict[str, Any]] = []
        total_courses_scanned = 0
        total_assignments_scanned = 0
        auth_required_any = False

        sso_wall_tokens = (
            "/public/home.html",
            "login.microsoftonline.com",
            "accounts.google.com",
            "okta.com",
            "clever.com/in/",
            "launchpad.classlink.com",
            "/oauth2/",
            "/saml2/",
        )

        for app_cfg in grade_apps:
            slug = app_cfg["slug"]
            tab = self._tabs.get(slug)
            if (tab is None or tab.page.is_closed()) and (open_if_configured or target_slug):
                try:
                    tab = await self.get_or_open_tab(slug, wait_for_dom=True)
                except Exception as exc:
                    logger.warning("Could not open grade tab %s: %s", slug, exc)
                    continue

            if tab is None or tab.page.is_closed():
                continue

            if reload_idle_tabs and tab.active_viewers == 0:
                try:
                    cur_lower = (tab.page.url or "").lower()
                    if "/guardian/scores.html" in cur_lower and app_cfg.get("url"):
                        await tab.page.goto(app_cfg["url"], wait_until="domcontentloaded", timeout=22000)
                    else:
                        await tab.page.reload(wait_until="domcontentloaded", timeout=22000)
                    await asyncio.sleep(1.2)
                except Exception:
                    pass

            cur_u = (tab.page.url or "").lower()
            auth_pats = [p.lower() for p in (app_cfg.get("auth_wall_patterns") or []) if p]
            is_sso_redirect = any(tok in cur_u for tok in sso_wall_tokens) or any(p in cur_u for p in auth_pats)
            if is_sso_redirect:
                # Give silent OIDC / SSO redirects up to 2.5s to bounce back to /guardian/home.html automatically
                await asyncio.sleep(2.5)
                cur_u = (tab.page.url or "").lower()
                is_sso_redirect = any(tok in cur_u for tok in sso_wall_tokens) or any(p in cur_u for p in auth_pats)
            if is_sso_redirect:
                tab.status = "auth_required"
                auth_required_any = True
                continue

            try:
                courses = await grades_service.extract_grades_from_page(
                    tab.page,
                    source_app=tab.app_name,
                    fetch_assignments=True,
                    force_assignments=True,
                )
                scanned_apps.append(tab.app_name)
                total_courses_scanned += len(courses)
                total_assignments_scanned += sum(
                    len(c.get("assignments") or []) for c in courses if isinstance(c, dict)
                )
                if courses:
                    with Session(engine) as session:
                        res = grades_service.ingest_courses(
                            session,
                            source_app=tab.app_name,
                            raw_courses=courses,
                            emit_pings=True,
                        )
                    all_changes.extend(res.get("changes_detected") or [])
            except Exception as exc:
                logger.warning("Grade scan failed on %s: %s", slug, exc)

        return {
            "scanned_apps": scanned_apps,
            "courses_scanned": total_courses_scanned,
            "assignments_scanned": total_assignments_scanned,
            "auth_required": auth_required_any,
            "changes_detected": all_changes,
        }

    async def enrich_notification_from_live_tab(self, notif_id: int) -> Dict[str, Any]:
        """
        Actively query the warm server browser tab for rich message/thread context when
        a notification only had a bare '<App> ping' or short snippet, and produce a crisp
        1-line AI summary (`ai_summary`) for the Inbox preview.
        """
        with Session(engine) as session:
            notif = session.get(Notification, notif_id)
            if not notif:
                raise KeyError("Notification not found")
            if notif.indicator_type == "ping_only":
                return {
                    "id": notif.id,
                    "title": notif.title,
                    "message": notif.message,
                    "ai_summary": "Count-only mode enabled for this app.",
                }
            source_app = notif.source_app
            orig_title = notif.title
            orig_msg = notif.message or ""
            ind_type = notif.indicator_type

        slug = slugify_app_name(source_app)
        tab = self._tabs.get(slug)
        harvested_snippets: List[str] = []

        if tab and not tab.page.is_closed() and not tab.zero_knowledge:
            try:
                # Harvest rich unread/feed items, aria-labels, and open message pane preview from the live tab!
                # Safe & non-disruptive: reads DOM without stealing mouse/keyboard if user is viewing.
                search_hint = re.sub(rf"^{re.escape(source_app)}:\s*", "", orig_title, flags=re.IGNORECASE).strip()
                if search_hint.lower().startswith(source_app.lower() + " ping"):
                    search_hint = ""

                dom_peek = await tab.page.evaluate(
                    """(hint) => {
                        const out = [];
                        const push = (s) => {
                          const c = String(s || '').replace(/\\s+/g, ' ').trim();
                          if (c.length >= 8 && c.length <= 500 && !out.includes(c)) out.push(c);
                        };
                        const hintLc = (hint || '').toLowerCase().split('—')[0].trim();

                        // 1. Check unread conversation / activity items & rich aria-labels (Teams, Slack, ProtonMail, Outlook)
                        const rows = document.querySelectorAll(
                          '[data-tid*="chat-list-item" i], [data-tid*="activity" i], [role="treeitem"], [role="listitem"], tr.zE, .unread, [aria-label*="unread" i], [data-shortcut-target="item-container"]'
                        );
                        for (const r of Array.from(rows).slice(0, 30)) {
                          const aria = r.getAttribute('aria-label') || '';
                          const txt = r.innerText || r.textContent || '';
                          const combined = (aria.length > txt.length ? aria : txt).replace(/\\s+/g, ' ').trim();
                          if (!combined) continue;
                          if (hintLc && hintLc.length >= 3 && combined.toLowerCase().includes(hintLc)) {
                            push(combined);
                          } else if (!hintLc && (/unread/i.test(aria) || r.classList.contains('unread') || r.matches('tr.zE'))) {
                            push(combined);
                          }
                          if (out.length >= 4) break;
                        }

                        // 2. If still empty on a generic ping, grab latest visible message bubble / email preview pane
                        if (out.length === 0) {
                          const msgNodes = document.querySelectorAll(
                            '[data-tid*="message-body" i], [class*="message-content" i], [class*="message-body" i], [role="document"] p'
                          );
                          const arr = Array.from(msgNodes);
                          for (const m of arr.slice(Math.max(0, arr.length - 3))) {
                            push(m.innerText || m.textContent || '');
                          }
                        }
                        return out;
                    }""",
                    search_hint,
                )
                if isinstance(dom_peek, list):
                    harvested_snippets.extend([str(s) for s in dom_peek if is_intelligible_text(str(s))])
            except Exception as exc:
                logger.debug("Live tab enrich peek warning (%s): %s", slug, exc)

        best_raw = orig_msg
        if harvested_snippets:
            joined_harvest = " • ".join(harvested_snippets[:2])
            if not best_raw or "new activity detected" in best_raw.lower() or len(joined_harvest) > len(best_raw):
                best_raw = joined_harvest[:1200]

        new_title = scrub_notification_artifacts(source_app, orig_title, best_raw)["title"]
        if (not new_title or new_title.lower().endswith("ping") or new_title.lower().startswith("new activity")) and harvested_snippets:
            new_title = scrub_notification_artifacts(source_app, harvested_snippets[0], "")["title"][:240]

        ai_proc = await model_router.process_incoming_notification(
            source_app=source_app,
            raw_title=new_title,
            raw_message=best_raw,
            indicator_type=ind_type,
        )
        new_title = ai_proc["title"]
        summary_line = ai_proc["ai_summary"]

        with Session(engine) as session:
            notif = session.get(Notification, notif_id)
            if not notif:
                raise KeyError("Notification not found")
            notif.title = new_title
            if best_raw:
                notif.message = best_raw
            notif.ai_summary = summary_line
            notif.category = ai_proc["category"]
            notif.ai_processed = True
            if notif.user_importance_override is None:
                is_imp, sc, rsn = importance_service.classify(source_app, notif.title, notif.message, notif.indicator_type)
                notif.is_important = bool(ai_proc["is_important"]) if "user-learned" not in (rsn or "") else is_imp
                notif.importance_score = sc
                notif.importance_reason = rsn
            session.add(notif)
            session.commit()
            session.refresh(notif)
            return {
                "id": notif.id,
                "title": notif.title,
                "message": notif.message,
                "ai_summary": notif.ai_summary,
                "is_important": notif.is_important,
                "importance_reason": notif.importance_reason,
            }

    async def handle_stream_websocket(self, websocket: WebSocket, slug: str) -> None:
        if settings.auth_enabled:
            raw_tok = websocket.cookies.get(SESSION_COOKIE_NAME) or websocket.query_params.get("_st")
            with Session(engine) as db:
                if not auth_service.verify_session_token(raw_tok, db):
                    await websocket.close(code=4401)
                    return

        await websocket.accept()
        tab: Optional[ManagedAppTab] = None
        cdp: Any = None
        warmup_task: Optional[asyncio.Task] = None
        settle_hd_task: Optional[asyncio.Task] = None
        send_lock = asyncio.Lock()

        async def safe_send_json(payload: Dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_text(json.dumps(payload))

        last_frame_crc: int = -1

        async def safe_send_jpeg_bytes(raw_jpg: bytes, width: int, height: int, force: bool = False) -> None:
            nonlocal last_frame_crc
            crc = zlib.crc32(raw_jpg)
            if not force and crc == last_frame_crc:
                return
            if send_lock.locked() and not force:
                return
            async with send_lock:
                last_frame_crc = crc
                await websocket.send_bytes(raw_jpg)

        try:
            tab = await self.get_or_open_tab(slug, wait_for_dom=False)
            tab.active_viewers += 1
            ctx = await self.ensure_browser(tab.account_profile)
            cdp = await ctx.new_cdp_session(tab.page)
            eng = self.get_engine_info()
            last_sent_meta: Tuple[str, int, int, str, int] = ("", 0, 0, "", -1)

            async def _sync_meta_if_changed(w: int, h: int) -> None:
                nonlocal last_sent_meta
                cur_u = tab.page.url or tab.target_url
                cur_tuple = (cur_u, w, h, tab.status, tab.last_unread)
                if cur_tuple != last_sent_meta:
                    last_sent_meta = cur_tuple
                    await safe_send_json(
                        {
                            "type": "status",
                            "slug": tab.slug,
                            "name": tab.app_name,
                            "url": cur_u,
                            "status": tab.status,
                            "width": w,
                            "height": h,
                            "engine": eng["engine"],
                            "is_brave": eng["is_brave"],
                            "zero_knowledge": tab.zero_knowledge,
                            "unread_count": tab.last_unread,
                        }
                    )

            default_vp = {"width": int(settings.stream_default_width or 1600), "height": int(settings.stream_default_height or 900)}
            vp0 = tab.page.viewport_size or default_vp
            await _sync_meta_if_changed(int(vp0["width"]), int(vp0["height"]))

            # Send cached last frame in <1ms if available so switching windows has zero blank flash!
            if tab.last_jpeg_bytes:
                cw, ch = tab.last_jpeg_dims
                await safe_send_jpeg_bytes(tab.last_jpeg_bytes, cw, ch, force=True)

            async def _push_manual_snapshot(quality_override: Optional[int] = None, force: bool = False) -> None:
                try:
                    q_val = max(45, min(90, int(quality_override or self.stream_jpeg_quality)))
                    raw_jpg = await tab.page.screenshot(type="jpeg", quality=q_val)
                    vp = tab.page.viewport_size or default_vp
                    w_px, h_px = int(vp["width"]), int(vp["height"])
                    tab.last_jpeg_bytes = raw_jpg
                    tab.last_jpeg_dims = (w_px, h_px)
                    await _sync_meta_if_changed(w_px, h_px)
                    await safe_send_jpeg_bytes(raw_jpg, w_px, h_px, force=force)
                except Exception:
                    pass

            def _schedule_sharp_settle_keyframe(delay_s: float = 0.22) -> None:
                nonlocal settle_hd_task
                if settle_hd_task and not settle_hd_task.done():
                    settle_hd_task.cancel()

                async def _emit_hd() -> None:
                    await asyncio.sleep(delay_s)
                    await _push_manual_snapshot(quality_override=86, force=False)

                settle_hd_task = asyncio.create_task(_emit_hd())

            last_frame_sent_ts = 0.0

            def _on_screencast_frame(params: Dict[str, Any]) -> None:
                async def _ack_and_forward() -> None:
                    nonlocal last_frame_sent_ts
                    session_id = params.get("sessionId")
                    try:
                        await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                    except Exception:
                        pass
                    min_interval = 1.0 / max(4, min(24, int(self.stream_fps)))
                    now_m = time.monotonic()
                    if now_m - last_frame_sent_ts < (min_interval * 0.85):
                        return
                    last_frame_sent_ts = now_m
                    meta = params.get("metadata") or {}
                    w = int(meta.get("deviceWidth") or default_vp["width"])
                    h = int(meta.get("deviceHeight") or default_vp["height"])
                    b64 = params.get("data") or ""
                    if not b64:
                        return
                    try:
                        await _sync_meta_if_changed(w, h)
                        await safe_send_jpeg_bytes(base64.b64decode(b64), w, h, force=False)
                    except Exception:
                        pass

                asyncio.create_task(_ack_and_forward())

            cdp.on("Page.screencastFrame", _on_screencast_frame)
            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": max(50, min(88, int(self.stream_jpeg_quality))),
                    "maxWidth": int(settings.stream_max_width or 1920),
                    "maxHeight": int(settings.stream_max_height or 1080),
                    "everyNthFrame": 1,
                },
            )
            await _push_manual_snapshot(quality_override=84, force=True)

            # Register live interception push callback AFTER initial status + first JPEG frame are sent!
            tab.viewer_callbacks.add(safe_send_json)
            self._schedule_post_navigation_scans(tab)

            async def _initial_settle_frames() -> None:
                for delay in (0.3, 0.85, 1.7):
                    await asyncio.sleep(delay)
                    await _push_manual_snapshot(quality_override=85, force=False)

            warmup_task = asyncio.create_task(_initial_settle_frames())

            while True:
                raw_msg = await websocket.receive_text()
                msg = json.loads(raw_msg)
                mtype = msg.get("type")

                if mtype == "mouse":
                    ev_type = msg.get("action", "mouseMoved")
                    x = float(msg.get("x", 0))
                    y = float(msg.get("y", 0))
                    btn = msg.get("button", "left")
                    params: Dict[str, Any] = {
                        "type": ev_type,
                        "x": x,
                        "y": y,
                        "button": btn if ev_type != "mouseMoved" else "none",
                        "clickCount": int(msg.get("clickCount", 1)),
                    }
                    if ev_type == "mouseWheel":
                        params["deltaX"] = float(msg.get("deltaX", 0))
                        params["deltaY"] = float(msg.get("deltaY", 0))
                    await cdp.send("Input.dispatchMouseEvent", params)
                    if ev_type in ("mouseReleased", "mouseWheel"):
                        _schedule_sharp_settle_keyframe(0.22)

                elif mtype == "key":
                    key = str(msg.get("key") or "")
                    text = str(msg.get("text") or "")
                    action = msg.get("action", "press")
                    if action == "insertText" and text:
                        await cdp.send("Input.insertText", {"text": text})
                    elif key:
                        await tab.page.keyboard.press(key)
                    _schedule_sharp_settle_keyframe(0.20)

                elif mtype == "navigate":
                    nav_action = msg.get("action", "goto")
                    if nav_action == "reload":
                        await tab.page.reload(wait_until="domcontentloaded", timeout=25000)
                    elif nav_action == "back":
                        await tab.page.go_back(wait_until="domcontentloaded", timeout=15000)
                    elif nav_action == "forward":
                        await tab.page.go_forward(wait_until="domcontentloaded", timeout=15000)
                    elif nav_action == "goto" and msg.get("url"):
                        target = str(msg["url"]).strip()
                        if not target.startswith(("http://", "https://", "data:")):
                            target = "https://" + target
                        await tab.page.goto(target, wait_until="domcontentloaded", timeout=25000)
                    await self._poll_single_tab(tab)
                    self._schedule_post_navigation_scans(tab)
                    await _push_manual_snapshot(quality_override=85, force=True)

                elif mtype == "resize":
                    max_w = int(settings.stream_max_width or 1920)
                    max_h = int(settings.stream_max_height or 1080)
                    w = max(800, min(max_w, int(msg.get("width", 1600))))
                    h = max(480, min(max_h, int(msg.get("height", 900))))
                    await tab.page.set_viewport_size({"width": w, "height": h})
                    await _push_manual_snapshot(quality_override=84, force=True)

                elif mtype == "set_quality":
                    mode = str(msg.get("mode") or "hd").lower()
                    if mode == "saver":
                        q_new, mw, mh = 58, 1280, 720
                    else:
                        q_new, mw, mh = 80, 1920, 1080
                    self.stream_jpeg_quality = q_new
                    try:
                        await cdp.send("Page.stopScreencast")
                    except Exception:
                        pass
                    await cdp.send(
                        "Page.startScreencast",
                        {"format": "jpeg", "quality": q_new, "maxWidth": mw, "maxHeight": mh, "everyNthFrame": 1},
                    )
                    await _push_manual_snapshot(quality_override=86 if mode == "hd" else 62, force=True)

                elif mtype == "snapshot":
                    await _push_manual_snapshot(quality_override=86, force=True)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("Stream websocket error (%s): %s", slug, exc)
            try:
                await safe_send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
            # Close after reporting, otherwise the client waits forever for
            # frames that will never come (e.g. the browser failed to launch).
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            if warmup_task is not None and not warmup_task.done():
                warmup_task.cancel()
            if settle_hd_task is not None and not settle_hd_task.done():
                settle_hd_task.cancel()
            if tab is not None:
                tab.viewer_callbacks.discard(safe_send_json)
                tab.active_viewers = max(0, tab.active_viewers - 1)
            if cdp is not None:
                try:
                    await cdp.send("Page.stopScreencast")
                    await cdp.detach()
                except Exception:
                    pass


browser_stream_manager = ServerBrowserStreamManager()
