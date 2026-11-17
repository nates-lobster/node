import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import yaml

from .config import settings


def slugify_app_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "app"


KNOWN_APP_PRESETS: List[Dict[str, Any]] = [
    {
        "keywords": ("powerschool", "/guardian/home"),
        "name": "PowerSchool",
        "url": "https://powerschool.com/",
        "indicator_type": "grades_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "table.linkDescList a[href*='scores.html']",
            "#tblgrades td",
        ],
        "preview_selector": "table.linkDescList tr, #tblgrades tr",
        "auth_wall_patterns": ["/public/home.html", "/guardian/pw.html", "/login", "sso."],
        "explanation": "Scans your PowerSchool courses, letter grades, and percentages into the Grades dashboard and alerts you whenever a grade changes on hourly cron checks.",
    },
    {
        "keywords": ("canvas", "instructure"),
        "name": "Canvas LMS",
        "url": "https://canvas.instructure.com/",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".menu-item__badge",
            "#global_nav_conversations_link .menu-item__badge",
            "[aria-label*='unread' i]",
        ],
        "preview_selector": ".ic-DashboardCard__action-badge, .todo-list-item",
        "auth_wall_patterns": ["/login", "sso."],
        "explanation": "Monitors Canvas global navigation unread badges (Inbox & To-Do sidebar) and tab title counters.",
    },
    {
        "keywords": ("github",),
        "name": "GitHub Notifications",
        "url": "https://github.com/notifications",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".mail-status.unread",
            "[data-target='notification-indicator.badge']",
            ".js-notification-inboxes .Counter",
        ],
        "preview_selector": ".notifications-list-item .markdown-title",
        "auth_wall_patterns": ["github.com/login", "github.com/session"],
        "explanation": "Tracks unread PR/issue notifications in GitHub's inbox counter and header indicator dot.",
    },
    {
        "keywords": ("gmail", "google mail", "mail.google"),
        "name": "Gmail",
        "url": "https://mail.google.com/mail/u/0/#inbox",
        "indicator_type": "title_regex",
        "zero_knowledge": False,
        "title_regex": r"Inbox\s*\((\d+)\)",
        "badge_selectors": [
            "a[href*='#inbox'] + div .bsU",
            "[aria-label*='Inbox'][aria-label*='unread' i]",
        ],
        "preview_selector": "tr.zE .bog",
        "auth_wall_patterns": ["accounts.google.com"],
        "explanation": "Uses Gmail's reliable `Inbox (N)` tab title regex plus unread row subject snippet (`tr.zE .bog`).",
    },
    {
        "keywords": ("ap classroom", "myap.collegeboard", "collegeboard", "apcentral"),
        "name": "AP Classroom",
        "url": "https://myap.collegeboard.org/",
        "indicator_type": "assignment_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "[data-testid*='assignment' i]",
            "[class*='assignment' i]",
            "[aria-label*='assigned' i]",
        ],
        "preview_selector": "[data-testid*='assignment' i], [class*='assignment-card' i], tr, [role='row'], [role='listitem']",
        "auth_wall_patterns": ["account.collegeboard.org", "/login", "idp.collegeboard.org"],
        "explanation": "Scans AP Classroom's rendered DOM cards and JSON payloads after login to capture already-active ('Assigned' / 'In Progress' / 'Due') and newly posted assignments with deduplicated item fingerprinting.",
    },
    {
        "keywords": ("gradescope",),
        "name": "Gradescope",
        "url": "https://www.gradescope.com/",
        "indicator_type": "assignment_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".submissionStatus--text",
            "tr[role='row']",
        ],
        "preview_selector": "tr[role='row'], .table--primaryLink",
        "auth_wall_patterns": ["gradescope.com/login", "/saml"],
        "explanation": "Extracts unsubmitted ('No Submission' / active due date) assignments from Gradescope course tables on page load and background checks.",
    },
    {
        "keywords": ("google classroom", "classroom.google"),
        "name": "Google Classroom",
        "url": "https://classroom.google.com/a/not-turned-in/all",
        "indicator_type": "assignment_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "[aria-label*='assigned' i]",
            "[aria-label*='missing' i]",
            ".unread-badge",
        ],
        "preview_selector": "h2, [role='listitem']",
        "auth_wall_patterns": ["accounts.google.com"],
        "explanation": "Opens your Classroom 'To-Do / Assigned' view to capture both already-active and newly posted assignments.",
    },
    {
        "keywords": ("groupme", "group me"),
        "name": "GroupMe",
        "url": "https://web.groupme.com/chats",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".badge-count",
            "[class*='unread'] .badge",
            "[aria-label*='unread' i]",
            ".chat-list-item.unread",
        ],
        "preview_selector": ".chat-list-item.unread .chat-name, .message-text",
        "api_unread_key": "unread_count",
        "auth_wall_patterns": ["web.groupme.com/signin", "oauth.groupme.com"],
        "explanation": "Streams GroupMe Web on server, intercepts live browser Notification events + `/chats` JSON `unread_count` traffic and unread chat badges.",
    },
    {
        "keywords": ("discord",),
        "name": "Discord",
        "url": "https://discord.com/channels/@me",
        "indicator_type": "title_regex",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "[class*='numberBadge']",
            "[aria-label*='unread' i]",
        ],
        "preview_selector": "[class*='unreadMentionsIndicator']",
        "auth_wall_patterns": ["discord.com/login"],
        "explanation": "Tracks Discord's `(N) Discord` mention counter in the tab title and numeric red mention badges.",
    },
    {
        "keywords": ("slack",),
        "name": "Slack",
        "url": "https://app.slack.com/client",
        "indicator_type": "title_regex",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)|^\* ",
        "badge_selectors": [
            ".p-channel_sidebar__badge",
            "[data-qa='channel_sidebar_name_badge']",
            "[aria-label*='unread' i]",
        ],
        "preview_selector": "[data-qa='channel_sidebar_name_page_unread']",
        "auth_wall_patterns": ["slack.com/signin", "slack.com/workspace-signin"],
        "explanation": "Monitors Slack sidebar mention badges and tab title unread counts.",
    },
    {
        "keywords": ("proton", "protonmail"),
        "name": "ProtonMail",
        "url": "https://mail.proton.me/u/0/inbox",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "[data-testid='navigation-link:inbox'] [data-testid='navigation-counter-item']",
            ".navigation-counter-item",
        ],
        "preview_selector": "[data-shortcut-target='item-container'].unread, [data-testid*='message-item'].unread, .item-container.unread",
        "auth_wall_patterns": ["account.proton.me/login"],
        "explanation": "Captures readable ProtonMail browser notifications, unread sender/subject previews, and JSON counts; falls back to 'ProtonMail ping' if payload is encrypted.",
    },
    {
        "keywords": ("tuta", "tutanota", "signal", "bitwarden", "vault"),
        "name": "Encrypted Vault",
        "url": "https://app.tuta.com/",
        "indicator_type": "ping_only",
        "zero_knowledge": True,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".notification-counter",
        ],
        "preview_selector": None,
        "auth_wall_patterns": ["/login"],
        "explanation": "Count-only monitor for encrypted vaults.",
    },
    {
        "keywords": ("schoology",),
        "name": "Schoology",
        "url": "https://app.schoology.com/home",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            "[data-sgy-sitenav='header-notifications-button'] span",
            ".notif-count",
            "[aria-label*='unread' i]",
        ],
        "preview_selector": ".upcoming-event .event-title",
        "auth_wall_patterns": ["/login"],
        "explanation": "Monitors Schoology's notification bell, active courses, and `/iapi` overdue & upcoming assignments.",
    },
    {
        "keywords": ("canvas", "instructure"),
        "name": "Canvas LMS",
        "url": "https://canvas.instructure.com/",
        "indicator_type": "grades_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".menu-item__badge",
            "[data-testid='unread-badge']",
        ],
        "preview_selector": ".PlannerItem, .ic-DashboardCard",
        "auth_wall_patterns": ["/login"],
        "explanation": "Intercepts Canvas `/api/v1/courses`, grades, scored/missing assignments, and Planner to-dos.",
    },
    {
        "keywords": ("google classroom", "classroom.google"),
        "name": "Google Classroom",
        "url": "https://classroom.google.com/",
        "indicator_type": "grades_tracker",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": ["[aria-label*='unread' i]"],
        "preview_selector": ".YVvGBb",
        "auth_wall_patterns": ["accounts.google.com"],
        "explanation": "Tracks Google Classroom courses, posted grades, and Assigned/Missing coursework.",
    },
    {
        "keywords": ("notion",),
        "name": "Notion Inbox",
        "url": "https://www.notion.so/",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".notion-sidebar-container [style*='background: rgb(235, 87, 87)']",
            "[aria-label*='Inbox'] + div",
        ],
        "preview_selector": None,
        "auth_wall_patterns": ["notion.so/login"],
        "explanation": "Watches Notion's `(N)` tab title counter and red sidebar Inbox notification badge.",
    },
    {
        "keywords": ("home assistant", "homeassistant", ":8123"),
        "name": "Home Assistant",
        "url": "http://homeassistant.local:8123/",
        "indicator_type": "dom_badge",
        "zero_knowledge": False,
        "title_regex": r"^\((\d+)\)",
        "badge_selectors": [
            ".notification-badge",
            "[aria-label*='Notifications' i]",
        ],
        "preview_selector": None,
        "auth_wall_patterns": ["/auth/authorize"],
        "explanation": "Tracks persistent Home Assistant sidebar notification badges on your LAN/Tailscale.",
    },
]


