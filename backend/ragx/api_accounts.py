"""Account endpoints: sign-up/sign-in, user management, access policy, usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from . import limits as usage_limits
from . import mailer
from .auth import (
    SESSION_COOKIE,
    AuthError,
    Principal,
    authenticate,
    consume_email_token,
    create_email_token,
    normalize_email,
    rate_guard,
    create_session,
    create_user,
    end_all_sessions,
    end_session,
    hash_password,
    load_policy,
    save_policy,
    signup,
    temp_password,
    validate_password,
    verify_password,
)
from .config import AccessPolicy, get_settings
from .control import audit
from .db import session_scope
from .deps import CSRF_HEADER, api, client_ip, signed_in
from .models import Job, Trace, UsageDay, User

router = APIRouter()


def _dt(v):
    return v.isoformat() if v is not None else None


def user_out(s, u: User, with_usage: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "plan": u.plan,
        "daily_limit_override": u.daily_limit_override,
        "active": u.active,
        "must_change_password": u.must_change_password,
        "email_verified": u.email_verified,
        "created_at": _dt(u.created_at),
        "last_login_at": _dt(u.last_login_at),
        "last_seen_at": _dt(u.last_seen_at),
    }
    if with_usage:
        out["usage"] = usage_limits.usage_summary(s, u)
    return out


def _require_csrf(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing CSRF header.")


def _set_session_cookie(response: Response, token: str) -> None:
    st = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=st.session_days * 86400,
        httponly=True,
        secure=st.cookie_secure,
        samesite="lax",
        path="/",
    )


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


# ===================================================================== auth
@router.get("/api/auth/options")
def auth_options() -> dict[str, Any]:
    """Public: what the sign-up form needs to show."""
    with session_scope() as s:
        pol = load_policy(s)
        return {
            "signup_enabled": pol.signup_enabled,
            "allowed_email_domains": pol.allowed_email_domains,
            "requires_join_code": bool(pol.join_code),
            "email_enabled": mailer.enabled(),
            "require_email_verification": pol.require_email_verification and mailer.enabled(),
        }


def verification_blocks(s, user: User) -> bool:
    """True if this user must confirm their email before using the service."""
    if user.role == "admin" or user.email_verified or not mailer.enabled():
        return False
    return load_policy(s).require_email_verification


class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    name: str = Field(default="", max_length=200)
    join_code: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    return_token: bool = False  # CLI: also return the session token in the body


@router.post("/api/auth/signup", status_code=201)
def auth_signup(body: SignupIn, request: Request, response: Response) -> dict[str, Any]:
    _require_csrf(request)
    rate_guard.hit(f"signup:{client_ip(request)}", 10, 3600, "Too many sign-ups from your network. Try again later.")
    with session_scope() as s:
        user = signup(s, email=body.email, password=body.password, name=body.name, join_code=body.join_code)
        user.last_login_at = user.created_at
        if mailer.enabled() and not user.email_verified:
            mailer.send_verification(user.email, create_email_token(s, user, "verify"))
        token = create_session(s, user, ip=client_ip(request), user_agent=_ua(request))
        audit(s, user.email, "auth.signup", user.id, role=user.role)
        _set_session_cookie(response, token)
        return {"user": user_out(s, user)}


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    _require_csrf(request)
    with session_scope() as s:
        user = authenticate(s, body.email, body.password, ip=client_ip(request))
        token = create_session(s, user, ip=client_ip(request), user_agent=_ua(request))
        _set_session_cookie(response, token)
        out: dict[str, Any] = {"user": user_out(s, user)}
        if body.return_token:
            out["token"] = token
        return out


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE, "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    with session_scope() as s:
        end_session(s, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def auth_me(p: Principal = signed_in) -> dict[str, Any]:
    if p.user_id is None:
        return {"user": {"id": None, "email": "service", "name": "Service key", "role": "admin", "plan": "pro", "usage": None}}
    with session_scope() as s:
        return {"user": user_out(s, s.get(User, p.user_id))}


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, response: Response, p: Principal = signed_in) -> dict[str, Any]:
    if p.user_id is None:
        raise HTTPException(400, "The service key has no password.")
    with session_scope() as s:
        user = s.get(User, p.user_id)
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(400, "Your current password is not correct.")
        validate_password(body.new_password)
        user.password_hash = hash_password(body.new_password)
        user.must_change_password = False
        end_all_sessions(s, user.id)  # sign out every other device
        token = create_session(s, user, ip=client_ip(request), user_agent=_ua(request))
        audit(s, user.email, "auth.password_change", user.id)
        _set_session_cookie(response, token)
        return {"ok": True}


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


class ForgotIn(BaseModel):
    email: str = Field(max_length=320)


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=256)


@router.post("/api/auth/verify")
def verify_email(body: TokenIn, request: Request) -> dict[str, Any]:
    _require_csrf(request)
    rate_guard.hit(f"verify-ip:{client_ip(request)}", 30, 3600, "Too many attempts. Try again later.")
    with session_scope() as s:
        user = consume_email_token(s, body.token, "verify")
        user.email_verified = True
        audit(s, user.email, "auth.email_verified", user.id)
        return {"ok": True, "email": user.email}


@router.post("/api/auth/resend-verification")
def resend_verification(p: Principal = signed_in) -> dict[str, Any]:
    if not mailer.enabled():
        raise HTTPException(409, "Email is not set up on this server.")
    if p.user_id is None:
        raise HTTPException(400, "The service key has no email address.")
    with session_scope() as s:
        user = s.get(User, p.user_id)
        if user.email_verified:
            return {"ok": True, "already_verified": True}
        rate_guard.hit(
            f"verify:{p.user_id}", 3, 3600, "We already sent a few emails. Check your inbox and spam folder, or try again in an hour."
        )
        if not mailer.send_verification(user.email, create_email_token(s, user, "verify")):
            raise HTTPException(502, "The email could not be sent. Please try again later.")
        return {"ok": True}


@router.post("/api/auth/forgot")
def forgot_password(body: ForgotIn, request: Request) -> dict[str, Any]:
    """Always answers the same way, so it cannot reveal who has an account."""
    _require_csrf(request)
    if not mailer.enabled():
        raise HTTPException(409, "Password reset by email is not set up here. Ask your administrator to reset it.")
    rate_guard.hit(f"forgot-ip:{client_ip(request)}", 10, 3600, "Too many requests. Try again later.")
    try:
        email = normalize_email(body.email)
        rate_guard.hit(f"forgot:{email}", 3, 3600, "")
    except AuthError:
        return {"ok": True}  # invalid address or flood for one address: same answer
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == email))
        if user is not None and user.active:
            mailer.send_reset(user.email, create_email_token(s, user, "reset"))
            audit(s, user.email, "auth.reset_requested", user.id)
    return {"ok": True}


@router.post("/api/auth/reset")
def reset_password(body: ResetIn, request: Request, response: Response) -> dict[str, Any]:
    _require_csrf(request)
    rate_guard.hit(f"reset-ip:{client_ip(request)}", 20, 3600, "Too many attempts. Try again later.")
    with session_scope() as s:
        validate_password(body.password)
        user = consume_email_token(s, body.token, "reset")
        user.password_hash = hash_password(body.password)
        user.must_change_password = False
        user.email_verified = True  # they proved they can read this inbox
        end_all_sessions(s, user.id)
        token = create_session(s, user, ip=client_ip(request), user_agent=_ua(request))
        audit(s, user.email, "auth.password_reset", user.id)
        _set_session_cookie(response, token)
        return {"user": user_out(s, user)}


# ==================================================================== users
class UserCreate(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=200)
    role: Literal["user", "admin"] = "user"
    plan: str | None = Field(default=None, max_length=40)


class UserPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: Literal["user", "admin"] | None = None
    plan: str | None = Field(default=None, max_length=40)
    daily_limit_override: int | None = Field(default=None, ge=0, le=100000)
    clear_daily_limit_override: bool = False
    active: bool | None = None


@router.get("/api/users")
def list_users(q: str = "", p: Principal = api) -> list[dict[str, Any]]:
    with session_scope() as s:
        stmt = select(User).order_by(User.created_at.desc())
        if q.strip():
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(User.email.like(like) | func.lower(User.name).like(like))
        return [user_out(s, u) for u in s.scalars(stmt.limit(1000))]


@router.post("/api/users", status_code=201)
def admin_create_user(body: UserCreate, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        if body.plan and body.plan not in load_policy(s).plans:
            raise HTTPException(422, f"Unknown plan '{body.plan}'.")
        user, password = create_user(s, email=body.email, name=body.name, role=body.role, plan=body.plan)
        audit(s, p.email, "user.create", user.id, email=user.email, role=user.role)
        if mailer.enabled() and mailer.send_invite(user.email, create_email_token(s, user, "invite"), p.email):
            # the person sets their own password from the link: no shared secret
            user.password_hash = hash_password(temp_password())
            user.must_change_password = False
            return {"user": user_out(s, user), "invite_sent": True}
        return {"user": user_out(s, user), "temporary_password": password, "invite_sent": False}


def _last_admin_guard(s, user: User) -> None:
    others = s.scalar(
        select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True), User.id != user.id)
    ) or 0
    if user.role == "admin" and others == 0:
        raise HTTPException(409, "This is the last active administrator. Make someone else admin first.")


@router.patch("/api/users/{user_id}")
def admin_update_user(user_id: str, body: UserPatch, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if body.role == "user" or body.active is False:
            _last_admin_guard(s, user)
        if body.plan is not None and body.plan not in load_policy(s).plans:
            raise HTTPException(422, f"Unknown plan '{body.plan}'.")
        if body.name is not None:
            user.name = body.name
        if body.role is not None:
            user.role = body.role
        if body.plan is not None:
            user.plan = body.plan
        if body.clear_daily_limit_override:
            user.daily_limit_override = None
        elif body.daily_limit_override is not None:
            user.daily_limit_override = body.daily_limit_override
        if body.active is not None:
            user.active = body.active
            if not body.active:
                end_all_sessions(s, user.id)
        audit(s, p.email, "user.update", user.id, email=user.email, **body.model_dump(exclude_none=True))
        return user_out(s, user)


class AdminResetIn(BaseModel):
    method: Literal["email", "temporary"] = "email"


@router.post("/api/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, body: AdminResetIn | None = None, p: Principal = api) -> dict[str, Any]:
    method = body.method if body else "email"
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if method == "email" and mailer.enabled() and mailer.send_reset(user.email, create_email_token(s, user, "reset")):
            audit(s, p.email, "user.reset_link_sent", user.id, email=user.email)
            return {"email_sent": True}
        password = temp_password()
        user.password_hash = hash_password(password)
        user.must_change_password = True
        end_all_sessions(s, user.id)
        audit(s, p.email, "user.reset_password", user.id, email=user.email)
        return {"temporary_password": password}


@router.delete("/api/users/{user_id}")
def admin_delete_user(user_id: str, p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.id == p.user_id:
            raise HTTPException(409, "You cannot delete your own account.")
        _last_admin_guard(s, user)
        # keep the questions for quality analysis, but detach them from the person
        s.execute(update(Trace).where(Trace.user_id == user.id).values(user_id=None, session_id=None))
        s.execute(update(Job).where(Job.user_id == user.id).values(user_id=None))
        email = user.email
        s.delete(user)
        audit(s, p.email, "user.delete", user_id, email=email)
        return {"deleted": True}


# ============================================================ access policy
@router.get("/api/access")
def get_access(p: Principal = api) -> dict[str, Any]:
    with session_scope() as s:
        return load_policy(s).model_dump()


@router.put("/api/access")
def put_access(body: dict[str, Any], p: Principal = api) -> dict[str, Any]:
    try:
        policy = AccessPolicy.model_validate(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if policy.default_plan not in policy.plans:
        raise HTTPException(422, f"Default plan '{policy.default_plan}' is not defined in plans.")
    policy.allowed_email_domains = sorted({d.strip().lower().lstrip("@") for d in policy.allowed_email_domains if d.strip()})
    policy.join_code = policy.join_code.strip()
    with session_scope() as s:
        save_policy(s, policy)
        audit(s, p.email, "access.update", "policy", signup_enabled=policy.signup_enabled, domains=policy.allowed_email_domains)
        return policy.model_dump()


# ==================================================================== usage
@router.get("/api/usage")
def usage_stats(days: int = Query(14, ge=1, le=90), p: Principal = api) -> dict[str, Any]:
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    today = usage_limits.today()
    with session_scope() as s:
        rows = s.execute(
            select(
                UsageDay.day,
                func.sum(UsageDay.questions),
                func.sum(UsageDay.deep),
                func.count(UsageDay.user_id),
                func.sum(UsageDay.tokens_in),
                func.sum(UsageDay.tokens_out),
            )
            .where(UsageDay.day >= start)
            .group_by(UsageDay.day)
            .order_by(UsageDay.day)
        ).all()
        top = s.execute(
            select(User.email, UsageDay.questions)
            .join(User, User.id == UsageDay.user_id)
            .where(UsageDay.day == today)
            .order_by(UsageDay.questions.desc())
            .limit(10)
        ).all()
        pol = load_policy(s)
        today_row = next((r for r in rows if r[0] == today), None)
        return {
            "days": [
                {
                    "day": r[0],
                    "questions": int(r[1] or 0),
                    "deep": int(r[2] or 0),
                    "active_users": int(r[3] or 0),
                    "tokens_in": int(r[4] or 0),
                    "tokens_out": int(r[5] or 0),
                }
                for r in rows
            ],
            "top_users_today": [{"email": e, "questions": q} for e, q in top],
            "today": {"questions": int(today_row[1] or 0) if today_row else 0, "global_limit": pol.global_daily_questions or None},
            "users": {
                "total": s.scalar(select(func.count()).select_from(User)) or 0,
                "active_today": int(today_row[3] or 0) if today_row else 0,
            },
        }
