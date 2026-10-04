"""DEEP mode: orchestrator-worker research (Claude Research), run as a durable
asynchronous job (Gemini Deep Research UX).

lead plan -> parallel workers (each a full Reflex loop on one objective)
-> follow-up workers for gaps -> lead synthesis over pooled evidence
-> independent verification -> trace + report. Checkpointed after every step.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import prompts
from ..control import active_config, get_version
from ..db import session_scope
from ..jobs import checkpoint, handler, load_state
from ..llm import Meter, ProviderError, current_meter, get_registry
from ..models import Job
from ..retrieval.engine import Candidate, SubQuery
from ..text import identifiers
from ..util import run_parallel
from .engine import Graded, QueryEngine, QueryOptions, TraceRecorder

log = logging.getLogger("ragx.deep")


def _compact(r: dict[str, Any], question: str) -> dict[str, Any]:
    return {
        "question": question,
        "status": r["status"],
        "answer": r["answer"],
        "citations": r["citations"],
        "unanswered": r["unanswered"],
        "metrics": r["metrics"],
        "llm_calls": r["llm"]["calls"],
    }


@handler("deep")
def run_deep(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        kb_id, inp = job.kb_id, dict(job.input)
    state = load_state(job_id)
    query = inp["query"]
    principals = set(inp.get("principals") or []) or None

    with session_scope() as s:
        if "config_version" in state:
            version, cfg = get_version(s, state["config_version"])
        else:
            version, cfg = active_config(s)
            state = checkpoint(job_id, config_version=version)

    meter = Meter(max_calls=cfg.deep_max_llm_calls)
    token = current_meter.set(meter)
    rec = TraceRecorder()
    reg = get_registry()
    try:
        # ---------------------------------------------------------- 1. plan
        if "plan" not in state:
            try:
                resp = reg.call("generator", prompts.plan(query, [], max(2, cfg.deep_workers), cfg))
                plan = [str(x.get("question", "")).strip() for x in resp.data.get("subquestions", []) if isinstance(x, dict)]
                plan = [p for p in plan if p][: cfg.deep_workers * 2]
            except (ProviderError, ValueError, TypeError, AttributeError) as e:
                log.warning("deep plan failed: %s", e)
                plan = []
            plan = plan or [query]
            state = checkpoint(job_id, plan=plan, phase="workers")
        plan: list[str] = state["plan"]
        rec.add("deep_plan", subquestions=plan)

        # ------------------------------------------------------- 2. workers
        def work(item: tuple[str, str]) -> None:
            key, question = item
            with session_scope() as s:
                eng = QueryEngine(s, kb_id, cfg, version)
                r = eng.answer(
                    question,
                    QueryOptions(mode="standard", persist=False, allow_escalation=False, meter=meter, principals=principals),
                )
            done = load_state(job_id).get("workers", {})
            done[key] = _compact(r, question)
            checkpoint(job_id, workers=done)

        workers = state.get("workers", {})
        todo = [(str(i), q) for i, q in enumerate(plan) if str(i) not in workers]
        run_parallel(work, todo, max_workers=max(1, cfg.deep_workers))
        state = load_state(job_id)
        workers = state.get("workers", {})
        rec.add("deep_workers", results={k: v["status"] for k, v in workers.items()})

        # ---------------------------------------------- 3. follow-up on gaps
        if not state.get("followups_done"):
            gaps = []
            for w in workers.values():
                if w["status"] != "verified":
                    gaps += [u for u in w["unanswered"] if u not in plan]
            gaps = list(dict.fromkeys(gaps))[: cfg.deep_workers]
            remaining = meter.remaining()
            if gaps and (remaining is None or remaining > 12):
                run_parallel(work, [(f"f{i}", g) for i, g in enumerate(gaps)], max_workers=max(1, cfg.deep_workers))
            state = checkpoint(job_id, followups_done=True, phase="synthesis")
            workers = state.get("workers", {})
            rec.add("deep_followups", gaps=gaps)

        # ------------------------------------------------------ 4. synthesis
        with session_scope() as s:
            eng = QueryEngine(s, kb_id, cfg, version)
            opts = QueryOptions(mode="deep", persist=True, allow_escalation=False, meter=meter, principals=principals, session_id=inp.get("session_id"))
            retr = eng._engine(opts)
            subqs = [SubQuery(q, identifiers=identifiers(q)) for q in plan]
            graded: list[Graded] = []
            seen: set[str] = set()
            for k, w in workers.items():
                qidx = int(k) if k.isdigit() and int(k) < len(subqs) else None
                for c in w["citations"]:
                    key = c.get("chunk_id") or c.get("url") or c.get("cited_text", "")[:60]
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    cand = retr.candidate_for(c["chunk_id"]) if c.get("chunk_id") else None
                    if cand is None:
                        cand = Candidate(
                            key=f"web:{len(seen)}", idx=None, doc_id=c.get("doc_id"), title=c.get("title", ""),
                            section=c.get("section", ""), text=c.get("cited_text", ""), url=c.get("url"),
                        )
                    sq = [qidx] if qidx is not None else list(range(len(subqs)))
                    cand.subqs = set(sq)
                    cand.final = 1.0 if w["status"] == "verified" else 0.6
                    graded.append(Graded(cand, "correct" if w["status"] == "verified" else "ambiguous", sq, [], "deep_worker"))
            diag: dict[str, Any] = {"heals": [], "rescues": [], "unsupported": [], "degraded": [], "errors": [], "deep_workers": workers}
            rec.add("deep_synthesis", evidence=len(graded))
            outcome = eng._answer_and_verify(query, subqs, graded, {}, opts, rec, diag, max_rounds=2)
            trace_id = eng._persist(query, opts, "deep", outcome, rec, diag, meter, subqs, graded, job_id)
        result = {
            "trace_id": trace_id,
            "status": outcome.status,
            "answer": outcome.answer,
            "sentences": outcome.sentences,
            "citations": outcome.citations,
            "unanswered": outcome.unanswered,
            "conflicts": outcome.conflicts,
            "metrics": outcome.metrics,
            "subquestions": plan,
            "workers": {k: {"question": w["question"], "status": w["status"]} for k, w in workers.items()},
            "llm": meter.snapshot(),
        }
        checkpoint(job_id, phase="done")
        return result
    finally:
        current_meter.reset(token)
