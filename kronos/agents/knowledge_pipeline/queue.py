"""File-backed task queue for the knowledge pipeline.

The queue implements the handoff rule used by the pipeline:
each phase reads a task JSON file from ``ops/queue/`` and writes its result
back to the same file instead of relying on in-memory context.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kronos.security.pii import mask_pii_object
from kronos.workspace import Workspace, ws

SCHEMA_VERSION = 1
TASK_SUFFIX = ".knowledge.json"
PIPELINE_PHASES = ("record", "process", "connect", "verify", "memory")
ACTIVE_STATES = {"recorded", "processed", "connected", "needs_review"}
FINAL_STATES = {"verified", "completed", "failed"}


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def _slug(value: str, *, max_len: int = 42) -> str:
    clean = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "-", value.strip()).strip("-").lower()
    return clean[:max_len].strip("-") or "knowledge"


def _stable_task_id(source_kind: str, content: str) -> str:
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"knowledge-{stamp}-{_slug(source_kind, max_len=18)}-{digest}"


class KnowledgeQueue:
    """Manage knowledge task files in ``ops/queue`` and records in ``notes/inbox``."""

    def __init__(self, workspace: Workspace | None = None):
        self.workspace = workspace or ws
        self.workspace.ensure_dirs()

    def record_source(
        self,
        source_kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an inbox record and a queue task for incoming knowledge."""
        normalized = content.strip()
        if not normalized:
            raise ValueError("knowledge content cannot be empty")

        task_id = _stable_task_id(source_kind, normalized)
        inbox_path = self.workspace.inbox_dir / f"{task_id}.md"
        safe_metadata = mask_pii_object(metadata or {})
        now = utc_now()

        inbox_path.write_text(
            "\n".join(
                [
                    "---",
                    f"task_id: {task_id}",
                    f"source: {source_kind}",
                    f"created_at: {now}",
                    "---",
                    "",
                    normalized,
                    "",
                ]
            ),
            encoding="utf-8",
        )

        task = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "source": {
                "kind": source_kind,
                "metadata": safe_metadata,
            },
            "inbox_path": self._portable_path(inbox_path),
            "state": "recorded",
            "created_at": now,
            "updated_at": now,
            "phases": {
                "record": {
                    "status": "completed",
                    "started_at": now,
                    "completed_at": now,
                    "details": {"bytes": len(normalized.encode("utf-8"))},
                }
            },
            "claims": [],
            "links": [],
            "verification": {},
            "memory": {"status": "not_started"},
        }
        self.save_task(task)
        return task

    def task_path(self, task_id: str) -> Path:
        """Return the queue path for a task id."""
        safe_id = Path(task_id).name
        if safe_id.endswith(TASK_SUFFIX):
            return self.workspace.queue_dir / safe_id
        return self.workspace.queue_dir / f"{safe_id}{TASK_SUFFIX}"

    def save_task(self, task: dict[str, Any]) -> Path:
        """Atomically write a task file."""
        task = dict(task)
        task["updated_at"] = utc_now()
        path = self.task_path(str(task["task_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return path

    def load_task(self, task_id_or_path: str | Path) -> dict[str, Any]:
        """Load and validate a task by id or path."""
        raw_path = Path(task_id_or_path)
        path = raw_path if raw_path.suffix == ".json" or raw_path.name.endswith(TASK_SUFFIX) else self.task_path(str(raw_path))
        if not path.is_absolute():
            path = self.workspace.root / path
            if not path.exists():
                path = self.task_path(str(task_id_or_path))
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_task_schema(data)
        if errors:
            raise ValueError(f"Invalid knowledge task {path}: {', '.join(errors)}")
        return data

    def list_tasks(self, *, include_final: bool = False) -> list[dict[str, Any]]:
        """List queued task files ordered by updated time."""
        tasks: list[dict[str, Any]] = []
        for path in sorted(self.workspace.queue_dir.glob(f"*{TASK_SUFFIX}")):
            try:
                task = self.load_task(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if include_final or task.get("state") not in FINAL_STATES:
                tasks.append(task)
        return sorted(tasks, key=lambda item: str(item.get("updated_at") or ""))

    def read_inbox(self, task: dict[str, Any]) -> str:
        """Read source text from the task's inbox file."""
        path = self.resolve_path(str(task.get("inbox_path") or ""))
        if not path.exists():
            raise FileNotFoundError(f"Knowledge inbox file is missing: {path}")
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                return parts[2].strip()
        return text.strip()

    def mark_phase(
        self,
        task: dict[str, Any],
        phase: str,
        status: str,
        details: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Update phase state on a task dictionary."""
        if phase not in PIPELINE_PHASES:
            raise ValueError(f"unknown knowledge phase: {phase}")
        now = utc_now()
        previous = dict(task.get("phases", {}).get(phase, {}))
        started_at = previous.get("started_at") or now
        phase_data: dict[str, Any] = {
            "status": status,
            "started_at": started_at,
            "completed_at": now if status in {"completed", "failed", "skipped"} else "",
        }
        if details:
            phase_data["details"] = mask_pii_object(details)
        if error:
            phase_data["error"] = error
        task.setdefault("phases", {})[phase] = phase_data
        task["updated_at"] = now
        return task

    def resolve_path(self, value: str) -> Path:
        """Resolve a task-local portable path."""
        path = Path(value)
        if path.is_absolute():
            return path
        return self.workspace.root / path

    def _portable_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace.root))
        except ValueError:
            return str(path)


def validate_task_schema(task: dict[str, Any]) -> list[str]:
    """Validate the minimal task-file schema."""
    errors: list[str] = []
    required = ("schema_version", "task_id", "source", "inbox_path", "state", "created_at", "updated_at")
    for field in required:
        if field not in task:
            errors.append(f"missing {field}")

    if task.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {task.get('schema_version')}")
    if not isinstance(task.get("source"), dict):
        errors.append("source must be an object")
    elif not task["source"].get("kind"):
        errors.append("source.kind is required")
    if not isinstance(task.get("claims", []), list):
        errors.append("claims must be a list")
    if not isinstance(task.get("links", []), list):
        errors.append("links must be a list")
    if not isinstance(task.get("phases", {}), dict):
        errors.append("phases must be an object")
    return errors
