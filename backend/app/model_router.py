import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from .config import settings
from .memory_store import memory_store
from .models import normalize_priority_value


# ---------------------------------------------------------------------------
# Accessibility & Scraped Webmail Artifact Scrubber (ProtonMail / Gmail / Teams)
# ---------------------------------------------------------------------------
KNOWN_SERVICE_PREFIX_RE = re.compile(
    r"^(?:proton\s*mail|proton|gmail|google\s*mail|outlook|microsoft\s*teams|teams|slack|discord|canvas|schoology|ap\s*classroom|powerschool)\s*:\s*",
    re.IGNORECASE,
)

# Matches glued accessibility / button labels emitted by ProtonMail, Gmail, Outlook, Teams:
# e.g., "SStar conversationUnread emailstella.rua..." or "Star conversation Unread email ..."
ARIA_JUNK_PATTERNS = [
    # ProtonMail icon letter + sr-only label collisions ("SStar conversation", "UUnread email")
    (re.compile(r"\bS?Star\s+conversation\b", re.IGNORECASE), " "),
    (re.compile(r"\bUnstar\s+conversation\b", re.IGNORECASE), " "),
    (re.compile(r"\bS?Star\s+message\b", re.IGNORECASE), " "),
    (re.compile(r"\bSelect\s+(?:message|conversation|item|email|row)\b", re.IGNORECASE), " "),
    (re.compile(r"\b(?:Unread|Read)\s+(?:email|message|conversation|thread|item)(?=[A-Za-z0-9\"'<(\s]|$)", re.IGNORECASE), " "),
    (re.compile(r"\bMark\s+as\s+(?:un)?read\b", re.IGNORECASE), " "),
    (re.compile(r"\bMove\s+to\s+(?:trash|archive|inbox|spam)\b", re.IGNORECASE), " "),
    (re.compile(r"\bHas\s+attachment(?:s)?\b", re.IGNORECASE), " "),
    (re.compile(r"\bReplied\s+to\b", re.IGNORECASE), " "),
    (re.compile(r"\bForwarded\s+message\b", re.IGNORECASE), " "),
]


