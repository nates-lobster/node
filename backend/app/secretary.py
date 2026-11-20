import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .apps_manager import apps_manager
from .calendar_engine import (
    build_smart_goal_breakdown,
    coordinate_day_schedule,
    find_free_windows_on_date,
    recalibrate_scheduled_blocks,
)
from .config import settings
from .diagnostics import probe_scraper_environment, probe_tailscale
from .grades_service import grades_service
from .importance_filter import importance_service
from .memory_store import memory_store
from .model_router import _smart_heuristic_duration, model_router
from .models import (
    CalendarEvent,
    ChatMessage,
    ChatThread,
    CourseGrade,
    LongTermGoal,
    Notification,
    ScraperHeartbeat,
    Task,
    normalize_priority_value,
)
from .ollama_client import _extract_user_name_from_memory, compute_briefing_workload, ollama_client
from .os_settings import os_settings_service
from .task_intelligence import sync_tasks_from_calendar_and_inbox
from .agent_logbook import agent_logbook
from .ai_providers import ai_provider_registry
from .web_search_tool import (
    WEEKDAY_ALIASES,
    build_14_day_calendar_table,
    fetch_webpage_text,
    resolve_spoken_weekday_date,
    search_web,
    verify_and_fix_weekday_date,
)

WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def get_local_now() -> datetime:
    tz_name = os.environ.get("TZ") or settings.local_timezone or "America/New_York"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz)


def get_local_time_reference(days_ahead: int = 21) -> Dict[str, Any]:
    now = get_local_now()
    today = now.date()
    upcoming: List[Dict[str, Any]] = []
    for offset in range(max(7, min(35, days_ahead))):
        d = today + timedelta(days=offset)
        wname = d.strftime("%A")
        if offset == 0:
            label = f"Today ({wname})"
        elif offset == 1:
            label = f"Tomorrow ({wname})"
        elif offset < 7:
            label = f"This {wname}"
        else:
            label = f"Next {wname} (+{offset}d)"
        upcoming.append(
            {
                "offset_days": offset,
                "date": d.isoformat(),
                "weekday": wname,
                "short": d.strftime("%a %b %d"),
                "label": label,
            }
        )
    return {
        "timezone": str(now.tzinfo),
        "now_iso": now.isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "current_time_24h": now.strftime("%H:%M"),
        "current_time_12h": now.strftime("%I:%M %p").lstrip("0"),
        "upcoming_days": upcoming,
    }


def _parse_natural_date(text: str) -> date:
    now_dt = get_local_now()
    today = now_dt.date()
    lower = text.lower()
    if "day after tomorrow" in lower:
        return today + timedelta(days=2)
    if "tomorrow" in lower:
        return today + timedelta(days=1)
    if "yesterday" in lower:
        return today - timedelta(days=1)
    iso_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso_m:
        try:
            return date.fromisoformat(iso_m.group(1))
        except ValueError:
            pass
    # "oct 3", "October 3rd", "3 Oct" — roll to next year once the date has passed.
    _MONTHS = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    for pattern in (
        rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\b",
    ):
        m_month = re.search(pattern, lower)
        if not m_month:
            continue
        g1, g2 = m_month.group(1), m_month.group(2)
        if g1 in _MONTHS:
            mon, day = _MONTHS[g1], int(g2)
        else:
            mon, day = _MONTHS[g2], int(g1)
        try:
            candidate = date(today.year, mon, day)
        except ValueError:
            candidate = None
        if candidate is not None:
            if candidate < today:
                try:
                    candidate = date(today.year + 1, mon, day)
                except ValueError:
                    pass
            return candidate

    spoken = resolve_spoken_weekday_date(text, now_dt)
    if spoken is not None:
        return spoken[0]
    for name, idx in WEEKDAY_ALIASES.items():
        if re.search(rf"\b{name}\b", lower):
            days_ahead = (idx - today.weekday()) % 7
            if "next week" in lower and days_ahead < 7:
                days_ahead += 7
            elif re.search(rf"\bnext\s+{name}\b", lower) and days_ahead == 0:
                days_ahead = 7
            return today + timedelta(days=days_ahead)
    return today


