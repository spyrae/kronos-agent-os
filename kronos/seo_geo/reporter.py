"""Format SEO/GEO state as Telegram digest (HTML).

Two outputs:
- ``format_weekly_report`` — LLM-synthesised strategic digest with
  positions context, competitor landscape, GSC traffic, and concrete
  Action Items. Sent after run_full_check.
- ``format_pulse_summary`` — one-liner for the daily pulse aggregator.

Design principle: the LLM gets RICH context (concrete keywords, sample
answers, competitor counts, GSC queries) and is instructed to produce
strategic interpretation — not just a numeric dashboard. Anti-hallucination
guards ("say so honestly if no data") prevent invention.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from kronos.llm import ModelTier, get_model
from kronos.seo_geo.config import SITE_BY_ID
from kronos.seo_geo.store import SeoGeoStore, get_store

log = logging.getLogger("kronos.seo_geo.reporter")


# ── Data gathering ──────────────────────────────────────────────────────


def _gather_site_data(site_id: str, store: SeoGeoStore) -> dict:
    """Collect ALL data we have for one site into a structured dict."""
    site = SITE_BY_ID[site_id]
    data: dict = {
        "site_id": site_id,
        "url": site.url,
        "description": site.description,
        "positions": {},
        "geo": {},
        "gsc": {},
    }

    # Positions per engine
    for engine_label, engine_id in (("google", "google_com"), ("yandex", "yandex_ru")):
        rows = store.all_positions_with_meta(site_id, engine_id)
        if not rows:
            continue
        ranked = [r for r in rows if r["position"] is not None]
        top10 = [r for r in ranked if r["position"] <= 10]
        top20 = [r for r in ranked if r["position"] <= 20]
        not_ranked = [r for r in rows if r["position"] is None]

        # WoW movers
        gainers: list[tuple] = []
        losers: list[tuple] = []
        for r in rows:
            delta = store.position_delta(site_id, engine_id, r["keyword"], days=7)
            if delta is None:
                continue
            entry = (r["keyword"], r["position"], delta, r["tier"])
            if delta < 0:
                gainers.append(entry)
            elif delta > 0:
                losers.append(entry)
        gainers.sort(key=lambda x: x[2])
        losers.sort(key=lambda x: -x[2])

        # High-value gaps: Tier A keywords NOT in top 20 (most impactful misses)
        high_value_gaps = [r["keyword"] for r in not_ranked if r["tier"] == "A"][:10]

        data["positions"][engine_label] = {
            "total_tracked": len(rows),
            "ranked": len(ranked),
            "top10_count": len(top10),
            "top20_count": len(top20),
            "top10_keywords": [(r["keyword"], r["position"]) for r in sorted(top10, key=lambda x: x["position"])[:8]],
            "top20_other": [
                (r["keyword"], r["position"])
                for r in sorted(top20, key=lambda x: x["position"])[len(top10) : len(top10) + 5]
            ],
            "gainers": gainers[:5],
            "losers": losers[:5],
            "high_value_gaps": high_value_gaps,
        }

    # GEO citations
    rates = store.citation_rate(site_id, days=7)
    mentions = store.competitor_mentions(site_id, days=7)
    samples = store.sample_answers(site_id, days=7, n=3)
    data["geo"] = {
        "rate_per_engine": rates,
        "avg_rate": round(sum(rates.values()) / len(rates), 1) if rates else 0.0,
        "competitor_mentions": dict(list(mentions.items())[:8]),
        "sample_answers": [
            {
                "engine": s["engine"],
                "question": s["question"][:120],
                "cited": bool(s["cited"]),
                "answer_excerpt": (s["answer"] or "")[:400],
                "competitors_in_answer": (s["competitors_cited"] or "[]"),
            }
            for s in samples
        ],
    }

    # GSC traffic
    totals = store.gsc_totals(site_id, days=28)
    top_q = store.gsc_top_queries(site_id, days=28, limit=15)
    data["gsc"] = {
        "totals_28d": totals,
        "top_queries": [
            {
                "q": q["query"][:100],
                "impr": q["impressions"],
                "clicks": q["clicks"],
                "pos": q["position"],
                "ctr": q["ctr"],
            }
            for q in top_q
        ],
    }

    return data


def _format_data_for_llm(per_site: list[dict]) -> str:
    """Render per-site data dicts into a prompt-friendly block."""
    blocks: list[str] = []
    for d in per_site:
        b: list[str] = []
        b.append(f"### {d['url']} — {d['description']}")
        # positions
        for engine_label, engine_data in d["positions"].items():
            b.append(f"  {engine_label.upper()} positions:")
            b.append(
                f"    tracked={engine_data['total_tracked']},"
                f" ranked={engine_data['ranked']},"
                f" top10={engine_data['top10_count']},"
                f" top20={engine_data['top20_count']}"
            )
            if engine_data["top10_keywords"]:
                tops = ", ".join(f"{kw}#{pos}" for kw, pos in engine_data["top10_keywords"])
                b.append(f"    top10 keywords: {tops}")
            if engine_data["top20_other"]:
                others = ", ".join(f"{kw}#{pos}" for kw, pos in engine_data["top20_other"])
                b.append(f"    top11-20: {others}")
            if engine_data["gainers"]:
                g = ", ".join(f"{kw} #{p}({d:+d})" for kw, p, d, _t in engine_data["gainers"])
                b.append(f"    🟢 movers up: {g}")
            if engine_data["losers"]:
                lo = ", ".join(f"{kw} #{p}({d:+d})" for kw, p, d, _t in engine_data["losers"])
                b.append(f"    🔴 movers down: {lo}")
            if engine_data["high_value_gaps"]:
                gaps = ", ".join(engine_data["high_value_gaps"][:6])
                b.append(f"    ⚠️ Tier-A NOT ranking: {gaps}")
        # GEO
        geo = d["geo"]
        b.append("  GEO citations (7d):")
        b.append(f"    our citation rate per engine: {geo['rate_per_engine']}")
        b.append(f"    our average citation rate: {geo['avg_rate']}%")
        if geo["competitor_mentions"]:
            mentions_str = ", ".join(f"{name}={cnt}" for name, cnt in geo["competitor_mentions"].items())
            b.append(f"    competitor mentions count across LLM answers: {mentions_str}")
        for sample in geo["sample_answers"]:
            cited_mark = "✅ we cited" if sample["cited"] else "❌ we NOT cited"
            b.append(
                f"    [{sample['engine']}] Q: {sample['question']!r}"
                f"\n      {cited_mark}; competitors in answer: {sample['competitors_in_answer']}"
                f"\n      excerpt: {sample['answer_excerpt']!r}"
            )
        # GSC
        gsc = d["gsc"]
        t = gsc["totals_28d"]
        b.append(
            f"  GSC 28d: clicks={t.get('clicks', 0)},"
            f" impressions={t.get('impressions', 0)},"
            f" avg_position={t.get('avg_position', 0)}"
        )
        if gsc["top_queries"]:
            b.append("  GSC top queries (real impressions, NOT our keyword list):")
            for q in gsc["top_queries"][:10]:
                b.append(
                    f"    - {q['q'][:80]!r}: {q['impr']} impr, {q['clicks']} clicks, pos {q['pos']}, CTR {q['ctr']}"
                )
        blocks.append("\n".join(b))
    return "\n\n".join(blocks)


# ── LLM prompt ─────────────────────────────────────────────────────────


WEEKLY_PROMPT = """You are a strategic SEO/GEO analyst for two sites.

