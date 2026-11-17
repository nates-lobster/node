import base64
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import mimetypes
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from sqlmodel import Session, select

from .calendar_engine import parse_ics_content
from .config import settings
from .memory_store import memory_store
from .model_router import _normalize_task_key, _smart_heuristic_duration
from .models import CalendarEvent, Task
from .task_intelligence import extract_and_save_tasks_from_text


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".ics", ".log"}

BUILTIN_WALLPAPERS: List[Dict[str, Any]] = [
    {
        "id": "emerald-fjord",
        "title": "Nordic Aurora Fjord",
        "subtitle": "Twilight fjord & #2bfbae mint aurora reflection",
        "kind": "image",
        "url": "/wallpapers/emerald-fjord.jpg",
        "builtin": True,
    },
    {
        "id": "misty-pines",
        "title": "Blue-Hour Pine Valley",
        "subtitle": "Layered Pacific Northwest ridges & glassy lake",
        "kind": "image",
        "url": "/wallpapers/misty-pines.jpg",
        "builtin": True,
    },
    {
        "id": "rainy-study-loft",
        "title": "Rainy Study Loft",
        "subtitle": "Cozy night desk lamp & rain-slicked slate skyline",
        "kind": "image",
        "url": "/wallpapers/rainy-study-loft.jpg",
        "builtin": True,
    },
    {
        "id": "starlight-peak",
        "title": "Starlight Alpine Lake",
        "subtitle": "Milky Way & emerald airglow over calm peaks",
        "kind": "image",
        "url": "/wallpapers/starlight-peak.jpg",
        "builtin": True,
    },
    {
        "id": "slate-mint-mesh",
        "title": "Slate Mint Ambient (Zero-Image)",
        "subtitle": "Ultra-lightweight pure CSS slate & #2bfbae glow",
        "kind": "css",
        "url": "",
        "builtin": True,
    },
    {
        "id": "minimal-slate",
        "title": "Pure Matte Slate (Zero-Image)",
        "subtitle": "Distraction-free flat dark slate canvas",
        "kind": "css",
        "url": "",
        "builtin": True,
    },
]


def sanitize_filename(raw_name: str) -> str:
    base = Path(raw_name or "uploaded_file.txt").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._-") or "file"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", Path(base).suffix.lower())[:10]
    return f"{stem[:80]}{ext}"


def _extract_inline_due_date(line: str) -> Tuple[str, Optional[str]]:
    """Extract an inline due date from a todo line if present, returning `(clean_title, due_iso)`."""
    today = datetime.now(timezone.utc).date()
    iso_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", line)
    if iso_m:
        due = iso_m.group(1)
        cleaned = re.sub(r"\s*(?:\(?\bdue:?\s*)?" + re.escape(due) + r"\)?", "", line, flags=re.IGNORECASE).strip()
        return cleaned, due

    lower = line.lower()
    if re.search(r"\b(?:due\s+)?today\b", lower):
        cleaned = re.sub(r"\s*\(?\bdue\s+today\)?|\s*\(today\)", "", line, flags=re.IGNORECASE).strip()
        return cleaned or line.strip(), today.isoformat()
    if re.search(r"\b(?:due\s+)?tomorrow\b", lower):
        cleaned = re.sub(r"\s*\(?\bdue\s+tomorrow\)?|\s*\(tomorrow\)", "", line, flags=re.IGNORECASE).strip()
        return cleaned or line.strip(), (today + timedelta(days=1)).isoformat()

    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for w_name, w_idx in weekdays.items():
        if re.search(rf"\bdue\s+{w_name}\b|\({w_name}\)", lower):
            delta = (w_idx - today.weekday()) % 7
            due = (today + timedelta(days=delta or 7)).isoformat()
            cleaned = re.sub(rf"\s*\(?\bdue\s+{w_name}\)?|\s*\({w_name}\)", "", line, flags=re.IGNORECASE).strip()
            return cleaned or line.strip(), due

    return line.strip(), today.isoformat()


