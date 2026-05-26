"""Orchestration for SEO/GEO checks.

Two entry points:
- ``run_full_check`` — heavy weekly job: positions for all tracked keywords
  on appropriate engines, GEO citations for all questions on all LLM
  engines, GSC pull for all sites. Writes to store, returns Telegram
  summary text.
- ``run_gsc_only`` — daily lightweight job to refresh GSC metrics so the
  daily pulse always has fresh impressions/clicks numbers.
"""

from __future__ import annotations

import logging
import time

from kronos.seo_geo.config import (
    GEO_QUESTIONS,
    KEYWORDS,
    SITE_BY_ID,
    SITES,
)
from kronos.seo_geo.store import get_store
from kronos.seo_geo.trackers import google, gsc, llm, yandex

log = logging.getLogger("kronos.seo_geo.runner")


# Throttle so we don't trip Brave/Yandex rate limits.
_PER_CHECK_DELAY = 1.2


def _run_positions(tiers: tuple[str, ...]) -> dict:
    """Run SERP position checks for keywords matching the given tiers."""
    store = get_store()
    counts = {"checked": 0, "ranked": 0, "errors": 0}
    for kw, site_id, locale, tier, category in KEYWORDS:
        if tier not in tiers:
            continue
        site = SITE_BY_ID[site_id]

        # Google check — always run.
        try:
            pos, url = google.find_position(site.url, kw, locale=locale)
            store.record_position(
                site_id=site_id, engine=google.engine_id(locale),
                keyword=kw, locale=locale, tier=tier, category=category,
                position=pos, url=url,
            )
            counts["checked"] += 1
            if pos is not None:
                counts["ranked"] += 1
        except Exception as e:
            log.warning("google check failed: %s | %s", kw, e)
            store.record_position(
                site_id=site_id, engine=google.engine_id(locale),
                keyword=kw, locale=locale, tier=tier, category=category,
                position=None, error=str(e)[:200],
            )
            counts["errors"] += 1

        # Yandex check — only for RU locale.
        if locale == "ru":
            time.sleep(_PER_CHECK_DELAY)
            try:
                pos, url = yandex.find_position(site.url, kw, locale="ru")
                store.record_position(
                    site_id=site_id, engine=yandex.engine_id(),
                    keyword=kw, locale=locale, tier=tier, category=category,
                    position=pos, url=url,
                )
                counts["checked"] += 1
                if pos is not None:
                    counts["ranked"] += 1
            except Exception as e:
                log.warning("yandex check failed: %s | %s", kw, e)
                store.record_position(
                    site_id=site_id, engine=yandex.engine_id(),
                    keyword=kw, locale=locale, tier=tier, category=category,
                    position=None, error=str(e)[:200],
                )
                counts["errors"] += 1

        time.sleep(_PER_CHECK_DELAY)
    return counts


def _run_geo_citations() -> dict:
    """Ask all LLM engines all GEO questions; record mentions."""
    store = get_store()
    counts = {"asked": 0, "cited": 0, "errors": 0}
    for question, site_id, locale in GEO_QUESTIONS:
        for engine in llm.ENGINES:
            try:
                rec = llm.ask_engine(engine, question, site_id)
            except Exception as e:
                log.warning("llm.ask_engine crashed: %s", e)
                rec = {
                    "engine": engine.id, "question": question, "answer": "",
                    "cited": False, "cited_url": None, "competitors_cited": "[]",
                    "error": str(e)[:200],
                }
            store.record_citation(
                site_id=site_id,
                engine=rec["engine"],
                question=rec["question"],
                locale=locale,
                answer=rec["answer"],
                cited=bool(rec["cited"]),
                cited_url=rec["cited_url"],
                competitors_cited=rec["competitors_cited"],
                error=rec.get("error"),
            )
            counts["asked"] += 1
            if rec["cited"]:
                counts["cited"] += 1
            if rec.get("error"):
                counts["errors"] += 1
            time.sleep(0.5)
    return counts


def _run_gsc() -> dict:
    """Pull top GSC queries (28d window) for each site."""
    store = get_store()
    counts = {"sites": 0, "queries": 0}
    for site in SITES:
        rows = gsc.top_queries(site.gsc_property, days=28, limit=100)
        for r in rows:
            store.record_gsc(
                site_id=site.id,
                window_days=28,
                query=r["query"],
                clicks=r["clicks"],
                impressions=r["impressions"],
                ctr=r["ctr"],
                position=r["position"],
            )
        counts["sites"] += 1 if rows else 0
        counts["queries"] += len(rows)
    return counts


def run_full_check(tiers: tuple[str, ...] = ("A", "B")) -> dict:
    """Full Sunday-weekly run. Returns aggregated counts."""
    log.info("seo_geo: starting full check tiers=%s", tiers)
    positions = _run_positions(tiers)
    citations = _run_geo_citations()
    gsc_pull = _run_gsc()
    return {"positions": positions, "geo": citations, "gsc": gsc_pull}


def run_gsc_only() -> dict:
    """Daily lightweight GSC refresh."""
    return {"gsc": _run_gsc()}
