"""
Inbox ingest-time duplicate detection.

Every path that creates an Inbox row (tab DOM rows, intercepted JSON/websocket
items, desktop-notification pop-ups, unread-counter pings, the REST/worker
endpoints) calls :func:`absorb_or_none` **before** inserting. The same email
reaches Node in several shapes::

    "Lab report due Friday"            (inbox row, preview in message)
    "Ms. Lee — Lab report due Friday"  (JSON/websocket: sender glued to title)
    "Ms. Lee" / "Lab report due…"      (desktop pop-up: sender as title)

so identity is a *token set* of title + preview with the sender, the app name
and filler words removed, compared by containment (how much of the shorter
item appears in the longer one). Word order, preview length and sender labels
don't matter.

* containment >= ``SAME`` (and enough words to be specific) → same item:
  the new arrival is folded into the existing row, no new row.
* ``MAYBE`` <= containment < ``SAME`` → borderline; the AI pass asks the
  decision model "same message?" (:func:`borderline_candidates`).

This runs without any model, so duplicates stop even when the AI is offline.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from sqlmodel import Session, select

from .config import settings
from .models import Notification

SAME = 0.8
MAYBE = 0.5
MIN_TOKENS = 3  # fewer informative words than this is too generic to merge on
MAX_TOKENS = 60
SCAN_LIMIT = 400  # newest rows inside the window that are compared

_FILLER = {
    "the", "and", "for", "you", "your", "with", "from", "this", "that", "are", "was",
    "were", "been", "have", "has", "will", "can", "our", "its", "into", "about", "via",
    "new", "message", "messages", "email", "notification", "notifications", "unread",
    "sent", "reply", "replied", "wrote", "just", "now", "today", "conversation",
}
_SENDER_SEP_RE = re.compile(r"\s+[—–|]\s+|\s+-\s+")
_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'@.]*[a-z0-9]|[a-z0-9]")


def _words(text: str) -> List[str]:
    text = _URL_RE.sub(" ", (text or "").lower())
    return [w.strip(".'") for w in _WORD_RE.findall(text)]


def _name_tokens(*names: str) -> Set[str]:
    out: Set[str] = set()
    for n in names:
        out.update(w for w in _words(n) if len(w) > 1)
    return out


def strip_sender_prefix(title: str, sender: str = "") -> str:
    """'Ms. Lee — Lab report due' → 'Lab report due' (short left part only)."""
    t = (title or "").strip()
    parts = _SENDER_SEP_RE.split(t, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()
        if right and len(left.split()) <= 4:
            return right
    if sender:
        s = sender.strip()
        for sep in (":", " -", " —", " –"):
            if s and t.lower().startswith((s + sep).lower()):
                return t[len(s) + len(sep):].strip()
    return t


def content_tokens(title: str, message: str = "", *, sender: str = "", source_app: str = "") -> Set[str]:
    """Order-free identity words: title (sender stripped) + preview, minus names and filler."""
    drop = _name_tokens(sender, source_app) | _FILLER
    words = _words(strip_sender_prefix(title, sender)) + _words(message)
    out: List[str] = []
    for w in words:
        if w.isdigit():
            # Numbers identify things at school (Unit 5 vs Unit 6, Period 3).
            w = "#" + w.lstrip("0")[:6] if w.strip("0") else "#0"
        elif len(w) < 3 or w in drop:
            continue
        if "@" in w:  # an address identifies the sender, not the content
            continue
        if w not in out:
            out.append(w)
        if len(out) >= MAX_TOKENS:
            break
    return set(out)


def containment(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def numbers_conflict(a: Set[str], b: Set[str]) -> bool:
    """Each side has a number the other lacks (Unit 5 vs Unit 6). A cut-off
    preview only loses numbers, which is a subset, not a conflict."""
    na = {t for t in a if t.startswith("#")}
    nb = {t for t in b if t.startswith("#")}
    return bool(na - nb) and bool(nb - na)


def is_same(a: Set[str], b: Set[str]) -> bool:
    small = min(len(a), len(b))
    if small < MIN_TOKENS or numbers_conflict(a, b):
        return False
    score = containment(a, b)
    if small == MIN_TOKENS:
        return score >= 1.0  # three words must all match
    return score >= SAME


def _row_tokens(n: Notification) -> Set[str]:
    return content_tokens(n.title or "", n.message or "", sender=n.sender or "", source_app=n.source_app or "")


def _window_rows(session: Session, window_hours: Optional[int], exclude_ids: Iterable[int] = ()) -> List[Notification]:
    hours = int(window_hours or settings.inbox_thread_window_hours or 72)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.exec(
        select(Notification)
        .where(Notification.timestamp >= cutoff)
        .order_by(Notification.timestamp.desc())
        .limit(SCAN_LIMIT)
    ).all()
    skip = set(exclude_ids)
    return [r for r in rows if r.id not in skip and (r.status or "active") != "deleted"]


def find_duplicate(
    session: Session,
    *,
    source_app: str,
    title: str,
    message: str = "",
    sender: str = "",
    window_hours: Optional[int] = None,
    exclude_ids: Iterable[int] = (),
) -> Optional[Notification]:
    """Best existing row that is the same item, or None."""
    new_tokens = content_tokens(title, message, sender=sender, source_app=source_app)
    if len(new_tokens) < MIN_TOKENS:
        # Count-only pings ("Outlook ping") carry no content: one row per app.
        if not title:
            return None
        for r in _window_rows(session, window_hours, exclude_ids):
            if r.source_app == source_app and (r.title or "").strip().lower() == title.strip().lower():
                return r
        return None
    best: Tuple[float, Optional[Notification]] = (0.0, None)
    for r in _window_rows(session, window_hours, exclude_ids):
        tok = _row_tokens(r)
        if is_same(new_tokens, tok):
            score = containment(new_tokens, tok)
            if score > best[0]:
                best = (score, r)
    return best[1]


def absorb(
    existing: Notification,
    *,
    source_app: str,
    title: str,
    message: str = "",
    sender: str = "",
    indicator_type: str = "",
) -> Notification:
    """Fold a duplicate arrival into ``existing`` (caller commits)."""
    clean_title = strip_sender_prefix(title, sender)
    cur_title = existing.title or ""
    # Prefer a real subject over a bare sender name or a "Sender — subject" form.
    if clean_title and (
        len(_words(cur_title)) <= 3 and len(_words(clean_title)) > len(_words(cur_title))
        or strip_sender_prefix(cur_title, existing.sender or "") != cur_title
    ):
        existing.title = clean_title[:300]
    if message and len(message) > len(existing.message or ""):
        existing.message = message[:2000]
    if sender and not existing.sender:
        existing.sender = sender[:200]
    sources = {s.strip() for s in (existing.dedupe_sources or "").split(",") if s.strip()}
    if source_app and source_app != existing.source_app:
        sources.add(source_app)
    existing.dedupe_sources = ", ".join(sorted(sources))[:400] or None
    existing.merged_count = int(existing.merged_count or 1) + 1
    if indicator_type == "ping_only" or (existing.indicator_type == "ping_only" and indicator_type in ("", "ping_only")):
        # A counter ping that moved again is news: resurface it.
        existing.timestamp = datetime.now(timezone.utc)
        existing.read_state = False
    # A real message the user already read stays read, and stays archived.
    return existing


def absorb_or_none(
    session: Session,
    *,
    source_app: str,
    title: str,
    message: str = "",
    sender: str = "",
    indicator_type: str = "",
    window_hours: Optional[int] = None,
) -> Optional[Notification]:
    """If this arrival duplicates a recent row, merge it in and return that row."""
    if not (title or message):
        return None
    dup = find_duplicate(
        session, source_app=source_app, title=title, message=message, sender=sender, window_hours=window_hours
    )
    if dup is None:
        return None
    absorb(dup, source_app=source_app, title=title, message=message, sender=sender, indicator_type=indicator_type)
    session.add(dup)
    return dup


def borderline_candidates(
    session: Session, row: Notification, *, window_hours: Optional[int] = None, limit: int = 3
) -> List[Notification]:
    """Rows that *might* be the same item as ``row`` (for the decision model to judge)."""
    mine = _row_tokens(row)
    if len(mine) < 4:
        return []
    scored = []
    for r in _window_rows(session, window_hours, exclude_ids=[row.id] if row.id else []):
        tok = _row_tokens(r)
        if min(len(tok), len(mine)) < 4:
            continue
        score = containment(mine, tok)
        if MAYBE <= score and not is_same(mine, tok):
            scored.append((score, r))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [r for _, r in scored[:limit]]


def merge_rows(session: Session, keep: Notification, dupes: Sequence[Notification]) -> Notification:
    """Merge whole rows (used after the decision model confirms a borderline pair)."""
    from .inbox_tags import parse_tags_csv, tags_to_csv

    group = [keep, *dupes]
    corrected = next((n for n in group if n.tag_corrected_by_user), None)
    first_tagged = next((n for n in group if parse_tags_csv(n.tags_csv)), None)
    for d in dupes:
        absorb(keep, source_app=d.source_app, title=d.title, message=d.message or "", sender=d.sender or "")
        keep.merged_count = int(keep.merged_count or 1) + int(d.merged_count or 1) - 1
        if len(d.body_text or "") > len(keep.body_text or ""):
            keep.body_text = d.body_text
        keep.has_attachment = bool(keep.has_attachment or d.has_attachment)
        keep.is_important = bool(keep.is_important or d.is_important)
        keep.starred = bool(keep.starred or d.starred)
        session.delete(d)
    keep.read_state = all(bool(n.read_state) for n in group)
    apply_merged_tags(keep, corrected=corrected, first_tagged=first_tagged)
    session.add(keep)
    return keep


def apply_merged_tags(keep: Notification, *, corrected: Optional[Notification], first_tagged: Optional[Notification]) -> None:
    """
    Tags after a merge. Never union every copy's tags (five copies tagged five
    ways made one email show up under five tags). A user correction on any
    copy wins; otherwise keep one copy's tags for now and queue the merged row
    to be re-tagged once from its fuller content.
    """
    if corrected is not None:
        keep.tags_csv = corrected.tags_csv
        keep.category = corrected.category
        keep.tag_corrected_by_user = True
        return
    if first_tagged is not None and not (keep.tags_csv or "").strip():
        keep.tags_csv = first_tagged.tags_csv
        keep.category = first_tagged.category
    keep.ai_processed = False


SAME_EVENT_YES = 0.85  # decision-model probability needed to merge a borderline pair


async def confirm_borderline_with_gate(ids: Sequence[int], *, window_hours: Optional[int] = None) -> int:
    """
    For freshly processed rows, ask the decision model about borderline look-alikes
    and merge the ones it calls the same item. Returns rows merged away.
    Does nothing when the decision model is offline.
    """
    from .database import engine
    from .decision_gate import same_item_probability

    def _view(n: Notification) -> dict:
        return {"app": n.source_app, "sender": n.sender or "", "subject": n.title or "", "preview": n.body_text or n.message or ""}

    merged = 0
    for nid in ids:
        with Session(engine) as session:
            row = session.get(Notification, nid)
            if row is None:
                continue
            pairs = [(c.id, _view(c)) for c in borderline_candidates(session, row, window_hours=window_hours)]
            mine = _view(row)
        for cid, other in pairs:
            prob = await same_item_probability(mine, other)
            if prob is None:
                return merged  # offline: stop asking
            if prob < SAME_EVENT_YES:
                continue
            with Session(engine) as session:
                a, b = session.get(Notification, nid), session.get(Notification, cid)
                if a is None or b is None:
                    break
                keep, dupe = (a, b) if (a.id or 0) <= (b.id or 0) else (b, a)  # older row survives
                merge_rows(session, keep, [dupe])
                session.commit()
                merged += 1
            break  # this row is gone or merged; next id
    return merged


def fuzzy_merge_window(session: Session, *, window_hours: int = 336, dry_run: bool = False, max_rows: int = 3000) -> dict:
    """
    Clean up copies already in the database (rows stored before ingest-time
    matching existed). Oldest row of each look-alike group survives.
    """
    from .decision_gate import compute_dedupe_key

    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(window_hours))
    rows = list(
        session.exec(
            select(Notification).where(Notification.timestamp >= cutoff).order_by(Notification.id.asc()).limit(max_rows)
        ).all()
    )
    rekeyed = 0
    kept: List[Tuple[Notification, Set[str]]] = []
    groups: List[dict] = []
    by_keep: dict = {}
    for r in rows:
        new_key = compute_dedupe_key(r.source_app, r.title or "", sender=r.sender or "", body=r.message or "")
        if r.dedupe_key != new_key:
            rekeyed += 1
            if not dry_run:
                r.dedupe_key = new_key
                session.add(r)
        tok = _row_tokens(r)
        target = None
        if len(tok) >= MIN_TOKENS:
            for k, ktok in kept:
                if is_same(tok, ktok):
                    target = k
                    break
        else:
            for k, _ in kept:
                if k.source_app == r.source_app and (k.title or "").strip().lower() == (r.title or "").strip().lower():
                    target = k
                    break
        if target is None:
            kept.append((r, tok))
            continue
        by_keep.setdefault(target.id, (target, []))[1].append(r)
    for keep, dupes in by_keep.values():
        groups.append({"kept_id": keep.id, "kept_title": keep.title, "merged_ids": [d.id for d in dupes], "count": 1 + len(dupes)})
        if not dry_run:
            merge_rows(session, keep, dupes)
    if not dry_run:
        session.commit()
    return {
        "groups": groups,
        "merged_groups": len(groups),
        "removed": sum(len(g["merged_ids"]) for g in groups),
        "rekeyed": rekeyed,
    }
