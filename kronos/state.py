"""Agent state definition."""

from typing import Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State for the main Kronos agent."""

    messages: list[BaseMessage]
    user_id: str
    session_id: str
    safety_passed: bool
    loop_detector: Any  # LoopDetector instance, managed by graph nodes
    # Captured when the turn starts; background storage stands down if a memory
    # reset has happened since (see kronos.memory.reset).
    memory_generation: NotRequired[str]
