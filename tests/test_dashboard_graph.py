"""The graph API reflects the live supervisor, not a hardcoded stale list.

Regression guard: /structure and /mermaid used to hardcode an agent list that
drifted from reality (missing agents, ignored registry toggles).
"""

import pytest


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _Supervisor:
    def __init__(self, names: list[str]):
        # delegate_to_* plus a non-delegation tool that must be ignored
        self._approval_tools = [_Tool(f"delegate_to_{n}") for n in names] + [_Tool("add_expense")]


class _Agent:
    def __init__(self, names: list[str]):
        self._supervisor = _Supervisor(names)


@pytest.fixture(autouse=True)
def _reset_agent():
    from dashboard.api import graph as g

    yield
    g.set_agent(None)  # never leak module state to other tests


def test_live_agent_names_filters_delegation_tools():
    from dashboard.api import graph as g

    g.set_agent(_Agent(["research", "finance"]))
    assert g._live_agent_names() == ["research", "finance"]  # add_expense excluded


async def test_structure_reflects_live_agents():
    from dashboard.api import graph as g

    g.set_agent(_Agent(["deep_research", "server_ops"]))
    result = await g.get_structure()
    ids = {n["id"] for n in result["nodes"]}

    assert {"deep_research", "server_ops", "supervisor", "store_memories"} <= ids
    assert "finance" not in ids  # a stale/absent agent is not invented
    # every agent is wired supervisor -> agent -> store_memories
    edge_pairs = {(e["source"], e["target"]) for e in result["edges"]}
    assert ("supervisor", "deep_research") in edge_pairs
    assert ("server_ops", "store_memories") in edge_pairs


async def test_structure_empty_without_agent():
    from dashboard.api import graph as g

    g.set_agent(None)
    assert await g.get_structure() == {"nodes": [], "edges": []}


async def test_mermaid_lists_live_agents():
    from dashboard.api import graph as g

    g.set_agent(_Agent(["research"]))
    mermaid = (await g.get_mermaid())["mermaid"]
    assert "supervisor -->|research| research" in mermaid
    assert "telegram_channels" not in mermaid  # no hardcoded stale agent
