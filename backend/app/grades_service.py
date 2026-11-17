"""PowerSchool & SIS Grades Tracker Service for Node WebOS.

Features:
- Multi-course header-aware DOM extraction across PowerSchool (`/guardian/home.html` `table#tblgrades`,
  `table.linkDescList`, classic & responsive 21.x-25.x layouts, plus automatic `/guardian/home.html`
  recovery when the browser tab is sitting on a single class's `scores.html` detail page) and generic SIS portals.
- Automated per-course individual assignment harvesting via PowerSchool's `scores.html` ->
  `POST /ws/xte/assignment/lookup` (with `Referer` & `'001'`-stripped `studentFRN`), static HTML
  `#scoreTable` parsing, and a non-disruptive background-page fallback for JS-hydrated portals.
- Persistent SQLite course memory (`CourseGrade`), chronological score trajectory (`history_json`),
  multi-term snapshots (`term_grades_json`), diff detection (`GradeChangeLog`), and Important Inbox alerts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from sqlmodel import Session, select

from .models import CourseGrade, GradeChangeLog, Notification, utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Multi-Course PowerSchool + Generic SIS Overview Extractor (runs in-page)
# ---------------------------------------------------------------------------
EXTRACT_GRADES_DOM_JS = r"""
async () => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();

  const VALID_LETTERS = new Set([
    'A+', 'A', 'A-',
    'B+', 'B', 'B-',
    'C+', 'C', 'C-',
    'D+', 'D', 'D-',
    'E', 'F',
    'P', 'NP', 'S', 'U', 'I', 'O', 'M', 'W', 'EX'
  ]);

  const TERM_HEADER_RE = /^(Q[1-4]|S[1-2]|T[1-4]|Y[1-2]|MP[1-6]|PR?[1-4]|M[1-4]|F[1-2]|E[1-2]|X[1-2]|C[1-2]|FINAL(\s*GRADE)?|CURRENT(\s*GRADE)?|GRADE|OVERALL|AVERAGE|MARK|TERM\s*\d+|SEM(ESTER)?\s*\d+|QTR\s*\d+)$/i;
  const IGNORE_COL_RE = /^(EXP(RESSION)?|PER(IOD)?|BLK|BLOCK|M|T|W|H|R|F|S|U|MON|TUE|WED|THU|FRI|LAST\s+WEEK|THIS\s+WEEK|ATTENDANCE|ABS(ENCES?)?|TARD(Y|IES)|COMMENTS?|CIT(IZENSHIP)?|CREDITS?|ROOM|RM)$/i;
  const COURSE_COL_RE = /^(COURSE(\s*NAME|\s*TITLE)?|CLASS(\s*NAME)?|SUBJECT|SECTION|DESCRIPTION)$/i;
  const TEACHER_COL_RE = /^(TEACHER|INSTRUCTOR|FACULTY|STAFF)$/i;
  const EXP_COL_RE = /^(EXP(RESSION)?|PER(IOD)?|BLOCK|BLK|HR)$/i;
  const ABS_COL_RE = /^ABS(ENCES?)?$/i;

  const resolveHref = (rawHref, baseUrl) => {
    if (!rawHref) return '';
    const trimmed = rawHref.trim();
    if (!trimmed || /^javascript:/i.test(trimmed) || trimmed === '#') return '';
    try {
      return new URL(trimmed, baseUrl || location.href).href;
    } catch (_) {
      return trimmed;
    }
  };

  // Build 2D header map accounting for rowspan + colspan (critical for PowerSchool #tblgrades!)
  const buildHeaderMap = (table) => {
    const allRows = Array.from(table.querySelectorAll('tr')).filter(
      (tr) => tr.closest('table') === table && !tr.querySelector('table')
    );
    const headerRows = [];
    for (const tr of allRows) {
      const ths = Array.from(tr.children).filter((el) => el.tagName === 'TH');
      const tds = Array.from(tr.children).filter((el) => el.tagName === 'TD');
      if (ths.length > 0 && tds.length === 0) {
        headerRows.push(tr);
      } else if (headerRows.length === 0 && ths.length > 0) {
        headerRows.push(tr);
      } else if (headerRows.length > 0) {
        break;
      }
    }
    if (headerRows.length === 0) {
      // Check if row 0 is a pseudo-header made of <td class="bold"> or similar
      if (allRows.length >= 2) {
        const firstCells = Array.from(allRows[0].children).filter((el) => el.tagName === 'TD' || el.tagName === 'TH');
        const texts = firstCells.map((c) => clean(c.innerText || c.textContent));
        if (texts.some((t) => COURSE_COL_RE.test(t)) && texts.some((t) => TERM_HEADER_RE.test(t))) {
          headerRows.push(allRows[0]);
        }
      }
    }

    const grid = [];
    for (let r = 0; r < headerRows.length; r++) {
      if (!grid[r]) grid[r] = [];
      let cIdx = 0;
      const cells = Array.from(headerRows[r].children).filter(
        (el) => el.tagName === 'TH' || el.tagName === 'TD'
      );
      for (const cell of cells) {
        while (grid[r][cIdx] !== undefined) cIdx++;
        const label = clean(cell.innerText || cell.textContent);
        const rs = Math.max(1, parseInt(cell.getAttribute('rowspan') || '1', 10) || 1);
        const cs = Math.max(1, parseInt(cell.getAttribute('colspan') || '1', 10) || 1);
        for (let dr = 0; dr < rs; dr++) {
          if (!grid[r + dr]) grid[r + dr] = [];
          for (let dc = 0; dc < cs; dc++) {
            // Prefer the most specific leaf header label if non-empty
            if (!grid[r + dr][cIdx + dc] || dr === 0) {
              grid[r + dr][cIdx + dc] = label;
            }
          }
        }
        cIdx += cs;
      }
    }

    // Collapse grid columns: leaf row wins over group header (e.g., "Last Week" -> "M")
    let maxCols = 0;
    for (const row of grid) {
      if (row && row.length > maxCols) maxCols = row.length;
    }
    const colLabels = [];
    for (let c = 0; c < maxCols; c++) {
      let chosen = '';
      for (let r = grid.length - 1; r >= 0; r--) {
        if (grid[r] && grid[r][c]) {
          chosen = grid[r][c];
          break;
        }
      }
      colLabels.push(chosen);
    }
    return { colLabels, headerRowSet: new Set(headerRows), allRows };
  };

  const normalizeTeacherName = (raw) => {
    let t = clean(raw || '')
      .replace(/^email\s+/i, '')
      .replace(/^teacher:\s*/i, '')
      .replace(/\s*-\s*rm:.*$/i, '')
      .replace(/\s*\brm:\s*\S+.*$/i, '')
      .trim();
    if (!t || /^details\s+about/i.test(t)) return '';
    // Convert "Last, First" -> "First Last" when unambiguous
    const commaMatch = t.match(/^([A-Za-z' -]{2,30}),\s*([A-Za-z.' -]{2,30})$/);
    if (commaMatch) {
      return `${commaMatch[2].trim()} ${commaMatch[1].trim()}`;
    }
    return t.slice(0, 100);
  };

  const extractCourseAndTeacher = (cell) => {
    if (!cell) return null;
    const clone = cell.cloneNode(true);
    // Remove screen-reader-only duplicate labels ("Details about ...")
    clone.querySelectorAll('.screen_readers_only, .sr-only, .visually-hidden, script, style').forEach((n) => n.remove());

    let teacher = '';
    const mailLink = clone.querySelector('a[href^="mailto:"], a[href*="teacherinfo.html"]');
    if (mailLink) {
      const mt = clean(mailLink.innerText || mailLink.textContent);
      if (mt && !/^details\s+about/i.test(mt)) {
        teacher = normalizeTeacherName(mt);
      }
    }
    clone.querySelectorAll('a[href*="teacherinfo.html"], a[href^="mailto:"]').forEach((a) => {
      const txt = clean(a.innerText || a.textContent);
      if (/^(details\s+about|email\s+)/i.test(txt)) {
        if (!teacher && /^email\s+/i.test(txt)) teacher = normalizeTeacherName(txt);
        a.remove();
      }
    });

    // Replace <br> with newline before reading text
    clone.querySelectorAll('br').forEach((br) => br.replaceWith('\n'));
    const rawLines = (clone.textContent || '')
      .split(/\n+/)
      .map((l) => clean(l))
      .filter(Boolean);

    let course = '';
    for (const line of rawLines) {
      const stripped = line
        .replace(/\s*-?\s*Details\s+about\s+.*$/i, '')
        .replace(/\s*-?\s*Email\s+[A-Za-z,.' -]+$/i, '')
        .replace(/\s*-?\s*(?:Rm|Room):?\s*[A-Za-z0-9 -]+$/i, '')
        .replace(/^\s*-\s*|\s*-\s*$/g, '')
        .trim();

      if (/^(details\s+about|email\s+|rm:|room\s+)/i.test(line)) {
        if (!teacher && /^email\s+/i.test(line)) teacher = normalizeTeacherName(line);
        continue;
      }
      if (
        !course &&
        stripped.length >= 2 &&
        !/^(course|class|subject|period|exp|expression|teacher|attendance|absences|tardies|last week|this week|standards|grades?|not\s+available|unavailable|none|n\/a|no\s+data|--+)$/i.test(stripped) &&
        !/^([0-9]{1,2}\s*(\([A-Za-z0-9, -]+\))?|P\d+|HR(\([A-Za-z0-9-]+\))?)$/i.test(stripped) &&
        !/^\d+(\.\d+)?%?$/.test(stripped)
      ) {
        course = stripped;
      } else if (!teacher && /^(email\s+)/i.test(line)) {
        teacher = normalizeTeacherName(line);
      } else if (
        course &&
        !teacher &&
        stripped.length >= 3 &&
        stripped.length <= 70 &&
        !/^(rm:|room\s+|\d+\s+missing)/i.test(stripped) &&
        (/[A-Za-z]{2,}/.test(stripped))
      ) {
        teacher = normalizeTeacherName(stripped);
      }
    }
    if (!course) return null;
    return { course: course.slice(0, 160), teacher };
  };

  const parseGradeCell = (cell, isKnownGradeCol, baseUrl) => {
    if (!cell) return null;
    const scoreAnchor = cell.tagName === 'A' ? cell : cell.querySelector('a[href*="scores.html"], a[href*="grade" i]');
    const scoresUrl = scoreAnchor ? resolveHref(scoreAnchor.getAttribute('href'), baseUrl) : '';
    const targetEl = scoreAnchor || cell;

    const clone = targetEl.cloneNode(true);
    clone.querySelectorAll('.screen_readers_only, .sr-only, script, style').forEach((n) => n.remove());
    clone.querySelectorAll('br').forEach((br) => br.replaceWith('\n'));
    const rawText = (clone.textContent || '').replace(/\u00a0/g, ' ').trim();
    const compact = clean(rawText);

    if (!compact || /^(\[\s*i\s*\]|--+|-|n\/a|na|ng|not\s*graded|\*+)$/i.test(compact)) {
      return { hasGrade: false, letter: '', score: null, scoresUrl };
    }

    // 1. Letter + Numeric (e.g. "A-\n91.4", "B+ 88%", "A (95.2%)", "91.4%\nA-", "90 A-")
    let m = compact.match(/^([A-DF][+-]?|P|NP|S|U|I|O|E)\s*[\(\/\-:\s]+\s*(\d{1,3}(?:\.\d{1,2})?)\s*%?\)?$/i);
    if (m) {
      const val = parseFloat(m[2]);
      if (val >= 0 && val <= 130) {
        return { hasGrade: true, letter: m[1].toUpperCase(), score: val, scoresUrl };
      }
    }
    m = compact.match(/^(\d{1,3}(?:\.\d{1,2})?)\s*%?\s*[\(\/\-:\s]+\s*([A-DF][+-]?|P|NP|S|U|I|O|E)\)?$/i);
    if (m) {
      const val = parseFloat(m[1]);
      if (val >= 0 && val <= 130) {
        return { hasGrade: true, letter: m[2].toUpperCase(), score: val, scoresUrl };
      }
    }

    // Multi-line fallback (e.g. line 0 = "A-", line 1 = "92" or extra flags)
    const lines = rawText.split(/\n+/).map((l) => clean(l)).filter(Boolean);
    if (lines.length >= 2) {
      let foundLetter = '';
      let foundScore = null;
      for (const ln of lines) {
        if (!foundLetter && VALID_LETTERS.has(ln.toUpperCase())) {
          foundLetter = ln.toUpperCase();
        } else if (foundScore === null && /^(\d{1,3}(?:\.\d{1,2})?)\s*%?$/.test(ln)) {
          const v = parseFloat(ln);
          if (v >= 0 && v <= 130) foundScore = v;
        }
      }
      if (foundLetter || foundScore !== null) {
        return { hasGrade: true, letter: foundLetter, score: foundScore, scoresUrl };
      }
    }

    // 2. Explicit percentage ("92.4%")
    m = compact.match(/^(\d{1,3}(?:\.\d{1,2})?)\s*%$/);
    if (m) {
      const val = parseFloat(m[1]);
      if (val >= 0 && val <= 130) {
        return { hasGrade: true, letter: '', score: val, scoresUrl };
      }
    }

    // 3. Bare numeric or bare letter ONLY when inside a mapped grade column or a `scores.html` link
    if (isKnownGradeCol || Boolean(scoresUrl)) {
      m = compact.match(/^(\d{1,3}(?:\.\d{1,2})?)$/);
      if (m) {
        const val = parseFloat(m[1]);
        if (val >= 0 && val <= 130) {
          return { hasGrade: true, letter: '', score: val, scoresUrl };
        }
      }
      if (VALID_LETTERS.has(compact.toUpperCase())) {
        return { hasGrade: true, letter: compact.toUpperCase(), score: null, scoresUrl };
      }
    }

    return { hasGrade: false, letter: '', score: null, scoresUrl };
  };

  const parseDateMDY = (str) => {
    if (!str) return null;
    const m = str.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
    if (!m) return null;
    let yr = parseInt(m[3], 10);
    if (yr < 100) yr += 2000;
    return new Date(yr, parseInt(m[1], 10) - 1, parseInt(m[2], 10));
  };

  const chooseActiveTerm = (termEntries) => {
    const graded = termEntries.filter((e) => e.hasGrade);
    if (graded.length === 0) {
      // Return latest entry with a scoresUrl so assignments can still be fetched
      const withUrl = termEntries.filter((e) => Boolean(e.scoresUrl));
      return withUrl.length > 0 ? withUrl[withUrl.length - 1] : null;
    }
    const now = new Date();
    // 1. Check if today falls inside [begdate, enddate] on a quarter/trimester scores.html link
    const inWindow = graded.filter((e) => {
      if (!e.scoresUrl) return false;
      try {
        const u = new URL(e.scoresUrl);
        const beg = parseDateMDY(u.searchParams.get('begdate') || '');
        const end = parseDateMDY(u.searchParams.get('enddate') || '');
        if (beg && end) {
          const endOfDay = new Date(end.getTime() + 86399000);
          return now >= beg && now <= endOfDay;
        }
      } catch (_) {}
      return false;
    });
    if (inWindow.length > 0) {
      // Prefer specific quarter/trimester (Q1..Q4, T1..T3, MP1..MP4) over full-year/semester rollup while in window
      const quarterMatch = inWindow.filter((e) => /^(Q[1-4]|T[1-4]|MP[1-6]|PR?[1-4])$/i.test(e.term));
      return quarterMatch.length > 0 ? quarterMatch[quarterMatch.length - 1] : inWindow[inWindow.length - 1];
    }

    // 2. Otherwise pick the rightmost posted term in chronological school-year order
    return graded[graded.length - 1];
  };

  const extractFromDoc = (doc, baseUrl) => {
    const discovered = [];
    const seenRowKeys = new Set();

    const tables = Array.from(doc.querySelectorAll('table#tblgrades, table.linkDescList, table.grid, table'));
    const visitedTables = new Set();

    for (const table of tables) {
      if (visitedTables.has(table)) continue;
      visitedTables.add(table);

      // Skip outer layout tables that merely wrap inner data tables
      const childTables = table.querySelectorAll('table');
      const directDataRows = Array.from(table.querySelectorAll('tr')).filter(
        (tr) => tr.closest('table') === table && !tr.querySelector('table')
      );
      if (childTables.length > 0 && directDataRows.length === 0) continue;

      // Skip assignment score tables on scores.html (those have Due Date + Category + Assignment headers)
      const { colLabels, headerRowSet, allRows } = buildHeaderMap(table);
      const joinedHeaders = colLabels.join(' | ');
      if (/due\s*date/i.test(joinedHeaders) && /category/i.test(joinedHeaders) && /assignment/i.test(joinedHeaders)) {
        continue;
      }

      let courseColIdx = -1;
      let teacherColIdx = -1;
      let expColIdx = -1;
      const gradeCols = []; // [{ idx, term }]

      colLabels.forEach((lbl, idx) => {
        if (!lbl) return;
        if (courseColIdx === -1 && COURSE_COL_RE.test(lbl)) courseColIdx = idx;
        else if (teacherColIdx === -1 && TEACHER_COL_RE.test(lbl)) teacherColIdx = idx;
        else if (expColIdx === -1 && EXP_COL_RE.test(lbl)) expColIdx = idx;
        else if (TERM_HEADER_RE.test(lbl) && !IGNORE_COL_RE.test(lbl)) {
          gradeCols.push({ idx, term: lbl.toUpperCase().replace(/\s+/g, ' ') });
        }
      });

      const isPowerSchoolGradesTable =
        table.id === 'tblgrades' ||
        table.classList.contains('linkDescList') ||
        gradeCols.length > 0 ||
        Boolean(table.querySelector('a[href*="scores.html"]'));

      for (const tr of allRows) {
        if (headerRowSet.has(tr)) continue;
        const rawTds = Array.from(tr.children).filter((el) => el.tagName === 'TD');
        if (rawTds.length < 2) continue;

        // Expand `colspan` on data cells (critical when PowerSchool renders `<td colspan="10">Not available</td>` for attendance!)
        const cells = [];
        for (const td of rawTds) {
          const cs = Math.max(1, parseInt(td.getAttribute('colspan') || '1', 10) || 1);
          for (let k = 0; k < cs; k++) cells.push(td);
        }

        // 1. Period / Expression
        let period = '';
        if (expColIdx >= 0 && expColIdx < cells.length) {
          const pt = clean(cells[expColIdx].innerText || cells[expColIdx].textContent);
          if (pt && pt.length <= 24 && !COURSE_COL_RE.test(pt)) period = pt;
        } else {
          const firstTxt = clean(rawTds[0].innerText || rawTds[0].textContent);
          if (/^([0-9]{1,2}\s*(\([A-Za-z0-9, -]+\))?|P\d+|HR(\([A-Za-z0-9, -]+\))?|ADV|BLK\s*\d+)$/i.test(firstTxt)) {
            period = firstTxt;
          }
        }

        // 2. Course Cell (Header column -> PowerSchool alignment/link cell -> fallback human text cell)
        const candidateCourseCells = [];
        if (courseColIdx >= 0 && courseColIdx < cells.length) {
          candidateCourseCells.push(cells[courseColIdx]);
        }
        const psAligned = tr.querySelector(
          ':scope > td.table-element-text-align-start, :scope > td[align="left"], :scope > td.courseName, :scope > td[data-column*="course" i]'
        );
        if (psAligned && !candidateCourseCells.includes(psAligned)) candidateCourseCells.push(psAligned);
        const teacherLinked = rawTds.find((td) => td.querySelector('a[href*="teacherinfo.html"], a[href^="mailto:"]'));
        if (teacherLinked && !candidateCourseCells.includes(teacherLinked)) candidateCourseCells.push(teacherLinked);
        for (const td of rawTds) {
          if (candidateCourseCells.includes(td)) continue;
          if (expColIdx >= 0 && cells[expColIdx] === td) continue;
          if (teacherColIdx >= 0 && cells[teacherColIdx] === td) continue;
          if (gradeCols.some((gc) => cells[gc.idx] === td)) continue;
          const txt = clean(td.innerText || td.textContent);
          if (txt.length < 3) continue;
          if (/^([0-9]{1,2}\s*(\([A-Za-z0-9, -]+\))?|P\d+|HR|[MTWHFSU]|ABS|not\s+available|unavailable|n\/a|--\s*)$/i.test(txt)) continue;
          if (/^\d+(\.\d+)?%?$/.test(txt)) continue;
          if (/[A-Za-z]{3,}/.test(txt)) candidateCourseCells.push(td);
        }

        let courseCell = null;
        let courseInfo = null;
        for (const cand of candidateCourseCells) {
          const parsedInfo = extractCourseAndTeacher(cand);
          if (parsedInfo && parsedInfo.course) {
            courseCell = cand;
            courseInfo = parsedInfo;
            break;
          }
        }
        if (!courseInfo || !courseInfo.course) continue;

        let teacher = courseInfo.teacher || '';
        if (!teacher && teacherColIdx >= 0 && teacherColIdx < cells.length) {
          teacher = normalizeTeacherName(cells[teacherColIdx].innerText || cells[teacherColIdx].textContent);
        }

        // 3. Extract all term grade cells on this row
        const termEntries = [];
        if (gradeCols.length > 0) {
          for (const gc of gradeCols) {
            if (gc.idx >= cells.length) continue;
            const parsed = parseGradeCell(cells[gc.idx], true, baseUrl);
            if (!parsed) continue;
            let termName = gc.term;
            if (parsed.scoresUrl) {
              try {
                const fg = new URL(parsed.scoresUrl).searchParams.get('fg');
                if (fg) termName = fg.toUpperCase();
              } catch (_) {}
            }
            termEntries.push({
              term: termName || 'Current',
              hasGrade: parsed.hasGrade,
              letter: parsed.letter,
              score: parsed.score,
              scoresUrl: parsed.scoresUrl,
            });
          }
        } else {
          // Fallback when table lacks explicit Q1..S2 headers: inspect scores.html links or remaining cells
          const scoreLinks = Array.from(tr.querySelectorAll('a[href*="scores.html"]'));
          if (scoreLinks.length > 0) {
            for (const a of scoreLinks) {
              const parsed = parseGradeCell(a, true, baseUrl);
              if (!parsed) continue;
              let termName = 'Current';
              if (parsed.scoresUrl) {
                try {
                  const fg = new URL(parsed.scoresUrl).searchParams.get('fg');
                  if (fg) termName = fg.toUpperCase();
                } catch (_) {}
              }
              termEntries.push({
                term: termName,
                hasGrade: parsed.hasGrade,
                letter: parsed.letter,
                score: parsed.score,
                scoresUrl: parsed.scoresUrl,
              });
            }
          } else {
            for (let i = 0; i < cells.length; i++) {
              if (cells[i] === courseCell || i === expColIdx || i === teacherColIdx) continue;
              const colLbl = colLabels[i] || '';
              if (colLbl && IGNORE_COL_RE.test(colLbl)) continue;
              const parsed = parseGradeCell(cells[i], false, baseUrl);
              if (parsed && parsed.hasGrade) {
                termEntries.push({
                  term: colLbl || 'Current',
                  hasGrade: true,
                  letter: parsed.letter,
                  score: parsed.score,
                  scoresUrl: parsed.scoresUrl,
                });
              }
            }
          }
        }

        const active = chooseActiveTerm(termEntries);
        // Require either a valid grade OR an explicit PowerSchool scores.html link on a grade table
        if (!active && !isPowerSchoolGradesTable) continue;
        if (!active) continue;
        if (!active.hasGrade && !active.scoresUrl) continue;

        // Build term_grades summary dictionary
        const termGrades = {};
        for (const te of termEntries) {
          if (te.hasGrade || te.scoresUrl) {
            termGrades[te.term] = {
              letter_grade: te.letter || '',
              score_percent: te.score,
              scores_url: te.scoresUrl || '',
            };
          }
        }

        // Missing indicator if surfaced on overview row
        const rowText = clean(tr.innerText || tr.textContent);
        let missingCount = 0;
        const missMatch = rowText.match(/(\d+)\s+missing/i);
        if (missMatch) missingCount = parseInt(missMatch[1], 10) || 0;

        // Deduplicate: extract section FRN if available so two distinct sections never collide,
        // while keeping course_name clean.
        let frnKey = '';
        if (active.scoresUrl) {
          try {
            frnKey = new URL(active.scoresUrl).searchParams.get('frn') || '';
          } catch (_) {}
        }
        const baseCourseKey = courseInfo.course.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
        let dedupIdentity = frnKey ? `${baseCourseKey}::${frnKey}` : (period ? `${baseCourseKey}::${period.toLowerCase()}` : baseCourseKey);
        if (seenRowKeys.has(dedupIdentity)) continue;
        seenRowKeys.add(dedupIdentity);

        discovered.push({
          course_name: courseInfo.course,
          teacher: teacher || null,
          period: period || null,
          term: active.term || 'Current',
          letter_grade: active.letter || '',
          score_percent: active.score,
          scores_url: active.scoresUrl || null,
          term_grades: termGrades,
          missing_count: missingCount,
        });
        if (discovered.length >= 32) break;
      }
      if (discovered.length >= 32) break;
    }

    // Fallback for card-based portals (Canvas / Schoology / Infinite Campus cards) when 0 table rows matched
    if (discovered.length === 0) {
      const cards = Array.from(
        doc.querySelectorAll('[data-testid*="course"], .ic-DashboardCard, .course-card, .gradebook-course')
      );
      for (const card of cards) {
        const titleEl = card.querySelector('h2, h3, h4, .ic-DashboardCard__header-subtitle, .course-title');
        const gradeEl = card.querySelector('.grade, .course-grade, [class*="grade" i], [class*="score" i]');
        if (!titleEl || !gradeEl) continue;
        const cname = clean(titleEl.innerText || titleEl.textContent);
        const g = parseGradeCell(gradeEl, true, baseUrl);
        if (!cname || !g || !g.hasGrade) continue;
        const key = cname.toLowerCase();
        if (seenRowKeys.has(key)) continue;
        seenRowKeys.add(key);
        const linkEl = card.querySelector('a[href]');
        discovered.push({
          course_name: cname.slice(0, 160),
          teacher: null,
          period: null,
          term: 'Current',
          letter_grade: g.letter || '',
          score_percent: g.score,
          scores_url: linkEl ? resolveHref(linkEl.getAttribute('href'), baseUrl) : null,
          term_grades: {},
          missing_count: 0,
        });
      }
    }

    return discovered;
  };

  let courses = extractFromDoc(document, location.href);

  // CRITICAL RECOVERY: If the user left the PowerSchool tab open on a single class's `scores.html`
  // detail page (which only shows 1 course header row!), fetch `/guardian/home.html` in-session
  // so ALL courses in the student's schedule are discovered!
  try {
    const path = (location.pathname || '').toLowerCase();
    const onSingleScoresPage = path.includes('scores.html') || (path.includes('/guardian/') && !path.endsWith('/home.html') && courses.length <= 1);
    if (onSingleScoresPage && /^https?:/i.test(location.protocol)) {
      const homeUrl = new URL('/guardian/home.html', location.href).href;
      const resp = await fetch(homeUrl, { credentials: 'include' });
      if (resp.ok && !resp.url.toLowerCase().includes('/public/')) {
        const html = await resp.text();
        const homeDoc = new DOMParser().parseFromString(html, 'text/html');
        const homeCourses = extractFromDoc(homeDoc, resp.url || homeUrl);
        if (homeCourses.length > courses.length) {
          courses = homeCourses;
        }
      }
    }
  } catch (_) {}

  // Ensure unique course_name/period disambiguation if two distinct rows share a generic name
  const nameCounts = {};
  for (const c of courses) {
    const k = c.course_name.toLowerCase();
    nameCounts[k] = (nameCounts[k] || 0) + 1;
  }
  const usedSlugs = new Set();
  for (const c of courses) {
    const k = c.course_name.toLowerCase();
    let candidateKey = k.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    if (nameCounts[k] > 1 && c.period) {
      candidateKey = `${candidateKey}-${c.period.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`.replace(/-+/g, '-').replace(/^-|-$/g, '');
    }
    if (usedSlugs.has(candidateKey)) {
      let n = 2;
      while (usedSlugs.has(`${candidateKey}-${n}`)) n++;
      candidateKey = `${candidateKey}-${n}`;
    }
    usedSlugs.add(candidateKey);
    c.course_key = candidateKey;
  }

  return {
    page_title: document.title || '',
    url: location.href,
    courses,
  };
}
"""


# ---------------------------------------------------------------------------
# 2. Automated Per-Course Individual Assignment Harvester (runs in-page)
#    Supports BOTH:
#    A) Modern PowerSchool 20.x-25.x `scores.html` -> `POST /ws/xte/assignment/lookup`
#       (with exact `referrer: scoresUrl` and `'001'`-stripped `studentFRN`)
#    B) Classic PowerSchool `#scoreTable` / `table.linkDescList` & HTML SIS tables
# ---------------------------------------------------------------------------
FETCH_BATCH_ASSIGNMENTS_JS = r"""
async (targets) => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();

  const parseNumeric = (val) => {
    if (val === null || val === undefined || val === '') return null;
    const n = typeof val === 'number' ? val : parseFloat(String(val).replace(/[^0-9.-]/g, ''));
    return Number.isFinite(n) ? Math.round(n * 100) / 100 : null;
  };

  const formatScoreDisplay = (pts, maxPts, actualEntered) => {
    if (pts !== null && maxPts !== null) return `${pts}/${maxPts}`;
    if (actualEntered) return String(actualEntered).trim();
    if (pts !== null) return String(pts);
    if (maxPts !== null) return `—/${maxPts}`;
    return '—';
  };

  // Parse classic or hydrated HTML assignment tables inside `scores.html`
  const parseHtmlAssignments = (doc) => {
    const items = [];
    const tables = Array.from(
      doc.querySelectorAll('table#scoreTable, table[id*="assignment" i], table.linkDescList, table.grid, table')
    );

    for (const table of tables) {
      if (table.querySelector('table')) continue;
      const rows = Array.from(table.querySelectorAll('tr')).filter((tr) => tr.closest('table') === table);
      if (rows.length < 2) continue;

      const headerCells = Array.from(rows[0].children).filter((el) => el.tagName === 'TH' || el.tagName === 'TD');
      const headers = headerCells.map((c) => clean(c.innerText || c.textContent).toLowerCase());
      const joined = headers.join(' | ');

      // Skip the 1-row Course Info summary table ("Course | Teacher | Expression | Final Grade")
      if (headers.includes('teacher') && headers.includes('expression')) continue;
      if (table.id === 'tblgrades') continue;

      const hasAssignmentCol = headers.some((h) => /(assignment|title|name|task|item|work|description)/.test(h));
      const hasScoreOrDueCol = headers.some((h) => /(due|date|score|pts|points|%|percent|grade|category)/.test(h));
      if (!hasAssignmentCol || !hasScoreOrDueCol) continue;

      const findCol = (re) => headers.findIndex((h) => re.test(h));
      const dueIdx = findCol(/(due|date)/);
      const catIdx = findCol(/(category|type|group)/);
      const nameIdx = findCol(/(assignment|title|name|task|item|work|description)/);
      const scoreIdx = findCol(/^(score|points|pts|earned|mark)$|score\s*\//);
      const pctIdx = findCol(/(^%$|percent|pct)/);
      const gradeIdx = findCol(/(^grade$|letter)/);
      const flagIdx = findCol(/(flag|code|status)/);

      for (let r = 1; r < rows.length; r++) {
        const tds = Array.from(rows[r].children).filter((el) => el.tagName === 'TD');
        if (tds.length < 3 || nameIdx < 0 || nameIdx >= tds.length) continue;

        const nameClone = tds[nameIdx].cloneNode(true);
        nameClone.querySelectorAll('.screen_readers_only, .sr-only, script, style').forEach((n) => n.remove());
        const title = clean(nameClone.innerText || nameClone.textContent);
        if (!title || /^(totals?|averages?|no\s+assignments?)$/i.test(title)) continue;

        const dueDate = dueIdx >= 0 && dueIdx < tds.length ? clean(tds[dueIdx].innerText || tds[dueIdx].textContent) : '';
        const category = catIdx >= 0 && catIdx < tds.length ? clean(tds[catIdx].innerText || tds[catIdx].textContent) : 'Assignment';
        const rawScore = scoreIdx >= 0 && scoreIdx < tds.length ? clean(tds[scoreIdx].innerText || tds[scoreIdx].textContent) : '';
        const rawPct = pctIdx >= 0 && pctIdx < tds.length ? clean(tds[pctIdx].innerText || tds[pctIdx].textContent) : '';
        const rawGrade = gradeIdx >= 0 && gradeIdx < tds.length ? clean(tds[gradeIdx].innerText || tds[gradeIdx].textContent) : '';

        let scorePoints = null;
        let maxPoints = null;
        let scoreDisplay = rawScore || '—';
        const slashMatch = rawScore.match(/^(-|\-\-|\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/);
        if (slashMatch) {
          scorePoints = slashMatch[1].startsWith('-') ? null : parseFloat(slashMatch[1]);
          maxPoints = parseFloat(slashMatch[2]);
          scoreDisplay = `${slashMatch[1]}/${slashMatch[2]}`;
        } else if (/^\d+(?:\.\d+)?$/.test(rawScore)) {
          scorePoints = parseFloat(rawScore);
        }

        let scorePercent = parseNumeric(rawPct);
        if (scorePercent === null && scorePoints !== null && maxPoints && maxPoints > 0) {
          scorePercent = Math.round((scorePoints / maxPoints) * 1000) / 10;
        }

        // Detect PowerSchool status flags in flag column, icons, or row text
        const rowHtml = rows[r].innerHTML || '';
        const flagText = flagIdx >= 0 && flagIdx < tds.length ? clean(tds[flagIdx].innerText || tds[flagIdx].textContent) : '';
        const flagInspect = `${flagText} ${rawScore}`;
        const isMissing = /\bmissing\b/i.test(flagInspect) || /ps-icon-missing|alt=["']missing/i.test(rowHtml) || Boolean(rows[r].querySelector('[title*="Missing" i], .missing'));
        const isLate = /\blate\b/i.test(flagInspect) || /ps-icon-late|alt=["']late/i.test(rowHtml) || Boolean(rows[r].querySelector('[title*="Late" i], .late'));
        const isExempt = /\bexempt\b/i.test(flagInspect) || Boolean(rows[r].querySelector('[title*="Exempt" i], .exempt'));
        const isIncomplete = /\bincomplete\b/i.test(flagInspect) || Boolean(rows[r].querySelector('[title*="Incomplete" i]'));
        const isCollected = /\bcollected\b/i.test(flagInspect) || Boolean(rows[r].querySelector('[title*="Collected" i]'));

        const flags = [];
        if (isMissing) flags.push('Missing');
        if (isLate) flags.push('Late');
        if (isIncomplete) flags.push('Incomplete');
        if (isExempt) flags.push('Exempt');
        if (isCollected) flags.push('Collected');

        items.push({
          assignment_id: `dom-${r}-${title.slice(0, 32)}`,
          title: title.slice(0, 180),
          category: (category || 'Assignment').slice(0, 80),
          due_date: dueDate.slice(0, 32),
          score_points: scorePoints,
          max_points: maxPoints,
          score_display: scoreDisplay,
          score_percent: scorePercent,
          letter_grade: rawGrade.replace(/^(\[\s*i\s*\]|--+|-)$/i, '').slice(0, 12),
          weight: null,
          flags,
          is_missing: isMissing,
          is_late: isLate,
          is_exempt: isExempt,
          is_incomplete: isIncomplete,
          is_collected: isCollected,
          comment: '',
        });
      }
      if (items.length > 0) break;
    }
    return items;
  };

  const mapXteJson = (arr) => {
    if (!Array.isArray(arr)) return [];
    const out = [];
    for (const item of arr) {
      const sections = Array.isArray(item._assignmentsections)
        ? item._assignmentsections
        : (item.assignmentsections || [item]);
      for (const sec of sections) {
        if (!sec || !sec.name) continue;
        let category = 'Assignment';
        const assocs = sec._assignmentcategoryassociations || [];
        const primary = assocs.find((a) => a && a.isprimary && a._teachercategory?.name) || assocs.find((a) => a && a._teachercategory?.name);
        if (primary && primary._teachercategory?.name) {
          category = clean(primary._teachercategory.name);
        }

        const scoreObj = Array.isArray(sec._assignmentscores) && sec._assignmentscores.length > 0
          ? sec._assignmentscores[0]
          : null;

        const maxPoints = parseNumeric(sec.scoreentrypoints ?? sec.totalpointvalue);
        const scorePoints = scoreObj ? parseNumeric(scoreObj.scorepoints) : null;
        const actualEntered = scoreObj?.actualscoreentered || '';
        let scorePercent = scoreObj ? parseNumeric(scoreObj.scorepercent) : null;
        if (scorePercent === null && scorePoints !== null && maxPoints && maxPoints > 0) {
          scorePercent = Math.round((scorePoints / maxPoints) * 1000) / 10;
        }
        const letterGrade = clean(scoreObj?.scorelettergrade || '');

        const isMissing = Boolean(scoreObj?.ismissing);
        const isLate = Boolean(scoreObj?.islate);
        const isIncomplete = Boolean(scoreObj?.isincomplete);
        const isExempt = Boolean(scoreObj?.isexempt);
        const isAbsent = Boolean(scoreObj?.isabsent);
        const isCollected = Boolean(scoreObj?.iscollected);

        const flags = [];
        if (isMissing) flags.push('Missing');
        if (isLate) flags.push('Late');
        if (isIncomplete) flags.push('Incomplete');
        if (isAbsent) flags.push('Absent');
        if (isExempt) flags.push('Exempt');
        if (isCollected) flags.push('Collected');

        const comment = clean(scoreObj?._assignmentscorecomment?.commentvalue || '');

        out.push({
          assignment_id: String(item.assignmentid || sec.assignmentsectionid || sec.id || sec.name),
          title: clean(sec.name).slice(0, 180),
          category: category.slice(0, 80),
          due_date: clean(sec.duedate || '').slice(0, 32),
          score_points: scorePoints,
          max_points: maxPoints,
          score_display: formatScoreDisplay(scorePoints, maxPoints, actualEntered),
          score_percent: scorePercent,
          letter_grade: letterGrade.slice(0, 12),
          weight: parseNumeric(sec.weight),
          flags,
          is_missing: isMissing,
          is_late: isLate,
          is_exempt: isExempt,
          is_incomplete: isIncomplete,
          is_collected: isCollected,
          comment: comment.slice(0, 300),
        });
      }
    }
    // Sort newest due_date first
    out.sort((a, b) => String(b.due_date || '').localeCompare(String(a.due_date || '')));
    return out;
  };

  const computeSchoolYearRange = (scoresUrl) => {
    const now = new Date();
    const startYr = now.getMonth() >= 6 ? now.getFullYear() : now.getFullYear() - 1;
    let startDate = `${startYr}-8-1`;
    let endDate = `${startYr + 1}-7-15`;
    let termBeg = null;
    let termEnd = null;
    try {
      const u = new URL(scoresUrl);
      const parseParam = (raw) => {
        if (!raw) return null;
        const m = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
        if (!m) return null;
        let y = parseInt(m[3], 10);
        if (y < 100) y += 2000;
        const mo = parseInt(m[1], 10);
        const da = parseInt(m[2], 10);
        return {
          api: `${y}-${mo}-${da}`,
          iso: `${y}-${String(mo).padStart(2, '0')}-${String(da).padStart(2, '0')}`,
        };
      };
      termBeg = parseParam(u.searchParams.get('begdate'));
      termEnd = parseParam(u.searchParams.get('enddate'));
    } catch (_) {}
    return { startDate, endDate, termBeg, termEnd };
  };

  const fetchOneCourse = async (target) => {
    const { course_key, scores_url } = target;
    if (!scores_url) return { course_key, assignments: [], needs_rendered_fallback: false };

    // If current document IS already this exact scores_url, inspect live DOM first!
    try {
      if (location.href === scores_url) {
        const liveItems = parseHtmlAssignments(document);
        if (liveItems.length > 0) {
          return { course_key, assignments: liveItems, needs_rendered_fallback: false };
        }
      }
    } catch (_) {}

    let html = '';
    let finalUrl = scores_url;
    try {
      const resp = await fetch(scores_url, { credentials: 'include' });
      if (!resp.ok) return { course_key, assignments: [], needs_rendered_fallback: false };
      finalUrl = resp.url || scores_url;
      if (finalUrl.toLowerCase().includes('/public/')) {
        return { course_key, assignments: [], needs_rendered_fallback: false };
      }
      html = await resp.text();
    } catch (_) {
      return { course_key, assignments: [], needs_rendered_fallback: false };
    }

    const doc = new DOMParser().parseFromString(html, 'text/html');

    // 1. Try classic / server-rendered HTML table inside `scores.html`
    const htmlItems = parseHtmlAssignments(doc);
    if (htmlItems.length > 0) {
      return { course_key, assignments: htmlItems, needs_rendered_fallback: false };
    }

    // 2. Try PowerSchool XTE JSON endpoint (`POST /ws/xte/assignment/lookup`)
    const secAttr =
      doc.querySelector('[data-sectionid]')?.getAttribute('data-sectionid') ||
      (html.match(/data-sectionid\s*=\s*["'](\d+)["']/i) || [])[1] ||
      '';

    const studentCandidates = [];
    const frnMatch = html.match(/studentFRN\s*=\s*['"](\d+)['"]/i);
    if (frnMatch && frnMatch[1]) {
      const rawFrn = frnMatch[1];
      if (rawFrn.startsWith('001') && rawFrn.length > 3) {
        studentCandidates.push(parseInt(rawFrn.slice(3), 10));
      } else if (rawFrn.length > 3) {
        studentCandidates.push(parseInt(rawFrn.slice(3), 10));
      }
      studentCandidates.push(parseInt(rawFrn, 10));
    }
    const dcidMatch = html.match(/studentsdcid["'\s:=]+(\d+)/i) || html.match(/student_ids["'\s:=\[]+(\d+)/i);
    if (dcidMatch && dcidMatch[1]) {
      studentCandidates.push(parseInt(dcidMatch[1], 10));
    }
    const uniqueStudentIds = Array.from(new Set(studentCandidates.filter((n) => Number.isFinite(n) && n > 0)));

    if (secAttr && uniqueStudentIds.length > 0) {
      const sectionId = parseInt(secAttr, 10);
      const { startDate, endDate, termBeg, termEnd } = computeSchoolYearRange(finalUrl);
      const xteBase = new URL('/ws/xte/assignment/lookup', finalUrl).href;

      for (const studentApiId of uniqueStudentIds) {
        try {
          const reqStart = termBeg ? termBeg.api : startDate;
          const reqEnd = termEnd ? termEnd.api : endDate;
          const xteResp = await fetch(`${xteBase}?_=${Date.now()}`, {
            method: 'POST',
            credentials: 'include',
            referrer: finalUrl,
            headers: {
              'Content-Type': 'application/json;charset=UTF-8',
              'Accept': 'application/json, text/plain, */*',
              'X-Requested-With': 'XMLHttpRequest',
            },
            body: JSON.stringify({
              section_ids: [sectionId],
              student_ids: [studentApiId],
              start_date: reqStart,
              end_date: reqEnd,
            }),
          });
          if (xteResp.ok) {
            const ctype = (xteResp.headers.get('content-type') || '').toLowerCase();
            if (ctype.includes('json')) {
              let data = await xteResp.json();
              let mapped = mapXteJson(data);
              // If strict term window returned 0 items, retry across full school year
              if (mapped.length === 0 && (termBeg || termEnd)) {
                const fullResp = await fetch(`${xteBase}?_=${Date.now()}`, {
                  method: 'POST',
                  credentials: 'include',
                  referrer: finalUrl,
                  headers: {
                    'Content-Type': 'application/json;charset=UTF-8',
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest',
                  },
                  body: JSON.stringify({
                    section_ids: [sectionId],
                    student_ids: [studentApiId],
                    start_date: startDate,
                    end_date: endDate,
                  }),
                });
                if (fullResp.ok && (fullResp.headers.get('content-type') || '').toLowerCase().includes('json')) {
                  mapped = mapXteJson(await fullResp.json());
                }
              }
              return { course_key, assignments: mapped, needs_rendered_fallback: false };
            }
          }
        } catch (_) {}
      }
    }

    const hasSpaMarkers = /data-pss-student-assignment-scores|ng-app|xteContentWrapper|assignment-scores/i.test(html);
    return { course_key, assignments: [], needs_rendered_fallback: hasSpaMarkers };
  };

  // Fetch in batches of 4 for speed without overwhelming slow school portals
  const results = {};
  const list = Array.isArray(targets) ? targets.slice(0, 20) : [];
  for (let i = 0; i < list.length; i += 4) {
    const batch = list.slice(i, i + 4);
    const settled = await Promise.all(batch.map((t) => fetchOneCourse(t)));
    for (const item of settled) {
      results[item.course_key] = item;
    }
  }
  return results;
}
"""


# Helper JS for scraping an already-hydrated fallback background tab
SCRAPE_HYDRATED_ASSIGNMENTS_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const tables = Array.from(document.querySelectorAll('table'));
  for (const table of tables) {
    if (table.querySelector('table') || table.id === 'tblgrades') continue;
    const rows = Array.from(table.querySelectorAll('tr')).filter((tr) => tr.closest('table') === table);
    if (rows.length < 2) continue;
    const headers = Array.from(rows[0].children).map((c) => clean(c.innerText || c.textContent).toLowerCase());
    if (headers.includes('teacher') && headers.includes('expression')) continue;
    const nameIdx = headers.findIndex((h) => /(assignment|title|name|task|item|work)/.test(h));
    if (nameIdx < 0) continue;
    const dueIdx = headers.findIndex((h) => /(due|date)/.test(h));
    const catIdx = headers.findIndex((h) => /(category|type|group)/.test(h));
    const scoreIdx = headers.findIndex((h) => /^(score|points|pts|earned|mark)$|score\s*\//.test(h));
    const pctIdx = headers.findIndex((h) => /(^%$|percent|pct)/.test(h));
    const gradeIdx = headers.findIndex((h) => /(^grade$|letter)/.test(h));
    const items = [];
    for (let r = 1; r < rows.length; r++) {
      const tds = Array.from(rows[r].children).filter((el) => el.tagName === 'TD');
      if (tds.length < 3 || nameIdx >= tds.length) continue;
      const title = clean(tds[nameIdx].innerText || tds[nameIdx].textContent);
      if (!title || /^(totals?|averages?|no\s+assignments?)$/i.test(title)) continue;
      const rawScore = scoreIdx >= 0 && scoreIdx < tds.length ? clean(tds[scoreIdx].innerText || tds[scoreIdx].textContent) : '';
      const rawPct = pctIdx >= 0 && pctIdx < tds.length ? clean(tds[pctIdx].innerText || tds[pctIdx].textContent) : '';
      const rawGrade = gradeIdx >= 0 && gradeIdx < tds.length ? clean(tds[gradeIdx].innerText || tds[gradeIdx].textContent) : '';
      let scorePoints = null, maxPoints = null;
      const sm = rawScore.match(/^(-|\-\-|\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/);
      if (sm) {
        scorePoints = sm[1].startsWith('-') ? null : parseFloat(sm[1]);
        maxPoints = parseFloat(sm[2]);
      }
      const pctNum = rawPct ? parseFloat(rawPct.replace(/[^0-9.-]/g, '')) : NaN;
      const scorePercent = Number.isFinite(pctNum)
        ? Math.round(pctNum * 100) / 100
        : (scorePoints !== null && maxPoints > 0 ? Math.round((scorePoints / maxPoints) * 1000) / 10 : null);
      const rowHtml = rows[r].innerHTML || '';
      const isMissing = /missing/i.test(rowHtml);
      const isLate = /late/i.test(rowHtml);
      const flags = [];
      if (isMissing) flags.push('Missing');
      if (isLate) flags.push('Late');
      items.push({
        assignment_id: `hyd-${r}-${title.slice(0, 32)}`,
        title: title.slice(0, 180),
        category: (catIdx >= 0 && catIdx < tds.length ? clean(tds[catIdx].innerText || tds[catIdx].textContent) : 'Assignment').slice(0, 80),
        due_date: (dueIdx >= 0 && dueIdx < tds.length ? clean(tds[dueIdx].innerText || tds[dueIdx].textContent) : '').slice(0, 32),
        score_points: scorePoints,
        max_points: maxPoints,
        score_display: rawScore || '—',
        score_percent: scorePercent,
        letter_grade: rawGrade.slice(0, 12),
        weight: null,
        flags,
        is_missing: isMissing,
        is_late: isLate,
        is_exempt: false,
        is_incomplete: false,
        is_collected: false,
        comment: '',
      });
    }
    if (items.length > 0) return items;
  }
  return [];
}
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:140]


def _letter_to_gpa_points(letter: str, score: Optional[float]) -> Optional[float]:
    table = {
        "A+": 4.0, "A": 4.0, "A-": 3.7,
        "B+": 3.3, "B": 3.0, "B-": 2.7,
        "C+": 2.3, "C": 2.0, "C-": 1.7,
        "D+": 1.3, "D": 1.0, "D-": 0.7,
        "F": 0.0, "E": 0.0,
    }
    clean = (letter or "").strip().upper()
    if clean in table:
        return table[clean]
    if score is not None:
        if score >= 93:
            return 4.0
        if score >= 90:
            return 3.7
        if score >= 87:
            return 3.3
        if score >= 83:
            return 3.0
        if score >= 80:
            return 2.7
        if score >= 77:
            return 2.3
        if score >= 73:
            return 2.0
        if score >= 70:
            return 1.7
        if score >= 67:
            return 1.3
        if score >= 60:
            return 1.0
        return 0.0
    return None


def _format_grade_display(letter: Optional[str], score: Optional[float]) -> str:
    l = (letter or "").strip()
    if l and score is not None:
        return f"{l} ({score:.1f}%)"
    if score is not None:
        return f"{score:.1f}%"
    return l or "—"


def _safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if type(parsed) is type(default) else default
    except Exception:
        return default


def _map_xte_payload_python(arr: Any) -> List[Dict[str, Any]]:
    """Python-side mapper for intercepted `/ws/xte/assignment/lookup` responses in background fallback."""
    if not isinstance(arr, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        sections = item.get("_assignmentsections") or item.get("assignmentsections") or [item]
        for sec in sections:
            if not isinstance(sec, dict) or not sec.get("name"):
                continue
            category = "Assignment"
            for assoc in sec.get("_assignmentcategoryassociations") or []:
                if isinstance(assoc, dict) and isinstance(assoc.get("_teachercategory"), dict):
                    cname = str(assoc["_teachercategory"].get("name") or "").strip()
                    if cname:
                        category = cname
                        if assoc.get("isprimary"):
                            break
            scores = sec.get("_assignmentscores") or []
            sc = scores[0] if scores and isinstance(scores[0], dict) else {}
            max_pts = sec.get("scoreentrypoints") if sec.get("scoreentrypoints") is not None else sec.get("totalpointvalue")
            pts = sc.get("scorepoints")
            pct = sc.get("scorepercent")
            try:
                max_f = round(float(max_pts), 2) if max_pts is not None else None
            except Exception:
                max_f = None
            try:
                pts_f = round(float(pts), 2) if pts is not None else None
            except Exception:
                pts_f = None
            try:
                pct_f = round(float(pct), 2) if pct is not None else None
            except Exception:
                pct_f = None
            if pct_f is None and pts_f is not None and max_f and max_f > 0:
                pct_f = round((pts_f / max_f) * 100.0, 1)

            is_missing = bool(sc.get("ismissing"))
            is_late = bool(sc.get("islate"))
            is_incomplete = bool(sc.get("isincomplete"))
            is_exempt = bool(sc.get("isexempt"))
            is_collected = bool(sc.get("iscollected"))
            flags = [
                label
                for cond, label in (
                    (is_missing, "Missing"),
                    (is_late, "Late"),
                    (is_incomplete, "Incomplete"),
                    (bool(sc.get("isabsent")), "Absent"),
                    (is_exempt, "Exempt"),
                    (is_collected, "Collected"),
                )
                if cond
            ]
            score_disp = (
                f"{pts_f:g}/{max_f:g}"
                if pts_f is not None and max_f is not None
                else (str(sc.get("actualscoreentered") or "").strip() or (f"{pts_f:g}" if pts_f is not None else "—"))
            )
            comment_obj = sc.get("_assignmentscorecomment")
            comment = str(comment_obj.get("commentvalue") or "").strip() if isinstance(comment_obj, dict) else ""
            out.append(
                {
                    "assignment_id": str(item.get("assignmentid") or sec.get("assignmentsectionid") or sec.get("name")),
                    "title": str(sec.get("name") or "").strip()[:180],
                    "category": category[:80],
                    "due_date": str(sec.get("duedate") or "").strip()[:32],
                    "score_points": pts_f,
                    "max_points": max_f,
                    "score_display": score_disp,
                    "score_percent": pct_f,
                    "letter_grade": str(sc.get("scorelettergrade") or "").strip()[:12],
                    "weight": float(sec["weight"]) if sec.get("weight") is not None else None,
                    "flags": flags,
                    "is_missing": is_missing,
                    "is_late": is_late,
                    "is_exempt": is_exempt,
                    "is_incomplete": is_incomplete,
                    "is_collected": is_collected,
                    "comment": comment[:300],
                }
            )
    out.sort(key=lambda a: str(a.get("due_date") or ""), reverse=True)
    return out


class GradesService:
    """Extracts, remembers, diffs, and summarizes PowerSchool / SIS grades & assignments."""

    def __init__(self) -> None:
        self._last_assignment_fetch_ts: Dict[str, float] = {}
        self._last_course_digest: Dict[str, str] = {}

    async def extract_grades_from_page(
        self,
        page: Any,
        source_app: str = "PowerSchool",
        fetch_assignments: bool = True,
        force_assignments: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Run multi-course extraction + automated individual assignment harvesting across:
          - Schoology (`/iapi` + `.upcoming-events` + `/grades/grades` via `lms_connector`)
          - Canvas LMS (`/api/v1/courses` + `/assignments` + `/planner/items` via `lms_connector`)
          - Google Classroom (`lms_connector`)
          - PowerSchool (`table#tblgrades` + `scores.html` + `/ws/xte/assignment/lookup`) & generic SIS portals
        """
        import time as _time
        from .lms_connector import extract_lms_courses_and_tasks, sync_lms_assignments_to_tasks

        specialized = await extract_lms_courses_and_tasks(page, source_app=source_app)
        if specialized is not None:
            return specialized

        try:
            res = await page.evaluate(EXTRACT_GRADES_DOM_JS)
            if not isinstance(res, dict):
                return []
        except Exception as exc:
            logger.debug("Grades DOM extraction error: %s", exc)
            return []

        courses: List[Dict[str, Any]] = res.get("courses") or []
        if not fetch_assignments or not courses:
            return courses

        # Smart throttling: on explicit `Scan Grades` / Cron (`force_assignments=True`), always harvest
        # `scores.html` assignments; on passive 15s stream polls, only re-harvest if >180s elapsed or a grade changed.
        app_key = (source_app or "PowerSchool").strip().lower()
        digest = "|".join(
            f"{c.get('course_name')}:{c.get('letter_grade')}:{c.get('score_percent')}" for c in courses
        )
        now_mono = _time.monotonic()
        prev_ts = self._last_assignment_fetch_ts.get(app_key, 0.0)
        prev_digest = self._last_course_digest.get(app_key, "")
        if not force_assignments and prev_ts > 0 and (now_mono - prev_ts) < 180.0 and digest == prev_digest:
            return courses

        # Automatically follow each course's `scores.html` link to harvest individual assignment grades
        targets = []
        base_url = str(res.get("url") or getattr(page, "url", "") or "")
        for idx, c in enumerate(courses):
            ck = str(c.get("course_key") or _slug(str(c.get("course_name") or f"course-{idx}")))
            c["course_key"] = ck
            surl = str(c.get("scores_url") or "").strip()
            if surl and base_url:
                surl = urljoin(base_url, surl)
                c["scores_url"] = surl
            if surl:
                targets.append({"course_key": ck, "scores_url": surl})

        if not targets:
            return courses

        batch_map: Dict[str, Any] = {}
        try:
            raw_batch = await page.evaluate(FETCH_BATCH_ASSIGNMENTS_JS, targets)
            if isinstance(raw_batch, dict):
                batch_map = raw_batch
        except Exception as exc:
            logger.debug("In-page batch assignment harvest skipped/failed: %s", exc)

        fallback_targets: List[Tuple[Dict[str, Any], str]] = []
        for c in courses:
            ck = c["course_key"]
            info = batch_map.get(ck) or {}
            assigns = info.get("assignments") if isinstance(info, dict) else None
            if isinstance(assigns, list) and len(assigns) > 0:
                c["assignments"] = assigns
                miss_cnt = sum(1 for a in assigns if a.get("is_missing"))
                if miss_cnt > int(c.get("missing_count") or 0):
                    c["missing_count"] = miss_cnt
            elif isinstance(info, dict) and info.get("needs_rendered_fallback") and c.get("scores_url"):
                fallback_targets.append((c, str(c["scores_url"])))

        # Hidden background-tab fallback for custom JS-hydrated PowerSchool plugins (never touches user's live stream tab)
        if fallback_targets and hasattr(page, "context") and page.context is not None:
            bg_page = None
            try:
                bg_page = await page.context.new_page()
                for course_dict, surl in fallback_targets[:6]:
                    intercepted_xte: List[Dict[str, Any]] = []

                    async def _on_resp(response: Any) -> None:
                        try:
                            if "/ws/xte/assignment/lookup" in (response.url or "") and response.status == 200:
                                data = await response.json()
                                parsed_items = _map_xte_payload_python(data)
                                if parsed_items:
                                    intercepted_xte.extend(parsed_items)
                        except Exception:
                            pass

                    bg_page.on("response", _on_resp)
                    try:
                        await bg_page.goto(surl, wait_until="domcontentloaded", timeout=7500)
                        await bg_page.wait_for_timeout(1100)
                        if intercepted_xte:
                            course_dict["assignments"] = intercepted_xte
                        else:
                            dom_items = await bg_page.evaluate(SCRAPE_HYDRATED_ASSIGNMENTS_JS)
                            if isinstance(dom_items, list) and dom_items:
                                course_dict["assignments"] = dom_items
                        if course_dict.get("assignments"):
                            miss_cnt = sum(1 for a in course_dict["assignments"] if a.get("is_missing"))
                            if miss_cnt > int(course_dict.get("missing_count") or 0):
                                course_dict["missing_count"] = miss_cnt
                    except Exception as inner_exc:
                        logger.debug("Background scores.html fallback skipped for %s: %s", surl, inner_exc)
                    finally:
                        try:
                            bg_page.remove_listener("response", _on_resp)
                        except Exception:
                            pass
            except Exception as bg_exc:
                logger.debug("Could not open background page for assignment fallback: %s", bg_exc)
            finally:
                if bg_page is not None:
                    try:
                        await bg_page.close()
                    except Exception:
                        pass

        self._last_assignment_fetch_ts[app_key] = now_mono
        self._last_course_digest[app_key] = digest

        # Bridge any missing PowerSchool assignments straight into Node Tasks
        missing_subs: List[Dict[str, Any]] = []
        for c in courses:
            for a in c.get("assignments") or []:
                if a.get("is_missing"):
                    missing_subs.append(
                        {
                            "title": a.get("title"),
                            "course_name": c.get("course_name"),
                            "due_date": a.get("due_date") or "",
                            "status": "overdue",
                            "is_missing": True,
                        }
                    )
        if missing_subs:
            sync_lms_assignments_to_tasks(missing_subs, source_app=source_app)

        return courses

    def ingest_courses(
        self,
        session: Session,
        source_app: str = "PowerSchool",
        raw_courses: Optional[List[Dict[str, Any]]] = None,
        extracted_courses: Optional[List[Dict[str, Any]]] = None,
        emit_pings: bool = True,
        emit_notifications: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Persist ALL discovered courses, track score history/trends & assignments, and alert on changes."""
        course_inputs = raw_courses if raw_courses is not None else (extracted_courses or [])
        should_emit = emit_notifications if emit_notifications is not None else emit_pings
        clean_app = (source_app or "PowerSchool").strip()
        raw_existing_rows = list(
            session.exec(select(CourseGrade).where(CourseGrade.source_app == clean_app)).all()
        )
        existing_rows: List[CourseGrade] = []
        for r in raw_existing_rows:
            if (r.course_name or "").strip().lower() in ("not available", "unavailable", "n/a"):
                session.delete(r)
            else:
                existing_rows.append(r)
        by_key: Dict[str, CourseGrade] = {r.course_key: r for r in existing_rows}
        # Secondary lookup by normalized course_name so legacy single-row entries link seamlessly
        by_norm_name: Dict[str, List[CourseGrade]] = {}
        for r in existing_rows:
            nk = _slug(r.course_name)
            by_norm_name.setdefault(nk, []).append(r)

        changes: List[Dict[str, Any]] = []
        updated_rows: List[CourseGrade] = []
        now = utc_now()
        now_iso = now.isoformat()

        # Check whether incoming scan has duplicate course names (e.g. two sections of same title)
        incoming_name_counts: Dict[str, int] = {}
        for item in course_inputs:
            nm = _slug(str(item.get("course_name") or ""))
            if nm:
                incoming_name_counts[nm] = incoming_name_counts.get(nm, 0) + 1

        matched_row_ids: set = set()

        for item in course_inputs:
            cname = str(item.get("course_name") or "").strip()[:200]
            if not cname:
                continue
            norm_name = _slug(cname)
            explicit_key = _slug(str(item.get("course_key") or ""))
            if incoming_name_counts.get(norm_name, 0) > 1 and item.get("period"):
                ckey = explicit_key or _slug(f"{cname}-{item.get('period')}")
            else:
                ckey = norm_name or explicit_key
            if not ckey:
                continue

            new_letter = str(item.get("letter_grade") or "").strip()[:24]
            raw_score = item.get("score_percent")
            new_score: Optional[float] = None
            if raw_score is not None:
                try:
                    new_score = round(float(raw_score), 2)
                except (TypeError, ValueError):
                    new_score = None

            teacher = (str(item.get("teacher")).strip()[:120] if item.get("teacher") else None)
            period = (str(item.get("period")).strip()[:40] if item.get("period") else None)
            term = str(item.get("term") or "Current").strip()[:40]
            scores_url = str(item.get("scores_url") or "").strip()[:1200] or None
            term_grades = item.get("term_grades") if isinstance(item.get("term_grades"), dict) else {}
            incoming_assignments = item.get("assignments") if isinstance(item.get("assignments"), list) else None
            missing_count = int(item.get("missing_count") or 0)
            if incoming_assignments is not None:
                miss_from_assign = sum(1 for a in incoming_assignments if isinstance(a, dict) and a.get("is_missing"))
                missing_count = max(missing_count, miss_from_assign)

            row = by_key.get(ckey)
            if row is None and incoming_name_counts.get(norm_name, 0) == 1:
                candidates = [r for r in by_norm_name.get(norm_name, []) if r.id not in matched_row_ids]
                if len(candidates) == 1:
                    row = candidates[0]
                    row.course_key = ckey

            if row is None:
                initial_history: List[Dict[str, Any]] = []
                if new_score is not None or new_letter:
                    initial_history.append(
                        {
                            "ts": now_iso,
                            "score_percent": new_score,
                            "letter_grade": new_letter,
                            "term": term,
                        }
                    )
                assign_list = incoming_assignments if incoming_assignments is not None else []
                recent_titles = [a.get("title") for a in assign_list[:5] if isinstance(a, dict) and a.get("title")]
                row = CourseGrade(
                    source_app=clean_app,
                    course_key=ckey,
                    course_name=cname,
                    teacher=teacher,
                    period=period,
                    term=term,
                    letter_grade=new_letter,
                    score_percent=new_score,
                    missing_count=missing_count,
                    scores_url=scores_url,
                    history_json=json.dumps(initial_history),
                    term_grades_json=json.dumps(term_grades),
                    assignments_json=json.dumps(assign_list),
                    recent_items_json=json.dumps(recent_titles),
                    updated_at=now,
                )
                session.add(row)
                by_key[ckey] = row
                updated_rows.append(row)
                continue

            if row.id is not None:
                matched_row_ids.add(row.id)

            old_letter = (row.letter_grade or "").strip()
            old_score = row.score_percent

            # Detect genuine score or letter change (do not overwrite an existing posted grade with blank `[ i ]`)
            incoming_has_grade = (new_score is not None) or bool(new_letter)
            score_changed = (
                old_score is not None
                and new_score is not None
                and abs(new_score - old_score) >= 0.05
            ) or (old_score is None and new_score is not None)
            letter_changed = bool(new_letter) and new_letter != old_letter

            history = _safe_json_loads(row.history_json, [])
            if not history and (old_score is not None or old_letter):
                history.append(
                    {
                        "ts": (row.updated_at or now).isoformat(),
                        "score_percent": old_score,
                        "letter_grade": old_letter,
                        "term": row.term or "Current",
                    }
                )

            # Identify any newly scored assignment to enrich change notifications
            prev_assignments = _safe_json_loads(row.assignments_json, [])
            prev_scored_keys = {
                f"{a.get('assignment_id')}::{a.get('score_display')}"
                for a in prev_assignments
                if isinstance(a, dict) and a.get("score_display") not in (None, "", "—")
            }
            newly_graded_note = ""
            if incoming_assignments:
                for a in incoming_assignments:
                    if not isinstance(a, dict):
                        continue
                    sd = a.get("score_display")
                    ak = f"{a.get('assignment_id')}::{sd}"
                    if sd not in (None, "", "—") and ak not in prev_scored_keys:
                        pct_part = f" ({a['score_percent']}%)" if a.get("score_percent") is not None else ""
                        newly_graded_note = f" — Latest: {a.get('title')}: {sd}{pct_part}"
                        break

            had_prior_grade = (old_score is not None) or bool(old_letter)
            if incoming_has_grade and (score_changed or letter_changed):
                delta: Optional[float] = None
                if old_score is not None and new_score is not None:
                    delta = round(new_score - old_score, 2)

                old_disp = _format_grade_display(old_letter, old_score)
                new_disp = _format_grade_display(new_letter, new_score)

                if had_prior_grade:
                    row.prev_letter_grade = old_letter or None
                    row.prev_score_percent = old_score
                    row.delta_percent = delta

                history.append(
                    {
                        "ts": now_iso,
                        "score_percent": new_score,
                        "letter_grade": new_letter,
                        "term": term,
                    }
                )
                history = history[-40:]

                if had_prior_grade:
                    direction = "rose" if (delta or 0) > 0 else ("dropped" if (delta or 0) < 0 else "changed")
                    delta_str = f" ({'+' if delta and delta > 0 else ''}{delta:.2f}%)" if delta is not None else ""
                    summary = f"{cname} {direction} from {old_disp} to {new_disp}{delta_str}{newly_graded_note}"

                    log_entry = GradeChangeLog(
                        source_app=clean_app,
                        course_name=cname,
                        old_display=old_disp,
                        new_display=new_disp,
                        delta_percent=delta,
                        summary=summary[:500],
                        detected_at=now,
                    )
                    session.add(log_entry)

                    if should_emit:
                        notif = Notification(
                            source_app=clean_app,
                            title=f"Grade Updated: {cname} → {new_disp}",
                            message=summary,
                            ai_summary=summary,
                            indicator_type="grade_change",
                            priority="high",
                            category="Academic",
                            ai_processed=True,
                            read_state=False,
                            is_important=True,
                            importance_score=0.95,
                            importance_reason="grade_change",
                            timestamp=now,
                        )
                        session.add(notif)

                    changes.append(
                        {
                            "course_name": cname,
                            "old_display": old_disp,
                            "new_display": new_disp,
                            "delta_percent": delta,
                            "summary": summary,
                        }
                    )

            row.course_name = cname
            if teacher:
                row.teacher = teacher
            if period:
                row.period = period
            row.term = term
            if incoming_has_grade:
                row.letter_grade = new_letter
                row.score_percent = new_score
            if scores_url:
                row.scores_url = scores_url
            if term_grades:
                merged_terms = _safe_json_loads(row.term_grades_json, {})
                merged_terms.update(term_grades)
                row.term_grades_json = json.dumps(merged_terms)
            if incoming_assignments is not None and (len(incoming_assignments) > 0 or not prev_assignments):
                row.assignments_json = json.dumps(incoming_assignments)
                recent_titles = [
                    a.get("title") for a in incoming_assignments[:5] if isinstance(a, dict) and a.get("title")
                ]
                row.recent_items_json = json.dumps(recent_titles)
            row.history_json = json.dumps(history[-40:])
            row.missing_count = missing_count
            row.updated_at = now
            session.add(row)
            updated_rows.append(row)

        session.commit()
        for r in updated_rows:
            session.refresh(r)

        return {
            "source_app": clean_app,
            "courses_scanned": len(updated_rows),
            "changes_detected": changes,
        }

    def get_dashboard(self, session: Session) -> Dict[str, Any]:
        """Return all remembered courses (with history, trends & assignments), GPA summary, and change logs."""
        raw_courses = list(
            session.exec(select(CourseGrade).order_by(CourseGrade.period.asc(), CourseGrade.course_name.asc())).all()
        )
        courses: List[CourseGrade] = []
        pruned_bogus = False
        for c in raw_courses:
            if (c.course_name or "").strip().lower() in ("not available", "unavailable", "n/a"):
                session.delete(c)
                pruned_bogus = True
            else:
                courses.append(c)
        if pruned_bogus:
            session.commit()
        changes = list(
            session.exec(select(GradeChangeLog).order_by(GradeChangeLog.detected_at.desc()).limit(30)).all()
        )

        pct_vals = [c.score_percent for c in courses if c.score_percent is not None]
        gpa_vals = [
            pts
            for pts in (_letter_to_gpa_points(c.letter_grade, c.score_percent) for c in courses)
            if pts is not None
        ]

        avg_pct = round(sum(pct_vals) / len(pct_vals), 2) if pct_vals else None
        unweighted_gpa = round(sum(gpa_vals) / len(gpa_vals), 2) if gpa_vals else None
        last_checked = max((c.updated_at for c in courses), default=None)

        serialized_courses = [self._serialize_course(c) for c in courses]
        total_assignments = sum(c["assignments_count"] for c in serialized_courses)
        total_missing = sum(int(c.get("missing_count") or 0) for c in serialized_courses)

        return {
            "courses": serialized_courses,
            "recent_changes": [
                {
                    "id": ch.id,
                    "source_app": ch.source_app,
                    "course_name": ch.course_name,
                    "old_display": ch.old_display,
                    "new_display": ch.new_display,
                    "delta_percent": ch.delta_percent,
                    "summary": ch.summary,
                    "detected_at": ch.detected_at.isoformat() if ch.detected_at else None,
                }
                for ch in changes
            ],
            "summary": {
                "total_courses": len(courses),
                "average_percent": avg_pct,
                "unweighted_gpa": unweighted_gpa,
                "total_assignments": total_assignments,
                "total_missing": total_missing,
                "last_checked_at": last_checked.isoformat() if last_checked else None,
            },
        }

    @staticmethod
    def _serialize_course(c: CourseGrade) -> Dict[str, Any]:
        history = _safe_json_loads(c.history_json, [])
        term_grades = _safe_json_loads(c.term_grades_json, {})
        assignments = _safe_json_loads(c.assignments_json, [])

        delta = c.delta_percent
        if delta is None and c.score_percent is not None and c.prev_score_percent is not None:
            delta = round(c.score_percent - c.prev_score_percent, 2)

        trend = "steady"
        trend_label = ""
        has_prev = (c.prev_score_percent is not None) or bool(c.prev_letter_grade)
        if has_prev:
            prev_disp = _format_grade_display(c.prev_letter_grade, c.prev_score_percent)
            if delta is not None and abs(delta) >= 0.05:
                trend = "up" if delta > 0 else "down"
                arrow = "↑" if delta > 0 else "↓"
                sign = "+" if delta > 0 else ""
                trend_label = f"{arrow} {sign}{delta:.1f}% (was {prev_disp})"
            elif c.prev_letter_grade and c.letter_grade and c.prev_letter_grade != c.letter_grade:
                old_pts = _letter_to_gpa_points(c.prev_letter_grade, c.prev_score_percent) or 0.0
                new_pts = _letter_to_gpa_points(c.letter_grade, c.score_percent) or 0.0
                trend = "up" if new_pts >= old_pts else "down"
                arrow = "↑" if trend == "up" else "↓"
                trend_label = f"{arrow} was {prev_disp}"

        return {
            "id": c.id,
            "source_app": c.source_app,
            "course_key": c.course_key,
            "course_name": c.course_name,
            "teacher": c.teacher,
            "teacher_name": c.teacher,
            "period": c.period,
            "period_label": c.period,
            "term": c.term,
            "term_label": c.term,
            "letter_grade": c.letter_grade,
            "score_percent": c.score_percent,
            "prev_letter_grade": c.prev_letter_grade,
            "previous_letter_grade": c.prev_letter_grade,
            "prev_score_percent": c.prev_score_percent,
            "previous_score_percent": c.prev_score_percent,
            "delta_percent": delta,
            "trend": trend,
            "trend_label": trend_label,
            "missing_count": c.missing_count,
            "scores_url": c.scores_url,
            "history": history,
            "term_grades": term_grades,
            "assignments": assignments,
            "assignments_count": len(assignments),
            "display": _format_grade_display(c.letter_grade, c.score_percent),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }


grades_service = GradesService()
