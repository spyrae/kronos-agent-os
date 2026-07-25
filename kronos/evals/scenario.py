"""Golden scenarios: a recorded turn replayed against a scripted model.

Why a script and not a keyed cassette. A cassette keyed on the conversation is
perfect for "same input, same code, same answer" regression — but the thing we
most want to check is a **changed prompt**: edit SOUL.md and every cassette key
changes, so a keyed replay always misses. A scenario instead stores the sequence
of model responses observed in a real turn and replays them in order, so the
deterministic half of the agent (routing, approval gates, loop detection,
compaction, untrusted framing, tool wiring) can be diffed across any change.

What a scenario cannot tell you: how a different prompt would change the model's
own choices. That needs live evaluation with real keys — `--live` territory,
never a CI gate.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import AIMessage, BaseMessage

log = logging.getLogger("kronos.evals.scenario")

SCENARIO_FILE = "scenario.yaml"
SCHEMA_VERSION = 1


class ScenarioError(Exception):
    """Raised when a scenario file is malformed or unusable."""


@dataclass
class Expectations:
    """Checkable claims about a turn. Empty fields are simply not checked."""

    tools_called: list[str] = field(default_factory=list)
    tools_forbidden: list[str] = field(default_factory=list)
    approval_required_for: list[str] = field(default_factory=list)
    must_mention: list[str] = field(default_factory=list)
    must_not_mention: list[str] = field(default_factory=list)
    max_tool_calls: int = 0
    ordered: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Expectations":
        raw = raw or {}
        return cls(
            tools_called=[str(item) for item in raw.get("tools_called") or []],
            tools_forbidden=[str(item) for item in raw.get("tools_forbidden") or []],
            approval_required_for=[str(item) for item in raw.get("approval_required_for") or []],
            must_mention=[str(item) for item in raw.get("must_mention") or []],
            must_not_mention=[str(item) for item in raw.get("must_not_mention") or []],
            max_tool_calls=int(raw.get("max_tool_calls") or 0),
            ordered=bool(raw.get("ordered")),
        )

    def to_dict(self) -> dict:
        return {
            "tools_called": self.tools_called,
            "tools_forbidden": self.tools_forbidden,
            "approval_required_for": self.approval_required_for,
            "must_mention": self.must_mention,
            "must_not_mention": self.must_not_mention,
            "max_tool_calls": self.max_tool_calls,
            "ordered": self.ordered,
        }


@dataclass
class Scenario:
    """One captured turn plus what we expect of it."""

    name: str
    input: str
    script: list[dict] = field(default_factory=list)
    tool_outputs: dict[str, list[str]] = field(default_factory=dict)
    expect: Expectations = field(default_factory=Expectations)
    schema_version: int = SCHEMA_VERSION
    captured_at: str = ""
    source_turn: str = ""
    draft: bool = True
    notes: str = ""
    path: Path | None = None

    @property
    def tool_names(self) -> list[str]:
        """Tools the script calls, in order, including repeats."""
        names: list[str] = []
        for step in self.script:
            for call in step.get("tool_calls") or []:
                names.append(str(call.get("name", "")))
        return [name for name in names if name]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "captured_at": self.captured_at,
            "source_turn": self.source_turn,
            "draft": self.draft,
            "notes": self.notes,
            "input": self.input,
            "script": self.script,
            "tool_outputs": self.tool_outputs,
            "expect": self.expect.to_dict(),
        }

    def save(self, directory: str | Path) -> Path:
        target = Path(directory) / SCENARIO_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        self.path = target
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        source = Path(path)
        if source.is_dir():
            source = source / SCENARIO_FILE
        if not source.exists():
            raise ScenarioError(f"scenario not found: {source}")
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ScenarioError(f"{source}: invalid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise ScenarioError(f"{source}: expected a mapping at the top level")

        version = int(raw.get("schema_version") or SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise ScenarioError(f"{source}: scenario schema v{version} is newer than supported v{SCHEMA_VERSION}")

        name = str(raw.get("name") or source.parent.name)
        if not raw.get("script"):
            raise ScenarioError(f"{source}: scenario has no script — nothing to replay")

        return cls(
            name=name,
            input=str(raw.get("input") or ""),
            script=[step for step in raw["script"] if isinstance(step, dict)],
            tool_outputs={
                str(key): [str(item) for item in (value if isinstance(value, list) else [value])]
                for key, value in (raw.get("tool_outputs") or {}).items()
            },
            expect=Expectations.from_dict(raw.get("expect")),
            schema_version=version,
            captured_at=str(raw.get("captured_at") or ""),
            source_turn=str(raw.get("source_turn") or ""),
            draft=bool(raw.get("draft", False)),
            notes=str(raw.get("notes") or ""),
            path=source,
        )


def discover(suite_dir: str | Path) -> list[Scenario]:
    """Load every scenario under a suite directory, sorted by name."""
    root = Path(suite_dir)
    if not root.exists():
        return []
    scenarios = []
    for path in sorted(root.rglob(SCENARIO_FILE)):
        try:
            scenarios.append(Scenario.load(path))
        except ScenarioError as e:
            log.warning("Skipping scenario: %s", e)
    return sorted(scenarios, key=lambda scenario: scenario.name)


class ScriptedChatModel:
    """Chat model that returns a fixed sequence of responses.

    Running out of script is an error rather than a silent stop: it means the
    agent asked for one more model turn than the recorded run did, which is
    exactly the behaviour change a diff should surface.
    """

    model_name = "scripted"

    def __init__(self, script: list[dict], *, scenario_name: str = ""):
        self._script = list(script)
        self._scenario = scenario_name
        self.calls = 0

    def bind_tools(self, tools: list) -> "ScriptedChatModel":
        """Tools do not change a scripted answer, so binding keeps the same tape.

        Returning self (rather than a clone) matters: the engine binds once and
        then invokes repeatedly, and a clone would reset the position counter.
        """
        return self

    def _next(self) -> AIMessage:
        if self.calls >= len(self._script):
            raise ScenarioError(
                f"scenario '{self._scenario}': the agent requested model turn "
                f"{self.calls + 1} but the script has {len(self._script)}"
            )
        step = self._script[self.calls]
        self.calls += 1
        tool_calls = [
            {
                "name": str(call.get("name", "")),
                "args": call.get("args") or {},
                "id": str(call.get("id") or f"scripted_{self.calls}_{index}"),
                "type": "tool_call",
            }
            for index, call in enumerate(step.get("tool_calls") or [])
        ]
        return AIMessage(content=str(step.get("content") or ""), tool_calls=tool_calls)

    async def ainvoke(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> AIMessage:
        return self._next()

    def invoke(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> AIMessage:
        return self._next()
