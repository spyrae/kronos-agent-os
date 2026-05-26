"""Format SEO/GEO state as Telegram digest (HTML).

Two outputs:
- ``format_weekly_report`` — full weekly digest sent after run_full_check.
- ``format_pulse_summary`` — one-paragraph summary for the daily pulse.
"""

from __future__ import annotations

from kronos.seo_geo.config import KEYWORDS, SITE_BY_ID
from kronos.seo_geo.store import SeoGeoStore, get_store


def _top_movers(store: SeoGeoStore, site_id: str, engine: str, n: int = 5) -> tuple[list, list]:
    """Return (gainers, losers) — keywords with biggest 7-day position change."""
    gainers, losers = [], []
    for kw, kw_site, _locale, tier, _category in KEYWORDS:
        if kw_site != site_id:
            continue
        if tier not in ("A", "B"):
            continue
        delta = store.position_delta(site_id, engine, kw, days=7)
        if delta is None:
            continue
        latest = next(
            (r for r in store.latest_positions(site_id, engine) if r["keyword"] == kw),
            None,
        )
        cur_pos = latest["position"] if latest else None
        entry = (kw, cur_pos, delta)
        if delta < 0:  # negative = improved (lower number)
            gainers.append(entry)
        elif delta > 0:
            losers.append(entry)
    gainers.sort(key=lambda x: x[2])  # most negative first
    losers.sort(key=lambda x: -x[2])  # most positive first
    return gainers[:n], losers[:n]


def _format_site_block(site_id: str, store: SeoGeoStore) -> str:
    site = SITE_BY_ID[site_id]
    out: list[str] = [f"<b>📈 {site.url}</b>"]

    # ── Positions ──
    for engine_label, engine_id in (
        ("Google", "google_com" if site_id != "futurecraft" else "google_com"),
        ("Yandex", "yandex_ru"),
    ):
        rows = store.latest_positions(site_id, engine_id)
        if not rows:
            continue
        ranked = [r for r in rows if r["position"] is not None]
        top10 = sum(1 for r in ranked if r["position"] <= 10)
        top20 = sum(1 for r in ranked if r["position"] <= 20)
        out.append(
            f"  {engine_label}: <b>{len(ranked)}</b>/{len(rows)} ranked"
            f" (top10: <b>{top10}</b>, top20: <b>{top20}</b>)"
        )
        gainers, losers = _top_movers(store, site_id, engine_id, n=3)
        if gainers:
            tops = ", ".join(f"{kw} #{pos} ({d:+d})" for kw, pos, d in gainers)
            out.append(f"    🟢 {tops}")
        if losers:
            tops = ", ".join(f"{kw} #{pos} ({d:+d})" for kw, pos, d in losers)
            out.append(f"    🔴 {tops}")

    # ── GEO citations ──
    rates = store.citation_rate(site_id, days=7)
    if rates:
        parts = [f"{eng}: <b>{rate}%</b>" for eng, rate in sorted(rates.items())]
        out.append(f"  🤖 GEO citation rate: {', '.join(parts)}")

    # ── GSC ──
    totals = store.gsc_totals(site_id, days=28)
    if totals.get("impressions"):
        out.append(
            f"  🔍 GSC 28d: <b>{totals['clicks']}</b> clicks /"
            f" <b>{totals['impressions']}</b> impr, avg pos <b>{totals['avg_position']}</b>"
        )

    return "\n".join(out)


def format_weekly_report() -> str:
    """Full weekly SEO/GEO digest."""
    store = get_store()
    lines: list[str] = ["<b>📊 SEO / GEO weekly</b>", ""]
    for site_id in SITE_BY_ID:
        lines.append(_format_site_block(site_id, store))
        lines.append("")
    return "\n".join(lines).strip()


def format_pulse_summary() -> str:
    """Compact one-block summary for the daily pulse."""
    store = get_store()
    parts: list[str] = []
    for site_id, site in SITE_BY_ID.items():
        google_rows = store.latest_positions(site_id, "google_com")
        ranked = [r for r in google_rows if r["position"] is not None]
        top10 = sum(1 for r in ranked if r["position"] <= 10)
        rates = store.citation_rate(site_id, days=7)
        avg_rate = (
            round(sum(rates.values()) / len(rates), 1) if rates else 0.0
        )
        gsc_totals = store.gsc_totals(site_id, days=7)
        parts.append(
            f"{site.id}: top10={top10}, GEO citation rate={avg_rate}%,"
            f" GSC 7d clicks={gsc_totals.get('clicks', 0)}"
        )
    return " | ".join(parts)
