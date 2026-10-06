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


def _collect_langfuse(monkeypatch, observations: list[dict]) -> dict:
    def fake_api_get(path, params=None):
        if path == "/traces":
            return {"meta": {"totalItems": len(observations)}}
        return {"data": observations, "meta": {"totalItems": len(observations)}}

    monkeypatch.setattr(langfuse_stats.settings, "langfuse_public_key", "public")
    monkeypatch.setattr(langfuse_stats.settings, "langfuse_secret_key", "secret")
    monkeypatch.setattr(langfuse_stats, "_api_get", fake_api_get)
    return langfuse_stats.collect()


def _keyless(route: str, start_time: str | None) -> dict:
    observation = {
        "statusMessage": "No api key passed in.",
        "level": "ERROR",
        "metadata": {"user_api_key_request_route": route},
    }
    if start_time is not None:
        observation["startTime"] = start_time
    return observation


def test_langfuse_separates_own_exposure_probe_from_other_unauthenticated(monkeypatch) -> None:
    result = _collect_langfuse(
        monkeypatch,
        [
            _keyless("/v1/models", "2026-10-06T05:00:02.000Z"),
            _keyless("/v1/models", "2026-10-06T05:00:03.000Z"),
            _keyless("/v1/models", "2026-10-06T06:00:02.000Z"),
            _keyless("/azure/.env", "2026-10-06T06:10:00.000Z"),
        ],
    )

    assert result["exposure_probe_requests"] == 3
    assert result["unauthenticated_requests"] == 1
    assert result["error_rate_pct"] == 0


def test_langfuse_flags_keyless_probe_route_requests_beyond_the_probe_footprint(monkeypatch) -> None:
    # The route is chosen by the sender, so it cannot excuse unlimited traffic:
    # only what the hourly probe itself produces is written off.
    result = _collect_langfuse(
        monkeypatch,
        [_keyless("/v1/models", f"2026-10-06T05:{minute:02d}:00.000Z") for minute in range(7)],
    )

    assert result["exposure_probe_requests"] == 2
    assert result["unauthenticated_requests"] == 5


def test_langfuse_flags_probe_route_request_without_a_timestamp(monkeypatch) -> None:
    result = _collect_langfuse(monkeypatch, [_keyless("/v1/models", None)])

    assert result["exposure_probe_requests"] == 0
    assert result["unauthenticated_requests"] == 1
