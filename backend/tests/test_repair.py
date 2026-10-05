"""End-to-end self-healing: failure -> heal -> diagnosis -> fix -> eval gate -> apply -> rollback."""

from __future__ import annotations

import copy

from conftest import ask, make_kb, set_config
from sqlalchemy import select

from ragx.db import session_scope
from ragx.llm import set_registry
from ragx.llm.fake import FakeProvider
from ragx.llm.registry import Registry
from ragx.models import Diagnosis, Fix, GoldenItem, Trace

SYNONYMS = {"money back": "money back refund"}


class SynonymModel(FakeProvider):
    """Offline model that understands one synonym ('money back' == 'refund'),
    the way a real LLM would, while retrieval itself stays purely lexical."""

    def complete(self, req):
        if req.task in ("grade", "generate", "verify", "judge", "gate", "plan"):
            req.payload = self._expand(copy.deepcopy(req.payload))
        return super().complete(req)

    def _expand(self, obj):
        if isinstance(obj, str):
            for k, v in SYNONYMS.items():
                obj = obj.replace(k, v)
            return obj
        if isinstance(obj, list):
            return [self._expand(x) for x in obj]
        if isinstance(obj, dict):
            return {k: (self._expand(v) if k in ("query", "subquestions", "question", "expected") else v) for k, v in obj.items()}
        return obj

    def _t_grade(self, p):
        out = super()._t_grade(p)
        if any("money back" in q for q in p["subquestions"]):
            texts = {c["id"]: c["text"].lower() for c in p["chunks"]}
            for g in out["grades"]:
                if "refund" not in texts[g["id"]]:
                    g["verdict"], g["subquestions"] = "incorrect", []
        return out

    def _t_rewrite(self, p):
        if "money back" in p["subquestion"]:
            return {"queries": ["refund Pro plan", "request a refund"], "new_terms": ["refund"]}
        return super()._t_rewrite(p)


def _install_model():
    reg = Registry()
    m = SynonymModel("syn")
    for role in ("generator", "verifier", "utility", "judge"):
        reg.chains[role] = [m]
    set_registry(reg)


def _distractors(docs_dir):
    (docs_dir / "backups.md").write_text(
        "# Backups\n\nPro plan customers get daily backups. To get data back, restore a backup from the Pro plan console. "
        "Getting files back is free on the Pro plan.\n",
        encoding="utf-8",
    )
    (docs_dir / "pricing.md").write_text(
        "# Pricing\n\nThe Pro plan costs money: 20 dollars per month. Pro plan money is billed monthly; plan changes take effect "
        "next cycle. Get the Pro plan from the Billing Portal.\n",
        encoding="utf-8",
    )


QUESTION = "How do I get my money back for the Pro plan?"


def test_self_healing_cycle(env, docs_dir):
    _distractors(docs_dir)
    kb, _ = make_kb(docs_dir)
    _install_model()
    # small first-pass cut so the vocabulary gap actually hides the right chunk
    set_config(long_context_max_tokens=0, final_k=2, stage1_k=2, generate_k=4, retriever_weights={"bm25": 1.0, "dense": 0.0, "exact": 0.8, "web": 0.6})

    # 1. the Reflex loop heals the query at runtime (but needed a heal to do it)
    r = ask(kb, QUESTION)
    assert r["status"] == "verified", r["answer"]
    assert r["healed"]
    assert "Nimbus Cloud Refund Policy" in {c["title"] for c in r["citations"]}
    with session_scope() as s:
        t = s.get(Trace, r["trace_id"])
        rescues = t.data["diag"]["rescues"]
        assert rescues and rescues[0]["origin"] in ("rewrite", "widen")

    # 2. the Repair loop diagnoses it, proposes a low-risk fix, eval-gates and applies it
    from ragx.repair.service import rollback_fix, run_repair_cycle

    report = run_repair_cycle(kb)
    assert report["traces"] >= 1
    with session_scope() as s:
        diags = s.scalars(select(Diagnosis).where(Diagnosis.kb_id == kb)).all()
        assert any(d.root_cause in ("vocabulary_gap", "ranking_miss") for d in diags)
        fixes = s.scalars(select(Fix).where(Fix.kb_id == kb, Fix.kind == "augment_chunk")).all()
        assert fixes, report
        fx = fixes[0]
        assert fx.status == "applied", (fx.status, fx.eval)
        assert fx.eval["passed"]
        assert fx.eval["targets"]["candidate"] > fx.eval["targets"]["baseline"]
        fix_id = fx.id
        # every healed failure becomes a regression test
        golden = s.scalars(select(GoldenItem).where(GoldenItem.kb_id == kb, GoldenItem.origin == "healed")).all()
        assert any(g.question == QUESTION for g in golden)

    # 3. the same question now succeeds on the first pass, without healing
    r2 = ask(kb, QUESTION)
    assert r2["status"] == "verified"
    with session_scope() as s:
        heals = s.get(Trace, r2["trace_id"]).data["diag"]["heals"]
        assert not any(h["step"] == "H2-H4" for h in heals), heals

    # 4. the fix is reversible
    rollback_fix(fix_id, reason="test")
    r3 = ask(kb, QUESTION)
    with session_scope() as s:
        heals = s.get(Trace, r3["trace_id"]).data["diag"]["heals"]
        assert any(h["step"] == "H2-H4" for h in heals)


