"""Accounts, roles, privacy and usage limits."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_EMAIL, PASSWORD, make_client, set_config


@pytest.fixture()
def admin(env, monkeypatch, docs_dir):
    c = make_client(monkeypatch)
    kb = c.post("/api/kbs", json={"name": "course"}).json()["id"]
    c.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(docs_dir)})
    set_config(long_context_max_tokens=0)
    c.kb = kb
    yield c
    c.__exit__(None, None, None)


def student(name="stu", email=None, password=PASSWORD, **extra):
    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    r = c.post("/api/auth/signup", json={"email": email or f"{name}@college.test", "password": password, "name": name, **extra})
    assert r.status_code == 201, r.text
    return c


def ask(c, kb, q="How long do I have to request a refund on the Pro plan?", **kw):
    return c.post(f"/api/kbs/{kb}/query", json={"query": q, **kw})


# ------------------------------------------------------------------ sign-up
def test_signup_login_logout_me(admin):
    s = student()
    me = s.get("/api/auth/me").json()["user"]
    assert me["role"] == "user" and me["plan"] == "free"
    assert me["usage"]["daily_limit"] == 30
    assert s.post("/api/auth/logout").json()["ok"]
    assert s.get("/api/auth/me").status_code == 401
    r = s.post("/api/auth/login", json={"email": "STU@college.test ", "password": PASSWORD})
    assert r.status_code == 200, r.text  # email is case/space insensitive
    assert s.get("/api/auth/me").status_code == 200


def test_session_cookie_is_httponly(admin):
    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    r = c.post("/api/auth/signup", json={"email": "a@college.test", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "token" not in r.json()  # browsers never see the token


def test_duplicate_and_weak_passwords(admin):
    student("dup")
    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    assert c.post("/api/auth/signup", json={"email": "dup@college.test", "password": PASSWORD}).status_code == 409
    assert c.post("/api/auth/signup", json={"email": "new@college.test", "password": "short"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "new@college.test", "password": "onlyletters"}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "not-an-email", "password": PASSWORD}).status_code == 400


def test_domain_and_join_code_policy(admin):
    pol = admin.get("/api/access").json()
    pol.update(allowed_email_domains=["@College.test"], join_code="CS101")
    assert admin.put("/api/access", json=pol).json()["allowed_email_domains"] == ["college.test"]
    opts = admin.get("/api/auth/options").json()
    assert opts["requires_join_code"] and opts["allowed_email_domains"] == ["college.test"]

    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    r = c.post("/api/auth/signup", json={"email": "x@gmail.com", "password": PASSWORD, "join_code": "CS101"})
    assert r.status_code == 403 and "college" in r.json()["detail"]
    r = c.post("/api/auth/signup", json={"email": "x@college.test", "password": PASSWORD, "join_code": "wrong"})
    assert r.status_code == 403
    r = c.post("/api/auth/signup", json={"email": "x@college.test", "password": PASSWORD, "join_code": "CS101"})
    assert r.status_code == 201

    pol["signup_enabled"] = False
    admin.put("/api/access", json=pol)
    r = c.post("/api/auth/signup", json={"email": "y@college.test", "password": PASSWORD, "join_code": "CS101"})
    assert r.status_code == 403 and "closed" in r.json()["detail"]


def test_admin_email_bypasses_closed_signup(env, monkeypatch):
    c = make_client(monkeypatch, admin=False)
    from ragx.auth import save_policy
    from ragx.config import AccessPolicy
    from ragx.db import session_scope

    with session_scope() as s:
        save_policy(s, AccessPolicy(signup_enabled=False))
    r = c.post("/api/auth/signup", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    assert r.status_code == 201 and r.json()["user"]["role"] == "admin"


def test_brute_force_lockout(admin):
    student("victim")
    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "victim@college.test", "password": "nope-nope1"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "victim@college.test", "password": PASSWORD})
    assert r.status_code == 429  # locked even with the right password


def test_csrf_header_required_for_cookie_requests(admin):
    s = student()
    s.headers.pop("x-ragx-csrf")
    assert ask(s, admin.kb).status_code == 403
    assert s.get(f"/api/kbs/{admin.kb}").status_code == 200  # reads are fine


# -------------------------------------------------------------------- roles
def test_students_cannot_manage(admin):
    s = student()
    for method, path, body in [
        ("post", "/api/kbs", {"name": "x"}),
        ("get", "/api/users", None),
        ("get", "/api/access", None),
        ("put", "/api/config", {"data": {}}),
        ("get", f"/api/kbs/{admin.kb}/documents", None),
        ("post", f"/api/kbs/{admin.kb}/repair", None),
        ("get", f"/api/kbs/{admin.kb}/metrics", None),
        ("get", "/api/audit", None),
        ("get", "/api/providers", None),
    ]:
        r = getattr(s, method)(path, json=body) if body is not None else getattr(s, method)(path)
        assert r.status_code == 403, (path, r.status_code)
    assert s.get("/api/kbs").status_code == 200


def test_unauthenticated_is_rejected(admin):
    from ragx.api import app

    c = TestClient(app)
    assert c.get("/api/kbs").status_code == 401
    assert c.post(f"/api/kbs/{admin.kb}/query", json={"query": "x"}).status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/options").status_code == 200


def test_kb_visibility(admin):
    hidden = admin.post("/api/kbs", json={"name": "staff", "visibility": "admins"}).json()["id"]
    s = student()
    names = [k["name"] for k in s.get("/api/kbs").json()]
    assert "course" in names and "staff" not in names
    assert s.get(f"/api/kbs/{hidden}").status_code == 404
    assert ask(s, hidden).status_code == 404
    admin.patch(f"/api/kbs/{hidden}", json={"visibility": "all"})
    assert "staff" in [k["name"] for k in s.get("/api/kbs").json()]


# ------------------------------------------------------------------ privacy
def test_students_only_see_their_own_history(admin):
    a, b = student("alice"), student("bob")
    ra = ask(a, admin.kb).json()
    assert ra["status"] == "verified"
    assert ra["usage"]["questions_today"] == 1
    assert b.get(f"/api/traces/{ra['trace_id']}").status_code == 404
    assert b.post(f"/api/traces/{ra['trace_id']}/feedback", json={"rating": -1}).status_code == 404
    assert b.get(f"/api/kbs/{admin.kb}/traces").json()["total"] == 0
    assert a.get(f"/api/kbs/{admin.kb}/traces").json()["total"] == 1
    items = admin.get(f"/api/kbs/{admin.kb}/traces").json()["items"]
    assert items[0]["user_email"] == "alice@college.test"


def test_student_correction_needs_admin_review(admin):
    s = student()
    t = ask(s, admin.kb).json()["trace_id"]
    s.post(f"/api/traces/{t}/feedback", json={"rating": -1, "correction": "It is 7 days."})
    golden = admin.get(f"/api/kbs/{admin.kb}/golden").json()
    assert golden and golden[0]["origin"] == "feedback" and golden[0]["active"] is False


def test_students_cannot_assert_acl_groups(admin):
    from ragx.db import session_scope
    from ragx.models import Document

    with session_scope() as s:
        for d in s.query(Document).all():
            d.acl = ["staff"]
    st = student()
    r = ask(st, admin.kb, principals=["staff"]).json()
    assert r["citations"] == []  # the claimed group is ignored for students
    r = ask(admin, admin.kb, principals=["staff"]).json()
    assert r["citations"]


# ------------------------------------------------------------------- limits
def _set_plan(admin, **limits):
    pol = admin.get("/api/access").json()
    pol["plans"]["free"].update(limits)
    admin.put("/api/access", json=pol)
    return pol


def test_daily_limit(admin):
    _set_plan(admin, daily_questions=2)
    s = student()
    assert ask(s, admin.kb).status_code == 200
    assert ask(s, admin.kb).status_code == 200
    r = ask(s, admin.kb)
    assert r.status_code == 429
    assert r.json()["limit"] == "daily" and int(r.headers["Retry-After"]) > 0
    me = s.get("/api/auth/me").json()["user"]["usage"]
    assert me["remaining"] == 0
    # admins are never blocked
    for _ in range(3):
        assert ask(admin, admin.kb).status_code == 200


def test_per_user_override_and_plan_upgrade(admin):
    _set_plan(admin, daily_questions=1)
    s = student()
    uid = s.get("/api/auth/me").json()["user"]["id"]
    assert ask(s, admin.kb).status_code == 200
    assert ask(s, admin.kb).status_code == 429
    admin.patch(f"/api/users/{uid}", json={"daily_limit_override": 3})
    assert ask(s, admin.kb).status_code == 200
    admin.patch(f"/api/users/{uid}", json={"clear_daily_limit_override": True, "plan": "pro"})
    assert ask(s, admin.kb).status_code == 200  # pro plan: 300 per day
    assert admin.patch(f"/api/users/{uid}", json={"plan": "platinum"}).status_code == 422


def test_per_minute_limit(admin):
    _set_plan(admin, per_minute=2)
    s = student()
    assert ask(s, admin.kb).status_code == 200
    assert ask(s, admin.kb).status_code == 200
    r = ask(s, admin.kb)
    assert r.status_code == 429 and r.json()["limit"] == "minute"


def test_global_daily_cap(admin):
    pol = admin.get("/api/access").json()
    pol["global_daily_questions"] = 2
    admin.put("/api/access", json=pol)
    a, b = student("a1"), student("b1")
    assert ask(a, admin.kb).status_code == 200
    assert ask(b, admin.kb).status_code == 200
    r = ask(a, admin.kb)
    assert r.status_code == 429 and r.json()["limit"] == "global"
    usage = admin.get("/api/usage").json()
    assert usage["today"]["questions"] == 2 and usage["today"]["global_limit"] == 2
    assert {u["email"] for u in usage["top_users_today"]} == {"a1@college.test", "b1@college.test"}


def test_deep_research_limit(admin):
    _set_plan(admin, deep_per_day=1)
    s = student()
    r = ask(s, admin.kb, q="Analyze our refund policy and uptime guarantee", mode="deep").json()
    assert r["job_id"]
    assert ask(s, admin.kb, mode="deep").status_code == 429
    # the deep job belongs to the student; others cannot read it
    assert s.get(f"/api/jobs/{r['job_id']}").status_code == 200
    assert student("other").get(f"/api/jobs/{r['job_id']}").status_code == 404


def test_provider_errors_are_not_charged(admin):
    from ragx.llm import set_registry
    from ragx.llm.base import ProviderError
    from ragx.llm.fake import FakeProvider
    from ragx.llm.registry import Registry

    class Broken(FakeProvider):
        def complete(self, req):
            raise ProviderError("down")

    reg = Registry()
    reg.chains["generator"] = [Broken("x")]
    set_registry(reg)
    s = student()
    assert ask(s, admin.kb).json()["status"] == "error"
    assert s.get("/api/auth/me").json()["user"]["usage"]["questions_today"] == 0


# ------------------------------------------------------- account management
def test_admin_user_management(admin):
    r = admin.post("/api/users", json={"email": "prof@college.test", "name": "Prof"}).json()
    temp = r["temporary_password"]
    uid = r["user"]["id"]
    assert r["user"]["must_change_password"]

    from ragx.api import app

    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    assert c.post("/api/auth/login", json={"email": "prof@college.test", "password": temp}).status_code == 200
    assert c.post("/api/auth/password", json={"current_password": temp, "new_password": "N3w-password!"}).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["must_change_password"] is False

    # disabling signs the user out everywhere and blocks sign-in
    admin.patch(f"/api/users/{uid}", json={"active": False})
    assert c.get("/api/auth/me").status_code == 401
    r = c.post("/api/auth/login", json={"email": "prof@college.test", "password": "N3w-password!"})
    assert r.status_code == 403

    # reset password issues a new temporary one
    admin.patch(f"/api/users/{uid}", json={"active": True})
    temp2 = admin.post(f"/api/users/{uid}/reset-password").json()["temporary_password"]
    assert c.post("/api/auth/login", json={"email": "prof@college.test", "password": temp2}).status_code == 200

    # promote to admin, then delete
    admin.patch(f"/api/users/{uid}", json={"role": "admin"})
    assert c.get("/api/users").status_code == 200
    assert admin.delete(f"/api/users/{uid}").json()["deleted"]
    assert c.get("/api/auth/me").status_code == 401

    actions = [a["action"] for a in admin.get("/api/audit").json()]
    assert {"user.create", "user.update", "user.reset_password", "user.delete"} <= set(actions)
    assert admin.get("/api/audit").json()[0]["actor"] == ADMIN_EMAIL


def test_last_admin_is_protected(admin):
    me = admin.get("/api/auth/me").json()["user"]["id"]
    assert admin.patch(f"/api/users/{me}", json={"role": "user"}).status_code == 409
    assert admin.patch(f"/api/users/{me}", json={"active": False}).status_code == 409
    assert admin.delete(f"/api/users/{me}").status_code == 409


def test_deleted_user_questions_are_kept_anonymised(admin):
    s = student("leaver")
    t = ask(s, admin.kb).json()["trace_id"]
    uid = s.get("/api/auth/me").json()["user"]["id"]
    admin.delete(f"/api/users/{uid}")
    tr = admin.get(f"/api/traces/{t}").json()
    assert tr["user_id"] is None


def test_bearer_token_for_cli(admin):
    from ragx.api import app

    student("cli")
    c = TestClient(app, headers={"x-ragx-csrf": "1"})
    tok = c.post("/api/auth/login", json={"email": "cli@college.test", "password": PASSWORD, "return_token": True}).json()["token"]
    bare = TestClient(app)  # no cookie, no CSRF header: bearer tokens are not ambient
    r = bare.post(f"/api/kbs/{admin.kb}/query", json={"query": "What does ERR-4012 mean?"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert bare.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_sessions_expire(admin):
    from datetime import timedelta

    from ragx.db import session_scope, utcnow
    from ragx.models import AuthSession

    s = student()
    with session_scope() as db:
        for row in db.query(AuthSession).all():
            row.expires_at = utcnow() - timedelta(seconds=1)
    assert s.get("/api/auth/me").status_code == 401


def test_migration_adds_columns_to_old_database(tmp_path, monkeypatch):
    """A database created before accounts existed gains the new columns."""
    import sqlite3

    from ragx import config, db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE knowledge_bases (id VARCHAR(32) PRIMARY KEY, name VARCHAR(200), description TEXT, created_at DATETIME)")
    con.execute("INSERT INTO knowledge_bases VALUES ('k1', 'legacy', '', '2026-01-01')")
    con.commit()
    con.close()
    monkeypatch.setenv("RAGX_DATABASE_URL", f"sqlite:///{path}")
    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(knowledge_bases)")]
    vis = con.execute("SELECT visibility FROM knowledge_bases WHERE id='k1'").fetchone()[0]
    con.close()
    db.reset_engine()
    config.get_settings.cache_clear()
    assert "visibility" in cols and vis == "all"
