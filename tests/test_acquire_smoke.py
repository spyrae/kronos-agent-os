"""The daily job that notices a fetch tier died.

Its whole value is in when it stays quiet. A checker that reports "all three
tiers fine" every morning teaches its reader to skip it, and then the one
morning it says something else gets skipped too — which is worse than not having
it, because it comes with the feeling of being covered.
"""

import json

import pytest

from kronos.config import settings
from kronos.cron import acquire_smoke
from kronos.tools.acquire import (
    TIER_BROKEN,
    TIER_BROWSER,
    TIER_OFF,
    TIER_OK,
    TIER_PLAIN,
    TIER_STEALTH,
    TierHealth,
)


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """A host running the owning agent, with every message captured."""
    monkeypatch.setattr(settings, "agent_name", acquire_smoke.OWNER_AGENT)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))

    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(acquire_smoke, "send_webhook", lambda text, **kw: messages.append(("webhook", text)))
    monkeypatch.setattr(acquire_smoke, "send_ntfy", lambda text, **kw: messages.append(("ntfy", text)))
    return messages


def tiers(monkeypatch, plain=TIER_OK, stealth=TIER_OK, browser=TIER_OK):
    async def _check(url=None):
        return [
            TierHealth(TIER_PLAIN, plain, "detail"),
            TierHealth(TIER_STEALTH, stealth, "detail"),
            TierHealth(TIER_BROWSER, browser, "detail"),
        ]

    monkeypatch.setattr(acquire_smoke, "check_tier_health", _check)


def state_file(tmp_path):
    return tmp_path / "acquire_health.json"


# --- silence is the default ---------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_changed_means_nothing_said(sent, tmp_path, monkeypatch):
    tiers(monkeypatch)
    state_file(tmp_path).write_text(json.dumps({TIER_PLAIN: TIER_OK, TIER_STEALTH: TIER_OK, TIER_BROWSER: TIER_OK}))

    await acquire_smoke.run_acquire_smoke()

    assert sent == []


@pytest.mark.asyncio
async def test_a_first_run_does_not_announce_a_backend_nobody_installed(sent, tmp_path, monkeypatch):
    """ "stealth: off" would be this job's first message and its least useful one."""
    tiers(monkeypatch, stealth=TIER_OFF, browser=TIER_OFF)

    await acquire_smoke.run_acquire_smoke()

    assert sent == []


@pytest.mark.asyncio
async def test_only_the_owning_agent_probes(sent, tmp_path, monkeypatch):
    """Six agents share one host: six probes are five wasted browser launches."""
    monkeypatch.setattr(settings, "agent_name", "impulse")
    called = []

    async def _check(url=None):
        called.append(True)
        return []

    monkeypatch.setattr(acquire_smoke, "check_tier_health", _check)

    await acquire_smoke.run_acquire_smoke()

    assert called == []
    assert sent == []


# --- but a change is always said ----------------------------------------------


@pytest.mark.asyncio
async def test_a_tier_that_broke_is_reported(sent, tmp_path, monkeypatch):
    tiers(monkeypatch, browser=TIER_BROKEN)
    state_file(tmp_path).write_text(json.dumps({TIER_PLAIN: TIER_OK, TIER_STEALTH: TIER_OK, TIER_BROWSER: TIER_OK}))

    await acquire_smoke.run_acquire_smoke()

    assert [channel for channel, _ in sent] == ["webhook", "ntfy"]
    body = sent[0][1]
    assert "browser: ok → broken" in body
    assert "Still working: plain, stealth" in body


@pytest.mark.asyncio
async def test_a_tier_that_vanished_from_the_config_is_reported(sent, tmp_path, monkeypatch):
    """The host-rebuild case: nothing errors, the tier is simply gone."""
    tiers(monkeypatch, stealth=TIER_OFF)
    state_file(tmp_path).write_text(json.dumps({TIER_PLAIN: TIER_OK, TIER_STEALTH: TIER_OK, TIER_BROWSER: TIER_OK}))

    await acquire_smoke.run_acquire_smoke()

    assert "stealth: ok → off" in sent[0][1]


@pytest.mark.asyncio
async def test_a_first_run_still_reports_something_actually_broken(sent, tmp_path, monkeypatch):
    """No baseline is a reason not to compare, not a reason not to look."""
    tiers(monkeypatch, plain=TIER_BROKEN, stealth=TIER_OFF, browser=TIER_OFF)

    await acquire_smoke.run_acquire_smoke()

    body = sent[0][1]
    assert "plain: broken" in body
    assert "stealth" not in body, "an uninstalled backend is not news"


@pytest.mark.asyncio
async def test_recovery_is_reported_too(sent, tmp_path, monkeypatch):
    """Closing the loop matters: otherwise the last word on a tier is 'broken'."""
    tiers(monkeypatch)
    state_file(tmp_path).write_text(json.dumps({TIER_PLAIN: TIER_OK, TIER_STEALTH: TIER_BROKEN, TIER_BROWSER: TIER_OK}))

    await acquire_smoke.run_acquire_smoke()

    assert "stealth: broken → ok" in sent[0][1]


@pytest.mark.asyncio
async def test_a_loss_is_louder_than_a_recovery(sent, tmp_path, monkeypatch):
    """A push that wakes someone should be reserved for something being wrong."""
    priorities = []
    monkeypatch.setattr(
        acquire_smoke,
        "send_ntfy",
        lambda text, **kw: priorities.append(kw.get("priority")),
    )

    tiers(monkeypatch, browser=TIER_BROKEN)
    state_file(tmp_path).write_text(json.dumps({TIER_PLAIN: TIER_OK, TIER_STEALTH: TIER_OK, TIER_BROWSER: TIER_OK}))
    await acquire_smoke.run_acquire_smoke()

    tiers(monkeypatch)
    await acquire_smoke.run_acquire_smoke()

    assert priorities == ["high", "default"]


# --- remembering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_result_is_remembered_so_it_is_said_once(sent, tmp_path, monkeypatch):
    tiers(monkeypatch, browser=TIER_BROKEN)
    state_file(tmp_path).write_text(json.dumps({TIER_BROWSER: TIER_OK}))

    await acquire_smoke.run_acquire_smoke()
    said_once = len(sent)
    await acquire_smoke.run_acquire_smoke()

    assert len(sent) == said_once, "a tier that is still broken is not new news"
    assert json.loads(state_file(tmp_path).read_text())[TIER_BROWSER] == TIER_BROKEN


@pytest.mark.asyncio
async def test_an_unreadable_state_file_does_not_stop_the_probe(sent, tmp_path, monkeypatch):
    """The check is the point; remembering it is the optimisation."""
    tiers(monkeypatch, plain=TIER_BROKEN)
    state_file(tmp_path).write_text("{not json")

    await acquire_smoke.run_acquire_smoke()

    assert "plain: broken" in sent[0][1]
    assert json.loads(state_file(tmp_path).read_text())[TIER_PLAIN] == TIER_BROKEN
