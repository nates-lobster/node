"""
Microsoft Teams over its own web APIs
=====================================

Three connectors, all from the user's ``record.py`` capture
(docs/connectors/microsoft-capture-notes.md):

* :class:`TeamsActivityConnector` reads the activity feed — the
  ``48:notifications`` conversation on the chat service. Each message is one
  feed entry (mention, reply, reaction, assignment post …) with the sender,
  a preview and the source chat/channel topic.
* :class:`TeamsChatsConnector` (experimental) lists recent chats and turns
  each unread last message into an Inbox row. The list call wasn't in the
  capture; it uses the chat service's standard conversation-list endpoint.
* :class:`TeamsAssignmentsConnector` reads Teams Assignments
  (``assignments.edu.cloud.microsoft/api/v1.0/edu``), files unsubmitted work
  into Tasks and posts new assignments to the Inbox.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..config import settings
from .base import Connector, ConnectorError, ConnectorItem, FetchResult, html_to_text, parse_time, raise_for_status

ACTIVITY_FEED_ID = "48:notifications"

# Feed entry types worth an Inbox row, with a readable verb.
_ACTIVITY_LABELS = {
    "mention": "Mentioned you",
    "mentionInChat": "Mentioned you",
    "teamMention": "Mentioned your team",
    "channelMention": "Mentioned the channel",
    "reply": "Replied",
    "replyToReply": "Replied",
    "message": "New message",
    "call": "Call",
    "missedCall": "Missed call",
    "meetingInvite": "Meeting invite",
    "addedToTeam": "Added you to a team",
    "teamMembershipChange": "Team membership",
    "assignment": "Assignment",
}
# Noise we don't import (they still count as "seen").
_SKIP_ACTIVITY = {"reaction", "like", "heart", "follow", "trending"}

_CHATSVC_BASE = re.compile(r"^(https://[^/]+/api/chatsvc/[^/]+/v1)/")


def chatsvc_base(auth_url: str) -> str:
    m = _CHATSVC_BASE.match(auth_url or "")
    if not m:
        raise ConnectorError("not_configured", "token wasn't captured from a chat-service request")
    return m.group(1)


async def _get_json(client: Any, url: str, headers: Dict[str, str], where: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        resp = await client.get(url, headers=headers, params=params)
    except Exception as exc:
        raise ConnectorError("network", f"{where}: {exc.__class__.__name__}: {exc}") from exc
    raise_for_status(resp.status_code, where)
    try:
        return resp.json()
    except Exception as exc:
        raise ConnectorError("shape", f"{where}: response is not JSON") from exc


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() == "true"


def activity_to_item(msg: Dict[str, Any], *, app_url: str) -> Optional[ConnectorItem]:
    props = msg.get("properties") or {}
    act = props.get("activity") or {}
    if not isinstance(act, dict) or not act:
        return None
    a_type = str(act.get("activityType") or "").strip()
    mid = str(msg.get("id") or act.get("activityId") or "").strip()
    if not mid:
        return None
    sender = str(act.get("sourceUserImDisplayName") or msg.get("imdisplayname") or "").strip()
    preview = html_to_text(str(act.get("messagePreview") or msg.get("content") or ""))
    topic = str(act.get("sourceThreadTopic") or "").strip()
    verb = _ACTIVITY_LABELS.get(a_type, a_type[:1].upper() + a_type[1:] if a_type else "Activity")
    title = f"{verb} in {topic}" if topic else (f"{sender}: {preview[:120]}" if sender and preview else verb)
    item = ConnectorItem(
        ext_id=f"feed:{mid}",
        thread_id=f"feed:{mid}",
        title=title[:300],
        message=preview,
        body_text=preview,
        sender=sender,
        timestamp=parse_time(msg.get("originalarrivaltime") or act.get("activityTimestamp") or msg.get("composetime")),
        read=_truthy(props.get("isread")),
        url=app_url,
        priority="high" if a_type.lower().startswith(("mention", "teammention")) else "normal",
    )
    item.kind = "skip" if a_type.lower() in _SKIP_ACTIVITY else "unread_message"
    return item


class TeamsActivityConnector(Connector):
    key = "teams-activity"
    label = "Teams activity feed"
    auth_target = "teams-chatsvc"

    async def fetch_since(self, cursor: Any, auth: Any) -> FetchResult:
        if auth is None:
            raise ConnectorError("not_configured", "no Teams token")
        base = chatsvc_base(auth.url)
        url = f"{base}/users/ME/conversations/{ACTIVITY_FEED_ID}/messages"
        params = {"view": "msnp24Equivalent|supportsMessageProperties", "pageSize": 50, "startTime": 1}
        headers = {**auth.headers, "accept": "application/json"}
        async with self.client() as client:
            data = await _get_json(client, url, headers, "activity feed", params)
        msgs = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(msgs, list):
            raise ConnectorError("shape", "activity feed: no messages[]")
        items = [i for i in (activity_to_item(m, app_url=self.app_url) for m in msgs if isinstance(m, dict)) if i]
        keep = [i for i in items if i.kind != "skip"]
        return FetchResult(
            items=keep,
            cursor={},
            unread_count=sum(1 for i in keep if not i.read),
        )


def _horizon_ts(props: Dict[str, Any]) -> int:
    raw = str((props or {}).get("consumptionhorizon") or "")
    head = raw.split(";", 1)[0]
    return int(head) if head.isdigit() else 0


def chat_to_item(conv: Dict[str, Any], *, my_oid: str, app_url: str) -> Optional[ConnectorItem]:
    cid = str(conv.get("id") or "")
    if not cid or cid.startswith("48:"):
        return None  # system streams (activity feed, calllogs, …)
    tp = conv.get("threadProperties") or {}
    thread_type = str(tp.get("threadType") or "").lower()
    if thread_type in ("space", "topic"):
        return None  # team channels: covered by the activity feed (mentions/replies)
    last = conv.get("lastMessage") or {}
    mtype = str(last.get("messagetype") or "")
    if not last or not (mtype.startswith("RichText") or mtype == "Text"):
        return None
    sender_ref = str(last.get("from") or "")
    if my_oid and sender_ref.lower().endswith(my_oid.lower()):
        return None  # my own message
    mid = str(last.get("id") or "")
    msg_ts = int(mid) if mid.isdigit() else 0
    unread = msg_ts > _horizon_ts(conv.get("properties") or {}) if msg_ts else False
    sender = str(last.get("imdisplayname") or "").strip()
    topic = str(tp.get("topic") or "").strip()
    text = html_to_text(str(last.get("content") or ""))
    if not text:
        return None
    return ConnectorItem(
        ext_id=f"chat:{cid}:{mid}",
        thread_id=f"chat:{cid}",
        title=(topic or sender or "Teams chat")[:300],
        message=text,
        body_text=text,
        sender=sender,
        timestamp=parse_time(last.get("originalarrivaltime") or last.get("composetime")),
        read=not unread,
        url=app_url,
    )


class TeamsChatsConnector(Connector):
    key = "teams-chats"
    label = "Teams chats"
    auth_target = "teams-chatsvc"
    experimental = True

    async def fetch_since(self, cursor: Any, auth: Any) -> FetchResult:
        if auth is None:
            raise ConnectorError("not_configured", "no Teams token")
        base = chatsvc_base(auth.url)
        params = {
            "view": "msnp24Equivalent",
            "pageSize": 30,
            "targetType": "Passport|Skype|Lync|Thread|Agent|ShortCircuit|PSTN|Flxt|NotificationStream|ModernBots|secureThreads|InteropChat",
        }
        headers = {**auth.headers, "accept": "application/json"}
        async with self.client() as client:
            data = await _get_json(client, f"{base}/users/ME/conversations", headers, "chat list", params)
        convs = data.get("conversations") if isinstance(data, dict) else None
        if not isinstance(convs, list):
            raise ConnectorError("shape", "chat list: no conversations[]")
        my_oid = str((auth.claims or {}).get("oid") or "")
        items = [i for i in (chat_to_item(c, my_oid=my_oid, app_url=self.app_url) for c in convs if isinstance(c, dict)) if i]
        return FetchResult(items=items, cursor={}, unread_count=sum(1 for i in items if not i.read))


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------
_DONE_SUBMISSION = {"submitted", "returned", "excused"}


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.local_timezone)
    except Exception:
        return ZoneInfo("UTC")


def _fmt_due(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{dt:%a %b} {dt.day}, {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _edu_base(auth_url: str) -> str:
    m = re.match(r"^(https://[^/]+/api/v1\.0/edu)/", auth_url or "")
    if not m:
        raise ConnectorError("not_configured", "token wasn't captured from an Assignments request")
    return m.group(1)


def assignment_rows(payload: Any) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Normalize `me/work` / `me/assignments` rows into (assignment, submissions)."""
    rows = payload.get("value") if isinstance(payload, dict) else None
    out: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        a = row.get("assignment") if isinstance(row.get("assignment"), dict) else row
        subs = row.get("submissions") or a.get("submissions") or []
        if isinstance(row.get("submission"), dict):
            subs = [row["submission"], *subs]
        out.append((a, [s for s in subs if isinstance(s, dict)]))
    return out


