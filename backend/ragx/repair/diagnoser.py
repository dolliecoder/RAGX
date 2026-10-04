"""Failure miner + root-cause diagnoser.

Turns traces into findings using the evidence the Reflex loop recorded: which
heal action rescued which sub-question, which claims were unsupported, what the
user said. Deterministic rules first (explainable, cheap); every finding names a
root cause from the architecture's taxonomy and a concrete target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document, Feedback, Trace
from ..text import STOPWORDS, sha256, stem, tokenize

ROOT_CAUSES = (
    "missing_content",
    "stale_content",
    "bad_chunking",
    "missing_context",
    "vocabulary_gap",
    "ranking_miss",
    "twiddler_error",
    "conflicting_sources",
    "generation_error",
    "gate_error",
    "injection",
    "unexplained_negative",
)


@dataclass
class Finding:
    root_cause: str
    target: str
    summary: str
    severity: float
    evidence: dict[str, Any] = field(default_factory=dict)


def mine_failures(session: Session, kb_id: str, limit: int = 200) -> list[Trace]:
    """Traces worth learning from: failures, partial answers, negative signals and
    answers that only succeeded because a heal step rescued them."""
    return list(
        session.scalars(
            select(Trace)
            .where(
                Trace.kb_id == kb_id,
                Trace.is_eval.is_(False),
                Trace.diagnosed.is_(False),
                or_(
                    Trace.status.in_(["failed", "partial"]),
                    Trace.negative_signal.is_(True),
                    Trace.healed.is_(True),
                ),
            )
            .order_by(Trace.created_at.asc())
            .limit(limit)
        )
    )


def _raw_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower()) if w not in STOPWORDS and len(w) > 2]


def _missing_words(question: str, chunk: Chunk) -> list[str]:
    have = set(tokenize(" ".join([chunk.text, chunk.context or "", " ".join(chunk.aliases or [])])))
    return [w for w in dict.fromkeys(_raw_words(question)) if stem(w) not in have]


def _present_words(texts: list[str], chunk: Chunk, exclude: set[str]) -> list[str]:
    have = set(tokenize(chunk.text + " " + (chunk.context or "")))
    out = []
    for t in texts:
        for w in _raw_words(t):
            if stem(w) in have and w not in exclude and w not in out:
                out.append(w)
    return out


def _source_changed(doc: Document) -> bool:
    if doc.uri.startswith(("http://", "https://")):
        return False  # remote: handled by the crawl schedule
    p = Path(doc.uri)
    try:
        return p.is_file() and sha256(p.read_bytes()) != doc.content_hash
    except OSError:
        return False


def diagnose(session: Session, trace: Trace) -> list[Finding]:
    data = trace.data or {}
    diag = data.get("diag", {})
    subqs = [s.get("question", "") for s in data.get("subquestions", [])]
    findings: list[Finding] = []
    severity = 1.0 if trace.status in ("failed", "partial") or trace.negative_signal else 0.5

    # --- rescues: a heal step found what first-pass retrieval missed --------
    rewrites = {r["subq"]: r for r in diag.get("rewrites", [])}
    for r in diag.get("rescues", []):
        origin, cid, qi = r.get("origin"), r.get("chunk_id"), r.get("subq", 0)
        question = subqs[qi] if qi < len(subqs) else trace.query
        if origin == "web":
            findings.append(
                Finding(
                    "missing_content",
                    f"web:{question[:120]}",
                    f"Answer to '{question}' was only found on the web",
                    severity,
                    {"question": question, "url": r.get("url"), "trace_id": trace.id},
                )
            )
            continue
        chunk = session.get(Chunk, cid) if cid else None
        if chunk is None:
            continue
        missing = _missing_words(question, chunk)
        if origin == "rewrite":
            rw = rewrites.get(qi, {})
            aliases = _present_words(rw.get("queries", []) + rw.get("new_terms", []), chunk, set(_raw_words(question)))
            findings.append(
                Finding(
                    "vocabulary_gap",
                    f"chunk:{cid}",
                    f"Users ask with words {missing or '[paraphrase]'}; document says {aliases[:6] or '[other terms]'}",
                    severity,
                    {"chunk_id": cid, "doc_id": chunk.doc_id, "question": question, "user_words": missing, "doc_words": aliases, "trace_id": trace.id},
                )
            )
        elif origin == "widen":
            findings.append(
                Finding(
                    "ranking_miss",
                    f"chunk:{cid}",
                    f"Relevant chunk ranked below the cut for '{question}'",
                    severity,
                    {"chunk_id": cid, "doc_id": chunk.doc_id, "question": question, "user_words": missing, "trace_id": trace.id},
                )
            )
        elif origin == "neighbor":
            findings.append(
                Finding(
                    "bad_chunking",
                    f"doc:{chunk.doc_id}",
                    f"Answer to '{question}' was split across adjacent chunks",
                    severity,
                    {"chunk_id": cid, "doc_id": chunk.doc_id, "question": question, "trace_id": trace.id},
                )
            )
        elif origin in ("exact", "cold"):
            doc = session.get(Document, chunk.doc_id)
            if origin == "cold" and doc is not None and doc.status == "quarantined":
                findings.append(
                    Finding(
                        "twiddler_error",
                        f"doc:{doc.id}",
                        f"Quarantined document '{doc.title}' was needed to answer '{question}'",
                        severity,
                        {"doc_id": doc.id, "question": question, "trace_id": trace.id},
                    )
                )
            elif origin == "cold" and doc is not None and doc.tier == "cold":
                findings.append(
                    Finding(
                        "ranking_miss",
                        f"tier:{doc.id}",
                        f"Cold-tier document '{doc.title}' was needed to answer '{question}'",
                        severity,
                        {"doc_id": doc.id, "chunk_id": cid, "question": question, "tier": "cold", "trace_id": trace.id},
                    )
                )
            else:
                findings.append(
                    Finding(
                        "missing_context",
                        f"chunk:{cid}",
                        f"Chunk only matched by exact identifier for '{question}'",
                        severity,
                        {"chunk_id": cid, "doc_id": chunk.doc_id, "question": question, "user_words": missing, "trace_id": trace.id},
                    )
                )

    # --- unsupported claims despite evidence: generation problem --------------
    unsupported = diag.get("unsupported", [])
    if unsupported:
        findings.append(
            Finding(
                "generation_error",
                "prompt:generate",
                f"{len(unsupported)} generated statement(s) were not supported by the cited evidence",
                severity * 0.8,
                {"examples": unsupported[:5], "query": trace.query, "trace_id": trace.id},
            )
        )

    # --- contradictions between sources --------------------------------------
    conflict_claims = diag.get("conflict_claims", [])
    if conflict_claims:
        ev = {e["id"]: e for e in data.get("evidence", [])}
        docs = sorted({e.get("doc_id") for e in data.get("evidence", []) if e.get("doc_id")})
        findings.append(
            Finding(
                "conflicting_sources",
                "docs:" + ",".join(docs[:4]),
                "Retrieved sources contradict each other",
                severity,
                {"claims": conflict_claims[:5], "doc_ids": docs[:6], "trace_id": trace.id, "evidence_ids": list(ev)[:10]},
            )
        )

    # --- nothing found anywhere -----------------------------------------------
    if trace.status == "failed" and not diag.get("rescues") and not diag.get("errors"):
        unanswered = data.get("unanswered") or [trace.query]
        findings.append(
            Finding(
                "missing_content",
                f"gap:{(unanswered[0] if unanswered else trace.query)[:120].lower()}",
                f"No document answers: {unanswered[0] if unanswered else trace.query}",
                severity,
                {"questions": unanswered, "trace_id": trace.id},
            )
        )

    # --- injection suspects in evidence ----------------------------------------
    ev_ids = [e["id"] for e in data.get("evidence", []) if e.get("id") and not str(e["id"]).startswith("web:")]
    if ev_ids:
        flagged = session.scalars(select(Chunk).where(Chunk.id.in_(ev_ids))).all()
        for ch in flagged:
            if "injection_suspect" in (ch.flags or []):
                findings.append(
                    Finding("injection", f"doc:{ch.doc_id}", "Retrieved chunk contains prompt-injection patterns", 1.0, {"doc_id": ch.doc_id, "chunk_id": ch.id, "trace_id": trace.id})
                )

    # --- negative user feedback ------------------------------------------------
    if trace.negative_signal:
        cited_docs = {c.get("doc_id") for c in data.get("citations", []) if c.get("doc_id")}
        stale = [d for d in (session.get(Document, i) for i in cited_docs) if d is not None and _source_changed(d)]
        for d in stale:
            findings.append(
                Finding("stale_content", f"doc:{d.id}", f"'{d.title}' changed at the source since it was indexed", 1.0, {"doc_id": d.id, "trace_id": trace.id})
            )
        if trace.route == "direct":
            findings.append(
                Finding("gate_error", "gate:threshold", "Answered without retrieval but the user was not satisfied", 1.0, {"trace_id": trace.id, "query": trace.query})
            )
        if not findings:
            fb = session.scalars(select(Feedback).where(Feedback.trace_id == trace.id)).all()
            findings.append(
                Finding(
                    "unexplained_negative",
                    f"trace:{trace.id}",
                    "User marked the answer as wrong; no automatic cause found",
                    1.0,
                    {"trace_id": trace.id, "query": trace.query, "comments": [f.comment for f in fb if f.comment], "corrections": [f.correction for f in fb if f.correction]},
                )
            )
    return findings