APP_LIBRARY_CATALOG: List[Dict[str, Any]] = [
    # --- Email ---
    {
        "template_id": "gmail",
        "category": "Email",
        "name": "Gmail",
        "tagline": "Bulk unread email sync + sender/subject extraction",
        "url": "https://mail.google.com/mail/u/0/#inbox",
        "needs_custom_domain": False,
        "config": {
            "name": "Gmail",
            "url": "https://mail.google.com/mail/u/0/#inbox",
            "indicator_type": "title_regex",
            "zero_knowledge": False,
            "title_regex": r"Inbox\s*\((\d+)\)",
            "badge_selectors": [".bsU", "[aria-label*='Inbox'] .bsU"],
            "preview_selector": "tr.zA.zE",
            "auth_wall_patterns": ["accounts.google.com"],
        },
    },
    {
        "template_id": "protonmail",
        "category": "Email",
        "name": "ProtonMail",
        "tagline": "End-to-end encrypted inbox monitor + `.sr-only` clean previews",
        "url": "https://mail.proton.me/u/0/inbox",
        "needs_custom_domain": False,
        "config": {
            "name": "ProtonMail",
            "url": "https://mail.proton.me/u/0/inbox",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [
                "[data-testid='navigation-link:inbox'] [data-testid='navigation-counter-item']",
                ".navigation-counter-item",
            ],
            "preview_selector": "[data-shortcut-target='item-container'].unread, [data-testid*='message-item'].unread, .item-container.unread",
            "auth_wall_patterns": ["account.proton.me/login"],
        },
    },
    {
        "template_id": "outlook",
        "category": "Email",
        "name": "Outlook Mail",
        "tagline": "Microsoft 365 & Outlook.com unread inbox sync",
        "url": "https://outlook.office.com/mail/",
        "needs_custom_domain": False,
        "config": {
            "name": "Outlook",
            "url": "https://outlook.office.com/mail/",
            "indicator_type": "title_regex",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["span[title*='Inbox'] + span"],
            "preview_selector": "[aria-label*='Unread' i]",
            "auth_wall_patterns": ["login.microsoftonline.com", "login.live.com"],
        },
    },
    # --- Messaging & Team Collaboration ---
    {
        "template_id": "ms-teams",
        "category": "Messaging",
        "name": "Microsoft Teams",
        "tagline": "Live activity & chat unread badges + message previews",
        "url": "https://teams.microsoft.com/v2/",
        "needs_custom_domain": False,
        "config": {
            "name": "Microsoft Teams",
            "url": "https://teams.microsoft.com/v2/",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["[data-tid='activity-unread-badge']", "[data-tid='chat-unread-badge']", ".fui-CounterBadge"],
            "preview_selector": "[data-tid='chat-list-item-unread']",
            "auth_wall_patterns": ["login.microsoftonline.com", "login.live.com"],
        },
    },
    {
        "template_id": "groupme",
        "category": "Messaging",
        "name": "GroupMe",
        "tagline": "Clubs, teams & campus group chats (`/chats` JSON + live notifications)",
        "url": "https://web.groupme.com/chats",
        "needs_custom_domain": False,
        "config": {
            "name": "GroupMe",
            "url": "https://web.groupme.com/chats",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".badge-count", "[class*='unread']"],
            "preview_selector": ".chat-list-item.unread .chat-name, .message-text",
            "auth_wall_patterns": ["web.groupme.com/signin", "oauth.groupme.com"],
        },
    },
    {
        "template_id": "slack",
        "category": "Messaging",
        "name": "Slack",
        "tagline": "Workspace DMs, mentions & channel unread badges",
        "url": "https://app.slack.com/client",
        "needs_custom_domain": False,
        "config": {
            "name": "Slack",
            "url": "https://app.slack.com/client",
            "indicator_type": "title_regex",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)|^\* ",
            "badge_selectors": [".p-channel_sidebar__badge", "[data-qa='channel_sidebar_name_badge']"],
            "preview_selector": "[data-qa='channel_sidebar_name_page_unread']",
            "auth_wall_patterns": ["slack.com/signin", "slack.com/workspace-signin"],
        },
    },
    {
        "template_id": "discord",
        "category": "Messaging",
        "name": "Discord",
        "tagline": "Direct messages & server mention counters",
        "url": "https://discord.com/channels/@me",
        "needs_custom_domain": False,
        "config": {
            "name": "Discord",
            "url": "https://discord.com/channels/@me",
            "indicator_type": "title_regex",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["[class*='numberBadge']"],
            "preview_selector": "[class*='unreadMentionsIndicator']",
            "auth_wall_patterns": ["discord.com/login"],
        },
    },
    {
        "template_id": "whatsapp",
        "category": "Messaging",
        "name": "WhatsApp Web",
        "tagline": "QR-paired encrypted chat monitor",
        "url": "https://web.whatsapp.com/",
        "needs_custom_domain": False,
        "config": {
            "name": "WhatsApp",
            "url": "https://web.whatsapp.com/",
            "indicator_type": "title_regex",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["span[aria-label*='unread message' i]"],
            "preview_selector": None,
            "auth_wall_patterns": [],
        },
    },
    # --- Academic & LMS Portals ---
    {
        "template_id": "powerschool",
        "category": "Academic & LMS",
        "name": "PowerSchool SIS",
        "tagline": "All-course gradebook, GPA, trajectories & XTE individual assignments",
        "url": "https://powerschool.example.edu/guardian/home.html",
        "needs_custom_domain": True,
        "url_placeholder": "https://yourdistrict.powerschool.com/guardian/home.html",
        "config": {
            "name": "PowerSchool",
            "url": "https://powerschool.example.edu/guardian/home.html",
            "indicator_type": "grades_tracker",
            "zero_knowledge": False,
            "title_regex": r"\((\d+)\)",
            "badge_selectors": ["a[href*='scores.html']", "td.bold"],
            "auth_wall_patterns": ["/public/home.html"],
        },
    },
    {
        "template_id": "schoology",
        "category": "Academic & LMS",
        "name": "Schoology LMS",
        "tagline": "Native `/iapi` overdue & upcoming assignment sync + courses + gradebook",
        "url": "https://app.schoology.com/home",
        "needs_custom_domain": True,
        "url_placeholder": "https://yourschool.schoology.com/home (or https://app.schoology.com/home)",
        "config": {
            "name": "Schoology",
            "url": "https://app.schoology.com/home",
            "indicator_type": "grades_tracker",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["[data-sgy-sitenav='header-notifications-button'] span", ".notif-count"],
            "preview_selector": ".upcoming-event .event-title",
            "auth_wall_patterns": ["/login"],
        },
    },
    {
        "template_id": "canvas",
        "category": "Academic & LMS",
        "name": "Canvas LMS",
        "tagline": "Auto-intercepts `/api/v1/courses`, live grades, submissions & Planner tasks",
        "url": "https://canvas.instructure.com/",
        "needs_custom_domain": True,
        "url_placeholder": "https://yourschool.instructure.com/",
        "config": {
            "name": "Canvas LMS",
            "url": "https://canvas.instructure.com/",
            "indicator_type": "grades_tracker",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".menu-item__badge"],
            "preview_selector": ".PlannerItem, .ic-DashboardCard",
            "auth_wall_patterns": ["/login"],
        },
    },
    {
        "template_id": "google-classroom",
        "category": "Academic & LMS",
        "name": "Google Classroom",
        "tagline": "Coursework stream, posted grades & Missing/Assigned tasks",
        "url": "https://classroom.google.com/",
        "needs_custom_domain": False,
        "config": {
            "name": "Google Classroom",
            "url": "https://classroom.google.com/",
            "indicator_type": "grades_tracker",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["[aria-label*='unread' i]"],
            "preview_selector": ".YVvGBb",
            "auth_wall_patterns": ["accounts.google.com"],
        },
    },
    {
        "template_id": "gradescope",
        "category": "Academic & LMS",
        "name": "Gradescope",
        "tagline": "Autograded programming & problem-set submissions",
        "url": "https://www.gradescope.com/",
        "needs_custom_domain": False,
        "config": {
            "name": "Gradescope",
            "url": "https://www.gradescope.com/",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".courseBox"],
            "preview_selector": ".table--assignments tbody tr",
            "auth_wall_patterns": ["gradescope.com/login"],
        },
    },
    {
        "template_id": "ap-classroom",
        "category": "Academic & LMS",
        "name": "AP Classroom",
        "tagline": "College Board AP daily videos, MCQs & FRQ deadlines",
        "url": "https://myap.collegeboard.org/",
        "needs_custom_domain": False,
        "config": {
            "name": "AP Classroom",
            "url": "https://myap.collegeboard.org/",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": ["[class*='badge']"],
            "preview_selector": "[data-testid*='assignment']",
            "auth_wall_patterns": ["account.collegeboard.org"],
        },
    },
    {
        "template_id": "infinite-campus",
        "category": "Academic & LMS",
        "name": "Infinite Campus",
        "tagline": "District SIS gradebook & missing coursework monitor",
        "url": "https://campus.example.edu/campus/portal/students/",
        "needs_custom_domain": True,
        "url_placeholder": "https://yourdistrict.infinitecampus.org/campus/portal/students/",
        "config": {
            "name": "Infinite Campus",
            "url": "https://campus.example.edu/campus/portal/students/",
            "indicator_type": "grades_tracker",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".grade"],
            "auth_wall_patterns": ["/portal/"],
        },
    },
    # --- Productivity & Dev ---
    {
        "template_id": "github",
        "category": "Productivity & Dev",
        "name": "GitHub Notifications",
        "tagline": "PR reviews, CI status & issue mentions",
        "url": "https://github.com/notifications",
        "needs_custom_domain": False,
        "config": {
            "name": "GitHub",
            "url": "https://github.com/notifications",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".mail-status.unread", ".notification-indicator .mail-status"],
            "preview_selector": ".notifications-list-item.notification-unread",
            "auth_wall_patterns": ["github.com/login"],
        },
    },
    {
        "template_id": "notion",
        "category": "Productivity & Dev",
        "name": "Notion",
        "tagline": "Workspace pages & sidebar inbox updates",
        "url": "https://www.notion.so/",
        "needs_custom_domain": False,
        "config": {
            "name": "Notion",
            "url": "https://www.notion.so/",
            "indicator_type": "dom_badge",
            "zero_knowledge": False,
            "title_regex": r"^\((\d+)\)",
            "badge_selectors": [".notion-sidebar-container [style*='background: rgb(235, 87, 87)']"],
            "auth_wall_patterns": ["notion.so/login"],
        },
    },
]


