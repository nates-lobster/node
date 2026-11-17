import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional
from sqlmodel import Session, select

from .database import engine
from .models import AIJob, ChatMessage, ChatThread
from .secretary import secretary_service

logger = logging.getLogger("node.ai_runtime")


class AIRuntimeManager:
    """
    100% Server-Side AI Job Queue & Execution Engine.
    - Detaches all LLM generation & tool execution from HTTP request lifetimes so closing
      the browser or losing Wi-Fi NEVER causes a lost prompt or aborted generation.
    - Supports firing multiple prompts back-to-back: prompts within the same `thread_id`
      execute sequentially in strict FIFO order so each sees prior tool mutations & context,
      while prompts across distinct `thread_id` contexts run concurrently in parallel.
    - Supports live NDJSON subscriber fan-out (`stream_job`) AND stateless polling (`list_jobs`).
    """

    def __init__(self) -> None:
        self._thread_locks: Dict[str, asyncio.Lock] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._event_history: Dict[str, List[Dict[str, Any]]] = {}

    def _get_thread_lock(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._thread_locks:
            self._thread_locks[thread_id] = asyncio.Lock()
        return self._thread_locks[thread_id]

    def serialize_job(self, job: AIJob) -> Dict[str, Any]:
        try:
            actions = json.loads(job.actions_json or "[]")
        except Exception:
            actions = []
        return {
            "id": job.id,
            "job_uid": job.job_uid,
            "job_type": job.job_type,
            "thread_id": job.thread_id,
            "prompt": job.prompt,
            "requested_model": job.requested_model,
            "model_used": job.model_used,
            "provider_used": job.provider_used,
            "status": job.status,
            "thinking_text": job.thinking_text or "",
            "reply_text": job.reply_text or "",
            "actions": actions,
            "error_text": job.error_text,
            "user_message_id": job.user_message_id,
            "assistant_message_id": job.assistant_message_id,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }

    def reconcile_stale_jobs_on_boot(self) -> None:
        """Mark any job left in `queued`/`running` from a previous container restart as interrupted."""
        try:
            with Session(engine) as session:
                stale = list(
                    session.exec(
                        select(AIJob).where(AIJob.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
                    ).all()
                )
                now = datetime.now(timezone.utc)
                for j in stale:
                    j.status = "failed"
                    j.error_text = "Server restarted before background job completed."
                    j.updated_at = now
                    j.completed_at = now
                    session.add(j)
                if stale:
                    session.commit()
        except Exception:
            pass

    def _broadcast(self, job_uid: str, event: Dict[str, Any]) -> None:
        hist = self._event_history.setdefault(job_uid, [])
        hist.append(event)
        if len(hist) > 400:
            del hist[:100]
        for q in list(self._subscribers.get(job_uid, [])):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def submit_chat_job(
        self,
        *,
        prompt: str,
        thread_id: str = "main",
        current_view: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        requested_model: Optional[str] = None,
        client_host: Optional[str] = None,
        forwarded_for: Optional[str] = None,
        request_host: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_thread = (thread_id or "main").strip()[:80] or "main"
        clean_prompt = prompt.strip()
        job_uid = f"job-{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)

        with Session(engine) as session:
            user_row = ChatMessage(
                thread_id=clean_thread,
                role="user",
                content=clean_prompt,
                source="user",
                actions_json="[]",
            )
            session.add(user_row)

            th = session.get(ChatThread, clean_thread)
            if not th:
                existing_names = {t.name for t in session.exec(select(ChatThread)).all()}
                n = 1
                while f"Chat {n}" in existing_names:
                    n += 1
                th = ChatThread(
                    thread_id=clean_thread,
                    name="Chat 1" if clean_thread == "main" and "Chat 1" not in existing_names else f"Chat {n}",
                    created_at=now,
                    updated_at=now,
                )
            else:
                th.updated_at = now
            session.add(th)
            session.commit()
            session.refresh(user_row)

            # Detect implicit user frustration/correction signals for disciplined self-improvement
            try:
                from .self_improvement import detect_frustration_signal, self_improvement_service

                frust = detect_frustration_signal(clean_prompt)
                if frust:
                    self_improvement_service.record_feedback(
                        session,
                        rating="frustration",
                        message_id=user_row.id,
                        thread_id=clean_thread,
                        reason=frust,
                        user_prompt=clean_prompt,
                    )
            except Exception:
                pass

            job = AIJob(
                job_uid=job_uid,
                job_type="chat",
                thread_id=clean_thread,
                prompt=clean_prompt,
                current_view=current_view,
                enable_thinking=True if enable_thinking is None else bool(enable_thinking),
                requested_model=(requested_model or "").strip() or None,
                status="queued",
                user_message_id=user_row.id,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            session.refresh(job)

            user_msg_dict = {
                "id": user_row.id,
                "thread_id": clean_thread,
                "role": "user",
                "content": user_row.content,
                "actions": [],
                "thinking": None,
                "source": "user",
                "created_at": user_row.created_at.isoformat(),
            }
            job_dict = self.serialize_job(job)

        self._event_history[job_uid] = []
        task = asyncio.create_task(
            self._execute_chat_job(
                job_uid=job_uid,
                thread_id=clean_thread,
                prompt=clean_prompt,
                user_message_id=user_msg_dict["id"],
                enable_thinking=enable_thinking,
                requested_model=requested_model,
                client_host=client_host,
                forwarded_for=forwarded_for,
                request_host=request_host,
            )
        )
        self._active_tasks[job_uid] = task
        task.add_done_callback(lambda _t, uid=job_uid: self._active_tasks.pop(uid, None))

        return {
            "job": job_dict,
            "job_uid": job_uid,
            "user_message": user_msg_dict,
        }

    async def _execute_chat_job(
        self,
        *,
        job_uid: str,
        thread_id: str,
        prompt: str,
        user_message_id: int,
        enable_thinking: Optional[bool],
        requested_model: Optional[str],
        client_host: Optional[str],
        forwarded_for: Optional[str],
        request_host: Optional[str],
    ) -> None:
        lock = self._get_thread_lock(thread_id)
        async with lock:
            try:
                with Session(engine) as session:
                    job = session.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
                    if not job:
                        return
                    job.status = "running"
                    job.updated_at = datetime.now(timezone.utc)
                    session.add(job)
                    session.commit()

                self._broadcast(job_uid, {"type": "job_status", "job_uid": job_uid, "status": "running"})

                acc_think: List[str] = []
                acc_reply: List[str] = []
                last_flush = asyncio.get_event_loop().time()

                with Session(engine) as run_session:
                    user_row = run_session.get(ChatMessage, user_message_id)
                    async for ev in secretary_service.stream_chat(
                        user_message=prompt,
                        session=run_session,
                        thread_id=thread_id,
                        client_host=client_host,
                        forwarded_for=forwarded_for,
                        request_host=request_host,
                        enable_thinking=enable_thinking,
                        model_override=requested_model,
                        existing_user_row=user_row,
                    ):
                        ev_with_job = {**ev, "job_uid": job_uid}
                        etype = ev.get("type")

                        if etype == "thinking_delta":
                            acc_think.append(ev.get("delta") or "")
                        elif etype == "content_delta":
                            acc_reply.append(ev.get("delta") or "")
                        elif etype == "content_replace":
                            acc_reply = [ev.get("content") or ""]

                        now_mono = asyncio.get_event_loop().time()
                        if etype in ("tool_done", "content_replace") or (now_mono - last_flush) > 0.6:
                            last_flush = now_mono
                            self._persist_progress(
                                job_uid,
                                thinking="".join(acc_think),
                                reply="".join(acc_reply),
                            )

                        if etype == "done":
                            res = ev.get("result") or {}
                            asst = res.get("assistant_message") or {}
                            self._persist_completion(
                                job_uid=job_uid,
                                thinking=res.get("thinking") or "".join(acc_think),
                                reply=res.get("reply_markdown") or "".join(acc_reply),
                                actions=res.get("actions") or [],
                                assistant_message_id=asst.get("id"),
                                model_used=res.get("engine") or requested_model or "auto",
                            )

                        self._broadcast(job_uid, ev_with_job)

            except Exception as exc:
                logger.exception("Server-side AIJob %s failed: %s", job_uid, exc)
                now = datetime.now(timezone.utc)
                with Session(engine) as err_session:
                    job = err_session.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
                    if job:
                        job.status = "failed"
                        job.error_text = f"{exc.__class__.__name__}: {exc}"
                        job.updated_at = now
                        job.completed_at = now
                        err_session.add(job)
                        err_session.commit()
                self._broadcast(
                    job_uid,
                    {
                        "type": "error",
                        "job_uid": job_uid,
                        "detail": f"{exc.__class__.__name__}: {exc}",
                    },
                )
            finally:
                self._broadcast(job_uid, {"type": "_eof", "job_uid": job_uid})
                # Keep history briefly for late subscribers, then trim old entries
                if len(self._event_history) > 80:
                    oldest_uid = next(iter(self._event_history.keys()))
                    if oldest_uid != job_uid and oldest_uid not in self._active_tasks:
                        self._event_history.pop(oldest_uid, None)

    def _persist_progress(self, job_uid: str, *, thinking: str, reply: str) -> None:
        try:
            with Session(engine) as s:
                job = s.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
                if job and job.status == "running":
                    job.thinking_text = thinking
                    job.reply_text = reply
                    job.updated_at = datetime.now(timezone.utc)
                    s.add(job)
                    s.commit()
        except Exception:
            pass

    def _persist_completion(
        self,
        *,
        job_uid: str,
        thinking: str,
        reply: str,
        actions: List[Dict[str, Any]],
        assistant_message_id: Optional[int],
        model_used: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with Session(engine) as s:
            job = s.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
            if job:
                job.status = "completed"
                job.thinking_text = thinking or ""
                job.reply_text = reply or ""
                job.actions_json = json.dumps(actions or [])
                job.assistant_message_id = assistant_message_id
                job.model_used = model_used
                job.updated_at = now
                job.completed_at = now
                s.add(job)
                s.commit()

    async def stream_job_events(self, job_uid: str) -> AsyncIterator[Dict[str, Any]]:
        """
        Subscribe to a server-side job's live events (replaying buffered events first).
        Disconnecting from this generator NEVER cancels `_execute_chat_job`!
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(job_uid, []).append(q)
        try:
            past = list(self._event_history.get(job_uid, []))
            saw_eof = False
            for ev in past:
                if ev.get("type") == "_eof":
                    saw_eof = True
                    break
                yield ev

            if saw_eof:
                return

            # If job already finished before subscriber attached and buffer was evicted, emit synthetic done
            if job_uid not in self._active_tasks and not past:
                with Session(engine) as s:
                    job = s.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
                    if job and job.status in ("completed", "failed"):
                        yield {"type": "job_snapshot", "job": self.serialize_job(job)}
                        return

            while True:
                ev = await q.get()
                if ev.get("type") == "_eof":
                    break
                yield ev
        finally:
            subs = self._subscribers.get(job_uid, [])
            if q in subs:
                subs.remove(q)

    async def wait_for_job_result(self, job_uid: str, timeout: float = 240.0) -> Dict[str, Any]:
        done_result: Optional[Dict[str, Any]] = None
        async with asyncio.timeout(timeout):
            async for ev in self.stream_job_events(job_uid):
                if ev.get("type") == "done":
                    done_result = ev.get("result") or ev
                elif ev.get("type") == "error":
                    raise RuntimeError(ev.get("detail") or "AI job failed")
        if done_result is not None:
            return done_result
        with Session(engine) as s:
            job = s.exec(select(AIJob).where(AIJob.job_uid == job_uid)).first()
            if job:
                return self.serialize_job(job)
        raise RuntimeError("Job ended without result")

    def list_jobs(
        self,
        session: Session,
        *,
        thread_id: Optional[str] = None,
        active_only: bool = False,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        stmt = select(AIJob).order_by(AIJob.id.desc())
        if thread_id:
            stmt = stmt.where(AIJob.thread_id == thread_id)
        if active_only:
            stmt = stmt.where(AIJob.status.in_(["queued", "running"]))  # type: ignore[attr-defined]
        rows = session.exec(stmt.limit(max(1, min(limit, 100)))).all()
        return [self.serialize_job(r) for r in rows]


ai_runtime_manager = AIRuntimeManager()
