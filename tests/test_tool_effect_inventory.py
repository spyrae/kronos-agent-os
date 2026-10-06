"""F10: every built-in tool states whether it changes anything outside the process.

A name heuristic used to decide this. A tool matching no pattern counted as
harmless and bypassed the intent ledger; a read-only tool matching no pattern
had no replay contract, so an interrupted turn using it could not continue.
"""

import pytest

from kronos.config import settings
from kronos.engine import _tool_has_replay_contract, tool_has_side_effect
from kronos.security.effects import SIDE_EFFECT_METADATA_KEY, mark_read_only, mark_side_effect

# The reviewed inventory. A new built-in tool must be added here on purpose.
EFFECTS = {
    "approve_skill",
    "import_skill_from_source",
    "run_code",
    "plan_start",
    "plan_add_step",
    "plan_cancel",
    "mcp_add_server",
    "mcp_remove_server",
    "mcp_reload",
    "create_new_tool",
    "schedule_task",
    "schedule_followup",
    "cancel_scheduled_task",
    "handoff_to_agent",
    "convene_council",
    "ask_agent_memory",
    "browser_click",
    "browser_type",
    "browser_evaluate",
}
READS = {
    "load_skill",
    "load_skill_reference",
    "fetch_page",
    "extract_structured",
    "compare_offers",
    "list_site_accounts",
    "open_site_session",
    "check_site_action",
    "plan_status",
    "repo_list",
    "repo_tree",
    "repo_read",
    "repo_search",
    "repo_history",
    "repo_diff",
    "mcp_list_servers",
    "list_dynamic_tools",
    "session_search",
    "list_scheduled_tasks",
    "browser_navigate",
    "browser_snapshot",
    "browser_screenshot",
}


def _builtin_tools(monkeypatch):
    """Every tool KronosAgent._init_tools can register, with all optional groups on."""
    monkeypatch.setattr(settings, "enable_mcp_gateway_management", True)
    monkeypatch.setattr(settings, "enable_dynamic_tools", True)
    from kronos.skills.tools import approve_skill, import_skill_from_source, load_skill, load_skill_reference
    from kronos.tools.accounts_tools import ACCOUNT_TOOLS
    from kronos.tools.acquire import ACQUIRE_TOOLS
    from kronos.tools.browser.tools import _BROWSER_TOOLS
    from kronos.tools.code import CODE_TOOLS
    from kronos.tools.compare import compare_offers
    from kronos.tools.council import convene_council
    from kronos.tools.dynamic_tools import get_dynamic_management_tools
    from kronos.tools.gateway_tools import get_gateway_tools
    from kronos.tools.handoff import handoff_to_agent
    from kronos.tools.memory_ask import ask_agent_memory
    from kronos.tools.plans_tools import PLAN_TOOLS
    from kronos.tools.reminders import cancel_scheduled_task, list_scheduled_tasks, schedule_followup, schedule_task
    from kronos.tools.repo_tools import REPO_TOOLS
    from kronos.tools.session_search import session_search

    return [
        load_skill,
        load_skill_reference,
        approve_skill,
        import_skill_from_source,
        *ACQUIRE_TOOLS,
        compare_offers,
        *CODE_TOOLS,
        *ACCOUNT_TOOLS,
        *PLAN_TOOLS,
        *REPO_TOOLS,
        *_BROWSER_TOOLS,
        *get_gateway_tools(),
        *get_dynamic_management_tools(),
        session_search,
        schedule_task,
        schedule_followup,
        list_scheduled_tasks,
        cancel_scheduled_task,
        handoff_to_agent,
        convene_council,
        ask_agent_memory,
    ]


def _declared(tool):
    return (tool.metadata or {}).get(SIDE_EFFECT_METADATA_KEY)


def test_no_builtin_tool_is_left_to_a_name_heuristic(monkeypatch):
    undeclared = sorted(tool.name for tool in _builtin_tools(monkeypatch) if _declared(tool) is None)
    assert undeclared == [], f"declare side_effect for: {undeclared}"


def test_the_declarations_match_the_reviewed_inventory(monkeypatch):
    tools = _builtin_tools(monkeypatch)
    assert {tool.name for tool in tools if _declared(tool) is True} == EFFECTS
    assert {tool.name for tool in tools if _declared(tool) is False} == READS
    assert not EFFECTS & READS