class AppsConfigManager:
    """Reads and writes `scrapers/apps_config.yaml` safely for UI and AI management."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config_path = Path(config_path or settings.apps_config_path)

    def _read_raw(self) -> Dict[str, Any]:
        if not self.config_path.is_file():
            return {
                "global": {
                    "poll_interval_seconds": 30,
                    "heartbeat_every_cycles": 10,
                    "warm_reload_minutes": 45,
                    "navigation_timeout_ms": 45000,
                },
                "apps": [],
            }
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if "global" not in data or not isinstance(data["global"], dict):
            data["global"] = {
                "poll_interval_seconds": 30,
                "heartbeat_every_cycles": 10,
                "warm_reload_minutes": 45,
                "navigation_timeout_ms": 45000,
            }
        if "apps" not in data or not isinstance(data["apps"], list):
            data["apps"] = []
        return data

    def _write_raw(self, data: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# =============================================================================\n"
            "# Universal Notification Hub — Modular App Scraper Registry\n"
            "# Managed via WebOS Apps Studio or direct YAML edits (hot-reloaded by scrapers).\n"
            "# =============================================================================\n\n"
        )
        yaml_body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        full_content = header + yaml_body

        # Docker single-file bind mounts (`./scrapers/apps_config.yaml:/config/apps_config.yaml`)
        # hold an active mount inode, so `os.replace(tmp, target)` raises EBUSY (errno 16) or EXDEV (errno 18).
        # Fall back cleanly to direct in-place write when atomic rename is rejected by the mount!
        tmp = self.config_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(full_content, encoding="utf-8")
            tmp.replace(self.config_path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            self.config_path.write_text(full_content, encoding="utf-8")

    def get_global_and_apps(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        raw = self._read_raw()
        normalized: List[Dict[str, Any]] = []
        for entry in raw.get("apps", []):
            name = str(entry.get("name", "Unnamed App")).strip()
            zk = bool(entry.get("zero_knowledge", False) or entry.get("indicator_type") == "ping_only")
            raw_prof = str(entry.get("account_profile") or "default").strip()
            prof = slugify_app_name(raw_prof) if raw_prof else "default"
            normalized.append(
                {
                    "slug": slugify_app_name(name),
                    "name": name,
                    "enabled": bool(entry.get("enabled", True)),
                    "url": str(entry.get("url", "")).strip(),
                    "account_profile": prof,
                    "indicator_type": "ping_only" if zk else str(entry.get("indicator_type", "dom_badge")),
                    "zero_knowledge": zk,
                    "title_regex": str(entry.get("title_regex") or r"^\((\d+)\)"),
                    "badge_selectors": list(entry.get("badge_selectors") or []),
                    "preview_selector": entry.get("preview_selector") or None,
                    "api_unread_key": entry.get("api_unread_key") or None,
                    "calibration_notes": entry.get("calibration_notes") or None,
                    "auth_wall_patterns": list(entry.get("auth_wall_patterns") or []),
                    "inbox_categories": [
                        str(c).strip()[:40]
                        for c in (entry.get("inbox_categories") or [])
                        if str(c).strip()
                    ],
                }
            )
        return raw["global"], normalized

    def add_or_replace_app(self, app_payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = self._read_raw()
        name = str(app_payload.get("name", "")).strip()
        if not name:
            raise ValueError("App name is required")
        url = str(app_payload.get("url", "")).strip()
        if not url:
            raise ValueError("App URL is required")
        if not url.startswith(("http://", "https://", "data:")):
            url = f"https://{url}"

        zk = bool(app_payload.get("zero_knowledge", False) or app_payload.get("indicator_type") == "ping_only")
        indicator_type = "ping_only" if zk else str(app_payload.get("indicator_type") or "dom_badge")
        raw_prof = str(app_payload.get("account_profile") or "default").strip()
        account_profile = slugify_app_name(raw_prof) if raw_prof else "default"

        clean_entry: Dict[str, Any] = {
            "name": name,
            "enabled": bool(app_payload.get("enabled", True)),
            "url": url,
            "account_profile": account_profile,
            "indicator_type": indicator_type,
            "zero_knowledge": zk,
            "title_regex": str(app_payload.get("title_regex") or r"^\((\d+)\)"),
            "badge_selectors": [
                s.strip() for s in (app_payload.get("badge_selectors") or []) if str(s).strip()
            ],
        }
        prev = app_payload.get("preview_selector")
        if prev and not zk and str(prev).strip():
            clean_entry["preview_selector"] = str(prev).strip()
        if app_payload.get("api_unread_key"):
            clean_entry["api_unread_key"] = str(app_payload["api_unread_key"]).strip()
        if app_payload.get("calibration_notes"):
            clean_entry["calibration_notes"] = str(app_payload["calibration_notes"]).strip()
        clean_entry["auth_wall_patterns"] = [
            p.strip() for p in (app_payload.get("auth_wall_patterns") or []) if str(p).strip()
        ]
        if app_payload.get("inbox_categories"):
            clean_entry["inbox_categories"] = [
                str(c).strip()[:40]
                for c in (app_payload.get("inbox_categories") or [])
                if str(c).strip()
            ]

        target_slug = slugify_app_name(name)
        replaced = False
        new_apps: List[Dict[str, Any]] = []
        for existing in raw["apps"]:
            if slugify_app_name(str(existing.get("name", ""))) == target_slug:
                new_apps.append(clean_entry)
                replaced = True
            else:
                new_apps.append(existing)
        if not replaced:
            new_apps.append(clean_entry)

        raw["apps"] = new_apps
        self._write_raw(raw)

        _, all_apps = self.get_global_and_apps()
        return next(a for a in all_apps if a["slug"] == target_slug)

    def update_app(self, slug_or_name: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw = self._read_raw()
        target_slug = slugify_app_name(slug_or_name)
        found_idx: Optional[int] = None

        for idx, existing in enumerate(raw["apps"]):
            if slugify_app_name(str(existing.get("name", ""))) == target_slug:
                found_idx = idx
                break

        if found_idx is None:
            return None

        old_name = str(raw["apps"][found_idx].get("name", "")).strip()
        old_slug = slugify_app_name(old_name)

        merged = dict(raw["apps"][found_idx])
        for k, v in updates.items():
            if v is not None:
                merged[k] = v

        if "inbox_categories" in merged and isinstance(merged["inbox_categories"], list):
            seen_cats = set()
            dedup_cats: List[str] = []
            for c in merged["inbox_categories"]:
                sc = str(c).strip()[:40]
                if sc and sc.lower() not in seen_cats:
                    seen_cats.add(sc.lower())
                    dedup_cats.append(sc)
            merged["inbox_categories"] = dedup_cats

        if "url" in merged and merged["url"] and not str(merged["url"]).startswith(("http://", "https://", "data:")):
            merged["url"] = f"https://{merged['url']}"

        if merged.get("zero_knowledge") or merged.get("indicator_type") == "ping_only":
            merged["zero_knowledge"] = True
            merged["indicator_type"] = "ping_only"
            merged.pop("preview_selector", None)

        raw["apps"][found_idx] = merged
        self._write_raw(raw)

        new_slug = slugify_app_name(str(merged.get("name", "")))
        _, all_apps = self.get_global_and_apps()
        found = next((a for a in all_apps if a["slug"] == new_slug), None)
        if found is not None:
            found = dict(found)
            found["previous_name"] = old_name
            found["previous_slug"] = old_slug
        return found

    def delete_app(self, slug_or_name: str) -> bool:
        raw = self._read_raw()
        target_slug = slugify_app_name(slug_or_name)
        before = len(raw["apps"])
        raw["apps"] = [
            a for a in raw["apps"] if slugify_app_name(str(a.get("name", ""))) != target_slug
        ]
        if len(raw["apps"]) == before:
            return False
        self._write_raw(raw)
        return True

    def list_apps(self) -> List[Dict[str, Any]]:
        return list(self._read_raw().get("apps", []))

    def list_catalog(self) -> List[Dict[str, Any]]:
        _, installed_apps = self.get_global_and_apps()
        installed_slugs = {a["slug"] for a in installed_apps}
        out: List[Dict[str, Any]] = []
        for item in APP_LIBRARY_CATALOG:
            cfg = item.get("config") or {}
            slug = slugify_app_name(cfg.get("name") or item["name"])
            out.append(
                {
                    **item,
                    "slug": slug,
                    "installed": slug in installed_slugs or item["template_id"] in installed_slugs,
                }
            )
        return out

    def install_from_catalog(
        self,
        template_id: str,
        custom_name: Optional[str] = None,
        custom_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        tid = slugify_app_name(template_id)
        tmpl = next(
            (
                t
                for t in APP_LIBRARY_CATALOG
                if t["template_id"] == tid
                or slugify_app_name(t["name"]) == tid
                or slugify_app_name((t.get("config") or {}).get("name", "")) == tid
            ),
            None,
        )
        if not tmpl:
            return None
        cfg = dict(tmpl.get("config") or {})
        if custom_name and custom_name.strip():
            cfg["name"] = custom_name.strip()
        if custom_url and custom_url.strip():
            u = custom_url.strip()
            if not u.startswith(("http://", "https://")):
                u = f"https://{u}"
            cfg["url"] = u
        return self.add_or_replace_app(cfg)

    def duplicate_for_new_account(
        self,
        slug_or_name: str,
        custom_name: Optional[str] = None,
        custom_profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Clone an existing app monitor (or an App Library catalog template if not installed yet)
        onto an isolated persistent browser cookie jar (`account_profile`) so the user can sign
        into a 2nd/3rd account concurrently without session/cookie collisions.
        """
        _, all_apps = self.get_global_and_apps()
        target_slug = slugify_app_name(slug_or_name)
        src = next((a for a in all_apps if a["slug"] == target_slug), None)
        if not src:
            # Also check APP_LIBRARY_CATALOG so duplicating a template works on 0-app fresh installs
            tmpl = next(
                (
                    t
                    for t in APP_LIBRARY_CATALOG
                    if t["template_id"] == target_slug
                    or slugify_app_name((t.get("config") or {}).get("name", "")) == target_slug
                ),
                None,
            )
            if tmpl:
                src = {**dict(tmpl["config"]), "slug": slugify_app_name(tmpl["config"]["name"])}
            else:
                raise KeyError(f"App '{slug_or_name}' not found")

        existing_slugs = {a["slug"] for a in all_apps}
        base_name = re.sub(r"\s*\(\d+\)\s*$", "", src["name"]).strip()

        if custom_name and custom_name.strip():
            new_name = custom_name.strip()
        else:
            idx = 2
            while slugify_app_name(f"{base_name} ({idx})") in existing_slugs:
                idx += 1
            new_name = f"{base_name} ({idx})"

        new_slug = slugify_app_name(new_name)
        prof = slugify_app_name(custom_profile) if custom_profile else f"{new_slug}-profile"
        if prof == "default":
            prof = f"{new_slug}-profile"

        cloned = dict(src)
        cloned["name"] = new_name
        cloned["account_profile"] = prof
        cloned["calibration_notes"] = f"Isolated cookie profile `{prof}` — sign in independently"
        return self.add_or_replace_app(cloned)

    async def build_ai_suggestion(
        self,
        prompt_or_url: str,
        zero_knowledge_preferred: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate a ready-to-save monitor configuration from a user description or URL,
        including a safe live HTTP redirect/title probe if a URL was supplied.
        """
        query = prompt_or_url.strip()
        lowered = query.lower()

        # Extract URL if embedded in natural language
        url_match = re.search(r"(https?://[^\s,]+)", query)
        extracted_url = url_match.group(1) if url_match else ""
        if not extracted_url and "." in query and " " not in query:
            extracted_url = f"https://{query}"

        # Match against curated platform presets first
        matched_preset: Optional[Dict[str, Any]] = None
        for preset in KNOWN_APP_PRESETS:
            if any(kw in lowered for kw in preset["keywords"]):
                matched_preset = dict(preset)
                break

        probed_info: Dict[str, Any] = {"probed": False}
        discovered_auth_pattern: Optional[str] = None
        discovered_title: Optional[str] = None

        if extracted_url:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=3.5,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0.0.0"},
                ) as client:
                    resp = await client.get(extracted_url)
                    final_url = str(resp.url)
                    probed_info = {
                        "probed": True,
                        "status_code": resp.status_code,
                        "final_url": final_url,
                    }
                    # Check if initial request redirected onto a login/SSO page
                    orig_host = urlparse(extracted_url).netloc.lower()
                    final_parsed = urlparse(final_url)
                    if any(tok in final_url.lower() for tok in ("login", "signin", "auth", "sso", "oauth")):
                        if final_parsed.netloc.lower() != orig_host:
                            discovered_auth_pattern = final_parsed.netloc.lower()
                        elif final_parsed.path and final_parsed.path != "/":
                            discovered_auth_pattern = final_parsed.path
                    title_m = re.search(r"<title[^>]*>(.*?)</title>", resp.text[:8000], re.IGNORECASE | re.DOTALL)
                    if title_m:
                        discovered_title = " ".join(title_m.group(1).split())[:80]
                        probed_info["page_title"] = discovered_title
            except Exception as exc:
                probed_info = {"probed": False, "note": f"Could not live-probe URL ({exc.__class__.__name__})"}

        if matched_preset:
            name = matched_preset["name"]
            url = extracted_url or matched_preset["url"]
            zk = True if zero_knowledge_preferred else bool(matched_preset["zero_knowledge"])
            config = {
                "name": name,
                "enabled": True,
                "url": url,
                "indicator_type": "ping_only" if zk else matched_preset["indicator_type"],
                "zero_knowledge": zk,
                "title_regex": matched_preset["title_regex"],
                "badge_selectors": list(matched_preset["badge_selectors"]),
                "preview_selector": None if zk else matched_preset.get("preview_selector"),
                "auth_wall_patterns": list(matched_preset["auth_wall_patterns"]),
            }
            if discovered_auth_pattern and discovered_auth_pattern not in config["auth_wall_patterns"]:
                config["auth_wall_patterns"].append(discovered_auth_pattern)
            explanation = matched_preset["explanation"]
        else:
            # Derive clean name and smart generic selectors from URL or text
            if extracted_url:
                parsed = urlparse(extracted_url)
                host_parts = [p for p in parsed.netloc.split(".") if p not in ("www", "app", "my", "mail", "com", "org", "net", "edu", "io")]
                derived_name = host_parts[0].replace("-", " ").title() if host_parts else parsed.netloc
                url = extracted_url
            else:
                derived_name = query.title()[:40] or "Custom Web App"
                slug_host = slugify_app_name(derived_name)
                url = f"https://{slug_host}.com"

            if discovered_title and not any(tok in discovered_title.lower() for tok in ("sign in", "log in", "just a moment", "error")):
                derived_name = discovered_title.split("|")[0].split("—")[0].split("-")[0].strip()[:40] or derived_name

            zk = bool(zero_knowledge_preferred)
            auth_patterns = [discovered_auth_pattern] if discovered_auth_pattern else ["/login", "/signin"]

            config = {
                "name": derived_name,
                "enabled": True,
                "url": url,
                "indicator_type": "ping_only" if zk else "dom_badge",
                "zero_knowledge": zk,
                "title_regex": r"^\((\d+)\)",
                "badge_selectors": [
                    "[aria-label*='unread' i]",
                    "[aria-label*='notification' i]",
                    ".unread-badge",
                    ".notification-badge",
                    "[data-unread-count]",
                ],
                "preview_selector": None if zk else ".notification-item, .unread",
                "auth_wall_patterns": auth_patterns,
            }
            explanation = (
                f"Prepared a universal monitor for **{derived_name}**. It checks both `(N)` tab title counters "
                f"and common unread badge attributes (`aria-label*='unread'`, `.unread-badge`)."
            )
            if discovered_auth_pattern:
                explanation += f" Live URL check detected a login redirect (`{discovered_auth_pattern}`), so I added it to the SSO login-wall detector automatically."

        tips = [
            "Once saved, run `auth_bootstrap.py` on your Debian server if this site requires an authenticated session.",
            "Toggle 'Zero-Knowledge (Ping Only)' on if this app holds private/encrypted messages you never want summarized.",
            "To target a specific badge element, right-click the unread badge in Chrome → Inspect → copy its CSS selector.",
        ]

        return {
            "config": config,
            "explanation": explanation,
            "tips": tips,
            "probe": probed_info,
        }


apps_manager = AppsConfigManager()
