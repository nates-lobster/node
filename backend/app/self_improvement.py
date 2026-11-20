"""
Disciplined AI Self-Improvement, Feedback Rating (`👍`/`👎`), Frustration Detection,
and Unified AI Instructions Manager (`self_improvement.py`).

Design Principles (per User Spec):
1. Unifies Persona (`persona` -> `ai_memory.md`), Shared Operational Logs (`shared_agent_log` -> `ai_shared_log.md`),
   Self-Improvement Reflections (`self_improvement.md`), and custom `.md` instruction/context files into ONE
   clean file tree so the user and AI models don't juggle confusing separate concepts.
2. Captures explicit user chat ratings (`good` / `poor` + optional note) and implicit frustration signals.
3. Self-improves during off-peak Cron passes (and on explicit negative feedback) WITHOUT excessive file spam
   and WITH explicit anti-overfitting guardrails: extracts concise, generalizable behavioral principles
   (capped to <= 15 distilled rules) rather than rigid templates or parroted canned phrases.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from .agent_logbook import agent_logbook
from .config import settings
from .memory_store import memory_store
from .models import AIFeedbackEntry, ChatMessage


DEFAULT_SELF_IMPROVEMENT_MD = """# AI Self-Improvement & Distilled Principles

> Maintained automatically from chat feedback (`👍` / `👎`), detected corrections, and off-peak Cron reflections.
> **Anti-Overfitting Rule:** Every entry below must stay generalizable, adaptable, and concise — never rigid phrasing templates or overfit patterns.

## What Works Well (Reinforce)
- Verify exact calendar dates and weekdays before creating or moving events.
- Confirm executed OS actions concisely without unnecessary filler paragraphs.

## Mistakes to Avoid (Correct)
- Never invent data or assume a tool succeeded without checking its return payload.
- Avoid repeating the same greeting, structure, or phrasing across unrelated turns.

