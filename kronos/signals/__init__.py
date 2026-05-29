"""Signal Intelligence primitives."""

from kronos.signals.clustering import DeduplicationResult, deduplicate_items
from kronos.signals.digest import RenderedDigest, render_digest, save_rendered_digest
from kronos.signals.ideas import idea_signal_score, is_idea_signal
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem, StoreWriteResult
from kronos.signals.pipeline import SignalDigestRun, run_signal_digest
from kronos.signals.quality import SourceQualityAudit, SourceRecommendation, build_source_quality_audit
from kronos.signals.routing import DigestRoute, route_for_category
from kronos.signals.scoring import EvidenceAssessment, EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource, SignalSourceConfigError, SourceRegistry, load_sources
from kronos.signals.store import SignalStore
from kronos.signals.travel import is_travel_insight, travel_insight_score

__all__ = [
    "DeduplicationResult",
    "EvidenceAssessment",
    "EvidenceLevel",
    "DigestRoute",
    "RenderedDigest",
    "SignalCluster",
    "SignalDigest",
    "SignalDigestRun",
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
    "deduplicate_items",
    "idea_signal_score",
    "is_idea_signal",
    "is_travel_insight",
    "load_sources",
    "render_digest",
    "route_for_category",
    "run_signal_digest",
    "sanitize_trend_language",
    "save_rendered_digest",
    "travel_insight_score",
]
