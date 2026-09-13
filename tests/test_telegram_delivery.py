"""Verify the exact MTProto payload without connecting to Telegram."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors
from telethon.tl.types import InputPeerUser, UpdateMessageID, UpdateShortSentMessage

from kronos.delivery import DeliveryChunk, DeliveryRetryError, DeliveryUncertainError
from kronos.telegram_delivery import ready_sender, send_chunk


@pytest.fixture
def client(monkeypatch):
    from kronos import bridge

    obj = AsyncMock()
    obj.is_connected = lambda: True
    obj.get_input_entity.return_value = InputPeerUser(77, 123)
    monkeypatch.setattr(bridge, "_client", obj)
    monkeypatch.setattr(bridge, "_my_id", 55)
    monkeypatch.setattr(bridge, "_rate_limit_wait", AsyncMock())
    return obj


def chunk(topic=42):
    return DeliveryChunk(77, topic, "plain **text**", 9999, 55)


async def test_request_has_frozen_id_destination_topic_and_no_paid_fallback(client):
    client.return_value = SimpleNamespace(updates=[UpdateMessageID(id=23, random_id=9999)])
    assert await send_chunk(chunk()) == 23
    request = client.call_args.args[0]
    assert request.peer.user_id == 77
    assert request.random_id == 9999 and request.message == "plain **text**"
    assert request.reply_to.reply_to_msg_id == request.reply_to.top_msg_id == 42
    assert request.no_webpage is True
    assert request.entities is None and request.allow_paid_stars is None
    assert not request.allow_paid_floodskip


async def test_short_sent_receipt(client):
    client.return_value = UpdateShortSentMessage(id=24, pts=1, pts_count=1, date=datetime.now(UTC))
    assert await send_chunk(chunk(None)) == 24
    assert client.call_args.args[0].reply_to is None


async def test_unrelated_update_is_not_an_acknowledgement(client):
    client.return_value = SimpleNamespace(updates=[UpdateMessageID(id=23, random_id=88)])
    with pytest.raises(DeliveryRetryError):
        await send_chunk(chunk())


async def test_disconnect_or_identity_change_before_dispatch_is_not_sent(client, monkeypatch):
    from kronos import bridge

    async def change():
        bridge._my_id = 66

    async def wait(_):
        await change()

    monkeypatch.setattr(bridge, "_rate_limit_wait", wait)
    with pytest.raises(DeliveryRetryError):
        await send_chunk(chunk())
    client.assert_not_awaited()
    client.is_connected = lambda: False
    assert ready_sender() == 0


@pytest.mark.parametrize("kind", ["flood", "duplicate"])
async def test_provider_delay_and_duplicate_do_not_create_new_message_id(client, kind):
    client.side_effect = (
        errors.FloodWaitError(request=None, capture=80)
        if kind == "flood"
        else errors.RandomIdDuplicateError(request=None)
    )
    with pytest.raises(DeliveryRetryError if kind == "flood" else DeliveryUncertainError) as exc:
        await send_chunk(chunk())
    if kind == "flood":
        assert exc.value.delay == 80
    assert client.call_args.args[0].random_id == 9999
    client.assert_awaited_once()
