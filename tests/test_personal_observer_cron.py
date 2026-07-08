from datetime import UTC, datetime

from kronos.config import settings
from kronos.cron.personal_observer import run_personal_observer
from kronos.cron.setup import setup_cron_jobs
from kronos.observer.models import DialogSnapshot
from kronos.observer.state import ObserverStateStore
from kronos.workspace import Workspace


class SchedulerSpy:
    def __init__(self):
        self.daily = {}

    def add_periodic(self, name, _func, interval_seconds):
        pass

    def add_daily(self, name, func, hour_utc):
        self.daily[name] = (func, hour_utc)

    def add_weekly(self, name, _func, weekday, hour_utc):
        pass


async def test_personal_observer_cron_scans_renders_and_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    store = ObserverStateStore(Workspace(tmp_path))
    sent = []

    async def fake_scanner(client, state, *, limit_dialogs, limit_messages_per_dialog):
        assert client == "client"
        assert state is store
        assert limit_dialogs == 2
        assert limit_messages_per_dialog == 3
        incoming_at = datetime(2026, 6, 18, 0, 0, tzinfo=UTC)
        return [
            DialogSnapshot(
                peer_id="1",
                peer_title="Alice",
                unread_count=2,
                excerpt="Нужно ответить по договору",
                metadata={
                    "recent_messages": [
                        {
                            "id": 1,
                            "date": incoming_at.isoformat(),
                            "direction": "incoming",
                            "excerpt": "Нужно ответить по договору",
                        }
                    ],
                    "last_incoming_at": incoming_at.isoformat(),
                    "last_message_direction": "incoming",
                },
            )
        ]

    def fake_sender(text, **kwargs):
        sent.append((text, kwargs))
        return True

    ok = await run_personal_observer(
        client="client",
        state_store=store,
        scanner=fake_scanner,
        sender=fake_sender,
        now=datetime(2026, 6, 19, 0, 0, tzinfo=UTC),
        limit_dialogs=2,
        limit_messages_per_dialog=3,
        threshold_hours=8,
    )

    assert ok is True
    assert len(sent) == 1
    assert sent[0][1]["parse_mode"] == "HTML"
    assert "Alice" in sent[0][0]
    assert "Ждут ответа" in sent[0][0]


async def test_personal_observer_skips_non_kronos_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_name", "nexus")
    called = False

    async def fake_scanner(*args, **kwargs):
        nonlocal called
        called = True
        return []

    ok = await run_personal_observer(
        client="client",
        state_store=ObserverStateStore(Workspace(tmp_path)),
        scanner=fake_scanner,
        sender=lambda *args, **kwargs: True,
    )

    assert ok is False
    assert called is False


async def test_personal_observer_skips_without_userbot(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_name", "kronos")

    async def no_userbot():
        return None

    monkeypatch.setattr("kronos.cron.personal_observer.get_userbot", no_userbot)

    ok = await run_personal_observer(
        state_store=ObserverStateStore(Workspace(tmp_path)),
        sender=lambda *args, **kwargs: True,
    )

    assert ok is False


def test_personal_observer_registered_in_cron_setup(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    scheduler = SchedulerSpy()

    setup_cron_jobs(scheduler)

    assert scheduler.daily["personal-observer"][1] == 23


def test_personal_observer_schedule_avoids_existing_morning_conflicts(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "kronos")
    scheduler = SchedulerSpy()

    setup_cron_jobs(scheduler)
    used_hours = {name: hour for name, (_func, hour) in scheduler.daily.items()}

    # personal-observer at 23:00 UTC must not collide with any other daily
    # job's hour. Checking against every registered daily job (instead of a
    # hardcoded list) keeps the test correct as jobs are paused/resumed —
    # e.g. group-digest was paused 2026-07-07 and its key no longer exists.
    other_daily_hours = {
        hour for name, hour in used_hours.items() if name != "personal-observer"
    }
    assert used_hours["personal-observer"] == 23
    assert used_hours["personal-observer"] not in other_daily_hours
