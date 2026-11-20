import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

REPO_ROOT = Path(__file__).resolve().parents[2]
_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp_dir, 'test_hub.db')}"
os.environ["AI_MEMORY_PATH"] = os.path.join(_tmp_dir, "ai_memory.md")
os.environ["AI_NOTES_PATH"] = os.path.join(_tmp_dir, "ai_notes.md")
os.environ["AI_SHARED_LOG_PATH"] = os.path.join(_tmp_dir, "ai_shared_log.md")
os.environ["AI_PROVIDERS_PATH"] = os.path.join(_tmp_dir, "ai_providers.json")
os.environ["AI_NOTEBOOK_DIR"] = os.path.join(_tmp_dir, "notebook")
os.environ["UPLOADS_DIR"] = os.path.join(_tmp_dir, "uploads")
os.environ["EXTENSIONS_DIR"] = os.path.join(_tmp_dir, "extensions")
os.environ["DESKTOP_WIDGETS_PATH"] = os.path.join(_tmp_dir, "desktop_widgets.json")
os.environ["DOCS_DIR"] = str(REPO_ROOT / "docs")
os.environ["FOCUS_GUARD_PATH"] = os.path.join(_tmp_dir, "focus_guard.json")
os.environ["SCHEDULE_SETTINGS_PATH"] = os.path.join(_tmp_dir, "schedule_settings.json")
os.environ["OS_SETTINGS_PATH"] = os.path.join(_tmp_dir, "os_settings.json")
os.environ["OS_PRESETS_PATH"] = os.path.join(_tmp_dir, "os_presets.json")
os.environ["AUTH_CREDENTIALS_PATH"] = os.path.join(_tmp_dir, "auth_credentials.json")
os.environ["AUTH_GUARD_TOKEN_PATH"] = os.path.join(_tmp_dir, "guard_token.secret")
os.environ["SCRAPER_STATE_PATH"] = os.path.join(_tmp_dir, "nonexistent_state.json")
os.environ["BROWSER_PROFILE_DIR"] = os.path.join(_tmp_dir, "nonexistent_profile")
os.environ["BROWSER_PROFILES_ROOT"] = os.path.join(_tmp_dir, "browser_profiles")
os.environ["IMPORTANCE_LEARNER_PATH"] = os.path.join(_tmp_dir, "importance_learner.json")
os.environ["APPS_CONFIG_PATH"] = os.path.join(_tmp_dir, "apps_config.yaml")
os.environ["NODE_AUTH_ENABLED"] = "true"
os.environ["NODE_AUTH_USERNAME"] = "admin"
os.environ["NODE_AUTH_PASSWORD"] = "test-secret-pass"
os.environ["SEED_DEMO_DATA"] = "false"

Path(os.environ["APPS_CONFIG_PATH"]).write_text(
    """global:
  poll_interval_seconds: 30
apps:
  - name: "ProtonMail"
    enabled: true
    url: "https://mail.proton.me/u/0/inbox"
    indicator_type: "dom_badge"
    zero_knowledge: false
    title_regex: "^\\\\((\\\\d+)\\\\)"
    badge_selectors:
      - '[data-testid="navigation-link:inbox"] .unread-count'
""",
    encoding="utf-8",
)

from app.main import app  # noqa: E402
from app.auth import auth_service  # noqa: E402


# ---------------------------------------------------------------------------
# Real-browser tests. Some tests drive a live Playwright tab (or Brave
# specifically). Without one they would fail or hang, so they skip instead.
# Set NODE_TESTS_REQUIRE_BROWSER=1 to turn those skips into failures (do this
# anywhere a browser is supposed to exist, e.g. the athlon container).
# ---------------------------------------------------------------------------
_BROWSER_PROBE_CACHE = {}


