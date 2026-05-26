"""LLM GEO citation tracker — does the LLM mention our site/brand?

Calls multiple LLMs through the existing LiteLLM proxy (single endpoint,
multiple providers) and detects brand/competitor mentions in the answer.

This is the **G**EO part of the SEO/GEO module — tracking whether AI
assistants surface our content when users ask natural product/dev
questions.

Engines tracked:
- ``chatgpt`` → openai/gpt-4o (via LiteLLM)
- ``perplexity`` → openrouter/perplexity/sonar-online (web-augmented)
- ``claude`` → anthropic/claude-sonnet-4 (web-augmented if available)
- ``gemini`` → google/gemini-2.0-flash
- ``kimi`` → fireworks/kimi-k2 (no web — pure parametric knowledge)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError

from kronos.seo_geo.config import BRAND_PATTERNS, COMPETITOR_PATTERNS

log = logging.getLogger("kronos.seo_geo.trackers.llm")

_TIMEOUT = 60  # LLMs are slow


@dataclass(frozen=True)
class Engine:
    id: str           # 'chatgpt' | 'perplexity' | ...
    model: str        # LiteLLM model id
    web_grounded: bool


# All routed through LiteLLM proxy. Adjust the model ids to match what
# is registered on the LiteLLM server.
ENGINES: tuple[Engine, ...] = (
    Engine("chatgpt", "openai/gpt-4o-mini", web_grounded=False),
    Engine("perplexity", "openrouter/perplexity/sonar", web_grounded=True),
    Engine("claude", "anthropic/claude-haiku-4-5-20251001", web_grounded=False),
    Engine("gemini", "gemini/gemini-2.0-flash", web_grounded=True),
    Engine("kimi", "openrouter/moonshotai/kimi-k2", web_grounded=False),
)


def _litellm_chat(model: str, question: str) -> tuple[str, str | None]:
    """Send chat completion via LiteLLM proxy. Returns (answer, error)."""
    base = (os.environ.get("LITELLM_BASE_URL") or "").rstrip("/")
    key = os.environ.get("LITELLM_ADMIN_KEY") or ""
    if not base or not key:
        return "", "LiteLLM not configured"

    body = json.dumps({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Answer the user's question "
                    "concretely and concisely. If you know specific products, "
                    "tools, or websites that answer the question, mention them "
                    "by name. Cite URLs when relevant."
                ),
            },
            {"role": "user", "content": question},
        ],
        "max_tokens": 600,
        "temperature": 0.4,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; KronosNexus/1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        err = e.read()[:200].decode("utf-8", errors="replace")
        return "", f"HTTP {e.code}: {err}"
    except Exception as e:
        return "", str(e)
    try:
        return data["choices"][0]["message"]["content"], None
    except (KeyError, IndexError) as e:
        return "", f"unexpected response: {e}"


def _detect_mentions(text: str, site_id: str) -> tuple[bool, list[str], str | None]:
    """Detect our brand + competitor mentions in answer.

    Returns (we_cited, competitors_cited_list, our_cited_url_if_any).
    """
    low = text.lower()
    we_cited = any(p in low for p in BRAND_PATTERNS.get(site_id, []))
    competitors = [
        c for c in COMPETITOR_PATTERNS.get(site_id, []) if c.lower() in low
    ]
    # Try to extract our URL if explicitly cited.
    cited_url = None
    if we_cited:
        # Look for a URL on our domain.
        match = re.search(
            rf"https?://(?:www\.)?({site_id}\.(?:co|pro))[^\s\)\]\"]*",
            text,
            re.IGNORECASE,
        )
        if match:
            cited_url = match.group(0)
    return we_cited, competitors, cited_url


def ask_engine(engine: Engine, question: str, site_id: str) -> dict:
    """Run one (engine, question) and produce a citation record."""
    answer, err = _litellm_chat(engine.model, question)
    if err:
        return {
            "engine": engine.id, "question": question, "answer": "",
            "cited": False, "cited_url": None, "competitors_cited": "[]",
            "error": err,
        }
    cited, competitors, cited_url = _detect_mentions(answer, site_id)
    return {
        "engine": engine.id,
        "question": question,
        "answer": answer,
        "cited": cited,
        "cited_url": cited_url,
        "competitors_cited": json.dumps(competitors),
        "error": None,
    }
