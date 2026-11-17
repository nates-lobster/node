from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from .config import settings
from .models import Notification
from .ollama_client import ollama_client

logger = logging.getLogger("node.importance")

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "have", "you",
    "are", "was", "were", "will", "just", "new", "ping", "mail", "message",
    "notification", "unread", "http", "https", "com", "org", "edu", "net",
    "today", "here", "check", "out", "our", "more", "view", "click",
}

PROMO_PATTERNS = re.compile(
    r"(\b\d{1,2}%\s*off\b|\bflash\s+sale\b|\bsale\s+ends\b|\blimited[\s\-]+time\b|"
    r"\bpromo\s*code\b|\bcoupon\b|\bfree\s+shipping\b|\bshop\s+now\b|\bbuy\s+now\b|"
    r"\bunsubscribe\b|\bnewsletter\b|\bweekly\s+digest\b|\bdaily\s+digest\b|"
    r"\bsponsored\b|\bmarketing\b|\brewards\s+member\b|\bspecial\s+offer\b|"
    r"\bexclusive\s+deal\b|\bdeals?\s+of\s+the\b|\brecommended\s+for\s+you\b|"
    r"\bno-?reply@.*(?:marketing|promo|news|mail\.|deals)\b)",
    re.IGNORECASE,
)

HIGH_SIGNAL_PATTERNS = re.compile(
    r"(\bpowerschool\b|\bgrade\s+chang|\bnew\s+grade\b|\bassignment\b|\bdue\s+(?:today|tonight|tomorrow|mon|tue|wed|thu|fri|sat|sun|\d)|"
    r"\bhomework\b|\bprogress\s+check\b|\bquiz\b|\bexam\b|\bmidterm\b|\bfinal\b|"
    r"\bmissing\b|\boverdue\b|\bprof(?:essor)?\.?\b|\bteacher\b|\binstructor\b|"
    r"\burgent\b|\baction\s+required\b|\bdeadline\b|\bmeeting\b|\bschedule\b|"
    r"\bverification\s+code\b|\bsecurity\s+alert\b|\b2fa\b|\bpasscode\b)",
    re.IGNORECASE,
)

BOILERPLATE_STRIP_RE = re.compile(
    r"(view\s+this\s+email\s+in\s+your\s+browser.*$|unsubscribe\s+from\s+these\s+emails.*$|"
    r"sent\s+from\s+(?:my\s+iphone|outlook|proton\s*mail).*$)",
    re.IGNORECASE,
)


