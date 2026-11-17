import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import settings

DEFAULT_MEMORY_TEMPLATE = """# Secretary Persona & Standing Instructions

## How to Talk to Me
- Act like a sharp, calm, organized personal secretary—friendly and concise.
- Put urgent deadlines, today's schedule blocks, and daily goal steps right at the top.
- Learn from my corrections: when I tell you how long something takes or how I like things organized, save it in your notebook files and apply it next time.

## My Workday Preferences
- Keep quick communication tasks (chat replies, quick emails, confirmations) short and realistic—never schedule 40 minutes for a 5-minute chat reply.
- Batch routine emails and chat check-ins together rather than fragmenting deep focus blocks.

## Standing Context
- You can update this file or organize topic files in `/data/notebook/` anytime.
"""

DEFAULT_TIME_ESTIMATES_MD = """# Learned Task Durations & Pacing Rules

> Read and updated live by your AI Secretary & Schedule Coordinator. Edit any number below or tell the Secretary in chat (e.g., *"I only need 5 minutes to reply to Teams"*).

## Active Archetype Durations
- **chat_reply**: 5 min — Quick replies on Microsoft Teams, Discord, Slack, DMs, or threads
- **email_triage**: 10 min — Reading/replying to emails (Outlook, ProtonMail, Gmail) & inbox checks
- **quick_admin**: 15 min — Uploading files, forms, reservations, quick checks
- **coursework_frq**: 45 min — Problem sets, assignments, focused study blocks
- **deep_project**: 50 min — Deep coding, writing, builds, or extended project sessions

## Custom Keyword Rules
<!-- Format: - keyword: "phrase" => 15 min -->

## Learned Calibration Log
- **[Initial Baseline]** Configured realistic defaults (5m chat replies, 10m emails, 15m quick admin, 45m assignments, 50m deep work).
"""

DEFAULT_COURSES_CLUBS_MD = """# Commitments & Schedule Context

> Persistent context for recurring classes, work, meetings, and commitments.

## Active Commitments

## Notes & Logistics
"""

DEFAULT_GOALS_STRATEGY_MD = """# Long-Term Goals & Daily Compounding Notes

> Tracks multi-week ambitions, milestone reflections, and daily habits.

## Coaching Principles
- Protect 20–30 minutes daily for long-term goals.
- Break large deliverables into clear milestones.

## Goal Reflections
"""

DEFAULT_SCRATCHPAD_MD = """# Secretary Scratchpad & General Notes

> General-purpose notes, ideas, reminders, and quick captures.

## Captured Notes
"""


def _slugify_filename(raw_name: str) -> str:
    clean = raw_name.strip().lower()
    if clean.endswith(".md"):
        clean = clean[:-3]
    slug = re.sub(r"[^a-z0-9_-]+", "_", clean).strip("_-")
    return slug[:60] or "note"


