from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field as PydanticField, field_validator
from sqlmodel import Field, SQLModel


def normalize_priority_value(val: Any) -> str:
    if val is None:
        return "normal"
    if isinstance(val, (int, float)):
        n = int(val)
        if n <= 0:
            return "critical"
        if n == 1:
            return "high"
        if n == 2:
            return "normal"
        return "low"
    s = str(val).strip().lower()
    if s in {"0", "p0", "urgent", "critical"}:
        return "critical"
    if s in {"1", "p1", "high", "important"}:
        return "high"
    if s in {"2", "p2", "normal", "medium", "med", ""}:
        return "normal"
    if s in {"3", "4", "p3", "low", "minor"}:
        return "low"
    return "normal"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# SQLite Tables (100% compatible with /data/hub.db)
# ===========================================================================
class AuthSession(SQLModel, table=True):
    """Server-side session store (stores ONLY SHA-256 hash of opaque HttpOnly cookie token)."""

    __tablename__ = "auth_sessions"

    token_hash: str = Field(primary_key=True, max_length=128)
    username: str = Field(index=True, max_length=120)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(index=True)
    last_seen_at: datetime = Field(default_factory=utc_now)
    client_info: Optional[str] = Field(default=None, max_length=300)


class ChatThread(SQLModel, table=True):
    """Named AI chat context so windows can plug into or switch named contexts."""

    __tablename__ = "chat_threads"

    thread_id: str = Field(primary_key=True, max_length=80)
    name: str = Field(default="Chat 1", max_length=160)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now, index=True)


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_app: str = Field(index=True, max_length=120)
    title: str = Field(max_length=300)
    message: str = Field(default="", max_length=2000)
    ai_summary: Optional[str] = Field(default=None, max_length=500)
    status: str = Field(default="active", index=True, max_length=32)
    timestamp: datetime = Field(default_factory=utc_now, index=True)
    priority: str = Field(default="normal", index=True, max_length=32)
    read_state: bool = Field(default=False, index=True)
    is_important: bool = Field(default=True, index=True)
    importance_score: float = Field(default=0.75)
    importance_reason: Optional[str] = Field(default=None, max_length=200)
    user_importance_override: Optional[bool] = Field(default=None)
    category: str = Field(default="General", index=True, max_length=64)
    ai_processed: bool = Field(default=False)
    indicator_type: str = Field(default="dom_badge", max_length=64)
    raw_payload: Optional[str] = Field(default=None)

    # ---- v10.2 tag-first Inbox -------------------------------------------
    tags_csv: str = Field(default="", index=True, max_length=400)
    sender: Optional[str] = Field(default=None, index=True, max_length=200)
    body_text: Optional[str] = Field(default=None)
    body_fetched_at: Optional[datetime] = Field(default=None)
    dedupe_key: Optional[str] = Field(default=None, index=True, max_length=64)
    dedupe_sources: Optional[str] = Field(default=None, max_length=400)
    merged_count: int = Field(default=1)
    thread_count: int = Field(default=0)
    has_attachment: bool = Field(default=False)
    starred: bool = Field(default=False, index=True)
    folder: str = Field(default="inbox", index=True, max_length=24)  # inbox | archive | snoozed
    tag_confidence: float = Field(default=0.0)
    tag_engine: Optional[str] = Field(default=None, max_length=40)
    tag_corrected_by_user: bool = Field(default=False)


class CourseVisibility(SQLModel, table=True):
    """Per-course user control: hide a course from GPA averages and/or the tracker."""

    __tablename__ = "course_visibility"

    id: Optional[int] = Field(default=None, primary_key=True)
    course_key: str = Field(index=True, max_length=180)
    source_app: str = Field(default="", index=True, max_length=120)
    tracked: bool = Field(default=True)
    counts_toward_gpa: bool = Field(default=True)
    counts_toward_average: bool = Field(default=True)
    tab_id: Optional[str] = Field(default=None, index=True, max_length=80)
    hidden: bool = Field(default=False)
    pinned: bool = Field(default=False)
    updated_at: datetime = Field(default_factory=utc_now)


