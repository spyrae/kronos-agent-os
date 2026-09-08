"""Validate tool-call/result pairing without inventing execution evidence."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from kronos.effect_state import DurableStateError


def trim_completed_history(messages: list[BaseMessage], limit: int) -> list[BaseMessage]:
    """Keep a bounded history without the orphan result tail of a trimmed batch.

    Only the discarded prefix is adjusted. An active execution journal must
    never be passed here: its missing calls/results are execution evidence.
    """
    start = max(0, len(messages) - limit)
    while start < len(messages) and isinstance(messages[start], ToolMessage):
        start += 1
    return messages[start:]


def unanswered_tool_calls(messages: list[BaseMessage]) -> list[dict]:
    """Return only an incomplete final batch; reject ambiguous or broken history.

    Each batch needs one result per call before another message is sent. Results
    may arrive out of order. Reusing an id for different arguments in the same
    user turn is unsafe because the durable tool cache is keyed by call id.
    """
    pending: dict[str, dict] = {}
    identities: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id
            if call_id not in pending:
                raise DurableStateError("tool history contains an orphan or duplicate result")
            del pending[call_id]
            continue
        if pending:
            raise DurableStateError("tool history contains an unfinished non-final batch")
        if isinstance(message, HumanMessage):
            identities.clear()
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or not isinstance(call.get("name"), str)
                or not call["name"]
                or not isinstance(call.get("args"), dict)
            ):
                raise DurableStateError("tool history contains an invalid tool call")
            call_id = call["id"]
            if call_id in pending:
                raise DurableStateError("tool history contains duplicate call ids in a batch")
            try:
                identity = json.dumps([call["name"], call["args"]], sort_keys=True, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise DurableStateError("tool history contains non-JSON arguments") from error
            if call_id in identities and identities[call_id] != identity:
                raise DurableStateError("tool call id was reused with different arguments")
            identities[call_id] = identity
            pending[call_id] = call
    return list(pending.values())