def assignment_state(a: Dict[str, Any], subs: List[Dict[str, Any]]) -> str:
    """'done' | 'open'."""
    statuses = {str(s.get("status") or "").lower() for s in subs}
    if statuses & _DONE_SUBMISSION or _truthy(a.get("isCompleted")):
        return "done"
    return "open"


def assignments_to_results(
    pairs: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]],
    class_names: Dict[str, str],
    *,
    app_url: str,
    now: Optional[datetime] = None,
) -> Tuple[List[ConnectorItem], List[Dict[str, Any]]]:
    now = now or datetime.now(timezone.utc)
    tz = _local_tz()
    items: List[ConnectorItem] = []
    tasks: List[Dict[str, Any]] = []
    for a, subs in pairs:
        aid = str(a.get("id") or "")
        name = str(a.get("displayName") or "").strip()
        if not aid or not name:
            continue
        due = parse_time(a.get("dueDateTime"))
        cls = class_names.get(str(a.get("classId") or ""), "")
        done = assignment_state(a, subs) == "done"
        overdue = bool(due and due < now and not done)
        if due and not done and due > now - timedelta(days=21):
            tasks.append(
                {
                    "title": name,
                    "course_hint": cls,
                    "due_date": due.astimezone(tz).date().isoformat(),
                    "status": "overdue" if overdue else "upcoming",
                }
            )
        if due and due < now - timedelta(days=14):
            continue  # old: don't post to the Inbox
        due_txt = _fmt_due(due.astimezone(tz)) if due else "no due date"
        soon = bool(due and not done and due - now < timedelta(hours=24))
        msg = f"{'Overdue' if overdue else 'Due'} {due_txt}" + (f" · {cls}" if cls else "")
        items.append(
            ConnectorItem(
                ext_id=f"asg:{aid}",
                thread_id=f"asg:{aid}",
                title=name[:300],
                message=msg,
                sender=cls,
                timestamp=parse_time(a.get("assignedDateTime") or a.get("createdDateTime")) or now,
                read=done,
                kind="active_assignment",
                url=str(a.get("webUrl") or app_url),
                priority="high" if (soon or overdue) else "normal",
            )
        )
    return items, tasks


