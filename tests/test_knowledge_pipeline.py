from langchain_core.messages import HumanMessage

from kronos.agents.knowledge_pipeline.graph import create_knowledge_pipeline_agent
from kronos.agents.knowledge_pipeline.nodes import run_pipeline
from kronos.agents.knowledge_pipeline.queue import KnowledgeQueue
from kronos.workspace import Workspace


def test_knowledge_queue_records_inbox_and_task_file(tmp_path):
    workspace = Workspace(tmp_path)
    queue = KnowledgeQueue(workspace)

    task = queue.record_source(
        "research",
        "Kronos Agent OS uses Mem0 for durable memory.",
        metadata={"email": "demo@example.com"},
    )

    inbox = workspace.root / task["inbox_path"]
    task_path = queue.task_path(task["task_id"])

    assert inbox.exists()
    assert task_path.exists()
    assert task["state"] == "recorded"
    assert task["source"]["metadata"]["email"] == "***@***.com"
    assert "Kronos Agent OS uses Mem0" in inbox.read_text(encoding="utf-8")


def test_knowledge_pipeline_uses_task_file_handoff(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    queue = KnowledgeQueue(workspace)
    task = queue.record_source(
        "news-monitor",
        "Kronos Agent OS uses Mem0 for durable memory. Mem0 links facts to KAOS.",
    )

    monkeypatch.setattr(
        "kronos.agents.knowledge_pipeline.nodes.add_memories",
        lambda messages, user_id, session_id=None: ["Kronos Agent OS uses Mem0 for durable memory."],
        raising=False,
    )

    final = run_pipeline(queue, task, sync_memory=False)
    reloaded = queue.load_task(final["task_id"])

    assert reloaded["state"] == "verified"
    assert reloaded["phases"]["process"]["status"] == "completed"
    assert reloaded["phases"]["connect"]["status"] == "completed"
    assert reloaded["phases"]["verify"]["status"] == "completed"
    assert reloaded["claims"][0]["text"] == "Kronos Agent OS uses Mem0 for durable memory."
    assert {"target": "Kronos Agent OS", "wiki": "[[Kronos Agent OS]]", "type": "entity"} in reloaded["links"]


async def test_knowledge_pipeline_agent_returns_summary(tmp_path):
    queue = KnowledgeQueue(Workspace(tmp_path))
    agent = create_knowledge_pipeline_agent(queue=queue, sync_memory=False)

    result = await agent([HumanMessage(content="Mem0 stores facts. Kronos Agent OS keeps task files.")])

    assert "Knowledge task" in result.content
    assert "claims" in result.content
    assert queue.list_tasks(include_final=True)[0]["verification"]["claims"] == 2
