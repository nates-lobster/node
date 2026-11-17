import html
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
import httpx

from .config import settings


WEEKDAY_ALIASES: Dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "weds": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def build_14_day_calendar_table(now_dt: datetime) -> str:
    """
    Build an unmistakable 14-day ISO <-> Weekday reference table injected into every AI call
    so models never do faulty mental date arithmetic.
    """
    base: date = now_dt.date()
    lines = [
        f"CURRENT LOCAL TIME: {now_dt.strftime('%A, %B %d, %Y %H:%M:%S %Z')} (ISO Date: {base.isoformat()}, Weekday={WEEKDAY_NAMES[base.weekday()]})",
        "EXACT 14-DAY CALENDAR LOOKUP TABLE (ALWAYS copy ISO dates from this table — NEVER guess day offsets):",
    ]
    row_A: List[str] = []
    row_B: List[str] = []
    for offset in range(14):
        d = base + timedelta(days=offset)
        wname = WEEKDAY_NAMES[d.weekday()]
        if offset == 0:
            label = f"Today ({wname[:3]})"
        elif offset == 1:
            label = f"Tomorrow ({wname[:3]})"
        elif offset < 7:
            label = f"This {wname}"
        else:
            label = f"Next {wname}"
        entry = f"{label} = {d.isoformat()}"
        if offset < 7:
            row_A.append(entry)
        else:
            row_B.append(entry)
    lines.append("  • Days 0–6:  " + " | ".join(row_A))
    lines.append("  • Days 7–13: " + " | ".join(row_B))
    return "\n".join(lines)


def resolve_spoken_weekday_date(user_text: str, now_dt: datetime) -> Optional[Tuple[date, str]]:
    """
    If `user_text` unambiguously specifies ONE target weekday without an explicit numeric calendar date,
    return `(exact_target_date, weekday_name)`.
    """
    if not user_text:
        return None
    low = user_text.lower()

    # Do not override if the user explicitly typed an ISO date or Month + Day number (e.g., "Oct 27" or "10/27")
    if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", low):
        return None
    if re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b",
        low,
    ):
        return None
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", low):
        return None

    # Find weekday mentions in the prompt
    found_weekdays: List[Tuple[int, str]] = []
    for match in re.finditer(
        r"\b(next\s+week\s+|next\s+|this\s+|coming\s+|on\s+)?"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)\b",
        low,
    ):
        qualifier = (match.group(1) or "").strip()
        wd_tok = match.group(2)
        wd_idx = WEEKDAY_ALIASES.get(wd_tok)
        if wd_idx is not None:
            found_weekdays.append((wd_idx, qualifier))

    # Only snap when the user targeted a single distinct weekday (avoids recurring multi-day prompts)
    unique_wds = {w for w, _ in found_weekdays}
    if len(unique_wds) != 1:
        return None

    target_wd, qualifier = found_weekdays[0]
    today = now_dt.date()
    days_ahead = (target_wd - today.weekday()) % 7

    if "next week" in qualifier:
        if days_ahead == 0:
            days_ahead = 7
        elif today.weekday() + days_ahead < 7:
            days_ahead += 7
    elif qualifier == "next":
        if days_ahead == 0:
            days_ahead = 7
    else:
        # "this Wednesday", "on Wednesday", "Wednesday at 3pm"
        # If today IS Wednesday and user says "this Wednesday", keep today unless time already passed isn't known;
        # if today is NOT Wednesday, `days_ahead` is strictly 1..6 (e.g. Sun -> Wed = +3 days).
        pass

    exact_date = today + timedelta(days=days_ahead)
    return exact_date, WEEKDAY_NAMES[target_wd]


