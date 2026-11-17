#!/usr/bin/env python3
"""
Universal Notification Hub — Modular Playwright Warm-Session & State-Diff Daemon
================================================================================
Maintains 24/7 authenticated ("warm") headless browser sessions inside a
persistent Chromium profile (`/data/browser_profile`) and monitors:
  1. DOM Unread Badges (CSS selectors + aria-label count extraction)
  2. Tab Title Regex Indicators (e.g. `(3) Mail - Outlook` via `^\\((\\d+)\\)`)
  3. Zero-Knowledge State Diffing (e.g. ProtonMail `ping_only` integer diffs
     with zero inspection of encrypted DOM payloads or network frames)

Designed for CPU-only Debian home servers orchestrated via Docker Compose / Dockge.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import signal
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
import yaml
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("hub.scrapers")


# -----------------------------------------------------------------------------
# Data Structures & Configuration
# -----------------------------------------------------------------------------
@dataclass
class AppMonitorConfig:
    name: str
    url: str
    enabled: bool = True
    indicator_type: str = "dom_badge"  # dom_badge | title_regex | ping_only
    zero_knowledge: bool = False
    title_regex: str = r"^\((\d+)\)"
    badge_selectors: List[str] = field(default_factory=list)
    preview_selector: Optional[str] = None
    auth_wall_patterns: List[str] = field(default_factory=list)

    @property
    def is_privacy_strict(self) -> bool:
        return self.zero_knowledge or self.indicator_type == "ping_only"


@dataclass
class AppRuntimeState:
    last_count: int = 0
    last_preview_hash: str = ""
    initialized: bool = False
    auth_warning_sent: bool = False
    consecutive_errors: int = 0
    last_warm_reload: float = 0.0


# -----------------------------------------------------------------------------
# Pure Parsing & Diffing Utilities (Unit-Testable Without Browser)
# -----------------------------------------------------------------------------
class IndicatorExtractor:
    """Stateless helpers for parsing unread counts from titles and DOM attributes."""

    INT_FALLBACK_RE = re.compile(r"(\d+)\+?")

    @staticmethod
    def parse_title_count(title: str, pattern: str = r"^\((\d+)\)") -> Optional[int]:
        """Extract unread integer from a browser tab title if regex matches."""
        if not title or not pattern:
            return None
        match = re.search(pattern, title.strip())
        if not match:
            return None
        try:
            return int(match.group(1))
        except (IndexError, ValueError):
            return None

    @classmethod
    def parse_badge_value(
        cls,
        inner_text: Optional[str],
        aria_label: Optional[str] = None,
        data_count: Optional[str] = None,
        element_visible: bool = True,
    ) -> Optional[int]:
        """
        Convert raw DOM badge text or aria-label into an unread integer.
        Examples:
          - inner_text="4" -> 4
          - inner_text="99+" -> 99
          - aria_label="3 unread messages" -> 3
          - visible empty dot badge -> 1
        """
        for candidate in (data_count, inner_text, aria_label):
            if candidate:
                cleaned = candidate.strip()
                m = cls.INT_FALLBACK_RE.search(cleaned)
                if m:
                    return int(m.group(1))

        if element_visible and inner_text is not None:
            # Visible unread indicator pill/dot with no numeric text
            return 1
        return None


class StateDiffEngine:
    """
    Evaluates previous vs. current indicator states and builds outbound `/api/v1/ping`
    payloads while strictly honoring Zero-Knowledge (`ping_only`) rules.
    """

    @staticmethod
    def evaluate_transition(
        config: AppMonitorConfig,
        state: AppRuntimeState,
        current_count: int,
        preview_text: Optional[str] = None,
        matched_source: str = "dom_badge",
    ) -> Optional[Dict[str, Any]]:
        """
        Compare `current_count` (and optional preview hash) against `state`.
        Mutates `state` in place and returns a Ping payload dict ONLY when an
        actionable upward transition or new message fingerprint occurs.
        """
        preview_clean = "" if config.is_privacy_strict else (preview_text or "").strip()[:240]
        preview_hash = (
            hashlib.sha256(preview_clean.encode("utf-8")).hexdigest()[:16]
            if preview_clean
            else ""
        )

        # First observation after fresh boot without persisted state:
        # Establish baseline; if unread > 0 on initial attach, emit initial summary once.
        if not state.initialized:
            state.initialized = True
            prev_count = state.last_count
            state.last_count = current_count
            state.last_preview_hash = preview_hash

            if current_count > prev_count:
                return StateDiffEngine._build_payload(
                    config=config,
                    previous_count=prev_count,
                    current_count=current_count,
                    preview_clean=preview_clean,
                    matched_source=matched_source,
                )
            return None

        previous_count = state.last_count
        previous_hash = state.last_preview_hash

        count_increased = current_count > previous_count
        preview_changed_while_unread = (
            not config.is_privacy_strict
            and current_count > 0
            and bool(preview_hash)
            and preview_hash != previous_hash
            and current_count == previous_count
        )

        state.last_count = current_count
        if preview_hash:
            state.last_preview_hash = preview_hash
        elif current_count == 0:
            state.last_preview_hash = ""

        if count_increased or preview_changed_while_unread:
            return StateDiffEngine._build_payload(
                config=config,
                previous_count=previous_count,
                current_count=current_count,
                preview_clean=preview_clean,
                matched_source=matched_source,
            )

        return None

    @staticmethod
    def _build_payload(
        config: AppMonitorConfig,
        previous_count: int,
        current_count: int,
        preview_clean: str,
        matched_source: str,
    ) -> Dict[str, Any]:
        delta = max(current_count - previous_count, 1)

        # Encrypted / Zero-Knowledge Fallback Strategy (e.g., ProtonMail)
        if config.is_privacy_strict:
            return {
                "source_app": config.name,
                "message": "New secure message detected",
                "indicator_type": "ping_only",
                "unread_count": current_count,
                "app_url": config.url,
                "zero_knowledge": True,
                "raw_payload": {
                    "previous_count": previous_count,
                    "current_count": current_count,
                    "delta": delta,
                    "zero_knowledge": True,
                },
            }

        # Standard Warm-Session Payload
        plural = "s" if current_count != 1 else ""
        title = (
            f"{config.name}: {preview_clean[:95]}"
            if preview_clean
            else f"{current_count} unread notification{plural} in {config.name}"
        )
        summary = (
            preview_clean
            if preview_clean
            else f"Unread counter incremented ({previous_count} → {current_count}) via {matched_source}."
        )

        return {
            "source_app": config.name,
            "indicator_type": config.indicator_type or matched_source,
            "title": title,
            "message": summary,
            "unread_count": current_count,
            "app_url": config.url,
            "zero_knowledge": False,
            "raw_payload": {
                "previous_count": previous_count,
                "current_count": current_count,
                "delta": delta,
                "detector": matched_source,
                "summary": summary,
            },
        }


# -----------------------------------------------------------------------------
# Persistent State Store (Prevents Duplicate Alerts Across Container Restarts)
# -----------------------------------------------------------------------------
class PersistentStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.states: Dict[str, AppRuntimeState] = {}

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for app_name, values in raw.items():
                self.states[app_name] = AppRuntimeState(
                    last_count=int(values.get("last_count", 0)),
                    last_preview_hash=str(values.get("last_preview_hash", "")),
                    initialized=bool(values.get("initialized", False)),
                )
            logger.info("Restored scraper diff state for %d app(s) from %s", len(self.states), self.path)
        except Exception as exc:
            logger.warning("Could not load state file %s (%s); starting fresh.", self.path, exc)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            serializable = {
                name: {
                    "last_count": st.last_count,
                    "last_preview_hash": st.last_preview_hash,
                    "initialized": st.initialized,
                }
                for name, st in self.states.items()
            }
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
            tmp_path.replace(self.path)
        except Exception as exc:
            logger.warning("Failed saving scraper state to %s: %s", self.path, exc)

    def get(self, app_name: str) -> AppRuntimeState:
        if app_name not in self.states:
            self.states[app_name] = AppRuntimeState()
        return self.states[app_name]


# -----------------------------------------------------------------------------
# Resilient HTTP Ping Dispatcher (Buffers Pings During Backend Restarts)
# -----------------------------------------------------------------------------
class PingDispatcher:
    def __init__(self, api_url: str, max_buffer: int = 200) -> None:
        self.api_url = api_url
        self.retry_queue: Deque[Dict[str, Any]] = deque(maxlen=max_buffer)

    async def send(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> bool:
        await self.flush_buffered(client)
        return await self._post_single(client, payload, buffer_on_fail=True)

    async def flush_buffered(self, client: httpx.AsyncClient) -> None:
        attempts = len(self.retry_queue)
        for _ in range(attempts):
            queued = self.retry_queue.popleft()
            ok = await self._post_single(client, queued, buffer_on_fail=False)
            if not ok:
                self.retry_queue.appendleft(queued)
                break

    async def _post_single(
        self,
        client: httpx.AsyncClient,
        payload: Dict[str, Any],
        buffer_on_fail: bool,
    ) -> bool:
        try:
            resp = await client.post(self.api_url, json=payload, timeout=10.0)
            resp.raise_for_status()
            logger.info(
                "Ping delivered -> [%s] (%s) unread=%s",
                payload.get("source_app"),
                payload.get("indicator_type"),
                payload.get("unread_count"),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Ping delivery failed for %s (%s). Buffered=%s",
                payload.get("source_app"),
                exc,
                buffer_on_fail,
            )
            if buffer_on_fail:
                self.retry_queue.append(payload)
            return False


# -----------------------------------------------------------------------------
# Pluggable Per-App Scraper Base Class
# -----------------------------------------------------------------------------
class BaseAppScraper:
    """
    Base monitor for one persistent web app tab.
    Subclass this for custom multi-step DOM scraping, or instantiate directly
    with an `AppMonitorConfig` from `apps_config.yaml`.
    """

    def __init__(
        self,
        config: AppMonitorConfig,
        navigation_timeout_ms: int = 45000,
        warm_reload_minutes: int = 45,
    ) -> None:
        self.config = config
        self.navigation_timeout_ms = navigation_timeout_ms
        self.warm_reload_seconds = warm_reload_minutes * 60
        self.page: Optional[Page] = None

    async def ensure_page(self, context: BrowserContext, state: AppRuntimeState) -> Page:
        """Open warm tab on startup or recreate it automatically if renderer crashed."""
        if self.page is None or self.page.is_closed():
            logger.info("[%s] Opening warm session tab -> %s", self.config.name, self.config.url)
            self.page = await context.new_page()
            self.page.set_default_navigation_timeout(self.navigation_timeout_ms)
            await self.page.goto(self.config.url, wait_until="domcontentloaded")
            state.last_warm_reload = time.monotonic()
            # Brief settle delay for SPAs (Teams / Outlook / ProtonMail)
            await asyncio.sleep(2.5)
        return self.page

    def is_on_auth_wall(self, current_url: str) -> bool:
        lowered = current_url.lower()
        return any(pat.lower() in lowered for pat in self.config.auth_wall_patterns)

    async def extract_indicators(self, page: Page) -> Tuple[int, Optional[str], str]:
        """
        Inspect tab title and configured DOM badge selectors.
        Returns `(unread_count, optional_preview_text, matched_detector_name)`.
        """
        counts: List[Tuple[int, str]] = []

        # 1. Title Regex Parsing (e.g. `(2) Mail - Outlook`)
        try:
            tab_title = await page.title()
            title_count = IndicatorExtractor.parse_title_count(tab_title, self.config.title_regex)
            if title_count is not None:
                counts.append((title_count, "title_regex"))
        except PlaywrightError as exc:
            logger.debug("[%s] Transient error reading page title: %s", self.config.name, exc)

        # 2. DOM Badge Selector Inspection
        for selector in self.config.badge_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                is_visible = await locator.is_visible()
                if not is_visible:
                    continue
                inner_text = await locator.inner_text(timeout=1500)
                aria_label = await locator.get_attribute("aria-label", timeout=1500)
                data_count = await locator.get_attribute("data-unread-count", timeout=1500)
                badge_val = IndicatorExtractor.parse_badge_value(
                    inner_text=inner_text,
                    aria_label=aria_label,
                    data_count=data_count,
                    element_visible=is_visible,
                )
                if badge_val is not None:
                    counts.append((badge_val, "dom_badge"))
                    break
            except PlaywrightError:
                # Graceful handling during React/Angular/Proton DOM re-renders
                continue

        resolved_count, detector = max(counts, key=lambda item: item[0]) if counts else (0, self.config.indicator_type)

        # 3. Optional Preview Extraction (STRICTLY SKIPPED for Zero-Knowledge apps)
        preview_text: Optional[str] = None
        if (
            not self.config.is_privacy_strict
            and resolved_count > 0
            and self.config.preview_selector
        ):
            try:
                prev_loc = page.locator(self.config.preview_selector).first
                if await prev_loc.count() > 0 and await prev_loc.is_visible():
                    raw_txt = await prev_loc.inner_text(timeout=1500)
                    preview_text = " ".join(raw_txt.split())[:200]
            except PlaywrightError:
                preview_text = None

        return resolved_count, preview_text, detector

    async def keep_session_warm(self, page: Page, state: AppRuntimeState) -> None:
        """
        Keep authenticated cookies & OAuth refresh tokens alive without heavy CPU usage.
        """
        now = time.monotonic()
        if now - state.last_warm_reload >= self.warm_reload_seconds:
            jitter = random.uniform(0, 30)
            if now - state.last_warm_reload >= (self.warm_reload_seconds + jitter):
                logger.info("[%s] Performing scheduled warm-session refresh", self.config.name)
                await page.reload(wait_until="domcontentloaded")
                state.last_warm_reload = now
                return

        # Subtle visibility/activity ping inside page context so idle timers don't log out
        try:
            await page.evaluate("() => { window.dispatchEvent(new Event('mousemove')); }")
        except PlaywrightError:
            pass


# -----------------------------------------------------------------------------
# Example Custom Python Subclass (Shows How to Extend Beyond YAML)
# -----------------------------------------------------------------------------
class CustomPortalScraperExample(BaseAppScraper):
    """
    Template showing how to write custom Python DOM logic for bespoke apps
    (e.g., scraping table rows or custom JSON-in-DOM state) while inheriting
    session persistence, resilience, and state diffing.
    """

    async def extract_indicators(self, page: Page) -> Tuple[int, Optional[str], str]:
        # Custom logic can override or augment `super().extract_indicators(page)`
        return await super().extract_indicators(page)


# -----------------------------------------------------------------------------
# Master Daemon Orchestrator
# -----------------------------------------------------------------------------
def cleanup_stale_chromium_locks(profile_dir: Path) -> None:
    """
    Remove stale Chromium Singleton lock files left behind if Docker stopped
    the container ungracefully, ensuring persistent context boots cleanly.
    """
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_file = profile_dir / lock_name
        if lock_file.exists() or lock_file.is_symlink():
            try:
                lock_file.unlink()
                logger.info("Cleaned stale Chromium lock file: %s", lock_file)
            except OSError as exc:
                logger.warning("Could not unlink %s: %s", lock_file, exc)


def load_monitor_configs(config_path: Path) -> Tuple[Dict[str, Any], List[AppMonitorConfig]]:
    if not config_path.is_file():
        raise FileNotFoundError(f"Scraper config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    global_cfg = raw.get("global", {})
    apps_list: List[AppMonitorConfig] = []

    for item in raw.get("apps", []):
        cfg = AppMonitorConfig(
            name=item["name"],
            url=item["url"],
            enabled=bool(item.get("enabled", True)),
            indicator_type=item.get("indicator_type", "dom_badge"),
            zero_knowledge=bool(item.get("zero_knowledge", False)),
            title_regex=item.get("title_regex", r"^\((\d+)\)"),
            badge_selectors=list(item.get("badge_selectors", [])),
            preview_selector=item.get("preview_selector"),
            auth_wall_patterns=list(item.get("auth_wall_patterns", [])),
        )
        if cfg.enabled:
            apps_list.append(cfg)

    return global_cfg, apps_list


class WarmSessionDaemon:
    def __init__(
        self,
        profile_dir: Path,
        state_file: Path,
        config_path: Path,
        hub_api_url: str,
        headless: bool = True,
    ) -> None:
        self.profile_dir = profile_dir
        self.state_store = PersistentStateStore(state_file)
        self.config_path = config_path
        self.dispatcher = PingDispatcher(hub_api_url)
        self.headless = headless
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        logger.info("Shutdown signal received — closing persistent browser profile cleanly...")
        self._stop_event.set()

    async def run(self, run_once: bool = False) -> None:
        global_cfg, app_configs = load_monitor_configs(self.config_path)
        last_cfg_mtime = self.config_path.stat().st_mtime if self.config_path.is_file() else 0.0
        poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", global_cfg.get("poll_interval_seconds", 30)))
        heartbeat_every = int(global_cfg.get("heartbeat_every_cycles", 10))
        warm_reload_min = int(global_cfg.get("warm_reload_minutes", 45))
        nav_timeout_ms = int(global_cfg.get("navigation_timeout_ms", 45000))

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        cleanup_stale_chromium_locks(self.profile_dir)
        self.state_store.load()

        scrapers_by_name: Dict[str, BaseAppScraper] = {
            cfg.name: BaseAppScraper(
                config=cfg,
                navigation_timeout_ms=nav_timeout_ms,
                warm_reload_minutes=warm_reload_min,
            )
            for cfg in app_configs
        }

        chromium_args = [
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            # Prevent Chromium from freezing WebSockets/timers in background warm tabs:
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]

        logger.info(
            "Starting Playwright persistent context at %s (headless=%s, apps=%d)",
            self.profile_dir,
            self.headless,
            len(scrapers_by_name),
        )

        cycle = 0
        async with httpx.AsyncClient() as http_client:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    args=chromium_args,
                    viewport={"width": 1366, "height": 900},
                )

                try:
                    while not self._stop_event.is_set():
                        # Hot-reload apps_config.yaml if modified via WebOS UI or AI Secretary
                        if self.config_path.is_file():
                            current_mtime = self.config_path.stat().st_mtime
                            if current_mtime != last_cfg_mtime:
                                last_cfg_mtime = current_mtime
                                _, updated_configs = load_monitor_configs(self.config_path)
                                updated_names = {c.name for c in updated_configs}
                                for old_name in list(scrapers_by_name.keys()):
                                    if old_name not in updated_names:
                                        old_scraper = scrapers_by_name.pop(old_name)
                                        if old_scraper.page and not old_scraper.page.is_closed():
                                            await old_scraper.page.close()
                                        logger.info("[%s] Closed tab (disabled or removed in config)", old_name)
                                for cfg in updated_configs:
                                    if cfg.name in scrapers_by_name:
                                        scrapers_by_name[cfg.name].config = cfg
                                    else:
                                        scrapers_by_name[cfg.name] = BaseAppScraper(
                                            config=cfg,
                                            navigation_timeout_ms=nav_timeout_ms,
                                            warm_reload_minutes=warm_reload_min,
                                        )
                                        logger.info("[%s] Added new monitored website from hot-reloaded config", cfg.name)

                        cycle += 1
                        send_heartbeat = (cycle == 1) or (cycle % heartbeat_every == 0)

                        for scraper in list(scrapers_by_name.values()):
                            if self._stop_event.is_set():
                                break
                            await self._poll_single_app(
                                scraper=scraper,
                                context=context,
                                http_client=http_client,
                                send_heartbeat=send_heartbeat,
                            )

                        self.state_store.save()

                        if run_once:
                            logger.info("Completed single --once inspection cycle.")
                            break

                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    self.state_store.save()
                    await context.close()
                    logger.info("Persistent browser profile closed safely.")

    async def _poll_single_app(
        self,
        scraper: BaseAppScraper,
        context: BrowserContext,
        http_client: httpx.AsyncClient,
        send_heartbeat: bool,
    ) -> None:
        cfg = scraper.config
        state = self.state_store.get(cfg.name)

        try:
            page = await scraper.ensure_page(context, state)

            # Check if session got logged out / redirected to SSO login wall
            if scraper.is_on_auth_wall(page.url):
                if not state.auth_warning_sent:
                    logger.warning("[%s] Redirected to login wall (%s)", cfg.name, page.url)
                    await self.dispatcher.send(
                        http_client,
                        {
                            "source_app": cfg.name,
                            "indicator_type": "auth_check",
                            "title": f"{cfg.name}: Warm Session Re-Authentication Needed",
                            "message": (
                                "Headless tab detected an SSO/login redirect. "
                                "Run `auth_bootstrap.py` to refresh persistent cookies."
                            ),
                            "priority": "critical",
                            "status": "auth_required",
                            "unread_count": 0,
                            "app_url": cfg.url,
                        },
                    )
                    state.auth_warning_sent = True
                return

            state.auth_warning_sent = False

            current_count, preview_text, detector = await scraper.extract_indicators(page)
            ping_payload = StateDiffEngine.evaluate_transition(
                config=cfg,
                state=state,
                current_count=current_count,
                preview_text=preview_text,
                matched_source=detector,
            )

            if ping_payload is not None:
                await self.dispatcher.send(http_client, ping_payload)
            elif send_heartbeat:
                await self.dispatcher.send(
                    http_client,
                    {
                        "source_app": cfg.name,
                        "indicator_type": "heartbeat",
                        "unread_count": current_count,
                        "app_url": cfg.url,
                        "zero_knowledge": cfg.is_privacy_strict,
                        "create_notification": False,
                    },
                )

            await scraper.keep_session_warm(page, state)
            state.consecutive_errors = 0

        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            state.consecutive_errors += 1
            logger.warning(
                "[%s] Playwright transient failure (#%d): %s",
                cfg.name,
                state.consecutive_errors,
                exc,
            )
            if state.consecutive_errors >= 3 and scraper.page and not scraper.page.is_closed():
                try:
                    await scraper.page.close()
                except PlaywrightError:
                    pass
                scraper.page = None
        except Exception as exc:
            state.consecutive_errors += 1
            logger.exception("[%s] Unexpected worker error (#%d): %s", cfg.name, state.consecutive_errors, exc)


async def send_simulated_demo_pings(hub_api_url: str) -> None:
    """Fire sample state-diff pings (including ProtonMail zero-knowledge fallback) to verify API wiring."""
    demo_payloads = [
        {
            "source_app": "ProtonMail",
            "message": "New secure message detected",
            "indicator_type": "ping_only",
            "unread_count": 2,
            "app_url": "https://mail.proton.me",
        },
        {
            "source_app": "Microsoft Teams",
            "indicator_type": "dom_badge",
            "title": "Microsoft Teams: Direct mention in #homelab-ops",
            "raw_payload": {"previous_count": 3, "current_count": 4, "delta": 1},
            "unread_count": 4,
            "app_url": "https://teams.microsoft.com",
        },
    ]
    dispatcher = PingDispatcher(hub_api_url)
    async with httpx.AsyncClient() as client:
        for payload in demo_payloads:
            await dispatcher.send(client, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright Warm-Session Notification Scraper")
    parser.add_argument(
        "--config",
        default=os.getenv("APPS_CONFIG_PATH", str(Path(__file__).with_name("apps_config.yaml"))),
        help="Path to apps_config.yaml",
    )
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("BROWSER_PROFILE_DIR", "/data/browser_profile"),
        help="Persistent Chromium user_data_dir mount path",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("STATE_FILE_PATH", "/data/scraper_state.json"),
        help="JSON state persistence file path",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("HUB_API_URL", "http://backend:8000/api/v1/ping"),
        help="Target FastAPI POST /api/v1/ping endpoint",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch headed browser (useful for initial login over X11/VNC)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check pass across enabled apps and exit",
    )
    parser.add_argument(
        "--simulate-pings",
        action="store_true",
        help="Send test pings (including ProtonMail zero-knowledge ping_only) to HUB_API_URL and exit",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.simulate_pings:
        asyncio.run(send_simulated_demo_pings(args.api_url))
        return

    daemon = WarmSessionDaemon(
        profile_dir=Path(args.profile_dir),
        state_file=Path(args.state_file),
        config_path=Path(args.config),
        hub_api_url=args.api_url,
        headless=not args.headed,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(daemon.run(run_once=args.once))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
