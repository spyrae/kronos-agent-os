import json
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _git_ls_files(prefix: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", prefix],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_only_workspace_template_is_tracked():
    tracked = _git_ls_files("workspaces")
    allowed_prefixes = ("workspaces/_template/", "workspaces/README.md")

    assert all(path.startswith(allowed_prefixes) for path in tracked)
    assert (ROOT / "workspaces" / "README.md").is_file()
    assert (ROOT / "workspaces" / "_template" / "README.md").is_file()


def test_private_runtime_files_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "workspaces/*" in gitignore
    assert "!workspaces/_template/**" in gitignore
    assert "agents.yaml" in gitignore
    assert "servers.yaml" in gitignore
    assert "dashboard-ui/dist/" in gitignore


def test_dashboard_build_output_is_not_tracked():
    assert _git_ls_files("dashboard-ui/dist") == []


def test_required_community_files_exist():
    for relative in [
        "LICENSE",
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "ROADMAP.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CODE_OF_CONDUCT.md",
        ".nvmrc",
        "MANIFEST.in",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]:
        assert (ROOT / relative).is_file(), relative


def test_required_docs_exist():
    for relative in [
        "docs/README.md",
        "docs/LANDING.md",
        "docs/DEMO.md",
        "docs/LLM_PROVIDERS.md",
        "docs/PERSONAL_OPERATOR_DEMO.md",
        "docs/SWARM_DEMO.md",
        "docs/LAUNCH_COPY.md",
        "docs/RELEASE_NOTES_v0.1.0.md",
        "docs/SOFT_LAUNCH.md",
        "docs/ARCHITECTURE.md",
        "docs/RUNTIME.md",
        "docs/MEMORY.md",
        "docs/SKILLS.md",
        "docs/MCP.md",
        "docs/AUTOMATIONS.md",
        "docs/SWARM.md",
        "docs/DASHBOARD.md",
        "docs/DEPLOYMENT.md",
        "docs/SECURITY.md",
    ]:
        assert (ROOT / relative).is_file(), relative


def test_systemd_public_units_are_generic():
    assert (ROOT / "systemd" / "kaos.service").is_file()
    assert (ROOT / "systemd" / "kaos@.service").is_file()

    old_named_units = {
        "im" + "pulse.service",
        "key" + "stone.service",
        "la" + "cuna.service",
        "nex" + "us.service",
        "res" + "onant.service",
        "kronos" + "-ii.service",
    }
    tracked_systemd = {path.name for path in (ROOT / "systemd").glob("*.service")}

    assert tracked_systemd.isdisjoint(old_named_units)


def test_issue_templates_are_valid_yaml():
    for path in (ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), path
        assert parsed, path


def test_workflows_are_valid_yaml():
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), path
        assert parsed, path


def test_dashboard_ui_declares_node_engine():
    package = json.loads((ROOT / "dashboard-ui" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "dashboard-ui" / "package-lock.json").read_text(encoding="utf-8"))
    engine = ">=18.18.0"

    assert (ROOT / ".nvmrc").read_text(encoding="utf-8").strip() == "20.11.0"
    assert package["engines"]["node"] == engine
    assert lock["packages"][""]["engines"]["node"] == engine
    assert package["devDependencies"]["vite"].startswith("^6.")
    assert package["devDependencies"]["@vitejs/plugin-react"].startswith("^4.")


def test_python_package_has_public_metadata():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert project["name"] == "kronos-agent-os"
    assert project["readme"] == "README.md"
    assert "ai-agents" in project["keywords"]
    assert project["urls"]["Repository"] == "https://github.com/spyrae/kronos-agent-os"
    assert "langgraph>=0.4" in project["optional-dependencies"]["aso"]
    assert not any(item.startswith("License ::") for item in project["classifiers"])
    assert pyproject["tool"]["setuptools"]["include-package-data"] is False
    assert pyproject["tool"]["setuptools"]["py-modules"] == []
    includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "dashboard" in includes
    assert "dashboard.*" in includes
    assert "aso" in includes
    assert "aso.*" in includes
    assert "dashboard*" not in includes
    assert pyproject["tool"]["setuptools"]["package-data"]["aso.prompts"] == ["*.md"]


def test_github_release_hygiene_surfaces_are_present():
    repo = yaml.safe_load((ROOT / ".github" / "repository.yml").read_text(encoding="utf-8"))
    bug = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8"))
    docs = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "docs_improvement.yml").read_text(encoding="utf-8"))
    release = (ROOT / "docs" / "RELEASE_NOTES_v0.1.0.md").read_text(encoding="utf-8")

    assert repo["repository"]["description"].startswith("Kronos Agent OS (KAOS)")
    assert "agent-os" in repo["repository"]["topics"]
    assert "self-hosted" in repo["repository"]["topics"]
    bug_body_ids = {item.get("id") for item in bug["body"] if isinstance(item, dict)}
    assert {"reproduce", "install", "capabilities", "logs"}.issubset(bug_body_ids)
    assert docs["name"] == "Docs improvement"
    assert "Known Limitations" in release
    assert "Safety Defaults" in release
    assert "kaos demo-seed --reset" in release


def test_public_product_surface_centers_agent_os_not_swarm_only():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    intro = readme.split("## Quickstart", 1)[0].lower()
    architecture_intro = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8").split(
        "## System Map",
        1,
    )[0].lower()

    assert "kronos agent os" in intro
    assert "agent operating layer" in intro
    assert "coordination" in intro
    assert "council" not in intro
    assert "swarm demo" not in intro
    assert "multi-agent coordination is one optional module" in architecture_intro


