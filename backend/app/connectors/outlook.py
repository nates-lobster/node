"""
Outlook mail over OWA's own JSON API (``/owa/service.svc``)
==========================================================

Shapes come from the user's ``record.py`` capture
(docs/connectors/microsoft-capture-notes.md):

* ``FindConversation`` lists the inbox (one row per conversation, with topic,
  senders, preview, unread count, last delivery time). OWA sends the request
  JSON URL-encoded in the ``x-owa-urlpostdata`` header; we do the same.
* ``GetConversationItems`` returns the messages of one conversation, with
  ``UniqueBody`` (just the new part of each reply). Used to fill in bodies
  for newly arrived mail.

Auth is the bearer + routing headers minted by :mod:`.token_mint`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..config import settings
from .base import Connector, ConnectorError, ConnectorItem, FetchResult, html_to_text, parse_time, raise_for_status

PREFER = 'IdType="ImmutableId", exchange.behavior="IncludeThirdPartyOnlineMeetingProviders"'

# OWA's TimeZoneContext wants Windows zone ids.
_WINDOWS_TZ = {
    "America/New_York": "Eastern Standard Time",
    "America/Detroit": "Eastern Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "America/Indiana/Indianapolis": "US Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Phoenix": "US Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "UTC": "UTC",
    "Etc/UTC": "UTC",
}


def windows_timezone(iana: str) -> str:
    return _WINDOWS_TZ.get((iana or "").strip(), "UTC")


def _header(version: str) -> Dict[str, Any]:
    return {
        "__type": "JsonRequestHeaders:#Exchange",
        "RequestServerVersion": version,
        "TimeZoneContext": {
            "__type": "TimeZoneContext:#Exchange",
            "TimeZoneDefinition": {
                "__type": "TimeZoneDefinitionType:#Exchange",
                "Id": windows_timezone(settings.local_timezone),
            },
        },
    }


def find_conversation_request(max_entries: int = 25, *, shape_name: Optional[str] = "ReactConversationListView") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ParentFolderId": {
            "__type": "TargetFolderId:#Exchange",
            "BaseFolderId": {"__type": "DistinguishedFolderId:#Exchange", "Id": "inbox"},
        },
        "ConversationShape": {"__type": "ConversationResponseShape:#Exchange", "BaseShape": "IdOnly"},
        "Paging": {
            "__type": "IndexedPageView:#Exchange",
            "BasePoint": "Beginning",
            "Offset": 0,
            "MaxEntriesReturned": int(max_entries),
        },
        "ViewFilter": "All",
        "SortOrder": [
            {
                "__type": "SortResults:#Exchange",
                "Order": "Descending",
                "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": "ConversationLastDeliveryTime"},
            }
        ],
        "FocusedViewFilter": -1,
    }
    if shape_name:
        body["ShapeName"] = shape_name
    return {"__type": "FindConversationJsonRequest:#Exchange", "Header": _header("V2018_01_08"), "Body": body}


_ITEM_PROPS = ("Subject", "From", "Sender", "DateTimeReceived", "IsRead", "HasAttachments", "UniqueBody", "Preview")


def get_conversation_items_request(conversation_id: str, *, max_items: int = 5) -> Dict[str, Any]:
    return {
        "__type": "GetConversationItemsJsonRequest:#Exchange",
        "Header": _header("V2017_08_18"),
        "Body": {
            "__type": "GetConversationItemsRequest:#Exchange",
            "Conversations": [
                {
                    "__type": "ConversationRequestType:#Exchange",
                    "ConversationId": {"__type": "ItemId:#Exchange", "Id": conversation_id},
                    "SyncState": "",
                }
            ],
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
                "FilterHtmlContent": True,
                "BlockContentFromUnknownSenders": False,
                "MaximumBodySize": int(settings.inbox_body_max_chars) * 4,
                "BodyShape": "UniqueFragment",
                "AdditionalProperties": [{"__type": "PropertyUri:#Exchange", "FieldURI": f} for f in _ITEM_PROPS],
            },
            "ShapeName": "ItemPart",
            "SortOrder": "DateOrderDescending",
            "MaxItemsToReturn": int(max_items),
            "Action": "ReturnRootNode",
            "ReturnSubmittedItems": True,
            "ReturnDeletedItems": False,
        },
    }


def _mailbox_name(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    mb = obj.get("Mailbox") if isinstance(obj.get("Mailbox"), dict) else obj
    return str(mb.get("Name") or mb.get("EmailAddress") or "").strip()


def conversation_to_item(conv: Dict[str, Any], *, origin: str) -> Optional[ConnectorItem]:
    cid = ((conv.get("ConversationId") or {}).get("Id") or "").strip()
    if not cid:
        return None
    last = conv.get("LastDeliveryTime") or conv.get("LastDeliveryOrRenewTime") or ""
    senders = conv.get("UniqueSenders") or []
    sender = _mailbox_name(conv.get("LastSender")) or (str(senders[0]) if senders else "")
    item_ids = conv.get("ItemIds") or conv.get("GlobalItemIds") or []
    first_item = (item_ids[0] or {}).get("Id") if item_ids and isinstance(item_ids[0], dict) else ""
    url = f"{origin}/mail/inbox/id/{quote(first_item, safe='')}" if first_item else f"{origin}/mail/"
    return ConnectorItem(
        ext_id=f"{cid}@{last}",
        thread_id=cid,
        title=str(conv.get("ConversationTopic") or "(no subject)").strip()[:300],
        message=str(conv.get("Preview") or "").strip(),
        sender=sender,
        timestamp=parse_time(last),
        read=int(conv.get("UnreadCount") or 0) == 0,
        has_attachment=bool(conv.get("HasAttachments")),
        thread_count=int(conv.get("MessageCount") or 0),
        url=url,
        priority="high" if str(conv.get("Importance") or "").lower() == "high" else "normal",
    )


def body_from_conversation_items(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Newest message of a GetConversationItems response -> {body_html, sender, has_attachment, count}."""
    items = (((payload.get("Body") or {}).get("ResponseMessages") or {}).get("Items")) or []
    if not items:
        return {}
    first = items[0] or {}
    if str(first.get("ResponseClass") or "Success") == "Error":
        return {}
    nodes = ((first.get("Conversation") or {}).get("ConversationNodes")) or []
    msgs: List[Dict[str, Any]] = []
    for node in nodes:
        for it in (node or {}).get("Items") or []:
            if isinstance(it, dict):
                msgs.append(it)
    if not msgs:
        return {}
    msgs.sort(key=lambda m: str(m.get("DateTimeReceived") or ""), reverse=True)
    newest = msgs[0]
    body = newest.get("UniqueBody") or newest.get("Body") or {}
    html = body.get("Value") if isinstance(body, dict) else str(body or "")
    return {
        "body_html": html or newest.get("Preview") or "",
        "sender": _mailbox_name(newest.get("From")) or _mailbox_name(newest.get("Sender")),
        "has_attachment": any(bool(m.get("HasAttachments")) for m in msgs),
        "count": len(msgs),
    }


