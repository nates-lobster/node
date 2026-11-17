#!/usr/bin/env python3
"""
Warm-Session Authentication Bootstrapper (`auth_bootstrap.py`)
==============================================================
Populates `/data/browser_profile` on your headless Debian server so
`scraper_template.py` starts with authenticated sessions for Microsoft Teams,
Outlook, AP Classroom, ProtonMail, and custom tools.

Two supported workflows:
  1. Import storage state captured on your laptop:
       Laptop:  playwright open --save-storage=auth.json https://teams.microsoft.com
       Server:  docker compose run --rm scrapers python3 auth_bootstrap.py --import-state /data/auth.json
  2. Interactive login session (via X11/Wayland/noVNC or SSH port-forwarded CDP):
       Server:  docker compose stop scrapers
                docker compose run --rm -p 127.0.0.1:9222:9222 scrapers \
                    python3 auth_bootstrap.py --cdp-port 9222 --duration 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List

import yaml
from playwright.async_api import async_playwright

from scraper_template import cleanup_stale_chromium_locks

logging.basicConfig(
    level="INFO",
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("hub.auth_bootstrap")


async def import_storage_state(profile_dir: Path, state_json_path: Path) -> None:
    if not state_json_path.is_file():
        raise FileNotFoundError(f"Storage state file not found: {state_json_path}")

    data = json.loads(state_json_path.read_text(encoding="utf-8"))
    cookies = data.get("cookies", [])
    origins = data.get("origins", [])

    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_chromium_locks(profile_dir)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            if cookies:
                await context.add_cookies(cookies)
                logger.info("Imported %d cookie(s) into %s", len(cookies), profile_dir)

            for origin_entry in origins:
                origin_url = origin_entry.get("origin")
                local_storage_items = origin_entry.get("localStorage", [])
                if not origin_url or not local_storage_items:
                    continue
                page = await context.new_page()
                try:
                    await page.goto(origin_url, wait_until="domcontentloaded", timeout=30000)
                    for kv in local_storage_items:
                        await page.evaluate(
                            "([k, v]) => window.localStorage.setItem(k, v)",
                            [kv["name"], kv["value"]],
                        )
                    logger.info(
                        "Hydrated %d localStorage key(s) for %s",
                        len(local_storage_items),
                        origin_url,
                    )
                except Exception as exc:
                    logger.warning("Could not hydrate origin %s: %s", origin_url, exc)
                finally:
                    await page.close()
        finally:
            await context.close()

    logger.info("Bootstrap complete. Restart `scrapers` container to begin monitoring.")


async def interactive_bootstrap(
    profile_dir: Path,
    urls: List[str],
    headed: bool,
    cdp_port: int,
    duration_seconds: int,
) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_chromium_locks(profile_dir)

    args = [
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]
    if cdp_port > 0:
        args.extend(
            [
                "--remote-debugging-address=0.0.0.0",
                f"--remote-debugging-port={cdp_port}",
            ]
        )

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not headed,
            args=args,
            viewport={"width": 1366, "height": 900},
        )
        try:
            for url in urls:
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    logger.info("Opened tab for authentication -> %s", url)
                except Exception as exc:
                    logger.warning("Navigation warning for %s: %s", url, exc)

            logger.info(
                "Bootstrap session active for %ds (CDP port=%s). Press Ctrl+C once logins finish.",
                duration_seconds,
                cdp_port if cdp_port > 0 else "disabled",
            )
            await asyncio.sleep(duration_seconds)
        except KeyboardInterrupt:
            logger.info("Interactive bootstrap ended by user.")
        finally:
            await context.close()
            logger.info("Saved persistent profile cleanly to %s", profile_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap authenticated cookies into /data/browser_profile")
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("BROWSER_PROFILE_DIR", "/data/browser_profile"),
    )
    parser.add_argument(
        "--config",
        default=os.getenv("APPS_CONFIG_PATH", str(Path(__file__).with_name("apps_config.yaml"))),
    )
    parser.add_argument(
        "--import-state",
        help="Path to Playwright storageState JSON file exported from your laptop",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Open headed window (for X11 forwarding or VNC desktop)",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=0,
        help="Expose Chromium DevTools Protocol port (e.g. 9222 over SSH/Tailscale tunnel)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=600,
        help="Seconds to keep bootstrap browser open before saving & exiting",
    )
    args = parser.parse_args()

    profile_dir = Path(args.profile_dir)

    if args.import_state:
        asyncio.run(import_storage_state(profile_dir, Path(args.import_state)))
        return

    urls: List[str] = []
    cfg_path = Path(args.config)
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        urls = [a["url"] for a in raw.get("apps", []) if a.get("enabled", True) and a.get("url")]

    asyncio.run(
        interactive_bootstrap(
            profile_dir=profile_dir,
            urls=urls,
            headed=args.headed,
            cdp_port=args.cdp_port,
            duration_seconds=args.duration,
        )
    )


if __name__ == "__main__":
    main()