def scrub_notification_artifacts(
    source_app: str,
    raw_title: str,
    raw_message: str = "",
) -> Dict[str, Any]:
    """
    Deterministically clean scraped DOM/accessibility artifacts before & alongside AI/Von:
      1. Strips redundant `<Service>:` prefixes (since service badge is shown separately).
      2. Detects & strips ProtonMail/Gmail/Teams screen-reader labels (`SStar conversation`,
         `Unread email`, `Select message`) even when concatenated without spaces!
      3. Restores missing word boundaries (`conversationUnread` -> `conversation Unread`,
         `emailstella.rua` -> `stella.rua`, `SubjectPreview` boundaries).
      4. Extracts clean `(title, preview, is_starred)`.
    """
    app_clean = (source_app or "").strip()
    combined_title = str(raw_title or "").replace("\u00a0", " ").replace("\u200b", "").strip()
    combined_msg = str(raw_message or "").replace("\u00a0", " ").replace("\u200b", "").strip()

    is_starred = bool(re.search(r"\bS?Starred\b", combined_title, re.IGNORECASE)) and not bool(
        re.search(r"\bS?Star\s+conversation\b", combined_title, re.IGNORECASE)
    )

    def _clean_segment(txt: str) -> str:
        if not txt:
            return ""
        s = txt.strip()
        # Strip explicit "<source_app>:" prefix if duplicated in title
        if app_clean:
            s = re.sub(rf"^{re.escape(app_clean)}\s*:\s*", "", s, flags=re.IGNORECASE)
        s = KNOWN_SERVICE_PREFIX_RE.sub("", s)

        # Unglue known ProtonMail / webmail adjacent token collisions FIRST
        s = re.sub(r"(conversation)(Unread|Read)\b", r"\1 \2", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(Unread\s+email|Read\s+email|Unread\s+message)([A-Za-z0-9._%+-]+)", r"\1 \2", s, flags=re.IGNORECASE)
        s = re.sub(r"\bSStar\s+conversation", " ", s, flags=re.IGNORECASE)

        for pat, repl in ARIA_JUNK_PATTERNS:
            s = pat.sub(repl, s)

        # Separate glued email addresses from surrounding capitalized words:
        # e.g. "stella.rua@domain.comMeeting Tomorrow" -> "stella.rua@domain.com — Meeting Tomorrow"
        s = re.sub(
            r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})([A-Z][a-z])",
            r"\1 — \2",
            s,
        )
        # Separate glued time stamps like "3:42 PMHey can we" or "Oct 24Assignment"
        s = re.sub(r"(\b\d{1,2}:\d{2}\s*(?:AM|PM))([A-Za-z])", r"\1 — \2", s, flags=re.IGNORECASE)
        # Separate lowercase-to-Uppercase collisions when clearly two DOM spans ("ruaWeekly Update" -> "rua — Weekly Update")
        # while avoiding normal camelCase product names (iPhone, PowerSchool, GitHub, YouTube, ProtonMail, eBay, macOS)
        camel_allow = {"iphone", "ipad", "macos", "powerschool", "github", "gitlab", "youtube", "protonmail", "groupme", "ebay", "linkedin", "onedrive", "sharepoint", "gradescope", "webassign", "deltamath", "chatgtp", "chatgpt"}

        def _split_dom_collision(m: re.Match) -> str:
            whole = m.group(0)
            if whole.lower() in camel_allow:
                return whole
            return f"{m.group(1)} {m.group(2)}"

        s = re.sub(r"\b([a-z]{3,})([A-Z][a-z]{2,})\b", _split_dom_collision, s)

        # Strip HTML tags if any leaked
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s).strip(" -–—•|:;,.")
        return s

    clean_t = _clean_segment(combined_title)
    clean_m = _clean_segment(combined_msg)

    # Strip boilerplate fallback sentences from preview
    clean_m = re.sub(
        r"Unread\s+(?:message/item|activity)\s+detected\s+on\s+[^.]+(?:\.)?",
        "",
        clean_m,
        flags=re.IGNORECASE,
    ).strip(" -–—•|")

    # If clean_t has "sender — Subject — Preview", split nicely into title & preview!
    parts = [p.strip() for p in re.split(r"\s+(?:—|•|\|)\s+", clean_t) if p.strip()]
    if len(parts) >= 2:
        # e.g. parts[0] = sender, parts[1] = subject, parts[2:] = snippet
        sender_cand, subj_cand = parts[0], parts[1]
        if "@" in sender_cand and len(subj_cand) >= 3:
            # Prefer clean Subject as headline and include sender in preview, or "Subject (from sender)"
            sender_short = sender_cand.split("@")[0].replace(".", " ").title()
            clean_t = f"{subj_cand} — {sender_short}"
        else:
            clean_t = f"{parts[0]} — {parts[1]}"
        if len(parts) >= 3 and not clean_m:
            clean_m = " • ".join(parts[2:])
    elif "@" in clean_t and " " in clean_t:
        # e.g. "stella.rua@school.edu Lab Partner Question" -> "Lab Partner Question — Stella Rua"
        m_em = re.match(r"^([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s+(.+)$", clean_t)
        if m_em:
            sender_short = m_em.group(1).split("@")[0].replace(".", " ").replace("_", " ").title()
            rest = m_em.group(2).strip(" -–—:")
            if rest:
                clean_t = f"{rest} — {sender_short}"

    if not clean_t:
        clean_t = clean_m[:110] if clean_m else "New message"

    if clean_m.lower() == clean_t.lower():
        clean_m = ""

    return {
        "title": clean_t[:220],
        "preview": clean_m[:260],
        "is_starred": is_starred,
    }


