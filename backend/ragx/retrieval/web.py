"""Web search fallback (disabled by default; used only by the heal ladder)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings

log = logging.getLogger("ragx.web")


@dataclass
class WebResult:
    url: str
    title: str
    text: str


def web_search(query: str, settings: Settings, max_results: int = 5) -> list[WebResult]:
    provider = settings.web_search_provider
    try:
        if provider == "tavily" and settings.tavily_api_key:
            r = httpx.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": max_results, "search_depth": "advanced", "include_raw_content": False},
                headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            return [
                WebResult(x.get("url", ""), x.get("title", ""), (x.get("content") or "")[:4000])
                for x in r.json().get("results", [])
                if x.get("content")
            ]
        if provider == "brave" and settings.brave_api_key:
            r = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": settings.brave_api_key, "Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            items = (r.json().get("web") or {}).get("results", [])
            out = []
            for x in items:
                text = " ".join(filter(None, [x.get("description", "")] + list(x.get("extra_snippets") or [])))
                if text:
                    out.append(WebResult(x.get("url", ""), x.get("title", ""), text[:4000]))
            return out
    except httpx.HTTPError as e:
        log.warning("web search failed: %s", e)
        return []
    return []


def web_available(settings: Settings) -> bool:
    return (settings.web_search_provider == "tavily" and bool(settings.tavily_api_key)) or (
        settings.web_search_provider == "brave" and bool(settings.brave_api_key)
    )
