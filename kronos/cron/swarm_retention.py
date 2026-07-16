"""Swarm ledger + competitor data retention — prune old rows weekly.

Runs weekly on every agent. Two trims:

1. ``swarm_messages`` — common group-chat ledger, keep
   :data:`kronos.swarm_store.MESSAGE_RETENTION_DAYS` (90d). ``reply_claims``
   are tiny by design and need no pruning.
2. ``competitor_snapshots`` / ``competitor_changes`` (only present on the
   Nexus agent in practice but the call is safe everywhere — empty DBs
   just return 0). See ``kronos.competitors.store.SNAPSHOT_RETENTION_DAYS``.

Running on every agent is intentional: each agent owns its own per-agent
DB copy. The first winner on shared tables clears the rows, the others
find nothing to delete; DELETEs under WAL mode are cheap at this scale.
"""

import logging

from kronos.competitors.store import CompetitorStore
from kronos.swarm_store import MESSAGE_RETENTION_DAYS, get_swarm

log = logging.getLogger("kronos.cron.swarm_retention")


async def run_swarm_retention() -> None:
    # Swarm messages (shared ledger across all agents)
    try:
        deleted = get_swarm().prune_old_messages(older_than_days=MESSAGE_RETENTION_DAYS)
        log.info("Swarm retention: pruned %d messages older than %d days", deleted, MESSAGE_RETENTION_DAYS)
    except Exception as e:
        log.error("Swarm retention failed: %s", e)

    # Competitor monitor data (per-agent DB; only Nexus actually accumulates
    # data, others are empty — call is harmless on empty stores).
    try:
        snap_n, change_n = CompetitorStore().prune_old()
        if snap_n or change_n:
            log.info(
                "Competitor retention: pruned %d snapshots, %d changes",
                snap_n,
                change_n,
            )
    except Exception as e:
        log.error("Competitor retention failed: %s", e)