class GradeTab(SQLModel, table=True):
    """A named group of courses (e.g. 'Core', 'Electives') with its own averages."""

    __tablename__ = "grade_tabs"

    id: Optional[str] = Field(primary_key=True, max_length=80)
    name: str = Field(max_length=120)
    color: str = Field(default="emerald", max_length=24)
    course_keys_json: str = Field(default="[]")
    match_rules_json: str = Field(default="[]")  # e.g. [{"source_app": "Canvas", "term": "Q1"}]
    order: int = Field(default=100)
    show_in_briefing: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InboxLearningHint(SQLModel, table=True):
    """A user correction used to teach the tag gate (exportable = local fine-tune set)."""

    __tablename__ = "inbox_learning_hints"

    id: Optional[int] = Field(default=None, primary_key=True)
    notification_id: Optional[int] = Field(default=None, index=True)
    source_app: str = Field(default="", index=True, max_length=120)
    sender_key: str = Field(default="", index=True, max_length=200)
    subject: str = Field(default="", max_length=300)
    body_excerpt: str = Field(default="", max_length=1200)
    from_tag: Optional[str] = Field(default=None, max_length=64)
    to_tag: str = Field(default="", index=True, max_length=64)
    action: str = Field(default="move", max_length=32)  # move | archive | important | not_important
    created_at: datetime = Field(default_factory=utc_now, index=True)


class CourseGrade(SQLModel, table=True):
    """Tracked course grade row (PowerSchool / SIS) with persistent history, assignments & cron diffing."""

    __tablename__ = "course_grades"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_app: str = Field(default="PowerSchool", index=True, max_length=120)
    course_key: str = Field(index=True, max_length=180)
    course_name: str = Field(max_length=200)
    teacher: Optional[str] = Field(default=None, max_length=120)
    period: Optional[str] = Field(default=None, max_length=40)
    term: str = Field(default="Current", max_length=40)
    letter_grade: str = Field(default="", max_length=24)
    score_percent: Optional[float] = Field(default=None)
    prev_letter_grade: Optional[str] = Field(default=None, max_length=24)
    prev_score_percent: Optional[float] = Field(default=None)
    delta_percent: Optional[float] = Field(default=None)
    missing_count: int = Field(default=0)
    scores_url: Optional[str] = Field(default=None, max_length=1200)
    history_json: str = Field(default="[]")
    term_grades_json: str = Field(default="{}")
    assignments_json: str = Field(default="[]")
    recent_items_json: str = Field(default="[]")
    updated_at: datetime = Field(default_factory=utc_now, index=True)


class GradeChangeLog(SQLModel, table=True):
    """Historical log of detected PowerSchool / SIS grade changes across cron scans."""

    __tablename__ = "grade_change_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_app: str = Field(default="PowerSchool", index=True, max_length=120)
    course_name: str = Field(max_length=200)
    old_display: str = Field(max_length=80)
    new_display: str = Field(max_length=80)
    delta_percent: Optional[float] = Field(default=None)
    summary: str = Field(default="", max_length=500)
    detected_at: datetime = Field(default_factory=utc_now, index=True)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_app: str = Field(default="Node", index=True, max_length=120)
    title: str = Field(max_length=400)
    due_date: Optional[str] = Field(default=None, max_length=64)
    completed: bool = Field(default=False, index=True)
    category: str = Field(default="General", index=True, max_length=80)
    priority: str = Field(default="normal", max_length=32)
    sort_order: int = Field(default=0, index=True)
    estimated_minutes: Optional[int] = Field(default=None)
    scheduled_block: Optional[str] = Field(default=None, max_length=120)
    deferred_from_date: Optional[str] = Field(default=None, max_length=32)


class CronRunLog(SQLModel, table=True):
    """Hourly & on-demand AI schedule/priority/YouTube-budget check log."""

    __tablename__ = "cron_run_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    ran_at: datetime = Field(default_factory=utc_now, index=True)
    trigger: str = Field(default="hourly_cron", max_length=40)
    bedtime_hhmm: str = Field(default="22:00", max_length=16)
    free_minutes_until_bedtime: int = Field(default=0)
    due_tonight_minutes: int = Field(default=0)
    scheduled_tonight_minutes: int = Field(default=0)
    youtube_slack_minutes: int = Field(default=0)
    youtube_allowed: bool = Field(default=True)
    on_schedule_status: str = Field(default="on_track", max_length=40)
    new_notifications_count: int = Field(default=0)
    prioritized_tasks_json: str = Field(default="[]")
    deferred_tasks_json: str = Field(default="[]")
    ai_summary: str = Field(default="")


