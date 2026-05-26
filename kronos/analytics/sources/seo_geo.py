"""SEO/GEO snapshot source for the daily pulse.

Reads from the existing SeoGeoStore — does NOT trigger fresh checks
(those happen in the weekly cron). Just summarises latest state.
"""

from __future__ import annotations

import logging

log = logging.getLogger("kronos.analytics.sources.seo_geo")


def collect() -> dict:
    """Return condensed SEO/GEO state for daily pulse."""
    try:
        from kronos.seo_geo.config import SITE_BY_ID
        from kronos.seo_geo.store import get_store
    except Exception as e:
        return {"error": f"seo_geo module unavailable: {e}"}
    try:
        store = get_store()
    except Exception as e:
        return {"error": f"store init failed: {e}"}

    out: dict[str, object] = {}
    has_any = False
    for site_id, site in SITE_BY_ID.items():
        # Positions
        google_rows = store.latest_positions(site_id, "google_com")
        ranked = [r for r in google_rows if r["position"] is not None]
        top10 = sum(1 for r in ranked if r["position"] <= 10)
        top20 = sum(1 for r in ranked if r["position"] <= 20)
        # GEO
        rates = store.citation_rate(site_id, days=7)
        avg_rate = (
            round(sum(rates.values()) / len(rates), 1) if rates else 0.0
        )
        # GSC
        gsc_28d = store.gsc_totals(site_id, days=28)

        out[f"{site_id}_top10"] = top10
        out[f"{site_id}_top20"] = top20
        out[f"{site_id}_geo_citation_rate"] = avg_rate
        out[f"{site_id}_gsc_clicks_28d"] = gsc_28d.get("clicks", 0)
        out[f"{site_id}_gsc_impressions_28d"] = gsc_28d.get("impressions", 0)

        if google_rows or rates or gsc_28d.get("impressions"):
            has_any = True

    if not has_any:
        return {"error": "no SEO/GEO data yet — first weekly run pending"}
    return out
