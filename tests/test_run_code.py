"""run_code — the gate, the workspace, and what the container is actually told.

Docker is not available in CI, so what is checked here is everything around the
run: that it is off unless enabled, that files persist per session and cannot
escape it, that the command really mounts what the platform layer created, and
that the declared storage budget is enforced rather than recorded.
"""

import json

import pytest

from kronos.config import settings
from kronos.tools import code as code_tool
from kronos.tools.code import default_session, run_code, safe_filename


@pytest.fixture(autouse=True)
def enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "agent_name", "kronos")
    monkeypatch.setattr(settings, "enable_code_execution", True)
    yield


@pytest.fixture
def sandbox(monkeypatch):
    """A ready sandbox that records what it was asked to run."""
    calls: list[dict] = []

    def install(stdout: str = "42", stderr: str = "") -> list[dict]:
        async def fake_execute(code, timeout=30, memory_limit="256m", network=False, files_dir=None, storage_mb=64):
            calls.append(
                {
                    "code": code,
                    "timeout": timeout,
                    "network": network,
                    "files_dir": files_dir,
                    "storage_mb": storage_mb,
                }
            )
            return stdout, stderr

        monkeypatch.setattr("kronos.tools.sandbox.execute_sandboxed", fake_execute)
        monkeypatch.setattr("kronos.tools.sandbox.sandbox_ready", lambda: True)
        return calls

    return install


# --- the gate -----------------------------------------------------------------


async def test_it_is_off_unless_turned_on(monkeypatch):
    monkeypatch.setattr(settings, "enable_code_execution", False)

    out = await run_code.ainvoke({"code": "print(1)"})

    assert out.startswith("[ERROR]")
    assert "ENABLE_CODE_EXECUTION" in out


async def test_without_docker_it_says_what_to_do(monkeypatch):
    monkeypatch.setattr("kronos.tools.sandbox.sandbox_ready", lambda: False)

    out = await run_code.ainvoke({"code": "print(1)"})

    assert out.startswith("[ERROR]")
    assert "build-sandbox.sh" in out or "Docker" in out


async def test_code_that_does_not_parse_is_refused_before_the_container(sandbox):
    calls = sandbox()

    out = await run_code.ainvoke({"code": "def broken(:\n"})

    assert "does not parse" in out
    assert calls == [], "no point starting a container for code Python cannot read"


async def test_empty_and_oversized_code_are_refused(sandbox):
    sandbox()

    assert "No code" in await run_code.ainvoke({"code": "   "})
    assert "longer than" in await run_code.ainvoke({"code": "x = 1\n" * 20_000})


# --- what the container is told -----------------------------------------------


async def test_the_session_directory_is_mounted_and_the_network_is_not(sandbox):
    calls = sandbox()

    await run_code.ainvoke({"code": "print(1)", "session": "bali"})

    assert calls[0]["network"] is False
    assert calls[0]["files_dir"].endswith("/files")
    assert "bali" in calls[0]["files_dir"], "the session's own directory, not a shared one"


async def test_the_timeout_is_capped_by_policy(sandbox):
    calls = sandbox()

    await run_code.ainvoke({"code": "print(1)", "timeout": 9999})

    assert calls[0]["timeout"] <= code_tool.MAX_TIMEOUT_SECONDS


async def test_the_storage_budget_is_passed_to_the_runner(sandbox):
    """A budget the runner never sees is a number in a manifest."""
    calls = sandbox()

    await run_code.ainvoke({"code": "print(1)"})

    assert calls[0]["storage_mb"] > 0


# --- files --------------------------------------------------------------------


async def test_files_are_placed_before_the_run_and_survive_it(sandbox, tmp_path):
    calls = sandbox()
    payload = json.dumps({"offers.json": '[{"price": 1}]'})

    out = await run_code.ainvoke({"code": "print(1)", "session": "s", "files": payload})

    written = tmp_path / "sandbox" / "s" / "files" / "offers.json"
    assert written.read_text() == '[{"price": 1}]'
    assert "offers.json" in out
    assert calls[0]["files_dir"] == str(written.parent)


