"""Second-stage rerankers (cross-encoder APIs, LLM listwise, lexical fallback)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from .. import prompts
from ..llm.base import ProviderError
from ..llm.providers import _post_with_retries
from ..text import content_terms, overlap
from ..util import run_parallel

if TYPE_CHECKING:
    from ..llm.registry import Registry

log = logging.getLogger("ragx.rerank")


class LexicalReranker:
    label = "lexical"

    def rerank(self, query: str, docs: list[tuple[str, str]], top_n: int) -> list[tuple[str, float]]:
        qt = content_terms(query)
        scored = [(i, overlap(qt, content_terms(t))) for i, t in docs]
        return sorted(scored, key=lambda x: -x[1])[:top_n]


class CohereReranker:
    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model or "rerank-v3.5"
        self.label = f"cohere:{self.model}"
        self.http = httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def rerank(self, query: str, docs: list[tuple[str, str]], top_n: int) -> list[tuple[str, float]]:
        data = _post_with_retries(
            self.http,
            "https://api.cohere.com/v2/rerank",
            json_body={"model": self.model, "query": query, "documents": [t[:8000] for _, t in docs], "top_n": min(top_n, len(docs))},
            headers=self.headers,
        )
        return [(docs[r["index"]][0], float(r["relevance_score"])) for r in data["results"]]


class VoyageReranker:
    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model or "rerank-2.5"
        self.label = f"voyage:{self.model}"
        self.http = httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def rerank(self, query: str, docs: list[tuple[str, str]], top_n: int) -> list[tuple[str, float]]:
        data = _post_with_retries(
            self.http,
            "https://api.voyageai.com/v1/rerank",
            json_body={"model": self.model, "query": query, "documents": [t[:8000] for _, t in docs], "top_k": min(top_n, len(docs))},
            headers=self.headers,
        )
        return [(docs[r["index"]][0], float(r["relevance_score"])) for r in data["data"]]


class LLMReranker:
    """Pointwise-in-batches relevance scoring by the utility model."""

    BATCH = 12

    def __init__(self, registry: "Registry"):
        self.registry = registry
        self.label = f"llm:{registry.model_label('utility')}"

    def rerank(self, query: str, docs: list[tuple[str, str]], top_n: int) -> list[tuple[str, float]]:
        batches = [docs[i : i + self.BATCH] for i in range(0, len(docs), self.BATCH)]

        def score(batch: list[tuple[str, str]]) -> list[tuple[str, float]]:
            req = prompts.rerank(query, [{"id": i, "text": t[:2500]} for i, t in batch])
            resp = self.registry.call("utility", req)
            got = {str(s.get("id")): float(s.get("score", 0)) for s in resp.data.get("scores", []) if isinstance(s, dict)}
            return [(i, max(0.0, min(10.0, got.get(i, 0.0))) / 10.0) for i, _ in batch]

        out = [x for part in run_parallel(score, batches, max_workers=4) for x in part]
        return sorted(out, key=lambda x: -x[1])[:top_n]


class SafeReranker:
    """Wraps a reranker; degrades to lexical on failure and records it."""

    def __init__(self, inner, fallback=None):
        self.inner = inner
        self.fallback = fallback or LexicalReranker()
        self.label = inner.label
        self.last_degraded = False

    def rerank(self, query: str, docs: list[tuple[str, str]], top_n: int) -> tuple[list[tuple[str, float]], bool]:
        if not docs:
            return [], False
        try:
            return self.inner.rerank(query, docs, top_n), False
        except (ProviderError, httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("reranker %s failed, degrading to lexical: %s", self.label, e)
            return self.fallback.rerank(query, docs, top_n), True


def build_reranker(spec: str, registry: "Registry") -> SafeReranker:
    provider, _, model = spec.partition(":")
    s = registry.settings
    if provider == "cohere" and s.cohere_api_key:
        return SafeReranker(CohereReranker(model, s.cohere_api_key, s.request_timeout))
    if provider == "voyage" and s.voyage_api_key:
        return SafeReranker(VoyageReranker(model, s.voyage_api_key, s.request_timeout))
    if provider == "llm" or (provider in ("cohere", "voyage")):
        if registry.is_offline("utility"):
            return SafeReranker(LexicalReranker())
        return SafeReranker(LLMReranker(registry))
    return SafeReranker(LexicalReranker())