def test_bad_fix_is_rejected_by_eval_gate(env, docs_dir):
    """A fix that does not improve its targets must not ship."""
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    from ragx.repair.service import process_fix

    with session_scope() as s:
        d = Diagnosis(kb_id=kb, root_cause="gate_error", target="gate:threshold", summary="test", evidence={}, trace_ids=[])
        s.add(d)
        s.flush()
        # a rule that drops every retrieved passage -> no answers from the KB
        f = Fix(kb_id=kb, diagnosis_id=d.id, kind="gate_threshold", risk="medium", params={"patch": {"set": {"max_chunks_per_doc": 0}}})
        s.add(f)
        s.add(GoldenItem(kb_id=kb, question="How long do I have to request a refund on the Pro plan?", expected_answer="Within 30 days of purchase."))
        s.flush()
        fid = f.id
    res = process_fix(fid)
    assert res["status"] == "rejected"
    with session_scope() as s:
        assert s.get(Fix, fid).eval["golden"]["comparison"]["regression"]


def test_medium_risk_canary_promote_and_rollback(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0, canary_min_samples=3, canary_pct=1.0)
    from ragx.control import active_config, canary_version
    from ragx.repair.service import evaluate_canaries, process_fix

    def new_fix(value, patch=None):
        with session_scope() as s:
            d = Diagnosis(kb_id=kb, root_cause="gate_error", target=f"gate:{value}", summary="t", evidence={}, trace_ids=[])
            s.add(d)
            s.flush()
            f = Fix(kb_id=kb, diagnosis_id=d.id, kind="gate_threshold", risk="medium", params={"value": value, "patch": patch or {"set": {"gate_threshold": value}}})
            s.add(f)
            s.flush()
            return f.id

    fid = new_fix(0.25)
    assert process_fix(fid)["status"] == "canary"
    with session_scope() as s:
        cv = canary_version(s)
        assert cv is not None
        canary_v = cv.version
    # live traffic goes to the canary (pct=1.0)
    from ragx.control import select_config
    from ragx.reflex.engine import QueryEngine, QueryOptions

    for _ in range(3):
        with session_scope() as s:
            v, cfg = select_config(s)
            assert v == canary_v
            QueryEngine(s, kb, cfg, v).answer("How long do I have to request a refund on the Pro plan?", QueryOptions())
    out = evaluate_canaries()
    assert out["decision"] == "promoted", out
    with session_scope() as s:
        _, cfg = active_config(s)
        assert cfg.gate_threshold == 0.25
        assert s.get(Fix, fid).status == "applied"

    # now a canary that makes things worse gets rolled back automatically
    fid2 = new_fix(0.99, {"set": {"max_chunks_per_doc": 0}})  # drops all evidence -> nothing verified
    with session_scope() as s:
        f = s.get(Fix, fid2)
        version, cfg = active_config(s)
        from ragx.control import apply_patch, create_version, start_canary

        cv = create_version(s, apply_patch(cfg, f.params["patch"]), status="candidate", parent=version, note="t")
        start_canary(s, cv.version, 1.0, "test")
        f.status, f.config_version = "canary", cv.version
    for _ in range(3):
        with session_scope() as s:
            v, cfg = select_config(s)
            QueryEngine(s, kb, cfg, v).answer("What does error ERR-4012 mean?", QueryOptions())
    # baseline traffic on the active version in the same window
    with session_scope() as s:
        v, cfg = active_config(s)
        QueryEngine(s, kb, cfg, v).answer("What does error ERR-4012 mean?", QueryOptions())
    out = evaluate_canaries()
    assert out["decision"] == "rolled_back", out
    with session_scope() as s:
        _, cfg = active_config(s)
        assert cfg.gate_threshold == 0.25
        assert s.get(Fix, fid2).status == "rolled_back"


def test_high_risk_requires_approval_and_missing_content_ticket(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    ask(kb, "Do you support Kubernetes autoscaling?")
    from ragx.repair.service import approve_fix, run_repair_cycle

    run_repair_cycle(kb)
    with session_scope() as s:
        d = s.scalar(select(Diagnosis).where(Diagnosis.kb_id == kb, Diagnosis.root_cause == "missing_content"))
        assert d is not None
        f = s.scalar(select(Fix).where(Fix.diagnosis_id == d.id))
        assert f.kind == "content_gap" and f.risk == "high" and f.status == "pending_approval"
        fid = f.id
    assert approve_fix(fid)["status"] == "applied"


def test_conflicting_sources_detected(env, docs_dir):
    (docs_dir / "old_refund.md").write_text(
        "# Legacy Refund Rules\n\nCustomers on the Pro plan can request a full refund within 60 days of purchase.\n", encoding="utf-8"
    )
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r = ask(kb, "Within how many days can customers on the Pro plan request a full refund?")
    from ragx.repair.service import run_repair_cycle

    with session_scope() as s:
        t = s.get(Trace, r["trace_id"])
        has_conflict = bool(t.data["diag"].get("conflict_claims"))
    assert has_conflict
    if has_conflict:
        run_repair_cycle(kb)
        with session_scope() as s:
            assert s.scalar(select(Diagnosis).where(Diagnosis.root_cause == "conflicting_sources")) is not None
            f = s.scalar(select(Fix).where(Fix.kind == "quarantine_doc"))
            assert f is not None and f.status == "pending_approval"
