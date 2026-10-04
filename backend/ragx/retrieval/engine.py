"""Retrieval engine: parallel retrievers -> RRF fusion -> cascade ranking -> twiddlers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RuntimeConfig
from ..llm import get_registry
from ..models import ChunkSignal, Document
from ..text import jaccard, shingles, tokenize
from .index import Snapshot, bm25_search, dense_search, get_snapshot


@dataclass
class SubQuery:
    question: str
    keywords: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)  # rewrites added by the heal ladder

    def all_texts(self) -> list[str]:
        return [self.question] + self.variants


@dataclass
class Candidate:
    key: str
    idx: int | None
    doc_id: str | None
    title: str
    section: str
    text: str
    context: str = ""
    page: int | None = None
    url: str | None = None
    subqs: set[int] = field(default_factory=set)
    provenance: dict[str, int] = field(default_factory=dict)  # retriever -> best rank
    fused: float = 0.0
    stage1: float = 0.0
    rerank: float | None = None
    final: float = 0.0
    notes: list[str] = field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "subqs": sorted(self.subqs),
            "provenance": self.provenance,
            "fused": round(self.fused, 5),
            "stage1": round(self.stage1, 4),
            "rerank": None if self.rerank is None else round(self.rerank, 4),
            "final": round(self.final, 4),
            "notes": self.notes,
        }


@dataclass
class RankResult:
    final: list[Candidate]
    pool: list[Candidate]  # stage-1 ordered pool beyond the final cut (used to widen K)
    removed: list[dict[str, Any]]
    twiddles: list[dict[str, Any]]
    counts: dict[str, int]
    degraded: list[str]


class RetrievalEngine:
    def __init__(
        self,
        session: Session,
        kb_id: str,
        cfg: RuntimeConfig,
        *,
        staged_docs: frozenset[str] = frozenset(),
        principals: set[str] | None = None,
    ):
        self.session = session
        self.kb_id = kb_id
        self.cfg = cfg
        self.registry = get_registry()
        self.snap: Snapshot = get_snapshot(session, kb_id)
        self.docs: dict[str, Document] = {
            d.id: d for d in session.scalars(select(Document).where(Document.kb_id == kb_id))
        }
        base = self.snap.visible_mask(staged_docs)
        self.acl_filtered = 0
        allowed = np.zeros(len(self.snap.chunks), dtype=bool)
        cold = np.zeros(len(self.snap.chunks), dtype=bool)
        for i, c in enumerate(self.snap.chunks):
            d = self.docs.get(c.doc_id)
            if d is None or not base[i]:
                continue
            if d.acl and not (principals and set(d.acl) & principals):
                self.acl_filtered += 1
                continue
            if d.status == "active" and d.tier != "cold":
                allowed[i] = True
            elif d.status in ("active", "quarantined"):
                cold[i] = True  # cold tier + quarantined: only on escalation
        self.mask = allowed
        self.cold_mask = allowed | cold
        self.degraded: list[str] = []

    # ------------------------------------------------------------- retrieve
    def _bm25_terms(self, texts: list[str]) -> list[tuple[str, float]]:
        terms: dict[str, float] = {}
        for t in texts:
            for tok in tokenize(t):
                terms[tok] = max(terms.get(tok, 0.0), 1.0)
        joined = " ".join(texts).lower()
        for phrase, aliases in self.cfg.query_aliases.items():
            if re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", joined):
                for a in aliases:
                    for tok in tokenize(a):
                        terms.setdefault(tok, 0.7)
        return list(terms.items())

    def _exact(self, patterns: list[str], mask: np.ndarray, k: int, require_all: bool = False) -> list[tuple[int, float]]:
        pats = [p for p in dict.fromkeys(p.strip() for p in patterns) if len(p) >= 2]
        if not pats:
            return []
        regs = [re.compile(r"(?<![\w])" + re.escape(p) + r"(?![\w])", re.IGNORECASE) for p in pats]
        out = []
        for i in np.nonzero(mask)[0]:
            c = self.snap.chunks[i]
            hay = f"{c.title}\n{c.section_path}\n{c.text}"
            hits = sum(1 for r in regs if r.search(hay))
            if hits and (not require_all or hits == len(regs)):
                out.append((int(i), hits / len(regs)))
        out.sort(key=lambda x: -x[1])
        return out[:k]

    def retrieve(
        self,
        subqueries: list[SubQuery],
        *,
        k: int | None = None,
        include_cold: bool = False,
        retrievers: tuple[str, ...] = ("bm25", "dense", "exact"),
        broad_exact: bool = False,
    ) -> dict[tuple[str, int], list[tuple[int, float]]]:
        k = k or self.cfg.retrieve_k
        mask = self.cold_mask if include_cold else self.mask
        lists: dict[tuple[str, int], list[tuple[int, float]]] = {}
        embedder = self.registry.embedder()
        for qi, sq in enumerate(subqueries):
            texts = sq.all_texts() + sq.keywords
            if "bm25" in retrievers:
                lists[("bm25", qi)] = bm25_search(self.snap, self._bm25_terms(texts), mask, k)
            if "dense" in retrievers:
                try:
                    hits: dict[int, float] = {}
                    for t in sq.all_texts():
                        for i, s in dense_search(self.snap, embedder.embed_query(t), mask, k, embedder.model_id):
                            hits[i] = max(hits.get(i, -1.0), s)
                    lists[("dense", qi)] = sorted(hits.items(), key=lambda x: -x[1])[:k]
                except Exception as e:  # noqa: BLE001 - graceful degradation: BM25 + exact still run
                    self.degraded.append(f"dense retrieval failed: {type(e).__name__}: {e}")
            if "exact" in retrievers:
                pats = list(sq.identifiers)
                lists[("exact", qi)] = self._exact(pats, mask, k)
                if broad_exact:
                    kws = [w for w in sq.keywords if len(w) > 3][:4]
                    lists[("exact", qi)] += self._exact(kws, mask, k, require_all=True)
        return lists

    # ----------------------------------------------------------------- rank
    def fuse(self, lists: dict[tuple[str, int], list[tuple[int, float]]]) -> dict[int, Candidate]:
        cands: dict[int, Candidate] = {}
        w = self.cfg.retriever_weights
        for (retriever, qi), hits in lists.items():
            weight = w.get(retriever, 1.0)
            for rank, (i, _score) in enumerate(hits):
                c = cands.get(i)
                if c is None:
                    rec = self.snap.chunks[i]
                    c = Candidate(
                        key=rec.id,
                        idx=i,
                        doc_id=rec.doc_id,
                        title=rec.title,
                        section=rec.section_path,
                        text=rec.text,
                        context=rec.context,
                        page=rec.page,
                    )
                    cands[i] = c
                c.fused += weight / (self.cfg.rrf_k + rank + 1)
                c.subqs.add(qi)
                prev = c.provenance.get(retriever)
                c.provenance[retriever] = rank if prev is None else min(prev, rank)
        return cands

    def _priors(self, cands: list[Candidate]) -> None:
        ids = [c.key for c in cands]
        signals = {}
        if ids:
            for s in self.session.scalars(select(ChunkSignal).where(ChunkSignal.chunk_id.in_(ids))):
                signals[s.chunk_id] = s
        top = max((c.fused for c in cands), default=1.0) or 1.0
        for c in cands:
            d = self.docs.get(c.doc_id or "")
            q = float((d.quality or {}).get("score", 0.7)) if d else 0.7
            auth = d.authority if d else 1.0
            score = (c.fused / top) * (1 + self.cfg.quality_prior_weight * (q - 0.5) * 2) * (0.75 + 0.25 * min(auth, 2.0))
            s = signals.get(c.key)
            if s is not None and (s.good + s.bad) > 0:
                mult = 1 + self.cfg.helpfulness_prior_weight * (s.good - s.bad) / (s.good + s.bad + 5)
                score *= mult
                c.notes.append(f"helpfulness prior x{mult:.2f}")
            c.stage1 = score

    def rank(
        self,
        subqueries: list[SubQuery],
        lists: dict[tuple[str, int], list[tuple[int, float]]],
        *,
        freshness_need: float = 0.0,
        final_k: int | None = None,
        exclude: set[str] | None = None,
    ) -> RankResult:
        final_k = final_k or self.cfg.final_k
        fused = self.fuse(lists)
        cands = [c for c in fused.values() if not (exclude and c.key in exclude)]
        self._priors(cands)
        cands.sort(key=lambda c: -c.stage1)
        counts = {"fused": len(cands)}
        stage1 = cands[: self.cfg.stage1_k]
        pool = cands[self.cfg.stage1_k :]
        counts["stage1"] = len(stage1)

        # Stage 2: rerank per sub-question against the candidates it retrieved.
        reranker = self.registry.reranker()
        n = max(1, len(subqueries))
        per = max(10, self.cfg.stage1_k // n)
        degraded = False
        for qi, sq in enumerate(subqueries):
            mine = [c for c in stage1 if qi in c.subqs][:per]
            if not mine:
                continue
            docs = [(c.key, f"{c.title} | {c.section}\n{c.context}\n{c.text}") for c in mine]
            scored, deg = reranker.rerank(sq.question, docs, len(docs))
            degraded = degraded or deg
            by = dict(scored)
            for c in mine:
                s = by.get(c.key)
                if s is not None:
                    c.rerank = s if c.rerank is None else max(c.rerank, s)
        if degraded:
            self.degraded.append("reranker unavailable: lexical fallback used")
        top1 = max((c.stage1 for c in stage1), default=1.0) or 1.0
        for c in stage1:
            r = c.rerank if c.rerank is not None else 0.0
            c.final = 0.8 * r + 0.2 * (c.stage1 / top1)

        twiddles: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        kept = self._twiddle(stage1, freshness_need, twiddles, removed)
        kept.sort(key=lambda c: -c.final)

        # Guarantee each sub-question some evidence, then fill by score.
        guarantee = max(2, final_k // (2 * n)) if n > 1 else 0
        chosen: list[Candidate] = []
        seen: set[str] = set()
        for qi in range(n if n > 1 else 0):
            for c in [c for c in kept if qi in c.subqs][:guarantee]:
                if c.key not in seen:
                    chosen.append(c)
                    seen.add(c.key)
        for c in kept:
            if len(chosen) >= final_k:
                break
            if c.key not in seen:
                chosen.append(c)
                seen.add(c.key)
        chosen.sort(key=lambda c: -c.final)
        rest = [c for c in kept if c.key not in seen] + pool
        counts["final"] = len(chosen)
        counts["acl_filtered"] = self.acl_filtered
        return RankResult(chosen[:final_k], rest, removed, twiddles, counts, list(self.degraded))

    def _twiddle(
        self,
        cands: list[Candidate],
        freshness_need: float,
        twiddles: list[dict[str, Any]],
        removed: list[dict[str, Any]],
    ) -> list[Candidate]:
        cfg = self.cfg
        # injection suspects and quarantined docs are demoted, not trusted
        for c in cands:
            rec = self.snap.chunks[c.idx] if c.idx is not None else None
            d = self.docs.get(c.doc_id or "")
            if rec and "injection_suspect" in rec.flags:
                c.final *= 0.3
                c.notes.append("injection suspect x0.3")
                twiddles.append({"twiddler": "injection_demote", "id": c.key, "factor": 0.3})
            if d and d.status == "quarantined":
                c.final *= 0.5
                c.notes.append("quarantined doc x0.5")
                twiddles.append({"twiddler": "quarantine_demote", "id": c.key, "factor": 0.5})
            if d and d.id in cfg.doc_boosts:
                f = cfg.doc_boosts[d.id]
                c.final *= f
                c.notes.append(f"doc boost x{f}")
                twiddles.append({"twiddler": "doc_boost", "id": c.key, "factor": f})
            if d and freshness_need >= 0.5:
                fr = float((d.quality or {}).get("freshness", 0.5))
                f = 1 + cfg.freshness_boost * fr
                c.final *= f
                twiddles.append({"twiddler": "freshness", "id": c.key, "factor": round(f, 3)})
        # dedupe near-identical chunks
        ordered = sorted(cands, key=lambda c: -c.final)
        kept: list[Candidate] = []
        sigs: list[set[str]] = []
        for c in ordered:
            sh = shingles(c.text)
            dup = next((k for k, s in zip(kept, sigs) if jaccard(sh, s) >= cfg.dedupe_jaccard), None)
            if dup is not None:
                removed.append({"twiddler": "dedupe", "id": c.key, "duplicate_of": dup.key})
                dup.subqs |= c.subqs
                continue
            kept.append(c)
            sigs.append(sh)
        # diversity: cap chunks per document
        per_doc: dict[str, int] = {}
        out: list[Candidate] = []
        for c in kept:
            key = c.doc_id or c.key
            per_doc[key] = per_doc.get(key, 0) + 1
            if per_doc[key] > cfg.max_chunks_per_doc:
                removed.append({"twiddler": "diversity", "id": c.key, "doc_id": c.doc_id})
                continue
            out.append(c)
        return out

    def candidate_for(self, chunk_id: str) -> Candidate | None:
        i = self.snap.by_id.get(chunk_id)
        if i is None:
            return None
        rec = self.snap.chunks[i]
        return Candidate(rec.id, i, rec.doc_id, rec.title, rec.section_path, rec.text, rec.context, rec.page)

    def neighbors(self, c: Candidate, radius: int = 1) -> list[Candidate]:
        """Adjacent chunks of the same document (for answers split across chunks)."""
        if c.idx is None or c.doc_id is None:
            return []
        idxs = self.snap.doc_chunks.get(c.doc_id, [])
        rec = self.snap.chunks[c.idx]
        out = []
        for j in idxs:
            o = self.snap.chunks[j]
            if o.status == rec.status and 0 < abs(o.ord - rec.ord) <= radius and self.cold_mask[j]:
                out.append(Candidate(o.id, j, o.doc_id, o.title, o.section_path, o.text, o.context, o.page))
        return out