def test_durable_demo_assets_are_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    demo = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")

    assert "docs/assets/kaos-durable-agent-demo.gif" in readme
    assert "kaos demo-seed --reset" in demo
    assert "memory, skills, tools, jobs, dashboard, and swarm" in demo
    assert (ROOT / "docs" / "assets" / "kaos-durable-agent-demo.svg").is_file()
    assert (ROOT / "docs" / "assets" / "kaos-durable-agent-demo.gif").is_file()
    assert (ROOT / "scripts" / "render_demo_assets.py").is_file()


def test_personal_operator_demo_uses_public_template():
    demo = (ROOT / "docs" / "PERSONAL_OPERATOR_DEMO.md").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "agents" / "personal-operator" / "template.yaml").read_text(encoding="utf-8")

    assert "kaos templates install personal-operator personal-demo --force" in demo
    assert "kaos skills install-pack productivity --agent personal-demo --force" in demo
    assert "does not require Telegram, private accounts, email access" in demo
    assert "Personal Operator" in template
    assert (ROOT / "docs" / "assets" / "kaos-personal-operator-demo.svg").is_file()
    assert (ROOT / "docs" / "assets" / "kaos-personal-operator-demo.gif").is_file()


def test_swarm_demo_frames_coordination_as_optional():
    demo = (ROOT / "docs" / "SWARM_DEMO.md").read_text(encoding="utf-8")
    swarm = (ROOT / "docs" / "SWARM.md").read_text(encoding="utf-8")

    assert "kaos demo-seed --reset" in demo
    assert "researcher, critic, operator, and synthesizer" in demo
    assert "Swarm is optional" in demo
    assert "simple tasks should stay single-agent" in demo
    assert "[Swarm Mode Demo](SWARM_DEMO.md)" in swarm
    assert (ROOT / "docs" / "assets" / "kaos-swarm-mode-demo.svg").is_file()
    assert (ROOT / "docs" / "assets" / "kaos-swarm-mode-demo.gif").is_file()


def test_launch_copy_covers_required_channels_and_objections():
    launch = (ROOT / "docs" / "LAUNCH_COPY.md").read_text(encoding="utf-8")

    assert "Kronos Agent OS (KAOS) is a self-hosted runtime" in launch
    assert "## X Thread" in launch
    assert "## Hacker News" in launch
    assert "## Reddit Variants" in launch
    assert "## Short Announcement" in launch
    assert "## Maintainer Reply Snippets" in launch
    assert "kaos demo-seed --reset" in launch
    assert "https://github.com/spyrae/kronos-agent-os" in launch
    assert "Is this just LangGraph?" in launch
    assert "How do you handle security?" in launch
    assert "Why MCP?" in launch


def test_landing_page_content_is_standalone_and_command_first():
    landing = (ROOT / "docs" / "LANDING.md").read_text(encoding="utf-8")

    assert landing.startswith("# Kronos Agent OS")
    assert "kaos demo" in landing.split("## What KAOS Includes", 1)[0]
    assert "kaos demo-seed --reset" in landing.split("## What KAOS Includes", 1)[0]
    assert "| Runtime |" in landing
    assert "| Memory |" in landing
    assert "| Tool Gateway |" in landing
    assert "[Durable Agent Demo](DEMO.md)" in landing
    assert "[Security](SECURITY.md)" in landing
    assert "[CONTRIBUTING.md](../CONTRIBUTING.md)" in landing
    assert "https://github.com/spyrae/kronos-agent-os" in landing


def test_llm_provider_docs_cover_common_provider_recipes():
    providers = (ROOT / "docs" / "LLM_PROVIDERS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "KAOS_STANDARD_PROVIDER_CHAIN" in providers
    assert "OpenRouter" in providers
    assert "LiteLLM Proxy" in providers
    assert "Ollama" in providers
    assert "Arbitrary OpenAI-Compatible Provider" in providers
    assert "[LLM Providers](docs/LLM_PROVIDERS.md)" in readme
    assert "KAOS_PROVIDER_MY_LAB_BASE_URL" in env


def test_contributing_explains_extension_lanes():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "## Contribution Map" in contributing
    assert "LLM providers" in contributing
    assert "Agent templates" in contributing
    assert "Skill packs" in contributing
    assert "Provider PR Checklist" in contributing


def test_soft_launch_plan_tracks_external_feedback_requirements():
    soft_launch = (ROOT / "docs" / "SOFT_LAUNCH.md").read_text(encoding="utf-8")

    assert "not a required gate before publishing KAOS on GitHub" in soft_launch
    assert "Time to first demo" in soft_launch
    assert "Setup blocker" in soft_launch
    assert "Security concern" in soft_launch
    assert "RB-1153" in soft_launch


def test_docs_describe_current_runtime_and_contributor_map():
    runtime = (ROOT / "docs" / "RUNTIME.md").read_text(encoding="utf-8")
    memory = (ROOT / "docs" / "MEMORY.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert "Message Flow" in runtime
    assert "User, Workspace, And Data Boundaries" in runtime
    assert "LangGraph Checkpointer" not in memory
    assert "data/<agent>/session.db" in memory
    assert "Contributor Map" in docs_index


def test_manifest_prunes_frontend_and_runtime_artifacts():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "prune dashboard-ui/node_modules" in manifest
    assert "prune dashboard-ui/dist" in manifest
    assert "prune workspaces" in manifest
    assert "prune data" in manifest
