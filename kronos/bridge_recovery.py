"""Trusted recovery routes and owner-only decisions for queued approval notices."""

from kronos.config import settings
from kronos.effect_state import DurableStateError
from kronos.turn_delivery import RecoveryDestination


def recovery_route(chat_id: int, topic_id: int | None, *, owner_review: bool = False) -> RecoveryDestination | None:
    """Capture the authenticated transport identity, not a model-supplied route."""
    from kronos import bridge
    from kronos.swarm_config import all_profiles

    sender = bridge._my_id
    if type(sender) is not int or sender <= 0:
        return None
    profile = all_profiles().get(settings.agent_name) if owner_review else None
    return RecoveryDestination(
        chat_id=chat_id,
        topic_id=topic_id,
        sender_id=sender,
        review_required=bool(profile and profile.dissent == "require"),
    )


async def has_delivery_duty(agent, pending: dict | None) -> bool:
    """A continuation must not also directly send a queue-owned response."""
    store = getattr(agent, "session_store", None)
    if store is None or not pending or not pending.get("turn_id"):
        return False
    return (await store.delivery_status(pending["turn_id"]))["requested"]


async def recovery_decision_allowed(
    agent, pending: dict, *, sender_id: int, chat_id: int, topic_id: int | None
) -> bool:
    """Require the owner, original chat/topic and original sending account."""
    from kronos import bridge

    if sender_id not in settings.allowed_user_ids:
        return False
    try:
        route = await agent.session_store.recovery_destination(pending["turn_id"])
        if route is not None:
            route.assert_thread(pending["thread_id"])
    except DurableStateError:
        return False
    return bool(
        route
        and route.sender_id == bridge._my_id
        and bridge._same_telegram_chat(chat_id, route.chat_id)
        and (topic_id or None) == route.topic_id
    )


async def handle_recovery_approval_command(event) -> bool:
    """Resolve only this agent's recovery duty; plan commands fall through."""
    from kronos import bridge

    parts = str(event.raw_text or "").strip().split()
    if len(parts) != 2 or parts[0] not in {"/approve", "/reject"}:
        return False
    agent = bridge.get_agent()
    if agent is None:
        return False
    pending = await agent.get_pending_tool_approval(parts[1])
    if not await has_delivery_duty(agent, pending):
        return False
    topic = bridge._extract_topic_id(event) if not event.is_private else None
    if not await recovery_decision_allowed(
        agent,
        pending,
        sender_id=int(event.sender_id or 0),
        chat_id=event.chat_id,
        topic_id=topic,
    ):
        return True
    async with bridge._thread_lock(pending["thread_id"]), bridge._agent_semaphore:
        await agent.resolve_tool_approval(parts[1], approved=parts[0] == "/approve", decided_by=str(event.sender_id))
    # Finalization/new approval writes the queue in the same producer transaction.
    # An event.respond here would duplicate that durable obligation.
    return True
