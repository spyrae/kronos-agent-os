from kronos.signals.jobs import is_job_signal, job_signal_score
from kronos.signals.models import SignalItem


def _item(title: str, text: str = "", url: str = "") -> SignalItem:
    return SignalItem(
        source_id="search_ai_jobs",
        source_platform="search",
        title=title,
        text=text,
        url=url,
        categories=("jobs",),
    )


def test_direct_job_posting_is_strong_signal():
    item = _item(
        "Founding AI Agent Engineer",
        "We're hiring a remote senior engineer. Apply now.",
        "https://jobs.ashbyhq.com/acme/123",
    )

    assert is_job_signal(item) is True
    assert job_signal_score(item) >= 80


def test_hiring_discussion_without_link_is_weak_but_allowed():
    item = _item("AI startup is hiring", "Looking for an agent engineer in Europe")

    assert is_job_signal(item) is True
    assert 25 <= job_signal_score(item) < 80


def test_generic_hiring_listicle_is_filtered_out():
    item = _item(
        "Top companies hiring AI engineers",
        "SEO listicle with generic companies are hiring copy",
        "https://example.com/top-companies-hiring-ai",
    )

    assert is_job_signal(item) is False
    assert job_signal_score(item) < 25


def test_job_channel_signature_does_not_make_news_a_job():
    item = _item(
        "Маркетинг-директор уходит из компании",
        "Яна оставляет должность. Сайт — https://example.com | Telegram — https://t.me/morejobs",
        "https://t.me/morejobs/15538",
    )

    assert is_job_signal(item) is False
    assert job_signal_score(item) < 25


def test_interview_advice_is_filtered_out():
    item = _item(
        "Как подготовиться к собеседованию",
        "Интервью с HR может вызвать стресс, вот несколько советов.",
        "https://t.me/zarubezhom_jobs/3893",
    )

    assert is_job_signal(item) is False
    assert job_signal_score(item) < 25
