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

**It only speaks when something changes.** A daily "all three tiers fine" is a
message that trains its reader to skip it, and the one day it says something
else it gets skipped too.
"""

import json
import logging
from pathlib import Path

from kronos.config import settings
from kronos.cron.notify import send_ntfy, send_webhook
from kronos.tools.acquire import TIER_BROKEN, TIER_OK, check_tier_health

log = logging.getLogger("kronos.cron.acquire_smoke")

# The backends are per-host, not per-agent: six agents on one machine share one
# stealth venv and one chromium. Six identical probes would be five wasted
# browser launches and five duplicate alerts about the same fault.
OWNER_AGENT = "kronos"


def _state_file() -> Path:
    # Next to the agent's own DB, matching the scheduler's own state file — not
    # in swarm.db. This is a fact about one host's install, not shared knowledge.
    return Path(settings.db_path).parent / "acquire_health.json"


def _load_previous() -> dict[str, str]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except Exception as e:
        # A corrupt state file must not stop the probe: the check is the point,
        # the memory of it is the optimisation. Worst case is one extra alert.
        log.warning("Could not read %s (%s); treating this as a first run", path.name, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(current: dict[str, str]) -> None:
    path = _state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2))
        tmp.replace(path)
    except Exception as e:
        log.warning("Could not persist acquisition health: %s", e)


def _describe(tier: str, before: str | None, after: str, detail: str) -> str:
    if before is None:
        return f"{tier}: {after} — {detail}"
    return f"{tier}: {before} → {after} — {detail}"


async def run_acquire_smoke() -> None:
    """Probe every tier, and report only what changed since yesterday."""
    if settings.agent_name != OWNER_AGENT:
        return

    results = await check_tier_health()
    current = {r.tier: r.status for r in results}
    previous = _load_previous()

    log.info("Acquisition tiers: %s", ", ".join(f"{r.tier}={r.status}" for r in results))

    notable: list[str] = []
    for result in results:
        before = previous.get(result.tier)
        if before == result.status:
            continue
        # A first run has nothing to compare against, so only a fault is worth
        # saying. Announcing "stealth: off" on a host that never wanted stealth
        # would be the checker's first message and its least useful one.
        if before is None and result.status != TIER_BROKEN:
            continue
        notable.append(_describe(result.tier, before, result.status, result.detail))

    _save(current)

    if not notable:
        return

    # A tier that was working and now is not — whether it errored (`broken`) or
    # vanished from the config (`off`) — is the case this job exists for.
    lost = [r for r in results if previous.get(r.tier) == TIER_OK and r.status != TIER_OK]
    healthy = [r.tier for r in results if r.status == TIER_OK]
    body = (
        "Acquisition tiers changed:\n"
        + "\n".join(f"• {line}" for line in notable)
        + f"\n\nStill working: {', '.join(healthy) or 'none'}"
    )
    if lost:
        body += "\n\nSites that needed that tier will now be reported as unreadable, rather than fetched wrongly."

    send_webhook(body)
    send_ntfy(
        body,
        title="Kronos Agent OS — acquisition",
        priority="high" if lost else "default",
        tags="warning" if lost else "white_check_mark",
    )
