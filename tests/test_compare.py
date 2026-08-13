"""Comparing offers — the three refusals are the feature.

A missing deposit is not a zero deposit; two currencies do not add up; a one-off
cost is not a monthly one. Each of those, got wrong, produces a number that
looks right and is not, which is worse than no comparison at all.
"""

import json

import pytest

from kronos.tools.compare import CompareError, compare, compare_offers, render


def _bali():
    return [
        {"name": "Villa A", "price": 800, "utilities": 90, "deposit": 1600, "rating": 4.8},
        {"name": "Villa B", "price": 700, "utilities": 150, "deposit": 2100, "rating": 4.4},
    ]


# --- the arithmetic -----------------------------------------------------------


def test_recurring_costs_are_multiplied_and_one_offs_are_not():
    result = compare(_bali(), recurring=["price", "utilities"], one_off=["deposit"], periods=3)

    by_name = {row["name"]: row for row in result.ranked}
    assert by_name["Villa A"]["total"] == 800 * 3 + 90 * 3 + 1600
    assert by_name["Villa B"]["total"] == 700 * 3 + 150 * 3 + 2100


def test_the_cheapest_over_the_whole_stay_is_not_the_cheapest_month():
    """The point of the function: B is cheaper per month and dearer to live in."""
    result = compare(_bali(), recurring=["price", "utilities"], one_off=["deposit"], periods=3)

    assert result.ranked[0]["name"] == "Villa A"
    assert result.ranked[1]["over_cheapest"] == 380


def test_the_breakdown_is_kept_so_the_total_can_be_checked():
    result = compare(_bali(), recurring=["price"], one_off=["deposit"], periods=2)

    assert result.ranked[0]["name"] == "Villa A"
    assert result.ranked[0]["parts"] == {"price": 1600, "deposit": 1600}


def test_prices_written_the_way_pages_write_them_are_read():
    offers = [
        {"name": "Toko", "price": "Rp 8.750.000", "shipping": "Rp 50.000"},
        {"name": "Shopee", "price": "Rp 8.900.000", "shipping": "0"},
    ]

    result = compare(offers, recurring=["price"], one_off=["shipping"])

    assert result.ranked[0]["total"] == 8_800_000


def test_fields_nobody_named_are_carried_through_untouched():
    """Rating and distance are for the owner to weigh, not for this to score."""
    result = compare(_bali(), recurring=["price"], one_off=["deposit"])

    assert result.ranked[0]["other"]["rating"] in (4.8, 4.4)
    assert "utilities" in result.ranked[0]["other"], "an unnamed cost is data, not silently added"


# --- the refusals -------------------------------------------------------------


def test_a_missing_cost_takes_the_offer_out_of_the_ranking():
    """Treating it as zero would make the least documented offer look cheapest."""
    offers = _bali() + [{"name": "Villa C", "price": 500, "utilities": 60}]

    result = compare(offers, recurring=["price", "utilities"], one_off=["deposit"], periods=3)

    assert [row["name"] for row in result.ranked] == ["Villa A", "Villa B"]
    assert result.incomplete[0]["name"] == "Villa C"
    assert result.incomplete[0]["missing"] == ["deposit not stated"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "deposit empty"),
        ("", "deposit empty"),
        ("по договорённости", "deposit unreadable"),
    ],
)
def test_an_unusable_value_is_reported_as_what_it_is(value, expected):
    offers = [{"name": "Villa", "price": 800, "deposit": value}]

    result = compare(offers, recurring=["price"], one_off=["deposit"])

    assert result.ranked == []
    assert expected in result.incomplete[0]["missing"][0]


def test_two_currencies_are_refused_rather_than_added():
    offers = [
        {"name": "Local", "currency": "IDR", "price": 8_750_000},
        {"name": "Import", "currency": "USD", "price": 560},
    ]

    with pytest.raises(CompareError, match="convert them to one currency"):
        compare(offers, recurring=["price"])


def test_one_currency_stated_once_is_fine():
    offers = [{"name": "A", "currency": "USD", "price": 10}, {"name": "B", "price": 12}]

    result = compare(offers, recurring=["price"])

    assert result.currency == "USD"
    assert result.ranked[0]["name"] == "A"


def test_comparing_nothing_is_an_error():
    with pytest.raises(CompareError, match="no offers"):
        compare([], recurring=["price"])


def test_comparing_without_naming_a_cost_is_an_error():
    with pytest.raises(CompareError, match="at least one cost field"):
        compare(_bali())


def test_a_silly_number_of_offers_is_refused():
    with pytest.raises(CompareError, match="more than this compares"):
        compare([{"name": str(i), "price": 1} for i in range(200)], recurring=["price"])


def test_periods_below_one_is_an_error():
    with pytest.raises(CompareError, match="at least 1"):
        compare(_bali(), recurring=["price"], periods=0)


def test_an_offer_that_is_not_an_object_is_an_error():
    with pytest.raises(CompareError, match="not an object"):
        compare([{"name": "A", "price": 1}, "Villa B"], recurring=["price"])


def test_an_offer_with_no_name_still_gets_one():
    result = compare([{"price": 1}], recurring=["price"])

    assert result.ranked[0]["name"] == "offer 1"


# --- what the model reads -----------------------------------------------------


def test_the_rendered_comparison_shows_the_sum_it_made():
    result = compare(_bali(), recurring=["price"], one_off=["deposit"], periods=2)

    text = render(result)

    assert "price 1,600 + deposit 1,600" in text
    assert "over 2 periods" in text


def test_the_rendered_comparison_says_it_is_not_a_recommendation():
    """Otherwise the model reports "the function chose Villa A"."""
    text = render(compare(_bali(), recurring=["price"]))

    assert "arithmetic only" in text
    assert "yourself" in text


def test_the_unranked_offers_are_named_with_the_reason():
    offers = _bali() + [{"name": "Villa C", "price": 500}]

    text = render(compare(offers, recurring=["price"], one_off=["deposit"]))

    assert "Villa C: deposit not stated" in text
    assert "missing figure is not a zero one" in text


# --- the tool -----------------------------------------------------------------


async def test_the_tool_compares_a_json_array():
    out = await compare_offers.ainvoke(
        {"offers": json.dumps(_bali()), "recurring": "price,utilities", "one_off": "deposit", "periods": 3}
    )

    assert "Villa A" in out
    assert out.index("Villa A") < out.index("Villa B")


async def test_the_tool_explains_bad_json_instead_of_raising():
    out = await compare_offers.ainvoke({"offers": "{not json", "recurring": "price"})

    assert out.startswith("[ERROR]")
    assert "JSON" in out


async def test_the_tool_refuses_a_json_object():
    out = await compare_offers.ainvoke({"offers": '{"name":"A"}', "recurring": "price"})

    assert out.startswith("[ERROR]")


async def test_the_tool_passes_the_refusal_through():
    offers = json.dumps([{"name": "A", "currency": "USD", "price": 1}, {"name": "B", "currency": "EUR", "price": 1}])

    out = await compare_offers.ainvoke({"offers": offers, "recurring": "price"})

    assert out.startswith("[ERROR]")
    assert "one currency" in out


async def test_cost_fields_can_be_separated_by_semicolons_too():
    out = await compare_offers.ainvoke({"offers": json.dumps(_bali()), "recurring": "price; utilities"})

    assert "utilities" in out
