"""Signal Intelligence primitives."""

from kronos.signals.clustering import DeduplicationResult, deduplicate_items
from kronos.signals.models import SignalCluster, SignalDigest, SignalItem, StoreWriteResult
from kronos.signals.scoring import EvidenceAssessment, EvidenceLevel, assess_evidence, sanitize_trend_language
from kronos.signals.sources import SignalSource, SignalSourceConfigError, SourceRegistry, load_sources
from kronos.signals.store import SignalStore

__all__ = [
    "DeduplicationResult",
    "EvidenceAssessment",
    "EvidenceLevel",
    "SignalCluster",
    "SignalDigest",
    "SignalItem",
    "SignalSource",
    "SignalSourceConfigError",
    "SignalStore",
    "SourceRegistry",
    "StoreWriteResult",
    "assess_evidence",
    "deduplicate_items",
    "load_sources",
    "sanitize_trend_language",
]
