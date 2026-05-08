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


def test_topic_route_matches_supergroup_link_id(monkeypatch):
    monkeypatch.setattr(bridge.settings, "telegram_swarm_chat_id", 3642435967)
    monkeypatch.setattr(bridge.settings, "telegram_general_topic_id", 23)
    monkeypatch.setattr(bridge.settings, "telegram_kronos_topic_id", 18)
    monkeypatch.setattr(bridge.settings, "telegram_finance_topic_id", 22)
    monkeypatch.setattr(bridge.settings, "telegram_digest_topic_id", 24)
    monkeypatch.setattr(bridge.settings, "telegram_kronos_agent", "kronos")
    monkeypatch.setattr(bridge.settings, "telegram_finance_agent", "kronos")
    monkeypatch.setattr(bridge.settings, "telegram_digest_agent", "kronos")

    assert bridge._normalize_telegram_chat_id(-1003642435967) == 3642435967

    general = bridge._resolve_topic_route(-1003642435967, 23)
    kronos = bridge._resolve_topic_route(-1003642435967, 18)
    unknown = bridge._resolve_topic_route(-1003642435967, 99)

    assert general.mode == "swarm"
    assert kronos.mode == "owner"
    assert kronos.owner_agent == "kronos"
    assert unknown.mode == "silent"


def test_topic_route_falls_back_outside_configured_chat(monkeypatch):
    monkeypatch.setattr(bridge.settings, "telegram_swarm_chat_id", 3642435967)
    monkeypatch.setattr(bridge.settings, "telegram_general_topic_id", 23)

    route = bridge._resolve_topic_route(-1009999999999, 23)

    assert route.mode == "default"


def test_shared_group_context_excludes_current_message(monkeypatch):
    monkeypatch.setattr(bridge.settings, "telegram_shared_context_messages", 3)

    class FakeSwarm:
        def get_recent_messages(self, *, chat_id, topic_id, limit):
            return [
                {"msg_id": 12, "sender_type": "user", "agent_name": None, "text": "current"},
                {"msg_id": 11, "sender_type": "agent", "agent_name": "nexus", "text": "agent answer"},
                {"msg_id": 10, "sender_type": "user", "agent_name": None, "text": "root question"},
            ]

    context = bridge._format_shared_group_context(
        FakeSwarm(),
        chat_id=-1003642435967,
        topic_id=23,
        current_msg_id=12,
    )

    assert "current" not in context
    assert "Пользователь: root question" in context
    assert "Агент nexus: agent answer" in context
