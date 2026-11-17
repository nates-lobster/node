from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlmodel import Session, select

from .memory_store import memory_store
from .models import CalendarEvent, CalendarFeed, LongTermGoal, Task, utc_now

BYDAY_MAP = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _unescape_ics_text(val: str) -> str:
    return (
        val.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _escape_ics_text(val: str) -> str:
    return (
        val.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def normalize_iso_local(dt_str: str, default_hour: int = 9) -> str:
    """Normalize input strings (`YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, ISO Z) to `YYYY-MM-DDTHH:MM`."""
    s = dt_str.strip()
    if len(s) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return f"{s}T{default_hour:02d}:00"
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+")[0]
    return s[:16]


def parse_local_dt(iso_str: str) -> datetime:
    norm = normalize_iso_local(iso_str)
    return datetime.strptime(norm, "%Y-%m-%dT%H:%M")


def format_local_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _parse_ics_datetime(raw_val: str, params: str = "") -> Tuple[datetime, bool]:
    """Parse RFC-5545 DTSTART/DTEND value into `(naive_datetime, is_all_day)`."""
    v = raw_val.strip()
    is_date_only = ("VALUE=DATE" in params.upper() and "DATE-TIME" not in params.upper()) or len(v) == 8

    if is_date_only:
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(hour=0, minute=0), True

    clean = v.rstrip("Z")
    if "T" in clean:
        date_part, time_part = clean.split("T", 1)
        hh = int(time_part[0:2]) if len(time_part) >= 2 else 0
        mm = int(time_part[2:4]) if len(time_part) >= 4 else 0
        d = datetime.strptime(date_part[:8], "%Y%m%d")
        dt = d.replace(hour=hh, minute=mm)
        # If UTC 'Z' timestamp, keep clean UTC/local representation
        return dt, False

    d = datetime.strptime(clean[:8], "%Y%m%d")
    return d, True


def parse_ics_content(
    ics_text: str,
    default_category: str = "Imported",
    default_color: str = "sky",
    source_label: str = "ICS Import",
    source_name: Optional[str] = None,
    horizon_days: int = 90,
) -> List[Dict[str, Any]]:
    """
    Parse RFC-5545 `.ics` text into normalized event dicts, including line unfolding
    and weekly/daily `RRULE` expansion across the active planning window.
    """
    if source_name:
        source_label = source_name
    # 1. Unfold folded lines per RFC-5545 (CRLF or LF followed by space/tab)
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    lines = [ln.strip("\r") for ln in unfolded.splitlines() if ln.strip()]

    events: List[Dict[str, Any]] = []
    in_event = False
    props: Dict[str, Tuple[str, str]] = {}

    window_start = datetime.now() - timedelta(days=14)
    window_end = datetime.now() + timedelta(days=horizon_days)

    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            props = {}
            continue
        if upper == "END:VEVENT":
            if in_event and "DTSTART" in props:
                parsed = _convert_vevent_props(
                    props=props,
                    default_category=default_category,
                    default_color=default_color,
                    source_label=source_label,
                    window_start=window_start,
                    window_end=window_end,
                )
                events.extend(parsed)
            in_event = False
            props = {}
            continue

        if in_event and ":" in line:
            left, val = line.split(":", 1)
            if ";" in left:
                key, param_str = left.split(";", 1)
            else:
                key, param_str = left, ""
            props[key.upper()] = (val, param_str)

    return events


def _convert_vevent_props(
    props: Dict[str, Tuple[str, str]],
    default_category: str,
    default_color: str,
    source_label: str,
    window_start: datetime,
    window_end: datetime,
) -> List[Dict[str, Any]]:
    dtstart_raw, dtstart_params = props["DTSTART"]
    try:
        start_dt, all_day = _parse_ics_datetime(dtstart_raw, dtstart_params)
    except ValueError:
        return []

    if "DTEND" in props:
        try:
            end_dt, _ = _parse_ics_datetime(props["DTEND"][0], props["DTEND"][1])
        except ValueError:
            end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
    else:
        end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))

    if end_dt <= start_dt:
        end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))

    duration = end_dt - start_dt
    title = _unescape_ics_text(props.get("SUMMARY", ("Untitled Event", ""))[0]) or "Untitled Event"
    location = _unescape_ics_text(props.get("LOCATION", ("", ""))[0]) or None
    notes = _unescape_ics_text(props.get("DESCRIPTION", ("", ""))[0])[:1800] or None
    raw_uid = props.get("UID", ("", ""))[0].strip()
    if not raw_uid:
        raw_uid = hashlib.sha256(f"{title}:{dtstart_raw}".encode("utf-8")).hexdigest()[:24]

    rrule_raw = props.get("RRULE", ("", ""))[0].strip()
    occurrences: List[Tuple[datetime, str]] = []

    if rrule_raw:
        parts = dict(
            item.split("=", 1) for item in rrule_raw.upper().split(";") if "=" in item
        )
        freq = parts.get("FREQ", "")
        max_count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else 60
        until_dt = window_end
        if "UNTIL" in parts:
            try:
                u_parsed, _ = _parse_ics_datetime(parts["UNTIL"])
                until_dt = min(until_dt, u_parsed + timedelta(days=1))
            except ValueError:
                pass

        if freq == "WEEKLY":
            byday_tokens = [t.strip()[-2:] for t in parts.get("BYDAY", "").split(",") if t.strip()]
            weekdays = [BYDAY_MAP[t] for t in byday_tokens if t in BYDAY_MAP] or [start_dt.weekday()]
            cursor_date = max(start_dt.date(), window_start.date())
            emitted = 0
            while cursor_date <= until_dt.date() and emitted < max_count:
                if cursor_date >= start_dt.date() and cursor_date.weekday() in weekdays:
                    occ_start = datetime.combine(cursor_date, start_dt.time())
                    occurrences.append((occ_start, f"{raw_uid}::{cursor_date.isoformat()}"))
                    emitted += 1
                cursor_date += timedelta(days=1)
        elif freq == "DAILY":
            cursor = start_dt
            emitted = 0
            while cursor <= until_dt and emitted < max_count:
                if cursor >= window_start:
                    occurrences.append((cursor, f"{raw_uid}::{cursor.date().isoformat()}"))
                cursor += timedelta(days=1)
                emitted += 1

    if not occurrences:
        occurrences.append((start_dt, raw_uid))

    results: List[Dict[str, Any]] = []
    for occ_start, occ_uid in occurrences:
        occ_end = occ_start + duration
        if all_day:
            # Store all-day events cleanly on their date 00:00 -> 23:59 so single-day grids render accurately
            start_iso = f"{occ_start.strftime('%Y-%m-%d')}T00:00"
            last_day = (occ_end - timedelta(seconds=1)).strftime("%Y-%m-%d")
            end_iso = f"{last_day}T23:59"
        else:
            start_iso = format_local_dt(occ_start)
            end_iso = format_local_dt(occ_end)

        results.append(
            {
                "title": title[:290],
                "start_at": start_iso,
                "end_at": end_iso,
                "all_day": all_day,
                "category": default_category,
                "color": default_color,
                "location": location,
                "notes": notes,
                "source": source_label,
                "external_uid": occ_uid[:250],
            }
        )

    return results


