"""Glue/NavBoost-style signals: query clusters, explicit and implicit feedback."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .llm import get_registry
from .models import ChunkSignal, Feedback, GoldenItem, QueryCluster, Trace
from .text import content_terms, jaccard

CLUSTER_SIM = 0.82
REPHRASE_WINDOW = timedelta(minutes=3)

# signal weights per feedback kind: (good, bad)
WEIGHTS = {
    "explicit": 1.0,
    "copy": 0.5,
    "citation_click": 0.3,
    "rephrase": 0.7,
}


def _cos(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0 or va.shape != vb.shape:
        return 0.0
    return float(va @ vb / (na * nb))


def assign_cluster(session: Session, kb_id: str, query: str) -> str | None:
    try:
        vec = get_registry().embedder().embed_query(query)
    except Exception:  # noqa: BLE001 - clustering is best effort
        return None
    best, best_sim = None, 0.0
    for c in session.scalars(select(QueryCluster).where(QueryCluster.kb_id == kb_id)):
        s = _cos(vec, c.centroid)
        if s > best_sim:
            best, best_sim = c, s
    if best is not None and best_sim >= CLUSTER_SIM:
        n = best.count
        best.centroid = [(x * n + y) / (n + 1) for x, y in zip(best.centroid, vec)]
        best.count = n + 1
        return best.id
    c = QueryCluster(kb_id=kb_id, label=query[:200], centroid=list(vec), count=1)
    session.add(c)
    session.flush()
    return c.id


def _cited_chunk_ids(trace: Trace) -> list[str]:
    return [c["chunk_id"] for c in (trace.data or {}).get("citations", []) if c.get("chunk_id")]


def _apply_chunk_signal(session: Session, chunk_ids: list[str], good: float, bad: float) -> None:
    for cid in dict.fromkeys(chunk_ids):
        s = session.get(ChunkSignal, cid)
        if s is None:
            s = ChunkSignal(chunk_id=cid, good=0.0, bad=0.0)
            session.add(s)
        s.good += good
        s.bad += bad


def record_feedback(
    session: Session,
    trace: Trace,
    *,
    kind: str = "explicit",
    rating: int = 0,
    comment: str = "",
    correction: str = "",
    trusted: bool = True,
) -> Feedback:
    if kind not in WEIGHTS:
        raise ValueError(f"unknown feedback kind {kind}")
    fb = Feedback(trace_id=trace.id, kind=kind, rating=rating, comment=comment[:4000], correction=correction[:8000])
    session.add(fb)
    w = WEIGHTS[kind]
    if rating > 0 or kind in ("copy", "citation_click"):
        _apply_chunk_signal(session, _cited_chunk_ids(trace), good=w, bad=0.0)
    elif rating < 0 or kind == "rephrase":
        _apply_chunk_signal(session, _cited_chunk_ids(trace), good=0.0, bad=w)
        trace.negative_signal = True
        trace.diagnosed = False  # re-open for the Repair loop
    if correction.strip() and rating < 0:
        # untrusted (student) corrections wait for admin review before they gate fixes
        session.add(
            GoldenItem(
                kb_id=trace.kb_id, question=trace.query, expected_answer=correction.strip(), origin="feedback", active=trusted
            )
        )
    return fb


def detect_rephrase(session: Session, kb_id: str, session_id: str | None, query: str) -> Trace | None:
    """If the user re-asks nearly the same question right after an answer, the
    previous answer probably failed them (Google's 'bad click' analog)."""
    if not session_id:
        return None
    prev = session.scalar(
        select(Trace)
        .where(Trace.kb_id == kb_id, Trace.session_id == session_id, Trace.is_eval.is_(False))
        .order_by(Trace.created_at.desc())
        .limit(1)
    )
    if prev is None:
        return None
    created = prev.created_at if prev.created_at.tzinfo else prev.created_at.replace(tzinfo=utcnow().tzinfo)
    if utcnow() - created > REPHRASE_WINDOW or prev.query.strip().lower() == query.strip().lower():
        return None
    if jaccard(content_terms(prev.query), content_terms(query)) >= 0.5:
        record_feedback(session, prev, kind="rephrase", rating=-1)
        return prev
    return None


def session_history(session: Session, kb_id: str, session_id: str | None, limit: int = 5) -> list[str]:
    if not session_id:
        return []
    rows = session.scalars(
        select(Trace.query)
        .where(Trace.kb_id == kb_id, Trace.session_id == session_id)
        .order_by(Trace.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))
