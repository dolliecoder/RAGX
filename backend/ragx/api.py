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
from sqlalchemy import delete, func, select

from . import __version__
from . import limits as usage_limits
from .auth import AuthError, Principal
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


class KBPatch(BaseModel):
    description: str | None = None
    visibility: Literal["all", "admins"] | None = None


def _kb(s, kb_id: str, p: Principal | None = None) -> KnowledgeBase:
    kb = s.get(KnowledgeBase, kb_id) or s.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_id))
    if kb is None or (p is not None and not p.is_admin and kb.visibility != "all"):
        _404("knowledge base")
    return kb


def _kb_out(s, kb: KnowledgeBase) -> dict[str, Any]:
    docs = s.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.id)) or 0
    tokens = s.scalar(select(func.coalesce(func.sum(Chunk.token_count), 0)).where(Chunk.kb_id == kb.id, Chunk.status == "active")) or 0
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "documents": docs,
        "tokens": int(tokens),
        "created_at": _dt(kb.created_at),
    }


@app.get("/api/kbs")
def list_kbs(p: Principal = signed_in) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(KnowledgeBase).order_by(KnowledgeBase.created_at)
        if not p.is_admin:
            q = q.where(KnowledgeBase.visibility == "all")
        return [_kb_out(s, kb) for kb in s.scalars(q)]


@app.post("/api/kbs", status_code=201)
def create_kb(body: KBIn, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        if s.scalar(select(KnowledgeBase).where(KnowledgeBase.name == body.name)):
            raise HTTPException(409, "a knowledge base with this name exists")
        kb = KnowledgeBase(name=body.name, description=body.description, visibility=body.visibility)
        s.add(kb)
        s.flush()
        audit(s, p.email, "kb.create", kb.id, name=body.name)
        return _kb_out(s, kb)


@app.get("/api/kbs/{kb_id}")
def get_kb(kb_id: str, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        return _kb_out(s, _kb(s, kb_id, p))


@app.patch("/api/kbs/{kb_id}")
def patch_kb(kb_id: str, body: KBPatch, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        if body.description is not None:
            kb.description = body.description
        if body.visibility is not None:
            kb.visibility = body.visibility
        audit(s, p.email, "kb.update", kb.id, **body.model_dump(exclude_none=True))
        return _kb_out(s, kb)


@app.delete("/api/kbs/{kb_id}")
def delete_kb(kb_id: str, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        s.delete(kb)
        audit(s, p.email, "kb.delete", kb.id, name=kb.name)
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
async def upload(kb_id: str, files: list[UploadFile] = File(...), p: Principal = api) -> dict[str, Any]:
    st = get_settings()
    with session_scope() as s:
        kb = _kb(s, kb_id)
        kb_real = kb.id
    folder = (st.data_dir / "uploads" / kb_real).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name).strip() or "upload"
        if Path(name).suffix.lower() not in SUPPORTED:
            rejected.append({"file": f.filename, "reason": "unsupported type (pdf, docx, html, md, txt)"})
            continue
        data = await f.read()
        if len(data) > 50 * 1024 * 1024:
            rejected.append({"file": f.filename, "reason": "larger than 50MB"})
            continue
        (folder / name).write_bytes(data)
        saved.append(name)
    with session_scope() as s:
        src = s.scalar(select(Source).where(Source.kb_id == kb_real, Source.kind == "directory", Source.uri == str(folder)))
        if src is None:
            src = Source(kb_id=kb_real, kind="directory", uri=str(folder), recursive=False)
            s.add(src)
            s.flush()
        job = create_job(s, "crawl", kb_real, {"source_id": src.id, "force": False}) if saved else None
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


@app.get("/api/kbs/{kb_id}/documents", dependencies=[api])
def list_documents(kb_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        kb = _kb(s, kb_id)
        counts = dict(
            s.execute(select(Chunk.doc_id, func.count()).where(Chunk.kb_id == kb.id, Chunk.status == "active").group_by(Chunk.doc_id)).all()
        )
        return [_doc_out(d, counts.get(d.id, 0)) for d in s.scalars(select(Document).where(Document.kb_id == kb.id).order_by(Document.title))]


@app.get("/api/documents/{doc_id}", dependencies=[api])
def get_document(doc_id: str) -> dict[str, Any]:
    with session_scope() as s:
        d = s.get(Document, doc_id) or _404("document")
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


class FeedbackIn(BaseModel):
    kind: Literal["explicit", "copy", "citation_click"] = "explicit"
    rating: int = Field(default=0, ge=-1, le=1)
    comment: str = Field(default="", max_length=4000)
    correction: str = Field(default="", max_length=8000)


@app.post("/api/traces/{trace_id}/feedback")
def feedback(trace_id: str, body: FeedbackIn, p: Principal = signed_in) -> dict[str, Any]:
    with session_scope() as s:
        t = _own_trace(s, trace_id, p)
        # student corrections become golden tests only after an admin reviews them
        record_feedback(
            s, t, kind=body.kind, rating=body.rating, comment=body.comment, correction=body.correction, trusted=p.is_admin
        )
        return {"ok": True}


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