def test_the_agent_registers_nothing_outside_the_inventory():
    """_init_tools must not grow a tool group this inventory does not know about."""
    import inspect

    from kronos.graph import KronosAgent

    source = inspect.getsource(KronosAgent._init_tools)
    registered = {
        line.split("import")[0].split("from")[1].strip()
        for line in source.splitlines()
        if " from kronos." in line and " import " in line
    }
    assert registered == {
        "kronos.tools.acquire",
        "kronos.tools.compare",
        "kronos.tools.code",
        "kronos.tools.accounts_tools",
        "kronos.tools.plans_tools",
        "kronos.repos",
        "kronos.tools.repo_tools",
        "kronos.tools.browser.tools",
        "kronos.tools.gateway_tools",
        "kronos.tools.dynamic",
        "kronos.tools.dynamic_tools",
        "kronos.tools.session_search",
        "kronos.tools.reminders",
        "kronos.tools.council",
        "kronos.tools.handoff",
        "kronos.tools.memory_ask",
        "kronos.tools.composio_integration",
    }


def test_every_builtin_tool_now_has_a_replay_contract(monkeypatch):
    """Effects are protected by the ledger; reads may simply run again."""
    for tool in _builtin_tools(monkeypatch):
        assert _tool_has_replay_contract(tool), tool.name
        assert tool_has_side_effect(tool) is (tool.name in EFFECTS), tool.name


def test_generated_tools_are_treated_as_effects():
    from kronos.tools.dynamic import _build_dynamic_tool, _extract_function_spec

    code = 'def shout(text: str) -> str:\n    """Upper-case text."""\n    return text.upper()\n'
    tool = _build_dynamic_tool("shout", code, _extract_function_spec(code, "Dynamic tool: shout"))
    assert _declared(tool) is True and tool_has_side_effect(tool)


def test_an_explicit_declaration_overrides_a_misleading_name():
    from langchain_core.tools import StructuredTool

    def _noop() -> str:
        return ""

    looks_safe = StructuredTool.from_function(func=_noop, name="get_and_wire_money", description="x")
    looks_risky = StructuredTool.from_function(func=_noop, name="delete_preview", description="x")
    assert not tool_has_side_effect(looks_safe) and tool_has_side_effect(looks_risky)

    mark_side_effect([looks_safe])
    mark_read_only([looks_risky])
    assert tool_has_side_effect(looks_safe) and not tool_has_side_effect(looks_risky)


@pytest.mark.parametrize("marker", [mark_side_effect, mark_read_only])
def test_a_tool_that_rejects_metadata_is_reported_not_silently_accepted(marker):
    class Frozen:
        name = "frozen"

        def __setattr__(self, key, value):
            raise AttributeError("read-only object")

    assert marker([Frozen()]) == []


def test_third_party_actions_are_classified_without_their_app_prefix():
    from langchain_core.tools import StructuredTool

    from kronos.security.untrusted import UNTRUSTED_METADATA_KEY
    from kronos.tools.composio_integration import classify_composio_tools

    def _noop() -> str:
        return ""

    names = ["GITHUB_GET_ISSUE", "SLACK_LIST_CHANNELS", "SLACK_SEND_MESSAGE", "GMAIL_CREATE_DRAFT", "NOTION_FROBNICATE"]
    tools = classify_composio_tools([StructuredTool.from_function(func=_noop, name=n, description="x") for n in names])

    effects = {tool.name: tool.metadata["side_effect"] for tool in tools}
    assert effects == {
        "GITHUB_GET_ISSUE": False,
        "SLACK_LIST_CHANNELS": False,
        "SLACK_SEND_MESSAGE": True,
        "GMAIL_CREATE_DRAFT": True,
        "NOTION_FROBNICATE": True,  # unrecognised is an effect, not a read
    }
    assert all(tool.metadata["needs_approval"] is tool.metadata["side_effect"] for tool in tools)
    assert all(tool.metadata[UNTRUSTED_METADATA_KEY] for tool in tools)
    assert all(_tool_has_replay_contract(tool) for tool in tools)
