from pathlib import Path

from kronos.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_templates_list_and_show(capsys):
    result = main(["templates", "list"])
    out = capsys.readouterr().out

    assert result == 0
    assert "personal-operator" in out
    assert "research-agent" in out

    result = main(["templates", "show", "personal-operator"])
    out = capsys.readouterr().out

    assert result == 0
    assert "Personal Operator" in out
    assert "Capability Policy" in out


def test_template_install_dry_run_does_not_write(capsys):
    target = ROOT / "workspaces" / "template-smoke-agent"
    assert not target.exists()

    result = main([
        "templates",
        "install",
        "research-agent",
        "template-smoke-agent",
        "--dry-run",
    ])
    out = capsys.readouterr().out

    assert result == 0
    assert "Dry run only" in out
    assert not target.exists()


def test_skill_packs_list_show_and_dry_run(capsys):
    result = main(["skills", "packs"])
    out = capsys.readouterr().out

    assert result == 0
    assert "research" in out
    assert "finance-lite" in out

    result = main(["skills", "show-pack", "research"])
    out = capsys.readouterr().out

    assert result == 0
    assert "Research Pack" in out
    assert "research-brief" in out

    result = main(["skills", "install-pack", "research", "--agent", "template-smoke-agent", "--dry-run"])
    out = capsys.readouterr().out

    assert result == 0
    assert "Would install: research-brief" in out
    assert not (ROOT / "workspaces" / "template-smoke-agent").exists()


def test_bundled_templates_and_packs_have_required_metadata():
    agent_templates = sorted((ROOT / "templates" / "agents").glob("*/template.yaml"))
    skill_packs = sorted((ROOT / "templates" / "skill-packs").glob("*/pack.yaml"))

    assert len(agent_templates) >= 5
    assert len(skill_packs) >= 5

    for path in agent_templates:
        text = path.read_text(encoding="utf-8")
        assert "capability_policy:" in text
        assert "memory_defaults:" in text
        assert "example_prompts:" in text

    for path in skill_packs:
        pack_dir = path.parent
        assert "capabilities:" in path.read_text(encoding="utf-8")
        assert (pack_dir / "fixtures" / "smoke.md").is_file()
        assert list((pack_dir / "skills").glob("*/SKILL.md"))
