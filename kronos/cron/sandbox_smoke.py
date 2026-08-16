"""Daily proof that the sandbox both runs code and still contains it.

`sandbox_ready()` asks whether Docker and the image exist. Both of this
subsystem's real bugs passed that question and failed everything after it — a
temp directory at 0700 that the container could not read, and a bind mount
passed relatively, which Docker takes for a named volume. In each case the
sandbox reported itself ready and every single run failed, and neither the
suite nor startup noticed. Only running code sees this, so that is what the
probe does.

The second half matters more than the first. A sandbox that refuses to start is
a capability nobody has; a sandbox that runs without containing is a capability
everybody has, including whatever wrote the code. So the probe also asks the
container what it can see and touch — no network, a read-only root, not root —
and turns three documented guarantees into three checked ones.

When it speaks and how is `kronos.health`'s decision, shared with the
acquisition check so both answer to one set of rules.
"""

import logging
from pathlib import Path

from kronos.config import settings
from kronos.health import STATUS_BROKEN, report_changes
from kronos.tools.sandbox import CONTAINMENT_CHECKS, check_sandbox_health

log = logging.getLogger("kronos.cron.sandbox_smoke")

# Losing the ability to run code and losing the walls around it are opposite
# problems, and one message cannot describe both. Saying "no safety was lost"
# about a container that just gained a network would be exactly backwards.
LOST_EXECUTION = (
    "run_code and dynamic tools now refuse rather than falling back — there is no unsandboxed path, "
    "so this is lost capability, not lost safety."
)
LOST_CONTAINMENT = (
    "Code is still running, but with one of its walls down. Until this is fixed, treat run_code and "
    "dynamic tools as unconfined: stop them with ENABLE_CODE_EXECUTION=false."
)

# Docker and the image belong to the host, not the agent: six agents on one
# machine share one daemon and one image, so six probes would be ten wasted
# container runs and five duplicate alerts about the same fault.
OWNER_AGENT = "kronos"


def _state_file() -> Path:
    return Path(settings.db_path).parent / "sandbox_health.json"


async def run_sandbox_smoke() -> None:
    """Probe the sandbox, and report only what changed since yesterday."""
    if settings.agent_name != OWNER_AGENT:
        return

    checks = await check_sandbox_health()
    breached = any(c.name in CONTAINMENT_CHECKS and c.status == STATUS_BROKEN for c in checks)

    report_changes(
        subject="Sandbox",
        checks=checks,
        state_path=_state_file(),
        title="Kronos Agent OS — sandbox",
        consequence=LOST_CONTAINMENT if breached else LOST_EXECUTION,
    )
