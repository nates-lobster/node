import asyncio
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
import zipfile

from sqlmodel import Session, select

from .config import settings
from .decision_gate import choice, decision_gate
from .cron_scheduler import cron_scheduler
from .database import engine
from .memory_store import memory_store
from .models import LongTermGoal, Notification, Task

logger = logging.getLogger("node.focus_guard")

LONG_VIDEO_THRESHOLD_SECONDS = 30 * 60  # 30 minutes
LONG_VIDEO_EARLY_TRIGGER_SECONDS = 10 * 60  # Trigger at 10 minutes for >30m videos

DEFAULT_SOCIAL_PLATFORMS: List[Dict[str, Any]] = [
    {"id": "youtube", "name": "YouTube", "domains": ["youtube.com", "youtu.be"], "enabled": True, "daily_limit_minutes": 45, "short_form_immediate": True},
    {"id": "instagram", "name": "Instagram", "domains": ["instagram.com"], "enabled": True, "daily_limit_minutes": 25, "short_form_immediate": True},
    {"id": "tiktok", "name": "TikTok", "domains": ["tiktok.com"], "enabled": True, "daily_limit_minutes": 20, "short_form_immediate": True},
    {"id": "facebook", "name": "Facebook", "domains": ["facebook.com", "fb.watch"], "enabled": True, "daily_limit_minutes": 25, "short_form_immediate": True},
    {"id": "x-twitter", "name": "X / Twitter", "domains": ["x.com", "twitter.com"], "enabled": True, "daily_limit_minutes": 25, "short_form_immediate": True},
    {"id": "reddit", "name": "Reddit", "domains": ["reddit.com"], "enabled": True, "daily_limit_minutes": 30, "short_form_immediate": False},
    {"id": "twitch", "name": "Twitch", "domains": ["twitch.tv"], "enabled": True, "daily_limit_minutes": 30, "short_form_immediate": False},
    {"id": "snapchat", "name": "Snapchat Web", "domains": ["snapchat.com"], "enabled": True, "daily_limit_minutes": 20, "short_form_immediate": True},
    {"id": "netflix", "name": "Netflix", "domains": ["netflix.com"], "enabled": True, "daily_limit_minutes": 45, "short_form_immediate": False},
]


