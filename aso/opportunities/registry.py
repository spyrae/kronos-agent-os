"""Registry of active opportunity detectors and executors.

Add new types here as they are implemented.
Phase 1: detectors run inside analyze.py via LLM (not as plugins yet).
Phase 2+: migrate to plugin-based detection.
"""

from __future__ import annotations

from .base import OpportunityDetector, OpportunityExecutor

# Phase 2: activate these as they are implemented
# from .keyword_gap import KeywordGapDetector, KeywordGapExecutor
# from .metadata import MetadataDetector, MetadataExecutor
# from .conversion import ConversionDetector, ConversionExecutor
# from .localization import LocalizationDetector, LocalizationExecutor

DETECTORS: list[OpportunityDetector] = [
    # KeywordGapDetector(),       # Phase 2
    # MetadataDetector(),         # Phase 2
    # ConversionDetector(),       # Phase 2
    # LocalizationDetector(),     # Phase 2
    # ReviewSentimentDetector(),  # Phase 3
    # ScreenshotDetector(),       # Phase 4
]

EXECUTORS: dict[str, OpportunityExecutor] = {
    # "keyword_gap": KeywordGapExecutor(),       # Phase 2
    # "metadata_weakness": MetadataExecutor(),   # Phase 2
    # "conversion_potential": ConversionExecutor(), # Phase 2
    # "localization_gap": LocalizationExecutor(),  # Phase 2
}
