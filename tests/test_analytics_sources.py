from kronos.analytics.sources import grafana, langfuse_stats, supabase_stats


def test_supabase_count_ignores_unexpected_head_response(monkeypatch) -> None:
    monkeypatch.setattr(supabase_stats, "_rest_get", lambda *args, **kwargs: {"count": 1})

    assert supabase_stats._count("global_users") is None


def test_langfuse_collect_rejects_non_object_traces(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_stats.settings, "langfuse_public_key", "public")
    monkeypatch.setattr(langfuse_stats.settings, "langfuse_secret_key", "secret")
    monkeypatch.setattr(langfuse_stats, "_api_get", lambda *args, **kwargs: [])

    assert langfuse_stats.collect() == {"error": "Unexpected Langfuse traces response"}


def test_grafana_prom_query_ignores_non_object_response(monkeypatch) -> None:
    monkeypatch.setattr(grafana, "_api_get", lambda *args, **kwargs: [])

    assert grafana._prom_query("up") is None
