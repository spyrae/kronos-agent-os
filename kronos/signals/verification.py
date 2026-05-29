"""Dry-run and rollout verification helpers for Signal Intelligence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kronos.signals.fetchers.runner import Fetcher
from kronos.signals.pipeline import SignalDigestRun, run_signal_digest
from kronos.signals.store import SignalStore


@dataclass(frozen=True)
class SignalDryRunArtifact:
    """Debug artifact for manual Signal Intelligence rollout checks."""

    category: str
    digest_id: int
    saved_item_count: int
    cluster_count: int
    source_counts: dict[str, int]
    source_errors: dict[str, int]
    evidence_counts: dict[str, int]
    rendered_title: str
    rendered_body: str

    def to_json(self) -> str:
        """Serialize the artifact as stable pretty JSON."""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, indent=2)

    def to_markdown(self) -> str:
        """Render a compact human-readable dry-run summary."""
        source_lines = [
            f"- {source_id}: {count} items"
            + (f", {self.source_errors.get(source_id, 0)} errors" if self.source_errors.get(source_id, 0) else "")
            for source_id, count in sorted(self.source_counts.items())
        ]
        evidence_lines = [
            f"- {level}: {count}"
            for level, count in sorted(self.evidence_counts.items())
        ] or ["- none"]
        return "\n".join(
            [
                f"# Signal dry-run: {self.category}",
                "",
                f"- digest_id: {self.digest_id}",
                f"- saved_items: {self.saved_item_count}",
                f"- clusters: {self.cluster_count}",
                "",
                "## Source counts",
                *source_lines,
                "",
                "## Evidence levels",
                *evidence_lines,
                "",
                "## Rendered digest",
                self.rendered_body,
            ]
        )


async def run_signal_dry_run(
    category: str,
    *,
    sources_path: str | Path | None = None,
    source_limit: int | None = None,
    fetch_limit: int = 8,
    output_path: str | Path | None = None,
    output_format: str = "json",
    store: SignalStore | None = None,
    fetchers: dict[str, Fetcher] | None = None,
    polish: bool = False,
) -> SignalDryRunArtifact:
    """Run a no-send digest pass and optionally write a debug artifact."""
    run = await run_signal_digest(
        category,
        sources_path=sources_path,
        dry_run=True,
        send=False,
        source_limit=source_limit,
        fetch_limit=fetch_limit,
        store=store,
        fetchers=fetchers,
        polish=polish,
    )
    artifact = build_dry_run_artifact(run)
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "md":
            path.write_text(artifact.to_markdown(), encoding="utf-8")
        else:
            path.write_text(artifact.to_json() + "\n", encoding="utf-8")
    return artifact


def build_dry_run_artifact(run: SignalDigestRun) -> SignalDryRunArtifact:
    """Convert a digest run result into an inspection artifact."""
    return SignalDryRunArtifact(
        category=run.category,
        digest_id=run.digest_id,
        saved_item_count=run.saved_item_count,
        cluster_count=run.cluster_count,
        source_counts={result.source.id: len(result.items) for result in run.fetch_results},
        source_errors={result.source.id: len(result.errors) for result in run.fetch_results if result.errors},
        evidence_counts={key: value for key, value in run.evidence_counts.items() if key},
        rendered_title=run.rendered.title,
        rendered_body=run.rendered.body,
    )


def artifact_payload(artifact: SignalDryRunArtifact) -> dict[str, Any]:
    """Return a plain dict for tests and CLI output."""
    return asdict(artifact)
