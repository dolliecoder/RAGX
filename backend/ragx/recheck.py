"""Re-check an answer someone marked as wrong.

A thumbs-down should not blindly change anything. Instead the question is answered
again from the documents, and the original answer *and* the person's correction are
each fact-checked by the independent verifier against freshly retrieved evidence.
The verdict decides what happens next:

- original supported, correction not   -> the answer stands; the negative signal is undone
- correction supported, original not   -> the person was right; the fresh answer replaces it
- neither / both                       -> reported honestly; repair and the admin take over

A correction only becomes an active golden test when the documents support it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from .config import RuntimeConfig
from .llm import BudgetExceeded, Meter, ProviderError, current_meter
from .models import ChunkSignal, GoldenItem, Trace
from .reflex.engine import Graded, QueryEngine, QueryOptions, TraceRecorder
from .retrieval.engine import SubQuery
from .signals import WEIGHTS, _cited_chunk_ids
from .text import sentences

log = logging.getLogger("ragx.recheck")

SUPPORTED = 0.8  # share of claims the verifier must find fully supported
MAX_CALLS = 40

MESSAGES = {
    "stands": "We re-checked your documents. The original answer is supported by them, so it stays.",
    "stands_correction_unsupported": (
        "We re-checked your documents. The original answer is supported by them, and your correction is not, "
        "so the answer stays. If the documents are out of date, update them and ask again."
    ),
    "you_were_right": "You were right. Your correction matches the documents and the original answer did not. Here is a corrected answer.",
    "fixed": "We re-checked your documents and the original answer was not fully supported. Here is a corrected answer.",
    "both": "Both the original answer and your correction are supported by the documents. Your correction may add something the answer missed.",
    "unclear": "We re-checked, but the documents don't clearly settle this. The question has been flagged for the admin to review.",
    "not_checkable": "This reply wasn't based on the documents, so there was nothing to fact-check. The question has been flagged for review.",
    "error": "The re-check couldn't run right now (the AI service is busy). Your feedback was saved and will be reviewed.",
}


def _check(eng: QueryEngine, query: str, texts: list[str], items: list[dict[str, Any]], rec: TraceRecorder) -> dict[str, Any]:
    sents = [{"text": t, "citations": []} for t in texts if t.strip()]
    if not sents:
        return {"supported": False, "grounded": 0.0, "contradicted": False, "claims": 0}
    ver = eng._verify(query, [SubQuery(query)], sents, items, rec)
    m = ver["metrics"]
    return {
        "supported": m["claims"] > 0 and m["groundedness"] >= SUPPORTED and m["contradiction"] == 0,
        "grounded": m["groundedness"],
        "contradicted": m["contradiction"] > 0,
        "claims": m["claims"],
    }


def _evidence(eng: QueryEngine, query: str, correction: str, opts: QueryOptions, rec: TraceRecorder) -> list[dict[str, Any]]:
    subqs = [SubQuery(query)] + ([SubQuery(correction[:500])] if correction.strip() else [])
    kb_tokens = eng._kb_tokens()
    limit = min(eng.cfg.long_context_max_tokens, int(eng.reg.context_tokens("verifier") * 0.6))
    if 0 < kb_tokens <= limit:
        graded: list[Graded] = eng._long_context_evidence(subqs, opts, rec)  # small KB: check against all of it
        ev = eng._evidence(graded, 0)
    else:
        diag: dict[str, Any] = {"heals": [], "rescues": [], "unsupported": [], "degraded": [], "errors": []}
        graded = eng._retrieve_and_grade(subqs, {}, opts, rec, diag)
        ev = eng._evidence(graded, eng.cfg.generate_k * 2)
    items, _labels = eng._evidence_items(ev)
    return items


def _undo_negative(session: Session, trace: Trace) -> None:
    w = WEIGHTS["explicit"]
    for cid in dict.fromkeys(_cited_chunk_ids(trace)):
        s = session.get(ChunkSignal, cid)
        if s is not None:
            s.bad = max(0.0, s.bad - w)
    trace.negative_signal = False
    trace.diagnosed = True  # nothing for the repair loop to fix


def recheck(session: Session, trace: Trace, cfg: RuntimeConfig, version: int, *, correction: str = "", trusted: bool = False) -> dict[str, Any]:
    correction = correction.strip()
    data = dict(trace.data or {})
    original = [s.get("text", "") for s in data.get("sentences") or []]
    checkable = bool(original) and trace.status not in ("direct", "error", "escalated")

    eng = QueryEngine(session, trace.kb_id, cfg, version)
    rec = TraceRecorder()
    opts = QueryOptions(mode="auto", persist=True, is_eval=True, allow_escalation=False, user_id=trace.user_id)
    result: dict[str, Any] = {"verdict": "error", "original": None, "correction": None, "updated_trace_id": None}
    meter = Meter(max_calls=MAX_CALLS)
    token = current_meter.set(meter)
    try:
        items = _evidence(eng, trace.query, correction, opts, rec)
        orig = _check(eng, trace.query, original, items, rec) if checkable else None
        corr = _check(eng, trace.query, sentences(correction) or [correction], items, rec) if correction else None
        result["original"], result["correction"] = orig, corr
    except (ProviderError, BudgetExceeded) as e:
        log.warning("recheck of %s failed: %s", trace.id, e)
        current_meter.reset(token)
        token = None
        orig = corr = None
    finally:
        if token is not None:
            current_meter.reset(token)

    if result["original"] is None and result["correction"] is None and (checkable or correction):
        verdict = "error"
    elif not checkable:
        verdict = "you_were_right" if corr and corr["supported"] else "not_checkable"
    elif corr is None:
        verdict = "stands" if orig["supported"] else "fixed"
    elif orig["supported"] and corr["supported"]:
        verdict = "both"
    elif orig["supported"]:
        verdict = "stands_correction_unsupported"
    elif corr["supported"]:
        verdict = "you_were_right"
    else:
        verdict = "fixed"

    fresh = None
    if verdict in ("fixed", "you_were_right", "both"):
        try:
            fresh = eng.answer(trace.query, QueryOptions(mode="auto", persist=True, is_eval=True, allow_escalation=False, user_id=trace.user_id))
        except (ProviderError, BudgetExceeded, ValueError) as e:
            log.warning("recheck answer for %s failed: %s", trace.id, e)
        if verdict == "fixed" and (fresh is None or fresh["status"] != "verified"):
            verdict = "unclear"  # neither the old nor a new answer could be verified
        if fresh is not None and verdict != "unclear":
            result["updated_trace_id"] = fresh.get("trace_id")
            fresh_trace = session.get(Trace, fresh.get("trace_id")) if fresh.get("trace_id") else None
            if fresh_trace is not None:
                fresh_trace.data = {**(fresh_trace.data or {}), "recheck_of": trace.id}

    if verdict.startswith("stands"):
        _undo_negative(session, trace)

    if correction:
        supported = bool(corr and corr["supported"])
        session.add(
            GoldenItem(
                kb_id=trace.kb_id,
                question=trace.query,
                expected_answer=correction,
                origin="feedback",
                # only corrections the documents back become tests; people who aren't
                # admins still need an admin to approve them
                active=supported and trusted,
                note="" if supported else "Not supported by the documents at re-check: review before enabling.",
            )
        )

    result["verdict"] = verdict
    result["message"] = MESSAGES[verdict]
    data["recheck"] = result
    trace.data = data
    log.info("recheck %s -> %s", trace.id, verdict)
    return result
