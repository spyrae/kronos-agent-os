from datetime import UTC, datetime
from types import SimpleNamespace

from kronos import bridge


class StopBridgeError(Exception):
    pass


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.handlers = {}
        self.sent = []
        self.read_acks = []

    def on(self, event_builder):
        def decorator(func):
            self.handlers[func.__name__] = func
            return func

        return decorator

    async def start(self, *args, **kwargs):
        return None

    async def get_me(self):
        return SimpleNamespace(id=999, username="kronosbot", first_name="Kronos")

    async def run_until_disconnected(self):
        raise StopBridgeError

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(id=700 + len(self.sent))

    async def send_read_acknowledge(self, chat_id, message):
        self.read_acks.append((chat_id, message.id))


class FakeMessage:
    def __init__(self, *, message_id=100, text="", media=None):
        self.id = message_id
        self.media = media
        self.photo = None
        self.reply_to = None
        self.action = None
        self.entities = []
        self.date = datetime(2026, 6, 19, 8, 0, tzinfo=UTC)
        self.text = text

    async def download_media(self, file):
        with open(file, "wb") as fh:
            fh.write(b"voice")


class FakeEvent:
    def __init__(self, *, text, private=True, message=None, chat_id=42, user_id=7):
        self.chat_id = chat_id
        self.is_private = private
        self.raw_text = text
        self.message = message or FakeMessage(text=text)
        self.is_reply = False
        self._sender = SimpleNamespace(id=user_id, first_name="Alice", username="alice")

    async def get_sender(self):
        return self._sender


async def _noop(*args, **kwargs):
    return None


async def _registered_message_handler(monkeypatch):
    fake_client = FakeClient()

    monkeypatch.setattr(bridge, "TelegramClient", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr(bridge, "_start_webhook_server", _noop)
    monkeypatch.setattr(bridge, "_rate_limit_wait", _noop)
    monkeypatch.setattr(bridge, "get_swarm", lambda: None)
    monkeypatch.setattr(bridge, "should_synthesize", lambda *args, **kwargs: False)
    monkeypatch.setattr(bridge.settings, "tg_bot_token", "")
    monkeypatch.setattr(
        type(bridge.settings),
        "is_telegram_user_allowed",
        lambda self, user_id: True,
    )

    try:
        await bridge.run_bridge(SimpleNamespace())
    except StopBridgeError:
        pass

    return fake_client, fake_client.handlers["handle_message"]


async def test_capture_dm_does_not_call_agent_and_sends_safe_confirmation(monkeypatch):
    fake_client, handle_message = await _registered_message_handler(monkeypatch)
    agent_calls = []
    record_calls = []

    async def fake_ask_agent(*args, **kwargs):
        agent_calls.append((args, kwargs))
        return "agent reply"

    def fake_record_capture(*args, **kwargs):
        record_calls.append((args, kwargs))
        return {"task_id": "knowledge-1-telegram_text_capture-safe"}

    monkeypatch.setattr(bridge, "_ask_agent", fake_ask_agent)
    monkeypatch.setattr(bridge, "record_capture", fake_record_capture)

    await handle_message(FakeEvent(text="note: demo@example.com secret"))

    assert agent_calls == []
    assert len(record_calls) == 1
    assert record_calls[0][0] == ("note: demo@example.com secret",)
    assert record_calls[0][1]["chat_id"] == 42
    assert record_calls[0][1]["user_id"] == 7
    assert record_calls[0][1]["message_id"] == 100
    assert fake_client.sent[0]["text"].startswith("Сохранил в inbox")
    assert "demo@example.com" not in fake_client.sent[0]["text"]


async def test_non_capture_dm_calls_agent_as_before(monkeypatch):
    fake_client, handle_message = await _registered_message_handler(monkeypatch)
    agent_calls = []
    record_calls = []

    async def fake_ask_agent(*args, **kwargs):
        agent_calls.append((args, kwargs))
        return "agent reply"

    class FakeGuardian:
        def check_budget(self, session_id=""):
            return True, ""

        def should_degrade(self):
            return False

    monkeypatch.setattr(bridge, "_ask_agent", fake_ask_agent)
    monkeypatch.setattr(bridge, "record_capture", lambda *args, **kwargs: record_calls.append((args, kwargs)))
    monkeypatch.setattr(bridge, "get_guardian", lambda: FakeGuardian())

    await handle_message(FakeEvent(text="привет, как дела?"))

    assert len(agent_calls) == 1
    assert record_calls == []
    assert fake_client.sent[0]["text"] == "agent reply"


async def test_group_message_never_goes_to_capture(monkeypatch):
    fake_client, handle_message = await _registered_message_handler(monkeypatch)
    record_calls = []

    monkeypatch.setattr(bridge.settings, "telegram_group_responses_enabled", False)
    monkeypatch.setattr(bridge, "record_capture", lambda *args, **kwargs: record_calls.append((args, kwargs)))

    await handle_message(FakeEvent(text="capture: group note", private=False, chat_id=-10042))

    assert record_calls == []
    assert fake_client.sent == []


async def test_voice_capture_uses_transcript_content(monkeypatch):
    fake_client, handle_message = await _registered_message_handler(monkeypatch)
    record_calls = []
    voice_message = FakeMessage(message_id=101, media=object())

    async def fake_transcribe_voice(file_path):
        return "голосовая идея для inbox"

    def fake_record_capture(*args, **kwargs):
        record_calls.append((args, kwargs))
        return {"task_id": "knowledge-voice"}

    monkeypatch.setattr(bridge.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(bridge, "_is_voice_message", lambda event: True)
    monkeypatch.setattr(bridge, "_is_image_message", lambda event: False)
    monkeypatch.setattr(bridge, "_transcribe_voice", fake_transcribe_voice)
    monkeypatch.setattr(bridge, "record_capture", fake_record_capture)

    await handle_message(FakeEvent(text="", message=voice_message))

    assert record_calls[0][0] == ("голосовая идея для inbox",)
    assert record_calls[0][1]["is_voice"] is True
    assert record_calls[0][1]["message_id"] == 101
    assert fake_client.sent[0]["text"].startswith("Сохранил в inbox")
