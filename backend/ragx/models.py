"""ORM tables."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, VectorType, new_id, utcnow


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercase
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    plan: Mapped[str] = mapped_column(String(40), default="free")
    daily_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    google_sub: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # Google account id
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthState(Base):
    """One pending "Continue with Google" attempt (10 minutes, single use)."""

    __tablename__ = "oauth_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(state)
    nonce: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(String(128))  # PKCE code verifier
    next_path: Mapped[str] = mapped_column(String(500), default="/ask")
    join_code: Mapped[str] = mapped_column(String(200), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailToken(Base):
    """Single-use link token (verify email, reset password, invite); only its hash is stored."""

    __tablename__ = "email_tokens"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))  # verify | reset
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class AuthSession(Base):
    """Server-side session; only the SHA-256 of the token is stored."""

    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class UsageDay(Base):
    __tablename__ = "usage_days"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    questions: Mapped[int] = mapped_column(Integer, default=0)
    deep: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)


class AppSetting(Base):
    """Small key/value store for non-versioned settings (e.g. the access policy)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # all = every signed-in user can ask it; admins = admins only (drafts, staff docs)
    visibility: Mapped[str] = mapped_column(String(20), default="all")
    # a workspace someone made for themselves; None = shared knowledge base made by an admin
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # display name (private workspaces keep a unique internal ``name``)
    title: Mapped[str] = mapped_column(String(200), default="")
    # answer from the model's general knowledge (clearly labelled) when the files don't cover a question
    general_knowledge: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class Source(Base):
    """Where documents come from. Crawled on a schedule (Googlebot-style)."""

    __tablename__ = "sources"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # file | directory | url
    uri: Mapped[str] = mapped_column(Text)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    authority: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    uri: Mapped[str] = mapped_column(Text)  # stable identity (path or URL)
    title: Mapped[str] = mapped_column(Text, default="")
    mime: Mapped[str] = mapped_column(String(100), default="text/plain")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    # active | quarantined | superseded | error
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    tier: Mapped[str] = mapped_column(String(10), default="warm")  # hot | warm | cold
    authority: Mapped[float] = mapped_column(Float, default=1.0)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    chunk_strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    acl: Mapped[list] = mapped_column(JSON, default=list)  # empty = public
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawl_interval_s: Mapped[int] = mapped_column(Integer, default=3600)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (Index("ix_documents_kb_uri", "kb_id", "uri", unique=True),)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    doc_version: Mapped[int] = mapped_column(Integer, default=1)
    # active | staged (blue/green candidate) | retired
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")  # Contextual Retrieval prefix
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    section_path: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding = mapped_column(VectorType, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(100), default="")
    flags: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["injection_suspect"]
    created_at: Mapped[datetime] = _created()


class ChunkSignal(Base):
    """NavBoost-style aggregated helpfulness per chunk."""

    __tablename__ = "chunk_signals"
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True)
    good: Mapped[float] = mapped_column(Float, default=0.0)
    bad: Mapped[float] = mapped_column(Float, default=0.0)


class QueryCluster(Base):
    __tablename__ = "query_clusters"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(Text)
    centroid: Mapped[list] = mapped_column(JSON)
    count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class Trace(Base):
    """The 'Glue' log: one row per answered query with every stage recorded."""

    __tablename__ = "traces"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    query: Mapped[str] = mapped_column(Text)
    query_cluster_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    route: Mapped[str] = mapped_column(String(20))  # direct | long_context | fast | standard | deep
    # verified | partial | failed | error | escalated
    status: Mapped[str] = mapped_column(String(20), index=True)
    answer: Mapped[str] = mapped_column(Text, default="")
    groundedness: Mapped[float | None] = mapped_column(Float, nullable=True)
    contradiction: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    healed: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    is_eval: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    diagnosed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    negative_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    # the asker removed it from their chat history (kept for repair/metrics)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = _id()
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"), index=True)
    # explicit | rephrase | citation_click | copy
    kind: Mapped[str] = mapped_column(String(20), default="explicit")
    rating: Mapped[int] = mapped_column(Integer, default=0)  # -1, 0, +1
    comment: Mapped[str] = mapped_column(Text, default="")
    correction: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    # active | canary | candidate | retired | rejected | rolled_back
    status: Mapped[str] = mapped_column(String(20), index=True)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canary_pct: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoldenItem(Base):
    __tablename__ = "golden_items"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str] = mapped_column(Text, default="")
    expected_doc_ids: Mapped[list] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # manual | healed | feedback
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")  # e.g. why a feedback item is disabled
    created_at: Mapped[datetime] = _created()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(40), default="manual")  # manual | fix:<id> | baseline
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()


class Diagnosis(Base):
    __tablename__ = "diagnoses"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    root_cause: Mapped[str] = mapped_column(String(40), index=True)
    target: Mapped[str] = mapped_column(Text, default="")  # grouping key (doc id, term, ...)
    summary: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    trace_ids: Mapped[list] = mapped_column(JSON, default=list)
    impact: Mapped[float] = mapped_column(Float, default=0.0)
    # open | fixing | fixed | wont_fix | needs_human
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Fix(Base):
    __tablename__ = "fixes"
    id: Mapped[str] = _id()
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    diagnosis_id: Mapped[str | None] = mapped_column(ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(10))  # low | medium | high
    # proposed | evaluating | rejected | pending_approval | canary | applied | rolled_back | failed
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    eval: Mapped[dict] = mapped_column(JSON, default=dict)
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rollback: Mapped[dict] = mapped_column(JSON, default=dict)  # what is needed to undo
    created_at: Mapped[datetime] = _created()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = _id()
    actor: Mapped[str] = mapped_column(String(40))  # system:repair | user | system:canary ...
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    """Durable background jobs (deep research, ingestion, repair) with checkpoints."""

    __tablename__ = "jobs"
    id: Mapped[str] = _id()
    kb_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30))  # deep | ingest | repair | eval | crawl
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # checkpoints
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
