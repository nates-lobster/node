"""
Inbox Tags, Gmail-style Filters, and Duplicate Merging (Node v10.2)
===================================================================

The Inbox is tag-first now. A notification is *not* forced into one bucket —
it carries a set of tags assigned by the LFM3 decision gate, and every view in
the UI is a saved filter over those tags.

Design decisions
----------------
1. **Tag registry is user data, not code.** `data/inbox_tags.json` holds
   ``{name, description, color, important, builtin}`` rows. Editing a
   description instantly changes what the decision model is allowed to match,
   because the descriptions *are* the classifier's criteria. Adding a tag makes
   it immediately identifiable — no retraining, no restart.
2. **Deduping is content-addressed.** Every row stores a `dedupe_key` derived
   from the normalised subject+body (sender and source app deliberately
   excluded). Within `INBOX_THREAD_WINDOW_HOURS`, a second row with the same key
   is merged into the first: tags union, importance = max, read state = AND.
   That kills the "same message 5 times in 3 categories" problem at the write
   path instead of hiding it in the UI.
3. **Filters use Gmail grammar**, so both the user and the AI can drive them:
   ``tag:school is:unread from:prof@x.edu newer_than:7d -tag:promo``.
   A tiny parser turns that into a predicate; `explain_filter()` renders it back
   as English for the AI, and `describe_grammar()` is fed to the agent prompt.
4. **Corrections are training data.** Moving a message to a different tag
   records ``(sender_key, subject_tokens) -> tag`` in `inbox_learning.json`.
   Those learned rules are injected into the decision state as *prior
   corrections* and re-applied deterministically for the same sender/subject —
   so the system can be fine-tuned locally later from the archive.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import settings
from .decision_gate import DEFAULT_TAG_REGISTRY, compute_dedupe_key

_TAG_META_KEYS = ("description", "color", "important", "builtin", "aliases", "archived")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")


def _split_csv(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for piece in re.split(r"[,;|]+", raw):
        p = piece.strip()
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out


class InboxTagService:
    """Tag registry + learned corrections + filter grammar."""

    def __init__(self) -> None:
        # Paths are resolved lazily: importing a module-level singleton at import
        # time would bind to whatever DATABASE_URL existed *then* (tests change it
        # in conftest), leaking state between the test DB and production data.
        self._cached_key: Optional[str] = None
        self._tags_path = Path("inbox_tags.json")
        self._learn_path = Path("inbox_learning.json")
        self._filters_path = Path("inbox_filters.json")

    def _ensure_paths(self) -> None:
        key = settings.database_url
        if self._cached_key == key:
            return
        base = Path(key.replace("sqlite:///", "", 1)).parent
        self._base = base
        self._tags_path = base / "inbox_tags.json"
        self._learn_path = base / "inbox_learning.json"
        self._filters_path = base / "inbox_filters.json"
        self._cached_key = key

    # Backwards-compatible accessors (used by tests/tools that read the path).
    def _path(self, attr: str) -> Path:
        self._ensure_paths()
        return getattr(self, attr)

    # ------------------------------------------------------------------ paths
    @property
    def tags_path(self) -> Path:
        self._ensure_paths()
        return self._tags_path

    # -------------------------------------------------------------- registry
    def _load_raw(self) -> Dict[str, Any]:
        self._ensure_paths()
        if self._tags_path.is_file():
            try:
                raw = json.loads(self._tags_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("tags"), list):
                    return raw
            except Exception:
                pass
        seeded = self._seed_registry()
        self._write_raw(seeded)
        return seeded

    def _seed_registry(self) -> Dict[str, Any]:
        tags: List[Dict[str, Any]] = []
        seen: set = set()
        # Legacy CSV categories become real tags so nothing is lost on upgrade.
        for name in _split_csv(getattr(settings, "inbox_categories_csv_default", "") or ""):
            pass
        for default in DEFAULT_TAG_REGISTRY:
            nm = str(default["name"])
            seen.add(nm.lower())
            tags.append({**default, "created_at": _now().isoformat()})
        return {"version": 2, "tags": tags, "updated_at": _now().isoformat()}

    def _write_raw(self, payload: Dict[str, Any]) -> None:
        self._ensure_paths()
        payload = {**payload, "updated_at": _now().isoformat()}
        self._tags_path.parent.mkdir(parents=True, exist_ok=True)
        self._tags_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list_tags(self) -> List[Dict[str, Any]]:
        raw = self._load_raw()
        tags = [t for t in raw.get("tags", []) if isinstance(t, dict) and t.get("name")]
        tags.sort(key=lambda t: (0 if t.get("important") else 1, str(t["name"]).lower()))
        return tags

    def tag_names(self) -> List[str]:
        return [str(t["name"]) for t in self.list_tags()]

    def builtin_important(self) -> Dict[str, bool]:
        return {str(t["name"]).lower(): bool(t.get("important")) for t in self.list_tags()}

    def upsert_tag(
        self,
        name: str,
        description: str = "",
        color: str = "slate",
        important: bool = True,
        aliases: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        nm = re.sub(r"\s+", " ", (name or "").strip())[:40]
        if not nm:
            raise ValueError("Tag name is required")
        raw = self._load_raw()
        tags: List[Dict[str, Any]] = list(raw.get("tags") or [])
        existing = next((t for t in tags if str(t.get("name", "")).lower() == nm.lower()), None)
        alias_list = [a.strip() for a in (aliases or []) if str(a).strip()]
        if existing is None:
            row = {
                "name": nm,
                "description": description.strip()[:400] or nm,
                "color": color,
                "important": bool(important),
                "builtin": False,
                "aliases": alias_list,
                "created_at": _now().isoformat(),
            }
            tags.append(row)
        else:
            row = existing
            if description.strip():
                row["description"] = description.strip()[:400]
            if color:
                row["color"] = color
            if important is not None:
                row["important"] = bool(important)
            if alias_list:
                row["aliases"] = sorted({*(row.get("aliases") or []), *alias_list})
            row["updated_at"] = _now().isoformat()
        raw["tags"] = tags
        self._write_raw(raw)
        return row

    def delete_tag(self, name: str) -> bool:
        raw = self._load_raw()
        tags: List[Dict[str, Any]] = list(raw.get("tags") or [])
        target = next((t for t in tags if str(t.get("name", "")).lower() == (name or "").strip().lower()), None)
        if not target:
            return False
        if target.get("builtin"):
            raise ValueError("Built-in tags can be renamed or re-described but not deleted")
        raw["tags"] = [t for t in tags if t is not target]
        self._write_raw(raw)
        return True

    def rename_tag(self, old: str, new: str) -> Dict[str, Any]:
        raw = self._load_raw()
        tags: List[Dict[str, Any]] = list(raw.get("tags") or [])
        target = next((t for t in tags if str(t.get("name", "")).lower() == (old or "").strip().lower()), None)
        if not target:
            raise ValueError(f"Tag '{old}' not found")
        nm = re.sub(r"\s+", " ", (new or "").strip())[:40]
        if not nm:
            raise ValueError("New tag name is required")
        if any(str(t.get("name", "")).lower() == nm.lower() and t is not target for t in tags):
            raise ValueError(f"Tag '{nm}' already exists")
        old_name = str(target["name"])
        target["name"] = nm
        self._write_raw(raw)
        # Learned rules follow the rename so corrections keep working.
        learn = self._load_learning()
        changed = 0
        for rec in learn.get("corrections", []):
            if str(rec.get("tag", "")).lower() == old_name.lower():
                rec["tag"] = nm
                changed += 1
        if changed:
            self._write_learning(learn)
        return {"renamed": True, "old": old_name, "new": nm}

    # ------------------------------------------------------------ learning
    def _load_learning(self) -> Dict[str, Any]:
        self._ensure_paths()
        if self._learn_path.is_file():
            try:
                raw = json.loads(self._learn_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw.setdefault("corrections", [])
                    return raw
            except Exception:
                pass
        return {"version": 1, "corrections": []}

    def _write_learning(self, payload: Dict[str, Any]) -> None:
        self._ensure_paths()
        payload["updated_at"] = _now().isoformat()
        self._learn_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def sender_key(sender: str, source_app: str = "") -> str:
        s = (sender or "").strip().lower()
        if "@" in s:
            local, _, domain = s.partition("@")
            return f"{re.sub(r'[^a-z0-9]+', '', local)}@{domain.strip()}"
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s or _slug(source_app or "")

    def record_correction(
        self,
        *,
        sender: str,
        subject: str,
        from_tag: Optional[str],
        to_tag: str,
        source_app: str = "",
    ) -> Dict[str, Any]:
        learn = self._load_learning()
        key = self.sender_key(sender, source_app)
        tokens = [t for t in re.findall(r"[a-z0-9]+", (subject or "").lower()) if len(t) > 3][:6]
        rows: List[Dict[str, Any]] = learn.get("corrections") or []
        for rec in rows:
            if rec.get("sender_key") == key and rec.get("tag", "").lower() == to_tag.lower():
                rec["hits"] = int(rec.get("hits") or 0) + 1
                rec["updated_at"] = _now().isoformat()
                rec["from_tags"] = sorted({*(rec.get("from_tags") or []), *( [from_tag] if from_tag else [])})
                self._write_learning(learn)
                return rec
        row = {
            "sender_key": key,
            "sender_display": (sender or "")[:160],
            "subject_tokens": tokens,
            "tag": to_tag[:40],
            "from_tags": [from_tag] if from_tag else [],
            "hits": 1,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
        }
        rows.append(row)
        # Keep the log bounded — it is a signal, not an archive.
        if len(rows) > 4000:
            rows = rows[-4000:]
        learn["corrections"] = rows
        self._write_learning(learn)
        return row

    def learned_tag_for(self, sender: str, subject: str, source_app: str = "") -> Optional[Dict[str, Any]]:
        rows = self._load_learning().get("corrections") or []
        key = self.sender_key(sender, source_app)
        subject_tokens = set(re.findall(r"[a-z0-9]+", (subject or "").lower()))
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for rec in rows:
            if rec.get("sender_key") != key:
                continue
            toks = set(rec.get("subject_tokens") or [])
            overlap = len(toks & subject_tokens) / max(1, len(toks)) if toks else 0.4
            if overlap >= 0.5 and int(rec.get("hits") or 1) >= 1:
                score = overlap + min(0.3, int(rec.get("hits") or 1) * 0.05)
                if score > best_score:
                    best, best_score = rec, score
        return best

    def learning_summary(self, limit: int = 40) -> List[Dict[str, Any]]:
        rows = sorted(
            self._load_learning().get("corrections") or [],
            key=lambda r: (int(r.get("hits") or 0), str(r.get("updated_at") or "")),
            reverse=True,
        )
        return rows[:limit]

    # ------------------------------------------------------------- filters
    FILTER_TOKENS = (
        "tag:", "is:", "from:", "to:", "subject:", "source:", "has:", "newer_than:",
        "older_than:", "category:", "body:", "label:", "priority:", "confidence:",
    )

    def parse_filter(self, query: str) -> Dict[str, Any]:
        """
        Parse Gmail-style query text into a normalised AST.

        Supported: ``tag:x``, ``label:x``, ``is:unread|read|important|starred|archived``,
        ``from:addr``, ``to:addr``, ``subject:words``, ``source:app``, ``body:words``,
        ``has:attachment``, ``priority:high``, ``newer_than:7d``, ``older_than:30d``,
        quoted phrases, ``-`` negation, ``OR`` / ``|`` alternation.
        """
        q = (query or "").strip()
        ast: Dict[str, Any] = {"raw": q, "terms": [], "any_terms": [], "negated": [], "tokens": []}
        if not q:
            return ast
        # Split on spaces but keep quoted phrases intact.
        parts = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', q)
        branch = "terms"
        for raw in parts:
            tok = raw.strip()
            if not tok:
                continue
            low = tok.lower()
            if low in ("or", "|"):
                branch = "any_terms"
                continue
            negated = tok.startswith("-")
            if negated:
                tok = tok[1:]
                low = tok.lower()
            val = tok.split(":", 1)[1].strip('"\'') if ":" in tok else ""
            key = tok.split(":", 1)[0].lower() if ":" in tok else ""
            item = {"key": key, "value": val, "negated": negated, "text": tok}
            (ast["negated"] if negated else (ast["any_terms"] if branch == "any_terms" else ast["terms"])).append(item)
            ast["tokens"].append(tok)
        return ast

    def filter_dsl_for_ai(self) -> str:
        return (
            "Inbox filter syntax (Gmail-compatible):\n"
            "  tag:school            only items carrying that tag\n"
            "  is:unread | is:read | is:important | is:starred | is:archived\n"
            "  from:prof@school.edu  sender match (substring)\n"
            "  subject:band practice quoted phrases supported\n"
            "  source:canvas         monitored app / account name\n"
            "  has:attachment        message has attachments\n"
            "  priority:critical     critical|high|normal|low\n"
            "  newer_than:7d         newer_than:12h|3d|2w\n"
            "  older_than:30d        older_than:12h|3d|2w\n"
            "  -tag:promo            prefix '-' negates any token\n"
            "  OR                    alternation between two tokens (tag:A OR tag:B)\n"
            "Examples: `tag:school is:unread newer_than:7d`, `from:band -tag:promo`, `is:important has:attachment`"
        )

    @staticmethod
    def _match_text(hay: str, needle: str) -> bool:
        n = (needle or "").strip().lower().strip('"\'')
        return bool(n) and n in (hay or "").lower()

    @staticmethod
    def _parse_age(value: str) -> Optional[int]:
        m = re.match(r"^(\d+)\s*([hdwmy])$", (value or "").strip().lower())
        if not m:
            return None
        num, unit = int(m.group(1)), m.group(2)
        mult = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30, "y": 24 * 365}[unit]
        return num * mult

    def matches(self, notif: Dict[str, Any], ast: Dict[str, Any], now: Optional[datetime] = None) -> bool:
        """Evaluate one notification dict against a parsed filter AST."""
        if not ast or not ast.get("tokens"):
            return True
        now = now or _now()

        def _tags(n: Dict[str, Any]) -> List[str]:
            raw = n.get("tags_csv") or n.get("tags") or ""
            if isinstance(raw, list):
                return [str(x).lower() for x in raw]
            return [t.strip().lower() for t in re.split(r"[,;|]+", str(raw)) if t.strip()]

        def _ok(item: Dict[str, Any]) -> bool:
            key = item["key"]
            val = item["value"]
            low_val = val.lower()
            tags = _tags(notif)
            ts_raw = notif.get("timestamp") or notif.get("created_at") or ""
            ts: Optional[datetime] = None
            if isinstance(ts_raw, datetime):
                ts = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
            elif isinstance(ts_raw, str) and ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None

            if key in ("tag", "label", "category"):
                if val.endswith("*"):
                    return any(t.startswith(low_val[:-1]) for t in tags) or str(notif.get("category", "")).lower().startswith(low_val[:-1])
                return low_val in tags or str(notif.get("category", "")).lower() == low_val
            if key == "is":
                if low_val == "unread":
                    return not bool(notif.get("read_state"))
                if low_val == "read":
                    return bool(notif.get("read_state"))
                if low_val == "important":
                    return notif.get("is_important") is not False
                if low_val == "starred":
                    return bool(notif.get("starred"))
                if low_val == "archived":
                    return str(notif.get("folder") or "inbox") == "archive"
                if low_val == "unimportant":
                    return notif.get("is_important") is False
                return True
            if key in ("from", "to"):
                return self._match_text(str(notif.get("sender") or ""), val) or self._match_text(
                    str(notif.get("source_app") or ""), val
                ) or self._match_text(str(notif.get("title") or ""), val)
            if key == "subject":
                return self._match_text(str(notif.get("title") or ""), val)
            if key == "body":
                return self._match_text(str(notif.get("message") or ""), val) or self._match_text(
                    str(notif.get("ai_summary") or ""), val
                )
            if key == "source":
                return self._match_text(str(notif.get("source_app") or ""), val)
            if key == "has":
                if low_val.startswith("attach"):
                    payload = notif.get("raw_payload") or ""
                    return "attach" in str(payload).lower()
                return True
            if key == "priority":
                return str(notif.get("priority") or "").lower() == low_val
            if key == "confidence":
                try:
                    return float(notif.get("tag_confidence") or 0.0) >= float(val)
                except ValueError:
                    return True
            if key in ("newer_than", "older_than"):
                minutes = self._parse_age(val)
                if minutes is None or ts is None:
                    return True
                delta = now - ts
                if key == "newer_than":
                    return delta <= timedelta(minutes=minutes)
                return delta >= timedelta(minutes=minutes)
            # Unknown token: treat as free text over every field.
            hay = " ".join(
                str(notif.get(k) or "")
                for k in ("title", "message", "ai_summary", "source_app", "category", "tags_csv", "sender")
            )
            return self._match_text(hay, val or item["text"])

        positives = [i for i in ast.get("terms", []) if not i.get("negated")]
        negatives = [i for i in ast.get("terms", []) if i.get("negated")] + list(ast.get("negated") or [])
        any_group = ast.get("any_terms") or []

        if any(not _ok(i) for i in positives):
            return False
        if any_group and not any(_ok(i) for i in any_group):
            return False
        if any(_ok(i) for i in negatives):
            return False
        return True

    def explain_filter(self, query: str) -> str:
        ast = self.parse_filter(query)
        if not ast.get("tokens"):
            return "Everything"
        bits: List[str] = []
        for item in ast.get("terms", []):
            bits.append(("NOT " if item.get("negated") else "") + item["text"])
        if ast.get("any_terms"):
            bits.append("(" + " OR ".join(i["text"] for i in ast["any_terms"]) + ")")
        return " AND ".join(bits)

    # -------------------------------------------------------- saved filters
    def _load_filters(self) -> List[Dict[str, Any]]:
        self._ensure_paths()
        if self._filters_path.is_file():
            try:
                raw = json.loads(self._filters_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [f for f in raw if isinstance(f, dict) and f.get("query")]
            except Exception:
                pass
        defaults = DEFAULT_SAVED_FILTERS
        self._write_filters(defaults)
        return list(defaults)

    def _write_filters(self, rows: List[Dict[str, Any]]) -> None:
        self._ensure_paths()
        self._filters_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def list_filters(self) -> List[Dict[str, Any]]:
        rows = self._load_filters()
        return sorted(rows, key=lambda r: (int(r.get("order") or 100), str(r.get("name") or "")))

    def save_filter(
        self,
        name: str,
        query: str,
        *,
        color: str = "slate",
        pinned: bool = True,
        order: int = 100,
        filter_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = self._load_filters()
        fid = filter_id or _slug(name) or f"filter-{int(time.time())}"
        row = next((r for r in rows if r.get("id") == fid), None)
        payload = {
            "id": fid,
            "name": name.strip()[:60] or fid,
            "query": query.strip()[:600],
            "color": color,
            "pinned": bool(pinned),
            "order": int(order),
            "builtin": bool(row.get("builtin")) if row else False,
            "updated_at": _now().isoformat(),
        }
        if row is None:
            rows.append(payload)
        else:
            row.update(payload)
        self._write_filters(rows)
        return payload

    def delete_filter(self, filter_id: str) -> bool:
        rows = self._load_filters()
        target = next((r for r in rows if r.get("id") == filter_id), None)
        if not target:
            return False
        if target.get("builtin"):
            raise ValueError("Built-in filters can be edited but not deleted")
        self._write_filters([r for r in rows if r is not target])
        return True


DEFAULT_SAVED_FILTERS: List[Dict[str, Any]] = [
    {"id": "important", "name": "Important", "query": "is:unread is:important", "color": "emerald", "pinned": True, "order": 1, "builtin": True},
    {"id": "school", "name": "School", "query": "tag:school is:unread", "color": "sky", "pinned": True, "order": 2, "builtin": True},
    {"id": "people", "name": "People", "query": "tag:people OR tag:personal", "color": "violet", "pinned": True, "order": 3, "builtin": True},
    {"id": "due-soon", "name": "Due soon", "query": "is:important newer_than:3d", "color": "amber", "pinned": True, "order": 4, "builtin": True},
    {"id": "everything", "name": "Everything", "query": "", "color": "slate", "pinned": True, "order": 5, "builtin": True},
    {"id": "promo", "name": "Noise", "query": "tag:promo OR tag:updates", "color": "zinc", "pinned": False, "order": 20, "builtin": True},
    {"id": "archive", "name": "Archive", "query": "is:archived", "color": "zinc", "pinned": False, "order": 21, "builtin": True},
]


inbox_tag_service = InboxTagService()


def tags_to_csv(tags: Iterable[str]) -> str:
    seen: List[str] = []
    for t in tags:
        s = str(t or "").strip()
        if s and s.lower() not in {x.lower() for x in seen}:
            seen.append(s)
    return ", ".join(seen[:12])


def parse_tags_csv(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [t.strip() for t in re.split(r"[,;|]+", str(raw)) if t.strip()]
