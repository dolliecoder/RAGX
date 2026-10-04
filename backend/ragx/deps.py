"""Request authentication dependencies shared by all API routers."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request

from .auth import SERVICE_PRINCIPAL, SESSION_COOKIE, Principal, principal_for, resolve_session
from .config import get_settings
from .db import session_scope

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-ragx-csrf"


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


def resolve_principal(request: Request) -> Principal | None:
    key = get_settings().api_key
    x_key = request.headers.get("x-api-key")
    if key and x_key and hmac.compare_digest(x_key, key):
        return SERVICE_PRINCIPAL
    auth = request.headers.get("authorization", "")
    token, via = "", ""
    if auth.lower().startswith("bearer "):
        token, via = auth[7:].strip(), "bearer"
    elif request.cookies.get(SESSION_COOKIE):
        token, via = request.cookies[SESSION_COOKIE], "cookie"
    if not token:
        return None
    with session_scope() as s:
        user = resolve_session(s, token)
        return principal_for(user, via) if user else None


def current_user(request: Request) -> Principal:
    p = resolve_principal(request)
    if p is None:
        raise HTTPException(401, "Please sign in.")
    # Browsers attach cookies automatically, so state-changing cookie requests must
    # carry a custom header that a cross-site page cannot add (CSRF protection).
    if p.via == "cookie" and request.method in _UNSAFE and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing CSRF header.")
    return p


def require_admin(p: Principal = Depends(current_user)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, "Only administrators can do this.")
    return p


signed_in = Depends(current_user)
api = Depends(require_admin)  # default for every management endpoint
