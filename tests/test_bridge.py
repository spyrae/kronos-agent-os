from types import SimpleNamespace

from kronos import bridge


def _clear_topic_alias_env(monkeypatch):
    for env_name in (
        "TOPIC_GENERAL",
        "TOPIC_DIGEST",
        "TOPIC_FINANCE",
        "TOPIC_DIGEST_NEWS",
        "TOPIC_JB_COMPETITORS",
        "TOPIC_JB_SYSTEM",
        "TOPIC_DIGEST_JOBS",
        "TOPIC_DIGEST_IDEAS",
        "TOPIC_JB_TRAVEL_INSIGHTS",
        "TELEGRAM_KRONOS_TOPIC_ID",
    ):
        monkeypatch.delenv(env_name, raising=False)


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
    _clear_topic_alias_env(monkeypatch)
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


def test_topic_route_uses_signal_topic_env_aliases(monkeypatch):
    _clear_topic_alias_env(monkeypatch)
    monkeypatch.setattr(bridge.settings, "telegram_swarm_chat_id", 3642435967)
    monkeypatch.setattr(bridge.settings, "telegram_general_topic_id", 23)
    monkeypatch.setenv("TOPIC_DIGEST_NEWS", "31")
    monkeypatch.setenv("TOPIC_JB_COMPETITORS", "32")
    monkeypatch.setenv("TOPIC_JB_SYSTEM", "33")
    monkeypatch.setenv("TOPIC_DIGEST_JOBS", "34")
    monkeypatch.setenv("TOPIC_DIGEST_IDEAS", "35")
    monkeypatch.setenv("TOPIC_JB_TRAVEL_INSIGHTS", "36")
    monkeypatch.setattr(bridge.settings, "telegram_digest_news_agent", "kronos")
    monkeypatch.setattr(bridge.settings, "telegram_jb_competitors_agent", "nexus")
    monkeypatch.setattr(bridge.settings, "telegram_jb_system_agent", "nexus")
    monkeypatch.setattr(bridge.settings, "telegram_digest_jobs_agent", "kronos")
    monkeypatch.setattr(bridge.settings, "telegram_digest_ideas_agent", "kronos")
    monkeypatch.setattr(bridge.settings, "telegram_jb_travel_insights_agent", "kronos")

    news = bridge._resolve_topic_route(-1003642435967, 31)
    competitors = bridge._resolve_topic_route(-1003642435967, 32)
    system = bridge._resolve_topic_route(-1003642435967, 33)
    jobs = bridge._resolve_topic_route(-1003642435967, 34)
    ideas = bridge._resolve_topic_route(-1003642435967, 35)
    travel = bridge._resolve_topic_route(-1003642435967, 36)

    assert news == bridge.TopicRoute("owner", label="digest_news", owner_agent="kronos")
    assert competitors == bridge.TopicRoute(
        "owner",
        label="jb_competitors",
        owner_agent="nexus",
    )
    assert system == bridge.TopicRoute("owner", label="jb_system", owner_agent="nexus")
    assert jobs == bridge.TopicRoute("owner", label="digest_jobs", owner_agent="kronos")
    assert ideas == bridge.TopicRoute("owner", label="digest_ideas", owner_agent="kronos")
    assert travel == bridge.TopicRoute(
        "owner",
        label="jb_travel_insights",
        owner_agent="kronos",
    )


def test_topic_route_uses_legacy_digest_env_alias(monkeypatch):
    _clear_topic_alias_env(monkeypatch)
    monkeypatch.setattr(bridge.settings, "telegram_swarm_chat_id", 3642435967)
    monkeypatch.setattr(bridge.settings, "telegram_general_topic_id", 23)
    monkeypatch.setattr(bridge.settings, "telegram_digest_topic_id", 0)
    monkeypatch.setattr(bridge.settings, "telegram_digest_agent", "kronos")
    monkeypatch.setenv("TOPIC_DIGEST", "24")

    route = bridge._resolve_topic_route(-1003642435967, 24)

    assert route == bridge.TopicRoute("owner", label="digest", owner_agent="kronos")


def test_topic_owner_can_allow_multiple_agents(monkeypatch):
    route = bridge.TopicRoute("owner", label="jb_system", owner_agent="kronos,nexus")

    monkeypatch.setattr(bridge.settings, "agent_name", "kronos")
    assert bridge._agent_owns_topic(route) is True

    monkeypatch.setattr(bridge.settings, "agent_name", "nexus")
    assert bridge._agent_owns_topic(route) is True

    monkeypatch.setattr(bridge.settings, "agent_name", "worker")
    assert bridge._agent_owns_topic(route) is False


def test_topic_route_falls_back_outside_configured_chat(monkeypatch):
    _clear_topic_alias_env(monkeypatch)
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


def test_owner_topic_rejects_peer_sender(monkeypatch):
    class FakeRouter:
        def _is_peer(self, sender_id):
            return sender_id == 2001

    monkeypatch.setattr(bridge, "_group_router", FakeRouter())

    assert bridge._owner_topic_accepts_sender(42) is True
    assert bridge._owner_topic_accepts_sender(2001) is False
