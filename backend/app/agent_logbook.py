from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from sqlmodel import Session, select

from .config import settings
from .models import AgentLogEntry


class AgentLogbookService:
    """
    Shared Multi-Agent Workspace Logbook (`agent_logs` SQLite table + `/data/ai_shared_log.md`).
    Gives every AI instance & model full rights to read, write, edit, pin, and prune shared
    operational logs so heterogeneous models (local Ollama, remote Ollama, Cloud APIs, Cron)
    collaborate seamlessly across sessions and contexts.
    """

    def __init__(self, md_path: Optional[str] = None) -> None:
        self.md_path = Path(md_path or settings.ai_shared_log_path)

    def _serialize(self, row: AgentLogEntry) -> Dict[str, Any]:
        return {
            "id": row.id,
            "agent_name": row.agent_name,
            "model_id": row.model_id,
            "category": row.category,
            "title": row.title,
            "content": row.content,
            "tags": row.tags,
            "pinned": bool(row.pinned),
            "revision": int(getattr(row, "revision", 1) or 1),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def sync_markdown_mirror(self, session: Session) -> None:
        try:
            rows = session.exec(
                select(AgentLogEntry)
                .order_by(AgentLogEntry.pinned.desc(), AgentLogEntry.updated_at.desc())
                .limit(120)
            ).all()
            lines = [
                "# Node Shared Multi-Agent Workspace Logbook",
                "_Automatically maintained by collaborating AI models & background services._",
                "",
            ]
            for r in rows:
                pin_badge = "[PINNED] " if r.pinned else ""
                ts = r.updated_at.strftime("%Y-%m-%d %H:%M UTC") if r.updated_at else ""
                lines.append(f"## #{r.id} {pin_badge}{r.title} (`{r.category}`)")
                lines.append(f"- **Agent / Model:** `{r.agent_name}` (`{r.model_id}`) • **Updated:** {ts}")
                if r.tags:
                    lines.append(f"- **Tags:** {r.tags}")
                lines.append("")
                lines.append(r.content.strip())
                lines.append("")
            self.md_path.parent.mkdir(parents=True, exist_ok=True)
            self.md_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def list_logs(
        self,
        session: Session,
        *,
        category: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        stmt = select(AgentLogEntry).order_by(AgentLogEntry.pinned.desc(), AgentLogEntry.updated_at.desc())
        if category:
            stmt = stmt.where(AgentLogEntry.category == category.strip())
        rows = session.exec(stmt.limit(max(1, min(limit, 200)))).all()
        if query:
            ql = query.strip().lower()
            rows = [
                r
                for r in rows
                if ql in (r.title or "").lower()
                or ql in (r.content or "").lower()
                or ql in (r.tags or "").lower()
                or ql in (r.model_id or "").lower()
            ]
        return [self._serialize(r) for r in rows]

    def write_log(
        self,
        session: Session,
        *,
        title: str,
        content: str,
        agent_name: str = "secretary",
        model_id: str = "auto",
        category: str = "shared-note",
        tags: str = "",
        pinned: bool = False,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        clean_title = (title or "Agent Log").strip()[:240]
        # Deduplicate rapid identical titles in same category by updating in-place for efficiency
        existing = session.exec(
            select(AgentLogEntry)
            .where(AgentLogEntry.title == clean_title)
            .where(AgentLogEntry.category == category.strip()[:60])
        ).first()
        if existing:
            existing.content = content.strip()[:12000]
            existing.agent_name = (agent_name or existing.agent_name)[:80]
            existing.model_id = (model_id or existing.model_id)[:160]
            if tags:
                existing.tags = tags[:300]
            existing.pinned = bool(pinned or existing.pinned)
            existing.revision = int(getattr(existing, "revision", 1) or 1) + 1
            existing.updated_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            self.sync_markdown_mirror(session)
            return self._serialize(existing)

        entry = AgentLogEntry(
            agent_name=(agent_name or "secretary")[:80],
            model_id=(model_id or "auto")[:160],
            category=(category or "shared-note")[:60],
            title=clean_title,
            content=content.strip()[:12000],
            tags=(tags or "")[:300],
            pinned=bool(pinned),
            created_at=now,
            updated_at=now,
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        self.sync_markdown_mirror(session)
        return self._serialize(entry)

    def edit_log(
        self,
        session: Session,
        log_id: int,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        append_content: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[Any] = None,
        pinned: Optional[bool] = None,
        agent_name: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        row = session.get(AgentLogEntry, log_id)
        if not row:
            return None
        if title is not None and title.strip():
            row.title = title.strip()[:240]
        if content is not None:
            row.content = content.strip()[:12000]
        if append_content is not None and str(append_content).strip():
            sep = "\n" if row.content else ""
            row.content = f"{row.content}{sep}{str(append_content).strip()}"[:12000]
        if category is not None and category.strip():
            row.category = category.strip()[:60]
        if tags is not None:
            tag_str = ", ".join(str(x) for x in tags) if isinstance(tags, list) else str(tags)
            row.tags = tag_str[:300]
        if pinned is not None:
            row.pinned = bool(pinned)
        if agent_name:
            row.agent_name = agent_name[:80]
        if model_id:
            row.model_id = model_id[:160]
        row.revision = int(getattr(row, "revision", 1) or 1) + 1
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        self.sync_markdown_mirror(session)
        return self._serialize(row)

    def delete_log(self, session: Session, log_id: int) -> bool:
        row = session.get(AgentLogEntry, log_id)
        if not row:
            return False
        session.delete(row)
        session.commit()
        self.sync_markdown_mirror(session)
        return True

    def auto_record_event(
        self,
        session: Session,
        *,
        agent_name: str,
        model_id: str,
        category: str,
        title: str,
        content: str,
        tags: str = "auto-telemetry",
        pinned: bool = False,
    ) -> Optional[Dict[str, Any]]:
        from .ai_providers import ai_provider_registry

        routing = ai_provider_registry.load_state().get("routing") or {}
        if not routing.get("auto_agent_logging", True):
            return None
        try:
            return self.write_log(
                session,
                title=title,
                content=content,
                agent_name=agent_name,
                model_id=model_id,
                category=category,
                tags=tags,
                pinned=pinned,
            )
        except Exception:
            return None

    def render_digest_for_prompt(self, session: Session, limit: int = 12) -> str:
        rows = session.exec(
            select(AgentLogEntry)
            .order_by(AgentLogEntry.pinned.desc(), AgentLogEntry.updated_at.desc())
            .limit(limit)
        ).all()
        if not rows:
            return "(Shared Multi-Agent Logbook is currently empty — feel free to call `write_shared_log` to record helpful discoveries, workflow shortcuts, or handoff context for peer models!)"
        out: List[str] = []
        for r in rows:
            pin = "[PINNED] " if r.pinned else ""
            snip = (r.content or "").replace("\n", " ").strip()
            if len(snip) > 220:
                snip = snip[:217] + "..."
            out.append(f"  - [log #{r.id}] {pin}({r.category} | by {r.agent_name}/{r.model_id}) {r.title}: {snip}")
        return "\n".join(out)


agent_logbook = AgentLogbookService()
