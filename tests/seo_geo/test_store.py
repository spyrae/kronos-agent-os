from datetime import UTC, datetime, timedelta

from kronos.seo_geo.store import SeoGeoStore


def test_time_window_queries_use_the_requested_interval(tmp_path) -> None:
    store = SeoGeoStore(tmp_path / "seo_geo.db")
    try:
        store.record_position(
            site_id="journeybay",
            engine="google_com",
            keyword="travel planner",
            locale="en",
            tier="core",
            category="product",
            position=3,
        )
        store._conn.execute(
            "INSERT INTO positions "
            "(checked_at, site_id, engine, keyword, locale, tier, category, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (datetime.now(UTC) - timedelta(days=8)).isoformat(),
                "journeybay",
                "google_com",
                "travel planner",
                "en",
                "core",
                "product",
                9,
            ),
        )
        store.record_citation(
            site_id="journeybay",
            engine="chatgpt",
            question="Which travel planner should I use?",
            locale="en",
            answer="JourneyBay is a travel planner with itinerary support.",
            cited=True,
            competitors_cited='["Other Planner"]',
        )
        store._conn.execute(
            "INSERT INTO geo_citations "
            "(checked_at, site_id, engine, question, locale, answer, cited, competitors_cited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (datetime.now(UTC) - timedelta(days=8)).isoformat(),
                "journeybay",
                "chatgpt",
                "Old question",
                "en",
                "An older answer that must not affect recent metrics.",
                0,
                '["Old Planner"]',
            ),
        )
        store._conn.commit()

        assert store.position_delta("journeybay", "google_com", "travel planner") == -6
        assert store.citation_rate("journeybay") == {"chatgpt": 100.0}
        assert store.competitor_mentions("journeybay") == {"other planner": 1}
        assert len(store.sample_answers("journeybay")) == 1
    finally:
        store.close()
