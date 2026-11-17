"""
Node Mini File Explorer & Shared Server Drive (`DriveService`).

Provides a real hierarchical workspace drive rooted at `settings.uploads_dir` so the user,
AI models (Chat, Secretary tools, 24/7 Cron), and server scripts can create folders, upload,
edit, preview, move, rename, search, and exchange files seamlessly.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings


STARTER_DRIVE_FOLDERS: List[str] = [
    "AI-Drop",
    "Study-Docs",
    "Projects",
]

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".py", ".js", ".ts", ".html", ".css", ".sh", ".env", ".toml", ".ini",
    ".ics", ".xml", ".sql", ".log",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
PDF_EXTENSIONS = {".pdf"}


def _clean_rel_path(rel_path: str) -> str:
    if not rel_path:
        return ""
    norm = str(rel_path).replace("\\", "/").strip().strip("/")
    parts: List[str] = []
    for seg in norm.split("/"):
        s = seg.strip()
        if not s or s == ".":
            continue
        if s == "..":
            raise ValueError("Path traversal ('..') is not allowed in Drive paths.")
        # Strip control chars / null bytes
        s = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", s)
        parts.append(s[:120])
    return "/".join(parts)


class DriveService:
    def root_dir(self) -> Path:
        root = Path(settings.uploads_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        # Ensure starter folders exist once on an empty drive (without overwriting if deleted later)
        marker = root / ".drive_initialized"
        if not marker.exists():
            for folder in STARTER_DRIVE_FOLDERS:
                (root / folder).mkdir(parents=True, exist_ok=True)
            try:
                marker.write_text("v10.1\n", encoding="utf-8")
            except OSError:
                pass
        return root

    def resolve_safe(self, rel_path: str = "") -> Path:
        root = self.root_dir()
        clean = _clean_rel_path(rel_path)
        target = (root / clean).resolve() if clean else root
        if target != root and root not in target.parents:
            raise ValueError("Resolved path escapes workspace drive root.")
        return target

    def classify_kind(self, path: Path) -> str:
        if path.is_dir():
            return "folder"
        ext = path.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return "image"
        if ext in PDF_EXTENSIONS:
            return "pdf"
        if ext in TEXT_EXTENSIONS:
            return "text"
        mime, _ = mimetypes.guess_type(path.name)
        if mime and mime.startswith("text/"):
            return "text"
        return "binary"

    def serialize_entry(self, path: Path) -> Dict[str, Any]:
        root = self.root_dir()
        rel = path.relative_to(root).as_posix()
        st = path.stat()
        is_dir = path.is_dir()
        child_count = 0
        if is_dir:
            try:
                child_count = sum(1 for c in path.iterdir() if not c.name.startswith("."))
            except OSError:
                child_count = 0
        kind = self.classify_kind(path)
        return {
            "name": path.name,
            "path": rel,
            "parent": path.parent.relative_to(root).as_posix() if path.parent != root else "",
            "is_dir": is_dir,
            "kind": kind,
            "extension": "" if is_dir else path.suffix.lower(),
            "size_bytes": 0 if is_dir else int(st.st_size),
            "child_count": child_count,
            "children_count": child_count,
            "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "raw_url": None if is_dir else f"/api/v1/drive/raw/{rel}",
        }

    def list_directory(self, rel_path: str = "", search: str = "") -> Dict[str, Any]:
        root = self.root_dir()
        target = self.resolve_safe(rel_path)
        if not target.exists():
            target = root
        if not target.is_dir():
            target = target.parent

        cur_rel = "" if target == root else target.relative_to(root).as_posix()
        breadcrumbs: List[Dict[str, str]] = [{"name": "Drive", "path": ""}]
        if cur_rel:
            acc: List[str] = []
            for seg in cur_rel.split("/"):
                acc.append(seg)
                breadcrumbs.append({"name": seg, "path": "/".join(acc)})

        q = (search or "").strip().lower()
        entries: List[Dict[str, Any]] = []
        if q:
            for item in root.rglob("*"):
                if any(part.startswith(".") for part in item.relative_to(root).parts):
                    continue
                if q in item.name.lower():
                    entries.append(self.serialize_entry(item))
                    if len(entries) >= 200:
                        break
        else:
            for item in target.iterdir():
                if item.name.startswith("."):
                    continue
                entries.append(self.serialize_entry(item))

        entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["name"].lower()))

        # Compute lightweight workspace totals
        total_files = 0
        total_folders = 0
        total_bytes = 0
        for p in root.rglob("*"):
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            if p.is_dir():
                total_folders += 1
            elif p.is_file():
                total_files += 1
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass

        parent_rel: Optional[str] = None
        if cur_rel:
            parent_rel = cur_rel.rsplit("/", 1)[0] if "/" in cur_rel else ""

        return {
            "root_dir": str(root),
            "cwd": cur_rel,
            "current_path": cur_rel,
            "parent": parent_rel,
            "breadcrumbs": breadcrumbs,
            "entries": entries,
            "items": entries,
            "stats": {
                "total_files": total_files,
                "total_folders": total_folders,
                "total_bytes": total_bytes,
            },
        }

    def create_folder(self, rel_path: str) -> Dict[str, Any]:
        clean = _clean_rel_path(rel_path)
        if not clean:
            raise ValueError("Folder name is required.")
        target = self.resolve_safe(clean)
        target.mkdir(parents=True, exist_ok=True)
        return self.serialize_entry(target)

    def read_file(self, rel_path: str, max_chars: int = 200_000) -> Dict[str, Any]:
        clean = _clean_rel_path(rel_path)
        if not clean:
            raise ValueError("File path is required.")
        target = self.resolve_safe(clean)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Drive file not found: {clean}")
        meta = self.serialize_entry(target)
        if meta["kind"] == "text" or target.stat().st_size <= 512_000:
            try:
                txt = target.read_text(encoding="utf-8")
                meta["content"] = txt[:max_chars]
                meta["truncated"] = len(txt) > max_chars
                meta["editable"] = True
                return meta
            except UnicodeDecodeError:
                pass
        meta["content"] = None
        meta["truncated"] = False
        meta["editable"] = False
        return meta

    def write_text_file(self, rel_path: str, content: str) -> Dict[str, Any]:
        clean = _clean_rel_path(rel_path)
        if not clean:
            raise ValueError("File path is required.")
        target = self.resolve_safe(clean)
        if target.exists() and target.is_dir():
            raise ValueError("Cannot overwrite a folder with a file.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        return self.read_file(clean)

    def save_binary_file(self, folder_path: str, filename: str, raw_bytes: bytes) -> Dict[str, Any]:
        safe_name = re.sub(r"[\x00-\x1f/\\<>:\"|?*]", "_", (filename or "upload.bin").strip()).strip(".")
        if not safe_name:
            safe_name = "upload.bin"
        folder_clean = _clean_rel_path(folder_path)
        rel = f"{folder_clean}/{safe_name}" if folder_clean else safe_name
        target = self.resolve_safe(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw_bytes)
        return self.serialize_entry(target)

    def move_or_rename(self, src_path: str, dest_path: str) -> Dict[str, Any]:
        src_clean = _clean_rel_path(src_path)
        dest_clean = _clean_rel_path(dest_path)
        if not src_clean or not dest_clean:
            raise ValueError("Both source and destination paths are required.")
        src = self.resolve_safe(src_clean)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {src_clean}")
        dest = self.resolve_safe(dest_clean)
        if dest.exists() and dest.is_dir():
            dest = dest / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return self.serialize_entry(dest)

    def delete_item(self, rel_path: str) -> bool:
        clean = _clean_rel_path(rel_path)
        if not clean:
            raise ValueError("Cannot delete Drive root.")
        target = self.resolve_safe(clean)
        if not target.exists():
            return False
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True


drive_service = DriveService()
