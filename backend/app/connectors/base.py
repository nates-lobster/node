"""
Connector contract
==================

A connector fetches one source over plain HTTP and returns normalized items.
It never touches the database: :mod:`.ingest` turns items into Inbox rows and
:mod:`.runner` owns scheduling, health and backoff.

    class MyConnector(Connector):
        key = "outlook-mail"
        async def fetch_since(self, cursor, auth) -> FetchResult: ...

``cursor`` is whatever the connector returned last time (JSON-serializable,
``None`` on the first run). ``auth`` is the :class:`~.token_mint.CapturedAuth`
the runner minted for ``auth_target`` (``None`` for connectors that need none).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx


class ConnectorError(Exception):
    """A fetch failed. ``kind`` drives the runner's reaction.

    * ``auth``        — token rejected (401/403): re-mint once, then "sign-in needed".
    * ``network``     — timeout / 5xx / DNS: back off and retry.
    * ``shape``       — the service answered with something we can't parse.
    * ``unsupported`` — the endpoint doesn't exist for this account (404/400):
                        back off for a long time.
    * ``not_configured`` — missing app / profile / token target.
    """

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class ConnectorItem:
    ext_id: str  # stable id of this *version* of the item (new reply = new id)
    title: str
    message: str = ""
    sender: str = ""
    body_text: str = ""
    timestamp: Optional[datetime] = None
    read: bool = False
    has_attachment: bool = False
    thread_count: int = 0
    kind: str = "unread_message"  # | active_assignment
    url: str = ""
    # Shared by every version of the same thread/conversation, so a read in
    # the source can mark the row read after the version id changed.
    thread_id: str = ""
    priority: str = "normal"


@dataclass
class FetchResult:
    items: List[ConnectorItem] = field(default_factory=list)
    cursor: Any = None
    unread_count: Optional[int] = None
    # Assignment-style rows for lms_connector.sync_lms_assignments_to_tasks.
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class Connector:
    key: str = ""
    label: str = ""
    # Token target this connector needs (see token_mint.MINT_TARGETS), or "".
    auth_target: str = ""
    poll_seconds: Optional[int] = None  # None = settings.connector_poll_seconds
    experimental: bool = False
    # Tests inject an httpx.MockTransport here.
    transport: Any = None

    def __init__(self, *, app_name: str, app_url: str, profile: str = "default") -> None:
        self.app_name = app_name
        self.app_url = app_url
        self.profile = profile or "default"

    async def fetch_since(self, cursor: Any, auth: Any) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def enrich(self, items: List[ConnectorItem], auth: Any) -> List[ConnectorItem]:
        """Optional second pass over the items about to be *created* (e.g. fetch bodies)."""
        return items

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20.0, transport=self.transport, follow_redirects=False)

    def describe(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "app": self.app_name,
            "auth_target": self.auth_target,
            "experimental": self.experimental,
        }


def raise_for_status(status: int, where: str) -> None:
    """Map an HTTP status to a ConnectorError (no-op for 2xx)."""
    if 200 <= status < 300:
        return
    if status in (401, 403, 440):  # 440 = OWA "login timeout"
        raise ConnectorError("auth", f"{where}: HTTP {status}", status=status)
    if status in (400, 404, 405, 410):
        raise ConnectorError("unsupported", f"{where}: HTTP {status}", status=status)
    raise ConnectorError("network", f"{where}: HTTP {status}", status=status)


def parse_time(raw: Any) -> Optional[datetime]:
    """ISO-8601 (with Z, offset or 7-digit fractions) or epoch ms -> aware UTC datetime."""
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
            val = float(raw)
            return datetime.fromtimestamp(val / 1000.0 if val > 1e11 else val, tz=timezone.utc)
        txt = str(raw).strip().replace("Z", "+00:00")
        # Python 3.11 accepts at most 6 fractional digits.
        if "." in txt:
            head, rest = txt.split(".", 1)
            digits = ""
            while rest and rest[0].isdigit():
                digits, rest = digits + rest[0], rest[1:]
            txt = f"{head}.{digits[:6]}{rest}" if digits else head + rest
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def html_to_text(raw: str) -> str:
    """Readable plain text from an email/chat HTML fragment (keeps line breaks)."""
    import html as _html
    import re as _re

    if not raw:
        return ""
    txt = _re.sub(r"(?is)<(style|script|head|title)[^>]*>.*?</\1>", " ", raw)
    txt = _re.sub(r"(?is)<!--.*?-->", " ", txt)
    txt = _re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = _re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", txt)
    txt = _re.sub(r"<[^>]+>", " ", txt)
    txt = _html.unescape(txt).replace("\u00a0", " ")
    lines = [" ".join(line.split()) for line in txt.splitlines()]
    out: List[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