class TeamsAssignmentsConnector(Connector):
    key = "teams-assignments"
    label = "Teams Assignments"
    auth_target = "teams-assignments"

    @property
    def poll_seconds(self) -> int:  # type: ignore[override]
        return int(settings.connector_assignments_poll_seconds)

    async def fetch_since(self, cursor: Any, auth: Any) -> FetchResult:
        if auth is None:
            raise ConnectorError("not_configured", "no Assignments token")
        base = _edu_base(auth.url)
        headers = {**auth.headers, "accept": "application/json, text/plain, */*", "prefer": "AssignmentStatusV2"}
        notes: List[str] = []
        class_names: Dict[str, str] = dict((cursor or {}).get("classes") or {}) if isinstance(cursor, dict) else {}
        async with self.client() as client:
            if not class_names:
                try:
                    data = await _get_json(client, f"{base}/me/classes", headers, "classes", {"$select": "id,displayName"})
                    for c in (data or {}).get("value") or []:
                        if isinstance(c, dict) and c.get("id"):
                            class_names[str(c["id"])] = str(c.get("displayName") or "")
                except ConnectorError as exc:
                    if exc.kind == "auth":
                        raise
                    notes.append(f"class names unavailable ({exc})")
            payload = None
            last_exc: Optional[ConnectorError] = None
            for path, params in (
                ("me/work", {"$top": 100, "$orderby": "dueDateTime desc", "$expand": "submissions"}),
                ("me/assignments", {"$top": 100, "$expand": "submissions"}),
            ):
                try:
                    payload = await _get_json(client, f"{base}/{path}", headers, path, params)
                    break
                except ConnectorError as exc:
                    if exc.kind == "auth":
                        raise
                    last_exc = exc
            if payload is None:
                raise last_exc or ConnectorError("shape", "no assignments endpoint answered")
        items, tasks = assignments_to_results(assignment_rows(payload), class_names, app_url=self.app_url)
        return FetchResult(
            items=items,
            tasks=tasks,
            cursor={"classes": class_names},
            unread_count=sum(1 for i in items if not i.read),
            notes=notes,
        )
