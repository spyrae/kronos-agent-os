"""Signal Intelligence primitives."""

from kronos.signals.clustering import DeduplicationResult, deduplicate_items
from kronos.signals.digest import (
    RenderedDigest,
    curate_news_digest,
    render_digest,
    save_rendered_digest,
    synthesize_ideas_digest,
)
from kronos.signals.ideas import idea_signal_score, is_idea_signal
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem, StoreWriteResult
from kronos.signals.news import is_news_signal, news_priority_score, news_signal_score
from kronos.signals.pipeline import SignalDigestRun, run_signal_digest
from kronos.signals.quality import SourceQualityAudit, SourceRecommendation, build_source_quality_audit
from kronos.signals.routing import DigestRoute, route_for_category
from kronos.signals.scoring import EvidenceAssessment, EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource, SignalSourceConfigError, SourceRegistry, load_sources
from kronos.signals.store import SignalStore
from kronos.signals.travel import is_travel_insight, travel_insight_score
from kronos.signals.verification import SignalDryRunArtifact, run_signal_dry_run

__all__ = [
    "DeduplicationResult",
    "EvidenceAssessment",
    "EvidenceLevel",
    "DigestRoute",
    "RenderedDigest",
    "SignalCluster",
    "SignalDigest",
    "SignalDigestRun",
    "SignalDryRunArtifact",
    "SignalItem",
    "SignalSource",
    "SignalSourceConfigError",
    "SignalStore",
    "SourceRegistry",
    "SourceQualityAudit",
    "SourceRecommendation",
    "StoreWriteResult",
    "assess_evidence",
    "build_source_quality_audit",
    "curate_news_digest",
    "deduplicate_items",
    "idea_signal_score",
    "is_idea_signal",
    "is_news_signal",
    "is_travel_insight",
    "load_sources",
    "news_priority_score",
    "news_signal_score",
    "render_digest",
    "route_for_category",
    "run_signal_digest",
    "run_signal_dry_run",
    "sanitize_trend_language",
    "save_rendered_digest",
    "synthesize_ideas_digest",
    "travel_insight_score",
]
