import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select

from .memory_store import memory_store
from .model_router import _normalize_task_key, _smart_heuristic_duration, model_router
from .models import CalendarEvent, CalendarFeed, Notification, Task, normalize_priority_value


PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}

ASSIGNMENT_EVENT_RE = re.compile(
    r"\b(?:due|assignment|hw|homework|quiz|exam|test|midterm|final|essay|paper|project|lab|pset|problem\s+set|worksheet|reading|frq|mcq|submit|submission|deadline|presentation|schoology|canvas|ap\s+classroom|gradescope)\b",
    re.IGNORECASE,
)

ROUTINE_NON_TASK_EVENT_RE = re.compile(
    r"^(?:lunch|recess|homeroom|advisory|passing\s+period|bus|commute|sleep|bedtime|wake\s+up|dinner|breakfast)\b",
    re.IGNORECASE,
)


def sort_tasks_for_display(tasks: List[Task]) -> List[Task]:
    """
    Canonical task ordering:
      1. Open tasks before completed tasks
      2. Explicit manual `sort_order` if set (`> 0`), grouped cleanly with priority
      3. Priority (`critical` -> `high` -> `normal` -> `low`)
      4. Due date (earliest first, undated after dated)
      5. ID ascending
    """
    return sorted(
        tasks,
        key=lambda t: (
            1 if t.completed else 0,
            PRIORITY_RANK.get((t.priority or "normal").lower(), 2),
            t.sort_order if (t.sort_order and t.sort_order > 0) else 999999,
            0 if t.due_date else 1,
            t.due_date or "9999-12-31",
            t.id or 0,
        ),
    )


def _existing_task_keys(session: Session) -> Set[str]:
    all_tasks = list(session.exec(select(Task)).all())
    keys: Set[str] = set()
    for t in all_tasks:
        k = _normalize_task_key(t.title)
        if k:
            keys.add(k)
    return keys


def _next_sort_order(session: Session) -> int:
    rows = list(session.exec(select(Task.sort_order)).all())
    nums = [int(x) for x in rows if x is not None]
    return (max(nums) + 10) if nums else 10


