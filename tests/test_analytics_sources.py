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


def test_supabase_active_trials_exclude_ended_trials(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_rest_get(table, params=None, **kwargs):
        calls.append((table, dict(params or {})))
        return 0 if kwargs.get("head") else []

    monkeypatch.setattr(supabase_stats.settings, "supabase_url", "https://db.test")
    monkeypatch.setattr(supabase_stats.settings, "supabase_service_role_key", "key")
    monkeypatch.setattr(supabase_stats, "_rest_get", fake_rest_get)

    result = supabase_stats.collect()

    trial_filters = [params for table, params in calls if table == "user_trials"]
    assert result["db_active_trials"] == 0
    assert len(trial_filters) == 1
    assert trial_filters[0]["status"] == "eq.active"
    assert trial_filters[0]["ends_at"].startswith("gt.")


def test_langfuse_separates_own_exposure_probe_from_other_unauthenticated(monkeypatch) -> None:
    def rejected(route: str) -> dict:
        return {
            "statusMessage": "No api key passed in.",
            "level": "ERROR",
            "metadata": {"user_api_key_request_route": route},
        }

    observations = [rejected("/v1/models")] * 3 + [rejected("/azure/.env")]

    def fake_api_get(path, params=None):
        if path == "/traces":
            return {"meta": {"totalItems": 4}}
        return {"data": observations, "meta": {"totalItems": len(observations)}}

    monkeypatch.setattr(langfuse_stats.settings, "langfuse_public_key", "public")
    monkeypatch.setattr(langfuse_stats.settings, "langfuse_secret_key", "secret")
    monkeypatch.setattr(langfuse_stats, "_api_get", fake_api_get)

    result = langfuse_stats.collect()

    assert result["exposure_probe_requests"] == 3
    assert result["unauthenticated_requests"] == 1
    assert result["error_rate_pct"] == 0
