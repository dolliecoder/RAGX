"""Health metrics / SLOs for the dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .control import active_version, canary_version
from .db import utcnow
from .models import Chunk, Diagnosis, Document, Fix, GoldenItem, Trace


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def kb_metrics(session: Session, kb_id: str, days: int = 7) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    rows = session.scalars(
        select(Trace).where(Trace.kb_id == kb_id, Trace.is_eval.is_(False), Trace.created_at >= since).order_by(Trace.created_at)
    ).all()
    answered = [t for t in rows if t.route not in ("direct",) and t.status != "escalated"]
    by_status: dict[str, int] = defaultdict(int)
    for t in rows:
        by_status[t.status] += 1
    healed = [t for t in answered if t.healed]
    g = [t.groundedness for t in answered if t.groundedness is not None]
    c = [t.contradiction for t in answered if t.contradiction is not None]
    cov = [t.coverage for t in answered if t.coverage is not None]
    lat = [t.latency_ms for t in rows]

    # repeat failures: failing traces whose query cluster already failed before
    seen_fail: set[str] = set()
    repeat = 0
    fails = 0
    for t in answered:
        if t.status in ("failed", "partial"):
            fails += 1
            if t.query_cluster_id and t.query_cluster_id in seen_fail:
                repeat += 1
            if t.query_cluster_id:
                seen_fail.add(t.query_cluster_id)

    daily: dict[str, dict[str, Any]] = {}
    for t in answered:
        day = t.created_at.date().isoformat()
        d = daily.setdefault(day, {"day": day, "n": 0, "verified": 0, "g": []})
        d["n"] += 1
        d["verified"] += t.status == "verified"
        if t.groundedness is not None:
            d["g"].append(t.groundedness)
    series = [
        {"day": d["day"], "n": d["n"], "verified_rate": round(d["verified"] / d["n"], 3), "groundedness": round(mean(d["g"]), 3) if d["g"] else None}
        for d in daily.values()
    ]

    applied = session.scalars(select(Fix).where(Fix.kb_id == kb_id, Fix.status == "applied", Fix.applied_at.is_not(None))).all()
    heal_times = []
    for f in applied:
        d = session.get(Diagnosis, f.diagnosis_id) if f.diagnosis_id else None
        if d is not None:
            a = f.applied_at if f.applied_at.tzinfo else f.applied_at.replace(tzinfo=utcnow().tzinfo)
            b = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=utcnow().tzinfo)
            heal_times.append((a - b).total_seconds())

    n = len(answered)
    docs = session.execute(select(Document.status, func.count()).where(Document.kb_id == kb_id).group_by(Document.status)).all()
    chunks = session.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id, Chunk.status == "active")) or 0
    tokens = session.scalar(select(func.coalesce(func.sum(Chunk.token_count), 0)).where(Chunk.kb_id == kb_id, Chunk.status == "active")) or 0
    canary = canary_version(session)
    return {
        "window_days": days,
        "queries": len(rows),
        "by_status": dict(by_status),
        "verified_rate": round(by_status.get("verified", 0) / n, 4) if n else None,
        "groundedness": round(mean(g), 4) if g else None,
        "contradiction_rate": round(mean(c), 4) if c else None,
        "coverage": round(mean(cov), 4) if cov else None,
        "abstention_rate": round(sum(1 for t in answered if t.status == "failed") / n, 4) if n else None,
        "heal_rate": round(len(healed) / n, 4) if n else None,
        "heal_success_rate": round(sum(1 for t in healed if t.status == "verified") / len(healed), 4) if healed else None,
        "repeat_failure_rate": round(repeat / fails, 4) if fails else None,
        "negative_signal_rate": round(sum(1 for t in rows if t.negative_signal) / len(rows), 4) if rows else None,
        "latency_p50_ms": _pct(lat, 0.5),
        "latency_p95_ms": _pct(lat, 0.95),
        "avg_llm_calls": round(mean(t.llm_calls for t in rows), 2) if rows else None,
        "tokens": {"in": sum(t.tokens_in for t in rows), "out": sum(t.tokens_out for t in rows)},
        "mean_time_to_heal_s": int(mean(heal_times)) if heal_times else None,
        "series": series,
        "corpus": {"documents": {s: k for s, k in docs}, "chunks": chunks, "tokens": int(tokens)},
        "repair": {
            "open_diagnoses": session.scalar(select(func.count()).select_from(Diagnosis).where(Diagnosis.kb_id == kb_id, Diagnosis.status.in_(["open", "fixing"]))) or 0,
            "needs_human": session.scalar(select(func.count()).select_from(Diagnosis).where(Diagnosis.kb_id == kb_id, Diagnosis.status == "needs_human")) or 0,
            "pending_approval": session.scalar(select(func.count()).select_from(Fix).where(Fix.kb_id == kb_id, Fix.status == "pending_approval")) or 0,
            "applied_fixes": len(applied),
            "golden_items": session.scalar(select(func.count()).select_from(GoldenItem).where(GoldenItem.kb_id == kb_id, GoldenItem.active.is_(True))) or 0,
        },
        "config": {"active": active_version(session).version, "canary": canary.version if canary else None, "canary_pct": canary.canary_pct if canary else None},
    }