class ScraperHeartbeat(SQLModel, table=True):
    __tablename__ = "scraper_heartbeats"

    source_app: str = Field(primary_key=True, max_length=120)
    indicator_type: str = Field(default="dom_badge", max_length=64)
    status: str = Field(default="not_started", max_length=32)
    unread_count: int = Field(default=0)
    app_url: Optional[str] = Field(default=None, max_length=500)
    zero_knowledge: bool = Field(default=False)
    total_pings: int = Field(default=0)
    last_seen: datetime = Field(default_factory=utc_now)


class AIBriefing(SQLModel, table=True):
    __tablename__ = "ai_briefings"

    id: Optional[int] = Field(default=None, primary_key=True)
    markdown: str = Field(default="")
    model: str = Field(default="qwen3.5:2b", max_length=120)
    source: str = Field(default="secretary_engine", max_length=64)
    unread_count: int = Field(default=0)
    active_tasks_count: int = Field(default=0)
    latency_ms: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    cards_json: Optional[str] = Field(default=None)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: str = Field(default="main", index=True, max_length=80)
    role: str = Field(index=True, max_length=24)  # user | assistant
    content: str
    actions_json: Optional[str] = Field(default=None)
    source: str = Field(default="secretary_engine", max_length=64)
    created_at: datetime = Field(default_factory=utc_now, index=True)


class CalendarFeed(SQLModel, table=True):
    """External calendar subscription link (`https://...ics` or `webcal://...`)."""

    __tablename__ = "calendar_feeds"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=160)
    url: str = Field(max_length=1200)
    color: str = Field(default="sky", max_length=32)
    category: str = Field(default="External", max_length=80)
    enabled: bool = Field(default=True)
    last_synced_at: Optional[datetime] = Field(default=None)
    last_status: str = Field(default="never_synced", max_length=80)
    last_error: Optional[str] = Field(default=None, max_length=600)
    event_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now)


class CalendarEvent(SQLModel, table=True):
    """Full calendar event supporting recurring blocks, AI time-blocking, and ICS sync."""

    __tablename__ = "calendar_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(max_length=300)
    start_at: str = Field(index=True, max_length=40)  # ISO local YYYY-MM-DDTHH:MM
    end_at: str = Field(index=True, max_length=40)
    all_day: bool = Field(default=False)
    category: str = Field(default="Event", index=True, max_length=80)
    color: str = Field(default="amber", max_length=32)
    location: Optional[str] = Field(default=None, max_length=240)
    notes: Optional[str] = Field(default=None, max_length=2000)
    source: str = Field(default="Manual", max_length=80)
    feed_id: Optional[int] = Field(default=None, index=True)
    external_uid: Optional[str] = Field(default=None, index=True, max_length=300)
    recurrence_rule: Optional[str] = Field(default=None, max_length=120)
    series_id: Optional[str] = Field(default=None, index=True, max_length=80)
    linked_task_id: Optional[int] = Field(default=None, index=True)
    linked_goal_id: Optional[int] = Field(default=None, index=True)
    completed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class LongTermGoal(SQLModel, table=True):
    """Persistent long-term goal remembered by AI Secretary with daily micro-habits."""

    __tablename__ = "long_term_goals"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(max_length=300)
    category: str = Field(default="General", index=True, max_length=80)
    target_date: Optional[str] = Field(default=None, max_length=32)
    why_it_matters: Optional[str] = Field(default=None, max_length=800)
    daily_habit: str = Field(default="", max_length=400)
    minutes_per_day: int = Field(default=25)
    progress_percent: int = Field(default=0)
    streak_days: int = Field(default=0)
    last_checkin_date: Optional[str] = Field(default=None, max_length=32)
    milestones_json: str = Field(default="[]")
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now)


# ===========================================================================
# API Request / Response Schemas
# ===========================================================================
class AuthLoginRequest(BaseModel):
    username: str = PydanticField(..., min_length=1, max_length=120)
    password: str = PydanticField(..., min_length=1, max_length=300)


class ChatThreadCreate(BaseModel):
    name: Optional[str] = PydanticField(default=None, max_length=160)
    thread_id: Optional[str] = PydanticField(default=None, max_length=80)


class ChatThreadUpdate(BaseModel):
    name: str = PydanticField(..., min_length=1, max_length=160)


class NotificationBulkRequest(BaseModel):
    ids: List[int] = PydanticField(..., min_length=1, max_length=500)
    action: str = PydanticField(..., min_length=1, max_length=40)