async def sync_feed_from_url(feed: CalendarFeed, session: Session) -> Dict[str, Any]:
    """Fetch remote `.ics` / `webcal://` feed URL and upsert events cleanly into SQLite."""
    fetch_url = feed.url.strip()
    if fetch_url.lower().startswith("webcal://"):
        fetch_url = "https://" + fetch_url[9:]

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "StudioHub-CalendarSync/2.0"},
        ) as client:
            resp = await client.get(fetch_url)
            resp.raise_for_status()
            ics_text = resp.text
    except Exception as exc:
        feed.last_status = f"Sync error: {exc.__class__.__name__}"
        feed.last_synced_at = utc_now()
        session.add(feed)
        session.commit()
        raise RuntimeError(f"Could not fetch calendar link ({exc})") from exc

    parsed_events = parse_ics_content(
        ics_text=ics_text,
        default_category=feed.category,
        default_color=feed.color,
        source_label=feed.name,
    )

    inserted = 0
    updated = 0
    for item in parsed_events:
        existing = session.exec(
            select(CalendarEvent).where(
                CalendarEvent.feed_id == feed.id,
                CalendarEvent.external_uid == item["external_uid"],
            )
        ).first()
        if existing:
            existing.title = item["title"]
            existing.start_at = item["start_at"]
            existing.end_at = item["end_at"]
            existing.all_day = item["all_day"]
            existing.location = item["location"]
            existing.notes = item["notes"]
            existing.color = feed.color
            existing.category = feed.category
            session.add(existing)
            updated += 1
        else:
            ev = CalendarEvent(feed_id=feed.id, **item)
            session.add(ev)
            inserted += 1

    feed.last_synced_at = utc_now()
    feed.event_count = inserted + updated
    feed.last_status = f"Synced {feed.event_count} event(s)"
    session.add(feed)
    session.commit()

    return {
        "feed_id": feed.id,
        "name": feed.name,
        "inserted": inserted,
        "updated": updated,
        "total": inserted + updated,
    }