def _normalize_task_key(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    # drop filler prefixes
    s = re.sub(r"^(?:complete|finish|do|submit|turn in|work on|read|review)\s+", "", s)
    return s.strip()


class MultiModelRouter:
    """
    Coordinates Node's 3-Tier Local AI Stack:
      1. Reasoning / Agent Model (`settings.ollama_model`, default `qwen3.5:2b`):
         Streaming chat, collapsible `<think>` reasoning, full-OS tool execution, Executive Briefings.
      2. Fast Utility Model (`settings.ollama_fast_model`, default `qwen3.5:0.8b`):
         Fast non-thinking JSON extraction, notification title/preview cleanup, task estimation.
         Auto-falls back to `settings.ollama_model` (`think=False`) if `0.8b` is not pulled yet.
      3. Optional System One Decision Engine (`settings.von_base_url`, `/v1/systemone`):
         Single-pass calibrated `Choice` / `Noul` / `Score` classification via Von (`wfzyx/von`).
    """

    def __init__(self) -> None:
        self._tags_cache_ts: float = 0.0
        self._available_ollama_models: List[str] = []
        self._ollama_online: bool = False
        self._von_cache_ts: float = 0.0
        self._von_online: bool = False
        self._von_detail: str = "Not configured (optional)"

    @property
    def ollama_base(self) -> str:
        return settings.ollama_base_url.rstrip("/")

    @property
    def von_base(self) -> str:
        return (settings.von_base_url or "").strip().rstrip("/")

    async def refresh_status(self, force: bool = False) -> Dict[str, Any]:
        from .ai_providers import ai_provider_registry

        now = time.monotonic()
        discovered = await ai_provider_registry.discover_all_models(force=force)
        pub_state = ai_provider_registry.public_state()
        routing = pub_state.get("routing") or {}

        self._available_ollama_models = [d["model"] for d in discovered]
        self._ollama_online = len(discovered) > 0

        if self.von_base:
            if force or (now - self._von_cache_ts) > 30.0:
                self._von_cache_ts = now
                try:
                    headers = {"Content-Type": "application/json"}
                    if settings.von_api_key:
                        headers["Authorization"] = f"Bearer {settings.von_api_key}"
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        probe = await client.get(f"{self.von_base}/health")
                        if probe.status_code < 500:
                            self._von_online = True
                            self._von_detail = f"Connected at {self.von_base} (/v1/systemone ready)"
                        else:
                            self._von_online = False
                            self._von_detail = f"HTTP {probe.status_code} at {self.von_base}"
                except Exception as exc:
                    self._von_online = False
                    self._von_detail = f"Unreachable at {self.von_base} ({exc.__class__.__name__})"
        else:
            self._von_online = False
            self._von_detail = "Optional — set VON_BASE_URL (e.g. http://localhost:8000) to enable Von /v1/systemone"

        primary_target = await ai_provider_registry.resolve_target(role="primary")
        fast_target = await ai_provider_registry.resolve_target(role="fast")

        primary_name = (
            primary_target["model"]
            if primary_target
            else (routing.get("primary_model") or "auto")
        )
        fast_name = (
            fast_target["model"]
            if fast_target
            else (routing.get("fast_model") or "auto")
        )

        return {
            "ollama_reachable": self._ollama_online,
            "ollama_base_url": self.ollama_base,
            "available_models": [d["model"] for d in discovered],
            "discovered_models": discovered,
            "providers": pub_state.get("providers") or [],
            "routing": routing,
            "presets": pub_state.get("presets") or [],
            "primary_chat": {
                "model": primary_name,
                "composite_id": primary_target["composite_id"] if primary_target else None,
                "provider": primary_target["provider_name"] if primary_target else None,
                "ready": bool(primary_target is not None),
            },
            "fast_router": {
                "model": fast_name,
                "composite_id": fast_target["composite_id"] if fast_target else None,
                "provider": fast_target["provider_name"] if fast_target else None,
                "ready": bool(fast_target is not None),
                "active_model": fast_name,
            },
            "von_router": {
                "configured": bool(self.von_base),
                "reachable": bool(self._von_online),
                "model": settings.von_model or "von-1.3.7",
                "detail": self._von_detail,
            },
            "reasoning_model": {
                "configured": routing.get("primary_model") or "auto",
                "effective": primary_name,
                "installed": bool(primary_target is not None),
                "role": "Server-Side Agentic Chat, Shared Multi-Agent Logging, Web Search & Full-OS Tools",
            },
            "fast_model": {
                "configured": routing.get("fast_model") or "auto",
                "effective": fast_name,
                "installed": bool(fast_target is not None),
                "using_fallback": False,
                "role": "Sub-second Inbox Cleanup, Tagging & Task Duration Extraction (think=false)",
            },
            "von": {
                "configured": bool(self.von_base),
                "base_url": self.von_base or None,
                "reachable": self._von_online,
                "model": settings.von_model or "von-1.3.7",
                "confidence_threshold": settings.von_confidence_threshold,
                "detail": self._von_detail,
                "role": "Optional Non-Autoregressive System One Classifier (/v1/systemone Choice/Noul/Score)",
            },
        }

    async def pick_fast_ollama_model(self) -> Optional[str]:
        from .ai_providers import ai_provider_registry

        target = await ai_provider_registry.resolve_target(role="fast")
        return target["composite_id"] if target else None

    async def von_system_one(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Any],
        timeout: float = 3.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a single-forward-pass query against a Von (`/v1/systemone`) server.
        Returns the parsed `answers` dict or `None` if Von is disabled/unreachable.
        """
        if not self.von_base:
            return None
        headers = {"Content-Type": "application/json"}
        if settings.von_api_key:
            headers["Authorization"] = f"Bearer {settings.von_api_key}"
        payload = {
            "model": settings.von_model or "von-1.3.7",
            "state": state,
            "questions": questions,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{self.von_base}/v1/systemone", headers=headers, json=payload)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                self._von_online = True
                return data.get("answers") or data
        except Exception:
            return None

    async def fast_json(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 380,
        timeout: float = 18.0,
    ) -> Optional[Any]:
        """
        Run a fast non-thinking structured JSON call on whichever fast model is active
        (local Ollama `0.8b` / `gemma4:e2b`, remote Ollama, or Cloud API).
        """
        from .ai_providers import ai_provider_registry

        try:
            res = await ai_provider_registry.chat_complete(
                messages=[
                    {"role": "system", "content": system_prompt + "\nReturn ONLY valid JSON with no markdown prose."},
                    {"role": "user", "content": user_prompt},
                ],
                role="fast",
                enable_thinking=False,
                temperature=0.15,
                max_tokens=num_predict,
                timeout=timeout,
            )
            if not res.get("ok") or not res.get("content"):
                return None
            raw = res["content"].strip()
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
            if "{" in raw and not raw.startswith("{") and not raw.startswith("["):
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end > start:
                    raw = raw[start : end + 1]
            return json.loads(raw)
        except Exception:
            return None

    def get_active_inbox_categories(self, source_app: Optional[str] = None) -> List[str]:
        """
        Merge global Inbox categories from OS Settings with any per-app `inbox_categories`
        configured on `source_app` (editable by the user in Settings/Apps or by the AI Secretary).
        """
        default_cats = ["Urgent", "School", "Work", "Personal", "Finance", "Updates", "Promo"]
        global_cats: List[str] = []
        try:
            from .os_settings import os_settings_service

            st = os_settings_service.load_state()
            csv_raw = str((st.get("values") or {}).get("inbox_categories_csv") or "").strip()
            if csv_raw:
                global_cats = [c.strip()[:40] for c in csv_raw.split(",") if c.strip()]
        except Exception:
            pass
        if not global_cats:
            global_cats = list(default_cats)

        app_cats: List[str] = []
        if source_app:
            try:
                from .apps_manager import AppsConfigManager, slugify_app_name

                mgr = AppsConfigManager()
                _, all_apps = mgr.get_global_and_apps()
                target_slug = slugify_app_name(source_app)
                for a in all_apps:
                    if a["slug"] == target_slug or a["name"].lower() == source_app.strip().lower():
                        app_cats = list(a.get("inbox_categories") or [])
                        break
            except Exception:
                pass

        merged: List[str] = []
        seen = set()
        # Put app-specific custom categories FIRST so Von & Fast AI prioritize user-defined app buckets!
        for c in app_cats + global_cats:
            cl = c.strip()
            if cl and cl.lower() not in seen:
                seen.add(cl.lower())
                merged.append(cl)
        return merged

    def _build_von_category_criteria(self, categories: List[str]) -> Dict[str, str]:
        builtin_desc = {
            "urgent": "Time-sensitive deadline, emergency, immediate reply required today",
            "school": "Classes, homework, assignments, teachers, professors, grades, clubs, exams",
            "work": "Projects, colleagues, meetings, code, career, job deadlines",
            "personal": "Direct messages from friends, family, or real individuals",
            "finance": "Receipts, bank alerts, billing, invoices, orders, subscriptions",
            "promo": "Marketing newsletters, sales, coupons, automated digests, spam",
            "updates": "Automated security codes, login alerts, package tracking, system updates",
        }
        criteria: Dict[str, str] = {}
        for cat in categories[:18]:
            desc = builtin_desc.get(cat.lower())
            if not desc:
                desc = f"Messages, updates, or items specifically related to '{cat}'"
            criteria[cat] = desc
        return criteria

    async def categorize_item_von_first(
        self,
        *,
        source_app: str,
        title: str,
        snippet: str = "",
        custom_categories: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Universal Categorizer:
          1. Uses `Von` (`/v1/systemone`) FIRST when configured & reachable (single forward pass).
          2. Automatically falls back to the fastest / smallest available model (`role="fast"`, `think=False`).
          3. Falls back to deterministic keyword & custom-category matching if all models are offline.
        """
        cats = list(custom_categories) if custom_categories else self.get_active_inbox_categories(source_app)
        criteria = self._build_von_category_criteria(cats)

        # Tier 1: Von (/v1/systemone) Zero-Shot Classifier
        von_ans = await self.von_system_one(
            state={
                "service": source_app,
                "headline": title[:220],
                "snippet": (snippet or "")[:400],
            },
            questions={
                "category": {
                    "type": "choice",
                    "instructions": "Select the single best category for this item.",
                    "criteria": criteria,
                },
                "disposition": {
                    "type": "choice",
                    "instructions": "Does the user need to read or act on this?",
                    "criteria": {
                        "important": "Direct human message, assignment, grade change, custom category match, or urgent alert",
                        "fyi_noise": "Promotional marketing email, newsletter, social digest, or low-value automated noise",
                    },
                },
                "urgency": {
                    "type": "choice",
                    "instructions": "Select urgency level.",
                    "criteria": {
                        "critical": "Immediate deadline today or emergency",
                        "high": "Actionable message, assignment, or important update",
                        "normal": "Standard informational message",
                        "low": "Newsletter, promo, or background digest",
                    },
                },
            },
        )
        if isinstance(von_ans, dict):
            cat_obj = von_ans.get("category") or {}
            disp_obj = von_ans.get("disposition") or {}
            urg_obj = von_ans.get("urgency") or {}
            chosen_cat = str(cat_obj.get("choice") or "").strip() if isinstance(cat_obj, dict) else ""
            if chosen_cat:
                disp_choice = str(disp_obj.get("choice") or "important") if isinstance(disp_obj, dict) else "important"
                is_imp = disp_choice != "fyi_noise" and chosen_cat.lower() != "promo"
                urg_choice = str(urg_obj.get("choice") or ("high" if is_imp else "low")) if isinstance(urg_obj, dict) else "normal"
                return {
                    "category": chosen_cat,
                    "is_important": is_imp,
                    "priority": normalize_priority_value(urg_choice),
                    "engine": "von",
                }

        # Tier 2: Fastest / Smallest Available Model Fallback (`role="fast"`, `think=False`)
        cats_pipe = "|".join(cats)
        fast_model_id = await self.pick_fast_ollama_model()
        llm_obj = await self.fast_json(
            system_prompt=(
                f"Categorize this item into EXACTLY ONE of [{cats_pipe}]. "
                "Return strict JSON: "
                f"{{\"category\": \"{cats[0] if cats else 'General'}\", \"is_important\": true|false, \"priority\": \"critical|high|normal|low\"}}"
            ),
            user_prompt=f"App: {source_app}\nTitle: {title}\nDetails: {snippet[:350]}",
            num_predict=120,
            timeout=10.0,
        )
        if isinstance(llm_obj, dict) and llm_obj.get("category"):
            raw_c = str(llm_obj["category"]).strip()
            matched_cat = next((c for c in cats if c.lower() == raw_c.lower()), None) or raw_c[:40]
            imp_v = bool(llm_obj.get("is_important", matched_cat.lower() != "promo"))
            return {
                "category": matched_cat,
                "is_important": imp_v,
                "priority": normalize_priority_value(llm_obj.get("priority") or ("high" if imp_v else "low")),
                "engine": f"fast_model:{fast_model_id or 'auto'}",
            }

        # Tier 3: Deterministic Custom-Category + Keyword Heuristic Fallback
        text_l = f"{source_app} {title} {snippet}".lower()
        for custom_c in cats:
            cl = custom_c.lower()
            if cl not in {"urgent", "school", "work", "personal", "finance", "updates", "promo", "general"} and cl in text_l:
                return {"category": custom_c, "is_important": True, "priority": "high", "engine": "heuristic"}
        if any(k in text_l for k in ("unsubscribe", "% off", "sale", "deal", "promo", "newsletter", "digest")):
            cat_fb = "Promo"
        elif any(k in text_l for k in ("urgent", "asap", "immediately", "due tonight", "overdue")):
            cat_fb = "Urgent" if any(c.lower() == "urgent" for c in cats) else "School"
        elif any(k in text_l for k in ("schoology", "canvas", "classroom", "powerschool", "homework", "quiz", "exam", "essay", "lab", "grade", "assignment", "teacher", "prof")):
            cat_fb = "School"
        elif any(k in text_l for k in ("invoice", "receipt", "payment", "statement", "bank")):
            cat_fb = "Finance"
        elif any(k in text_l for k in ("teams", "slack", "meeting", "project")):
            cat_fb = "Work"
        else:
            cat_fb = "Personal"
        imp_fb = cat_fb.lower() != "promo"
        return {
            "category": cat_fb,
            "is_important": imp_fb,
            "priority": "high" if cat_fb in ("Urgent", "School") and ("due" in text_l or "urgent" in text_l) else ("normal" if imp_fb else "low"),
            "engine": "heuristic",
        }

    async def process_incoming_notification(
        self,
        source_app: str,
        raw_title: str,
        raw_message: str,
        indicator_type: str = "dom_badge",
        sender: str = "",
        body: str = "",
    ) -> Dict[str, Any]:
        """
        Full multi-tier pipeline for newly harvested Inbox notifications.

        Tier 1 — **Decision gate** (`decision_gate`, LFM3 `d1` / TypeSafe System One):
                 one forward pass assigns tags (multi-label), primary category,
                 Important vs FYI, priority, clean title, 1-line body summary,
                 sender, due date, and the content-addressed `dedupe_key`.
        Tier 2 — `Von` (`/v1/systemone`) if the gate itself is not configured.
        Tier 3 — Fastest/smallest chat model (`role="fast"`, `think=False`).
        Tier 4 — Deterministic keyword/registry fallback (never fails).
        """
        from .decision_gate import compute_dedupe_key, tag_notification
        from .inbox_tags import inbox_tag_service

        scrubbed = scrub_notification_artifacts(source_app, raw_title, raw_message)
        clean_title = scrubbed["title"]
        preview = scrubbed["preview"]
        full_body = (body or raw_message or "").strip()

        dkey = compute_dedupe_key(source_app, clean_title, sender=sender, body=full_body)

        if indicator_type == "ping_only":
            return {
                "title": clean_title,
                "ai_summary": "Count-only privacy mode enabled for this service.",
                "category": "Encrypted",
                "tags": "Encrypted",
                "is_important": True,
                "priority": "normal",
                "sender": sender,
                "dedupe_key": dkey,
                "confidence": 1.0,
                "engine": "privacy_guard",
            }

        # ---- Learned correction from the user's previous moves (cheap, exact).
        learned = inbox_tag_service.learned_tag_for(sender, clean_title, source_app)

        # ---- Tier 1: decision gate
        gate = await tag_notification(
            source_app=source_app,
            title=clean_title,
            body=full_body or preview,
            sender=sender,
            tags=inbox_tag_service.list_tags(),
            known_categories=self.get_active_inbox_categories(source_app),
        )
        if gate:
            tags = list(gate["tags"])
            engine = "lfm3-d1"
            # User corrections outrank the model for the same sender + subject.
            if learned and learned.get("tag") and learned["tag"] not in tags:
                tags = [learned["tag"], *tags]
                engine = "lfm3-d1+user-rule"
            return {
                "title": (gate["clean_title"] or clean_title)[:220],
                "ai_summary": (gate["summary"] or preview or gate["clean_title"] or clean_title)[:300],
                "category": (tags[0] if tags else "General"),
                "tags": ", ".join(dict.fromkeys(tags)),
                "is_important": bool(gate["is_important"]),
                "priority": gate["priority"],
                "sender": gate["sender"] or sender,
                "due_hint": gate.get("due_hint") or "",
                "dedupe_key": dkey,
                "confidence": float(gate.get("confidence") or 0.0),
                "engine": engine,
            }

        # ---- Tier 2/3: Von then fast model (pre-gate behaviour)
        active_cats = self.get_active_inbox_categories(source_app)
        criteria = self._build_von_category_criteria(active_cats)

        von_category: Optional[str] = None
        von_important: Optional[bool] = None
        von_priority: Optional[str] = None
        von_conf: float = 0.0

        von_ans = await self.von_system_one(
            state={
                "service": source_app,
                "headline": clean_title,
                "snippet": preview[:400],
            },
            questions={
                "category": {
                    "type": "choice",
                    "instructions": "Classify which category this notification belongs to.",
                    "criteria": criteria,
                },
                "disposition": {
                    "type": "choice",
                    "instructions": "Does a busy user need to read or act on this notification?",
                    "criteria": {
                        "important": "Direct human message, assignment, grade change, calendar change, or urgent alert requiring attention",
                        "fyi_noise": "Promotional email, newsletter, social digest, marketing, or low-value automated notification",
                    },
                },
            },
        )
        if isinstance(von_ans, dict):
            cat_obj = von_ans.get("category") or {}
            disp_obj = von_ans.get("disposition") or {}
            if isinstance(cat_obj, dict) and cat_obj.get("choice"):
                von_category = str(cat_obj["choice"])
            if isinstance(disp_obj, dict) and disp_obj.get("choice"):
                von_conf = float(disp_obj.get("confidence") or 0.0)
                if von_conf >= settings.von_confidence_threshold:
                    von_important = disp_obj["choice"] == "important"
                    von_priority = "high" if von_important else "low"

        cats_pipe = "|".join(active_cats)
        llm_obj = await self.fast_json(
            system_prompt=(
                "You clean and tag incoming notifications for a personal OS Inbox. "
                "NEVER prefix `title` with the app name (e.g. NEVER start with 'ProtonMail:'). "
                "Fix any missing spaces or leftover UI words like 'Star conversation' or 'Unread email'. "
                "For emails, set `title` to `<Clean Subject> — <Sender Name>` (max 95 chars) and `preview` to a crisp 1-sentence summary of the content (max 150 chars). "
                "Return strict JSON: "
                f"{{\"title\": \"...\", \"preview\": \"...\", \"category\": \"{cats_pipe}\", "
                "\"is_important\": true|false, \"priority\": \"critical|high|normal|low\"}"
            ),
            user_prompt=(
                f"Service: {source_app}\n"
                f"Sender: {sender or '(unknown)'}\n"
                f"Scraped Headline: {clean_title}\n"
                f"Scraped Snippet: {preview or '(none)'}"
            ),
            num_predict=220,
            timeout=12.0,
        )

        if isinstance(llm_obj, dict):
            cand_title = str(llm_obj.get("title") or "").strip()
            if cand_title:
                cand_title = scrub_notification_artifacts(source_app, cand_title, "")["title"]
                if len(cand_title) >= 3:
                    clean_title = cand_title[:220]
            cand_prev = str(llm_obj.get("preview") or "").strip()
            if cand_prev:
                preview = cand_prev[:240]

            raw_llm_cat = str(llm_obj.get("category") or "General").strip()
            matched_cat = next((c for c in active_cats if c.lower() == raw_llm_cat.lower()), None)
            cat_val = von_category or matched_cat or (raw_llm_cat[:40] if raw_llm_cat else "General")
            if learned and learned.get("tag"):
                cat_val = learned["tag"]
            imp_val = von_important if von_important is not None else bool(llm_obj.get("is_important", cat_val.lower() != "promo"))
            prio_val = normalize_priority_value(von_priority or llm_obj.get("priority") or ("high" if imp_val else "low"))
            tag_list = [cat_val]
            if imp_val and "Urgent" in active_cats and prio_val in ("critical", "high"):
                tag_list.insert(0, "Urgent")
            return {
                "title": clean_title,
                "ai_summary": preview or clean_title,
                "category": cat_val,
                "tags": ", ".join(dict.fromkeys(tag_list)),
                "is_important": imp_val,
                "priority": prio_val,
                "sender": sender,
                "dedupe_key": dkey,
                "confidence": 0.6,
                "engine": "von+ollama_fast" if von_category else "ollama_fast",
            }

        # ---- Tier 4: deterministic fallback
        fb = await self.categorize_item_von_first(
            source_app=source_app,
            title=clean_title,
            snippet=preview,
            custom_categories=active_cats,
        )
        cat_final = von_category or (learned or {}).get("tag") or fb["category"]
        return {
            "title": clean_title,
            "ai_summary": preview or clean_title,
            "category": cat_final,
            "tags": cat_final,
            "is_important": von_important if von_important is not None else fb["is_important"],
            "priority": von_priority or fb["priority"],
            "sender": sender,
            "dedupe_key": dkey,
            "confidence": 0.35,
            "engine": "von" if von_category else fb["engine"],
        }

    async def extract_tasks_with_ai(
        self,
        raw_text: str,
        source_name: str = "Notes",
        existing_titles: Sequence[str] = (),
        today_iso: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extract ONLY genuine actionable tasks from notes, uploaded files, Schoology calendar
        events, or Inbox items — with AI-estimated duration, priority, clean title, and
        strict deduplication against `existing_titles`.
        """
        today_str = today_iso or datetime.now(timezone.utc).date().isoformat()
        seen_keys = {_normalize_task_key(t) for t in existing_titles if t}

        llm_res = await self.fast_json(
            system_prompt=(
                f"Today's date is {today_str}. Extract ONLY genuine, incomplete actionable tasks or assignments "
                "from the provided text. Ignore section headers, background notes, completed items ([x]), "
                "routine class attendance blocks, or non-actionable commentary. "
                "Give each task a crisp, clean title, a realistic `estimated_minutes` integer (5 to 180) "
                "based on task complexity (e.g. quick email/message reply = 5-10m, worksheet/quiz = 25-35m, "
                "problem set/essay/lab/project = 45-90m), `priority` ('critical', 'high', 'normal', or 'low'), "
                "`category` ('School', 'Work', 'Personal', or 'General'), and `due_date` ('YYYY-MM-DD' or null).\n"
                "Return strict JSON: {\"tasks\": [{\"title\": \"...\", \"estimated_minutes\": 30, "
                "\"priority\": \"high\", \"category\": \"School\", \"due_date\": \"YYYY-MM-DD\"}]}"
            ),
            user_prompt=f"Source: {source_name}\n\nContent:\n{raw_text[:8000]}",
            num_predict=700,
            timeout=25.0,
        )

        extracted: List[Dict[str, Any]] = []
        if isinstance(llm_res, dict) and isinstance(llm_res.get("tasks"), list):
            for item in llm_res["tasks"]:
                if not isinstance(item, dict):
                    continue
                t_title = str(item.get("title") or "").strip(" -•*[]")
                if len(t_title) < 3:
                    continue
                key = _normalize_task_key(t_title)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)

                est_raw = item.get("estimated_minutes")
                try:
                    est_mins = max(5, min(240, int(est_raw)))
                except Exception:
                    est_mins, _ = memory_store.estimate_task_minutes(t_title, str(item.get("category") or "General"), source_name)

                due_val = item.get("due_date")
                due_clean = str(due_val).strip()[:10] if due_val and re.match(r"^\d{4}-\d{2}-\d{2}", str(due_val).strip()) else None

                extracted.append(
                    {
                        "title": t_title[:240],
                        "estimated_minutes": est_mins,
                        "priority": normalize_priority_value(item.get("priority") or "normal"),
                        "category": str(item.get("category") or "General").strip()[:60] or "General",
                        "due_date": due_clean,
                        "source_app": source_name,
                    }
                )
            return extracted

        # Smart offline fallback: ONLY extract explicit checkboxes `- [ ]`, numbered action items,
        # or lines containing clear task/assignment verbs or due dates — NEVER every random paragraph newline!
        ACTION_CUE_RE = re.compile(
            r"^(?:\s*[-*•]\s*\[\s*\]\s+|\s*(?:todo|task|action|assignment|hw|homework)\s*[:\-]\s*)"
            r"|(?:\b(?:submit|complete|finish|write|solve|study\s+for|prepare|reply\s+to|email|turn\s+in|read\s+ch|problem\s+set|worksheet|essay|lab\s+report|quiz|exam|due\b)\b)",
            re.IGNORECASE,
        )
        for raw_ln in raw_text.splitlines():
            ln = raw_ln.strip()
            if not ln or ln.startswith("#") or len(ln) < 4 or len(ln) > 220:
                continue
            if re.match(r"^[-*•]?\s*\[[xX✓✔]\]", ln):
                continue
            is_bullet = bool(re.match(r"^(?:[-*•]|\d+[.)])\s+", ln))
            if not (ACTION_CUE_RE.search(ln) or (is_bullet and len(ln.split()) <= 16)):
                continue

            cleaned = re.sub(r"^(?:[-*•]|\d+[.)])\s*(?:\[\s*\]\s*)?", "", ln)
            cleaned = re.sub(r"^(?:todo|task|action)\s*[:\-]\s*", "", cleaned, flags=re.IGNORECASE).strip()
            if len(cleaned) < 3:
                continue

            # Extract inline duration if written like "(45m)" or "30 min"
            dur_m = re.search(r"\b(\d{1,3})\s*(?:m|min|mins|minutes)\b", cleaned, re.IGNORECASE)
            due_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", cleaned)
            due_iso: Optional[str] = due_m.group(1) if due_m else None
            if not due_iso:
                if re.search(r"\b(?:due\s+)?today\b|\btonight\b", cleaned, re.IGNORECASE):
                    due_iso = today_str
                elif re.search(r"\b(?:due\s+)?tomorrow\b", cleaned, re.IGNORECASE):
                    due_iso = (date.fromisoformat(today_str) + timedelta(days=1)).isoformat()

            cleaned_title = re.sub(r"\s*\(?\b(?:due:?\s*)?\d{4}-\d{2}-\d{2}\)?", "", cleaned, flags=re.IGNORECASE).strip(" -–—•:")
            key = _normalize_task_key(cleaned_title)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)

            if dur_m:
                est_mins = max(5, min(240, int(dur_m.group(1))))
            else:
                est_mins = _smart_heuristic_duration(cleaned_title)

            prio = "normal"
            low_t = cleaned_title.lower()
            if any(w in low_t for w in ("urgent", "asap", "exam", "final", "midterm", "overdue", "tonight")):
                prio = "critical" if "tonight" in low_t or "overdue" in low_t else "high"
            elif due_iso == today_str:
                prio = "high"

            cat = "School" if any(w in low_t for w in ("hw", "homework", "essay", "lab", "quiz", "ap ", "calc", "chem", "bio", "physics", "history", "english", "read", "chapter", "problem", "schoology")) else "General"
            extracted.append(
                {
                    "title": cleaned_title[:240],
                    "estimated_minutes": est_mins,
                    "priority": prio,
                    "category": cat,
                    "due_date": due_iso,
                    "source_app": source_name,
                }
            )
            if len(extracted) >= 35:
                break

        return extracted


def _smart_heuristic_duration(title: str, category: str = "General", source_app: str = "Node") -> int:
    """
    Respect user-learned `time_estimates.md` archetypes/keywords first, then refine generic
    unmatched defaults with realistic complexity-based durations (never flat 25m for everything!).
    """
    est, reason = memory_store.estimate_task_minutes(title, category, source_app)
    if reason and "default" not in reason.lower():
        return est
    low = (title or "").lower()
    if any(k in low for k in ("reply", "respond", "dm", "text", "ping", "check grade", "sign ", "rsvp", "form")):
        return 5
    if any(k in low for k in ("email", "message", "schedule", "submit", "upload", "turn in")):
        return 10
    if any(k in low for k in ("quiz", "mcq", "flashcard", "review notes", "vocab", "daily")):
        return 20
    if any(k in low for k in ("worksheet", "reading", "chapter", "homework", "hw")):
        return 35
    if any(k in low for k in ("problem set", "pset", "frq", "lab", "coding", "draft", "outline", "practice test")):
        return 50
    if any(k in low for k in ("essay", "paper", "project", "presentation", "exam prep", "study guide")):
        return 75
    return est


model_router = MultiModelRouter()