# Data context (live snapshot from kronos.seo_geo.store)

{data_block}

# Strict instructions

- Output: **Russian**, Telegram HTML-compatible (use **bold** markdown — sender converts to <b>).
- NEVER use ###/## headings. Group with emoji.
- Length: ≤ 4500 characters.
- Be **specific**: name keywords, name competitor brands, quote positions.
- **Anti-hallucination**: if a section has no real data, say so («данных нет, первый запуск»).
  Do NOT invent moves, positions, or trends to fill space.
- "GEO citation rate" = % of LLM answers where our brand appeared.
  competitor_mentions counter = how many times each competitor was mentioned by LLMs.
  This is our key competitive landscape signal.

# Required output structure

1. 📊 **EXECUTIVE SUMMARY** (3-5 bullets, the most strategic observations)
   - State the headline: are we visible? to whom? where?
   - One number per bullet (e.g. "GEO: упоминаются Wanderlog в 4/5 ответов, мы в 0/5").

2. 📈 **SEO POSITIONS per site**:
   - **journeybay.co**: где мы в top-10 / top-20, биггест gaps среди Tier-A, WoW movers.
   - **futurecraft.pro**: то же.
   - Compare Google vs Yandex if both have data.

3. 🤖 **GEO LANDSCAPE** (the most important section):
   - Who **dominates** LLM answers per site (concrete competitor names + counts).
   - Where we **are** cited (which engine, which question type).
   - Where we are **absent** (which competitors got the slot we wanted).
   - Quote ONE concrete answer excerpt that illustrates the gap.

4. 🔍 **REAL SEARCH DEMAND (GSC)**:
   - Top 3-5 queries that ACTUALLY bring impressions (not our hypothetical keyword list).
   - Mismatch alert: keywords with high impressions but no clicks → snippet needs fix.
   - Mismatch alert: keywords we track but DON'T appear in GSC at all → wrong target audience?