## Recent Reflection Log
"""

FRUSTRATION_PATTERNS = [
    re.compile(r"\b(?:that'?s\s+wrong|you\s+got\s+(?:it|that)\s+wrong|wrong\s+day|wrong\s+date)\b", re.IGNORECASE),
    re.compile(r"\b(?:stop\s+doing\s+that|don'?t\s+do\s+that|i\s+told\s+you\s+not\s+to|why\s+did\s+you)\b", re.IGNORECASE),
    re.compile(r"\b(?:you\s+messed\s+up|bad\s+response|useless|not\s+what\s+i\s+asked|listen\s+to\s+me)\b", re.IGNORECASE),
    re.compile(r"\b(?:too\s+wordy|too\s+long|stop\s+repeating|still\s+broken)\b", re.IGNORECASE),
]


def detect_frustration_signal(user_text: str) -> Optional[str]:
    txt = (user_text or "").strip()
    if len(txt) < 6 or len(txt) > 1200:
        return None
    for pat in FRUSTRATION_PATTERNS:
        m = pat.search(txt)
        if m:
            return f"Detected user correction/frustration phrase: '{m.group(0)}'"
    return None


class UnifiedInstructionsAndSelfImprovementService:
    def __init__(self) -> None:
        self._last_cron_reflection_at: Optional[datetime] = None

    def self_improvement_path(self) -> Path:
        nb_dir = Path(settings.ai_notebook_dir)
        nb_dir.mkdir(parents=True, exist_ok=True)
        p = nb_dir / "self_improvement.md"
        if not p.is_file():
            p.write_text(DEFAULT_SELF_IMPROVEMENT_MD, encoding="utf-8")
        return p

    def list_unified_files(self, session: Optional[Session] = None) -> List[Dict[str, Any]]:
        """
        Return all AI instruction, persona, reflection, and shared-log files in ONE unified list.
        """
        self.self_improvement_path()
        mem = memory_store.get_memory()
        if session is not None:
            agent_logbook.sync_markdown_mirror(session)
        shared_md_path = Path(settings.ai_shared_log_path)
        if not shared_md_path.is_file():
            shared_md_path.parent.mkdir(parents=True, exist_ok=True)
            shared_md_path.write_text(
                "# Node Shared Multi-Agent Workspace Logbook\n_Automatically maintained by collaborating AI models & background services._\n",
                encoding="utf-8",
            )

        out: List[Dict[str, Any]] = [
            {
                "id": "persona",
                "slug": "persona",
                "filename": "persona.md",
                "title": "Persona & Standing Instructions",
                "role": "persona",
                "role_badge": "Core Persona",
                "description": "Primary persona, tone, and standing operating instructions.",
                "deletable": False,
                "system": True,
                "size_bytes": len((mem.get("content") or "").encode("utf-8")),
                "updated_at": mem.get("updated_at"),
            },
            {
                "id": "self_improvement",
                "slug": "self_improvement",
                "filename": "self_improvement.md",
                "title": "Self-Improvement & Lessons",
                "role": "self_improvement",
                "role_badge": "Self-Reflection",
                "description": "Generalizable principles distilled from chat ratings & Cron reflections (anti-overfitting).",
                "deletable": False,
                "system": True,
                "size_bytes": self.self_improvement_path().stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    self.self_improvement_path().stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            },
            {
                "id": "shared_agent_log",
                "slug": "shared_agent_log",
                "filename": "shared_agent_log.md",
                "title": "Shared Multi-Agent Logbook",
                "role": "shared_log",
                "role_badge": "Agent Logbook",
                "description": "Shared memory & handoff log read/updated across all AI models.",
                "deletable": False,
                "system": True,
                "size_bytes": shared_md_path.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    shared_md_path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            },
        ]

        for nb in memory_store.list_notebook_files():
            slug = nb.get("slug") or ""
            if slug in ("self_improvement", "time_estimates"):
                # Keep time_estimates tucked at the very end as an optional system calibration file
                continue
            deletable = slug not in memory_store.CORE_NOTEBOOK_FILES
            out.append(
                {
                    "id": slug,
                    "slug": slug,
                    "filename": nb.get("filename") or f"{slug}.md",
                    "title": nb.get("title") or slug.replace("_", " ").title(),
                    "role": "context",
                    "role_badge": "Instructions / Context",
                    "deletable": deletable,
                    "system": not deletable,
                    "size_bytes": nb.get("size_bytes", 0),
                    "updated_at": nb.get("updated_at"),
                }
            )

        # Optionally include time_estimates at the very bottom labeled clearly as Pacing Rules
        for nb in memory_store.list_notebook_files():
            if nb.get("slug") == "time_estimates":
                out.append(
                    {
                        "id": "time_estimates",
                        "slug": "time_estimates",
                        "filename": "time_estimates.md",
                        "title": "Task Pacing Rules",
                        "role": "calibration",
                        "role_badge": "Calibration",
                        "deletable": False,
                        "system": True,
                        "size_bytes": nb.get("size_bytes", 0),
                        "updated_at": nb.get("updated_at"),
                    }
                )
        return out

    def read_unified_file(self, slug: str, session: Optional[Session] = None) -> Dict[str, Any]:
        key = (slug or "persona").strip().lower()
        if key.endswith(".md"):
            key = key[:-3]
        if key == "persona":
            doc = memory_store.get_memory()
            return {
                "slug": "persona",
                "filename": "persona.md",
                "title": "Persona & Standing Instructions",
                "role": "persona",
                "role_badge": "Core Persona",
                "description": "Primary persona, tone, and standing operating instructions.",
                "content": doc.get("content") or "",
                "updated_at": doc.get("updated_at"),
                "deletable": False,
                "system": True,
            }
        if key == "shared_agent_log":
            if session is not None:
                agent_logbook.sync_markdown_mirror(session)
            p = Path(settings.ai_shared_log_path)
            txt = p.read_text(encoding="utf-8") if p.is_file() else "# Shared Multi-Agent Logbook\n"
            return {
                "slug": "shared_agent_log",
                "filename": "shared_agent_log.md",
                "title": "Shared Multi-Agent Logbook",
                "role": "shared_log",
                "role_badge": "Agent Logbook",
                "description": "Shared memory & handoff log read/updated across all AI models.",
                "content": txt,
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat() if p.is_file() else None,
                "deletable": False,
                "system": True,
            }
        if key == "self_improvement":
            p = self.self_improvement_path()
            return {
                "slug": "self_improvement",
                "filename": "self_improvement.md",
                "title": "Self-Improvement & Lessons",
                "role": "self_improvement",
                "role_badge": "Self-Reflection",
                "description": "Generalizable principles distilled from chat ratings & Cron reflections (anti-overfitting).",
                "content": p.read_text(encoding="utf-8"),
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
                "deletable": False,
                "system": True,
            }
        doc = memory_store.get_notebook_file(key)
        deletable = key not in memory_store.CORE_NOTEBOOK_FILES
        return {
            "slug": doc.get("slug", key),
            "filename": doc.get("filename", f"{key}.md"),
            "title": doc.get("title", key.replace("_", " ").title()),
            "role": "calibration" if key == "time_estimates" else "context",
            "role_badge": "Calibration" if key == "time_estimates" else "Instructions / Context",
            "content": doc.get("content") or "",
            "updated_at": doc.get("updated_at"),
            "deletable": deletable,
            "system": not deletable,
        }

    def write_unified_file(self, slug: str, content: str) -> Dict[str, Any]:
        key = (slug or "persona").strip().lower()
        if key.endswith(".md"):
            key = key[:-3]
        if key == "persona":
            memory_store.save_memory(content or "")
            return self.read_unified_file("persona")
        if key == "shared_agent_log":
            p = Path(settings.ai_shared_log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content or "", encoding="utf-8")
            return self.read_unified_file("shared_agent_log")
        if key == "self_improvement":
            p = self.self_improvement_path()
            p.write_text(content or "", encoding="utf-8")
            return self.read_unified_file("self_improvement")
        memory_store.save_notebook_file(key, content or "")
        return self.read_unified_file(key)

    def record_feedback(
        self,
        session: Session,
        *,
        rating: str,
        message_id: Optional[int] = None,
        thread_id: str = "main",
        reason: str = "",
        user_prompt: str = "",
        assistant_reply: str = "",
        model_id: str = "auto",
    ) -> AIFeedbackEntry:
        norm_rating = rating.strip().lower()
        if norm_rating not in ("good", "poor", "frustration"):
            norm_rating = "poor"

        # Hydrate prompt/reply from ChatMessage if message_id was given
        if message_id and (not assistant_reply or not user_prompt):
            msg = session.get(ChatMessage, message_id)
            if msg:
                thread_id = msg.thread_id or thread_id
                if msg.role == "assistant" and not assistant_reply:
                    assistant_reply = msg.content or ""
                prev_user = session.exec(
                    select(ChatMessage)
                    .where(ChatMessage.thread_id == thread_id)
                    .where(ChatMessage.id < msg.id)
                    .where(ChatMessage.role == "user")
                    .order_by(ChatMessage.id.desc())
                ).first()
                if prev_user and not user_prompt:
                    user_prompt = prev_user.content or ""

        row = AIFeedbackEntry(
            thread_id=thread_id[:80],
            message_id=message_id,
            rating=norm_rating,
            reason=(reason or "").strip()[:600],
            user_prompt=(user_prompt or "").strip()[:1500],
            assistant_reply=(assistant_reply or "").strip()[:1500],
            model_id=(model_id or "auto")[:160],
            processed=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    async def reflect_and_self_improve(
        self,
        session: Session,
        *,
        force: bool = False,
        trigger: str = "cron",
    ) -> Dict[str, Any]:
        """
        Consolidate unprocessed feedback (`good`, `poor`, `frustration`) into concise,
        anti-overfit principles inside `self_improvement.md` (and a summary log in `agent_logs`).
        Rate-limited on Cron runs so AI never creates excessive files or repetitive rules.
        """
        now = datetime.now(timezone.utc)
        pending = session.exec(
            select(AIFeedbackEntry)
            .where(AIFeedbackEntry.processed == False)  # noqa: E712
            .order_by(AIFeedbackEntry.created_at.asc())
            .limit(20)
        ).all()

        if not pending and not force:
            return {"status": "skipped", "reason": "no_unprocessed_feedback", "processed_count": 0}

        if (
            not force
            and trigger == "cron"
            and self._last_cron_reflection_at is not None
            and (now - self._last_cron_reflection_at) < timedelta(minutes=45)
            and all(p.rating == "good" for p in pending)
        ):
            return {"status": "deferred", "reason": "cooldown_active", "pending_count": len(pending)}

        current_md = self.self_improvement_path().read_text(encoding="utf-8")
        events_summary = []
        for item in pending:
            events_summary.append(
                {
                    "rating": item.rating,
                    "reason": item.reason,
                    "user_prompt": item.user_prompt[:300],
                    "assistant_reply": item.assistant_reply[:300],
                }
            )

        updated_md: Optional[str] = None
        used_engine = "deterministic_distiller"

        # Try chat-model distillation with strict anti-overfitting instructions
        try:
            from .model_router import model_router

            llm_res = await model_router.chat_json(
                system_prompt=(
                    "You maintain `self_improvement.md` for a multi-agent personal OS assistant.\n"
                    "Analyse user ratings (`good`, `poor`, `frustration`) to diagnose WHY poor replies failed "
                    "and what generalizable habits made good replies succeed.\n"
                    "CRITICAL ANTI-OVERFITTING RULES:\n"
                    "1. Extract GENERAL, ADAPTABLE principles (max 8 bullets in 'What Works Well', max 8 bullets in 'Mistakes to Avoid').\n"
                    "2. NEVER overfit to a single topic, phrase, or rigid response template — do NOT tell the AI to repeat canned wording.\n"
                    "3. Deduplicate and merge overlapping bullets instead of growing the file endlessly.\n"
                    "Return STRICT JSON: {\"works_well\": [\"...\"], \"avoid\": [\"...\"], \"reflection_note\": \"1 concise sentence\"}"
                ),
                user_prompt=(
                    f"Existing self_improvement.md:\n{current_md[:2800]}\n\n"
                    f"New feedback signals:\n{json.dumps(events_summary, ensure_ascii=False)}"
                ),
                num_predict=450,
                timeout=16.0,
            )
            if isinstance(llm_res, dict) and (llm_res.get("works_well") or llm_res.get("avoid")):
                works = [str(x).strip() for x in (llm_res.get("works_well") or []) if str(x).strip()][:8]
                avoid = [str(x).strip() for x in (llm_res.get("avoid") or []) if str(x).strip()][:8]
                note = str(llm_res.get("reflection_note") or "Consolidated recent user feedback.").strip()
                stamp = now.strftime("%Y-%m-%d %H:%M UTC")
                updated_md = (
                    "# AI Self-Improvement & Distilled Principles\n\n"
                    "> Maintained automatically from chat feedback (`👍` / `👎`), detected corrections, and off-peak Cron reflections.\n"
                    "> **Anti-Overfitting Rule:** Every entry below must stay generalizable, adaptable, and concise — never rigid phrasing templates or overfit patterns.\n\n"
                    "## What Works Well (Reinforce)\n"
                    + "\n".join(f"- {w}" for w in works)
                    + "\n\n## Mistakes to Avoid (Correct)\n"
                    + "\n".join(f"- {a}" for a in avoid)
                    + f"\n\n## Recent Reflection Log\n- **[{stamp}]** ({trigger}) {note}\n"
                )
                used_engine = "ai_distiller"
        except Exception:
            updated_md = None

        # Deterministic anti-overfit fallback distiller when local/cloud LLM is offline
        if not updated_md:
            lines = current_md.splitlines()
            stamp = now.strftime("%Y-%m-%d %H:%M UTC")
            new_avoid: List[str] = []
            new_reinforce: List[str] = []
            for item in pending:
                reason_clean = (item.reason or "").strip()
                if item.rating in ("poor", "frustration"):
                    if reason_clean:
                        new_avoid.append(
                            f"Address user feedback ({reason_clean[:140]}) in Principle form without rigid scripting."
                        )
                    elif item.user_prompt:
                        new_avoid.append(
                            f"Re-check intent & tool execution carefully on requests similar to: \"{item.user_prompt[:90]}\" (avoid repeating prior error)."
                        )
                elif item.rating == "good":
                    if reason_clean:
                        new_reinforce.append(
                            f"Keep applying general principle noted by user: {reason_clean[:140]} (adapt naturally to context)."
                        )
                    else:
                        new_reinforce.append(
                            "Direct, accurate tool execution paired with concise natural confirmation delivered strong user satisfaction."
                        )

            updated_md = current_md.rstrip() + "\n"
            for r in new_reinforce[:2]:
                if r not in updated_md:
                    updated_md = updated_md.replace(
                        "## What Works Well (Reinforce)\n",
                        f"## What Works Well (Reinforce)\n- {r}\n",
                        1,
                    )
            for a in new_avoid[:3]:
                if a not in updated_md:
                    updated_md = updated_md.replace(
                        "## Mistakes to Avoid (Correct)\n",
                        f"## Mistakes to Avoid (Correct)\n- {a}\n",
                        1,
                    )
            summary_line = f"- **[{stamp}]** ({trigger}) Distilled {len(pending)} feedback signal(s) into generalizable guidance."
            if "## Recent Reflection Log" in updated_md:
                # Cap reflection log tail to last 10 lines so it never bloats
                head, _, tail = updated_md.partition("## Recent Reflection Log")
                tail_lines = [ln for ln in tail.splitlines() if ln.strip().startswith("- ")][-9:]
                tail_lines.append(summary_line)
                updated_md = head + "## Recent Reflection Log\n" + "\n".join(tail_lines) + "\n"
            else:
                updated_md += f"\n## Recent Reflection Log\n{summary_line}\n"

        self.self_improvement_path().write_text(updated_md, encoding="utf-8")

        for item in pending:
            item.processed = True
            session.add(item)
        session.commit()
        self._last_cron_reflection_at = now

        # Update a single consolidated entry in Shared Multi-Agent Logbook (no spam!)
        if pending:
            poor_cnt = sum(1 for x in pending if x.rating in ("poor", "frustration"))
            good_cnt = sum(1 for x in pending if x.rating == "good")
            agent_logbook.write_log(
                session,
                title="Self-Improvement Feedback Digest",
                content=(
                    f"Latest reflection ({trigger}): processed {poor_cnt} corrective signal(s) and {good_cnt} positive signal(s). "
                    "See `self_improvement.md` for distilled general principles (avoid rigid phrasing/overfitting)."
                ),
                agent_name="self_improver",
                model_id=used_engine,
                category="reflection",
                tags="feedback, self-improvement, anti-overfit",
                pinned=True,
            )

        return {
            "status": "updated",
            "engine": used_engine,
            "processed_count": len(pending),
            "content": updated_md,
        }


self_improvement_service = UnifiedInstructionsAndSelfImprovementService()
