"""The Reflex loop: query-time healing.

gate -> plan -> retrieve (BM25 | dense | exact) -> fuse -> cascade rank -> twiddlers
-> CRAG grade -> [heal retrieval: widen / rewrite / cold+exact / neighbours / web]
-> generate with span citations -> independent verification
-> [heal answer: repair citations, drop unsupported claims, conflict mode, re-retrieve]
-> verified | partial (+ deep escalation) | failed

Every step is recorded in the trace; heal outcomes are what the Repair loop learns from.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import prompts
from ..config import RuntimeConfig, get_settings
from ..llm import BudgetExceeded, Meter, ProviderError, current_meter, get_registry
from ..models import Chunk, Document, Job, Trace
from ..retrieval.engine import Candidate, RetrievalEngine, SubQuery
from ..retrieval.web import web_available, web_search
from ..signals import assign_cluster, detect_rephrase, session_history
from ..text import content_terms, estimate_tokens, identifiers, overlap, sentences
from ..util import run_parallel

log = logging.getLogger("ragx.reflex")

GRADE_BATCH = 10


class TraceRecorder:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.steps: list[dict[str, Any]] = []

    def add(self, stage: str, **data: Any) -> dict[str, Any]:
        step = {"stage": stage, "t_ms": int((time.perf_counter() - self.t0) * 1000), **data}
        self.steps.append(step)
        return step

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)


@dataclass
class Graded:
    cand: Candidate
    verdict: str
    subqs: list[int]
    key_sentences: list[str]
    origin: str  # initial | widen | rewrite | cold | exact | neighbor | web | long_context


@dataclass
class QueryOptions:
    mode: str = "auto"  # auto | fast | standard | deep
    session_id: str | None = None
    user_id: str | None = None  # owner of the trace / deep job
    principals: set[str] | None = None
    staged_docs: frozenset[str] = frozenset()
    is_eval: bool = False
    persist: bool = True
    allow_escalation: bool = True
    meter: Meter | None = None
    subquestions: list[str] | None = None  # pre-planned (deep workers)


@dataclass
class Outcome:
    status: str
    answer: str
    sentences: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)  # verifier verdicts (citation numbers)


class QueryEngine:
    def __init__(self, session: Session, kb_id: str, cfg: RuntimeConfig, config_version: int):
        self.session = session
        self.kb_id = kb_id
        self.cfg = cfg
        self.config_version = config_version
        self.reg = get_registry()
        self.settings = get_settings()

    # =================================================================== entry
    def answer(self, query: str, opts: QueryOptions | None = None) -> dict[str, Any]:
        opts = opts or QueryOptions()
        query = query.strip()
        if not query:
            raise ValueError("empty query")
        rec = TraceRecorder()
        meter = opts.meter or Meter(max_calls=self.cfg.standard.max_llm_calls)
        token = current_meter.set(meter)
        diag: dict[str, Any] = {"heals": [], "rescues": [], "unsupported": [], "degraded": [], "errors": []}
        route = "standard"
        outcome: Outcome
        job_id = None
        subqs: list[SubQuery] = []
        evidence: list[Graded] = []
        try:
            history = session_history(self.session, self.kb_id, opts.session_id)
            if opts.persist and not opts.is_eval:
                prev = detect_rephrase(self.session, self.kb_id, opts.session_id, query)
                if prev is not None:
                    rec.add("implicit_feedback", rephrase_of=prev.id)
            route, gate = self._route(query, history, opts, rec)
            if route == "direct":
                outcome = self._direct(query, rec)
            elif route == "deep":
                job_id = self._start_deep(query, opts)
                rec.add("escalate", job_id=job_id, reason="research-level question")
                outcome = Outcome(
                    status="escalated",
                    answer="This needs deep research across many sources. A research job has been started; "
                    "the full verified report will be available when it finishes.",
                )
            else:
                budget = self.cfg.budget("fast" if route == "fast" else "standard")
                if opts.meter is None:
                    meter.max_calls = budget.max_llm_calls
                subqs = self._plan(query, history, route, opts, rec)
                if route == "long_context":
                    evidence = self._long_context_evidence(subqs, opts, rec)
                else:
                    evidence = self._retrieve_and_grade(subqs, gate, opts, rec, diag)
                    evidence = self._heal_retrieval(subqs, evidence, gate, opts, rec, diag, budget.max_heal_rounds)
                outcome = self._answer_and_verify(query, subqs, evidence, gate, opts, rec, diag, budget.max_heal_rounds)
                if (
                    outcome.status in ("partial", "failed")
                    and opts.allow_escalation
                    and opts.mode == "auto"
                    and route == "standard"
                    and self.cfg.auto_escalate_to_deep
                    and not opts.is_eval
                ):
                    job_id = self._start_deep(query, opts)
                    diag["heals"].append({"step": "H6", "action": "escalate_deep", "job_id": job_id})
                    rec.add("escalate", job_id=job_id, reason="standard pipeline could not fully verify the answer")
        except BudgetExceeded as e:
            diag["errors"].append(str(e))
            rec.add("budget_exhausted", detail=str(e))
            outcome = Outcome(status="failed", answer="I could not complete a verified answer within the processing budget.")
        except ProviderError as e:
            diag["errors"].append(str(e))
            rec.add("provider_error", detail=str(e))
            outcome = Outcome(status="error", answer="The language model providers are currently unavailable. Please try again shortly.")
        finally:
            current_meter.reset(token)

        result = {
            "status": outcome.status,
            "answer": outcome.answer,
            "sentences": outcome.sentences,
            "citations": outcome.citations,
            "unanswered": outcome.unanswered,
            "conflicts": outcome.conflicts,
            "route": route,
            "metrics": outcome.metrics,
            "job_id": job_id,
            "config_version": self.config_version,
            "llm": meter.snapshot(),
            "latency_ms": rec.elapsed_ms(),
            "healed": bool(diag["heals"]),
            "offline": self.reg.is_offline("generator"),
        }
        if opts.persist:
            result["trace_id"] = self._persist(query, opts, route, outcome, rec, diag, meter, subqs, evidence, job_id)
        else:
            result["trace"] = {"steps": rec.steps, "diag": diag}
        return result

    # =================================================================== route
    def _kb_tokens(self) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(Chunk.token_count), 0)).where(Chunk.kb_id == self.kb_id, Chunk.status == "active")
            )
            or 0
        )

    def _route(self, query: str, history: list[str], opts: QueryOptions, rec: TraceRecorder) -> tuple[str, dict[str, Any]]:
        gate = {"retrieval_need": 1.0, "complexity": "simple", "freshness_need": 0.0, "reason": "explicit mode"}
        if opts.mode in ("fast", "standard", "deep"):
            rec.add("gate", mode=opts.mode, **gate)
            return opts.mode, gate
        try:
            resp = self.reg.call("utility", prompts.gate(query, history, self.cfg))
            d = resp.data
            gate = {
                "retrieval_need": float(d.get("retrieval_need", 1.0)),
                "complexity": d.get("complexity", "simple") if d.get("complexity") in ("simple", "multi_part", "research") else "simple",
                "freshness_need": float(d.get("freshness_need", 0.0)),
                "reason": str(d.get("reason", ""))[:300],
            }
        except (ProviderError, ValueError, TypeError, AttributeError) as e:
            gate["reason"] = f"gate unavailable ({type(e).__name__}); defaulting to retrieval"
        if gate["retrieval_need"] < self.cfg.gate_threshold:
            route = "direct"
        elif gate["complexity"] == "research" and opts.allow_escalation and not opts.is_eval:
            route = "deep"
        else:
            kb_tokens = self._kb_tokens()
            # only read the whole KB when it fits comfortably in the answer model's window
            # (small local models have far smaller windows than cloud models)
            limit = min(self.cfg.long_context_max_tokens, int(self.reg.context_tokens("generator") * 0.6))
            if 0 < kb_tokens <= limit and not opts.staged_docs:
                route = "long_context"
            else:
                route = "fast" if gate["complexity"] == "simple" else "standard"
        rec.add("gate", route=route, threshold=self.cfg.gate_threshold, **gate)
        return route, gate

    def _direct(self, query: str, rec: TraceRecorder) -> Outcome:
        resp = self.reg.call("generator", prompts.chat(query, self.cfg))
        text = str(resp.data.get("answer", "")).strip()
        rec.add("direct_answer", chars=len(text))
        return Outcome(status="direct", answer=text, metrics={"groundedness": None, "coverage": None})

    def _start_deep(self, query: str, opts: QueryOptions) -> str:
        job = Job(
            kb_id=self.kb_id,
            user_id=opts.user_id,
            kind="deep",
            input={"query": query, "session_id": opts.session_id, "principals": sorted(opts.principals or [])},
        )
        self.session.add(job)
        self.session.flush()
        from ..jobs import get_runner

        get_runner().submit_after_commit(self.session, job.id)
        return job.id

    # ==================================================================== plan
    def _plan(self, query: str, history: list[str], route: str, opts: QueryOptions, rec: TraceRecorder) -> list[SubQuery]:
        if opts.subquestions:
            subqs = [SubQuery(q, keywords=[], identifiers=identifiers(q)) for q in opts.subquestions]
            rec.add("plan", source="provided", subquestions=[s.question for s in subqs])
            return subqs
        if route in ("fast",):
            sq = SubQuery(query, keywords=[], identifiers=identifiers(query))
            rec.add("plan", source="single", subquestions=[query])
            return [sq]
        max_n = self.cfg.standard.max_subqueries
        try:
            resp = self.reg.call("utility", prompts.plan(query, history, max_n, self.cfg))
            subqs = []
            for s in resp.data.get("subquestions", [])[:max_n]:
                q = str(s.get("question", "")).strip()
                if q:
                    subqs.append(
                        SubQuery(
                            q,
                            keywords=[str(k) for k in s.get("keywords", [])][:12],
                            identifiers=list(dict.fromkeys([str(k) for k in s.get("identifiers", [])] + identifiers(q)))[:8],
                        )
                    )
        except (ProviderError, ValueError, TypeError, AttributeError) as e:
            rec.add("plan_error", detail=str(e))
            subqs = []
        if not subqs:
            subqs = [SubQuery(query, identifiers=identifiers(query))]
        rec.add("plan", source="llm", subquestions=[s.question for s in subqs])
        return subqs

    # =============================================================== retrieval
    def _engine(self, opts: QueryOptions) -> RetrievalEngine:
        if not hasattr(self, "_eng"):
            self._eng = RetrievalEngine(
                self.session, self.kb_id, self.cfg, staged_docs=opts.staged_docs, principals=opts.principals
            )
        return self._eng

    def _grade(self, subqs: list[SubQuery], cands: list[tuple[Candidate, str]], rec: TraceRecorder) -> list[Graded]:
        if not cands:
            return []
        questions = [s.question for s in subqs]
        batches = [cands[i : i + GRADE_BATCH] for i in range(0, len(cands), GRADE_BATCH)]

        def run(batch: list[tuple[Candidate, str]]) -> list[Graded]:
            items = [
                {"id": f"P{j}", "title": c.title, "section": c.section, "text": c.text[:3500]}
                for j, (c, _) in enumerate(batch)
            ]
            try:
                resp = self.reg.call("utility", prompts.grade(questions, items, self.cfg))
                by = {str(g.get("id")): g for g in resp.data.get("grades", []) if isinstance(g, dict)}
            except (ProviderError, ValueError, TypeError, AttributeError) as e:
                log.warning("grader failed, treating batch as ambiguous: %s", e)
                by = {}
            out = []
            for j, (c, origin) in enumerate(batch):
                g = by.get(f"P{j}")
                if g is None:
                    out.append(Graded(c, "ambiguous", sorted(c.subqs), [], origin))
                    continue
                verdict = g.get("verdict") if g.get("verdict") in ("correct", "ambiguous", "incorrect") else "ambiguous"
                sq = [int(x) for x in g.get("subquestions", []) if isinstance(x, (int, float)) and 0 <= int(x) < len(subqs)]
                if verdict == "correct" and not sq:
                    sq = sorted(c.subqs) or [0]
                out.append(Graded(c, verdict, sq, [str(s) for s in g.get("key_sentences", [])][:4], origin))
            return out

        graded = [g for part in run_parallel(run, batches, max_workers=4) for g in part]
        rec.add(
            "grade",
            graded=len(graded),
            correct=sum(g.verdict == "correct" for g in graded),
            ambiguous=sum(g.verdict == "ambiguous" for g in graded),
            incorrect=sum(g.verdict == "incorrect" for g in graded),
            verdicts=[{"id": g.cand.key, "verdict": g.verdict, "subqs": g.subqs, "origin": g.origin} for g in graded],
        )
        return graded

    def _coverage(self, subqs: list[SubQuery], graded: list[Graded]) -> list[bool]:
        cov = [False] * len(subqs)
        for g in graded:
            if g.verdict == "correct":
                for i in g.subqs:
                    if 0 <= i < len(cov):
                        cov[i] = True
        return cov

    def _retrieve_and_grade(
        self, subqs: list[SubQuery], gate: dict, opts: QueryOptions, rec: TraceRecorder, diag: dict
    ) -> list[Graded]:
        eng = self._engine(opts)
        lists = eng.retrieve(subqs)
        rr = eng.rank(subqs, lists, freshness_need=gate.get("freshness_need", 0.0))
        self._last_rank = rr
        diag["degraded"] += rr.degraded
        rec.add(
            "retrieve",
            lists={f"{r}:{q}": len(h) for (r, q), h in lists.items()},
            counts=rr.counts,
            final=[c.brief() for c in rr.final],
            removed=rr.removed,
            twiddles=rr.twiddles[:50],
            degraded=rr.degraded,
        )
        graded = self._grade(subqs, [(c, "initial") for c in rr.final], rec)
        return graded

    def _heal_retrieval(
        self,
        subqs: list[SubQuery],
        graded: list[Graded],
        gate: dict,
        opts: QueryOptions,
        rec: TraceRecorder,
        diag: dict,
        max_rounds: int,
        force_missing: list[int] | None = None,
    ) -> list[Graded]:
        eng = self._engine(opts)
        seen = {g.cand.key for g in graded}
        for rnd in range(1, max_rounds + 1):
            cov = self._coverage(subqs, graded)
            missing = [i for i, ok in enumerate(cov) if not ok]
            if force_missing and rnd == 1:
                missing = sorted(set(missing) | set(force_missing))
            if not missing:
                break
            meter = current_meter.get()
            if meter and meter.remaining() is not None and meter.remaining() < 3:
                diag["heals"].append({"step": "budget", "round": rnd, "detail": "too little budget left to heal retrieval"})
                break
            new: list[tuple[Candidate, str]] = []
            actions: list[str] = []
            miss_subqs = [subqs[i] for i in missing]

            def add(c: Candidate, origin: str, new: list = new) -> None:
                if c.key not in seen:
                    seen.add(c.key)
                    new.append((c, origin))

            # H3a widen: candidates that ranked just below the cut (ranking miss)
            pool = getattr(self, "_last_rank", None)
            if pool is not None:
                for i in missing:
                    for c in [c for c in pool.pool if i in c.subqs][:6]:
                        add(c, "widen")
                actions.append("widen")
            # H2 rewrite the uncovered sub-questions
            for i in missing:
                sq = subqs[i]
                try:
                    resp = self.reg.call("utility", prompts.rewrite(sq.question, sq.all_texts(), rnd, self.cfg))
                    qs = [str(q) for q in resp.data.get("queries", []) if str(q).strip()][:3]
                    terms = [str(t) for t in resp.data.get("new_terms", [])][:10]
                except (ProviderError, ValueError, TypeError, AttributeError):
                    qs, terms = [], []
                if qs:
                    variant = SubQuery(sq.question, keywords=sq.keywords, identifiers=sq.identifiers, variants=qs)
                    lists = eng.retrieve([variant], k=self.cfg.retrieve_k)
                    rr = eng.rank([variant], lists, final_k=8, exclude=seen)
                    for c in rr.final:
                        c.subqs = {i}
                        c.notes.append(f"rewrite: {qs[0][:80]}")
                        add(c, "rewrite")
                    sq.variants += qs
                    diag.setdefault("rewrites", []).append({"subq": i, "queries": qs, "new_terms": terms})
                    actions.append(f"rewrite[{i}]")
            # H3b cold tier + broad exact match
            lists = eng.retrieve(miss_subqs, include_cold=True, broad_exact=True, retrievers=("bm25", "exact"))
            remap = {j: missing[j] for j in range(len(missing))}
            rr = eng.rank(miss_subqs, lists, final_k=6, exclude=seen)
            for c in rr.final:
                c.subqs = {remap[j] for j in c.subqs}
                d = eng.docs.get(c.doc_id or "")
                origin = "cold" if d is not None and (d.tier == "cold" or d.status == "quarantined") else (
                    "exact" if "exact" in c.provenance and "bm25" not in c.provenance else "cold"
                )
                add(c, origin)
            actions.append("cold+exact")
            # neighbours of ambiguous evidence (answer split across chunks)
            for g in graded:
                if g.verdict == "ambiguous" and set(g.cand.subqs) & set(missing):
                    for nb in eng.neighbors(g.cand):
                        nb.subqs = set(g.cand.subqs) & set(missing)
                        add(nb, "neighbor")
            actions.append("neighbors")
            # H4 web fallback (last round, or freshness-critical)
            if self.cfg.web_fallback_enabled and web_available(self.settings) and (rnd == max_rounds or gate.get("freshness_need", 0) >= 0.5):
                for i in missing:
                    for n_, w in enumerate(web_search(subqs[i].question, self.settings, self.cfg.web_max_results)):
                        c = Candidate(
                            key=f"web:{rnd}:{i}:{n_}",
                            idx=None,
                            doc_id=None,
                            title=w.title or w.url,
                            section="web",
                            text=w.text,
                            url=w.url,
                            subqs={i},
                        )
                        add(c, "web")
                actions.append("web")
            regraded = self._grade(subqs, new, rec)
            graded = graded + regraded
            new_cov = self._coverage(subqs, graded)
            fixed = [i for i in missing if new_cov[i]]
            for g in regraded:
                if g.verdict == "correct":
                    for i in g.subqs:
                        if i in fixed:
                            diag["rescues"].append(
                                {"subq": i, "origin": g.origin, "chunk_id": g.cand.key, "doc_id": g.cand.doc_id, "url": g.cand.url}
                            )
            diag["heals"].append(
                {"step": "H2-H4", "round": rnd, "missing": missing, "actions": actions, "new_candidates": len(new), "fixed": fixed}
            )
            rec.add("heal_retrieval", round=rnd, missing=missing, actions=actions, new=len(new), fixed=fixed)
            if not new:
                break
        return graded

    def _long_context_evidence(self, subqs: list[SubQuery], opts: QueryOptions, rec: TraceRecorder) -> list[Graded]:
        eng = self._engine(opts)
        out = []
        for i, c in enumerate(eng.snap.chunks):
            if eng.mask[i]:
                cand = Candidate(c.id, i, c.doc_id, c.title, c.section_path, c.text, c.context, c.page, subqs=set(range(len(subqs))))
                out.append(Graded(cand, "correct", list(range(len(subqs))), [], "long_context"))
        rec.add("long_context", chunks=len(out), tokens=sum(estimate_tokens(g.cand.text) for g in out))
        return out

    # ============================================================== generation
    def _evidence(self, graded: list[Graded], limit: int) -> list[Graded]:
        order = {"correct": 0, "ambiguous": 1}
        usable = [g for g in graded if g.verdict in order]
        usable.sort(key=lambda g: (order[g.verdict], -g.cand.final))
        # one entry per chunk
        out, seen = [], set()
        for g in usable:
            if g.cand.key not in seen:
                seen.add(g.cand.key)
                out.append(g)
        return out[:limit] if limit else out

    def _evidence_items(self, ev: list[Graded]) -> tuple[list[dict[str, Any]], dict[str, Graded]]:
        items, labels = [], {}
        for n, g in enumerate(ev, start=1):
            label = f"C{n}"
            labels[label] = g
            d = self._engine_docs().get(g.cand.doc_id or "")
            text = g.cand.text if g.verdict == "correct" or not g.key_sentences else " ".join(g.key_sentences)
            items.append(
                {
                    "id": label,
                    "title": g.cand.title,
                    "section": g.cand.section,
                    "date": d.modified_at.date().isoformat() if d is not None and d.modified_at else "",
                    "authority": d.authority if d is not None else "",
                    "text": text,
                }
            )
        return items, labels

    def _engine_docs(self) -> dict[str, Document]:
        eng = getattr(self, "_eng", None)
        return eng.docs if eng is not None else {}

    def _generate(self, query, subqs, items, rec, *, must_avoid=None, show_conflicts=False, long_context=False):
        req = prompts.generate(query, [s.question for s in subqs], items, self.cfg, must_avoid=must_avoid, show_conflicts=show_conflicts)
        if long_context:
            req.cache_system = True
        resp = self.reg.call("generator", req)
        d = resp.data if isinstance(resp.data, dict) else {}
        valid = {it["id"] for it in items}
        sents = []
        for s in d.get("sentences", []):
            if not isinstance(s, dict):
                continue
            text = str(s.get("text", "")).strip()
            if text:
                cites = [str(c) for c in s.get("citations", []) if str(c) in valid]
                sents.append({"text": text, "citations": list(dict.fromkeys(cites))})
        unanswered = [str(u) for u in d.get("unanswered", []) if str(u).strip()]
        conflicts = [str(c) for c in d.get("conflicts", []) if str(c).strip()]
        rec.add("generate", model=resp.model or resp.provider, sentences=len(sents), unanswered=len(unanswered), conflicts=len(conflicts))
        return sents, unanswered, conflicts

    def _verify(self, query, subqs, sents, items, rec) -> dict[str, Any]:
        if not sents:
            return {"claims": [], "coverage": [{"subquestion": i, "answered": False} for i in range(len(subqs))], "metrics": self._metrics([], [], len(subqs), sents)}
        resp = self.reg.call("verifier", prompts.verify(query, [s.question for s in subqs], sents, items, self.cfg))
        d = resp.data if isinstance(resp.data, dict) else {}
        valid = {it["id"] for it in items}
        claims = []
        for c in d.get("claims", []):
            if not isinstance(c, dict):
                continue
            try:
                si = int(c.get("sentence", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= si < len(sents):
                continue
            support = c.get("support") if c.get("support") in ("full", "partial", "none") else "none"
            claims.append(
                {
                    "sentence": si,
                    "claim": str(c.get("claim", ""))[:500],
                    "support": support,
                    "supporting": [x for x in map(str, c.get("supporting", [])) if x in valid],
                    "contradicting": [x for x in map(str, c.get("contradicting", [])) if x in valid],
                }
            )
        # sentences the verifier skipped are unverified
        covered = {c["sentence"] for c in claims}
        for si, s in enumerate(sents):
            if si not in covered:
                claims.append({"sentence": si, "claim": s["text"], "support": "none", "supporting": [], "contradicting": [], "skipped": True})
        coverage = []
        for c in d.get("coverage", []):
            try:
                qi = int(c.get("subquestion", -1))
            except (TypeError, ValueError, AttributeError):
                continue
            if 0 <= qi < len(subqs):
                coverage.append({"subquestion": qi, "answered": bool(c.get("answered"))})
        have = {c["subquestion"] for c in coverage}
        coverage += [{"subquestion": i, "answered": False} for i in range(len(subqs)) if i not in have]
        metrics = self._metrics(claims, coverage, len(subqs), sents)
        rec.add("verify", model=resp.model or resp.provider, **metrics)
        return {"claims": claims, "coverage": coverage, "metrics": metrics}

    @staticmethod
    def _metrics(claims, coverage, n_subqs, sents) -> dict[str, Any]:
        n = len(claims)
        grounded = sum(1 for c in claims if c["support"] == "full")
        contra = sum(1 for c in claims if c["contradicting"])
        answered = sum(1 for c in coverage if c["answered"])
        cite_ok = 0
        for si, s in enumerate(sents):
            sup = {x for c in claims if c["sentence"] == si for x in c["supporting"]}
            if s["citations"] and set(s["citations"]) & sup:
                cite_ok += 1
        return {
            "groundedness": round(grounded / n, 4) if n else 0.0,
            "contradiction": round(contra / n, 4) if n else 0.0,
            "coverage": round(answered / n_subqs, 4) if n_subqs else 0.0,
            "citation_accuracy": round(cite_ok / len(sents), 4) if sents else 0.0,
            "claims": n,
        }

    def _passes(self, m: dict[str, Any]) -> bool:
        return (
            m["claims"] > 0
            and m["groundedness"] >= self.cfg.min_groundedness
            and m["contradiction"] <= self.cfg.max_contradiction
            and m["coverage"] >= self.cfg.min_coverage
        )

    def _repair(self, sents, ver, rec, diag) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """H1: keep fully supported sentences (with corrected citations), drop the rest.

        Returns the kept sentences and their claims re-indexed to the new sentence order."""
        by_sentence: dict[int, list[dict]] = {}
        for c in ver["claims"]:
            by_sentence.setdefault(c["sentence"], []).append(c)
        kept, kept_claims, dropped = [], [], []
        fixed_citations = 0
        for si, s in enumerate(sents):
            cl = by_sentence.get(si, [])
            if cl and all(c["support"] == "full" for c in cl):
                sup = list(dict.fromkeys(x for c in cl for x in c["supporting"]))
                if sup and set(sup) != set(s["citations"]):
                    fixed_citations += 1
                new_idx = len(kept)
                kept.append({"text": s["text"], "citations": sup or s["citations"]})
                kept_claims += [{**c, "sentence": new_idx} for c in cl]
            else:
                dropped.append({"sentence": s["text"], "claims": [c["claim"] for c in cl if c["support"] != "full"], "cited": s["citations"]})
        diag["unsupported"] += dropped
        diag["heals"].append({"step": "H1", "action": "repair_answer", "dropped": len(dropped), "fixed_citations": fixed_citations})
        rec.add("heal_answer", step="H1", dropped=len(dropped), fixed_citations=fixed_citations)
        return kept, kept_claims

    def _answer_and_verify(
        self,
        query: str,
        subqs: list[SubQuery],
        graded: list[Graded],
        gate: dict,
        opts: QueryOptions,
        rec: TraceRecorder,
        diag: dict,
        max_rounds: int,
    ) -> Outcome:
        long_ctx = bool(graded) and graded[0].origin == "long_context"
        ev = self._evidence(graded, 0 if long_ctx else self.cfg.generate_k)
        if not ev:
            rec.add("no_evidence")
            missing = [s.question for s in subqs]
            return Outcome(
                status="failed",
                answer="I could not find information in the knowledge base that answers this question.",
                unanswered=missing,
                metrics={"groundedness": 0.0, "contradiction": 0.0, "coverage": 0.0, "citation_accuracy": 0.0, "claims": 0},
            )
        items, labels = self._evidence_items(ev)
        sents, unanswered, conflicts = self._generate(query, subqs, items, rec, long_context=long_ctx)
        ver_items = items
        if long_ctx:  # verify against what was cited plus the closest passages
            cited = {c for s in sents for c in s["citations"]}
            qterms = content_terms(query + " " + " ".join(s["text"] for s in sents))
            ranked = sorted(items, key=lambda it: -overlap(qterms, content_terms(it["text"])))
            ver_items = [it for it in items if it["id"] in cited] + [it for it in ranked[:20] if it["id"] not in cited]
        ver = self._verify(query, subqs, sents, ver_items, rec)
        m = ver["metrics"]
        attempts = 0
        while not self._passes(m) and attempts < max(1, max_rounds):
            attempts += 1
            meter = current_meter.get()
            if meter and meter.remaining() is not None and meter.remaining() < 2:
                diag["heals"].append({"step": "budget", "detail": "too little budget left to heal the answer"})
                break
            if m["contradiction"] > self.cfg.max_contradiction:
                # H5: regenerate surfacing the conflict explicitly
                bad = [c["claim"] for c in ver["claims"] if c["support"] != "full"]
                diag["heals"].append({"step": "H5", "action": "conflict_mode", "contradictions": m["contradiction"]})
                diag["conflict_claims"] = [c for c in ver["claims"] if c["contradicting"]][:10]
                sents, unanswered, conflicts = self._generate(query, subqs, items, rec, must_avoid=bad, show_conflicts=True, long_context=long_ctx)
                ver = self._verify(query, subqs, sents, ver_items, rec)
                m = ver["metrics"]
                continue
            # H1 only when there is something to repair (an empty answer goes to coverage healing)
            if m["claims"] > 0 and m["groundedness"] < self.cfg.min_groundedness:
                old_sents = sents
                sents, kept_claims = self._repair(sents, ver, rec, diag)
                coverage = self._coverage_after_repair(subqs, sents, ver)
                ver = {"claims": kept_claims, "coverage": coverage}
                ver["metrics"] = self._metrics(kept_claims, coverage, len(subqs), sents)
                m = ver["metrics"]
                rec.add("verify_after_repair", kept=len(sents), dropped=len(old_sents) - len(sents), **m)
                if self._passes(m) or (not sents and long_ctx):
                    break
                continue
            if m["coverage"] < self.cfg.min_coverage:
                # retrieve again for the sub-questions the answer does not cover
                if long_ctx:
                    break
                missing = [c["subquestion"] for c in ver["coverage"] if not c["answered"]]
                before = len(graded)
                graded = self._heal_retrieval(subqs, graded, gate, opts, rec, diag, 1, force_missing=missing)
                if len(graded) == before:
                    break
                ev = self._evidence(graded, self.cfg.generate_k + 4)
                items, labels = self._evidence_items(ev)
                ver_items = items
                prev_good = [s["text"] for s in sents]
                sents, unanswered, conflicts = self._generate(query, subqs, items, rec)
                ver = self._verify(query, subqs, sents, ver_items, rec)
                m = ver["metrics"]
                diag["heals"].append({"step": "H2-coverage", "missing": missing, "previous_sentences": len(prev_good)})
                continue
            break

        answered = {c["subquestion"] for c in ver["coverage"] if c["answered"]}
        for i, s in enumerate(subqs):
            if i not in answered and s.question not in unanswered:
                unanswered.append(s.question)
        status = "verified" if self._passes(m) else ("partial" if sents else "failed")
        return self._compose(status, sents, unanswered, conflicts, labels, m, ver)

    def _coverage_after_repair(self, subqs, sents, ver) -> list[dict[str, Any]]:
        texts = " ".join(s["text"] for s in sents)
        tt = content_terms(texts)
        out = []
        for c in ver["coverage"]:
            qi = c["subquestion"]
            still = c["answered"] and overlap(content_terms(subqs[qi].question), tt) >= 0.25
            out.append({"subquestion": qi, "answered": bool(still)})
        return out

    # ================================================================ compose
    def _compose(self, status, sents, unanswered, conflicts, labels, metrics, ver) -> Outcome:
        answer_parts, spans, citations, label_num = [], [], [], {}
        pos = 0
        for s in sents:
            text = s["text"].strip()
            if answer_parts:
                pos += 1
            start, end = pos, pos + len(text)
            nums = []
            for lab in s["citations"]:
                g = labels.get(lab)
                if g is None:
                    continue
                if lab not in label_num:
                    label_num[lab] = len(label_num) + 1
                    citations.append(self._citation(label_num[lab], g, text))
                nums.append(label_num[lab])
            spans.append({"start": start, "end": end, "text": text, "citations": nums})
            answer_parts.append(text)
            pos = end
        answer = " ".join(answer_parts)
        if not answer:
            answer = "I could not produce an answer that is supported by the knowledge base."
        notes = []
        if unanswered and status != "verified":
            notes.append("Not found in the knowledge base: " + "; ".join(unanswered))
        if conflicts:
            notes.append("Sources disagree: " + "; ".join(conflicts))
        confidence = round(
            metrics.get("groundedness", 0) * (0.5 + 0.5 * metrics.get("coverage", 0)) * (1 - metrics.get("contradiction", 0)), 4
        )
        metrics = {**metrics, "confidence": confidence}
        return Outcome(
            status=status,
            answer=answer + ("\n\n" + "\n".join(notes) if notes else ""),
            sentences=spans,
            citations=citations,
            unanswered=unanswered,
            conflicts=conflicts,
            metrics=metrics,
            claims=[
                {**c, "supporting": [label_num[x] for x in c["supporting"] if x in label_num], "contradicting": [label_num[x] for x in c["contradicting"] if x in label_num]}
                for c in (ver or {}).get("claims", [])
            ],
        )

    def _citation(self, n: int, g: Graded, sentence: str) -> dict[str, Any]:
        st = content_terms(sentence)
        best = max(sentences(g.cand.text) or [g.cand.text[:300]], key=lambda x: overlap(st, content_terms(x)))
        return {
            "n": n,
            "chunk_id": None if g.cand.idx is None else g.cand.key,
            "doc_id": g.cand.doc_id,
            "title": g.cand.title,
            "section": g.cand.section,
            "page": g.cand.page,
            "url": g.cand.url,
            "cited_text": best[:300],
            "origin": g.origin,
        }

    # ================================================================ persist
    def _persist(self, query, opts, route, outcome: Outcome, rec, diag, meter, subqs, graded, job_id) -> str:
        cluster = assign_cluster(self.session, self.kb_id, query) if not opts.is_eval else None
        m = outcome.metrics or {}
        trace = Trace(
            kb_id=self.kb_id,
            session_id=opts.session_id,
            user_id=opts.user_id,
            query=query,
            query_cluster_id=cluster,
            route=route,
            status=outcome.status,
            answer=outcome.answer,
            groundedness=m.get("groundedness"),
            contradiction=m.get("contradiction"),
            coverage=m.get("coverage"),
            confidence=m.get("confidence"),
            healed=bool(diag["heals"]),
            llm_calls=meter.calls,
            tokens_in=meter.tokens_in,
            tokens_out=meter.tokens_out,
            latency_ms=rec.elapsed_ms(),
            config_version=self.config_version,
            is_eval=opts.is_eval,
            diagnosed=outcome.status in ("verified", "direct", "escalated") and not diag["heals"],
            data={
                "steps": rec.steps,
                "diag": diag,
                "subquestions": [{"question": s.question, "variants": s.variants, "identifiers": s.identifiers} for s in subqs],
                "evidence": [
                    {"id": g.cand.key, "doc_id": g.cand.doc_id, "verdict": g.verdict, "subqs": g.subqs, "origin": g.origin, "url": g.cand.url}
                    for g in graded[:200]
                ],
                "sentences": outcome.sentences,
                "citations": outcome.citations,
                "unanswered": outcome.unanswered,
                "conflicts": outcome.conflicts,
                "claims": outcome.claims,
                "metrics": m,
                "llm": meter.snapshot(),
                "job_id": job_id,
                "staged_docs": sorted(opts.staged_docs),
            },
        )
        self.session.add(trace)
        self.session.flush()
        return trace.id
