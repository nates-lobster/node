"""
Decision Gate — LFM3 `d1` / TypeSafe System One integration (Node v10.2)
=========================================================================

Every incoming notification, every tag assignment, and every AI chat turn now
passes through a **decision gate** before any generative model is invoked.

Why a decision model instead of a chat model?
---------------------------------------------
LFM3 `d1-2B` (a.k.a. LiquidAI/d1-2B, served locally by llama.cpp / LM Studio /
vLLM, or hosted at ``https://api.liquid.ai/decisions``) does **not generate
tokens**. You give it a *state* (text or JSON) plus a list of *questions*, and
it answers all of them in **one forward pass** with calibrated probabilities:

* ``noul``   – probability that the answer is yes (gates / flags)
* ``choice`` – most likely option + confidence + full distribution
* ``choice`` with ``multiple`` – every option that applies (multi-label tagging)
* ``text`` / ``number`` / ``date`` – values read out of the state, with spans
* ``score``  – ordered level + distribution

That makes it ideal for three jobs:

1. **Tagging gate** (`tag_notification`) — assigns tags from the live tag
   registry, decides Important vs FYI, picks priority, extracts sender /
   entities / due date, and produces a `dedupe_key` so the same event arriving
   through several feeds (mail, LMS, chat) is collapsed into one row.
2. **Tool-planning gate** (`plan_tools`) — reads the user message and attaches
   only the handful of tool schemas relevant to the turn, so a 2B chat model
   never has to remember 33 tool signatures at once. It also extracts typed
   slots ("band practice", "friday", "4:30pm") so the model *fills fields*
   instead of stuffing the whole sentence into a title.
3. **Event/entity extraction** (`extract_calendar_fields`) — turns
   "mark that I have band practice on friday" into
   ``{title: "Band Practice", date: "2026-10-23", ...}``.

Resolution order (all optional, zero-config degraded path)
----------------------------------------------------------
``settings.decision_base_url`` → ``settings.von_base_url`` → disabled
(``available == False``; callers fall back to the fast/small Ollama model).

Supported wire shapes
---------------------
* Liquid hosted: ``POST {base}/v1/systemone`` (base = ``.../decisions``)
* local server:  ``POST {base}/v1/systemone`` or ``POST {base}/systemone``
* Von container: ``POST {base}/v1/systemone`` (identical TypeSafe wire protocol)

The request body is always the documented System One shape::

    {"model": "d1.2", "state": "...", "questions": {"q1": {...}}}

and the response is normalised by :meth:`DecisionGate._extract_answers`, which
tolerates ``answers`` / ``results`` / ``response`` envelopes and both
``{"choice": "..."}`` and ``{"value": "..."}`` cell shapes.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from .config import settings


# ---------------------------------------------------------------------------
# Typed question helpers (TypeSafe wire format, hand-rolled so we need no SDK)
# ---------------------------------------------------------------------------
def noul(instructions: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


def choice(
    instructions: str,
    criteria: Dict[str, str],
    multiple: bool = False,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"type": "choice", "instructions": instructions, "criteria": criteria}
    if multiple:
        spec["multiple"] = True
    return spec


def score_levels(instructions: str, levels: Sequence[str]) -> Dict[str, Any]:
    return {"type": "score", "instructions": instructions, "levels": list(levels)}


def read_text(instructions: str) -> Dict[str, Any]:
    return {"type": "text", "instructions": instructions}


def read_date(instructions: str) -> Dict[str, Any]:
    return {"type": "date", "instructions": instructions}


def read_number(instructions: str, with_range: bool = False) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"type": "number", "instructions": instructions}
    if with_range:
        spec["range"] = True
    return spec


# ---------------------------------------------------------------------------
# Answer normalisation
# ---------------------------------------------------------------------------
_SUBKEYS = (
    "choice",
    "choices",
    "noul",
    "value",
    "text",
    "number",
    "date",
    "score",
    "label",
    "answer",
    "boolean",
    "bool",
)


def _first_present(d: Dict[str, Any], keys: Iterable[str]) -> Tuple[Any, Optional[str]]:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k], k
    return None, None


def _to_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%")) / (100.0 if "%" in v else 1.0)
        except ValueError:
            return None
    return None


@dataclass
class Answer:
    """One normalised answer cell from the decision model."""

    raw: Dict[str, Any] = field(default_factory=dict)
    value: Any = None
    kind: Optional[str] = None
    confidence: float = 0.0
    distribution: Dict[str, float] = field(default_factory=dict)
    spans: List[Any] = field(default_factory=list)

    # -- conveniences -------------------------------------------------------
    @property
    def text(self) -> str:
        if self.value is None:
            return ""
        if isinstance(self.value, (list, tuple)):
            return ", ".join(str(x) for x in self.value)
        return str(self.value).strip()

    @property
    def labels(self) -> List[str]:
        if isinstance(self.value, (list, tuple)):
            return [str(v).strip() for v in self.value if str(v).strip()]
        txt = self.text
        return [txt] if txt else []

    @property
    def is_yes(self) -> bool:
        if isinstance(self.value, bool):
            return self.value
        if isinstance(self.value, (int, float)):
            return float(self.value) >= 0.5
        txt = self.text.lower()
        if txt in ("true", "yes", "y", "1"):
            return True
        if txt in ("false", "no", "n", "0"):
            return False
        return self.confidence >= 0.5 and self.text not in ("no", "false")

    @property
    def top_label(self) -> str:
        if self.labels:
            return self.labels[0]
        if self.distribution:
            return max(self.distribution.items(), key=lambda kv: kv[1])[0]
        return ""


def _normalise_answer(cell: Any) -> Answer:
    """Turn whatever envelope the server used into one :class:`Answer`."""
    if not isinstance(cell, dict):
        return Answer(raw={"value": cell}, value=cell, confidence=0.9 if cell else 0.0)

    # Some servers wrap: {"answer": {"choice": "x"}}
    for wrap in ("answer", "result", "response", "output"):
        inner = cell.get(wrap)
        if isinstance(inner, dict) and any(k in inner for k in _SUBKEYS):
            merged = {**inner, **{k: v for k, v in cell.items() if k not in ("answer", "result", "response", "output")}}
            cell = merged
            break

    conf = 0.0
    for ck in ("confidence", "probability", "prob", "p", "score_confidence"):
        f = _to_float(cell.get(ck))
        if f is not None:
            conf = f
            break

    dist: Dict[str, float] = {}
    for dk in ("distribution", "probabilities", "probs", "scores", "options"):
        dval = cell.get(dk)
        if isinstance(dval, dict):
            for k, v in dval.items():
                f = _to_float(v if not isinstance(v, dict) else (v.get("probability") or v.get("confidence") or v.get("p")))
                if f is not None:
                    dist[str(k)] = f
            break
        if isinstance(dval, list):
            for item in dval:
                if isinstance(item, dict):
                    lbl = item.get("label") or item.get("choice") or item.get("option") or item.get("name")
                    f = _to_float(item.get("probability") or item.get("confidence") or item.get("p") or item.get("score"))
                    if lbl is not None and f is not None:
                        dist[str(lbl)] = f
            if dist:
                break

    val, kind = _first_present(cell, _SUBKEYS)
    if kind in ("choices",) and isinstance(val, list):
        # Multi-label: list of strings or list of {label, probability}
        labels: List[str] = []
        for v in val:
            if isinstance(v, dict):
                lbl = v.get("label") or v.get("choice") or v.get("value") or v.get("name")
                prob = _to_float(v.get("probability") or v.get("confidence") or v.get("p"))
                if lbl is not None and (prob is None or prob >= 0.5):
                    labels.append(str(lbl))
            elif v is not None:
                labels.append(str(v))
        val = labels
    elif kind in ("noul", "boolean", "bool"):
        if isinstance(val, str):
            val = val.strip().lower() in ("true", "yes", "1", "y")
        elif isinstance(val, (int, float)):
            if conf == 0.0:
                conf = float(val)
            val = float(val) >= 0.5
    if conf == 0.0 and dist:
        top = max(dist.items(), key=lambda kv: kv[1])
        conf = float(top[1])
        if val is None:
            val, kind = top[0], "distribution"

    spans: List[Any] = []
    for sk in ("spans", "span", "offsets", "source_spans"):
        sv = cell.get(sk)
        if sv:
            spans = sv if isinstance(sv, list) else [sv]
            break

    return Answer(raw=cell, value=val, kind=kind, confidence=conf, distribution=dist, spans=spans)


def _extract_answers(payload: Any) -> Dict[str, Answer]:
    if not isinstance(payload, dict):
        return {}
    envelope = payload
    for key in ("answers", "results", "responses", "output", "data", "decisions"):
        cand = payload.get(key)
        if isinstance(cand, dict):
            envelope = cand
            break
    out: Dict[str, Answer] = {}
    for qname, cell in envelope.items():
        if qname in ("model", "usage", "latency_ms", "id", "object", "state", "questions"):
            continue
        if isinstance(cell, (dict, str, int, float, bool, list)):
            out[str(qname)] = _normalise_answer(cell)
    return out


# ---------------------------------------------------------------------------
# Default Inbox tag registry (seeded once; user/AI can edit freely)
# ---------------------------------------------------------------------------
DEFAULT_TAG_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "Urgent",
        "description": "Deadline today, outage, emergency, or a person explicitly asking for immediate action",
        "color": "rose",
        "important": True,
        "builtin": True,
    },
    {
        "name": "School",
        "description": "Assignments, quizzes, exams, grades, professor or teacher messages, LMS updates",
        "color": "sky",
        "important": True,
        "builtin": True,
    },
    {
        "name": "Work",
        "description": "Managers, teammates, clients, meetings, project tooling, scheduling for work",
        "color": "amber",
        "important": True,
        "builtin": True,
    },
    {
        "name": "Personal",
        "description": "Friends, family, plans, invitations, appointments, hobbies (e.g. band practice)",
        "color": "emerald",
        "important": True,
        "builtin": True,
    },
    {
        "name": "Finance",
        "description": "Invoices, receipts, statements, payments, banking or billing notifications",
        "color": "lime",
        "important": True,
        "builtin": True,
    },
    {
        "name": "Updates",
        "description": "Order tracking, shipping, service status, password/security notices, account changes",
        "color": "slate",
        "important": False,
        "builtin": True,
    },
    {
        "name": "Promo",
        "description": "Marketing, sales, newsletters, digests, product announcements, social activity",
        "color": "zinc",
        "important": False,
        "builtin": True,
    },
    {
        "name": "Receipts",
        "description": "Purchase confirmations and receipts that are worth keeping but need no action",
        "color": "zinc",
        "important": False,
        "builtin": True,
    },
    {
        "name": "People",
        "description": "A direct human message addressed to the user by name, needing a reply",
        "color": "violet",
        "important": True,
        "builtin": True,
    },
]

# Priority vocabulary the decision model is allowed to choose from.
PRIORITY_LEVELS = ["critical", "high", "normal", "low"]

# Tool families used by the tool-planning gate.
TOOL_PLAN_GROUPS: Dict[str, List[str]] = {
    "calendar": [
        "list_calendar_events",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "coordinate_day",
    ],
    "tasks": [
        "list_tasks",
        "create_task",
        "update_task",
        "delete_task",
        "sync_tasks_from_sources",
        "learn_task_duration",
    ],
    "inbox": [
        "list_notifications",
        "manage_notifications",
        "manage_inbox_categories",
        "apply_inbox_filter_rule",
    ],
    "bridges": ["manage_api_bridge"],
    "apps": [
        "list_monitored_apps",
        "add_monitored_app",
        "remove_monitored_app",
        "duplicate_app_account",
        "install_catalog_app",
    ],
    "grades": ["get_grades", "manage_grade_tabs"],
    "drive": ["drive_explorer"],
    "goals": ["manage_goals"],
    "memory": [
        "save_notebook_note",
        "update_ai_memory",
        "read_shared_logs",
        "write_shared_log",
        "edit_shared_log",
        "delete_shared_log",
    ],
    "web": ["web_search", "fetch_webpage"],
    "os": ["get_os_overview", "open_os_window", "update_os_setting"],
}


class DecisionGate:
    """Thin, resilient client for LFM3 `d1` / TypeSafe System One decision models."""

    def __init__(self) -> None:
        self._probe_at: float = 0.0
        self._online: bool = False
        self._detail: str = "Not configured"
        self._model: str = ""
        self._resolved_path: str = "/v1/systemone"
        self._resolved_base: str = ""
        self._capabilities: Dict[str, bool] = {}
        self._sem = asyncio.Semaphore(max(1, int(settings.decision_max_parallel)))

    # -- configuration ------------------------------------------------------
    @property
    def configured_base(self) -> str:
        base = (settings.decision_base_url or "").strip().rstrip("/")
        if base:
            return base
        return (settings.von_base_url or "").strip().rstrip("/")

    @property
    def api_key(self) -> str:
        return (settings.decision_api_key or settings.von_api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(settings.decision_enabled and self.configured_base)

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        m = (settings.decision_model or "auto").strip()
        if m and m.lower() != "auto":
            return m
        # Hosted Liquid endpoint + local d1 servers both accept these aliases.
        return "d1.2" if "liquid.ai" in self.configured_base else "d1"

    def _path_candidates(self) -> List[str]:
        base = self.configured_base
        if not base:
            return []
        if base.endswith("/v1/systemone"):
            return [""]
        if base.endswith("/decisions"):
            return ["/v1/systemone"]
        return ["/v1/systemone", "/systemone", "/decisions/v1/systemone"]

    def _models_url(self) -> str:
        base = self.configured_base
        if base.endswith("/decisions"):
            return f"{base}/v1/models"
        if base.endswith("/v1/systemone"):
            return base.replace("/v1/systemone", "/v1/models")
        return f"{base}/v1/models"

    # -- status / probing ---------------------------------------------------
    async def status(self, force: bool = False) -> Dict[str, Any]:
        base = self.configured_base
        now = time.monotonic()
        if force or (now - self._probe_at) > 25.0:
            self._probe_at = now
            self._online = False
            self._detail = "Not configured (optional)"
            if not base:
                self._detail = (
                    "Optional — set DECISION_BASE_URL (e.g. http://localhost:8010 for local d1-2B, "
                    "or https://api.liquid.ai/decisions with DECISION_API_KEY)"
                )
            elif not settings.decision_enabled:
                self._detail = "Disabled via DECISION_ENABLED=false"
            else:
                await self._probe()
        return {
            "engine": "lfm3-d1" if self._online else "none",
            "online": self._online,
            "configured": bool(base),
            "enabled": bool(settings.decision_enabled),
            "base_url": base,
            "model": self.model if self._online else "",
            "path": self._resolved_path,
            "detail": self._detail,
            "confidence_threshold": settings.decision_confidence_threshold,
            "capabilities": dict(self._capabilities),
            "protocol": "typesafe-systemone",
        }

    async def _probe(self) -> None:
        base = self.configured_base
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = float(settings.decision_timeout_seconds or 6.0)

        # 1) Discover the model list (also proves the server is alive).
        discovered: List[str] = []
        try:
            async with httpx.AsyncClient(timeout=min(2.5, timeout)) as client:
                resp = await client.get(self._models_url(), headers=headers)
                if resp.status_code < 400:
                    payload = resp.json()
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict):
                                mid = row.get("id") or row.get("name") or row.get("model")
                                if mid:
                                    discovered.append(str(mid))
        except Exception:
            pass

        # 2) Live typed-question round trip. This is what actually matters:
        #    a generative model answers prose, a decision model returns `answers`.
        last_err = ""
        for path in self._path_candidates():
            url = f"{base}{path}" if path else base
            body = {
                "model": self.model,
                "state": "Gate self-test: the inbox is on fire and the exam is tomorrow.",
                "questions": {
                    "online": noul("Is this situation urgent and time-sensitive?"),
                    "topic": choice(
                        "Which topic does this belong to?",
                        {"School": "Class, exam, homework", "Personal": "Life outside school"},
                    ),
                },
            }
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code} at {url}"
                    continue
                payload = resp.json()
                answers = _extract_answers(payload)
                if not answers:
                    last_err = f"Reply from {url} had no typed `answers` block (is this a generative model?)"
                    continue
                self._resolved_base = base
                self._resolved_path = path or "/"
                self._online = True
                self._model = str(payload.get("model") or self.model)
                self._capabilities = {
                    "noul": "online" in answers,
                    "choice": "topic" in answers,
                    "multiple": True,
                    "text": True,
                    "date": True,
                }
                mods = f" • models: {', '.join(discovered[:6])}" if discovered else ""
                self._detail = f"Connected at {url} (model {self._model}){mods}"
                return
            except Exception as exc:  # pragma: no cover - network variance
                last_err = f"{exc.__class__.__name__} at {url}"
        self._online = False
        self._detail = last_err or "Unreachable"

    @property
    def online(self) -> bool:
        return bool(self._online)

    # -- core call ----------------------------------------------------------
    async def ask(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Answer]]:
        """Send one System One request. Returns ``None`` if the gate is offline."""
        if not self.enabled:
            return None
        if not self._online:
            await self.status()
        if not self._online:
            return None

        if isinstance(state, (dict, list)):
            state_txt = json.dumps(state, ensure_ascii=False)[:12000]
        else:
            state_txt = str(state)[:12000]

        if not questions:
            return {}
        # API cap: 128 questions per request.
        trimmed = dict(list(questions.items())[:120])

        url = f"{self._resolved_base}{self._resolved_path}" if self._resolved_path not in ("", "/") else self._resolved_base
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "state": state_txt, "questions": trimmed}
        use_timeout = float(timeout or settings.decision_timeout_seconds or 6.0)

        async with self._sem:
            try:
                async with httpx.AsyncClient(timeout=use_timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    # Auth / model mismatch is worth surfacing once.
                    self._detail = f"HTTP {resp.status_code} from {url}: {resp.text[:180]}"
                    if resp.status_code in (401, 403):
                        self._online = False
                    return None
                return _extract_answers(resp.json())
            except Exception as exc:
                self._detail = f"{exc.__class__.__name__} calling {url}"
                return None

    async def ask_many(
        self,
        items: Sequence[Any],
        build_questions,
        *,
        concurrency: int = 3,
    ) -> List[Optional[Dict[str, Answer]]]:
        """Run one decision request per item, bounded in parallel."""
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(idx: int, item: Any):
            async with sem:
                try:
                    return await self.ask(item, build_questions(item, idx))
                except Exception:
                    return None

        return list(await asyncio.gather(*[_one(i, it) for i, it in enumerate(items)]))


decision_gate = DecisionGate()


# ===========================================================================
# 1) Notification tagging gate
# ===========================================================================
_TAG_SPLIT_RE = re.compile(r"[,;|/]+|\s+&\s+|\s+and\s+", re.IGNORECASE)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "is", "are",
    "was", "were", "be", "been", "with", "your", "you", "we", "our", "it", "this",
    "that", "from", "by", "as", "has", "have", "will", "can", "new", "re", "fw",
    "fwd", "updates", "update", "notification", "message", "email", "via",
}


def _norm_tag(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip()).strip(" .,:;-|\"'").lower()


def compute_dedupe_key(source_app: str, title: str, *, sender: str = "", body: str = "") -> str:
    """
    Content-addressed identity for an Inbox item.

    Deliberately ignores the *source app* and the *sender display form* (a
    service name vs. a person's name) so the same real-world event arriving
    through Gmail + Canvas, or twice from the same scraper with a different
    sender label, collapses into ONE Inbox row with merged tags.
    """
    text = " ".join([title or "", body[:600]]).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\d{1,2}[:/.-]\d{1,2}(?:[:/.-]\d{2,4})?", " ", text)  # times / short dates
    text = re.sub(r"[^a-z0-9@.\s]", " ", text)
    tokens = [t for t in text.split() if len(t) > 2 and t not in _STOPWORDS]
    # Email addresses are strong identity signals: keep the local part only.
    addrs = sorted({t.split("@")[0] for t in tokens if "@" in t})
    body_tokens = [t for t in tokens if "@" not in t]
    head = " ".join(body_tokens[:18])
    import hashlib

    basis = "|".join([head, ",".join(addrs[:3]), str(len(body_tokens) // 6)])
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:24]


def build_tag_criteria(tags: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in tags:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        desc = str(t.get("description") or "").strip()
        out[name] = desc or name
    return out


async def tag_notification(
    *,
    source_app: str,
    title: str,
    body: str = "",
    sender: str = "",
    tags: Optional[Sequence[Dict[str, Any]]] = None,
    known_categories: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Run one notification through the decision gate.

    Returns a normalised dict::

        {"tags": [...], "category": str, "is_important": bool, "priority": str,
         "sender": str, "summary": str, "due_hint": str, "dedupe_key": str,
         "confidence": float, "engine": "lfm3-d1"}

    or ``None`` when the gate is offline (caller falls back to the fast model).
    """
    register = list(tags or DEFAULT_TAG_REGISTRY)
    criteria = build_tag_criteria(register)
    if known_categories:
        for c in known_categories:
            c = str(c or "").strip()
            if c and c not in criteria:
                criteria[c] = f"Custom user category '{c}'"

    state = {
        "service": source_app,
        "sender": sender,
        "subject_or_title": title[:300],
        "body": (body or "")[:2400],
        "instruction": "Tag this Inbox item. Pick every tag that genuinely applies.",
    }
    questions = {
        "tags": choice(
            "Which tags apply to this item? Select every tag that applies (usually 1-3).",
            criteria,
            multiple=True,
        ),
        "primary_category": choice(
            "Which single tag is the best primary category for the Inbox?",
            criteria,
        ),
        "needs_action": noul(
            "Does a busy student need to read this or take an action (reply, submit, pay, attend, prepare)?"
        ),
        "priority": choice(
            "How urgent is this item?",
            {
                "critical": "Due today, happening now, or an emergency",
                "high": "Due within ~48h or requires a reply/action soon",
                "normal": "Informational but genuinely relevant",
                "low": "Optional reading, marketing, digests, social noise",
            },
        ),
        "clean_title": read_text(
            "Read the subject/title only, cleaned of UI junk, service prefixes and sender names. "
            "If the title is only a sender name, write a short subject from the body. Max 90 characters."
        ),
        "sender_name": read_text("Read the human or organization name that sent this item. Empty if unknown."),
        "one_line": read_text(
            "Read or write ONE crisp sentence summarising what this item is about (max 140 chars). "
            "Prefer the actual content over the subject line."
        ),
        "due_hint": read_date(
            "Read the date this item is due or happens, if explicitly stated in the text (YYYY-MM-DD). "
            "Empty if no date is stated. Do not guess."
        ),
    }
    answers = await decision_gate.ask(state, questions)
    if not answers:
        return None

    tag_list: List[str] = []
    for lbl in (answers.get("tags").labels if answers.get("tags") else []):
        for piece in _TAG_SPLIT_RE.split(lbl):
            nm = _norm_tag(piece)
            if not nm:
                continue
            canonical = next((c for c in criteria if _norm_tag(c) == nm), None)
            if canonical and canonical not in tag_list:
                tag_list.append(canonical)

    primary = (answers.get("primary_category").text if answers.get("primary_category") else "").strip()
    if primary:
        canonical = next((c for c in criteria if _norm_tag(c) == _norm_tag(primary)), None) or primary[:40]
        if canonical not in tag_list:
            tag_list.insert(0, canonical)

    needs_action = answers.get("needs_action").is_yes if answers.get("needs_action") else True
    priority = (answers.get("priority").text if answers.get("priority") else "").strip().lower()
    if priority not in PRIORITY_LEVELS:
        priority = "high" if needs_action else "low"

    # A tag explicitly marked unimportant in the registry (Promo, Receipts…) never
    # promotes an item to Important on its own.
    important_tags = {_norm_tag(t["name"]) for t in register if t.get("important")}
    tag_implies_important = any(_norm_tag(t) in important_tags for t in tag_list)
    is_important = bool(needs_action or tag_implies_important) and "promo" not in {_norm_tag(t) for t in tag_list}

    clean_title = (answers.get("clean_title").text if answers.get("clean_title") else "").strip()
    one_line = (answers.get("one_line").text if answers.get("one_line") else "").strip()
    sender_name = (answers.get("sender_name").text if answers.get("sender_name") else "").strip()
    due_hint = (answers.get("due_hint").text if answers.get("due_hint") else "").strip()
    if due_hint and not re.match(r"^\d{4}-\d{2}-\d{2}$", due_hint):
        m = re.search(r"\d{4}-\d{2}-\d{2}", due_hint)
        due_hint = m.group(0) if m else ""

    confs = [
        float(a.confidence)
        for a in (
            answers.get("tags"),
            answers.get("primary_category"),
            answers.get("priority"),
        )
        if a is not None
    ]
    confidence = round(sum(confs) / len(confs), 3) if confs else 0.0

    return {
        "tags": tag_list or ["General"],
        "category": (tag_list[0] if tag_list else "General"),
        "is_important": bool(is_important),
        "priority": priority,
        "clean_title": clean_title[:220],
        "summary": one_line[:240],
        "sender": sender_name[:120],
        "due_hint": due_hint,
        "dedupe_key": compute_dedupe_key(source_app, title, sender=sender, body=body),
        "confidence": confidence,
        "engine": "lfm3-d1",
    }


# ===========================================================================
# 2) Chat tool-planning gate
# ===========================================================================
_CAL_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


async def plan_tools(
    message: str,
    *,
    tool_names: Sequence[str],
    calendar_table: str = "",
    today_iso: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Decide which tools a chat turn actually needs.

    Returns::

        {"groups": ["calendar"], "tools": ["create_calendar_event"],
         "intent": "create_event", "confidence": 0.9,
         "slots": {"title": "Band Practice", "date": "2026-10-23",
                   "start_time": "16:30", "duration_minutes": 60},
         "engine": "lfm3-d1"}

    A return value of ``None`` means "no opinion" — the caller should attach a
    safe default subset rather than all 33 tools.
    """
    available = list(tool_names)
    group_criteria = {
        g: ", ".join([n for n in names if n in available])
        for g, names in TOOL_PLAN_GROUPS.items()
        if any(n in available for n in names)
    }
    if not group_criteria:
        return None

    state = {
        "user_message": (message or "")[:1500],
        "today": today_iso,
        "instructions": (
            "You route a request to OS tool families before a small chat model runs. "
            "Only include families whose tools are actually needed to fulfil the request. "
            "A question needing no tool should return no groups."
        ),
    }
    questions: Dict[str, Dict[str, Any]] = {
        "groups": choice(
            "Which tool families must be called to fulfil this request? Select all that apply. "
            "Return no groups for pure questions, explanations, or small talk.",
            group_criteria,
            multiple=True,
        ),
        "intent": choice(
            "What is the user's intent?",
            {
                "none": "No OS action — a question, chat, or explanation",
                "create": "Create something new (event, task, note, app, file, goal)",
                "update": "Modify, move, reschedule, rename, complete or tag something existing",
                "delete": "Delete or cancel something",
                "query": "Look up / list / report existing data",
                "settings": "Change OS appearance, layout, settings or AI instructions",
                "web": "Search the web or read a webpage",
            },
        ),
    }

    # Calendar slot extraction gets its own focused questions — this is what
    # stops an event from being created as "I have band practice on friday".
    wants_event = bool(
        re.search(
            r"\b(calendar|event|schedule|appointment|meeting|practice|rehearsal|class|"
            r"dentist|doctor|flight|trip|birthday|party|remind|add|put|mark|block)\b",
            message or "",
            re.IGNORECASE,
        )
    )
    if wants_event and "create_calendar_event" in available:
        questions.update(
            {
                "event_title": read_text(
                    "Read or write the SHORT event title only (2-5 words, title case, no dates, "
                    "no pronouns, no 'I have', no 'on friday'). Example: 'Band Practice'."
                ),
                "event_date": read_date(
                    "Read the calendar date this event happens (YYYY-MM-DD). "
                    "Resolve relative phrases (friday, tomorrow, next tue) using the TODAY value and "
                    "the calendar lookup table in the state. Output the exact YYYY-MM-DD."
                ),
                "event_start": read_text(
                    "Read the start time (24-hour HH:MM). Empty string if no time was stated."
                ),
                "event_minutes": read_number(
                    "Read or estimate the event duration in minutes. Default 60 if unspecified."
                ),
                "event_category": choice(
                    "Which calendar category fits?",
                    {
                        "Class": "School class or lecture",
                        "Study Block": "Focused studying or homework",
                        "Extracurricular": "Club, sport, band, practice, rehearsal",
                        "Quick Comms": "Calls, emails, errands",
                        "Personal": "Anything else personal",
                    },
                ),
                "event_location": read_text("Read the location, if stated. Empty otherwise."),
            }
        )

    answers = await decision_gate.ask(state, questions)
    if not answers:
        return None

    groups: List[str] = []
    for lbl in (answers.get("groups").labels if answers.get("groups") else []):
        for piece in _TAG_SPLIT_RE.split(lbl):
            nm = _norm_tag(piece)
            match = next((g for g in group_criteria if _norm_tag(g) == nm), None)
            if match and match not in groups:
                groups.append(match)

    intent = (answers.get("intent").text if answers.get("intent") else "").strip().lower() or "none"
    # A create/update/delete intent with no group is almost always a routing miss;
    # fall back to a keyword-derived group rather than dropping the tool call.
    if not groups and intent not in ("none", "web"):
        low = (message or "").lower()
        if re.search(r"\b(event|calendar|schedule|meeting|practice|appointment)\b", low) and "calendar" in group_criteria:
            groups = ["calendar"]
        elif re.search(r"\b(task|to-?do|due|assignment|homework)\b", low) and "tasks" in group_criteria:
            groups = ["tasks"]
        elif re.search(r"\b(email|inbox|notification|message|tag)\b", low) and "inbox" in group_criteria:
            groups = ["inbox"]

    tools: List[str] = []
    for g in groups:
        tools.extend([n for n in TOOL_PLAN_GROUPS.get(g, []) if n in available])

    slots: Dict[str, Any] = {}
    if "create_calendar_event" in available and wants_event:
        def _txt(key: str) -> str:
            a = answers.get(key)
            return (a.text if a else "").strip()

        title = _txt("event_title")
        date_v = _txt("event_date")
        m = _CAL_DATE_RE.search(date_v)
        date_v = m.group(0) if m else ""
        start = _txt("event_start")
        m_t = re.search(r"(\d{1,2}):(\d{2})", start)
        if m_t:
            hh, mm = int(m_t.group(1)), int(m_t.group(2))
            if 1 <= hh <= 7:  # decision models sometimes return 12h without meridiem
                hh += 12
            start = f"{hh:02d}:{mm:02d}"
        elif re.search(r"\b(\d{1,2})\s*(am|pm)\b", start, re.IGNORECASE):
            m_a = re.search(r"\b(\d{1,2})\s*(am|pm)\b", start, re.IGNORECASE)
            hh = int(m_a.group(1)) % 12
            if m_a.group(2).lower() == "pm":
                hh += 12
            start = f"{hh:02d}:00"
        else:
            start = ""

        try:
            minutes = int(float(_txt("event_minutes") or 0))
        except ValueError:
            minutes = 0
        if minutes <= 0 or minutes > 720:
            minutes = 60

        slots = {
            "title": title[:200],
            "date": date_v,
            "start_time": start,
            "duration_minutes": minutes,
            "category": (_txt("event_category") or "Personal")[:40],
            "location": _txt("event_location")[:200],
        }

    conf = answers.get("groups").confidence if answers.get("groups") else 0.0
    return {
        "groups": groups,
        "tools": tools,
        "intent": intent,
        "slots": slots,
        "confidence": float(conf or 0.0),
        "engine": "lfm3-d1",
    }


async def classify_feedback(message: str, assistant_reply: str = "") -> Optional[Dict[str, Any]]:
    """Decide whether this exchange should trigger reflection / learning."""
    answers = await decision_gate.ask(
        {
            "user_message": (message or "")[:1200],
            "assistant_reply": (assistant_reply or "")[:1200],
        },
        {
            "frustrated": noul("Is the user frustrated, annoyed, correcting the assistant, or repeating a request?"),
            "needs_reflection": noul(
                "Is there a concrete lesson the assistant should record to avoid repeating a mistake?"
            ),
            "failure_mode": choice(
                "What was the primary failure, if any?",
                {
                    "none": "No failure",
                    "wrong_tool": "Used no tool / the wrong tool for an action request",
                    "bad_fields": "Tool called but fields were wrong (e.g. title contained the whole sentence, wrong date)",
                    "wrong_data": "Reported incorrect data or date",
                    "too_verbose": "Ignored formatting/length preferences",
                    "misunderstood": "Misread the request",
                },
            ),
        },
    )
    if not answers:
        return None
    return {
        "frustrated": answers.get("frustrated").is_yes if answers.get("frustrated") else False,
        "needs_reflection": answers.get("needs_reflection").is_yes if answers.get("needs_reflection") else False,
        "failure_mode": (answers.get("failure_mode").text if answers.get("failure_mode") else "none") or "none",
        "confidence": answers.get("needs_reflection").confidence if answers.get("needs_reflection") else 0.0,
        "engine": "lfm3-d1",
    }
