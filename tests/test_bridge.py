from types import SimpleNamespace

from kronos import bridge


def _event(*, private: bool, reply_to=None, action=None):
    return SimpleNamespace(
        is_private=private,
        message=SimpleNamespace(reply_to=reply_to, action=action),
    )


def test_extract_topic_id_ignores_private_topic_headers():
    reply_to = SimpleNamespace(
        reply_to_top_id=895151,
        forum_topic=True,
        reply_to_msg_id=724,
    )

    assert bridge._extract_topic_id(_event(private=True, reply_to=reply_to)) is None


def test_extract_topic_id_keeps_group_forum_topics():
    reply_to = SimpleNamespace(
        reply_to_top_id=895151,
        forum_topic=True,
        reply_to_msg_id=724,
    )

    assert bridge._extract_topic_id(_event(private=False, reply_to=reply_to)) == 895151


def test_service_message_detected():
    assert bridge._is_service_message(_event(private=False, action=object())) is True
    assert bridge._is_service_message(_event(private=False)) is False


def test_runtime_info_query_reports_configured_codex_model(monkeypatch):
    monkeypatch.setattr(bridge.settings, "kaos_orchestrator_provider_chain", "codex-cli")
    monkeypatch.setattr(bridge.settings, "kaos_codex_model", "gpt-5.5")
    monkeypatch.setattr(bridge.settings, "kaos_standard_provider_chain", "kimi,deepseek")
    monkeypatch.setattr(bridge.settings, "kaos_lite_provider_chain", "deepseek,kimi")

    reply = bridge._handle_runtime_info_query("что у тебя за модель?")

    assert reply is not None
    assert "`codex-cli`" in reply
    assert "`gpt-5.5`" in reply
    assert "standard=kimi,deepseek" in reply
