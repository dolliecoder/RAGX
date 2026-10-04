"""Per-knowledge-base in-memory index snapshot (BM25 + dense matrix).

The snapshot holds active *and* staged chunks; each query decides which are
visible (staged chunks are only visible to Repair-loop evaluations of a fix).
Snapshots are rebuilt automatically when the chunk table changes.
"""

from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document
from ..text import tokenize

K1, B = 1.2, 0.75


@dataclass
class ChunkRec:
    id: str
    doc_id: str
    status: str
    ord: int
    text: str
    context: str
    aliases: list[str]
    section_path: str
    page: int | None
    title: str
    flags: list[str]
    token_count: int
    embedding_model: str


@dataclass
class Snapshot:
    signature: tuple
    chunks: list[ChunkRec]
    by_id: dict[str, int]
    postings: dict[str, list[tuple[int, int]]]
    doc_len: np.ndarray
    avg_len: float
    matrix: np.ndarray | None  # normalized embeddings (rows aligned with chunks), NaN rows = missing
    has_vec: np.ndarray
    doc_chunks: dict[str, list[int]] = field(default_factory=dict)

    def visible_mask(self, staged_docs: set[str] | frozenset[str] = frozenset()) -> np.ndarray:
        m = np.zeros(len(self.chunks), dtype=bool)
        for i, c in enumerate(self.chunks):
            if c.doc_id in staged_docs:
                m[i] = c.status == "staged"
            else:
                m[i] = c.status == "active"
        return m


def _signature(session: Session, kb_id: str) -> tuple:
    row = session.execute(
        select(func.count(Chunk.id), func.max(Chunk.created_at), func.coalesce(func.sum(Chunk.doc_version), 0))
        .where(Chunk.kb_id == kb_id, Chunk.status.in_(["active", "staged"]))
    ).one()
    return (int(row[0]), str(row[1]), int(row[2]))


def index_terms(rec: ChunkRec) -> list[str]:
    return tokenize(" ".join([rec.title, rec.section_path, rec.context, " ".join(rec.aliases), rec.text]))


def _build(session: Session, kb_id: str, sig: tuple) -> Snapshot:
    rows = session.execute(
        select(Chunk, Document.title)
        .join(Document, Document.id == Chunk.doc_id)
        .where(Chunk.kb_id == kb_id, Chunk.status.in_(["active", "staged"]))
        .order_by(Chunk.doc_id, Chunk.ord)
    ).all()
    chunks: list[ChunkRec] = []
    vecs: list[list[float] | None] = []
    for ch, title in rows:
        chunks.append(
            ChunkRec(
                id=ch.id,
                doc_id=ch.doc_id,
                status=ch.status,
                ord=ch.ord,
                text=ch.text,
                context=ch.context or "",
                aliases=list(ch.aliases or []),
                section_path=ch.section_path or "",
                page=ch.page,
                title=title or "",
                flags=list(ch.flags or []),
                token_count=ch.token_count,
                embedding_model=ch.embedding_model or "",
            )
        )
        vecs.append(ch.embedding)
    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    lens = np.zeros(len(chunks), dtype=np.float32)
    doc_chunks: dict[str, list[int]] = defaultdict(list)
    for i, rec in enumerate(chunks):
        terms = index_terms(rec)
        lens[i] = len(terms)
        for t, tf in Counter(terms).items():
            postings[t].append((i, tf))
        doc_chunks[rec.doc_id].append(i)
    matrix = None
    has_vec = np.zeros(len(chunks), dtype=bool)
    dims = next((len(v) for v in vecs if v), 0)
    if dims:
        matrix = np.zeros((len(chunks), dims), dtype=np.float32)
        for i, v in enumerate(vecs):
            if v and len(v) == dims:
                arr = np.asarray(v, dtype=np.float32)
                n = float(np.linalg.norm(arr))
                if n > 0:
                    matrix[i] = arr / n
                    has_vec[i] = True
    return Snapshot(
        signature=sig,
        chunks=chunks,
        by_id={c.id: i for i, c in enumerate(chunks)},
        postings=dict(postings),
        doc_len=lens,
        avg_len=float(lens.mean()) if len(lens) else 0.0,
        matrix=matrix,
        has_vec=has_vec,
        doc_chunks=dict(doc_chunks),
    )


_cache: dict[str, Snapshot] = {}
_lock = threading.Lock()


def get_snapshot(session: Session, kb_id: str) -> Snapshot:
    sig = _signature(session, kb_id)
    with _lock:
        snap = _cache.get(kb_id)
        if snap is not None and snap.signature == sig:
            return snap
    snap = _build(session, kb_id, sig)
    with _lock:
        _cache[kb_id] = snap
    return snap


def invalidate(kb_id: str | None = None) -> None:
    with _lock:
        if kb_id is None:
            _cache.clear()
        else:
            _cache.pop(kb_id, None)


def bm25_search(
    snap: Snapshot,
    query_terms: list[tuple[str, float]],
    mask: np.ndarray,
    k: int,
) -> list[tuple[int, float]]:
    """Okapi BM25 over the visible chunks. ``query_terms`` are (term, weight)."""
    n = int(mask.sum())
    if n == 0 or not query_terms:
        return []
    scores = np.zeros(len(snap.chunks), dtype=np.float32)
    avg = snap.avg_len or 1.0
    for term, weight in query_terms:
        posts = snap.postings.get(term)
        if not posts:
            continue
        df = sum(1 for i, _ in posts if mask[i])
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, tf in posts:
            if mask[i]:
                denom = tf + K1 * (1 - B + B * snap.doc_len[i] / avg)
                scores[i] += weight * idf * tf * (K1 + 1) / denom
    idx = np.nonzero(scores > 0)[0]
    if len(idx) == 0:
        return []
    top = idx[np.argsort(-scores[idx])][:k]
    return [(int(i), float(scores[i])) for i in top]


def dense_search(snap: Snapshot, qvec: list[float], mask: np.ndarray, k: int, model_id: str) -> list[tuple[int, float]]:
    if snap.matrix is None or not qvec:
        return []
    q = np.asarray(qvec, dtype=np.float32)
    if q.shape[0] != snap.matrix.shape[1]:
        return []
    n = float(np.linalg.norm(q))
    if n == 0:
        return []
    q /= n
    model_ok = np.array([c.embedding_model == model_id for c in snap.chunks], dtype=bool)
    usable = mask & snap.has_vec & model_ok
    if not usable.any():
        return []
    sims = snap.matrix @ q
    sims[~usable] = -np.inf
    k = min(k, int(usable.sum()))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [(int(i), float(sims[i])) for i in top if np.isfinite(sims[i])]