class MemoryAndNotesStore:
    """
    Manages:
      1. Standing Persona & Rules (`ai_memory.md`)
      2. Multi-File AI Notebook (`/data/notebook/*.md`) so the AI can organize thoughts
         across dedicated files (`time_estimates.md`, `courses_and_clubs.md`,
         `goals_and_strategy.md`, `scratchpad.md`, plus any new `.md` files created on demand!)
      3. Live Task Duration Estimator & Feedback Learner backed directly by `time_estimates.md`.
    """

    CORE_NOTEBOOK_FILES: Dict[str, Tuple[str, str]] = {
        "time_estimates": ("Time Estimates & Pacing", DEFAULT_TIME_ESTIMATES_MD),
        "courses_and_clubs": ("Courses & Extracurriculars", DEFAULT_COURSES_CLUBS_MD),
        "goals_and_strategy": ("Goals & Strategy", DEFAULT_GOALS_STRATEGY_MD),
        "scratchpad": ("General Scratchpad", DEFAULT_SCRATCHPAD_MD),
    }

    DEFAULT_ARCHETYPE_MINUTES: Dict[str, int] = {
        "chat_reply": 5,
        "email_triage": 10,
        "quick_admin": 15,
        "coursework_frq": 45,
        "deep_project": 50,
    }

    def __init__(
        self,
        memory_path: Optional[str] = None,
        notes_path: Optional[str] = None,
        notebook_dir: Optional[str] = None,
    ) -> None:
        self.memory_file = Path(memory_path or settings.ai_memory_path)
        self.notes_file = Path(notes_path or settings.ai_notes_path)
        if notebook_dir:
            self.notebook_dir = Path(notebook_dir)
        elif memory_path:
            # Keep isolated test temp dirs clean!
            self.notebook_dir = Path(memory_path).parent / "notebook"
        else:
            self.notebook_dir = Path(settings.ai_notebook_dir)

    def ensure_files(self) -> None:
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.notes_file.parent.mkdir(parents=True, exist_ok=True)
        self.notebook_dir.mkdir(parents=True, exist_ok=True)

        if not self.memory_file.is_file():
            self.memory_file.write_text(DEFAULT_MEMORY_TEMPLATE.strip() + "\n", encoding="utf-8")

        for slug, (_label, template) in self.CORE_NOTEBOOK_FILES.items():
            fpath = self.notebook_dir / f"{slug}.md"
            if not fpath.is_file():
                # Migrate existing ai_notes.md into scratchpad.md on first upgrade if present
                if slug == "scratchpad" and self.notes_file.is_file():
                    existing_notes = self.notes_file.read_text(encoding="utf-8").strip()
                    if existing_notes:
                        fpath.write_text(existing_notes + "\n", encoding="utf-8")
                        continue
                fpath.write_text(template.strip() + "\n", encoding="utf-8")

        if not self.notes_file.is_file():
            scratch = (self.notebook_dir / "scratchpad.md").read_text(encoding="utf-8")
            self.notes_file.write_text(scratch, encoding="utf-8")

    def _file_info(self, path: Path, slug: Optional[str] = None, title: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_files()
        content = path.read_text(encoding="utf-8")
        stat = path.stat()
        first_heading = None
        for ln in content.splitlines():
            if ln.startswith("# "):
                first_heading = ln[2:].strip()
                break
        derived_slug = slug or path.stem
        return {
            "slug": derived_slug,
            "filename": path.name,
            "title": title or first_heading or derived_slug.replace("_", " ").title(),
            "is_core": derived_slug in self.CORE_NOTEBOOK_FILES,
            "content": content,
            "path": str(path),
            "size_bytes": stat.st_size,
            "line_count": len(content.splitlines()),
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------------
    # Standing Persona Memory (`ai_memory.md`)
    # -----------------------------------------------------------------------
    def get_memory(self) -> Dict[str, Any]:
        return self._file_info(self.memory_file, slug="ai_memory", title="Persona & Standing Rules")

    def save_memory(self, content: str) -> Dict[str, Any]:
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        cleaned = content.rstrip() + "\n"
        tmp = self.memory_file.with_suffix(".tmp")
        tmp.write_text(cleaned, encoding="utf-8")
        tmp.replace(self.memory_file)
        return self.get_memory()

    def append_memory_rule(self, rule_text: str) -> Dict[str, Any]:
        self.ensure_files()
        clean_rule = rule_text.strip().lstrip("-* ").strip()
        if not clean_rule:
            return self.get_memory()
        existing = self.memory_file.read_text(encoding="utf-8").rstrip()
        if clean_rule.lower() in existing.lower():
            return self.get_memory()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        updated = f"{existing}\n- {clean_rule} *(added {stamp})*\n"
        return self.save_memory(updated)

    # -----------------------------------------------------------------------
    # Multi-File AI Notebook (`/data/notebook/*.md`)
    # -----------------------------------------------------------------------
    def list_notebook_files(self) -> List[Dict[str, Any]]:
        self.ensure_files()
        files = sorted(self.notebook_dir.glob("*.md"), key=lambda p: (p.stem not in self.CORE_NOTEBOOK_FILES, p.name))
        out: List[Dict[str, Any]] = []
        for fpath in files:
            label = self.CORE_NOTEBOOK_FILES.get(fpath.stem, (None, ""))[0]
            out.append(self._file_info(fpath, slug=fpath.stem, title=label))
        return out

    def get_notebook_file(self, slug_or_name: str) -> Dict[str, Any]:
        self.ensure_files()
        slug = _slugify_filename(slug_or_name)
        fpath = self.notebook_dir / f"{slug}.md"
        if not fpath.is_file():
            raise FileNotFoundError(f"Notebook file '{slug}.md' not found")
        label = self.CORE_NOTEBOOK_FILES.get(slug, (None, ""))[0]
        return self._file_info(fpath, slug=slug, title=label)

    def save_notebook_file(self, slug_or_name: str, content: str, title: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_files()
        slug = _slugify_filename(slug_or_name)
        fpath = self.notebook_dir / f"{slug}.md"
        body = content.rstrip()
        if not body.startswith("#") and title:
            body = f"# {title.strip()}\n\n{body}"
        tmp = fpath.with_suffix(".tmp")
        tmp.write_text(body + "\n", encoding="utf-8")
        tmp.replace(fpath)

        # Mirror scratchpad to legacy ai_notes.md for backward compatibility
        if slug == "scratchpad":
            self.notes_file.write_text(body + "\n", encoding="utf-8")

        label = title or self.CORE_NOTEBOOK_FILES.get(slug, (None, ""))[0]
        return self._file_info(fpath, slug=slug, title=label)

    def append_to_notebook_file(
        self,
        slug_or_name: str,
        note_text: str,
        category: str = "Note",
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        self.ensure_files()
        slug = _slugify_filename(slug_or_name)
        fpath = self.notebook_dir / f"{slug}.md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        bullet = f"- **[{stamp}]** *({category})* — {note_text.strip()}"

        if not fpath.is_file():
            if not create_if_missing:
                raise FileNotFoundError(f"Notebook file '{slug}.md' does not exist")
            pretty_title = slug.replace("_", " ").title()
            initial = f"# {pretty_title}\n\n> Created by your AI Secretary to organize {pretty_title.lower()} notes.\n\n## Notes\n{bullet}\n"
            return self.save_notebook_file(slug, initial, title=pretty_title)

        existing = fpath.read_text(encoding="utf-8").rstrip()
        updated = f"{existing}\n{bullet}\n"
        return self.save_notebook_file(slug, updated)

    def delete_notebook_file(self, slug_or_name: str) -> bool:
        self.ensure_files()
        slug = _slugify_filename(slug_or_name)
        if slug in self.CORE_NOTEBOOK_FILES:
            raise ValueError(f"'{slug}.md' is a core system notebook file and cannot be deleted (you can edit or clear its contents).")
        fpath = self.notebook_dir / f"{slug}.md"
        if not fpath.is_file():
            return False
        fpath.unlink()
        return True

    def route_topic_slug_for_note(self, text: str) -> str:
        """Pick the best notebook `.md` file for a thought/note automatically."""
        lower = text.lower()
        if any(w in lower for w in ("minute", "mins", "hour", "takes me", "duration", "schedule length", "too long")):
            return "time_estimates"
        if any(w in lower for w in ("goal", "milestone", "sat", "act", "streak", "long-term", "common app", "scholarship")):
            return "goals_and_strategy"
        if any(w in lower for w in ("class", "course", "prof", "teacher", "lab", "robotics", "club", "practice", "ap ")):
            return "courses_and_clubs"
        return "scratchpad"

    def select_relevant_notebook_context(self, query: str, max_chars_per_file: int = 750) -> Tuple[str, List[str]]:
        """
        Return a compact, CPU-friendly context string of ONLY the most relevant notebook
        files for `query` so small 3B-4B CPU models think fast without 64K token bloat!
        """
        self.ensure_files()
        lower = query.lower()
        chosen_slugs = ["time_estimates"]

        for fpath in sorted(self.notebook_dir.glob("*.md")):
            slug = fpath.stem
            if slug in chosen_slugs:
                continue
            words = [w for w in slug.split("_") if len(w) > 2]
            if any(w in lower for w in words):
                chosen_slugs.append(slug)

        if "goal" in lower or "streak" in lower or "milestone" in lower:
            if "goals_and_strategy" not in chosen_slugs:
                chosen_slugs.append("goals_and_strategy")
        if any(k in lower for k in ("class", "club", "robotics", "course", "lab", "schedule", "calendar")):
            if "courses_and_clubs" not in chosen_slugs:
                chosen_slugs.append("courses_and_clubs")
        if len(chosen_slugs) < 2:
            chosen_slugs.append("scratchpad")

        chosen_slugs = chosen_slugs[:3]
        chunks: List[str] = []
        loaded_names: List[str] = []
        for slug in chosen_slugs:
            fpath = self.notebook_dir / f"{slug}.md"
            if fpath.is_file():
                txt = fpath.read_text(encoding="utf-8").strip()
                chunks.append(f"### File: {slug}.md\n{txt[:max_chars_per_file]}")
                loaded_names.append(f"{slug}.md")
        return "\n\n".join(chunks), loaded_names

    # Legacy compatibility methods (`ai_notes.md` <-> `scratchpad.md`)
    def get_notes(self) -> Dict[str, Any]:
        return self.get_notebook_file("scratchpad")

    def save_notes(self, content: str) -> Dict[str, Any]:
        return self.save_notebook_file("scratchpad", content)

    def append_note(self, note_text: str, category: str = "Note", heading: Optional[str] = None) -> Dict[str, Any]:
        cat = heading or category
        target_slug = self.route_topic_slug_for_note(note_text)
        return self.append_to_notebook_file(target_slug, note_text, category=cat)

    # -----------------------------------------------------------------------
    # Learned Task Duration Engine (Backed by `/data/notebook/time_estimates.md`)
    # -----------------------------------------------------------------------
    def parse_time_estimates(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        """
        Parse live archetype durations and custom keyword rules directly from `time_estimates.md`.
        Returns `(archetype_minutes, keyword_overrides)`.
        """
        self.ensure_files()
        md = (self.notebook_dir / "time_estimates.md").read_text(encoding="utf-8")
        archetypes = dict(self.DEFAULT_ARCHETYPE_MINUTES)
        keywords: Dict[str, int] = {}

        for key in archetypes:
            m = re.search(rf"\*\*{re.escape(key)}\*\*\s*:\s*(\d{{1,3}})\s*min", md, re.IGNORECASE)
            if m:
                archetypes[key] = max(2, min(240, int(m.group(1))))

        for km in re.finditer(r'-\s*keyword:\s*"([^"]+)"\s*=>\s*(\d{1,3})\s*min', md, re.IGNORECASE):
            kw = km.group(1).strip().lower()
            mins = max(2, min(240, int(km.group(2))))
            if kw:
                keywords[kw] = mins

        return archetypes, keywords

    def classify_task_archetype(self, title: str, category: str = "", source_app: str = "") -> str:
        """Classify a task into a realistic duration archetype."""
        combined = f"{title} {category} {source_app}".lower()

        # 1. Quick chat / thread / DM replies (Teams, Discord, Slack, chat, DM)
        is_chat_surface = any(w in combined for w in ("teams", "discord", "slack", "chat", "dm", "thread", "ping", "message"))
        is_quick_verb = any(w in combined for w in ("reply", "respond", "sign-off", "sign off", "acknowledge", "ping", "text", "ask"))
        if is_chat_surface and (is_quick_verb or "unread" in combined or "#" in title):
            return "chat_reply"
        if re.search(r"\b(?:reply|respond)\s+to\b", combined) and "email" not in combined and "outlook" not in combined:
            return "chat_reply"

        # 2. Email / Inbox triage (Outlook, ProtonMail, Gmail, email, inbox)
        if any(w in combined for w in ("email", "e-mail", "outlook", "protonmail", "proton", "gmail", "inbox", "key rotation")):
            return "email_triage"

        # 3. Quick admin / confirmation / upload / scheduling
        if any(w in combined for w in ("confirm ", "rsvp", "upload ", "submit ", "form", "permission", "reservation", "check ")):
            if not any(heavy in combined for heavy in ("frq", "problem set", "essay", "exam", "calculus", "physics")):
                return "quick_admin"

        # 4. Deep build / writing / project
        if any(w in combined for w in ("essay", "common app", "lab report", "autonomous", "prototype", "paper", "project", "build")):
            return "deep_project"

        # 5. Standard academic coursework / FRQ / problem set
        return "coursework_frq"

    def estimate_task_minutes(self, title: str, category: str = "", source_app: str = "") -> Tuple[int, str]:
        """Return `(estimated_minutes, reason_tag)` using `time_estimates.md`."""
        archetypes, keywords = self.parse_time_estimates()
        lower_title = title.lower()

        # Check specific learned keyword overrides first
        for kw, mins in keywords.items():
            if kw in lower_title or kw in source_app.lower():
                return mins, f"custom rule '{kw}' ({mins}m)"

        arch = self.classify_task_archetype(title=title, category=category, source_app=source_app)
        mins = archetypes.get(arch, 30)
        return mins, f"{arch} ({mins}m)"

    def learn_duration_preference(
        self,
        target_archetype: Optional[str],
        minutes: int,
        raw_user_feedback: str,
        custom_keyword: Optional[str] = None,
        also_calibrate_related: bool = True,
    ) -> Dict[str, Any]:
        """
        Persist user duration feedback into `time_estimates.md` AND generalize smartly
        to sibling categories (e.g. learning 5m for Teams chats ensures quick emails stay ~10m).
        """
        self.ensure_files()
        mins = max(2, min(240, int(minutes)))
        fpath = self.notebook_dir / "time_estimates.md"
        md = fpath.read_text(encoding="utf-8")
        archetypes, _keywords = self.parse_time_estimates()
        updates_applied: List[str] = []

        if target_archetype and target_archetype in archetypes:
            old_val = archetypes[target_archetype]
            md = re.sub(
                rf"(\*\*{re.escape(target_archetype)}\*\*\s*:\s*)\d{{1,3}}(\s*min)",
                rf"\g<1>{mins}\2",
                md,
                flags=re.IGNORECASE,
            )
            updates_applied.append(f"`{target_archetype}`: {old_val}m → **{mins}m**")

            # Cross-category generalization: if user teaches us quick chat replies take N min (e.g. 5m),
            # calibrate sibling quick-comms (`email_triage` ~ 2x = 10m, `quick_admin` ~ 3x = 15m) if they were higher!
            if also_calibrate_related and target_archetype == "chat_reply":
                suggested_email = max(5, min(20, mins * 2))
                if archetypes.get("email_triage", 10) != suggested_email:
                    old_em = archetypes["email_triage"]
                    md = re.sub(
                        r"(\*\*email_triage\*\*\s*:\s*)\d{1,3}(\s*min)",
                        rf"\g<1>{suggested_email}\2",
                        md,
                        flags=re.IGNORECASE,
                    )
                    updates_applied.append(f"`email_triage` (related quick comms): {old_em}m → **{suggested_email}m**")

                suggested_admin = max(10, min(25, mins * 3))
                if archetypes.get("quick_admin", 15) > suggested_admin:
                    old_ad = archetypes["quick_admin"]
                    md = re.sub(
                        r"(\*\*quick_admin\*\*\s*:\s*)\d{1,3}(\s*min)",
                        rf"\g<1>{suggested_admin}\2",
                        md,
                        flags=re.IGNORECASE,
                    )
                    updates_applied.append(f"`quick_admin` (short errands/uploads): {old_ad}m → **{suggested_admin}m**")

        if custom_keyword:
            kw_clean = custom_keyword.strip().lower()[:50]
            rule_line = f'- keyword: "{kw_clean}" => {mins} min'
            pattern = rf'-\s*keyword:\s*"{re.escape(kw_clean)}"\s*=>\s*\d{{1,3}}\s*min'
            if re.search(pattern, md, re.IGNORECASE):
                md = re.sub(pattern, rule_line, md, flags=re.IGNORECASE)
            elif "## Custom Keyword Rules" in md:
                md = md.replace("## Custom Keyword Rules", f"## Custom Keyword Rules\n{rule_line}", 1)
            else:
                md += f"\n## Custom Keyword Rules\n{rule_line}\n"
            updates_applied.append(f"keyword `\"{kw_clean}\"` → **{mins}m**")

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        summary_str = ", ".join(updates_applied) if updates_applied else f"Set duration to {mins}m"
        log_entry = f"- **[{stamp}]** Learned from feedback (*\"{raw_user_feedback[:120]}\"*): {summary_str}"
        if "## Learned Calibration Log" in md:
            md = md.rstrip() + f"\n{log_entry}\n"
        else:
            md = md.rstrip() + f"\n\n## Learned Calibration Log\n{log_entry}\n"

        self.save_notebook_file("time_estimates", md)
        new_arch, new_kw = self.parse_time_estimates()
        return {
            "updates_applied": updates_applied,
            "archetypes": new_arch,
            "keywords": new_kw,
            "file": "time_estimates.md",
        }


memory_store = MemoryAndNotesStore()
