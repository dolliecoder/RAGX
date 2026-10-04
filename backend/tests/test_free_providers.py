"""Free model providers: Ollama (local), Groq / OpenRouter (OpenAI-compatible)."""

from __future__ import annotations

import json

import httpx
import pytest

from ragx import prompts
from ragx.config import RuntimeConfig
from ragx.llm.base import ProviderError
from ragx.llm.embeddings import OllamaEmbedder
from ragx.llm.providers import OllamaProvider, OpenAICompatProvider


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ollama_sends_schema_and_context_window():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"content": '<think>plan {not json}</think>{"retrieval_need": 0.9, "complexity": "simple", "freshness_need": 0, "reason": "x"}'},
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        )

    p = OllamaProvider("qwen3:4b", "http://ollama:11434", 30, num_ctx=8192)
    p.http = _mock(handler)
    req = prompts.gate("What is the refund policy?", [], RuntimeConfig())
    r = p.complete(req)
    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["body"]["format"] == req.schema  # grammar-constrained JSON
    assert seen["body"]["options"]["num_ctx"] == 8192 and seen["body"]["stream"] is False
    assert json.loads(r.text)["complexity"] == "simple"  # <think> block stripped
    assert (r.tokens_in, r.tokens_out) == (120, 30)
    assert p.max_context == 8192


def test_ollama_missing_model_and_truncation():
    p = OllamaProvider("llama9:1b", "http://ollama:11434", 30, num_ctx=4096)
    p.http = _mock(lambda r: httpx.Response(404, json={"error": "model 'llama9:1b' not found"}))
    with pytest.raises(ProviderError, match="ollama pull llama9:1b"):
        p.complete(prompts.chat("hi", RuntimeConfig()))
    p.http = _mock(lambda r: httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"}))
    with pytest.raises(ProviderError, match="truncated"):
        p.complete(prompts.chat("hi", RuntimeConfig()))


def test_openai_compat_falls_back_from_json_schema():
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body.get("response_format", {}).get("type"))
        assert request.headers["authorization"] == "Bearer k"
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema not supported for this model"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"answer": "hello"}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )

    p = OpenAICompatProvider("groq", "llama-x", "https://api.groq.com/openai/v1", "k", 30, json_schema=True)
    p.http = _mock(handler)
    r = p.complete(prompts.chat("hi", RuntimeConfig()))
    assert calls == ["json_schema", "json_object"]
    assert json.loads(r.text) == {"answer": "hello"}


def test_openai_compat_rate_limit_is_retryable_error():
    p = OpenAICompatProvider("openrouter", "m:free", "https://openrouter.ai/api/v1", "k", 30)
    p.http = _mock(lambda r: httpx.Response(429, json={"error": "rate limited"}))
    import ragx.llm.providers as prov

    orig = prov.time.sleep
    prov.time.sleep = lambda s: None
    try:
        with pytest.raises(ProviderError) as e:
            p.complete(prompts.chat("hi", RuntimeConfig()))
    finally:
        prov.time.sleep = orig
    assert e.value.retryable  # registry fails over to the next free provider


def test_ollama_embeddings_are_normalised():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0] for _ in body["input"]]})

    e = OllamaEmbedder("nomic-embed-text", "http://ollama:11434", 30)
    e.http = _mock(handler)
    vecs = e.embed_documents(["a", "b"])
    assert vecs == [[0.6, 0.8], [0.6, 0.8]]
    assert e.model_id == "ollama:nomic-embed-text"


def test_registry_builds_free_providers(monkeypatch):
    from ragx.config import Settings
    from ragx.llm.registry import Registry

    s = Settings(
        generator="ollama:qwen3:4b",
        verifier="groq:llama-3.3-70b-versatile",
        utility="openrouter:meta-llama/llama-3.3-70b-instruct:free",
        embedder="ollama:nomic-embed-text",
        groq_api_key=None,
        openrouter_api_key="or-key",
        ollama_num_ctx=8192,
    )
    reg = Registry(s)
    assert reg.chains["generator"][0].name == "ollama"  # no key needed
    assert reg.is_offline("verifier")  # groq without a key -> offline fallback + note
    assert any("console.groq.com" in n for n in reg.notes["verifier"])
    assert reg.chains["utility"][0].name == "openrouter"
    assert reg.context_tokens("generator") == 8192
    assert reg.embedder().model_id == "ollama:nomic-embed-text"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]}))
    st = reg.ollama_status()
    assert st["running"] and st["missing"] == ["nomic-embed-text"]


def test_small_context_model_disables_whole_kb_route(env, docs_dir):
    """A small local model must not be handed the whole knowledge base."""
    from conftest import ask, make_kb

    from ragx.llm import get_registry

    kb, _ = make_kb(docs_dir)
    reg = get_registry()
    r = ask(kb, "What response time do Enterprise customers get for critical incidents?")
    assert r["route"] == "long_context"  # tiny KB fits a big window
    for p in reg.chains["generator"]:
        p.max_context = 500  # e.g. a local model with a small window
    r = ask(kb, "What response time do Enterprise customers get for critical incidents?")
    assert r["route"] in ("fast", "standard")
    assert r["status"] == "verified"
