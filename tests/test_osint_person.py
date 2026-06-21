from datetime import UTC, datetime

import pytest

from kronos.osint.person import (
    DossierFact,
    SourceLink,
    build_person_dossier,
    handle_osint_command,
    slugify_person_name,
)
from kronos.workspace import Workspace

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def test_slugify_person_name_transliterates_and_normalizes():
    assert slugify_person_name("Ivan Petrov") == "ivan-petrov"
    assert slugify_person_name("Иван Петров") == "ivan-petrov"
    assert slugify_person_name(" Dr. Jane   Doe, PhD ") == "dr-jane-doe-phd"


def test_build_person_dossier_saves_template_to_contacts(tmp_path):
    workspace = Workspace(tmp_path)

    result = build_person_dossier(
        "Ivan Petrov",
        workspace=workspace,
        facts=(
            DossierFact(
                text="Ivan Petrov is listed as founder of Example Labs.",
                source="https://example.com/ivan",
                confidence="medium",
            ),
        ),
        source_links=(
            SourceLink(
                title="Ivan Petrov — Example Labs",
                url="https://example.com/ivan",
                description="Founder profile",
            ),
        ),
        now=NOW,
    )

    expected_path = tmp_path / "notes" / "world" / "contacts" / "ivan-petrov.md"
    assert result.path == expected_path
    assert result.slug == "ivan-petrov"
    assert expected_path.exists()
    text = expected_path.read_text(encoding="utf-8")
    assert text.startswith("# Person: Ivan Petrov")
    assert "## Known facts" in text
    assert "source: https://example.com/ivan" in text
    assert "confidence: medium" in text
    assert "## Inferences" in text
    assert "## Open questions" in text
    assert "## Do not assume" in text
    assert "2026-06-19T12:00:00Z" in text


def test_unsourced_facts_are_low_confidence_inferences(tmp_path):
    result = build_person_dossier(
        "Ivan Petrov",
        workspace=Workspace(tmp_path),
        facts=(DossierFact(text="Ivan may advise stealth startups", confidence="high"),),
        now=NOW,
    )

    assert result.fact_count == 0
    assert result.inference_count == 1
    assert "- No source-backed facts collected yet." in result.markdown
    assert "Ivan may advise stealth startups" in result.markdown
    assert "confidence: low" in result.markdown
    assert "No public source supplied" in result.markdown


def test_default_searcher_results_are_curated_not_raw_dump(tmp_path):
    calls = []

    def fake_searcher(query, count, freshness):
        calls.append((query, count, freshness))
        return [
            {
                "title": "Ivan Petrov — Example Labs",
                "url": "https://example.com/ivan",
                "description": "Founder of Example Labs and AI product advisor.",
                "ignored_raw_payload": "SHOULD NOT BE SAVED",
            }
        ]

    result = build_person_dossier(
        "Ivan Petrov",
        workspace=Workspace(tmp_path),
        searcher=fake_searcher,
        now=NOW,
    )

    assert calls and '"Ivan Petrov"' in calls[0][0]
    assert result.source_count == 1
    assert result.fact_count == 1
    assert "Founder of Example Labs" in result.markdown
    assert "SHOULD NOT BE SAVED" not in result.markdown


def test_handle_osint_person_command_returns_relative_path(tmp_path):
    workspace = Workspace(tmp_path)

    def fake_searcher(*args, **kwargs):
        return [SourceLink(title="Ivan", url="https://example.com/ivan", description="Public profile")]

    reply = handle_osint_command(
        "/osint person Ivan Petrov",
        workspace=workspace,
        searcher=fake_searcher,
        now=NOW,
    )

    assert reply == "Собрал dossier: notes/world/contacts/ivan-petrov.md"
    assert (tmp_path / "notes" / "world" / "contacts" / "ivan-petrov.md").exists()


def test_private_contact_data_query_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="public name/handle"):
        build_person_dossier("ivan@example.com", workspace=Workspace(tmp_path), now=NOW)

    reply = handle_osint_command(
        "/osint person +1 202 555 0100",
        workspace=Workspace(tmp_path),
        now=NOW,
    )

    assert reply is not None
    assert "public name/handle" in reply
