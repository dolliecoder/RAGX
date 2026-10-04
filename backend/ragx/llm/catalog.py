"""List the models a provider offers with the configured credentials.

Model names change every few months; this lets people pick working (and free)
models instead of guessing.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings


class CatalogError(RuntimeError):
    pass


def list_models(provider: str, settings: Settings) -> list[dict[str, Any]]:
    try:
        if provider == "ollama":
            r = httpx.get(f"{settings.ollama_url.rstrip('/')}/api/tags", timeout=5)
            r.raise_for_status()
            return [
                {"id": m["name"], "free": True, "note": f"{m.get('size', 0) / 1e9:.1f} GB local"}
                for m in r.json().get("models", [])
            ]
        if provider == "gemini":
            if not settings.gemini_api_key:
                raise CatalogError("RAGX_GEMINI_API_KEY is not set (free key at aistudio.google.com)")
            out: list[dict[str, Any]] = []
            token = None
            while True:
                params = {"pageSize": 200, **({"pageToken": token} if token else {})}
                r = httpx.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params=params,
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                for m in data.get("models", []):
                    methods = m.get("supportedGenerationMethods", [])
                    kind = "chat" if "generateContent" in methods else "embedding" if "embedContent" in methods else None
                    if kind:
                        name = m["name"].removeprefix("models/")
                        out.append(
                            {"id": name, "kind": kind, "free": True, "note": "free tier: lite models have the highest daily limits" if "lite" in name else ""}
                        )
                token = data.get("nextPageToken")
                if not token:
                    return out
        if provider == "groq":
            if not settings.groq_api_key:
                raise CatalogError("RAGX_GROQ_API_KEY is not set (free key at console.groq.com)")
            r = httpx.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                timeout=15,
            )
            r.raise_for_status()
            return [{"id": m["id"], "free": True, "note": f"context {m.get('context_window', '?')}"} for m in r.json().get("data", [])]
        if provider == "openrouter":
            r = httpx.get("https://openrouter.ai/api/v1/models", timeout=15)
            r.raise_for_status()
            return [
                {"id": m["id"], "free": True, "note": f"context {m.get('context_length', '?')}"}
                for m in r.json().get("data", [])
                if m["id"].endswith(":free")
            ]
    except httpx.HTTPError as e:
        raise CatalogError(f"could not list {provider} models: {e}") from e
    raise CatalogError(f"listing is supported for: ollama, gemini, groq, openrouter (not '{provider}')")
