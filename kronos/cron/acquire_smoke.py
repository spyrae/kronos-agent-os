"""Daily proof that the acquisition tiers still work — and a shout when one stops.

Every tier in `kronos.tools.acquire` fails quietly. The stealth backend lives
outside this repo, so a host rebuild removes it without touching a line of code.
The browser tier broke once because Playwright deleted the API it was reading
pages through — and returned its own error message as page content, which reads
like a page that loaded. A plain fetch can start meeting a CDN that was not
there last month.

None of those raise at startup, none fail a test, and each turns into "the agent
can't read that site any more" weeks later, blamed on the site. This job is the
difference between finding out on a Tuesday and finding out from a task that
quietly returned nothing.

When it speaks and how is `kronos.health`'s decision, shared with the sandbox
check so both answer to one set of rules.
"""

import logging
from pathlib import Path

from kronos.config import settings
from kronos.health import report_changes
from kronos.tools.acquire import check_tier_health

log = logging.getLogger("kronos.cron.acquire_smoke")

# The backends are per-host, not per-agent: six agents on one machine share one
# stealth venv and one chromium. Six identical probes would be five wasted
# browser launches and five duplicate alerts about the same fault.
OWNER_AGENT = "kronos"


def _state_file() -> Path:
    # Next to the agent's own DB, matching the scheduler's own state file — not
    # in swarm.db. This is a fact about one host's install, not shared knowledge.
    return Path(settings.db_path).parent / "acquire_health.json"


async def run_acquire_smoke() -> None:
    """Probe every tier, and report only what changed since yesterday."""
    if settings.agent_name != OWNER_AGENT:
        return

    report_changes(
        subject="Acquisition tiers",
        checks=await check_tier_health(),
        state_path=_state_file(),
        title="Kronos Agent OS — acquisition",
        consequence=("Sites that needed that tier will now be reported as unreadable, rather than fetched wrongly."),
    )