def parse_tasks_from_text(text: str, filename: str = "upload.txt") -> List[Dict[str, Any]]:
    """
    Parse structured or semi-structured To-Do lists from `.md`, `.txt`, `.csv`, or `.json`
    into actionable task dictionaries. Skips completed `[x]` items automatically!
    """
    ext = Path(filename).suffix.lower()
    candidates: List[Tuple[str, Optional[str], str]] = []  # (title, due_date, category)

    if ext == ".json":
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("tasks", data.get("todos", []))
            for item in items:
                if isinstance(item, str) and item.strip():
                    t_clean, due = _extract_inline_due_date(item)
                    candidates.append((t_clean, due, "Imported"))
                elif isinstance(item, dict) and not item.get("completed"):
                    title = str(item.get("title") or item.get("task") or item.get("name") or "").strip()
                    if title:
                        due = item.get("due_date") or item.get("due")
                        cat = str(item.get("category") or "Imported")
                        candidates.append((title, due, cat))
        except Exception:
            pass

    elif ext in {".csv", ".tsv"}:
        delim = "\t" if ext == ".tsv" else ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        rows = [r for r in reader if any(c.strip() for c in r)]
        for idx, row in enumerate(rows):
            first = row[0].strip()
            if idx == 0 and first.lower() in {"task", "title", "todo", "item", "assignment"}:
                continue
            due = row[1].strip() if len(row) > 1 and re.match(r"^\d{4}-\d{2}-\d{2}$", row[1].strip()) else None
            cat = row[2].strip() if len(row) > 2 and row[2].strip() else "Academics"
            t_clean, inferred_due = _extract_inline_due_date(first)
            if t_clean:
                candidates.append((t_clean, due or inferred_due, cat))

    if not candidates:
        current_heading = "Academics"
        for raw_ln in text.splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            # Track markdown section headers as task categories!
            h_m = re.match(r"^#{1,3}\s+(.+)$", ln)
            if h_m:
                hdr = h_m.group(1).strip()
                if any(w in hdr.lower() for w in ("club", "robot", "extra", "team")):
                    current_heading = "Extracurricular"
                elif any(w in hdr.lower() for w in ("personal", "errand", "life")):
                    current_heading = "Personal"
                elif any(w in hdr.lower() for w in ("app", "college", "essay")):
                    current_heading = "Applications"
                else:
                    current_heading = "Academics"
                continue

            # Skip already-completed markdown checkboxes `- [x] ...`
            if re.match(r"^[-*+]\s*\[[xX]\]", ln):
                continue

            # Only extract explicit bullets/checkboxes or clear action verbs — NEVER every arbitrary newline!
            is_explicit_bullet = bool(re.match(r"^(?:[-*+]\s*(?:\[\s*\])?\s*|\d+[.)]\s+|(?:todo|task|action):\s*)", ln, re.IGNORECASE))
            has_action_verb = bool(
                re.search(
                    r"\b(?:submit|complete|finish|write|solve|study\s+for|prepare|reply\s+to|email|turn\s+in|problem\s+set|worksheet|essay|lab\s+report|quiz|exam|due\b)\b",
                    ln,
                    re.IGNORECASE,
                )
            )
            if not (is_explicit_bullet or has_action_verb):
                continue

            item_m = re.match(r"^(?:[-*+]\s*(?:\[\s*\])?\s*|\d+[.)]\s+|(?:todo|task|action):\s*)(.+)$", ln, re.IGNORECASE)
            raw_item = item_m.group(1).strip() if item_m else (ln if len(ln) <= 140 and not ln.endswith(":") else "")
            if len(raw_item) < 3:
                continue

            t_clean, due = _extract_inline_due_date(raw_item)
            if t_clean:
                candidates.append((t_clean[:380], due, current_heading))

    out: List[Dict[str, Any]] = []
    seen = set()
    for title, due, cat in candidates[:50]:
        key = _normalize_task_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        est_mins = _smart_heuristic_duration(title)
        out.append(
            {
                "title": title,
                "due_date": due,
                "category": cat,
                "estimated_minutes": est_mins,
                "estimate_source": "ai_heuristic",
            }
        )
    return out


