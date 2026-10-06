"""RAGX HTTP API (FastAPI). The web dashboard and the CLI are both clients of it."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from . import __version__
from . import limits as usage_limits
from .auth import AuthError, Principal, rate_guard
from .bootstrap import init_app
from .config import RuntimeConfig, get_settings
from .control import activate, active_config, audit, canary_version, create_version, rollback_version, select_config
from .db import session_scope
from .ingestion.crawler import is_safe_local_path, reingest_document
from .ingestion.parsers import SUPPORTED
from .jobs import create_job, get_runner
from .llm import get_registry
from .metrics import kb_metrics
from .models import (
    AuditLog,
    Chunk,
    ConfigVersion,
    Diagnosis,
    Document,
    EvalRun,
    Feedback,
    Fix,
    GoldenItem,
    Job,
    KnowledgeBase,
    Source,
    Trace,
    User,
)
from .reflex.engine import QueryEngine, QueryOptions
from .recheck import recheck as recheck_answer
from .signals import record_feedback
from .tasks import Scheduler
from .api_accounts import router as accounts_router
from .oauth_google import router as google_router

log = logging.getLogger("ragx.api")
_scheduler: Scheduler | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _scheduler
    init_app()
    resumed = get_runner().resume_incomplete()
    if resumed:
        log.info("resumed %d interrupted jobs", resumed)
    _scheduler = Scheduler()
    _scheduler.start()
    yield
    _scheduler.shutdown()
    get_runner().shutdown()


app = FastAPI(title="RAGX", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(accounts_router)
app.include_router(google_router)


@app.exception_handler(AuthError)
async def _auth_error(_request: Request, exc: AuthError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(usage_limits.LimitError)
async def _limit_error(_request: Request, exc: usage_limits.LimitError):
    return JSONResponse(
        {"detail": str(exc), "limit": exc.kind, "retry_after": exc.retry_after},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


from .deps import api, signed_in  # noqa: E402  (shared auth dependencies)


def _dt(v):
    return v.isoformat() if v is not None else None


def _404(what: str):
    raise HTTPException(404, f"{what} not found")


# ------------------------------------------------------------------ system
@app.get("/api/health")
def health() -> dict[str, Any]:
    with session_scope() as s:
        s.execute(select(1))
    return {"ok": True, "version": __version__}


@app.get("/api/providers", dependencies=[api])
def providers() -> dict[str, Any]:
    from .retrieval.web import web_available

    st = get_registry().status()
    st["web_search"] = {"provider": get_settings().web_search_provider, "available": web_available(get_settings())}
    return st


@app.get("/api/providers/models", dependencies=[api])
def provider_models(provider: str = Query(..., pattern="^(ollama|gemini|groq|openrouter)$")) -> list[dict[str, Any]]:
    from .llm.catalog import CatalogError, list_models

    try:
        return list_models(provider, get_settings())
    except CatalogError as e:
        raise HTTPException(409, str(e)) from e


# ---------------------------------------------------------- knowledge bases
class KBIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    visibility: Literal["all", "admins"] = "all"
    # admins: true = a shared knowledge base everyone can use; false = a private workspace
    shared: bool | None = None
    general_knowledge: bool = True


class KBPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    visibility: Literal["all", "admins"] | None = None
    general_knowledge: bool | None = None


PAGE_TOKENS = 500  # rough tokens per page, for the page-based workspace limits


def _can_see(kb: KnowledgeBase, p: Principal) -> bool:
    if kb.owner_id:  # private workspaces are only ever visible to their owner
        return p.user_id == kb.owner_id
    return p.is_admin or kb.visibility == "all"


def _can_edit(kb: KnowledgeBase, p: Principal) -> bool:
    return (p.user_id is not None and p.user_id == kb.owner_id) or (kb.owner_id is None and p.is_admin)


def _kb(s, kb_id: str, p: Principal | None = None, *, edit: bool = False) -> KnowledgeBase:
    kb = s.get(KnowledgeBase, kb_id) or s.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_id))
    if kb is None or (p is not None and not _can_see(kb, p)):
        _404("knowledge base")
    if edit and p is not None and not _can_edit(kb, p):
        raise HTTPException(403, "Only the owner of this workspace can change it.")
    return kb


def _plan_limits(s, p: Principal):
    from .auth import load_policy

    u = s.get(User, p.user_id) if p.user_id else None
    return load_policy(s).limits_for(u.plan if u is not None else "")


def _kb_usage(s, kb_id: str) -> tuple[int, int]:
    files = s.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb_id, Document.status.not_in(("superseded",)))
    ) or 0
    tokens = s.scalar(select(func.coalesce(func.sum(Chunk.token_count), 0)).where(Chunk.kb_id == kb_id, Chunk.status == "active")) or 0
    return int(files), int(tokens)


def _kb_out(s, kb: KnowledgeBase, p: Principal | None = None) -> dict[str, Any]:
    docs, tokens = _kb_usage(s, kb.id)
    limits = None
    if p is not None and kb.owner_id and not p.is_admin:
        pl = _plan_limits(s, p)
        limits = {"files": pl.files_per_workspace, "pages": pl.pages_per_workspace, "max_file_mb": pl.max_file_mb}
    return {
        "id": kb.id,
        "name": kb.title or kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "shared": kb.owner_id is None,
        "can_edit": bool(p is not None and _can_edit(kb, p)),
        "general_knowledge": kb.general_knowledge,
        "pages": round(tokens / PAGE_TOKENS),
        "limits": limits,
        "documents": docs,
        "tokens": int(tokens),
        "created_at": _dt(kb.created_at),
    }


@app.get("/api/kbs")
def list_kbs(p: Principal = signed_in) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at)).all()
        return [_kb_out(s, kb, p) for kb in rows if _can_see(kb, p)]


@app.post("/api/kbs", status_code=201)
def create_kb(body: KBIn, p: Principal = signed_in) -> dict[str, Any]:
    import uuid

    title = " ".join(body.name.split())
    # admins create shared knowledge bases by default (CLI/back-compat); everyone else gets a private workspace
    shared = p.is_admin if body.shared is None else (body.shared and p.is_admin)
    with session_scope() as s:
        if shared:
            if s.scalar(select(KnowledgeBase).where(KnowledgeBase.name == title)):
                raise HTTPException(409, "a knowledge base with this name exists")
            kb = KnowledgeBase(name=title, description=body.description, visibility=body.visibility, general_knowledge=body.general_knowledge)
        else:
            if not p.user_id:
                raise HTTPException(400, "Workspaces belong to signed-in accounts.")
            mine = s.scalars(select(KnowledgeBase).where(KnowledgeBase.owner_id == p.user_id)).all()
            if any((k.title or k.name).lower() == title.lower() for k in mine):
                raise HTTPException(409, f"You already have a workspace called “{title}”.")
            limit = _plan_limits(s, p).workspaces
            if not p.is_admin and limit and len(mine) >= limit:
                raise HTTPException(403, f"You can have up to {limit} workspaces. Delete one you no longer need to make room.")
            kb = KnowledgeBase(
                name=f"ws-{uuid.uuid4().hex[:20]}",
                title=title,
                description=body.description,
                visibility="all",
                owner_id=p.user_id,
                general_knowledge=body.general_knowledge,
            )
        s.add(kb)
        s.flush()
        audit(s, p.email, "kb.create", kb.id, name=title, shared=shared)
        return _kb_out(s, kb, p)


@app.get("/api/kbs/{kb_id}")
def get_kb(kb_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        return _kb_out(s, _kb(s, kb_id, p), p)


@app.patch("/api/kbs/{kb_id}")
def patch_kb(kb_id: str, body: KBPatch, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p, edit=True)
        if body.name is not None:
            title = " ".join(body.name.split())
            if kb.owner_id:
                clash = s.scalar(
                    select(KnowledgeBase).where(KnowledgeBase.owner_id == kb.owner_id, KnowledgeBase.id != kb.id, func.lower(KnowledgeBase.title) == title.lower())
                )
                if clash is not None:
                    raise HTTPException(409, f"You already have a workspace called “{title}”.")
            kb.title = title
        if body.description is not None:
            kb.description = body.description
        if body.visibility is not None and kb.owner_id is None:
            kb.visibility = body.visibility
        if body.general_knowledge is not None:
            kb.general_knowledge = body.general_knowledge
        audit(s, p.email, "kb.update", kb.id, **body.model_dump(exclude_none=True))
        return _kb_out(s, kb, p)


@app.delete("/api/kbs/{kb_id}")
def delete_kb(kb_id: str, p: Principal = signed_in) -> dict[str, Any]:
    import shutil

    with session_scope() as s:
        kb = _kb(s, kb_id, p, edit=True)
        kb_real, label = kb.id, kb.title or kb.name
        s.execute(delete(Chunk).where(Chunk.kb_id == kb_real))
        s.execute(delete(Document).where(Document.kb_id == kb_real))
        s.delete(kb)
        audit(s, p.email, "kb.delete", kb_real, name=label)
    shutil.rmtree(get_settings().data_dir / "uploads" / kb_real, ignore_errors=True)
    from .retrieval import index

    index.invalidate(kb_real)
    return {"deleted": True}


# ----------------------------------------------------------------- sources
class SourceIn(BaseModel):
    kind: Literal["file", "directory", "url"]
    uri: str = Field(min_length=1)
    recursive: bool = True
    authority: float = Field(default=1.0, ge=0.1, le=2.0)


def _source_out(src: Source) -> dict[str, Any]:
    return {
        "id": src.id,
        "kind": src.kind,
        "uri": src.uri,
        "recursive": src.recursive,
        "authority": src.authority,
        "status": src.status,
        "last_error": src.last_error,
        "last_crawled_at": _dt(src.last_crawled_at),
    }


@app.get("/api/kbs/{kb_id}/sources", dependencies=[api])
def list_sources(kb_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        return [_source_out(x) for x in s.scalars(select(Source).where(Source.kb_id == kb.id))]


@app.post("/api/kbs/{kb_id}/sources", status_code=201)
def add_source(kb_id: str, body: SourceIn, p: Principal = api) -> dict[str, Any]:
    st = get_settings()
    uri = body.uri.strip()
    if body.kind == "url":
        if not re.match(r"^https?://", uri):
            raise HTTPException(422, "url sources must start with http:// or https://")
    else:
        path = Path(uri).expanduser()
        if not path.exists():
            raise HTTPException(422, f"path does not exist on the server: {uri}")
        roots = [Path(r.strip()) for r in st.source_roots.split(",") if r.strip()]
        if roots and not is_safe_local_path(str(path), roots):
            raise HTTPException(403, "path is outside RAGX_SOURCE_ROOTS")
        uri = str(path.resolve())
    with session_scope() as s:
        kb = _kb(s, kb_id)
        src = Source(kb_id=kb.id, kind=body.kind, uri=uri, recursive=body.recursive, authority=body.authority)
        s.add(src)
        s.flush()
        job = create_job(s, "crawl", kb.id, {"source_id": src.id, "force": True})
        audit(s, p.email, "source.add", src.id, kind=body.kind, uri=uri)
        return {**_source_out(src), "job_id": job.id}


@app.post("/api/sources/{source_id}/crawl", dependencies=[api])
def crawl(source_id: str, force: bool = False) -> dict[str, Any]:
    with session_scope() as s:
        src = s.get(Source, source_id) or _404("source")
        job = create_job(s, "crawl", src.kb_id, {"source_id": src.id, "force": force})
        return {"job_id": job.id}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        src = s.get(Source, source_id) or _404("source")
        for d in s.scalars(select(Document).where(Document.source_id == src.id)):
            s.delete(d)
        s.delete(src)
        audit(s, p.email, "source.delete", source_id)
    return {"deleted": True}


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")


@app.post("/api/kbs/{kb_id}/upload", status_code=201)
async def upload(kb_id: str, files: list[UploadFile] = File(...), p: Principal = signed_in) -> dict[str, Any]:
    from .ingestion.parsers import ParseError, parse_bytes
    from .text import estimate_tokens

    st = get_settings()
    with session_scope() as s:
        kb = _kb(s, kb_id, p, edit=True)
        kb_real = kb.id
        limited = kb.owner_id is not None and not p.is_admin
        pl = _plan_limits(s, p)
        n_files, tokens = _kb_usage(s, kb_real)
    max_bytes = (pl.max_file_mb if limited and pl.max_file_mb else 50) * 1024 * 1024
    page_budget = pl.pages_per_workspace * PAGE_TOKENS if limited and pl.pages_per_workspace else None
    file_budget = pl.files_per_workspace if limited and pl.files_per_workspace else None
    folder = (st.data_dir / "uploads" / kb_real).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name).strip() or "upload"
        if Path(name).suffix.lower() not in SUPPORTED:
            rejected.append({"file": f.filename, "reason": "This file type isn't supported (use PDF, Word, HTML, Markdown or text)."})
            continue
        data = await f.read()
        if len(data) > max_bytes:
            rejected.append({"file": f.filename, "reason": f"Larger than {max_bytes // (1024 * 1024)} MB."})
            continue
        replacing = (folder / name).exists()
        if file_budget is not None and not replacing and n_files >= file_budget:
            rejected.append({"file": f.filename, "reason": f"This workspace is full ({file_budget} files). Remove a file to add more."})
            continue
        if page_budget is not None:
            try:
                est = estimate_tokens(parse_bytes(data, name).text)
            except ParseError as e:
                rejected.append({"file": f.filename, "reason": f"Couldn't read this file: {e}"})
                continue
            if tokens + est > page_budget:
                left = max(0, (page_budget - tokens) // PAGE_TOKENS)
                rejected.append(
                    {"file": f.filename, "reason": f"Too big: about {round(est / PAGE_TOKENS)} pages, and this workspace has room for about {left} more."}
                )
                continue
            tokens += est
        (folder / name).write_bytes(data)
        saved.append(name)
        if not replacing:
            n_files += 1
    with session_scope() as s:
        src = s.scalar(select(Source).where(Source.kb_id == kb_real, Source.kind == "directory", Source.uri == str(folder)))
        if src is None:
            src = Source(kb_id=kb_real, kind="directory", uri=str(folder), recursive=False)
            s.add(src)
            s.flush()
        job = create_job(s, "crawl", kb_real, {"source_id": src.id, "force": False}, user_id=p.user_id) if saved else None
        audit(s, p.email, "upload", kb_real, files=saved)
        return {"saved": saved, "rejected": rejected, "job_id": job.id if job else None, "source_id": src.id}


# --------------------------------------------------------------- documents
def _doc_out(d: Document, chunks: int | None = None) -> dict[str, Any]:
    return {
        "id": d.id,
        "uri": d.uri,
        "title": d.title,
        "mime": d.mime,
        "status": d.status,
        "tier": d.tier,
        "version": d.version,
        "authority": d.authority,
        "quality": d.quality,
        "token_count": d.token_count,
        "chunk_strategy": d.chunk_strategy,
        "modified_at": _dt(d.modified_at),
        "last_ingested_at": _dt(d.last_ingested_at),
        "next_crawl_at": _dt(d.next_crawl_at),
        "error": d.error,
        "chunks": chunks,
    }


@app.get("/api/kbs/{kb_id}/documents")
def list_documents(kb_id: str, p: Principal = signed_in) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        counts = dict(
            s.execute(select(Chunk.doc_id, func.count()).where(Chunk.kb_id == kb.id, Chunk.status == "active").group_by(Chunk.doc_id)).all()
        )
        return [_doc_out(d, counts.get(d.id, 0)) for d in s.scalars(select(Document).where(Document.kb_id == kb.id).order_by(Document.title))]


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Document, doc_id) or _404("document")
        _kb(s, d.kb_id, p)  # same visibility as the workspace it belongs to
        chunks = s.scalars(select(Chunk).where(Chunk.doc_id == d.id).order_by(Chunk.status, Chunk.ord)).all()
        return {
            **_doc_out(d, sum(1 for c in chunks if c.status == "active")),
            "chunk_list": [
                {
                    "id": c.id,
                    "ord": c.ord,
                    "status": c.status,
                    "version": c.doc_version,
                    "section": c.section_path,
                    "page": c.page,
                    "context": c.context,
                    "aliases": c.aliases,
                    "flags": c.flags,
                    "tokens": c.token_count,
                    "text": c.text,
                    "embedding_model": c.embedding_model,
                }
                for c in chunks
            ],
        }


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Document, doc_id) or _404("document")
        kb = _kb(s, d.kb_id, p, edit=True)
        uploads = (get_settings().data_dir / "uploads" / kb.id).resolve()
        path = Path(d.uri)
        try:  # uploaded copies are removed so the next crawl doesn't bring the file back
            if path.resolve().parent == uploads and path.exists():
                path.unlink()
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
        s.execute(delete(Chunk).where(Chunk.doc_id == d.id))
        s.delete(d)
        audit(s, p.email, "document.delete", doc_id, title=d.title, kb=kb.id)
        kb_real = kb.id
    from .retrieval import index

    index.invalidate(kb_real)
    return {"deleted": True}


class DocAction(BaseModel):
    tier: Literal["hot", "warm", "cold"] | None = None
    authority: float | None = Field(default=None, ge=0.1, le=2.0)


@app.post("/api/documents/{doc_id}/{action}")
def document_action(doc_id: str, action: Literal["quarantine", "unquarantine", "reingest", "update"], body: DocAction | None = None, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Document, doc_id) or _404("document")
        if action == "quarantine":
            d.status = "quarantined"
        elif action == "unquarantine":
            d.status = "active"
        elif action == "reingest":
            try:
                res = reingest_document(s, d)
            except FileNotFoundError as e:
                raise HTTPException(409, str(e)) from e
            audit(s, p.email, "document.reingest", doc_id, result=res.status)
            return {"status": res.status, "chunks": res.chunks, "notes": res.notes}
        elif action == "update" and body is not None:
            if body.tier:
                d.tier = body.tier
            if body.authority is not None:
                d.authority = body.authority
        audit(s, p.email, f"document.{action}", doc_id)
        return _doc_out(d)


# ------------------------------------------------------------------- query
class QueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=64)
    mode: Literal["auto", "fast", "standard", "deep"] = "auto"
    principals: list[str] = Field(default_factory=list)


@app.post("/api/kbs/{kb_id}/query")
def query(kb_id: str, body: QueryIn, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        if p.user_id:
            from .api_accounts import verification_blocks

            u = s.get(User, p.user_id)
            if u is not None and verification_blocks(s, u):
                raise HTTPException(
                    403, "Please confirm your email address first: open the link we sent you, or resend it from your account page."
                )
        with usage_limits.reserve(s, p, deep=body.mode == "deep") as can_go_deep:
            version, cfg = select_config(s)
            eng = QueryEngine(s, kb.id, cfg, version)
            result = eng.answer(
                body.query,
                QueryOptions(
                    mode=body.mode,
                    session_id=body.session_id,
                    user_id=p.user_id,
                    # document ACL groups can only be asserted by admins / automation
                    principals=(set(body.principals) or None) if p.is_admin else None,
                    allow_escalation=can_go_deep,
                    allow_general=kb.general_knowledge,
                ),
            )
            usage_limits.record(s, p.user_id, result)
        if p.user_id:
            user = s.get(User, p.user_id)
            if user is not None:
                result["usage"] = usage_limits.usage_summary(s, user)
        return result


# -------------------------------------------------------------------- jobs
def _job_out(j: Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "kb_id": j.kb_id,
        "user_id": j.user_id,
        "kind": j.kind,
        "status": j.status,
        "input": j.input,
        "state": {k: v for k, v in (j.state or {}).items() if k != "workers"},
        "result": j.result,
        "error": j.error,
        "attempts": j.attempts,
        "created_at": _dt(j.created_at),
        "updated_at": _dt(j.updated_at),
    }


@app.get("/api/jobs")
def list_jobs(kb_id: str | None = None, limit: int = Query(50, le=500), p: Principal = signed_in) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if kb_id:
            q = q.where(Job.kb_id == _kb(s, kb_id, p).id)
        if not p.is_admin:
            q = q.where(Job.user_id == p.user_id)
        return [_job_out(j) for j in s.scalars(q)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or (not p.is_admin and j.user_id != p.user_id):
            _404("job")
        return _job_out(j)


# ------------------------------------------------------------------ traces
def _trace_brief(t: Trace) -> dict[str, Any]:
    return {
        "id": t.id,
        "query": t.query,
        "route": t.route,
        "status": t.status,
        "groundedness": t.groundedness,
        "contradiction": t.contradiction,
        "coverage": t.coverage,
        "confidence": t.confidence,
        "healed": t.healed,
        "negative_signal": t.negative_signal,
        "llm_calls": t.llm_calls,
        "latency_ms": t.latency_ms,
        "config_version": t.config_version,
        "is_eval": t.is_eval,
        "session_id": t.session_id,
        "user_id": t.user_id,
        "created_at": _dt(t.created_at),
    }


@app.get("/api/kbs/{kb_id}/traces")
def list_traces(
    kb_id: str,
    status: str | None = None,
    include_eval: bool = False,
    healed: bool | None = None,
    user_id: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    p: Principal = signed_in,
) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        q = select(Trace).where(Trace.kb_id == kb.id)
        if not p.is_admin:  # students only ever see their own questions
            q = q.where(Trace.user_id == p.user_id)
            include_eval = False
        elif user_id:
            q = q.where(Trace.user_id == user_id)
        if not include_eval:
            q = q.where(Trace.is_eval.is_(False))
        if status:
            q = q.where(Trace.status.in_(status.split(",")))
        if healed is not None:
            q = q.where(Trace.healed.is_(healed))
        total = s.scalar(select(func.count()).select_from(q.subquery()))
        rows = s.scalars(q.order_by(Trace.created_at.desc()).offset(offset).limit(limit)).all()
        items = [_trace_brief(t) for t in rows]
        if p.is_admin:
            emails = _emails(s, {t.user_id for t in rows if t.user_id})
            for it in items:
                it["user_email"] = emails.get(it["user_id"])
        return {"total": total, "items": items}


def _emails(s, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    return dict(s.execute(select(User.id, User.email).where(User.id.in_(ids))).all())


def _own_trace(s, trace_id: str, p: Principal) -> Trace:
    t = s.get(Trace, trace_id)
    if t is None or (not p.is_admin and t.user_id != p.user_id):
        _404("trace")
    return t


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        t = _own_trace(s, trace_id, p)
        fb = s.scalars(select(Feedback).where(Feedback.trace_id == t.id).order_by(Feedback.created_at)).all()
        return {
            **_trace_brief(t),
            "kb_id": t.kb_id,
            "answer": t.answer,
            "tokens_in": t.tokens_in,
            "tokens_out": t.tokens_out,
            "data": t.data,
            "feedback": [{"kind": f.kind, "rating": f.rating, "comment": f.comment, "correction": f.correction, "created_at": _dt(f.created_at)} for f in fb],
            "user_email": _emails(s, {t.user_id}).get(t.user_id) if p.is_admin and t.user_id else None,
        }


# ------------------------------------------------------------------- chats
# A chat is the asker's own traces sharing a session_id. Everyone (admins too)
# only sees their own chats here; the Traces page is the admin-wide view.
def _chat_traces(s, kb_id: str, chat_id: str, p: Principal):
    if not p.user_id:
        raise HTTPException(400, "Chats belong to signed-in accounts.")
    return select(Trace).where(
        Trace.kb_id == kb_id,
        Trace.session_id == chat_id,
        Trace.user_id == p.user_id,
        Trace.is_eval.is_(False),
        Trace.hidden.is_(False),
    )


def _answer_from_trace(t: Trace) -> dict[str, Any]:
    d = t.data or {}
    return {
        "status": t.status,
        "answer": t.answer,
        "sentences": d.get("sentences") or [],
        "citations": d.get("citations") or [],
        "unanswered": d.get("unanswered") or [],
        "conflicts": d.get("conflicts") or [],
        "route": t.route,
        "metrics": d.get("metrics") or {},
        "job_id": d.get("job_id"),
        "trace_id": t.id,
        "config_version": t.config_version,
        "llm": d.get("llm") or {"calls": t.llm_calls, "tokens_in": t.tokens_in, "tokens_out": t.tokens_out, "by_task": {}},
        "latency_ms": t.latency_ms,
        "healed": t.healed,
        "offline": False,
        "recheck": d.get("recheck"),
        "general": d.get("general"),
    }


def _with_updated(s, ans: dict[str, Any]) -> dict[str, Any]:
    rc = ans.get("recheck")
    if rc and rc.get("updated_trace_id"):
        t2 = s.get(Trace, rc["updated_trace_id"])
        if t2 is not None:
            ans["recheck"] = {**rc, "updated": _answer_from_trace(t2)}
    return ans


@app.get("/api/kbs/{kb_id}/chats")
def list_chats(kb_id: str, limit: int = Query(100, le=500), p: Principal = signed_in) -> list[dict[str, Any]]:
    if not p.user_id:
        return []
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        mine = (
            Trace.kb_id == kb.id,
            Trace.user_id == p.user_id,
            Trace.session_id.is_not(None),
            Trace.is_eval.is_(False),
            Trace.hidden.is_(False),
        )
        groups = s.execute(
            select(Trace.session_id, func.min(Trace.created_at), func.max(Trace.created_at), func.count())
            .where(*mine)
            .group_by(Trace.session_id)
            .order_by(func.max(Trace.created_at).desc())
            .limit(limit)
        ).all()
        if not groups:
            return []
        # title = the first question of each chat
        firsts = {
            (sid, created): q
            for sid, created, q in s.execute(
                select(Trace.session_id, Trace.created_at, Trace.query).where(*mine, Trace.session_id.in_([g[0] for g in groups]))
            )
        }
        return [
            {"id": sid, "title": " ".join((firsts.get((sid, first)) or "New chat").split())[:120], "updated_at": _dt(last), "messages": n}
            for sid, first, last, n in groups
        ]


@app.get("/api/kbs/{kb_id}/chats/{chat_id}")
def get_chat(kb_id: str, chat_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        rows = s.scalars(_chat_traces(s, kb.id, chat_id, p).order_by(Trace.created_at)).all()
        if not rows:
            _404("chat")
        turns: list[dict[str, Any]] = []
        for t in rows:
            # a deep-research report is written as a second trace for the same question
            if turns and t.route == "deep" and turns[-1]["query"] == t.query and turns[-1]["answer"]["job_id"]:
                turns[-1]["answer"] = {**_answer_from_trace(t), "job_id": turns[-1]["answer"]["job_id"]}
                continue
            turns.append({"query": t.query, "answer": _with_updated(s, _answer_from_trace(t))})
        return {"id": chat_id, "title": " ".join(rows[0].query.split())[:120], "turns": turns}


@app.delete("/api/kbs/{kb_id}/chats/{chat_id}")
def delete_chat(kb_id: str, chat_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id, p)
        ids = s.scalars(_chat_traces(s, kb.id, chat_id, p).with_only_columns(Trace.id)).all()
        if not ids:
            _404("chat")
        s.execute(update(Trace).where(Trace.id.in_(ids)).values(hidden=True))
        return {"ok": True, "removed": len(ids)}


class FeedbackIn(BaseModel):
    kind: Literal["explicit", "copy", "citation_click"] = "explicit"
    rating: int = Field(default=0, ge=-1, le=1)
    comment: str = Field(default="", max_length=4000)
    correction: str = Field(default="", max_length=8000)


@app.post("/api/traces/{trace_id}/feedback")
def feedback(trace_id: str, body: FeedbackIn, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        t = _own_trace(s, trace_id, p)
        # a thumbs-down triggers a re-check against the documents (once per answer);
        # the re-check, not the click, decides whether anything changes
        wants_recheck = body.kind == "explicit" and body.rating < 0
        done = (t.data or {}).get("recheck")
        if wants_recheck and done is None:
            rate_guard.hit(f"recheck:{p.user_id or 'service'}", 20, 3600, "Too many re-checks in the last hour. Please try again later.")
        record_feedback(
            s,
            t,
            kind=body.kind,
            rating=body.rating,
            comment=body.comment,
            correction=body.correction,
            trusted=p.is_admin,
            make_golden=not wants_recheck,
        )
        if not wants_recheck:
            return {"ok": True}
        if done is not None:
            return {"ok": True, "recheck": done}
        version, cfg = select_config(s)
        return {"ok": True, "recheck": recheck_answer(s, t, cfg, version, correction=body.correction, trusted=p.is_admin)}


@app.get("/api/kbs/{kb_id}/metrics", dependencies=[api])
def metrics(kb_id: str, days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
    with session_scope() as s:
        return kb_metrics(s, _kb(s, kb_id).id, days)


# ------------------------------------------------------------------ repair
@app.post("/api/kbs/{kb_id}/repair", dependencies=[api])
def run_repair(kb_id: str) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        job = create_job(s, "repair", kb.id, {"trigger": "manual"})
        return {"job_id": job.id}


@app.get("/api/kbs/{kb_id}/diagnoses", dependencies=[api])
def list_diagnoses(kb_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        q = select(Diagnosis).where(Diagnosis.kb_id == kb.id)
        if status:
            q = q.where(Diagnosis.status.in_(status.split(",")))
        out = []
        for d in s.scalars(q.order_by(Diagnosis.impact.desc())):
            out.append(
                {
                    "id": d.id,
                    "root_cause": d.root_cause,
                    "target": d.target,
                    "summary": d.summary,
                    "evidence": d.evidence,
                    "trace_ids": d.trace_ids,
                    "impact": round(d.impact, 2),
                    "status": d.status,
                    "created_at": _dt(d.created_at),
                    "updated_at": _dt(d.updated_at),
                }
            )
        return out


class DiagnosisUpdate(BaseModel):
    status: Literal["open", "wont_fix", "fixed", "needs_human"]


@app.patch("/api/diagnoses/{diag_id}")
def update_diagnosis(diag_id: str, body: DiagnosisUpdate, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Diagnosis, diag_id) or _404("diagnosis")
        d.status = body.status
        audit(s, p.email, "diagnosis.update", diag_id, status=body.status)
        return {"id": d.id, "status": d.status}


def _fix_out(f: Fix) -> dict[str, Any]:
    return {
        "id": f.id,
        "diagnosis_id": f.diagnosis_id,
        "kind": f.kind,
        "params": f.params,
        "risk": f.risk,
        "status": f.status,
        "rationale": f.rationale,
        "eval": f.eval,
        "config_version": f.config_version,
        "created_at": _dt(f.created_at),
        "applied_at": _dt(f.applied_at),
    }


@app.get("/api/kbs/{kb_id}/fixes", dependencies=[api])
def list_fixes(kb_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        q = select(Fix).where(Fix.kb_id == kb.id)
        if status:
            q = q.where(Fix.status.in_(status.split(",")))
        return [_fix_out(f) for f in s.scalars(q.order_by(Fix.created_at.desc()))]


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


@app.post("/api/fixes/{fix_id}/{action}")
def fix_action(fix_id: str, action: Literal["approve", "reject", "rollback", "evaluate"], body: ReasonIn | None = None, p: Principal = api) -> dict[str, Any]:
    from .repair.service import approve_fix, process_fix, reject_fix, rollback_fix

    reason = body.reason if body else ""
    try:
        if action == "approve":
            return approve_fix(fix_id, actor=p.email)
        if action == "reject":
            return reject_fix(fix_id, actor=p.email, reason=reason)
        if action == "rollback":
            return rollback_fix(fix_id, actor=p.email, reason=reason)
        with session_scope() as s:
            f = s.get(Fix, fix_id) or _404("fix")
            if f.status in ("rejected", "failed"):
                f.status = "proposed"
        return process_fix(fix_id)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}") from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/canary/evaluate", dependencies=[api])
def canary_evaluate() -> dict[str, Any]:
    from .repair.service import evaluate_canaries

    return evaluate_canaries()


# ------------------------------------------------------------------ config
def _cv_out(cv: ConfigVersion) -> dict[str, Any]:
    return {
        "version": cv.version,
        "status": cv.status,
        "parent_version": cv.parent_version,
        "canary_pct": cv.canary_pct,
        "note": cv.note,
        "created_at": _dt(cv.created_at),
        "activated_at": _dt(cv.activated_at),
    }


@app.get("/api/config", dependencies=[api])
def get_config() -> dict[str, Any]:
    with session_scope() as s:
        version, cfg = active_config(s)
        canary = canary_version(s)
        versions = s.scalars(select(ConfigVersion).order_by(ConfigVersion.version.desc()).limit(50)).all()
        return {
            "active_version": version,
            "active": cfg.model_dump(),
            "canary": {**_cv_out(canary), "data": canary.data} if canary else None,
            "versions": [_cv_out(v) for v in versions],
            "schema": RuntimeConfig.model_json_schema(),
        }


class ConfigIn(BaseModel):
    data: dict[str, Any]
    note: str = Field(default="manual edit", max_length=500)


@app.put("/api/config")
def put_config(body: ConfigIn, p: Principal = api) -> dict[str, Any]:
    try:
        cfg = RuntimeConfig.model_validate(body.data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    with session_scope() as s:
        version, _ = active_config(s)
        cv = create_version(s, cfg, status="candidate", parent=version, note=body.note)
        activate(s, cv.version, p.email, note=body.note)
        return {"active_version": cv.version}


@app.get("/api/config/{version}", dependencies=[api])
def get_config_version(version: int) -> dict[str, Any]:
    with session_scope() as s:
        cv = s.get(ConfigVersion, version) or _404("config version")
        return {**_cv_out(cv), "data": cv.data}


@app.post("/api/config/{version}/{action}")
def config_action(version: int, action: Literal["activate", "rollback"], body: ReasonIn | None = None, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        try:
            if action == "activate":
                activate(s, version, p.email, note=body.reason if body else "")
            else:
                rollback_version(s, version, p.email, body.reason if body else "manual rollback")
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        v, _ = active_config(s)
        return {"active_version": v}


# ------------------------------------------------------------------- evals
class GoldenIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    expected_answer: str = Field(default="", max_length=8000)
    expected_doc_ids: list[str] = Field(default_factory=list)


class GoldenPatch(BaseModel):
    question: str | None = None
    expected_answer: str | None = None
    expected_doc_ids: list[str] | None = None
    active: bool | None = None


def _golden_out(g: GoldenItem) -> dict[str, Any]:
    return {
        "id": g.id,
        "question": g.question,
        "expected_answer": g.expected_answer,
        "expected_doc_ids": g.expected_doc_ids,
        "origin": g.origin,
        "note": g.note,
        "active": g.active,
        "created_at": _dt(g.created_at),
    }


@app.get("/api/kbs/{kb_id}/golden", dependencies=[api])
def list_golden(kb_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        return [_golden_out(g) for g in s.scalars(select(GoldenItem).where(GoldenItem.kb_id == kb.id).order_by(GoldenItem.created_at.desc()))]


@app.post("/api/kbs/{kb_id}/golden", dependencies=[api], status_code=201)
def add_golden(kb_id: str, body: GoldenIn) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        g = GoldenItem(kb_id=kb.id, question=body.question, expected_answer=body.expected_answer, expected_doc_ids=body.expected_doc_ids, origin="manual")
        s.add(g)
        s.flush()
        return _golden_out(g)


@app.patch("/api/golden/{item_id}", dependencies=[api])
def patch_golden(item_id: str, body: GoldenPatch) -> dict[str, Any]:
    with session_scope() as s:
        g = s.get(GoldenItem, item_id) or _404("golden item")
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(g, k, v)
        return _golden_out(g)


@app.delete("/api/golden/{item_id}", dependencies=[api])
def delete_golden(item_id: str) -> dict[str, Any]:
    with session_scope() as s:
        s.execute(delete(GoldenItem).where(GoldenItem.id == item_id))
    return {"deleted": True}


@app.post("/api/kbs/{kb_id}/evals", dependencies=[api])
def start_eval(kb_id: str) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        job = create_job(s, "eval", kb.id, {})
        return {"job_id": job.id}


@app.get("/api/kbs/{kb_id}/evals", dependencies=[api])
def list_evals(kb_id: str, limit: int = Query(30, le=200)) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        rows = s.scalars(select(EvalRun).where(EvalRun.kb_id == kb.id).order_by(EvalRun.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "purpose": r.purpose, "config_version": r.config_version, "summary": r.summary, "created_at": _dt(r.created_at)} for r in rows]


@app.get("/api/evals/{run_id}", dependencies=[api])
def get_eval(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        r = s.get(EvalRun, run_id) or _404("eval run")
        return {"id": r.id, "purpose": r.purpose, "config_version": r.config_version, "summary": r.summary, "results": r.results, "created_at": _dt(r.created_at)}


@app.post("/api/kbs/{kb_id}/reembed", dependencies=[api])
def reembed(kb_id: str) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        job = create_job(s, "reembed", kb.id, {})
        return {"job_id": job.id}


# ------------------------------------------------------------------- audit
@app.get("/api/audit", dependencies=[api])
def audit_log(limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
        return [{"id": a.id, "actor": a.actor, "action": a.action, "target": a.target, "details": a.details, "created_at": _dt(a.created_at)} for a in rows]
