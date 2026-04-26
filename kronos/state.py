"""Agent state definition."""

from typing import Any, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State for the main Kronos agent."""

    messages: list[BaseMessage]
    user_id: str
    session_id: str
    safety_passed: bool
    loop_detector: Any  # LoopDetector instance, managed by graph nodes
