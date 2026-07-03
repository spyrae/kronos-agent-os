import pytest

from kronos.config import settings
from kronos.signals.digest import _truncate_html, polish_rendered_digest, render_digest, save_rendered_digest
from kronos.signals.models import SignalItem
from kronos.signals.routing import route_for_category
from kronos.signals.sources import SignalSource
from kronos.signals.store import SignalStore


def _item(
    source_id: str,
    platform: str,
    text: str,
    url: str = "",
    *,
    categories: tuple[str, ...] = ("news",),
) -> SignalItem:
    return SignalItem(
        source_id=source_id,
        source_platform=platform,
        title=text,
        text=text,
        url=url,
        categories=categories,
    )


def test_routes_categories_to_destinations():
    assert route_for_category("news").destination == "Digest: News"
    assert route_for_category("jobs").destination == "Digest: Jobs"
    assert route_for_category("ideas").destination == "Digest: Product/Business Ideas"
    assert route_for_category("travel_insights").destination == "JB: Travel Insights"

    with pytest.raises(ValueError, match="unsupported signal category"):
        route_for_category("unknown")


def test_render_news_digest_is_unified_and_sanitizes_anecdote_language():
    clusters = [
        {
            "id": 1,
            "category": "news",
            "title": "рынок сдвигается к Codex",
            "summary": "все переходят массово после одного обсуждения",
            "item_ids": [101],
        },
        {
            "id": 2,
            "category": "news",
            "title": "Agent tooling releases",
            "summary": "Multiple independent sources mention agent tooling.",
            "item_ids": [201, 202],
        },
        {
            "id": 3,
            "category": "news",
            "title": "Official API update",
            "summary": "Official changelog shipped.",
            "item_ids": [301],
        },
    ]
    items_by_cluster = {
        1: [_item("telegram_nobilix_chat", "telegram", "One chat message")],
        2: [
            _item("reddit_local_llama", "reddit", "Agent tooling releases"),
            _item("x_omarsar0", "x", "Agent tooling commentary"),
        ],
        3: [_item("x_openai_devs", "x", "Official API update", "https://x.com/OpenAIDevs/status/1")],
    }
    sources = {
        "x_openai_devs": SignalSource(
            id="x_openai_devs",
            platform="x",
            handle="@OpenAIDevs",
            categories=("news",),
            tier="core",
            trust="official",
        )
    }

    rendered = render_digest("news", clusters, items_by_cluster, sources_by_id=sources)

    assert rendered.route.destination == "Digest: News"
    assert rendered.body.startswith("<b>📱 Дайджест — ")
    assert "<b>✅ Подтверждено / официально</b>" not in rendered.body
    assert "<b>📈 Формирующиеся сигналы</b>" not in rendered.body
    assert "<b>👀 Наблюдения к проверке</b>" not in rendered.body
    assert "есть единичный сигнал к Codex" in rendered.body
    assert "отдельные источники упоминают в отдельных обсуждениях" in rendered.body
    assert "рынок сдвигается" not in rendered.body
    assert "Доказательность:" not in rendered.body
    assert "Осторожно: это наблюдение" not in rendered.body
    assert 'href="https://x.com/OpenAIDevs/status/1"' in rendered.body


def test_render_digest_keeps_full_body_for_sender_chunking():
    clusters = [
        {
            "id": 1,
            "category": "ideas",
            "title": "Long idea",
            "summary": "x" * 500,
            "item_ids": [1],
        }
    ]
    rendered = render_digest(
        "ideas",
        clusters,
        {1: [_item("x_ideabrowser", "x", "Long idea")]},
        max_chars=220,
    )

    assert len(rendered.body) > 220
    assert "обрезано под лимит Telegram" not in rendered.body


def test_truncate_html_is_legacy_noop_because_sender_chunks():
    text = "\n".join(
        [
            "<b>Новости и ИИ-индустрия — обзор сигналов</b>",
            "",
            "<b>Наблюдения</b>",
            '• <b>Item</b> (<a href="https://example.com/source">source</a>)',
            "  " + ("long summary " * 80),
        ]
    )

    truncated = _truncate_html(text, max_chars=175)

    assert truncated == text
    assert "обрезано под лимит Telegram" not in truncated
    assert truncated.count("<b>") == truncated.count("</b>")
    assert truncated.count("<i>") == truncated.count("</i>")
    assert truncated.count("<a ") == truncated.count("</a>")


def test_polish_rendered_digest_uses_llm_for_russian_cleanup(monkeypatch):
    class Response:
        content = "<b>Новости и ИИ-индустрия</b>\n• <b>Сигнал</b> — чистый русский текст"

    called = False

    def fake_invoke(messages, tier):
        nonlocal called
        called = True
        return Response()

    monkeypatch.setattr("kronos.llm.is_runtime_llm_configured", lambda: True)
    monkeypatch.setattr("kronos.llm.invoke_with_fallback", fake_invoke)

    rendered = render_digest(
        "news",
        [{"id": 1, "category": "news", "title": "**AI launch**", "summary": "New tool shipped", "item_ids": [1]}],
        {1: [_item("x_openai_devs", "x", "New tool shipped")]},
        max_chars=10000,
    )

    polished = polish_rendered_digest(rendered)

    assert called is True
    assert "чистый русский текст" in polished.body
    assert "**" not in polished.body


