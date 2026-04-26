"""Tests for memory system."""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from kronos.memory.compaction import should_compact
from kronos.memory.nodes import retrieve_memories


class TestRetrieveMemories:
    def test_returns_empty_when_no_user_id(self):
        state = {"messages": [HumanMessage(content="test")], "user_id": ""}
        result = retrieve_memories(state)
        assert result == {}

    def test_returns_empty_when_no_messages(self):
        state = {"messages": [], "user_id": "test"}
        result = retrieve_memories(state)
        assert result == {}

    @patch("kronos.memory.nodes.search_memories")
    def test_injects_memories_as_system_message(self, mock_search):
        mock_search.return_value = ["User likes Python", "User is ESTJ"]
        state = {
            "messages": [HumanMessage(content="Расскажи о моих предпочтениях")],
            "user_id": "test-user",
        }
        result = retrieve_memories(state)
        assert "messages" in result
        assert "Python" in result["messages"][0].content
        assert "ESTJ" in result["messages"][0].content

    @patch("kronos.memory.nodes.search_memories")
    def test_returns_empty_when_no_memories_found(self, mock_search):
        mock_search.return_value = []
        state = {
            "messages": [HumanMessage(content="test")],
            "user_id": "test-user",
        }
        result = retrieve_memories(state)
        assert result == {}


class TestCompaction:
    def test_should_compact_when_many_messages(self):
        messages = [HumanMessage(content=f"msg {i}") for i in range(35)]
        assert should_compact({"messages": messages}) is True

    def test_should_not_compact_when_few_messages(self):
        messages = [HumanMessage(content=f"msg {i}") for i in range(5)]
        assert should_compact({"messages": messages}) is False

    def test_should_not_compact_empty(self):
        assert should_compact({"messages": []}) is False
