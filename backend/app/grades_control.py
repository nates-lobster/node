"""
Grades Control — explicit course ownership (Node v10.2)
=======================================================

Problem this solves: a scan re-discovers every course the LMS will list, so
deleting a course only lasted until the next visit. Here, deletion is an
*intent*, not a row removal:

* ``CourseVisibility.row(..., hidden=True)`` — the course stays in the DB (its
  history keeps accumulating quietly) but is excluded from the tracker, GPA and
  averages. Scans never resurrect it in the UI.
* ``tracked=False`` — excluded from everything, and re-discovery is ignored.
* ``counts_toward_gpa`` / ``counts_toward_average`` — independent toggles, so a
  pass/fail or elective course can show in the list without moving the GPA.
* ``GradeTab`` — named groups of courses with their own averages ("Core",
  "Electives", "AP only"). At least one `match_rules` entry maps courses in
  automatically; explicit `course_keys` always win.

Everything is additive to `grades_service.get_dashboard()`, which keeps its
existing shape and gains `tabs`, `visibility`, and `scoped` summary blocks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from .models import CourseGrade, CourseVisibility, GradeTab

# Only "All Courses" is virtual — it mirrors the dedicated button in the UI.
# Empty user groups ("Core", "Electives") are NOT seeded: the user creates the
# groups they actually want, and an empty group would just render as dead chrome.
DEFAULT_TABS: List[Dict[str, Any]] = [
    {
        "id": "all",
        "name": "All Courses",
        "color": "emerald",
        "course_keys": [],
        "match_rules": [],
        "order": 1,
        "builtin": True,
    },
]


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")


def _letter_points(letter: str, score: Optional[float]) -> Optional[float]:
    table = {
        "A+": 4.0, "A": 4.0, "A-": 3.7, "B+": 3.3, "B": 3.0, "B-": 2.7,
        "C+": 2.3, "C": 2.0, "C-": 1.7, "D+": 1.3, "D": 1.0, "D-": 0.7, "F": 0.0,
    }
    lg = (letter or "").strip().upper()
    if lg in table:
        return table[lg]
    if score is not None:
        if score >= 92.5:
            return 4.0
        if score >= 89.5:
            return 3.7
        if score >= 86.5:
            return 3.3
        if score >= 82.5:
            return 3.0
        if score >= 79.5:
            return 2.7
        if score >= 76.5:
            return 2.3
        if score >= 72.5:
            return 2.0
        if score >= 69.5:
            return 1.7
        if score >= 66.5:
            return 1.3
        if score >= 62.5:
            return 1.0
        if score >= 59.5:
            return 0.7
        return 0.0
    return None


class GradesControlService:
    # ------------------------------------------------------------- visibility
    def _row(self, session: Session, course_key: str, source_app: str = "") -> Optional[CourseVisibility]:
        ck = (course_key or "").strip()[:180]
        if not ck:
            return None
        stmt = select(CourseVisibility).where(CourseVisibility.course_key == ck)
        if source_app:
            stmt = stmt.where(CourseVisibility.source_app == source_app)
        return session.exec(stmt.order_by(CourseVisibility.id.desc())).first()

    def visibility_map(self, session: Session) -> Dict[str, Dict[str, Any]]:
        rows = list(session.exec(select(CourseVisibility)).all())
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            key = r.course_key
            out[key] = {
                "course_key": r.course_key,
                "source_app": r.source_app,
                "tracked": bool(r.tracked),
                "hidden": bool(r.hidden),
                "counts_toward_gpa": bool(r.counts_toward_gpa),
                "counts_toward_average": bool(r.counts_toward_average),
                "tab_id": r.tab_id,
                "pinned": bool(r.pinned),
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        return out

    def set_visibility(
        self,
        session: Session,
        *,
        course_key: str,
        source_app: str = "",
        tracked: Optional[bool] = None,
        hidden: Optional[bool] = None,
        counts_toward_gpa: Optional[bool] = None,
        counts_toward_average: Optional[bool] = None,
        tab_id: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> Dict[str, Any]:
        ck = (course_key or "").strip()[:180]
        if not ck:
            raise ValueError("course_key is required")
        row = self._row(session, ck, source_app)
        if row is None:
            row = CourseVisibility(course_key=ck, source_app=(source_app or "")[:120])
        if tracked is not None:
            row.tracked = bool(tracked)
            # Un-tracking implies hidden from the tracker but keeps history.
            if not tracked:
                row.hidden = True
        if hidden is not None:
            row.hidden = bool(hidden)
            if not hidden:
                row.tracked = True
        if counts_toward_gpa is not None:
            row.counts_toward_gpa = bool(counts_toward_gpa)
        if counts_toward_average is not None:
            row.counts_toward_average = bool(counts_toward_average)
        if tab_id is not None:
            row.tab_id = tab_id or None
        if pinned is not None:
            row.pinned = bool(pinned)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        return self.visibility_map(session)[row.course_key]

    def hide_course(self, session: Session, course: CourseGrade, *, also_delete_row: bool = False) -> Dict[str, Any]:
        """
        "Remove this course" from the user's point of view.

        Marks it untracked+hidden so the next scan cannot resurrect it. With
        `also_delete_row=True` the grade row is deleted too (history is lost) —
        used when the user really wants it gone rather than merely hidden.
        """
        ck = (course.course_key or "").strip()
        if not ck:
            return {"ok": False, "reason": "course_has_no_key"}
        res = self.set_visibility(
            session,
            course_key=ck,
            source_app=course.source_app or "",
            tracked=False,
            hidden=True,
            counts_toward_gpa=False,
            counts_toward_average=False,
        )
        if also_delete_row and course.id is not None:
            session.delete(course)
            session.commit()
        return {"ok": True, "hidden": res, "deleted_row": bool(also_delete_row)}

    def apply_to_courses(self, session: Session, courses: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Decorate serialized courses with visibility flags and drop hidden ones."""
        vis = self.visibility_map(session)
        out: List[Dict[str, Any]] = []
        for c in courses:
            key = str(c.get("course_key") or c.get("id") or "")
            v = vis.get(key) or {}
            c = {
                **c,
                "tracked": v.get("tracked", True),
                "counts_toward_gpa": v.get("counts_toward_gpa", True),
                "counts_toward_average": v.get("counts_toward_average", True),
                "pinned": v.get("pinned", False),
                "tab_id": v.get("tab_id"),
            }
            if v.get("hidden") or not v.get("tracked", True):
                continue
            out.append(c)
        return out

    def hidden_courses(self, session: Session, all_courses: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        vis = self.visibility_map(session)
        hidden = []
        for c in all_courses:
            v = vis.get(str(c.get("course_key") or ""))
            if v and (v.get("hidden") or not v.get("tracked", True)):
                hidden.append({**c, "visibility": v})
        return hidden

    # ------------------------------------------------------------------- tabs
    def list_tabs(self, session: Session) -> List[Dict[str, Any]]:
        rows = list(session.exec(select(GradeTab)).all())
        tabs: List[Dict[str, Any]] = []
        seen = set()
        for r in rows:
            try:
                keys = json.loads(r.course_keys_json or "[]")
            except Exception:
                keys = []
            try:
                rules = json.loads(r.match_rules_json or "[]")
            except Exception:
                rules = []
            tabs.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "color": r.color,
                    "course_keys": keys if isinstance(keys, list) else [],
                    "match_rules": rules if isinstance(rules, list) else [],
                    "order": r.order,
                    "show_in_briefing": bool(r.show_in_briefing),
                    "builtin": False,
                }
            )
            seen.add(r.id)
        for d in DEFAULT_TABS:
            if d["id"] not in seen:
                tabs.append(dict(d))
        tabs.sort(key=lambda t: (int(t.get("order") or 100), str(t.get("name") or "")))
        return tabs

    def upsert_tab(
        self,
        session: Session,
        *,
        tab_id: Optional[str],
        name: str,
        color: str = "emerald",
        course_keys: Optional[Sequence[str]] = None,
        match_rules: Optional[Sequence[Dict[str, Any]]] = None,
        order: int = 100,
        show_in_briefing: bool = True,
    ) -> Dict[str, Any]:
        tid = (tab_id or _slug(name) or f"tab-{int(datetime.now().timestamp())}")[:80]
        row = session.get(GradeTab, tid)
        if row is None:
            row = GradeTab(id=tid, name=name.strip()[:120])
        row.name = name.strip()[:120] or tid
        row.color = color
        row.course_keys_json = json.dumps(list(course_keys or []))
        row.match_rules_json = json.dumps(list(match_rules or []))
        row.order = int(order)
        row.show_in_briefing = bool(show_in_briefing)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        # Explicit membership is mirrored onto the course visibility rows so the
        # association survives re-scans.
        for ck in list(course_keys or []):
            try:
                self.set_visibility(session, course_key=ck, tab_id=tid)
            except Exception:
                continue
        return next((t for t in self.list_tabs(session) if t["id"] == tid), {"id": tid, "name": row.name})

    def delete_tab(self, session: Session, tab_id: str) -> bool:
        if tab_id in {t["id"] for t in DEFAULT_TABS}:
            raise ValueError("Built-in tabs cannot be deleted")
        row = session.get(GradeTab, tab_id)
        if not row:
            return False
        session.delete(row)
        for v in session.exec(select(CourseVisibility).where(CourseVisibility.tab_id == tab_id)).all():
            v.tab_id = None
            session.add(v)
        session.commit()
        return True

    # --------------------------------------------------------------- scoping
    @staticmethod
    def _tab_matches_course(tab: Dict[str, Any], course: Dict[str, Any]) -> bool:
        keys = {str(k) for k in (tab.get("course_keys") or [])}
        ck = str(course.get("course_key") or "")
        if ck and ck in keys:
            return True
        if course.get("tab_id") and course.get("tab_id") == tab.get("id"):
            return True
        for rule in tab.get("match_rules") or []:
            if not isinstance(rule, dict) or rule.get("exclude"):
                continue
            ok = True
            for field, expected in rule.items():
                if field in ("exclude", "regex"):
                    continue
                actual = str(course.get(field) or "").lower()
                if str(expected).lower() not in actual:
                    ok = False
                    break
            if rule.get("regex"):
                try:
                    if not re.search(str(rule["regex"]), str(course.get("course_name") or ""), re.IGNORECASE):
                        ok = False
                except re.error:
                    ok = False
            if ok and rule:
                return True
        # "All Courses" style tab (no explicit membership, no rules) shows everything.
        return not keys and not [r for r in (tab.get("match_rules") or []) if isinstance(r, dict) and not r.get("exclude")]

    def scoped_summaries(
        self,
        session: Session,
        courses: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        tabs = self.list_tabs(session)
        out: List[Dict[str, Any]] = []
        for tab in tabs:
            members = [c for c in courses if self._tab_matches_course(tab, c)]
            scores = [float(c["score_percent"]) for c in members if c.get("score_percent") is not None and c.get("counts_toward_average", True)]
            gpas = [
                _letter_points(str(c.get("letter_grade") or ""), c.get("score_percent"))
                for c in members
                if c.get("counts_toward_gpa", True)
            ]
            gpas = [g for g in gpas if g is not None]
            out.append(
                {
                    **{k: tab.get(k) for k in ("id", "name", "color", "order", "builtin", "show_in_briefing")},
                    "course_count": len(members),
                    "course_keys": [c.get("course_key") for c in members],
                    "average_percent": round(sum(scores) / len(scores), 2) if scores else None,
                    "unweighted_gpa": round(sum(gpas) / len(gpas), 2) if gpas else None,
                    "missing_total": sum(int(c.get("missing_count") or 0) for c in members),
                }
            )
        return out


grades_control_service = GradesControlService()
