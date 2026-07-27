"""SLA escalation for owned topics (moat 11.2).

Ownership without escalation is worse than no ownership: it teaches the other
agents to stay quiet and then relies on one process being alive. This poller
closes that loop — when a message on an owned topic has no answer by the owner's
deadline, the topic is handed to the owner's `escalates_to`.

It reuses the hand-off queue rather than inventing a second delivery path, so an
escalated request arrives at the covering agent exactly like a manual hand-off
and comes back through the same webhook.

All six processes run this poller against the same ledger. Correctness comes
from `resolve_sla_watch`, a compare-and-set on the watch row: every process sees
the same due rows, and only the one whose UPDATE changed a row creates the
hand-off.
"""

import logging

from kronos.config import settings
from kronos.swarm_store import get_swarm

log = logging.getLogger("kronos.cron.escalation")

# Safety bound per poll — a backlog drains over several cycles instead of
# monopolising one.
_MAX_PER_POLL = 10


def _escalation_context(watch: dict, target: str) -> str:
    """What the covering agent is asked to do."""
    return (
        f"Тема «{watch['topic']}» закреплена за агентом {watch['owner_agent']}, "
        f"но он не ответил за отведённое время. Ты подстраховка ({target}) — "
        f"ответь по существу.\n\nЗапрос пользователя:\n{watch['request']}"
    )


async def run_sla_escalation() -> None:
    """One poll: escalate every owned topic whose deadline has passed."""
    from kronos.swarm_config import all_profiles

    swarm = get_swarm()
    due = swarm.due_sla_watches(limit=_MAX_PER_POLL)
    if not due:
        return

    profiles = all_profiles()

    for watch in due:
        # The owner may have answered between the deadline and this poll.
        answered = swarm.count_sent_replies(
            chat_id=watch["chat_id"],
            topic_id=watch["topic_id"] or None,
            root_msg_id=watch["root_msg_id"],
        )
        if answered:
            swarm.resolve_sla_watch(watch["id"], state="answered")
            continue

        owner = profiles.get(watch["owner_agent"])
        target = owner.escalates_to if owner else ""
        if not target or target not in profiles:
            # Nobody covers this owner — record it and stop re-checking the row.
            swarm.resolve_sla_watch(watch["id"], state="dropped")
            log.info(
                "SLA missed on '%s' (owner=%s) with no escalation target",
                watch["topic"],
                watch["owner_agent"],
            )
            swarm.incr_metric("escalations_undeliverable")
            continue

        # Cross-process arbitration: exactly one poller proceeds past this line.
        if not swarm.resolve_sla_watch(watch["id"], state="escalated"):
            continue

        swarm.create_handoff(
            chat_id=watch["chat_id"],
            topic_id=watch["topic_id"] or None,
            thread_id=watch["thread_id"],
            from_agent=settings.agent_name,
            to_agent=target,
            context=_escalation_context(watch, target),
        )
        swarm.incr_metric("escalations_triggered")
        log.info(
            "Escalated '%s' from %s to %s after SLA",
            watch["topic"],
            watch["owner_agent"],
            target,
        )
