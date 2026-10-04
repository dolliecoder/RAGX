from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, make_client, set_config


@pytest.fixture()
def client(env, monkeypatch):
    c = make_client(monkeypatch)
    yield c
    c.__exit__(None, None, None)


def test_full_http_flow(client, docs_dir):
    r = client.post("/api/kbs", json={"name": "nimbus"})
    assert r.status_code == 201
    kb = r.json()["id"]
    assert client.post("/api/kbs", json={"name": "nimbus"}).status_code == 409

    r = client.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(docs_dir)})
    assert r.status_code == 201
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    assert job["result"]["counts"]["created"] == 4

    docs = client.get(f"/api/kbs/{kb}/documents").json()
    assert len(docs) == 4 and all(d["chunks"] >= 1 for d in docs)
    detail = client.get(f"/api/documents/{docs[0]['id']}").json()
    assert detail["chunk_list"][0]["context"]

    set_config(long_context_max_tokens=0)
    r = client.post(f"/api/kbs/{kb}/query", json={"query": "What does error ERR-4012 mean?", "session_id": "abc"})
    assert r.status_code == 200
    ans = r.json()
    assert ans["status"] == "verified"
    assert any("Rate Limits" in c["title"] for c in ans["citations"])

    t = client.get(f"/api/traces/{ans['trace_id']}").json()
    assert [s["stage"] for s in t["data"]["steps"]][:3] == ["gate", "plan", "retrieve"]

    assert client.post(f"/api/traces/{ans['trace_id']}/feedback", json={"rating": -1, "correction": "It means the key was revoked."}).json()["ok"]
    golden = client.get(f"/api/kbs/{kb}/golden").json()
    assert golden and golden[0]["origin"] == "feedback"

    m = client.get(f"/api/kbs/{kb}/metrics").json()
    assert m["queries"] == 1 and m["negative_signal_rate"] == 1.0

    r = client.post(f"/api/kbs/{kb}/repair").json()
    job = client.get(f"/api/jobs/{r['job_id']}").json()
    assert job["status"] == "done", job["error"]
    assert client.get(f"/api/kbs/{kb}/diagnoses").status_code == 200

    r = client.post(f"/api/kbs/{kb}/evals").json()
    job = client.get(f"/api/jobs/{r['job_id']}").json()
    assert job["status"] == "done", job["error"]
    assert job["result"]["summary"]["n"] == 1
    assert client.get(f"/api/kbs/{kb}/evals").json()

    cfg = client.get("/api/config").json()
    data = cfg["active"]
    data["final_k"] = 15
    v = client.put("/api/config", json={"data": data, "note": "test"}).json()["active_version"]
    assert client.get("/api/config").json()["active"]["final_k"] == 15
    client.post(f"/api/config/{v}/rollback", json={"reason": "undo"})
    assert client.get("/api/config").json()["active"]["final_k"] != 15
    assert client.put("/api/config", json={"data": {"final_k": "not a number"}}).status_code == 422

    assert client.get("/api/audit").json()
    assert client.get("/api/providers").json()["roles"]["generator"]["offline"] is True


def test_upload(client):
    kb = client.post("/api/kbs", json={"name": "up"}).json()["id"]
    files = [
        ("files", ("refund_policy.md", (FIXTURES / "refund_policy.md").read_bytes(), "text/markdown")),
        ("files", ("virus.exe", b"MZ...", "application/octet-stream")),
    ]
    r = client.post(f"/api/kbs/{kb}/upload", files=files)
    assert r.status_code == 201
    body = r.json()
    assert body["saved"] == ["refund_policy.md"]
    assert body["rejected"][0]["file"] == "virus.exe"
    assert client.get(f"/api/jobs/{body['job_id']}").json()["status"] == "done"
    assert len(client.get(f"/api/kbs/{kb}/documents").json()) == 1


def test_validation_and_404(client):
    assert client.get("/api/kbs/nope").status_code == 404
    kb = client.post("/api/kbs", json={"name": "v"}).json()["id"]
    assert client.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": "Z:/does/not/exist"}).status_code == 422
    assert client.post(f"/api/kbs/{kb}/sources", json={"kind": "url", "uri": "ftp://x"}).status_code == 422
    assert client.post(f"/api/kbs/{kb}/query", json={"query": ""}).status_code == 422
    assert client.post("/api/fixes/missing/approve").status_code == 404


def test_service_key_acts_as_admin(env, monkeypatch):
    from ragx import config
    from ragx.api import app

    monkeypatch.setenv("RAGX_API_KEY", "s3cret")
    config.get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200  # health is open for probes
        assert c.get("/api/kbs").status_code == 401
        assert c.get("/api/kbs", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/kbs", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.post("/api/kbs", json={"name": "svc"}, headers={"X-API-Key": "s3cret"}).status_code == 201


def test_source_roots_restriction(env, monkeypatch, docs_dir, tmp_path):
    from ragx import config

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("RAGX_SOURCE_ROOTS", str(allowed))
    config.get_settings.cache_clear()
    with make_client(monkeypatch) as c:
        kb = c.post("/api/kbs", json={"name": "r"}).json()["id"]
        assert c.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(docs_dir)}).status_code == 403
        assert c.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(allowed)}).status_code == 201


def test_interrupted_job_resumes(env, docs_dir):
    """A job that was 'running' when the process died is resumed from its checkpoint."""
    from ragx.db import session_scope
    from ragx.jobs import JobRunner
    from ragx.models import Job, KnowledgeBase, Source

    with session_scope() as s:
        kb = KnowledgeBase(name="resume")
        s.add(kb)
        s.flush()
        src = Source(kb_id=kb.id, kind="directory", uri=str(docs_dir))
        s.add(src)
        s.flush()
        job = Job(kind="crawl", kb_id=kb.id, input={"source_id": src.id}, status="running", attempts=1)
        s.add(job)
        s.flush()
        jid = job.id
    assert JobRunner(inline=True).resume_incomplete() == 1
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == "done" and j.attempts == 2