class OllamaStatusInfo(BaseModel):
    reachable: bool
    base_url: str
    configured_model: str
    model_installed: bool
    available_models: List[str] = PydanticField(default_factory=list)
    detail: str


class PingIn(BaseModel):
    source_app: str = PydanticField(..., min_length=1, max_length=120)
    indicator_type: str = PydanticField(default="dom_badge", max_length=64)
    title: Optional[str] = PydanticField(default=None, max_length=300)
    message: Optional[str] = PydanticField(default=None, max_length=2000)
    priority: Optional[str] = PydanticField(default=None, max_length=32)
    unread_count: int = 1
    app_url: Optional[str] = None
    zero_knowledge: bool = False
    simulated: bool = False
    create_notification: bool = True
    raw_payload: Optional[Dict[str, Any]] = None


class NotificationCreate(BaseModel):
    source_app: str = PydanticField(..., min_length=1, max_length=120)
    title: str = PydanticField(..., min_length=1, max_length=300)
    message: str = PydanticField(default="", max_length=2000)
    priority: str = PydanticField(default="normal")
    status: str = PydanticField(default="active")
    indicator_type: str = PydanticField(default="dom_badge")
    raw_payload: Optional[str] = None


class NotificationUpdate(BaseModel):
    title: Optional[str] = None
    read_state: Optional[bool] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    is_important: Optional[bool] = None
    ai_summary: Optional[str] = None