async def extract_and_save_tasks_from_text(
    session: Session,
    raw_text: str,
    source_name: str = "Notes",
    today_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """
    AI-driven task extractor for Files / Markdown Notes with strict deduplication.
    Never blindly splits every newline into a 25m task, and never duplicates already-added tasks!
    """
    today_str = today_iso or datetime.now(timezone.utc).date().isoformat()
    all_existing = list(session.exec(select(Task)).all())
    existing_titles = [t.title for t in all_existing]
    existing_keys = {_normalize_task_key(t) for t in existing_titles if t}

    # First check how many candidate items match before dedup so we can report skipped duplicates accurately
    candidates_unfiltered = await model_router.extract_tasks_with_ai(
        raw_text=raw_text,
        source_name=source_name,
        existing_titles=(),
        today_iso=today_str,
    )

    created: List[Task] = []
    skipped_duplicates = 0
    order_cursor = _next_sort_order(session)

    for item in candidates_unfiltered:
        title = str(item.get("title") or "").strip()
        key = _normalize_task_key(title)
        if not key:
            continue
        if key in existing_keys:
            skipped_duplicates += 1
            continue
        existing_keys.add(key)

        est = item.get("estimated_minutes")
        if not est or int(est) <= 0:
            est = _smart_heuristic_duration(title)

        t = Task(
            source_app=source_name[:120],
            title=title[:400],
            due_date=item.get("due_date"),
            completed=False,
            category=str(item.get("category") or "General")[:80],
            priority=normalize_priority_value(item.get("priority") or "normal"),
            sort_order=order_cursor,
            estimated_minutes=max(5, min(240, int(est))),
        )
        order_cursor += 10
        session.add(t)
        created.append(t)

    if created:
        session.commit()
        for t in created:
            session.refresh(t)

    return {
        "created_count": len(created),
        "skipped_duplicates": skipped_duplicates,
        "tasks": created,
    }


async def sync_tasks_from_calendar_and_inbox(
    session: Session,
    include_calendar: bool = True,
    include_inbox: bool = True,
    days_ahead: int = 14,
    today_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Scan Calendar events (especially Schoology / Canvas / imported `.ics` assignment feeds)
    and unread Important Inbox notifications to extract & add missing tasks with AI durations!
    """
    today_str = today_iso or datetime.now(timezone.utc).date().isoformat()
    horizon_str = (date.fromisoformat(today_str) + timedelta(days=max(1, min(60, days_ahead)))).isoformat()

    existing_keys = _existing_task_keys(session)
    existing_linked_event_ids: Set[int] = {
        int(e.linked_task_id)
        for e in session.exec(select(CalendarEvent).where(CalendarEvent.linked_task_id != None)).all()  # noqa: E711
        if e.linked_task_id is not None
    }
    feeds_by_id: Dict[int, CalendarFeed] = {
        f.id: f for f in session.exec(select(CalendarFeed)).all() if f.id is not None
    }

    created_from_cal: List[Task] = []
    created_from_inbox: List[Task] = []
    skipped_dups = 0
    order_cursor = _next_sort_order(session)

    # ------------------------------------------------------------------
    # 1. Calendar & Schoology .ics Feed Scanner
    # ------------------------------------------------------------------
    if include_calendar:
        # Look BACK a few weeks for LMS assignment deadlines: an assignment that
        # went overdue two days ago is still a task you need in front of you.
        # Only assignment-shaped events are eligible in the lookback window
        # (a normal past calendar event is history, not a to-do).
        lookback_str = (date.fromisoformat(today_str) - timedelta(days=21)).isoformat()
        cal_events = list(
            session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.start_at >= f"{lookback_str}T00:00")
                .where(CalendarEvent.start_at <= f"{horizon_str}T23:59")
                .where(CalendarEvent.completed == False)  # noqa: E712
                .order_by(CalendarEvent.start_at.asc())
            ).all()
        )
        cal_candidates_lines: List[str] = []
        direct_cal_items: List[Tuple[CalendarEvent, str, str]] = []

        for ev in cal_events:
            if ev.linked_task_id or (ev.source or "").lower() in {"ai secretary", "node coordinator"}:
                continue
            if ROUTINE_NON_TASK_EVENT_RE.match(ev.title or ""):
                continue

            feed = feeds_by_id.get(ev.feed_id) if ev.feed_id else None
            feed_name = feed.name if feed else (ev.source or "Calendar")
            combined_meta = f"{feed_name} {ev.category or ''} {ev.title} {ev.notes or ''}"

            is_schoology_or_lms = bool(
                re.search(r"schoology|canvas|classroom|gradescope|blackboard|moodle|assignment|homework", combined_meta, re.IGNORECASE)
            )
            has_assign_cue = bool(ASSIGNMENT_EVENT_RE.search(f"{ev.title} {ev.notes or ''}"))

            # Schoology exports assignments as all-day or 23:59 / due-time VEVENTs!
            is_deadline_slot = ev.all_day or ev.start_at[11:16] in {"23:59", "23:55", "00:00"} or ev.start_at == ev.end_at

            # Past events only qualify as overdue assignments, never as generic items.
            is_past = ev.start_at[:10] < today_str
            looks_like_assignment = bool(is_schoology_or_lms or has_assign_cue or is_deadline_slot or ev.feed_id is not None)
            if is_past and not (looks_like_assignment and (has_assign_cue or is_deadline_slot)):
                continue

            if is_schoology_or_lms and (has_assign_cue or is_deadline_slot or ev.feed_id is not None):
                due_d = ev.start_at[:10]
                src_label = "Schoology" if "schoology" in combined_meta.lower() else feed_name
                direct_cal_items.append((ev, due_d, src_label))
                cal_candidates_lines.append(
                    f"- [Date: {due_d}, Source: {src_label}] {ev.title}"
                    + (f" — {ev.notes[:140]}" if ev.notes else "")
                )
            elif has_assign_cue:
                due_d = ev.start_at[:10]
                direct_cal_items.append((ev, due_d, feed_name))
                cal_candidates_lines.append(f"- [Date: {due_d}, Source: {feed_name}] {ev.title}")

        if cal_candidates_lines:
            # Ask fast AI to refine titles, durations, and priorities if online
            ai_parsed = await model_router.fast_json(
                system_prompt=(
                    f"Today is {today_str}. Below are upcoming Calendar / Schoology feed items. "
                    "Convert ONLY genuine assignments, exams to study for, deadlines, or deliverables into tasks "
                    "(skip regular class attendance blocks). Return strict JSON:\n"
                    "{\"tasks\": [{\"title\": \"...\", \"due_date\": \"YYYY-MM-DD\", "
                    "\"estimated_minutes\": 40, \"priority\": \"critical|high|normal|low\", \"source\": \"Schoology\"}]}"
                ),
                user_prompt="\n".join(cal_candidates_lines[:30]),
                num_predict=650,
                timeout=20.0,
            )

            if isinstance(ai_parsed, dict) and isinstance(ai_parsed.get("tasks"), list) and ai_parsed["tasks"]:
                for item in ai_parsed["tasks"]:
                    if not isinstance(item, dict):
                        continue
                    t_title = str(item.get("title") or "").strip()
                    if len(t_title) < 3:
                        continue
                    key = _normalize_task_key(t_title)
                    if not key or key in existing_keys:
                        skipped_dups += 1
                        continue
                    existing_keys.add(key)
                    est = item.get("estimated_minutes") or _smart_heuristic_duration(t_title)
                    due_d = str(item.get("due_date") or today_str)[:10]
                    prio = normalize_priority_value(
                        item.get("priority") or ("high" if due_d <= (date.fromisoformat(today_str) + timedelta(days=1)).isoformat() else "normal")
                    )
                    src = str(item.get("source") or "Schoology")[:100]
                    t = Task(
                        source_app=src,
                        title=t_title[:400],
                        due_date=due_d,
                        completed=False,
                        category="School" if "school" in src.lower() or ASSIGNMENT_EVENT_RE.search(t_title) else "General",
                        priority=prio,
                        sort_order=order_cursor,
                        estimated_minutes=max(5, min(240, int(est))),
                    )
                    order_cursor += 10
                    session.add(t)
                    created_from_cal.append(t)
            if not created_from_cal:
                # Deterministic Schoology / LMS extraction when the AI is offline
                # OR returns no usable tasks (small models often drop items).
                tomorrow_iso = (date.fromisoformat(today_str) + timedelta(days=1)).isoformat()
                for ev, due_d, src_label in direct_cal_items:
                    clean_t = re.sub(r"^(?:Assignment|Homework|HW|Due)\s*[:\-]\s*", "", ev.title.strip(), flags=re.IGNORECASE)
                    key = _normalize_task_key(clean_t)
                    if not key or key in existing_keys:
                        skipped_dups += 1
                        continue
                    existing_keys.add(key)
                    est = _smart_heuristic_duration(clean_t)
                    prio = "critical" if due_d <= today_str else ("high" if due_d <= tomorrow_iso else "normal")
                    if due_d < today_str:
                        clean_t = f"{clean_t} (overdue)"
                    t = Task(
                        source_app=src_label[:120],
                        title=clean_t[:400],
                        due_date=due_d,
                        completed=False,
                        category="School",
                        priority=prio,
                        sort_order=order_cursor,
                        estimated_minutes=est,
                    )
                    order_cursor += 10
                    session.add(t)
                    created_from_cal.append(t)

    # ------------------------------------------------------------------
    # 2. Actionable Inbox Notification Scanner
    # ------------------------------------------------------------------
    if include_inbox:
        unread_important = list(
            session.exec(
                select(Notification)
                .where(Notification.read_state == False)  # noqa: E712
                .where(Notification.is_important == True)  # noqa: E712
                .order_by(Notification.timestamp.desc())
                .limit(25)
            ).all()
        )
        # Exclude pure grade-change pings or count-only pings
        action_notifs = [
            n
            for n in unread_important
            if n.indicator_type not in {"ping_only", "grade_change"}
        ]
        if action_notifs:
            notif_digest = "\n".join(
                f"- [Service: {n.source_app}] {n.title} | {(n.ai_summary or n.message or '')[:160]}"
                for n in action_notifs[:20]
            )
            ai_inbox = await model_router.fast_json(
                system_prompt=(
                    f"Today is {today_str}. Inspect these unread Inbox notifications and extract ONLY concrete "
                    "tasks/actions the user needs to perform (e.g. reply to a specific person's question, submit an "
                    "assignment, sign a form, prepare for a meeting). Skip pure informational updates or newsletters.\n"
                    "Return strict JSON: {\"tasks\": [{\"title\": \"...\", \"due_date\": \"YYYY-MM-DD\" or null, "
                    "\"estimated_minutes\": 10, \"priority\": \"high|normal|low\", \"source\": \"ProtonMail\"}]}"
                ),
                user_prompt=notif_digest,
                num_predict=550,
                timeout=20.0,
            )
            if isinstance(ai_inbox, dict) and isinstance(ai_inbox.get("tasks"), list):
                for item in ai_inbox["tasks"]:
                    if not isinstance(item, dict):
                        continue
                    t_title = str(item.get("title") or "").strip()
                    if len(t_title) < 4:
                        continue
                    key = _normalize_task_key(t_title)
                    if not key or key in existing_keys:
                        skipped_dups += 1
                        continue
                    existing_keys.add(key)
                    est = item.get("estimated_minutes") or _smart_heuristic_duration(t_title)
                    due_raw = item.get("due_date")
                    due_d = str(due_raw)[:10] if due_raw and re.match(r"^\d{4}-\d{2}-\d{2}", str(due_raw)) else today_str
                    t = Task(
                        source_app=str(item.get("source") or "Inbox")[:120],
                        title=t_title[:400],
                        due_date=due_d,
                        completed=False,
                        category="School" if ASSIGNMENT_EVENT_RE.search(t_title) else "General",
                        priority=normalize_priority_value(item.get("priority") or "normal"),
                        sort_order=order_cursor,
                        estimated_minutes=max(5, min(180, int(est))),
                    )
                    order_cursor += 10
                    session.add(t)
                    created_from_inbox.append(t)
            else:
                # Conservative offline fallback: only convert explicit assignment_tracker or due/reply requests
                INBOX_ACTION_RE = re.compile(
                    r"\b(?:please\s+(?:reply|send|submit|review|confirm|sign|complete)|due\s+(?:today|tomorrow|tonight|\w+day)|action\s+required|rsvp|assignment|homework|missing\s+submission)\b",
                    re.IGNORECASE,
                )
                for n in action_notifs:
                    blob = f"{n.title} {n.ai_summary or ''} {n.message or ''}"
                    if n.indicator_type != "assignment_tracker" and not INBOX_ACTION_RE.search(blob):
                        continue
                    prefix = "" if n.indicator_type == "assignment_tracker" else f"Reply / Action ({n.source_app}): "
                    cand_title = f"{prefix}{n.title}"[:240]
                    key = _normalize_task_key(n.title)
                    if not key or key in existing_keys:
                        skipped_dups += 1
                        continue
                    existing_keys.add(key)
                    est = _smart_heuristic_duration(cand_title)
                    t = Task(
                        source_app=n.source_app[:120],
                        title=cand_title,
                        due_date=today_str,
                        completed=False,
                        category="School" if n.indicator_type == "assignment_tracker" else "General",
                        priority=normalize_priority_value(n.priority or "normal"),
                        sort_order=order_cursor,
                        estimated_minutes=est,
                    )
                    order_cursor += 10
                    session.add(t)
                    created_from_inbox.append(t)

    all_created = created_from_cal + created_from_inbox
    if all_created:
        session.commit()
        for t in all_created:
            session.refresh(t)

    return {
        "created_count": len(all_created),
        "from_calendar": len(created_from_cal),
        "from_inbox": len(created_from_inbox),
        "skipped_duplicates": skipped_dups,
        "tasks": all_created,
    }
