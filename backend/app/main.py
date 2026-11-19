import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import json
import io
import logging
import re
import zipfile
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, func, select

from .apps_manager import apps_manager
from .agent_logbook import agent_logbook
from .ai_providers import ai_provider_registry
from .ai_runtime import ai_runtime_manager
from .auth import GUARD_SCOPES, SESSION_COOKIE_NAME, auth_service
from .extensions_manager import BUILTIN_WIDGET_CATALOG, extensions_manager
from .web_search_tool import fetch_webpage_text, search_web
from .browser_stream import browser_stream_manager, format_readable_or_ping_fallback, is_intelligible_text
from .calendar_engine import (
    build_smart_goal_breakdown,
    coordinate_day_schedule,
    export_calendar_to_ics,
    find_free_windows_on_date,
    parse_ics_content,
    recalibrate_scheduled_blocks,
    sync_feed_from_url,
)
from .config import settings
from .cron_scheduler import cron_scheduler
from .database import engine, get_session, init_db
from .diagnostics import evaluate_honest_session_state, probe_scraper_environment, probe_tailscale
from .file_drop import file_drop_service, guess_mime_type
from .focus_guard import focus_guard_service
from .grades_service import grades_service
from .importance_filter import importance_service
from .memory_store import memory_store
from .model_router import _smart_heuristic_duration, model_router, scrub_notification_artifacts
from .models import (
    AIAssistAppRequest,
    AIBriefing,
    AuthLoginRequest,
    BriefingGenerateRequest,
    CalendarCoordinateRequest,
    CalendarEvent,
    CalendarEventCreate,
    CalendarEventRead,
    CalendarEventUpdate,
    CalendarFeed,
    CalendarFeedCreate,
    CalendarFeedRead,
    CalendarImportICSRequest,
    ChatMessage,
    ChatRequest,
    ChatThread,
    ChatThreadCreate,
    ChatThreadUpdate,
    CourseGrade,
    CronRunRequest,
    CronSettingsUpdate,
    GoalBreakdownRequest,
    GoalCheckinRequest,
    GradeChangeLog,
    GradeSnapshotIngestRequest,
    LongTermGoal,
    LongTermGoalCreate,
    LongTermGoalRead,
    LongTermGoalUpdate,
    MarkdownDocUpdate,
    Notification,
    NotificationBulkRequest,
    NotificationCreate,
    NotificationImportanceFeedbackRequest,
    NotificationUpdate,
    OSPresetSaveRequest,
    OSSettingsAIAssistRequest,
    OSSettingsUpdateRequest,
    PingIn,
    QuickNoteAppendRequest,
    ScraperHeartbeat,
    Task,
    TaskAIExtractRequest,
    TaskCreate,
    TaskReorderRequest,
    TaskSyncSourcesRequest,
    TaskUpdate,
    YoutubeJustifyRequest,
    AIProviderUpsertRequest,
    AIRoleRoutingRequest,
    AgentLogUpsertRequest,
    DesktopWidgetsSaveRequest,
    ExtensionInstallRequest,
    ExtensionStorageRequest,
    FocusSiteRuleRequest,
    InstallCatalogAppRequest,
    ModelPingRequest,
    AIFeedbackEntry,
    AIFeedbackCreateRequest,
    DriveFolderRequest,
    DriveFileWriteRequest,
    DriveMoveRequest,
    InboxCategoriesUpdateRequest,
    InboxTagUpsertRequest,
    InboxTagRenameRequest,
    InboxFilterSaveRequest,
    InboxTagMoveRequest,
    InboxBulkTagRequest,
    InboxHarvestRequest,
    DecisionGateSelfTestRequest,
    ToolPlanRequest,
    GradeCourseVisibilityRequest,
    GradeHideRequest,
    GradeTabUpsertRequest,
    ApiBridgeLearnRequest,
    ApiBridgeCallRequest,
    SessionCookieImportRequest,
    NewTabSettingRequest,
    InboxLearningHint,
)
from .ollama_client import ollama_client
from .os_settings import WEBOS_CODE_MAP, SETTINGS_SCHEMA, os_settings_service
from .secretary import get_local_time_reference, secretary_service
from .seed import reconcile_app_unread_counts, reconcile_honest_heartbeats, seed_initial_data
from .task_intelligence import (
    extract_and_save_tasks_from_text,
    sort_tasks_for_display,
    sync_tasks_from_calendar_and_inbox,
)

BOOT_TIME = time.monotonic()
UI_DIR = Path(__file__).resolve().parent / "ui"

logger = logging.getLogger("node.main")

PUBLIC_EXACT_PATHS = {
    "/",
    "/index.html",
    "/styles.css",
    "/favicon.ico",
    "/favicon.svg",
    "/favicon.png",
    "/newtab",
    "/newtab.html",
    "/api/v1/health",
    "/api/v1/auth/session",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
}

# Routes reachable with `X-Node-Guard-Token` instead of a session, and which
# token scope each accepts, live in `auth.GUARD_SCOPES`.


# Hosts whose slug is not a clean first-label match.
_DOMAIN_SLUG_OVERRIDES = {
    "instructure.com": "canvas",
    "schoology.com": "schoology",
    "powerschool.com": "powerschool",
    "classroom.google.com": "google-classroom",
    "mail.google.com": "gmail",
    "outlook.office.com": "outlook",
    "outlook.live.com": "outlook",
}


def _domain_to_slug(host: str) -> str:
    """canvas.instructure.com -> canvas ; mail.google.com -> gmail."""
    h = str(host or "").strip().lstrip(".").lower()
    if not h:
        return ""
    for dom, slug in _DOMAIN_SLUG_OVERRIDES.items():
        if h == dom or h.endswith("." + dom):
            # Prefer the subdomain when it is meaningful (canvas, powerschool…)
            label = h[: -(len(dom) + 1)] if h != dom else ""
            if label and label not in ("www", "app", "portal", "my"):
                return re.sub(r"[^a-z0-9-]+", "-", label.split(".")[-1])[:40]
            return slug
    return re.sub(r"[^a-z0-9-]+", "-", h.split(".")[0])[:40]


def _extract_proxy_context(request: Request) -> Dict[str, Optional[str]]:
    client_ip = request.client.host if request.client else None
    forwarded = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for")
        or request.headers.get("x-real-ip")
    )
    req_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    origin = f"{proto}://{req_host}" if req_host else str(request.base_url).rstrip("/")
    return {
        "client_ip": client_ip,
        "forwarded_for": forwarded,
        "request_host": req_host,
        "public_origin": origin.rstrip("/"),
    }


def _request_authenticated_user(request: Request, db: Session) -> Optional[str]:
    if not settings.auth_enabled:
        return "local"
    raw_token = auth_service.extract_request_token(request, allow_query_token=False)
    sess = auth_service.verify_session_token(raw_token, db)
    return sess.username if sess else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    auth_service.ensure_guard_tokens()
    migrated_from = apps_manager.migrate_legacy_config()
    if migrated_from:
        logger.info("Migrated installed-apps registry from %s to %s", migrated_from, apps_manager.config_path)
    memory_store.ensure_files()
    ai_runtime_manager.reconcile_stale_jobs_on_boot()

    with Session(engine) as session:
        reconcile_honest_heartbeats(session)
        if settings.seed_demo_data:
            seed_initial_data(session)
        # Clean any legacy scraped notification titles with redundant app prefixes or aria junk!
        existing_notifs = list(session.exec(select(Notification)).all())
        dirty_changed = False
        for n in existing_notifs:
            if n.indicator_type == "ping_only":
                continue
            scrub = scrub_notification_artifacts(n.source_app, n.title, n.message)
            if scrub["title"] and scrub["title"] != n.title:
                n.title = scrub["title"]
                if scrub["preview"] and (not n.ai_summary or "SStar" in (n.ai_summary or "") or "Unread email" in (n.ai_summary or "")):
                    n.ai_summary = scrub["preview"]
                session.add(n)
                dirty_changed = True
        if dirty_changed:
            session.commit()
        recalibrate_scheduled_blocks(session)

    cron_scheduler.start_background_loop()
    try:
        yield
    finally:
        await cron_scheduler.stop_background_loop()
        await browser_stream_manager.shutdown()


app = FastAPI(
    title=settings.app_name,
    version="0.11.0",
    description="Node — Personal WebOS",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins if settings.cors_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_gate_and_cache_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("origin") or "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Node-Guard-Token, X-Node-Session"
        if request.headers.get("access-control-request-private-network") == "true":
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp

    path = request.url.path

    # Enforce strict server-side lock screen gate before ANY OS shell, JS, or API executes.
    # Bundled static scenic artwork under /wallpapers/* is safe for Lock Screen ambient streaming
    # (exposes zero personal/app/schedule data; user uploads under /api/v1/files/* remain strictly locked).
    if settings.auth_enabled:
        is_public = path in PUBLIC_EXACT_PATHS or path.startswith(("/assets/", "/wallpapers/"))
        if not is_public:
            authorized = False
            allow_q = path.startswith(("/wallpapers/", "/api/v1/files/raw/", "/api/v1/drive/raw/")) or path in (
                "/api/v1/focus-guard/extension.zip",
                "/api/v1/focus-guard/youtube-guard.user.js",
                "/api/v1/focus-guard/focus-guard.user.js",
                "/api/v1/calendar/export.ics",
            )
            raw_tok = auth_service.extract_request_token(request, allow_query_token=allow_q)
            if raw_tok:
                with Session(engine) as db:
                    if auth_service.verify_session_token(raw_tok, db):
                        authorized = True
            guard_scope = auth_service.guard_scope_for_path(path)
            if not authorized and guard_scope:
                guard_hdr = request.headers.get("x-node-guard-token")
                if auth_service.verify_guard_token(guard_hdr, guard_scope):
                    authorized = True

            if not authorized:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Locked — authentication required"},
                    headers={"Cache-Control": "no-store"},
                )

    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    if path.startswith(("/wallpapers/", "/assets/")) and "tailwind" not in path:
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Lock Screen Authentication API (Server-Side Only — Zero Client Credential Edits)
# ---------------------------------------------------------------------------
@app.get("/api/v1/auth/session")
def get_auth_session(
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    user = _request_authenticated_user(request, session)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "username": user}


@app.post("/api/v1/auth/login")
def login_os_lock_screen(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    client_key = auth_service.extract_client_key(request)
    retry_sec = auth_service.check_rate_limit(client_key)
    if retry_sec is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed unlock attempts. Try again in {retry_sec}s.",
        )

    verified_user = auth_service.verify_credentials(payload.username, payload.password)
    if not verified_user:
        auth_service.record_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    auth_service.clear_failures(client_key)
    raw_token = auth_service.create_session(verified_user, session, request)
    auth_service.attach_session_cookie(response, raw_token, request)
    return {
        "ok": True,
        "authenticated": True,
        "username": verified_user,
        "session_token": raw_token,
    }


