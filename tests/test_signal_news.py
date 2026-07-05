from kronos.signals.models import SignalItem
from kronos.signals.news import is_news_noise, is_news_signal, news_priority_score, news_signal_score
from kronos.signals.sources import SignalSource


def _item(
    title: str,
    text: str = "",
    *,
    url: str = "https://example.com/x",
    platform: str = "reddit",
    source_id: str = "reddit_local_llama",
) -> SignalItem:
    return SignalItem(
        source_id=source_id,
        source_platform=platform,
        title=title,
        text=text,
        url=url,
        categories=("news",),
    )


def _official_source() -> SignalSource:
    return SignalSource(
        id="x_openai_devs",
        platform="x",
        handle="@OpenAIDevs",
        categories=("news",),
        tier="core",
        trust="official",
    )


def test_release_headline_is_news_signal():
    item = _item("OpenAI released GPT-5", "New model now available with a bigger context window.")

    assert is_news_signal(item) is True
    assert news_signal_score(item) >= 40


def test_promo_giveaway_is_noise_and_filtered():
    item = _item("Massive giveaway", "Use this promo code, sponsored airdrop.")

    assert is_news_noise(item) is True
    assert is_news_signal(item) is False


def test_official_source_passes_even_low_signal():
    item = _item("Status note", "", url="", platform="x", source_id="x_openai_devs")

    assert news_signal_score(item) < 10
    assert is_news_signal(item, _official_source()) is True


def test_official_source_still_dropped_on_hard_noise():
    item = _item("Giveaway time", "airdrop giveaway", url="", platform="x", source_id="x_openai_devs")

    assert is_news_signal(item, _official_source()) is False


def test_low_effort_offtopic_is_dropped():
    item = _item("lol", "", url="")

    assert is_news_signal(item) is False


def test_news_priority_score_rewards_stronger_signal():
    strong = _item("Model launched", "Now available")
    weak = _item("hmm", "", url="")

    assert news_priority_score([strong]) > news_priority_score([weak])
