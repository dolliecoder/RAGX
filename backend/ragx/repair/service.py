"""The Repair loop: diagnose -> propose -> eval-gate -> apply by risk -> canary -> promote/rollback.

Risk policy (from the architecture):
- low    : applied automatically once it passes the eval gate
- medium : applied as a canary config version, promoted or rolled back from live metrics
- high   : evaluated (when possible) and then waits for human approval

All fixes are config or data changes, never code changes; every change is
versioned, audited and reversible.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import prompts
from ..config import RuntimeConfig
from ..control import (
    activate,
    active_config,
    apply_patch,
    audit,
    canary_version,
    create_version,
    rollback_version,
    start_canary,
)
from ..db import session_scope, utcnow
from ..evals import compare, golden_items, per_item_regressions, run_items, summarize
from ..ingestion.chunker import LARGE_STRATEGY
from ..ingestion.crawler import reingest_document
from ..ingestion.pipeline import discard_staged, promote_staged, rollback_document, stage_augmented_copy
from ..jobs import handler
from ..llm import ProviderError, get_registry
from ..models import ConfigVersion, Diagnosis, Document, EvalRun, Fix, GoldenItem, Job, Source, Trace
from .diagnoser import Finding, diagnose, mine_failures

log = logging.getLogger("ragx.repair")

RISK = {
    "augment_chunk": "low",
    "add_alias": "low",
    "rechunk_doc": "low",
    "recrawl_doc": "low",
    "set_tier": "low",
    "doc_boost": "medium",
    "gate_threshold": "medium",
    "improve_prompt": "high",
    "quarantine_doc": "high",
    "unquarantine_doc": "high",
    "content_gap": "high",
}
CONFIG_KINDS = {"add_alias", "doc_boost", "gate_threshold", "improve_prompt"}
STAGED_KINDS = {"augment_chunk", "rechunk_doc"}
MAX_FIXES_PER_CYCLE = 5
MAX_TARGETS = 8


# =============================================================== diagnoses
def _upsert_diagnosis(session: Session, kb_id: str, f: Finding) -> Diagnosis:
    d = session.scalar(
        select(Diagnosis).where(
            Diagnosis.kb_id == kb_id,
            Diagnosis.root_cause == f.root_cause,
            Diagnosis.target == f.target,
            Diagnosis.status.in_(["open", "fixing", "needs_human"]),
        )
    )
    tid = f.evidence.get("trace_id")
    if d is None:
        d = Diagnosis(kb_id=kb_id, root_cause=f.root_cause, target=f.target, summary=f.summary, evidence=f.evidence, trace_ids=[tid] if tid else [], impact=f.severity)
        session.add(d)
    else:
        if tid and tid not in d.trace_ids:
            d.trace_ids = list(d.trace_ids) + [tid]
            d.impact += f.severity
        ev = dict(d.evidence)
        for k in ("user_words", "doc_words", "questions", "examples"):
            if isinstance(f.evidence.get(k), list):
                ev[k] = list(dict.fromkeys(list(ev.get(k, [])) + f.evidence[k]))[:20]
        d.evidence = ev
        d.summary = f.summary
    session.flush()
    return d


# ================================================================ proposals
def _propose(session: Session, d: Diagnosis, cfg: RuntimeConfig) -> list[Fix]:
    existing = session.scalar(
        select(func.count()).select_from(Fix).where(
            Fix.diagnosis_id == d.id, Fix.status.in_(["proposed", "evaluating", "pending_approval", "canary"])
        )
    )
    if existing:
        return []
    ev, rc = d.evidence, d.root_cause
    out: list[tuple[str, dict[str, Any], str]] = []
    question = ev.get("question", "")
    if rc in ("vocabulary_gap", "ranking_miss", "missing_context") and ev.get("chunk_id"):
        words = [w for w in ev.get("user_words", []) if len(w) > 2][:8]
        out.append(
            (
                "augment_chunk",
                {"doc_id": ev["doc_id"], "chunk_id": ev["chunk_id"], "aliases_add": words, "context_append": f"Answers questions like: {question}" if question else ""},
                f"First-pass retrieval missed this chunk ({rc}); add the user's phrasing to its index context",
            )
        )
        if rc == "vocabulary_gap" and len(d.trace_ids) >= 2 and words and ev.get("doc_words"):
            out.append(
                ("add_alias", {"aliases": {w: ev["doc_words"][:5] for w in words[:4]}}, "Recurring vocabulary gap: expand these user terms at query time")
            )
    elif rc == "ranking_miss" and ev.get("tier") == "cold":
        out.append(("set_tier", {"doc_id": ev["doc_id"], "tier": "warm"}, "A cold-tier document is needed for live questions"))
    elif rc == "bad_chunking":
        doc_id = d.target.split(":", 1)[1]
        out.append(("rechunk_doc", {"doc_id": doc_id, "strategy": LARGE_STRATEGY}, "Answers span chunk boundaries; use larger structure-aware chunks"))
    elif rc == "stale_content":
        out.append(("recrawl_doc", {"doc_id": ev["doc_id"]}, "Source changed since indexing; re-ingest it"))
    elif rc == "missing_content":
        out.append(
            (
                "content_gap",
                {"questions": ev.get("questions") or [ev.get("question")], "urls": [u for u in [ev.get("url")] if u]},
                "The knowledge base does not contain this information; a person must add a source",
            )
        )
    elif rc == "generation_error" and d.impact >= 1.5:
        addendum, why = _improve_prompt(cfg, ev.get("examples", []))
        if addendum:
            current = cfg.prompt_addenda.get("generate", "")
            new_text = (current + "\n" + addendum).strip() if current else addendum
            out.append(("improve_prompt", {"patch": {"merge": {"prompt_addenda": {"generate": new_text}}}, "addendum": addendum}, why))
    elif rc == "conflicting_sources":
        docs = [session.get(Document, i) for i in ev.get("doc_ids", [])]
        docs = [x for x in docs if x is not None and x.status == "active"]
        if len(docs) >= 2:
            worst = min(docs, key=lambda x: (x.authority, x.modified_at.timestamp() if x.modified_at else 0))
            out.append(("quarantine_doc", {"doc_id": worst.id, "reason": "conflicts with other sources; older / lower authority"}, f"'{worst.title}' contradicts other sources"))
    elif rc == "twiddler_error":
        out.append(("unquarantine_doc", {"doc_id": ev["doc_id"]}, "A quarantined document is needed to answer questions"))
    elif rc == "injection":
        out.append(("quarantine_doc", {"doc_id": ev["doc_id"], "reason": "prompt-injection patterns"}, "Document contains prompt-injection text"))
    elif rc == "gate_error":
        out.append(("gate_threshold", {"value": round(max(0.05, cfg.gate_threshold - 0.1), 2)}, "Questions were answered without retrieval; lower the gate threshold"))

    fixes = []
    for kind, params, rationale in out:
        if kind in ("add_alias", "doc_boost", "gate_threshold") and "patch" not in params:
            params["patch"] = _patch_for(kind, params)
        fx = Fix(kb_id=d.kb_id, diagnosis_id=d.id, kind=kind, params=params, risk=RISK[kind], rationale=rationale)
        session.add(fx)
        fixes.append(fx)
    if not out:
        d.status = "needs_human" if rc in ("unexplained_negative", "generation_error") and d.impact >= 1.5 else d.status
    session.flush()
    return fixes


def _patch_for(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind == "add_alias":
        return {"merge": {"query_aliases": params["aliases"]}}
    if kind == "doc_boost":
        return {"merge": {"doc_boosts": {params["doc_id"]: params["factor"]}}}
    if kind == "gate_threshold":
        return {"set": {"gate_threshold": params["value"]}}
    raise KeyError(kind)


def _improve_prompt(cfg: RuntimeConfig, examples: list[dict[str, Any]]) -> tuple[str, str]:
    try:
        resp = get_registry().call("utility", prompts.improve_prompt("generate", cfg.prompt_addenda.get("generate", ""), examples))
        return str(resp.data.get("addendum", "")).strip()[:1200], str(resp.data.get("rationale", ""))[:500]
    except (ProviderError, ValueError, TypeError, AttributeError) as e:
        log.warning("prompt improvement failed: %s", e)
        return "", ""


def _inverse_patch(cfg: RuntimeConfig, patch: dict[str, Any]) -> dict[str, Any]:
    data = cfg.model_dump()
    inv: dict[str, Any] = {"set": {}, "merge": {}, "remove": {}}
    for k in (patch.get("set") or {}):
        inv["set"][k] = data[k]
    for k, v in (patch.get("merge") or {}).items():
        for kk in v:
            if kk in data.get(k, {}):
                inv["merge"].setdefault(k, {})[kk] = data[k][kk]
            else:
                inv["remove"].setdefault(k, []).append(kk)
    return inv


# ================================================================ eval gate
def _target_items(session: Session, d: Diagnosis | None) -> list[dict[str, Any]]:
    if d is None:
        return []
    qs = []
    for tid in d.trace_ids[-MAX_TARGETS * 2 :]:
        t = session.get(Trace, tid)
        if t is not None and t.query not in qs:
            qs.append(t.query)
    return [{"id": None, "question": q, "expected": "", "kind": "target"} for q in qs[:MAX_TARGETS]]


def _target_score(results: list[dict[str, Any]]) -> float:
    s = 0.0
    for r in results:
        if r["status"] == "verified":
            s += 1.0 if r.get("first_pass", False) else 0.6
        elif r["status"] == "partial":
            s += 0.3
    return s


def evaluate_fix(fx: Fix) -> dict[str, Any]:
    """Run target failures + golden set on the active config and on the candidate."""
    with session_scope() as s:
        version, cfg = active_config(s)
        d = s.get(Diagnosis, fx.diagnosis_id) if fx.diagnosis_id else None
        targets = _target_items(s, d)
        kind, params, kb_id = fx.kind, dict(fx.params), fx.kb_id
    golden = golden_items(kb_id, sample=cfg.eval_golden_sample)
    staged: frozenset[str] = frozenset()
    cand_cfg = cfg
    if kind in CONFIG_KINDS:
        cand_cfg = apply_patch(cfg, params["patch"])
    elif kind in STAGED_KINDS:
        staged = frozenset([params["doc_id"]])
    items = targets + golden
    base = run_items(kb_id, items, cfg, version)
    cand = run_items(kb_id, items, cand_cfg, version, staged_docs=staged)
    for res in (base, cand):
        _mark_first_pass(res)
    b_t = [r for r in base if r["kind"] == "target"]
    c_t = [r for r in cand if r["kind"] == "target"]
    b_g = [r for r in base if r["kind"] == "golden"]
    c_g = [r for r in cand if r["kind"] == "golden"]
    sb, sc = summarize(b_g), summarize(c_g)
    cmp = compare(sb, sc, cfg.eval_regression_tolerance)
    regressed_items = per_item_regressions(b_g, c_g)
    allowed = int(cfg.eval_regression_tolerance * len(b_g))
    target_base, target_cand = _target_score(b_t), _target_score(c_t)
    improved = target_cand > target_base or (not b_t and not cmp["regression"])
    passed = improved and not cmp["regression"] and len(regressed_items) <= allowed
    report = {
        "passed": passed,
        "targets": {"n": len(b_t), "baseline": round(target_base, 2), "candidate": round(target_cand, 2)},
        "golden": {"baseline": sb, "candidate": sc, "comparison": cmp, "regressed_items": regressed_items[:10]},
        "golden_missing": not golden,
        "candidate_results": [{"question": r["question"], "status": r["status"], "trace_id": r["trace_id"]} for r in c_t],
    }
    with session_scope() as s:
        s.add(EvalRun(kb_id=kb_id, purpose=f"fix:{fx.id}", config_version=version, summary={"passed": passed, **report["targets"], "golden": sc}, results=cand))
    return {"report": report, "candidate_targets": c_t}


def _mark_first_pass(results: list[dict[str, Any]]) -> None:
    """A target is solved on the first pass if no retrieval heal was needed."""
    with session_scope() as s:
        for r in results:
            t = s.get(Trace, r["trace_id"]) if r.get("trace_id") else None
            heals = ((t.data or {}).get("diag", {}).get("heals", [])) if t else []
            r["first_pass"] = not any(h.get("step") in ("H2-H4", "H2-coverage") for h in heals)


# ==================================================================== apply
def _apply(session: Session, fx: Fix, actor: str) -> None:
    kind, p = fx.kind, fx.params
    if kind in CONFIG_KINDS:
        version, cfg = active_config(session)
        new_cfg = apply_patch(cfg, p["patch"])
        cv = create_version(session, new_cfg, status="candidate", parent=version, note=f"fix {fx.id}: {fx.kind}")
        activate(session, cv.version, actor, note=fx.rationale)
        fx.config_version = cv.version
        fx.rollback = {"inverse_patch": _inverse_patch(cfg, p["patch"])}
    elif kind in STAGED_KINDS:
        v = promote_staged(session, p["doc_id"], strategy=p.get("strategy"))
        fx.rollback = {"doc_id": p["doc_id"], "version": v}
    elif kind == "recrawl_doc":
        doc = session.get(Document, p["doc_id"])
        if doc is None:
            raise KeyError(p["doc_id"])
        res = reingest_document(session, doc)
        fx.rollback = {"doc_id": doc.id, "version": doc.version, "result": res.status}
    elif kind == "set_tier":
        doc = session.get(Document, p["doc_id"])
        fx.rollback = {"doc_id": doc.id, "tier": doc.tier}
        doc.tier = p["tier"]
    elif kind == "quarantine_doc":
        doc = session.get(Document, p["doc_id"])
        fx.rollback = {"doc_id": doc.id, "status": doc.status}
        doc.status = "quarantined"
    elif kind == "unquarantine_doc":
        doc = session.get(Document, p["doc_id"])
        fx.rollback = {"doc_id": doc.id, "status": doc.status}
        doc.status = "active"
    elif kind == "content_gap":
        added = []
        for url in p.get("urls", []):
            src = Source(kb_id=fx.kb_id, kind="url", uri=url, authority=0.8)
            session.add(src)
            session.flush()
            added.append(src.id)
            from ..jobs import create_job

            create_job(session, "crawl", fx.kb_id, {"source_id": src.id, "force": True})
        fx.rollback = {"sources": added}
    else:
        raise ValueError(f"unknown fix kind {kind}")
    fx.status = "applied"
    fx.applied_at = utcnow()
    if fx.diagnosis_id:
        d = session.get(Diagnosis, fx.diagnosis_id)
        if d is not None:
            d.status = "fixed"
    audit(session, actor, "fix.apply", fx.id, kind=fx.kind, params=fx.params, risk=fx.risk)


def _record_healed_goldens(session: Session, kb_id: str, candidate_targets: list[dict[str, Any]]) -> int:
    """Every healed failure becomes a regression test."""
    n = 0
    for r in candidate_targets:
        if r["status"] != "verified":
            continue
        exists = session.scalar(select(GoldenItem).where(GoldenItem.kb_id == kb_id, GoldenItem.question == r["question"]))
        if exists is not None:
            continue
        t = session.get(Trace, r["trace_id"]) if r.get("trace_id") else None
        docs = sorted({c.get("doc_id") for c in ((t.data or {}).get("citations", []) if t else []) if c.get("doc_id")})
        session.add(GoldenItem(kb_id=kb_id, question=r["question"], expected_answer=r["answer"][:2000], expected_doc_ids=docs, origin="healed"))
        n += 1
    return n


def process_fix(fix_id: str) -> dict[str, Any]:
    """Evaluate a proposed fix and apply it according to the risk policy."""
    with session_scope() as s:
        fx = s.get(Fix, fix_id)
        if fx is None or fx.status != "proposed":
            return {"fix_id": fix_id, "skipped": True}
        fx.status = "evaluating"
        kind, risk, params = fx.kind, fx.risk, dict(fx.params)
        if kind in STAGED_KINDS:
            try:
                if kind == "augment_chunk":
                    stage_augmented_copy(s, params["doc_id"], {params["chunk_id"]: params})
                else:
                    doc = s.get(Document, params["doc_id"])
                    reingest_document(s, doc, strategy=params.get("strategy"), stage=True)
            except (KeyError, ValueError, FileNotFoundError) as e:
                fx.status = "failed"
                fx.eval = {"error": str(e)}
                return {"fix_id": fix_id, "status": "failed", "error": str(e)}
    needs_eval = kind in CONFIG_KINDS or kind in STAGED_KINDS
    evaluation: dict[str, Any] = {}
    if needs_eval:
        try:
            evaluation = evaluate_fix(fx)
        except Exception as e:  # noqa: BLE001
            log.exception("evaluation of fix %s failed", fix_id)
            with session_scope() as s:
                fx = s.get(Fix, fix_id)
                fx.status = "failed"
                fx.eval = {"error": f"{type(e).__name__}: {e}"}
                if kind in STAGED_KINDS:
                    discard_staged(s, params["doc_id"])
            return {"fix_id": fix_id, "status": "failed"}
    report = evaluation.get("report", {})
    with session_scope() as s:
        fx = s.get(Fix, fix_id)
        fx.eval = report
        if needs_eval and not report.get("passed"):
            fx.status = "rejected"
            if kind in STAGED_KINDS:
                discard_staged(s, params["doc_id"])
            audit(s, "system:repair", "fix.rejected", fix_id, kind=kind, report=report)
            return {"fix_id": fix_id, "status": "rejected"}
        if risk == "low":
            _apply(s, fx, "system:repair")
            n = _record_healed_goldens(s, fx.kb_id, evaluation.get("candidate_targets", []))
            return {"fix_id": fix_id, "status": "applied", "golden_added": n}
        if risk == "medium" and kind in CONFIG_KINDS:
            if canary_version(s) is not None:
                fx.status = "proposed"  # wait until the running canary finishes
                return {"fix_id": fix_id, "status": "queued_for_canary"}
            version, cfg = active_config(s)
            cv = create_version(s, apply_patch(cfg, params["patch"]), status="candidate", parent=version, note=f"canary for fix {fix_id}")
            start_canary(s, cv.version, cfg.canary_pct, "system:repair")
            fx.status = "canary"
            fx.config_version = cv.version
            return {"fix_id": fix_id, "status": "canary", "config_version": cv.version}
        fx.status = "pending_approval"
        if kind in STAGED_KINDS:
            discard_staged(s, params["doc_id"])
        audit(s, "system:repair", "fix.pending_approval", fix_id, kind=kind)
        return {"fix_id": fix_id, "status": "pending_approval"}


# ================================================================== human
def approve_fix(fix_id: str, actor: str = "user") -> dict[str, Any]:
    with session_scope() as s:
        fx = s.get(Fix, fix_id)
        if fx is None:
            raise KeyError(fix_id)
        if fx.status not in ("pending_approval", "canary", "proposed", "rejected"):
            raise ValueError(f"fix is {fx.status}")
        if fx.status == "canary" and fx.config_version:
            cv = s.get(ConfigVersion, fx.config_version)
            if cv is not None:
                cv.status = "retired"
                cv.canary_pct = 0
        if fx.kind in STAGED_KINDS:
            p = fx.params
            if fx.kind == "augment_chunk":
                stage_augmented_copy(s, p["doc_id"], {p["chunk_id"]: p})
            else:
                reingest_document(s, s.get(Document, p["doc_id"]), strategy=p.get("strategy"), stage=True)
        _apply(s, fx, actor)
        return {"fix_id": fix_id, "status": fx.status}


def reject_fix(fix_id: str, actor: str = "user", reason: str = "") -> dict[str, Any]:
    with session_scope() as s:
        fx = s.get(Fix, fix_id)
        if fx is None:
            raise KeyError(fix_id)
        if fx.status == "canary" and fx.config_version:
            rollback_version(s, fx.config_version, actor, reason or "fix rejected")
        fx.status = "rejected"
        if fx.diagnosis_id:
            d = s.get(Diagnosis, fx.diagnosis_id)
            if d is not None and d.status in ("open", "fixing"):
                d.status = "wont_fix" if reason else "open"
        audit(s, actor, "fix.reject", fix_id, reason=reason)
        return {"fix_id": fix_id, "status": "rejected"}


def rollback_fix(fix_id: str, actor: str = "user", reason: str = "") -> dict[str, Any]:
    with session_scope() as s:
        fx = s.get(Fix, fix_id)
        if fx is None:
            raise KeyError(fix_id)
        if fx.status not in ("applied", "canary"):
            raise ValueError(f"fix is {fx.status}, nothing to roll back")
        rb = fx.rollback or {}
        if fx.status == "canary" and fx.config_version:
            rollback_version(s, fx.config_version, actor, reason)
        elif fx.kind in CONFIG_KINDS:
            version, cfg = active_config(s)
            cv = create_version(s, apply_patch(cfg, rb["inverse_patch"]), status="candidate", parent=version, note=f"rollback of fix {fix_id}")
            activate(s, cv.version, actor, note=reason)
        elif fx.kind in STAGED_KINDS or fx.kind == "recrawl_doc":
            doc = s.get(Document, rb["doc_id"])
            if doc is None or doc.version != rb.get("version"):
                raise ValueError("document has changed since the fix; cannot roll back automatically")
            rollback_document(s, rb["doc_id"])
        elif fx.kind == "set_tier":
            s.get(Document, rb["doc_id"]).tier = rb["tier"]
        elif fx.kind in ("quarantine_doc", "unquarantine_doc"):
            s.get(Document, rb["doc_id"]).status = rb["status"]
        elif fx.kind == "content_gap":
            for sid in rb.get("sources", []):
                src = s.get(Source, sid)
                if src is not None:
                    for d in s.scalars(select(Document).where(Document.source_id == sid)):
                        d.status = "superseded"
                    s.delete(src)
        fx.status = "rolled_back"
        if fx.diagnosis_id:
            d = s.get(Diagnosis, fx.diagnosis_id)
            if d is not None:
                d.status = "open"
        audit(s, actor, "fix.rollback", fix_id, reason=reason)
        return {"fix_id": fix_id, "status": "rolled_back"}


# ================================================================== canary
def evaluate_canaries() -> dict[str, Any]:
    """Promote or roll back the running canary config from live traffic."""
    with session_scope() as s:
        cv = canary_version(s)
        if cv is None:
            return {"canary": None}
        active_v, cfg = active_config(s)
        since = cv.activated_at or cv.created_at

        def stats(version: int) -> dict[str, float]:
            rows = s.execute(
                # all live traffic counts: a canary that stops retrieving ('direct') must look worse
                select(Trace.status, Trace.negative_signal, Trace.groundedness).where(
                    Trace.config_version == version, Trace.is_eval.is_(False), Trace.created_at >= since
                )
            ).all()
            n = len(rows)
            if n == 0:
                return {"n": 0}
            return {
                "n": n,
                "verified": sum(1 for r in rows if r[0] == "verified") / n,
                "negative": sum(1 for r in rows if r[1]) / n,
                "groundedness": sum((r[2] or 0) for r in rows) / n,
            }

        c, a = stats(cv.version), stats(active_v)
        fx = s.scalar(select(Fix).where(Fix.config_version == cv.version, Fix.status == "canary"))
        if c["n"] < cfg.canary_min_samples:
            age = utcnow() - (since if since.tzinfo else since.replace(tzinfo=utcnow().tzinfo))
            if age > timedelta(days=7):
                rollback_version(s, cv.version, "system:canary", "insufficient traffic after 7 days")
                if fx:
                    fx.status = "proposed"  # re-evaluate later
                return {"canary": cv.version, "decision": "expired", "canary_stats": c}
            return {"canary": cv.version, "decision": "waiting", "canary_stats": c, "active_stats": a}
        worse = a.get("n", 0) > 0 and (
            c["verified"] < a["verified"] - 0.05 or c["negative"] > a["negative"] + 0.05 or c["groundedness"] < a["groundedness"] - 0.05
        )
        if worse:
            rollback_version(s, cv.version, "system:canary", f"canary worse than active: {c} vs {a}")
            if fx:
                fx.status = "rolled_back"
                fx.eval = {**(fx.eval or {}), "canary": {"canary": c, "active": a}}
            return {"canary": cv.version, "decision": "rolled_back", "canary_stats": c, "active_stats": a}
        # promote by re-applying the fix's patch on the *current* active config
        if fx is not None and fx.params.get("patch"):
            new_cfg = apply_patch(cfg, fx.params["patch"])
            new = create_version(s, new_cfg, status="candidate", parent=active_v, note=f"promoted canary v{cv.version}")
            activate(s, new.version, "system:canary", note="canary passed")
            cv.status = "retired"
            cv.canary_pct = 0
            fx.status = "applied"
            fx.applied_at = utcnow()
            fx.rollback = {"inverse_patch": _inverse_patch(cfg, fx.params["patch"])}
            fx.config_version = new.version
            fx.eval = {**(fx.eval or {}), "canary": {"canary": c, "active": a}}
            if fx.diagnosis_id and (d := s.get(Diagnosis, fx.diagnosis_id)) is not None:
                d.status = "fixed"
        else:
            activate(s, cv.version, "system:canary", note="canary passed")
        return {"canary": cv.version, "decision": "promoted", "canary_stats": c, "active_stats": a}


# =================================================================== cycle
def run_repair_cycle(kb_id: str, max_fixes: int = MAX_FIXES_PER_CYCLE) -> dict[str, Any]:
    report: dict[str, Any] = {"kb_id": kb_id, "traces": 0, "findings": 0, "diagnoses": [], "fixes": []}
    with session_scope() as s:
        _, cfg = active_config(s)
        traces = mine_failures(s, kb_id)
        report["traces"] = len(traces)
        touched: dict[str, Diagnosis] = {}
        for t in traces:
            for f in diagnose(s, t):
                d = _upsert_diagnosis(s, kb_id, f)
                touched[d.id] = d
                report["findings"] += 1
            t.diagnosed = True
        proposed: list[Fix] = []
        for d in sorted(touched.values(), key=lambda x: -x.impact):
            proposed += _propose(s, d, cfg)
            if d.status == "open" and proposed:
                d.status = "fixing"
        report["diagnoses"] = [{"id": d.id, "root_cause": d.root_cause, "target": d.target, "impact": d.impact, "status": d.status} for d in touched.values()]
        # also retry fixes left 'proposed' earlier (e.g. waiting for a canary slot)
        queue = s.scalars(
            select(Fix).where(Fix.kb_id == kb_id, Fix.status == "proposed").order_by(Fix.created_at.asc())
        ).all()
        impact = {d.id: d.impact for d in s.scalars(select(Diagnosis).where(Diagnosis.kb_id == kb_id))}
        order = sorted(queue, key=lambda f: -impact.get(f.diagnosis_id or "", 0))
        fix_ids = [f.id for f in order[:max_fixes]]
        audit(s, "system:repair", "repair.cycle", kb_id, traces=len(traces), findings=report["findings"], fixes=len(fix_ids))
    for fid in fix_ids:
        try:
            report["fixes"].append(process_fix(fid))
        except Exception as e:  # noqa: BLE001 - one bad fix must not stop the cycle
            log.exception("processing fix %s failed", fid)
            report["fixes"].append({"fix_id": fid, "status": "failed", "error": str(e)})
    return report


@handler("repair")
def repair_job(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        kb_id = job.kb_id
    return run_repair_cycle(kb_id)