class TaskCreate(BaseModel):
    source_app: str = PydanticField(default="Node", max_length=120)
    title: str = PydanticField(..., min_length=1, max_length=400)
    due_date: Optional[str] = None
    category: str = PydanticField(default="General", max_length=80)
    priority: str = PydanticField(default="normal", max_length=32)
    sort_order: Optional[int] = None
    estimated_minutes: Optional[int] = None

    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority(cls, v: Any) -> str:
        return normalize_priority_value(v)


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    due_date: Optional[str] = None
    completed: Optional[bool] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    sort_order: Optional[int] = None
    estimated_minutes: Optional[int] = None
    scheduled_block: Optional[str] = None
    deferred_from_date: Optional[str] = None

    @field_validator("priority", mode="before")
    @classmethod
    def _coerce_priority_opt(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return normalize_priority_value(v)


class TaskReorderRequest(BaseModel):
    ordered_ids: List[int] = PydanticField(default_factory=list)


class TaskAIExtractRequest(BaseModel):
    text: str = PydanticField(..., min_length=1, max_length=50000)
    source_name: str = PydanticField(default="Notes", max_length=120)


class TaskSyncSourcesRequest(BaseModel):
    include_calendar: bool = True
    include_inbox: bool = True
    days_ahead: int = PydanticField(default=14, ge=1, le=60)


class BriefingGenerateRequest(BaseModel):
    focus_note: Optional[str] = PydanticField(default=None, max_length=500)


class ChatRequest(BaseModel):
    message: str = PydanticField(..., min_length=1, max_length=6000)
    thread_id: str = PydanticField(default="main", max_length=80)
    current_view: Optional[str] = None
    enable_thinking: Optional[bool] = None
    model: Optional[str] = PydanticField(default=None, max_length=160)


class MarkdownDocUpdate(BaseModel):
    content: str = PydanticField(..., max_length=50000)


class QuickNoteAppendRequest(BaseModel):
    text: str = PydanticField(..., min_length=1, max_length=2000)
    category: str = PydanticField(default="Note", max_length=60)


class AIAssistAppRequest(BaseModel):
    query: str = PydanticField(..., min_length=1, max_length=500)
    zero_knowledge: bool = False


class CalendarEventCreate(BaseModel):
    title: str = PydanticField(..., min_length=1, max_length=300)
    start_at: str = PydanticField(..., min_length=10, max_length=40)
    end_at: str = PydanticField(..., min_length=10, max_length=40)
    all_day: bool = False
    category: str = PydanticField(default="Event", max_length=80)
    color: str = PydanticField(default="amber", max_length=32)
    location: Optional[str] = PydanticField(default=None, max_length=240)
    notes: Optional[str] = PydanticField(default=None, max_length=2000)
    source: str = PydanticField(default="Manual", max_length=80)
    repeat_days: Optional[List[int]] = None  # 0=Mon .. 6=Sun
    repeat_weeks: int = PydanticField(default=1, ge=1, le=26)
    linked_task_id: Optional[int] = None
    linked_goal_id: Optional[int] = None


class CalendarEventUpdate(BaseModel):
    title: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    all_day: Optional[bool] = None
    category: Optional[str] = None
    color: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    completed: Optional[bool] = None


class CalendarEventRead(BaseModel):
    id: int
    title: str
    start_at: str
    end_at: str
    all_day: bool
    category: str
    color: str
    location: Optional[str] = None
    notes: Optional[str] = None
    source: str
    feed_id: Optional[int] = None
    external_uid: Optional[str] = None
    recurrence_rule: Optional[str] = None
    series_id: Optional[str] = None
    linked_task_id: Optional[int] = None
    linked_goal_id: Optional[int] = None
    completed: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CalendarFeedCreate(BaseModel):
    name: str = PydanticField(..., min_length=1, max_length=160)
    url: str = PydanticField(..., min_length=4, max_length=1200)
    color: str = PydanticField(default="sky", max_length=32)
    category: str = PydanticField(default="External", max_length=80)


class CalendarFeedRead(BaseModel):
    id: int
    name: str
    url: str
    color: str
    category: str
    enabled: bool
    last_synced_at: Optional[datetime] = None
    last_status: str
    last_error: Optional[str] = None
    event_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class CalendarImportICSRequest(BaseModel):
    ics_content: str = PydanticField(..., min_length=10, max_length=2_000_000)
    source_name: str = PydanticField(default="ICS Import", max_length=80)
    category: str = PydanticField(default="Imported", max_length=80)
    color: str = PydanticField(default="teal", max_length=32)


class CalendarCoordinateRequest(BaseModel):
    target_date: Optional[str] = None
    day_start_hour: int = PydanticField(default=8, ge=5, le=20)
    day_end_hour: int = PydanticField(default=22, ge=12, le=23)
    include_tasks: bool = True
    include_goals: bool = True


class LongTermGoalCreate(BaseModel):
    title: str = PydanticField(..., min_length=1, max_length=300)
    category: str = PydanticField(default="General", max_length=80)
    target_date: Optional[str] = None
    why_it_matters: Optional[str] = None
    daily_habit: Optional[str] = None
    minutes_per_day: int = PydanticField(default=25, ge=5, le=240)
    milestones: Optional[List[Dict[str, Any]]] = None


class LongTermGoalUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    target_date: Optional[str] = None
    why_it_matters: Optional[str] = None
    daily_habit: Optional[str] = None
    minutes_per_day: Optional[int] = None
    progress_percent: Optional[int] = None
    streak_days: Optional[int] = None
    milestones: Optional[List[Dict[str, Any]]] = None
    active: Optional[bool] = None


class LongTermGoalRead(BaseModel):
    id: int
    title: str
    category: str
    target_date: Optional[str] = None
    why_it_matters: Optional[str] = None
    daily_habit: str
    minutes_per_day: int
    progress_percent: int
    streak_days: int
    last_checkin_date: Optional[str] = None
    milestones: List[Dict[str, Any]]
    active: bool
    created_at: datetime


class GoalCheckinRequest(BaseModel):
    minutes_logged: Optional[int] = None
    note: Optional[str] = None


class GoalBreakdownRequest(BaseModel):
    title: str = PydanticField(..., min_length=2, max_length=300)
    category: str = PydanticField(default="General", max_length=80)
    target_date: Optional[str] = None
    minutes_per_day: int = PydanticField(default=25, ge=5, le=240)


class CronRunRequest(BaseModel):
    trigger: str = PydanticField(default="manual", max_length=40)
    reference_now_iso: Optional[str] = PydanticField(default=None, max_length=40)


class CronSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = PydanticField(default=None, ge=5, le=360)
    bedtime_hhmm: Optional[str] = PydanticField(default=None, max_length=16)
    wake_hhmm: Optional[str] = PydanticField(default=None, max_length=16)
    sleep_buffer_minutes: Optional[int] = PydanticField(default=None, ge=0, le=90)


class YoutubeJustifyRequest(BaseModel):
    reasoning: str = PydanticField(..., min_length=1, max_length=2000)
    video_title: Optional[str] = PydanticField(default=None, max_length=300)
    channel: Optional[str] = PydanticField(default=None, max_length=160)
    description_snippet: Optional[str] = PydanticField(default=None, max_length=1200)
    duration_seconds: Optional[int] = None
    url: Optional[str] = PydanticField(default=None, max_length=500)
    platform: Optional[str] = PydanticField(default=None, max_length=80)


class OSSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    settings_patch: Dict[str, Any] = PydanticField(default_factory=dict)
    values: Dict[str, Any] = PydanticField(default_factory=dict)
    custom_css: Optional[str] = PydanticField(default=None, max_length=24000)
    custom_js: Optional[str] = PydanticField(default=None, max_length=24000)
    active_preset_id: Optional[str] = PydanticField(default=None, max_length=120)


class OSPresetSaveRequest(BaseModel):
    preset_id: Optional[str] = PydanticField(default=None, max_length=120)
    name: str = PydanticField(..., min_length=1, max_length=120)
    description: str = PydanticField(default="", max_length=400)
    settings_patch: Dict[str, Any] = PydanticField(default_factory=dict)
    custom_css: str = PydanticField(default="", max_length=24000)
    custom_js: str = PydanticField(default="", max_length=24000)
    activate: bool = True


class OSSettingsAIAssistRequest(BaseModel):
    prompt: str = PydanticField(..., min_length=2, max_length=3000)
    save_as_preset: bool = True
    preset_name: Optional[str] = PydanticField(default=None, max_length=120)
    apply_immediately: bool = True


class NotificationImportanceFeedbackRequest(BaseModel):
    is_important: Optional[bool] = None
    important: Optional[bool] = None

    def resolved(self) -> bool:
        if self.is_important is not None:
            return bool(self.is_important)
        if self.important is not None:
            return bool(self.important)
        return True


class GradeSnapshotIngestRequest(BaseModel):
    source_app: str = PydanticField(default="PowerSchool", max_length=120)
    courses: List[Dict[str, Any]] = PydanticField(default_factory=list)
    emit_pings: bool = True


# ===========================================================================
# v10.0 Production Release Tables & Request Schemas
# ===========================================================================
class AIJob(SQLModel, table=True):
    """Persistent server-side AI background job so client disconnect never aborts generation."""

    __tablename__ = "ai_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_uid: str = Field(index=True, unique=True, max_length=64)
    job_type: str = Field(default="chat", index=True, max_length=40)
    thread_id: str = Field(default="main", index=True, max_length=80)
    prompt: str = Field(default="")
    current_view: Optional[str] = Field(default=None, max_length=80)
    enable_thinking: bool = Field(default=True)
    requested_model: Optional[str] = Field(default=None, max_length=160)
    model_used: Optional[str] = Field(default=None, max_length=160)
    provider_used: Optional[str] = Field(default=None, max_length=120)
    status: str = Field(default="queued", index=True, max_length=32)  # queued | running | completed | failed
    thinking_text: str = Field(default="")
    reply_text: str = Field(default="")
    actions_json: str = Field(default="[]")
    error_text: Optional[str] = Field(default=None)
    user_message_id: Optional[int] = Field(default=None)
    assistant_message_id: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = Field(default=None)


class AgentLogEntry(SQLModel, table=True):
    """Shared multi-agent operational logbook readable/editable by all AI instances & models."""

    __tablename__ = "agent_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_name: str = Field(default="secretary", index=True, max_length=80)
    model_id: str = Field(default="system", max_length=160)
    category: str = Field(default="observation", index=True, max_length=60)
    title: str = Field(default="", max_length=240)
    content: str = Field(default="")
    tags: str = Field(default="", max_length=300)
    pinned: bool = Field(default=False, index=True)
    revision: int = Field(default=1)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class AIProviderUpsertRequest(BaseModel):
    provider_id: Optional[str] = PydanticField(default=None, max_length=80)
    name: str = PydanticField(..., min_length=1, max_length=120)
    protocol: str = PydanticField(default="ollama", max_length=40)  # ollama | openai | anthropic
    base_url: str = PydanticField(..., min_length=4, max_length=500)
    api_key: Optional[str] = PydanticField(default=None, max_length=500)
    models: List[str] = PydanticField(default_factory=list)
    enabled: bool = True
    auto_discover: bool = True


class ModelPingRequest(BaseModel):
    provider_id: Optional[str] = PydanticField(default=None, max_length=80)
    provider_name: str = PydanticField(default="Remote Model Node", max_length=120)
    protocol: str = PydanticField(default="ollama", max_length=40)
    base_url: str = PydanticField(..., min_length=4, max_length=500)
    models: List[str] = PydanticField(default_factory=list)
    api_key: Optional[str] = PydanticField(default=None, max_length=500)
    capabilities: Dict[str, Any] = PydanticField(default_factory=dict)


class AIRoleRoutingRequest(BaseModel):
    chat_model: Optional[str] = PydanticField(default=None, max_length=160)
    decision_model: Optional[str] = PydanticField(default=None, max_length=160)
    # Deprecated alias for chat_model (pre-0.13 clients). Ignored when chat_model is sent.
    primary_model: Optional[str] = PydanticField(default=None, max_length=160)
    enable_web_search: Optional[bool] = None
    auto_agent_logging: Optional[bool] = None
    searxng_url: Optional[str] = PydanticField(default=None, max_length=500)


class AgentLogUpsertRequest(BaseModel):
    log_id: Optional[int] = None
    agent_name: str = PydanticField(default="user-or-agent", max_length=80)
    model_id: str = PydanticField(default="manual", max_length=160)
    category: str = PydanticField(default="shared-context", max_length=60)
    title: str = PydanticField(..., min_length=1, max_length=240)
    content: str = PydanticField(..., min_length=1, max_length=12000)
    tags: str = PydanticField(default="", max_length=300)
    pinned: bool = False


class InstallCatalogAppRequest(BaseModel):
    template_id: str = PydanticField(..., min_length=1, max_length=80)
    custom_name: Optional[str] = PydanticField(default=None, max_length=120)
    custom_url: Optional[str] = PydanticField(default=None, max_length=800)


class DesktopWidgetsSaveRequest(BaseModel):
    widgets: List[Dict[str, Any]] = PydanticField(default_factory=list)


class FocusSiteRuleRequest(BaseModel):
    domain: str = PydanticField(..., min_length=2, max_length=160)
    name: Optional[str] = PydanticField(default=None, max_length=80)
    enabled: bool = True
    daily_limit_minutes: Optional[int] = PydanticField(default=None, ge=1, le=600)
    short_form_immediate: bool = True


class ExtensionInstallRequest(BaseModel):
    manifest: Dict[str, Any] = PydanticField(default_factory=dict)
    html: Optional[str] = PydanticField(default=None, max_length=250000)
    widget_html: Optional[str] = PydanticField(default=None, max_length=150000)
    overwrite: bool = True


class ExtensionStorageRequest(BaseModel):
    data: Dict[str, Any] = PydanticField(default_factory=dict)
    merge: bool = True


class AIFeedbackEntry(SQLModel, table=True):
    """Stores explicit user chat ratings (`good`/`poor`) and implicit frustration signals for disciplined self-improvement."""

    __tablename__ = "ai_feedback"

    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: str = Field(default="main", index=True, max_length=80)
    message_id: Optional[int] = Field(default=None, index=True)
    rating: str = Field(default="poor", index=True, max_length=32)
    reason: str = Field(default="", max_length=600)
    user_prompt: str = Field(default="")
    assistant_reply: str = Field(default="")
    model_id: str = Field(default="auto", max_length=160)
    processed: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)


