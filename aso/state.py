"""ASO Pipeline State — shared across all graph nodes."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import add_messages


class Opportunity(TypedDict, total=False):
    """Single optimization opportunity found by analysis."""

    id: str
    type: str  # keyword_gap | metadata | conversion | localization
    priority: str  # high | medium | low
    platform: str  # ios | android | both
    locale: str
    description: str
    expected_impact: str
    effort: str  # low | medium | high
    data: dict  # detector-specific payload


class ASOState(TypedDict, total=False):
    """Full pipeline state — persisted via LangGraph checkpointer."""

    # --- Identity ---
    app_id_ios: str  # App Store Connect app ID
    package_android: str  # Google Play package name
    cycle_id: str  # UUID for this optimization cycle

    # --- Monitor snapshots ---
    metadata_ios: dict  # {locale: {title, subtitle, keywords, description}}
    metadata_android: dict  # {locale: {title, short_desc, full_desc}}
    analytics: dict  # {impressions, page_views, downloads, conversion, ...}
    keyword_rankings: dict  # {keyword: {position, volume, difficulty}}
    competitor_data: list[dict]  # [{name, app_id, metadata, rankings}]
    reviews_summary: dict  # {avg_rating, total, sentiment, top_issues}

    # --- Analysis ---
    opportunities: list[Opportunity]
    selected_opportunity: Opportunity | None

    # --- Plan (Phase 2) ---
    optimization_plan: dict | None
    human_feedback: str | None

    # --- Execution (Phase 2) ---
    changes_applied: dict | None
    baseline_metrics: dict | None

    # --- Measurement (Phase 3) ---
    post_metrics: dict | None
    evaluation: dict | None

    # --- Meta ---
    phase: str  # monitor | analyze | decide | plan | review | execute | wait | measure | evaluate
    error: str | None
    messages: Annotated[list, add_messages]  # LangGraph message history
