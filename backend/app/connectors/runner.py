"""
Connector runner: which connectors exist, when they run, and their health
=========================================================================

Connectors are derived from the installed-apps registry: an enabled app whose
URL is Outlook on the web gets ``outlook-mail``; Teams gets
``teams-activity``, ``teams-chats`` and ``teams-assignments``. Nothing to
configure: installing the app (and signing in once in Node's browser) is
enough.

Each run: get a token (cached, or minted headless) → ``fetch_since(cursor)``
→ plan → ``enrich`` new items → write rows → save cursor + seen ids. On
a 401 the token is re-minted once. Failures back off:

* network: 5 min, doubling, max 6 h
* auth (still rejected after a fresh mint): 1 h, app shows "Sign-In Needed"
* unsupported (endpoint missing for this account): 24 h
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Type
from urllib.parse import urlparse

from ..apps_manager import AppsConfigManager
from ..config import settings
from .base import Connector, ConnectorError
from .ingest import mark_app_status, plan_items, write_items
from .outlook import OutlookMailConnector
from .state import ConnectorStateStore
from .teams import TeamsActivityConnector, TeamsAssignmentsConnector, TeamsChatsConnector
from .token_mint import TokenMint, token_mint

logger = logging.getLogger("node.connectors.runner")

OUTLOOK_HOSTS = ("outlook.cloud.microsoft", "outlook.office.com", "outlook.office365.com", "outlook.live.com")
TEAMS_HOSTS = ("teams.cloud.microsoft", "teams.microsoft.com", "teams.live.com")


def connector_classes_for_url(url: str) -> List[Type[Connector]]:
    host = urlparse(url or "").netloc.lower()
    if any(host == h or host.endswith("." + h) for h in OUTLOOK_HOSTS):
        return [OutlookMailConnector]
    if any(host == h or host.endswith("." + h) for h in TEAMS_HOSTS):
        classes: List[Type[Connector]] = [TeamsActivityConnector, TeamsChatsConnector]
        if not host.endswith("teams.live.com"):  # Assignments is school/work only
            classes.append(TeamsAssignmentsConnector)
        return classes
    return []


def backoff_seconds(kind: str, failures: int) -> float:
    if kind == "unsupported":
        return 24 * 3600
    if kind == "auth":
        return 3600
    if kind == "not_configured":
        return 6 * 3600
    return float(min(300 * 2 ** max(0, failures - 1), 6 * 3600))


class ConnectorRunner:
    def __init__(self, *, store: Optional[ConnectorStateStore] = None, mint: Optional[TokenMint] = None) -> None:
        self.store = store or ConnectorStateStore()
        self.mint = mint or token_mint
        self._apps = AppsConfigManager()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._task: Optional[asyncio.Task] = None
        # Tests can pin the connector list instead of reading the registry.
        self.override: Optional[List[Connector]] = None

    # ------------------------------------------------------------------ registry
    def connectors(self) -> List[Connector]:
        if self.override is not None:
            return list(self.override)
        try:
            _, apps = self._apps.get_global_and_apps()
        except Exception as exc:
            logger.debug("apps registry unreadable: %s", exc)
            return []
        out: List[Connector] = []
        for app in apps:
            if not app.get("enabled", True) or app.get("zero_knowledge"):
                continue
            for cls in connector_classes_for_url(str(app.get("url") or "")):
                conn = cls(app_name=app["name"], app_url=app["url"], profile=str(app.get("account_profile") or "default"))
                conn.key = f"{app['slug']}.{cls.key}"
                out.append(conn)
        return out

    def get(self, key: str) -> Optional[Connector]:
        return next((c for c in self.connectors() if c.key == key), None)

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _poll_seconds(self, conn: Connector) -> int:
        return int(conn.poll_seconds or settings.connector_poll_seconds)

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        now = time.time()
        rows = []
        for conn in self.connectors():
            st = self.store.get(conn.key)
            cached = self.mint.cached(conn.auth_target, conn.profile) if conn.auth_target else None
            health = "ok" if st["last_ok"] and not st["failures"] else ("error" if st["failures"] else "pending")
            if st["error_kind"] == "auth" and st["failures"]:
                health = "sign_in_needed"
            if not st["enabled"]:
                health = "disabled"
            rows.append(
                {
                    **conn.describe(),
                    "key": conn.key,
                    "enabled": bool(st["enabled"]),
                    "health": health,
                    "last_run": st["last_run"],
                    "last_ok": st["last_ok"],
                    "last_error": st["last_error"],
                    "error_kind": st["error_kind"],
                    "failures": st["failures"],
                    "next_run_in_seconds": max(
                        0, int(max(st["last_run"] + self._poll_seconds(conn), st["backoff_until"]) - now)
                    ),
                    "poll_seconds": self._poll_seconds(conn),
                    "created_total": st["created_total"],
                    "last_counts": st["last_counts"],
                    "seen_count": len(st["seen"]),
                    "notes": st["notes"],
                    "token": cached.describe() if cached else None,
                }
            )
        return {"enabled": bool(settings.connectors_enabled), "connectors": rows, "mint": self.mint.describe()}

    def set_enabled(self, key: str, enabled: bool) -> Dict[str, Any]:
        if self.get(key) is None:
            raise KeyError(key)
        patch: Dict[str, Any] = {"enabled": bool(enabled)}
        if enabled:
            patch.update(backoff_until=0.0, failures=0)
        return self.store.update(key, **patch)

    # ------------------------------------------------------------------ running
    async def _auth(self, conn: Connector, *, force: bool = False) -> Any:
        if not conn.auth_target:
            return None
        return await self.mint.get(conn.auth_target, app_url=conn.app_url, profile=conn.profile, force=force)

    async def run_one(self, key: str, *, force: bool = False) -> Dict[str, Any]:
        conn = self.get(key)
        if conn is None:
            return {"ok": False, "key": key, "error": "unknown connector"}
        async with self._lock(key):
            st = self.store.get(key)
            now = time.time()
            if not force and (not st["enabled"] or now < st["backoff_until"]):
                return {"ok": False, "key": key, "skipped": True}
            st["last_run"] = now
            try:
                auth = await self._auth(conn)
                try:
                    result = await conn.fetch_since(st["cursor"], auth)
                except ConnectorError as exc:
                    if exc.kind != "auth" or not conn.auth_target or getattr(auth, "minted_at", 0) > now:
                        raise
                    # The cached token was rejected: mint a fresh one and retry once.
                    self.mint.invalidate(conn.auth_target, conn.profile)
                    auth = await self._auth(conn, force=True)
                    result = await conn.fetch_since(st["cursor"], auth)

                seen = set(st["seen"])
                first_run = st["cursor"] is None and not seen
                create, known = plan_items(result.items, seen, first_run=first_run)
                if create:
                    create = await conn.enrich(create, auth)
                counts = write_items(
                    connector_key=key, source_app=conn.app_name, app_url=conn.app_url, create=create, known=known
                )
                if result.tasks:
                    from ..lms_connector import sync_lms_assignments_to_tasks

                    counts["tasks"] = sync_lms_assignments_to_tasks(result.tasks, source_app=conn.app_name)
            except ConnectorError as exc:
                return self._record_failure(conn, st, exc)
            except Exception as exc:  # a bug, not the service: report it, back off like a network error
                logger.exception("connector %s crashed", key)
                return self._record_failure(conn, st, ConnectorError("network", f"{exc.__class__.__name__}: {exc}"))

            seen_list = list(st["seen"])
            for it in result.items:
                if it.ext_id not in seen:
                    seen_list.append(it.ext_id)
                    seen.add(it.ext_id)
            st.update(
                cursor=result.cursor if result.cursor is not None else {},
                seen=seen_list,
                last_ok=time.time(),
                last_error="",
                error_kind="",
                failures=0,
                backoff_until=0.0,
                created_total=int(st["created_total"]) + int(counts.get("created", 0)),
                last_counts={k: v for k, v in counts.items() if k != "ids"},
                notes=list(result.notes or [])[:5],
            )
            self.store.put(key, st)
            return {"ok": True, "key": key, **st["last_counts"], "unread": result.unread_count}

    def _record_failure(self, conn: Connector, st: Dict[str, Any], exc: ConnectorError) -> Dict[str, Any]:
        failures = int(st["failures"]) + 1
        wait = backoff_seconds(exc.kind, failures)
        st.update(
            failures=failures,
            last_error=str(exc)[:400],
            error_kind=exc.kind,
            backoff_until=time.time() + wait,
        )
        self.store.put(conn.key, st)
        if exc.kind == "auth":
            try:
                mark_app_status(conn.app_name, "auth_required", conn.app_url)
            except Exception:
                pass
        logger.info("connector %s failed (%s): %s — retry in %ds", conn.key, exc.kind, exc, int(wait))
        return {"ok": False, "key": conn.key, "error": str(exc), "error_kind": exc.kind, "retry_in_seconds": int(wait)}

    async def run_due(self) -> List[Dict[str, Any]]:
        out = []
        now = time.time()
        for conn in self.connectors():
            st = self.store.get(conn.key)
            if not st["enabled"]:
                continue
            if now < max(st["last_run"] + self._poll_seconds(conn), st["backoff_until"]):
                continue
            out.append(await self.run_one(conn.key))
        return out

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # let startup finish first
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("connector loop error: %s", exc)
            await asyncio.sleep(60)

    def start(self) -> None:
        if not settings.connectors_enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None


connector_runner = ConnectorRunner()
