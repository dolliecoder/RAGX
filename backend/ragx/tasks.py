"""Job handlers for crawling/re-embedding and the background scheduler loops."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .ingestion.crawler import crawl_source
from .ingestion.pipeline import embedding_text
from .jobs import create_job, handler
from .llm import get_registry
from .models import Chunk, Document, Job, KnowledgeBase, Source, Trace

log = logging.getLogger("ragx.tasks")


@handler("crawl")
def crawl_job(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        sid, force = job.input["source_id"], bool(job.input.get("force"))
        src = s.get(Source, sid)
        if src is None:
            raise KeyError(f"source {sid} not found")
        rep = crawl_source(s, src, force=force)
        return rep.summary()


@handler("reembed")
def reembed_job(job_id: str) -> dict[str, Any]:
    """Re-embed chunks produced by a different embedding model (drift guard)."""
    emb = get_registry().embedder()
    with session_scope() as s:
        kb_id = s.get(Job, job_id).kb_id
        ids = s.scalars(
            select(Chunk.id).where(Chunk.kb_id == kb_id, Chunk.status.in_(["active", "staged"]), Chunk.embedding_model != emb.model_id)
        ).all()
    done = 0
    for i in range(0, len(ids), 64):
        with session_scope() as s:
            rows = s.execute(
                select(Chunk, Document.title).join(Document, Document.id == Chunk.doc_id).where(Chunk.id.in_(ids[i : i + 64]))
            ).all()
            vecs = emb.embed_documents(
                [embedding_text(t, c.section_path, (c.context + " " + " ".join(c.aliases or [])).strip(), c.text) for c, t in rows]
            )
            for (c, _), v in zip(rows, vecs):
                c.embedding = v
                c.embedding_model = emb.model_id
            done += len(rows)
    return {"reembedded": done, "model": emb.model_id}


class Scheduler:
    """Background loops: crawl sources, run the repair cycle, judge canaries."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        st = get_settings()
        for name, interval, fn in (
            ("crawl", st.crawl_interval, self._crawl_tick),
            ("repair", st.repair_interval, self._repair_tick),
            ("canary", st.canary_interval, self._canary_tick),
        ):
            if interval and interval > 0:
                t = threading.Thread(target=self._loop, args=(name, interval, fn), daemon=True, name=f"ragx-{name}")
                t.start()
                self.threads.append(t)

    def _loop(self, name: str, interval: int, fn) -> None:
        while not self.stop.wait(interval):
            try:
                fn()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("scheduler %s tick failed", name)

    def _busy(self, s, kind: str, kb_id: str | None) -> bool:
        q = select(Job).where(Job.kind == kind, Job.status.in_(["queued", "running"]))
        if kb_id:
            q = q.where(Job.kb_id == kb_id)
        return s.scalar(q.limit(1)) is not None

    def _crawl_tick(self) -> None:
        with session_scope() as s:
            for src in s.scalars(select(Source)):
                if not self._busy(s, "crawl", src.kb_id):
                    create_job(s, "crawl", src.kb_id, {"source_id": src.id, "force": False})

    def _repair_tick(self) -> None:
        with session_scope() as s:
            for kb in s.scalars(select(KnowledgeBase)):
                pending = s.scalar(
                    select(Trace.id).where(Trace.kb_id == kb.id, Trace.diagnosed.is_(False), Trace.is_eval.is_(False)).limit(1)
                )
                if pending and not self._busy(s, "repair", kb.id):
                    create_job(s, "repair", kb.id, {"trigger": "schedule"})

    def _canary_tick(self) -> None:
        from .repair.service import evaluate_canaries

        evaluate_canaries()

    def shutdown(self) -> None:
        self.stop.set()
