"""Conditions decide when weeks of waiting end, so their edges are the feature.

The distinctions worth testing: a site being down is not a price drop; a pattern
that stops matching is not a number that fell; and a price written 8.750.000 is
not eight point seven five. Each of those, read wrong, is either a notification
that never comes or one that comes falsely.
"""

import pytest

from kronos import plan_conditions as pc


@pytest.fixture(autouse=True)
def allow_egress(monkeypatch):
    """The allowlist is tested elsewhere; here it must not be the thing failing."""
    monkeypatch.setattr("kronos.security.egress.check_url", lambda url, tool="": None)
    yield


NOW = 1_000_000.0


# --- validation, while it can still be fixed -----------------------------------


def test_an_unknown_condition_is_refused():
    with pytest.raises(pc.ConditionError, match="unknown condition"):
        pc.normalize({"kind": "vibes"})


def test_at_needs_a_future():
    with pytest.raises(pc.ConditionError, match="seconds > 0"):
        pc.normalize({"kind": "at", "seconds": 0})


def test_at_turns_a_delay_into_a_moment():
    spec, wake = pc.normalize({"kind": "at", "seconds": 3600}, now=NOW)

    assert spec == {"kind": "at", "timestamp": NOW + 3600}
    assert wake == NOW + 3600


def test_at_accepts_an_absolute_time():
    spec, wake = pc.normalize({"kind": "at", "timestamp": NOW + 10}, now=NOW)

    assert wake == NOW + 10
    assert spec["timestamp"] == NOW + 10


def test_a_manual_wait_is_never_woken_by_the_clock():
    """It ends when the owner says so, or when the plan expires — not on a timer."""
    _, wake = pc.normalize({"kind": "manual", "note": "after I view the flat"}, now=NOW)

    assert wake > NOW + 86400 * 300


def test_a_page_condition_is_checked_at_once():
    """A price already below the threshold should not have to drop a second time."""
    _, wake = pc.normalize({"kind": "page_matches", "url": "https://x.test/a", "pattern": "sold"}, now=NOW)

    assert wake == 0.0


def test_a_page_condition_needs_a_real_url():
    with pytest.raises(pc.ConditionError, match="http"):
        pc.normalize({"kind": "page_matches", "url": "x.test/a", "pattern": "sold"})


def test_a_forbidden_host_is_refused_when_the_step_is_written(monkeypatch):
    def blocked(url, tool=""):
        raise RuntimeError("host not on the allowlist")

    monkeypatch.setattr("kronos.security.egress.check_url", blocked)

    with pytest.raises(RuntimeError, match="allowlist"):
        pc.normalize({"kind": "page_matches", "url": "https://x.test/a", "pattern": "sold"})


def test_a_broken_regex_is_refused():
    with pytest.raises(pc.ConditionError, match="not a valid regular expression"):
        pc.normalize({"kind": "page_matches", "url": "https://x.test/a", "pattern": "([unclosed"})


