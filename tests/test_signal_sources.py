from pathlib import Path

import pytest

from kronos.signals.sources import (
    SignalSourceConfigError,
    load_sources,
    merge_legacy_group_digest_sources,
    parse_sources,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_sources(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_valid_sources_and_renders_legacy_shapes(tmp_path):
    path = _write_sources(
        tmp_path / "SOURCES.yaml",
        """
sources:
  - id: reddit_local_llama
    platform: reddit
    handle: r/LocalLLaMA
    categories: [news, ideas]
    tier: core
    trust: community_high
    language: en
    enabled: true
    description: Local model releases and tooling
    filters:
      min_score: 20
  - id: x_openai_devs
    platform: x
    handle: "@OpenAIDevs"
    categories: [news]
    tier: core
    trust: official
    description: OpenAI developer platform announcements
  - id: telegram_ai_chat
    platform: telegram
    handle: "@ai_chat_cutcode"
    categories: [news, jobs]
    tier: candidate
    trust: community_low
    language: ru
    description: AI chat and job discussions
  - id: search_ai_news
    platform: search
    query: AI tools launched today
    categories: [news]
    tier: core
    trust: community_high
""",
    )

    registry = load_sources(path)

    assert registry.get("reddit_local_llama").filters["min_score"] == 20
    assert [source.id for source in registry.active(categories=("news",))] == [
        "reddit_local_llama",
        "x_openai_devs",
        "telegram_ai_chat",
        "search_ai_news",
    ]
    assert registry.news_monitor_queries(limit=2) == [
        "site:reddit.com r/LocalLLaMA Local model releases and tooling",
        "OpenAI developer platform announcements @OpenAIDevs news",
    ]
    assert registry.telegram_groups(categories=("jobs",)) == {
        "Digest: Jobs": [
            {
                "name": "AI chat and job discussions",
                "identifier": "@ai_chat_cutcode",
                "description": "AI chat and job discussions",
            }
        ]
    }


def test_missing_required_field_reports_source_index():
    with pytest.raises(SignalSourceConfigError, match=r"sources\[0\]\.id"):
        parse_sources(
            {
                "sources": [
                    {
                        "platform": "reddit",
                        "handle": "r/LocalLLaMA",
                        "categories": ["news"],
                    }
                ]
            }
        )


def test_duplicate_ids_are_rejected():
    raw = {
        "sources": [
            {
                "id": "same",
                "platform": "search",
                "query": "AI news",
                "categories": ["news"],
            },
            {
                "id": "same",
                "platform": "search",
                "query": "AI tools",
                "categories": ["news"],
            },
        ]
    }

    with pytest.raises(SignalSourceConfigError, match="duplicate source id 'same'"):
        parse_sources(raw)


def test_disabled_and_quarantine_sources_are_excluded_by_default():
    registry = parse_sources(
        {
            "sources": [
                {
                    "id": "core",
                    "platform": "search",
                    "query": "AI news",
                    "categories": ["news"],
                    "tier": "core",
                },
                {
                    "id": "disabled",
                    "platform": "search",
                    "query": "AI rumors",
                    "categories": ["news"],
                    "enabled": False,
                },
                {
                    "id": "quarantined",
                    "platform": "reddit",
                    "handle": "r/AIDankmemes",
                    "categories": ["news"],
                    "tier": "quarantine",
                    "trust": "noisy",
                },
            ]
        }
    )

    assert [source.id for source in registry.active(categories=("news",))] == ["core"]
    assert [source.id for source in registry.active(categories=("news",), include_quarantine=True)] == [
        "core",
        "quarantined",
    ]
    assert [source.id for source in registry.disabled()] == ["disabled"]
    assert [source.id for source in registry.quarantined()] == ["quarantined"]


def test_packaged_and_template_registries_are_valid_and_in_sync():
    packaged = load_sources(ROOT / "kronos" / "signals" / "SOURCES.yaml")
    registry = load_sources(
        ROOT / "workspaces" / "_template" / "self" / "skills" / "signal-intel" / "references" / "SOURCES.yaml"
    )

    assert [source.id for source in packaged.sources] == [source.id for source in registry.sources]
    locators = [(source.platform, source.locator.lower()) for source in registry.sources]
    assert len(locators) == len(set(locators))

    assert registry.get("telegram_nobilix_chat") is not None
    assert registry.get("telegram_ai_chat_cutcode") is not None
    assert registry.get("telegram_hiaimediaen") is not None
    assert registry.get("reddit_solotravel") is not None
    assert registry.get("search_itinerary_app_reddit") is not None
    assert registry.get("reddit_anthropic").tier == "core"
    assert registry.get("reddit_cline").tier == "core"
    assert registry.get("reddit_gpt_jailbreaks").tier == "quarantine"
    assert registry.get("reddit_local_llm") is None
    assert registry.get("reddit_cursor_ai") is None
    assert registry.get("x_google_ai_studio").trust == "official"
    assert registry.get("x_demishassabis").tier == "core"
    assert registry.get("x_bellcurvebot").tier == "quarantine"
    assert "jobs" in registry.get("telegram_ai_chat_cutcode").categories
    assert "travel_insights" in registry.get("reddit_solotravel").categories
    assert "reddit_gpt_jailbreaks" not in [source.id for source in registry.active(categories=("news",))]
    assert {
        "reddit_ai_dankmemes",
        "reddit_gpt_jailbreaks",
        "x_reddit_lies",
    }.issubset({source.id for source in registry.quarantined()})


def test_legacy_group_digest_sources_merge_into_runtime_registry(tmp_path):
    sources_path = _write_sources(
        tmp_path / "SOURCES.yaml",
        """
sources:
  - id: telegram_existing
    platform: telegram
    handle: "@already"
    categories: [news]
    tier: candidate
    trust: community_low
""",
    )
    groups_path = _write_sources(
        tmp_path / "GROUPS.md",
        """
## AI & LLM

| Name | ID | Description |
|------|----|-------------|
| Existing | @already | Already configured |
| AI News | @ai_news | AI channel |
| Private Chat | id:12345 | Private AI chat |

## Job Market

| Name | ID | Description |
|------|----|-------------|
| Jobs | @jobs | Hiring channel |
""",
    )

    registry = merge_legacy_group_digest_sources(load_sources(sources_path), path=groups_path)

    assert registry.get("telegram_existing").description == ""
    assert registry.get("telegram_ai_news").categories == ("news", "ideas")
    assert registry.get("telegram_id_12345").handle == "id:12345"
    assert registry.get("telegram_jobs").categories == ("jobs",)
    assert registry.get("telegram_jobs").filters["min_views"] == 200
    assert [source.handle for source in registry.sources].count("@already") == 1


def test_invalid_category_and_missing_locator_fail_fast():
    with pytest.raises(SignalSourceConfigError, match="unsupported value 'memes'"):
        parse_sources(
            {
                "sources": [
                    {
                        "id": "bad_category",
                        "platform": "reddit",
                        "handle": "r/LocalLLaMA",
                        "categories": ["memes"],
                    }
                ]
            }
        )

    with pytest.raises(SignalSourceConfigError, match="must define one of handle, url, or query"):
        parse_sources(
            {
                "sources": [
                    {
                        "id": "missing_locator",
                        "platform": "search",
                        "categories": ["news"],
                    }
                ]
            }
        )
