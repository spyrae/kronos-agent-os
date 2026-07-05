from kronos.cron.notify import _sanitize_html, _telegram_safe_html


def test_orphan_closing_tag_is_dropped():
    text = "<i>Релевантность:</i> высокая — ИИ плюс Telegram</i>"
    out = _telegram_safe_html(text)

    assert out == "<i>Релевантность:</i> высокая — ИИ плюс Telegram"
    assert out.count("<i>") == out.count("</i>")


def test_stray_ampersand_escaped_and_entities_preserved():
    out = _telegram_safe_html("R&D and Q&A but &amp; stays")

    assert "R&amp;D" in out
    assert "Q&amp;A" in out
    assert "&amp;amp;" not in out  # existing entity is not double-escaped


def test_stray_angle_brackets_escaped():
    out = _telegram_safe_html("команды < 10 человек, экономия > 50%")

    assert "&lt; 10" in out
    assert "&gt; 50%" in out


def test_unknown_tag_escaped_to_literal():
    out = _telegram_safe_html("use <script>alert()</script> here")

    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_allowed_tags_and_links_preserved():
    text = '<b>Title</b> (<a href="https://x.com/a">src</a>) <i>note</i>'

    assert _telegram_safe_html(text) == text


def test_unclosed_tag_is_closed():
    assert _telegram_safe_html("<b>bold never closed") == "<b>bold never closed</b>"


def test_sanitize_html_repairs_llm_idea_block():
    raw = "<b>Идея:</b> Bot & tool\n  <i>Релевантность:</i> высокая</i>"

    out = _sanitize_html(raw)

    assert out.count("<i>") == out.count("</i>")
    assert out.count("<b>") == out.count("</b>")
    assert "Bot &amp; tool" in out