def verify_and_fix_weekday_date(
    user_text: str,
    start_iso: str,
    end_iso: Optional[str],
    now_dt: datetime,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Deterministic guardrail against LLM off-by-one weekday math (e.g. scheduling "this Wednesday" on Tuesday).
    Preserves exact time-of-day and event duration when snapping an off-by-1..3 day date to the true weekday.
    Returns `(fixed_start_iso, fixed_end_iso, correction_note_or_None)`.
    """
    resolved = resolve_spoken_weekday_date(user_text, now_dt)
    if not resolved or not start_iso or len(start_iso) < 10:
        return start_iso, end_iso, None

    target_date, target_wname = resolved
    try:
        proposed_date = date.fromisoformat(start_iso[:10])
    except Exception:
        return start_iso, end_iso, None

    if proposed_date.weekday() == target_date.weekday():
        return start_iso, end_iso, None

    day_diff = (target_date - proposed_date).days
    if abs(day_diff) > 4:
        return start_iso, end_iso, None

    old_wname = WEEKDAY_NAMES[proposed_date.weekday()]
    fixed_start = target_date.isoformat() + start_iso[10:]
    fixed_end = end_iso
    if end_iso and len(end_iso) >= 10:
        try:
            end_d = date.fromisoformat(end_iso[:10]) + timedelta(days=day_diff)
            fixed_end = end_d.isoformat() + end_iso[10:]
        except Exception:
            fixed_end = target_date.isoformat() + end_iso[10:]

    note = (
        f"Verified calendar weekday: snapped {start_iso[:10]} ({old_wname}) -> "
        f"{target_date.isoformat()} ({target_wname})."
    )
    return fixed_start, fixed_end, note


# ---------------------------------------------------------------------------
# Privacy-First Web Search & Clean Webpage Reader
# ---------------------------------------------------------------------------
class _DDGResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[Dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._cur: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        amap = dict(attrs)
        cls = amap.get("class") or ""
        if tag == "a" and "result__a" in cls:
            href = amap.get("href") or ""
            if "uddg=" in href:
                parsed = parse_qs(urlparse(href).query)
                if parsed.get("uddg"):
                    href = unquote(parsed["uddg"][0])
            self._cur = {"title": "", "url": href, "snippet": ""}
            self._in_title = True
        elif tag in ("a", "div", "td", "span") and ("result__snippet" in cls or "result-snippet" in cls):
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        elif self._in_snippet and tag in ("a", "div", "td", "span"):
            self._in_snippet = False
            if self._cur.get("title") and self._cur.get("url"):
                self.results.append(dict(self._cur))
                self._cur = {}

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._cur["title"] = (self._cur.get("title", "") + data).strip()
        elif self._in_snippet:
            self._cur["snippet"] = (self._cur.get("snippet", "") + data).strip()


def _strip_html_to_text(raw_html: str, max_chars: int = 6000) -> str:
    cleaned = re.sub(r"<(script|style|noscript|svg|header|footer|nav)[^>]*>[\s\S]*?</\1>", " ", raw_html, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


async def search_web(query: str, max_results: int = 6, searxng_url: str = "") -> Dict[str, Any]:
    """
    Search the live web using:
      1. Optional self-hosted SearXNG JSON endpoint (if configured in AI Routing or `SEARXNG_URL`)
      2. DuckDuckGo HTML search (`html.duckduckgo.com/html/`)
      3. DuckDuckGo Instant Answer + Wikipedia OpenSearch fallback
    """
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": [], "source": "none"}

    limit = max(1, min(int(max_results or 6), 10))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    sx_url = (searxng_url or settings.searxng_url or "").strip().rstrip("/")
    if sx_url:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.get(f"{sx_url}/search", params={"q": q, "format": "json"}, headers=headers)
                if r.status_code == 200:
                    items = []
                    for row in (r.json().get("results") or [])[:limit]:
                        items.append(
                            {
                                "title": str(row.get("title") or ""),
                                "url": str(row.get("url") or ""),
                                "snippet": str(row.get("content") or ""),
                            }
                        )
                    if items:
                        return {"query": q, "results": items, "source": "searxng"}
        except Exception:
            pass

    results: List[Dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=9.0, follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": q},
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200 and resp.text:
                parser = _DDGResultParser()
                parser.feed(resp.text)
                results = parser.results[:limit]
    except Exception:
        pass

    if not results:
        try:
            async with httpx.AsyncClient(timeout=7.0, follow_redirects=True) as client:
                wiki = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": q,
                        "utf8": "1",
                        "format": "json",
                        "srlimit": str(limit),
                    },
                    headers=headers,
                )
                if wiki.status_code == 200:
                    for item in ((wiki.json().get("query") or {}).get("search") or [])[:limit]:
                        title = str(item.get("title") or "")
                        snip = _strip_html_to_text(str(item.get("snippet") or ""), 400)
                        results.append(
                            {
                                "title": f"{title} — Wikipedia",
                                "url": f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}",
                                "snippet": snip,
                            }
                        )
        except Exception:
            pass

    return {
        "query": q,
        "results": results[:limit],
        "count": len(results[:limit]),
        "source": "duckduckgo" if results else "fallback",
    }


async def fetch_webpage_text(url: str, max_chars: int = 5000) -> Dict[str, Any]:
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return {"ok": False, "url": target, "error": "URL must start with http:// or https://"}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(target, headers=headers)
            title_m = re.search(r"<title[^>]*>([\s\S]*?)</title>", resp.text or "", flags=re.I)
            title = _strip_html_to_text(title_m.group(1), 200) if title_m else target
            text = _strip_html_to_text(resp.text or "", max_chars=max_chars)
            return {
                "ok": resp.status_code < 400,
                "status_code": resp.status_code,
                "url": str(resp.url),
                "title": title,
                "content": text,
            }
    except Exception as exc:
        return {"ok": False, "url": target, "error": f"{exc.__class__.__name__}: {exc}"}
