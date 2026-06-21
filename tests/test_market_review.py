from datetime import UTC, datetime
from types import SimpleNamespace

from kronos.config import settings
from kronos.cron.market_review import (
    _load_watchlist,
    build_market_review_prompt,
    collect_market_news,
    run_market_review,
)
from kronos.llm import ModelTier
from kronos.tools.brave import SearchResult
from kronos.workspace import Workspace


def test_load_watchlist_parses_markdown_table_and_bullets(tmp_path):
    workspace = Workspace(tmp_path)
    path = workspace.skill_ref("investment-analysis", "WATCHLIST")
    path.parent.mkdir(parents=True)
    path.write_text(
        """
| Ticker | Notes |
| NVDA | AI |
- AAPL Apple
- BRK.B Berkshire
- invalid-too-long-symbol
""",
        encoding="utf-8",
    )

    assert _load_watchlist(workspace) == ["NVDA", "AAPL", "BRK.B"]


def test_collect_market_news_skips_empty_and_failed_search_results():
    def fake_search(query, count, freshness):
        if query.startswith("AAPL"):
            return [SearchResult("Apple news", "https://example.com/aapl", "Weekly update")]
        if query.startswith("MSFT"):
            raise RuntimeError("quota")
        return []

    bundles = collect_market_news(["AAPL", "MSFT", "TSLA"], search_fn=fake_search)

    assert len(bundles) == 1
    assert bundles[0].ticker == "AAPL"
    assert bundles[0].items == (("Apple news", "Weekly update"),)


def test_market_review_prompt_is_non_advisory():
    prompt = build_market_review_prompt(
        collect_market_news(
            ["AAPL"],
            search_fn=lambda *args, **kwargs: [SearchResult("Apple news", "", "Weekly update")],
        ),
        today="2026-06-19",
    )

    assert "не индивидуальная инвестиционная рекомендация" in prompt
    assert "Не пиши прямые команды купить/продать" in prompt
    assert "monitor / review thesis / reduce risk / wait for data" in prompt


async def test_run_market_review_sends_weekly_brief(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    sent = []
    prompts = []

    def fake_search(query, count, freshness):
        return [SearchResult("Nvidia earnings", "https://example.com/nvda", "Data center growth")]

    class FakeModel:
        def invoke(self, messages):
            assert len(messages) == 1
            prompts.append(messages[0].content)
            return SimpleNamespace(
                content=(
                    "<b>Обзор</b>\n"
                    "NVDA: 🟢 сильный отчёт, но это не индивидуальная рекомендация. "
                    "Следить за guidance и capex."
                )
            )

    def fake_model_factory(tier):
        assert tier == ModelTier.STANDARD
        return FakeModel()

    def fake_sender(text, **kwargs):
        sent.append((text, kwargs))
        return True

    ok = await run_market_review(
        tickers=["NVDA"],
        search_fn=fake_search,
        model_factory=fake_model_factory,
        sender=fake_sender,
        now=datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
    )

    assert ok is True
    assert prompts and "Новости по watchlist" in prompts[0]
    assert sent[0][0].startswith("<b>📈 Weekly Market Review — 2026-06-19</b>")
    assert sent[0][1]["parse_mode"] == "HTML"


async def test_run_market_review_skips_non_kronos_agent(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "nexus")

    ok = await run_market_review(
        tickers=["NVDA"],
        search_fn=lambda *args, **kwargs: [SearchResult("Nvidia", "", "")],
        sender=lambda *args, **kwargs: True,
    )

    assert ok is False
