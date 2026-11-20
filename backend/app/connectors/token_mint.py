"""
Token mint: borrow a signed request from the server's own browser profile
========================================================================

Outlook and Teams call their APIs with short-lived MSAL bearer tokens, not
cookies (docs/connectors/microsoft-capture-notes.md). Microsoft blocks student
accounts from registering their own app, so instead of an OAuth flow the
server does what the web app does:

1. open the app in the server's existing Playwright profile (headless, the
   same profile the user signed into through Node's browser viewer);
2. wait for the first request that matches the target (e.g. an OWA
   ``service.svc`` call) and copy its replayable headers — ``authorization``
   plus the routing headers (``x-anchormailbox``, ``x-owa-sessionid`` …);
3. close the page.

The headers live in memory only (never on disk) until shortly before the JWT's
``exp``. Then connectors talk plain HTTP and no tab stays open.

``put_external`` lets another source hand over headers (the browser extension
tap planned as "option B", and the tests).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

from ..config import settings
from .base import ConnectorError

logger = logging.getLogger("node.connectors.mint")

# Re-mint this long before the token's own expiry.
EXPIRY_MARGIN_SECONDS = 180
# When the token isn't a readable JWT, assume this lifetime.
DEFAULT_TOKEN_TTL_SECONDS = 45 * 60

# Request headers that are per-request or describe the body; never replayed.
_DROP_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "connection",
    "accept-encoding",
    "action",
    "x-owa-urlpostdata",
    "x-owa-correlationid",
    "x-owa-actionsource",
    "x-owa-priority",
    "ms-cv",
    "traceparent",
    "tracestate",
    "request-id",
    "client-request-id",
    "x-ms-client-request-id",
    "x-correlationid",
    "x-ms-request-priority",
}

_LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")


@dataclass(frozen=True)
class MintTarget:
    key: str
    label: str
    # Page to open, relative to the app's own URL (absolute URLs are used as-is).
    open_path: str
    # A request whose URL matches this (and carries `authorization`) is captured.
    url_pattern: str

    def open_url(self, app_url: str) -> str:
        path = self.open_path.format(assignments_app_id=settings.teams_assignments_app_id)
        if path.startswith("http"):
            return path
        parsed = urlparse(app_url or "")
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        return urljoin(origin + "/", path.lstrip("/"))


MINT_TARGETS: Dict[str, MintTarget] = {
    "outlook": MintTarget(
        key="outlook",
        label="Outlook (OWA service.svc)",
        open_path="/mail/",
        url_pattern=r"/owa/service\.svc\?action=",
    ),
    "teams-chatsvc": MintTarget(
        key="teams-chatsvc",
        label="Teams chat service",
        open_path="/",
        url_pattern=r"/api/chatsvc/[^/]+/v1/",
    ),
    "teams-assignments": MintTarget(
        key="teams-assignments",
        label="Teams Assignments",
        # Teams' documented app deep link; the Assignments app runs in a frame
        # on assignments.edu.cloud.microsoft and calls its own API from there.
        open_path="/l/app/{assignments_app_id}",
        url_pattern=r"//assignments\.[^/]+/api/v1\.0/edu/",
    ),
}


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Read a JWT payload without verifying it (we only need exp/aud/oid)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return {}


def replayable_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk.startswith(":") or lk in _DROP_HEADERS:
            continue
        out[lk] = str(v)
    return out


@dataclass
class CapturedAuth:
    target: str
    url: str
    headers: Dict[str, str]
    minted_at: float
    expires_at: float
    source: str = "mint"
    claims: Dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN_SECONDS

    @property
    def origin(self) -> str:
        p = urlparse(self.url)
        return f"{p.scheme}://{p.netloc}"

    def describe(self) -> Dict[str, Any]:
        """Safe summary for the UI: never includes header values."""
        return {
            "target": self.target,
            "source": self.source,
            "origin": self.origin,
            "audience": self.claims.get("aud"),
            "minted_at": self.minted_at,
            "expires_at": self.expires_at,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
            "header_names": sorted(self.headers.keys()),
        }


def build_auth(target: str, url: str, headers: Dict[str, str], *, source: str, cookie_header: str = "") -> CapturedAuth:
    clean = replayable_headers(headers)
    if cookie_header and "cookie" not in clean:
        clean["cookie"] = cookie_header
    bearer = clean.get("authorization", "")
    token = bearer.split(" ", 1)[1] if bearer.lower().startswith("bearer ") else bearer
    claims_all = decode_jwt_claims(token)
    claims = {k: claims_all[k] for k in ("aud", "oid", "tid", "exp", "scp") if k in claims_all}
    now = time.time()
    exp = float(claims.get("exp") or 0) or (now + DEFAULT_TOKEN_TTL_SECONDS)
    return CapturedAuth(
        target=target, url=url, headers=clean, minted_at=now, expires_at=exp, source=source, claims=claims
    )


def _is_login_url(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(host.endswith(h) for h in _LOGIN_HOSTS)


class _SharedWorkerWatch:
    """Watches SharedWorkers over CDP, which Playwright doesn't report.

    Outlook and Teams can run API calls in SharedWorkers. Every second it
    attaches to new shared workers (non-flattened, as in tools/api-recorder)
    and enables their Network domain; ``on_match(url, headers)`` gets the
    first signed request that matches.
    """

    def __init__(self, pattern: "re.Pattern[str]", on_match: Any) -> None:
        self.pattern = pattern
        self.on_match = on_match
        self.session: Any = None
        self.attached: Dict[str, str] = {}
        self.task: Optional[asyncio.Task] = None
        self._next_id = 0

    async def start(self, ctx: Any, page: Any) -> None:
        try:
            self.session = await ctx.new_cdp_session(page)
        except Exception as exc:  # non-Chromium engine: page/frame/worker events still work
            logger.debug("shared-worker watch unavailable: %s", exc)
            return
        self.session.on("Target.receivedMessageFromTarget", self._on_message)
        self.task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.session is not None:
            try:
                await self.session.detach()
            except Exception:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                infos = (await self.session.send("Target.getTargets"))["targetInfos"]
                for t in infos:
                    if t.get("type") != "shared_worker" or t["targetId"] in self.attached:
                        continue
                    res = await self.session.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": False})
                    sid = res["sessionId"]
                    self.attached[t["targetId"]] = sid
                    self._next_id += 1
                    msg = json.dumps({"id": self._next_id, "method": "Network.enable", "params": {}})
                    await self.session.send("Target.sendMessageToTarget", {"sessionId": sid, "message": msg})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("shared-worker watch: %s", exc)
            await asyncio.sleep(1.0)

    def _on_message(self, event: Dict[str, Any]) -> None:
        try:
            msg = json.loads(event.get("message") or "{}")
        except ValueError:
            return
        if msg.get("method") not in ("Network.requestWillBeSent", "Network.requestWillBeSentExtraInfo"):
            return
        params = msg.get("params") or {}
        req = params.get("request") or {}
        url = req.get("url") or ""
        headers = req.get("headers") or params.get("headers") or {}
        if url and self.pattern.search(url) and any(k.lower() == "authorization" for k in headers):
            self.on_match(url, dict(headers))


class TokenMint:
    def __init__(self) -> None:
        self._cache: Dict[Tuple[str, str], CapturedAuth] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.last_error: Dict[Tuple[str, str], str] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def cached(self, target: str, profile: str = "default") -> Optional[CapturedAuth]:
        auth = self._cache.get((target, profile or "default"))
        return auth if auth and not auth.expired else None

    def invalidate(self, target: str, profile: str = "default") -> None:
        self._cache.pop((target, profile or "default"), None)

    def put_external(
        self, target: str, url: str, headers: Dict[str, str], *, profile: str = "default", source: str = "external"
    ) -> CapturedAuth:
        if target not in MINT_TARGETS:
            raise ConnectorError("not_configured", f"unknown token target {target!r}")
        if not re.search(MINT_TARGETS[target].url_pattern, url or ""):
            raise ConnectorError("shape", f"{url!r} doesn't look like a {target} request")
        auth = build_auth(target, url, headers, source=source)
        if "authorization" not in auth.headers:
            raise ConnectorError("shape", "no authorization header")
        self._cache[(target, profile or "default")] = auth
        return auth

    async def get(self, target: str, *, app_url: str, profile: str = "default", force: bool = False) -> CapturedAuth:
        profile = profile or "default"
        if not force:
            hit = self.cached(target, profile)
            if hit:
                return hit
        async with self._lock(profile):  # one headless page per profile at a time
            if not force:
                hit = self.cached(target, profile)
                if hit:
                    return hit
            try:
                auth = await self._mint(target, app_url=app_url, profile=profile)
            except ConnectorError as exc:
                self.last_error[(target, profile)] = str(exc)
                raise
            self.last_error.pop((target, profile), None)
            self._cache[(target, profile)] = auth
            return auth

    async def _mint(self, target_key: str, *, app_url: str, profile: str) -> CapturedAuth:
        target = MINT_TARGETS.get(target_key)
        if target is None:
            raise ConnectorError("not_configured", f"unknown token target {target_key!r}")
        url, headers, cookie_header, page_url = await self._capture(target, app_url=app_url, profile=profile)
        auth = build_auth(target.key, url, headers, source="mint", cookie_header=cookie_header)
        logger.info("Minted %s token (aud=%s) from %s", target.key, auth.claims.get("aud"), page_url)
        return auth

    async def _capture(self, target: MintTarget, *, app_url: str, profile: str) -> Tuple[str, Dict[str, str], str, str]:
        """Open the app headless, return (request url, headers, cookie header, page url)."""
        from ..browser_stream import browser_stream_manager

        try:
            ctx = await browser_stream_manager.ensure_browser(profile)
        except Exception as exc:
            raise ConnectorError("not_configured", f"server browser unavailable: {exc}") from exc

        loop = asyncio.get_running_loop()
        found: "asyncio.Future[Tuple[str, Dict[str, str]]]" = loop.create_future()
        pattern = re.compile(target.url_pattern)

        async def inspect(request: Any) -> None:
            if found.done() or not pattern.search(request.url):
                return
            try:
                hdrs = await request.all_headers()
            except Exception:
                hdrs = dict(request.headers)
            if not any(k.lower() == "authorization" for k in hdrs):
                return
            if not found.done():
                found.set_result((request.url, hdrs))

        def on_request(request: Any) -> None:
            loop.create_task(inspect(request))

        def on_worker_match(url: str, headers: Dict[str, str]) -> None:
            if not found.done():
                found.set_result((url, headers))

        ctx.on("request", on_request)
        page = None
        page_url = ""
        watch = _SharedWorkerWatch(pattern, on_worker_match)
        try:
            page = await ctx.new_page()
            await watch.start(ctx, page)
            open_url = target.open_url(app_url)
            try:
                await page.goto(open_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # SPA navigations often "fail" mid-redirect; keep waiting
                logger.debug("mint goto %s: %s", open_url, exc)
            try:
                url, headers = await asyncio.wait_for(found, timeout=float(settings.connector_mint_timeout_seconds))
            except asyncio.TimeoutError:
                page_url = page.url if page else ""
                if _is_login_url(page_url):
                    raise ConnectorError(
                        "auth",
                        f"{target.label}: sign-in needed. Open the app once in Node's browser and sign in "
                        "(tick 'stay signed in').",
                    )
                raise ConnectorError(
                    "network", f"{target.label}: no signed request within {int(settings.connector_mint_timeout_seconds)}s"
                )
            page_url = page.url
            cookie_header = ""
            try:
                cookies = await ctx.cookies([url])
                cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            except Exception:
                pass
            return url, headers, cookie_header, page_url
        finally:
            await watch.stop()
            try:
                ctx.remove_listener("request", on_request)
            except Exception:
                pass
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    def describe(self) -> Dict[str, Any]:
        rows = []
        for (target, profile), auth in self._cache.items():
            rows.append({**auth.describe(), "profile": profile, "valid": not auth.expired})
        errors = [{"target": t, "profile": p, "error": e} for (t, p), e in self.last_error.items()]
        return {"tokens": rows, "errors": errors, "targets": {k: v.label for k, v in MINT_TARGETS.items()}}


token_mint = TokenMint()
