"""Tests for Deep Research agent."""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from kronos.agents.deep_research.graph import create_deep_research_agent


class TestDeepResearchGraph:
    def test_graph_builds(self):
        """Factory returns an async callable — the pipeline runner.

        Historical: used to return a LangGraph StateGraph with ``.compile()``.
        After the custom-engine migration, ``create_deep_research_agent``
        returns an async function directly.
        """
        import inspect

        from langchain_core.tools import tool

        @tool
        def brave_search(query: str) -> str:
            """Search the web."""
            return "results"

        agent = create_deep_research_agent([brave_search])
        assert callable(agent)
        assert inspect.iscoroutinefunction(agent)

    def test_classify_mode_topic(self):
        from kronos.agents.deep_research.nodes import classify_mode

        with patch("kronos.agents.deep_research.nodes.get_model") as mock:
            mock_model = MagicMock()
            mock_model.invoke.return_value = AIMessage(content="topic")
            mock.return_value = mock_model

            state = {
                "messages": [HumanMessage(content="Расскажи про LangGraph")],
                "topic": "",
                "mode": "topic",
                "user_id": "test",
                "search_queries": [],
                "search_results": [],
                "iteration": 0,
                "report": "",
                "quality_score": 0,
            }
            result = classify_mode(state)
            assert result["mode"] == "topic"
            assert result["topic"] == "Расскажи про LangGraph"

    def test_classify_mode_validation(self):
        from kronos.agents.deep_research.nodes import classify_mode

        with patch("kronos.agents.deep_research.nodes.get_model") as mock:
            mock_model = MagicMock()
            mock_model.invoke.return_value = AIMessage(content="validation")
            mock.return_value = mock_model

            state = {
                "messages": [HumanMessage(content="Проверь идею — сервис для AI агентов")],
                "topic": "",
                "mode": "topic",
                "user_id": "test",
                "search_queries": [],
                "search_results": [],
                "iteration": 0,
                "report": "",
                "quality_score": 0,
            }
            result = classify_mode(state)
            assert result["mode"] == "validation"

    def test_evaluate_quality_sufficient(self):
        from kronos.agents.deep_research.nodes import evaluate_quality

        state = {
            "messages": [],
            "search_results": [
                {"query": "q1", "source": "brave", "content": "x" * 6000, "url": ""},
                {"query": "q2", "source": "exa", "content": "y" * 5000, "url": ""},
            ],
            "iteration": 1,
            "topic": "test",
            "mode": "topic",
            "user_id": "test",
            "search_queries": [],
            "report": "",
            "quality_score": 0,
        }
        result = evaluate_quality(state)
        assert result["quality_score"] >= 60

    def test_evaluate_quality_insufficient(self):
        from kronos.agents.deep_research.nodes import evaluate_quality

        state = {
            "messages": [],
            "search_results": [
                {"query": "q1", "source": "brave", "content": "short", "url": ""},
            ],
            "iteration": 1,
            "topic": "test",
            "mode": "topic",
            "user_id": "test",
            "search_queries": [],
            "report": "",
            "quality_score": 0,
        }
        result = evaluate_quality(state)
        assert result["quality_score"] < 60

    def test_should_search_more(self):
        from kronos.agents.deep_research.nodes import should_search_more

        assert should_search_more({"quality_score": 80, "iteration": 1}) == "synthesize"
        assert should_search_more({"quality_score": 30, "iteration": 1}) == "plan_more_queries"
        assert should_search_more({"quality_score": 30, "iteration": 2}) == "synthesize"  # max iterations
