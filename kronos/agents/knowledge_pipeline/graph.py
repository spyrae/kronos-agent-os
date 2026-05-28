"""Knowledge Pipeline agent — file-handoff workflow for incoming knowledge.

Pipeline:
  record → process claims → connect wiki links → verify → sync to memory

No LangGraph dependency is added to the public runtime; this follows the same
plain async sub-agent pattern as the other KAOS pipelines while keeping the
state handoff durable in task files.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from kronos.agents.knowledge_pipeline.nodes import run_pipeline
from kronos.agents.knowledge_pipeline.queue import KnowledgeQueue
from kronos.engine import AgentResult

log = logging.getLogger("kronos.agents.knowledge_pipeline")


def create_knowledge_pipeline_agent(
    *,
    queue: KnowledgeQueue | None = None,
    sync_memory: bool = True,
    memory_user_id: str = "knowledge-pipeline",
):
    """Create an async sub-agent that records and processes knowledge."""
    knowledge_queue = queue or KnowledgeQueue()

    async def run(messages: list[BaseMessage]) -> AgentResult:
        """Record the last user message and run the file-backed pipeline."""
        content = _last_human_content(messages)
        if not content:
            return AgentResult(messages=list(messages), content="Нет входящего знания для обработки.")

        task = knowledge_queue.record_source("user-message", content, metadata={"agent": "knowledge_pipeline"})
        final_task = run_pipeline(
            knowledge_queue,
            task,
            sync_memory=sync_memory,
            memory_user_id=memory_user_id,
        )
        summary = _format_summary(final_task)
        return AgentResult(
            messages=list(messages) + [AIMessage(content=summary)],
            content=summary,
        )

    run.__name__ = "knowledge_pipeline_agent"
    run.__qualname__ = "knowledge_pipeline_agent"
    log.info("Knowledge Pipeline agent created")
    return run


def _last_human_content(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def _format_summary(task: dict) -> str:
    verification = task.get("verification", {})
    memory = task.get("memory", {})
    return (
        f"Knowledge task {task['task_id']} → {task.get('state', 'unknown')}: "
        f"{verification.get('claims', len(task.get('claims', [])))} claims, "
        f"{verification.get('links', len(task.get('links', [])))} links, "
        f"memory={memory.get('status', 'not_started')}."
    )
