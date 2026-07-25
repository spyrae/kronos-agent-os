"""Turn the durable turn journal into golden scenarios.

The point of capturing from production rather than writing scenarios by hand:
hand-written cases test what the author imagined, while the journal contains
what actually happened — including the awkward turns nobody would think to
invent.

Privacy is not optional here. Scenarios are committed, so every captured string
goes through the same redaction as an exported bundle, and a capture that still
contains PII-looking content is refused unless explicitly allowed for local use.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kronos.config import settings
from kronos.evals.scenario import Expectations, Scenario, ScenarioError
from kronos.portability.dbread import read_rows
from kronos.portability.redact import redact_private_text
from kronos.security.pii import mask_pii

log = logging.getLogger("kronos.evals.capture")

# Extra head-room over the observed count, so an equivalent-but-chattier run is
# not reported as a regression by the default expectation.
_TOOL_CALL_SLACK = 2
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class CaptureReport:
    """Outcome of capturing one or more turns."""

    scenarios: list[Scenario] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        for scenario in self.scenarios:
            target = scenario.path.parent if scenario.path else scenario.name
            lines.append(f"Captured '{scenario.name}' → {target}")
            lines.append(f"  model turns: {len(scenario.script)}, tools: {', '.join(scenario.tool_names) or 'none'}")
        for reason in self.skipped:
            lines.append(f"  ! {reason}")
        if self.scenarios:
            lines.append("")
            lines.append("Expectations are a DRAFT generated from the observed run — review them before")
            lines.append("relying on the scenario, and set draft: false once checked.")
        return "\n".join(lines)


def slugify(text: str, *, fallback: str = "turn") -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return (slug[:48].rstrip("-")) or fallback


def _session_db() -> Path:
    return Path(settings.db_path)


def list_turns(*, thread_id: str = "", limit: int = 20) -> list[dict]:
    """Recent durable turns, newest first."""
    sql = """
        SELECT turn_id, thread_id, status, input_message, started_at, completed_at
        FROM active_turns
        {where}
        ORDER BY started_at DESC
        LIMIT ?
    """
    params: tuple = (limit,)
    where = ""
    if thread_id:
        where = "WHERE thread_id = ?"
        params = (thread_id, limit)
    rows = read_rows(_session_db(), sql.format(where=where), params)
    return [dict(row) for row in rows]


def _journal(turn_id: str) -> list[dict]:
    rows = read_rows(
        _session_db(),
        "SELECT message_json FROM turn_journal WHERE turn_id = ? ORDER BY seq ASC",
        (turn_id,),
    )
    messages = []
    for row in rows:
        try:
            payload = json.loads(row["message_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            messages.append(payload)
    return messages


def _tool_results(turn_id: str) -> dict[str, str]:
    rows = read_rows(
        _session_db(),
        "SELECT tool_call_id, content FROM tool_results WHERE turn_id = ?",
        (turn_id,),
    )
    return {str(row["tool_call_id"]): str(row["content"]) for row in rows}


def _script_and_outputs(messages: list[dict], memoized: dict[str, str]) -> tuple[list[dict], dict[str, list[str]]]:
    """Split a journal into model turns (the script) and tool outputs.

    Tool output is looked up by call id — first in the ToolMessage that followed,
    then in the memoized tool_results table, which is what survives a turn that
    was interrupted mid-flight.
    """
    tool_content_by_id: dict[str, str] = dict(memoized)
    for payload in messages:
        if payload.get("type") == "ToolMessage":
            call_id = str(payload.get("tool_call_id") or "")
            if call_id:
                tool_content_by_id[call_id] = str(payload.get("content") or "")

    script: list[dict] = []
    outputs: dict[str, list[str]] = {}
    for payload in messages:
        if payload.get("type") != "AIMessage":
            continue
        step: dict = {}
        content = str(payload.get("content") or "").strip()
        if content:
            step["content"] = redact_private_text(content)
        calls = []
        for call in payload.get("tool_calls") or []:
            name = str(call.get("name") or "")
            if not name:
                continue
            calls.append({"name": name, "args": _redact_args(call.get("args") or {})})
            recorded = tool_content_by_id.get(str(call.get("id") or ""))
            if recorded is not None:
                outputs.setdefault(name, []).append(redact_private_text(recorded))
        if calls:
            step["tool_calls"] = calls
        if step:
            script.append(step)
    return script, outputs


def _redact_args(args: dict) -> dict:
    from kronos.portability.redact import redact_structure

    redacted = redact_structure(args, mask_personal=True)
    return redacted if isinstance(redacted, dict) else {}


def _draft_expectations(script: list[dict]) -> Expectations:
    """Turn what happened into a first pass at what should happen.

    Deliberately conservative: the observed tool set and a call ceiling. Content
    assertions are left empty because generating them from one run would pin
    whatever wording that run happened to produce.
    """
    called: list[str] = []
    for step in script:
        for call in step.get("tool_calls") or []:
            name = str(call.get("name") or "")
            if name and name not in called:
                called.append(name)

    total_calls = sum(len(step.get("tool_calls") or []) for step in script)
    return Expectations(
        tools_called=called,
        max_tool_calls=total_calls + _TOOL_CALL_SLACK,
    )


def capture_turn(
    turn_id: str,
    *,
    suite_dir: str | Path,
    name: str = "",
    allow_pii: bool = False,
) -> Scenario:
    """Build a scenario from one durable turn and write it into a suite."""
    rows = read_rows(
        _session_db(),
        "SELECT turn_id, thread_id, input_message FROM active_turns WHERE turn_id = ?",
        (turn_id,),
    )
    if not rows:
        raise ScenarioError(f"turn not found: {turn_id}")
    turn = dict(rows[0])

    messages = _journal(turn_id)
    if not messages:
        raise ScenarioError(f"turn {turn_id} has no journalled messages — nothing to capture")

    script, outputs = _script_and_outputs(messages, _tool_results(turn_id))
    if not script:
        raise ScenarioError(f"turn {turn_id} has no model turns — nothing to replay")

    user_input = redact_private_text(str(turn.get("input_message") or ""))
    if not allow_pii:
        _refuse_if_pii(user_input, script, outputs)

    scenario = Scenario(
        name=name or slugify(user_input, fallback=f"turn-{turn_id[:8]}"),
        input=user_input,
        script=script,
        tool_outputs=outputs,
        expect=_draft_expectations(script),
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source_turn=turn_id,
        draft=True,
        notes="Draft expectations generated from the observed run; review before trusting.",
    )
    scenario.save(Path(suite_dir) / scenario.name)
    log.info("Captured scenario '%s' from turn %s", scenario.name, turn_id)
    return scenario


def _refuse_if_pii(user_input: str, script: list[dict], outputs: dict[str, list[str]]) -> None:
    """Refuse to write a scenario whose text still looks personal.

    redact_private_text already masks known patterns; this is the belt-and-braces
    check that a committed scenario is not carrying someone's address around.
    """
    blob = json.dumps(
        {"input": user_input, "script": script, "outputs": outputs},
        ensure_ascii=False,
    )
    if mask_pii(blob) != blob:
        raise ScenarioError(
            "captured content still contains personal data after redaction — "
            "fix the source turn or pass allow_pii=True for a local-only capture"
        )


def capture_thread(
    thread_id: str,
    *,
    suite_dir: str | Path,
    last: int = 5,
    allow_pii: bool = False,
) -> CaptureReport:
    """Capture the most recent turns of one thread, skipping unusable ones."""
    report = CaptureReport()
    for turn in list_turns(thread_id=thread_id, limit=last):
        try:
            report.scenarios.append(capture_turn(str(turn["turn_id"]), suite_dir=suite_dir, allow_pii=allow_pii))
        except ScenarioError as e:
            report.skipped.append(str(e))
    return report
