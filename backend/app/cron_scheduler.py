from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .memory_store import memory_store
from .models import CalendarEvent, CronRunLog, Notification, Task, utc_now
from .ollama_client import ollama_client

logger = logging.getLogger("node.cron")

DEFAULT_SCHEDULE_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 60,
    "bedtime_hhmm": "22:00",
    "wake_hhmm": "07:00",
    "sleep_buffer_minutes": 15,
    "last_seen_notification_id": 0,
    "last_run_iso": None,
}


def _get_local_tz() -> ZoneInfo:
    tz_name = os.environ.get("TZ") or settings.local_timezone or "America/New_York"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _parse_hhmm_strict(raw: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(raw or ""))
    if not m:
        return fallback
    return int(m.group(1)), int(m.group(2))


def _fmt_hhmm_12h(hhmm: str) -> str:
    h, m = _parse_hhmm_strict(hhmm, (22, 0))
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


class HourlyAICronScheduler:
    """
    Hourly + on-demand AI schedule & priority coordinator:
      - Checks incoming unread notifications & newly added tasks every hour (or manually)
      - Computes real free windows between `now` and configured `bedtime` around Calendar events
      - Schedules due-tonight homework FIRST and pushes back less-urgent work when evening time is tight
      - Computes real discretionary YouTube time allowance for YouTube Focus Guard
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()

    @property
    def settings_path(self) -> Path:
        p = Path(settings.schedule_settings_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def load_settings(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_SCHEDULE_SETTINGS)
        if self.settings_path.exists():
            try:
                loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception:
                pass
        bh, bm = _parse_hhmm_strict(cfg.get("bedtime_hhmm", "22:00"), (22, 0))
        wh, wm = _parse_hhmm_strict(cfg.get("wake_hhmm", "07:00"), (7, 0))
        cfg["bedtime_hhmm"] = f"{bh:02d}:{bm:02d}"
        cfg["wake_hhmm"] = f"{wh:02d}:{wm:02d}"
        cfg["interval_minutes"] = max(5, min(360, int(cfg.get("interval_minutes") or 60)))
        cfg["sleep_buffer_minutes"] = max(0, min(90, int(cfg.get("sleep_buffer_minutes") or 15)))
        cfg["enabled"] = bool(cfg.get("enabled", True))
        return cfg

    def save_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.load_settings()
        for k, v in updates.items():
            if v is not None and k in DEFAULT_SCHEDULE_SETTINGS:
                cfg[k] = v
        bh, bm = _parse_hhmm_strict(cfg.get("bedtime_hhmm", "22:00"), (22, 0))
        wh, wm = _parse_hhmm_strict(cfg.get("wake_hhmm", "07:00"), (7, 0))
        cfg["bedtime_hhmm"] = f"{bh:02d}:{bm:02d}"
        cfg["wake_hhmm"] = f"{wh:02d}:{wm:02d}"
        cfg["interval_minutes"] = max(5, min(360, int(cfg.get("interval_minutes") or 60)))
        cfg["sleep_buffer_minutes"] = max(0, min(90, int(cfg.get("sleep_buffer_minutes") or 15)))
        cfg["enabled"] = bool(cfg.get("enabled", True))
        self.settings_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg

    def _resolve_local_now(self, reference_now_iso: Optional[str] = None) -> datetime:
        tz = _get_local_tz()
        if reference_now_iso:
            s = reference_now_iso.strip().replace(" ", "T")
            try:
                parsed = datetime.fromisoformat(s.rstrip("Z"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=tz)
                return parsed.astimezone(tz)
            except Exception:
                pass
        return datetime.now(tz)

    def _compute_pre_bedtime_windows(
        self,
        session: Session,
        local_now: datetime,
        cfg: Dict[str, Any],
    ) -> Tuple[List[Tuple[datetime, datetime]], int, datetime]:
        tz = local_now.tzinfo or _get_local_tz()
        today = local_now.date()
        bh, bm = _parse_hhmm_strict(cfg["bedtime_hhmm"], (22, 0))
        wh, wm = _parse_hhmm_strict(cfg["wake_hhmm"], (7, 0))

        wake_dt = datetime.combine(today, dtime(wh, wm), tzinfo=tz)
        bed_dt = datetime.combine(today, dtime(bh, bm), tzinfo=tz)
        if bed_dt <= wake_dt:
            bed_dt += timedelta(days=1)

        if local_now >= bed_dt:
            return [], 0, bed_dt

        window_start = max(local_now, wake_dt)
        # Round start up to nearest 5 minutes for clean scheduling blocks
        rem = window_start.minute % 5
        if rem:
            window_start = (window_start + timedelta(minutes=(5 - rem))).replace(second=0, microsecond=0)
        else:
            window_start = window_start.replace(second=0, microsecond=0)

        if window_start >= bed_dt:
            return [], 0, bed_dt

        day_prefix = today.isoformat()
        events = list(
            session.exec(
                select(CalendarEvent).where(CalendarEvent.start_at >= f"{day_prefix}T00:00")
            ).all()
        )

        busy: List[Tuple[datetime, datetime]] = []
        for ev in events:
            # Ignore previously auto-placed AI Cron blocks so recalculation uses real obligations
            if (ev.source or "") == "AI Cron":
                continue
            if ev.all_day:
                continue
            if not str(ev.start_at or "").startswith(day_prefix):
                continue
            try:
                es = datetime.strptime(ev.start_at[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
                ee = datetime.strptime(ev.end_at[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
                if ee > window_start and es < bed_dt:
                    busy.append((max(window_start, es), min(bed_dt, ee)))
            except Exception:
                continue

        busy.sort(key=lambda pair: pair[0])
        merged: List[Tuple[datetime, datetime]] = []
        for bs, be in busy:
            if not merged or bs > merged[-1][1]:
                merged.append((bs, be))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], be))

        free_slots: List[Tuple[datetime, datetime]] = []
        cursor = window_start
        for bs, be in merged:
            if bs > cursor and int((bs - cursor).total_seconds() // 60) >= 10:
                free_slots.append((cursor, bs))
            cursor = max(cursor, be)
        if bed_dt > cursor and int((bed_dt - cursor).total_seconds() // 60) >= 10:
            free_slots.append((cursor, bed_dt))

        total_free = sum(int((fe - fs).total_seconds() // 60) for fs, fe in free_slots)
        return free_slots, total_free, bed_dt

    def _classify_tasks(
        self,
        tasks: List[Task],
        today_iso: str,
        tomorrow_iso: str,
    ) -> Tuple[List[Tuple[Task, int]], List[Tuple[Task, int]], List[Tuple[Task, int]]]:
        due_tonight: List[Tuple[Task, int]] = []
        due_tomorrow: List[Tuple[Task, int]] = []
        less_urgent: List[Tuple[Task, int]] = []

        for t in tasks:
            est = t.estimated_minutes
            if not est or est <= 0:
                est, _ = memory_store.estimate_task_minutes(t.title, t.category, t.source_app)
            est = max(10, min(240, int(est)))

            title_low = (t.title or "").lower()
            due = (t.due_date or "").strip()[:10]
            mentions_tonight = any(k in title_low for k in ("due tonight", "tonight", "due today"))

            if (due and due <= today_iso) or mentions_tonight:
                due_tonight.append((t, est))
            elif due == tomorrow_iso:
                due_tomorrow.append((t, est))
            else:
                less_urgent.append((t, est))

        return due_tonight, due_tomorrow, less_urgent

    def compute_live_budget(
        self,
        session: Session,
        reference_now_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg = self.load_settings()
        local_now = self._resolve_local_now(reference_now_iso)
        today_iso = local_now.date().isoformat()
        tomorrow_iso = (local_now.date() + timedelta(days=1)).isoformat()

        free_slots, free_mins, bed_dt = self._compute_pre_bedtime_windows(session, local_now, cfg)
        open_tasks = list(
            session.exec(select(Task).where(Task.completed == False).order_by(Task.id.asc())).all()  # noqa: E712
        )
        due_tonight, due_tomorrow, less_urgent = self._classify_tasks(open_tasks, today_iso, tomorrow_iso)

        due_tonight_mins = sum(m for _, m in due_tonight)
        due_tomorrow_mins = sum(m for _, m in due_tomorrow)
        later_mins = sum(m for _, m in less_urgent)

        sleep_buf = int(cfg.get("sleep_buffer_minutes") or 15)
        usable_mins = max(0, free_mins - sleep_buf) if free_mins > 0 else 0

        # Discretionary YouTube budget accounts for tonight's mandatory due homework first
        raw_slack = usable_mins - due_tonight_mins
        # Reserve up to half of due-tomorrow prep before counting unlimited entertainment slack
        tomorrow_reserve = min(45, due_tomorrow_mins)
        youtube_slack_mins = max(0, raw_slack - tomorrow_reserve)

        if local_now >= bed_dt:
            status_code = "past_bedtime"
            youtube_allowed = False
            youtube_slack_mins = 0
        elif due_tonight_mins > usable_mins:
            status_code = "behind"
            youtube_allowed = False
            youtube_slack_mins = 0
        elif due_tonight_mins > 0 and raw_slack <= 30:
            status_code = "tight"
            youtube_allowed = False
            youtube_slack_mins = max(0, raw_slack)
        else:
            status_code = "on_track"
            youtube_allowed = youtube_slack_mins >= 10 or (due_tonight_mins == 0 and usable_mins >= 15)

        return {
            "now_local": local_now.strftime("%Y-%m-%d %H:%M"),
            "today_date": today_iso,
            "bedtime_hhmm": cfg["bedtime_hhmm"],
            "bedtime_label": _fmt_hhmm_12h(cfg["bedtime_hhmm"]),
            "free_windows": [
                {
                    "start": fs.strftime("%H:%M"),
                    "end": fe.strftime("%H:%M"),
                    "minutes": int((fe - fs).total_seconds() // 60),
                }
                for fs, fe in free_slots
            ],
            "free_minutes_until_bedtime": free_mins,
            "usable_minutes_before_bedtime": usable_mins,
            "due_tonight_count": len(due_tonight),
            "due_tonight_minutes": due_tonight_mins,
            "due_tomorrow_count": len(due_tomorrow),
            "less_urgent_count": len(less_urgent),
            "total_open_minutes": due_tonight_mins + due_tomorrow_mins + later_mins,
            "youtube_slack_minutes": youtube_slack_mins,
            "youtube_allowed": youtube_allowed,
            "on_schedule_status": status_code,
        }

    async def run_cron_check(
        self,
        session: Session,
        trigger: str = "hourly_cron",
        reference_now_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._run_lock:
            cfg = self.load_settings()
            local_now = self._resolve_local_now(reference_now_iso)
            today_iso = local_now.date().isoformat()
            tomorrow_iso = (local_now.date() + timedelta(days=1)).isoformat()

            free_slots, free_mins, bed_dt = self._compute_pre_bedtime_windows(session, local_now, cfg)
            sleep_buf = int(cfg.get("sleep_buffer_minutes") or 15)
            effective_bed_cutoff = bed_dt - timedelta(minutes=sleep_buf) if free_mins > sleep_buf else bed_dt

            # 0. 24/7 Background Sync: scan grades, AI-tag unprocessed Inbox messages, and sync Schoology/Calendar assignments!
            try:
                from .browser_stream import browser_stream_manager
                from .task_intelligence import sync_tasks_from_calendar_and_inbox

                await browser_stream_manager.scan_grades_tabs(reload_idle_tabs=True)
                await browser_stream_manager.process_notifications_with_ai(limit=25)
                await sync_tasks_from_calendar_and_inbox(
                    session=session,
                    include_calendar=True,
                    include_inbox=False,
                    days_ahead=14,
                    today_iso=today_iso,
                )
                from .self_improvement import self_improvement_service

                await self_improvement_service.reflect_and_self_improve(
                    session,
                    force=False,
                    trigger=trigger,
                )
            except Exception as exc:
                logger.debug("Cron background sync note: %s", exc)

            # 1. Inspect new unread notifications since last cron pass
            last_seen_notif = int(cfg.get("last_seen_notification_id") or 0)
            unread_notifs = list(
                session.exec(
                    select(Notification)
                    .where(Notification.status == "active", Notification.read_state == False)  # noqa: E712
                    .order_by(Notification.id.desc())
                ).all()
            )
            new_notifs = [n for n in unread_notifs if (n.id or 0) > last_seen_notif]
            max_notif_id = max([last_seen_notif] + [n.id or 0 for n in unread_notifs])

            # 2. Clear stale uncompleted AI Cron blocks from today so tonight's slots repack cleanly
            old_cron_events = list(
                session.exec(
                    select(CalendarEvent).where(
                        CalendarEvent.source == "AI Cron",
                        CalendarEvent.completed == False,  # noqa: E712
                        CalendarEvent.start_at >= f"{today_iso}T00:00",
                        CalendarEvent.start_at <= f"{today_iso}T23:59",
                    )
                ).all()
            )
            for old_ev in old_cron_events:
                session.delete(old_ev)

            # 3. Prioritize due-tonight tasks FIRST into pre-bedtime free windows; push back less-urgent ones
            open_tasks = list(
                session.exec(select(Task).where(Task.completed == False).order_by(Task.id.asc())).all()  # noqa: E712
            )
            due_tonight, due_tomorrow, less_urgent = self._classify_tasks(open_tasks, today_iso, tomorrow_iso)

            mutable_slots: List[List[datetime]] = []
            for fs, fe in free_slots:
                capped_end = min(fe, effective_bed_cutoff) if fe > effective_bed_cutoff and fs < effective_bed_cutoff else fe
                if capped_end > fs:
                    mutable_slots.append([fs, capped_end])

            prioritized_tonight: List[Dict[str, Any]] = []
            deferred_items: List[Dict[str, Any]] = []
            scheduled_mins = 0

            def _allocate_slot(minutes_needed: int) -> Optional[Tuple[datetime, datetime]]:
                for slot in mutable_slots:
                    avail = int((slot[1] - slot[0]).total_seconds() // 60)
                    if avail >= 15:
                        take = min(minutes_needed, avail)
                        st = slot[0]
                        en = st + timedelta(minutes=take)
                        slot[0] = en + timedelta(minutes=5)  # 5m breather between tasks
                        return st, en
                return None

            # Tier 1: Homework / tasks due tonight MUST go first!
            for task, est_m in due_tonight:
                task.estimated_minutes = est_m
                task.priority = "critical"
                task.deferred_from_date = None
                alloc = _allocate_slot(est_m)
                if alloc:
                    st, en = alloc
                    span = f"{st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
                    dur = int((en - st).total_seconds() // 60)
                    scheduled_mins += dur
                    task.scheduled_block = f"Tonight {span}"
                    session.add(
                        CalendarEvent(
                            title=f"[Due Tonight] {task.title}"[:280],
                            start_at=st.strftime("%Y-%m-%dT%H:%M"),
                            end_at=en.strftime("%Y-%m-%dT%H:%M"),
                            all_day=False,
                            category="Focus Block",
                            color="rose",
                            notes=f"Scheduled first before {_fmt_hhmm_12h(cfg['bedtime_hhmm'])} bedtime by Hourly AI Cron.",
                            source="AI Cron",
                            linked_task_id=task.id,
                        )
                    )
                    prioritized_tonight.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "slot": span,
                            "minutes": dur,
                            "reason": "Due tonight — scheduled first before bedtime",
                        }
                    )
                else:
                    task.scheduled_block = "Urgent tonight (over capacity!)"
                    prioritized_tonight.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "slot": "ASAP (exceeds bedtime window)",
                            "minutes": est_m,
                            "reason": "Due tonight — exceeds remaining pre-bedtime window",
                        }
                    )
                session.add(task)

            # Tier 2 & 3: Fit due-tomorrow / less-urgent tasks ONLY if comfortable free time remains before bedtime;
            # otherwise push them back so tonight stays focused on due-tonight work & sleep!
            for tier_label, group in (("due_tomorrow", due_tomorrow), ("less_urgent", less_urgent)):
                for task, est_m in group:
                    task.estimated_minutes = est_m
                    # Check remaining slot capacity
                    rem_slot_mins = sum(
                        max(0, int((s[1] - s[0]).total_seconds() // 60)) for s in mutable_slots
                    )
                    if rem_slot_mins >= est_m and local_now < bed_dt:
                        alloc = _allocate_slot(est_m)
                        if alloc:
                            st, en = alloc
                            span = f"{st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
                            dur = int((en - st).total_seconds() // 60)
                            scheduled_mins += dur
                            task.priority = "high" if tier_label == "due_tomorrow" else "normal"
                            task.scheduled_block = f"Tonight {span}"
                            task.deferred_from_date = None
                            session.add(
                                CalendarEvent(
                                    title=task.title[:280],
                                    start_at=st.strftime("%Y-%m-%dT%H:%M"),
                                    end_at=en.strftime("%Y-%m-%dT%H:%M"),
                                    all_day=False,
                                    category="Focus Block",
                                    color="amber",
                                    notes="Scheduled by Hourly AI Cron in remaining pre-bedtime window.",
                                    source="AI Cron",
                                    linked_task_id=task.id,
                                )
                            )
                            prioritized_tonight.append(
                                {
                                    "id": task.id,
                                    "title": task.title,
                                    "slot": span,
                                    "minutes": dur,
                                    "reason": "Fits comfortably before bedtime",
                                }
                            )
                            session.add(task)
                            continue

                    # Push back less-urgent task to protect tonight's homework + bedtime!
                    task.priority = "low" if tier_label == "less_urgent" else "normal"
                    task.deferred_from_date = today_iso
                    if not task.due_date:
                        task.due_date = tomorrow_iso
                    task.scheduled_block = f"Pushed to {tomorrow_iso}"
                    session.add(task)
                    deferred_items.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "minutes": est_m,
                            "pushed_to": task.due_date or tomorrow_iso,
                            "reason": f"Pushed back to protect due-tonight work & {_fmt_hhmm_12h(cfg['bedtime_hhmm'])} bedtime",
                        }
                    )

            session.commit()

            budget = self.compute_live_budget(session, reference_now_iso=reference_now_iso)
            ai_summary = await self._generate_cron_summary(
                budget=budget,
                prioritized=prioritized_tonight,
                deferred=deferred_items,
                new_notifs=new_notifs,
                trigger=trigger,
            )

            log_row = CronRunLog(
                ran_at=utc_now(),
                trigger=trigger,
                bedtime_hhmm=cfg["bedtime_hhmm"],
                free_minutes_until_bedtime=budget["free_minutes_until_bedtime"],
                due_tonight_minutes=budget["due_tonight_minutes"],
                scheduled_tonight_minutes=scheduled_mins,
                youtube_slack_minutes=budget["youtube_slack_minutes"],
                youtube_allowed=budget["youtube_allowed"],
                on_schedule_status=budget["on_schedule_status"],
                new_notifications_count=len(new_notifs),
                prioritized_tasks_json=json.dumps(prioritized_tonight),
                deferred_tasks_json=json.dumps(deferred_items),
                ai_summary=ai_summary,
            )
            session.add(log_row)
            session.commit()
            session.refresh(log_row)

            self.save_settings(
                {
                    "last_seen_notification_id": max_notif_id,
                    "last_run_iso": datetime.now(_get_local_tz()).isoformat(timespec="seconds"),
                }
            )

            return self.get_status(session, reference_now_iso=reference_now_iso)

    async def _generate_cron_summary(
        self,
        budget: Dict[str, Any],
        prioritized: List[Dict[str, Any]],
        deferred: List[Dict[str, Any]],
        new_notifs: List[Notification],
        trigger: str,
    ) -> str:
        # Crisp deterministic baseline summary (always accurate on exact time math)
        bed_lbl = budget["bedtime_label"]
        free_m = budget["free_minutes_until_bedtime"]
        yt_m = budget["youtube_slack_minutes"]
        st = budget["on_schedule_status"]

        parts: List[str] = []
        if st == "past_bedtime":
            parts.append(f"Past your {bed_lbl} bedtime — wind down for sleep (YouTube budget: 0m).")
        elif prioritized:
            first_items = ", ".join(f"{p['title']} ({p['slot']})" for p in prioritized[:3])
            parts.append(f"Prioritized before {bed_lbl} bedtime ({free_m}m free): {first_items}.")
        else:
            parts.append(f"Clear evening before {bed_lbl} bedtime ({free_m}m free).")

        if deferred:
            pushed_names = ", ".join(d["title"] for d in deferred[:3])
            parts.append(f"Pushed back {len(deferred)} less-urgent task(s) ({pushed_names}) so tonight stays realistic.")

        if new_notifs:
            parts.append(f"Checked {len(new_notifs)} new incoming notification(s).")

        if budget["youtube_allowed"] and yt_m > 0:
            parts.append(f"YouTube allowance: ~{yt_m}m of guilt-free slack available before {bed_lbl}.")
        else:
            parts.append("YouTube locked to strict accountability until tonight's priority work is finished.")

        fallback_text = " ".join(parts)

        # Enhance via local Qwen (`qwen3.5:2b`) if reachable, keeping exact facts grounded
        try:
            st_ollama = await ollama_client.check_status()
            if st_ollama.reachable and st_ollama.model_installed:
                prompt = (
                    f"Trigger: {trigger}\n"
                    f"Local Time: {budget['now_local']} | Bedtime: {bed_lbl}\n"
                    f"Free minutes until bedtime: {free_m}m | Due-tonight work: {budget['due_tonight_minutes']}m\n"
                    f"Prioritized tonight: {json.dumps(prioritized[:5])}\n"
                    f"Pushed back to tomorrow: {json.dumps(deferred[:5])}\n"
                    f"New unread notifications: {len(new_notifs)}\n"
                    f"YouTube slack minutes: {yt_m}m (allowed={budget['youtube_allowed']})\n"
                    "Write a concise 2-sentence schedule check telling the user what to focus on first before bedtime, "
                    "what was pushed back, and whether they have time for YouTube."
                )
                res = await ollama_client.chat_json_or_text(
                    system_prompt="You are Node's hourly schedule & bedtime coordinator. Be crisp, direct, and grounded strictly in the provided numbers.",
                    user_prompt=prompt,
                    enable_thinking=False,
                )
                txt = (res.get("content") or "").strip()
                if len(txt) >= 24:
                    return txt[:900]
        except Exception:
            pass

        return fallback_text

    def _serialize_log(self, row: CronRunLog) -> Dict[str, Any]:
        try:
            prio = json.loads(row.prioritized_tasks_json or "[]")
        except Exception:
            prio = []
        try:
            defd = json.loads(row.deferred_tasks_json or "[]")
        except Exception:
            defd = []
        return {
            "id": row.id,
            "ran_at": row.ran_at.isoformat(),
            "trigger": row.trigger,
            "bedtime_hhmm": row.bedtime_hhmm,
            "free_minutes_until_bedtime": row.free_minutes_until_bedtime,
            "due_tonight_minutes": row.due_tonight_minutes,
            "scheduled_tonight_minutes": row.scheduled_tonight_minutes,
            "youtube_slack_minutes": row.youtube_slack_minutes,
            "youtube_allowed": row.youtube_allowed,
            "on_schedule_status": row.on_schedule_status,
            "new_notifications_count": row.new_notifications_count,
            "prioritized_tasks": prio,
            "deferred_tasks": defd,
            "ai_summary": row.ai_summary,
        }

    def get_status(
        self,
        session: Session,
        reference_now_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg = self.load_settings()
        budget = self.compute_live_budget(session, reference_now_iso=reference_now_iso)
        logs = list(
            session.exec(select(CronRunLog).order_by(CronRunLog.id.desc()).limit(10)).all()
        )
        serialized_logs = [self._serialize_log(r) for r in logs]
        latest = serialized_logs[0] if serialized_logs else None

        next_run_in_minutes = int(cfg["interval_minutes"])
        if latest and cfg.get("enabled"):
            try:
                ran_dt = datetime.fromisoformat(latest["ran_at"])
                if ran_dt.tzinfo is None:
                    ran_dt = ran_dt.replace(tzinfo=ZoneInfo("UTC"))
                elapsed_m = (utc_now() - ran_dt).total_seconds() / 60.0
                next_run_in_minutes = max(1, int(round(cfg["interval_minutes"] - elapsed_m)))
            except Exception:
                pass

        return {
            "settings": cfg,
            "budget": budget,
            "last_run": latest,
            "history": serialized_logs,
            "next_run_in_minutes": next_run_in_minutes if cfg.get("enabled") else None,
        }

    async def _hourly_loop(self) -> None:
        # Run a lightweight initial check shortly after server startup if none ran in the last hour
        await asyncio.sleep(2.5)
        while True:
            try:
                cfg = self.load_settings()
                if cfg.get("enabled", True):
                    should_fire = False
                    with Session(engine) as session:
                        last = session.exec(select(CronRunLog).order_by(CronRunLog.id.desc()).limit(1)).first()
                        if last is None:
                            should_fire = True
                        else:
                            ran_dt = last.ran_at
                            if ran_dt.tzinfo is None:
                                ran_dt = ran_dt.replace(tzinfo=ZoneInfo("UTC"))
                            elapsed_sec = (utc_now() - ran_dt).total_seconds()
                            if elapsed_sec >= int(cfg["interval_minutes"]) * 60:
                                should_fire = True
                        if should_fire:
                            await self.run_cron_check(session, trigger="hourly_cron")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Hourly AI cron loop warning: %s", exc)
            await asyncio.sleep(30)

    def start_background_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task and not self._task.done():
            try:
                if self._task.get_loop() is loop:
                    return
            except Exception:
                pass
        self._task = loop.create_task(self._hourly_loop())

    async def stop_background_loop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                if self._task.get_loop() is asyncio.get_running_loop():
                    await self._task
            except (asyncio.CancelledError, RuntimeError):
                pass
        self._task = None


cron_scheduler = HourlyAICronScheduler()
