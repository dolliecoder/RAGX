"""'Continue with Google': the OAuth flow and the attacks it must stop."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD, make_client

CLIENT_ID = "test-client.apps.googleusercontent.com"


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.signature"


@pytest.fixture()
def google(env, monkeypatch):
    from ragx import config

    monkeypatch.setenv("RAGX_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("RAGX_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("RAGX_APP_URL", "http://testserver")
    config.get_settings.cache_clear()
    admin = make_client(monkeypatch)  # admin@college.test via password
    state = {"claims": {}, "status": 200, "posted": {}}

    def fake_post(url, data=None, timeout=None, **kw):
        assert url == "https://oauth2.googleapis.com/token"
        state["posted"] = data
        return httpx.Response(state["status"], json={"id_token": _jwt(state["claims"]), "access_token": "x"})

    import ragx.oauth_google as og

    monkeypatch.setattr(og.httpx, "post", fake_post)
    yield admin, state
    admin.__exit__(None, None, None)


def _browser():
    from ragx.api import app

    return TestClient(app, headers={"x-ragx-csrf": "1"}, follow_redirects=False)


def _start(c, **params):
    r = c.get("/api/auth/google/start", params=params)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q


def _claims(real_nonce, **over):
    base = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "maya@gmail.com",
        "email_verified": True,
        "name": "Maya",
        "nonce": real_nonce,
        "exp": time.time() + 600,
    }
    base.update(over)
    return base


def test_options_and_start_redirect(google):
    c = _browser()
    assert c.get("/api/auth/options").json()["google_enabled"] is True
    q = _start(c, next="/ask")
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == ["http://testserver/api/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    assert c.cookies.get("ragx_oauth") == q["state"][0]


def test_new_user_signs_up_with_google(google):
    _, st = google
    c = _browser()
    q = _start(c, next="/ask")
    st["claims"] = _claims(q["nonce"][0])
    r = c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.status_code == 302 and r.headers["location"] == "/ask"
    assert st["posted"]["code_verifier"] and st["posted"]["redirect_uri"].endswith("/api/auth/google/callback")
    me = c.get("/api/auth/me").json()["user"]
    assert me["email"] == "maya@gmail.com" and me["role"] == "user"
    assert me["email_verified"] is True and me["google_linked"] is True and me["has_password"] is False
    # a Google-only user can set a password without knowing an old one
    assert c.post("/api/auth/password", json={"new_password": "Maya-pass-123"}).status_code == 200
    assert _browser().post("/api/auth/login", json={"email": "maya@gmail.com", "password": "Maya-pass-123"}).status_code == 200


def test_existing_password_account_is_linked_not_duplicated(google):
    admin, st = google
    from ragx.api import app

    TestClient(app, headers={"x-ragx-csrf": "1"}).post("/api/auth/signup", json={"email": "maya@gmail.com", "password": PASSWORD})
    c = _browser()
    q = _start(c)
    st["claims"] = _claims(q["nonce"][0])
    c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    me = c.get("/api/auth/me").json()["user"]
    assert me["google_linked"] and me["has_password"]
    assert len([u for u in admin.get("/api/users").json() if u["email"] == "maya@gmail.com"]) == 1


@pytest.mark.parametrize(
    "override, message",
    [
        ({"aud": "someone-else"}, "could not be verified"),
        ({"iss": "https://evil.example"}, "could not be verified"),
        ({"exp": time.time() - 5}, "could not be verified"),
        ({"nonce": "replayed"}, "could not be verified"),
        ({"email_verified": False}, "not verified"),
    ],
)
def test_bad_tokens_are_rejected(google, override, message):
    _, st = google
    c = _browser()
    q = _start(c)
    st["claims"] = _claims(q["nonce"][0], **override)
    r = c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.status_code == 302 and r.headers["location"].startswith("/login?error=")
    assert message.replace(" ", "%20") in r.headers["location"]
    assert c.get("/api/auth/me").status_code == 401


def test_state_must_match_this_browser(google):
    _, st = google
    victim = _browser()
    q = _start(victim)
    st["claims"] = _claims(q["nonce"][0])
    attacker = _browser()  # no state cookie: a forged callback link must not log anyone in
    r = attacker.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert "/login?error=" in r.headers["location"] and attacker.get("/api/auth/me").status_code == 401
    # and a state works only once
    r = victim.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.headers["location"] == "/ask"
    victim.cookies.set("ragx_oauth", q["state"][0], path="/api/auth/google")
    r = victim.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert "/login?error=" in r.headers["location"]


def test_policy_applies_to_google_signups(google):
    admin, st = google
    pol = admin.get("/api/access").json()
    pol["join_code"] = "TEAM-1"
    admin.put("/api/access", json=pol)
    c = _browser()
    q = _start(c)
    st["claims"] = _claims(q["nonce"][0])
    r = c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert "invite%20code" in r.headers["location"]
    c = _browser()
    q = _start(c, code="TEAM-1")
    st["claims"] = _claims(q["nonce"][0])
    assert c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]}).headers["location"] == "/ask"


def test_open_redirect_is_blocked(google):
    _, st = google
    c = _browser()
    q = _start(c, next="//evil.example/steal")
    st["claims"] = _claims(q["nonce"][0])
    r = c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.headers["location"] == "/ask"


def test_cancel_and_disabled_account(google):
    admin, st = google
    c = _browser()
    r = c.get("/api/auth/google/callback", params={"error": "access_denied"})
    assert "cancelled" in r.headers["location"]
    q = _start(c)
    st["claims"] = _claims(q["nonce"][0])
    c.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    uid = c.get("/api/auth/me").json()["user"]["id"]
    admin.patch(f"/api/users/{uid}", json={"active": False})
    c2 = _browser()
    q = _start(c2)
    st["claims"] = _claims(q["nonce"][0])
    r = c2.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert "disabled" in r.headers["location"]


def test_google_off_by_default(env, monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/auth/options").json()["google_enabled"] is False
    r = TestClient(c.app, follow_redirects=False).get("/api/auth/google/start")
    assert r.status_code == 302 and "not%20set%20up" in r.headers["location"]
