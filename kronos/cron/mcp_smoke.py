"""Daily proof that the MCP servers still hand over their tools.

Two of them were dead for months and nothing said so. The tool manager is
deliberately resilient — a server that will not start is skipped so the rest
keep working — which is the right behaviour and is also why nobody noticed: the
agents came up with 102 tools instead of 113, no error reached anyone, and the
finance agent went on answering market questions from news search alone.

The failure was upstream and unannounced: `mcp>=1.6` with no upper bound, uv
installing SDK 2.0, and an API the servers were written against being gone. That
class of break arrives without a deploy, so a check that only ran at deploy time
would not have caught it either.

When it speaks and how is `kronos.health`'s decision, shared with the
acquisition and sandbox checks so all three answer to one set of rules.
"""

import logging
from pathlib import Path

from kronos.config import settings
from kronos.health import report_changes
from kronos.tools.manager import check_mcp_health

log = logging.getLogger("kronos.cron.mcp_smoke")

# Most servers are per-host — the same binaries, the same uv cache — and the
# per-agent ones (google-workspace is scoped to a single agent by design) are
# meant to be absent elsewhere. So one agent probes, and its `off` entries are
# read as that agent's configuration rather than as the swarm's.
OWNER_AGENT = "kronos"


def _state_file() -> Path:
    return Path(settings.db_path).parent / "mcp_health.json"


async def run_mcp_smoke() -> None:
    """Probe every known server, and report only what changed since yesterday."""
    if settings.agent_name != OWNER_AGENT:
        return

    report_changes(
        subject="MCP servers",
        checks=await check_mcp_health(),
        state_path=_state_file(),
        title="Kronos Agent OS — MCP",
        consequence=(
            "The tools that server provides are simply absent from the agent — nothing errors, "
            "answers just get built without them. Check `kaos mcp check` for the reason."
        ),
    )
