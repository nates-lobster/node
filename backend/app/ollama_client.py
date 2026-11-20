import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .memory_store import memory_store
from .models import CalendarEvent, LongTermGoal, Notification, OllamaStatusInfo, Task


def _get_local_now() -> datetime:
    tz_name = os.environ.get("TZ") or settings.local_timezone or "America/New_York"
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone.utc)


def _extract_user_name_from_memory(memory_md: str) -> Optional[str]:
    m = re.search(
        r"(?:my name is|call me|preferred name\s*[:=-])\s*([A-Za-z][A-Za-z0-9 _-]{0,24})",
        memory_md,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().split("\n")[0].strip(" .,!")
    return None


def _time_of_day_greeting(name: Optional[str] = None) -> str:
    hour = _get_local_now().hour
    salutation = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
    return f"{salutation}, {name}." if name else f"{salutation}."


def _split_think_tags(raw_text: str) -> Tuple[str, Optional[str]]:
    """Extract `<think>...</think>` chain-of-thought blocks cleanly from model output."""
    if not raw_text:
        return "", None
    thoughts: List[str] = []
    for match in re.finditer(r"<think>(.*?)</think>", raw_text, flags=re.DOTALL | re.IGNORECASE):
        chunk = match.group(1).strip()
        if chunk:
            thoughts.append(chunk)
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
    return cleaned, ("\n\n".join(thoughts) if thoughts else None)


def _extract_tool_calls_from_text_and_msg(
    msg_obj: Dict[str, Any],
    raw_content: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Parse BOTH native Ollama `message.tool_calls` AND inline `<tool_call>{...}</tool_call>`
    or standalone JSON tool call objects emitted by Qwen 3.5 models.
    Returns `(cleaned_content, parsed_tool_calls)`.
    """
    parsed_calls: List[Dict[str, Any]] = []

    for tc in msg_obj.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        args = fn.get("arguments") if "arguments" in fn else tc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if name:
            parsed_calls.append({"name": str(name), "arguments": args if isinstance(args, dict) else {}})

    clean_content = raw_content or ""

    # 1. Parse `<tool_call>{...}</tool_call>` blocks
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", clean_content, flags=re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            tname = obj.get("name") or (obj.get("function") or {}).get("name")
            targs = obj.get("arguments") or obj.get("parameters") or (obj.get("function") or {}).get("arguments") or {}
            if isinstance(targs, str):
                targs = json.loads(targs)
            if tname:
                parsed_calls.append({"name": str(tname), "arguments": targs if isinstance(targs, dict) else {}})
        except Exception:
            pass
    clean_content = re.sub(r"<tool_call>.*?</tool_call>", "", clean_content, flags=re.DOTALL).strip()

    # 2. If no native calls yet, check if the model emitted a fenced or bare JSON tool call
    if not parsed_calls and clean_content:
        fenced = re.search(r"```(?:json)?\s*(\{\s*\"name\"\s*:\s*\"[a-z0-9_]+\".*?\})\s*```", clean_content, flags=re.DOTALL)
        candidate_json = fenced.group(1) if fenced else (
            clean_content if (clean_content.startswith("{") and clean_content.endswith("}") and '"name"' in clean_content) else None
        )
        if candidate_json:
            try:
                obj = json.loads(candidate_json)
                if obj.get("name"):
                    targs = obj.get("arguments") or obj.get("parameters") or {}
                    if isinstance(targs, str):
                        targs = json.loads(targs)
                    parsed_calls.append({"name": str(obj["name"]), "arguments": targs if isinstance(targs, dict) else {}})
                    if fenced:
                        clean_content = (clean_content[:fenced.start()] + clean_content[fenced.end():]).strip()
                    else:
                        clean_content = ""
            except Exception:
                pass

    return clean_content, parsed_calls


def compute_briefing_workload(
    notifications: List[Notification],
    tasks: List[Task],
    events_today: List[CalendarEvent],
    cutoff_hhmm: str = "22:00",
    wind_down_minutes: int = 15,
) -> Dict[str, Any]:
    """
    Compute realistic time math for the Desktop Briefing:
      - Remaining awake minutes before daily cutoff (minus wind-down buffer & remaining events)
      - Inbox triage load (`important` vs `FYI/promo` — e.g. 40 important pings ~ 70m vs 2 pings ~ 4m)
      - Critical / High / Due-Today task minutes vs remaining free time
    """
    now = _get_local_now()
    today_iso = now.date().isoformat()
    now_mins = now.hour * 60 + now.minute

    try:
        ch, cm = [int(x) for x in (cutoff_hhmm or "22:00")[:5].split(":")]
    except Exception:
        ch, cm = 22, 0
    cutoff_mins = ch * 60 + cm - max(0, int(wind_down_minutes or 0))
    if cutoff_mins <= now_mins:
        # Past evening cutoff — show minimal remaining window for tonight
        raw_awake_left = max(0, (24 * 60) - now_mins)
    else:
        raw_awake_left = cutoff_mins - now_mins

    # Subtract remaining non-completed scheduled calendar blocks today
    busy_event_mins = 0
    upcoming_events: List[CalendarEvent] = []
    for ev in events_today:
        if ev.completed or ev.all_day:
            continue
        try:
            sh, sm = [int(x) for x in ev.start_at[11:16].split(":")]
            eh, em = [int(x) for x in ev.end_at[11:16].split(":")]
            s_m = sh * 60 + sm
            e_m = max(s_m + 5, eh * 60 + em)
        except Exception:
            continue
        if e_m <= now_mins:
            continue
        upcoming_events.append(ev)
        overlap_start = max(now_mins, s_m)
        overlap_end = min(max(cutoff_mins, now_mins), e_m)
        if overlap_end > overlap_start:
            busy_event_mins += overlap_end - overlap_start

    free_minutes_left = max(0, raw_awake_left - busy_event_mins)

    important_notifs = [n for n in notifications if getattr(n, "is_important", True) and not n.read_state]
    fyi_notifs = [n for n in notifications if not getattr(n, "is_important", True) and not n.read_state]
    # Realistic inbox triage scaling: 2 important pings ~ 4m; 40 important pings ~ 65-80m!
    inbox_minutes = int(round(len(important_notifs) * 1.8 + len(fyi_notifs) * 0.25))

    prio_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    open_tasks = [t for t in tasks if not t.completed]
    sorted_tasks = sorted(
        open_tasks,
        key=lambda t: (
            prio_rank.get((t.priority or "normal").lower(), 2),
            0 if (t.due_date and t.due_date <= today_iso) else 1,
            getattr(t, "sort_order", 0) or 0,
            t.due_date or "9999-12-31",
        ),
    )

    urgent_tasks: List[Tuple[Task, int]] = []
    other_tasks: List[Tuple[Task, int]] = []
    for t in sorted_tasks:
        est = t.estimated_minutes or memory_store.estimate_task_minutes(t.title, t.category, t.source_app)[0]
        is_urg = (t.priority in ("critical", "high")) or Boolean_due_today(t.due_date, today_iso)
        if is_urg:
            urgent_tasks.append((t, est))
        else:
            other_tasks.append((t, est))

    urgent_task_mins = sum(m for _, m in urgent_tasks)
    total_task_mins = urgent_task_mins + sum(m for _, m in other_tasks)
    essential_demand = urgent_task_mins + inbox_minutes

    if essential_demand == 0 and not open_tasks:
        pressure = "clear"
    elif free_minutes_left > 0 and essential_demand > free_minutes_left:
        pressure = "overloaded"
    elif free_minutes_left > 0 and essential_demand > int(free_minutes_left * 0.72):
        pressure = "tight"
    else:
        pressure = "comfortable"

    return {
        "now_12h": now.strftime("%I:%M %p").lstrip("0"),
        "today_iso": today_iso,
        "free_minutes_left": free_minutes_left,
        "busy_event_minutes_left": busy_event_mins,
        "upcoming_events": upcoming_events,
        "important_unread_count": len(important_notifs),
        "fyi_unread_count": len(fyi_notifs),
        "inbox_triage_minutes": inbox_minutes,
        "sorted_tasks": sorted_tasks,
        "urgent_tasks": urgent_tasks,
        "urgent_task_minutes": urgent_task_mins,
        "total_task_minutes": total_task_mins,
        "essential_demand_minutes": essential_demand,
        "pressure": pressure,
    }


def Boolean_due_today(due_date: Optional[str], today_iso: str) -> bool:
    return bool(due_date and due_date <= today_iso)


def _fmt_mins(mins: int) -> str:
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if m else f"{h}h"


class OllamaClient:
    """
    Async client wrapper for local Ollama CPU inference (`qwen3.5:2b` + `qwen3.5:0.8b`)
    supporting real-time token/thinking streaming, reliable tool calling, and adaptive briefings.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.effective_chat_model
        self.timeout_seconds = timeout_seconds or settings.ollama_timeout_seconds

    async def check_status(self) -> OllamaStatusInfo:
        from .ai_providers import ai_provider_registry

        discovered = await ai_provider_registry.discover_all_models(force=False)
        target = await ai_provider_registry.resolve_target(role="chat")
        available = [d["model"] for d in discovered]

        if target:
            self.model = target["model"]
            detail = (
                f"Connected — {target['model']} via {target['provider_name']} "
                f"({len(available)} model{'s' if len(available) != 1 else ''} discovered)"
            )
            return OllamaStatusInfo(
                reachable=True,
                base_url=target.get("base_url") or self.base_url,
                configured_model=target["model"],
                model_installed=True,
                available_models=available,
                detail=detail,
            )

        return OllamaStatusInfo(
            reachable=False,
            base_url=self.base_url,
            configured_model=self.model or "auto",
            model_installed=False,
            available_models=[],
            detail="No local Ollama, remote Ollama, or Cloud API model online yet — using built-in Secretary Engine",
        )

    async def generate_with_thinking(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 800,
        enable_thinking: Optional[bool] = None,
        temperature: Optional[float] = None,
        model_override: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        from .ai_providers import ai_provider_registry

        use_think = settings.ollama_enable_thinking if enable_thinking is None else enable_thinking
        effective_predict = max(num_predict, 1536) if use_think else num_predict
        temp = temperature if temperature is not None else (0.70 if use_think else 0.50)

        try:
            res = await ai_provider_registry.chat_complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                role="chat",
                explicit_model=model_override,
                enable_thinking=bool(use_think),
                temperature=temp,
                max_tokens=effective_predict,
                timeout=self.timeout_seconds,
            )
            if not res.get("ok"):
                return None, None
            self.model = res.get("model") or self.model
            clean_content, tag_thinking = _split_think_tags((res.get("content") or "").strip())
            combined_thinking = (res.get("thinking") or "").strip() or tag_thinking

            if not clean_content and use_think:
                res2 = await ai_provider_registry.chat_complete(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    role="chat",
                    explicit_model=model_override,
                    enable_thinking=False,
                    temperature=0.50,
                    max_tokens=700,
                    timeout=self.timeout_seconds,
                )
                if res2.get("ok"):
                    clean_content, _ = _split_think_tags((res2.get("content") or "").strip())

            return (clean_content or None), combined_thinking
        except Exception:
            return None, None

    async def generate_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 750,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        answer, _thinking = await self.generate_with_thinking(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_predict=num_predict,
            enable_thinking=False,
            temperature=temperature,
        )
        return answer

    async def chat_json_or_text(
        self,
        system_prompt: str,
        user_prompt: str,
        enable_thinking: bool = False,
        num_predict: int = 600,
    ) -> Dict[str, Any]:
        content, thinking = await self.generate_with_thinking(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_predict=num_predict,
            enable_thinking=enable_thinking,
            temperature=0.25,
        )
        return {"content": content or "", "thinking": thinking}

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: Optional[bool] = None,
        num_predict: int = 1000,
    ) -> Optional[Dict[str, Any]]:
        """
        Non-streaming multi-turn `/api/chat` call supporting native `tools` AND `<tool_call>` parsing.
        NEVER drops `tools` from the request!
        """
        use_think = settings.ollama_enable_thinking if enable_thinking is None else enable_thinking
        chat_url = f"{self.base_url}/api/chat"
        effective_predict = max(num_predict, 1600) if use_think else num_predict
        options: Dict[str, Any] = {
            "num_ctx": settings.ollama_num_ctx,
            "num_thread": settings.ollama_num_threads,
            "temperature": 0.35 if tools else (0.75 if use_think else 0.6),
            "top_p": 0.90 if use_think else 0.85,
            "top_k": 20,
            "presence_penalty": 1.1,
            "repeat_penalty": 1.0,
            "num_predict": effective_predict,
        }
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "24h",
            "think": bool(use_think),
            "options": options,
        }
        if tools:
            body["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(chat_url, json=body)
                if resp.status_code == 400 and "think" in body:
                    # Keep `tools` intact — only omit `think` if Ollama build dislikes think+tools together
                    body.pop("think", None)
                    resp = await client.post(chat_url, json=body)
                if resp.status_code != 200:
                    return None

                data = resp.json()
                msg_obj = data.get("message") or {}
                raw_content = (msg_obj.get("content") or "").strip()
                native_thinking = (msg_obj.get("thinking") or "").strip() or None
                no_think_content, tag_thinking = _split_think_tags(raw_content)
                combined_thinking = native_thinking or tag_thinking

                clean_content, parsed_calls = _extract_tool_calls_from_text_and_msg(msg_obj, no_think_content)

                if not clean_content and not parsed_calls and use_think:
                    fast_body = {
                        **body,
                        "think": False,
                        "options": {**options, "num_predict": 700, "temperature": 0.35},
                    }
                    resp2 = await client.post(chat_url, json=fast_body)
                    if resp2.status_code == 200:
                        m2 = resp2.json().get("message") or {}
                        c2, _ = _split_think_tags((m2.get("content") or "").strip())
                        clean_content, parsed_calls = _extract_tool_calls_from_text_and_msg(m2, c2)

                return {
                    "content": clean_content,
                    "thinking": combined_thinking,
                    "tool_calls": parsed_calls,
                    "raw_message": msg_obj,
                }
        except Exception:
            return None

    async def stream_chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: Optional[bool] = None,
        num_predict: int = 1100,
        model_override: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Real-time streaming multi-provider agent generator yielding live deltas:
          - `{"type": "thinking_delta", "delta": "..."}`
          - `{"type": "content_delta", "delta": "..."}`
          - `{"type": "turn_complete", "content": "...", "thinking": "...", "tool_calls": [...], "model": "...", "provider": "..."}`
          - `{"type": "unreachable"}` if no configured provider/model is reachable.
        """
        from .ai_providers import ai_provider_registry

        target = await ai_provider_registry.resolve_target(role="chat", explicit_model=model_override)
        if not target:
            yield {"type": "unreachable"}
            return

        active_model = target["model"]
        active_provider = target["provider_name"]
        self.model = active_model
        use_think = settings.ollama_enable_thinking if enable_thinking is None else enable_thinking

        # Cloud API path (OpenAI, Anthropic, Gemini, OpenRouter, Groq, Custom OpenAI)
        if target.get("protocol") in ("openai", "anthropic"):
            try:
                async for ev in ai_provider_registry.stream_turn(
                    target=target,
                    messages=messages,
                    tools=tools,
                    enable_thinking=bool(use_think),
                    temperature=0.35 if tools else 0.65,
                    timeout=self.timeout_seconds,
                ):
                    et = ev.get("type")
                    if et == "thinking":
                        yield {"type": "thinking_delta", "delta": ev["delta"]}
                    elif et == "token":
                        yield {"type": "content_delta", "delta": ev["delta"]}
                    elif et == "turn_complete":
                        clean_content, parsed_calls = _extract_tool_calls_from_text_and_msg(
                            {"tool_calls": ev.get("tool_calls") or []},
                            ev.get("content") or "",
                        )
                        yield {
                            "type": "turn_complete",
                            "content": clean_content,
                            "thinking": ev.get("thinking") or None,
                            "tool_calls": parsed_calls,
                            "model": active_model,
                            "provider": active_provider,
                        }
                return
            except Exception:
                yield {"type": "unreachable"}
                return

        # Local or Remote Ollama Server path (dynamically adapts `think` & `tools` per model!)
        chat_url = f"{target['base_url']}/api/chat"
        supports_think = ai_provider_registry.model_supports_thinking(active_model)
        send_think = bool(use_think and supports_think)
        effective_predict = max(num_predict, 1600) if send_think else num_predict
        options: Dict[str, Any] = {
            "num_ctx": settings.ollama_num_ctx,
            "num_thread": settings.ollama_num_threads,
            "temperature": 0.35 if tools else (0.75 if send_think else 0.6),
            "top_p": 0.90 if send_think else 0.85,
            "top_k": 20,
            "presence_penalty": 1.1,
            "repeat_penalty": 1.0,
            "num_predict": effective_predict,
        }
        body: Dict[str, Any] = {
            "model": active_model,
            "messages": messages,
            "stream": True,
            "keep_alive": "24h",
            "options": options,
        }
        if supports_think:
            body["think"] = bool(send_think)
        if tools:
            body["tools"] = tools

        acc_thinking: List[str] = []
        acc_content: List[str] = []
        raw_tool_calls: List[Dict[str, Any]] = []
        in_inline_think = False
        inline_buf = ""

        try:
            timeout_cfg = httpx.Timeout(self.timeout_seconds, connect=3.5)
            async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                req_ctx = client.stream("POST", chat_url, json=body)
                resp = await req_ctx.__aenter__()
                try:
                    if resp.status_code == 400 and "think" in body:
                        await req_ctx.__aexit__(None, None, None)
                        ai_provider_registry.record_model_capability(active_model, "supports_thinking", False)
                        body.pop("think", None)
                        req_ctx = client.stream("POST", chat_url, json=body)
                        resp = await req_ctx.__aenter__()

                    if resp.status_code == 400 and "tools" in body:
                        await req_ctx.__aexit__(None, None, None)
                        ai_provider_registry.record_model_capability(active_model, "supports_tools", False)
                        body.pop("tools", None)
                        req_ctx = client.stream("POST", chat_url, json=body)
                        resp = await req_ctx.__aenter__()

                    if resp.status_code != 200:
                        yield {"type": "unreachable"}
                        return

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue

                        msg_part = chunk.get("message") or {}
                        t_delta = msg_part.get("thinking") or ""
                        c_delta = msg_part.get("content") or ""
                        t_calls = msg_part.get("tool_calls") or []

                        if t_calls:
                            raw_tool_calls.extend(t_calls)

                        if t_delta:
                            acc_thinking.append(t_delta)
                            yield {"type": "thinking_delta", "delta": t_delta}

                        if c_delta:
                            # Handle inline <think>...</think> tags cleanly during streaming
                            inline_buf += c_delta
                            while inline_buf:
                                if not in_inline_think:
                                    idx = inline_buf.find("<think>")
                                    if idx == -1:
                                        # Keep trailing `<` buffer in case `<think>` spans chunks
                                        partial = 0
                                        for k in range(1, min(7, len(inline_buf) + 1)):
                                            if "<think>".startswith(inline_buf[-k:]):
                                                partial = k
                                        emit = inline_buf[:-partial] if partial else inline_buf
                                        inline_buf = inline_buf[-partial:] if partial else ""
                                        if emit:
                                            acc_content.append(emit)
                                            # Suppress streaming raw `<tool_call>` XML to user bubble
                                            if "<tool_call>" not in "".join(acc_content):
                                                yield {"type": "content_delta", "delta": emit}
                                        break
                                    else:
                                        before = inline_buf[:idx]
                                        if before:
                                            acc_content.append(before)
                                            yield {"type": "content_delta", "delta": before}
                                        inline_buf = inline_buf[idx + 7:]
                                        in_inline_think = True
                                else:
                                    end_idx = inline_buf.find("</think>")
                                    if end_idx == -1:
                                        partial = 0
                                        for k in range(1, min(8, len(inline_buf) + 1)):
                                            if "</think>".startswith(inline_buf[-k:]):
                                                partial = k
                                        emit_t = inline_buf[:-partial] if partial else inline_buf
                                        inline_buf = inline_buf[-partial:] if partial else ""
                                        if emit_t:
                                            acc_thinking.append(emit_t)
                                            yield {"type": "thinking_delta", "delta": emit_t}
                                        break
                                    else:
                                        before_t = inline_buf[:end_idx]
                                        if before_t:
                                            acc_thinking.append(before_t)
                                            yield {"type": "thinking_delta", "delta": before_t}
                                        inline_buf = inline_buf[end_idx + 8:]
                                        in_inline_think = False

                        if chunk.get("done"):
                            break
                finally:
                    await req_ctx.__aexit__(None, None, None)

            if inline_buf:
                if in_inline_think:
                    acc_thinking.append(inline_buf)
                    yield {"type": "thinking_delta", "delta": inline_buf}
                else:
                    acc_content.append(inline_buf)
                    if "<tool_call>" not in "".join(acc_content):
                        yield {"type": "content_delta", "delta": inline_buf}

            full_content = "".join(acc_content).strip()
            full_thinking = "".join(acc_thinking).strip() or None
            clean_content, parsed_calls = _extract_tool_calls_from_text_and_msg(
                {"tool_calls": raw_tool_calls},
                full_content,
            )

            yield {
                "type": "turn_complete",
                "content": clean_content,
                "thinking": full_thinking,
                "tool_calls": parsed_calls,
            }
        except Exception:
            yield {"type": "unreachable"}

    def build_structured_cards(
        self,
        notifications: List[Notification],
        tasks: List[Task],
        memory_md: str,
        events_today: Optional[List[CalendarEvent]] = None,
        active_goals: Optional[List[LongTermGoal]] = None,
        focus_note: Optional[str] = None,
        cutoff_hhmm: str = "22:00",
        wind_down_minutes: int = 15,
        recent_grade_changes: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        events_today = events_today or []
        active_goals = active_goals or []
        recent_grade_changes = recent_grade_changes or []

        wl = compute_briefing_workload(
            notifications=notifications,
            tasks=tasks,
            events_today=events_today,
            cutoff_hhmm=cutoff_hhmm,
            wind_down_minutes=wind_down_minutes,
        )

        user_name = _extract_user_name_from_memory(memory_md)
        hello = _time_of_day_greeting(user_name)
        free_str = _fmt_mins(wl["free_minutes_left"])
        imp_cnt = wl["important_unread_count"]
        fyi_cnt = wl["fyi_unread_count"]
        inbox_m = wl["inbox_triage_minutes"]
        urg_tasks = wl["urgent_tasks"]
        upcoming_evs = wl["upcoming_events"]

        # Adaptive, situation-aware fallback greeting (replaced by live LLM prose when Ollama is online!)
        segments: List[str] = [hello]
        if wl["pressure"] == "clear" and imp_cnt == 0 and not upcoming_evs:
            segments.append(f"Your schedule is clear with {free_str} of open time ahead today—no urgent tasks or important inbox pings waiting.")
        else:
            if wl["pressure"] == "overloaded":
                segments.append(
                    f"Heads up: you have ~{_fmt_mins(wl['essential_demand_minutes'])} of priority work & triage vs {free_str} of open time left today, so focus strictly on critical items first."
                )
            elif wl["pressure"] == "tight":
                segments.append(
                    f"Tight window ahead: ~{_fmt_mins(wl['essential_demand_minutes'])} of priority work across {free_str} of open time today."
                )
            else:
                segments.append(f"You have {free_str} of open time remaining today.")

            if upcoming_evs:
                nxt = upcoming_evs[0]
                segments.append(f"Next on your calendar is {nxt.title} at {nxt.start_at[11:16]}.")

            if urg_tasks:
                top_t, top_m = urg_tasks[0]
                more = f" (+{len(urg_tasks) - 1} more priority)" if len(urg_tasks) > 1 else ""
                segments.append(f"Top task: \"{top_t.title}\" (~{top_m}m{more}).")
            elif wl["sorted_tasks"]:
                top_t = wl["sorted_tasks"][0]
                est_m = top_t.estimated_minutes or 25
                segments.append(f"Up next on your task list: \"{top_t.title}\" (~{est_m}m).")

            if imp_cnt >= 15:
                segments.append(
                    f"Your Inbox is heavy with {imp_cnt} important notifications (~{_fmt_mins(inbox_m)} to triage)—batch-clear high-priority senders after your top task."
                )
            elif imp_cnt > 0:
                segments.append(
                    f"{imp_cnt} important Inbox message{'s' if imp_cnt != 1 else ''} (~{_fmt_mins(max(2, inbox_m))}) need a quick look"
                    + (f", while {fyi_cnt} low-priority FYI item{'s' if fyi_cnt != 1 else ''} can wait." if fyi_cnt else ".")
                )

            if recent_grade_changes:
                gc = recent_grade_changes[0]
                cname = getattr(gc, "course_name", None) or (gc.get("course_name") if isinstance(gc, dict) else "")
                ndisp = getattr(gc, "new_display", None) or (gc.get("new_display") if isinstance(gc, dict) else "")
                if cname:
                    segments.append(f"Recent grade update in {cname} ({ndisp}).")

        if focus_note and focus_note.strip():
            segments.append(f"Note: \"{focus_note.strip()}\".")

        summary_line = " ".join(segments)

        focus_cards: List[Dict[str, Any]] = []
        for t, est_m in urg_tasks[:2]:
            focus_cards.append(
                {
                    "kind": "task",
                    "badge": f"{(t.priority or 'high').capitalize()} • ~{est_m}m",
                    "urgency": t.priority or "high",
                    "title": t.title,
                    "subtitle": f"{t.category} • Due {t.due_date or 'soon'}",
                    "action_label": "Open Tasks",
                    "target_view": "tasks",
                    "target_filter": "focus",
                }
            )
        if imp_cnt > 0 and len(focus_cards) < 3:
            focus_cards.append(
                {
                    "kind": "notification",
                    "badge": f"{imp_cnt} Important • ~{_fmt_mins(max(2, inbox_m))}",
                    "urgency": "high" if imp_cnt >= 10 else "normal",
                    "title": f"{imp_cnt} actionable Inbox notification{'s' if imp_cnt != 1 else ''}",
                    "subtitle": f"{fyi_cnt} low-priority/promo filtered out",
                    "action_label": "Triage Inbox",
                    "target_view": "inbox",
                    "target_filter": "",
                }
            )

        day_blocks = [
            {
                "period": "Open Time Remaining Today",
                "duration": free_str,
                "headline": f"Workload status: {wl['pressure'].upper()}",
                "note": f"{_fmt_mins(wl['urgent_task_minutes'])} priority tasks + ~{_fmt_mins(inbox_m)} inbox triage.",
            }
        ]

        return {
            "greeting": summary_line,
            "workload": {
                "free_minutes_left": wl["free_minutes_left"],
                "important_unread_count": imp_cnt,
                "fyi_unread_count": fyi_cnt,
                "inbox_triage_minutes": inbox_m,
                "urgent_task_minutes": wl["urgent_task_minutes"],
                "total_task_minutes": wl["total_task_minutes"],
                "pressure": wl["pressure"],
            },
            "focus_note": focus_note.strip() if focus_note else None,
            "focus_cards": focus_cards,
            "day_blocks": day_blocks,
            "quick_wins": [],
        }

    async def generate_daily_briefing(
        self,
        notifications: List[Notification],
        tasks: List[Task],
        events_today: Optional[List[CalendarEvent]] = None,
        active_goals: Optional[List[LongTermGoal]] = None,
        focus_note: Optional[str] = None,
        cutoff_hhmm: str = "22:00",
        wind_down_minutes: int = 15,
        recent_grade_changes: Optional[List[Any]] = None,
    ) -> Tuple[str, Dict[str, Any], str, bool, int, Optional[str]]:
        started = time.perf_counter()
        events_today = events_today or []
        active_goals = active_goals or []
        recent_grade_changes = recent_grade_changes or []

        memory_doc = memory_store.get_memory()["content"]
        cards = self.build_structured_cards(
            notifications=notifications,
            tasks=tasks,
            memory_md=memory_doc,
            events_today=events_today,
            active_goals=active_goals,
            focus_note=focus_note,
            cutoff_hhmm=cutoff_hhmm,
            wind_down_minutes=wind_down_minutes,
            recent_grade_changes=recent_grade_changes,
        )
        wl = cards["workload"]
        now = _get_local_now()

        imp_notifs = [n for n in notifications if getattr(n, "is_important", True) and not n.read_state]
        cal_lines = [
            f"- {e.start_at[11:16]}–{e.end_at[11:16]} {e.title} ({e.category})"
            for e in events_today
            if not e.completed
        ][:8] or ["- No fixed events left today."]

        task_lines = []
        for t in tasks[:10]:
            if t.completed:
                continue
            est = t.estimated_minutes or memory_store.estimate_task_minutes(t.title, t.category, t.source_app)[0]
            task_lines.append(
                f"- [{(t.priority or 'normal').upper()}, ~{est}m, due {t.due_date or 'flexible'}] {t.title}"
            )

        notif_sample = [
            f"- [{n.source_app}] {n.title}" + (f" ({n.ai_summary[:80]})" if n.ai_summary else "")
            for n in imp_notifs[:6]
        ] or ["- Zero important unread notifications."]

        grade_lines = []
        for gc in recent_grade_changes[:3]:
            cname = getattr(gc, "course_name", "") or (gc.get("course_name", "") if isinstance(gc, dict) else "")
            odisp = getattr(gc, "old_display", "") or (gc.get("old_display", "") if isinstance(gc, dict) else "")
            ndisp = getattr(gc, "new_display", "") or (gc.get("new_display", "") if isinstance(gc, dict) else "")
            if cname:
                grade_lines.append(f"- {cname}: {odisp} -> {ndisp}")

        system_prompt = (
            "You are Node's sharp, realistic executive AI briefing engine. "
            "Write a natural, direct, zero-fluff briefing grounded strictly in the user's real numbers:\n"
            "1. FIRST PARAGRAPH (2-3 sentences max, plain text, no heading): Summarize what matters most RIGHT NOW "
            "given the remaining free time today, upcoming calendar events, critical/high tasks, and realistic Inbox load "
            "(e.g. 40 important notifications takes ~70m and needs batching, whereas 2 takes ~4m; if priority work exceeds "
            "remaining free time, explicitly call out what to tackle first and what to defer).\n"
            "2. Followed by 2-4 crisp bullet points ordering the exact recommended sequence for the rest of today.\n"
            "NEVER invent tasks, events, or notifications that aren't listed below."
        )

        user_prompt = (
            f"Current Local Time: {now.strftime('%A %Y-%m-%d %I:%M %p')}\n"
            f"Remaining Free Time Today (before {cutoff_hhmm} cutoff): {_fmt_mins(wl['free_minutes_left'])}\n"
            f"Workload Pressure: {wl['pressure'].upper()} "
            f"(Priority tasks: {_fmt_mins(wl['urgent_task_minutes'])}, Total open tasks: {_fmt_mins(wl['total_task_minutes'])}, "
            f"Inbox triage: ~{_fmt_mins(wl['inbox_triage_minutes'])} across {wl['important_unread_count']} important & {wl['fyi_unread_count']} low-priority unread)\n"
            f"User Focus Note: {focus_note or 'None'}\n\n"
            f"Today's Calendar:\n" + "\n".join(cal_lines) + "\n\n"
            f"Open Tasks:\n" + ("\n".join(task_lines) or "- No open tasks.") + "\n\n"
            f"Important Unread Inbox Sample:\n" + "\n".join(notif_sample)
            + ("\n\nRecent Grade Changes:\n" + "\n".join(grade_lines) if grade_lines else "")
        )

        # Use think=False for fast, punchy briefing generation (~1.5-4s on Haswell CPU!)
        llm_text, _thinking = await self.generate_with_thinking(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_predict=420,
            enable_thinking=False,
            temperature=0.45,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if llm_text:
            paragraphs = [
                re.sub(r"\*\*([^*]+)\*\*", r"\1", p.strip())
                for p in re.split(r"\n\s*\n", llm_text.strip())
                if p.strip() and not p.strip().startswith("#") and not p.strip().startswith("-")
            ]
            if paragraphs and len(paragraphs[0]) >= 24:
                cards["greeting"] = re.sub(r"\s+", " ", paragraphs[0]).strip()
            else:
                first_ln = next(
                    (ln.strip(" -*#") for ln in llm_text.splitlines() if len(ln.strip()) >= 24),
                    None,
                )
                if first_ln:
                    cards["greeting"] = first_ln
            return llm_text, cards, "ollama", True, elapsed_ms, None

        return (
            cards["greeting"],
            cards,
            "secretary_engine",
            False,
            elapsed_ms,
            f"Ollama ({self.model}) not connected at {self.base_url}",
        )


ollama_client = OllamaClient()