def test_a_number_condition_needs_a_capture_group():
    with pytest.raises(pc.ConditionError, match="capture the number"):
        pc.normalize(
            {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp [\d.]+", "op": "below", "value": 1}
        )


def test_a_number_condition_needs_a_direction_and_a_threshold():
    base = {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp ([\d.]+)"}

    with pytest.raises(pc.ConditionError, match="op must be"):
        pc.normalize({**base, "value": 1})
    with pytest.raises(pc.ConditionError, match="numeric value"):
        pc.normalize({**base, "op": "below"})


def test_the_check_interval_has_a_floor():
    """A minute-by-minute condition is a crawler pointed at one site for weeks."""
    spec, _ = pc.normalize({"kind": "page_matches", "url": "https://x.test/a", "pattern": "x", "every_seconds": 5})

    assert spec["every_seconds"] == pc.MIN_INTERVAL_SECONDS


# --- reading numbers off a page ------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8750000", 8750000),
        ("8.750.000", 8750000),
        ("8,750,000", 8750000),
        ("Rp 8.750.000", 8750000),
        ("1 234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("99", 99),
        ("12.5", 12.5),
        ("", None),
        ("free", None),
    ],
)
def test_prices_are_read_the_way_pages_write_them(text, expected):
    assert pc.parse_number(text) == expected


# --- evaluating ---------------------------------------------------------------


async def test_a_time_condition_fires_when_the_time_comes():
    spec = {"kind": "at", "timestamp": NOW}

    assert (await pc.evaluate(spec, step={}, plan={}, now=NOW + 1)).fired is True

    early = await pc.evaluate(spec, step={}, plan={}, now=NOW - 60)
    assert early.fired is False
    assert early.next_check_at == NOW


async def test_a_manual_condition_never_fires_on_its_own():
    verdict = await pc.evaluate({"kind": "manual"}, step={}, plan={}, now=NOW)

    assert verdict.fired is False


async def test_a_page_that_now_says_the_thing_fires(monkeypatch):
    _page(monkeypatch, "This listing is no longer available")

    verdict = await pc.evaluate(
        {"kind": "page_matches", "url": "https://x.test/a", "pattern": "no longer available"},
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is True
    assert "no longer available" in verdict.detail


async def test_absent_waits_for_the_text_to_go_away(monkeypatch):
    spec = {"kind": "page_matches", "url": "https://x.test/a", "pattern": "sold out", "absent": True}

    _page(monkeypatch, "sold out")
    assert (await pc.evaluate(spec, step={}, plan={}, now=NOW)).fired is False

    _page(monkeypatch, "add to cart")
    assert (await pc.evaluate(spec, step={}, plan={}, now=NOW)).fired is True


async def test_a_price_below_the_threshold_fires_with_what_it_saw(monkeypatch):
    _page(monkeypatch, "Best price Rp 8.750.000 today")

    verdict = await pc.evaluate(
        {
            "kind": "page_number",
            "url": "https://x.test/a",
            "pattern": r"Rp ([\d.]+)",
            "op": "below",
            "value": 9_000_000,
        },
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is True
    assert "8,750,000" in verdict.detail, "a price a person can read, not 8.75e+06"


async def test_a_price_still_above_the_threshold_waits(monkeypatch):
    _page(monkeypatch, "Best price Rp 9.500.000 today")

    verdict = await pc.evaluate(
        {
            "kind": "page_number",
            "url": "https://x.test/a",
            "pattern": r"Rp ([\d.]+)",
            "op": "below",
            "value": 9_000_000,
            "every_seconds": 3600,
        },
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is False
    assert verdict.next_check_at == NOW + 3600


async def test_a_site_that_is_down_is_not_a_price_drop(monkeypatch):
    """The one confusion that must never happen."""

    async def broken(url):
        raise RuntimeError("503 from the marketplace")

    monkeypatch.setattr("kronos.tools.acquire.fetch_tiered", broken)

    verdict = await pc.evaluate(
        {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp ([\d.]+)", "op": "below", "value": 1},
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is False
    assert verdict.next_check_at > NOW
    assert any("could not read" in note for note in verdict.notes)


async def test_a_pattern_that_stopped_matching_is_not_a_number_that_fell(monkeypatch):
    """A redesigned page must not read as a price of zero."""
    _page(monkeypatch, "Harga: hubungi penjual")

    verdict = await pc.evaluate(
        {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp ([\d.]+)", "op": "below", "value": 1},
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is False
    assert any("did not match" in note for note in verdict.notes)


async def test_an_unrecognisable_number_does_not_fire(monkeypatch):
    _page(monkeypatch, "Price: Rp lots")

    verdict = await pc.evaluate(
        {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp (\w+)", "op": "below", "value": 1},
        step={},
        plan={},
        now=NOW,
    )

    assert verdict.fired is False
    assert any("could not read a number" in note for note in verdict.notes)


async def test_an_unrecognised_condition_parks_rather_than_spins(monkeypatch):
    verdict = await pc.evaluate({"kind": "from-the-future"}, step={"id": 1}, plan={}, now=NOW)

    assert verdict.fired is False
    assert verdict.next_check_at > NOW + 86400


# --- waiting for the owner ----------------------------------------------------


async def test_a_reply_after_parking_fires(monkeypatch):
    _turns(monkeypatch, [{"started_at": "2026-01-01 12:00:00"}])

    verdict = await pc.evaluate(
        {"kind": "reply"},
        step={"parked_at": _epoch("2026-01-01 11:00:00")},
        plan={"thread_id": "42"},
        now=NOW,
    )

    assert verdict.fired is True


async def test_a_reply_from_before_parking_does_not_count(monkeypatch):
    _turns(monkeypatch, [{"started_at": "2026-01-01 10:00:00"}])

    verdict = await pc.evaluate(
        {"kind": "reply"},
        step={"parked_at": _epoch("2026-01-01 11:00:00")},
        plan={"thread_id": "42"},
        now=NOW,
    )

    assert verdict.fired is False


async def test_a_plan_with_no_chat_behind_it_never_waits_for_a_reply(monkeypatch):
    _turns(monkeypatch, [{"started_at": "2026-01-01 12:00:00"}])

    verdict = await pc.evaluate({"kind": "reply"}, step={"parked_at": 0}, plan={}, now=NOW)

    assert verdict.fired is False


# --- describing ---------------------------------------------------------------


def test_every_condition_can_be_read_by_a_human():
    specs = [
        {"kind": "at", "timestamp": NOW},
        {"kind": "manual", "note": "after the viewing"},
        {"kind": "reply"},
        {"kind": "page_matches", "url": "https://x.test/a", "pattern": "sold"},
        {"kind": "page_number", "url": "https://x.test/a", "pattern": r"Rp ([\d.]+)", "op": "below", "value": 9},
    ]

    for spec in specs:
        assert pc.describe(spec) and "unrecognised" not in pc.describe(spec), spec


def _page(monkeypatch, text: str) -> None:
    async def fake_fetch(url):
        return "plain", text, []

    monkeypatch.setattr("kronos.tools.acquire.fetch_tiered", fake_fetch)


def _turns(monkeypatch, rows: list[dict]) -> None:
    async def fake_list(self, *, thread_id: str = "", limit: int = 5, status: str = ""):
        return rows

    monkeypatch.setattr("kronos.session.SessionStore.list_turns", fake_list)


def _epoch(stamp: str) -> float:
    from datetime import UTC, datetime

    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