5. ⚡ **ACTION ITEMS** (concrete, prioritised, actionable):
   - 🔴 **Urgent this week** — 2-3 items max, must be doable in 7 days.
   - 🟡 **Important next sprint** — 2-3 items, structural improvements.
   - 🟢 **Backlog** — 1-2 items, strategic content/positioning bets.

Each action must reference a specific data point (e.g. «написать lp под query X — pos 38, 17 impr, 0 clicks»).

If positions/GEO data is empty (first weekly run), focus the report on GSC + recommendations for what to monitor going forward."""


# ── Public API ─────────────────────────────────────────────────────────


def format_weekly_report() -> str:
    """LLM-synthesised SEO/GEO weekly digest with Action Items."""
    store = get_store()
    per_site = [_gather_site_data(site_id, store) for site_id in SITE_BY_ID]
    data_block = _format_data_for_llm(per_site)

    # Quick sanity: if every site has zero data, return a stub message
    any_data = any(d["positions"] or d["geo"]["rate_per_engine"] or d["gsc"]["top_queries"] for d in per_site)
    if not any_data:
        return (
            "<b>📊 SEO / GEO weekly</b>\n\n"
            "Данных ещё нет — это первый запуск трекера. "
            "Следующий полный прогон в воскресенье 03:00 UTC."
        )

    prompt = WEEKLY_PROMPT.format(data_block=data_block)

    try:
        model = get_model(ModelTier.STANDARD)
        response = model.invoke([HumanMessage(content=prompt)])
        narrative = response.content if isinstance(response.content, str) else str(response.content)
    except Exception as e:
        log.error("SEO/GEO LLM synthesis failed: %s", e)
        # Fallback to mechanical formatter
        return _mechanical_report(per_site)

    # Append footer with raw data snapshot for trail / verification
    footer = _build_footer(per_site)
    return f"<b>📊 SEO / GEO weekly</b>\n\n{narrative.strip()}\n\n{footer}"


def _mechanical_report(per_site: list[dict]) -> str:
    """Plain factual fallback used when LLM call fails."""
    lines: list[str] = ["<b>📊 SEO / GEO weekly</b> (mechanical fallback)\n"]
    for d in per_site:
        lines.append(f"<b>📈 {d['url']}</b>")
        for eng_label, ed in d["positions"].items():
            lines.append(
                f"  {eng_label}: <b>{ed['ranked']}</b>/{ed['total_tracked']} ranked,"
                f" top10=<b>{ed['top10_count']}</b>, top20=<b>{ed['top20_count']}</b>"
            )
        if d["geo"]["rate_per_engine"]:
            lines.append(f"  GEO citation: {d['geo']['rate_per_engine']}")
        if d["gsc"]["totals_28d"].get("impressions"):
            t = d["gsc"]["totals_28d"]
            lines.append(f"  GSC 28d: <b>{t['clicks']}</b> clicks / <b>{t['impressions']}</b> impressions")
        lines.append("")
    return "\n".join(lines).strip()


def _build_footer(per_site: list[dict]) -> str:
    """Compact factual footer attached after LLM narrative for traceability."""
    parts: list[str] = ["<b>— raw snapshot —</b>"]
    for d in per_site:
        bits: list[str] = [d["site_id"]]
        for engine_label, engine_data in d["positions"].items():
            bits.append(
                f"{engine_label} top10/top20/total: "
                f"{engine_data['top10_count']}/{engine_data['top20_count']}/{engine_data['total_tracked']}"
            )
        if d["geo"]["rate_per_engine"]:
            avg = d["geo"]["avg_rate"]
            bits.append(f"GEO citation avg {avg}%")
        t = d["gsc"]["totals_28d"]
        if t.get("impressions"):
            bits.append(f"GSC {t['clicks']}c/{t['impressions']}i")
        parts.append("  • " + " · ".join(bits))
    return "\n".join(parts)


def format_pulse_summary() -> str:
    """Compact one-block summary for the daily pulse aggregator."""
    store = get_store()
    parts: list[str] = []
    for site_id, site in SITE_BY_ID.items():
        google_rows = store.latest_positions(site_id, "google_com")
        ranked = [r for r in google_rows if r["position"] is not None]
        top10 = sum(1 for r in ranked if r["position"] <= 10)
        rates = store.citation_rate(site_id, days=7)
        avg_rate = round(sum(rates.values()) / len(rates), 1) if rates else 0.0
        gsc_totals = store.gsc_totals(site_id, days=28)
        parts.append(
            f"{site.id}: top10={top10}, GEO citation rate={avg_rate}%, GSC 28d clicks={gsc_totals.get('clicks', 0)}"
        )
    return " | ".join(parts)
