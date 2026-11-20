"""
Inbox Body Harvest & Dedupe-Merge (Node v10.2)
==============================================

Two jobs:

1. **Go read the message.** A notification row often only knows a subject line
   ("3 new updates"), which is not enough to ditch Gmail/ProtonMail as a client.
   :class:`InboxHarvester` opens the *live server-side tab* for that source app
   (the same Brave session that already holds the cookies), finds the DOM node
   that matches the notification, clicks it, reads the conversation body, and
   writes `body_text` / `sender` / `thread_count` / `has_attachment` back to the
   row. Selectors are generic first (ARIA + role-based) with per-service
   refinements for ProtonMail, Gmail, Outlook/Teams and LMS inboxes.

2. **Merge the duplicates.** :func:`merge_duplicate_notifications` groups rows
   by `dedupe_key` inside a rolling window and collapses them: tags union,
   important = any, read = all read, body = longest, timestamps = oldest
   (so the thread keeps its real arrival time). The surviving row records
   `merged_from` so the UI can show "5 sources".

Everything is best-effort: if the tab is closed or the service changed its DOM,
we return `ok=False` with the reason and leave the row untouched.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .inbox_tags import parse_tags_csv, tags_to_csv
from .models import Notification


# ---------------------------------------------------------------------------
# DOM extraction scripts (run inside the live tab)
# ---------------------------------------------------------------------------
_EXTRACT_SCRIPT = r"""
(args) => {
  const needle = String(args.needle || "").toLowerCase().trim();
  const maxChars = Number(args.maxChars || 8000);

  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && Number(st.opacity) > 0.05;
  };
  const clean = (s) =>
    String(s || "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();

  // 1. Find the row / list item that matches the notification subject.
  const rowSelectors = [
    "tr.zA", "div[role='row']", "li[role='listitem']", "div[role='listitem']",
    ".message-list-item", ".conversation-item", ".mail-item", ".item-row",
    "li", "tr", "article", "div[data-testid*='message']", "div[data-testid*='conversation']"
  ];
  let candidate = null;
  let bestScore = 0;
  const tokens = needle.split(/\s+/).filter((t) => t.length > 2).slice(0, 8);
  for (const sel of rowSelectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (_e) { continue; }
    for (const el of nodes) {
      if (!visible(el)) continue;
      const txt = (el.innerText || "").toLowerCase();
      if (!txt || txt.length < 6) continue;
      let score = 0;
      for (const t of tokens) if (txt.includes(t)) score += 1;
      if (needle && txt.includes(needle.slice(0, 40))) score += 2;
      // Prefer compact rows: a huge container matches everything.
      if (txt.length > 4000) score -= 2;
      if (score > bestScore) { bestScore = score; candidate = el; }
    }
    if (candidate && bestScore >= Math.max(2, Math.ceil(tokens.length * 0.5))) break;
  }

  // 2. If we found a clickable row, open it.
  let clicked = false;
  if (candidate) {
    const clickable = candidate.querySelector("a, button, [role='link'], [role='button']") || candidate;
    try {
      clickable.scrollIntoView({ block: "center" });
      clickable.click();
      clicked = true;
    } catch (_e) {}
  }

  const readBody = () => {
    const bodySelectors = [
      "div[role='main'] article", ".message-body", ".message-content", ".email-body",
      ".conversation-body", ".thread-body", "[data-testid='message-body']",
      "[data-testid='conversation-body']", ".prose", ".mail-body", ".body-text",
      "div[aria-label*='Message body' i]", "div[aria-label*='conversation' i]"
    ];
    let best = "";
    for (const sel of bodySelectors) {
      let nodes;
      try { nodes = document.querySelectorAll(sel); } catch (_e) { continue; }
      for (const el of nodes) {
        if (!visible(el)) continue;
        const txt = clean(el.innerText);
        if (txt.length > best.length) best = txt;
      }
    }
    if (!best) {
      const main = document.querySelector("div[role='main'], main, article");
      if (main && visible(main)) best = clean(main.innerText);
    }
    return best.slice(0, maxChars);
  };

  const readMeta = () => {
    const meta = { sender: "", subject: "", attachments: false, thread_count: 0 };
    const senderCandidates = [
      "[data-testid='message-sender']", ".message-sender", ".from", "[class*='sender' i]",
      "[aria-label*='From' i]", "span[email]", "[data-hovercard-id]"
    ];
    for (const sel of senderCandidates) {
      const el = document.querySelector(sel);
      if (el && visible(el)) {
        const t = clean(el.getAttribute("email") || el.innerText || el.getAttribute("aria-label") || "");
        if (t) { meta.sender = t.slice(0, 160); break; }
      }
    }
    const subjectEl = document.querySelector("h1, [data-testid='message-subject'], .subject, h2");
    if (subjectEl && visible(subjectEl)) meta.subject = clean(subjectEl.innerText).slice(0, 240);
    meta.attachments = Boolean(
      document.querySelector("[data-testid*='attachment'], .attachment, [aria-label*='attachment' i], img[src*='attachment']")
    );
    const threadEl = document.querySelectorAll(".message, article, [data-testid='message'], .message-item");
    meta.thread_count = Math.min(99, threadEl.length);
    return meta;
  };

  const meta = readMeta();
  const firstPass = readBody();
  return {
    clicked,
    matched: Boolean(candidate),
    score: bestScore,
    subject: meta.subject,
    sender: meta.sender,
    has_attachment: meta.attachments,
    thread_count: meta.thread_count,
    body: firstPass,
    title: document.title,
    url: location.href,
  };
}
"""


_SERVICE_MATCH_HINTS: Dict[str, str] = {
    "gmail": "tr.zA",
    "protonmail": ".message-list-item, [data-testid='message-list-item']",
    "outlook": "div[role='row']",
    "teams": "[data-tid='chat-list-item'], [role='listitem']",
    "canvas": ".conversation, .message-list-item",
    "schoology": ".message-item, .inbox-message",
}


class InboxHarvester:
    """Reads full message bodies out of the already-authenticated live tabs."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, slug: str) -> asyncio.Lock:
        if slug not in self._locks:
            self._locks[slug] = asyncio.Lock()
        return self._locks[slug]

    async def _resolve_tab(self, app_name: str):
        """Return a live ManagedAppTab for this app, opening it if the app is configured."""
        from .browser_stream import browser_stream_manager

        slug = re.sub(r"[^a-z0-9]+", "-", (app_name or "").strip().lower()).strip("-")
        live = getattr(browser_stream_manager, "tabs", {}) or {}
        tab = live.get(slug)
        if tab is not None and getattr(tab, "page", None) is not None and not tab.page.is_closed():
            return slug, tab
        try:
            tab = await browser_stream_manager.get_or_open_tab(slug)
            return slug, tab
        except Exception:
            return slug, None

    async def harvest_notification(self, notif_id: int) -> Dict[str, Any]:
        """Open the message in its live tab and persist the body + extracted metadata."""
        with Session(engine) as session:
            notif = session.get(Notification, notif_id)
            if not notif:
                return {"ok": False, "reason": "notification_not_found"}
            snap = {
                "id": notif.id,
                "source_app": notif.source_app,
                "title": notif.title,
                "message": notif.message,
                "ai_summary": notif.ai_summary,
            }

        slug, tab = await self._resolve_tab(snap["source_app"])
        if tab is None:
            return {"ok": False, "reason": "no_live_tab", "slug": slug}

        needle = (snap["title"] or "").split(" — ")[0].strip() or snap["title"] or ""
        script = _EXTRACT_SCRIPT
        hint = next(
            (sel for key, sel in _SERVICE_MATCH_HINTS.items() if key in (snap["source_app"] or "").lower()),
            "",
        )
        try:
            async with self._lock(slug):
                result = await tab.page.evaluate(
                    script,
                    {"needle": needle[:180], "maxChars": int(settings.inbox_body_max_chars), "hint": hint},
                )
                if result.get("clicked"):
                    # Give the client-side router / network a beat to paint the body.
                    await asyncio.sleep(0.9)
                    result = await tab.page.evaluate(
                        script,
                        {"needle": needle[:180], "maxChars": int(settings.inbox_body_max_chars), "hint": hint},
                    )
        except Exception as exc:
            return {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}", "slug": slug}

        body = str(result.get("body") or "").strip()
        sender = str(result.get("sender") or "").strip()
        if not body and not sender:
            return {"ok": False, "reason": "no_content_extracted", "slug": slug}

        with Session(engine) as session:
            notif = session.get(Notification, notif_id)
            if not notif:
                return {"ok": False, "reason": "notification_deleted"}
            # Never overwrite a richer body with a poorer one.
            if len(body) > len(notif.body_text or ""):
                notif.body_text = body[:12000]
            if sender and (not notif.sender or len(sender) > len(notif.sender)):
                notif.sender = sender[:200]
            if result.get("has_attachment"):
                notif.has_attachment = True
            if result.get("thread_count"):
                notif.thread_count = int(result["thread_count"])
            notif.body_fetched_at = datetime.now(timezone.utc)
            session.add(notif)
            session.commit()
            session.refresh(notif)
            return {
                "ok": True,
                "id": notif.id,
                "body_chars": len(notif.body_text or ""),
                "sender": notif.sender,
                "has_attachment": bool(notif.has_attachment),
                "thread_count": notif.thread_count,
                "url": result.get("url"),
                "notification": notif.model_dump(mode="json"),
            }

    async def refresh_selected(self, ids: Sequence[int], limit: int = 10) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        for nid in list(ids)[:limit]:
            results.append(await self.harvest_notification(int(nid)))
        ok = sum(1 for r in results if r.get("ok"))
        return {"ok": ok > 0, "harvested": ok, "attempted": len(results), "results": results}


