"""Base classes for opportunity detection and execution.

Plugin system: each opportunity type implements Detector + Executor.
Register in registry.py to activate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..state import ASOState, Opportunity


class OpportunityDetector(ABC):
    """Detects a specific type of optimization opportunity."""

    type: str  # e.g. "keyword_gap", "metadata_weakness"

    @abstractmethod
    async def detect(self, state: ASOState) -> list[Opportunity]:
        """Analyze state and return found opportunities."""
        ...


class OpportunityExecutor(ABC):
    """Plans and executes changes for a specific opportunity type."""

    type: str

    @abstractmethod
    async def plan(self, opportunity: Opportunity, state: ASOState) -> dict:
        """Generate an optimization plan for the opportunity.

        Returns plan dict with 'changes', 'expected_impact', 'risk', etc.
        """
        ...

    @abstractmethod
    async def execute(self, plan: dict, state: ASOState) -> dict:
        """Apply the planned changes.

        Returns dict with 'changes_applied', 'baseline_metrics', etc.
        """
        ...
