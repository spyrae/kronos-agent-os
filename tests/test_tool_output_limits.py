"""How much of a tool's output the model sees.

This used to be decided by whether the tool's *name* contained one of twenty
markers. A name is a guess: run_code, plan_status and list_site_accounts matched
nothing and went into the context whole, however large. A size is a fact.
"""

import pytest
from langchain_core.tools import tool

from kronos.engine import (
    GENERAL_OUTPUT_MAX_CHARS,
    MODEL_OUTPUT_MAX_CHARS,
    _tool_model_output,
    compact_tool_output,
    tool_output_limit,
)


@tool
def fetch_something(url: str) -> str:
    """A verbose source — its name carries a marker."""
    return "page"


@tool
def plan_status_like(plan_id: int = 0) -> str:
    """A tool whose name matches no marker at all."""
    return "status"


@tool
def bounded_by_construction() -> str:
    """A tool whose whole output is the point."""
    return "table"


bounded_by_construction.metadata = {"output_max_chars": 0}


def test_a_verbose_source_keeps_the_tight_cap():
    assert tool_output_limit(fetch_something) == MODEL_OUTPUT_MAX_CHARS


def test_a_tool_matching_no_marker_still_has_a_ceiling():
    """The hole this closes: unnamed tools used to be unbounded."""
    assert tool_output_limit(plan_status_like) == GENERAL_OUTPUT_MAX_CHARS


def test_a_tool_can_declare_that_it_must_not_be_cut():
    assert tool_output_limit(bounded_by_construction) == 0


def test_the_real_tools_land_where_intended():
    from kronos.skills.tools import load_skill
    from kronos.tools.acquire import fetch_page
    from kronos.tools.compare import compare_offers

    assert tool_output_limit(fetch_page) == MODEL_OUTPUT_MAX_CHARS, "a fetched page is long by nature"
    assert tool_output_limit(compare_offers) == 0, "half a ranking is a different ranking"
    assert tool_output_limit(load_skill) == 0, "half a procedure reads as a complete one"


@pytest.mark.parametrize("limit", [500, 2400, 8000])
def test_compaction_honours_the_limit_it_was_given(limit):
    compacted = compact_tool_output("y" * 50_000, limit=limit)

    assert "COMPRESSED tool output" in compacted
    assert len(compacted) < limit + 200


def test_short_output_is_left_exactly_as_it_is():
    assert compact_tool_output("just this", limit=100) == "just this"


async def test_output_under_the_ceiling_reaches_the_model_whole():
    model_content, raw = await _tool_model_output(plan_status_like, "a" * 100)

    assert model_content == raw == "a" * 100


async def test_output_over_the_ceiling_is_cut_and_says_so():
    model_content, raw = await _tool_model_output(plan_status_like, "a" * (GENERAL_OUTPUT_MAX_CHARS + 5_000))

    assert "COMPRESSED tool output" in model_content
    assert len(raw) == GENERAL_OUTPUT_MAX_CHARS + 5_000, "the full text is still kept for the audit"


async def test_a_tool_that_declared_no_limit_is_not_cut():
    model_content, raw = await _tool_model_output(bounded_by_construction, "a" * 50_000)

    assert model_content == raw
    assert "COMPRESSED" not in model_content
