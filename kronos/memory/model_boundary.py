"""Per-operation model boundary for Mem0's OpenAI-compatible sync adapter."""

import json
from contextvars import copy_context
from copy import copy
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import convert_to_messages

from kronos.execution_control import check_execution
from kronos.llm import ModelTier, get_model
from kronos.security.direct_model import record_direct_response
from kronos.security.model_budget import admit_model_call


def _lite_completion(request: dict[str, Any]) -> SimpleNamespace:
    model = get_model(ModelTier.LITE)
    if request.get("tools"):
        options = {"tool_choice": request["tool_choice"]} if request.get("tool_choice") else {}
        model = model.bind_tools(request["tools"], **options)
    options = {key: value for key, value in request.items() if key not in {"model", "messages", "tools", "tool_choice"}}
    reply = model.invoke(convert_to_messages(request["messages"]), **options)
    if not isinstance(reply.content, str) or getattr(reply, "invalid_tool_calls", None):
        raise RuntimeError("Invalid lite memory model response")
    # Mem0's DeepSeek parser reads these fields; usage has already been recorded
    # by the factory callback, so do not add a second direct-model charge.
    calls = [
        SimpleNamespace(function=SimpleNamespace(name=call["name"], arguments=json.dumps(call["args"])))
        for call in reply.tool_calls
    ]
    message = SimpleNamespace(content=reply.content, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _BudgetedCompletions:
    def __init__(self, create) -> None:
        self._create = create

    def create(self, **request):
        if admit_model_call():
            return _lite_completion(request)
        response = self._create(**request)
        record_direct_response(
            model=request["model"],
            response=response,
            input_content=request,
            output_content=getattr(response, "choices", []),
        )
        return response


class ScopedMemoryLlm:
    """Capture one operation's context, including Mem0's private worker threads.

    Copy the adapter, not its SDK client or configuration. The only substituted
    capability is chat.completions.create; parsing and provider options stay with
    the existing Mem0 adapter. Each generate call enters a fresh Context copy so
    concurrent internal workers never enter the same Context simultaneously.
    """

    def __init__(self, model) -> None:
        create = getattr(getattr(getattr(getattr(model, "client", None), "chat", None), "completions", None), "create", None)
        if not callable(create) or not callable(getattr(model, "generate_response", None)):
            raise RuntimeError("Mem0 adapter has no supported model budget boundary")
        self._context = copy_context()
        self._model = copy(model)
        self._model.client = SimpleNamespace(chat=SimpleNamespace(completions=_BudgetedCompletions(create)))

    def generate_response(self, *args, **kwargs):
        """Run Mem0 parsing and dispatch under the original operation's scope."""
        return self._context.copy().run(self._model.generate_response, *args, **kwargs)


class BudgetedMemory:
    """Share storage resources, never caller-specific LLM state, across requests.

    Expose only the three operations used by KAOS. New operations, graph stores
    or rerankers require their own boundary instead of silently bypassing it.
    """

    def __init__(self, memory) -> None:
        if getattr(memory, "enable_graph", False) or getattr(memory, "reranker", None) is not None:
            raise RuntimeError("Mem0 graph/reranker has no supported model budget boundary")
        ScopedMemoryLlm(memory.llm)
        self._memory = memory

    def _call(self, operation: str, *args, **kwargs):
        check_execution()
        scoped = copy(self._memory)
        scoped.llm = ScopedMemoryLlm(self._memory.llm)
        result = getattr(scoped, operation)(*args, **kwargs)
        check_execution()
        return result

    def add(self, *args, **kwargs):
        """Extract/store facts without losing the original model scope."""
        return self._call("add", *args, **kwargs)

    def search(self, *args, **kwargs):
        """Keep local vector reads available even when model spend is blocked."""
        return self._call("search", *args, **kwargs)

    def get_all(self, *args, **kwargs):
        """Read stored facts without introducing a model request."""
        return self._call("get_all", *args, **kwargs)
