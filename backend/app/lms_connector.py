"""
Generalized Academic & LMS Connector Engine for Node v10.0.

Supports:
  1. Schoology (`leftathome/schoology-go` session/iapi architecture + `.ics` calendar fallback):
     - Extracts `csrf_token`, `csrf_key`, and `user.uid` from `window.Drupal.settings.s_common`
     - Sends `X-CSRF-Token` & `X-CSRF-Key` headers on same-origin XHRs (`credentials: 'include'`)
     - Queries `/iapi/parent/info` (student vs parent `view_child` & `children`)
     - Queries `/iapi2/site-navigation/courses` for active courses & section NIDs
     - Queries `/iapi/parent/overdue_submissions/{uid}` + live `/home` `.upcoming-events` DOM
       (interleaved `h3.submissions-title` `OVERDUE`/`UPCOMING`, `.upcoming-event[data-start]` Unix epoch,
       `.event-title a[href^="/assignment/"]`, and `.readonly-title.event-subtitle` last-subtitle course name)
     - Probes `/grades/grades` for student gradebook scores when enabled by the district; gracefully
       degrades to course + assignment + overdue tracking when SIS-locked (`HTTP 403`).
  2. Canvas LMS (Native REST `/api/v1/*` Cookie Interception + DOM Fallback):
     - `GET /api/v1/courses?enrollment_state=active&include[]=total_scores&include[]=current_grading_period_scores&include[]=teachers&include[]=term&per_page=50`
     - `GET /api/v1/courses/{id}/assignments?include[]=submission&per_page=40`
     - `GET /api/v1/planner/items` & `GET /api/v1/users/self/todo`
     - Strips optional `while(1);` JSON prefix and falls back to `.ic-DashboardCard` / `#grades_summary` DOM.
  3. Google Classroom (Course cards + To-Do / Missing coursework).
  4. PowerSchool (handled by `grades_service.py` `table#tblgrades` + XTE `/ws/xte/assignment/lookup`,
     with missing/upcoming assignments bridged into Node `Tasks` here).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from .database import engine
from .models import Task, normalize_priority_value

logger = logging.getLogger("node.lms_connector")

LMS_KEYWORDS = (
    "powerschool",
    "schoology",
    "canvas",
    "instructure",
    "classroom.google",
    "google classroom",
    "infinitecampus",
    "infinite campus",
    "skyward",
    "gradescope",
    "aeries",
    "genesis",
    "synergy",
)


def is_lms_app(app_cfg_or_name: Any, url: str = "") -> bool:
    if isinstance(app_cfg_or_name, dict):
        if app_cfg_or_name.get("indicator_type") == "grades_tracker":
            return True
        hay = f"{app_cfg_or_name.get('name', '')} {app_cfg_or_name.get('url', '')} {app_cfg_or_name.get('category', '')}".lower()
    else:
        hay = f"{app_cfg_or_name or ''} {url or ''}".lower()
    return any(k in hay for k in LMS_KEYWORDS)


def detect_lms_kind(url: str = "", app_name: str = "") -> str:
    hay = f"{url or ''} {app_name or ''}".lower()
    if "schoology" in hay:
        return "schoology"
    if "instructure" in hay or "canvas" in hay:
        return "canvas"
    if "classroom.google" in hay or "google classroom" in hay:
        return "classroom"
    return "powerschool"


# ---------------------------------------------------------------------------
# 1. Schoology In-Page Session + `/iapi` + DOM Extractor (`schoology-go` spec)
# ---------------------------------------------------------------------------
SCHOOLOGY_EXTRACT_JS = r"""
async () => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const slugify = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 120);

  // 1. Extract Drupal.settings.s_common CSRF credentials & numeric UID (per leftathome/schoology-go)
  let sCommon = (window.Drupal && window.Drupal.settings && window.Drupal.settings.s_common) || null;
  if (!sCommon) {
    // Also inspect inline <script> JSON if Drupal.settings hasn't attached or was wrapped
    for (const sc of Array.from(document.querySelectorAll('script'))) {
      const txt = sc.textContent || '';
      if (txt.includes('"s_common"') && txt.includes('csrf_token')) {
        try {
          const m = txt.match(/jQuery\.extend\(Drupal\.settings,\s*(\{[\s\S]+?\})\s*\);/);
          if (m) {
            const parsed = JSON.parse(m[1]);
            if (parsed && parsed.s_common) { sCommon = parsed.s_common; break; }
          }
        } catch (_) {}
      }
    }
  }
  sCommon = sCommon || {};
  const sUser = sCommon.user || {};
  const csrfToken = String(sUser.csrf_token || sCommon.csrf_token || '');
  const csrfKey = String(sUser.csrf_key || sCommon.csrf_key || '');
  const selfUid = sUser.uid ? String(sUser.uid) : (sCommon.uid ? String(sCommon.uid) : '');

  const iapiHeaders = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
  };
  if (csrfToken) iapiHeaders['X-CSRF-Token'] = csrfToken;
  if (csrfKey) iapiHeaders['X-CSRF-Key'] = csrfKey;

  const safeJsonFetch = async (path) => {
    try {
      const u = new URL(path, location.origin).href;
      const r = await fetch(u, { credentials: 'include', headers: iapiHeaders });
      if (!r.ok) return { ok: false, status: r.status, data: null };
      const ct = (r.headers.get('content-type') || '').toLowerCase();
      if (!ct.includes('json') && !ct.includes('javascript')) {
        const txt = await r.text();
        try { return { ok: true, status: r.status, data: JSON.parse(txt) }; } catch (_) { return { ok: false, status: r.status, data: null }; }
      }
      return { ok: true, status: r.status, data: await r.json() };
    } catch (_) {
      return { ok: false, status: 0, data: null };
    }
  };

  // 2. Parent / Student info (`/iapi/parent/info` -> `body.session.view_child`, `body.children`)
  let targetUids = [];
  if (selfUid) targetUids.push(selfUid);
  const parentInfo = await safeJsonFetch('/iapi/parent/info');
  if (parentInfo.ok && parentInfo.data && parentInfo.data.body) {
    const body = parentInfo.data.body;
    const vc = body.session && body.session.view_child ? String(body.session.view_child) : '';
    if (vc && !targetUids.includes(vc)) targetUids.unshift(vc);
    const children = body.children || {};
    const childList = Array.isArray(children) ? children : Object.values(children);
    for (const ch of childList) {
      const cuid = ch && (ch.uid || ch.id) ? String(ch.uid || ch.id) : '';
      if (cuid && !targetUids.includes(cuid)) targetUids.push(cuid);
    }
  }

  // 3. Courses discovery (`/iapi2/site-navigation/courses`)
  const coursesMap = new Map();
  const navRes = await safeJsonFetch('/iapi2/site-navigation/courses');
  if (navRes.ok && navRes.data) {
    const rawCourses = (navRes.data.data && navRes.data.data.courses) || navRes.data.courses || [];
    for (const c of rawCourses) {
      const cTitle = clean(c.courseTitle || c.title || '');
      const sTitle = clean(c.sectionTitle || '');
      const nid = String(c.nid || c.sectionNid || c.id || '');
      if (!cTitle) continue;
      const fullName = sTitle && !cTitle.toLowerCase().includes(sTitle.toLowerCase()) ? `${cTitle} (${sTitle})` : cTitle;
      const key = slugify(cTitle) || `schoology-${nid}`;
      coursesMap.set(key, {
        course_key: key,
        course_name: fullName.slice(0, 160),
        base_title: cTitle,
        section_nid: nid,
        teacher: clean(c.instructor || '') || null,
        period: sTitle || null,
        term: 'Current',
        letter_grade: '',
        score_percent: null,
        scores_url: nid ? new URL(`/course/${nid}/student_grades`, location.origin).href : location.href,
        term_grades: {},
        missing_count: 0,
        assignments: [],
      });
    }
  }

  // 4. Parse `.upcoming-events` HTML fragment (exact `leftathome/schoology-go/assignments.go` logic!)
  const parseSubmissionsContainer = (rootNode) => {
    const found = [];
    if (!rootNode) return found;
    const containers = rootNode.classList && rootNode.classList.contains('upcoming-events')
      ? [rootNode]
      : Array.from(rootNode.querySelectorAll('.upcoming-events, .overdue-submissions, #right-column'));

    for (const container of containers) {
      let currentBucket = 'upcoming';
      const walk = (parent) => {
        for (const el of Array.from(parent.children || [])) {
          const tag = (el.tagName || '').toUpperCase();
          const cls = el.className || '';
          if ((tag === 'H3' || tag === 'H4' || cls.includes('submissions-title')) && /overdue|upcoming/i.test(el.textContent || '')) {
            const htxt = clean(el.textContent).toUpperCase();
            if (htxt.includes('OVERDUE')) currentBucket = 'overdue';
            else if (htxt.includes('UPCOMING')) currentBucket = 'upcoming';
            continue;
          }
          if (el.classList && el.classList.contains('upcoming-event')) {
            const linkEl = el.querySelector('.event-title a[href], a[href*="/assignment/"], a[href*="/event/"], a[href*="/assessment/"]');
            if (!linkEl) continue;
            // Remove visually-hidden spans inside link so title stays clean
            const cloneLink = linkEl.cloneNode(true);
            for (const vh of Array.from(cloneLink.querySelectorAll('.visually-hidden, .sr-only'))) vh.remove();
            const title = clean(cloneLink.textContent);
            if (!title) continue;
            const rawHref = linkEl.getAttribute('href') || '';
            const fullHref = rawHref ? new URL(rawHref, location.origin).href : '';
            const idMatch = rawHref.match(/\/(?:assignment|assessment|event)\/(\d+)/);
            const itemId = idMatch ? idMatch[1] : slugify(title);

            // `data-start` is Unix epoch seconds in Schoology `.upcoming-event`
            const dataStart = el.getAttribute('data-start') || '';
            let dueIso = '';
            if (/^\d{9,12}$/.test(dataStart)) {
              const ms = parseInt(dataStart, 10) * 1000;
              const d = new Date(ms);
              if (!isNaN(d.getTime())) dueIso = d.toISOString().slice(0, 10);
            }

            // `.readonly-title.event-subtitle` -> last subtitle is Course Name; first may be "N days overdue"
            const subs = Array.from(el.querySelectorAll('.readonly-title.event-subtitle, .event-subtitle'))
              .map((s) => clean(s.textContent))
              .filter(Boolean);
            let courseLabel = subs.length > 0 ? subs[subs.length - 1] : '';
            courseLabel = courseLabel.replace(/\s*:\s*Section.*$/i, '').trim();
            const overdueNote = subs.find((s) => /overdue/i.test(s)) || '';
            const isOverdue = currentBucket === 'overdue' || Boolean(overdueNote);

            found.push({
              assignment_id: `sch-${itemId}`,
              raw_id: idMatch ? idMatch[1] : '',
              title: title.slice(0, 180),
              course_hint: courseLabel,
              due_date: dueIso,
              url: fullHref,
              status: isOverdue ? 'overdue' : 'upcoming',
              overdue_note: overdueNote,
              is_missing: isOverdue,
            });
            continue;
          }
          if (el.children && el.children.length > 0) walk(el);
        }
      };
      walk(container);
    }
    return found;
  };

  const allSubmissions = [];
  const seenSubIds = new Set();
  const addSubs = (arr) => {
    for (const s of arr) {
      const k = `${s.assignment_id}::${s.title.toLowerCase()}`;
      if (seenSubIds.has(k)) continue;
      seenSubIds.add(k);
      allSubmissions.push(s);
    }
  };

  // 4a. Query `/iapi/parent/overdue_submissions/{uid}` for every discovered UID
  for (const uid of targetUids.slice(0, 3)) {
    const subRes = await safeJsonFetch(`/iapi/parent/overdue_submissions/${uid}`);
    if (subRes.ok && subRes.data && subRes.data.body && subRes.data.body.html) {
      const frag = new DOMParser().parseFromString(`<div class="upcoming-events">${subRes.data.body.html}</div>`, 'text/html');
      addSubs(parseSubmissionsContainer(frag.body));
    }
  }
  // 4b. Also parse live DOM on `/home` or current page
  addSubs(parseSubmissionsContainer(document));

  // 5. Probe `/grades/grades` (works on student accounts / districts where Schoology gradebook is enabled;
  //    returns 403 on SIS-locked parent tenants — handled gracefully!)
  let gradebookStatus = 'unavailable';
  try {
    const gUrls = location.pathname.includes('/grades')
      ? [location.href]
      : ['/grades/grades', '/parent/grades_attendance/grades'];
    for (const gp of gUrls) {
      const gr = await fetch(new URL(gp, location.origin).href, { credentials: 'include' });
      if (!gr.ok) {
        if (gr.status === 403) gradebookStatus = 'sis_locked_403';
        continue;
      }
      const gHtml = await gr.text();
      const gDoc = new DOMParser().parseFromString(gHtml, 'text/html');
      const courseBlocks = Array.from(gDoc.querySelectorAll('.gradebook-course, .gradebook-course-wrapper'));
      if (courseBlocks.length > 0) {
        gradebookStatus = 'ok';
        for (const blk of courseBlocks) {
          const titleNode = blk.querySelector('.gradebook-course-title, h3, a[href*="/course/"]');
          if (!titleNode) continue;
          const courseLink = blk.querySelector('a[href*="/course/"]');
          const nidMatch = courseLink ? (courseLink.getAttribute('href') || '').match(/\/course\/(\d+)/) : null;
          const gbNid = nidMatch ? nidMatch[1] : '';
          const rawCName = clean(titleNode.textContent).replace(/\s*:\s*Sec(?:tion)?.*$/i, '').replace(/\s*\(Sec(?:tion)?[^)]*\)\s*$/i, '');
          if (!rawCName) continue;
          const cKey = slugify(rawCName);
          const gradeNode = blk.querySelector('.course-grade-value, .awarded-grade, .grade-column');
          const gTxt = gradeNode ? clean(gradeNode.textContent) : clean(blk.textContent);
          let pct = null;
          let letter = '';
          const pm = gTxt.match(/(\d{1,3}(?:\.\d+)?)\s*%/);
          if (pm) pct = Math.round(parseFloat(pm[1]) * 100) / 100;
          const lm = gTxt.match(/\b([A-DF][+-]?)\b/);
          if (lm) letter = lm[1];

          let entry = coursesMap.get(cKey);
          if (!entry && gbNid) {
            for (const cand of coursesMap.values()) {
              if (cand.section_nid && cand.section_nid === gbNid) {
                entry = cand;
                break;
              }
            }
          }
          if (!entry) {
            entry = {
              course_key: cKey,
              course_name: rawCName.slice(0, 160),
              base_title: rawCName,
              section_nid: '',
              teacher: null,
              period: null,
              term: 'Current',
              letter_grade: letter,
              score_percent: pct,
              scores_url: new URL(gp, location.origin).href,
              term_grades: {},
              missing_count: 0,
              assignments: [],
            };
            coursesMap.set(cKey, entry);
          } else {
            if (pct !== null) entry.score_percent = pct;
            if (letter) entry.letter_grade = letter;
          }

          // Extract graded item rows inside `.gradebook-course-grades`
          for (const tr of Array.from(blk.querySelectorAll('tr.item-row, tr.report-row'))) {
            const tEl = tr.querySelector('.title, a[href*="/assignment/"], td:first-child');
            if (!tEl) continue;
            const aTitle = clean(tEl.textContent);
            if (!aTitle || /^(category|total|overall|course grade)/i.test(aTitle)) continue;
            const ptsTxt = clean((tr.querySelector('.grade-column, .awarded-grade, .points') || {}).textContent || '');
            let sp = null, mp = null;
            const sm = ptsTxt.match(/(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)/);
            if (sm) { sp = parseFloat(sm[1]); mp = parseFloat(sm[2]); }
            const isMiss = /missing|incomplete/i.test(tr.innerHTML || '');
            entry.assignments.push({
              assignment_id: `sch-gb-${slugify(aTitle).slice(0, 40)}`,
              title: aTitle.slice(0, 180),
              category: 'Assignment',
              due_date: clean((tr.querySelector('.due-date') || {}).textContent || '').slice(0, 32),
              score_points: sp,
              max_points: mp,
              score_display: ptsTxt || (isMiss ? 'Missing' : '—'),
              score_percent: sp !== null && mp && mp > 0 ? Math.round((sp / mp) * 1000) / 10 : null,
              letter_grade: '',
              flags: isMiss ? ['Missing'] : [],
              is_missing: isMiss,
              is_late: /late/i.test(tr.innerHTML || ''),
            });
          }
        }
        break;
      }
    }
  } catch (_) {}

  // 6. Attach overdue & upcoming submissions onto matching courses (and synthesize course if needed)
  for (const sub of allSubmissions) {
    let matched = null;
    const hintLow = (sub.course_hint || '').toLowerCase();
    if (hintLow) {
      for (const c of coursesMap.values()) {
        const cl = c.course_name.toLowerCase();
        const bl = (c.base_title || '').toLowerCase();
        if (cl.includes(hintLow) || hintLow.includes(bl) || slugify(hintLow) === c.course_key) {
          matched = c;
          break;
        }
      }
      if (!matched) {
        const nk = slugify(sub.course_hint);
        if (nk) {
          matched = {
            course_key: nk,
            course_name: sub.course_hint.slice(0, 160),
            base_title: sub.course_hint,
            section_nid: '',
            teacher: null,
            period: null,
            term: 'Current',
            letter_grade: '',
            score_percent: null,
            scores_url: sub.url || location.href,
            term_grades: {},
            missing_count: 0,
            assignments: [],
          };
          coursesMap.set(nk, matched);
        }
      }
    }
    if (matched) {
      if (sub.is_missing) matched.missing_count = (matched.missing_count || 0) + 1;
      if (!matched.assignments.some((a) => a.title.toLowerCase() === sub.title.toLowerCase())) {
        matched.assignments.push({
          assignment_id: sub.assignment_id,
          title: sub.title,
          category: sub.status === 'overdue' ? 'Overdue' : 'Upcoming',
          due_date: sub.due_date || '',
          score_points: null,
          max_points: null,
          score_display: sub.status === 'overdue' ? (sub.overdue_note || 'Overdue') : 'Upcoming',
          score_percent: null,
          letter_grade: '',
          flags: sub.status === 'overdue' ? ['Missing', 'Overdue'] : ['Upcoming'],
          is_missing: Boolean(sub.is_missing),
          is_late: Boolean(sub.is_missing),
          url: sub.url || '',
        });
      }
    }
  }

  return {
    lms: 'schoology',
    url: location.href,
    has_csrf: Boolean(csrfToken && csrfKey),
    gradebook_status: gradebookStatus,
    courses: Array.from(coursesMap.values()),
    submissions: allSubmissions,
  };
}
"""


# ---------------------------------------------------------------------------
# 2. Canvas LMS Same-Origin `/api/v1/*` + DOM Interceptor
# ---------------------------------------------------------------------------
CANVAS_EXTRACT_JS = r"""
async () => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const slugify = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 120);

  const canvasApiGet = async (path) => {
    try {
      const u = new URL(path, location.origin).href;
      const r = await fetch(u, {
        credentials: 'include',
        headers: { 'Accept': 'application/json+canvas-string-ids, application/json' },
      });
      if (!r.ok) return null;
      let txt = await r.text();
      // Canvas prefixes some JSON responses with `while(1);`
      txt = txt.replace(/^while\s*\(1\);\s*/i, '');
      return JSON.parse(txt);
    } catch (_) {
      return null;
    }
  };

  const coursesOut = [];
  const tasksOut = [];

  // 1. Fetch active Canvas courses + computed scores + teachers + term
  const rawCourses = await canvasApiGet(
    '/api/v1/courses?enrollment_state=active&include[]=total_scores&include[]=current_grading_period_scores&include[]=teachers&include[]=term&per_page=50'
  );

  if (Array.isArray(rawCourses) && rawCourses.length > 0) {
    const validCourses = rawCourses.filter((c) => c && c.name && !c.access_restricted_by_date);

    // Fetch per-course assignments concurrently in batches of 4
    const fetchCourseAssignments = async (course) => {
      const cid = course.id;
      const rawAssigns = await canvasApiGet(
        `/api/v1/courses/${cid}/assignments?include[]=submission&order_by=due_at&per_page=40`
      );
      if (!Array.isArray(rawAssigns)) return [];
      const mapped = [];
      for (const a of rawAssigns) {
        if (!a || !a.name) continue;
        const sub = a.submission || {};
        const ptsPossible = typeof a.points_possible === 'number' ? a.points_possible : null;
        const score = typeof sub.score === 'number' ? Math.round(sub.score * 100) / 100 : null;
        const isMissing = Boolean(sub.missing);
        const isLate = Boolean(sub.late);
        const isExcused = Boolean(sub.excused);
        const pct = score !== null && ptsPossible && ptsPossible > 0
          ? Math.round((score / ptsPossible) * 1000) / 10
          : null;
        const dueIso = a.due_at ? String(a.due_at).slice(0, 10) : '';
        const flags = [];
        if (isMissing) flags.push('Missing');
        if (isLate) flags.push('Late');
        if (isExcused) flags.push('Exempt');

        let disp = '—';
        if (isExcused) disp = 'Excused';
        else if (score !== null && ptsPossible !== null) disp = `${score}/${ptsPossible}`;
        else if (sub.grade) disp = String(sub.grade);
        else if (isMissing) disp = 'Missing';

        mapped.push({
          assignment_id: `canvas-${a.id || slugify(a.name)}`,
          title: clean(a.name).slice(0, 180),
          category: 'Assignment',
          due_date: dueIso,
          score_points: score,
          max_points: ptsPossible,
          score_display: disp,
          score_percent: pct,
          letter_grade: sub.grade && /^[A-DF][+-]?$/i.test(String(sub.grade)) ? String(sub.grade).toUpperCase() : '',
          flags,
          is_missing: isMissing,
          is_late: isLate,
          is_exempt: isExcused,
          url: a.html_url || '',
        });

        // Queue unsubmitted upcoming or missing items for Node Tasks sync!
        const unsubmitted = !sub.workflow_state || sub.workflow_state === 'unsubmitted';
        if ((isMissing || (unsubmitted && dueIso)) && !isExcused) {
          tasksOut.push({
            title: clean(a.name),
            course_name: clean(course.name),
            due_date: dueIso,
            status: isMissing ? 'overdue' : 'upcoming',
            url: a.html_url || '',
          });
        }
      }
      return mapped;
    };

    for (let i = 0; i < validCourses.length; i += 4) {
      const batch = validCourses.slice(i, i + 4);
      const assignsBatch = await Promise.all(batch.map((c) => fetchCourseAssignments(c)));
      for (let j = 0; j < batch.length; j++) {
        const c = batch[j];
        const assigns = assignsBatch[j] || [];
        const enr = (Array.isArray(c.enrollments) ? c.enrollments.find((e) => e.type === 'student' || e.role === 'StudentEnrollment') || c.enrollments[0] : null) || {};
        const rawScore = enr.current_period_computed_current_score ?? enr.computed_current_score ?? null;
        const rawGrade = enr.current_period_computed_current_grade ?? enr.computed_current_grade ?? '';
        const teacherName = Array.isArray(c.teachers) && c.teachers[0] ? clean(c.teachers[0].display_name || '') : null;
        const termName = c.term && c.term.name ? clean(c.term.name) : 'Current';
        const missCount = assigns.filter((x) => x.is_missing).length;

        coursesOut.push({
          course_key: slugify(`${c.course_code || c.name}-${c.id}`),
          course_name: clean(c.name).slice(0, 160),
          teacher: teacherName,
          period: c.course_code ? clean(c.course_code).slice(0, 40) : null,
          term: termName,
          letter_grade: rawGrade ? String(rawGrade).trim() : '',
          score_percent: typeof rawScore === 'number' ? Math.round(rawScore * 100) / 100 : null,
          scores_url: new URL(`/courses/${c.id}/grades`, location.origin).href,
          term_grades: {},
          missing_count: missCount,
          assignments: assigns,
        });
      }
    }
  }

  // 2. Also query Canvas Planner (`/api/v1/planner/items`) for upcoming quizzes/discussions/to-dos
  const todayIso = new Date().toISOString().slice(0, 10);
  const plannerItems = await canvasApiGet(`/api/v1/planner/items?start_date=${todayIso}&per_page=50`);
  if (Array.isArray(plannerItems)) {
    for (const pi of plannerItems) {
      if (!pi || !pi.plannable) continue;
      if (pi.planner_override && pi.planner_override.marked_complete) continue;
      if (pi.submissions && pi.submissions.submitted) continue;
      const pTitle = clean(pi.plannable.title || pi.plannable.name || '');
      if (!pTitle) continue;
      const pDue = (pi.plannable_date || pi.plannable.due_at || '').slice(0, 10);
      tasksOut.push({
        title: pTitle,
        course_name: clean(pi.context_name || ''),
        due_date: pDue,
        status: pi.submissions && pi.submissions.missing ? 'overdue' : 'upcoming',
        url: pi.html_url ? new URL(pi.html_url, location.origin).href : '',
      });
    }
  }

  // 3. DOM Fallback if Canvas REST API was disabled by institution
  if (coursesOut.length === 0) {
    for (const card of Array.from(document.querySelectorAll('.ic-DashboardCard'))) {
      const tEl = card.querySelector('.ic-DashboardCard__header-subtitle, .ic-DashboardCard__header_hero, h2, h3');
      if (!tEl) continue;
      const cname = clean(tEl.textContent);
      if (!cname) continue;
      const gEl = card.querySelector('.ic-DashboardCard__header-grade, [class*="grade" i]');
      const gTxt = gEl ? clean(gEl.textContent) : '';
      const pm = gTxt.match(/(\d{1,3}(?:\.\d+)?)\s*%/);
      const lm = gTxt.match(/\b([A-DF][+-]?)\b/);
      const aEl = card.querySelector('a.ic-DashboardCard__link, a[href*="/courses/"]');
      coursesOut.push({
        course_key: slugify(cname),
        course_name: cname.slice(0, 160),
        teacher: null,
        period: null,
        term: 'Current',
        letter_grade: lm ? lm[1] : '',
        score_percent: pm ? parseFloat(pm[1]) : null,
        scores_url: aEl ? new URL(aEl.getAttribute('href'), location.origin).href : location.href,
        term_grades: {},
        missing_count: 0,
        assignments: [],
      });
    }
  }

  return {
    lms: 'canvas',
    url: location.href,
    courses: coursesOut,
    submissions: tasksOut,
  };
}
"""


# ---------------------------------------------------------------------------
# 3. Google Classroom Course & To-Do DOM Extractor
# ---------------------------------------------------------------------------
CLASSROOM_EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const slugify = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 120);
  const courses = [];
  const submissions = [];

  for (const card of Array.from(document.querySelectorAll('li[data-course-id], div[data-course-id], .gHz6xd'))) {
    const titleEl = card.querySelector('h2, .YVvGBb, a[href*="/c/"]');
    if (!titleEl) continue;
    const cname = clean(titleEl.textContent);
    if (!cname) continue;
    const gradeEl = card.querySelector('[aria-label*="overall grade" i], .z3vRcc');
    const gTxt = gradeEl ? clean(gradeEl.textContent) : '';
    const pm = gTxt.match(/(\d{1,3}(?:\.\d+)?)\s*%/);
    courses.push({
      course_key: slugify(cname),
      course_name: cname.slice(0, 160),
      teacher: null,
      period: null,
      term: 'Current',
      letter_grade: '',
      score_percent: pm ? parseFloat(pm[1]) : null,
      scores_url: location.href,
      term_grades: {},
      missing_count: 0,
      assignments: [],
    });
  }

  for (const row of Array.from(document.querySelectorAll('a[href*="/a/"], a[href*="/tc/"]'))) {
    const txt = clean(row.textContent);
    if (txt && txt.length >= 4 && txt.length <= 200) {
      submissions.push({
        title: txt.slice(0, 160),
        course_name: 'Google Classroom',
        due_date: '',
        status: /missing/i.test(txt) ? 'overdue' : 'upcoming',
        url: row.href || '',
      });
    }
  }
  return { lms: 'classroom', url: location.href, courses, submissions };
}
"""