async def test_two_runs_in_one_session_share_their_files(sandbox, tmp_path):
    """The reason sessions exist: today's run prepares what Thursday's step reads."""
    calls = sandbox()

    await run_code.ainvoke({"code": "print(1)", "session": "bali", "files": json.dumps({"a.txt": "x"})})
    await run_code.ainvoke({"code": "print(2)", "session": "bali"})

    assert calls[0]["files_dir"] == calls[1]["files_dir"]
    assert (tmp_path / "sandbox" / "bali" / "files" / "a.txt").exists()


async def test_two_sessions_do_not_see_each_other(sandbox):
    calls = sandbox()

    await run_code.ainvoke({"code": "print(1)", "session": "one"})
    await run_code.ainvoke({"code": "print(1)", "session": "two"})

    assert calls[0]["files_dir"] != calls[1]["files_dir"]


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/etc/passwd", "..", ".", "sub/dir.txt"],
)
def test_a_file_name_cannot_leave_the_session(name):
    safe = safe_filename(name)

    assert "/" not in safe
    assert safe not in ("..", ".")


async def test_a_hopeless_file_name_is_refused_rather_than_mangled(sandbox):
    sandbox()

    out = await run_code.ainvoke({"code": "print(1)", "files": json.dumps({"..": "x"})})

    assert out.startswith("[ERROR]")


async def test_files_must_be_a_json_object(sandbox):
    sandbox()

    assert "JSON object" in await run_code.ainvoke({"code": "print(1)", "files": "[1,2]"})
    assert "JSON object" in await run_code.ainvoke({"code": "print(1)", "files": "not json"})


async def test_too_many_or_too_large_files_are_refused(sandbox):
    sandbox()
    many = json.dumps({f"f{i}.txt": "x" for i in range(code_tool.MAX_INPUT_FILES + 1)})
    huge = json.dumps({"big.txt": "x" * (code_tool.MAX_INPUT_FILE_CHARS + 1)})

    assert "too many files" in await run_code.ainvoke({"code": "print(1)", "files": many})
    assert "larger than" in await run_code.ainvoke({"code": "print(1)", "files": huge})


# --- what comes back ----------------------------------------------------------


async def test_the_output_is_returned_with_the_files_that_now_exist(sandbox):
    sandbox(stdout="total 8,800,000")

    out = await run_code.ainvoke({"code": "print(1)", "session": "s", "files": json.dumps({"in.csv": "a,b"})})

    assert "total 8,800,000" in out
    assert "in.csv" in out


async def test_errors_are_not_folded_into_the_output(sandbox):
    """Code that printed a total and then crashed did not produce a total."""
    sandbox(stdout="partial", stderr="Traceback: boom")

    out = await run_code.ainvoke({"code": "print(1)"})

    assert "Output:" in out
    assert "Errors:" in out
    assert "boom" in out


async def test_a_silent_program_is_told_that_stdout_is_the_answer(sandbox):
    sandbox(stdout="", stderr="")

    out = await run_code.ainvoke({"code": "x = 1"})

    assert "printed nothing" in out


async def test_long_output_is_truncated_visibly(sandbox):
    sandbox(stdout="y" * (code_tool.MAX_OUTPUT_CHARS + 500))

    out = await run_code.ainvoke({"code": "print(1)"})

    assert "truncated at" in out


# --- session naming -----------------------------------------------------------


def test_the_session_defaults_to_the_thread_so_a_plan_shares_its_files():
    from kronos.audit import reset_tool_audit_context, set_tool_audit_context

    token = set_tool_audit_context(thread_id="plan:7", session_id="77")
    try:
        assert default_session() == "plan:7"
    finally:
        reset_tool_audit_context(token)


def test_without_a_thread_there_is_still_a_session():
    assert default_session() == "default"


# --- how the runtime must treat it -------------------------------------------


def test_running_code_counts_as_a_side_effect():
    """Resume replays a turn; a second run would see the first one's files."""
    assert (run_code.metadata or {}).get("side_effect") is True