def test_polish_rendered_digest_retries_when_english_role_terms_remain(monkeypatch):
    class Response:
        def __init__(self, content: str) -> None:
            self.content = content

    calls: list[str] = []

    def fake_invoke(messages, tier):
        prompt = messages[-1].content
        calls.append(prompt)
        if len(calls) == 1:
            return Response("<b>Jobs</b>\n• <b>Middle Product Manager</b>\nRemote work")
        return Response("<b>Вакансии</b>\n• <b>Менеджер продукта среднего уровня</b>\nУдалённая работа")

    monkeypatch.setattr("kronos.llm.is_runtime_llm_configured", lambda: True)
    monkeypatch.setattr("kronos.llm.invoke_with_fallback", fake_invoke)

    rendered = render_digest(
        "jobs",
        [{"id": 1, "category": "jobs", "title": "Middle Product Manager", "summary": "Remote work", "item_ids": [1]}],
        {1: [_item("search_ai_jobs", "search", "Remote work", categories=("jobs",))]},
        max_chars=10000,
    )

    polished = polish_rendered_digest(rendered)

    assert len(calls) == 2
    assert "СТРОГО" in calls[1]
    assert "Менеджер продукта" in polished.body
    assert "Product Manager" not in polished.body


def test_polish_rendered_digest_localizes_ai_acronym_without_touching_openai(monkeypatch):
    monkeypatch.setattr("kronos.llm.is_runtime_llm_configured", lambda: False)

    rendered = render_digest(
        "travel_insights",
        [
            {
                "id": 1,
                "category": "travel_insights",
                "title": "AI trip planner by OpenAI",
                "summary": "",
                "item_ids": [1],
            }
        ],
        {
            1: [
                _item(
                    "search_travel_planning_ai",
                    "search",
                    "AI trip planner by OpenAI: https://t.me/AI_Handler/146",
                    url="https://t.me/AI_Handler/146",
                )
            ]
        },
        max_chars=10000,
    )

    polished = polish_rendered_digest(rendered)

    assert "ИИ trip planner by OpenAI" in polished.body
    assert "OpenAI" in polished.body
    assert "https://t.me/AI_Handler/146" in polished.body
    assert "https://t.me/ИИ_Handler/146" not in polished.body


def test_render_ideas_digest_uses_product_format_and_limits_to_ten():
    clusters = [
        {
            "id": index,
            "category": "ideas",
            "title": f"Looking for a tool #{index}",
            "summary": "I wish there was a tool to automate this manual workflow.",
            "item_ids": [index],
            "importance_score": 100 - index,
        }
        for index in range(1, 13)
    ]
    items_by_cluster = {
        index: [
            SignalItem(
                source_id="reddit_ai_agents",
                source_platform="reddit",
                title=f"Looking for a tool #{index}",
                text="I wish there was a tool to automate this manual workflow.",
                categories=("ideas",),
            )
        ]
        for index in range(1, 13)
    }

    rendered = render_digest("ideas", clusters, items_by_cluster, max_chars=10000)

    assert rendered.body.count("<b>Идея:</b>") == 10
    assert len(rendered.cluster_ids) == 10
    assert "<b>Продуктовый угол:</b>" in rendered.body
    assert "<b>Почему сейчас:</b>" in rendered.body
    assert "<b>Ограничение:</b>" in rendered.body
    assert "подтверждённый спрос" in rendered.body


def test_render_travel_digest_uses_journeybay_format_and_guardrails():
    clusters = [
        {
            "id": 1,
            "category": "travel_insights",
            "title": "Group itinerary sharing is confusing",
            "summary": "Travelers wish trip planning apps made collaboration and offline maps easier.",
            "item_ids": [1],
            "importance_score": 90,
        }
    ]
    items_by_cluster = {
        1: [
            SignalItem(
                source_id="reddit_travel",
                source_platform="reddit",
                title="Group itinerary sharing is confusing",
                text="I wish trip planning apps made collaboration and offline maps easier.",
                categories=("travel_insights",),
            )
        ]
    }

    rendered = render_digest("travel_insights", clusters, items_by_cluster)

    assert rendered.route.destination == "JB: Travel Insights"
    assert "<b>Инсайт:</b>" in rendered.body
    assert "<b>Что это значит для JourneyBay:</b>" in rendered.body
    assert "совместные маршруты" in rendered.body
    assert "нельзя называть это трендом" in rendered.body


def test_save_rendered_digest_persists_dry_run(tmp_path, monkeypatch):
    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_dir", str(db_dir))
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))

    from kronos import db as _db

    _db._instances.clear()
    store = SignalStore()
    rendered = render_digest(
        "jobs",
        [{"id": 9, "category": "jobs", "title": "AI job", "summary": "Hiring", "item_ids": [90]}],
        {9: [_item("telegram_ai_chat_cutcode", "telegram", "Hiring AI engineer")]},
    )

    digest_id = save_rendered_digest(store, rendered, dry_run=True)

    digest = store.list_digests(destination="Digest: Jobs")[0]
    assert digest["id"] == digest_id
    assert digest["title"].startswith("[dry-run]")
    assert digest["cluster_ids"] == [9]
    assert digest["item_ids"] == [90]