class FileDropService:
    @property
    def uploads_dir(self) -> Path:
        p = Path(settings.uploads_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def list_files(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for f in sorted(self.uploads_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file() or f.name.startswith("."):
                continue
            ext = f.suffix.lower()
            st = f.stat()
            kind = (
                "image"
                if ext in IMAGE_EXTENSIONS
                else ("ics" if ext == ".ics" else ("text" if ext in TEXT_EXTENSIONS else "binary"))
            )
            preview = ""
            if kind in {"text", "ics"} and st.st_size <= 200_000:
                try:
                    preview = f.read_text(encoding="utf-8", errors="replace")[:260]
                except Exception:
                    preview = ""
            items.append(
                {
                    "filename": f.name,
                    "size_bytes": st.st_size,
                    "kind": kind,
                    "ext": ext,
                    "preview": preview,
                    "raw_url": f"/api/v1/files/raw/{f.name}",
                    "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return items

    def list_wallpapers(self) -> List[Dict[str, Any]]:
        out = list(BUILTIN_WALLPAPERS)
        for f in self.list_files():
            if f["kind"] == "image":
                out.append(
                    {
                        "id": f"upload:{f['filename']}",
                        "title": Path(f["filename"]).stem.replace("_", " ").title(),
                        "subtitle": f"Uploaded Photo ({round(f['size_bytes'] / 1024)} KB)",
                        "kind": "image",
                        "url": f["raw_url"],
                        "filename": f["filename"],
                        "builtin": False,
                    }
                )
        return out

    def save_uploaded_file(
        self,
        filename: str,
        content_base64: Optional[str] = None,
        text_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_name = sanitize_filename(filename)
        target = self.uploads_dir / safe_name

        if content_base64:
            # Strip optional `data:...;base64,` prefix
            raw_b64 = content_base64.split(",", 1)[-1] if "," in content_base64[:80] else content_base64
            raw_bytes = base64.b64decode(raw_b64)
            if len(raw_bytes) > 15 * 1024 * 1024:
                raise ValueError("File exceeds 15 MB transfer limit.")
            target.write_bytes(raw_bytes)
        elif text_content is not None:
            raw_bytes = text_content.encode("utf-8")
            if len(raw_bytes) > 15 * 1024 * 1024:
                raise ValueError("File exceeds 15 MB transfer limit.")
            target.write_bytes(raw_bytes)
        else:
            raise ValueError("Provide either content_base64 or text_content.")

        for item in self.list_files():
            if item["filename"] == safe_name:
                return item
        return {"filename": safe_name, "raw_url": f"/api/v1/files/raw/{safe_name}"}

    def get_file_path(self, filename: str) -> Path:
        safe_name = sanitize_filename(filename)
        target = self.uploads_dir / safe_name
        if not target.is_file():
            raise FileNotFoundError(safe_name)
        return target

    def delete_file(self, filename: str) -> bool:
        safe_name = sanitize_filename(filename)
        target = self.uploads_dir / safe_name
        if target.is_file():
            target.unlink()
            return True
        return False

    async def ingest_file_async(self, filename: str, mode: str, session: Session) -> Dict[str, Any]:
        """
        Async file ingestion using AI Task Extraction (`qwen3.5:0.8b` / `2b`) + strict deduplication
        so clicking `+ Tasks` never duplicates tasks or turns prose paragraphs into 25m tasks!
        """
        if mode != "extract_tasks":
            return self.ingest_file(filename, mode, session)

        path = self.get_file_path(filename)
        text = path.read_text(encoding="utf-8", errors="replace")
        ext = path.suffix.lower()

        if ext in {".csv", ".tsv", ".json"}:
            return self.ingest_file(filename, mode, session)

        res = await extract_and_save_tasks_from_text(
            session=session,
            raw_text=text,
            source_name=f"File: {path.name}",
        )
        created_tasks = [
            {
                "id": t.id,
                "title": t.title,
                "due_date": t.due_date,
                "priority": t.priority,
                "estimated_minutes": t.estimated_minutes,
            }
            for t in res["tasks"]
        ]
        dup_note = f" ({res['skipped_duplicates']} already in Tasks skipped)" if res["skipped_duplicates"] else ""
        return {
            "ok": True,
            "mode": mode,
            "tasks_created": len(created_tasks),
            "created_tasks": len(created_tasks),
            "skipped_duplicates": res["skipped_duplicates"],
            "tasks": created_tasks,
            "summary": (
                f"AI extracted {len(created_tasks)} new task(s) from `{path.name}`{dup_note}."
                if created_tasks
                else f"No new tasks to add from `{path.name}`{dup_note}."
            ),
        }

    def ingest_file(self, filename: str, mode: str, session: Session) -> Dict[str, Any]:
        """
        Process a transferred client file into AI Secretary Notebook, Tasks, or Calendar!
        `mode`: `to_notebook` | `extract_tasks` | `import_ics` | `add_to_memory`
        """
        path = self.get_file_path(filename)
        text = path.read_text(encoding="utf-8", errors="replace")

        if mode == "to_notebook":
            slug = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_") or "client_upload"
            pretty = path.stem.replace("_", " ").replace("-", " ").title()
            wrapped = (
                text
                if text.lstrip().startswith("#")
                else f"# {pretty}\n\n> Transferred from client file `{path.name}`.\n\n{text}\n"
            )
            doc = memory_store.save_notebook_file(slug, wrapped, title=pretty)
            return {
                "ok": True,
                "mode": mode,
                "notebook_slug": doc["slug"],
                "notebook_filename": doc["filename"],
                "summary": f"Saved `{path.name}` directly into AI Notebook as `{doc['filename']}`.",
            }

        if mode == "add_to_memory":
            snippet = text.strip()[:2500]
            memory_store.append_memory_rule(f"Imported from `{path.name}`: {snippet[:300]}")
            doc = memory_store.append_to_notebook_file("scratchpad", f"Imported context from `{path.name}`:\n\n{snippet}", category="File Transfer")
            return {
                "ok": True,
                "mode": mode,
                "notebook_slug": doc["slug"],
                "summary": f"Added key rules from `{path.name}` to AI Memory & Scratchpad.",
            }

        if mode == "extract_tasks":
            parsed = parse_tasks_from_text(text, filename=path.name)
            existing_keys = {_normalize_task_key(t.title) for t in session.exec(select(Task)).all()}
            created_tasks: List[Dict[str, Any]] = []
            skipped = 0
            for item in parsed:
                key = _normalize_task_key(item["title"])
                if not key or key in existing_keys:
                    skipped += 1
                    continue
                existing_keys.add(key)
                t = Task(
                    source_app=f"File: {path.name}",
                    title=item["title"],
                    due_date=item["due_date"],
                    category=item["category"],
                    estimated_minutes=item["estimated_minutes"],
                    completed=False,
                )
                session.add(t)
                session.commit()
                session.refresh(t)
                created_tasks.append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "due_date": t.due_date,
                        "estimated_minutes": t.estimated_minutes,
                    }
                )
            dup_note = f" ({skipped} duplicate(s) skipped)" if skipped else ""
            return {
                "ok": True,
                "mode": mode,
                "tasks_created": len(created_tasks),
                "created_tasks": len(created_tasks),
                "skipped_duplicates": skipped,
                "tasks": created_tasks,
                "summary": f"Added {len(created_tasks)} task(s) from `{path.name}`{dup_note}.",
            }

        if mode == "import_ics":
            events = parse_ics_content(text, source_name=path.name)
            added = 0
            for item in events:
                session.add(CalendarEvent(**item))
                added += 1
            session.commit()
            return {
                "ok": True,
                "mode": mode,
                "events_added": added,
                "summary": f"Imported {added} calendar event(s) from `{path.name}`.",
            }

        raise ValueError(f"Unsupported ingest mode: {mode}")


def guess_mime_type(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


file_drop_service = FileDropService()
