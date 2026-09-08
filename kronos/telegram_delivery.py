"""Send frozen outbox chunks through the already authenticated Telegram client."""

from telethon import errors
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import InputReplyToMessage, UpdateMessageID, UpdateShortSentMessage

from kronos.delivery import DeliveryChunk, DeliveryRetryError, DeliveryUncertainError


def ready_sender() -> int:
    """Return the authenticated sender, or zero while transport is unavailable."""
    from kronos import bridge

    if bridge._client is None or not bridge._my_id or not bridge._client.is_connected():
        return 0
    return bridge._my_id


async def send_chunk(chunk: DeliveryChunk) -> int:
    """Return a correlated server message ID, never infer receipt from no error.

    MTProto's random_id is stable across retries; no Bot API fallback may create
    a new identity after an ambiguous send. Paid-message flags stay disabled.
    """
    from kronos import bridge

    client = bridge._client
    if not client or ready_sender() != chunk.sender_id:
        raise DeliveryRetryError()
    try:
        peer = await client.get_input_entity(chunk.chat_id)
        await bridge._rate_limit_wait(chunk.chat_id)
        if client is not bridge._client or ready_sender() != chunk.sender_id:
            raise DeliveryRetryError()
        result = await client(
            SendMessageRequest(
                peer=peer,
                message=chunk.text,
                random_id=chunk.random_id,
                no_webpage=True,
                reply_to=InputReplyToMessage(chunk.topic_id, top_msg_id=chunk.topic_id) if chunk.topic_id else None,
            )
        )
    except errors.FloodWaitError as error:
        raise DeliveryRetryError(delay=error.seconds) from error
    except errors.RandomIdDuplicateError as error:
        # Do not replace the random_id to get past this error or invent an ID.
        raise DeliveryUncertainError("duplicate id without a correlated receipt") from error
    if isinstance(result, UpdateShortSentMessage):
        return result.id
    for update in getattr(result, "updates", ()):
        if isinstance(update, UpdateMessageID) and update.random_id == chunk.random_id:
            return update.id
    raise DeliveryRetryError()
