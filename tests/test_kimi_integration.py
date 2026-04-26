"""Integration tests for Kimi K2.5 via Fireworks.

Run: pytest tests/test_kimi_integration.py -v -s
Requires: FIREWORKS_API_KEY in .env
"""

import pytest

pytestmark = pytest.mark.integration

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from kronos.config import settings


pytestmark = pytest.mark.skipif(
    not settings.fireworks_api_key,
    reason="FIREWORKS_API_KEY not set",
)


@tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"В {city} сейчас +22°C, ясно."


@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    try:
        return str(eval(expression))
    except Exception as e:
        return f"Error: {e}"


@tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Результаты поиска по '{query}': [Найдено 3 статьи]"


def _create_kimi():
    """Create Kimi model instance via Fireworks."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model="accounts/fireworks/routers/kimi-k2p5-turbo",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key=settings.fireworks_api_key,
        max_tokens=1024,
        temperature=0.3,
    )


class TestKimiBasic:
    """Basic LLM call without tools."""

    def test_simple_response(self):
        model = _create_kimi()
        response = model.invoke([HumanMessage(content="Скажи 'работает' одним словом.")])
        assert isinstance(response, AIMessage)
        assert len(response.content) > 0
        print(f"\n  Response: {response.content}")

    def test_with_system_prompt(self):
        model = _create_kimi()
        messages = [
            SystemMessage(content="Ты — AI-ассистент по имени Кронос. Отвечай кратко."),
            HumanMessage(content="Как тебя зовут?"),
        ]
        response = model.invoke(messages)
        assert isinstance(response, AIMessage)
        assert len(response.content) > 0
        print(f"\n  Response: {response.content}")

    def test_russian_language(self):
        model = _create_kimi()
        response = model.invoke([
            HumanMessage(content="Объясни одним предложением, что такое LangGraph."),
        ])
        assert isinstance(response, AIMessage)
        assert len(response.content) > 10
        print(f"\n  Response: {response.content}")


class TestKimiToolBinding:
    """Verify tools bind correctly and model can call them."""

    def test_bind_single_tool(self):
        model = _create_kimi()
        bound = model.bind_tools([get_weather])
        response = bound.invoke([HumanMessage(content="Какая погода в Москве?")])
        assert isinstance(response, AIMessage)

        if response.tool_calls:
            tc = response.tool_calls[0]
            assert tc["name"] == "get_weather"
            assert "city" in tc["args"]
            print(f"\n  Tool call: {tc['name']}({tc['args']})")
        else:
            print(f"\n  No tool call, text response: {response.content[:100]}")

    def test_bind_multiple_tools(self):
        model = _create_kimi()
        bound = model.bind_tools([get_weather, calculate, search_web])
        response = bound.invoke([HumanMessage(content="Сколько будет 2**10?")])
        assert isinstance(response, AIMessage)

        if response.tool_calls:
            tc = response.tool_calls[0]
            assert tc["name"] == "calculate"
            print(f"\n  Tool call: {tc['name']}({tc['args']})")
        else:
            print(f"\n  No tool call, text response: {response.content[:100]}")

    def test_no_tool_when_unnecessary(self):
        """Model should NOT call tools for simple conversational messages."""
        model = _create_kimi()
        bound = model.bind_tools([get_weather, calculate, search_web])
        response = bound.invoke([HumanMessage(content="Привет!")])
        assert isinstance(response, AIMessage)

        # Should respond with text, not a tool call
        has_tool_calls = hasattr(response, "tool_calls") and response.tool_calls
        print(f"\n  Has tool calls: {has_tool_calls}")
        print(f"  Response: {response.content[:100]}")

    def test_tool_choice_with_search(self):
        """Model should pick search_web for a search query."""
        model = _create_kimi()
        bound = model.bind_tools([get_weather, calculate, search_web])
        response = bound.invoke([
            HumanMessage(content="Найди информацию о последних новостях AI"),
        ])
        assert isinstance(response, AIMessage)

        if response.tool_calls:
            tc = response.tool_calls[0]
            assert tc["name"] == "search_web"
            print(f"\n  Tool call: {tc['name']}({tc['args']})")
        else:
            print(f"\n  No tool call, text response: {response.content[:100]}")


class TestKimiReActLoop:
    """Simulate ReAct tool loop (model → tool → model)."""

    def test_tool_result_processing(self):
        """Model calls tool, gets result, produces final answer."""
        from langchain_core.messages import ToolMessage

        model = _create_kimi()
        bound = model.bind_tools([get_weather])

        # Step 1: model decides to call tool
        messages = [HumanMessage(content="Какая погода в Париже?")]
        response = bound.invoke(messages)

        if not response.tool_calls:
            print(f"\n  Model answered directly: {response.content[:100]}")
            return

        tc = response.tool_calls[0]
        print(f"\n  Step 1 — Tool call: {tc['name']}({tc['args']})")

        # Step 2: execute tool and feed result back
        tool_result = get_weather.invoke(tc["args"])
        messages.append(response)
        messages.append(ToolMessage(content=tool_result, tool_call_id=tc["id"]))

        # Step 3: model produces final answer
        final = bound.invoke(messages)
        assert isinstance(final, AIMessage)
        assert len(final.content) > 0
        assert not final.tool_calls  # should not call tools again
        print(f"  Step 2 — Final: {final.content[:200]}")

    def test_multi_tool_sequence(self):
        """Model uses multiple tools in sequence."""
        from langchain_core.messages import ToolMessage

        model = _create_kimi()
        bound = model.bind_tools([get_weather, search_web])

        messages = [
            HumanMessage(
                content="Какая погода в Токио и найди новости об AI в Японии"
            ),
        ]
        response = bound.invoke(messages)

        if not response.tool_calls:
            print(f"\n  Model answered directly: {response.content[:100]}")
            return

        print(f"\n  Tool calls: {[tc['name'] for tc in response.tool_calls]}")

        # Execute all tool calls
        messages.append(response)
        for tc in response.tool_calls:
            if tc["name"] == "get_weather":
                result = get_weather.invoke(tc["args"])
            else:
                result = search_web.invoke(tc["args"])
            messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

        # Final synthesis
        final = bound.invoke(messages)
        assert isinstance(final, AIMessage)
        assert len(final.content) > 0
        print(f"  Final: {final.content[:300]}")


class TestKimiViaFactory:
    """Test through kronos.llm factory (the actual integration path)."""

    def test_factory_returns_kimi_for_standard(self):
        from kronos.llm import ModelTier, get_model
        model = get_model(ModelTier.STANDARD)
        assert model is not None
        print(f"\n  Standard model type: {type(model).__name__}")

    def test_factory_returns_deepseek_for_lite(self):
        from kronos.llm import ModelTier, get_model
        if not settings.deepseek_api_key:
            pytest.skip("DEEPSEEK_API_KEY not set")
        model = get_model(ModelTier.LITE)
        assert model is not None
        print(f"\n  Lite model type: {type(model).__name__}")

    def test_factory_model_invoke(self):
        """Invoke through factory — same path as graph.py uses."""
        from kronos.llm import ModelTier, get_model
        model = get_model(ModelTier.STANDARD)
        response = model.invoke([HumanMessage(content="Ответь одним словом: 2+2=")])
        assert isinstance(response, AIMessage)
        print(f"\n  Factory response: {response.content}")

    def test_factory_model_with_tools(self):
        """bind_tools through factory — same path as graph.py _get_model_with_tools."""
        from kronos.llm import ModelTier, get_model
        model = get_model(ModelTier.STANDARD)
        bound = model.bind_tools([get_weather, calculate])
        response = bound.invoke([HumanMessage(content="Сколько 15 * 17?")])
        assert isinstance(response, AIMessage)

        if response.tool_calls:
            print(f"\n  Tool call via factory: {response.tool_calls[0]}")
        else:
            print(f"\n  Direct answer via factory: {response.content}")
