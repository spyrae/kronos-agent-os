import importlib

from kronos.cron import notify

SIGNAL_TOPIC_ENVS = (
    "TOPIC_DIGEST",
    "TOPIC_DIGEST_NEWS",
    "TOPIC_JB_COMPETITORS",
    "TOPIC_JB_SYSTEM",
    "TOPIC_DIGEST_JOBS",
    "TOPIC_DIGEST_IDEAS",
    "TOPIC_JB_TRAVEL_INSIGHTS",
)


def test_resolve_topic_id_prefers_positive_env(monkeypatch):
    monkeypatch.setenv("TOPIC_TEST", "123")

    result = notify._resolve_topic_id(
        "TOPIC_TEST",
        setting_value=456,
        fallback=789,
    )

    assert result == 123


def test_resolve_topic_id_treats_zero_env_as_unconfigured(monkeypatch):
    monkeypatch.setenv("TOPIC_TEST", "0")

    result = notify._resolve_topic_id(
        "TOPIC_TEST",
        setting_value=0,
        fallback=789,
    )

    assert result == 789


def test_resolve_topic_id_uses_setting_before_fallback(monkeypatch):
    monkeypatch.delenv("TOPIC_TEST", raising=False)

    result = notify._resolve_topic_id(
        "TOPIC_TEST",
        setting_value=456,
        fallback=789,
    )

    assert result == 456


def test_resolve_topic_id_returns_zero_when_nothing_configured(monkeypatch):
    monkeypatch.setenv("TOPIC_TEST", "not-an-int")

    result = notify._resolve_topic_id(
        "TOPIC_TEST",
        setting_value=0,
        fallback=0,
    )

    assert result == 0


def test_jb_topics_prefer_dedicated_env_vars(monkeypatch):
    with monkeypatch.context() as patch:
        for env_name in SIGNAL_TOPIC_ENVS:
            patch.delenv(env_name, raising=False)
        patch.setenv("TOPIC_DIGEST", "24")
        patch.setenv("TOPIC_JB_COMPETITORS", "323")
        patch.setenv("TOPIC_JB_SYSTEM", "324")

        reloaded = importlib.reload(notify)

        assert reloaded.TOPIC_JB_COMPETITORS == 323
        assert reloaded.TOPIC_JB_SYSTEM == 324
    importlib.reload(notify)


def test_jb_topics_fallback_to_legacy_digest_when_unconfigured(monkeypatch):
    with monkeypatch.context() as patch:
        for env_name in SIGNAL_TOPIC_ENVS:
            patch.delenv(env_name, raising=False)
        patch.setenv("TOPIC_DIGEST", "24")
        patch.setattr(notify.settings, "telegram_jb_competitors_topic_id", 0)
        patch.setattr(notify.settings, "telegram_jb_system_topic_id", 0)

        reloaded = importlib.reload(notify)

        assert reloaded.TOPIC_JB_COMPETITORS == 24
        assert reloaded.TOPIC_JB_SYSTEM == 24
    importlib.reload(notify)