def strip_html_tags(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<br\s*/?>", " ", raw, flags=re.IGNORECASE)
    txt = re.sub(r"</(p|div|li|tr|h[1-6])>", " • ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return " ".join(txt.split()).strip()


def extract_sender_and_subject(source_app: str, title: str) -> Tuple[str, str]:
    """
    Parse normalized `(sender_key, clean_subject)` from titles like:
      'ProtonMail: Old Navy — 50% Off Everything Today!'
      'Microsoft Teams: Sarah Lin — Can you send the lab slides?'
    """
    t = (title or "").strip()
    app_prefix = (source_app or "").strip()
    if app_prefix and t.lower().startswith(app_prefix.lower() + ":"):
        t = t[len(app_prefix) + 1 :].strip()

    for sep in (" — ", " - ", ": "):
        if sep in t:
            left, right = t.split(sep, 1)
            sender_clean = re.sub(r"[^a-z0-9@. ]+", "", left.lower()).strip()
            if 2 <= len(sender_clean) <= 60:
                return sender_clean, right.strip()

    return "", t


def extract_tokens(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9%]{3,24}", (text or "").lower())
    out: List[str] = []
    for w in words:
        if w in STOPWORDS or w.isdigit():
            continue
        if w not in out:
            out.append(w)
        if len(out) >= 14:
            break
    return out


class ImportanceLearnerService:
    """
    Classifies incoming notifications as Important vs Low-Priority/Promo, builds crisp
    1-line content previews (`ai_summary`), and continuously learns sender & keyword
    preferences whenever the user marks items Important (`★`) or Not Important (`⊘`).
    """

    @property
    def storage_path(self) -> Path:
        p = Path(settings.importance_learner_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load_model(self) -> Dict[str, Any]:
        default_state: Dict[str, Any] = {
            "sender_weights": {},
            "token_weights": {},
            "app_weights": {},
            "feedback_count": 0,
            "examples": [],
        }
        if not self.storage_path.exists():
            return default_state
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default_state.update(data)
        except Exception:
            pass
        return default_state

    def _save_model(self, state: Dict[str, Any]) -> None:
        tmp = self.storage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.storage_path)

    def get_stats(self) -> Dict[str, Any]:
        return self.get_learner_stats()

    def get_learner_stats(self) -> Dict[str, Any]:
        st = self._load_model()
        top_important_senders = sorted(
            [(k, v) for k, v in st.get("sender_weights", {}).items() if v > 0.2],
            key=lambda x: -x[1],
        )[:8]
        muted_senders = sorted(
            [(k, v) for k, v in st.get("sender_weights", {}).items() if v < -0.2],
            key=lambda x: x[1],
        )[:8]
        return {
            "feedback_count": int(st.get("feedback_count") or 0),
            "trusted_senders": [k for k, _ in top_important_senders],
            "filtered_senders": [k for k, _ in muted_senders],
        }

    def build_one_line_summary(
        self,
        source_app: str,
        title: str,
        message: str,
        indicator_type: str = "dom_badge",
    ) -> str:
        """
        Produce a clean, single-line human preview (avoiding bare '<App> ping' whenever
        any sender, subject, snippet, due date, or grade detail is available).
        """
        clean_msg = strip_html_tags(message or "")
        clean_msg = BOILERPLATE_STRIP_RE.sub("", clean_msg).strip(" •—-:")
        sender, subj = extract_sender_and_subject(source_app, title or "")

        is_generic_ping = bool(re.match(rf"^{re.escape(source_app)}\s+ping\b", (title or "").strip(), re.IGNORECASE))

        if indicator_type == "active_assignment":
            base = f"{subj} ({clean_msg})" if clean_msg and clean_msg.lower() not in subj.lower() else subj
            return base[:160]

        if is_generic_ping and clean_msg and "new activity detected" not in clean_msg.lower():
            return clean_msg[:160]

        if sender and subj and clean_msg and clean_msg.lower() not in subj.lower():
            return f"{sender.title()}: {subj} — {clean_msg}"[:165]
        if subj and clean_msg and clean_msg.lower() not in subj.lower():
            return f"{subj} — {clean_msg}"[:165]
        if sender and subj:
            return f"{sender.title()}: {subj}"[:165]
        if clean_msg and "new activity detected" not in clean_msg.lower():
            return clean_msg[:165]
        return (subj or title or f"{source_app} update")[:165]

    async def summarize_with_ai(
        self,
        source_app: str,
        title: str,
        raw_content: str,
        indicator_type: str = "dom_badge",
    ) -> str:
        """
        Generate a crisp 1-sentence summary using local `qwen3.5:2b` when online,
        falling back cleanly to deterministic single-line extraction.
        """
        fallback = self.build_one_line_summary(source_app, title, raw_content, indicator_type)
        cleaned_raw = strip_html_tags(raw_content or "")
        if len(cleaned_raw) < 28:
            return fallback

        try:
            st = await ollama_client.check_status()
            if st.reachable and st.model_installed:
                prompt = (
                    f"App: {source_app}\n"
                    f"Header: {title}\n"
                    f"Content: {cleaned_raw[:1200]}\n\n"
                    "Write ONE concise, concrete plain-text sentence (max 22 words) summarizing what this message "
                    "or notification actually says and any action/time mentioned. No quotes or prefixes."
                )
                res = await ollama_client.chat_json_or_text(
                    system_prompt="You write crisp 1-line inbox message previews. Output one short sentence only.",
                    user_prompt=prompt,
                    enable_thinking=False,
                )
                line = " ".join((res.get("content") or "").split()).strip().strip('"')
                if 10 <= len(line) <= 220:
                    return line
        except Exception:
            pass
        return fallback

    def classify(
        self,
        source_app: str,
        title: str,
        message: str = "",
        indicator_type: str = "dom_badge",
    ) -> Tuple[bool, float, str]:
        """
        Return `(is_important, score_0_to_1, reason)` combining built-in ad/promo rules
        with learned sender & keyword weights from the user's past feedback.
        """
        model = self._load_model()
        sender_weights: Dict[str, float] = model.get("sender_weights", {})
        token_weights: Dict[str, float] = model.get("token_weights", {})
        app_weights: Dict[str, float] = model.get("app_weights", {})

        combined_text = f"{title or ''} {strip_html_tags(message or '')}".strip()
        sender_key, subj = extract_sender_and_subject(source_app, title or "")
        app_key = (source_app or "").strip().lower()

        score = 0.62
        reasons: List[str] = []

        # 1. Assignment / Grade / Direct Chat structural defaults
        if indicator_type in ("active_assignment", "grade_change", "grades_tracker"):
            score += 0.32
            reasons.append("Academic / assignment update")
        elif any(chat_kw in app_key for chat_kw in ("teams", "groupme", "slack", "discord", "signal")):
            score += 0.14
            reasons.append("Direct messaging app")

        # 2. Built-in Promo / Ad / Newsletter suppression vs High-Signal boost
        if PROMO_PATTERNS.search(combined_text):
            score -= 0.52
            reasons.append("Promotional / newsletter pattern")
        if HIGH_SIGNAL_PATTERNS.search(combined_text):
            score += 0.30
            reasons.append("Actionable / academic keyword")

        # 3. User-learned sender, app, and keyword weights (strongest signal!)
        if sender_key and sender_key in sender_weights:
            sw = float(sender_weights[sender_key])
            score += sw
            if sw >= 0.3:
                reasons.insert(0, f"Learned important sender ({sender_key})")
            elif sw <= -0.3:
                reasons.insert(0, f"Learned muted sender ({sender_key})")

        if app_key in app_weights:
            score += max(-0.25, min(0.25, float(app_weights[app_key])))

        tokens = extract_tokens(f"{subj} {message}")
        tok_delta = sum(float(token_weights.get(t, 0.0)) for t in tokens)
        if abs(tok_delta) >= 0.05:
            score += max(-0.45, min(0.45, tok_delta))
            if abs(tok_delta) >= 0.25 and not reasons:
                reasons.append("Learned topic preference")

        clamped = round(max(0.02, min(0.99, score)), 3)
        is_imp = clamped >= 0.50
        final_reason = reasons[0] if reasons else ("Important" if is_imp else "Filtered low-priority")
        return is_imp, clamped, final_reason[:180]

    def record_feedback(
        self,
        session: Session,
        notif: Notification,
        important: bool,
    ) -> Dict[str, Any]:
        """
        Train the adaptive filter from a user's explicit Important / Not Important mark,
        and immediately re-score other un-overridden notifications from the same sender.
        """
        st = self._load_model()
        sender_weights: Dict[str, float] = st.setdefault("sender_weights", {})
        token_weights: Dict[str, float] = st.setdefault("token_weights", {})
        app_weights: Dict[str, float] = st.setdefault("app_weights", {})

        sender_key, subj = extract_sender_and_subject(notif.source_app, notif.title)
        app_key = (notif.source_app or "").strip().lower()
        direction = 1.0 if important else -1.0

        if sender_key:
            cur_s = float(sender_weights.get(sender_key, 0.0))
            sender_weights[sender_key] = round(max(-2.0, min(2.0, cur_s + (0.95 * direction))), 3)

        if app_key:
            cur_a = float(app_weights.get(app_key, 0.0))
            app_weights[app_key] = round(max(-0.5, min(0.5, cur_a + (0.08 * direction))), 3)

        for tok in extract_tokens(f"{subj} {notif.message}"):
            cur_t = float(token_weights.get(tok, 0.0))
            token_weights[tok] = round(max(-1.2, min(1.2, cur_t + (0.28 * direction))), 3)

        st["feedback_count"] = int(st.get("feedback_count") or 0) + 1
        examples: List[Dict[str, Any]] = list(st.get("examples") or [])
        examples.append(
            {
                "app": notif.source_app,
                "sender": sender_key,
                "title": notif.title[:120],
                "important": important,
            }
        )
        st["examples"] = examples[-30:]
        self._save_model(st)

        # Update the target notification
        notif.is_important = important
        notif.user_importance_override = important
        notif.importance_score = 0.98 if important else 0.05
        notif.importance_reason = "Marked important by you" if important else "Marked not important by you"
        session.add(notif)

        # Also re-evaluate sibling notifications from the same sender that don't have a manual override
        updated_siblings = 0
        if sender_key:
            siblings = session.exec(
                select(Notification).where(
                    Notification.source_app == notif.source_app,
                    Notification.status == "active",
                )
            ).all()
            for sib in siblings:
                if sib.id == notif.id or sib.user_importance_override is not None:
                    continue
                sib_sender, _ = extract_sender_and_subject(sib.source_app, sib.title)
                if sib_sender == sender_key:
                    imp, sc, rsn = self.classify(sib.source_app, sib.title, sib.message, sib.indicator_type)
                    sib.is_important = imp
                    sib.importance_score = sc
                    sib.importance_reason = rsn
                    session.add(sib)
                    updated_siblings += 1

        session.commit()
        return {
            "important": important,
            "sender_learned": sender_key or None,
            "sender_key": sender_key or None,
            "updated_similar": updated_siblings,
            "feedback_count": st["feedback_count"],
        }


importance_service = ImportanceLearnerService()
