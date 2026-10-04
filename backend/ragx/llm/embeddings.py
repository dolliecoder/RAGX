"""Embedding providers. ``hash`` is a local, deterministic, offline fallback."""

from __future__ import annotations

import math
import re
import zlib
from typing import Protocol

import httpx

from .providers import _post_with_retries

_WORD = re.compile(r"[\w][\w\-\.]*", re.UNICODE)


class Embedder(Protocol):
    model_id: str  # stored on every chunk; a change means re-embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else v


class HashEmbedder:
    """Feature-hashing embedder (words, bigrams, char 4-grams). Not semantic, but
    deterministic and good enough for offline development and tests."""

    def __init__(self, dims: int = 512):
        self.dims = dims
        self.model_id = f"hash:hash-{dims}"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        words = [w.lower().strip(".-") for w in _WORD.findall(text)]
        words = [w for w in words if w]
        feats: list[tuple[str, float]] = [(w, 1.0) for w in words]
        feats += [(f"{a} {b}", 0.7) for a, b in zip(words, words[1:])]
        for w in words:
            padded = f"#{w}#"
            feats += [(padded[i : i + 4], 0.3) for i in range(max(1, len(padded) - 3))]
        for f, weight in feats:
            h = zlib.crc32(f.encode("utf-8"))
            sign = 1.0 if (h >> 31) & 1 else -1.0
            v[h % self.dims] += sign * weight
        v = [math.copysign(math.log1p(abs(x)), x) for x in v]
        return _normalize(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class OpenAIEmbedder:
    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model
        self.model_id = f"openai:{model}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = httpx.Client(timeout=timeout)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            batch = [t[:30000] or " " for t in texts[i : i + 128]]
            data = _post_with_retries(
                self.http,
                "https://api.openai.com/v1/embeddings",
                json_body={"model": self.model, "input": batch},
                headers=self.headers,
            )
            out += [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class GeminiEmbedder:
    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model
        self.model_id = f"gemini:{model}"
        self.headers = {"x-goog-api-key": api_key}
        self.http = httpx.Client(timeout=timeout)

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:batchEmbedContents"
        for i in range(0, len(texts), 100):
            reqs = [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": t[:20000] or " "}]},
                    "taskType": task_type,
                }
                for t in texts[i : i + 100]
            ]
            data = _post_with_retries(self.http, url, json_body={"requests": reqs}, headers=self.headers)
            out += [_normalize(e["values"]) for e in data["embeddings"]]
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


class OllamaEmbedder:
    """Local open-source embeddings (e.g. nomic-embed-text, bge-m3) via Ollama."""

    def __init__(self, model: str, base_url: str, timeout: float):
        self.model = model
        self.model_id = f"ollama:{model}"
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=max(timeout, 300.0))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            data = _post_with_retries(
                self.http,
                f"{self.base_url}/api/embed",
                json_body={"model": self.model, "input": [t[:8000] or " " for t in texts[i : i + 32]], "truncate": True},
                headers={},
            )
            out += [_normalize(v) for v in data["embeddings"]]
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class VoyageEmbedder:
    def __init__(self, model: str, api_key: str, timeout: float):
        self.model = model
        self.model_id = f"voyage:{model}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = httpx.Client(timeout=timeout)

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            data = _post_with_retries(
                self.http,
                "https://api.voyageai.com/v1/embeddings",
                json_body={"model": self.model, "input": [t or " " for t in texts[i : i + 64]], "input_type": input_type},
                headers=self.headers,
            )
            out += [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