class OutlookMailConnector(Connector):
    key = "outlook-mail"
    label = "Outlook mail"
    auth_target = "outlook"
    page_size = 25
    max_bodies_per_run = 8

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._n = 0
        self._shape_name_ok: Optional[bool] = None

    async def _call(self, client: Any, auth: Any, action: str, payload: Dict[str, Any], *, in_header: bool) -> Dict[str, Any]:
        self._n += 1
        url = f"{auth.origin}/owa/service.svc?action={action}&app=Mail&n={self._n}"
        headers = dict(auth.headers)
        headers.update(
            {
                "action": action,
                "content-type": "application/json; charset=utf-8",
                "accept": "*/*",
                "prefer": PREFER,
                "x-req-source": headers.get("x-req-source") or "Mail",
                "x-owa-correlationid": str(uuid.uuid4()),
                "x-owa-actionsource": "Node",
            }
        )
        compact = json.dumps(payload, separators=(",", ":"))
        try:
            if in_header:
                headers["x-owa-urlpostdata"] = quote(compact, safe="")
                resp = await client.post(url, headers=headers, content=b"")
            else:
                resp = await client.post(url, headers=headers, content=compact.encode("utf-8"))
        except Exception as exc:
            raise ConnectorError("network", f"{action}: {exc.__class__.__name__}: {exc}") from exc
        raise_for_status(resp.status_code, action)
        try:
            data = resp.json()
        except Exception as exc:
            # OWA answers an expired session with an HTML sign-in page and 200.
            if "<html" in resp.text[:500].lower():
                raise ConnectorError("auth", f"{action}: got a sign-in page") from exc
            raise ConnectorError("shape", f"{action}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ConnectorError("shape", f"{action}: unexpected response")
        return data

    async def _find_conversations(self, client: Any, auth: Any) -> Dict[str, Any]:
        attempts = [None] if self._shape_name_ok is False else ["ReactConversationListView", None]
        last_exc: Optional[ConnectorError] = None
        for shape in attempts:
            try:
                data = await self._call(
                    client, auth, "FindConversation", find_conversation_request(self.page_size, shape_name=shape), in_header=True
                )
            except ConnectorError as exc:
                if exc.kind in ("auth", "network") and exc.status not in (500,):
                    raise
                last_exc = exc
                continue
            body = data.get("Body") or {}
            if str(body.get("ResponseClass") or "Success") == "Error" or "Conversations" not in body:
                last_exc = ConnectorError("shape", f"FindConversation: {body.get('ResponseCode') or 'no Conversations'}")
                continue
            if shape:
                self._shape_name_ok = True
            elif self._shape_name_ok is None:
                self._shape_name_ok = False
            return body
        raise last_exc or ConnectorError("shape", "FindConversation failed")

    async def fetch_since(self, cursor: Any, auth: Any) -> FetchResult:
        if auth is None:
            raise ConnectorError("not_configured", "no Outlook token")
        async with self.client() as client:
            body = await self._find_conversations(client, auth)
        items = [i for i in (conversation_to_item(c, origin=auth.origin) for c in body.get("Conversations") or []) if i]
        newest = max((i.timestamp for i in items if i.timestamp), default=None)
        prev = (cursor or {}).get("newest") if isinstance(cursor, dict) else None
        return FetchResult(
            items=items,
            cursor={"newest": newest.isoformat() if newest else prev},
            unread_count=sum(1 for i in items if not i.read),
        )

    async def enrich(self, items: List[ConnectorItem], auth: Any) -> List[ConnectorItem]:
        """Fetch bodies for new unread mail (a few per run; the rest keep their preview)."""
        targets = [i for i in items if not i.read][: self.max_bodies_per_run]
        if not targets or auth is None:
            return items
        async with self.client() as client:
            for it in targets:
                try:
                    data = await self._call(
                        client, auth, "GetConversationItems", get_conversation_items_request(it.thread_id), in_header=False
                    )
                except ConnectorError as exc:
                    if exc.kind == "auth":
                        break
                    continue
                info = body_from_conversation_items(data)
                text = html_to_text(info.get("body_html") or "")
                if text:
                    it.body_text = text[: int(settings.inbox_body_max_chars)]
                if info.get("sender"):
                    it.sender = info["sender"]
                if info.get("has_attachment"):
                    it.has_attachment = True
        return items