class AIFeedbackCreateRequest(BaseModel):
    rating: str = PydanticField(..., min_length=2, max_length=32)
    message_id: Optional[int] = None
    thread_id: str = PydanticField(default="main", max_length=80)
    reason: str = PydanticField(default="", max_length=600)
    user_prompt: str = PydanticField(default="", max_length=2000)
    assistant_reply: str = PydanticField(default="", max_length=2000)
    model_id: str = PydanticField(default="auto", max_length=160)


class DriveFolderRequest(BaseModel):
    path: str = PydanticField(..., min_length=1, max_length=400)


class DriveFileWriteRequest(BaseModel):
    path: str = PydanticField(..., min_length=1, max_length=400)
    content: str = PydanticField(default="", max_length=500000)


class DriveMoveRequest(BaseModel):
    src_path: str = PydanticField(..., min_length=1, max_length=400)
    dest_path: str = PydanticField(..., min_length=1, max_length=400)


class InboxCategoriesUpdateRequest(BaseModel):
    action: str = PydanticField(default="set", max_length=32)  # set | add | remove
    categories: List[str] = PydanticField(default_factory=list)
    category: Optional[str] = PydanticField(default=None, max_length=60)
    app_slug: Optional[str] = PydanticField(default=None, max_length=120)




# ===========================================================================
# v10.2 request/response schemas (decision gate, tags, filters, grades tabs,
# api-anything bridges, newtab modes, body harvest)
# ===========================================================================
class InboxTagUpsertRequest(BaseModel):
    name: str = PydanticField(..., min_length=1, max_length=40)
    description: str = PydanticField(default="", max_length=400)
    color: str = PydanticField(default="slate", max_length=24)
    important: bool = True
    aliases: List[str] = PydanticField(default_factory=list)


