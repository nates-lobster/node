"""
Connector items -> Inbox rows
=============================

Rules:

* **Seen** (ext id already imported): only sync "read". If the source says it
  was read, the row is marked read. Never the other way round: a row the user
  read in Node stays read.
* **New version of a known thread** (e.g. a reply in an Outlook conversation):
  update that row in place — new preview/body/time, unread again, AI re-run —
  so a conversation is one row. Tags the user corrected are left alone.
* **First sync**: read items older than ``connector_backfill_days`` are skipped
  so connecting an old mailbox doesn't flood the Inbox.
* **Same item already in the Inbox from another path** (live tab, extension,
  pop-up): fold into that row with :func:`inbox_ingest.absorb`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import Session, select

from ..config import settings
from ..database import engine
from ..decision_gate import compute_dedupe_key
from ..importance_filter import importance_service
from ..inbox_ingest import absorb, find_duplicate
from ..models import Notification, ScraperHeartbeat
from .base import ConnectorItem

logger = logging.getLogger("node.connectors.ingest")


def plan_items(
    items: Iterable[ConnectorItem], seen: Set[str], *, first_run: bool, now: Optional[datetime] = None
) -> Tuple[List[ConnectorItem], List[ConnectorItem]]:
    """Split into (to_create, already_seen)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(0, int(settings.connector_backfill_days)))
    create: List[ConnectorItem] = []
    known: List[ConnectorItem] = []
    for it in items:
        if it.ext_id in seen:
            known.append(it)
            continue
        if first_run and it.read and (it.timestamp is None or it.timestamp < cutoff):
            continue
        create.append(it)
    return create, known


def _payload(connector_key: str, it: ConnectorItem) -> str:
    return json.dumps({"connector": connector_key, "ext": it.ext_id, "thread": it.thread_id or it.ext_id, "url": it.url})


def _thread_marker(it: ConnectorItem) -> str:
    return json.dumps({"thread": it.thread_id or it.ext_id})[1:-1]  # '"thread": "…"'


def _rows_for_thread(session: Session, source_app: str, connector_key: str, it: ConnectorItem) -> List[Notification]:
    marker = _thread_marker(it)
    rows = session.exec(
        select(Notification).where(
            Notification.source_app == source_app,
            Notification.raw_payload.contains(marker, autoescape=True),  # type: ignore[union-attr]
        )
    ).all()
    out = []
    for r in rows:
        try:
            if json.loads(r.raw_payload or "{}").get("connector") == connector_key:
                out.append(r)
        except Exception:
            continue
    return out


def _from_connector(row: Notification, connector_key: str) -> bool:
    try:
        return json.loads(row.raw_payload or "{}").get("connector") == connector_key
    except Exception:
        return False


