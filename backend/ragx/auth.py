"""Accounts, sessions, roles and the access policy.

- Passwords: scrypt (stdlib) with a per-user salt; constant-time verification.
- Sessions: random 256-bit tokens; only their SHA-256 is stored server-side, so a
  database leak does not leak usable sessions. Browsers get an HttpOnly cookie,
  the CLI sends the same token as ``Authorization: Bearer``.
- Roles: ``user`` (ask questions, see own history) and ``admin`` (everything).
- The service key (RAGX_API_KEY) acts as an admin for automation; it is never
  given to browsers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import AccessPolicy, get_settings
from .db import utcnow
from .models import AppSetting, AuthSession, EmailToken, User

SESSION_COOKIE = "ragx_session"
POLICY_KEY = "access_policy"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    user_id: str | None  # None for the service key
    email: str
    role: str
    plan: str
    via: str  # cookie | bearer | service

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ------------------------------------------------------------------ passwords
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(dk_b64))
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 256:
        raise AuthError("Password is too long.")
    if password.lower() == password and password.isalpha():
        raise AuthError("Use a mix of letters with numbers or symbols.")


def temp_password() -> str:
    return secrets.token_urlsafe(9)


# --------------------------------------------------------------------- policy
def load_policy(session: Session) -> AccessPolicy:
    row = session.get(AppSetting, POLICY_KEY)
    return AccessPolicy.model_validate(row.data) if row and row.data else AccessPolicy()


def save_policy(session: Session, policy: AccessPolicy) -> AccessPolicy:
    row = session.get(AppSetting, POLICY_KEY)
    if row is None:
        row = AppSetting(key=POLICY_KEY, data={})
        session.add(row)
    row.data = policy.model_dump()
    return policy


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 320:
        raise AuthError("Enter a valid email address.")
    return email


# -------------------------------------------------------------------- signup
def signup(session: Session, *, email: str, password: str, name: str = "", join_code: str = "") -> User:
    email = normalize_email(email)
    policy = load_policy(session)
    is_admin = email in admin_emails()
    if not is_admin:
        if not policy.signup_enabled:
            raise AuthError("Sign-up is closed. Ask your administrator for an account.", 403)
        domains = [d.strip().lower().lstrip("@") for d in policy.allowed_email_domains if d.strip()]
        if domains and email.rsplit("@", 1)[1] not in domains:
            raise AuthError(f"Use your college email ({', '.join('@' + d for d in domains)}).", 403)
        if policy.join_code and not hmac.compare_digest(join_code.strip(), policy.join_code):
            raise AuthError("The join code is not correct.", 403)
    validate_password(password)
    if session.scalar(select(User).where(User.email == email)):
        raise AuthError("An account with this email already exists. Sign in instead.", 409)
    user = User(
        email=email,
        name=name.strip()[:200],
        password_hash=hash_password(password),
        role="admin" if is_admin else "user",
        plan=policy.default_plan,
        email_verified=is_admin,  # bootstrap admins are trusted via server config
    )
    session.add(user)
    session.flush()
    return user


def create_user(
    session: Session, *, email: str, name: str = "", role: str = "user", plan: str | None = None, password: str | None = None
) -> tuple[User, str]:
    """Admin-created account. Returns the user and its (temporary) password."""
    email = normalize_email(email)
    if session.scalar(select(User).where(User.email == email)):
        raise AuthError("An account with this email already exists.", 409)
    pw = password or temp_password()
    if password:
        validate_password(password)
    user = User(
        email=email,
        name=name.strip()[:200],
        password_hash=hash_password(pw),
        role="admin" if role == "admin" else "user",
        plan=plan or load_policy(session).default_plan,
        must_change_password=password is None,
        email_verified=True,  # an administrator vouched for this address
    )
    session.add(user)
    session.flush()
    return user, pw


# ------------------------------------------------------------ brute force guard
class _LoginGuard:
    """Locks an email or IP after repeated failures (5 per 15 minutes)."""

    LIMIT = 5
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self.fails: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self.fails[key]
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        return q

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        with self.lock:
            for k in keys:
                q = self._recent(k, now)
                if len(q) >= self.LIMIT:
                    wait = int(self.WINDOW - (now - q[0])) // 60 + 1
                    raise AuthError(f"Too many failed sign-in attempts. Try again in {wait} minutes.", 429)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        with self.lock:
            for k in keys:
                self._recent(k, now).append(now)

    def clear(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)

    def reset(self) -> None:
        with self.lock:
            self.fails.clear()


login_guard = _LoginGuard()


def authenticate(session: Session, email: str, password: str, ip: str = "") -> User:
    try:
        email = normalize_email(email)
    except AuthError:
        raise AuthError("Wrong email or password.", 401) from None
    keys = (f"email:{email}", f"ip:{ip}") if ip else (f"email:{email}",)
    login_guard.check(*keys)
    user = session.scalar(select(User).where(User.email == email))
    # verify even when the user does not exist to keep timing uniform
    ok = verify_password(password, user.password_hash if user else hash_password("x" * 12))
    if not user or not ok:
        login_guard.fail(*keys)
        raise AuthError("Wrong email or password.", 401)
    if not user.active:
        raise AuthError("This account has been disabled. Contact your administrator.", 403)
    login_guard.clear(f"email:{email}")
    if email in admin_emails() and user.role != "admin":
        user.role = "admin"
    user.last_login_at = utcnow()
    return user


# ------------------------------------------------------------------ sessions
def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session: Session, user: User, *, ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        AuthSession(
            id=_token_id(token),
            user_id=user.id,
            expires_at=utcnow() + timedelta(days=get_settings().session_days),
            ip=ip[:64],
            user_agent=user_agent[:300],
        )
    )
    return token


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_session(session: Session, token: str) -> User | None:
    if not token or len(token) > 200:
        return None
    s = session.get(AuthSession, _token_id(token))
    if s is None:
        return None
    now = utcnow()
    if _aware(s.expires_at) <= now:
        session.delete(s)
        return None
    user = session.get(User, s.user_id)
    if user is None or not user.active:
        return None
    # sliding activity timestamps, written at most once a minute
    if (now - _aware(s.last_seen_at)).total_seconds() > 60:
        s.last_seen_at = now
        user.last_seen_at = now
    return user


def end_session(session: Session, token: str) -> None:
    if token:
        session.execute(delete(AuthSession).where(AuthSession.id == _token_id(token)))


def end_all_sessions(session: Session, user_id: str) -> None:
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


def principal_for(user: User, via: str) -> Principal:
    return Principal(user.id, user.email, user.role, user.plan, via)


SERVICE_PRINCIPAL = Principal(None, "service", "admin", "pro", "service")


# --------------------------------------------------------------- email tokens
TOKEN_TTL = {"verify": timedelta(days=3), "reset": timedelta(hours=1), "invite": timedelta(days=7)}


def create_email_token(session: Session, user: User, purpose: str) -> str:
    """Issue a single-use link token; older unused tokens of the same purpose die."""
    kind = "reset" if purpose == "invite" else purpose
    session.execute(delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.purpose == kind, EmailToken.used_at.is_(None)))
    token = secrets.token_urlsafe(32)
    session.add(EmailToken(id=_token_id(token), user_id=user.id, purpose=kind, expires_at=utcnow() + TOKEN_TTL[purpose]))
    return token


def consume_email_token(session: Session, token: str, purpose: str) -> User:
    bad = AuthError("This link is invalid or has expired. Request a new one.", 400)
    if not token or len(token) > 200:
        raise bad
    row = session.get(EmailToken, _token_id(token))
    if row is None or row.purpose != purpose or row.used_at is not None or _aware(row.expires_at) <= utcnow():
        raise bad
    user = session.get(User, row.user_id)
    if user is None or not user.active:
        raise bad
    row.used_at = utcnow()
    return user


# ------------------------------------------------------------ abuse limiter
class _RateGuard:
    """Sliding-window limiter for anonymous endpoints (sign-up, password reset)."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int, message: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                raise AuthError(message, 429)
            q.append(now)

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


rate_guard = _RateGuard()
