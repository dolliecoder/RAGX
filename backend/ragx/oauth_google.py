"""'Continue with Google' (OAuth 2.0 authorization code flow + OpenID Connect).

Security:
- PKCE (S256) and a random ``state`` stored server-side *and* in a short-lived
  cookie, so the callback only works in the browser that started the sign-in.
- A ``nonce`` inside the ID token prevents token replay.
- The ID token comes straight from Google's token endpoint over TLS using our
  client secret; per OpenID Connect Core 3.1.3.7 that channel authenticates the
  issuer, and we validate iss, aud, exp, nonce and email_verified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete

from .api_accounts import _set_session_cookie, _ua
from .auth import AuthError, create_session, google_signin, rate_guard
from .config import get_settings
from .control import audit
from .db import session_scope, utcnow
from .deps import client_ip
from .models import OAuthState

log = logging.getLogger("ragx.google")
router = APIRouter()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_COOKIE = "ragx_oauth"
COOKIE_PATH = "/api/auth/google"
STATE_TTL = timedelta(minutes=10)


def enabled() -> bool:
    st = get_settings()
    return bool(st.google_client_id.strip() and st.google_client_secret.strip())


def redirect_uri() -> str:
    return get_settings().app_url.rstrip("/") + "/api/auth/google/callback"


def _safe_next(path: str) -> str:
    # same-site paths only (no //host or scheme)
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path and len(path) < 500:
        return path
    return "/ask"


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as e:
        raise AuthError("Google returned an unreadable sign-in token.", 400) from e


def _fail(message: str) -> RedirectResponse:
    resp = RedirectResponse("/login?error=" + quote(message), status_code=302)
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


@router.get("/api/auth/google/start")
def google_start(request: Request, next: str = "/ask", code: str = "") -> RedirectResponse:
    if not enabled():
        return _fail("Google sign-in is not set up on this server.")
    try:
        rate_guard.hit(f"google:{client_ip(request)}", 30, 3600, "Too many sign-in attempts. Try again later.")
    except AuthError as e:
        return _fail(str(e))
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    with session_scope() as s:
        s.execute(delete(OAuthState).where(OAuthState.expires_at < utcnow()))  # housekeeping
        s.add(
            OAuthState(
                id=_sha(state),
                nonce=nonce,
                verifier=verifier,
                next_path=_safe_next(next),
                join_code=code[:200],
                expires_at=utcnow() + STATE_TTL,
            )
        )
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    resp = RedirectResponse(AUTH_URL + "?" + urlencode(params), status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        state,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path=COOKIE_PATH,
    )
    return resp


@router.get("/api/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error:
        return _fail("Google sign-in was cancelled." if error == "access_denied" else f"Google sign-in failed ({error}).")
    if not enabled():
        return _fail("Google sign-in is not set up on this server.")
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    if not state or not code or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return _fail("Your sign-in session expired. Please try again.")
    with session_scope() as s:
        row = s.get(OAuthState, _sha(state))
        if row is None:
            return _fail("Your sign-in session expired. Please try again.")
        pending = {"nonce": row.nonce, "verifier": row.verifier, "next": row.next_path, "join_code": row.join_code}
        expired = (row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)) < utcnow()
        s.delete(row)  # single use
    if expired:
        return _fail("Your sign-in session expired. Please try again.")

    st = get_settings()
    try:
        r = httpx.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": st.google_client_id,
                "client_secret": st.google_client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": pending["verifier"],
            },
            timeout=15,
        )
    except httpx.HTTPError as e:
        log.warning("google token exchange failed: %s", e)
        return _fail("Could not reach Google. Please try again.")
    if r.status_code != 200:
        log.warning("google token exchange rejected: %s %s", r.status_code, r.text[:300])
        return _fail("Google did not accept the sign-in. Please try again.")
    try:
        claims = decode_jwt_payload(r.json().get("id_token", ""))
    except (AuthError, ValueError):
        return _fail("Google returned an unreadable sign-in token.")

    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    exp = claims.get("exp")
    checks = {
        "issuer": claims.get("iss") in ISSUERS,
        "audience": st.google_client_id in auds,
        "expiry": isinstance(exp, (int, float)) and exp > time.time(),
        "nonce": hmac.compare_digest(str(claims.get("nonce", "")), pending["nonce"]),
        "subject": bool(claims.get("sub")),
    }
    problems = [name for name, ok in checks.items() if not ok]
    if problems:
        log.warning("rejected google id token: %s", problems)
        return _fail("Google sign-in could not be verified. Please try again.")
    if not claims.get("email") or claims.get("email_verified") not in (True, "true"):
        return _fail("Your Google account's email address is not verified.")

    try:
        with session_scope() as s:
            user = google_signin(
                s, sub=str(claims["sub"]), email=claims["email"], name=claims.get("name", ""), join_code=pending["join_code"]
            )
            token = create_session(s, user, ip=client_ip(request), user_agent=_ua(request))
            audit(s, user.email, "auth.google_signin", user.id)
    except AuthError as e:
        if "join code" in str(e) and not pending["join_code"]:
            return _fail("This site needs an invite code. Use your invite link, or enter the code on the sign-up page first.")
        return _fail(str(e))
    resp = RedirectResponse(pending["next"], status_code=302)
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    _set_session_cookie(resp, token)
    return resp
