"""Owner-only plan approvals for both Telegram bots and userbot accounts."""

import json
import logging

from kronos import plans
from kronos.config import settings
from kronos.security.output_validator import validate_output

log = logging.getLogger("kronos.bridge.plan_approval")


async def deliver_plan_approval(turn_id: str, approval_id: str) -> bool:
    """Deliver a durable approval to its plan owner; acknowledge actual send only.

    Telegram user accounts cannot send inline keyboards, so plain-text commands
    are always included. Bot accounts additionally get the existing buttons.
    No destination or tool arguments are accepted from a webhook caller.
    """
    from kronos import bridge

    agent = bridge.get_agent()
    if agent is None or bridge._client is None:
        return False
    plan = plans.plan_for_turn(turn_id, settings.agent_name)
    if not plan or not plan["chat_id"]:
        return False
    pending = await agent.get_pending_tool_approval(approval_id)
    outcome = await agent.get_turn_outcome(turn_id)
    if (
        not pending
        or pending["turn_id"] != turn_id
        or pending["thread_id"] != f"plan:{plan['id']}"
        or outcome.status != "waiting_approval"
        or outcome.approval_id != approval_id
    ):
        return False
    arguments = json.dumps(pending.get("args", {}), ensure_ascii=False, sort_keys=True)
    # Do not invite approval of an operation whose arguments cannot be shown.
    full_arguments = len(arguments) <= 2500
    if not full_arguments:
        arguments = "Аргументы слишком длинные: проверь approval в CLI перед подтверждением."
    text = f"План #{plan['id']} ожидает подтверждения.\nTool: {pending['tool_name']}\nАргументы: {arguments}\n\n"
    if full_arguments:
        text += f"Подтвердить: /approve {approval_id}\n"
    text += f"Отклонить: /reject {approval_id}"
    text = validate_output(text).redacted_text
    kwargs = {"parse_mode": None}
    if plan.get("topic_id"):
        kwargs["reply_to"] = plan["topic_id"]
    if settings.tg_bot_token:
        buttons = bridge._approval_buttons(approval_id)
        kwargs["buttons"] = buttons if full_arguments else [[buttons[0][1]]]
    try:
        await bridge._rate_limit_wait(plan["chat_id"])
        sent = await bridge._client.send_message(plan["chat_id"], text, **kwargs)
        return sent is not None
    except Exception:
        log.warning("Plan approval delivery failed for plan #%s", plan["id"])
        return False


async def handle_plan_approval_command(event) -> bool:
    """Consume /approve and /reject without sending them to a model.

    Only allowlisted owners in the stored destination can act. All agents see
    group messages, so an unknown approval is ignored rather than answered by
    five unrelated agents. A repeated decision is already guarded by the store.
    """
    from kronos import bridge

    parts = str(event.raw_text or "").strip().split()
    if not parts or parts[0] not in {"/approve", "/reject"}:
        return False
    if len(parts) != 2 or int(event.sender_id or 0) not in settings.allowed_user_ids:
        return True
    agent = bridge.get_agent()
    if agent is None:
        return True
    pending = await agent.get_pending_tool_approval(parts[1])
    if not pending or not str(pending.get("thread_id", "")).startswith("plan:"):
        return True
    plan = plans.plan_for_turn(pending["turn_id"], settings.agent_name)
    if not plan or not bridge._same_telegram_chat(event.chat_id, plan["chat_id"]):
        return True
    topic = bridge._extract_topic_id(event) if not event.is_private else None
    if (topic or None) != (plan.get("topic_id") or None):
        return True
    async with bridge._thread_lock(pending["thread_id"]), bridge._agent_semaphore:
        reply = await agent.resolve_tool_approval(
            parts[1], approved=parts[0] == "/approve", decided_by=str(event.sender_id)
        )
    await event.respond(validate_output(reply).redacted_text, parse_mode=None)
    # The poller observes the durable continuation and sends any further gate.
    return True
