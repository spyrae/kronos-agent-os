"""Swarm ledger retention — prune old group messages.

Runs weekly. Keeps ``swarm_messages`` from growing unboundedly by deleting
rows older than :data:`kronos.swarm_store.MESSAGE_RETENTION_DAYS` (90 days
by default). ``reply_claims`` are kept small by design (one row per
agent×trigger pair) and do not need pruning.

Only one of the six agent processes actually needs to run this job, but
running it from several is safe — the first winner clears the rows, the
others find nothing to delete. We leave the job registered on every
agent for simplicity; the DELETE under WAL is cheap at this scale.
"""

import logging

from kronos.swarm_store import MESSAGE_RETENTION_DAYS, get_swarm

log = logging.getLogger("kronos.cron.swarm_retention")


async def run_swarm_retention() -> None:
    try:
        deleted = get_swarm().prune_old_messages(older_than_days=MESSAGE_RETENTION_DAYS)
        log.info("Swarm retention: pruned %d messages older than %d days",
                 deleted, MESSAGE_RETENTION_DAYS)
    except Exception as e:
        log.error("Swarm retention failed: %s", e)
