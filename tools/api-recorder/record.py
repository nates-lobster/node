#!/usr/bin/env python3
"""Node API Recorder: standalone edition.

Opens a browser window (its own profile, so you log in once and it stays
logged in), records the API calls the sites make, and serves a plain local
page at http://127.0.0.1:8765 where you tick the calls that matter and save
them to a JSON file.

The file holds STRUCTURE ONLY: method, URL with ids masked, request header
names (values only for protocol headers like `action`), cookie names, and
the JSON shape of bodies with text replaced by <text N>. Redaction is done by
extension/recorder/shape.js (the same code the extension and the console
snippet use), run inside the recorder's own tab. Raw values never touch the
disk and nothing is sent anywhere.

Setup (once):
    pip install playwright
    python -m playwright install chromium     # or use --channel msedge / chrome

Use:
    python tools/api-recorder/record.py --url https://outlook.cloud.microsoft/mail/
    python tools/api-recorder/record.py --channel msedge --url https://teams.microsoft.com
    python tools/api-recorder/record.py --cdp http://127.0.0.1:9222   # attach to a browser you started

Ctrl+C stops; anything you marked is also written to ./node-api-capture-*.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SHAPE_CANDIDATES = [HERE / "shape.js", HERE.parents[1] / "extension" / "recorder" / "shape.js"]
# Network resource types worth recording (pages, scripts, images etc. are not APIs).
KEEP_TYPES = {"xhr", "fetch", "eventsource", "other"}
MAX_BODY = 3_000_000
MAX_ENTRIES = 6000
OP_RE = re.compile(r'"operationName"\s*:\s*"([A-Za-z0-9_]{1,60})"')


def find_shape_js() -> str:
    for p in SHAPE_CANDIDATES:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise SystemExit(
        "shape.js not found. Run this from the repo checkout (it uses extension/recorder/shape.js), "
        "or copy shape.js next to record.py."
    )


def textish(ctype: str) -> bool:
    c = (ctype or "").lower()
    return not c or "json" in c or "text/plain" in c or "javascript" in c


def auth_kind(names: List[str], cookie_count: int) -> str:
    has = set(names).__contains__
    bits = []
    if has("authorization"):
        bits.append("authorization header")
    if has("authentication") or has("x-skypetoken"):
        bits.append("skype token header")
    if has("x-owa-canary"):
        bits.append("OWA canary")
    if has("x-csrf-token") or has("x-xsrf-token") or has("x-csrftoken"):
        bits.append("CSRF token")
    if cookie_count:
        bits.append("cookies")
    return " + ".join(bits) or "none seen"


# ----------------------------------------------------------------- shaping
SHAPE_FN = """(raw) => {
  const S = globalThis.NodeRecorderShape;
  const h = S.summarizeHeaders(raw.headers || []);
  const reqShape = raw.reqText != null ? S.shapeBodyText(raw.reqText) : (h.bodyFromHeader || null);
  const respShape = raw.respText != null ? S.shapeBodyText(raw.respText) : (raw.respMarker || null);
  return {
    key: S.groupKey(raw.method, raw.url, raw.op || ""),
    url: S.redactUrl(raw.url),
    names: h.names, safe: h.safe, cookieNames: h.cookieNames,
    reqShape, respShape,
    noise: S.isNoise(raw.url),
    suggested: S.isSuggested(raw.url, respShape),
    messageList: !!(respShape && typeof respShape === "object" && S.looksLikeMessageList(respShape)),
  };
}"""


class Shaper:
    """Runs shape.js in the recorder UI tab. Raw text goes in; only shapes come out."""

    def __init__(self, context, ui_url: str):
        self.context = context
        self.ui_url = ui_url
        self.page = None
        self.lock = asyncio.Lock()

    async def _ensure(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        async with self.lock:
            if self.page is None or self.page.is_closed():
                page = await self.context.new_page()
                await page.goto(self.ui_url)
                await page.wait_for_function("() => !!globalThis.NodeRecorderShape", timeout=15000)
                self.page = page
        return self.page

    async def shape(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            try:
                page = await self._ensure()
                await page.wait_for_function("() => !!globalThis.NodeRecorderShape", timeout=10000)
                return await page.evaluate(SHAPE_FN, raw)
            except Exception:  # tab closed/reloading: reopen and retry
                if attempt == 2:
                    return None
                await asyncio.sleep(0.5)
        return None


# ---------------------------------------------------------------- recorder
class Recorder:
    def __init__(self, matches: List[str], own_origin: str):
        self.matches = [m.lower() for m in matches if m]
        self.own_origin = own_origin
        self.entries: List[Dict[str, Any]] = []
        self.marks: Dict[str, str] = {}
        self.paused = False
        self.dropped = 0
        self.shaper: Optional[Shaper] = None
        self.started_at = time.time() * 1000

    # -- filtering
    def wants(self, url: str, page_url: str) -> bool:
        if self.paused:
            return False
        if url.startswith(self.own_origin) or page_url.startswith(self.own_origin):
            return False
        if not url.startswith(("http://", "https://", "ws://", "wss://")):
            return False
        if not self.matches:
            return True
        hay = (url + " " + page_url).lower()
        return any(m in hay for m in self.matches)

    def add(self, entry: Dict[str, Any]) -> None:
        self.entries.append(entry)
        if len(self.entries) > MAX_ENTRIES:
            del self.entries[: len(self.entries) - MAX_ENTRIES]

    # -- playwright event handlers
    async def on_finished(self, req, failed: bool = False) -> None:
        try:
            if req.resource_type not in KEEP_TYPES:
                return
            page_url = ""
            try:
                page_url = req.frame.page.url
            except Exception:
                pass  # service-worker request: no frame
            if not self.wants(req.url, page_url):
                return
            t = time.time() * 1000
            try:
                timing = req.timing
                if timing and timing.get("startTime", 0) > 0:
                    t = timing["startTime"]
            except Exception:
                pass
            headers = [{"name": k, "value": v} for k, v in (await req.all_headers()).items()]
            try:
                req_text = req.post_data
            except Exception:
                req_text = None  # binary body
            status: Any = "failed"
            resp_text = None
            resp_marker = None
            resp = None if failed else await req.response()
            if resp is not None:
                status = resp.status
                ctype = (await resp.all_headers()).get("content-type", "")
                if textish(ctype) and not (300 <= resp.status < 400) and resp.status != 204:
                    try:
                        body = await resp.body()
                        if len(body) <= MAX_BODY:
                            resp_text = body.decode("utf-8", "replace")
                        else:
                            resp_marker = f"<large {len(body)} bytes>"
                    except Exception:
                        pass
            op = ""
            if isinstance(req_text, str) and len(req_text) < 200000:
                m = OP_RE.search(req_text)
                op = m.group(1) if m else ""
            await self._store(
                {"method": req.method, "url": req.url, "headers": headers, "reqText": req_text,
                 "respText": resp_text, "respMarker": resp_marker, "op": op},
                t=t, status=status, via=req.resource_type, page_url=page_url,
            )
        except Exception as exc:  # never let one odd request kill recording
            self.dropped += 1
            if os.getenv("NODE_REC_DEBUG"):
                print("drop:", exc, file=sys.stderr)

    async def _store(self, raw, t, status, via, page_url, frames=None) -> Optional[Dict[str, Any]]:
        shaped = await self.shaper.shape(raw) if self.shaper else None
        del raw  # only shapes are kept
        if shaped is None:
            self.dropped += 1
            return None
        shaped.update({"t": t, "status": status, "via": via, "origin": _origin(page_url)})
        if frames is not None:
            shaped["frames"] = frames
        self.add(shaped)
        return shaped

    def attach_websockets(self, page) -> None:
        def on_ws(ws):
            asyncio.ensure_future(self._on_ws(ws, page))
        page.on("websocket", on_ws)

    async def _on_ws(self, ws, page) -> None:
        try:
            if not self.wants(ws.url, page.url):
                return
            frames = {"in": 0, "out": 0}
            entry = await self._store(
                {"method": "WS", "url": ws.url, "headers": [], "reqText": None, "respText": None, "op": ""},
                t=time.time() * 1000, status=101, via="websocket", page_url=page.url, frames=frames,
            )
            if entry is None:
                return

            async def shape_frame(payload, direction):
                frames[direction] += 1
                entry["frames"] = dict(frames)
                field = "respShape" if direction == "in" else "reqShape"
                if entry.get(field) or not isinstance(payload, str):
                    return
                text = re.sub(r"^\d+:+", "", payload)  # socket.io / trouter "3:::" prefixes
                res = await self.shaper.shape({"method": "WS", "url": ws.url, "headers": [], "reqText": None, "respText": text})
                if res:
                    entry[field] = res["respShape"]
                    entry["suggested"] = entry.get("suggested") or res["suggested"]
                    entry["messageList"] = entry.get("messageList") or res["messageList"]

            ws.on("framereceived", lambda p: asyncio.ensure_future(shape_frame(p, "in")))
            ws.on("framesent", lambda p: asyncio.ensure_future(shape_frame(p, "out")))
        except Exception:
            self.dropped += 1

    # -- views
    def groups(self) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for e in self.entries:
            g = groups.get(e["key"])
            if g is None:
                g = groups[e["key"]] = {
                    "key": e["key"], "method": e["key"].split(" ", 1)[0], "url": e["url"], "via": e["via"],
                    "count": 0, "ts": [], "statuses": [], "names": set(), "safe": {}, "cookies": set(),
                    "reqShape": None, "respShape": None, "noise": True, "suggested": False,
                    "messageList": False, "frames": None, "origins": Counter(),
                }
            g["count"] += 1
            g["ts"].append(e["t"])
            if e["status"] not in g["statuses"]:
                g["statuses"].append(e["status"])
            g["names"].update(e.get("names") or [])
            g["safe"].update(e.get("safe") or {})
            g["cookies"].update(e.get("cookieNames") or [])
            g["reqShape"] = g["reqShape"] or e.get("reqShape")
            g["respShape"] = g["respShape"] or e.get("respShape")
            g["noise"] = g["noise"] and bool(e.get("noise"))
            g["suggested"] = g["suggested"] or bool(e.get("suggested"))
            g["messageList"] = g["messageList"] or bool(e.get("messageList"))
            g["frames"] = e.get("frames") or g["frames"]
            if e.get("origin"):
                g["origins"][e["origin"]] += 1
        out = []
        for g in groups.values():
            names = sorted(g["names"])
            out.append({
                "key": g["key"], "method": g["method"], "url": g["url"], "via": g["via"], "count": g["count"],
                "ts": g["ts"][-60:], "lastT": max(g["ts"]), "statuses": g["statuses"][:6],
                "requestHeaderNames": names, "requestHeaders": g["safe"], "cookieNames": sorted(g["cookies"]),
                "auth": auth_kind(names, len(g["cookies"])), "requestBody": g["reqShape"], "responseBody": g["respShape"],
                "frames": g["frames"], "noise": g["noise"], "suggested": (not g["noise"]) and g["suggested"],
                "messageList": g["messageList"], "mark": self.marks.get(g["key"]),
                "origin": g["origins"].most_common(1)[0][0] if g["origins"] else "",
            })
        out.sort(key=lambda g: (not g["suggested"], -g["lastT"]))
        return out

    def export(self, include_all: bool = False) -> Dict[str, Any]:
        gs = [g for g in self.groups() if include_all or g["mark"] is not None]
        sites = Counter(g["origin"] for g in gs if g["origin"])
        return {
            "tool": "node-api-recorder",
            "version": "python-1",
            "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "site": sites.most_common(1)[0][0] if sites else "",
            "privacy": "Structure only: header names (values only for protocol headers like action/content-type), "
                       "cookie NAMES, redacted URLs and JSON shapes with personal text replaced by <text N>. "
                       "No tokens, cookies or message content.",
            "calls": [{
                "label": g["mark"] or "", "method": g["method"], "url": g["url"], "via": g["via"], "count": g["count"],
                "statuses": g["statuses"], "auth": g["auth"], "requestHeaderNames": g["requestHeaderNames"],
                "requestHeaders": g["requestHeaders"], "cookieNames": g["cookieNames"],
                "requestBody": g["requestBody"], "responseBody": g["responseBody"],
                **({"websocketFrames": g["frames"]} if g["frames"] else {}),
            } for g in gs],
        }

    def state(self) -> Dict[str, Any]:
        return {"now": time.time() * 1000, "paused": self.paused, "total": len(self.entries),
                "dropped": self.dropped, "groups": self.groups()}


def _origin(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme in ("http", "https") else ""
    except Exception:
        return ""


def export_filename(data: Dict[str, Any]) -> str:
    host = re.sub(r"[^a-z0-9.-]+", "_", re.sub(r"^https?://", "", data.get("site") or "site"), flags=re.I)
    return f"node-api-capture-{host}-{time.strftime('%Y-%m-%d-%H-%M')}.json"


# ------------------------------------------------------------- tiny server
class UiServer:
    """Minimal asyncio HTTP server for the marking page (stdlib only)."""

    def __init__(self, rec: Recorder, shape_js: str, port: int):
        self.rec = rec
        self.shape_js = shape_js
        self.port = port
        self.server = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def route(self, method: str, path: str, body: bytes):
        rec = self.rec
        if method == "GET" and path == "/":
            return 200, "text/html; charset=utf-8", UI_HTML.encode(), {}
        if method == "GET" and path == "/shape.js":
            return 200, "application/javascript; charset=utf-8", self.shape_js.encode(), {}
        if method == "GET" and path == "/api/state":
            return 200, "application/json", json.dumps(rec.state()).encode(), {}
        if method == "GET" and path.startswith("/api/export"):
            data = rec.export(include_all="all=1" in path)
            return 200, "application/json", json.dumps(data, indent=2).encode(), {
                "Content-Disposition": f'attachment; filename="{export_filename(data)}"'}
        if method == "POST" and path == "/api/mark":
            req = json.loads(body or b"{}")
            if req.get("mark") is None:
                rec.marks.pop(req.get("key"), None)
            else:
                rec.marks[str(req["key"])] = str(req["mark"])[:80]
            return 200, "application/json", b'{"ok":true}', {}
        if method == "POST" and path == "/api/pause":
            rec.paused = not rec.paused
            return 200, "application/json", json.dumps({"paused": rec.paused}).encode(), {}
        if method == "POST" and path == "/api/clear":
            rec.entries.clear()
            rec.marks.clear()
            return 200, "application/json", b'{"ok":true}', {}
        return 404, "text/plain", b"not found", {}

    async def handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            body = await reader.readexactly(int(headers.get("content-length") or 0))
            # Only answer our own origin (blocks DNS-rebinding pages from reading it).
            if headers.get("host") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
                status, ctype, payload, extra = 403, "text/plain", b"forbidden host", {}
            else:
                status, ctype, payload, extra = self.route(method, target, body)
            reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
            hdrs = {"Content-Type": ctype, "Content-Length": str(len(payload)), "Cache-Control": "no-store",
                    "Connection": "close", **extra}
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode() +
                         "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode() + b"\r\n" + payload)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


# ------------------------------------------------------ shared workers
class SharedWorkerTap:
    """Records requests made inside SharedWorkers, which Playwright doesn't report.

    Uses the DevTools protocol: every 2 s it lists the browser's targets,
    attaches to new shared workers (non-flattened, so it works through a
    normal Playwright CDP session) and enables their Network domain. Calls a
    worker made before it was attached are missed, so re-do the action.
    """

    KEEP = {"Fetch", "XHR", "EventSource", "Other"}

    def __init__(self, app: "App"):
        self.app = app
        self.session = None
        self.session_page = None
        self.attached: Dict[str, str] = {}  # targetId -> sessionId
        self.requests: Dict[tuple, Dict[str, Any]] = {}
        self.pending: Dict[tuple, asyncio.Future] = {}
        self.extra: Dict[tuple, Dict[str, str]] = {}
        self.cookie_cache: Dict[str, tuple] = {}  # ExtraInfo can arrive before requestWillBeSent
        self.next_id = 1000
        self.task: Optional[asyncio.Task] = None
        self.workers_seen = 0

    def start(self) -> None:
        self.task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._scan()
            except Exception as exc:
                if os.getenv("NODE_REC_DEBUG"):
                    print("shared-worker tap:", exc, file=sys.stderr)
                self.session = None
            await asyncio.sleep(2)

    async def _ensure_session(self):
        page = await self.app.rec.shaper._ensure()
        if self.session is None or self.session_page is not page or page.is_closed():
            self.session = await self.app.context.new_cdp_session(page)
            self.session_page = page
            self.attached.clear()
            self.session.on("Target.receivedMessageFromTarget", self._on_message)
        return self.session

    async def _scan(self) -> None:
        session = await self._ensure_session()
        infos = (await session.send("Target.getTargets"))["targetInfos"]
        live = set()
        for t in infos:
            if t.get("type") != "shared_worker":
                continue
            live.add(t["targetId"])
            if t["targetId"] in self.attached:
                continue  # requests are filtered by --match one by one, like page requests
            res = await session.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": False})
            sid = res["sessionId"]
            self.attached[t["targetId"]] = sid
            self.workers_seen += 1
            await self._send(sid, "Network.enable", {}, wait=False)
        for tid in list(self.attached):
            if tid not in live:
                self.attached.pop(tid, None)

    async def _send(self, sid: str, method: str, params: Dict[str, Any], wait: bool = True):
        self.next_id += 1
        mid = self.next_id
        fut = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self.pending[(sid, mid)] = fut
        await self.session.send("Target.sendMessageToTarget",
                                {"sessionId": sid, "message": json.dumps({"id": mid, "method": method, "params": params})})
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self.pending.pop((sid, mid), None)

    def _on_message(self, event: Dict[str, Any]) -> None:
        sid = event.get("sessionId")
        try:
            msg = json.loads(event.get("message") or "{}")
        except ValueError:
            return
        if "id" in msg:
            fut = self.pending.get((sid, msg["id"]))
            if fut and not fut.done():
                fut.set_result(msg.get("result") or {"error": msg.get("error")})
            return
        method, params = msg.get("method"), msg.get("params") or {}
        rid = (sid, params.get("requestId"))
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            if params.get("type", "Other") not in self.KEEP or self.app.rec.paused:
                return
            if not self.app.rec.wants(req.get("url", ""), ""):
                return
            self.requests[rid] = {"method": req.get("method", "GET"), "url": req.get("url", ""),
                                  "headers": req.get("headers") or {}, "postData": req.get("postData"),
                                  "hasPostData": req.get("hasPostData"), "t": time.time() * 1000, "type": params.get("type", "Other")}
        elif method == "Network.requestWillBeSentExtraInfo":
            # The headers that really went out, cookies included. Applied when the request finishes.
            self.extra[rid] = params.get("headers") or {}
            if len(self.extra) > 2000:
                self.extra.pop(next(iter(self.extra)))
        elif method == "Network.responseReceived" and rid in self.requests:
            resp = params.get("response") or {}
            self.requests[rid]["status"] = resp.get("status")
            self.requests[rid]["ctype"] = (resp.get("mimeType") or "")
        elif method in ("Network.loadingFinished", "Network.loadingFailed") and rid in self.requests:
            info = self.requests.pop(rid)
            if rid in self.extra:
                info["headers"] = self.extra.pop(rid) or info["headers"]
            asyncio.ensure_future(self._finish(sid, rid[1], info, failed=method == "Network.loadingFailed"))

    async def _cookie_names(self, url: str) -> List[str]:
        host = urlparse(url).netloc
        hit = self.cookie_cache.get(host)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        try:
            res = await self.session.send("Network.getCookies", {"urls": [url]})
            names = sorted({c["name"] for c in res.get("cookies", [])})
        except Exception:
            names = []
        self.cookie_cache[host] = (time.time(), names)
        return names

    async def _finish(self, sid: str, request_id: str, info: Dict[str, Any], failed: bool) -> None:
        try:
            body_text = None
            marker = None
            if not failed and textish(info.get("ctype", "")) and info.get("status") not in (204, None) and not (300 <= (info.get("status") or 0) < 400):
                res = await self._send(sid, "Network.getResponseBody", {"requestId": request_id})
                if res and "body" in res:
                    if res.get("base64Encoded"):
                        import base64
                        res["body"] = base64.b64decode(res["body"]).decode("utf-8", "replace")
                    body = res["body"]
                    if len(body) <= MAX_BODY:
                        body_text = body
                    else:
                        marker = f"<large {len(body)} chars>"
            post = info.get("postData")
            if post is None and info.get("hasPostData"):
                res = await self._send(sid, "Network.getRequestPostData", {"requestId": request_id})
                post = (res or {}).get("postData")
            op = ""
            if isinstance(post, str) and len(post) < 200000:
                m = OP_RE.search(post)
                op = m.group(1) if m else ""
            hdrs = dict(info.get("headers") or {})
            if not any(k.lower() == "cookie" for k in hdrs):
                # Worker requests don't report their final headers here; ask
                # the browser which cookies it holds for the URL instead.
                names = await self._cookie_names(info["url"])
                if names:
                    hdrs["cookie"] = "; ".join(f"{n}=x" for n in names)  # names only; values never read
            headers = [{"name": k, "value": v} for k, v in hdrs.items()]
            await self.app.rec._store(
                {"method": info["method"], "url": info["url"], "headers": headers, "reqText": post,
                 "respText": body_text, "respMarker": marker, "op": op},
                t=info["t"], status="failed" if failed else info.get("status"), via="shared-worker", page_url="",
            )
        except Exception:
            self.app.rec.dropped += 1


# --------------------------------------------------------------- the app
class App:
    def __init__(self, args):
        self.args = args
        self.shape_js = find_shape_js()
        self.rec: Optional[Recorder] = None
        self.ui: Optional[UiServer] = None
        self.pw = None
        self.browser = None
        self.context = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        a = self.args
        matches = [m.strip() for part in (a.match or []) for m in part.split(",")]
        self.rec = Recorder(matches, own_origin="")
        self.ui = UiServer(self.rec, self.shape_js, a.port)
        await self.ui.start()
        self.rec.own_origin = self.ui.url.rstrip("/")

        self.pw = await async_playwright().start()
        if a.cdp:
            self.browser = await self.pw.chromium.connect_over_cdp(a.cdp)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        else:
            launch: Dict[str, Any] = {"headless": a.headless, "no_viewport": not a.headless, "args": ["--no-first-run"]}
            if a.browser:
                launch["executable_path"] = a.browser
            elif a.channel:
                launch["channel"] = a.channel
            if a.headless:
                launch["args"].append("--no-sandbox")
            Path(a.profile).mkdir(parents=True, exist_ok=True)
            self.context = await self.pw.chromium.launch_persistent_context(a.profile, **launch)

        self.rec.shaper = Shaper(self.context, self.ui.url)
        self.context.on("requestfinished", lambda r: asyncio.ensure_future(self.rec.on_finished(r)))
        self.context.on("requestfailed", lambda r: asyncio.ensure_future(self.rec.on_finished(r, failed=True)))
        self.context.on("page", self.rec.attach_websockets)
        for p in self.context.pages:
            self.rec.attach_websockets(p)
        await self.rec.shaper._ensure()  # the marking UI tab
        self.tap = SharedWorkerTap(self)
        self.tap.start()
        if a.url and not a.cdp:
            blank = [p for p in self.context.pages if p.url in ("about:blank", "chrome://newtab/")]
            page = blank[0] if blank else await self.context.new_page()
            await page.goto(a.url)
            await page.bring_to_front()

    def save_marked(self) -> Optional[Path]:
        if not self.rec or not self.rec.marks:
            return None
        data = self.rec.export()
        if not data["calls"]:
            return None
        out = Path.cwd() / export_filename(data)
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return out

    async def close(self) -> None:
        if getattr(self, "tap", None):
            await self.tap.stop()
        # In --cdp mode the browser is the user's own: disconnect, don't close it.
        steps = []
        if self.context is not None and not self.args.cdp:
            steps.append(self.context.close)
        if self.browser is not None:
            steps.append(self.browser.close)
        if self.pw is not None:
            steps.append(self.pw.stop)
        for step in steps:
            try:
                await step()
            except Exception:
                pass
        if self.ui and self.ui.server:
            self.ui.server.close()


async def amain(args) -> None:
    app = App(args)
    await app.start()
    print(f"\nRecording. Marking page: {app.ui.url}  (it's also open as a tab)")
    print("Use the site; Ctrl+C here when done (marked calls are saved to a file).\n")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig), stop.set)
        except (NotImplementedError, RuntimeError, AttributeError):
            pass  # Windows: Ctrl+C arrives as KeyboardInterrupt / task cancel instead
    try:
        last = None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            n = (len(app.rec.entries), len(app.rec.marks))
            if n != last:
                print(f"\r  {n[0]} calls recorded, {len(app.rec.groups())} kinds, {n[1]} marked   ", end="", flush=True)
                last = n
            if not args.cdp and not app.context.pages:
                print("\nBrowser closed.")
                break
    finally:
        out = app.save_marked()
        if out:
            print(f"\nSaved marked calls to {out}")
        await app.close()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Record a site's API calls (structure only) and save the ones you mark.")
    ap.add_argument("--url", help="page to open first, e.g. https://outlook.cloud.microsoft/mail/")
    ap.add_argument("--match", action="append", help="only record URLs/pages containing this (repeatable or comma-separated), e.g. outlook,office")
    ap.add_argument("--channel", choices=["chrome", "msedge", "chrome-beta", "msedge-beta"], help="use an installed Chrome/Edge instead of Playwright's Chromium")
    ap.add_argument("--browser", help="path to a Chromium-based browser (e.g. Brave) to launch")
    ap.add_argument("--cdp", help="attach to a running browser started with --remote-debugging-port, e.g. http://127.0.0.1:9222")
    ap.add_argument("--profile", default=str(Path.home() / ".node-api-recorder" / "profile"), help="browser profile folder (keeps you logged in between runs)")
    ap.add_argument("--port", type=int, default=8765, help="port for the marking page (0 = any free port)")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


# ---------------------------------------------------------------- the page
UI_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>API Recorder</title>
<script src="/shape.js"></script>
<style>
 body{font:13px/1.4 system-ui,sans-serif;margin:0;background:#111;color:#eee}
 header{position:sticky;top:0;background:#1a1a1a;border-bottom:1px solid #333;padding:8px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;z-index:2}
 header b{margin-right:auto} button{font:inherit;background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:5px;padding:4px 10px;cursor:pointer}
 button.go{background:#1d6b4c;border-color:#2bfbae} button:disabled{opacity:.5}
 #msg{padding:8px 12px;background:#1b2b24;border-bottom:1px solid #2bfbae55;display:none} #msg input{width:340px}
 .help{padding:8px 12px;color:#aaa;border-bottom:1px solid #222} .help ol{margin:0;padding-left:18px}
 h3{font-size:11px;color:#999;text-transform:uppercase;margin:12px 12px 4px;display:flex;gap:10px} h3 a{color:#2bfbae;cursor:pointer;text-transform:none}
 .row{padding:4px 12px;border-top:1px solid #222} .row.hit{background:#17392b}
 .line{display:flex;gap:8px;align-items:center;cursor:pointer} .name{flex:1;font-family:ui-monospace,monospace;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tag{font-size:11px;padding:0 6px;border-radius:8px;background:#333;white-space:nowrap} .good{background:#1d6b4c} .bad{background:#6b1d2a}
 input[type=text]{font:inherit;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:3px 6px}
 .label{margin:4px 0 0 26px;width:420px} pre{margin:4px 0 4px 26px;padding:8px;background:#000;max-height:320px;overflow:auto;font:11px ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
 .empty{padding:8px 12px;color:#888}
</style></head><body>
<header><b id="title">API Recorder</b>
 <button class="go" id="watch">&#9654; Watch my next action</button>
 <button id="save">Save marked (0)</button><button id="saveall">Save all</button><button id="copy">Copy</button>
 <button id="pause">Pause</button><button id="clear">Clear</button></header>
<div id="msg"></div>
<div class="help"><ol>
 <li>Use the site in the other tab (log in the first time; the profile remembers you).</li>
 <li><b>Watch my next action</b>, then switch tabs and do ONE thing (open an email / a folder / a chat) within 8 s. Come back, name it, Mark.</li>
 <li><b>Save marked</b> and send the file. It has structure only, no messages, tokens or cookie values.</li></ol></div>
<div id="list"><div class="empty">Waiting for requests…</div></div>
<script>
const $=id=>document.getElementById(id); const WATCH=8;
let st={groups:[]}, watch=null, open={}, showOther=false, showNoise=false, sig="";
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(r=>r.json());
const hit=g=>watch&&watch.done&&g.ts.some(t=>t>=watch.start&&t<=watch.end);
function el(tag,props,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(props||{})){if(k.startsWith("on"))e.addEventListener(k.slice(2),v);else if(k==="text")e.textContent=v;else if(k==="cls")e.className=v;else if(v!==false&&v!=null)e.setAttribute(k,v===true?"":v)}for(const c of kids.flat())if(c!=null&&c!==false)e.append(c);return e}
function row(g){const marked=g.mark!==null&&g.mark!==undefined;
 const ok=g.statuses.some(s=>(s>=200&&s<300)||s===101);
 const r=el("div",{cls:"row"+(hit(g)?" hit":"")},
  el("div",{cls:"line",onclick:()=>{open[g.key]=!open[g.key];render(true)}},
   el("input",{type:"checkbox",checked:marked,onclick:async e=>{e.stopPropagation();await post("/api/mark",{key:g.key,mark:marked?null:""});refresh(true)}}),
   el("span",{cls:"tag",text:g.method}),el("span",{cls:"name",title:g.url,text:g.key.replace(/^[A-Z]+ /,"")}),
   g.messageList&&el("span",{cls:"tag good",text:"list of items"}),el("span",{cls:"tag",text:"×"+g.count}),
   el("span",{cls:"tag "+(ok?"":"bad"),text:g.statuses.join(",")})));
 if(marked){const i=el("input",{type:"text",cls:"label",placeholder:"What is this? e.g. inbox list",value:g.mark||""});
  i.addEventListener("change",()=>post("/api/mark",{key:g.key,mark:i.value}));r.append(i)}
 if(open[g.key])r.append(el("pre",{text:JSON.stringify({url:g.url,auth:g.auth,requestHeaderNames:g.requestHeaderNames,requestHeaders:g.requestHeaders,cookieNames:g.cookieNames,requestBody:g.requestBody,responseBody:g.responseBody,websocketFrames:g.frames||undefined},null,1)}));
 return r}
function render(force){const gs=st.groups, n=gs.filter(g=>g.mark!==null&&g.mark!==undefined).length;
 $("title").textContent=`API Recorder: ${st.paused?"paused":"recording"} · ${st.total||0} calls`+(st.dropped?` · ${st.dropped} skipped`:"");
 $("save").textContent=`Save marked (${n})`;$("save").disabled=$("copy").disabled=!n;$("pause").textContent=st.paused?"Resume":"Pause";
 $("watch").disabled=!!(watch&&!watch.done);
 const hf=a=>watch&&watch.done?a.slice().sort((x,y)=>hit(y)-hit(x)):a;
 const sug=hf(gs.filter(g=>g.suggested)),oth=hf(gs.filter(g=>!g.suggested&&!g.noise)),noi=gs.filter(g=>g.noise);
 if(!gs.length)return;
 const t=(on,f)=>el("a",{onclick:()=>{f();render(true)},text:on?"hide":"show"});
 $("list").replaceChildren(el("h3",{},`Likely useful (${sug.length})`),...(sug.length?sug.map(row):[el("div",{cls:"empty",text:"None yet."})]),
  el("h3",{},`Other calls (${oth.length})`,t(showOther,()=>showOther=!showOther)),...(showOther?oth.map(row):[]),
  el("h3",{},`Hidden as noise (${noi.length})`,t(showNoise,()=>showNoise=!showNoise)),...(showNoise?noi.map(row):[]))}
async function refresh(force){if(!force&&document.activeElement&&document.activeElement.matches("input[type=text]"))return;
 st=await (await fetch("/api/state")).json();
 const s=JSON.stringify([st.total,st.paused,st.groups.map(g=>[g.key,g.count,g.mark,g.statuses]),open,showOther,showNoise,watch]);
 if(force||s!==sig){sig=s;render()}}
function msg(...k){$("msg").replaceChildren(...k);$("msg").style.display=k.length?"block":"none"}
$("watch").onclick=()=>{watch={start:st.now-300,end:st.now+WATCH*1000,done:false};let left=WATCH;const c=el("span",{text:left+"s"});
 msg(el("b",{text:"Switch to the site and do ONE thing now… "}),c);render(true);
 const iv=setInterval(()=>{left--;c.textContent=left+"s";if(left>0)return;clearInterval(iv);setTimeout(done,1500)},1000)};
async function done(){watch.done=true;await refresh(true);const hits=st.groups.filter(g=>hit(g)&&!g.noise);
 if(!hits.length)return msg("Nothing fired. Try another action (switch folder and back re-loads the list). ",el("button",{onclick:()=>msg(),text:"ok"}));
 const i=el("input",{type:"text",placeholder:"What did you just do? e.g. open an email"});
 const apply=async()=>{const label=i.value.trim()||"action";const pick=hits.filter(g=>g.suggested||g.messageList);
  for(const g of(pick.length?pick:hits))await post("/api/mark",{key:g.key,mark:g.mark?g.mark+"; "+label:label});msg();refresh(true)};
 i.addEventListener("keydown",e=>e.key==="Enter"&&apply());
 msg(el("b",{text:hits.length+" call type(s) fired"})," (highlighted). ",i," ",el("button",{cls:"go",onclick:apply,text:"Mark them"})," ",el("button",{onclick:()=>msg(),text:"skip"}));i.focus()}
$("save").onclick=()=>location.href="/api/export";
$("saveall").onclick=()=>{if(confirm("Save every recorded call (still structure only)?"))location.href="/api/export?all=1"};
$("copy").onclick=async()=>{const t=await (await fetch("/api/export")).text();try{await navigator.clipboard.writeText(t);$("copy").textContent="Copied ✓"}catch(e){$("copy").textContent="Blocked, use Save"}setTimeout(()=>$("copy").textContent="Copy",1800)};
$("pause").onclick=async()=>{await post("/api/pause");refresh(true)};
$("clear").onclick=async()=>{if(confirm("Clear everything recorded?")){await post("/api/clear");watch=null;$("list").replaceChildren(el("div",{cls:"empty",text:"Waiting for requests…"}));refresh(true)}};
refresh(true);setInterval(()=>refresh(false),1500);
</script></body></html>"""


if __name__ == "__main__":
    try:
        asyncio.run(amain(parse_args()))
    except KeyboardInterrupt:
        pass
