from __future__ import annotations

from conftest import ask, make_kb, set_config

from ragx.llm import set_registry
from ragx.llm.fake import FakeProvider
from ragx.llm.registry import Registry


def _doc_of(r):
    return {c["title"] for c in r["citations"]}


def test_verified_answer_with_citations(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r = ask(kb, "How long do I have to request a refund on the Pro plan?")
    assert r["route"] == "fast"
    assert r["status"] == "verified"
    assert "30 days" in r["answer"]
    assert "Nimbus Cloud Refund Policy" in _doc_of(r)
    assert r["metrics"]["groundedness"] >= 0.9
    # spans point into the answer text and reference citation numbers
    for sp in r["sentences"]:
        assert r["answer"][sp["start"] : sp["end"]] == sp["text"]
        assert sp["citations"]
    assert r["trace_id"]


def test_multi_part_question_fans_out(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r = ask(kb, "What is the uptime guarantee for the Pro plan and what does error ERR-4012 mean?")
    assert r["route"] == "standard"
    assert r["metrics"]["coverage"] == 1.0
    assert {"SERVICE LEVEL AGREEMENT", "Nimbus Cloud API Reference: Rate Limits"} <= _doc_of(r)


def test_long_context_route_for_small_kb(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    r = ask(kb, "What response time do Enterprise customers get for critical incidents?")
    assert r["route"] == "long_context"
    assert r["status"] == "verified"
    assert "1-hour" in r["answer"]


def test_direct_route(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    r = ask(kb, "hello")
    assert r["route"] == "direct" and r["status"] == "direct"
    assert r["citations"] == []


def test_honest_failure_when_absent(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r = ask(kb, "Do you support Kubernetes autoscaling?")
    assert r["status"] == "failed"
    assert r["citations"] == []
    assert "Not found in the knowledge base" in r["answer"]
    assert r["healed"]  # the heal ladder tried before giving up


class _Hallucinator(FakeProvider):
    """Generator that appends an unsupported claim to every answer."""

    def _t_generate(self, p):
        out = super()._t_generate(p)
        if p["evidence"] and not p.get("must_avoid"):
            out["sentences"].append({"text": "Refunds are also available in cryptocurrency on Tuesdays.", "citations": [p["evidence"][0]["id"]]})
        return out


def test_verifier_drops_unsupported_claim(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    reg = Registry()
    reg.chains["generator"] = [_Hallucinator("halluc")]
    set_registry(reg)
    r = ask(kb, "How long do I have to request a refund on the Pro plan?")
    assert "cryptocurrency" not in r["answer"]
    assert r["status"] == "verified"
    assert r["healed"]
    from ragx.db import session_scope
    from ragx.models import Trace

    with session_scope() as s:
        t = s.get(Trace, r["trace_id"])
        assert any("cryptocurrency" in u["sentence"] for u in t.data["diag"]["unsupported"])
        assert any(h["step"] == "H1" for h in t.data["diag"]["heals"])


class _Broken(FakeProvider):
    def complete(self, req):
        from ragx.llm.base import ProviderError

        raise ProviderError("simulated outage")


def test_provider_fallback_chain(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    reg = Registry()
    reg.chains["generator"] = [_Broken("primary"), FakeProvider("backup")]
    set_registry(reg)
    for _ in range(4):
        r = ask(kb, "How long do I have to request a refund on the Pro plan?")
        assert r["status"] == "verified"
    st = reg.status()
    assert st["roles"]["generator"]["chain"][0]["state"] == "open"  # circuit breaker tripped


def test_total_outage_is_reported(env, docs_dir):
    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    reg = Registry()
    reg.chains["generator"] = [_Broken("only")]
    set_registry(reg)
    r = ask(kb, "How long do I have to request a refund on the Pro plan?")
    assert r["status"] == "error"


def test_budget_is_enforced(env, docs_dir):
    from ragx.config import TierBudget

    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0, fast=TierBudget(max_subqueries=1, max_heal_rounds=3, max_llm_calls=3).model_dump())
    r = ask(kb, "Do you support Kubernetes autoscaling?")
    assert r["llm"]["calls"] <= 3
    assert r["status"] in ("failed", "partial")


def test_rephrase_is_negative_signal(env, docs_dir):
    from ragx.db import session_scope
    from ragx.models import Trace

    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r1 = ask(kb, "How long is the refund window for the Pro plan?", session_id="s1")
    ask(kb, "How long is the refund window on a Pro plan exactly?", session_id="s1")
    with session_scope() as s:
        assert s.get(Trace, r1["trace_id"]).negative_signal


def test_deep_research_job(env, docs_dir):
    from ragx.db import session_scope
    from ragx.models import Job, Trace

    kb, _ = make_kb(docs_dir)
    set_config(long_context_max_tokens=0)
    r = ask(kb, "Analyze our refund policy and our uptime guarantee for Pro customers")
    assert r["route"] == "deep" and r["status"] == "escalated" and r["job_id"]
    with session_scope() as s:  # inline runner: already executed after commit
        job = s.get(Job, r["job_id"])
        assert job.status == "done", job.error
        assert job.result["trace_id"]
        t = s.get(Trace, job.result["trace_id"])
        assert t.route == "deep"
        assert job.result["citations"]
