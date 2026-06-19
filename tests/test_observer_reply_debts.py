from datetime import UTC, datetime, timedelta

from kronos.observer.models import DialogSnapshot
from kronos.observer.reply_debts import detect_reply_debts
from kronos.observer.state import ObserverState

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _snapshot(
    peer_id,
    *,
    peer_title="Alice",
    newest_direction="incoming",
    incoming_age_hours=9,
    outgoing_after=False,
    excerpt="Нужно обсудить контракт",
    unread_count=1,
):
    incoming_at = NOW - timedelta(hours=incoming_age_hours)
    messages = []
    if outgoing_after:
        messages.append(
            {
                "id": 12,
                "date": (NOW - timedelta(hours=1)).isoformat(),
                "direction": "outgoing",
                "excerpt": "ответил",
            }
        )
    messages.append(
        {
            "id": 11,
            "date": incoming_at.isoformat(),
            "direction": newest_direction,
            "excerpt": excerpt,
        }
    )
    return DialogSnapshot(
        peer_id=str(peer_id),
        peer_title=peer_title,
        last_message_id=int(messages[0]["id"]),
        unread_count=unread_count,
        message_count=len(messages),
        excerpt=excerpt,
        metadata={
            "last_incoming_at": incoming_at.isoformat(),
            "last_message_direction": messages[0]["direction"],
            "recent_messages": messages,
        },
    )


def test_incoming_last_message_becomes_reply_debt():
    debts = detect_reply_debts([_snapshot("1")], NOW)

    assert len(debts) == 1
    debt = debts[0]
    assert debt.peer_id == "1"
    assert debt.severity == "medium"
    assert debt.hours_waiting == 9
    assert debt.last_incoming_message_id == 11
    assert debt.last_incoming_excerpt == "Нужно обсудить контракт"
    assert "9.0h" in debt.reason
    assert debt.suggested_action == "Ответить Alice"


def test_outgoing_after_incoming_is_not_reply_debt():
    debts = detect_reply_debts([_snapshot("1", outgoing_after=True)], NOW)

    assert debts == []


def test_ignored_or_muted_peers_are_skipped():
    state = ObserverState(ignored_peers={"1"}, muted_peers={"2"})

    debts = detect_reply_debts([_snapshot("1"), _snapshot("2"), _snapshot("3")], NOW, state=state)

    assert [debt.peer_id for debt in debts] == ["3"]


def test_threshold_is_strictly_greater_than_threshold_hours():
    edge = _snapshot("edge", incoming_age_hours=8)
    over = _snapshot("over", incoming_age_hours=8.1)

    debts = detect_reply_debts([edge, over], NOW, threshold_hours=8)

    assert [debt.peer_id for debt in debts] == ["over"]


def test_severity_buckets_and_sorting():
    medium = _snapshot("medium", peer_title="Medium", incoming_age_hours=10)
    critical = _snapshot("critical", peer_title="Critical", incoming_age_hours=80)
    high = _snapshot("high", peer_title="High", incoming_age_hours=30)

    debts = detect_reply_debts([medium, critical, high], NOW)

    assert [(debt.peer_id, debt.severity) for debt in debts] == [
        ("critical", "critical"),
        ("high", "high"),
        ("medium", "medium"),
    ]


def test_noise_message_is_not_reply_debt():
    debts = detect_reply_debts([_snapshot("1", excerpt="ок")], NOW)

    assert debts == []


def test_reply_debt_dedupe_fields_roundtrip_in_state():
    state = ObserverState(
        last_reported_debts={"1": "2026-06-19T08:00:00Z"},
        last_critical_alerts={"1": "2026-06-19T09:00:00Z"},
    )

    reloaded = ObserverState.from_dict(state.to_dict())

    assert reloaded.last_reported_debts == {"1": "2026-06-19T08:00:00Z"}
    assert reloaded.last_critical_alerts == {"1": "2026-06-19T09:00:00Z"}