def write_items(
    *,
    connector_key: str,
    source_app: str,
    app_url: str,
    create: List[ConnectorItem],
    known: List[ConnectorItem],
) -> Dict[str, Any]:
    from ..seed import reconcile_app_unread_counts

    now = datetime.now(timezone.utc)
    created_ids: List[int] = []
    refreshed_ids: List[int] = []
    merged = 0
    marked_read = 0
    with Session(engine) as session:
        new_rows: List[Notification] = []
        for it in create:
            indicator = "active_assignment" if it.kind == "active_assignment" else "api_connector"
            title = (it.title or "").strip()[:300] or "(no subject)"
            message = (it.message or "").strip()[:2000]
            body = (it.body_text or "").strip()

            # A new version of a thread we already have: refresh that row.
            same_thread = _rows_for_thread(session, source_app, connector_key, it) if it.thread_id else []
            if same_thread:
                row = max(same_thread, key=lambda r: int(r.id or 0))
                row.title = title
                row.message = message
                if body:
                    row.body_text = body[:12000]
                    row.body_fetched_at = now
                if it.sender:
                    row.sender = it.sender[:200]
                row.timestamp = it.timestamp or now
                row.read_state = bool(it.read)
                row.has_attachment = bool(it.has_attachment or row.has_attachment)
                row.thread_count = max(int(it.thread_count or 0), int(row.thread_count or 0))
                row.priority = it.priority if it.priority in ("high", "critical") else row.priority
                row.raw_payload = _payload(connector_key, it)
                row.folder = "inbox" if not it.read else row.folder
                row.ai_processed = False
                session.add(row)
                refreshed_ids.append(int(row.id))
                continue

            dup = find_duplicate(session, source_app=source_app, title=title, message=message, sender=it.sender)
            if dup is not None and not _from_connector(dup, connector_key):
                absorb(dup, source_app=source_app, title=title, message=message, sender=it.sender, indicator_type=indicator)
                if body and len(body) > len(dup.body_text or ""):
                    dup.body_text = body[:12000]
                    dup.body_fetched_at = now
                if it.read and not dup.read_state:
                    dup.read_state = True
                if not dup.raw_payload or "connector" not in dup.raw_payload:
                    dup.raw_payload = _payload(connector_key, it)
                session.add(dup)
                merged += 1
                continue

            is_imp, imp_score, imp_reason = importance_service.classify(source_app, title, message, indicator)
            one_line = importance_service.build_one_line_summary(source_app, title, message, indicator)
            row = Notification(
                source_app=source_app,
                title=title,
                message=message,
                ai_summary=one_line,
                status="active",
                timestamp=it.timestamp or now,
                priority=it.priority or "normal",
                read_state=bool(it.read),
                is_important=is_imp,
                importance_score=imp_score,
                importance_reason=imp_reason,
                category="School" if it.kind == "active_assignment" else "General",
                ai_processed=False,
                indicator_type=indicator,
                sender=(it.sender or "")[:200] or None,
                body_text=body[:12000] or None,
                body_fetched_at=now if body else None,
                has_attachment=bool(it.has_attachment),
                thread_count=int(it.thread_count or 0),
                dedupe_key=compute_dedupe_key(source_app, title, sender=it.sender, body=message),
                raw_payload=_payload(connector_key, it),
            )
            session.add(row)
            new_rows.append(row)

        for it in known:
            if not it.read:
                continue
            for row in _rows_for_thread(session, source_app, connector_key, it):
                if not row.read_state:
                    row.read_state = True
                    session.add(row)
                    marked_read += 1

        session.flush()
        created_ids = [int(r.id) for r in new_rows if r.id is not None]

        hb = session.get(ScraperHeartbeat, source_app)
        if hb is None:
            hb = ScraperHeartbeat(source_app=source_app, indicator_type="api_connector", app_url=app_url or None)
        hb.status = "warm"
        hb.indicator_type = "api_connector"
        hb.last_seen = now
        hb.total_pings = int(hb.total_pings or 0) + len(created_ids)
        if app_url:
            hb.app_url = app_url
        session.add(hb)
        session.commit()
        reconcile_app_unread_counts(session)

    ai_ids = created_ids + refreshed_ids
    if ai_ids:
        try:
            from ..browser_stream import browser_stream_manager

            browser_stream_manager._schedule_ai_notification_processing(ai_ids)
        except Exception as exc:  # AI is best-effort; rows are already saved
            logger.debug("AI scheduling skipped: %s", exc)
    return {
        "created": len(created_ids),
        "refreshed": len(refreshed_ids),
        "merged": merged,
        "marked_read": marked_read,
        "ids": ai_ids,
    }


def mark_app_status(source_app: str, status: str, app_url: str = "") -> None:
    """Reflect connector health on the app's heartbeat (e.g. 'auth_required')."""
    with Session(engine) as session:
        hb = session.get(ScraperHeartbeat, source_app)
        if hb is None:
            hb = ScraperHeartbeat(source_app=source_app, indicator_type="api_connector", app_url=app_url or None)
        hb.status = status
        hb.last_seen = datetime.now(timezone.utc)
        session.add(hb)
        session.commit()
