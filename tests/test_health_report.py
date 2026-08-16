"""When a health check speaks, and — mostly — when it does not.

The rules are shared by the acquisition and sandbox probes, which is the point:
they are the subtle part, and a second copy of them is a second thing to get
wrong. Their whole value is in the silence. A checker that reports "everything
is fine" every morning teaches its reader to skip it, and then the one morning
it says something else gets skipped too — worse than no check at all, because it
comes with the feeling of being covered.
"""

import json

import pytest

from kronos import health
from kronos.health import STATUS_BROKEN, STATUS_OFF, STATUS_OK, HealthCheck, report_changes


@pytest.fixture
def sent(monkeypatch):
    """Capture both channels. report_changes imports them at call time."""
    from kronos.cron import notify

    messages: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notify, "send_webhook", lambda text, **kw: messages.append(("webhook", text, "")))
    monkeypatch.setattr(
        notify,
        "send_ntfy",
        lambda text, **kw: messages.append(("ntfy", text, kw.get("priority", ""))),
    )
    return messages


@pytest.fixture
def state(tmp_path):
    return tmp_path / "health.json"


def given(state_path, **statuses):
    state_path.write_text(json.dumps(statuses))


def probe(**statuses):
    return [HealthCheck(name, status, "detail") for name, status in statuses.items()]


def run(checks, state_path, consequence=""):
    return report_changes(
        subject="Widgets",
        checks=checks,
        state_path=state_path,
        title="test",
        consequence=consequence,
    )


# --- silence is the default ---------------------------------------------------


def test_nothing_changed_means_nothing_said(sent, state):
    given(state, a=STATUS_OK, b=STATUS_OK)

    assert run(probe(a=STATUS_OK, b=STATUS_OK), state) is False
    assert sent == []


def test_a_first_run_does_not_announce_something_nobody_installed(sent, state):
    """ "off" would be this job's first message and its least useful one."""
    assert run(probe(a=STATUS_OFF, b=STATUS_OFF), state) is False
    assert sent == []


def test_a_first_run_still_reports_something_actually_broken(sent, state):
    """No baseline is a reason not to compare, not a reason not to look."""
    run(probe(a=STATUS_BROKEN, b=STATUS_OFF), state)

    body = sent[0][1]
    assert "a: broken" in body
    assert "b:" not in body, "an uninstalled capability is not news"


# --- but a change is always said ----------------------------------------------


def test_something_that_broke_is_reported_on_both_channels(sent, state):
    given(state, a=STATUS_OK, b=STATUS_OK)

    run(probe(a=STATUS_OK, b=STATUS_BROKEN), state)

    assert [channel for channel, _, _ in sent] == ["webhook", "ntfy"]
    assert "b: ok → broken" in sent[0][1]
    assert "Still working: a" in sent[0][1]


def test_something_that_vanished_from_the_config_is_reported(sent, state):
    """The host-rebuild case: nothing errors, the capability is simply gone."""
    given(state, a=STATUS_OK)

    run(probe(a=STATUS_OFF), state)

    assert "a: ok → off" in sent[0][1]


def test_recovery_is_reported_too(sent, state):
    """Otherwise the last word on a capability stays 'broken' forever."""
    given(state, a=STATUS_BROKEN)

    run(probe(a=STATUS_OK), state)

    assert "a: broken → ok" in sent[0][1]


def test_a_first_run_finding_a_fault_still_carries_the_consequence(sent, state):
    """There is no yesterday to have lost it from, but it is broken all the same."""
    run(probe(a=STATUS_BROKEN), state, consequence="Widgets are unavailable.")

    assert "Widgets are unavailable." in sent[0][1]
    assert [p for c, _, p in sent if c == "ntfy"] == ["high"]


def test_a_loss_is_louder_than_a_recovery(sent, state):
    """A push that wakes someone is for something being wrong, not right."""
    given(state, a=STATUS_OK)
    run(probe(a=STATUS_BROKEN), state)
    run(probe(a=STATUS_OK), state)

    priorities = [priority for channel, _, priority in sent if channel == "ntfy"]
    assert priorities == ["high", "default"]


def test_the_consequence_is_spelled_out_only_while_something_is_broken(sent, state):
    """On a recovery it would read as a warning about a problem that just ended."""
    given(state, a=STATUS_OK)
    run(probe(a=STATUS_BROKEN), state, consequence="Widgets are unavailable.")
    assert "Widgets are unavailable." in sent[0][1]

    sent.clear()
    run(probe(a=STATUS_OK), state, consequence="Widgets are unavailable.")
    assert "Widgets are unavailable." not in sent[0][1]


# --- remembering ---------------------------------------------------------------


def test_the_result_is_remembered_so_it_is_said_once(sent, state):
    given(state, a=STATUS_OK)

    run(probe(a=STATUS_BROKEN), state)
    said_once = len(sent)
    run(probe(a=STATUS_BROKEN), state)

    assert len(sent) == said_once, "still broken is not new news"
    assert json.loads(state.read_text())["a"] == STATUS_BROKEN


def test_an_unreadable_state_file_does_not_stop_the_report(sent, state):
    """The check is the point; remembering it is the optimisation."""
    state.write_text("{not json")

    run(probe(a=STATUS_BROKEN), state)

    assert "a: broken" in sent[0][1]
    assert json.loads(state.read_text())["a"] == STATUS_BROKEN


def test_a_state_file_that_cannot_be_written_does_not_stop_the_report(sent, state, monkeypatch):
    def _explode(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(health.Path, "mkdir", _explode)

    run(probe(a=STATUS_BROKEN), state)

    assert "a: broken" in sent[0][1]


# --- what a probe could not determine ------------------------------------------


def test_a_check_left_out_is_neither_compared_nor_remembered(sent, state):
    """How a probe says "I could not tell" without inventing a status.

    When the sandbox cannot run code at all, its containment guarantees are
    absent from the list rather than reported broken — because nothing was
    measured. Calling them broken would be a guess, and calling them ok a lie.
    """
    given(state, runs=STATUS_OK, contained=STATUS_OK)

    run(probe(runs=STATUS_BROKEN), state)

    assert "contained" not in sent[0][1]
    assert "contained" not in json.loads(state.read_text())


def test_a_dropped_check_is_reported_when_it_returns_broken(sent, state):
    """The hole this could have left: a guarantee that quietly never comes back."""
    given(state, runs=STATUS_OK, contained=STATUS_OK)
    run(probe(runs=STATUS_BROKEN), state)
    sent.clear()

    run(probe(runs=STATUS_OK, contained=STATUS_BROKEN), state)

    assert "contained: broken" in sent[0][1]
