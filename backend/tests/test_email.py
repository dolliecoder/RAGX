"""Email flows: verification, forgot/reset password, invites, abuse limits."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD, make_client


def _client():
    from ragx.api import app

    return TestClient(app, headers={"x-ragx-csrf": "1"})


def _token(kind: str) -> str:
    from ragx import mailer

    msg = mailer.outbox[-1]
    m = re.search(r"/(verify|reset)\?token=([\w\-]+)", msg["text"])
    assert m and m.group(1) == kind, msg["text"]
    return m.group(2)


@pytest.fixture()
def mail_admin(env, monkeypatch, docs_dir):
    from ragx import config

    monkeypatch.setenv("RAGX_SMTP_HOST", "memory")
    monkeypatch.setenv("RAGX_APP_URL", "https://ragx.example.edu")
    config.get_settings.cache_clear()
    c = make_client(monkeypatch)
    kb = c.post("/api/kbs", json={"name": "course"}).json()["id"]
    c.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(docs_dir)})
    c.kb = kb
    yield c
    c.__exit__(None, None, None)


def test_options_report_email(mail_admin):
    o = _client().get("/api/auth/options").json()
    assert o["email_enabled"] is True and o["require_email_verification"] is False


def test_signup_sends_verification_and_link_verifies(mail_admin):
    from ragx import mailer

    s = _client()
    s.post("/api/auth/signup", json={"email": "ana@college.test", "password": PASSWORD})
    assert mailer.outbox[-1]["to"] == "ana@college.test"
    assert "https://ragx.example.edu/verify?token=" in mailer.outbox[-1]["text"]
    assert s.get("/api/auth/me").json()["user"]["email_verified"] is False
    token = _token("verify")
    assert _client().post("/api/auth/verify", json={"token": token}).json()["ok"]
    assert s.get("/api/auth/me").json()["user"]["email_verified"] is True
    # single use
    assert _client().post("/api/auth/verify", json={"token": token}).status_code == 400


def test_required_verification_blocks_questions(mail_admin):
    pol = mail_admin.get("/api/access").json()
    pol["require_email_verification"] = True
    mail_admin.put("/api/access", json=pol)
    s = _client()
    s.post("/api/auth/signup", json={"email": "ben@college.test", "password": PASSWORD})
    r = s.post(f"/api/kbs/{mail_admin.kb}/query", json={"query": "What does ERR-4012 mean?"})
    assert r.status_code == 403 and "confirm your email" in r.json()["detail"]
    assert s.post("/api/auth/resend-verification").json()["ok"]
    _client().post("/api/auth/verify", json={"token": _token("verify")})
    assert s.post(f"/api/kbs/{mail_admin.kb}/query", json={"query": "What does ERR-4012 mean?"}).status_code == 200
    # admins are never blocked
    assert mail_admin.post(f"/api/kbs/{mail_admin.kb}/query", json={"query": "What does ERR-4012 mean?"}).status_code == 200


def test_forgot_and_reset_password(mail_admin):
    from ragx import mailer

    _client().post("/api/auth/signup", json={"email": "cy@college.test", "password": PASSWORD})
    old = _client()
    old.post("/api/auth/login", json={"email": "cy@college.test", "password": PASSWORD})
    n = len(mailer.outbox)
    assert _client().post("/api/auth/forgot", json={"email": "CY@college.test"}).json() == {"ok": True}
    assert len(mailer.outbox) == n + 1
    token = _token("reset")
    c = _client()
    assert c.post("/api/auth/reset", json={"token": token, "password": "short"}).status_code == 400
    r = c.post("/api/auth/reset", json={"token": token, "password": "Brand-new-pass1"})
    assert r.status_code == 200 and r.json()["user"]["email_verified"] is True
    assert c.get("/api/auth/me").status_code == 200  # signed in by the reset
    assert old.get("/api/auth/me").status_code == 401  # other sessions ended
    assert _client().post("/api/auth/reset", json={"token": token, "password": "Another-pass2"}).status_code == 400
    assert _client().post("/api/auth/login", json={"email": "cy@college.test", "password": "Brand-new-pass1"}).status_code == 200


def test_forgot_does_not_reveal_accounts(mail_admin):
    from ragx import mailer

    n = len(mailer.outbox)
    r = _client().post("/api/auth/forgot", json={"email": "nobody@college.test"})
    assert r.json() == {"ok": True} and len(mailer.outbox) == n


def test_reset_tokens_expire(mail_admin):
    from datetime import timedelta

    from ragx.db import session_scope, utcnow
    from ragx.models import EmailToken

    _client().post("/api/auth/signup", json={"email": "dee@college.test", "password": PASSWORD})
    _client().post("/api/auth/forgot", json={"email": "dee@college.test"})
    token = _token("reset")
    with session_scope() as s:
        for t in s.query(EmailToken).all():
            assert token not in t.id  # only the hash is stored
            t.expires_at = utcnow() - timedelta(seconds=1)
    assert _client().post("/api/auth/reset", json={"token": token, "password": "Brand-new-pass1"}).status_code == 400


def test_admin_invite_sends_link_not_password(mail_admin):
    from ragx import mailer

    r = mail_admin.post("/api/users", json={"email": "prof@college.test", "name": "Prof"}).json()
    assert r["invite_sent"] is True and "temporary_password" not in r
    assert mailer.outbox[-1]["subject"].startswith("You have been invited")
    c = _client()
    assert c.post("/api/auth/reset", json={"token": _token("reset"), "password": "Prof-pass-123"}).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["email"] == "prof@college.test"


def test_admin_reset_sends_link_or_temporary(mail_admin):
    from ragx import mailer

    s = _client()
    uid = s.post("/api/auth/signup", json={"email": "eve@college.test", "password": PASSWORD}).json()["user"]["id"]
    assert mail_admin.post(f"/api/users/{uid}/reset-password").json() == {"email_sent": True}
    assert mailer.outbox[-1]["to"] == "eve@college.test"
    r = mail_admin.post(f"/api/users/{uid}/reset-password", json={"method": "temporary"}).json()
    assert r["temporary_password"]


def test_forgot_without_email_configured(env, monkeypatch):
    c = make_client(monkeypatch)
    r = c.post("/api/auth/forgot", json={"email": "x@college.test"})
    assert r.status_code == 409 and "administrator" in r.json()["detail"]
    assert c.get("/api/auth/options").json()["email_enabled"] is False


def test_signup_flood_is_limited(env, monkeypatch):
    make_client(monkeypatch, admin=False)
    c = _client()
    codes = [c.post("/api/auth/signup", json={"email": f"bot{i}@college.test", "password": PASSWORD}).status_code for i in range(12)]
    assert codes[:10] == [201] * 10 and codes[10] == 429
