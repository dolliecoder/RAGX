"""Role-based model registry with fallback chains, circuit breakers and metering.

Roles: generator, verifier, utility, judge. Each role resolves to an ordered chain
of providers; a failing provider is skipped (circuit breaker) and the next one is
tried. If no provider has credentials the role falls back to the offline provider.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from .base import LLMProvider, LLMRequest, LLMResponse, ProviderError, current_meter, extract_json
from .embeddings import Embedder, GeminiEmbedder, HashEmbedder, OpenAIEmbedder, VoyageEmbedder

log = logging.getLogger("ragx.llm")

ROLES = ("generator", "verifier", "utility", "judge")


@dataclass
class Breaker:
    failures: int = 0
    open_until: float = 0.0
    last_error: str = ""
    successes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    THRESHOLD = 3
    COOLDOWN = 60.0

    def available(self) -> bool:
        return time.monotonic() >= self.open_until

    def ok(self) -> None:
        with self.lock:
            self.failures = 0
            self.successes += 1

    def fail(self, err: str) -> None:
        with self.lock:
            self.failures += 1
            self.last_error = err[:300]
            if self.failures >= self.THRESHOLD:
                self.open_until = time.monotonic() + self.COOLDOWN

    def state(self) -> str:
        return "open" if not self.available() else ("degraded" if self.failures else "closed")


def _build_provider(spec: str, settings: Settings) -> tuple[LLMProvider | None, str]:
    provider, _, model = spec.strip().partition(":")
    provider = provider.lower()
    t = settings.request_timeout
    if provider == "fake":
        from .fake import FakeProvider

        return FakeProvider(model or "offline"), ""
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            return None, "RAGX_ANTHROPIC_API_KEY not set"
        from .providers import AnthropicProvider

        return AnthropicProvider(model, settings.anthropic_api_key, t), ""
    if provider == "openai":
        if not settings.openai_api_key:
            return None, "RAGX_OPENAI_API_KEY not set"
        from .providers import OpenAIProvider

        return OpenAIProvider(model, settings.openai_api_key, t), ""
    if provider == "gemini":
        if not settings.gemini_api_key:
            return None, "RAGX_GEMINI_API_KEY not set"
        from .providers import GeminiProvider

        return GeminiProvider(model, settings.gemini_api_key, t), ""
    return None, f"unknown provider '{provider}'"


class Registry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.chains: dict[str, list[LLMProvider]] = {}
        self.notes: dict[str, list[str]] = {}
        self.breakers: dict[str, Breaker] = {}
        for role in ROLES:
            self._build_role(role)
        self._embedder: Embedder | None = None
        self._embedder_note = ""
        self._reranker = None

    # ------------------------------------------------------------------ build
    def _role_specs(self, role: str) -> list[str]:
        s = self.settings
        primary = {
            "generator": s.generator,
            "verifier": s.verifier,
            "utility": s.utility,
            "judge": s.judge or s.verifier,
        }[role]
        fallbacks = {
            "generator": s.generator_fallbacks,
            "verifier": s.verifier_fallbacks,
            "utility": s.utility_fallbacks,
            "judge": s.verifier_fallbacks,
        }[role]
        return [primary] + [f for f in fallbacks.split(",") if f.strip()]

    def _build_role(self, role: str) -> None:
        chain: list[LLMProvider] = []
        notes: list[str] = []
        for spec in self._role_specs(role):
            p, note = _build_provider(spec, self.settings)
            if p is None:
                notes.append(f"{spec}: {note}")
            else:
                chain.append(p)
        if not chain:
            from .fake import FakeProvider

            chain.append(FakeProvider("offline"))
            notes.append("no configured provider available: using OFFLINE heuristic provider")
        self.chains[role] = chain
        self.notes[role] = notes

    def _key(self, p: LLMProvider) -> str:
        return f"{p.name}:{p.model}"

    def breaker(self, p: LLMProvider) -> Breaker:
        return self.breakers.setdefault(self._key(p), Breaker())

    # ------------------------------------------------------------------ calls
    def call(self, role: str, req: LLMRequest) -> LLMResponse:
        """Run a JSON task on ``role``; returns a response with ``.data`` parsed."""
        meter = current_meter.get()
        if meter is not None:
            meter.reserve(req.task)
        errors: list[str] = []
        chain = self.chains[role]
        candidates = [p for p in chain if self.breaker(p).available()] or chain[-1:]
        for p in candidates:
            br = self.breaker(p)
            for attempt in range(2):
                try:
                    r = req
                    if attempt == 1:
                        r = LLMRequest(**{**req.__dict__, "user": req.user + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."})
                    resp = p.complete(r)
                    resp.data = resp.data if resp.data is not None else extract_json(resp.text)
                    # Some models wrap the object in a one-element list.
                    if req.schema and req.schema.get("type") == "object" and isinstance(resp.data, list):
                        if len(resp.data) == 1 and isinstance(resp.data[0], dict):
                            resp.data = resp.data[0]
                        else:
                            raise ValueError("expected a JSON object, got a list")
                    br.ok()
                    if meter is not None:
                        meter.record(resp)
                    return resp
                except ValueError as e:  # JSON parse failure -> one corrective retry
                    errors.append(f"{self._key(p)}: bad JSON ({e})")
                    continue
                except ProviderError as e:
                    errors.append(f"{self._key(p)}: {e}")
                    br.fail(str(e))
                    break
                except Exception as e:  # noqa: BLE001 - never let one provider kill the pipeline
                    log.exception("provider %s crashed", self._key(p))
                    errors.append(f"{self._key(p)}: {type(e).__name__}: {e}")
                    br.fail(str(e))
                    break
            else:
                br.fail("invalid JSON twice")
        raise ProviderError(f"all providers failed for role={role} task={req.task}: {' | '.join(errors)}")

    def model_label(self, role: str) -> str:
        p = self.chains[role][0]
        return self._key(p)

    def is_offline(self, role: str) -> bool:
        return all(p.name == "fake" for p in self.chains[role])

    # ------------------------------------------------------------ embeddings
    def embedder(self) -> Embedder:
        if self._embedder is None:
            s = self.settings
            provider, _, model = s.embedder.partition(":")
            emb: Embedder | None = None
            try:
                if provider == "openai" and s.openai_api_key:
                    emb = OpenAIEmbedder(model or "text-embedding-3-small", s.openai_api_key, s.request_timeout)
                elif provider == "gemini" and s.gemini_api_key:
                    emb = GeminiEmbedder(model or "gemini-embedding-001", s.gemini_api_key, s.request_timeout)
                elif provider == "voyage" and s.voyage_api_key:
                    emb = VoyageEmbedder(model or "voyage-3.5", s.voyage_api_key, s.request_timeout)
                elif provider != "hash":
                    self._embedder_note = f"embedder '{s.embedder}' missing credentials: using local hash embedder"
            except Exception as e:  # noqa: BLE001
                self._embedder_note = f"embedder init failed ({e}): using local hash embedder"
            self._embedder = emb or HashEmbedder()
        return self._embedder

    def reranker(self):
        if self._reranker is None:
            from ..retrieval.rerank import build_reranker

            self._reranker = build_reranker(self.settings.reranker, self)
        return self._reranker

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        roles = {}
        for role in ROLES:
            roles[role] = {
                "chain": [
                    {"provider": self._key(p), "state": self.breaker(p).state(), "last_error": self.breaker(p).last_error}
                    for p in self.chains[role]
                ],
                "notes": self.notes[role],
                "offline": self.is_offline(role),
            }
        independent = self.model_label("generator") != self.model_label("verifier")
        emb = self.embedder()
        return {
            "roles": roles,
            "verifier_independent": independent,
            "embedder": {"model": emb.model_id, "note": self._embedder_note},
            "reranker": getattr(self.reranker(), "label", "unknown"),
        }


_registry: Registry | None = None
_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    with _lock:
        if _registry is None:
            _registry = Registry()
        return _registry


def set_registry(reg: Registry | None) -> None:
    global _registry
    with _lock:
        _registry = reg