inbox_harvester = InboxHarvester()


# ---------------------------------------------------------------------------
# Dedupe / merge
# ---------------------------------------------------------------------------
def merge_duplicate_notifications(
    session: Session,
    *,
    window_hours: Optional[int] = None,
    since_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Collapse rows that represent the *same real-world event*.

    Identity is the content-addressed `dedupe_key` (subject + body, sender and
    source app deliberately excluded). When a match is found the newest row is
    merged into the oldest inside the window.
    """
    window_hours = int(window_hours or settings.inbox_thread_window_hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    stmt = select(Notification).where(Notification.timestamp >= cutoff).order_by(Notification.timestamp.asc())
    rows = list(session.exec(stmt).all())
    if since_ids:
        wanted = {int(i) for i in since_ids}
        # Include the candidate rows plus anything they might collide with.
        keys = {r.dedupe_key for r in rows if r.id in wanted and r.dedupe_key}
        rows = [r for r in rows if r.dedupe_key in keys or r.id in wanted]

    buckets: Dict[str, List[Notification]] = {}
    for r in rows:
        key = (r.dedupe_key or "").strip()
        if not key:
            key = f"_row{r.id}"
        buckets.setdefault(key, []).append(r)

    merged_groups: List[Dict[str, Any]] = []
    removed = 0
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda n: (n.timestamp or datetime.now(timezone.utc)))
        keep = group[0]
        dupes = group[1:]
        merged_ids: List[int] = [n.id for n in group if n.id is not None]
        # read = all read; important = any important; priority = strongest.
        keep.read_state = all(bool(n.read_state) for n in group)
        keep.is_important = any(n.is_important is not False for n in group)
        prio_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        keep.priority = min((n.priority or "normal" for n in group), key=lambda p: prio_rank.get(p, 2))
        longest_body = max(group, key=lambda n: len(n.body_text or ""))
        keep.body_text = longest_body.body_text
        keep.sender = next((n.sender for n in group if n.sender), keep.sender)
        keep.has_attachment = any(bool(n.has_attachment) for n in group)
        keep.thread_count = max(int(n.thread_count or 0) for n in group)
        keep.dedupe_sources = tags_to_csv(
            {*(parse_tags_csv(keep.dedupe_sources)), *(n.source_app for n in dupes if n.source_app)}
        )[:400]
        keep.merged_count = int(sum(int(n.merged_count or 1) for n in group))
        if not dry_run:
            from .inbox_ingest import apply_merged_tags

            apply_merged_tags(
                keep,
                corrected=next((n for n in group if n.tag_corrected_by_user), None),
                first_tagged=next((n for n in group if parse_tags_csv(n.tags_csv)), None),
            )

        info = {
            "dedupe_key": key,
            "kept_id": keep.id,
            "kept_title": keep.title,
            "merged_ids": [n.id for n in dupes],
            "tags": keep.tags_csv,
            "sources": keep.dedupe_sources,
            "count": len(group),
        }
        merged_groups.append(info)
        if dry_run:
            continue
        for n in dupes:
            session.delete(n)
            removed += 1
        session.add(keep)

    if not dry_run and merged_groups:
        session.commit()

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "groups": merged_groups,
        "merged_groups": len(merged_groups),
        "removed": removed,
        "window_hours": window_hours,
    }


def backfill_dedupe_keys(session: Session) -> int:
    """Compute `dedupe_key` for legacy rows that predate the v10.2 schema."""
    from .decision_gate import compute_dedupe_key

    updated = 0
    for n in session.exec(select(Notification).where(Notification.dedupe_key == None)).all():  # noqa: E711
        n.dedupe_key = compute_dedupe_key(n.source_app, n.title, sender=n.sender or "", body=n.body_text or n.message or "")
        session.add(n)
        updated += 1
    if updated:
        session.commit()
    return updated