class InboxTagRenameRequest(BaseModel):
    old_name: str = PydanticField(..., min_length=1, max_length=40)
    new_name: str = PydanticField(..., min_length=1, max_length=40)


class InboxFilterSaveRequest(BaseModel):
    name: str = PydanticField(..., min_length=1, max_length=60)
    query: str = PydanticField(default="", max_length=600)
    color: str = PydanticField(default="slate", max_length=24)
    pinned: bool = True
    order: int = 100
    filter_id: Optional[str] = PydanticField(default=None, max_length=80)


class InboxTagMoveRequest(BaseModel):
    tag: str = PydanticField(..., min_length=1, max_length=64)
    learn: bool = True


class InboxBulkTagRequest(BaseModel):
    ids: List[int] = PydanticField(..., min_length=1, max_length=500)
    add_tags: List[str] = PydanticField(default_factory=list)
    remove_tags: List[str] = PydanticField(default_factory=list)
    set_tags: Optional[List[str]] = None
    archive: Optional[bool] = None
    star: Optional[bool] = None


class InboxQueryRequest(BaseModel):
    query: str = PydanticField(default="", max_length=600)
    limit: int = 200


class InboxHarvestRequest(BaseModel):
    ids: List[int] = PydanticField(default_factory=list, max_length=50)
    limit: int = 10