def _probe_chromium() -> str:
    """'' if Playwright can launch headless Chromium, else the reason it can't."""
    if "chromium" not in _BROWSER_PROBE_CACHE:
        code = (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    p.chromium.launch(headless=True).close()\n"
        )
        try:
            res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=90)
            lines = [ln.strip() for ln in res.stderr.splitlines() if ln.strip()]
            useful = [ln for ln in lines if "Error" in ln or "exist" in ln or "not found" in ln] or lines or ["launch failed"]
            reason = "" if res.returncode == 0 else useful[0][:200]
        except Exception as exc:  # noqa: BLE001
            reason = f"{exc.__class__.__name__}: {exc}"
        _BROWSER_PROBE_CACHE["chromium"] = reason
    return _BROWSER_PROBE_CACHE["chromium"]


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("NODE_TESTS_REQUIRE_BROWSER") == "1":
        pytest.fail(f"NODE_TESTS_REQUIRE_BROWSER=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture()
def requires_browser():
    reason = _probe_chromium()
    if reason:
        _skip_or_fail(f"no launchable Playwright Chromium ({reason})")


@pytest.fixture()
def requires_brave(requires_browser):
    from app.browser_stream import resolve_browser_binary

    exe, _label = resolve_browser_binary()
    if not exe or "brave" not in exe.lower():
        _skip_or_fail("Brave is not installed (test asserts the Brave engine)")


def _ics_stamp(days_from_today: int, hhmmss: str) -> str:
    """Relative .ics timestamps: parse_ics_content drops events >14 days old (AGENT.md 7.6)."""
    return (date.today() + timedelta(days=days_from_today)).strftime("%Y%m%d") + "T" + hhmmss


@pytest.fixture()
def unauth_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client():
    with TestClient(app) as c:
        res = c.post("/api/v1/auth/login", json={"username": "admin", "password": "test-secret-pass"})
        assert res.status_code == 200
        yield c


def test_v6_zero_leak_lock_screen_and_server_only_auth(unauth_client: TestClient):
    # 1. Public root serves ONLY Lock Screen — leaks zero app names, models, or OS code
    root = unauth_client.get("/")
    assert root.status_code == 200
    html = root.text
    assert "Session Locked" in html
    for secret_term in ("ProtonMail", "Microsoft Teams", "qwen3.5", "Tailscale", "webosStudio"):
        assert secret_term not in html

    # 2. Old /app.js is gone / blocked, and authenticated shell + APIs return 401 before login
    assert unauth_client.get("/app.js").status_code == 401
    for protected in (
        "/api/v1/auth/shell",
        "/api/v1/auth/app.js",
        "/api/v1/system/status",
        "/api/v1/apps",
        "/api/v1/notifications",
        "/api/v1/tasks",
        "/api/v1/calendar/events",
        "/api/v1/bootstrap",
    ):
        r = unauth_client.get(protected)
        assert r.status_code == 401, f"Expected 401 on {protected}, got {r.status_code}"

    # 3. Unauthenticated /api/v1/health leaks zero system metadata
    hlth = unauth_client.get("/api/v1/health")
    assert hlth.status_code == 200
    assert hlth.json() == {"status": "ok"}

    # 4. Unauthenticated WebSocket stream is rejected with 4401
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with unauth_client.websocket_connect("/api/v1/stream/ws/protonmail"):
            pass
    assert exc_info.value.code == 4401

    # 5. Wrong password fails; no client-side password-change endpoint exists
    bad = unauth_client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    assert unauth_client.post("/api/v1/auth/password", json={"password": "hack"}).status_code in (401, 404, 405)

    # 6. Server-only CLI credential update (PBKDF2-HMAC-SHA256) works and enforces new password
    try:
        saved_path = auth_service.write_server_credentials("max", "server-only-strong-pw")
        assert saved_path.is_file()
        assert unauth_client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-secret-pass"}).status_code == 401
        ok_login = unauth_client.post("/api/v1/auth/login", json={"username": "max", "password": "server-only-strong-pw"})
        assert ok_login.status_code == 200
        assert "node_session" in ok_login.cookies

        # Shell & OS JS now load cleanly after login
        assert unauth_client.get("/api/v1/auth/shell").status_code == 200
        assert "webosStudio" in unauth_client.get("/api/v1/auth/app.js").text

        # Logout revokes session immediately
        unauth_client.post("/api/v1/auth/logout")
        assert unauth_client.get("/api/v1/system/status").status_code == 401
    finally:
        auth_service.write_server_credentials("admin", "test-secret-pass")


def test_v6_clean_empty_start_and_honest_diagnostics(client: TestClient):
    res = client.get("/api/v1/system/status")
    assert res.status_code == 200
    body = res.json()
    from app.config import settings as _settings
    assert body["version"] == _settings.app_version  # one source of truth, not a stale literal
    assert body["scraper_runtime"]["daemon_active"] is False
    assert body["scraper_runtime"]["live_warm_sessions"] == 0
    assert body["unread_notifications"] == 0
    assert body["active_tasks"] == 0
    assert body["scrapers"][0]["status"] == "not_started"
    assert body["scrapers"][0]["unread_count"] == 0

    mem = client.get("/api/v1/ai/memory").json()["content"]
    assert "Robotics" not in mem
    assert "AP Calculus" not in mem


def test_v6_protonmail_readable_non_obfuscation_vs_pgp_fallback(client: TestClient):
    # 1. Readable ProtonMail sender + subject is preserved in full (NOT censored!)
    p_read = client.post(
        "/api/v1/ping",
        json={
            "source_app": "ProtonMail",
            "indicator_type": "dom_badge",
            "unread_count": 1,
            "title": "Dr. Lin — Lab Safety Sign-Off",
            "message": "Please bring signed lab form before Thursday.",
        },
    )
    assert p_read.status_code == 201
    assert p_read.json()["zero_knowledge"] is False
    assert "Dr. Lin — Lab Safety Sign-Off" in p_read.json()["title"]

    notifs = client.get("/api/v1/notifications").json()
    pm_notif = next(n for n in notifs if n["id"] == p_read.json()["notification_id"])
    assert "Dr. Lin" in pm_notif["title"]
    assert "Thursday" in pm_notif["message"]

    # 2. Unintelligible PGP ciphertext / garbled junk falls back cleanly to "ProtonMail ping"
    pgp_blob = "-----BEGIN PGP MESSAGE-----\nwcBMA3X9K2L8fP0AAQf/d8Jk92LmQ1x0vR7z\n-----END PGP MESSAGE-----"
    p_enc = client.post(
        "/api/v1/ping",
        json={
            "source_app": "ProtonMail",
            "indicator_type": "dom_badge",
            "unread_count": 2,
            "title": pgp_blob,
            "message": "A9f8K2mP0xR7qW4zL1vN6bC3yH5tJ8uV0eD2sG4kF9wQ7rT5yU3iO1pA6sD8fG0hJ2kL4==",
        },
    )
    assert p_enc.status_code == 201
    assert p_enc.json()["title"].startswith("ProtonMail ping")
    assert "BEGIN PGP" not in p_enc.json()["title"]

    # 3. Explicit ping_only mode still honors zero_knowledge: True when explicitly requested
    p_zk = client.post(
        "/api/v1/ping",
        json={"source_app": "CustomVault", "indicator_type": "ping_only", "unread_count": 1, "simulated": True},
    )
    assert p_zk.status_code == 201
    assert p_zk.json()["zero_knowledge"] is True


def test_v6_gmail_protonmail_bulk_inbox_and_badge_reconciliation(client: TestClient):
    # ProtonMail currently has 2 unread notifications from previous test -> app badge must be 2
    apps_before = client.get("/api/v1/apps").json()["apps"]
    pm_before = next(a for a in apps_before if a["name"] == "ProtonMail")
    assert pm_before["unread_count"] == 2

    all_ids = [n["id"] for n in client.get("/api/v1/notifications").json()]
    assert len(all_ids) >= 2

    # Bulk mark_read -> unread badges reconcile immediately to 0 (no ghost '1' badge!)
    r_read = client.post("/api/v1/notifications/bulk", json={"ids": all_ids, "action": "mark_read"})
    assert r_read.status_code == 200
    assert r_read.json()["affected"] == len(all_ids)

    apps_mid = client.get("/api/v1/apps").json()["apps"]
    assert next(a for a in apps_mid if a["name"] == "ProtonMail")["unread_count"] == 0
    assert client.get("/api/v1/system/status").json()["unread_notifications"] == 0

    # Bulk mark_unread -> restores unread count
    r_unread = client.post("/api/v1/notifications/bulk", json={"ids": all_ids[:1], "action": "mark_unread"})
    assert r_unread.status_code == 200
    assert client.get("/api/v1/system/status").json()["unread_notifications"] == 1

    # Bulk add_to_tasks -> creates Task(s) and marks notification read
    r_task = client.post("/api/v1/notifications/bulk", json={"ids": all_ids[:1], "action": "add_to_tasks"})
    assert r_task.status_code == 200
    assert r_task.json()["tasks_created"] == 1
    assert client.get("/api/v1/system/status").json()["unread_notifications"] == 0

    # Bulk delete -> removes notifications completely
    r_del = client.post("/api/v1/notifications/bulk", json={"ids": all_ids, "action": "delete"})
    assert r_del.status_code == 200
    assert client.get("/api/v1/notifications").json() == []
    # Clean up task created during bulk test
    for t in client.get("/api/v1/tasks").json():
        client.delete(f"/api/v1/tasks/{t['id']}")


def test_v6_named_ai_chat_contexts_crud(client: TestClient):
    # Create two new named contexts -> default editable names "Chat 1", "Chat 2", ...
    c1 = client.post("/api/v1/ai/threads", json={}).json()
    c2 = client.post("/api/v1/ai/threads", json={}).json()
    assert c1["name"].startswith("Chat ")
    assert c2["name"].startswith("Chat ")
    assert c1["name"] != c2["name"]

    # Rename c1 to a custom topic
    ren = client.patch(f"/api/v1/ai/threads/{c1['thread_id']}", json={"name": "Differential Equations"})
    assert ren.status_code == 200
    assert ren.json()["name"] == "Differential Equations"

    # Send messages in separate contexts and verify strict isolation
    client.post("/api/v1/ai/chat", json={"message": "Add a task: Solve ODE problem 4", "thread_id": c1["thread_id"]})
    client.post("/api/v1/ai/chat", json={"message": "What time is it right now?", "thread_id": c2["thread_id"]})

    m1 = client.get(f"/api/v1/ai/chat?thread_id={c1['thread_id']}").json()
    m2 = client.get(f"/api/v1/ai/chat?thread_id={c2['thread_id']}").json()
    assert any("ODE problem 4" in m["content"] for m in m1)
    assert not any("ODE problem 4" in m["content"] for m in m2)

    # Delete context c2
    del_r = client.delete(f"/api/v1/ai/threads/{c2['thread_id']}")
    assert del_r.status_code == 200
    remaining_ids = {t["thread_id"] for t in del_r.json()["threads"]}
    assert c2["thread_id"] not in remaining_ids
    assert c1["thread_id"] in remaining_ids


def test_calendar_crud_recurring_and_ics_import_export(client: TestClient):
    r = client.post(
        "/api/v1/calendar/events",
        json={
            "title": "Morning Seminar",
            "start_at": "2026-10-26T09:00",
            "end_at": "2026-10-26T10:15",
            "category": "Class",
            "color": "sky",
            "repeat_days": [0, 2, 4],
            "repeat_weeks": 2,
        },
    )
    assert r.status_code == 201
    created = r.json()
    assert len(created) == 6
    assert created[0]["series_id"] is not None

    sample_ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Calendar//EN
BEGIN:VEVENT
UID:event-991@example.edu
DTSTART:{_ics_stamp(5, "153000")}
DTEND:{_ics_stamp(5, "170000")}
SUMMARY:Engineering Design Review
LOCATION:Room 102
DESCRIPTION:Bring prototype notes
END:VEVENT
END:VCALENDAR"""
    imp = client.post(
        "/api/v1/calendar/import-ics",
        json={"ics_content": sample_ics, "source_name": "External ICS", "category": "Work", "color": "violet"},
    )
    assert imp.status_code == 200
    assert imp.json()["added"] == 1

    exp = client.get("/api/v1/calendar/export.ics")
    assert exp.status_code == 200
    assert "BEGIN:VCALENDAR" in exp.text
    assert "Engineering Design Review" in exp.text
    assert "Morning Seminar" in exp.text


def test_long_term_goals_breakdown_and_streak_checkin(client: TestClient):
    bd = client.post(
        "/api/v1/goals/breakdown",
        json={"title": "Master Rust Systems Programming", "category": "Study", "minutes_per_day": 30},
    )
    assert bd.status_code == 200
    assert len(bd.json()["milestones"]) == 4

    g_res = client.post(
        "/api/v1/goals",
        json={"title": "Master Rust Systems Programming", "category": "Study", "minutes_per_day": 30},
    )
    assert g_res.status_code == 201
    goal = g_res.json()
    assert goal["minutes_per_day"] == 30

    mem = client.get("/api/v1/ai/memory").json()["content"]
    assert "Master Rust Systems Programming" in mem

    chk = client.post(
        f"/api/v1/goals/{goal['id']}/checkin",
        json={"minutes_logged": 30, "note": "Ownership & lifetimes exercises"},
    )
    assert chk.status_code == 200
    assert chk.json()["streak_days"] == 1
    assert chk.json()["progress_percent"] > 0


def test_ai_coordinate_day_schedule(client: TestClient):
    target_day = "2026-10-28"
    t = client.post(
        "/api/v1/tasks",
        json={"title": "Finish Systems Lab Report", "due_date": target_day, "category": "Study"},
    )
    assert t.status_code == 201

    coord = client.post(
        "/api/v1/calendar/coordinate",
        json={"target_date": target_day, "include_tasks": True, "include_goals": True},
    )
    assert coord.status_code == 200
    data = coord.json()
    assert data["created_count"] >= 2
    titles = [b["title"] for b in data["created_blocks"]]
    assert any("Systems Lab Report" in x for x in titles)


def test_secretary_chat_calendar_and_goals_commands(client: TestClient):
    c1 = client.post(
        "/api/v1/ai/chat",
        json={"message": "Schedule Team Sync on 2026-10-29 at 4:30pm for 60 minutes"},
    )
    assert c1.status_code == 200
    actions1 = c1.json()["assistant_message"]["actions"]
    assert any(a["type"] == "event_created" for a in actions1)

    c2 = client.post(
        "/api/v1/ai/chat",
        json={"message": "Add a long-term goal: Read 12 Technical Papers, 25 min a day"},
    )
    assert c2.status_code == 200
    actions2 = c2.json()["assistant_message"]["actions"]
    assert any(a["type"] == "goal_created" for a in actions2)


def test_adaptive_duration_learning_and_calendar_recalibration(client: TestClient):
    t_res = client.post(
        "/api/v1/tasks",
        json={
            "title": "Reply to project sign-off thread",
            "source_app": "Microsoft Teams",
            "category": "Work",
            "due_date": "2026-10-30",
        },
    )
    assert t_res.status_code == 201
    task_obj = t_res.json()
    assert task_obj["estimated_minutes"] == 5

    ev_res = client.post(
        "/api/v1/calendar/events",
        json={
            "title": "Work Block: Reply to project sign-off thread",
            "start_at": "2026-10-30T17:00",
            "end_at": "2026-10-30T17:40",
            "category": "Focus Block",
            "color": "amber",
            "source": "AI Secretary",
            "linked_task_id": task_obj["id"],
        },
    )
    assert ev_res.status_code == 201
    ev_id = ev_res.json()[0]["id"]

    chat_res = client.post(
        "/api/v1/ai/chat",
        json={"message": "You scheduled 40 minutes to reply to a Teams chat, I only need 5 minutes!"},
    )
    assert chat_res.status_code == 200
    assert "time_estimates.md" in chat_res.json()["reply_markdown"]

    evs = client.get("/api/v1/calendar/events?start_date=2026-10-30&end_date=2026-10-30").json()
    updated_ev = next(e for e in evs if e["id"] == ev_id)
    assert updated_ev["start_at"] == "2026-10-30T17:00"
    assert updated_ev["end_at"] == "2026-10-30T17:05"


def test_multifile_ai_notebook_and_file_transfer(client: TestClient):
    nb = client.get("/api/v1/ai/notebook")
    assert nb.status_code == 200
    slugs = [f["slug"] for f in nb.json()["files"]]
    assert {"time_estimates", "courses_and_clubs", "goals_and_strategy", "scratchpad"}.issubset(set(slugs))

    todo_md = """# Weekly To-Do
- [ ] Reply to Teams #general thread (due today)
- [ ] Complete problem set 4 (2026-11-02)
- [x] Finished item skipped
"""
    up = client.post(
        "/api/v1/files/upload",
        json={
            "filename": "todos.md",
            "text_content": todo_md,
            "ingest_mode": "extract_tasks",
        },
    )
    assert up.status_code == 201
    assert up.json()["ingest"]["tasks_created"] == 2


def test_youtube_focus_guard_and_guard_token_auth(client: TestClient, unauth_client: TestClient):
    # Verify companion extension & userscript embed X-Node-Guard-Token so background heartbeats pass auth gate!
    us = client.get("/api/v1/focus-guard/youtube-guard.user.js", headers={"X-Forwarded-Host": "100.84.12.9:8080"})
    assert us.status_code == 200
    assert "X-Node-Guard-Token" in us.text

    guard_tok = auth_service.get_guard_token("focus_guard")
    # The download embeds the focus-guard token, never the extension token.
    assert guard_tok in us.text
    assert auth_service.get_or_create_guard_token() not in us.text
    # Pin bedtime far into the future: this test asserts *trigger timing*, and the
    # "past bedtime" branch legitimately forces a nudge, which made the assertions
    # time-of-day dependent (any run after 22:00 local failed).
    import json as _json_fg
    import os as _os
    from pathlib import Path as _Path
    _fg_path = _Path(_os.environ["SCHEDULE_SETTINGS_PATH"])
    _fg = _json_fg.loads(_fg_path.read_text(encoding="utf-8")) if _fg_path.is_file() else {}
    _fg["bedtime_hhmm"] = "23:59"
    _fg["enabled"] = True
    _fg_path.write_text(_json_fg.dumps(_fg), encoding="utf-8")

    # 1) <= 30m video mid-playback should NOT nudge yet (analyzes in background first)
    hb_mid = unauth_client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "video_title": "12-Minute Entertainment Clip",
            "url": "https://www.youtube.com/watch?v=shortmid",
            "playing": True,
            "duration_seconds": 720,
            "current_time_seconds": 180,
            "video_ended": False,
            "override_session_minutes": 3,
            "clear_allowance": True,
        },
    )
    assert hb_mid.status_code == 200
    assert hb_mid.json()["session"]["should_nudge"] is False
    assert hb_mid.json()["session"]["ai_video_analysis"] is not None

    # 2) <= 30m video at the END of the video triggers accountability check!
    hb_end = unauth_client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "video_title": "12-Minute Entertainment Clip",
            "url": "https://www.youtube.com/watch?v=shortmid",
            "playing": True,
            "duration_seconds": 720,
            "current_time_seconds": 719,
            "video_ended": True,
            "override_session_minutes": 12,
        },
    )
    assert hb_end.status_code == 200
    assert hb_end.json()["session"]["should_nudge"] is True
    assert hb_end.json()["session"]["prompt_mode"] == "require_reasoning"

    # 3) Weak reasoning is rejected and triggers force_close!
    just_bad = unauth_client.post(
        "/api/v1/focus-guard/justify",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "reasoning": "idk im bored",
            "video_title": "12-Minute Entertainment Clip",
            "url": "https://www.youtube.com/watch?v=shortmid",
            "duration_seconds": 720,
        },
    )
    assert just_bad.status_code == 200
    assert just_bad.json()["justification_result"]["approved"] is False
    assert just_bad.json()["session"]["force_close"] is True

    # 4) > 30m video triggers at 10 minutes watched, and concrete study justification is approved!
    hb_long = unauth_client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "video_title": "MIT 18.06 Linear Algebra Lecture 14",
            "channel": "MIT OpenCourseWare",
            "url": "https://www.youtube.com/watch?v=longlec",
            "playing": True,
            "duration_seconds": 3000,
            "current_time_seconds": 605,
            "video_ended": False,
            "override_session_minutes": 10,
            "override_today_minutes": 12,
            "clear_allowance": True,
        },
    )
    assert hb_long.status_code == 200
    assert hb_long.json()["session"]["should_nudge"] is True

    just_ok = unauth_client.post(
        "/api/v1/focus-guard/justify",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "reasoning": "Watching the MIT Linear Algebra lecture on orthogonal projections required to solve problem 4 on tonight's homework.",
            "video_title": "MIT 18.06 Linear Algebra Lecture 14",
            "channel": "MIT OpenCourseWare",
            "url": "https://www.youtube.com/watch?v=longlec",
            "duration_seconds": 3000,
        },
    )
    assert just_ok.status_code == 200
    assert just_ok.json()["justification_result"]["approved"] is True
    assert just_ok.json()["session"]["force_close"] is False

    # 5) Daily hard cap exhausted gives NO choice to continue (hard_block + force_close)
    hb_cap = unauth_client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "video_title": "Random Funny Clips",
            "url": "https://www.youtube.com/watch?v=overcap",
            "playing": True,
            "duration_seconds": 600,
            "current_time_seconds": 60,
            "override_session_minutes": 50,
            "override_today_minutes": 60,
            "clear_allowance": True,
        },
    )
    assert hb_cap.status_code == 200
    assert hb_cap.json()["session"]["force_close"] is True
    assert hb_cap.json()["session"]["prompt_mode"] == "hard_block"

    # Invalid guard token is rejected with 401
    hb_bad = unauth_client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": "invalid-token"},
        json={"video_title": "Sample", "playing": True},
    )
    assert hb_bad.status_code == 401


def test_v7_hourly_ai_cron_and_bedtime_priority_engine(client: TestClient, unauth_client: TestClient):
    # Built-in lock screen wallpaper is publicly accessible without leaking API data
    wp = unauth_client.get("/wallpapers/emerald-fjord.jpg")
    assert wp.status_code == 200
    assert unauth_client.get("/api/v1/cron/status").status_code == 401

    from datetime import date, timedelta
    today_iso = date.today().isoformat()
    next_week_iso = (date.today() + timedelta(days=5)).isoformat()

    # Set bedtime to 23:30 so evening window exists regardless of current test clock
    st = client.patch("/api/v1/cron/settings", json={"bedtime_hhmm": "23:30", "enabled": True})
    assert st.status_code == 200
    assert st.json()["settings"]["bedtime_hhmm"] == "23:30"

    # Add a busy block until 19:00 ("School + Robotics Club")
    client.post(
        "/api/v1/calendar/events",
        json={
            "title": "School & Extracurriculars",
            "start_at": f"{today_iso}T08:00",
            "end_at": f"{today_iso}T19:00",
            "category": "Personal",
            "color": "cyan",
        },
    )

    # Add a due-tonight homework task + a next-week lower-priority task
    t_tonight = client.post(
        "/api/v1/tasks",
        json={"title": "AP Physics Problem Set Due Tonight", "due_date": today_iso, "category": "Study", "estimated_minutes": 45},
    ).json()
    t_later = client.post(
        "/api/v1/tasks",
        json={"title": "Read Optional History Chapter", "due_date": next_week_iso, "category": "Study", "estimated_minutes": 40},
    ).json()

    run_res = client.post("/api/v1/cron/run", json={"trigger": "manual"})
    assert run_res.status_code == 200
    body = run_res.json()
    assert body["last_run"] is not None
    assert body["last_run"]["trigger"] == "manual"
    assert "budget" in body

    tasks_after = client.get("/api/v1/tasks").json()
    tonight_updated = next(t for t in tasks_after if t["id"] == t_tonight["id"])
    later_updated = next(t for t in tasks_after if t["id"] == t_later["id"])
    assert tonight_updated["priority"] == "critical"


def test_v6_real_ai_calendar_edit_delete_and_apps_assist(client: TestClient):
    create_r = client.post(
        "/api/v1/ai/chat",
        json={"message": "Schedule Signal Processing Study on 2026-11-04 at 3:00pm for 60 minutes"},
    )
    assert create_r.status_code == 200
    evs_before = client.get("/api/v1/calendar/events?start_date=2026-11-04&end_date=2026-11-04").json()
    ev = next(e for e in evs_before if "Signal Processing" in e["title"])

    move_r = client.post(
        "/api/v1/ai/chat",
        json={"message": "Move Signal Processing Study to 6:30pm for 45 minutes"},
    )
    assert move_r.status_code == 200
    evs_after = client.get("/api/v1/calendar/events?start_date=2026-11-04&end_date=2026-11-04").json()
    ev_moved = next(e for e in evs_after if e["id"] == ev["id"])
    assert ev_moved["start_at"] == "2026-11-04T18:30"

    del_r = client.post(
        "/api/v1/ai/chat",
        json={"message": "Delete Signal Processing Study from my calendar"},
    )
    assert del_r.status_code == 200
    assert any(a["type"] == "event_deleted" for a in del_r.json()["assistant_message"]["actions"])

    gm = client.post("/api/v1/apps/ai-assist", json={"query": "GroupMe", "zero_knowledge": False})
    assert gm.status_code == 200
    assert gm.json()["config"]["name"] == "GroupMe"


@pytest.mark.usefixtures("requires_brave")
def test_v6_server_browser_stream_websocket_and_ai_calibrate(client: TestClient):
    add_r = client.post(
        "/api/v1/apps",
        json={
            "name": "Local Test Portal",
            "url": "data:text/html,<html><head><title>(4) Portal</title></head><body><h1>Portal</h1><span class='unread-badge'>4</span></body></html>",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
        },
    )
    assert add_r.status_code == 201
    slug = add_r.json()["app"]["slug"]

    with client.websocket_connect(f"/api/v1/stream/ws/{slug}") as ws:
        got_status = False
        got_binary_jpeg = False
        for _ in range(5):
            pkt = ws.receive()
            if pkt.get("bytes"):
                raw = pkt["bytes"]
                assert raw[:3] == b"\xff\xd8\xff"  # Valid raw JPEG magic header!
                got_binary_jpeg = True
            elif pkt.get("text"):
                import json as _json
                meta = _json.loads(pkt["text"])
                if meta.get("type") == "status":
                    got_status = True
            if got_status and got_binary_jpeg:
                break
        assert got_binary_jpeg is True

    tabs = client.get("/api/v1/stream/tabs").json()
    assert "engine" in tabs
    assert tabs["engine"]["zero_google_telemetry"] is True
    assert tabs["engine"]["is_brave"] is True

    cal = client.post(f"/api/v1/apps/{slug}/ai-calibrate")
    assert cal.status_code == 200
    assert cal.json()["telemetry"]["live_poll"]["unread_count"] == 4

    cl = client.delete(f"/api/v1/stream/tabs/{slug}")
    assert cl.status_code == 200


def test_v71_comprehensive_settings_presets_ai_code_assist_and_core_safety(client: TestClient):
    # 1. Load initial comprehensive settings schema, presets, and AI code map
    st = client.get("/api/v1/settings")
    assert st.status_code == 200
    body = st.json()
    assert len(body["schema"]) >= 25
    assert len(body["presets"]) >= 4
    assert "css_tokens_and_selectors" in body["code_map"]
    assert "js_sandbox_api" in body["code_map"]

    # 2. Patch dock_auto_hide + bedtime_hhmm -> verify two-way sync to cron_scheduler
    patched = client.patch(
        "/api/v1/settings",
        json={"settings_patch": {"dock_auto_hide": True, "tile_gap": 12, "bedtime_hhmm": "22:45"}},
    )
    assert patched.status_code == 200
    assert patched.json()["values"]["dock_auto_hide"] is True
    assert patched.json()["values"]["tile_gap"] == 12
    cron_st = client.get("/api/v1/cron/status").json()
    assert cron_st["settings"]["bedtime_hhmm"] == "22:45"

    # 3. Ask AI a pure question ("How do I hide the taskbar?") -> guides without overwriting
    q_res = client.post(
        "/api/v1/settings/ai-assist",
        json={"prompt": "How do I hide the taskbar?", "save_as_preset": True},
    )
    assert q_res.status_code == 200
    assert q_res.json()["assist"]["is_question_only"] is True
    assert q_res.json()["assist"]["target_tab"] == "wm"

    # 4. Ask AI to directly tweak settings + custom CSS + custom JS -> auto-saved into a Preset!
    ai_mod = client.post(
        "/api/v1/settings/ai-assist",
        json={
            "prompt": "Auto-hide the taskbar, change accent to violet #a855f7, add neon glow to focused windows, and add shortcut Alt+C to open Calendar",
            "save_as_preset": True,
            "preset_name": "Violet Study Mode",
        },
    )
    assert ai_mod.status_code == 200
    assist = ai_mod.json()["assist"]
    assert assist["has_changes"] is True
    assert assist["saved_preset"] is not None
    saved_pid = assist["saved_preset"]["id"]
    state_after = ai_mod.json()["state"]
    assert state_after["values"]["accent_color"] == "#a855f7"
    assert state_after["values"]["dock_auto_hide"] is True
    assert ".node-win.is-focused" in state_after["custom_css"]
    assert "api.registerShortcut" in state_after["custom_js"]
    assert state_after["active_preset_id"] == saved_pid

    # 5. Reset to safe default, then reload & further edit the saved preset!
    rst = client.post("/api/v1/settings/reset")
    assert rst.status_code == 200
    assert rst.json()["active_preset_id"] == "default-mint"
    assert rst.json()["values"]["dock_auto_hide"] is False

    reloaded = client.post(f"/api/v1/settings/presets/{saved_pid}/load")
    assert reloaded.status_code == 200
    assert reloaded.json()["values"]["accent_color"] == "#a855f7"

    edited = client.post(
        "/api/v1/settings/presets",
        json={
            "preset_id": saved_pid,
            "name": "Violet Study Mode v2",
            "description": "Updated gap to 4px",
            "settings_patch": {"accent_color": "#a855f7", "dock_auto_hide": True, "tile_gap": 4},
            "custom_css": state_after["custom_css"],
            "custom_js": state_after["custom_js"],
            "activate": True,
        },
    )
    assert edited.status_code == 201
    assert edited.json()["state"]["values"]["tile_gap"] == 4

    # 6. Core Safety Guard blocks unsafe JS / CSS that tries to touch auth/cookies/external URLs
    bad_js = client.patch(
        "/api/v1/settings",
        json={"custom_js": "fetch('/api/v1/auth/logout'); console.log(document.cookie);"},
    )
    assert bad_js.status_code == 400

    bad_css = client.patch(
        "/api/v1/settings",
        json={"custom_css": "@import url('https://evil.example/leak.css');"},
    )
    assert bad_css.status_code == 400


@pytest.mark.usefixtures("requires_browser")
def test_v81_initial_unreads_live_ws_pings_and_ap_classroom_assignments(client: TestClient):
    import json as _json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    # Spin up a tiny local HTTP server simulating:
    #  1) An SPA mail/chat app that hydrates asynchronously via fetch('/api/inbox.json') + delayed DOM render
    #  2) An AP Classroom portal with 2 active assignments ('Assigned', 'In Progress') + 1 'Completed' assignment
    class _MockPortalHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/spa-mail":
                html = """<!doctype html><html><head><title>WebMail</title></head><body>
                <div id="root">Loading SPA...</div>
                <script>
                  setTimeout(async () => {
                    const r = await fetch('/api/inbox.json');
                    const data = await r.json();
                    const root = document.getElementById('root');
                    root.innerHTML = data.conversations.map(c =>
                      `<div class="item-container ${c.Unread ? 'unread' : 'read'}">
                         <div class="sender">${c.Sender.Name}</div>
                         <div class="subject">${c.Subject}</div>
                       </div>`
                    ).join('');
                  }, 350);
                </script></body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            elif self.path == "/api/inbox.json":
                payload = {
                    "conversations": [
                        {"ID": "1", "Unread": 1, "Sender": {"Name": "Alice Chen"}, "Subject": "Lab Partner Notes"},
                        {"ID": "2", "Unread": 1, "Sender": {"Name": "Prof Miller"}, "Subject": "Exam Room Changed"},
                        {"ID": "3", "Unread": 0, "Sender": {"Name": "Campus Digest"}, "Subject": "Old Read Newsletter"},
                    ]
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(_json.dumps(payload).encode("utf-8"))
            elif self.path == "/ap-classroom":
                html = """<!doctype html><html><head><title>AP Classroom</title></head><body>
                <main id="assignments-list">
                  <article class="cb-assignment-card" data-testid="assignment-card-1">
                    <h3>Unit 4 Progress Check: MCQ</h3>
                    <span>AP Calculus BC</span>
                    <span>Assigned</span>
                    <span>Due Oct 27, 11:59 PM</span>
                  </article>
                  <article class="cb-assignment-card" data-testid="assignment-card-2">
                    <h3>Unit 4 Progress Check: FRQ</h3>
                    <span>AP Calculus BC</span>
                    <span>In Progress</span>
                    <span>Due Oct 28, 11:59 PM</span>
                  </article>
                  <article class="cb-assignment-card" data-testid="assignment-card-3">
                    <h3>Unit 3 Progress Check: MCQ</h3>
                    <span>AP Calculus BC</span>
                    <span>Completed</span>
                    <span>Scored 18/18</span>
                  </article>
                </main>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), _MockPortalHandler)
    port = httpd.server_port
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    try:
        # --- A. Test delayed SPA hydration + JSON interception of ALREADY-UNREAD messages ---
        r_mail = client.post(
            "/api/v1/apps",
            json={
                "name": "SPA Mail Test",
                "url": f"http://127.0.0.1:{port}/spa-mail",
                "indicator_type": "dom_badge",
                "zero_knowledge": False,
            },
        )
        assert r_mail.status_code == 201
        mail_slug = r_mail.json()["app"]["slug"]

        client.post(f"/api/v1/stream/tabs/{mail_slug}/open")
        time.sleep(2.1)  # Allow 350ms SPA hydration + 1.4s staggered settle scan

        notifs = client.get("/api/v1/notifications?limit=50").json()
        mail_notifs = [n for n in notifs if n["source_app"] == "SPA Mail Test"]
        mail_titles = " | ".join(n["title"] for n in mail_notifs)
        assert "Lab Partner Notes" in mail_titles
        assert "Exam Room Changed" in mail_titles
        assert "Old Read Newsletter" not in mail_titles
        count_before_repoll = len(mail_notifs)

        # Re-scanning the exact same page must emit ZERO duplicates!
        cal_again = client.post(f"/api/v1/apps/{mail_slug}/ai-calibrate")
        assert cal_again.status_code == 200
        notifs_after = [
            n for n in client.get("/api/v1/notifications?limit=50").json() if n["source_app"] == "SPA Mail Test"
        ]
        assert len(notifs_after) == count_before_repoll

        # --- B. Test AP Classroom auto-assist + already-active assignments on load + newly posted assignment ---
        ap_assist = client.post("/api/v1/apps/ai-assist", json={"query": "AP Classroom"})
        assert ap_assist.status_code == 200
        assert ap_assist.json()["config"]["indicator_type"] == "assignment_tracker"

        r_ap = client.post(
            "/api/v1/apps",
            json={
                "name": "AP Classroom",
                "url": f"http://127.0.0.1:{port}/ap-classroom",
                "indicator_type": "assignment_tracker",
                "zero_knowledge": False,
            },
        )
        assert r_ap.status_code == 201
        ap_slug = r_ap.json()["app"]["slug"]

        client.post(f"/api/v1/stream/tabs/{ap_slug}/open")
        time.sleep(0.5)

        ap_notifs = [
            n for n in client.get("/api/v1/notifications?limit=50").json() if n["source_app"] == "AP Classroom"
        ]
        ap_titles = [n["title"] for n in ap_notifs]
        assert any("Unit 4 Progress Check: MCQ" in t for t in ap_titles)
        assert any("Unit 4 Progress Check: FRQ" in t for t in ap_titles)
        assert not any("Unit 3 Progress Check: MCQ" in t for t in ap_titles)  # Completed ignored!
        assert len(ap_notifs) == 2

        # Clean up test tabs & apps
        client.delete(f"/api/v1/stream/tabs/{mail_slug}")
        client.delete(f"/api/v1/stream/tabs/{ap_slug}")
        client.delete(f"/api/v1/apps/{mail_slug}")
        client.delete(f"/api/v1/apps/{ap_slug}")
    finally:
        httpd.shutdown()


@pytest.mark.usefixtures("requires_browser")
def test_v82_importance_learning_multi_account_profiles_grades_and_live_enrich(client: TestClient):
    """
    Verifies all 6 Node v8.2 upgrades end-to-end:
    1. Rich 1-line summary + live server-tab enrichment (`/api/v1/notifications/{id}/enrich`)
    2. Multiple isolated accounts for the same service (`/api/v1/apps/{slug}/duplicate` + separate cookie profiles)
    3. AI Importance Filter (`is_important` promo filtering + user feedback adaptive learning)
    4. PowerSchool Grades DOM extraction, silent baseline, cron diffing, and grade-change pings
    5. Decluttered OS Topbar + Desktop "Next Up" + Consistent Minimalist SVG Icons + Branching Node Logo
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    import time

    grade_state = {"calc_score": "88", "calc_letter": "B+"}

    class _MockV82Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/powerschool/guardian/home.html"):
                html = f"""<!doctype html><html><head><title>Grades and Attendance</title></head><body>
                <table>
                  <thead><tr><th>Exp</th><th>Course</th><th>Q1</th></tr></thead>
                  <tbody>
                    <tr>
                      <td>1(A)</td>
                      <td>AP Calculus BC<br/>Dr. Taylor</td>
                      <td><a href="scores.html?frn=001">{grade_state['calc_letter']}<br/>{grade_state['calc_score']}</a></td>
                    </tr>
                    <tr>
                      <td>2(A)</td>
                      <td>AP Physics C<br/>Ms. Patel</td>
                      <td><a href="scores.html?frn=002">A<br/>95</a></td>
                    </tr>
                  </tbody>
                </table>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            elif self.path.startswith("/teams-live"):
                html = """<!doctype html><html><head><title>(1) Microsoft Teams</title></head><body>
                <div role="treeitem" aria-label="Unread message from Sarah Lin: Can you review the lab slides before 4pm today?">
                  <span>Sarah Lin</span>
                </div>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            elif self.path.startswith("/acct"):
                # Echo/set cookie to verify zero cross-account cookie leakage!
                cookie_hdr = self.headers.get("Cookie") or ""
                if "set=1" in self.path:
                    who = self.path.split("who=")[-1]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Set-Cookie", f"acct_session={who}; Path=/")
                    self.end_headers()
                    self.wfile.write(f"<html><body>Signed in as {who}</body></html>".encode("utf-8"))
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(f"<html><body>Cookie={cookie_hdr}</body></html>".encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), _MockV82Handler)
    port = httpd.server_port
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    try:
        # --- 1. OS Shell UI Audit: Branching Logo, Decluttered Topbar, Next Up, Important Tab, Grades ---
        fav_ico = client.get("/favicon.ico")
        assert fav_ico.status_code == 200
        fav_svg = client.get("/favicon.svg")
        assert fav_svg.status_code == 200
        assert "<circle" in fav_svg.text and "<path" in fav_svg.text
        fav_png = client.get("/favicon.png")
        assert fav_png.status_code == 200

        shell_html = client.get("/api/v1/auth/shell").text
        header_slice = shell_html.split('<header')[1].split('</header>')[0]
        assert "nodeSvg('node-logo'" in header_slice
        assert "toggleWmMode()" not in header_slice  # Tiled/Floating button removed from OS topbar!
        assert "openSettings(" not in header_slice   # Settings button removed from OS topbar!
        assert "Bed " not in header_slice            # Bedtime button removed from OS topbar!
        assert "desktopNextUpItems" in shell_html    # Next Up right next to Desktop clock!
        # v10.2: Inbox is tag-first with a Gmail-style query bar and conversation list
        assert "inboxQuery" in shell_html
        assert "inboxGroups" in shell_html
        assert "inboxTagFilterPills" in shell_html
        assert 'placeholder="tag:school is:unread from:prof@school.edu"' in shell_html
        assert "win.view === 'grades'" in shell_html
        # v10.2: Grades exposes course groups + persistent visibility controls
        assert "gradesShowHidden" in shell_html
        assert "activeGradeTab" in shell_html

        # --- 2. AI Importance Filter + Adaptive User Learning ---
        promo_ping = client.post(
            "/api/v1/ping",
            json={
                "source_app": "ProtonMail",
                "title": "MegaStore Deals — 50% OFF Flash Sale Limited Time",
                "message": "Shop now! Unsubscribe at the bottom of this promotional newsletter.",
                "unread_count": 1,
            },
        )
        assert promo_ping.status_code == 201
        promo_id = promo_ping.json()["notification_id"]

        urgent_ping = client.post(
            "/api/v1/ping",
            json={
                "source_app": "ProtonMail",
                "title": "Prof. Alvarez — Midterm Exam Room Update",
                "message": "Please bring your calculator; exam starts at 9:00 AM tomorrow.",
                "unread_count": 1,
            },
        )
        assert urgent_ping.status_code == 201
        urgent_id = urgent_ping.json()["notification_id"]

        imp_only = client.get("/api/v1/notifications?important_only=true&limit=50").json()
        imp_ids = {n["id"] for n in imp_only}
        assert urgent_id in imp_ids
        assert promo_id not in imp_ids  # Promo automatically filtered out of Important tab!

        # Test adaptive learning: user marks a custom newsletter sender as Not Important -> future pings from same sender auto-mute!
        custom_n1 = client.post(
            "/api/v1/ping",
            json={
                "source_app": "ProtonMail",
                "title": "Club Digest — Weekly Campus Recap",
                "message": "Here are the club booths on Tuesday afternoon.",
                "unread_count": 1,
            },
        ).json()["notification_id"]
        fb = client.post(f"/api/v1/notifications/{custom_n1}/importance", json={"is_important": False})
        assert fb.status_code == 200
        assert fb.json()["notification"]["is_important"] is False

        custom_n2 = client.post(
            "/api/v1/ping",
            json={
                "source_app": "ProtonMail",
                "title": "Club Digest — Friday Social",
                "message": "Snacks in the quad at 4pm.",
                "unread_count": 1,
            },
        ).json()["notification_id"]
        imp_after = {n["id"] for n in client.get("/api/v1/notifications?important_only=true&limit=50").json()}
        assert custom_n2 not in imp_after  # Learned sender preference applied automatically!

        # --- 3. Multi-Account Isolated Browser Profiles (e.g. 2 ProtonMail Accounts) ---
        dup_res = client.post(
            "/api/v1/apps/protonmail/duplicate",
            json={"label": "ProtonMail (School)", "account_profile": "protonmail-school"},
        )
        assert dup_res.status_code == 201
        dup_app = dup_res.json()["app"]
        assert dup_app["name"] == "ProtonMail (School)"
        assert dup_app["account_profile"] == "protonmail-school"

        # Open two live tabs on different account_profiles and prove cookies DO NOT leak across them!
        a1 = client.post(
            "/api/v1/apps",
            json={
                "name": "Mail Acct 1",
                "url": f"http://127.0.0.1:{port}/acct?set=1&who=alice",
                "account_profile": "acct-alice",
            },
        ).json()["app"]["slug"]
        a2 = client.post(
            "/api/v1/apps",
            json={
                "name": "Mail Acct 2",
                "url": f"http://127.0.0.1:{port}/acct",
                "account_profile": "acct-bob",
            },
        ).json()["app"]["slug"]

        client.post(f"/api/v1/stream/tabs/{a1}/open")
        client.post(f"/api/v1/stream/tabs/{a2}/open")
        tabs_info = client.get("/api/v1/stream/tabs").json()
        active_profiles = tabs_info["engine"]["active_profiles"]
        assert "acct-alice" in active_profiles and "acct-bob" in active_profiles

        # --- 4. Live Tab Enrichment (`Teams ping` -> Rich 1-Line Summary!) ---
        t_app = client.post(
            "/api/v1/apps",
            json={
                "name": "Teams Live",
                "url": f"http://127.0.0.1:{port}/teams-live",
                "indicator_type": "dom_badge",
            },
        ).json()["app"]["slug"]
        client.post(f"/api/v1/stream/tabs/{t_app}/open")
        time.sleep(0.4)

        bare_ping_id = client.post(
            "/api/v1/notifications",
            json={
                "source_app": "Teams Live",
                "title": "Teams Live ping",
                "message": "New activity detected (1).",
                "indicator_type": "dom_badge",
            },
        ).json()["id"]
        enriched = client.post(f"/api/v1/notifications/{bare_ping_id}/enrich")
        assert enriched.status_code == 200
        enr_body = enriched.json()
        assert "Sarah Lin" in enr_body["title"] or "Sarah Lin" in (enr_body["ai_summary"] or "")
        assert "lab slides" in (enr_body["ai_summary"] or "").lower() or "lab slides" in enr_body["message"].lower()

        # --- 5. PowerSchool Grades Scan -> Baseline -> Cron Grade Change Detection & Ping! ---
        ps_app = client.post(
            "/api/v1/apps",
            json={
                "name": "PowerSchool",
                "url": f"http://127.0.0.1:{port}/powerschool/guardian/home.html",
                "indicator_type": "grades_tracker",
            },
        ).json()["app"]["slug"]

        scan1 = client.post(f"/api/v1/grades/scan?slug={ps_app}")
        assert scan1.status_code == 200
        g1 = scan1.json()
        assert g1["summary"]["total_courses"] == 2
        assert g1["scan"]["changes_detected"] == []  # First baseline scan does NOT spam pings!

        # Update AP Calculus BC grade on PowerSchool from B+ (88%) -> A- (92%) and run Cron check!
        grade_state["calc_score"] = "92"
        grade_state["calc_letter"] = "A-"

        cron_res = client.post("/api/v1/cron/run", json={})
        assert cron_res.status_code == 200

        g2 = client.get("/api/v1/grades").json()
        calc_row = next(c for c in g2["courses"] if "Calculus" in c["course_name"])
        assert calc_row["letter_grade"] == "A-"
        assert calc_row["score_percent"] == 92.0
        assert calc_row["previous_score_percent"] == 88.0
        assert len(g2["recent_changes"]) >= 1

        # Verify a high-signal grade_change ping was emitted into Inbox!
        grade_pings = [
            n for n in client.get("/api/v1/notifications?important_only=true&limit=50").json()
            if n["indicator_type"] == "grade_change"
        ]
        assert any("AP Calculus BC" in n["title"] and "92" in n["title"] for n in grade_pings)

        # Cleanup tabs
        for sl in (a1, a2, t_app, ps_app):
            client.delete(f"/api/v1/stream/tabs/{sl}")
            client.delete(f"/api/v1/apps/{sl}")
        client.delete(f"/api/v1/apps/{dup_app['slug']}")
    finally:
        httpd.shutdown()


def test_v90_streaming_full_tools_inbox_tasks_monitors_and_widgets(client: TestClient, tmp_path):
    """
    Comprehensive Node v9.0 verification:
    - Real-time NDJSON streaming AI chat + full-OS tool execution (calendar create/edit/delete, tasks, inbox, settings, monitors)
    - Multi-model status endpoint (`qwen3.5:2b` + `qwen3.5:0.8b` + `Von`)
    - ProtonMail `.sr-only` accessibility artifact scrubber & zero redundant `<AppName>:` prefix
    - Tasks priority normalization (int + str), priority sorting, manual reorder, deduplicated AI note extraction, and Schoology `.ics` + Inbox sync
    - Adaptive Daily Briefing workload accounting (40 important notifications vs 2)
    - Docker single-file bind-mount safe `apps_manager._write_raw()` fallback (`EBUSY` resilience)
    - UI shell checks: Start Menu, Dedicated Grades Donut Widget, no `+ PowerSchool` in Monitors, separated 24/7 Cron vs Cutoff
    """
    import json as _json
    from unittest.mock import patch
    from sqlmodel import Session
    from app.database import engine
    from app.apps_manager import AppsConfigManager
    from app.model_router import scrub_notification_artifacts
    from app.ollama_client import compute_briefing_workload
    from app.models import Notification
    from app.secretary import execute_secretary_tool

    # 1. Multi-model status endpoint
    ms = client.get("/api/v1/models/status")
    assert ms.status_code == 200
    ms_data = ms.json()
    assert "chat_model" in ms_data and "decision_model" in ms_data

    # 2. ProtonMail accessibility artifact scrubbing + no redundant "ProtonMail:" prefix
    raw_garbled = (
        "ProtonMail: SStar conversationUnread emailstella.rua, "
        "Your Friend in FaithToday is the Feast of St. Mark!Apr 25Dear Friend, Today we celebrate..."
    )
    scrubbed = scrub_notification_artifacts("ProtonMail", raw_garbled, "")
    assert not scrubbed["title"].lower().startswith("protonmail:")
    assert "SStar" not in scrubbed["title"]
    assert "Unread email" not in scrubbed["title"]
    assert "stella.rua" in scrubbed["title"]
    assert "Feast of St. Mark" in scrubbed["title"] or "Feast of St. Mark" in scrubbed["preview"]

    n_res = client.post(
        "/api/v1/notifications",
        json={
            "source_app": "ProtonMail",
            "title": raw_garbled,
            "message": "",
            "indicator_type": "dom_badge",
        },
    )
    assert n_res.status_code == 201
    n_created = n_res.json()
    assert not n_created["title"].lower().startswith("protonmail:")
    assert "SStar" not in n_created["title"]

    # 3. Streaming AI Chat (`POST /api/v1/ai/chat/stream`) + Real Calendar Mutation
    with client.stream(
        "POST",
        "/api/v1/ai/chat/stream",
        json={
            "message": "Schedule Physics Lab Review tomorrow at 4pm for 1h",
            "thread_id": "main",
            "enable_thinking": True,
        },
    ) as sresp:
        assert sresp.status_code == 200
        events = [_json.loads(line) for line in sresp.iter_lines() if line.strip()]
    event_types = [e.get("type") for e in events]
    assert "meta" in event_types
    assert "done" in event_types
    done_ev = next(e for e in events if e.get("type") == "done")
    assert "Physics Lab Review" in done_ev["assistant_message"]["content"]

    all_cal = client.get("/api/v1/calendar/events").json()
    created_ev = next((ev for ev in all_cal if "Physics Lab Review" in ev["title"]), None)
    assert created_ev is not None

    # 4. Direct Full-OS Secretary Tools Execution (update event, delete event, OS overview, settings)
    import asyncio as _asyncio
    from app.secretary import execute_secretary_tool_async

    with Session(engine) as db:
        acts: list = []
        upd_res = execute_secretary_tool(
            "update_calendar_event",
            {"event_id": created_ev["id"], "new_title": "AP Physics Lab Review Updated"},
            db,
            acts,
        )
        assert upd_res["status"] == "updated"
        assert upd_res["event"]["title"] == "AP Physics Lab Review Updated"

        ov_res = _asyncio.run(execute_secretary_tool_async("get_os_overview", {}, db, acts))
        assert "workload" in ov_res and "models" in ov_res

        st_acts: list = []
        st_res = execute_secretary_tool(
            "update_os_setting",
            {"key": "sleep_buffer_minutes", "value": 20},
            db,
            st_acts,
        )
        assert st_res["status"] == "updated"
        assert any(a.get("type") == "os_settings_updated" for a in st_acts)

        del_res = execute_secretary_tool(
            "delete_calendar_event",
            {"event_id": created_ev["id"]},
            db,
            acts,
        )
        assert del_res["status"] == "deleted"

    # 5. Tasks: int + string priority normalization, priority sorting, manual reorder, AI note extraction + deduplication, Schoology sync
    t_crit = client.post("/api/v1/tasks", json={"title": "Submit College Essay Final", "priority": "critical", "category": "Study"})
    t_legacy_int = client.post("/api/v1/tasks", json={"title": "Email counselor form", "priority": 2, "category": "Study"})
    t_low = client.post("/api/v1/tasks", json={"title": "Clean desk drawer", "priority": "low", "category": "Personal"})
    assert t_crit.status_code == 201
    assert t_legacy_int.status_code == 201 and t_legacy_int.json()["priority"] == "normal"
    assert t_low.status_code == 201

    listed = client.get("/api/v1/tasks?include_completed=false").json()
    ids_in_order = [t["id"] for t in listed]
    assert ids_in_order.index(t_crit.json()["id"]) < ids_in_order.index(t_legacy_int.json()["id"])
    assert ids_in_order.index(t_legacy_int.json()["id"]) < ids_in_order.index(t_low.json()["id"])

    # Manual reorder within same priority
    t_crit2 = client.post("/api/v1/tasks", json={"title": "AP Calc FRQ Packet", "priority": "critical", "category": "Study"}).json()
    reord = client.post("/api/v1/tasks/reorder", json={"ordered_ids": [t_crit2["id"], t_crit.json()["id"]]})
    assert reord.status_code == 200
    reord_ids = [t["id"] for t in reord.json()["tasks"]]
    assert reord_ids.index(t_crit2["id"]) < reord_ids.index(t_crit.json()["id"])

    # File upload + AI task extraction deduplication (second click creates 0 duplicates!)
    note_md = (
        "# Study Notes\n"
        "Here are general lecture thoughts that are NOT tasks.\n"
        "- [ ] Reply to lab partner message\n"
        "- [ ] Complete Chemistry titration lab report\n"
    )
    up = client.post(
        "/api/v1/files/upload",
        json={"filename": "chem_notes.md", "text_content": note_md, "ingest_mode": "store"},
    )
    assert up.status_code == 201
    ing1 = client.post("/api/v1/files/chem_notes.md/ingest", json={"mode": "extract_tasks"}).json()
    assert ing1["created_tasks"] == 2  # Prose heading/paragraph NOT extracted!
    ing2 = client.post("/api/v1/files/chem_notes.md/ingest", json={"mode": "extract_tasks"}).json()
    assert ing2["created_tasks"] == 0 and ing2["skipped_duplicates"] >= 2  # Zero duplicate spam!

    # Schoology .ics Calendar import -> Sync Tasks from Schoology & Inbox
    ics_body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:sch-hw-99@schoology\r\n"
        f"DTSTART:{_ics_stamp(4, '235900')}\r\nDTEND:{_ics_stamp(4, '235900')}\r\n"
        "SUMMARY:AP US History DBQ Essay Outline\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    imp = client.post("/api/v1/calendar/import-ics", json={"ics_content": ics_body, "source_name": "Schoology"})
    assert imp.status_code in (200, 201)
    sync_res = client.post("/api/v1/tasks/sync-sources", json={"include_calendar": True, "include_inbox": True}).json()
    assert sync_res["ok"] is True
    assert sync_res["from_calendar"] >= 1
    all_task_titles = [t["title"] for t in client.get("/api/v1/tasks").json()]
    assert any("AP US History DBQ Essay Outline" in title for title in all_task_titles)

    # 6. Workload-Adaptive Briefing (40 important notifications takes significantly more triage time than 2!)
    two_imp = [Notification(source_app="ProtonMail", title=f"Msg {i}", is_important=True) for i in range(2)]
    forty_imp = [Notification(source_app="ProtonMail", title=f"Msg {i}", is_important=True) for i in range(40)]
    w_small = compute_briefing_workload(two_imp, [], [], cutoff_hhmm="23:00")
    w_heavy = compute_briefing_workload(forty_imp, [], [], cutoff_hhmm="23:00")
    assert w_heavy["inbox_triage_minutes"] > w_small["inbox_triage_minutes"] + 45

    # 7. Docker single-file bind-mount safe write (`OSError: [Errno 16] Device or resource busy` on Path.replace)
    cfg_file = tmp_path / "apps_config.yaml"
    mgr = AppsConfigManager(config_path=cfg_file)
    orig_replace = type(cfg_file).replace

    def _busy_replace(self_p, target):
        raise OSError(16, "Device or resource busy")

    with patch.object(type(cfg_file), "replace", _busy_replace):
        added = mgr.add_or_replace_app({"name": "BindMount Test App", "url": "https://example.org"})
        assert added["slug"] == "bindmount-test-app"
    assert "BindMount Test App" in cfg_file.read_text(encoding="utf-8")

    # 8. Shell HTML verification for v9.0 UX requirements
    shell_html = client.get("/api/v1/auth/shell").text
    assert "node-start-menu" in shell_html
    assert "toggleStartMenu()" in shell_html
    assert "gradesChartSummary" in shell_html
    assert "openNewAppModal('PowerSchool')" not in shell_html  # Removed overfit + PowerSchool from Monitors!
    assert "24/7 Background Auto-Sync (Cron)" in shell_html
    assert "Daily Work Cutoff &amp; Evening Quiet Buffer" in shell_html


@pytest.mark.usefixtures("requires_browser")
def test_v92_powerschool_all_courses_trends_history_and_assignments(client: TestClient):
    """
    Verifies the full v9.2 PowerSchool multi-course + trends + individual assignment suite:
    1. Extracts ALL 7 courses from realistic PowerSchool `#tblgrades` wrapped in an outer layout `<table>`,
       with `rowspan=2` + `colspan=5` Last/This Week M-F attendance columns, `td.table-element-text-align-start`,
       `1 (M-F)` expressions, screen-reader `Details about ...` spans, bare numeric grades (`94`), letter+percent,
       and `[ i ]` future-term links.
    2. Automatically harvests individual assignments via BOTH PowerSchool XTE JSON (`POST /ws/xte/assignment/lookup`
       requiring `Referer: .../guardian/scores.html?...` and `'001'`-stripped `studentFRN`) AND classic `#scoreTable` HTML.
    3. Recovers all 7 courses from `/guardian/home.html` even if the tab is sitting on a single class's `scores.html`.
    4. Persists grade history across scans, computes upward & downward deltas/trends + sparkline history points,
       and exposes full course + assignment state to `get_grades` AI tool.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse
    import json as _json
    import threading

    ps_state = {
        "calc_letter": "B+",
        "calc_score": "88.5",
        "chem_letter": "A",
        "chem_score": "93.0",
        "calc_quiz4_pts": None,
        "xte_referer_checks": 0,
    }

    class _RealisticPowerSchoolHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/guardian/home.html":
                # Realistic PowerSchool layout: outer wrapper table + #tblgrades with rowspan/colspan M-F attendance
                html = f"""<!doctype html><html><head><title>Grades and Attendance</title></head><body>
                <table class="layout-wrapper"><tr><td>
                  <table id="tblgrades" class="linkDescList grid">
                    <thead>
                      <tr>
                        <th rowspan="2">Exp</th>
                        <th colspan="5">Last Week</th>
                        <th colspan="5">This Week</th>
                        <th rowspan="2">Course</th>
                        <th rowspan="2">Q1</th>
                        <th rowspan="2">Q2</th>
                        <th rowspan="2">S1</th>
                        <th rowspan="2">Absences</th>
                        <th rowspan="2">Tardies</th>
                      </tr>
                      <tr>
                        <th>M</th><th>T</th><th>W</th><th>H</th><th>F</th>
                        <th>M</th><th>T</th><th>W</th><th>H</th><th>F</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr class="center">
                        <td>1 (M-F)</td>
                        <td colspan="10">Not available</td>
                        <td class="table-element-text-align-start">
                          AP Calculus BC<br/>
                          <span class="screen_readers_only">Details about AP Calculus BC</span>
                          <a href="teacherinfo.html?frn=004501">Details about AP Calculus BC</a> -
                          <a href="mailto:taylor@school.edu">Email Taylor, Alan</a> - Rm: 301
                        </td>
                        <td><a href="scores.html?frn=004501&begdate=08/25/2026&enddate=11/15/2026&fg=Q1&schoolid=100">{ps_state['calc_letter']}<br/>{ps_state['calc_score']}</a></td>
                        <td><a href="scores.html?frn=004501&fg=Q2">[ i ]</a></td>
                        <td><a href="scores.html?frn=004501&fg=S1">{ps_state['calc_letter']}<br/>{ps_state['calc_score']}</a></td>
                        <td>1</td><td>0</td>
                      </tr>
                      <tr class="center">
                        <td>2 (M-F)</td>
                        <td colspan="10">Not available</td>
                        <td class="table-element-text-align-start">
                          AP English Literature<br/>
                          <a href="mailto:morrison@school.edu">Email Morrison, Claire</a> - Rm: 114
                        </td>
                        <td><a href="scores.html?frn=004502&begdate=08/25/2026&enddate=11/15/2026&fg=Q1&schoolid=100">A<br/>95.2</a></td>
                        <td>[ i ]</td>
                        <td><a href="scores.html?frn=004502&fg=S1">A<br/>95.2</a></td>
                        <td>0</td><td>1</td>
                      </tr>
                      <tr class="center">
                        <td>3 (M-F)</td>
                        <td>Not available</td><td></td><td></td><td></td><td></td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td class="table-element-text-align-start">
                          Honors Chemistry<br/>
                          <a href="mailto:patel@school.edu">Email Patel, Priya</a> - Rm: Lab 2
                        </td>
                        <td><a href="scores.html?frn=004503&begdate=08/25/2026&enddate=11/15/2026&fg=Q1&schoolid=100">{ps_state['chem_letter']}<br/>{ps_state['chem_score']}</a></td>
                        <td>[ i ]</td>
                        <td>[ i ]</td>
                        <td>0</td><td>0</td>
                      </tr>
                      <tr class="center">
                        <td>4 (M-F)</td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td class="table-element-text-align-start">
                          AP US History<br/>
                          <a href="mailto:kim@school.edu">Email Kim, Daniel</a> - Rm: 208
                        </td>
                        <td><a href="scores.html?frn=004504&fg=Q1">94</a></td>
                        <td>[ i ]</td>
                        <td>[ i ]</td>
                        <td>2</td><td>0</td>
                      </tr>
                      <tr class="center">
                        <td>5 (M-F)</td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td class="table-element-text-align-start">
                          Spanish IV<br/>
                          <a href="mailto:rivera@school.edu">Email Rivera, Sofia</a> - Rm: 109
                        </td>
                        <td><a href="scores.html?frn=004505&fg=Q1">A-</a></td>
                        <td>[ i ]</td>
                        <td>[ i ]</td>
                        <td>0</td><td>0</td>
                      </tr>
                      <tr class="center">
                        <td>6 (M-F)</td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td class="table-element-text-align-start">
                          AP Computer Science A<br/>
                          <a href="mailto:lin@school.edu">Email Lin, Grace</a> - Rm: 405
                        </td>
                        <td><a href="scores.html?frn=004506&fg=Q1">A+<br/>98.6</a></td>
                        <td>[ i ]</td>
                        <td>[ i ]</td>
                        <td>0</td><td>0</td>
                      </tr>
                      <tr class="center">
                        <td>7 (M-F)</td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td></td><td></td><td></td><td></td><td></td>
                        <td class="table-element-text-align-start">
                          Chamber Orchestra<br/>
                          <a href="mailto:cho@school.edu">Email Cho, Hannah</a> - Rm: Band
                        </td>
                        <td><a href="scores.html?frn=004507&fg=Q1">A<br/>100</a></td>
                        <td>[ i ]</td>
                        <td>[ i ]</td>
                        <td>0</td><td>0</td>
                      </tr>
                    </tbody>
                  </table>
                </td></tr></table>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            if parsed.path == "/guardian/scores.html":
                qs = parse_qs(parsed.query)
                frn = (qs.get("frn") or [""])[0]
                if frn == "004501":
                    # Modern PowerSchool XTE AngularJS scores.html (requires POST /ws/xte/assignment/lookup)
                    html = f"""<!doctype html><html><head><title>Class Score Detail</title></head><body>
                    <table class="linkDescList">
                      <tr><th>Course</th><th>Teacher</th><th>Expression</th><th>Final Grade</th></tr>
                      <tr><td>AP Calculus BC</td><td>Alan Taylor</td><td>1 (M-F)</td><td>{ps_state['calc_letter']} {ps_state['calc_score']}%</td></tr>
                    </table>
                    <div id="xteContentWrapper" data-ng-init="studentFRN = '00177889';">
                      <div data-pss-student-assignment-scores="true" data-sectionid="501"></div>
                    </div>
                    </body></html>"""
                elif frn == "004503":
                    # Classic PowerSchool HTML #scoreTable scores.html
                    html = """<!doctype html><html><head><title>Class Score Detail</title></head><body>
                    <table id="scoreTable" class="linkDescList">
                      <tr><th>Due Date</th><th>Category</th><th>Assignment</th><th>Flags</th><th>Score</th><th>%</th><th>Grade</th></tr>
                      <tr><td>10/22/2026</td><td>Labs</td><td>Titration Stoichiometry Lab</td><td>Late</td><td>43/50</td><td>86%</td><td>B</td></tr>
                      <tr><td>10/18/2026</td><td>Homework</td><td>Molarity Worksheet</td><td>Missing</td><td>--/20</td><td>0%</td><td>F</td></tr>
                    </table>
                    </body></html>"""
                else:
                    html = "<!doctype html><html><body><p>No assignments</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/ws/xte/assignment/lookup":
                referer = self.headers.get("Referer") or ""
                length = int(self.headers.get("Content-Length") or 0)
                body = _json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                # Enforce real PowerSchool XTE contract: Referer must point to /guardian/scores.html and student_ids stripped of '001'
                if "/guardian/scores.html" not in referer or body.get("student_ids") != [77889] or body.get("section_ids") != [501]:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Bad Request</body></html>")
                    return
                ps_state["xte_referer_checks"] += 1
                items = [
                    {
                        "assignmentid": 9001,
                        "_assignmentsections": [
                            {
                                "name": "Series Convergence Test",
                                "duedate": "2026-10-15",
                                "scoreentrypoints": 50,
                                "weight": 1.0,
                                "_assignmentcategoryassociations": [
                                    {"isprimary": True, "_teachercategory": {"name": "Tests"}}
                                ],
                                "_assignmentscores": [
                                    {
                                        "scorepoints": 44,
                                        "scorepercent": 88.0,
                                        "scorelettergrade": "B+",
                                        "ismissing": False,
                                        "islate": False,
                                        "_assignmentscorecomment": {"commentvalue": "Strong Taylor error bound"},
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "assignmentid": 9002,
                        "_assignmentsections": [
                            {
                                "name": "Parametric Curves Problem Set",
                                "duedate": "2026-10-19",
                                "scoreentrypoints": 20,
                                "_assignmentcategoryassociations": [
                                    {"isprimary": True, "_teachercategory": {"name": "Homework"}}
                                ],
                                "_assignmentscores": [
                                    {
                                        "scorepoints": 18,
                                        "scorepercent": 90.0,
                                        "scorelettergrade": "A-",
                                        "ismissing": False,
                                        "islate": True,
                                    }
                                ],
                            }
                        ],
                    },
                ]
                if ps_state["calc_quiz4_pts"] is not None:
                    pts = ps_state["calc_quiz4_pts"]
                    items.append(
                        {
                            "assignmentid": 9003,
                            "_assignmentsections": [
                                {
                                    "name": "Polar Coordinates Quiz 4",
                                    "duedate": "2026-10-24",
                                    "scoreentrypoints": 20,
                                    "_assignmentcategoryassociations": [
                                        {"isprimary": True, "_teachercategory": {"name": "Quizzes"}}
                                    ],
                                    "_assignmentscores": [
                                        {
                                            "scorepoints": pts,
                                            "scorepercent": round((pts / 20.0) * 100, 1),
                                            "scorelettergrade": "A",
                                            "ismissing": False,
                                            "islate": False,
                                        }
                                    ],
                                }
                            ],
                        }
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                self.end_headers()
                self.wfile.write(_json.dumps(items).encode("utf-8"))
                return

            self.send_response(404)
            self.end_headers()

    # Clear any leftover courses from earlier tests so counts are crisp
    for old_c in client.get("/api/v1/grades").json().get("courses", []):
        client.delete(f"/api/v1/grades/{old_c['id']}")

    httpd = HTTPServer(("127.0.0.1", 0), _RealisticPowerSchoolHandler)
    port = httpd.server_port
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    try:
        ps_slug = client.post(
            "/api/v1/apps",
            json={
                "name": "PowerSchool Unified",
                "url": f"http://127.0.0.1:{port}/guardian/home.html",
                "indicator_type": "grades_tracker",
            },
        ).json()["app"]["slug"]

        # 1. Baseline Scan: ALL 7 courses + XTE JSON assignments + HTML #scoreTable assignments extracted!
        r1 = client.post(f"/api/v1/grades/scan?slug={ps_slug}")
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["summary"]["total_courses"] == 7, f"Expected 7 courses, got {d1['summary']['total_courses']}: {[c['course_name'] for c in d1['courses']]}"
        assert ps_state["xte_referer_checks"] >= 1

        by_name = {c["course_name"]: c for c in d1["courses"]}
        assert set(by_name.keys()) == {
            "AP Calculus BC",
            "AP English Literature",
            "Honors Chemistry",
            "AP US History",
            "Spanish IV",
            "AP Computer Science A",
            "Chamber Orchestra",
        }
        assert by_name["AP Calculus BC"]["teacher_name"] == "Alan Taylor"
        assert by_name["AP US History"]["score_percent"] == 94.0  # Bare numeric without %!
        assert by_name["Spanish IV"]["letter_grade"] == "A-"     # Bare letter grade!

        # Verify XTE assignments on AP Calculus BC
        calc_asgs = by_name["AP Calculus BC"]["assignments"]
        assert len(calc_asgs) == 2
        assert any(a["title"] == "Series Convergence Test" and a["score_display"] == "44/50" and "Taylor error" in a["comment"] for a in calc_asgs)
        assert any(a["title"] == "Parametric Curves Problem Set" and a["is_late"] is True for a in calc_asgs)

        # Verify classic HTML #scoreTable assignments + missing flag detection on Honors Chemistry
        chem_asgs = by_name["Honors Chemistry"]["assignments"]
        assert len(chem_asgs) == 2
        assert by_name["Honors Chemistry"]["missing_count"] == 1
        assert any(a["title"] == "Molarity Worksheet" and a["is_missing"] is True for a in chem_asgs)

        # 2. Scan 2: Update Calculus (up) & Chemistry (down) + add newly graded Quiz 4
        ps_state["calc_letter"] = "A-"
        ps_state["calc_score"] = "91.4"
        ps_state["calc_quiz4_pts"] = 19.5
        ps_state["chem_letter"] = "B+"
        ps_state["chem_score"] = "89.5"

        r2 = client.post(f"/api/v1/grades/scan?slug={ps_slug}")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["summary"]["total_courses"] == 7
        assert len(d2["scan"]["changes_detected"]) == 2

        by_name2 = {c["course_name"]: c for c in d2["courses"]}
        calc2 = by_name2["AP Calculus BC"]
        chem2 = by_name2["Honors Chemistry"]

        assert calc2["score_percent"] == 91.4
        assert calc2["previous_score_percent"] == 88.5
        assert calc2["delta_percent"] == 2.9
        assert calc2["trend"] == "up"
        assert "↑" in calc2["trend_label"]
        assert len(calc2["history"]) == 2
        assert len(calc2["assignments"]) == 3
        assert any("Polar Coordinates Quiz 4" in chg["summary"] for chg in d2["recent_changes"])

        assert chem2["score_percent"] == 89.5
        assert chem2["previous_score_percent"] == 93.0
        assert chem2["delta_percent"] == -3.5
        assert chem2["trend"] == "down"
        assert "↓" in chem2["trend_label"]

        # 3. Scan 3: Even if user navigated the live tab to single-course `scores.html`, scanning recovers ALL 7 courses
        # and builds a 3-point sparkline history when Calculus rises again to 94.2%!
        ps_state["calc_letter"] = "A"
        ps_state["calc_score"] = "94.2"
        r3 = client.post(f"/api/v1/grades/scan?slug={ps_slug}")
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["summary"]["total_courses"] == 7
        calc3 = next(c for c in d3["courses"] if c["course_name"] == "AP Calculus BC")
        assert [h["score_percent"] for h in calc3["history"]] == [88.5, 91.4, 94.2]

        client.delete(f"/api/v1/stream/tabs/{ps_slug}")
        client.delete(f"/api/v1/apps/{ps_slug}")
    finally:
        httpd.shutdown()


def test_v10_app_library_zero_preinstalled_and_1click_install(client: TestClient, tmp_path):
    """
    Production Release Pillar 3A:
    - A fresh install has ZERO preinstalled apps (no hardcoded MS Teams/Outlook); the registry is runtime data under data/.
    - `GET /api/v1/apps/catalog` exposes 17+ popular templates (Gmail, ProtonMail, Outlook, Teams, GroupMe, PowerSchool, Schoology, Canvas, Classroom).
    - `POST /api/v1/apps/install-template` installs selected apps on demand.
    """
    import yaml as _yaml
    from app.apps_manager import AppsConfigManager

    # No app registry is tracked in the repo any more: installed apps are user data.
    assert not (REPO_ROOT / "scrapers").exists()
    from app.config import settings as _settings
    assert "scrapers" not in Path(type(_settings).model_fields["apps_config_path"].default).parts

    fresh_mgr = AppsConfigManager(config_path=tmp_path / "fresh_apps.yaml")
    assert fresh_mgr.list_apps() == []
    cat_ids = {t["template_id"] for t in fresh_mgr.list_catalog()}
    for expected in ("gmail", "protonmail", "outlook", "ms-teams", "groupme", "powerschool", "schoology", "canvas", "google-classroom"):
        assert expected in cat_ids

    cat_res = client.get("/api/v1/apps/catalog")
    assert cat_res.status_code == 200
    assert len(cat_res.json()["catalog"]) >= 15

    inst = client.post(
        "/api/v1/apps/install-template",
        json={"template_id": "schoology", "custom_url": "https://district.schoology.com"},
    )
    assert inst.status_code == 201
    app_obj = inst.json()["app"]
    assert app_obj["name"] == "Schoology"
    assert app_obj["url"] == "https://district.schoology.com"
    client.delete(f"/api/v1/apps/{app_obj['slug']}")


@pytest.mark.usefixtures("requires_browser")
def test_v10_schoology_and_canvas_lms_extraction_and_task_sync(client: TestClient):
    """
    Production Release Pillar 1B & 1C:
    - Schoology (`leftathome/schoology-go` pattern: `Drupal.settings.s_common` CSRF + `/iapi2/site-navigation/courses` + `/iapi/parent/overdue_submissions/{uid}` + `/grades/grades`)
    - Canvas LMS (`/api/v1/courses?include[]=total_scores` + `/api/v1/courses/:id/assignments?include[]=submission` + `/api/v1/planner/items`)
    - Both extract courses, grades, and assignments inside Brave tabs AND sync overdue/upcoming items into `Tasks`!
    """
    import json as _json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _MockLMSHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            p = self.path.split("?")[0]
            # --- Schoology endpoints ---
            if p == "/schoology/home":
                html = """<!doctype html><html><head><title>Home | Schoology</title>
                <script>
                  window.Drupal = { settings: { s_common: { user: { uid: 4412, csrf_token: "tok-abc", csrf_key: "key-xyz" } } } };
                </script></head><body><div class="s-edge-feed">Feed</div></body></html>"""
                self._send(200, "text/html", html)
            elif p == "/iapi/parent/info":
                # Verify CSRF headers sent per schoology-go spec!
                assert self.headers.get("X-CSRF-Token") == "tok-abc"
                self._send(200, "application/json", _json.dumps({
                    "response_code": 200,
                    "body": {"session": {"view_mode": 1, "view_child": 9901}, "children": {"9901": {"uid": 9901, "name": "Student"}}},
                }))
            elif p == "/iapi2/site-navigation/courses":
                self._send(200, "application/json", _json.dumps({
                    "data": {"courses": [
                        {"nid": 501, "courseTitle": "AP Biology", "sectionTitle": "Sec 2"},
                        {"nid": 502, "courseTitle": "World Literature", "sectionTitle": "Sec 4"},
                    ]}
                }))
            elif p.startswith("/iapi/parent/overdue_submissions/"):
                sub_html = """<div class="upcoming-events">
                  <h3 class="submissions-title">OVERDUE</h3>
                  <div class="upcoming-event" data-start="1792886340">
                    <div class="event-title"><a href="/assignment/88001">Cellular Respiration Lab Report</a></div>
                    <div class="readonly-title event-subtitle">2 days overdue</div>
                    <div class="readonly-title event-subtitle">AP Biology</div>
                  </div>
                  <h3 class="submissions-title">UPCOMING</h3>
                  <div class="upcoming-event" data-start="1793145540">
                    <div class="event-title"><a href="/assignment/88002">Odyssey Analytical Essay</a></div>
                    <div class="readonly-title event-subtitle">Due Wednesday</div>
                    <div class="readonly-title event-subtitle">World Literature</div>
                  </div>
                </div>"""
                self._send(200, "application/json", _json.dumps({"response_code": 200, "body": {"html": sub_html}}))
            elif p == "/grades/grades":
                g_html = """<html><body>
                  <div class="gradebook-course">
                    <div class="gradebook-course-title"><a href="/course/501">AP Biology: Sec 2</a></div>
                    <span class="awarded-grade">93.6%</span>
                  </div>
                </body></html>"""
                self._send(200, "text/html", g_html)

            # --- Canvas LMS endpoints ---
            elif p == "/canvas/dashboard":
                html = """<!doctype html><html><head><title>Dashboard | Canvas</title>
                <script>window.ENV = { current_user_id: "77", FEATURES: {} };</script>
                </head><body><div id="DashboardCard_Container"><div class="ic-DashboardCard">Card</div></div></body></html>"""
                self._send(200, "text/html", html)
            elif p == "/api/v1/courses":
                # Include `while(1);` prefix to verify Canvas CSRF-prefix stripping!
                body = "while(1);" + _json.dumps([
                    {
                        "id": 3101,
                        "name": "Multivariable Calculus",
                        "course_code": "MATH-301",
                        "teachers": [{"display_name": "Prof. Lin"}],
                        "term": {"name": "Fall 2026"},
                        "enrollments": [{"type": "student", "computed_current_score": 96.4, "computed_current_grade": "A"}],
                    }
                ])
                self._send(200, "application/json", body)
            elif p == "/api/v1/courses/3101/assignments":
                body = "while(1);" + _json.dumps([
                    {
                        "id": 9101,
                        "name": "Problem Set 6: Stokes Theorem",
                        "due_at": "2026-10-29T03:59:00Z",
                        "points_possible": 50,
                        "submission": {"score": None, "missing": True, "workflow_state": "unsubmitted"},
                    },
                    {
                        "id": 9102,
                        "name": "Midterm Exam 1",
                        "due_at": "2026-10-15T18:00:00Z",
                        "points_possible": 100,
                        "submission": {"score": 97, "missing": False, "workflow_state": "graded"},
                    },
                ])
                self._send(200, "application/json", body)
            elif p == "/api/v1/planner/items":
                self._send(200, "application/json", "[]")
            else:
                self._send(404, "text/plain", "Not found")

        def _send(self, code, ctype, text):
            raw = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = HTTPServer(("127.0.0.1", port), _MockLMSHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        sch = client.post(
            "/api/v1/apps",
            json={"name": "Schoology", "url": f"http://127.0.0.1:{port}/schoology/home", "indicator_type": "grades_tracker"},
        ).json()["app"]
        cnv = client.post(
            "/api/v1/apps",
            json={"name": "Canvas LMS", "url": f"http://127.0.0.1:{port}/canvas/dashboard", "indicator_type": "grades_tracker"},
        ).json()["app"]

        for slug in (sch["slug"], cnv["slug"]):
            with client.websocket_connect(f"/api/v1/stream/ws/{slug}") as ws:
                ws.receive()

        scan_sch = client.post(f"/api/v1/grades/scan?slug={sch['slug']}")
        assert scan_sch.status_code == 200
        scan_cnv = client.post(f"/api/v1/grades/scan?slug={cnv['slug']}")
        assert scan_cnv.status_code == 200

        dash = client.get("/api/v1/grades").json()
        by_name = {c["course_name"]: c for c in dash["courses"]}
        assert "AP Biology (Sec 2)" in by_name
        assert by_name["AP Biology (Sec 2)"]["score_percent"] == 93.6
        assert any("Cellular Respiration Lab Report" in a["title"] for a in by_name["AP Biology (Sec 2)"]["assignments"])

        assert "Multivariable Calculus" in by_name
        assert by_name["Multivariable Calculus"]["score_percent"] == 96.4
        assert by_name["Multivariable Calculus"]["letter_grade"] == "A"
        assert len(by_name["Multivariable Calculus"]["assignments"]) == 2

        # Verify overdue/missing LMS assignments automatically synced into OS Tasks!
        tasks = client.get("/api/v1/tasks").json()
        task_titles = " | ".join(t["title"] for t in tasks)
        assert "Cellular Respiration Lab Report" in task_titles
        assert "Problem Set 6: Stokes Theorem" in task_titles

        for c in list(dash["courses"]):
            if c["course_name"] in ("AP Biology (Sec 2)", "World Literature (Sec 4)", "Multivariable Calculus"):
                client.delete(f"/api/v1/grades/{c['id']}")
        for slug in (sch["slug"], cnv["slug"]):
            client.delete(f"/api/v1/stream/tabs/{slug}")
            client.delete(f"/api/v1/apps/{slug}")
    finally:
        httpd.shutdown()


def test_v10_exact_weekday_calendar_fix_and_web_search(client: TestClient):
    """
    Production Release Pillar 2D:
    - Fixes the off-by-one weekday bug ("this Wednesday" MUST NEVER schedule on Tuesday!).
    - Even if an LLM tool call passes Tuesday `2026-10-27` while the user said "Schedule Dentist this Wednesday at 14:00"
      on Sunday `2026-10-25`, `verify_and_fix_weekday_date` auto-corrects it to Wednesday `2026-10-28`!
    """
    from datetime import date as _date, datetime as _datetime
    from sqlmodel import Session
    from app.database import engine
    from app.secretary import _parse_natural_date, execute_secretary_tool
    from app.web_search_tool import build_14_day_calendar_table, resolve_spoken_weekday_date, verify_and_fix_weekday_date

    # Reference date: Sunday 2026-10-25 -> Wednesday is 2026-10-28 (NOT Tuesday 2026-10-27!)
    ref_sunday_dt = _datetime(2026, 10, 25, 11, 0)
    fixed_start, fixed_end, note = verify_and_fix_weekday_date(
        user_text="Schedule Chemistry Review this Wednesday at 3pm",
        start_iso="2026-10-27T15:00:00",  # Wrong Tuesday ISO from LLM!
        end_iso="2026-10-27T16:00:00",
        now_dt=ref_sunday_dt,
    )
    assert fixed_start.startswith("2026-10-28T15:00")
    assert fixed_end.startswith("2026-10-28T16:00")
    assert note is not None and "Wednesday" in note

    cal_table = build_14_day_calendar_table(ref_sunday_dt)
    assert "Wednesday = 2026-10-28" in cal_table

    # Also test live `execute_secretary_tool("create_calendar_event", ...)` with `user_message_context`
    today_d = _parse_natural_date("today")
    now_local = _datetime(today_d.year, today_d.month, today_d.day, 10, 0)
    expected_wed_date, _ = resolve_spoken_weekday_date("this Wednesday", now_local)
    expected_wed = expected_wed_date.isoformat()
    wrong_tue_iso = _date.fromordinal(expected_wed_date.toordinal() - 1).isoformat()

    with Session(engine) as db:
        acts: list = []
        created = execute_secretary_tool(
            "create_calendar_event",
            {"title": "Wednesday Seminar Check", "date": wrong_tue_iso, "start_time": "15:00"},
            db,
            acts,
            user_message_context="Put Wednesday Seminar Check on my calendar this Wednesday at 15:00",
        )
        assert created["status"] == "created"
        ev_date = created["event"]["start_at"][:10]
        assert ev_date == expected_wed
        assert _date.fromisoformat(ev_date).strftime("%A") == "Wednesday"
        execute_secretary_tool("delete_calendar_event", {"event_id": created["event"]["id"]}, db, acts)


def test_v10_multi_provider_models_ping_server_job_queue_and_shared_logs(client: TestClient):
    """
    Production Release Pillar 2A, 2B & 2C:
    - Remote Ollama / worker heartbeat `POST /api/v1/models/ping` dynamically registers arbitrary models (`gemma4:e2b`, `qwen3.5:0.8b`, `qwen3.5:2b`).
    - Cloud API key provider registration (`POST /api/v1/models/providers`) masks keys in responses.
    - Server-side `POST /api/v1/ai/jobs/chat` accepts multiple rapid fire-and-forget prompts and completes them in background `asyncio.Task`s with zero client stream open!
    - Shared Multi-Agent AI Logbook (`POST /api/v1/ai/shared-logs` + Secretary `write_shared_log` / `edit_shared_log` / `read_shared_logs`).
    """
    import time as _time
    from sqlmodel import Session
    from app.database import engine
    from app.secretary import execute_secretary_tool

    # 1. Dynamic Model Ping (Guard-token OR authenticated session)
    guard_tok = auth_service.get_guard_token("worker")
    ping_res = client.post(
        "/api/v1/models/ping",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "provider_id": "lan-gpu-rig",
            "provider_name": "LAN Ollama Rig",
            "protocol": "ollama",
            "base_url": "http://127.0.0.1:65531",
            "models": ["gemma4:e2b", "qwen3.5:0.8b", "qwen3.5:2b"],
        },
    )
    assert ping_res.status_code == 200
    disc_ids = {m["model"] for m in ping_res.json()["discovered_models"]}
    assert {"gemma4:e2b", "qwen3.5:0.8b", "qwen3.5:2b"}.issubset(disc_ids)

    # 2. Cloud API Provider Registration (API key masked on read!)
    cloud_res = client.post(
        "/api/v1/models/providers",
        json={
            "provider_id": "openrouter-cloud",
            "name": "OpenRouter Cloud",
            "protocol": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-v1-supersecretkey12345",
            "models": ["anthropic/claude-3.7-sonnet"],
            "auto_discover": False,
            "enabled": False,
        },
    )
    assert cloud_res.status_code == 201
    prov_info = cloud_res.json()["provider"]
    assert prov_info["has_api_key"] is True
    assert "supersecretkey" not in str(prov_info)

    # 3. Fire-and-Forget Server-Side AI Job Queue (2 concurrent prompts without holding any stream open!)
    j1 = client.post(
        "/api/v1/ai/jobs/chat",
        json={"message": "Add task Review Organic Chem Mechanisms", "thread_id": "main", "enable_thinking": False},
    )
    j2 = client.post(
        "/api/v1/ai/jobs/chat",
        json={"message": "What time and date is it right now?", "thread_id": "main", "enable_thinking": False},
    )
    assert j1.status_code == 202 and j2.status_code == 202
    uid1, uid2 = j1.json()["job_uid"], j2.json()["job_uid"]

    # Poll server job status (simulating user closing browser & coming back!)
    deadline = _time.time() + 15.0
    st1 = st2 = ""
    while _time.time() < deadline:
        st1 = client.get(f"/api/v1/ai/jobs/{uid1}").json()["job"]["status"]
        st2 = client.get(f"/api/v1/ai/jobs/{uid2}").json()["job"]["status"]
        if st1 == "completed" and st2 == "completed":
            break
        _time.sleep(0.15)
    assert st1 == "completed" and st2 == "completed"

    # Both assistant replies were persisted to SQLite on the server!
    hist_raw = client.get("/api/v1/ai/chat?thread_id=main").json()
    hist = hist_raw["messages"] if isinstance(hist_raw, dict) else hist_raw
    assistant_texts = " ".join(m["content"] for m in hist if m["role"] == "assistant")
    assert "Organic Chem" in assistant_texts

    # 4. Shared Multi-Agent AI Logbook (API + Secretary Tools + Markdown sync)
    with Session(engine) as db:
        acts: list = []
        w_log = execute_secretary_tool(
            "write_shared_log",
            {
                "title": "User Study Cadence",
                "content": "Prefers 45m evening deep-work blocks before 22:30 cutoff.",
                "category": "preference",
                "tags": ["study", "schedule"],
                "pinned": True,
            },
            db,
            acts,
        )
        assert w_log["status"] == "saved"
        log_obj = w_log.get("log") or w_log.get("entry")
        lid = log_obj["id"]

        e_log = execute_secretary_tool(
            "edit_shared_log",
            {"log_id": lid, "append_content": "Verified by gemma4:e2b.", "pinned": True},
            db,
            acts,
        )
        assert e_log["status"] == "updated"
        edited_obj = e_log.get("log") or e_log.get("entry")
        assert edited_obj["revision"] == 2
        assert "Verified by gemma4:e2b" in edited_obj["content"]

    logs_api = client.get("/api/v1/ai/shared-logs").json()
    assert any(l["title"] == "User Study Cadence" for l in logs_api["logs"])
    assert Path(logs_api["markdown_path"]).is_file()


def test_v10_universal_social_focus_guard_resizable_widgets_and_extensions_sdk(client: TestClient):
    """
    Production Release Pillar 1A, 3C, 3D & 3E:
    - Universal Social & Media Focus Guard across Instagram, TikTok, Reddit, YouTube + custom sites + universal MV3 `.zip`.
    - Resizable, removable, addable Desktop Widgets (`GET/PUT /api/v1/desktop/widgets`).
    - Custom Apps & Widgets SDK (`gallery` install, `.nodeapp.json` export/upload, isolated `storage`, SDK docs).
    - Full Theme Surface & Typography settings (`canvas_bg`, `surface_bg`, `card_bg`, `text_color`, `font_family`, generalized presets).
    """
    import io
    import zipfile

    # 1. Universal Social Focus Guard
    rule_res = client.post(
        "/api/v1/focus-guard/platforms",
        json={"domain": "news.ycombinator.com", "name": "Hacker News", "enabled": True, "daily_limit_minutes": 20},
    )
    assert rule_res.status_code == 201
    platforms = rule_res.json()["platforms"]
    platform_ids = {p["id"] for p in platforms} if isinstance(platforms, list) else set(platforms.keys())
    for built_in in ("youtube", "instagram", "tiktok", "facebook", "x-twitter", "reddit"):
        assert built_in in platform_ids
    assert any("ycombinator" in pid for pid in platform_ids)

    guard_tok = auth_service.get_guard_token("focus_guard")
    ig_hb = client.post(
        "/api/v1/focus-guard/heartbeat",
        headers={"X-Node-Guard-Token": guard_tok},
        json={
            "url": "https://www.instagram.com/reels/C9xyz/",
            "title": "Instagram Reels",
            "is_short": True,
            "is_playing": True,
            "watched_seconds_delta": 15,
        },
    )
    assert ig_hb.status_code == 200
    assert ig_hb.json()["session"]["platform_id"] == "instagram"
    assert ig_hb.json()["session"]["should_nudge"] is True

    zbytes = client.get(f"/api/v1/focus-guard/extension.zip?token={guard_tok}").content
    with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
        manifest_txt = zf.read("manifest.json").decode("utf-8")
        assert "*://*.instagram.com/*" in manifest_txt
        assert "*://*.tiktok.com/*" in manifest_txt
        assert "*://*.news.ycombinator.com/*" in manifest_txt
        assert "Universal Social & Media Focus Guard" in manifest_txt

    # 2. Resizable Desktop Widgets Grid Persistence
    dw_get = client.get("/api/v1/desktop/widgets")
    assert dw_get.status_code == 200
    widgets = dw_get.json()["widgets"]
    assert len(widgets) >= 5
    widgets[0]["col_span"] = 6
    widgets[0]["min_height"] = 280
    dw_put = client.put("/api/v1/desktop/widgets", json={"widgets": widgets})
    assert dw_put.status_code == 200
    assert dw_put.json()["widgets"][0]["col_span"] == 6
    assert dw_put.json()["widgets"][0]["min_height"] == 280

    # 3. Custom Apps & Widgets SDK (Starter Gallery + Storage + Export + Docs)
    gal_inst = client.post("/api/v1/extensions/gallery/pomodoro-studio/install")
    assert gal_inst.status_code == 201
    ext = gal_inst.json()["extension"]
    assert ext["id"] == "pomodoro-studio"
    assert "window.NodeOS" in ext["widget_srcdoc"]

    st_save = client.post(
        "/api/v1/extensions/pomodoro-studio/storage",
        json={"data": {"sessions": 4}, "merge": True},
    )
    assert st_save.status_code == 200
    assert st_save.json()["storage"]["sessions"] == 4

    exp = client.get("/api/v1/extensions/pomodoro-studio/export")
    assert exp.status_code == 200
    assert exp.json()["format"] == "nodeapp-v1"

    sdk = client.get("/api/v1/extensions/sdk-docs")
    assert sdk.status_code == 200
    assert "window.NodeOS" in sdk.json()["markdown"]

    # 4. Generalized Theme Surfaces & Fonts
    theme_patch = client.patch(
        "/api/v1/settings",
        json={
            "canvas_bg": "#101622",
            "surface_bg": "#162032",
            "card_bg": "#1b273d",
            "text_color": "#f8fafc",
            "font_family": "geist",
        },
    )
    assert theme_patch.status_code == 200
    vals = theme_patch.json()["values"]
    assert vals["canvas_bg"] == "#101622" and vals["font_family"] == "geist"
    preset_ids = {p["id"] for p in theme_patch.json()["presets"]}
    assert {"nordic-frost", "warm-espresso", "crisp-daylight"}.issubset(preset_ids)


def test_v101_app_rename_cascades_without_duplicate_inbox_tags(client: TestClient):
    """Renaming a monitored account must atomically migrate Inbox notifications and heartbeats."""
    created = client.post(
        "/api/v1/apps",
        json={
            "name": "Temp School Mail",
            "url": "https://mail.example.edu",
            "indicator_type": "dom_badge",
            "account_profile": "school",
        },
    )
    assert created.status_code in (200, 201)
    old_slug = created.json()["app"]["slug"]

    ping = client.post(
        "/api/v1/notifications",
        json={
            "source_app": "Temp School Mail",
            "title": "Financial Aid Notice",
            "message": "Please review your semester statement by Friday.",
            "indicator_type": "dom_badge",
            "unread_count": 2,
        },
    )
    assert ping.status_code in (200, 201)

    renamed = client.patch(
        f"/api/v1/apps/{old_slug}",
        json={"name": "University Mail"},
    )
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["app"]["name"] == "University Mail"
    assert body["migrated_notifications"] >= 1

    notifs = client.get("/api/v1/notifications").json()
    source_names = {n["source_app"] for n in notifs}
    assert "University Mail" in source_names
    assert "Temp School Mail" not in source_names

    hbs = client.get("/api/v1/monitors").json()
    hb_rows = hbs.get("monitors", []) if isinstance(hbs, dict) else hbs
    hb_names = {h["source_app"] for h in hb_rows}
    assert "Temp School Mail" not in hb_names


def test_v101_inbox_custom_categories_and_decision_first_autosort(client: TestClient):
    """Custom global + per-app Inbox categories & decision-model-first auto-sorting with chat-model fallback."""
    cat_res = client.post(
        "/api/v1/notifications/categories",
        json={"action": "add", "categories": ["Professors", "Receipts"]},
    )
    assert cat_res.status_code == 200
    data = cat_res.json()
    assert "Professors" in data["global_categories"]
    assert "Receipts" in data["global_categories"]
    assert data["autosort_engine"]["primary"] == "decision"

    client.post(
        "/api/v1/notifications",
        json={
            "source_app": "University Mail",
            "title": "Invoice Receipt #4491 Paid",
            "message": "Your payment receipt for textbook order #4491.",
            "indicator_type": "dom_badge",
        },
    )
    sorted_res = client.post("/api/v1/notifications/autosort")
    assert sorted_res.status_code == 200
    assert sorted_res.json()["status"] == "ok"
    assert sorted_res.json()["autosort_engine"]["primary"] == "decision"


def test_v101_unified_ai_instructions_feedback_and_anti_overfit_self_improve(client: TestClient):
    """Unified AI Instructions section + chat ratings + frustration detection + disciplined self-improvement."""
    from app.self_improvement import detect_frustration_signal

    assert detect_frustration_signal("No, that's wrong! You didn't read what I asked.") is not None
    assert detect_frustration_signal("Please add Chem Lab at 2pm tomorrow.") is None

    listing = client.get("/api/v1/ai/instructions")
    assert listing.status_code == 200
    filenames = [f["filename"] for f in listing.json()["files"]]
    assert "persona.md" in filenames
    assert "self_improvement.md" in filenames
    assert "shared_agent_log.md" in filenames

    saved = client.put(
        "/api/v1/ai/instructions/research_workflow.md",
        json={"content": "# Research Workflow\n\n- Cite primary sources concisely.\n"},
    )
    assert saved.status_code == 200
    assert saved.json()["filename"] == "research_workflow.md"

    fb_poor = client.post(
        "/api/v1/ai/feedback",
        json={
            "rating": "poor",
            "reason": "Too verbose and missed the due date",
            "user_prompt": "Summarize my assignment due date",
            "assistant_reply": "Here is a very long essay without the date...",
        },
    )
    assert fb_poor.status_code in (200, 201)
    assert fb_poor.json()["status"] == "recorded"

    fb_good = client.post(
        "/api/v1/ai/feedback",
        json={
            "rating": "good",
            "user_prompt": "Add AP Calc homework for Friday",
            "assistant_reply": "Added AP Calc homework due Friday.",
        },
    )
    assert fb_good.status_code in (200, 201)

    reflect = client.post("/api/v1/ai/self-improve")
    assert reflect.status_code == 200
    assert reflect.json()["status"] in ("updated", "no_pending_feedback")

    si_doc = client.get("/api/v1/ai/instructions/self_improvement.md")
    assert si_doc.status_code == 200
    assert "Anti-Overfitting" in si_doc.json()["content"]


def test_v101_shared_drive_explorer_crud_and_move(client: TestClient):
    """Hierarchical Shared Drive between user, AI, and server."""
    root = client.get("/api/v1/drive")
    assert root.status_code == 200
    starter_names = {e["name"] for e in root.json()["entries"] if e["is_dir"]}
    assert {"AI-Drop", "Study-Docs", "Projects"}.issubset(starter_names)

    mkdir = client.post("/api/v1/drive/mkdir", json={"path": "Projects/Chem-101"})
    assert mkdir.status_code in (200, 201)

    written = client.put(
        "/api/v1/drive/file",
        json={
            "path": "Projects/Chem-101/lab_notes.md",
            "content": "# Titration Lab\n\n- Molarity = 0.12M\n",
        },
    )
    assert written.status_code == 200

    read_back = client.get("/api/v1/drive/file", params={"path": "Projects/Chem-101/lab_notes.md"})
    assert read_back.status_code == 200
    assert "Titration Lab" in read_back.json()["content"]

    moved = client.post(
        "/api/v1/drive/move",
        json={"src_path": "Projects/Chem-101/lab_notes.md", "dest_path": "Study-Docs/chem_lab_notes.md"},
    )
    assert moved.status_code == 200

    deleted = client.delete("/api/v1/drive", params={"path": "Study-Docs/chem_lab_notes.md"})
    assert deleted.status_code == 200


def test_v101_settings_bar_visibility_and_keybinds(client: TestClient):
    """Topbar/Dock customization and keybinds persist cleanly including clearing back to empty CSV."""
    p1 = client.patch(
        "/api/v1/settings",
        json={
            "topbar_hidden_items": "zen_btn,cron_btn",
            "dock_hidden_items": "goals,gallery",
            "kb_start_menu": "Ctrl+Space",
        },
    )
    assert p1.status_code == 200
    vals = p1.json()["values"]
    assert vals["topbar_hidden_items"] == "zen_btn,cron_btn"
    assert vals["dock_hidden_items"] == "goals,gallery"
    assert vals["kb_start_menu"] == "Ctrl+Space"

    p2 = client.patch("/api/v1/settings", json={"topbar_hidden_items": ""})
    assert p2.status_code == 200
    assert p2.json()["values"]["topbar_hidden_items"] == ""







# ===========================================================================
# v10.2 — Decision Gate (LFM3 d1), tag-first Inbox, Gmail filters, dedupe,
#         grades ownership/tabs, api-anything bridge, new-tab modes
# ===========================================================================


def test_v102_decision_gate_status_and_offline_fallback(client: TestClient):
    """With no gate configured the system must degrade cleanly, never 500."""
    st = client.get("/api/v1/decision-gate/status?force=true")
    assert st.status_code == 200
    body = st.json()
    assert "online" in body and "detail" in body and "protocol" in body
    assert body["protocol"] == "typesafe-systemone"
    if not body.get("configured"):
        assert body["online"] is False

    # Self-test surfaces the offline state instead of raising.
    sel = client.post("/api/v1/decision-gate/selftest", json={"text": "Quiz 3 due tonight 11:59pm"})
    assert sel.status_code == 200
    assert "ok" in sel.json()


def test_v102_tag_registry_and_gmail_filter_grammar(client: TestClient):
    """Tags are user data; the query language round-trips and explains itself."""
    tags = client.get("/api/v1/inbox/tags").json()
    names = tags["names"]
    assert "School" in names and "Urgent" in names

    # Add a custom tag with a description that becomes the classifier criteria.
    created = client.post(
        "/api/v1/inbox/tags",
        json={
            "name": "Band",
            "description": "Marching band, rehearsals, sectionals, and band director messages",
            "color": "violet",
            "important": True,
        },
    )
    assert created.status_code == 200
    assert "Band" in created.json()["names"]

    # Rename cascades onto every notification carrying it.
    n = client.post(
        "/api/v1/notifications",
        json={"source_app": "ProtonMail", "title": "Sectional moved to 6pm", "message": "band room"},
    ).json()
    moved = client.post(f"/api/v1/inbox/{n['id']}/tags", json={"tag": "Band", "learn": True})
    refreshed_debug = moved.json()
    ren = client.patch("/api/v1/inbox/tags", json={"old_name": "Band", "new_name": "Band & Music"})
    assert ren.status_code == 200
    assert ren.json()["migrated_notifications"] >= 1, (ren.json(), refreshed_debug)[1]
    refreshed = [x for x in client.get("/api/v1/notifications?limit=200").json() if x["id"] == n["id"]][0]
    assert "Band & Music" in refreshed["tags_csv"]

    # Built-in tags are protected from deletion.
    assert client.delete("/api/v1/inbox/tags/School").status_code == 400
    assert client.delete("/api/v1/inbox/tags/NoSuchTag").status_code == 404


def test_v102_gmail_query_dsl_matches_tags_senders_dates(client: TestClient):
    from app.inbox_tags import inbox_tag_service

    ast = inbox_tag_service.parse_filter('tag:school is:unread from:prof@school.edu -tag:promo newer_than:7d')
    assert ast["tokens"]
    assert inbox_tag_service.explain_filter('tag:school is:unread').startswith("tag:school")

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    item = {
        "title": "DBQ essay outline due",
        "message": "Submit before midnight",
        "source_app": "Canvas LMS",
        "sender": "prof@school.edu",
        "tags_csv": "School, Urgent",
        "category": "School",
        "read_state": False,
        "is_important": True,
        "priority": "high",
        "timestamp": now.isoformat(),
        "folder": "inbox",
    }
    assert inbox_tag_service.matches(item, ast, now=now) is True
    assert inbox_tag_service.matches({**item, "read_state": True}, ast, now=now) is False
    assert inbox_tag_service.matches({**item, "tags_csv": "Promo"}, inbox_tag_service.parse_filter("tag:promo"), now=now) is True
    assert inbox_tag_service.matches(
        {**item, "tags_csv": "Promo"}, inbox_tag_service.parse_filter("-tag:promo"), now=now
    ) is False

    # Saved filters (Gmail-style labels) are CRUD-able; builtins are protected.
    saved = client.post("/api/v1/inbox/filters", json={"name": "Band stuff", "query": "tag:school OR tag:personal"})
    assert saved.status_code == 200
    f_list = client.get("/api/v1/inbox/filters").json()
    assert any(f["name"] == "Band stuff" for f in f_list["filters"])
    assert client.delete("/api/v1/inbox/filters/important").status_code == 400


def test_v102_dedupe_merges_identical_messages_without_tag_pileup(client: TestClient):
    """The same event arriving from 3 feeds becomes ONE row. Since 0.14 the tags are
    not unioned (that made one email show under every copy's tag); the survivor keeps
    one copy's tags and is queued to be re-tagged from the merged content."""
    from app.decision_gate import compute_dedupe_key

    title = "Orchestra rehearsal moved to 7pm"
    body = "Rehearsal is now at 7pm in the band hall. Bring your folder."
    key = compute_dedupe_key("Gmail", title, sender="director@school.edu", body=body)

    from sqlmodel import Session, select
    from app.database import engine
    from app.models import Notification

    ids = []
    with Session(engine) as s:
        for app_name, tag in (("Gmail", "School"), ("Canvas LMS", "Urgent"), ("Outlook", "Personal")):
            row = Notification(
                source_app=app_name,
                title=title,
                message=body,
                tags_csv=tag,
                category=tag,
                dedupe_key=key,
                is_important=True,
                priority="high",
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            ids.append(row.id)

    res = client.post("/api/v1/inbox/dedupe").json()
    assert res["ok"] is True
    assert res["removed"] >= 2

    remaining = [x for x in client.get("/api/v1/notifications?limit=300").json() if x["id"] in ids]
    assert len(remaining) == 1
    survivor = remaining[0]
    assert survivor["tags_csv"] == "School"
    assert survivor["ai_processed"] is False
    assert survivor["merged_count"] >= 3
    assert survivor["dedupe_sources"]


def test_v102_tag_move_learns_and_exports_finetune_jsonl(client: TestClient):
    n = client.post(
        "/api/v1/notifications",
        json={"source_app": "ProtonMail", "title": "Your section leader sent a chart update", "message": "band"},
    ).json()
    moved = client.post(f"/api/v1/inbox/{n['id']}/tags", json={"tag": "People", "learn": True})
    assert moved.status_code == 200
    assert moved.json()["notification"]["tags_csv"].startswith("People")
    assert moved.json()["notification"]["tag_corrected_by_user"] is True
    assert moved.json()["learned"]["sender_key"]

    learning = client.get("/api/v1/inbox/learning").json()
    assert learning["counts"]["hints"] >= 1

    export = client.get("/api/v1/inbox/learning/export")
    assert export.status_code == 200
    assert "application/x-ndjson" in export.headers["content-type"]
    assert '"label"' in export.text


def test_v102_quick_capture_creates_clean_event_titles(client: TestClient):
    """"mark that I have band practice on friday" must not become the event title."""
    res = client.post("/api/v1/ai/quick-capture", json={"text": "mark that I have band practice on friday at 4pm"})
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] in ("event", "task")
    if data["kind"] == "event":
        title = data["event"]["title"].lower()
        assert "band practice" in title
        assert "mark" not in title and "i have" not in title and "friday" not in title

    task_res = client.post("/api/v1/ai/quick-capture", json={"text": "buy new rosin for the bow"})
    assert task_res.status_code == 200
    assert task_res.json()["kind"] == "task"


def test_v102_tool_plan_and_calendar_title_cleaning(client: TestClient):
    from app.secretary import _clean_event_title

    assert _clean_event_title("mark that I have band practice on friday") == "band practice"
    assert "dentist" in _clean_event_title("add an event for dentist appointment on 2026-11-03 at 2pm")
    assert _clean_event_title("block off study time friday") == "study time"
    assert "mom birthday" in _clean_event_title("remember that my mom birthday is on oct 3")

    plan = client.post("/api/v1/ai/tool-plan", json={"message": "put band practice on my calendar friday"})
    assert plan.status_code == 200
    body = plan.json()
    assert body["tool_count"] >= 30  # full suite still available
    if body.get("plan"):
        assert "create_calendar_event" in body["plan"]["tools"] or "list_calendar_events" in body["plan"]["tools"]


def test_v102_grades_hidden_courses_do_not_return(client: TestClient):
    """Removing a course must survive the next scan."""
    ingest = client.post(
        "/api/v1/grades/ingest",
        json={
            "source_app": "Schoology",
            "courses": [
                {"course_name": "AP Music Theory", "letter_grade": "A", "score_percent": 95.5, "period": "1"},
                {"course_name": "Study Hall", "letter_grade": "P", "score_percent": None, "period": "7"},
            ],
        },
    )
    assert ingest.status_code == 200
    courses = client.get("/api/v1/grades").json()["courses"]
    study_hall = next(c for c in courses if c["course_name"] == "Study Hall")

    hid = client.post(
        "/api/v1/grades/hide",
        params={"course_key": study_hall["course_key"], "source_app": "Schoology"},
    )
    assert hid.status_code == 200
    assert hid.json()["ok"] is True

    # Re-scan (ingest again) — the hidden course must NOT come back into view.
    client.post(
        "/api/v1/grades/ingest",
        json={
            "source_app": "Schoology",
            "courses": [
                {"course_name": "AP Music Theory", "letter_grade": "A", "score_percent": 96.0, "period": "1"},
                {"course_name": "Study Hall", "letter_grade": "P", "score_percent": None, "period": "7"},
            ],
        },
    )
    control = client.get("/api/v1/grades/control").json()
    visible_keys = {v["course_key"] for v in control["visibility"].values() if not v["hidden"]}
    assert study_hall["course_key"] not in {
        c["course_key"] for c in client.get("/api/v1/grades").json()["courses"]
    }
    assert any(h["course_key"] == study_hall["course_key"] for h in control["hidden_courses"])

    # Restoring brings it back.
    restored = client.post(
        "/api/v1/grades/visibility",
        json={"course_key": study_hall["course_key"], "tracked": True, "hidden": False},
    )
    assert restored.status_code == 200
    assert restored.json()["visibility"]["hidden"] is False


def test_v102_grade_tabs_scope_averages(client: TestClient):
    client.post(
        "/api/v1/grades/ingest",
        json={
            "source_app": "Canvas LMS",
            "courses": [
                {"course_name": "Algebra II", "letter_grade": "A", "score_percent": 94.0},
                {"course_name": "Ceramics", "letter_grade": "B", "score_percent": 85.0},
            ],
        },
    )
    tab = client.post(
        "/api/v1/grades/tabs",
        json={"name": "Core Only", "color": "sky", "course_keys": ["algebra-ii"]},
    )
    assert tab.status_code == 200
    scoped = {s["name"]: s for s in client.get("/api/v1/grades/control").json()["scoped"]}
    assert "Core Only" in scoped
    core = scoped["Core Only"]
    assert core["course_count"] == 1
    assert core["average_percent"] == 94.0
    assert scoped["All Courses"]["course_count"] >= 2

    assert client.delete("/api/v1/grades/tabs/all").status_code == 400
    assert client.delete("/api/v1/grades/tabs/core-only").status_code == 200


def test_v102_api_bridge_status_config_and_cookie_import(client: TestClient):
    st = client.get("/api/v1/bridges/status?force=true")
    assert st.status_code == 200
    body = st.json()
    assert "reachable" in body and "cli" in body and "config" in body
    # Bridge is optional: unreachable is a normal state, not an error.
    if not body["reachable"]:
        assert body["detail"]

    cfg = client.post("/api/v1/bridges/config", json={"base_url": "http://127.0.0.1:8787", "token": "test-tok"})
    assert cfg.status_code == 200
    assert cfg.json()["config"]["base_url"] == "http://127.0.0.1:8787"

    imp = client.post(
        "/api/v1/bridges/session",
        json={
            "app_slug": "ProtonMail",
            "cookies": [{"name": "SESSION", "value": "abc123", "domain": ".proton.me", "path": "/"}],
            "device": "extension",
        },
    )
    assert imp.status_code == 200
    assert imp.json()["stored"] is True
    assert imp.json()["count"] == 1


def test_v102_newtab_modes_and_standalone_page(client: TestClient):
    cfg = client.get("/api/v1/newtab/config")
    assert cfg.status_code == 200
    body = cfg.json()
    assert body["mode"] in body["modes"]
    assert set(body["modes"]) == {"dashboard", "lock", "wallpaper", "custom", "off"}

    set_res = client.post(
        "/api/v1/newtab/config",
        json={
            "mode": "custom",
            "show_on_open": True,
            "custom_blocks_csv": "clock,urgent_tasks,next_event",
            "wallpaper_id": "misty-pines.jpg",
            "greeting_name": "Max",
            "overlay_strength": 55,
        },
    )
    assert set_res.status_code == 200
    got = client.get("/api/v1/newtab/config").json()
    assert got["mode"] == "custom"
    assert "urgent_tasks" in got["custom_blocks"]
    assert got["greeting_name"] == "Max"
    assert got["overlay_strength"] == 55

    bad = client.post("/api/v1/newtab/config", json={"mode": "nonsense"})
    assert bad.status_code == 400

    # Standalone page is served and pulls in no external assets (works in extension iframes).
    page = client.get("/newtab")
    assert page.status_code == 200
    assert "Node · New Tab" in page.text
    assert "cdn." not in page.text


def test_v102_offline_event_classification_and_month_dates(client: TestClient):
    """With no decision model reachable, quick capture must still classify correctly."""
    from app.main import _text_looks_like_event
    from app.secretary import _clean_event_title, _parse_natural_date

    assert _text_looks_like_event("band practice friday 4pm") is True
    assert _text_looks_like_event("dentist appointment tomorrow") is True
    assert _text_looks_like_event("concert on sept 12") is True
    assert _text_looks_like_event("submit homework friday") is False
    assert _text_looks_like_event("buy milk tomorrow") is False
    assert _text_looks_like_event("call mom tomorrow") is False

    # Trailing times/dates never survive into the event title.
    assert _clean_event_title("band practice friday 4pm") == "band practice"
    assert _clean_event_title("standup 9:30") == "standup"
    assert _clean_event_title("band rehearsal next tuesday 6pm") == "band rehearsal"

    # Month names parse, and roll to next year once the date has gone by.
    d = _parse_natural_date("party december 31")
    assert (d.month, d.day) == (12, 31)
    d2 = _parse_natural_date("concert sept 12")
    assert (d2.month, d2.day) == (9, 12)

    res = client.post("/api/v1/ai/quick-capture", json={"text": "band practice friday 4pm"})
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "event"
    assert body["event"]["title"].lower() == "band practice"
    assert "t" in body["event"]["start_at"].lower()


def test_v102_extension_guard_token_surface(client: TestClient, unauth_client: TestClient):
    """The extension token unlocks only the extension's own routes."""
    info = client.get("/api/v1/ext/info")
    assert info.status_code == 200
    token = info.json()["guard_token"]
    assert token

    boot = client.get("/api/v1/ext/bootstrap", headers={"X-Node-Guard-Token": token})
    assert boot.status_code == 200
    data = boot.json()
    assert set(["mode", "tasks", "urgent", "gate", "counts"]).issubset(data.keys())
    assert data["mode"] in ("dashboard", "lock", "wallpaper", "custom", "off")

    # A cold, cookieless client must be locked out (the token is the only way in).
    assert unauth_client.get("/api/v1/ext/bootstrap").status_code == 401
    # The token alone works with no session at all.
    assert unauth_client.get(
        "/api/v1/ext/bootstrap", headers={"X-Node-Guard-Token": token}
    ).status_code == 200

    # The package ships the whole MV3 extension.
    pkg = client.get("/api/v1/ext/package.zip")
    assert pkg.status_code == 200
    import io as _io
    import zipfile as _zip
    zf = _zip.ZipFile(_io.BytesIO(pkg.content))
    names = set(zf.namelist())
    assert "node-webos-extension/manifest.json" in names
    assert "node-webos-extension/newtab.html" in names
    assert "node-webos-extension/background.js" in names
    import json as _json_mod
    manifest = _json_mod.loads(zf.read("node-webos-extension/manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["chrome_url_overrides"]["newtab"] == "newtab.html"
    assert "cookies" in manifest["permissions"]



def test_v102_api_bridge_contract(client: TestClient):
    """The bridge must speak the real api-anything REST contract, not a guess."""
    st = client.get("/api/v1/bridges/status").json()
    assert "reachable" in st and "cli" in st and "operations" in st
    assert st["cli"]["npx_available"] in (True, False)

    # Missing "site/op" is rejected with a helpful pointer, not a traceback.
    bad = client.post("/api/v1/bridges/call", json={"operation": "nonsense", "args": {}})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert "site/op" in bad.json()["error"]

    # Config round-trips through the service.
    cfg = client.post(
        "/api/v1/bridges/config",
        json={"base_url": "http://127.0.0.1:8787", "token": "t", "prefer_http_over_tabs": True},
    )
    assert cfg.status_code == 200
    assert cfg.json()["config"]["base_url"] == "http://127.0.0.1:8787"

    # Host -> bridge slug mapping keeps multi-tenant LMS hosts distinct.
    from app.main import _domain_to_slug
    assert _domain_to_slug("canvas.instructure.com") == "canvas"
    assert _domain_to_slug("mail.google.com") == "gmail"
    assert _domain_to_slug("powerschool.com") == "powerschool"
    assert _domain_to_slug("ps.example.edu") == "ps"


def test_v102_user_tag_correction_beats_ai_pass(client: TestClient):
    """A hand-chosen tag must survive a later AI pass or re-scan."""
    client.post("/api/v1/inbox/tags", json={"name": "Band", "description": "band", "color": "violet", "important": True})
    n = client.post(
        "/api/v1/notifications",
        json={"source_app": "ProtonMail", "title": "Sectional moved", "message": "band room"},
    ).json()
    moved = client.post(f"/api/v1/inbox/{n['id']}/tags", json={"tag": "Band", "learn": True}).json()
    assert moved["notification"]["tag_corrected_by_user"] is True
    assert "Band" in moved["notification"]["tags_csv"]

    # Sweep the AI pass over it; the correction must still win.
    client.post("/api/v1/notifications/ai-process?limit=20")
    row = [x for x in client.get("/api/v1/notifications?limit=200").json() if x["id"] == n["id"]][0]
    assert "Band" in row["tags_csv"]


def test_v011_guard_tokens_are_scoped(client: TestClient, unauth_client: TestClient):
    """Each guard token unlocks only its own routes; none can read or rotate tokens."""
    listing = client.get("/api/v1/auth/guard-tokens")
    assert listing.status_code == 200
    body = listing.json()
    # A fresh install never starts in legacy shared mode.
    assert body["legacy_shared"] is False
    toks = {s["id"]: s["token"] for s in body["scopes"]}
    assert set(toks) == {"extension", "focus_guard", "worker"}
    assert len(set(toks.values())) == 3
    assert toks["extension"] == client.get("/api/v1/ext/info").json()["guard_token"]

    def call(path: str, tok: str):
        hdr = {"X-Node-Guard-Token": tok}
        if path == "/api/v1/models/ping":
            return unauth_client.post(path, headers=hdr, json={"provider_id": "scope-test", "protocol": "ollama", "base_url": "http://10.9.9.9:11434", "models": []})
        return unauth_client.get(path, headers=hdr)

    allowed = {
        "extension": ["/api/v1/ext/bootstrap", "/api/v1/newtab/config"],
        "focus_guard": ["/api/v1/focus-guard/status"],
        "worker": ["/api/v1/models/ping"],
    }
    for scope, paths in allowed.items():
        for path in paths:
            assert call(path, toks[scope]).status_code == 200, (scope, path)
            for other in toks:
                if other != scope:
                    assert call(path, toks[other]).status_code == 401, (other, "must not open", path)

    # Guard tokens never reach session-only routes, including the token API itself.
    for tok in toks.values():
        for path in ("/api/v1/auth/guard-tokens", "/api/v1/notifications", "/api/v1/grades"):
            assert unauth_client.get(path, headers={"X-Node-Guard-Token": tok}).status_code == 401
        assert unauth_client.post("/api/v1/auth/guard-tokens/worker/rotate", headers={"X-Node-Guard-Token": tok}).status_code == 401

    # Rotation: old token dies, new one works.
    rotated = client.post("/api/v1/auth/guard-tokens/focus_guard/rotate").json()
    new_focus = {s["id"]: s["token"] for s in rotated["scopes"]}["focus_guard"]
    assert new_focus != toks["focus_guard"]
    assert call("/api/v1/focus-guard/status", toks["focus_guard"]).status_code == 401
    assert call("/api/v1/focus-guard/status", new_focus).status_code == 200
    assert client.post("/api/v1/auth/guard-tokens/nope/rotate").status_code == 404


def test_v011_guard_token_legacy_migration(client: TestClient, unauth_client: TestClient, tmp_path, monkeypatch):
    """Installs that pre-date scoping keep working until the user ends legacy mode."""
    from app.config import settings as _settings

    # An existing install: an extension token file exists, no scoped store yet.
    legacy_file = tmp_path / "guard_token.secret"
    legacy_file.write_text("L" * 40, encoding="utf-8")
    monkeypatch.setattr(_settings, "auth_guard_token_path", str(legacy_file))
    monkeypatch.setattr(_settings, "auth_guard_tokens_path", "")
    store = auth_service.ensure_guard_tokens()
    assert store["legacy_shared"] is True
    assert (tmp_path / "guard_tokens.json").is_file()
    assert auth_service.get_guard_token("extension") == "L" * 40  # unchanged, the extension keeps working

    old = {"X-Node-Guard-Token": "L" * 40}
    assert unauth_client.get("/api/v1/focus-guard/status", headers=old).status_code == 200
    # Legacy never widens past the old allowlist.
    assert unauth_client.get("/api/v1/notifications", headers=old).status_code == 401

    ended = client.post("/api/v1/auth/guard-tokens/end-legacy").json()
    assert ended["legacy_shared"] is False
    assert unauth_client.get("/api/v1/focus-guard/status", headers=old).status_code == 401
    assert unauth_client.get("/api/v1/ext/bootstrap", headers=old).status_code == 200

    # A brand-new install (no token file at all) starts scoped.
    fresh = tmp_path / "fresh"
    monkeypatch.setattr(_settings, "auth_guard_token_path", str(fresh / "guard_token.secret"))
    assert auth_service.ensure_guard_tokens()["legacy_shared"] is False


def test_v011_stream_websocket_closes_after_error(client: TestClient, monkeypatch):
    """If the server browser can't start, the socket reports the error and closes instead of hanging."""
    from app.browser_stream import browser_stream_manager

    async def _boom(*_a, **_kw):
        raise RuntimeError("browser failed to launch")

    monkeypatch.setattr(browser_stream_manager, "get_or_open_tab", _boom)
    with client.websocket_connect("/api/v1/stream/ws/anything") as ws:
        first = ws.receive()
        assert "browser failed to launch" in (first.get("text") or "")
        closing = ws.receive()
        assert closing["type"] == "websocket.close"
        assert closing["code"] == 1011


def test_v012_legacy_apps_registry_migrates_into_data(tmp_path):
    """Pre-0.12 installs kept installed apps in the tracked scrapers/ file; boot moves them to data/."""
    import yaml as _yaml
    from app.apps_manager import AppsConfigManager

    legacy = tmp_path / "legacy_apps_config.yaml"
    legacy.write_text(_yaml.safe_dump({"global": {"poll_interval_seconds": 30}, "apps": [{"name": "Gmail", "url": "https://mail.google.com"}]}), encoding="utf-8")
    empty_legacy = tmp_path / "empty.yaml"
    empty_legacy.write_text("apps: []\n", encoding="utf-8")

    mgr = AppsConfigManager(config_path=tmp_path / "data" / "apps_config.yaml")
    assert mgr.migrate_legacy_config([str(tmp_path / "missing.yaml"), str(empty_legacy), str(legacy)]) == str(legacy)
    assert [a["name"] for a in mgr.list_apps()] == ["Gmail"]
    assert legacy.is_file()  # source is never deleted

    # Idempotent: an existing registry is never overwritten.
    mgr.add_or_replace_app({"name": "Canvas", "url": "https://canvas.instructure.com"})
    assert mgr.migrate_legacy_config([str(legacy)]) is None
    assert sorted(a["name"] for a in mgr.list_apps()) == ["Canvas", "Gmail"]

    # Nothing to migrate from an empty legacy file.
    fresh = AppsConfigManager(config_path=tmp_path / "fresh" / "apps_config.yaml")
    assert fresh.migrate_legacy_config([str(empty_legacy)]) is None
    assert not fresh.config_path.exists()


# ---------------------------------------------------------------------------
# 0.13: two models (decision + chat), Von removed
# ---------------------------------------------------------------------------
def _fake_discovery(models):
    """Discovery rows shaped like ai_providers.discover_all_models output."""
    from app.ai_providers import _estimate_param_rank, is_decision_model

    return [
        {
            "id": f"ollama-default::{name}",
            "model": name,
            "provider_id": "ollama-default",
            "provider_name": "Ollama Server",
            "protocol": "ollama",
            "base_url": "http://localhost:11434",
            "loaded_in_ram": loaded,
            "size_rank": _estimate_param_rank(name),
            "supports_thinking": False,
            "kind": "decision" if is_decision_model(name) else "chat",
        }
        for name, loaded in models
    ]


def test_v013_decision_model_detection():
    from app.ai_providers import is_decision_model

    for name in ("d1", "d1:2b", "d1-omni", "d1.2", "ollama-default::d1:latest", "hf.co/LiquidAI/d1-2B-GGUF:Q8_0", "lfm3-d1"):
        assert is_decision_model(name), name
    for name in ("lfm3", "lfm3:1.7b", "qwen3.5:2b", "gemma4:e2b", "llama3.1:8b", "dolphin3", "deepseek-r1:1.5b"):
        assert not is_decision_model(name), name


def test_v013_chat_resolution_never_picks_decision_model(monkeypatch):
    """d1 loaded in RAM must not become the chat model, for any legacy role name."""
    import asyncio

    from app.ai_providers import ai_provider_registry

    rows = _fake_discovery([("d1:2b", True), ("lfm3:1.7b", False)])

    async def fake_discover(force=False):
        ai_provider_registry._discovery_cache = rows
        return rows

    monkeypatch.setattr(ai_provider_registry, "discover_all_models", fake_discover)
    for role in ("chat", "primary", "fast", "briefing"):
        target = asyncio.run(ai_provider_registry.resolve_target(role=role))
        assert target and target["model"] == "lfm3:1.7b", role
    # Even an explicit request for a decision model falls back to a chat model.
    target = asyncio.run(ai_provider_registry.resolve_target(role="chat", explicit_model="d1"))
    assert target["model"] == "lfm3:1.7b"
    assert ai_provider_registry.discovered_decision_models() == ["d1:2b"]


def test_v013_routing_migrates_legacy_primary_fast(tmp_path):
    import json

    from app.ai_providers import AIProviderRegistry

    path = tmp_path / "ai_providers.json"
    path.write_text(json.dumps({"routing": {"primary_model": "ollama-default::lfm3:1.7b", "fast_model": "qwen3.5:0.8b", "briefing_model": "auto"}}))
    routing = AIProviderRegistry(str(path)).load_state()["routing"]
    assert routing["chat_model"] == "ollama-default::lfm3:1.7b"
    assert routing["decision_model"] == "auto"
    assert not {"primary_model", "fast_model", "briefing_model"} & set(routing)

    # Primary on auto: the old fast pick becomes the chat model.
    path.write_text(json.dumps({"routing": {"primary_model": "auto", "fast_model": "qwen3.5:0.8b"}}))
    assert AIProviderRegistry(str(path)).load_state()["routing"]["chat_model"] == "qwen3.5:0.8b"

    # A decision model saved as the chat model is dropped back to auto.
    path.write_text(json.dumps({"routing": {"chat_model": "d1"}}))
    assert AIProviderRegistry(str(path)).load_state()["routing"]["chat_model"] == "auto"


def test_v013_routing_endpoint_and_status_shape(client: TestClient):
    bad = client.patch("/api/v1/models/routing", json={"chat_model": "d1:2b"})
    assert bad.status_code == 400

    ok = client.patch("/api/v1/models/routing", json={"chat_model": "auto", "decision_model": "d1:2b"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["routing"]["decision_model"] == "d1:2b"
    assert body["routing"]["chat_model"] == "auto"
    assert "primary_model" not in body["routing"] and "fast_model" not in body["routing"]

    st = client.get("/api/v1/models/status").json()
    assert st["chat_model"]["configured"] == "auto"
    assert st["decision_model"]["configured"] == "d1:2b"
    assert "role" in st["decision_model"] and "capabilities" in st["decision_model"]
    assert not any("von" in k for k in st)

    gate = client.get("/api/v1/decision-gate/status").json()
    assert gate["model"] == "d1:2b"
    assert gate["source"] == "ollama"  # no DECISION_BASE_URL: the decision model lives on Ollama

    # Old clients still sending primary_model are mapped onto chat_model.
    legacy = client.patch("/api/v1/models/routing", json={"primary_model": "auto", "decision_model": "auto"})
    assert legacy.status_code == 200 and legacy.json()["routing"]["chat_model"] == "auto"

    from app.config import settings

    assert not any(name.startswith("von") for name in type(settings).model_fields)


class _FakeSystemOne:
    """Minimal Ollama-like server: /v1/models + /v1/systemone (noul/choice only, optionally typed)."""

    def __init__(self, typed: bool):
        self.typed = typed
        self.requests = []

    def handler(self, request):
        import json

        import httpx

        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "lfm3:1.7b"}, {"id": "d1:2b"}]})
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        answers = {}
        for key, q in (body.get("questions") or {}).items():
            typed_q = q.get("multiple") or q.get("type") in ("text", "date", "number")
            if typed_q and not self.typed:
                return httpx.Response(400, json={"error": f"unsupported question type for {key}"})
            if q.get("type") == "noul":
                # "Promo" never applies; everything else does.
                answers[key] = {"type": "noul", "noul": 0.05 if "Promo" in q.get("instructions", "") else 0.9}
            elif q.get("multiple"):
                first = list(q["criteria"])[0]
                answers[key] = {"type": "choice", "choices": [first], "probabilities": {first: 0.9}}
            elif q.get("type") == "choice":
                names = list(q["criteria"])
                # Shape copied from a real Ollama d1 reply: shares sum to 1.
                rest = 0.08 / max(1, len(names) - 1)
                probs = {n: (0.92 if i == 0 else rest) for i, n in enumerate(names)}
                answers[key] = {"type": "choice", "choice": names[0], "probabilities": probs, "confidence": 0.88}
            elif q.get("type") == "date":
                answers[key] = {"type": "date", "date": "2026-03-14", "text": "2026-03-14", "found": 0.9}
            else:
                answers[key] = {"type": "text", "text": "Ms. Lee", "found": 0.9}
        return httpx.Response(200, json={"model": body.get("model"), "answers": answers})


def _patch_gate_transport(monkeypatch, fake):
    import httpx

    import app.decision_gate as dg

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(dg.httpx, "AsyncClient", client_factory)


@pytest.mark.parametrize("typed", [False, True])
def test_v013_gate_probe_detects_typed_answer_support(monkeypatch, typed):
    import asyncio

    from app.ai_providers import ai_provider_registry
    from app.decision_gate import decision_gate

    fake = _FakeSystemOne(typed=typed)
    _patch_gate_transport(monkeypatch, fake)
    monkeypatch.setattr(ai_provider_registry, "decision_model_preference", lambda: "auto")
    monkeypatch.setattr(ai_provider_registry, "_discovery_cache", [])
    decision_gate.invalidate()
    try:
        st = asyncio.run(decision_gate.status(force=True))
        assert st["online"] is True
        assert st["model"] == "d1:2b"  # found via /v1/models, not the chat model
        assert st["base_url"] == ai_provider_registry.default_ollama_base()
        caps = st["capabilities"]
        assert caps["noul"] and caps["choice"]
        assert caps["multiple"] is typed and caps["text"] is typed and caps["date"] is typed
    finally:
        decision_gate.invalidate()


@pytest.mark.parametrize("typed", [False, True])
def test_v013_tagging_degrades_to_per_tag_questions(monkeypatch, typed):
    """Without multi-select: one choice for the main tag, then yes/no on a few runners-up only."""
    import asyncio

    from app.ai_providers import ai_provider_registry
    from app.decision_gate import decision_gate, plan_tools, tag_notification

    fake = _FakeSystemOne(typed=typed)
    _patch_gate_transport(monkeypatch, fake)
    monkeypatch.setattr(ai_provider_registry, "decision_model_preference", lambda: "auto")
    decision_gate.invalidate()
    try:
        tags = [{"name": "School", "description": "Classes", "important": True}, {"name": "Promo", "description": "Marketing"}]
        tags += [{"name": f"Club {i}", "description": f"Club number {i}"} for i in range(20)]
        asyncio.run(decision_gate.status(force=True))  # probe first, so only tagging requests follow
        before = len(fake.requests)
        res = asyncio.run(
            tag_notification(source_app="Schoology", title="Lab report due Friday", body="Bring goggles", tags=tags)
        )
        assert res is not None and res["category"] == "School"
        sent_batches = [r["questions"] for r in fake.requests[before:] if "state" in r and r.get("questions")]
        first = sent_batches[0]
        if typed:
            assert first["tags"].get("multiple") is True and "clean_title" in first
            assert len(sent_batches) == 1
        else:
            assert "tags" not in first and "clean_title" not in first
            assert first["primary_category"]["type"] == "choice"
            assert not any(k.startswith("tag_") for k in first)
            # Second request: a handful of runners-up, not all 22 tags.
            assert len(sent_batches) == 2
            extra = sent_batches[1]
            assert 1 <= len(extra) <= 4 and all(q["type"] == "noul" for q in extra.values())
            assert "Promo" not in res["tags"]  # confirmed "no"

        plan = asyncio.run(plan_tools("add band practice to my calendar friday", tool_names=["create_calendar_event"]))
        assert plan is not None and "calendar" in plan["groups"]
        sent = fake.requests[-1]["questions"]
        assert ("event_title" in sent) is typed
        assert not any(q.get("multiple") for q in sent.values()) or typed
    finally:
        decision_gate.invalidate()


def test_v013_autosort_keeps_user_corrected_tags(client: TestClient):
    created = client.post(
        "/api/v1/notifications",
        json={
            "source_app": "University Mail",
            "title": "Your weekly coupon digest",
            "message": "Unsubscribe any time. 20% off sale this week.",
            "indicator_type": "dom_badge",
        },
    )
    assert created.status_code == 201
    nid = created.json()["id"]
    moved = client.post(f"/api/v1/inbox/{nid}/tags", json={"tag": "Professors", "learn": False})
    assert moved.status_code == 200 and moved.json()["notification"]["tag_corrected_by_user"] is True

    res = client.post("/api/v1/notifications/autosort")
    assert res.status_code == 200
    assert res.json()["engines_used"].get("user", 0) >= 1
    rows = client.get("/api/v1/notifications").json()
    rows = rows if isinstance(rows, list) else rows.get("notifications", rows.get("items", []))
    row = next(r for r in rows if r["id"] == nid)
    assert row["category"] == "Professors"


def test_v013_extra_tag_candidates_from_real_ollama_distribution():
    """Distribution copied from a real `ollama` d1 reply (choice, single)."""
    from app.decision_gate import _extra_tag_candidates

    dist = {"School": 0.0756, "Deadline": 0.9160, "Promo": 0.0044, "Social": 0.0041}
    names = ["School", "Deadline", "Promo", "Social"]
    cands = _extra_tag_candidates(names, "Deadline", dist)
    assert cands[0] == "School"  # the squeezed second tag is checked first
    assert "Deadline" not in cands and len(cands) <= 4

    many = {f"T{i}": 0.001 for i in range(40)}
    many.update({"Deadline": 0.8, "School": 0.12, "Work": 0.06})
    cands = _extra_tag_candidates(list(many), "Deadline", many)
    assert cands[:2] == ["School", "Work"] and len(cands) <= 4

    # No probabilities from the server: fall back to checking tags directly (bounded).
    assert len(_extra_tag_candidates([f"T{i}" for i in range(30)], "T0", {})) == 12


def test_v013_first_call_after_restart_probes_before_choosing_mode(monkeypatch):
    import asyncio

    from app.ai_providers import ai_provider_registry
    from app.decision_gate import decision_gate, tag_notification

    fake = _FakeSystemOne(typed=True)
    _patch_gate_transport(monkeypatch, fake)
    monkeypatch.setattr(ai_provider_registry, "decision_model_preference", lambda: "auto")
    decision_gate.invalidate()  # like a fresh start: nothing probed yet
    try:
        res = asyncio.run(tag_notification(source_app="Mail", title="Lab report due Friday", tags=[{"name": "School"}, {"name": "Promo"}]))
        assert res is not None
        tagging = [r["questions"] for r in fake.requests if "primary_category" in (r.get("questions") or {})]
        assert tagging and tagging[-1]["tags"].get("multiple") is True
    finally:
        decision_gate.invalidate()


# ---------------------------------------------------------------------------
# 0.14: ingest-time duplicate detection ("I see one message 5 times")
# ---------------------------------------------------------------------------
def _uniq() -> str:
    import uuid

    return "q" + uuid.uuid4().hex[:8]


def _post_notif(client: TestClient, title: str, message: str = "", app: str = "School Mail", indicator: str = "dom_badge"):
    res = client.post(
        "/api/v1/notifications",
        json={"source_app": app, "title": title, "message": message, "indicator_type": indicator},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _all_notifs(client: TestClient):
    rows = client.get("/api/v1/notifications").json()
    return rows if isinstance(rows, list) else rows.get("notifications", rows.get("items", []))


def test_v014_same_email_in_every_shape_is_one_row(client: TestClient):
    w = _uniq()  # unique word so other tests' rows can't match
    first = _post_notif(client, f"Lab report {w} due Friday", "Bring goggles to class")
    # JSON/websocket shape: sender glued to the subject
    b = _post_notif(client, f"Ms. Lee — Lab report {w} due Friday")
    # desktop pop-up shape: sender as title, subject in the body
    c = _post_notif(client, "Ms. Lee", f"Lab report {w} due Friday, bring goggles")
    # same email reported by another app with the service as sender
    d = _post_notif(client, "Schoology", f"Lab report {w} due Friday, bring goggles to class.", app="Schoology")
    assert b["id"] == c["id"] == d["id"] == first["id"]

    # Related but different emails stay separate.
    graded = _post_notif(client, f"Lab report {w} graded", "Your lab report received 92/100")
    trip = _post_notif(client, f"Field trip {w} permission slip due Friday", "Bring signed form")
    assert len({first["id"], graded["id"], trip["id"]}) == 3

    row = next(r for r in _all_notifs(client) if r["id"] == first["id"])
    assert row["merged_count"] == 4
    assert row["title"] == f"Lab report {w} due Friday"  # the real subject, not "Ms. Lee"
    assert "Schoology" in (row["dedupe_sources"] or "")


def test_v014_duplicate_does_not_resurrect_a_read_message(client: TestClient):
    w = _uniq()
    first = _post_notif(client, f"Chess club {w} meeting moved to Thursday", "Room 204 after school")
    assert client.patch(f"/api/v1/notifications/{first['id']}", json={"read_state": True}).status_code == 200
    again = _post_notif(client, f"Mr. Kim — Chess club {w} meeting moved to Thursday")
    assert again["id"] == first["id"] and again["read_state"] is True


def test_v014_counter_pings_update_one_row_per_app(client: TestClient):
    app_name = f"Pinger {_uniq()}"
    ids = {_post_notif(client, f"{app_name} ping", "Count updated (3).", app=app_name, indicator="ping_only")["id"] for _ in range(3)}
    assert len(ids) == 1


def test_v014_merge_keeps_user_tag_and_never_unions_tags(client: TestClient):
    from sqlmodel import Session

    from app.database import engine
    from app.inbox_ingest import merge_rows
    from app.models import Notification

    w = _uniq()
    with Session(engine) as s:
        a = Notification(source_app="Mail", title=f"Robotics {w} signup closes", tags_csv="Promo, Social", category="Promo")
        b = Notification(source_app="Mail", title=f"Robotics {w} signup closes", tags_csv="Clubs", category="Clubs", tag_corrected_by_user=True)
        c = Notification(source_app="Mail", title=f"Robotics {w} signup closes", tags_csv="Deadline, Finance", category="Deadline")
        s.add_all([a, b, c])
        s.commit()
        merge_rows(s, a, [b, c])
        s.commit()
        s.refresh(a)
        assert a.tags_csv == "Clubs" and a.category == "Clubs" and a.tag_corrected_by_user
        assert a.merged_count == 3

        d = Notification(source_app="Mail", title=f"Debate {w} tryouts", tags_csv="School", category="School", ai_processed=True)
        e = Notification(source_app="Mail", title=f"Debate {w} tryouts", tags_csv="Promo", category="Promo", ai_processed=True)
        s.add_all([d, e])
        s.commit()
        merge_rows(s, d, [e])
        s.commit()
        s.refresh(d)
        assert d.tags_csv == "School"  # not "School, Promo"
        assert d.ai_processed is False  # queued to be re-tagged from the merged content


def test_v014_dedupe_button_cleans_up_existing_copies(client: TestClient):
    from sqlmodel import Session

    from app.database import engine
    from app.models import Notification

    w = _uniq()
    with Session(engine) as s:  # rows stored before ingest matching existed
        rows = [
            Notification(source_app="Outlook", title=f"Band {w} concert tickets on sale", message="Order online by Monday"),
            Notification(source_app="Outlook", title=f"Ms. Diaz — Band {w} concert tickets on sale"),
            Notification(source_app="Schoology", title="Ms. Diaz", message=f"Band {w} concert tickets on sale, order online"),
        ]
        s.add_all(rows)
        s.commit()
        ids = [r.id for r in rows]
    preview = client.post("/api/v1/inbox/dedupe?dry_run=true").json()
    assert any(g["kept_id"] == ids[0] and set(g["merged_ids"]) == set(ids[1:]) for g in preview["groups"])
    res = client.post("/api/v1/inbox/dedupe").json()
    assert res["removed"] >= 2
    left = {r["id"] for r in _all_notifs(client)}
    assert ids[0] in left and not (set(ids[1:]) & left)


@pytest.mark.parametrize("prob,merged", [(0.95, True), (0.1, False)])
def test_v014_borderline_pairs_go_to_the_decision_model(monkeypatch, prob, merged):
    import asyncio

    from sqlmodel import Session

    import app.decision_gate as dg
    from app.database import engine
    from app.inbox_ingest import borderline_candidates, confirm_borderline_with_gate
    from app.models import Notification

    w = _uniq()
    with Session(engine) as s:
        # Shares the topic words (4 of 7) but not the rest: borderline, not certain.
        a = Notification(source_app="Mail", title=f"Spanish {w} essay draft feedback posted", message="Check comments before revising")
        b = Notification(source_app="Canvas", title=f"Spanish {w} essay due tomorrow", message="Submit draft on Canvas")
        s.add_all([a, b])
        s.commit()
        a_id, b_id = a.id, b.id
        assert [r.id for r in borderline_candidates(s, b)] == [a_id]  # similar, not certain

    seen = []

    async def fake_same(x, y):
        seen.append((x["subject"], y["subject"]))
        return prob

    monkeypatch.setattr(dg, "same_item_probability", fake_same)
    removed = asyncio.run(confirm_borderline_with_gate([b_id]))
    assert seen, "decision model was not asked"
    with Session(engine) as s:
        assert (s.get(Notification, b_id) is None) is merged
        assert s.get(Notification, a_id) is not None
    assert removed == (1 if merged else 0)


def test_v014_numbers_keep_similar_school_items_apart():
    from app.inbox_ingest import content_tokens, is_same

    a = content_tokens("Unit 5 quiz due tomorrow", "Algebra II, period 3", source_app="Schoology")
    b = content_tokens("Unit 6 quiz due tomorrow", "Algebra II, period 3", source_app="Schoology")
    assert not is_same(a, b)
    assert is_same(a, content_tokens("Mr. Ortiz — Unit 5 quiz due tomorrow", "", source_app="Schoology"))
