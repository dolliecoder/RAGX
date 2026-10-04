"""Evaluation harness: golden set + targeted failures, LLM-judge rubric, retrieval recall."""

from __future__ import annotations

import logging
import random
from statistics import mean
from typing import Any

from sqlalchemy import select

from . import prompts
from .config import RuntimeConfig
from .control import active_config
from .db import session_scope
from .jobs import handler
from .llm import ProviderError, get_registry
from .models import Chunk, EvalRun, GoldenItem, Job
from .reflex.engine import QueryEngine, QueryOptions
from .util import run_parallel

log = logging.getLogger("ragx.evals")

PASS_SCORE = 0.7


def golden_items(kb_id: str, sample: int | None = None, seed: int = 7) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(GoldenItem).where(GoldenItem.kb_id == kb_id, GoldenItem.active.is_(True))).all()
        items = [
            {"id": g.id, "question": g.question, "expected": g.expected_answer, "expected_doc_ids": list(g.expected_doc_ids or []), "kind": "golden"}
            for g in rows
        ]
    if sample and len(items) > sample:
        items = random.Random(seed).sample(items, sample)
    return items


def _judge(item: dict[str, Any], result: dict[str, Any], chunk_texts: dict[str, str]) -> dict[str, Any]:
    cites = [
        {"id": f"C{c['n']}", "title": c.get("title", ""), "text": chunk_texts.get(c.get("chunk_id") or "", c.get("cited_text", ""))[:2000]}
        for c in result.get("citations", [])
    ]
    try:
        resp = get_registry().call("judge", prompts.judge(item["question"], item.get("expected", ""), result["answer"], cites))
        d = resp.data
        scores = {k: max(0.0, min(1.0, float(d.get(k, 0)))) for k in ("factual_accuracy", "citation_accuracy", "completeness", "source_quality", "overall")}
        scores["rationale"] = str(d.get("rationale", ""))[:500]
    except (ProviderError, ValueError, TypeError, AttributeError) as e:
        log.warning("judge failed: %s", e)
        scores = {"factual_accuracy": 0.0, "citation_accuracy": 0.0, "completeness": 0.0, "source_quality": 0.0, "overall": 0.0, "rationale": f"judge error: {e}"}
    return scores


def run_items(
    kb_id: str,
    items: list[dict[str, Any]],
    cfg: RuntimeConfig,
    version: int,
    *,
    staged_docs: frozenset[str] = frozenset(),
    workers: int = 4,
) -> list[dict[str, Any]]:
    def one(item: dict[str, Any]) -> dict[str, Any]:
        with session_scope() as s:
            eng = QueryEngine(s, kb_id, cfg, version)
            r = eng.answer(
                item["question"],
                QueryOptions(mode="auto", is_eval=True, persist=True, allow_escalation=False, staged_docs=staged_docs),
            )
            ids = [c["chunk_id"] for c in r.get("citations", []) if c.get("chunk_id")]
            texts = {c.id: c.text for c in s.scalars(select(Chunk).where(Chunk.id.in_(ids)))} if ids else {}
        scores = _judge(item, r, texts)
        exp_docs = set(item.get("expected_doc_ids") or [])
        cited_docs = {c.get("doc_id") for c in r.get("citations", []) if c.get("doc_id")}
        recall = None if not exp_docs else (1.0 if exp_docs & cited_docs else 0.0)
        if item.get("kind") == "target" and not item.get("expected"):
            passed = r["status"] == "verified"
        else:
            passed = scores["overall"] >= PASS_SCORE and r["status"] not in ("error", "failed")
            if item.get("expected") and r["status"] == "failed":
                # an honest "not found" is correct when the reference says so
                passed = scores["overall"] >= PASS_SCORE
        return {
            "item_id": item.get("id"),
            "kind": item.get("kind", "golden"),
            "question": item["question"],
            "status": r["status"],
            "trace_id": r.get("trace_id"),
            "answer": r["answer"][:2000],
            "metrics": r.get("metrics", {}),
            "judge": scores,
            "doc_recall": recall,
            "passed": bool(passed),
            "llm_calls": r["llm"]["calls"],
            "latency_ms": r["latency_ms"],
        }

    return run_parallel(one, items, max_workers=workers)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"n": 0}
    rec = [r["doc_recall"] for r in results if r["doc_recall"] is not None]
    gr = [r["metrics"].get("groundedness") for r in results if r["metrics"].get("groundedness") is not None]
    return {
        "n": len(results),
        "pass_rate": round(mean(1.0 if r["passed"] else 0.0 for r in results), 4),
        "judge_overall": round(mean(r["judge"]["overall"] for r in results), 4),
        "groundedness": round(mean(gr), 4) if gr else None,
        "verified_rate": round(mean(1.0 if r["status"] == "verified" else 0.0 for r in results), 4),
        "doc_recall": round(mean(rec), 4) if rec else None,
        "avg_llm_calls": round(mean(r["llm_calls"] for r in results), 2),
        "avg_latency_ms": int(mean(r["latency_ms"] for r in results)),
    }


def compare(base: dict[str, Any], cand: dict[str, Any], tolerance: float) -> dict[str, Any]:
    """Regression check between two summaries of the same item set."""
    if not base.get("n"):
        return {"regression": False, "reason": "no baseline items"}
    drops = {}
    for k in ("pass_rate", "judge_overall"):
        if base.get(k) is not None and cand.get(k) is not None and cand[k] < base[k] - tolerance:
            drops[k] = {"base": base[k], "candidate": cand[k]}
    return {"regression": bool(drops), "drops": drops}


def per_item_regressions(base: list[dict[str, Any]], cand: list[dict[str, Any]]) -> list[str]:
    b = {r["question"]: r["passed"] for r in base}
    return [r["question"] for r in cand if b.get(r["question"]) and not r["passed"]]


@handler("eval")
def run_eval_job(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        kb_id = job.kb_id
        version, cfg = active_config(s)
    items = golden_items(kb_id)
    results = run_items(kb_id, items, cfg, version)
    summary = summarize(results)
    with session_scope() as s:
        run = EvalRun(kb_id=kb_id, purpose="manual", config_version=version, summary=summary, results=results)
        s.add(run)
        s.flush()
        return {"eval_run_id": run.id, "summary": summary}
