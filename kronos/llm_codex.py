"""Codex CLI backed chat model.

This adapter lets KAOS use ChatGPT/Codex OAuth credentials stored by the
Codex CLI instead of an OpenAI API key. It intentionally treats Codex as a
model backend only: KAOS still owns tool execution through its ReAct loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field


class ChatCodexCLI(BaseChatModel):
    """LangChain chat model that calls `codex exec`.

    `codex exec` uses the local Codex auth store, so a server deployment must
    have `codex login --device-auth` completed for the service user.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_name: str = "gpt-5.5"
    command: str = "codex"
    timeout_seconds: int = 180
    cwd: str = ""
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "codex-cli"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "cwd": self.cwd,
            "tools": len(self.bound_tools),
        }

    def bind_tools(
        self,
        tools: list[dict[str, Any] | type | Any | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ChatCodexCLI:
        del tool_choice, kwargs
        return self.model_copy(update={"bound_tools": list(tools)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        prompt = _build_prompt(messages, self.bound_tools, self.model_name)
        output = self._run_sync(prompt)
        return ChatResult(generations=[ChatGeneration(message=_parse_output(output, self.bound_tools))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        prompt = _build_prompt(messages, self.bound_tools, self.model_name)
        output = await self._run_async(prompt)
        return ChatResult(generations=[ChatGeneration(message=_parse_output(output, self.bound_tools))])

    def _run_sync(self, prompt: str) -> str:
        output_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as output_file:
                output_path = output_file.name
            proc = subprocess.run(
                self._args(prompt, output_path),
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            return _read_codex_result(proc.returncode, proc.stdout, proc.stderr, output_path)
        finally:
            if output_path and os.path.exists(output_path):
                os.unlink(output_path)

    async def _run_async(self, prompt: str) -> str:
        output_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as output_file:
                output_path = output_file.name
            proc = await asyncio.create_subprocess_exec(
                *self._args(prompt, output_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout_seconds,
            )
            return _read_codex_result(
                proc.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                output_path,
            )
        finally:
            if output_path and os.path.exists(output_path):
                os.unlink(output_path)

    def _args(self, prompt: str, output_path: str) -> list[str]:
        args = [
            self.command,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--output-last-message",
            output_path,
        ]
        if self.cwd:
            args.extend(["--cd", self.cwd])
        if self.model_name:
            args.extend(["-m", self.model_name])
        args.append(prompt)
        return args


def _read_codex_result(returncode: int | None, stdout: str, stderr: str, output_path: str) -> str:
    if returncode != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(f"Codex CLI failed ({returncode}): {detail[:1000]}")

    text = Path(output_path).read_text(encoding="utf-8").strip() if os.path.exists(output_path) else ""
    if not text:
        text = stdout.strip()
    if not text:
        raise RuntimeError("Codex CLI returned an empty response")
    return text


def _build_prompt(messages: list[BaseMessage], tools: list[Any], model_name: str = "") -> str:
    transcript = _format_messages(messages)
    model_context = (
        f"Runtime identity: KAOS is using Codex CLI as its LLM backend with model `{model_name}`. "
        "If asked what model/backend is running, answer with that instead of saying the model is hidden.\n"
        if model_name
        else ""
    )
    if not tools:
        return (
            "You are the LLM backend for Kronos Agent OS. Answer the latest user message.\n"
            f"{model_context}"
            "Do not run shell commands, modify files, or claim external actions unless the conversation contains tool results.\n\n"
            f"Conversation:\n{transcript}\n\nAnswer:"
        )

    schemas = [_safe_tool_schema(tool) for tool in tools]
    return (
        "You are the LLM backend for Kronos Agent OS. KAOS, not Codex, executes tools.\n"
        f"{model_context}"
        "Choose whether KAOS should call a tool or whether you can answer now.\n\n"
        "Return ONLY valid JSON in exactly one of these forms:\n"
        '{"final":"final answer for the user"}\n'
        '{"tool_calls":[{"id":"call_unique_id","name":"tool_name","args":{"arg":"value"}}]}\n\n'
        "Rules:\n"
        "- Use exact tool names from the schema list.\n"
        "- Do not invent tool results.\n"
        "- After tool results appear in the conversation, either call another needed tool or return final.\n"
        "- Keep final answers in the user's language.\n\n"
        f"Available tools:\n{json.dumps(schemas, ensure_ascii=False, indent=2)}\n\n"
        f"Conversation:\n{transcript}\n\nJSON:"
    )


def _safe_tool_schema(tool: Any) -> dict[str, Any]:
    try:
        schema = convert_to_openai_tool(tool)
    except Exception:
        name = getattr(tool, "name", "unknown_tool")
        description = getattr(tool, "description", "")
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    return schema


def _format_messages(messages: list[BaseMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = _content_text(msg.content)
        if isinstance(msg, SystemMessage):
            role = "system"
        elif isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, ToolMessage):
            role = f"tool_result:{msg.tool_call_id}"
        elif isinstance(msg, AIMessage):
            role = "assistant"
            if getattr(msg, "tool_calls", None):
                content = f"{content}\nTool calls: {json.dumps(msg.tool_calls, ensure_ascii=False)}".strip()
        else:
            role = msg.type
        lines.append(f"<{role}>\n{content}\n</{role}>")
    return "\n".join(lines)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _parse_output(output: str, tools: list[Any]) -> AIMessage:
    if not tools:
        return AIMessage(content=output.strip())

    data = _parse_json(output)
    if not isinstance(data, dict):
        return AIMessage(content=output.strip())

    final = data.get("final")
    if isinstance(final, str):
        return AIMessage(content=final)

    raw_calls = data.get("tool_calls")
    if not isinstance(raw_calls, list):
        return AIMessage(content=output.strip())

    tool_calls = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        args = raw.get("args", {})
        if isinstance(args, str):
            args = _parse_json(args) or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            continue
        tool_calls.append(
            {
                "id": str(raw.get("id") or f"call_{index}_{uuid4().hex[:8]}"),
                "name": name,
                "args": args,
            }
        )

    if not tool_calls:
        return AIMessage(content=output.strip())
    return AIMessage(content="", tool_calls=tool_calls)


def _parse_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None
