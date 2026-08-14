from pathlib import Path

from kronos.tools.sandbox_platform import (
    SandboxPolicy,
    SandboxResourceLimits,
    SandboxRunRequest,
    create_session_workspace,
    evaluate_policy,
    read_sandbox_records,
    record_sandbox_decision,
    sandbox_platform_status,
    write_sandbox_record,
)


def test_sandbox_policy_denies_network_by_default():
    request = SandboxRunRequest(
        tool_name="dynamic_fetch",
        session_id="s1",
        network_domains=("https://api.example.com/v1",),
    )

    decision = evaluate_policy(request, SandboxPolicy())

    assert decision.allowed is False
    assert decision.violations == ("network:api.example.com",)


def test_sandbox_policy_allows_declared_capabilities():
    request = SandboxRunRequest(
        tool_name="dynamic_fetch",
        session_id="s1",
        input_mounts=("prompt.json",),
        network_domains=("api.example.com",),
        packages=("requests>=2",),
        secret_capabilities=("weather_api",),
        resources=SandboxResourceLimits(cpu=0.5, memory_mb=128, timeout_seconds=10),
    )
    policy = SandboxPolicy(
        allowed_inputs=("prompt.json",),
        allowed_network_domains=("*.example.com",),
        allowed_packages=("requests",),
        allowed_secret_capabilities=("weather_api",),
    )

    decision = evaluate_policy(request, policy)

    assert decision.allowed is True
    assert decision.violations == ()


def test_sandbox_policy_blocks_resource_over_budget():
    request = SandboxRunRequest(
        tool_name="heavy_tool",
        session_id="s1",
        resources=SandboxResourceLimits(cpu=2.0, memory_mb=1024, timeout_seconds=120),
    )

    decision = evaluate_policy(request, SandboxPolicy(max_resources=SandboxResourceLimits()))

    assert decision.allowed is False
    assert "resource:cpu>1" in decision.violations
    assert "resource:memory>256mb" in decision.violations
    assert "resource:timeout>30s" in decision.violations


def test_sandbox_audit_records_are_redacted(tmp_path):
    audit_path = tmp_path / "sandbox_runs.jsonl"
    request = SandboxRunRequest(
        tool_name="secret_tool",
        session_id="s1",
        secret_capabilities=("linear_api",),
    )
    decision = evaluate_policy(request, SandboxPolicy())

    record_sandbox_decision(
        request,
        decision,
        stdout="ok",
        artifacts=[{"path": "/tmp/out.txt", "api_key": "sk-live-secret"}],
        audit_path=audit_path,
    )
    write_sandbox_record(
        {"status": "blocked", "token": "raw-token", "nested": {"password": "raw-password"}},
        audit_path=audit_path,
    )
    text = audit_path.read_text(encoding="utf-8")

    assert "sk-live-secret" not in text
    assert "raw-token" not in text
    assert "raw-password" not in text
    assert "linear_api" in text
    assert "[REDACTED]" in text
    assert read_sandbox_records(audit_path=audit_path, status="blocked")[0]["token"] == "[REDACTED]"


def test_create_session_workspace_writes_manifest(tmp_path):
    request = SandboxRunRequest(
        tool_name="artifact_tool",
        session_id="session/with unsafe chars",
        input_mounts=("input.json",),
    )

    paths = create_session_workspace(request, base_dir=tmp_path)

    root = Path(paths["root"])
    assert root.exists()
    assert (root / "tmp").is_dir()
    assert "session-with-unsafe-chars" in str(root)
    assert "input.json" in (root / "manifest.json").read_text(encoding="utf-8")


def test_the_files_directory_is_shared_by_every_run_of_a_session(tmp_path):
    """The reason a session exists: one run prepares what a later one reads."""
    first = create_session_workspace(SandboxRunRequest(tool_name="t", session_id="bali"), base_dir=tmp_path)
    second = create_session_workspace(SandboxRunRequest(tool_name="t", session_id="bali"), base_dir=tmp_path)

    assert first["files"] == second["files"]
    assert Path(first["files"]).is_dir()
    # Per-run history lives in the append-only sandbox audit, not in the manifest,
    # so a repeated identical request reusing its run directory is fine.
    assert (Path(first["root"]) / "manifest.json").exists()


