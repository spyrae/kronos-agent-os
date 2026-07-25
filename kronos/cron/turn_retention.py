"""Turn-history retention — prune finished durable turns weekly.

The journal is what makes resume, capture and forking possible, so it grows with
every turn. Only *finished* turns are pruned: a running or resuming turn is live
state, and an unfinished turn older than the window is a bug worth looking at,
not garbage to sweep.

Window comes from the governance policy (``retention.turn_journal_days``).
"""

import logging

from kronos.config import settings
from kronos.policy import get_policy
from kronos.session import SessionStore

log = logging.getLogger("kronos.cron.turn_retention")


async def run_turn_retention() -> None:
    days = get_policy().retention.turn_journal_days
    try:
        store = SessionStore(settings.db_path, agent_name=settings.agent_name)
        pruned = await store.prune_turn_history(older_than_days=days)
    except Exception as e:
        log.error("Turn retention failed: %s", e)
        return

    if pruned.get("turns"):
        log.info(
            "Turn retention: %d turn(s), %d journal row(s), %d tool result(s), %d effect(s) older than %dd",
            pruned["turns"],
            pruned["journal"],
            pruned["tool_results"],
            pruned["effects"],
            days,
        )
