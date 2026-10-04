"""Usage limits per user (plan based) and for the whole site.

Checked before a question runs; recorded after it finishes. Daily counters live in
the database (survive restarts); the per-minute window and concurrency slots are
in-process (the API runs as one process).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import Principal, load_policy
from .config import PlanLimits
from .models import UsageDay, User


class LimitError(Exception):
    def __init__(self, message: str, *, kind: str, retry_after: int):
        super().__init__(message)
        self.kind = kind
        self.retry_after = max(1, retry_after)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def seconds_to_reset() -> int:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((nxt - now).total_seconds())


def resets_at() -> str:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass
class EffectiveLimits:
    daily_questions: int
    per_minute: int
    deep_per_day: int
    max_concurrent: int


def effective_limits(user: User, plan: PlanLimits) -> EffectiveLimits:
    daily = user.daily_limit_override if user.daily_limit_override is not None else plan.daily_questions
    return EffectiveLimits(daily, plan.per_minute, plan.deep_per_day, plan.max_concurrent)


class _Live:
    """Per-minute windows and in-flight counts (in process)."""

    def __init__(self) -> None:
        self.minute: dict[str, deque[float]] = defaultdict(deque)
        self.inflight: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.minute.clear()
            self.inflight.clear()


live = _Live()


def usage_row(session: Session, user_id: str, day: str | None = None) -> UsageDay | None:
    return session.get(UsageDay, (user_id, day or today()))


def usage_summary(session: Session, user: User) -> dict[str, Any]:
    policy = load_policy(session)
    lim = effective_limits(user, policy.limits_for(user.plan))
    row = usage_row(session, user.id)
    q = row.questions if row else 0
    d = row.deep if row else 0
    unlimited = user.role == "admin"
    return {
        "plan": user.plan,
        "questions_today": q,
        "daily_limit": None if unlimited or lim.daily_questions == 0 else lim.daily_questions,
        "remaining": None if unlimited or lim.daily_questions == 0 else max(0, lim.daily_questions - q),
        "deep_today": d,
        "deep_limit": None if unlimited else lim.deep_per_day,
        "per_minute": None if unlimited else lim.per_minute,
        "resets_at": resets_at(),
    }


@contextmanager
def reserve(session: Session, principal: Principal, *, deep: bool = False):
    """Enforce limits for one question; yields whether auto-escalation to deep
    research is still allowed. Raises LimitError when a limit is reached."""
    if principal.via == "service" or principal.user_id is None:
        yield True
        return
    user = session.get(User, principal.user_id)
    if user is None:
        raise LimitError("Account not found.", kind="account", retry_after=60)
    policy = load_policy(session)
    lim = effective_limits(user, policy.limits_for(user.plan))
    exempt = user.role == "admin"
    row = usage_row(session, user.id)
    used = row.questions if row else 0
    deep_used = row.deep if row else 0

    if not exempt:
        if lim.daily_questions and used >= lim.daily_questions:
            raise LimitError(
                f"You have used all {lim.daily_questions} questions for today. Your limit resets at midnight UTC.",
                kind="daily",
                retry_after=seconds_to_reset(),
            )
        if deep and deep_used >= lim.deep_per_day:
            raise LimitError(
                f"You have used all {lim.deep_per_day} deep research runs for today.",
                kind="deep",
                retry_after=seconds_to_reset(),
            )
    if policy.global_daily_questions:
        total = session.scalar(select(func.coalesce(func.sum(UsageDay.questions), 0)).where(UsageDay.day == today())) or 0
        if total >= policy.global_daily_questions and not exempt:
            raise LimitError(
                "The service has reached today's question limit for everyone. Please try again tomorrow.",
                kind="global",
                retry_after=seconds_to_reset(),
            )

    uid = user.id
    now = time.monotonic()
    with live.lock:
        window = live.minute[uid]
        while window and now - window[0] > 60:
            window.popleft()
        if not exempt:
            if lim.per_minute and len(window) >= lim.per_minute:
                raise LimitError(
                    f"You are asking too fast ({lim.per_minute} questions per minute). Wait a moment.",
                    kind="minute",
                    retry_after=int(60 - (now - window[0])) + 1,
                )
            if lim.max_concurrent and live.inflight[uid] >= lim.max_concurrent:
                raise LimitError("Wait for your current question to finish.", kind="concurrent", retry_after=5)
        window.append(now)
        live.inflight[uid] += 1
    try:
        yield exempt or deep_used < lim.deep_per_day
    finally:
        with live.lock:
            live.inflight[uid] = max(0, live.inflight[uid] - 1)


def record(session: Session, user_id: str | None, result: dict[str, Any]) -> None:
    """Count a finished question. Failures caused by our side ('error') are not
    charged to the student."""
    if not user_id or result.get("status") == "error":
        return
    row = usage_row(session, user_id)
    if row is None:
        row = UsageDay(user_id=user_id, day=today(), questions=0, deep=0, tokens_in=0, tokens_out=0)
        session.add(row)
    row.questions += 1
    if result.get("job_id"):
        row.deep += 1
    llm = result.get("llm") or {}
    row.tokens_in += int(llm.get("tokens_in", 0))
    row.tokens_out += int(llm.get("tokens_out", 0))
