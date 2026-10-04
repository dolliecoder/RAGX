"""Control plane: versioned RuntimeConfig, canary routing and the audit log."""

from __future__ import annotations

import copy
import random
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import RuntimeConfig
from .db import utcnow
from .models import AuditLog, ConfigVersion


def audit(session: Session, actor: str, action: str, target: str = "", **details: Any) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, details=details))


def ensure_initial_config(session: Session) -> None:
    if session.scalar(select(func.count()).select_from(ConfigVersion)) == 0:
        session.add(
            ConfigVersion(
                version=1,
                data=RuntimeConfig().model_dump(),
                status="active",
                note="initial defaults",
                activated_at=utcnow(),
            )
        )
        session.flush()


def _load(cv: ConfigVersion) -> RuntimeConfig:
    return RuntimeConfig.model_validate(cv.data)


def active_version(session: Session) -> ConfigVersion:
    ensure_initial_config(session)
    cv = session.scalar(select(ConfigVersion).where(ConfigVersion.status == "active").order_by(ConfigVersion.version.desc()))
    if cv is None:  # should not happen; recover by activating the newest version
        cv = session.scalar(select(ConfigVersion).order_by(ConfigVersion.version.desc()))
        assert cv is not None
        cv.status = "active"
    return cv


def active_config(session: Session) -> tuple[int, RuntimeConfig]:
    cv = active_version(session)
    return cv.version, _load(cv)


def canary_version(session: Session) -> ConfigVersion | None:
    return session.scalar(select(ConfigVersion).where(ConfigVersion.status == "canary").order_by(ConfigVersion.version.desc()))


def get_version(session: Session, version: int) -> tuple[int, RuntimeConfig]:
    cv = session.get(ConfigVersion, version)
    if cv is None:
        raise KeyError(f"config version {version} not found")
    return cv.version, _load(cv)


def select_config(session: Session, rng: random.Random | None = None) -> tuple[int, RuntimeConfig]:
    """Pick the config for one live query: canary with probability canary_pct."""
    canary = canary_version(session)
    if canary is not None and (rng or random).random() < canary.canary_pct:
        return canary.version, _load(canary)
    return active_config(session)


def apply_patch(cfg: RuntimeConfig, patch: dict[str, Any]) -> RuntimeConfig:
    """``patch = {"set": {field: value}, "merge": {dict_field: {k: v}}, "remove": {dict_field: [k]}}``"""
    data = copy.deepcopy(cfg.model_dump())
    for k, v in (patch.get("set") or {}).items():
        if k not in data:
            raise KeyError(f"unknown config field {k}")
        data[k] = v
    for k, v in (patch.get("merge") or {}).items():
        if not isinstance(data.get(k), dict):
            raise KeyError(f"config field {k} is not mergeable")
        for kk, vv in v.items():
            if isinstance(data[k].get(kk), list) and isinstance(vv, list):
                data[k][kk] = list(dict.fromkeys(data[k][kk] + vv))
            else:
                data[k][kk] = vv
    for k, keys in (patch.get("remove") or {}).items():
        for kk in keys:
            data.get(k, {}).pop(kk, None)
    return RuntimeConfig.model_validate(data)


def next_version_number(session: Session) -> int:
    return (session.scalar(select(func.max(ConfigVersion.version))) or 0) + 1


def create_version(
    session: Session,
    cfg: RuntimeConfig,
    *,
    status: str,
    parent: int | None,
    note: str,
    canary_pct: float = 0.0,
) -> ConfigVersion:
    cv = ConfigVersion(
        version=next_version_number(session),
        data=cfg.model_dump(),
        status=status,
        parent_version=parent,
        note=note,
        canary_pct=canary_pct,
    )
    session.add(cv)
    session.flush()
    return cv


def activate(session: Session, version: int, actor: str, note: str = "") -> ConfigVersion:
    target = session.get(ConfigVersion, version)
    if target is None:
        raise KeyError(f"config version {version} not found")
    for cv in session.scalars(select(ConfigVersion).where(ConfigVersion.status == "active")):
        if cv.version != version:
            cv.status = "retired"
    target.status = "active"
    target.canary_pct = 0.0
    target.activated_at = utcnow()
    audit(session, actor, "config.activate", f"v{version}", note=note)
    return target


def start_canary(session: Session, version: int, pct: float, actor: str) -> None:
    for cv in session.scalars(select(ConfigVersion).where(ConfigVersion.status == "canary")):
        if cv.version != version:
            cv.status = "rolled_back"
            audit(session, actor, "config.canary_superseded", f"v{cv.version}")
    cv = session.get(ConfigVersion, version)
    assert cv is not None
    cv.status = "canary"
    cv.canary_pct = pct
    cv.activated_at = utcnow()
    audit(session, actor, "config.canary_start", f"v{version}", pct=pct)


def rollback_version(session: Session, version: int, actor: str, reason: str) -> None:
    """Undo a version. If it is active, the parent becomes active again."""
    cv = session.get(ConfigVersion, version)
    if cv is None:
        raise KeyError(f"config version {version} not found")
    was_active = cv.status == "active"
    cv.status = "rolled_back"
    cv.canary_pct = 0.0
    if was_active:
        parent = cv.parent_version
        target = session.get(ConfigVersion, parent) if parent else None
        if target is None or target.status not in ("retired", "active"):
            target = session.scalar(
                select(ConfigVersion)
                .where(ConfigVersion.version < version, ConfigVersion.status.in_(["retired", "active"]))
                .order_by(ConfigVersion.version.desc())
            )
        if target is not None:
            activate(session, target.version, actor, note=f"rollback of v{version}")
    audit(session, actor, "config.rollback", f"v{version}", reason=reason)