@app.post("/api/v1/auth/logout")
def logout_os_lock_screen(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    raw_tok = auth_service.extract_request_token(request, allow_query_token=False)
    auth_service.revoke_session(raw_tok, session)
    auth_service.clear_session_cookie(response, request)
    return {"ok": True, "authenticated": False}


# ---------------------------------------------------------------------------
# Scoped guard tokens (session-only: none of these paths are in GUARD_SCOPES,
# so a guard token can never read or rotate another scope's token).
# Deliberately NOT exposed as an AI tool — secrets must not land in chat logs
# or be sent to cloud model providers.
# ---------------------------------------------------------------------------
@app.get("/api/v1/auth/guard-tokens")
def list_guard_tokens() -> Dict[str, Any]:
    return auth_service.describe_guard_tokens()


@app.post("/api/v1/auth/guard-tokens/end-legacy")
def end_guard_token_legacy() -> Dict[str, Any]:
    auth_service.end_guard_legacy()
    return auth_service.describe_guard_tokens()


@app.post("/api/v1/auth/guard-tokens/{scope}/rotate")
def rotate_guard_token(scope: str) -> Dict[str, Any]:
    if scope not in GUARD_SCOPES:
        raise HTTPException(status_code=404, detail=f"Unknown token scope '{scope}'")
    auth_service.rotate_guard_token(scope)
    return auth_service.describe_guard_tokens()


@app.get("/api/v1/auth/shell")
def serve_authenticated_os_shell() -> HTMLResponse:
    shell_path = UI_DIR / "os_shell.html"
    if not shell_path.is_file():
        raise HTTPException(status_code=404, detail="OS shell missing")
    return HTMLResponse(
        content=shell_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/v1/auth/app.js")
def serve_authenticated_os_js() -> Response:
    js_path = UI_DIR / "os_app.js"
    if not js_path.is_file():
        raise HTTPException(status_code=404, detail="OS controller missing")
    cust_path = UI_DIR / "os_customizer.js"
    cust_code = cust_path.read_text(encoding="utf-8") + "\n\n" if cust_path.is_file() else ""
    return Response(
        content=cust_code + js_path.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/v1/auth/customizer.js")
def serve_authenticated_customizer_js() -> Response:
    cust_path = UI_DIR / "os_customizer.js"
    if not cust_path.is_file():
        raise HTTPException(status_code=404, detail="OS customizer missing")
    return Response(
        content=cust_path.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


def _goal_to_read(g: LongTermGoal) -> LongTermGoalRead:
    try:
        ms = json.loads(g.milestones_json or "[]")
    except Exception:
        ms = []
    return LongTermGoalRead(
        id=g.id,
        title=g.title,
        category=g.category,
        target_date=g.target_date,
        why_it_matters=g.why_it_matters,
        daily_habit=g.daily_habit,
        minutes_per_day=g.minutes_per_day,
        progress_percent=g.progress_percent,
        streak_days=g.streak_days,
        last_checkin_date=g.last_checkin_date,
        milestones=ms,
        active=g.active,
        created_at=g.created_at,
    )


def _briefing_to_dict(row: AIBriefing) -> Dict[str, Any]:
    cards = None
    if row.cards_json:
        try:
            cards = json.loads(row.cards_json)
        except Exception:
            cards = None
    return {
        "id": row.id,
        "markdown": row.markdown,
        "content_markdown": row.markdown,
        "cards": cards,
        "structured": cards,
        "model": row.model,
        "source": row.source,
        "engine": row.source,
        "ollama_reachable": row.source == "ollama",
        "unread_count": row.unread_count,
        "active_tasks_count": row.active_tasks_count,
        "latency_ms": row.latency_ms,
        "created_at": row.created_at.isoformat(),
    }


def _next_default_chat_name(session: Session) -> str:
    existing_names = {t.name.strip() for t in session.exec(select(ChatThread)).all()}
    n = 1
    while f"Chat {n}" in existing_names:
        n += 1
    return f"Chat {n}"


def _sync_and_list_chat_threads(session: Session) -> List[Dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    threads_by_id: Dict[str, ChatThread] = {
        t.thread_id: t for t in session.exec(select(ChatThread)).all()
    }
    msg_rows = list(session.exec(select(ChatMessage).order_by(ChatMessage.id.asc())).all())
    counts: Dict[str, int] = {}
    last_ts: Dict[str, datetime] = {}

    dirty = False
    for m in msg_rows:
        tid = (m.thread_id or "main").strip() or "main"
        counts[tid] = counts.get(tid, 0) + 1
        last_ts[tid] = m.created_at
        if tid not in threads_by_id:
            default_name = _next_default_chat_name(session)
            new_t = ChatThread(
                thread_id=tid,
                name="Chat 1" if tid == "main" and all(x.name != "Chat 1" for x in threads_by_id.values()) else default_name,
                created_at=m.created_at,
                updated_at=m.created_at,
            )
            session.add(new_t)
            session.flush()
            threads_by_id[tid] = new_t
            dirty = True

    if not threads_by_id:
        starter = ChatThread(
            thread_id="main",
            name="Chat 1",
            created_at=now_utc,
            updated_at=now_utc,
        )
        session.add(starter)
        session.flush()
        threads_by_id["main"] = starter
        dirty = True

    if dirty:
        session.commit()

    ordered = sorted(
        threads_by_id.values(),
        key=lambda t: (last_ts.get(t.thread_id) or t.updated_at or t.created_at),
        reverse=True,
    )
    return [
        {
            "thread_id": t.thread_id,
            "name": t.name,
            "title": t.name,
            "count": counts.get(t.thread_id, 0),
            "updated_at": (last_ts.get(t.thread_id) or t.updated_at).isoformat(),
        }
        for t in ordered
    ]


# ---------------------------------------------------------------------------
# Health & Honest Live System / Diagnostics
# ---------------------------------------------------------------------------
NODE_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="15" fill="#0d131c"/>'
    '<rect x="1.5" y="1.5" width="61" height="61" rx="13.5" fill="none" stroke="#2bfbae" stroke-opacity="0.28" stroke-width="2"/>'
    '<path d="M25.5 28L38.5 20.5M25.5 36L38.5 43.5" stroke="#2bfbae" stroke-width="4.2" stroke-linecap="round"/>'
    '<circle cx="20" cy="32" r="7" fill="#0d131c" stroke="#2bfbae" stroke-width="4.2"/>'
    '<circle cx="44" cy="18" r="6" fill="#0d131c" stroke="#2bfbae" stroke-width="4.2"/>'
    '<circle cx="44" cy="46" r="6" fill="#0d131c" stroke="#2bfbae" stroke-width="4.2"/>'
    "</svg>"
)


@app.get("/favicon.svg")
def serve_node_favicon_svg() -> Response:
    svg_file = Path(settings.dashboard_dir) / "favicon.svg"
    body = svg_file.read_text(encoding="utf-8") if svg_file.is_file() else NODE_FAVICON_SVG
    return Response(content=body, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.png")
def serve_node_favicon_png() -> Response:
    png_file = Path(settings.dashboard_dir) / "favicon.png"
    if png_file.is_file():
        return FileResponse(str(png_file), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return Response(content=NODE_FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def serve_node_favicon_ico() -> Response:
    ico_file = Path(settings.dashboard_dir) / "favicon.ico"
    if ico_file.is_file():
        return FileResponse(str(ico_file), media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})
    return Response(
        content=NODE_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/v1/health")
def health_check(
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    # Zero information leak when unauthenticated (used by Docker localhost healthcheck)
    if settings.auth_enabled and not _request_authenticated_user(request, session):
        return {"status": "ok"}
    return {
        "status": "ok",
        "service": "node-backend",
        "app": "Node",
        "version": "7.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/time")
def current_time_reference(days: int = Query(default=21, ge=7, le=35)) -> Dict[str, Any]:
    return get_local_time_reference(days)


@app.get("/api/v1/bootstrap")
async def bootstrap_workspace(
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    user = _request_authenticated_user(request, session) or "local"
    sys_data = await system_status(request=request, session=session)
    notifs = list(
        session.exec(select(Notification).order_by(Notification.timestamp.desc()).limit(100)).all()
    )
    raw_tasks = list(session.exec(select(Task)).all())
    tasks = [_task_with_estimate(t) for t in sort_tasks_for_display(raw_tasks)]
    events = list(session.exec(select(CalendarEvent).order_by(CalendarEvent.start_at.asc())).all())
    feeds = list(session.exec(select(CalendarFeed).order_by(CalendarFeed.id.asc())).all())
    goals = [_goal_to_read(g) for g in session.exec(select(LongTermGoal).order_by(LongTermGoal.id.asc())).all()]
    latest_briefing = session.exec(select(AIBriefing).order_by(AIBriefing.id.desc())).first()
    wallpapers = file_drop_service.list_wallpapers()
    focus = focus_guard_service.get_status(session)
    apps_data = get_monitored_apps(session=session)
    threads = _sync_and_list_chat_threads(session)
    primary_tid = threads[0]["thread_id"] if threads else "main"
    chat_primary = get_chat_history(thread_id=primary_tid, limit=50, session=session)
    models_st = await model_router.refresh_status()

    return {
        "username": user,
        "time": get_local_time_reference(21),
        "system": sys_data,
        "models_status": models_st,
        "notifications": notifs,
        "tasks": tasks,
        "events": events,
        "feeds": feeds,
        "goals": goals,
        "briefing": _briefing_to_dict(latest_briefing) if latest_briefing else None,
        "wallpapers": wallpapers,
        "focus_guard": focus,
        "apps": apps_data["apps"],
        "chat_threads": threads,
        "chat_main": chat_primary,
        "primary_thread_id": primary_tid,
        "stream_tabs": browser_stream_manager.list_live_tabs(),
        "grades": grades_service.get_dashboard(session),
        "importance_stats": importance_service.get_stats(),
    }


@app.get("/api/v1/models/status")
async def get_multi_model_status(force: bool = Query(default=False)) -> Dict[str, Any]:
    return await model_router.refresh_status(force=force)


@app.get("/api/v1/system/status")
async def system_status(
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    pctx = _extract_proxy_context(request)
    ollama_info = await ollama_client.check_status()
    tailscale_info = probe_tailscale(
        client_host=pctx["client_ip"],
        forwarded_for=pctx["forwarded_for"],
        request_host=pctx["request_host"],
    )

    global_cfg, configured_apps = apps_manager.get_global_and_apps()
    poll_sec = int(global_cfg.get("poll_interval_seconds", 30))

    unread_by_app = reconcile_app_unread_counts(session)
    heartbeats = list(session.exec(select(ScraperHeartbeat)).all())
    hb_map = {h.source_app: h for h in heartbeats}
    live_tabs_map = {t["slug"]: t for t in browser_stream_manager.list_live_tabs()}
    scraper_runtime = probe_scraper_environment(
        heartbeats,
        poll_interval_seconds=poll_sec,
        live_stream_tabs=len(live_tabs_map),
    )

    scraper_rows: List[Dict[str, Any]] = []
    for cfg in configured_apps:
        hb = hb_map.get(cfg["name"])
        live_tab = live_tabs_map.get(cfg["slug"])
        st_code, st_label = evaluate_honest_session_state(hb, poll_interval_seconds=poll_sec)
        if live_tab and st_code not in ("warm", "auth_required"):
            st_code = live_tab["status"]
            st_label = "Warm on Server" if st_code == "warm" else "Sign-In Needed"
        scraper_rows.append(
            {
                "source_app": cfg["name"],
                "slug": cfg["slug"],
                "enabled": cfg["enabled"],
                "url": cfg["url"],
                "indicator_type": cfg["indicator_type"],
                "zero_knowledge": cfg["zero_knowledge"],
                "status": st_code,
                "status_label": st_label,
                "stream_active": bool(live_tab),
                "unread_count": unread_by_app.get(cfg["name"], 0),
                "total_pings": hb.total_pings if hb else 0,
                "last_seen": hb.last_seen.isoformat() if (hb and hb.total_pings > 0) else None,
            }
        )

    today_str = datetime.now(timezone.utc).date().isoformat()
    total_notifs = session.exec(select(func.count(Notification.id))).one()
    unread_notifs = session.exec(
        select(func.count(Notification.id)).where(Notification.read_state == False)  # noqa: E712
    ).one()
    active_tasks = session.exec(
        select(func.count(Task.id)).where(Task.completed == False)  # noqa: E712
    ).one()
    completed_tasks = session.exec(
        select(func.count(Task.id)).where(Task.completed == True)  # noqa: E712
    ).one()
    events_today = session.exec(
        select(func.count(CalendarEvent.id))
        .where(CalendarEvent.start_at >= f"{today_str}T00:00")
        .where(CalendarEvent.start_at <= f"{today_str}T23:59")
    ).one()
    active_goals = session.exec(
        select(func.count(LongTermGoal.id)).where(LongTermGoal.active == True)  # noqa: E712
    ).one()
    total_pings = sum(h.total_pings for h in heartbeats)

    return {
        "status": "ok",
        "app": "Node",
        "version": "7.0.0",
        "uptime_seconds": int(time.monotonic() - BOOT_TIME),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "total_pings_received": int(total_pings),
        "total_notifications": int(total_notifs or 0),
        "unread_notifications": int(unread_notifs or 0),
        "active_tasks": int(active_tasks or 0),
        "completed_tasks": int(completed_tasks or 0),
        "events_today": int(events_today or 0),
        "active_goals": int(active_goals or 0),
        "ollama": ollama_info.model_dump(),
        "tailscale": tailscale_info,
        "scraper_runtime": scraper_runtime,
        "scrapers": scraper_rows,
        "cron": cron_scheduler.get_status(session),
    }


@app.post("/api/v1/diagnostics/test-ollama")
async def test_ollama_inference() -> Dict[str, Any]:
    status_info = await ollama_client.check_status()
    if not status_info.reachable:
        return {
            "ok": False,
            "reachable": False,
            "model": settings.ollama_model,
            "base_url": settings.ollama_base_url,
            "message": status_info.detail,
        }
    reply = await ollama_client.generate_raw(
        system_prompt="Reply in one friendly sentence confirming local CPU inference works.",
        user_prompt="Run quick health check.",
        num_predict=60,
    )
    return {
        "ok": bool(reply),
        "reachable": True,
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        "sample_output": reply or "Model reachable, but empty output returned.",
    }


# ---------------------------------------------------------------------------
# Universal Ping Ingestion (`POST /api/v1/ping`) — Intelligible Content First!
# ---------------------------------------------------------------------------
@app.post("/api/v1/ping", status_code=status.HTTP_201_CREATED)
def ingest_ping(
    payload: PingIn,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    # Only enforce count-only mode if caller explicitly requested zero_knowledge / ping_only
    is_zk = bool(payload.zero_knowledge or payload.indicator_type == "ping_only")
    effective_indicator = "ping_only" if is_zk else payload.indicator_type

    hb = session.get(ScraperHeartbeat, payload.source_app)
    hb_status = "simulated" if payload.simulated else ("auth_required" if payload.indicator_type == "auth_wall" else "warm")
    if hb:
        hb.indicator_type = effective_indicator
        hb.status = hb_status
        if payload.app_url:
            hb.app_url = payload.app_url
        hb.zero_knowledge = is_zk
        hb.total_pings += 1
        hb.last_seen = now
    else:
        hb = ScraperHeartbeat(
            source_app=payload.source_app,
            indicator_type=effective_indicator,
            status=hb_status,
            unread_count=0,
            app_url=payload.app_url,
            zero_knowledge=is_zk,
            total_pings=1,
            last_seen=now,
        )
    session.add(hb)

    if not payload.create_notification or payload.indicator_type == "heartbeat":
        session.commit()
        reconcile_app_unread_counts(session)
        return {"ok": True, "heartbeat_only": True, "status": hb.status}

    if is_zk:
        title = payload.title if (payload.title and is_intelligible_text(payload.title)) else f"{payload.source_app} ping"
        message = (
            payload.message
            if (payload.message and is_intelligible_text(payload.message))
            else f"Count updated ({payload.unread_count})."
        )
        priority = payload.priority or "high"
    else:
        title, message = format_readable_or_ping_fallback(
            app_name=payload.source_app,
            raw_title=payload.title,
            raw_message=payload.message,
            unread_count=payload.unread_count,
            strict_zero_knowledge=False,
        )
        priority = payload.priority or "normal"

    is_imp, imp_score, imp_reason = importance_service.classify(
        payload.source_app, title, message, effective_indicator
    )
    one_line = (
        None
        if is_zk
        else importance_service.build_one_line_summary(payload.source_app, title, message, effective_indicator)
    )
    notif = Notification(
        source_app=payload.source_app,
        title=title,
        message=message,
        ai_summary=one_line,
        status="active",
        timestamp=now,
        priority=priority,
        read_state=False,
        is_important=is_imp,
        importance_score=imp_score,
        importance_reason=imp_reason,
        indicator_type=effective_indicator,
        raw_payload=json.dumps(payload.raw_payload) if payload.raw_payload else None,
    )
    session.add(notif)
    session.commit()
    session.refresh(notif)
    reconcile_app_unread_counts(session)
    return {"ok": True, "notification_id": notif.id, "status": hb.status, "zero_knowledge": is_zk, "title": notif.title}


# ---------------------------------------------------------------------------
# Calendar App: Events CRUD, Recurring Blocks, ICS Import/Export, Coordination
# ---------------------------------------------------------------------------
@app.get("/api/v1/calendar/events", response_model=List[CalendarEventRead])
def list_calendar_events(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[CalendarEvent]:
    stmt = select(CalendarEvent)
    if start_date:
        stmt = stmt.where(CalendarEvent.end_at >= f"{start_date}T00:00")
    if end_date:
        stmt = stmt.where(CalendarEvent.start_at <= f"{end_date}T23:59")
    if category:
        stmt = stmt.where(CalendarEvent.category == category)
    stmt = stmt.order_by(CalendarEvent.start_at.asc())
    return list(session.exec(stmt).all())


@app.post("/api/v1/calendar/events", response_model=List[CalendarEventRead], status_code=status.HTTP_201_CREATED)
def create_calendar_event(
    payload: CalendarEventCreate,
    session: Session = Depends(get_session),
) -> List[CalendarEvent]:
    created_events: List[CalendarEvent] = []
    repeat_days = sorted({d for d in (payload.repeat_days or []) if 0 <= d <= 6})
    weeks = max(1, min(26, payload.repeat_weeks or 1))

    if repeat_days and len(payload.start_at) >= 10:
        series_id = f"series-{uuid.uuid4().hex[:10]}"
        base_date = date.fromisoformat(payload.start_at[:10])
        start_time_part = payload.start_at[10:] if len(payload.start_at) > 10 else "T09:00"
        end_time_part = payload.end_at[10:] if len(payload.end_at) > 10 else "T10:00"

        week_monday = base_date - timedelta(days=base_date.weekday())
        for wk in range(weeks):
            for wd in repeat_days:
                occ_date = week_monday + timedelta(days=wk * 7 + wd)
                if occ_date < base_date:
                    continue
                d_iso = occ_date.isoformat()
                ev = CalendarEvent(
                    title=payload.title.strip(),
                    start_at=f"{d_iso}{start_time_part}",
                    end_at=f"{d_iso}{end_time_part}",
                    all_day=payload.all_day,
                    category=payload.category,
                    color=payload.color,
                    location=payload.location,
                    notes=payload.notes,
                    source=payload.source,
                    recurrence_rule=f"WEEKLY:{','.join(str(x) for x in repeat_days)}x{weeks}",
                    series_id=series_id,
                    linked_task_id=payload.linked_task_id,
                    linked_goal_id=payload.linked_goal_id,
                )
                session.add(ev)
                created_events.append(ev)
        session.commit()
        for ev in created_events:
            session.refresh(ev)
        return created_events

    single = CalendarEvent(
        title=payload.title.strip(),
        start_at=payload.start_at,
        end_at=payload.end_at,
        all_day=payload.all_day,
        category=payload.category,
        color=payload.color,
        location=payload.location,
        notes=payload.notes,
        source=payload.source,
        linked_task_id=payload.linked_task_id,
        linked_goal_id=payload.linked_goal_id,
    )
    session.add(single)
    session.commit()
    session.refresh(single)
    return [single]


@app.patch("/api/v1/calendar/events/{event_id}", response_model=CalendarEventRead)
def update_calendar_event(
    event_id: int,
    payload: CalendarEventUpdate,
    session: Session = Depends(get_session),
) -> CalendarEvent:
    ev = session.get(CalendarEvent, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(ev, k, v)
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev


@app.delete("/api/v1/calendar/events/{event_id}")
def delete_calendar_event(
    event_id: int,
    delete_series: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    ev = session.get(CalendarEvent, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    if delete_series and ev.series_id:
        siblings = session.exec(select(CalendarEvent).where(CalendarEvent.series_id == ev.series_id)).all()
        count = len(siblings)
        for s in siblings:
            session.delete(s)
        session.commit()
        return {"deleted": count, "series_id": ev.series_id}
    session.delete(ev)
    session.commit()
    return {"deleted": 1, "id": event_id}


@app.post("/api/v1/calendar/import-ics")
def import_ics_calendar(
    payload: CalendarImportICSRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    parsed = parse_ics_content(
        ics_text=payload.ics_content,
        default_category=payload.category,
        default_color=payload.color,
        source_name=payload.source_name,
    )
    if not parsed:
        raise HTTPException(status_code=400, detail="No valid VEVENT entries found in .ics content.")

    added = 0
    updated = 0
    for item in parsed:
        ext_uid = item.get("external_uid")
        existing = None
        if ext_uid:
            existing = session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.external_uid == ext_uid)
                .where(CalendarEvent.feed_id == None)  # noqa: E711
            ).first()
        if existing:
            existing.title = item["title"]
            existing.start_at = item["start_at"]
            existing.end_at = item["end_at"]
            existing.all_day = item["all_day"]
            existing.location = item.get("location")
            existing.notes = item.get("notes")
            session.add(existing)
            updated += 1
        else:
            session.add(CalendarEvent(**item))
            added += 1
    session.commit()
    return {
        "ok": True,
        "parsed": len(parsed),
        "added": added,
        "updated": updated,
        "source_name": payload.source_name,
    }


@app.get("/api/v1/calendar/feeds", response_model=List[CalendarFeedRead])
def list_calendar_feeds(session: Session = Depends(get_session)) -> List[CalendarFeed]:
    return list(session.exec(select(CalendarFeed).order_by(CalendarFeed.created_at.desc())).all())


@app.post("/api/v1/calendar/feeds", status_code=status.HTTP_201_CREATED)
async def add_calendar_feed(
    payload: CalendarFeedCreate,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    feed = CalendarFeed(
        name=payload.name.strip(),
        url=payload.url.strip(),
        color=payload.color,
        category=payload.category,
        enabled=True,
    )
    session.add(feed)
    session.commit()
    session.refresh(feed)

    sync_result = await sync_feed_from_url(feed, session)
    session.refresh(feed)
    return {
        "feed": CalendarFeedRead.model_validate(feed).model_dump(),
        "sync": sync_result,
    }


@app.post("/api/v1/calendar/feeds/{feed_id}/sync")
async def sync_calendar_feed_endpoint(
    feed_id: int,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    feed = session.get(CalendarFeed, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found")
    sync_result = await sync_feed_from_url(feed, session)
    session.refresh(feed)
    return {
        "feed": CalendarFeedRead.model_validate(feed).model_dump(),
        "sync": sync_result,
    }


@app.delete("/api/v1/calendar/feeds/{feed_id}")
def delete_calendar_feed(
    feed_id: int,
    remove_events: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    feed = session.get(CalendarFeed, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found")
    removed_events = 0
    if remove_events:
        evs = session.exec(select(CalendarEvent).where(CalendarEvent.feed_id == feed_id)).all()
        removed_events = len(evs)
        for e in evs:
            session.delete(e)
    session.delete(feed)
    session.commit()
    return {"deleted_feed": feed_id, "removed_events": removed_events}


@app.get("/api/v1/calendar/export.ics")
def export_hub_ics_feed(session: Session = Depends(get_session)) -> Response:
    events = list(session.exec(select(CalendarEvent).order_by(CalendarEvent.start_at.asc())).all())
    tasks = list(session.exec(select(Task).where(Task.completed == False)).all())  # noqa: E712
    ics_body = export_calendar_to_ics(events=events, tasks=tasks)
    return Response(
        content=ics_body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="node-schedule.ics"'},
    )


@app.get("/api/v1/calendar/free-slots")
def get_free_slots_endpoint(
    target_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    dt = (target_date or datetime.now(timezone.utc).date().isoformat())[:10]
    day_evs = list(
        session.exec(
            select(CalendarEvent)
            .where(CalendarEvent.start_at >= f"{dt}T00:00")
            .where(CalendarEvent.start_at <= f"{dt}T23:59")
        ).all()
    )
    wins = find_free_windows_on_date(day_evs, dt)
    return {
        "date": dt,
        "windows": [
            {
                "start_at": s.strftime("%Y-%m-%dT%H:%M"),
                "end_at": e.strftime("%Y-%m-%dT%H:%M"),
                "minutes": int((e - s).total_seconds() // 60),
            }
            for s, e in wins
        ],
    }


@app.post("/api/v1/calendar/coordinate")
def coordinate_calendar_day(
    payload: CalendarCoordinateRequest = CalendarCoordinateRequest(),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    result = coordinate_day_schedule(
        session=session,
        target_date=payload.target_date,
        day_start_hour=payload.day_start_hour,
        day_end_hour=payload.day_end_hour,
        include_tasks=payload.include_tasks,
        include_goals=payload.include_goals,
    )
    return {
        "target_date": result["target_date"],
        "created_count": len(result["created_blocks"]),
        "created_blocks": [CalendarEventRead.model_validate(b).model_dump() for b in result["created_blocks"]],
        "free_windows_remaining": result["free_windows_remaining"],
        "summary": result["summary"],
    }


# ---------------------------------------------------------------------------
# Long-Term Goals (`/api/v1/goals`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/goals", response_model=List[LongTermGoalRead])
def list_goals(
    active_only: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> List[LongTermGoalRead]:
    stmt = select(LongTermGoal)
    if active_only:
        stmt = stmt.where(LongTermGoal.active == True)  # noqa: E712
    stmt = stmt.order_by(LongTermGoal.active.desc(), LongTermGoal.created_at.asc())
    return [_goal_to_read(g) for g in session.exec(stmt).all()]


@app.post("/api/v1/goals/breakdown")
async def generate_goal_breakdown_preview(payload: GoalBreakdownRequest) -> Dict[str, Any]:
    plan = build_smart_goal_breakdown(
        title=payload.title,
        category=payload.category,
        target_date=payload.target_date,
        minutes_per_day=payload.minutes_per_day,
    )
    llm_tip = await ollama_client.generate_raw(
        system_prompt="Give 1 encouraging, practical 2-sentence tip for working toward this goal daily.",
        user_prompt=f"Goal: {payload.title} ({payload.minutes_per_day} min/day)",
        num_predict=90,
    )
    if llm_tip:
        plan["coaching_tip"] = llm_tip
        plan["engine"] = "ollama"
    else:
        plan["engine"] = "secretary_engine"
    return plan


@app.post("/api/v1/goals", response_model=LongTermGoalRead, status_code=status.HTTP_201_CREATED)
def create_goal(
    payload: LongTermGoalCreate,
    session: Session = Depends(get_session),
) -> LongTermGoalRead:
    plan = build_smart_goal_breakdown(
        title=payload.title,
        category=payload.category,
        target_date=payload.target_date,
        minutes_per_day=payload.minutes_per_day,
    )
    milestones = payload.milestones if payload.milestones else plan["milestones"]
    daily_habit = (payload.daily_habit or "").strip() or plan["daily_habit"]

    done_cnt = sum(1 for m in milestones if m.get("done"))
    pct = int(round((done_cnt / len(milestones)) * 100)) if milestones else 0

    g = LongTermGoal(
        title=payload.title.strip(),
        category=payload.category,
        target_date=payload.target_date,
        why_it_matters=payload.why_it_matters,
        daily_habit=daily_habit,
        minutes_per_day=payload.minutes_per_day,
        progress_percent=pct,
        streak_days=0,
        milestones_json=json.dumps(milestones),
        active=True,
    )
    session.add(g)
    session.commit()
    session.refresh(g)

    memory_store.append_memory_rule(
        f"Long-Term Goal: {g.title} ({g.minutes_per_day}m/day — {g.daily_habit})"
    )
    return _goal_to_read(g)


@app.patch("/api/v1/goals/{goal_id}", response_model=LongTermGoalRead)
def update_goal(
    goal_id: int,
    payload: LongTermGoalUpdate,
    session: Session = Depends(get_session),
) -> LongTermGoalRead:
    g = session.get(LongTermGoal, goal_id)
    if not g:
        raise HTTPException(status_code=404, detail="Goal not found")

    data = payload.model_dump(exclude_unset=True)
    if "milestones" in data and data["milestones"] is not None:
        ms = data.pop("milestones")
        g.milestones_json = json.dumps(ms)
        if "progress_percent" not in data and len(ms) > 0:
            done_cnt = sum(1 for m in ms if m.get("done"))
            g.progress_percent = int(round((done_cnt / len(ms)) * 100))

    for k, v in data.items():
        setattr(g, k, v)

    session.add(g)
    session.commit()
    session.refresh(g)
    return _goal_to_read(g)


@app.post("/api/v1/goals/{goal_id}/checkin", response_model=LongTermGoalRead)
def checkin_goal_today(
    goal_id: int,
    payload: GoalCheckinRequest = GoalCheckinRequest(),
    session: Session = Depends(get_session),
) -> LongTermGoalRead:
    g = session.get(LongTermGoal, goal_id)
    if not g:
        raise HTTPException(status_code=404, detail="Goal not found")

    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()

    if g.last_checkin_date != today_iso:
        if g.last_checkin_date == yesterday_iso:
            g.streak_days += 1
        else:
            g.streak_days = max(1, g.streak_days + 1 if g.last_checkin_date else 1)
        g.last_checkin_date = today_iso
        g.progress_percent = min(100, g.progress_percent + 5)

    if payload.note:
        memory_store.append_note(
            f"[{g.title}] Logged {payload.minutes_logged or g.minutes_per_day}m — {payload.note}",
            category="Goal Progress",
        )

    session.add(g)
    session.commit()
    session.refresh(g)
    return _goal_to_read(g)


@app.delete("/api/v1/goals/{goal_id}")
def delete_goal(goal_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    g = session.get(LongTermGoal, goal_id)
    if not g:
        raise HTTPException(status_code=404, detail="Goal not found")
    session.delete(g)
    session.commit()
    return {"deleted": goal_id}


# ---------------------------------------------------------------------------
# Notifications API (Single + Gmail/ProtonMail Bulk Management)
# ---------------------------------------------------------------------------
@app.get("/api/v1/notifications")
def list_notifications(
    source_app: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    unread_only: bool = Query(default=False),
    important_only: bool = Query(default=False),
    sort: str = Query(default="priority_timestamp"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> List[Notification]:
    stmt = select(Notification)
    if source_app:
        stmt = stmt.where(Notification.source_app == source_app)
    if priority:
        stmt = stmt.where(Notification.priority == priority)
    if unread_only:
        stmt = stmt.where(Notification.read_state == False)  # noqa: E712
    if important_only:
        stmt = stmt.where(Notification.is_important == True)  # noqa: E712
    items = list(session.exec(stmt.order_by(Notification.timestamp.desc()).limit(limit)).all())
    if sort == "priority_timestamp":
        p_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        items.sort(key=lambda n: (1 if n.read_state else 0, p_rank.get(n.priority, 2), -n.timestamp.timestamp()))
    return items


@app.get("/api/v1/notifications/importance-stats")
def get_notification_importance_stats() -> Dict[str, Any]:
    return importance_service.get_stats()


@app.post("/api/v1/notifications", status_code=status.HTTP_201_CREATED)
async def create_notification(payload: NotificationCreate, session: Session = Depends(get_session)) -> Notification:
    clean_title = payload.title
    clean_msg = payload.message
    if payload.indicator_type != "ping_only":
        scrub = scrub_notification_artifacts(payload.source_app, payload.title, payload.message)
        if scrub["title"]:
            clean_title = scrub["title"]
        if scrub["preview"] and not clean_msg:
            clean_msg = scrub["preview"]

    is_imp, imp_score, imp_reason = importance_service.classify(
        payload.source_app, clean_title, clean_msg, payload.indicator_type
    )
    one_line = (
        None
        if payload.indicator_type == "ping_only"
        else importance_service.build_one_line_summary(
            payload.source_app, clean_title, clean_msg, payload.indicator_type
        )
    )
    notif = Notification(
        source_app=payload.source_app,
        title=clean_title,
        message=clean_msg,
        ai_summary=one_line,
        priority=payload.priority,
        status=payload.status,
        indicator_type=payload.indicator_type,
        raw_payload=payload.raw_payload,
        read_state=False,
        is_important=is_imp,
        importance_score=imp_score,
        importance_reason=imp_reason,
        category="General",
        ai_processed=False,
        timestamp=datetime.now(timezone.utc),
    )
    session.add(notif)
    session.commit()
    reconcile_app_unread_counts(session)
    session.refresh(notif)
    if notif.id is not None and payload.indicator_type != "ping_only":
        browser_stream_manager._schedule_ai_notification_processing([notif.id])
    return notif


@app.post("/api/v1/notifications/ai-process")
async def trigger_ai_notification_processing(limit: int = Query(default=25, ge=1, le=100)) -> Dict[str, Any]:
    processed = await browser_stream_manager.process_notifications_with_ai(limit=limit)
    return {"ok": True, "processed": processed}


@app.post("/api/v1/notifications/bulk")
def bulk_manage_notifications(
    payload: NotificationBulkRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    action = payload.action.strip().lower()
    if action not in ("mark_read", "mark_unread", "delete", "add_to_tasks", "mark_important", "mark_not_important"):
        raise HTTPException(status_code=400, detail=f"Unsupported bulk action '{action}'")

    unique_ids = list(dict.fromkeys(payload.ids))
    items = list(session.exec(select(Notification).where(Notification.id.in_(unique_ids))).all())
    today_iso = get_local_time_reference(7)["today_date"]
    tasks_created = 0

    for item in items:
        if action == "mark_read":
            item.read_state = True
            item.status = "archived"
            session.add(item)
        elif action == "mark_unread":
            item.read_state = False
            item.status = "active"
            session.add(item)
        elif action == "delete":
            session.delete(item)
        elif action in ("mark_important", "mark_not_important"):
            mark_imp = action == "mark_important"
            importance_service.record_feedback(session, item, important=mark_imp)
        elif action == "add_to_tasks":
            t_title = item.title if item.source_app.lower() in item.title.lower() else f"{item.source_app}: {item.title}"
            session.add(
                Task(
                    source_app=item.source_app,
                    title=t_title[:380],
                    due_date=today_iso,
                    category="General",
                    completed=False,
                )
            )
            item.read_state = True
            item.status = "archived"
            session.add(item)
            tasks_created += 1

    session.commit()
    reconcile_app_unread_counts(session)
    return {
        "ok": True,
        "action": action,
        "affected": len(items),
        "tasks_created": tasks_created,
    }


@app.post("/api/v1/notifications/{notification_id}/importance")
def set_notification_importance(
    notification_id: int,
    payload: NotificationImportanceFeedbackRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    notif = session.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    res = importance_service.record_feedback(session, notif, important=payload.resolved())
    session.refresh(notif)
    return {
        "ok": True,
        "notification": notif,
        "learner": res,
    }


@app.post("/api/v1/notifications/{notification_id}/enrich")
async def enrich_notification_summary(notification_id: int) -> Dict[str, Any]:
    try:
        return await browser_stream_manager.enrich_notification_from_live_tab(notification_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/notifications/{notification_id}/read")
def toggle_notification_read(notification_id: int, session: Session = Depends(get_session)) -> Notification:
    notif = session.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.read_state = not notif.read_state
    notif.status = "archived" if notif.read_state else "active"
    session.add(notif)
    session.commit()
    reconcile_app_unread_counts(session)
    session.refresh(notif)
    return notif


@app.patch("/api/v1/notifications/{notification_id}")
def update_notification(
    notification_id: int,
    payload: NotificationUpdate,
    session: Session = Depends(get_session),
) -> Notification:
    notif = session.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(notif, k, v)
    session.add(notif)
    session.commit()
    reconcile_app_unread_counts(session)
    session.refresh(notif)
    return notif


@app.post("/api/v1/notifications/read-all")
@app.post("/api/v1/notifications/mark-all-read")
def mark_all_notifications_read(
    source_app: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    stmt = select(Notification).where(Notification.read_state == False)  # noqa: E712
    if source_app:
        stmt = stmt.where(Notification.source_app == source_app)
    items = session.exec(stmt).all()
    for item in items:
        item.read_state = True
        item.status = "archived"
        session.add(item)
    session.commit()
    reconcile_app_unread_counts(session)
    return {"updated": len(items)}


@app.delete("/api/v1/notifications/{notification_id}")
def delete_notification(notification_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    notif = session.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    session.delete(notif)
    session.commit()
    reconcile_app_unread_counts(session)
    return {"deleted": notification_id}


# ---------------------------------------------------------------------------
# Tasks API
# ---------------------------------------------------------------------------
def _task_with_estimate(t: Task) -> Dict[str, Any]:
    est_mins, est_reason = memory_store.estimate_task_minutes(t.title, t.category, t.source_app)
    if est_reason and "default" in est_reason.lower():
        est_mins = _smart_heuristic_duration(t.title, t.category, t.source_app)
    base = t.model_dump()
    if isinstance(base.get("created_at"), datetime):
        base["created_at"] = base["created_at"].isoformat()
    base["estimated_minutes"] = t.estimated_minutes if t.estimated_minutes else est_mins
    base["estimate_source"] = "explicit" if t.estimated_minutes else est_reason
    return base


@app.get("/api/v1/tasks")
def list_tasks(
    include_completed: bool = Query(default=True),
    category: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[Dict[str, Any]]:
    stmt = select(Task)
    if not include_completed:
        stmt = stmt.where(Task.completed == False)  # noqa: E712
    if category:
        stmt = stmt.where(Task.category == category)
    rows = list(session.exec(stmt).all())
    return [_task_with_estimate(t) for t in sort_tasks_for_display(rows)]


@app.post("/api/v1/tasks", status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, session: Session = Depends(get_session)) -> Dict[str, Any]:
    clean_title = payload.title.strip()
    existing_orders = [int(x) for x in session.exec(select(Task.sort_order)).all() if x is not None]
    next_order = payload.sort_order if payload.sort_order is not None else ((max(existing_orders) + 10) if existing_orders else 10)
    task = Task(
        source_app=payload.source_app,
        title=clean_title,
        due_date=payload.due_date,
        category=payload.category,
        priority=payload.priority or "normal",
        sort_order=next_order,
        estimated_minutes=payload.estimated_minutes,
        completed=False,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_with_estimate(task)


@app.post("/api/v1/tasks/reorder")
def reorder_tasks(payload: TaskReorderRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    for idx, tid in enumerate(payload.ordered_ids):
        t = session.get(Task, int(tid))
        if t:
            t.sort_order = (idx + 1) * 10
            session.add(t)
    session.commit()
    rows = list(session.exec(select(Task)).all())
    return {"ok": True, "tasks": [_task_with_estimate(t) for t in sort_tasks_for_display(rows)]}


@app.post("/api/v1/tasks/ai-extract")
async def ai_extract_tasks_endpoint(
    payload: TaskAIExtractRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    res = await extract_and_save_tasks_from_text(
        session=session,
        raw_text=payload.text,
        source_name=payload.source_name,
        today_iso=get_local_time_reference(7)["today_date"],
    )
    return {
        "ok": True,
        "created_count": res["created_count"],
        "skipped_duplicates": res["skipped_duplicates"],
        "tasks": [_task_with_estimate(t) for t in res["tasks"]],
    }


@app.post("/api/v1/tasks/sync-sources")
async def sync_tasks_from_sources_endpoint(
    payload: TaskSyncSourcesRequest = TaskSyncSourcesRequest(),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    res = await sync_tasks_from_calendar_and_inbox(
        session=session,
        include_calendar=payload.include_calendar,
        include_inbox=payload.include_inbox,
        days_ahead=payload.days_ahead,
        today_iso=get_local_time_reference(7)["today_date"],
    )
    return {
        "ok": True,
        "created_count": res["created_count"],
        "from_calendar": res["from_calendar"],
        "from_inbox": res["from_inbox"],
        "skipped_duplicates": res["skipped_duplicates"],
        "tasks": [_task_with_estimate(t) for t in res["tasks"]],
    }


@app.post("/api/v1/tasks/{task_id}/toggle")
def toggle_task_endpoint(task_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.completed = not task.completed
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_with_estimate(task)


@app.patch("/api/v1/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, session: Session = Depends(get_session)) -> Dict[str, Any]:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(task, k, v)
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_with_estimate(task)


@app.delete("/api/v1/tasks/{task_id}")
def delete_task(task_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    session.delete(task)
    session.commit()
    return {"deleted": task_id}


# ---------------------------------------------------------------------------
# AI Daily Briefing & Named Multi-Context Secretary (`ChatThread`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/ai/latest")
@app.get("/api/v1/briefings/latest")
def get_latest_briefing(session: Session = Depends(get_session)) -> Optional[Dict[str, Any]]:
    row = session.exec(select(AIBriefing).order_by(AIBriefing.created_at.desc()).limit(1)).first()
    if not row:
        return None
    return _briefing_to_dict(row)


@app.post("/api/v1/ai/summarize")
@app.post("/api/v1/briefings/generate")
async def generate_briefing(
    payload: BriefingGenerateRequest = BriefingGenerateRequest(),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    today_str = get_local_time_reference(7)["today_date"]
    unread_notifs = list(
        session.exec(
            select(Notification)
            .where(Notification.read_state == False)  # noqa: E712
            .order_by(Notification.timestamp.desc())
        ).all()
    )
    pending_tasks = list(
        session.exec(select(Task).where(Task.completed == False)).all()  # noqa: E712
    )
    events_today = list(
        session.exec(
            select(CalendarEvent)
            .where(CalendarEvent.start_at >= f"{today_str}T00:00")
            .where(CalendarEvent.start_at <= f"{today_str}T23:59")
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
    recent_grades = list(
        session.exec(select(GradeChangeLog).order_by(GradeChangeLog.detected_at.desc()).limit(4)).all()
    )
    cron_cfg = cron_scheduler.load_settings()

    content_md, cards, engine_used, _ollama_ok, elapsed_ms, _err = await ollama_client.generate_daily_briefing(
        notifications=unread_notifs,
        tasks=pending_tasks,
        events_today=events_today,
        active_goals=active_goals,
        focus_note=payload.focus_note,
        cutoff_hhmm=str(cron_cfg.get("bedtime_hhmm") or "22:00"),
        wind_down_minutes=int(cron_cfg.get("sleep_buffer_minutes") or 15),
        recent_grade_changes=recent_grades,
    )

    record = AIBriefing(
        markdown=content_md,
        model=settings.ollama_model,
        source=engine_used,
        unread_count=len(unread_notifs),
        active_tasks_count=len(pending_tasks),
        latency_ms=elapsed_ms,
        cards_json=json.dumps(cards),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return _briefing_to_dict(record)


@app.get("/api/v1/ai/threads")
def list_chat_threads(session: Session = Depends(get_session)) -> Dict[str, Any]:
    return {"threads": _sync_and_list_chat_threads(session)}


@app.post("/api/v1/ai/threads", status_code=status.HTTP_201_CREATED)
def create_chat_thread(
    payload: ChatThreadCreate = ChatThreadCreate(),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    tid = (payload.thread_id or f"ctx-{uuid.uuid4().hex[:8]}").strip()[:80]
    existing = session.get(ChatThread, tid)
    if existing:
        if payload.name and payload.name.strip():
            existing.name = payload.name.strip()
            existing.updated_at = now_utc
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return {
            "thread_id": existing.thread_id,
            "name": existing.name,
            "title": existing.name,
            "count": 0,
            "updated_at": existing.updated_at.isoformat(),
        }

    chosen_name = (payload.name or "").strip() or _next_default_chat_name(session)
    row = ChatThread(
        thread_id=tid,
        name=chosen_name,
        created_at=now_utc,
        updated_at=now_utc,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "thread_id": row.thread_id,
        "name": row.name,
        "title": row.name,
        "count": 0,
        "updated_at": row.updated_at.isoformat(),
    }


@app.patch("/api/v1/ai/threads/{thread_id}")
def rename_chat_thread(
    thread_id: str,
    payload: ChatThreadUpdate,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    clean_tid = thread_id.strip()[:80]
    clean_name = payload.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Context name cannot be empty")
    now_utc = datetime.now(timezone.utc)
    row = session.get(ChatThread, clean_tid)
    if not row:
        row = ChatThread(thread_id=clean_tid, name=clean_name, created_at=now_utc, updated_at=now_utc)
    else:
        row.name = clean_name
        row.updated_at = now_utc
    session.add(row)
    session.commit()
    session.refresh(row)
    cnt = session.exec(select(func.count(ChatMessage.id)).where(ChatMessage.thread_id == clean_tid)).one()
    return {
        "thread_id": row.thread_id,
        "name": row.name,
        "title": row.name,
        "count": int(cnt or 0),
        "updated_at": row.updated_at.isoformat(),
    }


@app.delete("/api/v1/ai/threads/{thread_id}")
def delete_chat_thread(
    thread_id: str,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    clean_tid = thread_id.strip()[:80]
    msgs = list(session.exec(select(ChatMessage).where(ChatMessage.thread_id == clean_tid)).all())
    for m in msgs:
        session.delete(m)
    th = session.get(ChatThread, clean_tid)
    if th:
        session.delete(th)
    session.commit()
    threads = _sync_and_list_chat_threads(session)
    return {"deleted": clean_tid, "messages_removed": len(msgs), "threads": threads}


@app.get("/api/v1/ai/chat")
@app.get("/api/v1/secretary/history")
def get_chat_history(
    thread_id: str = Query(default="main"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> List[Dict[str, Any]]:
    clean_tid = (thread_id or "main").strip()[:80] or "main"
    rows = list(
        session.exec(
            select(ChatMessage)
            .where(ChatMessage.thread_id == clean_tid)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        ).all()
    )
    rows.reverse()
    out: List[Dict[str, Any]] = []
    for r in rows:
        thinking = None
        try:
            parsed = json.loads(r.actions_json or "[]")
            if isinstance(parsed, dict):
                acts = parsed.get("actions", [])
                thinking = parsed.get("thinking")
            else:
                acts = parsed
        except Exception:
            acts = []
        out.append(
            {
                "id": r.id,
                "thread_id": r.thread_id or "main",
                "role": r.role,
                "content": r.content,
                "actions": acts,
                "thinking": thinking,
                "source": r.source,
                "created_at": r.created_at.isoformat(),
            }
        )
    return out


@app.post("/api/v1/ai/chat")
@app.post("/api/v1/secretary/chat")
async def chat_with_secretary(
    payload: ChatRequest,
    request: Request,
) -> Dict[str, Any]:
    pctx = _extract_proxy_context(request)
    submitted = ai_runtime_manager.submit_chat_job(
        prompt=payload.message,
        thread_id=payload.thread_id or "main",
        current_view=payload.current_view,
        enable_thinking=payload.enable_thinking,
        requested_model=payload.model,
        client_host=pctx["client_ip"],
        forwarded_for=pctx["forwarded_for"],
        request_host=pctx["request_host"],
    )
    return await ai_runtime_manager.wait_for_job_result(submitted["job_uid"])


@app.post("/api/v1/ai/chat/stream")
async def stream_chat_with_secretary(
    payload: ChatRequest,
    request: Request,
) -> StreamingResponse:
    """
    Submits a detached server-side AIJob and streams its events.
    If the client disconnects or closes the browser mid-generation, the server-side
    job continues running to 100% completion and persists the response in SQLite.
    """
    pctx = _extract_proxy_context(request)
    submitted = ai_runtime_manager.submit_chat_job(
        prompt=payload.message,
        thread_id=payload.thread_id or "main",
        current_view=payload.current_view,
        enable_thinking=payload.enable_thinking,
        requested_model=payload.model,
        client_host=pctx["client_ip"],
        forwarded_for=pctx["forwarded_for"],
        request_host=pctx["request_host"],
    )
    job_uid = submitted["job_uid"]

    async def _ndjson_gen():
        try:
            async for ev in ai_runtime_manager.stream_job_events(job_uid):
                yield json.dumps(ev) + "\n"
        except asyncio.CancelledError:
            # Client disconnected — server-side AIJob keeps running cleanly!
            return
        except Exception as exc:
            yield json.dumps({"type": "error", "job_uid": job_uid, "detail": str(exc)}) + "\n"

    return StreamingResponse(
        _ndjson_gen(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "X-Node-Job-Uid": job_uid,
        },
    )


@app.delete("/api/v1/ai/chat")
@app.delete("/api/v1/secretary/history")
def clear_chat_history(
    thread_id: Optional[str] = Query(default=None),
    clear_all: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    if clear_all:
        rows = list(session.exec(select(ChatMessage)).all())
    else:
        target_tid = (thread_id or "main").strip()[:80] or "main"
        rows = list(session.exec(select(ChatMessage).where(ChatMessage.thread_id == target_tid)).all())
    for r in rows:
        session.delete(r)
    session.commit()
    return {"cleared": len(rows), "thread_id": "all" if clear_all else (thread_id or "main")}


# ---------------------------------------------------------------------------
# AI Memory (`ai_memory.md`) & Multi-File Notebook (`/data/notebook/*.md`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/ai/memory")
def read_ai_memory() -> Dict[str, Any]:
    return memory_store.get_memory()


@app.put("/api/v1/ai/memory")
def write_ai_memory(payload: MarkdownDocUpdate) -> Dict[str, Any]:
    return memory_store.save_memory(payload.content)


@app.get("/api/v1/ai/notebook")
def list_ai_notebook_files() -> Dict[str, Any]:
    files = memory_store.list_notebook_files()
    archetypes, overrides = memory_store.parse_time_estimates()
    return {
        "notebook_dir": str(memory_store.notebook_dir),
        "files": files,
        "time_estimates": {
            "archetypes": archetypes,
            "keyword_overrides": overrides,
        },
    }


@app.get("/api/v1/ai/notebook/{slug}")
def read_ai_notebook_file(slug: str) -> Dict[str, Any]:
    return memory_store.get_notebook_file(slug)


@app.put("/api/v1/ai/notebook/{slug}")
def write_ai_notebook_file(slug: str, payload: MarkdownDocUpdate) -> Dict[str, Any]:
    return memory_store.save_notebook_file(slug, payload.content)


@app.post("/api/v1/ai/notebook/{slug}/append")
def append_ai_notebook_file(slug: str, payload: QuickNoteAppendRequest) -> Dict[str, Any]:
    return memory_store.append_to_notebook_file(slug, payload.text, category=payload.category)


@app.delete("/api/v1/ai/notebook/{slug}")
def remove_ai_notebook_file(slug: str) -> Dict[str, Any]:
    if not memory_store.delete_notebook_file(slug):
        raise HTTPException(
            status_code=400,
            detail="Core file 'time_estimates.md' is protected or file does not exist.",
        )
    return {"deleted": slug}


@app.post("/api/v1/ai/time-estimates/learn")
def learn_time_estimate_endpoint(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    minutes = int(payload.get("minutes", 10))
    archetype = payload.get("archetype")
    keyword = payload.get("keyword")
    feedback = str(payload.get("feedback") or f"Updated {archetype or keyword} to {minutes}m")
    res = memory_store.learn_duration_preference(
        target_archetype=archetype,
        minutes=minutes,
        raw_user_feedback=feedback,
        custom_keyword=keyword,
        also_calibrate_related=bool(payload.get("also_calibrate_related", True)),
    )
    resized = recalibrate_scheduled_blocks(session)
    return {**res, "resized_blocks": resized}


@app.get("/api/v1/ai/notes")
def read_ai_notes() -> Dict[str, Any]:
    return memory_store.get_notes()


@app.put("/api/v1/ai/notes")
def write_ai_notes(payload: MarkdownDocUpdate) -> Dict[str, Any]:
    return memory_store.save_notes(payload.content)


@app.post("/api/v1/ai/notes/append")
def append_ai_note(payload: QuickNoteAppendRequest) -> Dict[str, Any]:
    return memory_store.append_note(payload.text, category=payload.category)


# ---------------------------------------------------------------------------
# Monitored Websites / Apps Manager (installed-apps registry under data/)
# ---------------------------------------------------------------------------
@app.get("/api/v1/apps")
def get_monitored_apps(session: Session = Depends(get_session)) -> Dict[str, Any]:
    global_cfg, apps = apps_manager.get_global_and_apps()
    poll_sec = int(global_cfg.get("poll_interval_seconds", 30))
    unread_by_app = reconcile_app_unread_counts(session)
    hbs = {h.source_app: h for h in session.exec(select(ScraperHeartbeat)).all()}
    live_tabs = {t["slug"]: t for t in browser_stream_manager.list_live_tabs()}

    enriched = []
    for a in apps:
        hb = hbs.get(a["name"])
        lt = live_tabs.get(a["slug"])
        st_code, st_label = evaluate_honest_session_state(hb, poll_interval_seconds=poll_sec)
        if lt and st_code not in ("warm", "auth_required"):
            st_code = lt["status"]
            st_label = "Warm on Server" if st_code == "warm" else "Sign-In Needed"
        enriched.append(
            {
                **a,
                "runtime_status": st_code,
                "runtime_label": st_label,
                "stream_active": bool(lt),
                "active_viewers": lt["active_viewers"] if lt else 0,
                "unread_count": unread_by_app.get(a["name"], 0),
            }
        )
    return {
        "config_path": settings.apps_config_path,
        "global": global_cfg,
        "apps": enriched,
    }


@app.post("/api/v1/apps", status_code=status.HTTP_201_CREATED)
def add_monitored_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        saved = apps_manager.add_or_replace_app(payload)
        return {"app": saved, "config_path": settings.apps_config_path}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/v1/apps/{slug}")
async def patch_monitored_app(
    slug: str,
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    updated = apps_manager.update_app(slug, payload)
    if not updated:
        raise HTTPException(status_code=404, detail=f"App '{slug}' not found")

    old_name = str(updated.get("previous_name") or "").strip()
    old_slug = str(updated.get("previous_slug") or slug).strip()
    new_name = str(updated.get("name") or "").strip()
    new_slug = str(updated.get("slug") or slug).strip()

    migrated_notifications = 0
    if old_name and new_name and (old_name != new_name or old_slug != new_slug):
        from .apps_manager import slugify_app_name

        # 1. Cascade-update all existing Inbox Notification rows so there are NEVER 2 tags after rename!
        all_notifs = list(session.exec(select(Notification)).all())
        for n in all_notifs:
            if n.source_app == old_name or slugify_app_name(n.source_app) == old_slug:
                n.source_app = new_name
                session.add(n)
                migrated_notifications += 1

        # 2. Merge/rename ScraperHeartbeat row so old_name heartbeat doesn't linger
        old_hb = session.get(ScraperHeartbeat, old_name)
        if old_hb and old_name != new_name:
            existing_new_hb = session.get(ScraperHeartbeat, new_name)
            if not existing_new_hb:
                session.add(
                    ScraperHeartbeat(
                        source_app=new_name,
                        status=old_hb.status,
                        unread_count=old_hb.unread_count,
                        last_checked_at=old_hb.last_checked_at,
                        error_detail=old_hb.error_detail,
                    )
                )
            session.delete(old_hb)

        # 3. Cascade-update CourseGrade rows sourced from this app
        for cg in session.exec(select(CourseGrade)).all():
            if cg.source_app == old_name or slugify_app_name(cg.source_app) == old_slug:
                cg.source_app = new_name
                session.add(cg)

        session.commit()
        reconcile_honest_heartbeats(session)

        # 4. Re-key any live server browser tab in-place without closing or reloading the page!
        await browser_stream_manager.rename_tab(old_slug, new_slug, new_name)

    return {
        "app": updated,
        "previous_name": old_name,
        "previous_slug": old_slug,
        "migrated_notifications": migrated_notifications,
    }


@app.delete("/api/v1/apps/{slug}")
async def remove_monitored_app(slug: str) -> Dict[str, Any]:
    await browser_stream_manager.close_tab(slug)
    removed = apps_manager.delete_app(slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"App '{slug}' not found")
    return {"deleted": slug}


@app.post("/api/v1/apps/ai-assist")
async def ai_assist_app_config(payload: AIAssistAppRequest) -> Dict[str, Any]:
    return await apps_manager.build_ai_suggestion(
        prompt_or_url=payload.query,
        zero_knowledge_preferred=payload.zero_knowledge,
    )


@app.post("/api/v1/apps/{slug}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_monitored_app_account(
    slug: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = payload or {}
    try:
        cloned = apps_manager.duplicate_for_new_account(
            slug,
            custom_name=body.get("label") or body.get("name") or "",
            custom_profile=body.get("account_profile") or body.get("profile") or "",
        )
        return {"app": cloned, "config_path": settings.apps_config_path}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/apps/{slug}/ai-calibrate")
async def ai_calibrate_live_app(slug: str) -> Dict[str, Any]:
    try:
        return await browser_stream_manager.calibrate_app_with_ai(slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# PowerSchool / SIS Grades Tracker API (`/api/v1/grades*`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/grades")
def get_grades_dashboard(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """
    Grades dashboard with user ownership applied.

    Courses the user removed/hid are filtered out here (so a re-scan can never
    resurrect them) and the summary is recomputed over only the visible set.
    """
    from .grades_control import grades_control_service

    dash = grades_service.get_dashboard(session)
    all_courses = dash.get("courses", [])
    visible = grades_control_service.apply_to_courses(session, all_courses)

    scores = [float(c["score_percent"]) for c in visible if c.get("score_percent") is not None and c.get("counts_toward_average", True)]
    gpas = [
        pts
        for pts in (
            __import__("app.grades_service", fromlist=["_letter_to_gpa_points"])._letter_to_gpa_points(
                str(c.get("letter_grade") or ""), c.get("score_percent")
            )
            for c in visible
            if c.get("counts_toward_gpa", True)
        )
        if pts is not None
    ]
    summary = dict(dash.get("summary") or {})
    summary.update(
        {
            "total_courses": len(visible),
            "average_percent": round(sum(scores) / len(scores), 2) if scores else None,
            "unweighted_gpa": round(sum(gpas) / len(gpas), 2) if gpas else None,
            "total_assignments": sum(int(c.get("assignments_count") or 0) for c in visible),
            "total_missing": sum(int(c.get("missing_count") or 0) for c in visible),
            "hidden_courses": max(0, len(all_courses) - len(visible)),
        }
    )
    return {
        **dash,
        "courses": visible,
        "summary": summary,
        "tabs": grades_control_service.list_tabs(session),
        "scoped": grades_control_service.scoped_summaries(session, visible),
    }


@app.post("/api/v1/grades/scan")
async def scan_grades_now(
    slug: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    scan_res = await browser_stream_manager.scan_grades_tabs(
        target_slug=slug,
        reload_idle_tabs=True,
        open_if_configured=True,
    )
    dash = get_grades_dashboard(session)
    return {
        "scan": scan_res,
        **dash,
    }


@app.post("/api/v1/grades/ingest")
def ingest_grades_snapshot(
    payload: GradeSnapshotIngestRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    res = grades_service.ingest_courses(
        session,
        source_app=payload.source_app,
        raw_courses=[c if isinstance(c, dict) else c.model_dump() for c in payload.courses],
        emit_pings=payload.emit_pings,
    )
    dash = get_grades_dashboard(session)
    return {
        "ingest": res,
        **dash,
    }


@app.delete("/api/v1/grades/{course_id}")
def delete_tracked_course(course_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    row = session.get(CourseGrade, course_id)
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")
    session.delete(row)
    session.commit()
    return {"deleted": course_id}


# ---------------------------------------------------------------------------
# Server-Side Browser Streamer (`/api/v1/stream/*`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/stream/tabs")
def list_stream_tabs() -> Dict[str, Any]:
    return {
        "engine": browser_stream_manager.get_engine_info(),
        "tabs": browser_stream_manager.list_live_tabs(),
    }


@app.post("/api/v1/stream/tabs/{slug}/open")
async def open_server_tab(slug: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        custom_url = (payload or {}).get("url")
        tab = await browser_stream_manager.get_or_open_tab(slug, custom_url=custom_url)
        return {
            "slug": tab.slug,
            "name": tab.app_name,
            "url": tab.page.url,
            "status": tab.status,
            "zero_knowledge": tab.zero_knowledge,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/v1/stream/tabs/{slug}")
async def close_server_tab(slug: str) -> Dict[str, Any]:
    closed = await browser_stream_manager.close_tab(slug)
    return {"slug": slug, "closed": closed}


@app.websocket("/api/v1/stream/ws/{slug}")
async def stream_app_websocket(websocket: WebSocket, slug: str) -> None:
    await browser_stream_manager.handle_stream_websocket(websocket, slug)


# ---------------------------------------------------------------------------
# Client → Server File Transfer & Wallpaper Gallery (`/api/v1/files`, `/api/v1/wallpapers`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/wallpapers")
def list_os_wallpapers() -> Dict[str, Any]:
    return {"wallpapers": file_drop_service.list_wallpapers()}


@app.get("/api/v1/files")
def list_transferred_files() -> Dict[str, Any]:
    return {
        "uploads_dir": str(file_drop_service.uploads_dir),
        "files": file_drop_service.list_files(),
    }


@app.post("/api/v1/files/upload", status_code=status.HTTP_201_CREATED)
async def upload_client_file(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    filename = str(payload.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    try:
        saved = file_drop_service.save_uploaded_file(
            filename=filename,
            content_base64=payload.get("content_base64"),
            text_content=payload.get("text_content"),
        )
        ingest_mode = str(payload.get("ingest_mode") or "store").strip()
        ingest_result = None
        if ingest_mode and ingest_mode != "store":
            ingest_result = await file_drop_service.ingest_file_async(saved["filename"], ingest_mode, session)
        return {"file": saved, "ingest": ingest_result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/files/raw/{filename}")
def download_raw_file(filename: str) -> FileResponse:
    try:
        path = file_drop_service.get_file_path(filename)
        return FileResponse(path=str(path), media_type=guess_mime_type(path), filename=path.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")


@app.post("/api/v1/files/{filename}/ingest")
async def ingest_transferred_file(
    filename: str,
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    mode = str(payload.get("mode") or "to_notebook")
    try:
        return await file_drop_service.ingest_file_async(filename, mode, session)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/v1/files/{filename}")
def delete_transferred_file(filename: str) -> Dict[str, Any]:
    if not file_drop_service.delete_file(filename):
        raise HTTPException(status_code=404, detail="File not found")
    return {"deleted": filename}


# ---------------------------------------------------------------------------
# YouTube Anti-Distraction Accountability Guard (`/api/v1/focus-guard/*`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/focus-guard/status")
def get_focus_guard_status(session: Session = Depends(get_session)) -> Dict[str, Any]:
    return focus_guard_service.get_status(session)


@app.patch("/api/v1/focus-guard/settings")
def patch_focus_guard_settings(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return focus_guard_service.update_settings(payload, session)


@app.post("/api/v1/focus-guard/heartbeat")
def youtube_focus_guard_heartbeat(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return focus_guard_service.record_heartbeat(payload, session)


@app.post("/api/v1/focus-guard/respond")
def youtube_focus_guard_respond(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    action = str(payload.get("action") or "stop_watching")
    mins = payload.get("minutes")
    return focus_guard_service.respond_to_nudge(action, int(mins) if mins else None, session)


@app.post("/api/v1/focus-guard/justify")
async def youtube_focus_guard_justify(
    payload: YoutubeJustifyRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return await focus_guard_service.evaluate_justification(
        reasoning=payload.reasoning,
        payload_meta=payload.model_dump(exclude_none=True),
        session=session,
    )


# ---------------------------------------------------------------------------
# Hourly AI Cron Jobs & Bedtime Schedule Priority Engine (`/api/v1/cron/*`)
# ---------------------------------------------------------------------------
@app.get("/api/v1/cron/status")
def get_cron_status(
    reference_now_iso: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return cron_scheduler.get_status(session, reference_now_iso=reference_now_iso)


@app.post("/api/v1/cron/run")
async def trigger_cron_now(
    payload: Optional[CronRunRequest] = None,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    trig = payload.trigger if payload else "manual"
    ref_iso = payload.reference_now_iso if payload else None
    return await cron_scheduler.run_cron_check(
        session=session,
        trigger=trig,
        reference_now_iso=ref_iso,
    )


@app.patch("/api/v1/cron/settings")
def update_cron_settings(
    payload: CronSettingsUpdate,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    cron_scheduler.save_settings(payload.model_dump(exclude_none=True))
    return cron_scheduler.get_status(session)


@app.get("/api/v1/focus-guard/youtube-guard.user.js")
def download_youtube_guard_userscript(request: Request, origin: Optional[str] = Query(default=None)) -> Response:
    pctx = _extract_proxy_context(request)
    hub_origin = (origin or pctx["public_origin"] or str(request.base_url)).rstrip("/")
    guard_tok = auth_service.get_guard_token("focus_guard")
    body = focus_guard_service.build_userscript(hub_origin, guard_token=guard_tok)
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="node-youtube-guard.user.js"'},
    )


@app.get("/api/v1/focus-guard/extension.zip")
def download_youtube_guard_extension(request: Request, origin: Optional[str] = Query(default=None)) -> Response:
    pctx = _extract_proxy_context(request)
    hub_origin = (origin or pctx["public_origin"] or str(request.base_url)).rstrip("/")
    guard_tok = auth_service.get_guard_token("focus_guard")
    raw_zip = focus_guard_service.build_extension_zip(hub_origin, guard_token=guard_tok)
    return Response(
        content=raw_zip,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="node-youtube-focus-guard-extension.zip"'},
    )


# ---------------------------------------------------------------------------
# Comprehensive WebOS Settings, Presets, Code Map & AI Settings/Code Studio
# ---------------------------------------------------------------------------
@app.get("/api/v1/settings")
def get_os_settings() -> Dict[str, Any]:
    return os_settings_service.full_payload()


@app.patch("/api/v1/settings")
def patch_os_settings(payload: OSSettingsUpdateRequest) -> Dict[str, Any]:
    try:
        merged_patch: Dict[str, Any] = {}
        if getattr(payload, "model_extra", None):
            merged_patch.update(payload.model_extra)
        if payload.values:
            merged_patch.update(payload.values)
        if payload.settings_patch:
            merged_patch.update(payload.settings_patch)
        return os_settings_service.update_state(
            settings_patch=merged_patch,
            custom_css=payload.custom_css,
            custom_js=payload.custom_js,
            active_preset_id=payload.active_preset_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/settings/reset")
def reset_os_settings_to_default() -> Dict[str, Any]:
    return os_settings_service.load_preset("default-mint")


@app.get("/api/v1/settings/code-map")
def get_os_code_map() -> Dict[str, Any]:
    md_path = Path(__file__).resolve().parents[2] / "WEBOS_ARCHITECTURE_MAP.md"
    arch_md = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    return {
        "code_map": WEBOS_CODE_MAP,
        "schema": SETTINGS_SCHEMA,
        "architecture_markdown": arch_md,
    }


@app.post("/api/v1/settings/presets", status_code=201)
def save_os_preset(payload: OSPresetSaveRequest) -> Dict[str, Any]:
    try:
        record, full = os_settings_service.save_preset(
            name=payload.name,
            description=payload.description,
            settings_patch=payload.settings_patch,
            custom_css=payload.custom_css,
            custom_js=payload.custom_js,
            preset_id=payload.preset_id,
            activate=payload.activate,
        )
        return {"preset": record, "state": full}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/settings/presets/{preset_id}/load")
def load_os_preset(preset_id: str) -> Dict[str, Any]:
    try:
        return os_settings_service.load_preset(preset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/v1/settings/presets/{preset_id}")
def delete_os_preset(preset_id: str) -> Dict[str, Any]:
    try:
        return os_settings_service.delete_preset(preset_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/settings/ai-assist")
async def ai_assist_os_settings(payload: OSSettingsAIAssistRequest) -> Dict[str, Any]:
    try:
        return await os_settings_service.ai_assist(
            prompt=payload.prompt,
            save_as_preset=payload.save_as_preset,
            preset_name=payload.preset_name,
            apply_immediately=payload.apply_immediately,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# v10.0 Server-Side AI Job Queue, Multi-Provider Models, Shared Logs,
#       App Library, Universal Social Focus Guard, Resizable Widgets & SDK
# ---------------------------------------------------------------------------
@app.post("/api/v1/ai/jobs/chat", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_server_ai_chat_job(
    payload: ChatRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Fire-and-forget server-side AI prompt submission.
    Returns immediately (<15ms) with `job_uid` and `user_message` while generation
    and OS tool calls execute asynchronously on the server.
    """
    pctx = _extract_proxy_context(request)
    return ai_runtime_manager.submit_chat_job(
        prompt=payload.message,
        thread_id=payload.thread_id or "main",
        current_view=payload.current_view,
        enable_thinking=payload.enable_thinking,
        requested_model=payload.model,
        client_host=pctx["client_ip"],
        forwarded_for=pctx["forwarded_for"],
        request_host=pctx["request_host"],
    )


@app.get("/api/v1/ai/jobs")
def list_server_ai_jobs(
    thread_id: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return {
        "jobs": ai_runtime_manager.list_jobs(
            session,
            thread_id=thread_id,
            active_only=active_only,
            limit=limit,
        )
    }


@app.get("/api/v1/ai/jobs/{job_uid}")
def get_server_ai_job(
    job_uid: str,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    from .models import AIJob

    job = session.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    return {"job": ai_runtime_manager.serialize_job(job)}


@app.get("/api/v1/ai/jobs/{job_uid}/stream")
async def attach_to_server_ai_job_stream(job_uid: str) -> StreamingResponse:
    async def _gen():
        try:
            async for ev in ai_runtime_manager.stream_job_events(job_uid):
                yield json.dumps(ev) + "\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/models/providers")
async def list_ai_providers(refresh: bool = Query(default=False)) -> Dict[str, Any]:
    await ai_provider_registry.discover_all_models(force=refresh)
    return ai_provider_registry.public_state()


@app.post("/api/v1/models/providers", status_code=status.HTTP_201_CREATED)
async def upsert_ai_provider(payload: AIProviderUpsertRequest) -> Dict[str, Any]:
    saved = ai_provider_registry.upsert_provider(
        provider_id=payload.provider_id,
        name=payload.name,
        protocol=payload.protocol,
        base_url=payload.base_url,
        api_key=payload.api_key,
        models=payload.models,
        enabled=payload.enabled,
        auto_discover=payload.auto_discover,
    )
    discovered = await ai_provider_registry.discover_all_models(force=True)
    return {
        "provider": saved,
        "state": ai_provider_registry.public_state(),
        "discovered_models": discovered,
    }


@app.delete("/api/v1/models/providers/{provider_id}")
async def remove_ai_provider(provider_id: str) -> Dict[str, Any]:
    removed = ai_provider_registry.delete_provider(provider_id)
    await ai_provider_registry.discover_all_models(force=True)
    return {"removed": removed, "state": ai_provider_registry.public_state()}


@app.patch("/api/v1/models/routing")
async def update_ai_role_routing(payload: AIRoleRoutingRequest) -> Dict[str, Any]:
    routing = ai_provider_registry.update_routing(payload.model_dump(exclude_none=True))
    status_data = await model_router.refresh_status(force=True)
    return {"routing": routing, "status": status_data}


@app.post("/api/v1/models/ping")
async def register_remote_model_heartbeat(payload: ModelPingRequest) -> Dict[str, Any]:
    """
    Allows any local or remote Ollama host / model worker to ping Node and dynamically
    register all models it serves (`qwen3.5:0.8b`, `gemma4:e2b`, `qwen3.5:2b`, etc.).
    """
    prov = ai_provider_registry.register_model_ping(
        provider_id=payload.provider_id,
        provider_name=payload.provider_name,
        protocol=payload.protocol,
        base_url=payload.base_url,
        models=payload.models,
        api_key=payload.api_key,
        capabilities=payload.capabilities,
    )
    discovered = await ai_provider_registry.discover_all_models(force=True)
    return {
        "ok": True,
        "provider": prov,
        "discovered_models": discovered,
    }


@app.post("/api/v1/models/discover")
async def force_discover_models() -> Dict[str, Any]:
    return await model_router.refresh_status(force=True)


@app.get("/api/v1/ai/shared-logs")
def get_shared_agent_logs(
    category: Optional[str] = Query(default=None),
    query: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return {
        "logs": agent_logbook.list_logs(session, category=category, query=query, limit=limit),
        "markdown_path": str(agent_logbook.md_path),
    }


@app.post("/api/v1/ai/shared-logs", status_code=status.HTTP_201_CREATED)
def upsert_shared_agent_log(
    payload: AgentLogUpsertRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    if payload.log_id:
        updated = agent_logbook.edit_log(
            session,
            payload.log_id,
            title=payload.title,
            content=payload.content,
            category=payload.category,
            tags=payload.tags,
            pinned=payload.pinned,
            agent_name=payload.agent_name,
            model_id=payload.model_id,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Shared log entry not found")
        return {"log": updated}
    saved = agent_logbook.write_log(
        session,
        title=payload.title,
        content=payload.content,
        agent_name=payload.agent_name,
        model_id=payload.model_id,
        category=payload.category,
        tags=payload.tags,
        pinned=payload.pinned,
    )
    return {"log": saved}


@app.delete("/api/v1/ai/shared-logs/{log_id}")
def remove_shared_agent_log(
    log_id: int,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    ok = agent_logbook.delete_log(session, log_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Shared log entry not found")
    return {"deleted": log_id}


@app.get("/api/v1/ai/web-search")
async def api_web_search(
    q: str = Query(..., min_length=1, max_length=300),
    max_results: int = Query(default=6, ge=1, le=10),
) -> Dict[str, Any]:
    routing = ai_provider_registry.load_state().get("routing") or {}
    return await search_web(q, max_results=max_results, searxng_url=str(routing.get("searxng_url") or ""))


@app.get("/api/v1/apps/catalog")
def get_app_library_catalog() -> Dict[str, Any]:
    return {"catalog": apps_manager.list_catalog()}


@app.post("/api/v1/apps/install-template", status_code=status.HTTP_201_CREATED)
def install_app_library_template(
    payload: InstallCatalogAppRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    installed = apps_manager.install_from_catalog(
        template_id=payload.template_id,
        custom_name=payload.custom_name,
        custom_url=payload.custom_url,
    )
    if not installed:
        raise HTTPException(status_code=404, detail=f"Template '{payload.template_id}' not found in App Library")
    reconcile_honest_heartbeats(session)
    return {"app": installed, "catalog": apps_manager.list_catalog()}


@app.get("/api/v1/focus-guard/focus-guard.user.js")
def download_universal_focus_guard_userscript(request: Request) -> Response:
    pctx = _extract_proxy_context(request)
    js = focus_guard_service.build_userscript(
        hub_origin=pctx["public_origin"],
        guard_token=auth_service.get_guard_token("focus_guard"),
    )
    return Response(content=js, media_type="application/javascript")


@app.post("/api/v1/focus-guard/platforms", status_code=status.HTTP_201_CREATED)
def upsert_focus_guard_platform(
    payload: FocusSiteRuleRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return focus_guard_service.upsert_platform_rule(
        domain=payload.domain,
        name=payload.name,
        enabled=payload.enabled,
        daily_limit_minutes=payload.daily_limit_minutes,
        short_form_immediate=payload.short_form_immediate,
        session=session,
    )


@app.delete("/api/v1/focus-guard/platforms/{platform_id}")
def delete_focus_guard_platform(
    platform_id: str,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    return focus_guard_service.delete_platform_rule(platform_id, session=session)


@app.get("/api/v1/desktop/widgets")
def get_desktop_widget_layout() -> Dict[str, Any]:
    return {
        "widgets": extensions_manager.get_desktop_widgets(),
        "builtin_catalog": BUILTIN_WIDGET_CATALOG,
        "installed_extensions": extensions_manager.list_installed(),
    }


@app.put("/api/v1/desktop/widgets")
def save_desktop_widget_layout(payload: DesktopWidgetsSaveRequest) -> Dict[str, Any]:
    saved = extensions_manager.save_desktop_widgets(payload.widgets)
    return {"widgets": saved}


@app.post("/api/v1/desktop/widgets/reset")
def reset_desktop_widget_layout() -> Dict[str, Any]:
    saved = extensions_manager.reset_desktop_widgets()
    return {"widgets": saved}


@app.get("/api/v1/extensions")
def list_installed_extensions() -> Dict[str, Any]:
    return {
        "extensions": extensions_manager.list_installed(),
        "gallery": extensions_manager.list_gallery(),
    }


@app.get("/api/v1/extensions/gallery")
def list_extension_gallery() -> Dict[str, Any]:
    return {"gallery": extensions_manager.list_gallery()}


@app.post("/api/v1/extensions/install", status_code=status.HTTP_201_CREATED)
def install_custom_extension(payload: ExtensionInstallRequest) -> Dict[str, Any]:
    try:
        ext = extensions_manager.install_extension(
            manifest=payload.manifest,
            html=payload.html or "",
            widget_html=payload.widget_html or "",
            overwrite=payload.overwrite,
        )
        return {"extension": ext, "extensions": extensions_manager.list_installed()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/extensions/gallery/{gallery_id}/install", status_code=status.HTTP_201_CREATED)
def install_starter_gallery_extension(gallery_id: str) -> Dict[str, Any]:
    ext = extensions_manager.install_from_gallery(gallery_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"Gallery extension '{gallery_id}' not found")
    return {
        "extension": ext,
        "extensions": extensions_manager.list_installed(),
        "gallery": extensions_manager.list_gallery(),
    }


@app.post("/api/v1/extensions/upload", status_code=status.HTTP_201_CREATED)
def upload_extension_package(payload: Dict[str, Any]) -> Dict[str, Any]:
    fname = str(payload.get("filename") or "package.nodeapp.json")
    raw_b64 = payload.get("content_base64")
    raw_txt = payload.get("text_content")
    try:
        if raw_b64:
            raw_bytes = base64.b64decode(raw_b64)
        elif raw_txt:
            raw_bytes = str(raw_txt).encode("utf-8")
        else:
            raise ValueError("Provide `content_base64` or `text_content`.")
        ext = extensions_manager.install_from_package_bytes(raw_bytes, filename=fname)
        return {"extension": ext, "extensions": extensions_manager.list_installed()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/extensions/{ext_id}/export")
def export_custom_extension(ext_id: str) -> Dict[str, Any]:
    pkg = extensions_manager.export_package(ext_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Extension not found")
    return pkg


@app.delete("/api/v1/extensions/{ext_id}")
def uninstall_custom_extension(ext_id: str) -> Dict[str, Any]:
    removed = extensions_manager.uninstall_extension(ext_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Extension not found")
    return {
        "deleted": ext_id,
        "extensions": extensions_manager.list_installed(),
        "gallery": extensions_manager.list_gallery(),
        "widgets": extensions_manager.get_desktop_widgets(),
    }


@app.get("/api/v1/extensions/{ext_id}/storage")
def get_extension_storage(ext_id: str) -> Dict[str, Any]:
    return {"storage": extensions_manager.get_storage(ext_id)}


@app.post("/api/v1/extensions/{ext_id}/storage")
def set_extension_storage(ext_id: str, payload: ExtensionStorageRequest) -> Dict[str, Any]:
    try:
        saved = extensions_manager.set_storage(ext_id, payload.data, merge=payload.merge)
        return {"storage": saved}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/v1/extensions/sdk-docs")
def get_extensions_sdk_docs() -> Dict[str, Any]:
    doc_file = Path(settings.docs_dir) / "APPS_AND_WIDGETS_SDK.md"
    content = doc_file.read_text(encoding="utf-8") if doc_file.is_file() else "# Node Apps & Widgets SDK"
    return {"markdown": content, "permissions": sorted(list({"storage", "tasks.read", "tasks.write", "calendar.read", "grades.read", "inbox.read", "notifications.send", "ai.ask"}))}


# ===========================================================================
# v10.1 Upgrades: Inbox Custom Categories + Von-First Auto-Sort, Unified AI
# Instructions + Chat Feedback Self-Improvement, & Shared Workspace Drive
# ===========================================================================


@app.get("/api/v1/notifications/categories")
async def get_inbox_categories_config(app_slug: Optional[str] = None) -> Dict[str, Any]:
    _, all_apps = apps_manager.get_global_and_apps()
    per_app = {a["slug"]: list(a.get("inbox_categories") or []) for a in all_apps}
    global_cats = model_router.get_active_inbox_categories(None)
    merged_cats = model_router.get_active_inbox_categories(app_slug) if app_slug else list(global_cats)
    if not app_slug:
        seen = {c.lower() for c in merged_cats}
        for app_list in per_app.values():
            for c in app_list:
                if c.lower() not in seen:
                    seen.add(c.lower())
                    merged_cats.append(c)

    fast_id = await model_router.pick_fast_ollama_model()
    von_ready = bool(model_router.von_base and model_router._von_online)
    return {
        "global_categories": global_cats,
        "per_app_categories": per_app,
        "all_categories": merged_cats,
        "autosort_engine": {
            "primary": "von",
            "von_configured": bool(model_router.von_base),
            "von_online": von_ready,
            "fallback_fast_model": fast_id or "auto",
            "active_label": "Von (/v1/systemone)" if von_ready else (f"Fast AI ({fast_id.split('::')[-1]})" if fast_id else "Fast AI / Smart Rules"),
        },
    }


@app.post("/api/v1/notifications/categories")
async def update_inbox_categories_config(payload: InboxCategoriesUpdateRequest) -> Dict[str, Any]:
    act = (payload.action or "set").strip().lower()
    single = (payload.category or "").strip()[:40]
    incoming_list = [str(c).strip()[:40] for c in (payload.categories or []) if str(c).strip()]
    if single and single not in incoming_list:
        incoming_list.append(single)

    if payload.app_slug:
        _, all_apps = apps_manager.get_global_and_apps()
        from .apps_manager import slugify_app_name

        tslug = slugify_app_name(payload.app_slug)
        target = next((a for a in all_apps if a["slug"] == tslug or a["name"].lower() == payload.app_slug.strip().lower()), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"App '{payload.app_slug}' not found")
        cur = list(target.get("inbox_categories") or [])
        if act == "set":
            cur = incoming_list
        elif act == "add":
            for item in incoming_list:
                if item.lower() not in {c.lower() for c in cur}:
                    cur.append(item)
        elif act == "remove":
            rm_lower = {item.lower() for item in incoming_list}
            cur = [c for c in cur if c.lower() not in rm_lower]
        apps_manager.update_app(target["slug"], {"inbox_categories": cur})
    else:
        cur = model_router.get_active_inbox_categories(None)
        if act == "set" and incoming_list:
            cur = incoming_list
        elif act == "add":
            for item in incoming_list:
                if item.lower() not in {c.lower() for c in cur}:
                    cur.append(item)
        elif act == "remove":
            rm_lower = {item.lower() for item in incoming_list}
            cur = [c for c in cur if c.lower() not in rm_lower]
        os_settings_service.update_state(settings_patch={"inbox_categories_csv": ", ".join(cur)})

    return await get_inbox_categories_config(payload.app_slug)


@app.post("/api/v1/notifications/autosort")
async def autosort_inbox_notifications(
    app_filter: Optional[str] = None,
    limit: int = Query(default=60, ge=1, le=200),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """
    Re-categorize active Inbox notifications using Von (`/v1/systemone`) FIRST,
    automatically falling back to the fastest/smallest available model (`role='fast'`),
    and honoring all global + per-app custom Inbox categories!
    """
    stmt = select(Notification).where(Notification.status == "active").order_by(Notification.id.desc()).limit(limit)
    rows = list(session.exec(stmt).all())
    if app_filter and app_filter.lower() != "all":
        rows = [r for r in rows if r.source_app.lower() == app_filter.strip().lower()]

    engines_used: Dict[str, int] = {}
    updated_count = 0
    for n in rows:
        if n.indicator_type == "ping_only":
            continue
        res = await model_router.categorize_item_von_first(
            source_app=n.source_app,
            title=n.title,
            snippet=n.ai_summary or n.message or "",
        )
        n.category = res["category"]
        n.priority = res["priority"]
        if n.user_importance_override is None:
            n.is_important = bool(res["is_important"])
        n.ai_processed = True
        session.add(n)
        eng = str(res.get("engine") or "heuristic")
        engines_used[eng] = engines_used.get(eng, 0) + 1
        updated_count += 1

    session.commit()
    cat_cfg = await get_inbox_categories_config(None)
    return {
        "status": "ok",
        "updated_count": updated_count,
        "sorted_count": updated_count,
        "engines_used": engines_used,
        "autosort_engine": cat_cfg["autosort_engine"],
    }


@app.get("/api/v1/ai/instructions")
def list_unified_ai_instructions(session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .self_improvement import self_improvement_service

    return {"files": self_improvement_service.list_unified_files(session)}


@app.get("/api/v1/ai/instructions/{slug}")
def read_unified_ai_instruction_file(slug: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .self_improvement import self_improvement_service

    return self_improvement_service.read_unified_file(slug, session)


@app.put("/api/v1/ai/instructions/{slug}")
def write_unified_ai_instruction_file(slug: str, payload: MarkdownDocUpdate) -> Dict[str, Any]:
    from .self_improvement import self_improvement_service

    return self_improvement_service.write_unified_file(slug, payload.content)


@app.delete("/api/v1/ai/instructions/{slug}")
def delete_unified_ai_instruction_file(slug: str) -> Dict[str, Any]:
    key = (slug or "").strip().lower()
    if key.endswith(".md"):
        key = key[:-3]
    if key in ("persona", "self_improvement", "shared_agent_log", "time_estimates"):
        raise HTTPException(status_code=400, detail="Core system instruction files cannot be deleted.")
    ok = memory_store.delete_notebook_file(key)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Instruction file '{slug}' not found.")
    return {"status": "deleted", "slug": key}


@app.post("/api/v1/ai/feedback", status_code=status.HTTP_201_CREATED)
async def submit_ai_chat_feedback(
    payload: AIFeedbackCreateRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    from .self_improvement import self_improvement_service

    entry = self_improvement_service.record_feedback(
        session,
        rating=payload.rating,
        message_id=payload.message_id,
        thread_id=payload.thread_id,
        reason=payload.reason,
        user_prompt=payload.user_prompt,
        assistant_reply=payload.assistant_reply,
        model_id=payload.model_id,
    )
    reflection: Optional[Dict[str, Any]] = None
    # Immediately reflect & update self_improvement.md on explicit poor/corrective feedback
    if entry.rating in ("poor", "frustration"):
        reflection = await self_improvement_service.reflect_and_self_improve(
            session,
            force=True,
            trigger="user_feedback",
        )
    return {
        "status": "recorded",
        "feedback": {
            "id": entry.id,
            "rating": entry.rating,
            "reason": entry.reason,
            "processed": entry.processed,
        },
        "reflection": reflection,
    }


@app.get("/api/v1/ai/feedback")
def list_ai_chat_feedback(
    limit: int = Query(default=40, ge=1, le=200),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    rows = session.exec(select(AIFeedbackEntry).order_by(AIFeedbackEntry.id.desc()).limit(limit)).all()
    return {
        "feedback": [
            {
                "id": r.id,
                "thread_id": r.thread_id,
                "message_id": r.message_id,
                "rating": r.rating,
                "reason": r.reason,
                "processed": r.processed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@app.post("/api/v1/ai/self-improve")
async def run_ai_self_improvement_now(session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .self_improvement import self_improvement_service

    return await self_improvement_service.reflect_and_self_improve(session, force=True, trigger="manual")


@app.get("/api/v1/drive")
@app.get("/api/v1/drive/tree")
def list_drive_directory(path: str = "", search: str = "") -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        return drive_service.list_directory(path, search=search)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/drive/mkdir", status_code=status.HTTP_201_CREATED)
@app.post("/api/v1/drive/folder", status_code=status.HTTP_201_CREATED)
def create_drive_folder(payload: DriveFolderRequest) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        created = drive_service.create_folder(payload.path)
        return {**created, "entry": created}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/drive/file")
def read_drive_file_endpoint(path: str = Query(..., min_length=1)) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        fdoc = drive_service.read_file(path)
        return {**fdoc, "file": fdoc}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/v1/drive/file")
def write_drive_file_endpoint(payload: DriveFileWriteRequest) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        fdoc = drive_service.write_text_file(payload.path, payload.content)
        return {**fdoc, "file": fdoc}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/drive/upload", status_code=status.HTTP_201_CREATED)
async def upload_drive_file_endpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        folder = str(payload.get("folder") or "")
        filename = str(payload.get("filename") or "upload.bin")
        b64 = str(payload.get("content_base64") or "")
        if "," in b64 and b64.lstrip().startswith("data:"):
            b64 = b64.split(",", 1)[1]
        raw = base64.b64decode(b64) if b64 else str(payload.get("content") or "").encode("utf-8")
        if len(raw) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File exceeds 50 MB limit")
        entry = drive_service.save_binary_file(folder, filename, raw)
        return {**entry, "entry": entry}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/drive/move")
def move_or_rename_drive_item(payload: DriveMoveRequest) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        moved = drive_service.move_or_rename(payload.src_path, payload.dest_path)
        return {**moved, "entry": moved}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/v1/drive")
@app.delete("/api/v1/drive/item")
def delete_drive_item_endpoint(path: str = Query(..., min_length=1)) -> Dict[str, Any]:
    from .drive_service import drive_service

    try:
        removed = drive_service.delete_item(path)
        if not removed:
            raise HTTPException(status_code=404, detail="Drive item not found")
        return {"deleted": path}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/drive/raw/{rel_path:path}")
def serve_drive_raw_file(rel_path: str) -> FileResponse:
    from .drive_service import drive_service

    try:
        target = drive_service.resolve_safe(rel_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Drive file not found")
        return FileResponse(path=str(target), filename=target.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



# ===========================================================================
# v10.2 — Decision Gate (LFM3 d1 / System One), Tag-First Inbox, Body Harvest,
#         Gmail-style Filters, Grades Ownership, API Bridges, New Tab Modes
# ===========================================================================


@app.get("/api/v1/decision-gate/status")
async def decision_gate_status(force: bool = Query(default=False)) -> Dict[str, Any]:
    from .decision_gate import decision_gate

    return await decision_gate.status(force=force)


@app.post("/api/v1/decision-gate/selftest")
async def decision_gate_selftest(payload: DecisionGateSelfTestRequest) -> Dict[str, Any]:
    """Round-trip a real tagging request so the UI can prove the gate works."""
    from .decision_gate import decision_gate, tag_notification
    from .inbox_tags import inbox_tag_service

    status = await decision_gate.status(force=True)
    if not status.get("online"):
        return {"ok": False, "status": status, "error": "Decision gate offline"}
    started = time.monotonic()
    res = await tag_notification(
        source_app="SelfTest",
        title=payload.text[:120],
        body=payload.text,
        tags=inbox_tag_service.list_tags(),
    )
    return {
        "ok": bool(res),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "status": status,
        "result": res,
    }


@app.post("/api/v1/ai/tool-plan")
async def preview_tool_plan(payload: ToolPlanRequest) -> Dict[str, Any]:
    """
    Show which tools the decision gate would attach for a message, plus the
    typed slots it extracted (title/date/time) — this is the debugging surface
    for "why did it create an event called *I have band practice on friday*".
    """
    from .decision_gate import plan_tools
    from .secretary import SECRETARY_TOOLS

    names = [t["function"]["name"] for t in SECRETARY_TOOLS]
    plan = await plan_tools(
        payload.message,
        tool_names=names,
        calendar_table=_calendar_table_for_router(),
        today_iso=datetime.now().date().isoformat(),
    )
    return {"ok": bool(plan), "tool_count": len(names), "plan": plan}


def _calendar_table_for_router() -> str:
    try:
        from .secretary import build_14_day_calendar_table, get_local_now

        return build_14_day_calendar_table(get_local_now())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Tag registry
# ---------------------------------------------------------------------------
@app.get("/api/v1/inbox/tags")
def list_inbox_tags() -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    rows = inbox_tag_service.list_tags()
    return {"tags": rows, "names": [t["name"] for t in rows]}


@app.post("/api/v1/inbox/tags")
def create_or_update_inbox_tag(payload: InboxTagUpsertRequest) -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    try:
        row = inbox_tag_service.upsert_tag(
            payload.name,
            description=payload.description,
            color=payload.color,
            important=payload.important,
            aliases=payload.aliases,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "tag": row,
        "tags": inbox_tag_service.list_tags(),
        "names": inbox_tag_service.tag_names(),
    }


@app.patch("/api/v1/inbox/tags")
def rename_inbox_tag(payload: InboxTagRenameRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .inbox_harvest import backfill_dedupe_keys
    from .inbox_tags import inbox_tag_service, parse_tags_csv, tags_to_csv

    try:
        res = inbox_tag_service.rename_tag(payload.old_name, payload.new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Rewrite the tag on every row that carries it so nothing is orphaned.
    migrated = 0
    for n in session.exec(select(Notification)).all():
        tags = parse_tags_csv(n.tags_csv)
        if not tags:
            continue
        replaced = [res["new"] if t.lower() == res["old"].lower() else t for t in tags]
        if replaced != tags:
            n.tags_csv = tags_to_csv(replaced)
            if (n.category or "").lower() == res["old"].lower():
                n.category = res["new"]
            session.add(n)
            migrated += 1
    session.commit()
    backfill_dedupe_keys(session)
    return {
        **res,
        "migrated_notifications": migrated,
        "tags": inbox_tag_service.list_tags(),
        "names": inbox_tag_service.tag_names(),
    }


@app.delete("/api/v1/inbox/tags/{name}")
def delete_inbox_tag(name: str) -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    try:
        ok = inbox_tag_service.delete_tag(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Tag not found")
    return {"deleted": name, "tags": inbox_tag_service.list_tags(), "names": inbox_tag_service.tag_names()}


# ---------------------------------------------------------------------------
# Saved filters + query language
# ---------------------------------------------------------------------------
@app.get("/api/v1/inbox/filters")
def list_inbox_filters() -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    return {
        "filters": inbox_tag_service.list_filters(),
        "grammar": inbox_tag_service.filter_dsl_for_ai(),
    }


@app.post("/api/v1/inbox/filters")
def save_inbox_filter(payload: InboxFilterSaveRequest) -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    row = inbox_tag_service.save_filter(
        payload.name,
        payload.query,
        color=payload.color,
        pinned=payload.pinned,
        order=payload.order,
        filter_id=payload.filter_id,
    )
    return {"ok": True, "filter": row, "filters": inbox_tag_service.list_filters()}


@app.delete("/api/v1/inbox/filters/{filter_id}")
def delete_inbox_filter(filter_id: str) -> Dict[str, Any]:
    from .inbox_tags import inbox_tag_service

    try:
        ok = inbox_tag_service.delete_filter(filter_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Filter not found")
    return {"deleted": filter_id, "filters": inbox_tag_service.list_filters()}


@app.get("/api/v1/inbox/query")
def query_inbox(
    q: str = Query(default="", max_length=600),
    limit: int = Query(default=200, ge=1, le=1000),
    include_archived: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Gmail-style Inbox query over tags, sender, source, dates and importance."""
    from .inbox_tags import inbox_tag_service

    ast = inbox_tag_service.parse_filter(q)
    rows = list(
        session.exec(select(Notification).order_by(Notification.timestamp.desc()).limit(1200)).all()
    )
    now = datetime.now(timezone.utc)
    matched = []
    for n in rows:
        payload = n.model_dump(mode="json")
        if not include_archived and (n.folder or "inbox") == "archive" and "is:archived" not in (q or ""):
            continue
        if inbox_tag_service.matches(payload, ast, now=now):
            matched.append(payload)
    return {
        "query": q,
        "explained": inbox_tag_service.explain_filter(q),
        "count": len(matched),
        "notifications": matched[:limit],
    }


# ---------------------------------------------------------------------------
# Body harvest, dedupe, tag moves & learning
# ---------------------------------------------------------------------------
@app.post("/api/v1/inbox/harvest")
async def harvest_inbox_bodies(payload: InboxHarvestRequest) -> Dict[str, Any]:
    from .inbox_harvest import inbox_harvester

    ids = payload.ids
    if not ids:
        with Session(engine) as session:
            rows = session.exec(
                select(Notification)
                .where(Notification.body_text == None)  # noqa: E711
                .order_by(Notification.timestamp.desc())
                .limit(max(1, min(25, payload.limit)))
            ).all()
            ids = [n.id for n in rows if n.id is not None]
    return await inbox_harvester.refresh_selected(ids, limit=max(1, min(25, payload.limit)))


@app.post("/api/v1/inbox/dedupe")
def dedupe_inbox(
    dry_run: bool = Query(default=False),
    window_hours: Optional[int] = Query(default=None, ge=1, le=720),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    from .inbox_harvest import backfill_dedupe_keys, merge_duplicate_notifications

    backfill_dedupe_keys(session)
    return merge_duplicate_notifications(session, window_hours=window_hours, dry_run=dry_run)


@app.post("/api/v1/inbox/{notification_id}/tags")
def move_notification_tag(
    notification_id: int,
    payload: InboxTagMoveRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Move a message to a tag (the correction that teaches the gate)."""
    from .inbox_tags import inbox_tag_service, parse_tags_csv, tags_to_csv

    notif = session.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    previous = parse_tags_csv(notif.tags_csv) or ([notif.category] if notif.category else [])
    new_tags = [payload.tag, *[t for t in previous if t.lower() != payload.tag.lower()]]
    notif.tags_csv = tags_to_csv(new_tags[:6])
    notif.category = payload.tag[:64]
    notif.tag_corrected_by_user = True
    session.add(notif)
    session.commit()

    learned = None
    if payload.learn:
        learned = inbox_tag_service.record_correction(
            sender=notif.sender or notif.source_app,
            subject=notif.title,
            from_tag=(previous[0] if previous else None),
            to_tag=payload.tag,
            source_app=notif.source_app,
        )
        try:
            session.add(
                InboxLearningHint(
                    notification_id=notif.id,
                    source_app=notif.source_app,
                    sender_key=(learned or {}).get("sender_key", ""),
                    subject=notif.title[:300],
                    body_excerpt=(notif.body_text or notif.message or "")[:1200],
                    from_tag=(previous[0] if previous else None),
                    to_tag=payload.tag,
                    action="move",
                )
            )
            session.commit()
        except Exception:
            session.rollback()
    session.refresh(notif)
    return {"ok": True, "notification": notif.model_dump(mode="json"), "learned": learned}


@app.post("/api/v1/inbox/bulk-tags")
def bulk_tag_notifications(payload: InboxBulkTagRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .inbox_tags import parse_tags_csv, tags_to_csv

    add = [t.strip() for t in payload.add_tags if t.strip()]
    remove = {t.strip().lower() for t in payload.remove_tags if t.strip()}
    affected = 0
    for nid in payload.ids:
        n = session.get(Notification, nid)
        if not n:
            continue
        tags = parse_tags_csv(n.tags_csv) or ([n.category] if n.category else [])
        if payload.set_tags is not None:
            tags = [t.strip() for t in payload.set_tags if t.strip()]
        else:
            tags = [t for t in tags if t.lower() not in remove]
            for t in add:
                if t.lower() not in {x.lower() for x in tags}:
                    tags.append(t)
        n.tags_csv = tags_to_csv(tags)
        if tags:
            n.category = tags[0][:64]
        if payload.archive is not None:
            n.folder = "archive" if payload.archive else "inbox"
        if payload.star is not None:
            n.starred = bool(payload.star)
        session.add(n)
        affected += 1
    session.commit()
    return {"ok": True, "affected": affected}


@app.get("/api/v1/inbox/learning")
def get_inbox_learning(limit: int = Query(default=60, ge=1, le=500), session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Learned corrections + raw fine-tuning export (JSONL-ready)."""
    from .inbox_tags import inbox_tag_service

    hints = list(
        session.exec(select(InboxLearningHint).order_by(InboxLearningHint.created_at.desc()).limit(limit)).all()
    )
    return {
        "rules": inbox_tag_service.learning_summary(limit),
        "hints": [
            {
                "id": h.id,
                "source_app": h.source_app,
                "sender_key": h.sender_key,
                "subject": h.subject,
                "body_excerpt": h.body_excerpt,
                "from_tag": h.from_tag,
                "to_tag": h.to_tag,
                "action": h.action,
                "created_at": h.created_at.isoformat() if h.created_at else None,
            }
            for h in hints
        ],
        "counts": {"rules": len(inbox_tag_service.learning_summary(500)), "hints": len(hints)},
    }


@app.get("/api/v1/inbox/learning/export")
def export_inbox_learning(session: Session = Depends(get_session)) -> Response:
    """Export corrections as JSONL suitable for local fine-tuning of the gate."""
    rows = list(session.exec(select(InboxLearningHint).order_by(InboxLearningHint.created_at.asc())).all())
    lines = []
    for h in rows:
        lines.append(
            json.dumps(
                {
                    "state": {
                        "service": h.source_app,
                        "sender": h.sender_key,
                        "subject": h.subject,
                        "body": h.body_excerpt,
                    },
                    "label": h.to_tag,
                    "rejected": h.from_tag,
                    "action": h.action,
                },
                ensure_ascii=False,
            )
        )
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="inbox_learning.jsonl"'},
    )


# ---------------------------------------------------------------------------
# Grades: course ownership + tabs
# ---------------------------------------------------------------------------
@app.get("/api/v1/grades/control")
def get_grades_control(session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .grades_control import grades_control_service

    dash = grades_service.get_dashboard(session)
    all_courses = dash.get("courses", [])
    visible = grades_control_service.apply_to_courses(session, all_courses)
    return {
        "tabs": grades_control_service.list_tabs(session),
        "visibility": grades_control_service.visibility_map(session),
        "hidden_courses": grades_control_service.hidden_courses(session, all_courses),
        "scoped": grades_control_service.scoped_summaries(session, visible),
        "visible_course_count": len(visible),
        "total_course_count": len(all_courses),
    }


@app.post("/api/v1/grades/visibility")
def set_grade_course_visibility(
    payload: GradeCourseVisibilityRequest,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    from .grades_control import grades_control_service

    row = grades_control_service.set_visibility(
        session,
        course_key=payload.course_key,
        source_app=payload.source_app,
        tracked=payload.tracked,
        hidden=payload.hidden,
        counts_toward_gpa=payload.counts_toward_gpa,
        counts_toward_average=payload.counts_toward_average,
        tab_id=payload.tab_id,
        pinned=payload.pinned,
    )
    return {"ok": True, "visibility": row}


@app.post("/api/v1/grades/hide")
def hide_grade_course(
    payload: Optional[GradeHideRequest] = Body(default=None),
    course_key: str = Query(default=""),
    source_app: str = Query(default=""),
    hidden: bool = Query(default=True),
    delete_row: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """
    Hide (or restore) a course. The visibility row is written even when the
    course is not in the DB yet, so a future Scan can never resurrect it.
    Accepts a JSON body (UI) or query params (scripts / AI tools).
    """
    from .grades_control import grades_control_service

    ck = ((payload.course_key if payload else "") or course_key or "").strip()
    if not ck:
        raise HTTPException(status_code=422, detail="course_key is required")
    src = (payload.source_app if payload else "") or source_app or ""
    want_hidden = bool(payload.hidden) if payload is not None else bool(hidden)
    del_row = bool(payload.delete_row) if payload is not None else bool(delete_row)

    course = session.exec(
        select(CourseGrade).where(CourseGrade.course_key == ck).order_by(CourseGrade.id.desc())
    ).first()

    if not want_hidden:
        row = grades_control_service.set_visibility(
            session,
            course_key=ck,
            source_app=src or (course.source_app if course else ""),
            tracked=True,
            hidden=False,
            counts_toward_gpa=True,
            counts_toward_average=True,
        )
        return {"ok": True, "hidden": row, "restored": True}

    if not course:
        # Nothing remembered yet — still remember the intent so the next scan respects it.
        row = grades_control_service.set_visibility(
            session, course_key=ck, source_app=src, tracked=False, hidden=True,
            counts_toward_gpa=False, counts_toward_average=False,
        )
        return {"ok": True, "hidden": row, "memory_only": True}

    res = grades_control_service.hide_course(session, course, also_delete_row=del_row)
    res["grade"] = get_grades_dashboard(session)
    return res


@app.get("/api/v1/grades/tabs")
def list_grade_tabs(session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .grades_control import grades_control_service

    return {"tabs": grades_control_service.list_tabs(session)}


@app.post("/api/v1/grades/tabs")
def upsert_grade_tab(payload: GradeTabUpsertRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .grades_control import grades_control_service

    tab = grades_control_service.upsert_tab(
        session,
        tab_id=payload.id,
        name=payload.name,
        color=payload.color,
        course_keys=payload.course_keys,
        match_rules=payload.match_rules,
        order=payload.order,
    )
    return {"ok": True, "tab": tab, "tabs": grades_control_service.list_tabs(session)}


@app.delete("/api/v1/grades/tabs/{tab_id}")
def delete_grade_tab(tab_id: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    from .grades_control import grades_control_service

    try:
        ok = grades_control_service.delete_tab(session, tab_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Tab not found")
    return {"deleted": tab_id, "tabs": grades_control_service.list_tabs(session)}


# ---------------------------------------------------------------------------
# API bridges (api-anything) + session cookie import
# ---------------------------------------------------------------------------
@app.get("/api/v1/bridges/status")
async def api_bridge_status(force: bool = Query(default=False)) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    return {**await api_bridge_service.status(force=force), "config": api_bridge_service.config()}


@app.post("/api/v1/bridges/config")
def api_bridge_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    return {"ok": True, "config": api_bridge_service.update_config(patch)}


@app.post("/api/v1/bridges/learn")
async def api_bridge_learn(payload: ApiBridgeLearnRequest) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    row = api_bridge_service.register_bridge(payload.app_slug, operation=payload.operation_name, notes=payload.url)
    status = await api_bridge_service.status(force=True)
    return {"ok": True, "bridge": row, "server": status}


@app.post("/api/v1/bridges/serve")
def api_bridge_serve(port: int = Query(default=8787, ge=1024, le=65535)) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    return api_bridge_service.start_server(port)


@app.post("/api/v1/bridges/call")
async def api_bridge_call(payload: ApiBridgeCallRequest) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    return await api_bridge_service.call(payload.operation, payload.args)


@app.post("/api/v1/bridges/session")
async def import_browser_session_cookies(payload: SessionCookieImportRequest) -> Dict[str, Any]:
    """Extension → server cookie handoff so server sessions survive local logins."""
    from .api_bridge import api_bridge_service

    return await api_bridge_service.import_session_cookies(payload.app_slug, payload.cookies, device=payload.device)


@app.get("/api/v1/bridges/session/{app_slug}")
async def export_browser_session_cookies(app_slug: str) -> Dict[str, Any]:
    from .api_bridge import api_bridge_service

    return await api_bridge_service.export_session_cookies(app_slug)


_EVENT_CUE_RE = re.compile(
    r"\b(calendar|event|appointment|meeting|practice|rehearsal|concert|gig|game|"
    r"dentist|doctor|flight|trip|vacation|birthday|party|interview|class|lecture|"
    r"shift|ceremony|performance|lesson|session|wedding|funeral)\b",
    re.IGNORECASE,
)
_TASK_CUE_RE = re.compile(
    r"\b(buy|order|call|email|text|pay|submit|turn in|finish|send|read|write|"
    r"fix|clean|email|reply|schedule|book|renew|sign|drop off|pick up)\b",
    re.IGNORECASE,
)
_DAY_CUE_RE = re.compile(
    r"\b(today|tonight|tomorrow|this (?:morning|afternoon|evening|weekend)|"
    r"next week|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})\b",
    re.IGNORECASE,
)
_TIME_CUE_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)


def _text_looks_like_event(text: str) -> bool:
    """
    Deterministic event-vs-task classifier used only when the gate is offline.

    An event needs a point in time plus something calendar-shaped; a verb like
    "buy" or "submit" keeps it a task even when a day is mentioned.
    """
    t = text or ""
    if not _DAY_CUE_RE.search(t):
        return False
    if _TASK_CUE_RE.search(t) and not _TIME_CUE_RE.search(t):
        return False
    return bool(_EVENT_CUE_RE.search(t) or _TIME_CUE_RE.search(t))


# ---------------------------------------------------------------------------
# Quick capture (new-tab one-liner -> task or calendar event)
# ---------------------------------------------------------------------------
@app.post("/api/v1/ai/quick-capture")
async def quick_capture(text_payload: Dict[str, Any], session: Session = Depends(get_session)) -> Dict[str, Any]:
    return await _quick_capture_impl(text_payload, session)


async def _quick_capture_impl(text_payload: Dict[str, Any], session: Session) -> Dict[str, Any]:
    from .decision_gate import plan_tools
    from .secretary import _clean_event_title, _parse_hhmm, _parse_natural_date, SECRETARY_TOOLS

    text = str((text_payload or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    plan = await plan_tools(
        text,
        tool_names=[t["function"]["name"] for t in SECRETARY_TOOLS],
        calendar_table=_calendar_table_for_router(),
        today_iso=datetime.now().date().isoformat(),
    )
    slots = (plan or {}).get("slots") or {}
    wants_event = bool(slots.get("date")) or bool(
        plan and plan.get("intent") == "create" and "calendar" in (plan.get("groups") or [])
    )
    # Offline gate: classify deterministically so "band practice friday 4pm"
    # is still an event when no decision model is reachable.
    if not wants_event and _text_looks_like_event(text):
        wants_event = True

    if wants_event:
        title = (slots.get("title") or _clean_event_title(text))[:290]
        date_iso = slots.get("date") or _parse_natural_date(text).isoformat()
        start = (slots.get("start_time") or "").strip()
        if not start:
            m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.IGNORECASE)
            if m:
                hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
                start = f"{hh:02d}:{m.group(2) or '00'}"
        minutes = int(slots.get("duration_minutes") or 60)
        start_dt = datetime.fromisoformat(f"{date_iso}T{start}") if start else datetime.fromisoformat(f"{date_iso}T00:00")
        end_dt = start_dt + timedelta(minutes=minutes) if start else start_dt
        event = CalendarEvent(
            title=title,
            start_at=start_dt.strftime("%Y-%m-%dT%H:%M"),
            end_at=end_dt.strftime("%Y-%m-%dT%H:%M"),
            all_day=not bool(start),
            category=(slots.get("category") or "Personal")[:80],
            color="amber",
            notes=f"Quick capture: {text[:280]}",
            source="Quick Capture",
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return {
            "ok": True,
            "kind": "event",
            "reply": f"Added **{title}** — {date_iso}{(' at ' + start) if start else ' (all day)'}.",
            "event": event.model_dump(mode="json"),
            "engine": (plan or {}).get("engine"),
        }

    # Otherwise it is a task. Split inline due dates when present.
    title = text[:400]
    due = None
    m_due = re.search(r"\b(?:by|due|before)\s+([^,.;]+)$", text, re.IGNORECASE)
    if m_due:
        try:
            due = _parse_natural_date(m_due.group(1)).isoformat()
            title = text[: m_due.start()].strip() or text[:400]
        except Exception:
            due = None
    existing_orders = [int(x) for x in session.exec(select(Task.sort_order)).all() if x is not None]
    task = Task(
        title=title.strip()[:400],
        due_date=due,
        category=str((slots.get("category") or "General"))[:80],
        priority="normal",
        sort_order=(max(existing_orders) + 10) if existing_orders else 10,
        estimated_minutes=int(slots.get("duration_minutes") or 25),
        source_app="Quick Capture",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return {
        "ok": True,
        "kind": "task",
        "reply": f"Added task **{task.title}**" + (f" due {due}." if due else "."),
        "task": task.model_dump(mode="json"),
        "engine": (plan or {}).get("engine"),
    }


# ---------------------------------------------------------------------------
# Browser-extension endpoints (guard-token authenticated, CORS-safe)
#
# The MV3 extension cannot rely on the httpOnly session cookie: it calls from
# an extension origin. Instead it sends `X-Node-Guard-Token` (the `extension`
# scope), which the user copies once from Settings → Browser Extension. That
# token unlocks only `auth.GUARD_SCOPES["extension"]["routes"]`; none of them
# can read arbitrary data, and it cannot reach focus-guard or worker routes.
# ---------------------------------------------------------------------------
@app.get("/api/v1/ext/bootstrap")
async def ext_bootstrap(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Everything the new-tab page needs in one round trip."""
    from .decision_gate import decision_gate
    from .os_settings import os_settings_service

    values = os_settings_service.load_state().get("values") or {}
    tasks = session.exec(select(Task).where(Task.completed == False).order_by(Task.sort_order)).all()  # noqa: E712
    urgent = session.exec(
        select(Notification)
        .where(Notification.read_state == False)  # noqa: E712
        .order_by(Notification.id.desc())
        .limit(60)
    ).all()
    inbox_items = [
        {
            "id": n.id,
            "title": n.title,
            "sender": n.sender,
            "tags_csv": n.tags_csv,
            "category": n.category,
            "priority": n.priority,
            "read_state": n.read_state,
            "is_important": n.is_important,
            "timestamp": n.timestamp.isoformat() if n.timestamp else None,
        }
        for n in urgent
        if n.is_important is not False and str(n.folder or "inbox") != "archive"
    ][:5]
    return {
        "ok": True,
        "mode": values.get("newtab_mode") or "custom",
        "greeting_name": values.get("newtab_greeting_name") or "",
        "gate": await decision_gate.status(),
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "due_date": t.due_date,
                "sort_order": t.sort_order,
            }
            for t in tasks[:8]
        ],
        "urgent": inbox_items,
        "counts": {
            "tasks": len(tasks),
            "unread": len(inbox_items),
        },
    }


EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extension"


def _build_extension_zip() -> bytes:
    """Zip the MV3 extension folder on demand (always matches the running server)."""
    src = EXTENSION_DIR
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="extension/ folder not found on the server")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            zf.write(path, arcname=str(Path("node-webos-extension") / path.relative_to(src)))
    return buf.getvalue()


@app.get("/api/v1/ext/info")
def ext_info() -> Dict[str, Any]:
    """What the extension needs to know, handed to Settings for one-click setup."""
    from .os_settings import os_settings_service

    values = os_settings_service.load_state().get("values") or {}
    version = "unknown"
    manifest_path = EXTENSION_DIR / "manifest.json"
    if manifest_path.is_file():
        try:
            version = json.loads(manifest_path.read_text(encoding="utf-8")).get("version", "unknown")
        except Exception:
            version = "unreadable"
    return {
        "ok": True,
        "installed": manifest_path.is_file(),
        "version": version,
        "zip_url": "/api/v1/ext/package.zip",
        "guard_token": auth_service.get_or_create_guard_token(),
        "newtab_mode": values.get("newtab_mode") or "custom",
        "ext_routes": sorted(GUARD_SCOPES["extension"]["routes"]),
        "bridge_detail": "Cookies are grouped per host and injected into the bridge store, so learned api-anything operations and the server browser reuse your browser login.",
    }


@app.get("/api/v1/ext/package.zip")
def ext_package() -> Response:
    data = _build_extension_zip()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="node-webos-extension.zip"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/v1/ext/quick-capture")
async def ext_quick_capture(text_payload: Dict[str, Any], session: Session = Depends(get_session)) -> Dict[str, Any]:
    return await _quick_capture_impl(text_payload, session)


@app.post("/api/v1/ext/cookies")
async def ext_cookie_import(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extension → server cookie handoff.

    Body: {"cookies": [...], "reason": str, "captured_at": iso}
    Cookies are grouped per host and handed to the bridge's per-app stores so
    learned operations and the server browser reuse the user's real logged-in session.
    """
    from .api_bridge import api_bridge_service

    cookies = payload.get("cookies") or []
    if not isinstance(cookies, list):
        raise HTTPException(status_code=422, detail="cookies must be a list")

    by_slug: Dict[str, list] = {}
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            continue
        host = str(c.get("domain")).lstrip(".").lower()
        slug = _domain_to_slug(host)
        if not slug:
            continue
        c["domain"] = host
        by_slug.setdefault(slug, []).append(c)

    results = {}
    for slug, group in by_slug.items():
        try:
            results[slug] = await api_bridge_service.import_session_cookies(slug, group, device="extension")
        except Exception as exc:  # a single bad host must not fail the sweep
            results[slug] = {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}

    return {
        "ok": any(bool(r.get("ok")) for r in results.values()) or not by_slug,
        "accepted": sum(len(g) for g in by_slug.values()),
        "hosts": {k: len(v) for k, v in by_slug.items()},
        "results": results,
    }


@app.post("/api/v1/ext/session/import")
async def ext_session_import_alias(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Backwards-compatible alias used by earlier extension builds."""
    return await ext_cookie_import(payload)


# ---------------------------------------------------------------------------
# New Tab page config (used by the browser extension)
# ---------------------------------------------------------------------------
@app.get("/newtab", response_class=HTMLResponse)
@app.get("/newtab.html", response_class=HTMLResponse)
def serve_newtab_page() -> HTMLResponse:
    """Standalone minimal new-tab surface used by the browser extension (works even when locked)."""
    page = Path(settings.dashboard_dir) / "newtab.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="newtab.html not found")
    return HTMLResponse(
        content=page.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/v1/newtab/config")
def get_newtab_config() -> Dict[str, Any]:
    from .os_settings import os_settings_service

    values = os_settings_service.load_state().get("values") or {}
    mode = values.get("newtab_mode") or "dashboard"
    return {
        "mode": mode,
        "show_on_open": bool(values.get("newtab_show_on_open", True)),
        "custom_blocks": [b.strip() for b in str(values.get("newtab_custom_blocks_csv") or "").split(",") if b.strip()],
        "wallpaper_id": values.get("newtab_wallpaper_id") or "",
        "greeting_name": values.get("newtab_greeting_name") or "",
        "overlay_strength": int(values.get("newtab_overlay_strength") or 35),
        "modes": ["dashboard", "lock", "wallpaper", "custom", "off"],
        "targets": {
            "dashboard": "/",
            "lock": "/?newtab=lock",
            "wallpaper": "/?newtab=wallpaper",
            "custom": "/newtab",
        },
    }


@app.post("/api/v1/newtab/config")
def set_newtab_config(payload: NewTabSettingRequest) -> Dict[str, Any]:
    from .os_settings import os_settings_service

    if payload.mode not in ("dashboard", "lock", "wallpaper", "custom", "off"):
        raise HTTPException(status_code=400, detail="Invalid new-tab mode")
    payload_state = os_settings_service.update_state(
        settings_patch={
            "newtab_mode": payload.mode,
            "newtab_show_on_open": payload.show_on_open,
            "newtab_custom_blocks_csv": payload.custom_blocks_csv,
            "newtab_wallpaper_id": payload.wallpaper_id,
            "newtab_greeting_name": payload.greeting_name,
            "newtab_overlay_strength": payload.overlay_strength,
        }
    )
    return {"ok": True, "values": (payload_state.get("values") or {}), "config": get_newtab_config()}


# Mount static Lock-Screen shell
_dashboard_dir = Path(settings.dashboard_dir)
if _dashboard_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")
