from datetime import UTC, datetime

from kronos.observer.models import DialogSnapshot, ReplyDebt
from kronos.observer.render import render_morning_observer_digest


def test_render_empty_morning_digest():
    body = render_morning_observer_digest([], [], generated_at=datetime(2026, 6, 19, 0, 0, tzinfo=UTC))

    assert "🌅 Утренний обзор лички" in body
    assert "Новых долгов/непрочитанного нет" in body


def test_render_escapes_unread_and_debt_blocks():
    snapshot = DialogSnapshot(
        peer_id="1",
        peer_title="<Alice & Bob>",
        unread_count=3,
        excerpt="обсудить <контракт> & оплату",
    )
    debt = ReplyDebt(
        peer_id="2",
        peer_title="Ivan",
        last_incoming_excerpt="жду <ответ>",
        hours_waiting=18,
        severity="medium",
    )

    body = render_morning_observer_digest(
        [snapshot],
        [debt],
        generated_at=datetime(2026, 6, 19, 0, 0, tzinfo=UTC),
    )

    assert "<b>Непрочитанное:</b>" in body
    assert "&lt;Alice &amp; Bob&gt;" in body
    assert "обсудить &lt;контракт&gt; &amp; оплату" in body
    assert "<b>Ждут ответа:</b>" in body
    assert "18ч" in body
    assert "жду &lt;ответ&gt;" in body
