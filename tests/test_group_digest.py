"""Tests for group digest cron job."""


from kronos.cron.group_digest import _filter_significant, _load_groups
from kronos.workspace import Workspace


def _patch_ws(monkeypatch, tmp_path):
    """Patch workspace singleton to use tmp_path."""
    monkeypatch.setattr("kronos.workspace.ws", Workspace(tmp_path))


class TestLoadGroups:
    """Test GROUPS.md parsing with categories."""

    def test_parses_categories_and_groups(self, tmp_path, monkeypatch):
        md = tmp_path / "self" / "skills" / "group-digest" / "references" / "GROUPS.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "# Monitored Groups\n\n"
            "## AI & LLM\n\n"
            "| Name | ID | Description |\n"
            "|------|----|-------------|\n"
            "| AI Chat | @aichat | AI discussion |\n"
            "| LLM Group | @llmgroup | LLM talk |\n\n"
            "## Startups\n\n"
            "| Name | ID | Description |\n"
            "|------|----|-------------|\n"
            "| Startup Club | @startups | Startup news |\n"
        )
        _patch_ws(monkeypatch, tmp_path)

        result = _load_groups()

        assert "AI & LLM" in result
        assert "Startups" in result
        assert len(result["AI & LLM"]) == 2
        assert len(result["Startups"]) == 1
        assert result["AI & LLM"][0]["identifier"] == "@aichat"
        assert result["AI & LLM"][0]["name"] == "AI Chat"

    def test_skips_empty_categories(self, tmp_path, monkeypatch):
        md = tmp_path / "self" / "skills" / "group-digest" / "references" / "GROUPS.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "## Has Groups\n\n"
            "| Name | ID | Desc |\n"
            "| A | @a | test |\n\n"
            "## Empty Category\n\n"
            "| Name | ID | Desc |\n"
            "|------|----|------|\n"
        )
        _patch_ws(monkeypatch, tmp_path)

        result = _load_groups()

        assert "Has Groups" in result
        assert "Empty Category" not in result

    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        _patch_ws(monkeypatch, tmp_path)
        assert _load_groups() == {}

    def test_skips_non_at_identifiers(self, tmp_path, monkeypatch):
        md = tmp_path / "self" / "skills" / "group-digest" / "references" / "GROUPS.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "## Test\n\n"
            "| Name | ID | Desc |\n"
            "|------|----|------|\n"
            "| Header | ID | Description |\n"
            "| Good | @good | works |\n"
            "| Also Good | id:12345 | numeric id |\n"
            "| Bad | nope | no at or id |\n"
        )
        _patch_ws(monkeypatch, tmp_path)

        result = _load_groups()
        assert len(result["Test"]) == 2
        assert result["Test"][0]["identifier"] == "@good"
        assert result["Test"][1]["identifier"] == "id:12345"


class TestFilterSignificant:
    """Test engagement-based message filtering."""

    def test_filters_by_reactions(self):
        # Need >= 5 significant messages to avoid fallback logic
        messages = [
            {"text": "top", "reactions": 20, "views": 50},
            {"text": "high", "reactions": 10, "views": 50},
            {"text": "mid1", "reactions": 5, "views": 100},
            {"text": "mid2", "reactions": 4, "views": 100},
            {"text": "mid3", "reactions": 3, "views": 100},
            {"text": "low1", "reactions": 0, "views": 10},
            {"text": "low2", "reactions": 0, "views": 10},
            {"text": "low3", "reactions": 1, "views": 10},
        ]
        result = _filter_significant(messages, min_reactions=3, min_views=200)

        # 5 messages pass reactions >= 3 threshold (no fallback)
        assert len(result) == 5
        # Sorted by score descending
        assert result[0]["text"] == "top"
        assert result[1]["text"] == "high"

    def test_filters_by_views(self):
        messages = [
            {"text": "viral1", "reactions": 0, "views": 1000},
            {"text": "viral2", "reactions": 0, "views": 800},
            {"text": "viral3", "reactions": 0, "views": 500},
            {"text": "viral4", "reactions": 0, "views": 300},
            {"text": "viral5", "reactions": 0, "views": 200},
            {"text": "quiet1", "reactions": 0, "views": 10},
            {"text": "quiet2", "reactions": 0, "views": 10},
        ]
        result = _filter_significant(messages, min_reactions=100, min_views=200)
        # 5 pass views >= 200 (no fallback)
        assert len(result) == 5
        assert result[0]["text"] == "viral1"
        assert all("viral" in r["text"] for r in result)

    def test_fallback_to_top_when_few_pass(self):
        messages = [
            {"text": f"msg{i}", "reactions": 1, "views": 50}
            for i in range(20)
        ]
        result = _filter_significant(messages, min_reactions=10, min_views=10000)

        # None pass filter, so fallback to top 10
        assert len(result) == 10

    def test_caps_at_20(self):
        messages = [
            {"text": f"msg{i}", "reactions": 10, "views": 500}
            for i in range(30)
        ]
        result = _filter_significant(messages)
        assert len(result) == 20

    def test_empty_input(self):
        assert _filter_significant([]) == []