def detect_social_platform(url: str, platforms: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return `(matched_platform_dict_or_None, is_short_form_vertical_feed)`."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().lstrip("www.")
        path = (parsed.path or "").lower()
    except Exception:
        host, path = (url or "").lower(), ""

    for p in platforms:
        for dom in p.get("domains") or []:
            d = dom.lower().lstrip("www.")
            if host == d or host.endswith("." + d):
                is_short = bool(
                    "/shorts" in path
                    or "/reel" in path
                    or "tiktok.com" in host
                    or ("instagram.com" in host and ("/reels" in path or "/explore" in path))
                )
                return p, is_short
    return None, False


DEFAULT_GUARD_STATE: Dict[str, Any] = {
    "enabled": True,
    "earned_break_limit_minutes": 20,
    "hard_limit_minutes": 45,
    "long_video_threshold_minutes": 30,
    "long_video_trigger_minutes": 10,
    "study_allowance_minutes": 20,
    "quick_break_minutes": 10,
    "nudge_on_shorts_immediately": True,
    "today_date": "",
    "total_youtube_seconds_today": 0,
    "total_social_seconds_today": 0,
    "platform_seconds_today": {},
    "platforms": DEFAULT_SOCIAL_PLATFORMS,
    "video_analyses": {},
    "session": {
        "active": False,
        "platform_id": "youtube",
        "platform_name": "YouTube",
        "video_id": "",
        "video_title": "",
        "channel": "",
        "description_snippet": "",
        "url": "",
        "is_short": False,
        "duration_seconds": 0,
        "current_time_seconds": 0,
        "video_watched_seconds": 0,
        "video_ended": False,
        "session_seconds": 0,
        "last_heartbeat_ts": 0.0,
        "allowance_until_ts": 0.0,
        "allowance_label": "",
        "timer_was_granted": False,
        "should_nudge": False,
        "prompt_mode": "none",  # none | require_reasoning | hard_block | choice
        "keep_playing": True,
        "force_close": False,
        "allow_continue_choice": True,
        "verdict": "idle",
        "headline": "",
        "message": "",
        "ai_video_analysis": None,
        "last_justification_result": None,
    },
}

WEAK_EXCUSE_PATTERNS = (
    r"^\s*(idk|i don'?t know|bored|just because|because i want to|idk lol|nothing|test|asdf|qwerty|pls|please|chill|relaxing|5 more min|five more min)\s*\.?\s*$",
)


def extract_youtube_video_id(url: str, fallback_title: str = "") -> str:
    try:
        parsed = urlparse(url or "")
        if "/shorts/" in parsed.path:
            seg = parsed.path.split("/shorts/", 1)[1].split("/")[0].strip()
            if seg:
                return f"short:{seg[:32]}"
        qs = parse_qs(parsed.query or "")
        v = (qs.get("v") or [""])[0].strip()
        if v:
            return f"vid:{v[:32]}"
    except Exception:
        pass
    slug = re.sub(r"[^a-z0-9]+", "-", (fallback_title or "video").lower()).strip("-")[:36]
    return f"title:{slug or 'unknown'}"


class FocusGuardService:
    def __init__(self) -> None:
        self._bg_analyses: Dict[str, asyncio.Task] = {}

    @property
    def state_path(self) -> Path:
        p = Path(settings.focus_guard_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load(self) -> Dict[str, Any]:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        state = json.loads(json.dumps(DEFAULT_GUARD_STATE))
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        if k not in ("session", "video_analyses"):
                            state[k] = v
                    if isinstance(loaded.get("video_analyses"), dict):
                        # Keep most recent 30 cached per-video AI analyses
                        items = list(loaded["video_analyses"].items())[-30:]
                        state["video_analyses"] = dict(items)
                    if isinstance(loaded.get("session"), dict):
                        state["session"].update(loaded["session"])
            except Exception:
                pass

        if state.get("today_date") != today_iso:
            state["today_date"] = today_iso
            state["total_youtube_seconds_today"] = 0
            state["total_social_seconds_today"] = 0
            state["platform_seconds_today"] = {}
            state["video_analyses"] = {}

        if not isinstance(state.get("platforms"), list) or not state["platforms"]:
            state["platforms"] = json.loads(json.dumps(DEFAULT_SOCIAL_PLATFORMS))
        if not isinstance(state.get("platform_seconds_today"), dict):
            state["platform_seconds_today"] = {}

        now_ts = time.time()
        last_hb = float(state["session"].get("last_heartbeat_ts") or 0.0)
        if state["session"].get("active") and last_hb > 0 and (now_ts - last_hb) > 90:
            state["session"]["active"] = False
            state["session"]["should_nudge"] = False
            state["session"]["force_close"] = False
            state["session"]["prompt_mode"] = "none"
            state["session"]["verdict"] = "idle"
        return state

    def _save(self, state: Dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _gather_workload_and_schedule(self, session: Session) -> Dict[str, Any]:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        tasks = list(
            session.exec(select(Task).where(Task.completed == False).order_by(Task.due_date.asc())).all()  # noqa: E712
        )
        urgent_tasks = [
            t for t in tasks
            if (t.due_date and t.due_date <= today_iso) or "tonight" in (t.title or "").lower()
        ]
        goals = list(
            session.exec(select(LongTermGoal).where(LongTermGoal.active == True)).all()  # noqa: E712
        )
        unchecked_goals = [g for g in goals if g.last_checkin_date != today_iso]

        top_items: List[Dict[str, Any]] = []
        for t in (urgent_tasks or tasks)[:3]:
            est_m = t.estimated_minutes
            if not est_m:
                est_m, _ = memory_store.estimate_task_minutes(t.title, t.category, t.source_app)
            top_items.append(
                {
                    "kind": "task",
                    "id": t.id,
                    "title": t.title,
                    "urgent": bool((t.due_date and t.due_date <= today_iso) or "tonight" in (t.title or "").lower()),
                    "minutes": est_m,
                    "scheduled_block": t.scheduled_block,
                }
            )
        for g in unchecked_goals[:2]:
            top_items.append(
                {
                    "kind": "goal",
                    "id": g.id,
                    "title": f"{g.daily_habit or g.title} ({g.title})",
                    "urgent": False,
                    "minutes": g.minutes_per_day or 25,
                }
            )

        schedule_budget = cron_scheduler.compute_live_budget(session)

        return {
            "urgent_tasks_count": len(urgent_tasks),
            "total_pending_tasks": len(tasks),
            "unchecked_goals_count": len(unchecked_goals),
            "all_urgent_clear": len(urgent_tasks) == 0 and schedule_budget.get("on_schedule_status") != "behind",
            "top_items": top_items,
            "task_titles": [t.title for t in tasks[:10]],
            "schedule_budget": schedule_budget,
        }

    def _heuristic_video_analysis(
        self,
        video_title: str,
        channel: str,
        description_snippet: str,
        duration_seconds: int,
        workload: Dict[str, Any],
        total_yt_minutes_today: float,
        hard_limit_minutes: int,
    ) -> Dict[str, Any]:
        blob = f"{video_title} {channel} {description_snippet}".lower()
        edu_keywords = (
            "lecture", "tutorial", "course", "calculus", "linear algebra", "physics",
            "chemistry", "biology", "programming", "algorithm", "python", "rust", "linux",
            "history", "economics", "mit ", "stanford", "3blue1brown", "khan academy",
            "exam", "homework", "review", "proof", "derivation", "documentation", "system design",
        )
        distraction_keywords = (
            "gameplay", "highlights", "funny", "prank", "reacts", "reaction", "tier list",
            "vlog", "drama", "shorts", "compilation", "memes", "stream highlights", "unboxing",
            "trailer", "montage",
        )

        # Check overlap with user's actual open task titles!
        matched_task = None
        for t_title in workload.get("task_titles") or []:
            words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", t_title) if w.lower() not in {"task", "homework", "assignment", "finish", "complete", "study", "read", "write"}]
            if words and any(w in blob for w in words):
                matched_task = t_title
                break

        is_edu = bool(matched_task) or any(k in blob for k in edu_keywords)
        is_distract = any(k in blob for k in distraction_keywords)

        sched = workload.get("schedule_budget") or {}
        yt_allowed_by_sched = bool(sched.get("youtube_allowed", True))
        over_hard_cap = total_yt_minutes_today >= hard_limit_minutes

        if matched_task:
            classification = "coursework_aligned"
            relevance = 88
            summary = f"Directly relates to active task \"{matched_task}\"."
        elif is_edu and not is_distract:
            classification = "educational_general"
            relevance = 68
            summary = "Educational/technical topic, though not explicitly tied to tonight's due list."
        else:
            classification = "entertainment_distraction"
            relevance = 18
            summary = "Entertainment / non-task video."

        if over_hard_cap and classification != "coursework_aligned":
            recommended_mode = "hard_block"
        elif not yt_allowed_by_sched and classification != "coursework_aligned" and total_yt_minutes_today >= 20:
            recommended_mode = "hard_block"
        elif classification == "coursework_aligned" and not over_hard_cap:
            recommended_mode = "allow_aligned"
        else:
            recommended_mode = "require_reasoning"

        return {
            "classification": classification,
            "relevance_score": relevance,
            "matched_task": matched_task,
            "recommended_mode": recommended_mode,
            "summary": summary,
            "duration_minutes": round(duration_seconds / 60.0, 1) if duration_seconds > 0 else None,
            "analyzed_by": "heuristic+schedule",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def run_background_ai_video_analysis(
        self,
        video_id: str,
        video_title: str,
        channel: str,
        description_snippet: str,
        duration_seconds: int,
    ) -> Dict[str, Any]:
        """
        Runs during playback so D1 pre-evaluates the video's relation to active
        tasks before the accountability prompt fires; uses deterministic fallback offline.
        """
        with Session(engine) as session:
            state = self._load()
            workload = self._gather_workload_and_schedule(session)
            total_m = (state.get("total_youtube_seconds_today") or 0) / 60.0
            hard_cap = int(state.get("hard_limit_minutes") or 45)
            baseline = self._heuristic_video_analysis(
                video_title=video_title,
                channel=channel,
                description_snippet=description_snippet,
                duration_seconds=duration_seconds,
                workload=workload,
                total_yt_minutes_today=total_m,
                hard_limit_minutes=hard_cap,
            )

        try:
            # D1 is the policy decision model: compare video metadata with actual
            # open tasks. Fall back to the deterministic heuristic if it is offline.
            if await decision_gate.ensure_ready() and decision_gate.supports("choice"):
                sched = workload.get("schedule_budget") or {}
                answers = await decision_gate.ask(
                    {
                        "video_title": video_title,
                        "channel": channel,
                        "description": description_snippet[:400],
                        "duration_minutes": round(duration_seconds / 60.0, 1),
                        "active_tasks": workload.get("task_titles", [])[:8],
                        "schedule": {
                            "free_minutes_until_bedtime": sched.get("free_minutes_until_bedtime", 0),
                            "due_tonight_minutes": sched.get("due_tonight_minutes", 0),
                            "youtube_watched_today_minutes": round(total_m, 1),
                            "youtube_hard_cap_minutes": hard_cap,
                        },
                    },
                    {
                        "classification": choice(
                            "Classify this video's likely value for the user's active tasks. Choose coursework_aligned only when the topic directly matches an active course/task; educational_general is useful learning but not task-specific; entertainment_distraction is mainly leisure or unrelated.",
                            {
                                "coursework_aligned": "Directly relevant to one or more active coursework tasks.",
                                "educational_general": "Educational, but not clearly tied to active work.",
                                "entertainment_distraction": "Entertainment or unrelated to active work.",
                            },
                        ),
                        "recommended_mode": choice(
                            "Given relevance, workload, schedule slack and today's usage, select the least restrictive safe policy. A hard cap or urgent workload with no slack must not be softened.",
                            {
                                "hard_block": "Force close now: hard cap exhausted or urgent work and no available slack.",
                                "require_reasoning": "Ask the user for a concrete reason before continuing.",
                                "allow_aligned": "Allow because this directly supports active work and no hard cap is reached.",
                            },
                        ),
                    },
                    timeout=settings.decision_timeout_seconds,
                )
                if answers:
                    classification = (answers.get("classification").text if answers.get("classification") else "").lower()
                    mode = (answers.get("recommended_mode").text if answers.get("recommended_mode") else "").lower()
                    if classification in ("coursework_aligned", "educational_general", "entertainment_distraction"):
                        baseline["classification"] = classification
                    if mode in ("hard_block", "require_reasoning", "allow_aligned"):
                        if baseline["recommended_mode"] != "hard_block":
                            baseline["recommended_mode"] = mode
                    baseline["analyzed_by"] = decision_gate.model
        except Exception as exc:
            logger.debug("D1 video analysis fallback: %s", exc)


        state = self._load()
        state.setdefault("video_analyses", {})[video_id] = baseline
        if state["session"].get("video_id") == video_id:
            state["session"]["ai_video_analysis"] = baseline
        self._save(state)
        return baseline

    def _schedule_bg_analysis_if_needed(
        self,
        video_id: str,
        video_title: str,
        channel: str,
        description_snippet: str,
        duration_seconds: int,
    ) -> None:
        existing_task = self._bg_analyses.get(video_id)
        if existing_task and not existing_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._bg_analyses[video_id] = loop.create_task(
                self.run_background_ai_video_analysis(
                    video_id=video_id,
                    video_title=video_title,
                    channel=channel,
                    description_snippet=description_snippet,
                    duration_seconds=duration_seconds,
                )
            )
        except RuntimeError:
            pass

    def get_status(self, session: Session) -> Dict[str, Any]:
        state = self._load()
        workload = self._gather_workload_and_schedule(session)
        now_ts = time.time()
        allow_until = float(state["session"].get("allowance_until_ts") or 0.0)
        rem_sec = max(0, int(allow_until - now_ts)) if allow_until > now_ts else 0
        plat_secs = state.get("platform_seconds_today") or {}
        total_social_sec = max(
            int(state.get("total_social_seconds_today") or 0),
            int(state.get("total_youtube_seconds_today") or 0),
            sum(int(v or 0) for v in plat_secs.values()),
        )
        platform_minutes = {k: round(int(v or 0) / 60.0, 1) for k, v in plat_secs.items()}
        return {
            **state,
            "allowance_remaining_seconds": rem_sec,
            "total_youtube_minutes_today": round((state.get("total_youtube_seconds_today") or 0) / 60.0, 1),
            "total_social_minutes_today": round(total_social_sec / 60.0, 1),
            "platform_minutes_today": platform_minutes,
            "workload": workload,
        }

    def update_settings(self, payload: Dict[str, Any], session: Session) -> Dict[str, Any]:
        state = self._load()
        if "enabled" in payload:
            state["enabled"] = bool(payload["enabled"])
        for key in (
            "earned_break_limit_minutes",
            "hard_limit_minutes",
            "long_video_threshold_minutes",
            "long_video_trigger_minutes",
            "study_allowance_minutes",
            "quick_break_minutes",
        ):
            if key in payload and payload[key] is not None:
                state[key] = max(1, min(360, int(payload[key])))
        if "nudge_on_shorts_immediately" in payload:
            state["nudge_on_shorts_immediately"] = bool(payload["nudge_on_shorts_immediately"])
        if isinstance(payload.get("platforms"), list):
            state["platforms"] = payload["platforms"]
        self._save(state)
        return self.get_status(session)

    def upsert_platform_rule(
        self,
        *,
        domain: str,
        name: Optional[str],
        enabled: bool,
        daily_limit_minutes: Optional[int],
        short_form_immediate: bool,
        session: Session,
    ) -> Dict[str, Any]:
        state = self._load()
        clean_dom = re.sub(r"^https?://", "", (domain or "").strip().lower()).split("/")[0].lstrip("www.")
        if not clean_dom:
            return self.get_status(session)
        pid = re.sub(r"[^a-z0-9]+", "-", clean_dom).strip("-")[:48] or "custom-site"
        existing = next(
            (p for p in state["platforms"] if p.get("id") == pid or clean_dom in (p.get("domains") or [])),
            None,
        )
        if existing:
            if name:
                existing["name"] = name.strip()
            existing["enabled"] = bool(enabled)
            if daily_limit_minutes is not None:
                existing["daily_limit_minutes"] = max(1, min(600, int(daily_limit_minutes)))
            existing["short_form_immediate"] = bool(short_form_immediate)
        else:
            state["platforms"].append(
                {
                    "id": pid,
                    "name": (name or clean_dom.split(".")[0].title()).strip(),
                    "domains": [clean_dom],
                    "enabled": bool(enabled),
                    "daily_limit_minutes": max(1, min(600, int(daily_limit_minutes or 25))),
                    "short_form_immediate": bool(short_form_immediate),
                    "custom": True,
                }
            )
        self._save(state)
        return self.get_status(session)

    def delete_platform_rule(self, platform_id: str, session: Session) -> Dict[str, Any]:
        state = self._load()
        state["platforms"] = [p for p in state["platforms"] if p.get("id") != platform_id]
        self._save(state)
        return self.get_status(session)

    def record_heartbeat(self, payload: Dict[str, Any], session: Session) -> Dict[str, Any]:
        """
        Universal Social & Media Focus Guard Heartbeat:
          - Protects across YouTube, Instagram, TikTok, Facebook, X/Twitter, Reddit, Twitch, Snapchat, Netflix & custom domains!
          - Short-form vertical feeds (`/shorts`, `/reels`, `tiktok.com`, `/explore`): immediate accountability check
          - Standard videos <= 30 min: triggers at end-of-video; long videos / open-ended feeds > 30 min: triggers at `long_video_trigger_minutes`
          - Enforces both per-platform daily caps AND global daily social media hard cap!
        """
        state = self._load()
        workload = self._gather_workload_and_schedule(session)
        sched = workload["schedule_budget"]
        now_ts = time.time()

        url = str(payload.get("url") or "https://www.youtube.com").strip()[:500]
        matched_plat, auto_short = detect_social_platform(url, state.get("platforms") or DEFAULT_SOCIAL_PLATFORMS)
        plat_id = (matched_plat or {}).get("id") or str(payload.get("platform") or "youtube").lower()
        plat_name = (matched_plat or {}).get("name") or "Social Media"
        plat_enabled = True if matched_plat is None else bool(matched_plat.get("enabled", True))
        plat_cap_mins = int((matched_plat or {}).get("daily_limit_minutes") or state.get("hard_limit_minutes") or 45)

        video_title = str(payload.get("video_title") or f"{plat_name} Feed").strip()[:200]
        channel = str(payload.get("channel") or plat_name).strip()[:120]
        desc_snip = str(payload.get("description_snippet") or "").strip()[:800]
        is_short = bool(payload.get("is_short")) or auto_short or ("/shorts/" in url.lower()) or ("/reels/" in url.lower())
        video_ended = bool(payload.get("video_ended", False))
        is_playing = bool(payload.get("playing", True)) or video_ended
        duration_sec = max(0, int(float(payload.get("duration_seconds") or 0)))
        current_time_sec = max(0, int(float(payload.get("current_time_seconds") or 0)))
        delta_sec = max(0, min(120, int(payload.get("elapsed_seconds", 15))))

        video_id = str(payload.get("video_id") or f"{plat_id}:{extract_youtube_video_id(url, video_title)}")
        sess = state["session"]
        sess["platform_id"] = plat_id
        sess["platform_name"] = plat_name

        prev_vid = sess.get("video_id")
        if prev_vid != video_id:
            sess["video_id"] = video_id
            sess["video_watched_seconds"] = 0
            sess["last_justification_result"] = None
            sess["force_close"] = False

        plat_secs = state.setdefault("platform_seconds_today", {})

        if payload.get("override_session_minutes") is not None:
            override_sec = int(float(payload["override_session_minutes"]) * 60)
            sess["session_seconds"] = override_sec
            sess["video_watched_seconds"] = override_sec
            plat_secs[plat_id] = max(int(plat_secs.get(plat_id) or 0), override_sec)
            state["total_youtube_seconds_today"] = max(
                int(state.get("total_youtube_seconds_today") or 0),
                override_sec,
            )
            state["total_social_seconds_today"] = max(
                int(state.get("total_social_seconds_today") or 0),
                override_sec,
            )
            if payload.get("override_today_minutes") is not None:
                ov_tot = int(float(payload["override_today_minutes"]) * 60)
                state["total_youtube_seconds_today"] = ov_tot
                state["total_social_seconds_today"] = ov_tot
                plat_secs[plat_id] = ov_tot
            if payload.get("clear_allowance"):
                sess["allowance_until_ts"] = 0.0
                sess["timer_was_granted"] = False
        else:
            last_hb = float(sess.get("last_heartbeat_ts") or 0.0)
            if last_hb and (now_ts - last_hb) > 600:
                sess["session_seconds"] = 0
                sess["timer_was_granted"] = False
            if is_playing and plat_enabled:
                sess["session_seconds"] = int(sess.get("session_seconds") or 0) + delta_sec
                sess["video_watched_seconds"] = int(sess.get("video_watched_seconds") or 0) + delta_sec
                plat_secs[plat_id] = int(plat_secs.get(plat_id) or 0) + delta_sec
                state["total_social_seconds_today"] = int(state.get("total_social_seconds_today") or 0) + delta_sec
                if plat_id == "youtube":
                    state["total_youtube_seconds_today"] = int(state.get("total_youtube_seconds_today") or 0) + delta_sec
                else:
                    # Also keep total_youtube_seconds_today synced as unified social counter for legacy callers
                    state["total_youtube_seconds_today"] = max(
                        int(state.get("total_youtube_seconds_today") or 0),
                        int(state.get("total_social_seconds_today") or 0),
                    )

        if payload.get("override_today_minutes") is not None:
            ov_tot = int(float(payload["override_today_minutes"]) * 60)
            state["total_youtube_seconds_today"] = ov_tot
            state["total_social_seconds_today"] = ov_tot
            plat_secs[plat_id] = ov_tot

        sess["active"] = bool(is_playing and plat_enabled)
        sess["video_title"] = video_title
        sess["channel"] = channel
        sess["description_snippet"] = desc_snip
        sess["url"] = url
        sess["is_short"] = is_short
        sess["duration_seconds"] = duration_sec
        sess["current_time_seconds"] = current_time_sec
        sess["video_ended"] = video_ended
        sess["last_heartbeat_ts"] = now_ts

        total_today_mins = round((state.get("total_youtube_seconds_today") or 0) / 60.0, 1)
        hard_cap_mins = int(state.get("hard_limit_minutes") or 45)
        long_thresh_sec = int(state.get("long_video_threshold_minutes") or 30) * 60
        long_trig_sec = int(state.get("long_video_trigger_minutes") or 10) * 60

        # Ensure per-video AI pre-analysis exists immediately AND refine asynchronously via Ollama
        cached_analysis = (state.get("video_analyses") or {}).get(video_id)
        if not cached_analysis:
            cached_analysis = self._heuristic_video_analysis(
                video_title=video_title,
                channel=channel,
                description_snippet=desc_snip,
                duration_seconds=duration_sec,
                workload=workload,
                total_yt_minutes_today=total_today_mins,
                hard_limit_minutes=hard_cap_mins,
            )
            state.setdefault("video_analyses", {})[video_id] = cached_analysis
        if cached_analysis.get("analyzed_by") == "heuristic+schedule":
            self._schedule_bg_analysis_if_needed(video_id, video_title, channel, desc_snip, duration_sec)
        sess["ai_video_analysis"] = cached_analysis

        allow_until = float(sess.get("allowance_until_ts") or 0.0)
        is_near_end = (
            video_ended
            or (duration_sec > 20 and duration_sec <= long_thresh_sec and current_time_sec > 0 and (duration_sec - current_time_sec) <= 3)
        )
        is_long_video = duration_sec > long_thresh_sec or (not is_short and duration_sec == 0 and sess["video_watched_seconds"] >= long_trig_sec)
        hit_long_10m_mark = is_long_video and (
            sess["video_watched_seconds"] >= long_trig_sec or current_time_sec >= long_trig_sec
        )

        # Decide whether the timing trigger has arrived!
        over_hard_cap = total_today_mins >= hard_cap_mins or sched.get("on_schedule_status") == "past_bedtime"

        if not state.get("enabled", True) or not is_playing:
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "paused_or_disabled"
            sess["headline"] = "Focus Guard Idle"
            sess["message"] = "Playback paused or Focus Guard snoozed."
        elif allow_until > now_ts and not over_hard_cap:
            rem_m = max(1, round((allow_until - now_ts) / 60))
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "allowed_timer"
            sess["headline"] = f"Approved Pass ({rem_m}m left)"
            sess["message"] = f"{sess.get('allowance_label') or 'Approved watch window'} — Focus Guard will check back in {rem_m}m."
        elif over_hard_cap and cached_analysis.get("classification") != "coursework_aligned":
            # AI Choice 1: Accumulated too much YouTube time or past bedtime -> NO choice to continue, force close!
            sess["should_nudge"] = True
            sess["prompt_mode"] = "hard_block"
            sess["keep_playing"] = False
            sess["force_close"] = True
            sess["allow_continue_choice"] = False
            sess["verdict"] = "hard_block"
            bed_str = sched.get("bedtime_label", "10:00 PM")
            if sched.get("on_schedule_status") == "past_bedtime":
                sess["headline"] = f"Past {bed_str} Bedtime — Closing YouTube"
                sess["message"] = f"It's past your {bed_str} bedtime and you've watched {int(total_today_mins)}m today. Closing YouTube so you get to sleep on time."
            else:
                sess["headline"] = f"YouTube Limit Reached ({int(total_today_mins)}m Today) — No Continue Option"
                sess["message"] = (
                    f"You've already accumulated {int(total_today_mins)} min on YouTube today "
                    f"(limit {hard_cap_mins}m) with {sched.get('due_tonight_minutes', 0)}m of priority work before {bed_str} bedtime. "
                    f"Force-closing \"{video_title}\"."
                )
        elif is_short and state.get("nudge_on_shorts_immediately", True):
            sess["should_nudge"] = True
            sess["prompt_mode"] = "require_reasoning"
            sess["keep_playing"] = True
            sess["force_close"] = False
            sess["allow_continue_choice"] = True
            sess["verdict"] = "nudge_shorts"
            sess["headline"] = "YouTube Shorts Check — Why Watch Instead of Working?"
            sess["message"] = (
                f"You're on Shorts (\"{video_title}\"). Before scrolling further, type a concrete reason why "
                f"this beats working on tonight's tasks (video stays live while AI checks; weak excuses close the tab)."
            )
        elif hit_long_10m_mark or is_near_end or bool(payload.get("force_trigger")):
            # Video trigger point hit! (End of <=30m video, OR 10-minute mark of >30m video!)
            if cached_analysis.get("recommended_mode") == "allow_aligned" and sched.get("youtube_allowed", True):
                sess["should_nudge"] = False
                sess["prompt_mode"] = "none"
                sess["force_close"] = False
                sess["verdict"] = "ok_aligned"
                sess["headline"] = "Aligned With Active Work"
                sess["message"] = f"AI pre-analysis: {cached_analysis.get('summary')}"
            else:
                trigger_desc = (
                    f"10-minute check on a {max(31, round(duration_sec / 60))}m video"
                    if hit_long_10m_mark
                    else "Video finished"
                )
                sess["should_nudge"] = True
                sess["prompt_mode"] = "require_reasoning"
                sess["keep_playing"] = True
                sess["force_close"] = False
                sess["allow_continue_choice"] = True
                sess["verdict"] = "require_reasoning"
                sess["headline"] = f"{trigger_desc} — Justify Continuing to Node AI"
                sess["message"] = (
                    f"AI Analysis: {cached_analysis.get('summary')} "
                    f"({sched.get('free_minutes_until_bedtime', 0)}m left before {sched.get('bedtime_label', '10:00 PM')} bedtime, "
                    f"{sched.get('due_tonight_minutes', 0)}m due tonight). "
                    f"Type why you should keep watching instead of working — playback continues while AI evaluates, "
                    f"and insufficient reasoning will force-close this tab."
                )
        else:
            # Mid-playback monitoring window (e.g. minutes 0..9 of a >30m video, or mid <=30m video):
            # AI analyzes quietly in the background without interrupting early!
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "monitoring_pre_trigger"
            if duration_sec > long_thresh_sec:
                rem_to_10m = max(1, int(round((long_trig_sec - max(sess["video_watched_seconds"], current_time_sec)) / 60.0)))
                sess["headline"] = f"Analyzing Long Video (Check at 10m — {rem_to_10m}m left)"
                sess["message"] = f"AI pre-analysis ({cached_analysis.get('classification')}): {cached_analysis.get('summary')}"
            else:
                sess["headline"] = "Monitoring Video (Will Check at End of Video)"
                sess["message"] = f"AI pre-analysis ({cached_analysis.get('classification')}): {cached_analysis.get('summary')}"

        self._save(state)
        return self.get_status(session)

    async def evaluate_justification(
        self,
        reasoning: str,
        payload_meta: Dict[str, Any],
        session: Session,
    ) -> Dict[str, Any]:
        """
        Evaluates the user's typed explanation while the video continues playing.
        If sufficient -> grants timed pass.
        If insufficient -> returns `approved=False, force_close=True` so the extension
        force-closes the YouTube tab immediately!
        """
        state = self._load()
        sess = state["session"]
        workload = self._gather_workload_and_schedule(session)
        sched = workload["schedule_budget"]
        now_ts = time.time()

        clean_reason = " ".join((reasoning or "").split()).strip()
        video_title = str(payload_meta.get("video_title") or sess.get("video_title") or "YouTube Video")
        channel = str(payload_meta.get("channel") or sess.get("channel") or "")
        analysis = sess.get("ai_video_analysis") or {}
        total_today_m = round((state.get("total_youtube_seconds_today") or 0) / 60.0, 1)

        # Deterministic policy / anti-excuse gates first. A hard cap or hard-block
        # video assessment is never overrideable through an API call or model answer.
        approved = False
        ai_explanation = ""
        word_count = len(clean_reason.split())
        matches_weak = any(re.match(pat, clean_reason, re.IGNORECASE) for pat in WEAK_EXCUSE_PATTERNS)
        hard_block = (
            total_today_m >= int(state.get("hard_limit_minutes") or 45)
            or analysis.get("recommended_mode") == "hard_block"
        )

        if hard_block:
            ai_explanation = "The Focus Guard hard limit or policy block is active; no viewing pass can override it."
        elif len(clean_reason) < 18 or word_count < 4 or matches_weak:
            approved = False
            ai_explanation = (
                "Reasoning is too brief or generic to justify delaying your work. Force-closing YouTube so you stay on schedule."
            )
        else:
            # Deterministic fallback rubric; D1 may refine non-hard-block decisions below.
            low_r = clean_reason.lower()
            study_signals = any(
                w in low_r
                for w in (
                    "homework", "assignment", "lecture", "class", "exam", "quiz", "project",
                    "research", "concept", "problem set", "lab", "course", "study", "tutorial", "understand",
                )
            )
            earned_rest_signals = any(
                w in low_r for w in ("finished", "completed", "done with", "break before", "ate dinner", "eating")
            )
            if study_signals and analysis.get("classification") != "entertainment_distraction":
                approved = True
                ai_explanation = "Valid study/project justification aligned with this video's topic."
            elif study_signals and analysis.get("classification") == "entertainment_distraction":
                approved = False
                ai_explanation = (
                    f"Your note claims study value, but \"{video_title}\" ({channel or 'YouTube'}) "
                    "was classified as entertainment unrelated to your tasks. Closing tab."
                )
            elif earned_rest_signals and sched.get("youtube_allowed", False) and total_today_m < int(state.get("hard_limit_minutes") or 45):
                approved = True
                ai_explanation = (
                    f"Schedule confirms {sched.get('youtube_slack_minutes', 0)}m of free slack before "
                    f"{sched.get('bedtime_label', '10:00 PM')} bedtime — granting a bounded break."
                )
            else:
                approved = False
                ai_explanation = (
                    f"With {sched.get('due_tonight_minutes', 0)}m of priority work and {sched.get('bedtime_label', '10:00 PM')} "
                    "bedtime approaching, that reason isn't strong enough to skip working. Closing tab."
                )

            # D1 decides whether the claimed reason is genuinely relevant to active
            # work. The deterministic checks above remain the fallback and hard
            # cap / obvious entertainment mismatch cannot be overridden.
            try:
                if await decision_gate.ensure_ready() and decision_gate.supports("choice"):
                    answers = await decision_gate.ask(
                        {
                            "video": {
                                "title": video_title,
                                "channel": channel,
                                "classification": analysis.get("classification"),
                                "summary": analysis.get("summary"),
                            },
                            "active_tasks": workload.get("task_titles", [])[:8],
                            "schedule": {
                                "free_minutes_until_bedtime": sched.get("free_minutes_until_bedtime", 0),
                                "due_tonight_minutes": sched.get("due_tonight_minutes", 0),
                                "youtube_watched_today_minutes": round(total_today_m, 1),
                                "youtube_hard_cap_minutes": int(state.get("hard_limit_minutes") or 45),
                            },
                            "user_reason": clean_reason,
                        },
                        {
                            "decision": choice(
                                "Approve only if the user gives a concrete reason grounded in an active task/course and this video is actually relevant, or if the schedule allows a bounded rest break. Reject generic excuses and claims contradicted by the video's classification.",
                                {
                                    "approve": "The reason is specific, supported by task/video context or allowed rest time.",
                                    "reject": "The reason is vague, unrelated, contradicted by the video, or workload makes this unsafe.",
                                },
                            )
                        },
                        timeout=settings.decision_timeout_seconds,
                    )
                    decision = (answers.get("decision").text if answers and answers.get("decision") else "").lower()
                    if decision == "approve" and not (study_signals and analysis.get("classification") == "entertainment_distraction"):
                        approved = True
                        ai_explanation = "D1 approved this specific reason against the video and active-task context."
                    elif decision == "reject":
                        approved = False
                        ai_explanation = "D1 found the reason unrelated to active work or inconsistent with the available time."
            except Exception as exc:
                logger.debug("D1 justification check fallback: %s", exc)

        if approved:
            grant_m = int(state.get("study_allowance_minutes") or 20)
            sess["allowance_until_ts"] = now_ts + grant_m * 60
            sess["allowance_label"] = f"AI-Approved ({grant_m}m)"
            sess["timer_was_granted"] = True
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "approved_reasoning"
            sess["headline"] = f"Approved (+{grant_m}m)"
            sess["message"] = ai_explanation
        else:
            sess["active"] = False
            sess["should_nudge"] = False
            sess["prompt_mode"] = "hard_block"
            sess["force_close"] = True
            sess["verdict"] = "rejected_force_close"
            sess["headline"] = "Reasoning Insufficient — Closing YouTube Tab"
            sess["message"] = ai_explanation
            session.add(
                Notification(
                    source_app="Focus Guard",
                    title=f"Force-closed YouTube: {video_title[:90]}",
                    message=f"Your reason (\"{clean_reason[:140]}\") was declined: {ai_explanation}",
                    status="active",
                    priority="normal",
                    read_state=False,
                    indicator_type="focus_guard",
                )
            )
            session.commit()

        result_obj = {
            "approved": approved,
            "force_close": not approved,
            "explanation": ai_explanation,
            "granted_minutes": int(state.get("study_allowance_minutes") or 20) if approved else 0,
        }
        sess["last_justification_result"] = result_obj
        self._save(state)
        status_payload = self.get_status(session)
        status_payload["justification_result"] = result_obj
        return status_payload

    def respond_to_nudge(self, action: str, minutes: Optional[int], session: Session) -> Dict[str, Any]:
        state = self._load()
        sess = state["session"]
        now_ts = time.time()

        if action == "study_needed":
            dur = max(1, min(90, int(minutes or state.get("study_allowance_minutes") or 20)))
            sess["allowance_until_ts"] = now_ts + dur * 60
            sess["allowance_label"] = f"Class / Study Video ({dur}m)"
            sess["timer_was_granted"] = True
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "allowed_timer"
        elif action == "intentional_break":
            dur = max(1, min(60, int(minutes or state.get("quick_break_minutes") or 10)))
            sess["allowance_until_ts"] = now_ts + dur * 60
            sess["allowance_label"] = f"Intentional Break ({dur}m)"
            sess["timer_was_granted"] = True
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["verdict"] = "allowed_timer"
        else:
            sess["active"] = False
            sess["should_nudge"] = False
            sess["prompt_mode"] = "none"
            sess["force_close"] = False
            sess["allowance_until_ts"] = 0.0
            sess["timer_was_granted"] = False
            sess["session_seconds"] = 0
            sess["verdict"] = "idle"
            sess["headline"] = "Returned to Focus"
            sess["message"] = "Closed YouTube distraction — nice discipline!"

        self._save(state)
        return self.get_status(session)

    def _overlay_ui_js(self) -> str:
        return """
  let panelEl = null;
  let badgeEl = null;
  let activePromptKey = '';

  function extractVideoMetadata() {
    const path = location.pathname || '';
    const vid = document.querySelector('video');
    const isShort = path.startsWith('/shorts');
    const rawTitle = (
      document.querySelector('ytd-watch-metadata h1 yt-formatted-string')?.textContent ||
      document.title || ''
    ).replace(/\\s*-\\s*YouTube$/i, '').replace(/^\\(\\d+\\)\\s*/, '').trim();
    const chEl = document.querySelector('#channel-name a, ytd-channel-name a, ytd-reel-player-header-renderer a');
    const descEl = document.querySelector('#description-inline-expander, ytd-text-inline-expander, meta[name="description"]');
    const desc = descEl ? (descEl.getAttribute?.('content') || descEl.textContent || '').trim().slice(0, 600) : '';
    const dur = (vid && Number.isFinite(vid.duration) && vid.duration > 0) ? Math.round(vid.duration) : 0;
    const cur = (vid && Number.isFinite(vid.currentTime) && vid.currentTime > 0) ? Math.round(vid.currentTime) : 0;
    const ended = Boolean(vid && vid.ended);
    const playing = vid ? (!vid.paused && !vid.ended) : false;
    return {
      video_title: rawTitle || 'YouTube Video',
      channel: chEl ? (chEl.textContent || '').trim() : '',
      description_snippet: desc,
      url: location.href,
      is_short: isShort,
      duration_seconds: dur,
      current_time_seconds: cur,
      video_ended: ended,
      playing
    };
  }

  function updateStatusBadge(ok, label) {
    if (!document.body) return;
    if (!badgeEl) {
      badgeEl = document.createElement('div');
      badgeEl.id = 'node-focus-guard-pill';
      badgeEl.style.cssText = 'position:fixed;bottom:14px;right:14px;z-index:2147483645;padding:5px 10px;border-radius:999px;font:600 11px/1.2 Inter,-apple-system,BlinkMacSystemFont,sans-serif;background:rgba(13,18,28,0.85);border:1px solid rgba(43,251,174,0.28);color:#cbd5e1;backdrop-filter:blur(8px);pointer-events:none;transition:opacity 0.3s ease;opacity:0.9;';
      document.body.appendChild(badgeEl);
    }
    const dot = ok ? '#2bfbae' : '#f87171';
    badgeEl.innerHTML = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${dot};margin-right:6px;vertical-align:middle;"></span>${label}`;
    badgeEl.style.opacity = '0.92';
    clearTimeout(badgeEl._hideTimer);
    if (ok) {
      badgeEl._hideTimer = setTimeout(() => { if (badgeEl) badgeEl.style.opacity = '0.24'; }, 4500);
    }
  }

  function triggerForceCloseTab(hubOrigin, closeTabFn, reasonText) {
    const vid = document.querySelector('video');
    if (vid) {
      try { vid.pause(); vid.muted = true; vid.src = ''; vid.load(); } catch (_) {}
    }
    if (!panelEl) {
      panelEl = document.createElement('div');
      panelEl.id = 'node-focus-guard-overlay';
      document.body.appendChild(panelEl);
    }
    panelEl.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(8,12,18,0.95);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:center;font-family:Inter,-apple-system,sans-serif;color:#f8fafc;';
    panelEl.innerHTML = `
      <div style="max-width:440px;width:92%;background:#111824;border:1px solid rgba(248,113,113,0.55);border-radius:18px;padding:24px;text-align:center;box-shadow:0 28px 64px rgba(0,0,0,0.8);">
        <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#f87171;margin-bottom:6px;">Node • Tab Force-Closing</div>
        <h2 style="font-size:18px;font-weight:700;margin:0 0 8px;">Back to Scheduled Focus</h2>
        <p style="font-size:13px;line-height:1.5;color:#cbd5e1;margin:0;">${reasonText || 'Closing YouTube tab…'}</p>
      </div>
    `;
    setTimeout(async () => {
      if (typeof closeTabFn === 'function') {
        await closeTabFn();
      }
      try { window.close(); } catch (_) {}
      window.location.replace((hubOrigin || 'about:blank'));
    }, 1800);
  }

  function ensureAccountabilityUI(data, hubOrigin, actions) {
    const sess = data && data.session ? data.session : null;
    if (!sess || (!sess.should_nudge && !sess.force_close)) {
      if (panelEl) { panelEl.remove(); panelEl = null; activePromptKey = ''; }
      return;
    }

    // Hard block: accumulated too much watch time or past bedtime -> NO option to continue!
    if (sess.force_close || sess.prompt_mode === 'hard_block') {
      triggerForceCloseTab(hubOrigin, actions.closeTab, sess.message || sess.headline);
      return;
    }

    // Non-pausing floating accountability card: video CONTINUES playing while user types & while AI evaluates!
    // Block YouTube's 'Up Next' autoplay if the video just ended so it doesn't silently roll into a new video.
    const promptKey = `${sess.video_id}:${sess.verdict}`;
    if (panelEl && activePromptKey === promptKey) {
      return;
    }
    activePromptKey = promptKey;

    if (!panelEl) {
      panelEl = document.createElement('div');
      panelEl.id = 'node-focus-guard-overlay';
      document.body.appendChild(panelEl);
    }
    panelEl.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:2147483647;width:410px;max-width:calc(100vw - 32px);background:rgba(14,20,31,0.96);border:1px solid rgba(43,251,174,0.45);border-radius:18px;padding:18px;box-shadow:0 24px 60px rgba(0,0,0,0.75);backdrop-filter:blur(14px);font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#f1f5f9;';

    const items = ((data.workload && data.workload.top_items) || [])
      .slice(0, 3)
      .map(i => `<li style="margin:3px 0;">• <strong>${i.title}</strong> <span style="color:#2bfbae;">(~${i.minutes}m${i.scheduled_block ? ' • ' + i.scheduled_block : ''})</span></li>`)
      .join('');

    panelEl.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <span style="font-size:10.5px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#2bfbae;">Node • AI Accountability</span>
        <span style="font-size:11px;color:#94a3b8;">${data.total_youtube_minutes_today || 0}m watched today</span>
      </div>
      <h3 style="font-size:15px;font-weight:700;margin:0 0 6px;color:#f8fafc;">${sess.headline || 'Why keep watching instead of working?'}</h3>
      <p style="font-size:12.5px;line-height:1.45;color:#cbd5e1;margin:0 0 10px;">${sess.message || ''}</p>
      ${items ? `<ul style="list-style:none;padding:8px 11px;margin:0 0 10px;background:rgba(9,14,22,0.85);border-radius:10px;font-size:11.5px;color:#e2e8f0;">${items}</ul>` : ''}
      <textarea id="nfg-reason-input" rows="3" placeholder="Type 2–3 concrete reasons why watching this right now beats working on your tasks…" style="width:100%;box-sizing:border-box;border-radius:10px;border:1px solid rgba(148,163,184,0.28);background:#090e16;color:#f8fafc;padding:9px 10px;font:12px/1.4 Inter,sans-serif;resize:vertical;outline:none;margin-bottom:8px;"></textarea>
      <div id="nfg-ai-status" style="display:none;font-size:11.5px;color:#2bfbae;margin-bottom:8px;font-weight:600;"></div>
      <div style="display:flex;gap:8px;">
        <button id="nfg-close-now" style="flex:1;padding:9px 12px;border-radius:10px;border:none;background:#2bfbae;color:#052015;font-weight:700;font-size:12px;cursor:pointer;">Close Tab &amp; Work</button>
        <button id="nfg-submit-reason" style="flex:1;padding:9px 12px;border-radius:10px;border:1px solid rgba(255,255,255,0.16);background:#182334;color:#e2e8f0;font-weight:600;font-size:12px;cursor:pointer;">Submit to AI</button>
      </div>
    `;

    panelEl.querySelector('#nfg-close-now').onclick = async () => {
      await actions.sendDecision('stop_watching');
      triggerForceCloseTab(hubOrigin, actions.closeTab, 'Nice call — heading back to your tasks!');
    };

    panelEl.querySelector('#nfg-submit-reason').onclick = async () => {
      const ta = panelEl.querySelector('#nfg-reason-input');
      const stEl = panelEl.querySelector('#nfg-ai-status');
      const btn = panelEl.querySelector('#nfg-submit-reason');
      const text = (ta.value || '').trim();
      if (!text) {
        stEl.style.display = 'block';
        stEl.style.color = '#fca5a5';
        stEl.textContent = 'Type your reasoning first — or click Close Tab & Work.';
        return;
      }
      // Keep video playing while Node AI analyzes the typed reasoning!
      btn.disabled = true;
      btn.textContent = 'AI Checking…';
      stEl.style.display = 'block';
      stEl.style.color = '#2bfbae';
      stEl.textContent = '✦ Node AI is evaluating your reasoning while playback continues…';

      const meta = extractVideoMetadata();
      const resp = await actions.submitJustification({
        reasoning: text,
        video_title: meta.video_title,
        channel: meta.channel,
        description_snippet: meta.description_snippet,
        duration_seconds: meta.duration_seconds,
        url: meta.url
      });
      const jr = resp && (resp.justification_result || (resp.data && resp.data.justification_result));
      if (jr && jr.approved) {
        updateStatusBadge(true, `Approved (+${jr.granted_minutes || 20}m)`);
        if (panelEl) { panelEl.remove(); panelEl = null; activePromptKey = ''; }
      } else {
        const why = (jr && jr.explanation) || 'Reasoning was not sufficient given tonight\\'s tasks & bedtime.';
        triggerForceCloseTab(hubOrigin, actions.closeTab, why);
      }
    };
  }
"""

    def _match_patterns(self) -> List[str]:
        state = self._load()
        patterns: List[str] = []
        for p in state.get("platforms") or DEFAULT_SOCIAL_PLATFORMS:
            if not p.get("enabled", True):
                continue
            for dom in p.get("domains") or []:
                d = str(dom).strip().lower().lstrip("www.")
                if d:
                    pat = f"*://*.{d}/*"
                    pat_bare = f"*://{d}/*"
                    if pat not in patterns:
                        patterns.append(pat)
                    if pat_bare not in patterns:
                        patterns.append(pat_bare)
        if "*://*.youtube.com/*" not in patterns:
            patterns.append("*://*.youtube.com/*")
        return patterns

    def build_userscript(self, hub_origin: str, guard_token: str = "") -> str:
        origin = hub_origin.rstrip("/")
        overlay_js = self._overlay_ui_js()
        match_lines = "\n".join(f"// @match        {m}" for m in self._match_patterns())
        return f"""// ==UserScript==
// @name         Node — Universal Social & Media Focus Guard (v10.0)
// @namespace    http://node.local/
// @version      10.0
// @description  Guards YouTube, Instagram, TikTok, Facebook, X/Twitter, Reddit, Twitch, Netflix & custom distraction domains with schedule-aware AI accountability.
{match_lines}
// @grant        GM_xmlhttpRequest
// @grant        window.close
// @connect      *
// ==/UserScript==

(function() {{
  'use strict';
  const HUB_ORIGIN = {json.dumps(origin)};
  const GUARD_TOKEN = {json.dumps(guard_token)};
  const POLL_MS = 10000;
  {overlay_js}

  function hubPost(path, body) {{
    const hdrs = {{ 'Content-Type': 'application/json' }};
    if (GUARD_TOKEN) hdrs['X-Node-Guard-Token'] = GUARD_TOKEN;
    return new Promise((resolve, reject) => {{
      if (typeof GM_xmlhttpRequest === 'function') {{
        GM_xmlhttpRequest({{
          method: 'POST',
          url: HUB_ORIGIN + path,
          headers: hdrs,
          data: JSON.stringify(body),
          timeout: 25000,
          onload: (res) => {{
            try {{ resolve(JSON.parse(res.responseText)); }}
            catch (e) {{ reject(e); }}
          }},
          onerror: reject,
          ontimeout: reject
        }});
      }} else {{
        fetch(HUB_ORIGIN + path, {{
          method: 'POST',
          headers: hdrs,
          body: JSON.stringify(body)
        }}).then(r => r.json()).then(resolve).catch(reject);
      }}
    }});
  }}

  const actions = {{
    sendDecision: (action) => hubPost('/api/v1/focus-guard/respond', {{ action }}).catch(() => ({{}})),
    submitJustification: (payload) => hubPost('/api/v1/focus-guard/justify', payload).catch(() => ({{}})),
    closeTab: async () => {{ try {{ window.close(); }} catch (_) {{}} }}
  }};

  async function heartbeat(elapsedSec, forceEnded = false) {{
    const host = (location.hostname || '').toLowerCase();
    const path = location.pathname || '';
    if (host.includes('youtube.com') && !path.startsWith('/watch') && !path.startsWith('/shorts')) return;
    const meta = extractVideoMetadata();
    if (!host.includes('youtube.com')) meta.playing = !document.hidden;
    if (forceEnded) meta.video_ended = true;
    meta.elapsed_seconds = elapsedSec;
    try {{
      const data = await hubPost('/api/v1/focus-guard/heartbeat', meta);
      updateStatusBadge(true, 'Node Guard');
      ensureAccountabilityUI(data, HUB_ORIGIN, actions);
    }} catch (_) {{
      updateStatusBadge(false, 'Node unreachable');
    }}
  }}

  window.addEventListener('yt-navigate-finish', () => setTimeout(() => heartbeat(2), 900));
  window.addEventListener('popstate', () => setTimeout(() => heartbeat(2), 700));
  document.addEventListener('play', () => setTimeout(() => heartbeat(1), 500), true);
  document.addEventListener('ended', () => heartbeat(1, true), true);
  setInterval(() => heartbeat(Math.round(POLL_MS / 1000)), POLL_MS);
  setTimeout(() => heartbeat(2), 1600);
}})();
"""

    def build_extension_zip(self, hub_origin: str, guard_token: str = "") -> bytes:
        default_origin = hub_origin.rstrip("/")
        matches = self._match_patterns()
        manifest = {
            "manifest_version": 3,
            "name": "Node — Universal Social & Media Focus Guard",
            "version": "10.0.0",
            "description": "Monitors YouTube, Instagram, TikTok, Facebook, X/Twitter, Reddit, Twitch, Netflix & custom domains with AI accountability and tab force-close.",
            "permissions": ["storage", "tabs"],
            "host_permissions": [
                *matches,
                "http://*/*",
                "https://*/*",
            ],
            "background": {
                "service_worker": "background.js",
            },
            "action": {
                "default_popup": "popup.html",
                "default_title": "Node Focus Guard",
            },
            "content_scripts": [
                {
                    "matches": matches,
                    "js": ["content.js"],
                    "run_at": "document_idle",
                }
            ],
        }

        background_js = f"""// Node YouTube Focus Guard v7.0 — MV3 Background Service Worker
// Supports real tab force-closing via chrome.tabs.remove(sender.tab.id) when Node AI rejects an excuse!

const DEFAULT_HUB_ORIGIN = {json.dumps(default_origin)};
const DEFAULT_GUARD_TOKEN = {json.dumps(guard_token)};

async function getHubConfig() {{
  return new Promise((resolve) => {{
    chrome.storage.local.get(['hubOrigin', 'guardToken'], (res) => {{
      const url = (res && res.hubOrigin ? String(res.hubOrigin) : DEFAULT_HUB_ORIGIN).trim().replace(/\\/+$/, '');
      const tok = (res && res.guardToken ? String(res.guardToken) : DEFAULT_GUARD_TOKEN).trim();
      resolve({{ hubOrigin: url || DEFAULT_HUB_ORIGIN, guardToken: tok }});
    }});
  }});
}}

async function callHub(path, method, payload) {{
  const {{ hubOrigin, guardToken }} = await getHubConfig();
  const headers = {{ 'Content-Type': 'application/json' }};
  if (guardToken) headers['X-Node-Guard-Token'] = guardToken;
  const opts = {{ method: method || 'GET', headers }};
  if (payload !== undefined) opts.body = JSON.stringify(payload);
  const res = await fetch(hubOrigin + path, opts);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const data = await res.json();
  chrome.storage.local.set({{
    lastOkTs: Date.now(),
    lastError: '',
    lastVerdict: (data && data.session && data.session.verdict) || 'connected',
  }});
  return {{ ok: true, data, hubOrigin }};
}}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {{
  (async () => {{
    try {{
      if (msg.type === 'NODE_YT_HEARTBEAT') {{
        sendResponse(await callHub('/api/v1/focus-guard/heartbeat', 'POST', msg.payload));
      }} else if (msg.type === 'NODE_YT_DECISION') {{
        sendResponse(await callHub('/api/v1/focus-guard/respond', 'POST', {{ action: msg.action }}));
      }} else if (msg.type === 'NODE_YT_JUSTIFY') {{
        sendResponse(await callHub('/api/v1/focus-guard/justify', 'POST', msg.payload));
      }} else if (msg.type === 'NODE_YT_CLOSE_TAB') {{
        if (sender && sender.tab && sender.tab.id !== undefined) {{
          await chrome.tabs.remove(sender.tab.id);
        }}
        sendResponse({{ ok: true, closed: true }});
      }} else if (msg.type === 'NODE_YT_TEST') {{
        if (msg.hubOrigin) {{
          await new Promise((r) => chrome.storage.local.set({{ hubOrigin: String(msg.hubOrigin).trim().replace(/\\/+$/, '') }}, r));
        }}
        sendResponse(await callHub('/api/v1/focus-guard/status', 'GET'));
      }} else {{
        sendResponse({{ ok: false, error: 'Unknown message' }});
      }}
    }} catch (err) {{
      const errMsg = err && err.message ? err.message : String(err);
      chrome.storage.local.set({{ lastError: errMsg }});
      sendResponse({{ ok: false, error: errMsg }});
    }}
  }})();
  return true;
}});
"""

        content_js = f"""(function() {{
  'use strict';
  const POLL_MS = 10000;
  {self._overlay_ui_js()}

  function sendMsg(msg) {{
    return new Promise((resolve) => {{
      try {{
        chrome.runtime.sendMessage(msg, (resp) => {{
          if (chrome.runtime.lastError) {{
            resolve({{ ok: false, error: chrome.runtime.lastError.message }});
          }} else {{
            resolve(resp || {{ ok: false }});
          }}
        }});
      }} catch (e) {{
        resolve({{ ok: false, error: String(e) }});
      }}
    }});
  }}

  const actions = {{
    sendDecision: (action) => sendMsg({{ type: 'NODE_YT_DECISION', action }}),
    submitJustification: (payload) => sendMsg({{ type: 'NODE_YT_JUSTIFY', payload }}),
    closeTab: () => sendMsg({{ type: 'NODE_YT_CLOSE_TAB' }})
  }};

  async function heartbeat(elapsedSec, forceEnded = false) {{
    const path = location.pathname || '';
    if (!path.startsWith('/watch') && !path.startsWith('/shorts')) return;
    const meta = extractVideoMetadata();
    if (forceEnded) meta.video_ended = true;
    meta.elapsed_seconds = elapsedSec;
    const resp = await sendMsg({{ type: 'NODE_YT_HEARTBEAT', payload: meta }});
    if (resp && resp.ok && resp.data) {{
      updateStatusBadge(true, 'Node Guard');
      ensureAccountabilityUI(resp.data, resp.hubOrigin, actions);
    }} else {{
      updateStatusBadge(false, 'Node offline — click extension icon');
    }}
  }}

  window.addEventListener('yt-navigate-finish', () => setTimeout(() => heartbeat(2), 900));
  document.addEventListener('play', () => setTimeout(() => heartbeat(1), 500), true);
  document.addEventListener('ended', () => heartbeat(1, true), true);
  setInterval(() => heartbeat(Math.round(POLL_MS / 1000)), POLL_MS);
  setTimeout(() => heartbeat(2), 1400);
}})();
"""

        popup_html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { width: 315px; margin: 0; padding: 14px; background: #0d131d; color: #f1f5f9; font: 12.5px/1.45 Inter, -apple-system, BlinkMacSystemFont, sans-serif; }
    .hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
    .brand { font-weight: 700; font-size: 13.5px; color: #f8fafc; letter-spacing: -0.01em; }
    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(43,251,174,0.14); color: #2bfbae; font-weight: 600; }
    .pill.err { background: rgba(248,113,113,0.16); color: #fca5a5; }
    label { display: block; font-size: 11px; color: #94a3b8; margin-bottom: 4px; }
    input { width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 9px; border: 1px solid rgba(255,255,255,0.14); background: #141d2b; color: #f8fafc; font-size: 12px; margin-bottom: 8px; outline: none; }
    input:focus { border-color: #2bfbae; }
    button { width: 100%; padding: 8px 10px; border-radius: 9px; border: none; background: #2bfbae; color: #052015; font-weight: 700; font-size: 12px; cursor: pointer; }
    .meta { margin-top: 8px; font-size: 11px; color: #94a3b8; word-break: break-word; }
  </style>
</head>
<body>
  <div class="hdr">
    <span class="brand">Node • Focus Guard v7</span>
    <span id="statusPill" class="pill">Checking…</span>
  </div>
  <label for="hubUrl">Node Server URL (Tailscale IP or Cloudflare Host)</label>
  <input id="hubUrl" type="text" placeholder="http://100.x.y.z:8080">
  <button id="saveBtn">Save &amp; Test Connection</button>
  <div id="meta" class="meta"></div>
  <script src="popup.js"></script>
</body>
</html>
"""

        popup_js = f"""const DEFAULT_HUB_ORIGIN = {json.dumps(default_origin)};
const input = document.getElementById('hubUrl');
const pill = document.getElementById('statusPill');
const meta = document.getElementById('meta');
const btn = document.getElementById('saveBtn');

function setStatus(ok, label, detail) {{
  pill.textContent = label;
  pill.className = ok ? 'pill' : 'pill err';
  meta.textContent = detail || '';
}}

chrome.storage.local.get(['hubOrigin', 'lastOkTs', 'lastError'], (st) => {{
  input.value = (st && st.hubOrigin) ? st.hubOrigin : DEFAULT_HUB_ORIGIN;
  if (st && st.lastError) {{
    setStatus(false, 'Offline', st.lastError);
  }} else if (st && st.lastOkTs) {{
    const sec = Math.max(0, Math.round((Date.now() - st.lastOkTs) / 1000));
    setStatus(true, 'Connected', 'Last sync ' + sec + 's ago');
  }} else {{
    testNow();
  }}
}});

function testNow() {{
  const url = input.value.trim().replace(/\\/+$/, '');
  setStatus(true, 'Testing…', url);
  chrome.runtime.sendMessage({{ type: 'NODE_YT_TEST', hubOrigin: url }}, (resp) => {{
    if (resp && resp.ok) {{
      const mins = resp.data ? resp.data.total_youtube_minutes_today : 0;
      setStatus(true, 'Connected', 'Synced with ' + resp.hubOrigin + ' • ' + mins + 'm today');
    }} else {{
      setStatus(false, 'Unreachable', (resp && resp.error) || 'Check Tailscale IP & port');
    }}
  }});
}}

btn.addEventListener('click', testNow);
"""

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("background.js", background_js)
            zf.writestr("content.js", content_js)
            zf.writestr("popup.html", popup_html)
            zf.writestr("popup.js", popup_js)
        return buf.getvalue()


focus_guard_service = FocusGuardService()
