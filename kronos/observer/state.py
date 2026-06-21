"""File-backed state store for the Observer/Capture Engine."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self

from kronos.observer.models import ObserverRunResult, utc_now_iso
from kronos.security.pii import mask_pii_object
from kronos.workspace import Workspace, ws

SCHEMA_VERSION = 1


@dataclass
class ObserverState:
    """Persistent local state for read-only observer scans and digests."""

    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    dialog_cursors: dict[str, str] = field(default_factory=dict)
    last_seen_message_ids: dict[str, int] = field(default_factory=dict)
    ignored_peers: set[str] = field(default_factory=set)
    muted_peers: set[str] = field(default_factory=set)
    ignored_peer_reasons: dict[str, str] = field(default_factory=dict)
    muted_peer_reasons: dict[str, str] = field(default_factory=dict)
    last_scan_at: dict[str, str] = field(default_factory=dict)
    last_digest_at: dict[str, str] = field(default_factory=dict)
    last_reported_debts: dict[str, str] = field(default_factory=dict)
    last_critical_alerts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable payload."""
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dialog_cursors": dict(sorted(self.dialog_cursors.items())),
            "last_seen_message_ids": dict(sorted(self.last_seen_message_ids.items())),
            "ignored_peers": sorted(self.ignored_peers),
            "muted_peers": sorted(self.muted_peers),
            "ignored_peer_reasons": dict(sorted(self.ignored_peer_reasons.items())),
            "muted_peer_reasons": dict(sorted(self.muted_peer_reasons.items())),
            "last_scan_at": dict(sorted(self.last_scan_at.items())),
            "last_digest_at": dict(sorted(self.last_digest_at.items())),
            "last_reported_debts": dict(sorted(self.last_reported_debts.items())),
            "last_critical_alerts": dict(sorted(self.last_critical_alerts.items())),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Load state from a JSON payload with safe defaults for missing fields."""
        schema_version = int(data.get("schema_version", SCHEMA_VERSION))
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported observer state schema_version {schema_version}")

        return cls(
            schema_version=schema_version,
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            dialog_cursors={str(key): str(value) for key, value in dict(data.get("dialog_cursors") or {}).items()},
            last_seen_message_ids={
                str(key): int(value)
                for key, value in dict(data.get("last_seen_message_ids") or {}).items()
                if value is not None
            },
            ignored_peers={str(item) for item in data.get("ignored_peers") or []},
            muted_peers={str(item) for item in data.get("muted_peers") or []},
            ignored_peer_reasons={
                str(key): str(value)
                for key, value in dict(data.get("ignored_peer_reasons") or {}).items()
                if value
            },
            muted_peer_reasons={
                str(key): str(value)
                for key, value in dict(data.get("muted_peer_reasons") or {}).items()
                if value
            },
            last_scan_at={str(key): str(value) for key, value in dict(data.get("last_scan_at") or {}).items()},
            last_digest_at={str(key): str(value) for key, value in dict(data.get("last_digest_at") or {}).items()},
            last_reported_debts={
                str(key): str(value) for key, value in dict(data.get("last_reported_debts") or {}).items()
            },
            last_critical_alerts={
                str(key): str(value) for key, value in dict(data.get("last_critical_alerts") or {}).items()
            },
        )


class ObserverStateStore:
    """Read and write Observer state under ``workspace/ops/observer``."""

    def __init__(self, workspace: Workspace | None = None):
        self.workspace = workspace or ws
        self.observer_dir: Path = getattr(self.workspace, "observer_dir", self.workspace.ops_dir / "observer")
        self.state_path: Path = getattr(self.workspace, "observer_state", self.observer_dir / "state.json")
        self.runs_path: Path = getattr(self.workspace, "observer_runs", self.observer_dir / "runs.jsonl")
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """Create workspace and observer directories idempotently."""
        self.workspace.ensure_dirs()
        self.observer_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> ObserverState:
        """Load state, returning safe defaults for a fresh workspace."""
        self.ensure_dirs()
        if not self.state_path.exists():
            return ObserverState()
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError(f"observer state must be an object: {self.state_path}")
        return ObserverState.from_dict(data)

    def save(self, state: ObserverState) -> ObserverState:
        """Atomically save observer state and return the written snapshot."""
        self.ensure_dirs()
        current = replace(state, updated_at=utc_now_iso())
        payload = current.to_dict()
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_path)
        return current

    def update_dialog(
        self,
        peer_id: str,
        *,
        cursor: str | None = None,
        last_seen_message_id: int | None = None,
    ) -> ObserverState:
        """Persist per-dialog scan cursors and last seen Telegram message ids."""
        state = self.load()
        key = str(peer_id)
        if cursor is not None:
            state.dialog_cursors[key] = str(cursor)
        if last_seen_message_id is not None:
            state.last_seen_message_ids[key] = int(last_seen_message_id)
        return self.save(state)

    def set_ignored(
        self,
        peer_id: str,
        ignored: bool = True,
        *,
        reason: str = "",
    ) -> ObserverState:
        """Mark a peer as ignored or remove it from the ignore list."""
        state = self.load()
        key = str(peer_id)
        if ignored:
            state.ignored_peers.add(key)
            if reason.strip():
                state.ignored_peer_reasons[key] = reason.strip()
        else:
            state.ignored_peers.discard(key)
            state.ignored_peer_reasons.pop(key, None)
        return self.save(state)

    def set_muted(
        self,
        peer_id: str,
        muted: bool = True,
        *,
        reason: str = "",
    ) -> ObserverState:
        """Mark a peer as muted for notifications or remove that mute."""
        state = self.load()
        key = str(peer_id)
        if muted:
            state.muted_peers.add(key)
            if reason.strip():
                state.muted_peer_reasons[key] = reason.strip()
        else:
            state.muted_peers.discard(key)
            state.muted_peer_reasons.pop(key, None)
        return self.save(state)

    def mark_digest(self, digest_name: str, timestamp: str | None = None) -> ObserverState:
        """Record when an observer digest/scope was last generated or sent."""
        state = self.load()
        state.last_digest_at[str(digest_name)] = timestamp or utc_now_iso()
        return self.save(state)

    def mark_scan(self, scan_name: str, timestamp: str | None = None) -> ObserverState:
        """Record when an observer scanner last ran."""
        state = self.load()
        state.last_scan_at[str(scan_name)] = timestamp or utc_now_iso()
        return self.save(state)

    def mark_debt_reported(
        self,
        peer_id: str,
        timestamp: str | None = None,
        *,
        critical: bool = False,
    ) -> ObserverState:
        """Record reply-debt reporting timestamps for dedupe/cooldown logic."""
        state = self.load()
        stamped_at = timestamp or utc_now_iso()
        state.last_reported_debts[str(peer_id)] = stamped_at
        if critical:
            state.last_critical_alerts[str(peer_id)] = stamped_at
        return self.save(state)

    def append_run(self, result: ObserverRunResult | Mapping[str, Any]) -> Path:
        """Append one sanitized observer run record to ``runs.jsonl``."""
        self.ensure_dirs()
        payload = result.to_dict() if isinstance(result, ObserverRunResult) else dict(result)
        payload["schema_version"] = SCHEMA_VERSION
        payload["logged_at"] = utc_now_iso()
        if "metadata" in payload:
            payload["metadata"] = mask_pii_object(payload.get("metadata") or {})
        if "errors" in payload:
            payload["errors"] = mask_pii_object(payload.get("errors") or [])

        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return self.runs_path

    def list_runs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Read observer run records from the append-only JSONL log."""
        if not self.runs_path.exists():
            return []
        runs = [
            json.loads(line)
            for line in self.runs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if limit is None:
            return runs
        return runs[-limit:]
