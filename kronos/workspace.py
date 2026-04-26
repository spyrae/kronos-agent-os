"""Three-Space workspace layout.

Architecture (Ars Contexta pattern):
  workspace/
  ├── self/     ← WHO I AM (identity, skills, methodology)
  ├── notes/    ← WHAT I KNOW (user model, memory, world knowledge)
  └── ops/      ← WHAT I DO (sessions, heartbeat, tools, workflow)

All workspace paths are centralized here. Import `ws` and use attributes.
"""

from pathlib import Path

from kronos.config import settings


class Workspace:
    """Central registry for all workspace paths."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

        # === SELF — who I am ===
        self.self_dir = self.root / "self"
        self.identity = self.self_dir / "IDENTITY.md"
        self.soul = self.self_dir / "SOUL.md"
        self.agents = self.self_dir / "AGENTS.md"
        self.methodology = self.self_dir / "methodology.md"
        self.skills_dir = self.self_dir / "skills"

        # === NOTES — what I know ===
        self.notes_dir = self.root / "notes"
        self.user_dir = self.notes_dir / "user"
        self.user = self.user_dir / "USER.md"
        self.memory = self.user_dir / "MEMORY.md"
        self.user_model = self.user_dir / "USER-MODEL.md"
        self.user_patterns = self.user_dir / "USER-PATTERNS.md"
        self.inbox_dir = self.notes_dir / "inbox"
        self.world_dir = self.notes_dir / "world"
        self.contacts_dir = self.world_dir / "contacts"

        # === OPS — what I do ===
        self.ops_dir = self.root / "ops"
        self.heartbeat = self.ops_dir / "HEARTBEAT.md"
        self.tools = self.ops_dir / "TOOLS.md"
        self.workflow = self.ops_dir / "WORKFLOW_AUTO.md"
        self.sessions_dir = self.ops_dir / "sessions"
        self.handoff = self.sessions_dir / "handoff.md"
        self.queue_dir = self.ops_dir / "queue"
        self.dynamic_tools_dir = self.ops_dir / "dynamic_tools"
        self.self_improve_dir = self.ops_dir / "self-improve"

    def skill_path(self, skill_name: str) -> Path:
        """Path to a skill's SKILL.md."""
        return self.skills_dir / skill_name / "SKILL.md"

    def skill_ref(self, skill_name: str, ref_name: str) -> Path:
        """Path to a skill's reference file."""
        return self.skills_dir / skill_name / "references" / f"{ref_name}.md"

    def ensure_dirs(self) -> None:
        """Create all workspace directories (idempotent)."""
        for d in [
            self.self_dir, self.skills_dir,
            self.user_dir, self.inbox_dir, self.world_dir, self.contacts_dir,
            self.ops_dir, self.sessions_dir, self.queue_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)


def _resolve_workspace_root() -> Path:
    """Resolve workspace root from settings.

    Priority: explicit workspace_path > workspaces/<agent_name>/
    """
    if settings.workspace_path:
        return Path(settings.workspace_path)
    # Default: workspaces/<agent_name>/ relative to app/
    app_dir = Path(__file__).resolve().parent.parent
    return app_dir / "workspaces" / settings.agent_name


ws = Workspace(_resolve_workspace_root())
