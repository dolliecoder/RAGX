"""Offline heuristic provider.

Implements every pipeline task with deterministic lexical heuristics using the
structured ``payload`` of the request. It lets the whole platform run end-to-end
without API keys (development, CI, demos) and makes the test-suite deterministic.
Answers are extractive, so they stay grounded, but quality is far below a real model.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from ..text import STOPWORDS, content_terms, identifiers, overlap, sentences, tokenize
from .base import LLMRequest, LLMResponse

_GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|evening|afternoon)|how are you)\b[\s!.?]*$", re.I)
_NUM = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_ACRONYM = re.compile(r"\(([A-Z]{2,8})\)")


def _acronyms(text: str) -> list[tuple[str, str]]:
    """'Single Sign-On (SSO)' -> ('Single Sign-On', 'SSO'): the words before the
    parenthesis whose initials spell the acronym."""
    out = []
    for m in _ACRONYM.finditer(text):
        acro = m.group(1)
        before = text[: m.start()].rstrip()
        words = re.findall(r"[A-Za-z][A-Za-z\-]*", before[-120:])
        parts: list[str] = []
        for w in reversed(words):
            parts.insert(0, w)
            initials = "".join(p[0] for w2 in parts for p in w2.split("-") if p).upper()
            if initials == acro:
                out.append((" ".join(parts), acro))
                break
            if len(initials) >= len(acro):
                break
    return out


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


class FakeProvider:
    name = "fake"

    def __init__(self, model: str = "offline"):
        self.model = model

    def complete(self, req: LLMRequest) -> LLMResponse:
        handler = getattr(self, f"_t_{req.task}", None)
        data = handler(req.payload) if handler else {}
        text = json.dumps(data)
        return LLMResponse(
            text=text,
            data=data,
            provider=self.name,
            model=self.model,
            tokens_in=len(req.system + req.user) // 4,
            tokens_out=len(text) // 4,
        )

    # ------------------------------------------------------------ handlers
    def _t_contextualize(self, p: dict[str, Any]) -> dict[str, Any]:
        doc, chunk, title = p["document"], p["chunk"], p.get("doc_title") or "document"
        heading = next((ln.lstrip("# ").strip() for ln in chunk.splitlines() if ln.startswith("#")), "")
        counts = Counter(t for t in tokenize(chunk) if len(t) > 3)
        topics = ", ".join(w for w, _ in counts.most_common(6))
        ctx = f"From '{title}'" + (f", section '{heading}'" if heading else "") + f". Topics: {topics}."
        aliases: list[str] = []
        chunk_lower = chunk.lower()
        for full, acro in _acronyms(doc):
            if acro.lower() in chunk_lower or full.lower() in chunk_lower:
                aliases += [full, acro]
        return {"context": ctx, "aliases": list(dict.fromkeys(aliases))[:8]}

    def _t_gate(self, p: dict[str, Any]) -> dict[str, Any]:
        q = p["query"]
        ql = q.lower()
        if _GREETING.match(q):
            return {"retrieval_need": 0.05, "complexity": "simple", "freshness_need": 0.0, "reason": "greeting"}
        complexity = "simple"
        if (
            re.search(r"\b(compare|difference|differ|versus|vs\.?|both|each)\b", ql)
            or q.count("?") > 1
            or re.search(r"\band (also |then )?(what|how|which|who|when|where|why|is|are|does|do|can)\b", ql)
        ):
            complexity = "multi_part"
        if re.search(r"^(analy[sz]e|research|write a report|give (me )?an overview|summari[sz]e everything)", ql):
            complexity = "research"
        fresh = 0.8 if re.search(r"\b(latest|today|current|right now|recent|news)\b", ql) else 0.1
        return {"retrieval_need": 0.9, "complexity": complexity, "freshness_need": fresh, "reason": "heuristic"}

    def _t_plan(self, p: dict[str, Any]) -> dict[str, Any]:
        q, max_n = p["query"], max(1, int(p.get("max_subquestions", 3)))
        splitter = r"\?|;|\band (?:also |then )?(?=(?:what|how|which|who|when|where|why|is|are|does|do|can)\b)"
        parts = [s.strip() for s in re.split(splitter, q, flags=re.I) if len(content_terms(s)) >= 1]
        if not parts:
            parts = [q]
        parts = parts[:max_n]
        subs = []
        for part in parts:
            question = part if part.endswith("?") else part + "?"
            kws = [w for w in re.findall(r"[A-Za-z0-9][\w\-\.]*", part) if w.lower() not in STOPWORDS and len(w) > 2]
            subs.append({"question": question, "keywords": kws[:8], "identifiers": identifiers(part)})
        return {"subquestions": subs}

    def _t_rewrite(self, p: dict[str, Any]) -> dict[str, Any]:
        sq = p["subquestion"]
        kws = [w for w in re.findall(r"[A-Za-z0-9][\w\-\.]*", sq) if w.lower() not in STOPWORDS and len(w) > 2]
        queries = [" ".join(kws)]
        if len(kws) > 2:
            queries.append(" ".join(sorted(kws, key=len, reverse=True)[:2]))
        queries.append(" ".join(tokenize(sq)))
        tried = set(p.get("tried", []))
        return {"queries": [q for q in dict.fromkeys(queries) if q and q not in tried], "new_terms": []}

    def _t_grade(self, p: dict[str, Any]) -> dict[str, Any]:
        subqs = [content_terms(q) for q in p["subquestions"]]
        grades = []
        for ch in p["chunks"]:
            terms = content_terms(ch["text"])
            scores = [overlap(sq, terms) for sq in subqs]
            best = max(scores) if scores else 0.0
            helped = [i for i, s in enumerate(scores) if s >= 0.5]
            verdict = "correct" if best >= 0.5 else "ambiguous" if best >= 0.25 else "incorrect"
            sents = sorted(sentences(ch["text"]), key=lambda s: -max((overlap(sq, content_terms(s)) for sq in subqs), default=0))
            grades.append({"id": ch["id"], "verdict": verdict, "subquestions": helped, "key_sentences": sents[:2]})
        return {"grades": grades}

    def _t_generate(self, p: dict[str, Any]) -> dict[str, Any]:
        avoid = {s.strip().lower() for s in p.get("must_avoid", [])}
        cands: list[tuple[str, str]] = []  # (sentence, evidence id)
        for ev in p["evidence"]:
            for s in sentences(ev["text"]):
                if not s.lstrip().startswith("#") and len(content_terms(s)) >= 2:
                    cands.append((s, ev["id"]))
        out: list[dict[str, Any]] = []
        unanswered: list[str] = []
        used: set[str] = set()
        for sq in p["subquestions"] or [p["query"]]:
            qt = content_terms(sq)
            ranked = sorted(cands, key=lambda c: -overlap(qt, content_terms(c[0])))
            picked = 0
            for s, cid in ranked:
                if overlap(qt, content_terms(s)) < 0.34:
                    break
                if s in used or s.strip().lower() in avoid:
                    continue
                out.append({"text": s, "citations": [cid]})
                used.add(s)
                picked += 1
                if picked == 2:
                    break
            if picked == 0:
                unanswered.append(sq)
        return {"sentences": out, "unanswered": unanswered, "conflicts": []}

    def _t_verify(self, p: dict[str, Any]) -> dict[str, Any]:
        evidence = [(ev["id"], content_terms(ev["text"]), _numbers(ev["text"])) for ev in p["evidence"]]
        claims = []
        for i, s in enumerate(p["sentences"]):
            st = content_terms(s["text"])
            nums = _numbers(s["text"])
            supporting, contradicting, best = [], [], 0.0
            for eid, terms, enums in evidence:
                cov = overlap(st, terms)
                nums_ok = nums <= enums  # every number in the claim must appear in the evidence
                if nums_ok:
                    best = max(best, cov)
                if cov >= 0.8 and nums_ok:
                    supporting.append(eid)
                elif cov >= 0.6 and nums and enums and not (nums & enums):
                    contradicting.append(eid)
            support = "full" if supporting else "partial" if best >= 0.5 else "none"
            claims.append(
                {"sentence": i, "claim": s["text"], "support": support, "supporting": supporting, "contradicting": contradicting}
            )
        coverage = []
        for qi, q in enumerate(p["subquestions"]):
            qt = content_terms(q)
            answered = any(
                c["support"] == "full" and overlap(qt, content_terms(c["claim"])) >= 0.34 for c in claims
            )
            coverage.append({"subquestion": qi, "answered": answered})
        return {"claims": claims, "coverage": coverage}

    def _t_rerank(self, p: dict[str, Any]) -> dict[str, Any]:
        qt = content_terms(p["query"])
        return {"scores": [{"id": d["id"], "score": round(10 * overlap(qt, content_terms(d["text"])), 3)} for d in p["docs"]]}

    def _t_judge(self, p: dict[str, Any]) -> dict[str, Any]:
        answer, expected = p["answer"], p.get("expected") or ""
        at = content_terms(answer)
        if expected:
            et = content_terms(expected)
            not_found = bool(re.search(r"\b(not (found|available|mentioned)|no information|unknown)\b", expected, re.I))
            if not_found:
                abstained = bool(re.search(r"(could not|couldn't|not able|no evidence|not find|unanswered)", answer, re.I)) or not at
                fact = 1.0 if abstained else 0.0
            else:
                fact = overlap(et, at)
        else:
            fact = 0.7 if at else 0.0
        cites = p.get("citations") or []
        cite_ok = (
            sum(1 for c in cites if overlap(content_terms(c["text"]), at) > 0.1 or overlap(at, content_terms(c["text"])) > 0.3) / len(cites)
            if cites
            else (1.0 if not at else 0.0)
        )
        overall = round(0.6 * fact + 0.2 * cite_ok + 0.2 * fact, 3)
        return {
            "factual_accuracy": round(fact, 3),
            "citation_accuracy": round(cite_ok, 3),
            "completeness": round(fact, 3),
            "source_quality": 0.8,
            "overall": overall,
            "rationale": "offline lexical judge",
        }

    def _t_general(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"answer": f"From general knowledge: {p['query']}"}

    def _t_chat(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"answer": "Hello! Ask me anything about the documents in this knowledge base."}

    def _t_improve_prompt(self, p: dict[str, Any]) -> dict[str, Any]:
        return {
            "addendum": "Only state facts that appear explicitly in the cited evidence. If the evidence does not "
            "state something directly, put the sub-question in 'unanswered' rather than inferring.",
            "rationale": "offline heuristic: tighten grounding",
        }