def _parse_hhmm(val: Optional[str]) -> Optional[Tuple[int, int]]:
    if not val:
        return None
    s = str(val).strip()
    m12 = re.search(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", s, re.IGNORECASE)
    if m12:
        hr = int(m12.group(1))
        mn = int(m12.group(2) or 0)
        ap = m12.group(3).lower()
        if ap == "pm" and hr < 12:
            hr += 12
        elif ap == "am" and hr == 12:
            hr = 0
        return max(0, min(23, hr)), max(0, min(59, mn))
    m24 = re.search(r"(\d{1,2}):(\d{2})", s)
    if m24:
        return max(0, min(23, int(m24.group(1)))), max(0, min(59, int(m24.group(2))))
    return None


def _parse_natural_time_and_duration(
    text: str, fallback_duration: int = 45
) -> Tuple[Optional[Tuple[int, int]], Optional[int]]:
    duration: Optional[int] = None
    dur_m = re.search(r"\b(?:for|to)\s+(\d{1,3})\s*(?:min|mins|minutes|m)\b", text, re.IGNORECASE)
    hr_dur = re.search(r"\b(?:for|to)\s+(\d(?:\.\d)?)\s*(?:hr|hrs|hour|hours)\b", text, re.IGNORECASE)
    if dur_m:
        duration = max(5, min(360, int(dur_m.group(1))))
    elif hr_dur:
        duration = max(10, min(360, int(float(hr_dur.group(1)) * 60)))

    # Range like "from 3pm to 4:30pm" or "3pm-4pm"
    range_m = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|until|-|–)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        text,
        re.IGNORECASE,
    )
    if range_m:
        h1, m1, ap1, h2, m2, ap2 = (
            int(range_m.group(1)),
            int(range_m.group(2) or 0),
            (range_m.group(3) or range_m.group(6)).lower(),
            int(range_m.group(4)),
            int(range_m.group(5) or 0),
            range_m.group(6).lower(),
        )
        if ap1 == "pm" and h1 < 12:
            h1 += 12
        elif ap1 == "am" and h1 == 12:
            h1 = 0
        if ap2 == "pm" and h2 < 12:
            h2 += 12
        elif ap2 == "am" and h2 == 12:
            h2 = 0
        diff = (h2 * 60 + m2) - (h1 * 60 + m1)
        if diff > 0:
            return (h1, m1), diff

    to_tm = re.search(r"\b(?:to|at|from)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    tm = to_tm or re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    if tm:
        hr = int(tm.group(1))
        mn = int(tm.group(2) or 0)
        ampm = tm.group(3).lower()
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        return (max(0, min(23, hr)), max(0, min(59, mn))), (duration or fallback_duration)

    tm24 = re.search(r"\b(?:to|at|from)\s+([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if tm24:
        return (int(tm24.group(1)), int(tm24.group(2))), (duration or fallback_duration)

    return None, duration


# Conversational lead-ins that must never end up inside an event title.
_TITLE_LEADIN_RE = re.compile(
    r"^(?:please\s+|kindly\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+)?"
    r"(?:mark\s+(?:that\s+|down\s+that\s+)?|note\s+(?:that\s+)?|remember\s+(?:that\s+)?|"
    r"add\s+(?:that\s+)?|put\s+(?:that\s+)?|schedule\s+|create\s+|make\s+|set\s+up\s+|block\s+(?:off\s+)?|book\s+)"
    r"(?:an?\s+|the\s+|my\s+)?"
    r"(?:calendar\s+event\s*(?:for|called|named|that)?\s*|event\s*(?:for|called|named|that)?\s*)?",
    re.IGNORECASE,
)
_TITLE_PRONOUN_RE = re.compile(
    r"^(?:i\s+(?:have|had|got|need|want|will\s+have|am\s+going\s+to\s+have|'ve\s+got|have\s+got)\s+|"
    r"i'm\s+(?:having|going\s+to|at)\s+|i\s+am\s+(?:having|going\s+to|at)\s+|"
    r"there'?s\s+|there\s+is\s+|we\s+have\s+|we\s+got\s+|my\s+)",
    re.IGNORECASE,
)
_TITLE_TRAIL_DAY_RE = re.compile(
    r"\s+(?:on|this|next|coming|for)\s+(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b.*$",
    re.IGNORECASE,
)
_TITLE_TRAIL_DATE_RE = re.compile(
    r"\s+(?:is\s+)?(?:on\s+)?(?:today|tonight|tomorrow|day\s+after\s+tomorrow|the\s+\d{1,2}(?:st|nd|rd|th)?|"
    r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)"
    r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?\b.*$",
    re.IGNORECASE,
)


def _clean_event_title(raw: str) -> str:
    txt = _TITLE_LEADIN_RE.sub("", (raw or "").strip())
    txt = _TITLE_PRONOUN_RE.sub("", txt.strip())
    txt = re.sub(r"\s+(?:on\s+my\s+calendar|to\s+my\s+calendar|in\s+my\s+calendar|to\s+calendar|on\s+calendar)\b.*$", "", txt, flags=re.IGNORECASE)
    txt = re.sub(
        r"\s+(?:today|tomorrow|day\s+after\s+tomorrow|on\s+\w+day|this\s+\w+day|next\s+\w+day|on\s+\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2})\b.*$",
        "",
        txt,
        flags=re.IGNORECASE,
    )
    txt = _TITLE_TRAIL_DAY_RE.sub("", txt)
    txt = _TITLE_TRAIL_DATE_RE.sub("", txt)
    # "study time friday" -> "study time" (bare trailing weekday, no preposition)
    txt = re.sub(
        r"\s+(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?$",
        "",
        txt,
        flags=re.IGNORECASE,
    )
    # "band practice friday 4pm" -> "band practice" (bare weekday followed by a time)
    txt = re.sub(
        r"\s+(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?"
        r"\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b.*$",
        "",
        txt,
        flags=re.IGNORECASE,
    )
    # Trailing bare clock time: "band practice 4pm", "standup 9:30"
    txt = re.sub(r"\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b.*$", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+\d{1,2}:\d{2}\b.*$", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+(?:at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?.*$", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+for\s+\d{1,3}\s*(?:min|mins|minutes|m|hr|hrs|hour|hours)\b.*$", "", txt, flags=re.IGNORECASE)
    txt = txt.strip(" .,:;\"'-")
    return txt[:120] if txt else "Scheduled Event"


def _detect_duration_learning_feedback(msg: str) -> Optional[Dict[str, Any]]:
    lower = msg.lower()
    if re.match(
        r"^(?:please\s+)?(?:can\s+you\s+)?(?:schedule|book|put|add|create|move|reschedule|delete|remove|cancel|rename)\b",
        lower,
    ):
        return None

    has_learning_cue = any(
        cue in lower
        for cue in (
            "only need",
            "only takes",
            "takes me",
            "take me",
            "should take",
            "should only be",
            "should be ",
            "too long",
            "scheduled 40",
            "scheduled 45",
            "instead of 40",
            "instead of 45",
            "for teams",
            "for chat",
            "for email",
        )
    )
    if not has_learning_cue:
        return None

    preferred_m = re.search(
        r"(?:only\s+need|only\s+takes?|takes?\s+me|should\s+(?:only\s+)?(?:take|be)|make\s+it|adjust\s+(?:it\s+)?to|need)\s+(\d{1,3})\s*(?:min|mins|minutes|m)\b",
        lower,
    )
    if not preferred_m:
        all_mins = re.findall(r"(\d{1,3})\s*(?:min|mins|minutes|m)\b", lower)
        if not all_mins:
            return None
        mins_val = int(all_mins[-1])
    else:
        mins_val = int(preferred_m.group(1))

    archetype: Optional[str] = None
    custom_kw: Optional[str] = None
    if any(w in lower for w in ("teams", "discord", "slack", "groupme", "chat", "dm", "thread", "message")):
        archetype = "chat_reply"
    elif any(w in lower for w in ("email", "e-mail", "outlook", "proton", "gmail", "inbox")):
        archetype = "email_triage"
    elif any(w in lower for w in ("upload", "submit", "confirm", "form", "admin")):
        archetype = "quick_admin"
    elif any(w in lower for w in ("essay", "project", "build", "lab report", "code")):
        archetype = "deep_project"
    elif any(w in lower for w in ("frq", "problem set", "homework", "worksheet", "calc", "physics")):
        archetype = "coursework_frq"
    else:
        kw_m = re.search(r"^([a-z0-9 #_-]{3,35}?)\s+(?:only\s+)?(?:takes?|needs?|should)", lower)
        if kw_m:
            custom_kw = kw_m.group(1).strip()

    secondary_email_mins: Optional[int] = None
    em_m = re.search(r"email[s]?[^0-9]{0,20}(\d{1,2})\s*(?:min|mins|minutes|m)?", lower)
    if em_m and archetype == "chat_reply":
        secondary_email_mins = int(em_m.group(1))

    return {
        "minutes": mins_val,
        "archetype": archetype,
        "custom_keyword": custom_kw,
        "secondary_email_minutes": secondary_email_mins,
    }


# ===========================================================================
# Full-OS Tool Suite (`SECRETARY_TOOLS`) — Every Action & Query Across Node
# ===========================================================================
SECRETARY_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_os_overview",
            "description": "Get live OS status: local time/date, remaining free time today, unread notifications, open tasks, grades GPA, monitored apps, and AI model status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "List calendar events in a date range or matching a search query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD start date"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD end date"},
                    "query": {"type": "string", "description": "Optional title substring filter"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create and save a new event on the user's Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "date": {"type": "string", "description": "YYYY-MM-DD date"},
                    "start_time": {"type": "string", "description": "HH:MM (24h) or e.g. 3:30pm"},
                    "end_time": {"type": "string", "description": "Optional HH:MM end time"},
                    "duration_minutes": {"type": "integer", "description": "Duration in minutes if end_time omitted"},
                    "category": {"type": "string", "description": "Class, Study Block, Extracurricular, Quick Comms, or Personal"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["title", "date", "start_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_calendar_event",
            "description": "Edit, move, reschedule, resize, complete, or rename an existing Calendar event by event_id or match_title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "match_title": {"type": "string"},
                    "new_title": {"type": "string"},
                    "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "new_start_time": {"type": "string", "description": "HH:MM or 5:00pm"},
                    "new_end_time": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                    "category": {"type": "string"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                    "completed": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Delete/cancel an event from the Calendar by event_id or match_title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "match_title": {"type": "string"},
                    "date": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "coordinate_day",
            "description": "Auto-schedule pending tasks and daily goal habits into open free windows on a date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List tasks sorted by priority and manual order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_completed": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new Task with priority, due date, category, and estimated duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "description": "critical, high, normal, or low"},
                    "estimated_minutes": {"type": "integer"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "category": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Complete/reopen, rename, reprioritize, resize duration, change due_date, or reorder a Task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "match_title": {"type": "string"},
                    "completed": {"type": "boolean"},
                    "new_title": {"type": "string"},
                    "priority": {"type": "string", "description": "critical, high, normal, or low"},
                    "estimated_minutes": {"type": "integer"},
                    "due_date": {"type": "string"},
                    "sort_order": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Delete a Task by task_id or match_title, or clear all completed tasks when clear_completed=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "match_title": {"type": "string"},
                    "clear_completed": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_tasks_from_sources",
            "description": "Scan Calendar (including Schoology .ics feeds) and unread Inbox notifications to extract & add missing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_calendar": {"type": "boolean"},
                    "include_inbox": {"type": "boolean"},
                    "days_ahead": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notifications",
            "description": "List Inbox notifications filtered by unread_only, important_only, source_app, or category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unread_only": {"type": "boolean"},
                    "important_only": {"type": "boolean"},
                    "source_app": {"type": "string"},
                    "category": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_notifications",
            "description": "Mark notifications read/unread, mark important/not_important, or delete notifications (by notification_id, source_app, promo_only, or all_unread).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "mark_read, mark_unread, mark_important, mark_not_important, or delete"},
                    "notification_id": {"type": "integer"},
                    "source_app": {"type": "string"},
                    "promo_only": {"type": "boolean"},
                    "all_unread": {"type": "boolean"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_monitored_apps",
            "description": "List all monitored web apps, cookie profiles, and unread counts.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_monitored_app",
            "description": "Add and save a new monitored web app (e.g. ProtonMail, GitHub, Discord, Schoology, any URL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "account_profile": {"type": "string", "description": "default or custom isolated cookie profile"},
                    "zero_knowledge": {"type": "boolean"},
                },
                "required": ["name", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_monitored_app",
            "description": "Remove a monitored web app by slug or name.",
            "parameters": {
                "type": "object",
                "properties": {"slug_or_name": {"type": "string"}},
                "required": ["slug_or_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "duplicate_app_account",
            "description": "Clone an existing monitored app onto a separate isolated cookie profile for a 2nd account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug_or_name": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["slug_or_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_grades",
            "description": "Get all tracked PowerSchool course grades, trends/history, individual assignment grades, overall average/GPA, and recent grade changes. Set refresh_live=true to scan live PowerSchool first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "course_query": {"type": "string", "description": "Optional course name filter to inspect full assignment list"},
                    "refresh_live": {"type": "boolean", "description": "Trigger a live PowerSchool scan before returning grades"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_goals",
            "description": "List, create, check_in, or delete long-term goals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "list, create, check_in, or delete"},
                    "title": {"type": "string"},
                    "goal_id": {"type": "integer"},
                    "minutes_per_day": {"type": "integer"},
                    "category": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_notebook_note",
            "description": "Append or save markdown notes into `/data/notebook/<file_slug>.md` or read a notebook when action='read'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "append (default), create, read, or list"},
                    "file_slug": {"type": "string", "description": "e.g. classes, scratchpad, projects"},
                    "text": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_ai_memory",
            "description": "Save a standing personal rule or preference to `ai_memory.md`.",
            "parameters": {
                "type": "object",
                "properties": {"rule": {"type": "string"}},
                "required": ["rule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_task_duration",
            "description": "Update learned duration minutes in `time_estimates.md` and recalibrate AI calendar blocks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "archetype": {"type": "string", "description": "chat_reply, email_triage, quick_admin, coursework_frq, or deep_project"},
                    "minutes": {"type": "integer"},
                    "keyword": {"type": "string"},
                },
                "required": ["minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_os_window",
            "description": "Open and focus an OS window on the user's screen (inbox, calendar, tasks, grades, goals, apps, files, memory, focus_guard, extensions, settings).",
            "parameters": {
                "type": "object",
                "properties": {
                    "view": {"type": "string", "description": "inbox, calendar, tasks, grades, goals, apps, files, memory, focus_guard, extensions, or settings"},
                },
                "required": ["view"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_os_setting",
            "description": "Update any OS setting (e.g. theme='dark'|'light', accent_color='#38bdf8', canvas_bg='#13161c', wm_mode='tiling'|'floating', dock_auto_hide=true, cron_interval_minutes=30, bedtime_hhmm='22:30').",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"description": "New value for the setting (boolean, string, or number)"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web for factual information, school schedules, documentation, definitions, current events, or URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "max_results": {"type": "integer", "description": "Number of results (1-8, default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "Fetch and read clean text content from a specific http/https URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_shared_logs",
            "description": "Read entries from the Shared Multi-Agent Workspace Logbook written by you or peer AI models/daemons.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_shared_log",
            "description": "Create or update a Shared Multi-Agent Workspace Log entry to record insights, shortcuts, schedules, or handoff notes for yourself and other AI models.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {"type": "string", "description": "e.g. workflow, schedule-pattern, lms-context, handoff, research"},
                    "tags": {"type": "string"},
                    "pinned": {"type": "boolean"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_shared_log",
            "description": "Edit, refine, consolidate, or pin/unpin an existing Shared Multi-Agent Workspace Log entry by log_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "append_content": {"type": "string", "description": "Optional note to append without overwriting existing content"},
                    "category": {"type": "string"},
                    "tags": {"type": "string"},
                    "pinned": {"type": "boolean"},
                },
                "required": ["log_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_shared_log",
            "description": "Delete an obsolete Shared Multi-Agent Workspace Log entry by log_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "integer"},
                },
                "required": ["log_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_catalog_app",
            "description": "Install an app from Node's built-in App Library (e.g. gmail, protonmail, ms-teams, outlook, powerschool, schoology, canvas, google-classroom, groupme, slack, discord).",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "custom_url": {"type": "string"},
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_inbox_categories",
            "description": "List, add, or remove custom Inbox sorting categories (globally or for a specific app_slug), or trigger AI auto-sorting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "remove"]},
                    "category": {"type": "string", "description": "Category name to add or remove (e.g. 'Professors', 'Clubs', 'Receipts')"},
                    "app_slug": {"type": "string", "description": "Optional app slug/name for per-app categories; omit for global Inbox categories"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_explorer",
            "description": "Interact with the Shared Server Drive (Files app): list folders/files, read a file, write/create a file, create a folder, move/rename, or delete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "write", "mkdir", "move", "delete"]},
                    "path": {"type": "string", "description": "Relative path inside Drive (e.g. 'AI-Outputs/study_plan.md')"},
                    "dest_path": {"type": "string", "description": "Destination path when action='move'"},
                    "content": {"type": "string", "description": "Text/markdown/code content when action='write'"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_inbox_filter_rule",
            "description": (
                "Gmail-style Inbox automation: create saved filters, tags, and tag moves. "
                "Accepts natural requests like 'make a filter for band emails' or "
                "'always tag mail from prof@school.edu as School'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "save_filter", "delete_filter", "add_tag", "rename_tag", "move_message", "query", "dedupe"]},
                    "name": {"type": "string", "description": "Filter or tag name"},
                    "query": {"type": "string", "description": "Gmail-style query, e.g. 'from:prof@school.edu is:unread'"},
                    "tag": {"type": "string"},
                    "new_name": {"type": "string"},
                    "notification_id": {"type": "integer"},
                    "description": {"type": "string", "description": "What this tag means — this text is the classifier's criteria"},
                    "color": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_grade_tabs",
            "description": "Create/edit/delete Grades tabs (course groups with their own averages), hide or restore a course, or change which courses count toward GPA/average.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create_tab", "update_tab", "delete_tab", "hide_course", "restore_course", "set_weights"]},
                    "tab_id": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "course_keys": {"type": "array", "items": {"type": "string"}},
                    "match_rules": {"type": "array", "items": {"type": "object"}},
                    "course_key": {"type": "string"},
                    "counts_toward_gpa": {"type": "boolean"},
                    "counts_toward_average": {"type": "boolean"},
                    "order": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_api_bridge",
            "description": "Check or drive the api-anything bridge that replaces browser tabs with direct HTTP calls (status, learn an operation, call it, import local session cookies).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "learn", "call", "start", "import_cookies"]},
                    "app_slug": {"type": "string"},
                    "operation": {"type": "string"},
                    "args": {"type": "object"},
                    "port": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
    },
]


def _match_calendar_event(
    session: Session,
    event_id: Optional[int] = None,
    match_title: Optional[str] = None,
    date_filter: Optional[str] = None,
) -> Optional[CalendarEvent]:
    if event_id is not None:
        ev = session.get(CalendarEvent, int(event_id))
        if ev:
            return ev

    q = select(CalendarEvent).order_by(CalendarEvent.start_at.asc())
    all_evs = list(session.exec(q).all())
    if date_filter:
        date_evs = [e for e in all_evs if e.start_at.startswith(date_filter)]
        if date_evs:
            all_evs = date_evs

    if not match_title:
        return None

    needle = match_title.strip().lower()
    for ev in all_evs:
        if needle == ev.title.lower():
            return ev
    for ev in all_evs:
        if needle in ev.title.lower():
            return ev
    tokens = [w for w in re.split(r"\W+", needle) if len(w) >= 3]
    best_ev = None
    best_score = 0
    for ev in all_evs:
        tl = ev.title.lower()
        score = sum(1 for w in tokens if w in tl)
        if score > best_score:
            best_score = score
            best_ev = ev
    return best_ev if best_score > 0 else None


async def execute_secretary_tool_async(
    name: str,
    args: Dict[str, Any],
    session: Session,
    actions_out: List[Dict[str, Any]],
    user_message_context: str = "",
    active_model_id: str = "auto",
) -> Dict[str, Any]:
    """Async wrapper supporting both async OS tools and synchronous DB tools."""
    if name == "web_search":
        q = str(args.get("query") or "").strip()
        routing = ai_provider_registry.load_state().get("routing") or {}
        if not routing.get("enable_web_search", True):
            return {"status": "disabled", "error": "Web search is currently disabled in AI Settings."}
        res = await search_web(
            query=q,
            max_results=int(args.get("max_results") or 5),
            searxng_url=str(routing.get("searxng_url") or ""),
        )
        actions_out.append({"type": "web_search", "label": f"Searched Web: {q[:36]}", "view": "chat"})
        return {"status": "ok", **res}

    if name == "manage_api_bridge":
        from .api_bridge import api_bridge_service

        act = str(args.get("action") or "status").strip().lower()
        if act == "status":
            return {"status": "ok", **(await api_bridge_service.status(force=True))}
        if act == "learn":
            row = api_bridge_service.register_bridge(
                str(args.get("app_slug") or ""), operation=str(args.get("operation") or "")
            )
            actions_out.append({"type": "api_bridge_learned", "label": f"Bridge: {row['app_slug']}", "view": "apps"})
            return {"status": "learning", "bridge": row}
        if act == "call":
            return await api_bridge_service.call(str(args.get("operation") or ""), dict(args.get("args") or {}))
        if act == "start":
            return api_bridge_service.start_server(int(args.get("port") or 8787))
        if act == "import_cookies":
            res = await api_bridge_service.export_session_cookies(str(args.get("app_slug") or ""))
            return {"status": "ok", "cookie_count": res.get("cookie_count", 0), "detail": res}
        return {"status": "error", "error": f"Unknown action '{act}'"}

    if name == "fetch_webpage":
        target_url = str(args.get("url") or "").strip()
        res = await fetch_webpage_text(target_url, max_chars=5000)
        actions_out.append({"type": "web_fetch", "label": f"Read Page: {target_url[:36]}", "view": "chat"})
        return {"status": "ok" if res.get("ok") else "error", **res}

    if name == "install_catalog_app":
        tid = str(args.get("template_id") or "").strip()
        custom_url = str(args.get("custom_url") or "").strip() or None
        installed = apps_manager.install_from_catalog(tid, custom_url=custom_url)
        if not installed:
            return {"status": "not_found", "error": f"Unknown App Library template '{tid}'."}
        actions_out.append({"type": "app_added", "label": f"Installed App: {installed['name']}", "view": "apps"})
        return {"status": "installed", "app": installed}

    if name == "sync_tasks_from_sources":
        res = await sync_tasks_from_calendar_and_inbox(
            session=session,
            include_calendar=bool(args.get("include_calendar", True)),
            include_inbox=bool(args.get("include_inbox", True)),
            days_ahead=int(args.get("days_ahead") or 14),
            today_iso=get_local_now().date().isoformat(),
        )
        actions_out.append(
            {
                "type": "tasks_synced",
                "label": f"Synced {res['created_count']} Task(s)",
                "view": "tasks",
            }
        )
        return {
            "status": "synced",
            "created_count": res["created_count"],
            "from_calendar": res["from_calendar"],
            "from_inbox": res["from_inbox"],
            "skipped_duplicates": res["skipped_duplicates"],
            "tasks": [{"id": t.id, "title": t.title, "due_date": t.due_date, "priority": t.priority} for t in res["tasks"]],
        }

    if name == "add_monitored_app":
        raw_name = str(args.get("name") or "").strip()
        raw_url = str(args.get("url") or "").strip()
        suggestion = await apps_manager.build_ai_suggestion(
            f"{raw_name} {raw_url}".strip(),
            zero_knowledge_preferred=bool(args.get("zero_knowledge", False)),
        )
        cfg = dict(suggestion.get("config") or {})
        if raw_name:
            cfg["name"] = raw_name
        if raw_url:
            cfg["url"] = raw_url
        if args.get("account_profile"):
            cfg["account_profile"] = str(args["account_profile"]).strip()
        saved = apps_manager.add_or_replace_app(cfg)
        actions_out.append(
            {
                "type": "app_added",
                "label": f"Added Monitor: {saved['name']}",
                "view": "apps",
            }
        )
        return {"status": "created", "app": saved}

    if name == "get_os_overview":
        tref = get_local_time_reference(7)
        today_iso = tref["today_date"]
        notifs = list(session.exec(select(Notification).where(Notification.read_state == False)).all())  # noqa: E712
        tasks = list(session.exec(select(Task).where(Task.completed == False)).all())  # noqa: E712
        evs_today = list(
            session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.start_at >= f"{today_iso}T00:00")
                .where(CalendarEvent.start_at <= f"{today_iso}T23:59")
                .order_by(CalendarEvent.start_at.asc())
            ).all()
        )
        os_vals = os_settings_service.full_payload().get("settings") or {}
        wl = compute_briefing_workload(
            notifications=notifs,
            tasks=tasks,
            events_today=evs_today,
            cutoff_hhmm=str(os_vals.get("bedtime_hhmm") or "22:00"),
            wind_down_minutes=int(os_vals.get("sleep_buffer_minutes") or 15),
        )
        grades_dash = grades_service.get_dashboard(session)
        models_st = await model_router.refresh_status()
        _, apps = apps_manager.get_global_and_apps()
        return {
            "time": {
                "date": today_iso,
                "weekday": tref["weekday"],
                "time_12h": tref["current_time_12h"],
            },
            "workload": {
                "free_minutes_left_today": wl["free_minutes_left"],
                "important_unread": wl["important_unread_count"],
                "fyi_unread": wl["fyi_unread_count"],
                "inbox_triage_minutes": wl["inbox_triage_minutes"],
                "open_tasks_count": len(tasks),
                "urgent_task_minutes": wl["urgent_task_minutes"],
                "pressure": wl["pressure"],
            },
            "grades_summary": grades_dash.get("summary"),
            "monitored_apps_count": len(apps),
            "models": models_st,
        }

    if name == "get_grades" and bool(args.get("refresh_live")):
        try:
            from .browser_stream import browser_stream_manager

            await browser_stream_manager.scan_grades_tabs(reload_idle_tabs=True, open_if_configured=True)
        except Exception:
            pass

    return execute_secretary_tool(
        name,
        args,
        session,
        actions_out,
        user_message_context=user_message_context,
        active_model_id=active_model_id,
    )


def execute_secretary_tool(
    name: str,
    args: Dict[str, Any],
    session: Session,
    actions_out: List[Dict[str, Any]],
    user_message_context: str = "",
    active_model_id: str = "auto",
) -> Dict[str, Any]:
    """Execute a real database/notebook/OS mutation or query and record UI action badges."""
    now_dt = get_local_now()
    today_iso = now_dt.date().isoformat()
    color_map = {
        "Extracurricular": "violet",
        "Class": "sky",
        "Quick Comms": "teal",
        "Study Block": "amber",
        "Personal": "emerald",
    }

    if name == "read_shared_logs":
        rows = agent_logbook.list_logs(
            session,
            category=args.get("category"),
            query=args.get("query"),
            limit=int(args.get("limit") or 30),
        )
        return {"status": "ok", "count": len(rows), "logs": rows}

    if name == "write_shared_log":
        saved = agent_logbook.write_log(
            session,
            title=str(args.get("title") or "Workspace Note"),
            content=str(args.get("content") or ""),
            agent_name="secretary",
            model_id=active_model_id or "auto",
            category=str(args.get("category") or "shared-note"),
            tags=str(args.get("tags") or ""),
            pinned=bool(args.get("pinned", False)),
        )
        actions_out.append({"type": "agent_log_saved", "label": f"Shared Log #{saved['id']}", "view": "memory"})
        return {"status": "saved", "log": saved}

    if name == "edit_shared_log":
        lid = int(args.get("log_id") or 0)
        updated = agent_logbook.edit_log(
            session,
            lid,
            title=args.get("title"),
            content=args.get("content"),
            append_content=args.get("append_content"),
            category=args.get("category"),
            tags=args.get("tags"),
            pinned=args.get("pinned"),
            agent_name="secretary",
            model_id=active_model_id or "auto",
        )
        if not updated:
            return {"status": "not_found", "error": f"Shared log #{lid} not found."}
        actions_out.append({"type": "agent_log_updated", "label": f"Edited Log #{lid}", "view": "memory"})
        return {"status": "updated", "log": updated}

    if name == "delete_shared_log":
        lid = int(args.get("log_id") or 0)
        ok = agent_logbook.delete_log(session, lid)
        return {"status": "deleted" if ok else "not_found", "log_id": lid}

    if name == "manage_inbox_categories":
        act = str(args.get("action") or "list").strip().lower()
        cat_name = str(args.get("category") or "").strip()[:40]
        app_slug = str(args.get("app_slug") or "").strip()
        from .apps_manager import AppsConfigManager

        mgr = AppsConfigManager()
        if act == "list":
            return {
                "status": "ok",
                "categories": model_router.get_active_inbox_categories(app_slug or None),
            }
        if app_slug:
            _, all_apps = mgr.get_global_and_apps()
            found_app = next(
                (a for a in all_apps if a["slug"] == app_slug.lower() or a["name"].lower() == app_slug.lower()),
                None,
            )
            if not found_app:
                return {"status": "error", "error": f"App '{app_slug}' not found"}
            cur_cats = list(found_app.get("inbox_categories") or [])
            if act == "add" and cat_name and cat_name.lower() not in {c.lower() for c in cur_cats}:
                cur_cats.append(cat_name)
            elif act == "remove" and cat_name:
                cur_cats = [c for c in cur_cats if c.lower() != cat_name.lower()]
            updated_app = mgr.update_app(found_app["slug"], {"inbox_categories": cur_cats})
            actions_out.append({"type": "inbox_categories_updated", "label": f"App Categories: {found_app['name']}", "view": "inbox"})
            return {"status": "updated", "scope": found_app["slug"], "inbox_categories": updated_app.get("inbox_categories", cur_cats)}

        cur_global = model_router.get_active_inbox_categories(None)
        if act == "add" and cat_name and cat_name.lower() not in {c.lower() for c in cur_global}:
            cur_global.append(cat_name)
        elif act == "remove" and cat_name:
            cur_global = [c for c in cur_global if c.lower() != cat_name.lower()]
        os_settings_service.update_state(settings_patch={"inbox_categories_csv": ", ".join(cur_global)})
        actions_out.append({"type": "inbox_categories_updated", "label": f"Inbox Categories ({len(cur_global)})", "view": "inbox"})
        return {"status": "updated", "scope": "global", "categories": cur_global}

    if name == "apply_inbox_filter_rule":
        from .inbox_harvest import backfill_dedupe_keys, merge_duplicate_notifications
        from .inbox_tags import inbox_tag_service

        act = str(args.get("action") or "list").strip().lower()
        if act == "list":
            return {
                "status": "ok",
                "tags": inbox_tag_service.tag_names(),
                "filters": inbox_tag_service.list_filters(),
                "grammar": inbox_tag_service.filter_dsl_for_ai(),
            }
        if act == "save_filter":
            row = inbox_tag_service.save_filter(
                str(args.get("name") or "Filter")[:60],
                str(args.get("query") or "")[:600],
                color=str(args.get("color") or "slate")[:24],
            )
            actions_out.append({"type": "inbox_filter_saved", "label": f"Filter: {row['name']}", "view": "inbox"})
            return {"status": "saved", "filter": row}
        if act == "delete_filter":
            try:
                ok = inbox_tag_service.delete_filter(str(args.get("name") or ""))
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            return {"status": "deleted" if ok else "not_found"}
        if act == "add_tag":
            try:
                row = inbox_tag_service.upsert_tag(
                    str(args.get("tag") or args.get("name") or "")[:40],
                    description=str(args.get("description") or ""),
                    color=str(args.get("color") or "slate"),
                )
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            actions_out.append({"type": "inbox_tag_added", "label": f"Tag: {row['name']}", "view": "inbox"})
            return {"status": "created", "tag": row, "tags": inbox_tag_service.tag_names()}
        if act == "rename_tag":
            try:
                res = inbox_tag_service.rename_tag(
                    str(args.get("tag") or args.get("name") or ""),
                    str(args.get("new_name") or ""),
                )
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            return {"status": "renamed", **res}
        if act == "move_message":
            nid = args.get("notification_id")
            tag = str(args.get("tag") or "").strip()
            if not nid or not tag:
                return {"status": "error", "error": "notification_id and tag are required"}
            notif = session.get(Notification, int(nid))
            if not notif:
                return {"status": "error", "error": "notification not found"}
            notif.tags_csv = tag[:400]
            notif.category = tag[:64]
            notif.tag_corrected_by_user = True
            session.add(notif)
            session.commit()
            inbox_tag_service.record_correction(
                sender=notif.sender or notif.source_app,
                subject=notif.title,
                from_tag=None,
                to_tag=tag,
                source_app=notif.source_app,
            )
            actions_out.append({"type": "inbox_tagged", "label": f"Tagged as {tag}", "view": "inbox"})
            return {"status": "moved", "id": notif.id, "tags": notif.tags_csv}
        if act == "query":
            ast = inbox_tag_service.parse_filter(str(args.get("query") or ""))
            limit = int(args.get("limit") or 20)
            rows = list(session.exec(select(Notification).order_by(Notification.timestamp.desc()).limit(600)).all())
            now = datetime.now(timezone.utc)
            hits = [
                {
                    "id": n.id,
                    "title": n.title,
                    "source_app": n.source_app,
                    "tags": n.tags_csv,
                    "read": n.read_state,
                    "timestamp": n.timestamp.isoformat() if n.timestamp else None,
                }
                for n in rows
                if inbox_tag_service.matches(n.model_dump(mode="json"), ast, now=now)
            ][:limit]
            return {"status": "ok", "explained": inbox_tag_service.explain_filter(str(args.get("query") or "")), "count": len(hits), "items": hits}
        if act == "dedupe":
            backfill_dedupe_keys(session)
            return {"status": "ok", **merge_duplicate_notifications(session)}
        return {"status": "error", "error": f"Unknown action '{act}'"}

    if name == "manage_grade_tabs":
        from .grades_control import grades_control_service

        act = str(args.get("action") or "list").strip().lower()
        if act == "list":
            course_rows = list(session.exec(select(CourseGrade)).all())
            return {
                "status": "ok",
                "tabs": grades_control_service.list_tabs(session),
                "courses": [
                    {"course_key": c.course_key, "course_name": c.course_name, "source_app": c.source_app}
                    for c in course_rows
                ],
                "visibility": grades_control_service.visibility_map(session),
            }
        if act in ("create_tab", "update_tab"):
            tab = grades_control_service.upsert_tab(
                session,
                tab_id=args.get("tab_id"),
                name=str(args.get("name") or "Tab"),
                color=str(args.get("color") or "emerald"),
                course_keys=list(args.get("course_keys") or []),
                match_rules=[r for r in (args.get("match_rules") or []) if isinstance(r, dict)],
                order=int(args.get("order") or 100),
            )
            actions_out.append({"type": "grades_tab_saved", "label": f"Grades tab: {tab.get('name')}", "view": "grades"})
            return {"status": "saved", "tab": tab}
        if act == "delete_tab":
            try:
                ok = grades_control_service.delete_tab(session, str(args.get("tab_id") or ""))
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            return {"status": "deleted" if ok else "not_found"}
        if act == "hide_course":
            ck = str(args.get("course_key") or "")
            course = session.exec(
                select(CourseGrade).where(CourseGrade.course_key == ck).order_by(CourseGrade.id.desc())
            ).first()
            if course:
                res = grades_control_service.hide_course(session, course)
            else:
                res = {
                    "ok": True,
                    "hidden": grades_control_service.set_visibility(
                        session, course_key=ck, tracked=False, hidden=True,
                        counts_toward_gpa=False, counts_toward_average=False,
                    ),
                }
            actions_out.append({"type": "grades_course_hidden", "label": f"Removed: {ck}", "view": "grades"})
            return {"status": "hidden", **res}
        if act == "restore_course":
            row = grades_control_service.set_visibility(
                session,
                course_key=str(args.get("course_key") or ""),
                tracked=True,
                hidden=False,
            )
            return {"status": "restored", "visibility": row}
        if act == "set_weights":
            row = grades_control_service.set_visibility(
                session,
                course_key=str(args.get("course_key") or ""),
                counts_toward_gpa=args.get("counts_toward_gpa"),
                counts_toward_average=args.get("counts_toward_average"),
            )
            return {"status": "updated", "visibility": row}
        return {"status": "error", "error": f"Unknown action '{act}'"}

    if name == "drive_explorer":
        from .drive_service import drive_service

        act = str(args.get("action") or "list").strip().lower()
        rel = str(args.get("path") or "").strip()
        try:
            if act == "list":
                return {"status": "ok", **drive_service.list_directory(rel)}
            if act == "mkdir":
                created = drive_service.create_folder(rel)
                actions_out.append({"type": "drive_folder_created", "label": f"Folder: {created['path']}", "view": "files"})
                return {"status": "created", "entry": created}
            if act == "read":
                return {"status": "ok", "file": drive_service.read_file(rel)}
            if act == "write":
                written = drive_service.write_text_file(rel, str(args.get("content") or ""))
                actions_out.append({"type": "drive_file_saved", "label": f"Saved {written['name']}", "view": "files"})
                return {"status": "saved", "file": written}
            if act == "move":
                moved = drive_service.move_or_rename(rel, str(args.get("dest_path") or ""))
                actions_out.append({"type": "drive_item_moved", "label": f"Moved to {moved['path']}", "view": "files"})
                return {"status": "moved", "entry": moved}
            if act == "delete":
                ok = drive_service.delete_item(rel)
                return {"status": "deleted" if ok else "not_found", "path": rel}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    if name in ("get_current_time_and_dates", "get_time"):
        return get_local_time_reference(21)

    if name == "list_calendar_events":
        s_date = str(args.get("start_date") or today_iso)
        e_date = str(args.get("end_date") or (date.fromisoformat(today_iso) + timedelta(days=14)).isoformat())
        q_str = str(args.get("query") or "").strip().lower()
        evs = list(
            session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.start_at >= f"{s_date}T00:00")
                .where(CalendarEvent.start_at <= f"{e_date}T23:59")
                .order_by(CalendarEvent.start_at.asc())
            ).all()
        )
        if q_str:
            evs = [e for e in evs if q_str in e.title.lower() or q_str in (e.category or "").lower()]
        return {
            "count": len(evs),
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "start_at": e.start_at,
                    "end_at": e.end_at,
                    "category": e.category,
                    "location": e.location,
                    "completed": e.completed,
                }
                for e in evs[:35]
            ],
        }

    if name == "create_calendar_event":
        title = str(args.get("title") or "Scheduled Block").strip()
        d_str = str(args.get("date") or today_iso).strip()
        try:
            d_obj = date.fromisoformat(d_str)
        except ValueError:
            d_obj = _parse_natural_date(d_str)
        st_parsed = _parse_hhmm(args.get("start_time")) or (16, 0)
        start_dt = datetime(d_obj.year, d_obj.month, d_obj.day, st_parsed[0], st_parsed[1])

        et_parsed = _parse_hhmm(args.get("end_time"))
        if et_parsed:
            end_dt = datetime(d_obj.year, d_obj.month, d_obj.day, et_parsed[0], et_parsed[1])
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(minutes=45)
        else:
            dur = int(args.get("duration_minutes") or memory_store.estimate_task_minutes(title)[0] or 45)
            end_dt = start_dt + timedelta(minutes=max(5, min(480, dur)))

        raw_start_iso = start_dt.strftime("%Y-%m-%dT%H:%M")
        raw_end_iso = end_dt.strftime("%Y-%m-%dT%H:%M")
        fixed_start_iso, fixed_end_iso, wd_note = verify_and_fix_weekday_date(
            user_message_context,
            raw_start_iso,
            raw_end_iso,
            now_dt,
        )
        final_date_iso = fixed_start_iso[:10]

        cat = str(args.get("category") or "Study Block")
        ev = CalendarEvent(
            title=title,
            start_at=fixed_start_iso,
            end_at=fixed_end_iso or raw_end_iso,
            all_day=False,
            category=cat,
            color=color_map.get(cat, "amber"),
            location=args.get("location"),
            notes=args.get("notes"),
            source="AI Secretary",
        )
        session.add(ev)
        session.commit()
        session.refresh(ev)
        actions_out.append(
            {
                "type": "event_created",
                "label": f"Created: {ev.title} ({ev.start_at[5:].replace('T', ' ')})",
                "view": "calendar",
                "date": final_date_iso,
                "event_id": ev.id,
            }
        )
        res_out: Dict[str, Any] = {
            "status": "created",
            "weekday": date.fromisoformat(final_date_iso).strftime("%A"),
            "event": {"id": ev.id, "title": ev.title, "start_at": ev.start_at, "end_at": ev.end_at},
        }
        if wd_note:
            res_out["weekday_verified"] = wd_note
        return res_out

    if name == "update_calendar_event":
        ev = _match_calendar_event(
            session,
            event_id=args.get("event_id"),
            match_title=args.get("match_title"),
        )
        if not ev:
            return {"status": "not_found", "error": "No matching calendar event found. Call list_calendar_events first."}

        old_start = ev.start_at
        old_end = ev.end_at
        cur_start = datetime.fromisoformat(ev.start_at)
        cur_end = datetime.fromisoformat(ev.end_at)
        cur_dur_mins = max(5, int((cur_end - cur_start).total_seconds() // 60))

        target_date = cur_start.date()
        if args.get("new_date"):
            try:
                target_date = date.fromisoformat(str(args["new_date"]).strip())
            except ValueError:
                target_date = _parse_natural_date(str(args["new_date"]))

        st_hm = _parse_hhmm(args.get("new_start_time")) or (cur_start.hour, cur_start.minute)
        new_start_dt = datetime(target_date.year, target_date.month, target_date.day, st_hm[0], st_hm[1])

        et_hm = _parse_hhmm(args.get("new_end_time"))
        if et_hm:
            new_end_dt = datetime(target_date.year, target_date.month, target_date.day, et_hm[0], et_hm[1])
            if new_end_dt <= new_start_dt:
                new_end_dt = new_start_dt + timedelta(minutes=cur_dur_mins)
        elif args.get("duration_minutes") is not None:
            new_end_dt = new_start_dt + timedelta(minutes=max(5, min(480, int(args["duration_minutes"]))))
        else:
            new_end_dt = new_start_dt + timedelta(minutes=cur_dur_mins)

        if args.get("new_title"):
            ev.title = str(args["new_title"]).strip()
        if args.get("category"):
            ev.category = str(args["category"]).strip()
        if args.get("location") is not None:
            ev.location = str(args["location"])
        if args.get("notes") is not None:
            ev.notes = str(args["notes"])
        if args.get("completed") is not None:
            ev.completed = bool(args["completed"])

        ev.start_at = new_start_dt.strftime("%Y-%m-%dT%H:%M")
        ev.end_at = new_end_dt.strftime("%Y-%m-%dT%H:%M")
        if args.get("new_date") and user_message_context:
            fixed_s, fixed_e, _ = verify_and_fix_weekday_date(
                user_message_context,
                ev.start_at,
                ev.end_at,
                now_dt,
            )
            ev.start_at = fixed_s
            ev.end_at = fixed_e or ev.end_at
            target_date = date.fromisoformat(ev.start_at[:10])
        session.add(ev)
        session.commit()
        session.refresh(ev)

        actions_out.append(
            {
                "type": "event_updated",
                "label": f"Updated: {ev.title} ({ev.start_at[5:].replace('T', ' ')})",
                "view": "calendar",
                "date": target_date.isoformat(),
                "event_id": ev.id,
            }
        )
        return {
            "status": "updated",
            "previous": {"start_at": old_start, "end_at": old_end},
            "event": {"id": ev.id, "title": ev.title, "start_at": ev.start_at, "end_at": ev.end_at, "category": ev.category},
        }

    if name == "delete_calendar_event":
        ev = _match_calendar_event(
            session,
            event_id=args.get("event_id"),
            match_title=args.get("match_title"),
            date_filter=args.get("date"),
        )
        if not ev:
            return {"status": "not_found", "error": "No matching calendar event found to delete."}
        deleted_info = {"id": ev.id, "title": ev.title, "start_at": ev.start_at}
        ev_date = ev.start_at[:10]
        session.delete(ev)
        session.commit()
        actions_out.append(
            {
                "type": "event_deleted",
                "label": f"Removed: {deleted_info['title']}",
                "view": "calendar",
                "date": ev_date,
            }
        )
        return {"status": "deleted", "deleted_event": deleted_info}

    if name == "coordinate_day":
        d_str = str(args.get("date") or today_iso)
        try:
            d_iso = date.fromisoformat(d_str).isoformat()
        except ValueError:
            d_iso = _parse_natural_date(d_str).isoformat()
        res = coordinate_day_schedule(session=session, target_date=d_iso, include_tasks=True, include_goals=True)
        created = res["created_blocks"]
        actions_out.append(
            {
                "type": "calendar_coordinated",
                "label": f"View {d_iso} ({len(created)} block(s))",
                "view": "calendar",
                "date": d_iso,
            }
        )
        return {
            "status": "coordinated",
            "date": d_iso,
            "placed_count": len(created),
            "blocks": [{"id": b.id, "title": b.title, "start_at": b.start_at, "end_at": b.end_at} for b in created],
        }

    if name == "list_tasks":
        inc_done = bool(args.get("include_completed", False))
        q = select(Task) if inc_done else select(Task).where(Task.completed == False)  # noqa: E712
        rows = list(session.exec(q).all())
        prio_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        rows.sort(key=lambda t: (1 if t.completed else 0, prio_rank.get(t.priority, 2), t.sort_order or 999999, t.due_date or "9999-12-31"))
        return {
            "count": len(rows),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "priority": t.priority,
                    "estimated_minutes": t.estimated_minutes or _smart_heuristic_duration(t.title),
                    "due_date": t.due_date,
                    "category": t.category,
                    "completed": t.completed,
                }
                for t in rows[:40]
            ],
        }

    if name == "create_task":
        title = str(args.get("title") or "New Task").strip()
        raw_due = args.get("due_date")
        due_str: Optional[str] = None
        if raw_due:
            try:
                due_str = date.fromisoformat(str(raw_due).strip()[:10]).isoformat()
            except ValueError:
                due_str = _parse_natural_date(str(raw_due)).isoformat()
        else:
            due_str = today_iso

        est_m = args.get("estimated_minutes")
        est_clean = max(5, min(240, int(est_m))) if est_m else _smart_heuristic_duration(title)
        prio = normalize_priority_value(args.get("priority") or "normal")
        existing_orders = [int(x) for x in session.exec(select(Task.sort_order)).all() if x is not None]
        next_ord = (max(existing_orders) + 10) if existing_orders else 10

        t = Task(
            source_app="Secretary",
            title=title[:400],
            due_date=due_str,
            completed=False,
            category=str(args.get("category") or "General")[:80],
            priority=prio,
            sort_order=next_ord,
            estimated_minutes=est_clean,
        )
        session.add(t)
        session.commit()
        session.refresh(t)
        actions_out.append({"type": "task_created", "label": f"Added Task: {t.title[:28]}", "view": "tasks", "task_id": t.id})
        return {
            "status": "created",
            "task": {
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "estimated_minutes": t.estimated_minutes,
                "due_date": t.due_date,
            },
        }

    if name == "update_task":
        tid = args.get("task_id")
        mt = str(args.get("match_title") or "").strip().lower()
        t_obj = session.get(Task, int(tid)) if tid is not None else None
        if not t_obj and mt:
            all_t = list(session.exec(select(Task)).all())
            t_obj = next((x for x in all_t if mt in x.title.lower()), None)
        if not t_obj:
            return {"status": "not_found", "error": "Task not found"}
        if args.get("completed") is not None:
            t_obj.completed = bool(args["completed"])
        if args.get("new_title"):
            t_obj.title = str(args["new_title"]).strip()
        if args.get("priority") is not None:
            t_obj.priority = normalize_priority_value(args["priority"])
        if args.get("estimated_minutes") is not None:
            t_obj.estimated_minutes = max(5, min(240, int(args["estimated_minutes"])))
        if args.get("due_date") is not None:
            t_obj.due_date = str(args["due_date"]).strip() or None
        if args.get("sort_order") is not None:
            t_obj.sort_order = int(args["sort_order"])
        session.add(t_obj)
        session.commit()
        session.refresh(t_obj)
        actions_out.append({"type": "task_updated", "label": f"Task Updated: {t_obj.title[:26]}", "view": "tasks"})
        return {
            "status": "updated",
            "task": {
                "id": t_obj.id,
                "title": t_obj.title,
                "completed": t_obj.completed,
                "priority": t_obj.priority,
                "estimated_minutes": t_obj.estimated_minutes,
                "due_date": t_obj.due_date,
            },
        }

    if name == "delete_task":
        if args.get("clear_completed"):
            done_rows = list(session.exec(select(Task).where(Task.completed == True)).all())  # noqa: E712
            for r in done_rows:
                session.delete(r)
            session.commit()
            actions_out.append({"type": "task_deleted", "label": f"Cleared {len(done_rows)} completed task(s)", "view": "tasks"})
            return {"status": "cleared_completed", "count": len(done_rows)}

        tid = args.get("task_id")
        mt = str(args.get("match_title") or "").strip().lower()
        t_obj = session.get(Task, int(tid)) if tid is not None else None
        if not t_obj and mt:
            all_t = list(session.exec(select(Task)).all())
            t_obj = next((x for x in all_t if mt in x.title.lower()), None)
        if not t_obj:
            return {"status": "not_found", "error": "Task not found"}
        title_copy = t_obj.title
        session.delete(t_obj)
        session.commit()
        actions_out.append({"type": "task_deleted", "label": f"Deleted Task: {title_copy[:24]}", "view": "tasks"})
        return {"status": "deleted", "title": title_copy}

    if name == "list_notifications":
        q = select(Notification).order_by(Notification.timestamp.desc())
        rows = list(session.exec(q).all())
        if args.get("unread_only", True):
            rows = [n for n in rows if not n.read_state]
        if args.get("important_only"):
            rows = [n for n in rows if n.is_important]
        if args.get("source_app"):
            sa = str(args["source_app"]).strip().lower()
            rows = [n for n in rows if sa in (n.source_app or "").lower()]
        if args.get("category"):
            cat = str(args["category"]).strip().lower()
            rows = [n for n in rows if cat in (getattr(n, "category", "") or "").lower()]
        return {
            "count": len(rows),
            "notifications": [
                {
                    "id": n.id,
                    "source_app": n.source_app,
                    "title": n.title,
                    "summary": n.ai_summary or n.message[:140],
                    "category": getattr(n, "category", "General"),
                    "is_important": n.is_important,
                    "priority": n.priority,
                    "read_state": n.read_state,
                }
                for n in rows[:30]
            ],
        }

    if name == "manage_notifications":
        act = str(args.get("action") or "mark_read").strip().lower()
        nid = args.get("notification_id")
        rows: List[Notification] = []
        if nid is not None:
            one = session.get(Notification, int(nid))
            if one:
                rows = [one]
        else:
            cand = list(session.exec(select(Notification)).all())
            if args.get("source_app"):
                sa = str(args["source_app"]).strip().lower()
                cand = [n for n in cand if sa in (n.source_app or "").lower()]
            if args.get("promo_only"):
                cand = [n for n in cand if not n.is_important or getattr(n, "category", "").lower() == "promo"]
            elif args.get("all_unread") or not args.get("source_app"):
                cand = [n for n in cand if not n.read_state]
            rows = cand

        for n in rows:
            if act == "delete":
                session.delete(n)
            elif act == "mark_unread":
                n.read_state = False
                session.add(n)
            elif act in ("mark_important", "important"):
                importance_service.record_feedback(session, n, important=True)
            elif act in ("mark_not_important", "not_important", "mute"):
                importance_service.record_feedback(session, n, important=False)
            else:
                n.read_state = True
                session.add(n)

        session.commit()
        actions_out.append({"type": "inbox_updated", "label": f"Inbox: {act} ({len(rows)})", "view": "inbox"})
        return {"status": "ok", "action": act, "affected": len(rows)}

    if name == "list_monitored_apps":
        _, apps = apps_manager.get_global_and_apps()
        unread_rows = list(session.exec(select(Notification).where(Notification.read_state == False)).all())  # noqa: E712
        counts: Dict[str, int] = {}
        for n in unread_rows:
            counts[n.source_app] = counts.get(n.source_app, 0) + 1
        return {
            "count": len(apps),
            "apps": [
                {
                    "slug": a["slug"],
                    "name": a["name"],
                    "url": a["url"],
                    "account_profile": a.get("account_profile", "default"),
                    "enabled": a["enabled"],
                    "unread_count": counts.get(a["name"], 0),
                }
                for a in apps
            ],
        }

    if name == "remove_monitored_app":
        slug = str(args.get("slug_or_name") or "").strip()
        ok = apps_manager.delete_app(slug)
        if not ok:
            return {"status": "not_found", "error": f"Monitor '{slug}' not found"}
        actions_out.append({"type": "app_removed", "label": f"Removed Monitor: {slug}", "view": "apps"})
        return {"status": "deleted", "slug": slug}

    if name == "duplicate_app_account":
        slug = str(args.get("slug_or_name") or "").strip()
        try:
            cloned = apps_manager.duplicate_for_new_account(slug, custom_name=args.get("label"))
            actions_out.append({"type": "app_added", "label": f"Added Account: {cloned['name']}", "view": "apps"})
            return {"status": "created", "app": cloned}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    if name == "get_grades":
        dash = grades_service.get_dashboard(session)
        q = str(args.get("course_query") or "").strip().lower()
        if q:
            dash["courses"] = [
                c
                for c in (dash.get("courses") or [])
                if q in str(c.get("course_name") or "").lower() or q in str(c.get("course_key") or "").lower()
            ]
        actions_out.append({"type": "navigate", "label": "Open Grades", "view": "grades", "auto": False})
        return dash

    if name == "manage_goals":
        act = str(args.get("action") or "list").strip().lower()
        if act == "list":
            gs = list(session.exec(select(LongTermGoal).where(LongTermGoal.active == True)).all())  # noqa: E712
            return {
                "goals": [
                    {"id": g.id, "title": g.title, "minutes_per_day": g.minutes_per_day, "streak_days": g.streak_days, "daily_habit": g.daily_habit}
                    for g in gs
                ]
            }
        if act == "create":
            title = str(args.get("title") or "New Goal").strip()
            mins = max(10, min(180, int(args.get("minutes_per_day") or 25)))
            cat = str(args.get("category") or "General")
            plan = build_smart_goal_breakdown(title=title, category=cat, minutes_per_day=mins)
            ng = LongTermGoal(
                title=title,
                category=cat,
                target_date=(get_local_now().date() + timedelta(days=60)).isoformat(),
                why_it_matters="Tracked daily by Node.",
                daily_habit=plan["daily_habit"],
                minutes_per_day=plan["minutes_per_day"],
                milestones_json=json.dumps(plan["milestones"]),
                active=True,
            )
            session.add(ng)
            session.commit()
            session.refresh(ng)
            actions_out.append({"type": "goal_created", "label": f"Goal: {ng.title[:24]}", "view": "goals"})
            return {"status": "created", "goal": {"id": ng.id, "title": ng.title, "minutes_per_day": ng.minutes_per_day}}
        if act in ("check_in", "checkin"):
            gid = args.get("goal_id")
            g = session.get(LongTermGoal, int(gid)) if gid else session.exec(select(LongTermGoal)).first()
            if not g:
                return {"status": "not_found"}
            if g.last_checkin_date != today_iso:
                g.streak_days = (g.streak_days or 0) + 1
                g.last_checkin_date = today_iso
                g.progress_percent = min(100, (g.progress_percent or 0) + 5)
                session.add(g)
                session.commit()
            actions_out.append({"type": "goal_updated", "label": f"Checked in: {g.title[:22]}", "view": "goals"})
            return {"status": "checked_in", "streak_days": g.streak_days}
        if act == "delete":
            gid = args.get("goal_id")
            g = session.get(LongTermGoal, int(gid)) if gid else None
            if not g and args.get("title"):
                needle = str(args["title"]).lower()
                g = next((x for x in session.exec(select(LongTermGoal)).all() if needle in x.title.lower()), None)
            if not g:
                return {"status": "not_found"}
            session.delete(g)
            session.commit()
            return {"status": "deleted"}

    if name == "save_notebook_note":
        act = str(args.get("action") or "append").strip().lower()
        if act == "list":
            return {"files": memory_store.list_notebook_files()}
        if act == "read":
            slug = str(args.get("file_slug") or "scratchpad")
            return memory_store.get_notebook_file(slug)
        slug = str(args.get("file_slug") or memory_store.route_topic_slug_for_note(str(args.get("text") or "")))
        doc = memory_store.append_to_notebook_file(slug, str(args.get("text") or ""), category="Secretary")
        actions_out.append(
            {
                "type": "note_added",
                "label": f"Updated {doc['filename']}",
                "view": "memory",
                "subtab": "notes",
                "notebook_slug": doc["slug"],
            }
        )
        return {"status": "saved", "file": doc["filename"]}

    if name == "update_ai_memory":
        rule = str(args.get("rule") or "").strip()
        if rule:
            memory_store.append_memory_rule(rule)
        actions_out.append({"type": "memory_updated", "label": "Open Memory", "view": "memory", "subtab": "memory"})
        return {"status": "saved", "rule": rule}

    if name == "learn_task_duration":
        mins = max(2, min(240, int(args.get("minutes") or 15)))
        res = memory_store.learn_duration_preference(
            target_archetype=args.get("archetype"),
            minutes=mins,
            raw_user_feedback=f"Tool update: {mins}m",
            custom_keyword=args.get("keyword"),
            also_calibrate_related=True,
        )
        resized = recalibrate_scheduled_blocks(session)
        actions_out.append(
            {
                "type": "notebook_updated",
                "label": "Updated time_estimates.md",
                "view": "memory",
                "subtab": "notes",
                "notebook_slug": "time_estimates",
            }
        )
        return {"status": "updated", "updates": res["updates_applied"], "resized_blocks": len(resized)}

    if name == "open_os_window":
        view_raw = str(args.get("view") or "calendar").strip().lower()
        alias_map = {
            "monitors": "apps",
            "monitor": "apps",
            "notes": "memory",
            "notebook": "memory",
            "youtube": "focus_guard",
            "focus": "focus_guard",
            "system": "settings",
        }
        view = alias_map.get(view_raw, view_raw)
        actions_out.append({"type": "navigate", "label": f"Opened {view.title()}", "view": view, "auto": True})
        return {"status": "opened", "view": view}

    if name == "update_os_setting":
        key = str(args.get("key") or "").strip()
        val = args.get("value")
        updated_state = os_settings_service.update_state(settings_patch={key: val})
        updated = updated_state.get("settings") or {}
        actions_out.append(
            {
                "type": "os_settings_updated",
                "label": f"Setting `{key}` → `{updated.get(key)}`",
                "view": "settings",
                "settings_patch": {key: updated.get(key)},
            }
        )
        return {"status": "updated", "key": key, "value": updated.get(key)}

    return {"status": "error", "error": f"Unknown tool: {name}"}


class SecretaryService:
    """
    Node AI Secretary with:
      - Real-time Streaming Chat + Collapsible `<think>` Traces
      - Full-OS Tool-Calling CRUD over Calendar, Tasks, Inbox, Monitors, Grades, Notes & Settings
      - Anti-Hallucination Safety Nets for Create, Edit, and Delete Operations
    """

    def _gather_context(self, session: Session) -> Dict[str, Any]:
        time_ref = get_local_time_reference(14)
        today_str = time_ref["today_date"]
        horizon_end = (date.fromisoformat(today_str) + timedelta(days=10)).isoformat()

        notifs = list(
            session.exec(
                select(Notification)
                .where(Notification.read_state == False)  # noqa: E712
                .order_by(Notification.timestamp.desc())
            ).all()
        )
        tasks = list(session.exec(select(Task).where(Task.completed == False)).all())  # noqa: E712
        events_today = list(
            session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.start_at >= f"{today_str}T00:00")
                .where(CalendarEvent.start_at <= f"{today_str}T23:59")
                .order_by(CalendarEvent.start_at.asc())
            ).all()
        )
        events_upcoming = list(
            session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.start_at >= f"{today_str}T00:00")
                .where(CalendarEvent.start_at <= f"{horizon_end}T23:59")
                .order_by(CalendarEvent.start_at.asc())
            ).all()
        )
        active_goals = list(
            session.exec(
                select(LongTermGoal)
                .where(LongTermGoal.active == True)  # noqa: E712
                .order_by(LongTermGoal.created_at.asc())
            ).all()
        )
        courses = list(session.exec(select(CourseGrade)).all())
        _, apps = apps_manager.get_global_and_apps()
        memory_doc = memory_store.get_memory()
        notebook_files = memory_store.list_notebook_files()
        shared_logs_digest = agent_logbook.render_digest_for_prompt(session, limit=12)

        return {
            "time_ref": time_ref,
            "today_str": today_str,
            "notifications": notifs,
            "tasks": tasks,
            "events_today": events_today,
            "events_upcoming": events_upcoming,
            "active_goals": active_goals,
            "courses": courses,
            "apps": apps,
            "memory": memory_doc["content"],
            "notebook_files": notebook_files,
            "shared_logs_digest": shared_logs_digest,
        }

    def _apply_gate_slots(self, msg: str, slots: Dict[str, Any], actions: List[Dict[str, Any]], session: Session) -> str:
        """
        Create the event the decision gate already parsed, and hand the chat model
        a confirmed result instead of a raw sentence.

        "mark that I have band practice on friday" becomes title='Band Practice',
        date=<the real Friday>, category='Extracurricular' — created immediately.
        """
        from datetime import datetime as _dt, timedelta as _td

        from .calendar_engine import format_local_dt

        title = (slots.get("title") or "").strip()
        date_iso = (slots.get("date") or "").strip()
        if not title or not date_iso:
            return msg
        start = (slots.get("start_time") or "").strip()
        minutes = int(slots.get("duration_minutes") or 60)
        category = (slots.get("category") or "Personal").strip() or "Personal"
        try:
            if start:
                start_dt = _dt.fromisoformat(f"{date_iso}T{start}")
                end_dt = start_dt + _td(minutes=minutes)
            else:
                start_dt = _dt.fromisoformat(f"{date_iso}T00:00")
                end_dt = start_dt
            event = CalendarEvent(
                title=title[:290],
                start_at=format_local_dt(start_dt),
                end_at=format_local_dt(end_dt),
                all_day=not bool(start),
                category=category[:80],
                color="amber",
                location=(slots.get("location") or "").strip()[:240] or None,
                notes=("Created by the decision gate from: " + msg[:280]),
                source="Decision Gate",
            )
            session.add(event)
            session.commit()
            session.refresh(event)
        except Exception:  # pragma: no cover - never break chat
            try:
                session.rollback()
            except Exception:
                pass
            return msg
        actions.append(
            {
                "type": "calendar_event_created",
                "label": f"{title} on {date_iso}" + (f" at {start}" if start else ""),
                "view": "calendar",
            }
        )
        gateway_note = (
            f"[DECISION GATE PRE-EXECUTED] The calendar event was already created: "
            f"title='{title}', date='{date_iso}', start='{start or 'all-day'}', "
            f"category='{category}'. Do NOT call create_calendar_event again. "
            f"Confirm it to the user in one short sentence."
        )
        return f"{msg}\n\n{gateway_note}"

    def _build_agent_messages(
        self,
        msg: str,
        ctx: Dict[str, Any],
        prior_rows: List[ChatMessage],
        tool_plan: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        notebook_ctx, loaded_files = memory_store.select_relevant_notebook_context(msg)
        cal_lookup = build_14_day_calendar_table(get_local_now())
        upcoming_ev_lines = "\n".join(
            f"- [id={e.id}] {e.start_at.replace('T', ' ')}–{e.end_at[-5:]} | {e.title} ({e.category})"
            for e in ctx["events_upcoming"][:18]
        ) or "- (No upcoming events)"
        task_lines = "\n".join(
            f"- [id={t.id}, {t.priority}, ~{t.estimated_minutes or 25}m] {t.title} (Due: {t.due_date or 'flexible'})"
            for t in ctx["tasks"][:14]
        ) or "- (No open tasks)"
        notif_lines = "\n".join(
            f"- [id={n.id}, {n.source_app}, {'IMP' if n.is_important else 'FYI'}] {n.title}"
            for n in ctx["notifications"][:10]
        ) or "- (Inbox zero)"
        grade_lines = "\n".join(
            (
                f"- [{c.source_app}] {c.course_name}: {c.letter_grade} ({c.score_percent}%)"
                + (
                    f" [was {c.prev_score_percent}%]"
                    if c.prev_score_percent is not None and c.prev_score_percent != c.score_percent
                    else ""
                )
                + (f" ({c.missing_count} missing)" if c.missing_count else "")
            )
            if c.score_percent is not None
            else f"- [{c.source_app}] {c.course_name}: {c.letter_grade or '—'}"
            for c in ctx["courses"][:16]
        ) or "- (No grades scanned yet)"
        app_lines = ", ".join(f"{a['name']} ({a['slug']})" for a in ctx["apps"][:10]) or "(None — user can install from App Library via `install_catalog_app`)"

        from .self_improvement import self_improvement_service

        self_imp_txt = ""
        try:
            self_imp_txt = self_improvement_service.self_improvement_path().read_text(encoding="utf-8")[:650]
        except Exception:
            self_imp_txt = ""

        sys_prompt = (
            "You are Node's executive AI OS Agent. You have FULL tool-calling control over the entire OS: "
            "Calendar, Tasks, Inbox Notifications & Categories, Shared Drive (`drive_explorer`), App Library & Monitored Web Apps, "
            "Grades (PowerSchool / Schoology / Canvas / Classroom), Goals, Unified AI Instructions & Shared Logs, Web Search, Windows, and OS Settings.\n\n"
            "MULTI-AGENT SHARED INSTRUCTIONS & SELF-IMPROVEMENT PROTOCOL:\n"
            "- You share unified instruction files (`persona.md`, `self_improvement.md`, `shared_agent_log.md`, and topic `.md` files) with peer AI models and 24/7 Cron jobs.\n"
            "- You may create or edit instruction/log files (`save_notebook_note`, `write_shared_log`, `edit_shared_log`) whenever doing so genuinely improves future recall, efficiency, or self-reflection — keep notes concise and consolidate duplicates rather than creating excessive files.\n"
            "- ANTI-OVERFITTING RULE: Apply learned preferences as flexible, natural principles; NEVER parrot canned templates or become rigidly patterned.\n\n"
            "CRITICAL EXECUTION & DATE ACCURACY RULES:\n"
            "1. Whenever the user asks to add, schedule, put, move, edit, delete, complete, mark read, categorize, create/read Drive files, install an app, search the web, or change settings, "
            "you MUST invoke the matching tool (`create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `coordinate_day`, "
            "`create_task`, `update_task`, `delete_task`, `sync_tasks_from_sources`, `manage_notifications`, `manage_inbox_categories`, `drive_explorer`, `install_catalog_app`, `add_monitored_app`, "
            "`remove_monitored_app`, `duplicate_app_account`, `manage_goals`, `apply_inbox_filter_rule`, `manage_grade_tabs`, `manage_api_bridge`, `save_notebook_note`, `update_ai_memory`, `write_shared_log`, `edit_shared_log`, `web_search`, `fetch_webpage`, `open_os_window`, `update_os_setting`).\n"
            "2. NEVER merely open or describe the calendar when asked to add/edit/delete an event — call `create_calendar_event` / `update_calendar_event` / `delete_calendar_event`!\n"
            "3. EXACT WEEKDAY ACCURACY: NEVER compute weekday offsets in your head. When the user says e.g. 'this Wednesday' or 'on Friday', copy the EXACT `YYYY-MM-DD` for that weekday directly from the 14-DAY CALENDAR LOOKUP TABLE below!\n"
            "4. LIVE WEB ACCESS: Call `web_search` (and `fetch_webpage` if needed) whenever the user asks about external facts, current info, URLs, or documentation.\n\n"
            f"{cal_lookup}\n\n"
            f"SELF-IMPROVEMENT PRINCIPLES:\n{self_imp_txt}\n\n"
            f"SHARED MULTI-AGENT LOGBOOK DIGEST:\n{ctx['shared_logs_digest']}\n\n"
            f"UPCOMING CALENDAR EVENTS:\n{upcoming_ev_lines}\n\n"
            f"OPEN TASKS:\n{task_lines}\n\n"
            f"UNREAD INBOX ({len(ctx['notifications'])} total):\n{notif_lines}\n\n"
            f"GRADES:\n{grade_lines}\n\n"
            f"MONITORED APPS: {app_lines}\n\n"
            f"INSTRUCTIONS & CONTEXT ({', '.join(loaded_files)}):\n{ctx['memory'][:450]}\n{notebook_ctx[:700]}"
            + (
                "\n\nDECISION GATE PLAN (pre-computed by the LFM3 d1 router):\n"
                f"- intent: {tool_plan.get('intent')}\n"
                f"- allowed tool families: {', '.join(tool_plan.get('groups') or []) or '(none — answer directly)'}\n"
                f"- extracted slots: {json.dumps(tool_plan.get('slots') or {})[:600]}\n"
                "RULES: use ONLY the tools provided in this turn. When a slot value is given above, "
                "use it verbatim instead of re-deriving it. Titles must be short nouns ('Band Practice'), "
                "never the user's sentence."
                if tool_plan
                else ""
            )
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        for pr in prior_rows:
            if pr.role in ("user", "assistant") and pr.content:
                messages.append({"role": pr.role, "content": pr.content[:1100]})
        messages.append({"role": "user", "content": msg})
        return messages

    async def _try_deterministic_intent_fallback(
        self,
        msg: str,
        ctx: Dict[str, Any],
        session: Session,
        actions: List[Dict[str, Any]],
    ) -> Optional[Tuple[str, str]]:
        """
        Safety-net executor when Ollama is offline OR when a small 2B model answers in prose
        without emitting the required tool call for an explicit user mutation command!
        """
        lower = msg.lower().strip()

        # 1. Explicit Edit / Move / Delete Calendar Event or Task
        det_ed = self._try_deterministic_edit_or_delete(msg, session, actions)
        if det_ed:
            return det_ed

        # 2. Explicit Add / Schedule / Put / Create Calendar Event
        is_cal_add = bool(
            re.match(r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:schedule|book|block)\s+(.+)$", msg, re.IGNORECASE)
            or (
                re.search(r"\b(?:add|put|create|set\s+up)\b", lower)
                and (
                    re.search(r"\b(?:calendar|event|appointment|meeting)\b", lower)
                    or re.search(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", lower)
                )
                and "task" not in lower
                and "goal" not in lower
            )
        )
        if is_cal_add:
            target_dt = _parse_natural_date(msg)
            title = _clean_event_title(msg)
            est_default, _ = memory_store.estimate_task_minutes(title)
            hm_pair, dur = _parse_natural_time_and_duration(msg, fallback_duration=est_default)
            hr, mn = hm_pair or (16, 0)
            dur_val = dur or est_default
            cat = (
                "Extracurricular"
                if any(w in lower for w in ("club", "robotics", "practice", "rehearsal", "meeting", "team", "scrimmage"))
                else ("Class" if "class" in lower or "lecture" in lower else ("Personal" if any(w in lower for w in ("dentist", "doctor", "dinner", "lunch", "gym")) else "Study Block"))
            )
            res = execute_secretary_tool(
                "create_calendar_event",
                {
                    "title": title,
                    "date": target_dt.isoformat(),
                    "start_time": f"{hr:02d}:{mn:02d}",
                    "duration_minutes": dur_val,
                    "category": cat,
                },
                session,
                actions,
            )
            ev = res["event"]
            return (
                f"Scheduled **{ev['title']}** on `{target_dt.isoformat()}` (**{ev['start_at'][-5:]} – {ev['end_at'][-5:]}**).",
                f"Executed `create_calendar_event` -> CalendarEvent id={ev['id']}.",
            )

        # 3. Coordinate / Auto-Schedule Day
        if any(
            kw in lower
            for kw in (
                "coordinate my",
                "time-block",
                "time block",
                "auto-schedule",
                "autoschedule",
                "fit my tasks",
                "put my tasks on my calendar",
                "schedule my tasks",
            )
        ):
            target_dt = _parse_natural_date(msg)
            res = execute_secretary_tool("coordinate_day", {"date": target_dt.isoformat()}, session, actions)
            blocks = res.get("blocks", [])
            lines = [f"- **{b['start_at'][-5:]} – {b['end_at'][-5:]}** • {b['title']}" for b in blocks]
            reply = (
                f"Coordinated **{target_dt.isoformat()}** ({len(blocks)} block(s) placed):\n\n" + "\n".join(lines)
                if blocks
                else f"Checked **{target_dt.isoformat()}** — no unscheduled tasks needed placement."
            )
            return reply, f"Executed `coordinate_day({target_dt.isoformat()})`."

        # 4. Explicit Add Task
        task_match = re.match(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:add\s+(?:a\s+)?task|create\s+(?:a\s+)?task|new\s+task|todo|remind\s+me\s+to)\s*[:\-]?\s*(.+)$",
            msg,
            re.IGNORECASE,
        )
        if task_match:
            raw_title = task_match.group(1).strip().rstrip(".")
            prio = "high" if any(k in lower for k in ("high priority", "urgent", "important")) else ("critical" if "critical" in lower else "normal")
            raw_title = re.sub(r"\b(?:high|critical|low|normal)\s+priority\b", "", raw_title, flags=re.IGNORECASE).strip(" ,:-")
            has_day = any(w in raw_title.lower() for w in ("today", "tomorrow", *WEEKDAY_MAP.keys()))
            due_str = _parse_natural_date(raw_title).isoformat() if has_day else ctx["today_str"]
            res = execute_secretary_tool("create_task", {"title": raw_title, "due_date": due_str, "priority": prio}, session, actions)
            t = res["task"]
            return (
                f"Added task **{t['title']}** *(Priority: `{t['priority']}` • Due `{t['due_date']}` • ~{t['estimated_minutes']}m)*.",
                f"Executed `create_task` -> Task id={t['id']}.",
            )

        # 5. Complete Task
        done_match = re.match(
            r"^(?:please\s+)?(?:complete|finish|check\s+off|mark\s+done|done\s+with)\s+(?:task\s+)?#?(\d+|.+)$",
            msg,
            re.IGNORECASE,
        )
        if done_match:
            q_part = done_match.group(1).strip()
            res = execute_secretary_tool(
                "update_task",
                {"task_id": int(q_part) if q_part.isdigit() else None, "match_title": q_part, "completed": True},
                session,
                actions,
            )
            if res["status"] == "updated":
                return f"Checked off **{res['task']['title']}**.", f"Executed `update_task(completed=True)`."

        # 6. Scan Schoology / Calendar / Inbox for tasks
        if any(k in lower for k in ("scan schoology", "sync tasks", "import tasks from calendar", "scan inbox for tasks", "find tasks in")):
            res = await execute_secretary_tool_async("sync_tasks_from_sources", {}, session, actions)
            return (
                f"Scanned Calendar & Inbox: added **{res['created_count']}** new task(s) "
                f"({res['from_calendar']} from Calendar/Schoology, {res['from_inbox']} from Inbox; {res['skipped_duplicates']} duplicate(s) skipped).",
                "Executed `sync_tasks_from_sources`.",
            )

        # 7. Monitor Website / Add App (Saves Directly!)
        mon_m = re.match(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:monitor|add\s+website|add\s+app|add\s+monitor|watch)\s+(.+)$",
            msg,
            re.IGNORECASE,
        )
        if mon_m:
            target_q = mon_m.group(1).strip()
            suggestion = await apps_manager.build_ai_suggestion(target_q, zero_knowledge_preferred=("zero" in lower or "count only" in lower))
            cfg = suggestion["config"]
            saved = apps_manager.add_or_replace_app(cfg)
            actions.append({"type": "app_added", "label": f"Added Monitor: {saved['name']}", "view": "apps"})
            return (
                f"Added and activated monitor **{saved['name']}** (`{saved['url']}`, profile `{saved['account_profile']}`).",
                f"Executed `add_monitored_app` -> `{saved['slug']}`.",
            )

        # 8. Mark Notifications Read
        if re.search(r"\bmark\s+(?:all\s+)?(?:my\s+)?(?:promo\s+|unread\s+)?(?:notifications|inbox|emails|messages)\s+(?:as\s+)?read\b", lower):
            promo_only = "promo" in lower
            res = execute_secretary_tool("manage_notifications", {"action": "mark_read", "promo_only": promo_only, "all_unread": not promo_only}, session, actions)
            return f"Marked **{res['affected']}** notification(s) as read.", "Executed `manage_notifications(action='mark_read')`."

        # 9. Add Goal
        goal_match = re.match(
            r"^(?:please\s+)?(?:add|create|set|start|track)\s+(?:a\s+)?(?:new\s+)?(?:long[- ]term\s+)?goal\s*[:\-]?\s*(.+)$",
            msg,
            re.IGNORECASE,
        )
        if goal_match:
            raw_goal = goal_match.group(1).strip().rstrip(".")
            min_m = re.search(r"(\d{1,3})\s*(?:m|min|mins|minutes)", raw_goal, re.IGNORECASE)
            mins_per_day = max(10, min(180, int(min_m.group(1)))) if min_m else 25
            clean_title = re.sub(
                r",?\s*\d{1,3}\s*(?:m|min|mins|minutes)\s*(?:a\s+day|per\s+day|daily)?",
                "",
                raw_goal,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            res = execute_secretary_tool("manage_goals", {"action": "create", "title": clean_title, "minutes_per_day": mins_per_day}, session, actions)
            g = res["goal"]
            return f"Added goal **{g['title']}** ({g['minutes_per_day']}m/day).", f"Executed `manage_goals(action='create')`."

        # 10. Save Note or Memory Rule
        note_match = re.match(
            r"^(?:take a note|note down|add a note|write down|jot down|save note|log note)(?:\s+in\s+([a-z0-9_-]+))?\s*[:\-]?\s*(.+)$",
            msg,
            re.IGNORECASE | re.DOTALL,
        )
        if note_match or ("add to my notes" in lower and ":" in msg):
            explicit_slug = note_match.group(1) if note_match else None
            note_body = note_match.group(2).strip() if note_match else msg.split(":", 1)[1].strip()
            res = execute_secretary_tool("save_notebook_note", {"file_slug": explicit_slug, "text": note_body}, session, actions)
            return f"Saved to **`{res['file']}`**:\n\n> {note_body}", f"Executed `save_notebook_note`."

        mem_match = re.match(
            r"^(?:remember that|always remember|remember to|update memory|my preference is|from now on)\s*[:\-]?\s*(.+)$",
            msg,
            re.IGNORECASE | re.DOTALL,
        )
        if mem_match:
            rule_body = mem_match.group(1).strip()
            execute_secretary_tool("update_ai_memory", {"rule": rule_body}, session, actions)
            return f"Saved to `ai_memory.md`:\n- **{rule_body}**", "Executed `update_ai_memory`."

        return None

    def _try_deterministic_edit_or_delete(
        self,
        msg: str,
        session: Session,
        actions: List[Dict[str, Any]],
    ) -> Optional[Tuple[str, str]]:
        lower = msg.lower().strip()

        del_m = re.match(
            r"^(?:please\s+)?(?:can you\s+)?(?:delete|remove|cancel|clear)\s+(?:the\s+|my\s+)?(?:calendar\s+event\s+|event\s+|block\s+)?(.+)$",
            msg.strip(),
            re.IGNORECASE,
        )
        if del_m:
            target_phrase = re.sub(
                r"\s+(?:from\s+(?:my\s+)?calendar|on\s+(?:my\s+)?calendar|today|tomorrow)$",
                "",
                del_m.group(1),
                flags=re.IGNORECASE,
            ).strip(" .\"'")
            if "completed task" in lower:
                res = execute_secretary_tool("delete_task", {"clear_completed": True}, session, actions)
                return f"Cleared **{res['count']}** completed task(s).", "Executed `delete_task(clear_completed=True)`."
            if "task" in lower:
                t_clean = re.sub(r"^task\s+", "", target_phrase, flags=re.IGNORECASE).strip()
                res = execute_secretary_tool("delete_task", {"match_title": t_clean}, session, actions)
                if res["status"] == "deleted":
                    return f"Deleted task **{res['title']}**.", f"Executed `delete_task(match_title='{t_clean}')`."
            id_m = re.search(r"#(\d+)\b", target_phrase)
            res = execute_secretary_tool(
                "delete_calendar_event",
                {"event_id": int(id_m.group(1)) if id_m else None, "match_title": target_phrase},
                session,
                actions,
            )
            if res["status"] == "deleted":
                de = res["deleted_event"]
                return (
                    f"Removed **{de['title']}** (`{de['start_at'].replace('T', ' ')}`) from your Calendar.",
                    f"Executed `delete_calendar_event` -> deleted CalendarEvent id={de['id']}.",
                )
            return (
                f"I checked your Calendar, and I couldn't find an event matching **\"{target_phrase}\"** to delete.",
                f"Searched `calendar_events` for '{target_phrase}' — 0 matches.",
            )

        edit_m = re.match(
            r"^(?:please\s+)?(?:can you\s+)?(?:move|reschedule|shift|push|change|update|edit|shorten|extend|rename)\s+(?:the\s+|my\s+)?(.+)$",
            msg.strip(),
            re.IGNORECASE,
        )
        if edit_m and not any(w in lower for w in ("memory", "wallpaper", "theme", "accent", "setting")):
            body = edit_m.group(1)
            subj_m = re.split(r"\b(?:to|from|at|on|for)\b", body, maxsplit=1, flags=re.IGNORECASE)
            subj = re.sub(r"\b(?:event|calendar|block|appointment)\b", "", subj_m[0], flags=re.IGNORECASE).strip(" .\"'")
            id_m = re.search(r"#(\d+)\b", body)

            hm_pair, new_dur = _parse_natural_time_and_duration(msg, fallback_duration=0)
            has_date_word = any(
                w in lower
                for w in ("today", "tomorrow", *WEEKDAY_MAP.keys())
            ) or bool(re.search(r"\b\d{4}-\d{2}-\d{2}\b", lower))
            new_date_str = _parse_natural_date(msg).isoformat() if has_date_word else None

            new_title: Optional[str] = None
            ren_m = re.search(r"\brename\s+(?:the\s+|my\s+)?(.+?)\s+to\s+(.+)$", msg.strip(), re.IGNORECASE)
            if ren_m:
                subj = ren_m.group(1).strip(" .\"'")
                new_title = ren_m.group(2).strip(" .\"'")

            tool_args: Dict[str, Any] = {
                "event_id": int(id_m.group(1)) if id_m else None,
                "match_title": subj,
            }
            if new_title:
                tool_args["new_title"] = new_title
            if new_date_str:
                tool_args["new_date"] = new_date_str
            if hm_pair:
                tool_args["new_start_time"] = f"{hm_pair[0]:02d}:{hm_pair[1]:02d}"
            if new_dur:
                tool_args["duration_minutes"] = new_dur

            res = execute_secretary_tool("update_calendar_event", tool_args, session, actions)
            if res["status"] == "updated":
                ev = res["event"]
                prev = res["previous"]
                return (
                    f"Updated **{ev['title']}** on your Calendar:\n\n"
                    f"- **Previous:** `{prev['start_at'].replace('T', ' ')} – {prev['end_at'][-5:]}`\n"
                    f"- **New Time:** **`{ev['start_at'].replace('T', ' ')} – {ev['end_at'][-5:]}`**",
                    f"Executed `update_calendar_event` on CalendarEvent id={ev['id']}.",
                )
            return (
                f"I looked for **\"{subj}\"** on your Calendar to update it, but didn't find a matching event.",
                f"Attempted `update_calendar_event(match_title='{subj}')` — 0 matches.",
            )

        return None

    async def handle_chat(
        self,
        user_message: str,
        session: Session,
        thread_id: str = "main",
        client_host: Optional[str] = None,
        forwarded_for: Optional[str] = None,
        request_host: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        model_override: Optional[str] = None,
        existing_user_row: Optional[ChatMessage] = None,
    ) -> Dict[str, Any]:
        """Non-streaming chat handler (consumes `stream_chat` internally so behavior is 100% unified)."""
        final_payload: Optional[Dict[str, Any]] = None
        async for ev in self.stream_chat(
            user_message=user_message,
            session=session,
            thread_id=thread_id,
            client_host=client_host,
            forwarded_for=forwarded_for,
            request_host=request_host,
            enable_thinking=enable_thinking,
            model_override=model_override,
            existing_user_row=existing_user_row,
        ):
            if ev.get("type") == "done":
                final_payload = ev["result"]
        if final_payload is None:
            raise RuntimeError("Chat failed to finalize")
        return final_payload

    async def stream_chat(
        self,
        user_message: str,
        session: Session,
        thread_id: str = "main",
        client_host: Optional[str] = None,
        forwarded_for: Optional[str] = None,
        request_host: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        model_override: Optional[str] = None,
        existing_user_row: Optional[ChatMessage] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Real-time streaming chat & full-OS agent loop yielding NDJSON event dicts:
          - `{"type": "meta", "thread_id": ..., "user_message": ...}`
          - `{"type": "thinking_delta", "delta": "..."}`
          - `{"type": "tool_start", "name": "...", "arguments": {...}}`
          - `{"type": "tool_done", "name": "...", "status": "...", "actions": [...]}`
          - `{"type": "content_delta", "delta": "..."}`
          - `{"type": "done", "result": {...}}`
        """
        clean_thread = (thread_id or "main").strip()[:80] or "main"
        msg = user_message.strip()
        lower = msg.lower()
        actions: List[Dict[str, Any]] = []

        prior_q = select(ChatMessage).where(ChatMessage.thread_id == clean_thread)
        if existing_user_row is not None and existing_user_row.id is not None:
            prior_q = prior_q.where(ChatMessage.id < existing_user_row.id)
        prior_rows = list(session.exec(prior_q.order_by(ChatMessage.id.desc()).limit(10)).all())
        prior_rows.reverse()

        if existing_user_row is not None:
            user_row = existing_user_row
        else:
            user_row = ChatMessage(
                thread_id=clean_thread,
                role="user",
                content=msg,
                source="user",
                actions_json="[]",
            )
            session.add(user_row)
            session.commit()
            session.refresh(user_row)

        yield {
            "type": "meta",
            "thread_id": clean_thread,
            "user_message": {
                "id": user_row.id,
                "thread_id": clean_thread,
                "role": "user",
                "content": user_row.content,
                "actions": [],
                "thinking": None,
                "source": "user",
                "created_at": user_row.created_at.isoformat(),
            },
        }

        ctx = self._gather_context(session)
        user_name = _extract_user_name_from_memory(ctx["memory"])
        name_tag = f", {user_name}" if user_name else ""

        # 0. Duration learning feedback shortcut
        learned_fb = _detect_duration_learning_feedback(msg)
        if learned_fb:
            learn_res = memory_store.learn_duration_preference(
                target_archetype=learned_fb["archetype"],
                minutes=learned_fb["minutes"],
                raw_user_feedback=msg,
                custom_keyword=learned_fb["custom_keyword"],
                also_calibrate_related=True,
            )
            if learned_fb.get("secondary_email_minutes"):
                memory_store.learn_duration_preference(
                    target_archetype="email_triage",
                    minutes=learned_fb["secondary_email_minutes"],
                    raw_user_feedback=msg,
                    also_calibrate_related=False,
                )
            resized = recalibrate_scheduled_blocks(session)
            thinking_trace = (
                f"1. Updated `/data/notebook/time_estimates.md` ({learned_fb['minutes']}m).\n"
                f"2. Recalibrated {len(resized)} AI-scheduled calendar block(s)."
            )
            yield {"type": "thinking_delta", "delta": thinking_trace}
            actions.append(
                {
                    "type": "notebook_updated",
                    "label": "Open time_estimates.md",
                    "view": "memory",
                    "subtab": "notes",
                    "notebook_slug": "time_estimates",
                }
            )
            if resized:
                actions.append(
                    {
                        "type": "calendar_recalibrated",
                        "label": f"Calendar ({len(resized)} resized)",
                        "view": "calendar",
                    }
                )
            updates_bullets = "\n".join(f"- {u}" for u in learn_res["updates_applied"])
            resized_md = (
                "\n\n**Resized Calendar Blocks:**\n"
                + "\n".join(
                    f"- **{r['title']}**: `{r['old_minutes']}m` → **`{r['new_minutes']}m`** (`{r['start_at'][-5:]}–{r['end_at'][-5:]}`)"
                    for r in resized
                )
                if resized
                else ""
            )
            reply = f"Saved to **`time_estimates.md`**{name_tag}:\n\n{updates_bullets}{resized_md}"
            yield {"type": "content_delta", "delta": reply}
            fin = self._finalize(session, user_row, reply, actions, "secretary_engine", clean_thread, thinking=thinking_trace)
            yield {"type": "done", **fin, "result": fin}
            return

        # 1. Create dedicated Notebook file shortcut
        new_file_m = re.match(
            r"^(?:create|make|start)\s+(?:a\s+)?(?:new\s+)?(?:note|notebook|topic)?\s*file\s+(?:called|named|for)\s+([A-Za-z0-9 _-]+?)(?:\s*[:\-]\s*(.+))?$",
            msg,
            re.IGNORECASE | re.DOTALL,
        )
        if new_file_m:
            raw_slug = new_file_m.group(1).strip()
            initial_note = (new_file_m.group(2) or "").strip()
            pretty = raw_slug.replace("_", " ").strip().title()
            body = f"# {pretty}\n\n## Notes\n"
            if initial_note:
                stamp = get_local_now().strftime("%Y-%m-%d %H:%M")
                body += f"- **[{stamp}]** — {initial_note}\n"
            saved_doc = memory_store.save_notebook_file(raw_slug, body, title=pretty)
            actions.append(
                {
                    "type": "notebook_created",
                    "label": f"Open {saved_doc['filename']}",
                    "view": "memory",
                    "subtab": "notes",
                    "notebook_slug": saved_doc["slug"],
                }
            )
            reply = f"Created **`{saved_doc['filename']}`**{name_tag}." + (f"\n\n> {initial_note}" if initial_note else "")
            yield {"type": "content_delta", "delta": reply}
            fin = self._finalize(session, user_row, reply, actions, "secretary_engine", clean_thread, thinking=f"Created `{saved_doc['filename']}`.")
            yield {"type": "done", **fin, "result": fin}
            return

        # 1.5 Decision-gate tool planning: attach ONLY the tools this turn needs.
        # A 2B model forgets 33 tool signatures; it does not forget 4. The gate
        # also extracts typed slots so fields get filled instead of glued into a title.
        active_tools = SECRETARY_TOOLS
        tool_plan: Optional[Dict[str, Any]] = None
        try:
            from .decision_gate import plan_tools as _plan_tools

            tool_plan = await _plan_tools(
                msg,
                tool_names=[t["function"]["name"] for t in SECRETARY_TOOLS],
                calendar_table=build_14_day_calendar_table(get_local_now()),
                today_iso=get_local_now().date().isoformat(),
            )
        except Exception:
            tool_plan = None

        if tool_plan and tool_plan.get("groups"):
            allowed = set(tool_plan.get("tools") or [])
            # Always keep the read-only orientation tools available — they are
            # cheap and prevent the model from guessing at dates or ids.
            if tool_plan.get("intent") in ("create", "update", "delete", "settings"):
                allowed.add("get_os_overview")
            narrowed = [t for t in SECRETARY_TOOLS if t["function"]["name"] in allowed]
            if narrowed:
                active_tools = narrowed
            yield {
                "type": "thinking_delta",
                "delta": f"\n**Decision gate** routed intent `{tool_plan.get('intent')}` → "
                f"{len(active_tools)}/{len(SECRETARY_TOOLS)} tools: "
                f"{', '.join(t['function']['name'] for t in active_tools)}",
            }
        elif tool_plan is not None and not tool_plan.get("groups"):
            # No action needed: give the model only the orientation tool so it
            # answers instead of inventing a tool call.
            active_tools = [t for t in SECRETARY_TOOLS if t["function"]["name"] == "get_os_overview"]

        slots = (tool_plan or {}).get("slots") or {}
        if slots.get("title") or slots.get("date"):
            msg = self._apply_gate_slots(msg, slots, actions, session)

        # 2. Streaming Multi-Turn Tool-Calling Loop (Multi-Provider Ollama / Remote Ollama / Cloud API)
        messages = self._build_agent_messages(msg, ctx, prior_rows, tool_plan=tool_plan)
        thinking_parts: List[str] = []
        executed_tools: List[str] = []
        llm_final_text: Optional[str] = None
        ollama_used = False
        used_model_label = model_override or ollama_client.model or "auto"

        for round_idx in range(4):
            turn_complete: Optional[Dict[str, Any]] = None
            async for chunk_ev in ollama_client.stream_chat_with_tools(
                messages=messages,
                tools=active_tools,
                enable_thinking=enable_thinking,
                num_predict=1000,
                model_override=model_override,
            ):
                etype = chunk_ev.get("type")
                if etype == "unreachable":
                    turn_complete = None
                    break
                ollama_used = True
                if etype == "thinking_delta":
                    yield chunk_ev
                elif etype == "content_delta":
                    yield chunk_ev
                elif etype == "turn_complete":
                    turn_complete = chunk_ev
                    if chunk_ev.get("model"):
                        used_model_label = chunk_ev["model"]

            if turn_complete is None:
                break

            if turn_complete.get("thinking"):
                thinking_parts.append(turn_complete["thinking"])

            tool_calls = turn_complete.get("tool_calls") or []
            if not tool_calls:
                llm_final_text = turn_complete.get("content") or ""
                break

            # Execute requested OS tools for real!
            messages.append(
                {
                    "role": "assistant",
                    "content": turn_complete.get("content") or "",
                    "tool_calls": [
                        {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                t_name = tc["name"]
                t_args = tc.get("arguments") or {}
                yield {"type": "tool_start", "name": t_name, "arguments": t_args}
                t_result = await execute_secretary_tool_async(
                    t_name,
                    t_args,
                    session,
                    actions,
                    user_message_context=msg,
                    active_model_id=used_model_label,
                )
                status_str = str(t_result.get("status") or "ok")
                executed_tools.append(f"`{t_name}({json.dumps(t_args)})` → `{status_str}`")
                yield {
                    "type": "tool_done",
                    "name": t_name,
                    "status": status_str,
                    "actions": list(actions),
                }
                messages.append(
                    {
                        "role": "tool",
                        "name": t_name,
                        "content": json.dumps(t_result),
                    }
                )
            if round_idx < 3:
                yield {"type": "thinking_delta", "delta": "\n"}

        if ollama_used:
            # Anti-hallucination safety net: if the user explicitly asked for a mutation
            # (create/move/delete event, add/complete task, monitor site, mark read, etc.)
            # and the small 2B model emitted 0 tool calls, execute the real mutation now!
            if not executed_tools:
                det_fallback = await self._try_deterministic_intent_fallback(msg, ctx, session, actions)
                if det_fallback:
                    det_reply, det_think = det_fallback
                    thinking_parts.append(det_think)
                    yield {"type": "content_replace", "content": det_reply}
                    fin = self._finalize(
                        session,
                        user_row,
                        det_reply,
                        actions,
                        "ollama",
                        clean_thread,
                        thinking="\n\n".join(thinking_parts),
                    )
                    yield {"type": "done", **fin, "result": fin}
                    return

            if executed_tools:
                thinking_parts.insert(0, "Executed OS Tools:\n- " + "\n- ".join(executed_tools))
            final_md = (llm_final_text or "").strip() or (
                "Done — applied your requested changes:\n\n- " + "\n- ".join(a["label"] for a in actions)
                if actions
                else "Checked your workspace."
            )
            if not (llm_final_text or "").strip():
                yield {"type": "content_delta", "delta": final_md}
            fin = self._finalize(
                session,
                user_row,
                final_md,
                actions,
                "ollama",
                clean_thread,
                thinking="\n\n".join(thinking_parts) if thinking_parts else f"Thread `{clean_thread}` • {ollama_client.model}",
            )
            yield {"type": "done", **fin, "result": fin}
            return

        # ------------------------------------------------------------------
        # 3. Offline / Deterministic Engine (100% Real DB & OS Mutations!)
        # ------------------------------------------------------------------
        det_any = await self._try_deterministic_intent_fallback(msg, ctx, session, actions)
        if det_any:
            det_reply, det_think = det_any
            yield {"type": "thinking_delta", "delta": det_think}
            yield {"type": "content_delta", "delta": det_reply}
            fin = self._finalize(session, user_row, det_reply, actions, "secretary_engine", clean_thread, thinking=det_think)
            yield {"type": "done", **fin, "result": fin}
            return

        tref = ctx["time_ref"]
        if any(k in lower for k in ("what time", "what day", "today's date", "current date", "current time")):
            reply = f"It is **{tref['current_time_12h']}** on **{tref['weekday']}, {tref['today_date']}** (`{tref['timezone']}`)."
            yield {"type": "content_delta", "delta": reply}
            fin = self._finalize(session, user_row, reply, actions, "secretary_engine", clean_thread)
            yield {"type": "done", **fin, "result": fin}
            return

        if any(kw in lower for kw in ("pull up", "open ", "go to ", "switch to ", "show me ", "show my ")):
            if any(k in lower for k in ("diagnostic", "tailscale", "health", "ollama", "system status")):
                ts = probe_tailscale(client_host=client_host, forwarded_for=forwarded_for, request_host=request_host)
                ol = await ollama_client.check_status()
                hbs = list(session.exec(select(ScraperHeartbeat)).all())
                sc = probe_scraper_environment(hbs)
                actions.append({"type": "navigate", "label": "System Settings", "view": "settings", "auto": True})
                reply = (
                    f"- **Tailscale:** {ts['detail']}\n"
                    f"- **Ollama (`{ol.configured_model}`):** {ol.detail}\n"
                    f"- **Server Browser:** {sc['detail']}"
                )
                yield {"type": "content_delta", "delta": reply}
                fin = self._finalize(session, user_row, reply, actions, "secretary_engine", clean_thread)
                yield {"type": "done", **fin, "result": fin}
                return

            for view_kw, view_id in (
                ("grade", "grades"),
                ("calendar", "calendar"),
                ("schedule", "calendar"),
                ("task", "tasks"),
                ("inbox", "inbox"),
                ("notification", "inbox"),
                ("monitor", "apps"),
                ("file", "files"),
                ("note", "memory"),
                ("setting", "settings"),
            ):
                if view_kw in lower:
                    execute_secretary_tool("open_os_window", {"view": view_id}, session, actions)
                    reply = f"Opened **{view_id.title()}**."
                    yield {"type": "content_delta", "delta": reply}
                    fin = self._finalize(session, user_row, reply, actions, "secretary_engine", clean_thread)
                    yield {"type": "done", **fin, "result": fin}
                    return

        # Crisp live OS summary fallback when Ollama is offline
        free_wins = find_free_windows_on_date(ctx["events_today"], ctx["today_str"])
        next_free = (
            f"`{free_wins[0][0].strftime('%H:%M')}–{free_wins[0][1].strftime('%H:%M')}`"
            if free_wins
            else "evening"
        )
        parts = [
            f"**{tref['weekday']} {tref['today_date']} • {tref['current_time_12h']}**",
            f"- **Calendar Today:** {len(ctx['events_today'])} event(s) • Next open slot: {next_free}",
            f"- **Open Tasks:** {len(ctx['tasks'])} • **Unread Inbox:** {len(ctx['notifications'])} • **Tracked Courses:** {len(ctx['courses'])}",
        ]
        if ctx["events_today"]:
            parts.append(
                "- **Today's Blocks:** "
                + ", ".join(f"`{e.start_at[-5:]}` {e.title}" for e in ctx["events_today"][:4])
            )
        reply = "\n".join(parts)
        offline_think = f"Thread `{clean_thread}` • Local Ollama ({ollama_client.model}) offline; synthesized live OS snapshot."
        yield {"type": "thinking_delta", "delta": offline_think}
        yield {"type": "content_delta", "delta": reply}
        fin = self._finalize(
            session,
            user_row,
            reply,
            actions,
            "secretary_engine",
            clean_thread,
            thinking=offline_think,
        )
        yield {"type": "done", **fin, "result": fin}

    def _finalize(
        self,
        session: Session,
        user_row: ChatMessage,
        reply: str,
        actions: List[Dict[str, Any]],
        source: str,
        thread_id: str = "main",
        thinking: Optional[str] = None,
    ) -> Dict[str, Any]:
        meta_actions = list(actions)
        stored_payload = (
            json.dumps({"actions": meta_actions, "thinking": thinking})
            if thinking
            else json.dumps(meta_actions)
        )

        asst_row = ChatMessage(
            thread_id=thread_id,
            role="assistant",
            content=reply,
            actions_json=stored_payload,
            source=source,
        )
        session.add(asst_row)

        now_utc = datetime.now(timezone.utc)
        th = session.get(ChatThread, thread_id)
        if not th:
            existing_names = {t.name for t in session.exec(select(ChatThread)).all()}
            n = 1
            while f"Chat {n}" in existing_names:
                n += 1
            th = ChatThread(
                thread_id=thread_id,
                name="Chat 1" if thread_id == "main" and "Chat 1" not in existing_names else f"Chat {n}",
                created_at=now_utc,
                updated_at=now_utc,
            )
        else:
            th.updated_at = now_utc
        session.add(th)

        session.commit()
        session.refresh(asst_row)

        if meta_actions:
            action_labels = ", ".join(a.get("label", a.get("type", "action")) for a in meta_actions[:6])
            agent_logbook.auto_record_event(
                session,
                agent_name=f"chat:{thread_id}",
                model_id=ollama_client.model if source == "ollama" else source,
                category="os-mutation",
                title=f"Executed {len(meta_actions)} OS action(s) in `{thread_id}`",
                content=(
                    f"Prompt: {user_row.content[:220]}\n"
                    f"Actions: {action_labels}\n"
                    f"Outcome: {reply[:280]}"
                ),
                tags="auto-telemetry,chat-mutation",
            )

        return {
            "thread_id": thread_id,
            "user_message": {
                "id": user_row.id,
                "thread_id": thread_id,
                "role": "user",
                "content": user_row.content,
                "actions": [],
                "thinking": None,
                "source": "user",
                "created_at": user_row.created_at.isoformat(),
            },
            "assistant_message": {
                "id": asst_row.id,
                "thread_id": thread_id,
                "role": "assistant",
                "content": asst_row.content,
                "actions": meta_actions,
                "thinking": thinking,
                "source": source,
                "created_at": asst_row.created_at.isoformat(),
            },
            "reply_markdown": reply,
            "thinking": thinking,
            "actions": meta_actions,
            "engine": source,
            "ollama_connected": source == "ollama",
        }


secretary_service = SecretaryService()
