from kronos.cron import notify


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
