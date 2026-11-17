"""
API Bridge — adopt `goodnight000/api-anything` for browser-free service access
=============================================================================

`api-anything` learns the request a website's own frontend makes, then replays
it over plain HTTP so an agent gets JSON instead of driving a browser. It ships
as a CLI / MCP server / HTTP server (loopback + token):

    api-anything serve --port 8787 --token <token>
    GET  /openapi.json
    POST /call  { "operation": "gmail-list", "arguments": {...} }

This module is the **integration seam**, deliberately thin and optional:

* Detects the CLI (``api-anything`` on PATH, or ``npx -y api-anything``).
* Keeps bridge settings + learned operations in `data/api_bridges.json`.
* Calls the local HTTP server when it is up, and falls back to "not installed"
  with an actionable message when it is not.
* Mirrors the browser's cookies into the bridge so a learned operation can run
  with the *same authenticated session* the scraper tab uses. Cookies are read
  out of the Playwright storage state for an app and pushed through the
  documented ``/session`` endpoint (or ``api-anything login`` when CLI-only).

Nothing here breaks the existing browser streaming path — bridges are an
accelerator: if a learned operation works, tabs can be closed; if it fails, the
tab is still there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

from .config import settings

BRIDGE_DEFAULT_PORT = 8787


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _port_open(base_url: str, timeout: float = 0.6) -> bool:
    """Cheap TCP check so 'listening but unauthorized' is distinguishable."""
    try:
        parsed = httpx.URL(base_url)
        host, port = parsed.host, parsed.port or 80
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")


class ApiBridgeService:
    """Optional bridge to a local `api-anything` HTTP server."""

    def __init__(self) -> None:
        base = Path(settings.database_url.replace("sqlite:///", "", 1)).parent
        self._state_path = base / "api_bridges.json"
        self._proc: Optional[subprocess.Popen] = None
        self._last_probe: float = 0.0
        self._probe_cache: Dict[str, Any] = {}

    # ----------------------------------------------------------- state file
    def _load(self) -> Dict[str, Any]:
        if self._state_path.is_file():
            try:
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw.setdefault("servers", {})
                    raw.setdefault("bridges", [])
                    raw.setdefault("operations", {})
                    return raw
            except Exception:
                pass
        return {"version": 1, "servers": {}, "bridges": [], "operations": {}}

    def _save(self, payload: Dict[str, Any]) -> None:
        payload["updated_at"] = _now()
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- settings
    def config(self) -> Dict[str, Any]:
        state = self._load()
        srv = state.get("servers", {})
        return {
            "base_url": srv.get("base_url") or f"http://127.0.0.1:{BRIDGE_DEFAULT_PORT}",
            "token": srv.get("token") or "",
            "auto_start": bool(srv.get("auto_start", False)),
            "prefer_http_over_tabs": bool(srv.get("prefer_http_over_tabs", True)),
            "bridges": state.get("bridges", []),
            "operation_count": len(state.get("operations", {}) or {}),
        }

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        state = self._load()
        srv = dict(state.get("servers") or {})
        for key in ("base_url", "token", "auto_start", "prefer_http_over_tabs"):
            if key in patch and patch[key] is not None:
                srv[key] = patch[key]
        state["servers"] = srv
        self._save(state)
        return self.config()

    def cli_available(self) -> Dict[str, Any]:
        path = shutil.which("api-anything")
        node = shutil.which("node")
        return {
            "installed": bool(path),
            "path": path or "",
            "npx_available": bool(node and shutil.which("npx")),
            "install_hint": (
                "npm i -g api-anything   # or: npx -y api-anything setup  "
                "(needs Node 22.13+; package may require a git build: "
                "git clone https://github.com/goodnight000/api-anything && npm ci && npm i -g \"$(npm pack --silent)\")"
            ),
        }

    # --------------------------------------------------------------- health
    async def status(self, force: bool = False) -> Dict[str, Any]:
        cfg = self.config()
        now = time.monotonic()
        if force or (now - self._last_probe) > 15.0 or not self._probe_cache:
            self._last_probe = now
            reachable = False
            ops: List[Dict[str, Any]] = []
            detail = "Not running"
            headers = {"Content-Type": "application/json"}
            if cfg["token"]:
                headers["Authorization"] = f"Bearer {cfg['token']}"
            try:
                async with httpx.AsyncClient(timeout=2.5) as client:
                    resp = await client.get(f"{cfg['base_url']}/openapi.json", headers=headers)
                    if resp.status_code < 400:
                        reachable = True
                        spec = resp.json()
                        for path, methods in (spec.get("paths") or {}).items():
                            for verb, meta in (methods or {}).items():
                                if not isinstance(meta, dict):
                                    continue
                                ops.append(
                                    {
                                        "path": path,
                                        "method": verb.upper(),
                                        "operation_id": meta.get("operationId") or "",
                                        "summary": meta.get("summary") or "",
                                    }
                                )
                        detail = f"Connected at {cfg['base_url']} ({len(ops)} operations)"
                    else:
                        detail = f"HTTP {resp.status_code} from {cfg['base_url']}"
            except Exception as exc:
                detail = f"{exc.__class__.__name__} reaching {cfg['base_url']}"
            if not reachable and _port_open(cfg["base_url"]):
                # Listening but not answering our unauthenticated probe: almost
                # always the bearer token. Surface that instead of "not running".
                reachable = True
                detail = (
                    f"Listening at {cfg['base_url']} but rejected the probe — "
                    "set the bridge token (API_ANYTHING_TOKEN) to match the server."
                )
            self._probe_cache = {
                "reachable": reachable,
                "detail": detail,
                "operations": ops[:60],
                "checked_at": _now(),
            }
        return {
            "reachable": self._probe_cache.get("reachable", False),
            "detail": self._probe_cache.get("detail", ""),
            "base_url": cfg["base_url"],
            "operations": self._probe_cache.get("operations", []),
            "cli": self.cli_available(),
            "checked_at": self._probe_cache.get("checked_at"),
            "bridges": cfg["bridges"],
        }

    # ------------------------------------------------------------- bridging
    def register_bridge(self, app_slug: str, *, operation: str = "", notes: str = "") -> Dict[str, Any]:
        state = self._load()
        bridges: List[Dict[str, Any]] = list(state.get("bridges") or [])
        slug = _slug(app_slug)
        row = next((b for b in bridges if b.get("app_slug") == slug), None)
        payload = {
            "app_slug": slug,
            "operation": operation.strip()[:160],
            "notes": notes.strip()[:400],
            "updated_at": _now(),
            "learned": bool(operation),
        }
        if row is None:
            bridges.append({**payload, "created_at": _now()})
        else:
            row.update(payload)
        state["bridges"] = bridges
        self._save(state)
        return payload

    def remove_bridge(self, app_slug: str) -> bool:
        state = self._load()
        slug = _slug(app_slug)
        before = len(state.get("bridges") or [])
        state["bridges"] = [b for b in (state.get("bridges") or []) if b.get("app_slug") != slug]
        self._save(state)
        return len(state["bridges"]) != before

    async def call(self, operation: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Invoke a learned read operation.

        `operation` is "site/op" (e.g. "hacker-news/search"); the server exposes
        these at POST /v1/call/{site}/{op}. Args go in the JSON body.
        """
        cfg = self.config()
        op = str(operation or "").strip().strip("/")
        if "/" not in op:
            return {
                "ok": False,
                "error": "Operation must look like 'site/op' (e.g. hacker-news/search).",
                "operations": [o.get("path") for o in self._probe_cache.get("operations", [])][:20],
            }
        headers = {"Content-Type": "application/json"}
        token = cfg.get("token") or os.environ.get("API_ANYTHING_TOKEN") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{cfg['base_url']}/v1/call/{op}"
        # The REST contract nests operation inputs under "args"; "view" is optional
        # field-projection and is passed through when the caller supplies it.
        body: Dict[str, Any] = {"args": dict(args or {})}
        if isinstance(args, dict) and "view" in args and "args" not in args:
            body["view"] = args["view"]
            body["args"] = {k: v for k, v in args.items() if k != "view"}
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                self._record_operation(op, ok=False)
                return {"ok": False, "status": resp.status_code, "error": resp.text[:400]}
            body = resp.json()
            self._record_operation(op, ok=True)
            # The server answers {"ok":true,"data":[...],"tier":1,...}; pass it through
            # but hoist `data` so callers don't have to guess at the shape.
            return {
                "ok": bool(body.get("ok", True)),
                "operation": op,
                "tier": body.get("tier"),
                "ms": body.get("ms"),
                "data": body.get("data"),
                "result": body,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
                "hint": "Start it with: api-anything serve --port 8787 (API_ANYTHING_TOKEN for auth).",
            }

    def _record_operation(self, operation: str, *, ok: bool) -> None:
        state = self._load()
        ops = dict(state.get("operations") or {})
        row = ops.get(operation) or {"operation": operation, "calls": 0, "failures": 0}
        row["calls"] = int(row.get("calls") or 0) + 1
        if not ok:
            row["failures"] = int(row.get("failures") or 0) + 1
        row["last_called_at"] = _now()
        ops[operation] = row
        state["operations"] = ops
        self._save(state)

    def start_server(self, port: int = BRIDGE_DEFAULT_PORT) -> Dict[str, Any]:
        """Best-effort background start of a local bridge server (opt-in)."""
        cli = self.cli_available()
        cfg = self.config()
        if not cli["installed"] and not cli["npx_available"]:
            return {"ok": False, "reason": "api-anything CLI not found", "hint": cli["install_hint"]}
        if self._proc and self._proc.poll() is None:
            return {"ok": True, "already_running": True, "pid": self._proc.pid}
        token = cfg.get("token") or ""
        cmd: Sequence[str]
        if cli["installed"]:
            cmd = [cli["path"], "serve", "--host", "127.0.0.1", "--port", str(port)]
        else:
            cmd = ["npx", "-y", "api-anything", "serve", "--host", "127.0.0.1", "--port", str(port)]
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        # The real CLI reads its bearer token from API_ANYTHING_TOKEN (falling back
        # to ~/.api-anything/serve-token). There is no --token flag.
        if token:
            env["API_ANYTHING_TOKEN"] = token
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - explicit, admin-initiated
                list(cmd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            return {"ok": True, "pid": self._proc.pid, "cmd": " ".join(cmd)}
        except Exception as exc:
            return {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}

    # ------------------------------------------------------- cookie bridging
    async def export_session_cookies(self, app_slug: str) -> Dict[str, Any]:
        """
        Read cookies out of the live Playwright context for `app_slug`.

        This is the server-side counterpart of "grab cookies from my local
        session in the extension": the extension pushes its cookies in through
        `/api/v1/bridges/session`, we store them in the persistent browser
        profile, and a learned api-anything operation can then replay requests
        with that exact authenticated state.
        """
        from .browser_stream import browser_stream_manager

        slug = _slug(app_slug)
        tab = (getattr(browser_stream_manager, "tabs", {}) or {}).get(slug)
        if tab is None or getattr(tab, "page", None) is None or tab.page.is_closed():
            return {"ok": False, "reason": "no_live_tab", "app_slug": slug}
        try:
            ctx = tab.page.context
            cookies = await ctx.cookies()
            return {
                "ok": True,
                "app_slug": slug,
                "cookie_count": len(cookies),
                "cookies": cookies,
                "user_agent": await tab.page.evaluate("() => navigator.userAgent"),
                "origin": (tab.page.url or "").split("/")[0] + "//" + (tab.page.url or "").split("/")[2]
                if "://" in (tab.page.url or "")
                else "",
            }
        except Exception as exc:
            return {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}

    async def import_session_cookies(
        self,
        app_slug: str,
        cookies: Sequence[Dict[str, Any]],
        *,
        device: str = "extension",
    ) -> Dict[str, Any]:
        """Accept cookies pushed from the local browser extension."""
        from .browser_stream import browser_stream_manager

        slug = _slug(app_slug)
        if not cookies:
            return {"ok": False, "reason": "no_cookies"}

        state = self._load()
        stores = dict(state.get("cookie_stores") or {})
        stores[slug] = {
            "app_slug": slug,
            "cookies": list(cookies)[:300],
            "device": device,
            "imported_at": _now(),
            "count": len(cookies),
        }
        state["cookie_stores"] = stores
        self._save(state)

        injected = 0
        tab = (getattr(browser_stream_manager, "tabs", {}) or {}).get(slug)
        if tab is not None and getattr(tab, "page", None) is not None and not tab.page.is_closed():
            try:
                ctx = tab.page.context
                cleaned: List[Dict[str, Any]] = []
                for c in cookies[:300]:
                    if not isinstance(c, dict) or not c.get("name") or not c.get("value"):
                        continue
                    ck = {
                        "name": str(c["name"]),
                        "value": str(c["value"]),
                        "domain": str(c.get("domain") or ""),
                        "path": str(c.get("path") or "/"),
                        "httpOnly": bool(c.get("httpOnly", False)),
                        "secure": bool(c.get("secure", True)),
                    }
                    if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
                        ck["expires"] = float(c["expires"])
                    cleaned.append(ck)
                await ctx.add_cookies(cleaned)
                injected = len(cleaned)
                await tab.page.reload(wait_until="domcontentloaded")
            except Exception as exc:
                return {
                    "ok": False,
                    "reason": f"cookie_injection_failed: {exc.__class__.__name__}: {exc}",
                    "stored": True,
                    "count": len(cookies),
                }
        return {"ok": True, "stored": True, "injected": injected, "count": len(cookies), "app_slug": slug}

    def stored_cookies(self, app_slug: str) -> Dict[str, Any]:
        state = self._load()
        return (state.get("cookie_stores") or {}).get(_slug(app_slug)) or {}

    # ----------------------------------------------------- agent-facing view
    def describe_for_agent(self) -> str:
        cfg = self.config()
        lines = [
            "API BRIDGE (api-anything):",
            f"- server: {cfg['base_url']} (token {'set' if cfg['token'] else 'unset'})",
            f"- learned operations: {cfg['operation_count']}",
        ]
        for b in cfg.get("bridges", [])[:8]:
            lines.append(f"- bridge: {b['app_slug']} -> {b.get('operation') or '(learning)'}")
        return "\n".join(lines)


api_bridge_service = ApiBridgeService()
