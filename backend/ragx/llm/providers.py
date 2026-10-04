"""Concrete chat providers: Anthropic (official SDK), OpenAI and Gemini (REST)."""

from __future__ import annotations

import random
import re
import time
from typing import Any

import anthropic
import httpx

from .base import LLMRequest, LLMResponse, ProviderError, extract_json

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

# Claude models that accept the server-side refusal fallback ("default" routing).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


def _post_with_retries(client: httpx.Client, url: str, *, json_body: dict, headers: dict, attempts: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            r = client.post(url, json=json_body, headers=headers)
        except httpx.HTTPError as e:  # network / timeout
            last = e
        else:
            if r.status_code < 400:
                return r.json()
            if r.status_code not in _RETRY_STATUS:
                raise ProviderError(f"HTTP {r.status_code}: {r.text[:500]}", retryable=False)
            if r.status_code == 429 and "PerDay" in r.text:
                # Daily quota exhausted: retrying cannot help, fail over immediately.
                raise ProviderError(f"daily quota exhausted: {r.text[:300]}", retryable=False)
            last = ProviderError(f"HTTP {r.status_code}: {r.text[:300]}")
            # An overloaded model rarely recovers within seconds: retry once, then let the
            # registry fail over to the next provider in the chain.
            if r.status_code in (503, 529) and attempt >= 1:
                break
        if attempt < attempts - 1:
            time.sleep(min(2**attempt + random.random(), 20))
    raise ProviderError(f"request failed after {attempts} attempts: {last}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model
        self.max_context = 200_000 if "haiku" in model else 1_000_000
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def _supports_effort(self) -> bool:
        return "haiku" not in self.model and not self.model.endswith("-4-5")

    def complete(self, req: LLMRequest) -> LLMResponse:
        system_block: dict[str, Any] = {"type": "text", "text": req.system}
        if req.cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(req.max_tokens, 1024),
            "system": [system_block],
            "messages": [{"role": "user", "content": req.user}],
        }
        output_config: dict[str, Any] = {}
        if req.schema:
            output_config["format"] = {"type": "json_schema", "schema": req.schema}
        if req.effort and self._supports_effort():
            output_config["effort"] = req.effort
        if output_config:
            kwargs["output_config"] = output_config
        try:
            msg = self._create(kwargs)
        except anthropic.BadRequestError as e:
            # Schema features vary by model; degrade to prompt-only JSON once.
            if "format" in output_config and ("schema" in str(e).lower() or "output_config" in str(e).lower()):
                output_config.pop("format")
                if output_config:
                    kwargs["output_config"] = output_config
                else:
                    kwargs.pop("output_config", None)
                msg = self._create(kwargs)
            else:
                raise ProviderError(f"anthropic bad request: {e}", retryable=False) from e
        if msg.stop_reason == "refusal":
            raise ProviderError("anthropic refusal", retryable=False)
        if msg.stop_reason == "max_tokens":
            raise ProviderError("anthropic response truncated at max_tokens", retryable=False)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        usage = msg.usage
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=usage.output_tokens or 0,
        )

    def _create(self, kwargs: dict[str, Any]):
        try:
            if self.model.startswith(_FALLBACK_MODELS):
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    extra_body={"fallbacks": "default"},
                    **kwargs,
                )
            return self.client.messages.create(**kwargs)
        except anthropic.BadRequestError:
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise ProviderError(f"anthropic: {e}", retryable=False) from e
        except anthropic.RateLimitError as e:
            raise ProviderError(f"anthropic rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic status {e.status_code}: {e}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"anthropic connection error: {e}") from e


class OpenAIProvider:
    name = "openai"
    max_context = 128_000

    def __init__(self, model: str, api_key: str, timeout: float, base_url: str = "https://api.openai.com/v1"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = httpx.Client(timeout=timeout)

    def complete(self, req: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "max_completion_tokens": max(req.max_tokens, 1024) * 2,
        }
        if req.schema:
            body["response_format"] = {"type": "json_object"}
        if req.effort and self.model.startswith(("o", "gpt-5")):
            body["reasoning_effort"] = req.effort
        data = _post_with_retries(self.http, f"{self.base_url}/chat/completions", json_body=body, headers=self.headers)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ProviderError("openai response truncated", retryable=False)
        usage = data.get("usage", {})
        return LLMResponse(
            text=choice["message"].get("content") or "",
            provider=self.name,
            model=self.model,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
        )


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Open reasoning models (Qwen3, DeepSeek-R1 ...) may prefix their answer with a
    <think> block that can itself contain braces; drop it before JSON parsing."""
    return _THINK.sub("", text).strip()


_THINKING_FAMILIES = ("qwen3", "deepseek-r1", "magistral", "phi4-reasoning")


class OllamaProvider:
    """Local open-source models served by Ollama (free, private, no API key).

    Uses Ollama's native chat API so the context window (num_ctx) and a JSON schema
    for structured output can be set; the OpenAI-compatible endpoint allows neither.
    """

    name = "ollama"

    def __init__(self, model: str, base_url: str, timeout: float, num_ctx: int):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.max_context = num_ctx
        self.http = httpx.Client(timeout=max(timeout, 300.0))  # CPU inference is slow

    def complete(self, req: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "stream": False,
            "options": {"num_ctx": self.num_ctx, "temperature": 0, "num_predict": max(req.max_tokens, 1024) * 2},
        }
        if req.schema:
            body["format"] = req.schema  # grammar-constrained JSON: reliable even on small models
        # Reasoning models (Qwen3, DeepSeek-R1 ...) think at length by default, which is
        # very slow on CPUs; the verifier catches mistakes, so skip it.
        if self.model.split(":")[0].lower().startswith(_THINKING_FAMILIES):
            body["think"] = False
        url = f"{self.base_url}/api/chat"
        try:
            try:
                data = _post_with_retries(self.http, url, json_body=body, headers={})
            except ProviderError as e:
                if "think" in body and not e.retryable and "think" in str(e).lower():
                    body.pop("think")  # older Ollama or a model without the switch
                    data = _post_with_retries(self.http, url, json_body=body, headers={})
                else:
                    raise
        except ProviderError as e:
            if "not found" in str(e).lower() and "model" in str(e).lower():
                raise ProviderError(f"Ollama model '{self.model}' is not installed: run `ollama pull {self.model}`", retryable=False) from e
            raise
        if data.get("done_reason") == "length":
            raise ProviderError("ollama response truncated (raise RAGX_OLLAMA_NUM_CTX or max tokens)", retryable=False)
        text = _strip_thinking((data.get("message") or {}).get("content", ""))
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
        )


class OpenAICompatProvider:
    """Any OpenAI-compatible chat API: Groq and OpenRouter free tiers, LM Studio, vLLM.

    JSON output degrades gracefully: json_schema -> json_object -> plain prompt, because
    support differs by host and model.
    """

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout: float,
        *,
        max_context: int = 32_000,
        json_schema: bool = False,
        extra_headers: dict[str, str] | None = None,
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_context = max_context
        self.json_schema = json_schema
        self.headers = {**({"Authorization": f"Bearer {api_key}"} if api_key else {}), **(extra_headers or {})}
        self.http = httpx.Client(timeout=timeout)

    def complete(self, req: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "max_tokens": max(req.max_tokens, 1024) * 2,
            "temperature": 0,
        }
        formats: list[dict[str, Any] | None] = [None]
        if req.schema:
            formats = [{"type": "json_object"}, None]
            if self.json_schema:
                formats.insert(0, {"type": "json_schema", "json_schema": {"name": req.task, "schema": req.schema, "strict": False}})
        url = f"{self.base_url}/chat/completions"
        last: ProviderError | None = None
        for fmt in formats:
            if fmt is None:
                body.pop("response_format", None)
            else:
                body["response_format"] = fmt
            try:
                data = _post_with_retries(self.http, url, json_body=body, headers=self.headers)
                break
            except ProviderError as e:
                last = e
                if e.retryable or "HTTP 400" not in str(e) and "HTTP 422" not in str(e):
                    raise
        else:
            assert last is not None
            raise last
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} returned no choices: {str(data)[:200]}", retryable=False)
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError(f"{self.name} response truncated", retryable=False)
        usage = data.get("usage") or {}
        return LLMResponse(
            text=_strip_thinking((choice.get("message") or {}).get("content") or ""),
            provider=self.name,
            model=self.model,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
        )


class GeminiProvider:
    name = "gemini"
    max_context = 1_000_000

    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model
        self.headers = {"x-goog-api-key": api_key}
        self.http = httpx.Client(timeout=timeout)

    def complete(self, req: LLMRequest) -> LLMResponse:
        gen_cfg: dict[str, Any] = {"maxOutputTokens": max(req.max_tokens * 2, 8192)}
        if req.schema:
            gen_cfg["responseMimeType"] = "application/json"
            gen_cfg["responseJsonSchema"] = req.schema  # enforces the exact output shape
        # Helper steps use low effort, answer writing and verification medium: Gemini 3
        # defaults to high thinking, which measured slower with identical answers.
        if req.effort in ("low", "medium") and self.model.startswith("gemini-3"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": req.effort}
        body = {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.user}]}],
            "generationConfig": gen_cfg,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        try:
            data = _post_with_retries(self.http, url, json_body=body, headers=self.headers)
        except ProviderError as e:
            optional = [k for k in ("thinkingConfig", "responseJsonSchema") if k in gen_cfg]
            if e.retryable or not optional:
                raise
            for k in optional:  # model rejected an optional setting: retry with defaults
                gen_cfg.pop(k)
            data = _post_with_retries(self.http, url, json_body=body, headers=self.headers)
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise ProviderError(f"gemini returned no candidates ({reason})", retryable=False)
        cand = cands[0]
        if cand.get("finishReason") == "MAX_TOKENS":
            raise ProviderError("gemini response truncated", retryable=False)
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = data.get("usageMetadata", {})
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=usage.get("promptTokenCount", 0),
            tokens_out=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
        )


def parse_json_response(resp: LLMResponse) -> LLMResponse:
    resp.data = extract_json(resp.text)
    return resp
