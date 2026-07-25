"""Chat-model proxy that records or replays provider calls.

In ``record`` mode the real model is called and the response is stored. In
``replay`` mode no provider is touched at all — which is the point: eval runs
need no API keys, no network, and give the same answer every time.

A replay miss raises ``CassetteMissError`` instead of quietly falling through to a
live call. A silent fallback would turn a deterministic suite into one that
sometimes costs money and sometimes changes its verdict.
"""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from kronos.cassettes.store import (
    KIND_LLM,
    CassetteStore,
    deserialize_ai_message,
    llm_key,
    serialize_ai_message,
)

log = logging.getLogger("kronos.cassettes.model")


class CassetteMissError(RuntimeError):
    """Raised when replay mode has no cassette for a call."""


class CassetteChatModel:
    """Proxy around a chat model (or nothing at all, in replay mode)."""

    def __init__(
        self,
        inner: Any,
        *,
        label: str,
        mode: str,
        store: CassetteStore,
        tools: list | None = None,
        model_name: str = "",
    ):
        self._inner = inner
        self._label = label
        self._mode = mode
        self._store = store
        self._tools = list(tools or [])
        self._model_name = model_name or _inner_model_name(inner)

    # ------------------------------------------------------------------ plumbing

    def bind_tools(self, tools: list) -> "CassetteChatModel":
        """Bind tools on the inner model and keep them in the cassette key."""
        inner = self._inner.bind_tools(tools) if self._inner is not None else None
        return CassetteChatModel(
            inner,
            label=self._label,
            mode=self._mode,
            store=self._store,
            tools=tools,
            model_name=self._model_name,
        )

    def __getattr__(self, name: str) -> Any:
        """Delegate anything we do not intercept to the wrapped model."""
        if self._inner is None:
            raise AttributeError(f"replay-mode cassette model has no attribute '{name}'")
        return getattr(self._inner, name)

    @property
    def model_name(self) -> str:
        return self._model_name

    # -------------------------------------------------------------------- invoke

    def _key(self, messages: list[BaseMessage]) -> str:
        return llm_key(messages=messages, tools=self._tools, label=self._label)

    def _replay(self, key: str) -> AIMessage:
        payload = self._store.read(KIND_LLM, key)
        if payload is None:
            raise CassetteMissError(
                f"no cassette for label={self._label} model={self._model_name} key={key}. "
                "Record it first: KAOS_CASSETTE_MODE=record with real provider keys."
            )
        return deserialize_ai_message(payload.get("response") or {})

    def _record(self, key: str, messages: list[BaseMessage], response: Any) -> None:
        if not isinstance(response, AIMessage):
            log.debug("Not recording non-AIMessage response for label=%s", self._label)
            return
        self._store.write(
            KIND_LLM,
            key,
            {
                "label": self._label,
                "model": self._model_name,
                "message_count": len(messages),
                "tools": [getattr(tool, "name", str(tool)) for tool in self._tools],
                "response": serialize_ai_message(response),
            },
        )

    async def ainvoke(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        key = self._key(messages)
        if self._mode == "replay":
            return self._replay(key)
        response = await self._inner.ainvoke(messages, *args, **kwargs)
        if self._mode == "record":
            self._record(key, messages, response)
        return response

    def invoke(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        key = self._key(messages)
        if self._mode == "replay":
            return self._replay(key)
        response = self._inner.invoke(messages, *args, **kwargs)
        if self._mode == "record":
            self._record(key, messages, response)
        return response


def _inner_model_name(inner: Any) -> str:
    for attribute in ("model_name", "model"):
        value = getattr(inner, attribute, None)
        if isinstance(value, str) and value:
            return value
    return "replay"
