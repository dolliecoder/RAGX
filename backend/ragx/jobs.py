"""Durable background jobs with checkpoints (resume, not restart, after a crash)."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Job

log = logging.getLogger("ragx.jobs")

MAX_ATTEMPTS = 3
Handler = Callable[[str], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def load_state(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return dict(job.state or {}) if job else {}


_ckpt_lock = threading.Lock()


def checkpoint(job_id: str, **updates: Any) -> dict[str, Any]:
    """Persist partial progress; a resumed job continues from here."""
    with _ckpt_lock, session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return {}
        state = dict(job.state or {})
        state.update(updates)
        job.state = state
        return state


class JobRunner:
    def __init__(self, workers: int = 2, inline: bool = False):
        self.inline = inline
        self.pool = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ragx-job")
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active:
                return
            self._active.add(job_id)
        if self.inline:
            self._run(job_id)
        else:
            assert self.pool is not None
            self.pool.submit(self._run, job_id)

    def submit_after_commit(self, session: Session, job_id: str) -> None:
        """Start the job only once the transaction that created it is committed."""
        event.listen(session, "after_commit", lambda _s: self.submit(job_id), once=True)

    def _run(self, job_id: str) -> None:
        try:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is None or job.status == "done":
                    return
                if job.attempts >= MAX_ATTEMPTS:
                    job.status = "failed"
                    job.error = (job.error or "") + "\nmax attempts reached"
                    return
                job.status = "running"
                job.attempts += 1
                kind = job.kind
            fn = HANDLERS.get(kind)
            if fn is None:
                raise RuntimeError(f"no handler for job kind {kind}")
            result = fn(job_id)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is not None:
                    job.status = "done"
                    job.result = result or {}
                    job.error = None
        except Exception as e:  # noqa: BLE001 - recorded on the job
            log.error("job %s failed: %s", job_id, e)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
        finally:
            with self._lock:
                self._active.discard(job_id)

    def resume_incomplete(self) -> int:
        """Called at startup: re-run jobs interrupted by a crash or restart."""
        with session_scope() as s:
            ids = [
                j.id
                for j in s.scalars(select(Job).where(Job.status.in_(["queued", "running"]), Job.attempts < MAX_ATTEMPTS))
            ]
        for i in ids:
            self.submit(i)
        return len(ids)

    def shutdown(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)


_runner: JobRunner | None = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner()
    return _runner


def set_runner(r: JobRunner | None) -> None:
    global _runner
    _runner = r


def create_job(session: Session, kind: str, kb_id: str | None, payload: dict[str, Any]) -> Job:
    job = Job(kind=kind, kb_id=kb_id, input=payload)
    session.add(job)
    session.flush()
    get_runner().submit_after_commit(session, job.id)
    return job
