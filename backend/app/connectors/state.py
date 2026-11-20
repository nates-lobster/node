"""Per-connector state (cursor, seen ids, health) in ``data/connectors.json``.

Holds no secrets: tokens stay in memory (token_mint).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from ..config import settings

SEEN_LIMIT = 3000

_DEFAULT: Dict[str, Any] = {
    "enabled": True,
    "cursor": None,
    "seen": [],
    "last_run": 0.0,
    "last_ok": 0.0,
    "last_error": "",
    "error_kind": "",
    "failures": 0,
    "backoff_until": 0.0,
    "created_total": 0,
    "last_counts": {},
    "notes": [],
}


class ConnectorStateStore:
    def __init__(self, path: str = "") -> None:
        self._path_override = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return Path(self._path_override or settings.connectors_state_path)

    def _load_all(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            raw = self._load_all().get(key) or {}
        out = json.loads(json.dumps(_DEFAULT))
        out.update(raw if isinstance(raw, dict) else {})
        return out

    def put(self, key: str, state: Dict[str, Any]) -> None:
        state = dict(state)
        seen = list(state.get("seen") or [])
        state["seen"] = seen[-SEEN_LIMIT:]
        with self._lock:
            data = self._load_all()
            data[key] = state
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, key: str, **patch: Any) -> Dict[str, Any]:
        st = self.get(key)
        st.update(patch)
        self.put(key, st)
        return st