class DecisionGateSelfTestRequest(BaseModel):
    text: str = PydanticField(default="Quiz 3 is due tonight at 11:59pm, worth 15% of the grade.", max_length=2000)


class ToolPlanRequest(BaseModel):
    message: str = PydanticField(..., min_length=1, max_length=2000)
    thread_id: str = PydanticField(default="main", max_length=80)


class GradeCourseVisibilityRequest(BaseModel):
    course_key: str = PydanticField(..., min_length=1, max_length=180)
    source_app: str = PydanticField(default="", max_length=120)
    tracked: Optional[bool] = None
    counts_toward_gpa: Optional[bool] = None
    counts_toward_average: Optional[bool] = None
    tab_id: Optional[str] = PydanticField(default=None, max_length=80)
    hidden: Optional[bool] = None
    pinned: Optional[bool] = None


class GradeHideRequest(BaseModel):
    course_key: str = PydanticField(..., min_length=1, max_length=180)
    source_app: str = PydanticField(default="", max_length=120)
    hidden: bool = True
    delete_row: bool = False


class GradeTabUpsertRequest(BaseModel):
    id: Optional[str] = PydanticField(default=None, max_length=80)
    name: str = PydanticField(..., min_length=1, max_length=120)
    color: str = PydanticField(default="emerald", max_length=24)
    course_keys: List[str] = PydanticField(default_factory=list)
    match_rules: List[Dict[str, Any]] = PydanticField(default_factory=list)
    order: int = 100


class ApiBridgeLearnRequest(BaseModel):
    app_slug: str = PydanticField(..., min_length=1, max_length=120)
    url: str = PydanticField(default="", max_length=1200)
    operation_name: str = PydanticField(default="", max_length=120)


class ApiBridgeCallRequest(BaseModel):
    operation: str = PydanticField(..., min_length=1, max_length=160)
    args: Dict[str, Any] = PydanticField(default_factory=dict)


class SessionCookieImportRequest(BaseModel):
    app_slug: str = PydanticField(..., min_length=1, max_length=120)
    cookies: List[Dict[str, Any]] = PydanticField(default_factory=list)
    device: str = PydanticField(default="extension", max_length=80)


class NewTabSettingRequest(BaseModel):
    mode: str = PydanticField(default="dashboard", max_length=32)  # dashboard|lock|wallpaper|custom|off
    show_on_open: bool = True
    custom_blocks_csv: str = PydanticField(default="clock,urgent_tasks,next_event,inbox_digest", max_length=400)
    wallpaper_id: str = PydanticField(default="", max_length=120)
    greeting_name: str = PydanticField(default="", max_length=80)
    overlay_strength: int = 35