def test_files_of_different_sessions_are_separate(tmp_path):
    one = create_session_workspace(SandboxRunRequest(tool_name="t", session_id="one"), base_dir=tmp_path)
    two = create_session_workspace(SandboxRunRequest(tool_name="t", session_id="two"), base_dir=tmp_path)

    assert one["files"] != two["files"]


def test_directory_size_adds_up_what_is_actually_there(tmp_path):
    from kronos.tools.sandbox_platform import directory_size_bytes

    (tmp_path / "a.txt").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"y" * 5)

    assert directory_size_bytes(tmp_path) == 15


def test_the_policy_comes_from_governance_not_from_the_caller(monkeypatch):
    """The old dynamic-tool path built the allowlist out of the request it checked."""
    from kronos.policy import Policy
    from kronos.policy import SandboxPolicy as DeclaredSandbox
    from kronos.tools.sandbox_platform import sandbox_policy

    declared = Policy(sandbox=DeclaredSandbox(network_domains=["api.example.com"], max_timeout_seconds=15))
    monkeypatch.setattr("kronos.policy.get_policy", lambda: declared)

    policy = sandbox_policy()

    assert policy.allowed_network_domains == ("api.example.com",)
    assert policy.max_resources.timeout_seconds == 15
    assert policy.allowed_packages == (), "packages stay refused unless a deployment writes them"


def test_a_run_asking_for_an_undeclared_domain_is_refused(monkeypatch):
    from kronos.policy import Policy
    from kronos.policy import SandboxPolicy as DeclaredSandbox
    from kronos.tools.sandbox_platform import sandbox_policy

    monkeypatch.setattr("kronos.policy.get_policy", lambda: Policy(sandbox=DeclaredSandbox()))
    request = SandboxRunRequest(tool_name="t", session_id="s", network_domains=("evil.test",))

    decision = evaluate_policy(request, sandbox_policy())

    assert decision.allowed is False
    assert "network:evil.test" in decision.violations


def test_sandbox_platform_status_separates_platform_from_execution(monkeypatch, tmp_path):
    from kronos.tools import sandbox, sandbox_platform

    monkeypatch.setattr(sandbox_platform.settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(
        sandbox,
        "sandbox_status",
        lambda: {
            "docker_available": False,
            "image": "kronos-sandbox:latest",
            "image_available": False,
            "build_script": "scripts/build-sandbox.sh",
        },
    )

    status = sandbox_platform_status()

    assert status["platform"]["ready"] is True
    assert status["platform"]["execution_ready"] is False
    assert status["basic_sandbox"]["docker_available"] is False


async def test_dynamic_tool_execution_records_sandbox_audit(monkeypatch, tmp_path):
    from kronos.tools import dynamic, sandbox, sandbox_platform

    async def fake_execute_sandboxed(code, timeout=30, memory_limit="256m", network=False, **mounts):
        return "hello Ada", ""

    code = 'async def hello_tool(name: str) -> str:\n    """Say hello."""\n    return f\'hello {name}\'\n'
    spec = dynamic._extract_function_spec(code, "Say hello")

    monkeypatch.setattr(sandbox_platform.settings, "db_path", str(tmp_path / "session.db"))
    monkeypatch.setattr(sandbox, "sandbox_ready", lambda: True)
    monkeypatch.setattr(sandbox, "execute_sandboxed", fake_execute_sandboxed)

    tool = dynamic._build_dynamic_tool("hello_tool", code, spec)
    result = await tool.ainvoke({"name": "Ada"})
    records = read_sandbox_records(audit_path=tmp_path / "logs" / "sandbox_runs.jsonl")

    assert result == "hello Ada"
    assert records[0]["status"] == "allowed"
    assert records[0]["tool_name"] == "hello_tool"
    assert records[0]["request"]["input_mounts"] == ["name"]
