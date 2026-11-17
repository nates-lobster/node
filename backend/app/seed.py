from pathlib import Path
from typing import Dict
from sqlmodel import Session, select

from .config import settings
from .models import Notification, ScraperHeartbeat


def reconcile_app_unread_counts(session: Session) -> Dict[str, int]:
    """
    Ensure every ScraperHeartbeat.unread_count reflects actual unread Notification rows
    in SQLite so cleared or unloaded monitors NEVER show ghost placeholder badges.
    """
    unread_rows = session.exec(
        select(Notification).where(Notification.read_state == False)  # noqa: E712
    ).all()
    counts_by_app: Dict[str, int] = {}
    for n in unread_rows:
        counts_by_app[n.source_app] = counts_by_app.get(n.source_app, 0) + 1

    hbs = session.exec(select(ScraperHeartbeat)).all()
    dirty = False
    for hb in hbs:
        real_cnt = counts_by_app.get(hb.source_app, 0)
        if hb.unread_count != real_cnt:
            hb.unread_count = real_cnt
            session.add(hb)
            dirty = True
    if dirty:
        session.commit()
    return counts_by_app


def reconcile_honest_heartbeats(session: Session) -> None:
    """
    Ensure no stale 'warm' statuses or leftover unread counts remain in SQLite
    when no real Playwright scraper state file exists on disk.
    """
    state_file = Path(settings.scraper_state_path)
    if not state_file.is_file():
        rows = session.exec(select(ScraperHeartbeat)).all()
        dirty = False
        for hb in rows:
            if hb.status == "warm":
                hb.status = "not_started"
                hb.total_pings = 0
                dirty = True
                session.add(hb)
        if dirty:
            session.commit()
    reconcile_app_unread_counts(session)


def seed_initial_data(session: Session) -> None:
    """
    No fake pings, tasks, or prepopulated schedule events.
    Simply reconciles heartbeat counts against real unread notifications.
    """
    reconcile_honest_heartbeats(session)
