"""Security tests for server_ops.server_query_swarm.

Focus: the SQL query must never be interpolated into the remote shell
command (shell-injection hole), and dangerous inputs are rejected before
any SSH call is made.
"""

import pytest

from kronos.tools import server_ops


@pytest.fixture
def registry(monkeypatch):
    reg = {
        "primary": {
            "host": "10.0.0.9",
            "username": "deploy",
            "data_path": "/opt/kaos/data",
            "description": "test host",
            "services": ["kaos"],
        }
    }
    monkeypatch.setattr(server_ops, "SERVER_REGISTRY", reg)
    return reg


@pytest.fixture
def captured_ssh(monkeypatch):
    """Replace _ssh_run with a recorder so tests never touch the network."""
    calls = []

    async def fake_ssh_run(
        host,
        command,
        username="deploy",
        timeout=server_ops._SSH_TIMEOUT,
        input_data=None,
    ):
        calls.append(
            {
                "host": host,
                "command": command,
                "username": username,
                "input_data": input_data,
            }
        )
        return "(fake output)"

    monkeypatch.setattr(server_ops, "_ssh_run", fake_ssh_run)
    return calls


async def _run(query, server_name="primary"):
    return await server_ops.server_query_swarm.ainvoke(
        {"query": query, "server_name": server_name}
    )


async def test_valid_select_passes_sql_via_stdin(registry, captured_ssh):
    out = await _run("SELECT agent_name FROM reply_claims")

    assert out == "(fake output)"
    assert len(captured_ssh) == 1
    call = captured_ssh[0]
    # SQL travels through stdin, opened read-only…
    assert call["input_data"] == "SELECT agent_name FROM reply_claims"
    assert "sqlite3 -readonly" in call["command"]
    # …and never appears inside the shell command itself.
    assert "reply_claims" not in call["command"]
    assert "SELECT" not in call["command"]


async def test_injection_payload_never_reaches_shell(registry, captured_ssh):
    # Passes the SELECT/keyword/dot filters, but tries to break out of the
    # old `"{query}"` quoting to run a host command.
    payload = "SELECT body FROM swarm_messages WHERE body = 'x'; touch /tmp/pwned; --'"
    out = await _run(payload)

    assert out == "(fake output)"
    call = captured_ssh[0]
    # The whole payload is confined to stdin; the shell command is fixed.
    assert call["input_data"] == payload
    assert "touch /tmp/pwned" not in call["command"]
    assert "/tmp/pwned" not in call["command"]


async def test_dot_command_blocked(registry, captured_ssh):
    out = await _run(".shell echo pwned")
    assert out.startswith("[BLOCKED]")
    assert captured_ssh == []


async def test_dot_command_blocked_on_second_line(registry, captured_ssh):
    # A SELECT prefix must not smuggle a dot-command on a later line.
    out = await _run("SELECT 1;\n.shell rm -rf /")
    assert out.startswith("[BLOCKED]")
    assert captured_ssh == []


async def test_non_select_blocked(registry, captured_ssh):
    out = await _run("PRAGMA table_info(reply_claims)")
    assert out.startswith("[BLOCKED]")
    assert captured_ssh == []


async def test_dangerous_keyword_blocked(registry, captured_ssh):
    out = await _run("SELECT 1; DROP TABLE reply_claims")
    assert out.startswith("[BLOCKED]")
    assert "DROP" in out
    assert captured_ssh == []


async def test_unknown_server(registry, captured_ssh):
    out = await _run("SELECT 1", server_name="ghost")
    assert "Unknown server" in out
    assert captured_ssh == []