def _normalize_task_dedup_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def sync_lms_assignments_to_tasks(
    submissions: List[Dict[str, Any]],
    source_app: str = "LMS",
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Bridge overdue & upcoming unsubmitted assignments scraped from Schoology (`/iapi`),
    Canvas (`/api/v1/planner` & `/assignments`), Google Classroom, or PowerSchool straight
    into Node's `Task` queue without creating duplicates.
    """
    if not submissions:
        return {"created": 0, "updated": 0}

    own_session = session is None
    db = session or Session(engine)
    created_cnt = 0
    updated_cnt = 0

    try:
        existing_tasks = list(db.exec(select(Task)).all())
        by_key: Dict[str, Task] = {}
        for t in existing_tasks:
            by_key[_normalize_task_dedup_key(t.title)] = t

        today_iso = datetime.now(timezone.utc).date().isoformat()
        for item in submissions[:60]:
            raw_title = str(item.get("title") or "").strip()
            if not raw_title or len(raw_title) < 2:
                continue
            course_hint = str(item.get("course_hint") or item.get("course_name") or "").strip()
            full_title = (
                f"[{course_hint}] {raw_title}"
                if course_hint and course_hint.lower() not in raw_title.lower()
                else raw_title
            )[:220]

            dedup_full = _normalize_task_dedup_key(full_title)
            dedup_bare = _normalize_task_dedup_key(raw_title)
            due_str = str(item.get("due_date") or "").strip()[:10] or None
            is_overdue = item.get("status") == "overdue" or bool(item.get("is_missing"))

            prio = "critical" if is_overdue else ("high" if due_str and due_str <= today_iso else "normal")
            prio = normalize_priority_value(prio)

            matched_task = by_key.get(dedup_full) or by_key.get(dedup_bare)
            if matched_task:
                changed = False
                if due_str and not matched_task.due_date:
                    matched_task.due_date = due_str
                    changed = True
                if is_overdue and matched_task.priority in ("normal", "low") and not matched_task.completed:
                    matched_task.priority = "critical"
                    changed = True
                if changed:
                    db.add(matched_task)
                    updated_cnt += 1
                continue

            new_t = Task(
                title=full_title,
                completed=False,
                category=source_app[:40] or "Coursework",
                priority=prio,
                estimated_minutes=35 if any(k in raw_title.lower() for k in ("essay", "project", "lab", "exam", "test")) else 25,
                due_date=due_str,
            )
            db.add(new_t)
            by_key[dedup_full] = new_t
            created_cnt += 1

        if created_cnt or updated_cnt:
            db.commit()
            if created_cnt > 0:
                from .agent_logbook import agent_logbook

                agent_logbook.auto_record_event(
                    db,
                    agent_name="lms-connector",
                    model_id="system",
                    category="lms-sync",
                    title=f"Synced {created_cnt} assignment(s) from {source_app}",
                    content=f"Imported {created_cnt} new assignment task(s) and updated {updated_cnt} item(s) from {source_app}.",
                    tags=f"lms,{source_app.lower()}",
                )
    except Exception as exc:
        logger.debug("LMS task sync warning: %s", exc)
    finally:
        if own_session:
            db.close()

    return {"created": created_cnt, "updated": updated_cnt}


async def extract_lms_courses_and_tasks(
    page: Any,
    source_app: str = "LMS",
) -> Optional[List[Dict[str, Any]]]:
    """
    Dispatch Schoology, Canvas LMS, and Google Classroom pages to their native session extractors.
    Returns `None` for PowerSchool / generic SIS pages so `grades_service` runs its native XTE/table pipeline.
    """
    page_url = str(getattr(page, "url", "") or "")
    kind = detect_lms_kind(page_url, source_app)
    if kind == "powerschool":
        return None

    script = (
        SCHOOLOGY_EXTRACT_JS
        if kind == "schoology"
        else (CANVAS_EXTRACT_JS if kind == "canvas" else CLASSROOM_EXTRACT_JS)
    )
    try:
        payload = await page.evaluate(script)
    except Exception as exc:
        logger.debug("LMS extractor (%s) evaluate error: %s", kind, exc)
        return []

    if not isinstance(payload, dict):
        return []

    submissions = payload.get("submissions") or []
    if submissions:
        sync_lms_assignments_to_tasks(submissions, source_app=source_app)

    courses = payload.get("courses") or []
    return courses if isinstance(courses, list) else []