def export_calendar_to_ics(events: List[CalendarEvent], tasks: List[Task]) -> str:
    """Export Hub calendar events and task deadlines as a subscribable RFC-5545 `.ics` feed."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Studio Hub WebOS//Student Calendar & AI Secretary//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Studio Hub Schedule",
    ]

    for ev in events:
        uid = ev.external_uid or f"hub-event-{ev.id}@studiohub.local"
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{uid}")
        out.append(f"DTSTAMP:{stamp}")
        if ev.all_day:
            d_str = ev.start_at[:10].replace("-", "")
            d_end = (datetime.strptime(ev.end_at[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
            out.append(f"DTSTART;VALUE=DATE:{d_str}")
            out.append(f"DTEND;VALUE=DATE:{d_end}")
        else:
            dt_s = parse_local_dt(ev.start_at).strftime("%Y%m%dT%H%M00")
            dt_e = parse_local_dt(ev.end_at).strftime("%Y%m%dT%H%M00")
            out.append(f"DTSTART:{dt_s}")
            out.append(f"DTEND:{dt_e}")
        out.append(f"SUMMARY:{_escape_ics_text(ev.title)}")
        if ev.location:
            out.append(f"LOCATION:{_escape_ics_text(ev.location)}")
        if ev.notes:
            out.append(f"DESCRIPTION:{_escape_ics_text(ev.notes)}")
        out.append(f"CATEGORIES:{_escape_ics_text(ev.category)}")
        out.append("END:VEVENT")

    for t in tasks:
        if t.completed or not t.due_date:
            continue
        try:
            d_clean = t.due_date[:10].replace("-", "")
            d_next = (datetime.strptime(t.due_date[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        except ValueError:
            continue
        out.extend(
            [
                "BEGIN:VEVENT",
                f"UID:hub-task-{t.id}@studiohub.local",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{d_clean}",
                f"DTEND;VALUE=DATE:{d_next}",
                f"SUMMARY:{_escape_ics_text('[Due] ' + t.title)}",
                f"CATEGORIES:{_escape_ics_text(t.category)}",
                "END:VEVENT",
            ]
        )

    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def find_free_windows_on_date(
    events_on_day: List[CalendarEvent],
    target_date_str: str,
    day_start_hour: int = 8,
    day_end_hour: int = 21,
    buffer_minutes: int = 5,
    min_window_minutes: int = 8,
) -> List[Tuple[datetime, datetime]]:
    """Compute open conflict-free `[start, end]` intervals on `target_date_str`."""
    base_date = datetime.strptime(target_date_str[:10], "%Y-%m-%d").date()
    win_start = datetime.combine(base_date, dtime(hour=day_start_hour, minute=0))
    win_end = datetime.combine(base_date, dtime(hour=day_end_hour, minute=30))

    now_local = datetime.now()
    if base_date == now_local.date() and now_local > win_start:
        rounded_min = ((now_local.minute // 5) + 1) * 5
        bump_hr = now_local.hour + (1 if rounded_min >= 60 else 0)
        rounded_min = rounded_min % 60
        if bump_hr < 23:
            win_start = max(win_start, datetime.combine(base_date, dtime(hour=bump_hr, minute=rounded_min)))

    if win_start >= win_end:
        return []

    busy: List[Tuple[datetime, datetime]] = []
    for ev in events_on_day:
        if ev.all_day:
            continue
        try:
            s = parse_local_dt(ev.start_at) - timedelta(minutes=buffer_minutes)
            e = parse_local_dt(ev.end_at) + timedelta(minutes=buffer_minutes)
            busy.append((max(win_start, s), min(win_end, e)))
        except ValueError:
            continue

    busy.sort(key=lambda pair: pair[0])
    merged: List[Tuple[datetime, datetime]] = []
    for s, e in busy:
        if e <= s:
            continue
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))

    free: List[Tuple[datetime, datetime]] = []
    cursor = win_start
    min_delta = timedelta(minutes=min_window_minutes)
    for s, e in merged:
        if s > cursor and (s - cursor) >= min_delta:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if win_end > cursor and (win_end - cursor) >= min_delta:
        free.append((cursor, win_end))

    return free


def recalibrate_scheduled_blocks(session: Session) -> List[Dict[str, Any]]:
    """
    Re-evaluate existing AI-scheduled task blocks on the calendar against current
    learned rules in `time_estimates.md` (e.g. shrinking an old 40m Teams reply block to 5m).
    """
    ai_blocks = list(
        session.exec(
            select(CalendarEvent)
            .where(CalendarEvent.completed == False)  # noqa: E712
            .where(CalendarEvent.linked_task_id != None)  # noqa: E711
        ).all()
    )
    adjusted: List[Dict[str, Any]] = []
    for ev in ai_blocks:
        if ev.source not in ("AI Secretary", "Task Time-Block"):
            continue
        linked_task = session.get(Task, ev.linked_task_id) if ev.linked_task_id else None
        raw_title = linked_task.title if linked_task else re.sub(r"^(?:Focus|Work Block|Quick Action):\s*", "", ev.title)
        cat = linked_task.category if linked_task else ev.category
        src_app = linked_task.source_app if linked_task else ""

        est_mins, reason = memory_store.estimate_task_minutes(raw_title, category=cat, source_app=src_app)
        try:
            s_dt = parse_local_dt(ev.start_at)
            e_dt = parse_local_dt(ev.end_at)
        except ValueError:
            continue

        old_mins = int((e_dt - s_dt).total_seconds() // 60)
        if old_mins != est_mins and abs(old_mins - est_mins) >= 3:
            new_end = s_dt + timedelta(minutes=est_mins)
            ev.end_at = format_local_dt(new_end)
            if est_mins <= 15:
                ev.title = f"Quick: {raw_title}"[:290]
                ev.category = "Quick Comms"
                ev.color = "teal"
            ev.notes = f"Estimated {est_mins}m via time_estimates.md ({reason})"
            session.add(ev)
            adjusted.append(
                {
                    "event_id": ev.id,
                    "title": ev.title,
                    "old_minutes": old_mins,
                    "new_minutes": est_mins,
                    "start_at": ev.start_at,
                    "end_at": ev.end_at,
                }
            )
    if adjusted:
        session.commit()
    return adjusted


def coordinate_day_schedule(
    session: Session,
    target_date_str: Optional[str] = None,
    target_date: Optional[str] = None,
    day_start_hour: int = 8,
    day_end_hour: int = 21,
    include_tasks: bool = True,
    include_goals: bool = True,
    max_blocks: int = 5,
) -> Dict[str, Any]:
    """
    AI Secretary Smart Time-Blocker:
    Reads learned task durations from `time_estimates.md` (e.g. 5m for Teams replies,
    10m for emails, 45m for FRQs) and slots conflict-free blocks around classes & clubs!
    """
    # First recalibrate any stale over-scheduled blocks
    recalibrate_scheduled_blocks(session)

    day_iso = (target_date or target_date_str or date.today().isoformat())[:10]

    day_events = list(
        session.exec(
            select(CalendarEvent)
            .where(CalendarEvent.start_at >= f"{day_iso}T00:00")
            .where(CalendarEvent.start_at <= f"{day_iso}T23:59")
            .order_by(CalendarEvent.start_at.asc())
        ).all()
    )

    already_scheduled_task_ids = {ev.linked_task_id for ev in day_events if ev.linked_task_id}
    already_scheduled_goal_ids = {ev.linked_goal_id for ev in day_events if ev.linked_goal_id}

    unscheduled_tasks: List[Task] = []
    if include_tasks:
        active_tasks = list(
            session.exec(
                select(Task).where(Task.completed == False).order_by(Task.due_date.asc(), Task.id.asc())  # noqa: E712
            ).all()
        )
        unscheduled_tasks = [t for t in active_tasks if t.id not in already_scheduled_task_ids]
        unscheduled_tasks.sort(
            key=lambda t: (
                0 if (t.due_date and t.due_date <= day_iso) else (1 if t.due_date else 2),
                t.due_date or "9999-12-31",
            )
        )

    active_goals: List[LongTermGoal] = []
    if include_goals:
        active_goals = [
            g
            for g in session.exec(select(LongTermGoal).where(LongTermGoal.active == True)).all()  # noqa: E712
            if g.id not in already_scheduled_goal_ids and g.last_checkin_date != day_iso
        ]

    candidates: List[Dict[str, Any]] = []

    def _build_task_candidate(t: Task, urgent: bool) -> Dict[str, Any]:
        est_mins, reason = memory_store.estimate_task_minutes(
            title=t.title,
            category=t.category,
            source_app=t.source_app,
        )
        is_quick = est_mins <= 15
        prefix = "Quick" if is_quick else ("Focus" if urgent else "Work Block")
        return {
            "kind": "task",
            "title": f"{prefix}: {t.title}",
            "duration_min": est_mins,
            "category": "Quick Comms" if is_quick else "Study Block",
            "color": "teal" if is_quick else "amber",
            "notes": f"Estimated {est_mins}m via time_estimates.md ({reason}) • Due {t.due_date or 'upcoming'}",
            "linked_task_id": t.id,
            "linked_goal_id": None,
        }

    # 1. Urgent / due-today tasks first
    for t in unscheduled_tasks:
        if t.due_date and t.due_date <= day_iso:
            candidates.append(_build_task_candidate(t, urgent=True))

    # 2. Daily micro-step for Long-Term Goals
    for g in active_goals[:2]:
        habit_label = g.daily_habit or g.title
        candidates.append(
            {
                "kind": "goal",
                "title": f"Goal Step: {habit_label}",
                "duration_min": max(10, min(g.minutes_per_day or 25, 60)),
                "category": "Goal Block",
                "color": "emerald",
                "notes": f"Daily progress toward '{g.title}'",
                "linked_task_id": None,
                "linked_goal_id": g.id,
            }
        )

    # 3. Upcoming tasks (including quick 5m Teams replies or 10m emails!)
    for t in unscheduled_tasks:
        if not (t.due_date and t.due_date <= day_iso):
            candidates.append(_build_task_candidate(t, urgent=False))

    created_events: List[CalendarEvent] = []
    working_events = list(day_events)

    for item in candidates:
        if len(created_events) >= max_blocks:
            break
        free_windows = find_free_windows_on_date(
            events_on_day=working_events,
            target_date_str=day_iso,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        )
        needed = timedelta(minutes=item["duration_min"])
        chosen_start: Optional[datetime] = None
        for w_start, w_end in free_windows:
            if (w_end - w_start) >= needed:
                chosen_start = w_start
                break
        if chosen_start is None:
            continue

        chosen_end = chosen_start + needed
        ev = CalendarEvent(
            title=item["title"][:290],
            start_at=format_local_dt(chosen_start),
            end_at=format_local_dt(chosen_end),
            all_day=False,
            category=item["category"],
            color=item["color"],
            notes=item["notes"],
            source="AI Secretary",
            linked_task_id=item["linked_task_id"],
            linked_goal_id=item["linked_goal_id"],
        )
        session.add(ev)
        working_events.append(ev)
        created_events.append(ev)

    session.commit()
    for ev in created_events:
        session.refresh(ev)

    remaining_free = find_free_windows_on_date(
        events_on_day=working_events,
        target_date_str=day_iso,
        day_start_hour=day_start_hour,
        day_end_hour=day_end_hour,
    )

    free_list = [
        {
            "start_at": format_local_dt(s),
            "end_at": format_local_dt(e),
            "minutes": int((e - s).total_seconds() // 60),
        }
        for s, e in remaining_free
    ]
    summary = (
        f"Scheduled {len(created_events)} focus/goal block(s) around your commitments on {day_iso}."
        if created_events
        else f"All open tasks/goals are already scheduled or no free slots remained on {day_iso}."
    )
    return {
        "date": day_iso,
        "target_date": day_iso,
        "scheduled_count": len(created_events),
        "created_blocks": created_events,
        "scheduled_events": [ev.model_dump() for ev in created_events],
        "free_windows": free_list,
        "free_windows_remaining": free_list,
        "summary": summary,
    }


def build_smart_goal_breakdown(
    title: str,
    category: str = "Academics",
    minutes_per_day: int = 25,
    target_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate actionable milestones and a daily micro-habit tailored for a busy student."""
    lower = title.lower()
    if any(k in lower for k in ("ap ", "exam", "sat", "act", "calc", "physics", "chem", "bio", "score", "test")):
        daily_habit = f"{minutes_per_day}m active recall + 2 timed practice problems"
        coaching_tip = "Anchor this short daily drill at a consistent time—steady daily reps beat weekend cramming."
        milestones = [
            {"id": "m1", "title": "Audit weak units & gather past FRQ/practice sets", "done": False},
            {"id": "m2", "title": "Complete untimed mastery drills on core topics", "done": False},
            {"id": "m3", "title": "Take 2 full timed practice sections & log error patterns", "done": False},
            {"id": "m4", "title": "Final rapid-review formula/concept sheet & full mock exam", "done": False},
        ]
    elif any(k in lower for k in ("essay", "college", "application", "common app", "scholarship", "internship", "resume")):
        daily_habit = f"{minutes_per_day}m focused drafting or revising one paragraph/entry"
        coaching_tip = "Treat each daily block as ugly-draft permission—getting words on the page daily removes perfectionism paralysis."
        milestones = [
            {"id": "m1", "title": "Brainstorm core stories, requirements & deadline tracker", "done": False},
            {"id": "m2", "title": "Write rough zero-draft without self-editing", "done": False},
            {"id": "m3", "title": "Tighten structure & get feedback from mentor/teacher", "done": False},
            {"id": "m4", "title": "Final polish, proofread, and submit 48h before deadline", "done": False},
        ]
    elif any(k in lower for k in ("robot", "code", "app", "project", "server", "build", "hack", "github", "webos")):
        daily_habit = f"{minutes_per_day}m shipping one small testable component or fix"
        coaching_tip = "End every daily build block by writing one sentence on the exact next step so tomorrow's startup takes zero friction."
        milestones = [
            {"id": "m1", "title": "Define MVP scope & core architecture checklist", "done": False},
            {"id": "m2", "title": "Build & verify core prototype end-to-end", "done": False},
            {"id": "m3", "title": "Test under real conditions & fix edge-case bugs", "done": False},
            {"id": "m4", "title": "Document setup & ship clean release", "done": False},
        ]
    else:
        daily_habit = f"Dedicated {minutes_per_day}m daily block advancing '{title}'"
        coaching_tip = f"Protect just {minutes_per_day} focused minutes a day—even on packed club days, keeping the streak alive compounds fast."
        milestones = [
            {"id": "m1", "title": "Break goal into weekly targets & gather materials", "done": False},
            {"id": "m2", "title": "Complete first 25% foundation milestone", "done": False},
            {"id": "m3", "title": "Hit 75% midpoint review & adjust pace", "done": False},
            {"id": "m4", "title": "Finish final stretch & celebrate completion", "done": False},
        ]

    return {
        "title": title,
        "category": category,
        "target_date": target_date,
        "minutes_per_day": minutes_per_day,
        "daily_habit": daily_habit,
        "coaching_tip": coaching_tip,
        "milestones": milestones,
    }
