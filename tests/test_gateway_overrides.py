"""The MCP gateway applies dashboard overrides (mcp_overrides.json).

Regression guard for the split-brain where the dashboard wrote disable toggles
and custom servers that the runtime gateway never read, so the UI had no effect.
"""

import json


def test_apply_overrides(monkeypatch, tmp_path):
    from kronos.config import settings

    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "enable_dynamic_mcp_servers", False)

    from kronos.tools.gateway import MCPGateway

    gw = MCPGateway()
    base = {
        "keep": {"transport": "stdio", "command": "x"},
        "drop_me": {"transport": "stdio", "command": "y"},
    }

    # No overrides file → base config unchanged (fail-safe).
    assert gw._apply_overrides(dict(base)) == base

    (db_dir / "mcp_overrides.json").write_text(
        json.dumps(
            {
                "drop_me": {"disabled": True},
                "custom": {"command": "mycmd", "args": ["--x"], "env": {"K": "v"}},
            }
        ),
        encoding="utf-8",
    )
    result = gw._apply_overrides(dict(base))

    assert "drop_me" not in result  # operator-disabled server removed
    assert "keep" in result  # untouched builtin stays
    assert result["custom"]["command"] == "mycmd"  # custom server merged in
    assert result["custom"]["transport"] == "stdio"
    assert result["custom"]["env"] == {"K": "v"}


def test_apply_overrides_safe_on_malformed(monkeypatch, tmp_path):
    from kronos.config import settings

    db_dir = tmp_path / "agent"
    db_dir.mkdir()
    monkeypatch.setattr(settings, "db_path", str(db_dir / "session.db"))
    monkeypatch.setattr(settings, "enable_dynamic_mcp_servers", False)
    (db_dir / "mcp_overrides.json").write_text("{ broken", encoding="utf-8")

    from kronos.tools.gateway import MCPGateway

    base = {"a": {"command": "x"}}
    assert MCPGateway()._apply_overrides(dict(base)) == base
