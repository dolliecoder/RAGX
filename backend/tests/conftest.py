from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "docs"

# Offline, deterministic test environment (set before ragx settings are read).
os.environ.update(
    {
        "RAGX_GENERATOR": "fake:gen",
        "RAGX_VERIFIER": "fake:ver",
        "RAGX_UTILITY": "fake:util",
        "RAGX_EMBEDDER": "hash:hash-512",
        "RAGX_RERANKER": "lexical",
        "RAGX_REPAIR_INTERVAL": "0",
        "RAGX_CRAWL_INTERVAL": "0",
        "RAGX_CANARY_INTERVAL": "0",
        "RAGX_WEB_SEARCH_PROVIDER": "none",
    }
)
for k in ("RAGX_ANTHROPIC_API_KEY", "RAGX_OPENAI_API_KEY", "RAGX_GEMINI_API_KEY", "RAGX_API_KEY", "RAGX_ADMIN_EMAILS", "RAGX_SMTP_HOST"):
    os.environ.pop(k, None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh database, registry, index cache and an inline job runner per test."""
    from ragx import config, db
    from ragx.jobs import JobRunner, set_runner
    from ragx.llm import set_registry
    from ragx.retrieval import index

    pg = os.environ.get("RAGX_TEST_DATABASE_URL")  # run the suite against PostgreSQL+pgvector
    monkeypatch.setenv("RAGX_DATABASE_URL", pg or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RAGX_DATA_DIR", str(tmp_path / "data"))
    config.get_settings.cache_clear()
    db.reset_engine()
    if pg:
        from ragx import models  # noqa: F401

        db.Base.metadata.drop_all(db.get_engine())
    set_registry(None)
    index.invalidate()
    set_runner(JobRunner(inline=True))
    from ragx import limits
    from ragx.auth import login_guard

    limits.live.reset()
    login_guard.reset()
    from ragx import mailer
    from ragx.auth import rate_guard

    rate_guard.reset()
    mailer.outbox.clear()
    from ragx.bootstrap import init_app

    init_app()
    yield tmp_path
    db.reset_engine()
    set_registry(None)
    index.invalidate()
    config.get_settings.cache_clear()


@pytest.fixture()
def docs_dir(tmp_path):
    d = tmp_path / "docs"
    shutil.copytree(FIXTURES, d)
    return d


def set_config(**fields):
    """Activate a new config version with the given fields."""
    from ragx.control import activate, active_config, apply_patch, create_version
    from ragx.db import session_scope

    with session_scope() as s:
        v, cfg = active_config(s)
        new = create_version(s, apply_patch(cfg, {"set": fields}), status="candidate", parent=v, note="test")
        activate(s, new.version, "test")
        return new.version


def make_kb(docs_dir, name="nimbus"):
    from ragx.db import session_scope
    from ragx.ingestion.crawler import crawl_source
    from ragx.models import KnowledgeBase, Source

    with session_scope() as s:
        kb = KnowledgeBase(name=name)
        s.add(kb)
        s.flush()
        src = Source(kb_id=kb.id, kind="directory", uri=str(docs_dir))
        s.add(src)
        s.flush()
        rep = crawl_source(s, src)
        assert not rep.error, rep.error
        return kb.id, src.id


def ask(kb_id, question, **opts):
    from ragx.control import active_config
    from ragx.db import session_scope
    from ragx.reflex.engine import QueryEngine, QueryOptions

    with session_scope() as s:
        v, cfg = active_config(s)
        return QueryEngine(s, kb_id, cfg, v).answer(question, QueryOptions(**opts))


ADMIN_EMAIL = "admin@college.test"
PASSWORD = "Sup3r-secret!"


def make_client(monkeypatch, *, admin=True):
    """TestClient that sends the CSRF header; signs up the admin account."""
    from fastapi.testclient import TestClient

    from ragx import config
    from ragx.api import app

    monkeypatch.setenv("RAGX_ADMIN_EMAILS", ADMIN_EMAIL)
    config.get_settings.cache_clear()
    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    c.__enter__()
    if admin:
        r = c.post("/api/auth/signup", json={"email": ADMIN_EMAIL, "password": PASSWORD, "name": "Admin"})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["role"] == "admin"
    return c
