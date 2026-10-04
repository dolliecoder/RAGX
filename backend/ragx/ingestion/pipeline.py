"""Ingestion: parse -> chunk -> contextualize -> quality signals -> embed -> store.

Every (re)ingest produces a new document *version*. New chunks are written either
as ``active`` (normal path) or as ``staged`` (blue/green candidate evaluated by the
Repair loop before promotion). One previous version is kept as ``retired`` so it
can be rolled back instantly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .. import prompts
from ..db import utcnow
from ..llm import get_registry
from ..llm.base import ProviderError
from ..models import Chunk, Document
from ..text import estimate_tokens, looks_like_injection, sha256
from ..util import run_parallel
from .chunker import DEFAULT_STRATEGY, ChunkDraft, chunk_document
from .parsers import ParseError, ParsedDoc, parse_bytes

log = logging.getLogger("ragx.ingest")

MAX_CONTEXT_DOC_CHARS = 400_000  # ~100K tokens of document given to the contextualizer


@dataclass
class IngestResult:
    doc_id: str | None
    uri: str
    status: str  # created | updated | unchanged | staged | error
    chunks: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def _heuristic_context(title: str, section: str) -> str:
    return f"From '{title}'" + (f", section '{section}'" if section else "") + "."


def contextualize_chunks(parsed: ParsedDoc, drafts: list[ChunkDraft]) -> list[tuple[str, list[str], bool]]:
    """Return (context, aliases, used_llm) per chunk. Falls back to a heuristic
    context when the model is unavailable so ingestion never blocks on an LLM."""
    reg = get_registry()
    document = parsed.text
    if len(document) > MAX_CONTEXT_DOC_CHARS:
        document = document[:MAX_CONTEXT_DOC_CHARS] + "\n[... document truncated ...]"

    def one(d: ChunkDraft) -> tuple[str, list[str], bool]:
        try:
            resp = reg.call("utility", prompts.contextualize(parsed.title, document, d.text))
            ctx = str(resp.data.get("context", "")).strip()
            aliases = [str(a).strip() for a in resp.data.get("aliases", []) if str(a).strip()][:8]
            if ctx:
                return ctx, aliases, True
        except (ProviderError, AttributeError, TypeError) as e:
            log.warning("contextualize failed for chunk %s of %s: %s", d.ord, parsed.title, e)
        return _heuristic_context(parsed.title, d.section_path), [], False

    return run_parallel(one, drafts, max_workers=8)


def embedding_text(title: str, section: str, context: str, text: str) -> str:
    head = context or _heuristic_context(title, section)
    return f"{head}\n\n{text}"


def quality_signals(parsed: ParsedDoc, raw_len: int, modified_at: datetime | None) -> dict:
    text = parsed.text
    alnum = sum(c.isalnum() for c in text)
    parse_quality = round(alnum / max(1, len(text)), 3)
    age_days = None
    if modified_at:
        m = modified_at if modified_at.tzinfo else modified_at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (utcnow() - m).total_seconds() / 86400)
    freshness = 1.0 if age_days is None else round(1.0 / (1.0 + age_days / 180.0), 3)
    headings = sum(1 for b in parsed.blocks if b.kind == "heading")
    score = round(
        0.5 * min(1.0, parse_quality / 0.7) + 0.3 * freshness + 0.2 * (1.0 if headings else 0.6),
        3,
    )
    return {
        "parse_quality": parse_quality,
        "freshness": freshness,
        "age_days": None if age_days is None else round(age_days, 1),
        "headings": headings,
        "bytes": raw_len,
        "score": score,
        "injection_chunks": 0,
    }


def ingest_bytes(
    session: Session,
    kb_id: str,
    data: bytes,
    *,
    uri: str,
    filename: str,
    source_id: str | None = None,
    authority: float = 1.0,
    modified_at: datetime | None = None,
    strategy: dict | None = None,
    stage: bool = False,
    force: bool = False,
) -> IngestResult:
    content_hash = sha256(data)
    doc = session.scalar(select(Document).where(Document.kb_id == kb_id, Document.uri == uri))
    if doc is not None and not force and not stage and doc.content_hash == content_hash and doc.status in ("active", "quarantined"):
        return IngestResult(doc.id, uri, "unchanged", chunks=0)

    try:
        parsed = parse_bytes(data, filename)
    except ParseError as e:
        if doc is None:
            doc = Document(kb_id=kb_id, uri=uri, title=filename, source_id=source_id, status="error")
            session.add(doc)
        doc.status, doc.error = "error", str(e)
        doc.content_hash = content_hash
        doc.last_ingested_at = utcnow()
        session.flush()
        return IngestResult(doc.id, uri, "error", error=str(e))

    is_new = doc is None
    if doc is None:
        doc = Document(kb_id=kb_id, uri=uri, source_id=source_id, version=0, authority=authority)
        session.add(doc)
        session.flush()

    st = strategy or (doc.chunk_strategy if doc.chunk_strategy else DEFAULT_STRATEGY)
    drafts = chunk_document(parsed, st)
    contexts = contextualize_chunks(parsed, drafts)
    reg = get_registry()
    embedder = reg.embedder()
    vectors = embedder.embed_documents(
        [embedding_text(parsed.title, d.section_path, (c[0] + " " + " ".join(c[1])).strip(), d.text) for d, c in zip(drafts, contexts)]
    )

    # A previous staged candidate for this doc is superseded by this one.
    session.execute(delete(Chunk).where(Chunk.doc_id == doc.id, Chunk.status == "staged"))
    # Versions are never reused (a rolled-back version may still be retained).
    max_existing = session.scalar(select(func.max(Chunk.doc_version)).where(Chunk.doc_id == doc.id)) or 0
    new_version = max(doc.version, max_existing) + 1
    quality = quality_signals(parsed, len(data), modified_at)
    injection = 0
    for d, (ctx, aliases, _), vec in zip(drafts, contexts, vectors):
        flags = []
        if looks_like_injection(d.text):
            flags.append("injection_suspect")
            injection += 1
        session.add(
            Chunk(
                kb_id=kb_id,
                doc_id=doc.id,
                doc_version=new_version,
                status="staged" if stage else "active",
                ord=d.ord,
                text=d.text,
                context=ctx,
                aliases=aliases,
                section_path=d.section_path,
                page=d.page,
                token_count=d.token_count,
                embedding=vec,
                embedding_model=embedder.model_id,
                flags=flags,
            )
        )
    quality["injection_chunks"] = injection
    llm_ctx = sum(1 for c in contexts if c[2])
    notes = [] if llm_ctx == len(contexts) else [f"{len(contexts) - llm_ctx} chunks used heuristic context"]

    if stage:
        doc.chunk_strategy = doc.chunk_strategy or DEFAULT_STRATEGY
        session.flush()
        return IngestResult(doc.id, uri, "staged", chunks=len(drafts), notes=notes)

    _activate_version(session, doc, new_version)
    doc.title = parsed.title[:500]
    doc.mime = parsed.mime
    doc.content_hash = content_hash
    doc.quality = quality
    doc.chunk_strategy = st
    doc.token_count = estimate_tokens(parsed.text)
    doc.modified_at = modified_at
    doc.last_ingested_at = utcnow()
    doc.error = None
    if doc.status in ("error", "superseded", "missing"):
        doc.status = "active"
    if injection and doc.status == "active":
        notes.append(f"{injection} chunks look like prompt injection: they are demoted at query time")
    session.flush()
    return IngestResult(doc.id, uri, "created" if is_new else "updated", chunks=len(drafts), notes=notes)


def _activate_version(session: Session, doc: Document, version: int) -> None:
    """Make ``version`` the live chunks of ``doc``; keep exactly one older version."""
    session.execute(
        update(Chunk)
        .where(Chunk.doc_id == doc.id, Chunk.status == "active", Chunk.doc_version != version)
        .values(status="retired")
    )
    session.execute(
        update(Chunk).where(Chunk.doc_id == doc.id, Chunk.doc_version == version).values(status="active")
    )
    session.execute(
        delete(Chunk).where(Chunk.doc_id == doc.id, Chunk.status == "retired", Chunk.doc_version < version - 1)
    )
    doc.version = version


def stage_augmented_copy(session: Session, doc_id: str, modifications: dict[str, dict]) -> int:
    """Copy the live chunks of a document into a new *staged* version, applying
    targeted changes: ``{chunk_id: {"aliases_add": [...], "context_append": str}}``.
    Only modified chunks are re-embedded. Used by healing fixes (blue/green)."""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise KeyError(doc_id)
    active = session.scalars(select(Chunk).where(Chunk.doc_id == doc_id, Chunk.status == "active").order_by(Chunk.ord)).all()
    if not active:
        raise ValueError("document has no active chunks")
    session.execute(delete(Chunk).where(Chunk.doc_id == doc_id, Chunk.status == "staged"))
    max_existing = session.scalar(select(func.max(Chunk.doc_version)).where(Chunk.doc_id == doc_id)) or 0
    new_version = max(doc.version, max_existing) + 1
    embedder = get_registry().embedder()
    changed: list[Chunk] = []
    for ch in active:
        mod = modifications.get(ch.id)
        aliases = list(ch.aliases or [])
        context = ch.context or ""
        if mod:
            aliases = list(dict.fromkeys(aliases + [a for a in mod.get("aliases_add", []) if a]))[:24]
            extra = (mod.get("context_append") or "").strip()
            if extra and extra not in context:
                context = (context + " " + extra).strip()
        new = Chunk(
            kb_id=ch.kb_id,
            doc_id=doc_id,
            doc_version=new_version,
            status="staged",
            ord=ch.ord,
            text=ch.text,
            context=context,
            aliases=aliases,
            section_path=ch.section_path,
            page=ch.page,
            token_count=ch.token_count,
            embedding=ch.embedding,
            embedding_model=ch.embedding_model,
            flags=list(ch.flags or []),
        )
        session.add(new)
        if mod:
            changed.append(new)
    if changed:
        vecs = embedder.embed_documents(
            [embedding_text(doc.title, c.section_path, c.context + " " + " ".join(c.aliases), c.text) for c in changed]
        )
        for c, v in zip(changed, vecs):
            c.embedding = v
            c.embedding_model = embedder.model_id
    session.flush()
    return new_version


def promote_staged(session: Session, doc_id: str, strategy: dict | None = None) -> int:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise KeyError(doc_id)
    staged_version = session.scalar(
        select(Chunk.doc_version).where(Chunk.doc_id == doc_id, Chunk.status == "staged").limit(1)
    )
    if staged_version is None:
        raise ValueError("no staged chunks for document")
    _activate_version(session, doc, staged_version)
    if strategy:
        doc.chunk_strategy = strategy
    doc.last_ingested_at = utcnow()
    session.flush()
    return staged_version


def discard_staged(session: Session, doc_id: str) -> None:
    session.execute(delete(Chunk).where(Chunk.doc_id == doc_id, Chunk.status == "staged"))


def rollback_document(session: Session, doc_id: str, strategy: dict | None = None) -> int:
    """Re-activate the retained previous version of a document."""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise KeyError(doc_id)
    prev = session.scalar(
        select(Chunk.doc_version)
        .where(Chunk.doc_id == doc_id, Chunk.status == "retired")
        .order_by(Chunk.doc_version.desc())
        .limit(1)
    )
    if prev is None:
        raise ValueError("no previous version retained")
    current = doc.version
    session.execute(update(Chunk).where(Chunk.doc_id == doc_id, Chunk.status == "active").values(status="retired"))
    session.execute(update(Chunk).where(Chunk.doc_id == doc_id, Chunk.doc_version == prev).values(status="active"))
    # keep the rolled-back-from version as the retained one so it can be re-applied
    session.execute(
        delete(Chunk).where(Chunk.doc_id == doc_id, Chunk.status == "retired", Chunk.doc_version.notin_([current]))
    )
    doc.version = prev
    if strategy is not None:
        doc.chunk_strategy = strategy
    session.flush()
    return prev
