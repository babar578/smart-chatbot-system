"""
Live web search for current-affairs / market questions.

Providers (in order):
  1. Tavily — if TAVILY_API_KEY is set (best quality)
  2. DuckDuckGo — free fallback, no API key required
"""

from __future__ import annotations

import os
import re

# Phrases that usually need fresh web results (not model memory).
_LIVE_HINTS = re.compile(
    r"\b("
    r"today|tonight|yesterday|this\s+week|this\s+month|latest|breaking|"
    r"current\s+affairs?|news|headline|update|live|"
    r"stock|share\s*price|market|sensex|nifty|nasdaq|dow|"
    r"crypto|bitcoin|election|score|weather|who\s+won|price\s+of|"
    r"aaj|khabar|bazaar|share\s*bazaar"
    r")\b",
    re.IGNORECASE,
)


def needs_live_info(question: str) -> bool:
    """True when the question likely needs up-to-date web data."""
    text = (question or "").strip()
    if not text:
        return False
    return bool(_LIVE_HINTS.search(text))


def _normalize(results):
    """Keep a uniform shape: title, url, snippet."""
    cleaned = []
    seen = set()
    for item in results or []:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip() or url
        snippet = (item.get("snippet") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        cleaned.append({"title": title, "url": url, "snippet": snippet})
    return cleaned


def search_tavily(query: str, max_results: int = 5):
    import requests

    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return []

    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return _normalize(
        [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content"),
            }
            for item in data.get("results") or []
        ]
    )


def search_duckduckgo(query: str, max_results: int = 5):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    return _normalize(
        [
            {
                "title": item.get("title"),
                "url": item.get("href"),
                "snippet": item.get("body"),
            }
            for item in raw
        ]
    )


def search_web(query: str, max_results: int = 5):
    """
    Run a web search. Prefer Tavily when configured; otherwise DuckDuckGo.
    Returns (results, provider_name).
    """
    query = (query or "").strip()
    if not query:
        return [], "none"

    if (os.getenv("TAVILY_API_KEY") or "").strip():
        try:
            results = search_tavily(query, max_results=max_results)
            if results:
                return results, "tavily"
        except Exception:
            # Fall through to DuckDuckGo
            pass

    results = search_duckduckgo(query, max_results=max_results)
    return results, "duckduckgo"


def format_web_context(results):
    """Turn search hits into a prompt-friendly context block."""
    if not results:
        return "No web search results were found."

    lines = []
    for i, item in enumerate(results, start=1):
        lines.append(
            f"{i}. {item['title']}\n"
            f"   URL: {item['url']}\n"
            f"   Snippet: {item['snippet'] or '(no snippet)'}"
        )
    return "\n\n".join(lines)


def source_labels(results):
    """URL list for the UI Sources line (clickable in the frontend)."""
    labels = []
    seen = set()
    for item in results or []:
        url = (item.get("url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            labels.append(url)
    return labels
