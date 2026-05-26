"""SEO/GEO tracking module for Nexus.

Tracks keyword positions in Google + Yandex SERP, brand mentions in LLM
answers (ChatGPT/Perplexity/Claude/Gemini), and organic traffic via
Google Search Console for journeybay.co and futurecraft.pro.

Public entry points:
- ``run_full_check`` — Sunday weekly job: pull positions + GEO citations +
  GSC metrics into the SQLite store, return Telegram-ready summary.
- ``collect_summary`` — quick snapshot for the daily pulse.
"""

from kronos.seo_geo.config import GEO_QUESTIONS, KEYWORDS, SITES
from kronos.seo_geo.store import get_store

__all__ = ["GEO_QUESTIONS", "KEYWORDS", "SITES", "get_store"]
