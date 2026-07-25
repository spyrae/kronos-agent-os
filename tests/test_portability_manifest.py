"""Agent bundle manifest — hashing, determinism, verification (moat phase 7.1)."""

import json

import pytest

from kronos.portability import manifest as m


@pytest.fixture
def bundle(tmp_path):
    """A minimal populated bundle directory (no manifest yet)."""
    root = tmp_path / "bundle"
    (root / "persona").mkdir(parents=True)
    (root / "memory").mkdir(parents=True)
    (root / "persona" / "SOUL.md").write_text("быть точным\n", encoding="utf-8")
    (root / "memory" / "facts.jsonl").write_text('{"content": "любит краткость"}\n', encoding="utf-8")
    return root


def test_hash_artifacts_uses_sorted_posix_keys(bundle):
    artifacts = m.hash_artifacts(bundle)

    assert list(artifacts) == sorted(artifacts)
    assert "persona/SOUL.md" in artifacts
    assert artifacts["persona/SOUL.md"].startswith("sha256:")


def test_manifest_excludes_itself_from_artifacts(bundle):
    first = m.build_manifest(bundle, agent_name="kronos", includes=["persona"], counts={"facts": 1})
    m.write_manifest(bundle, first)

    rehashed = m.hash_artifacts(bundle)
    assert m.MANIFEST_NAME not in rehashed
    assert rehashed == first.artifacts


def test_manifest_is_deterministic_for_same_content(bundle):
    counts = {"facts": 1, "skills": 0}
    first = m.build_manifest(
        bundle, agent_name="kronos", includes=["persona"], counts=counts, created_at="2026-01-01T00:00:00+00:00"
    )
    second = m.build_manifest(
        bundle, agent_name="kronos", includes=["persona"], counts=counts, created_at="2026-01-01T00:00:00+00:00"
    )

    assert first.to_json() == second.to_json()


def test_created_at_is_the_only_time_dependent_field(bundle):
    early = m.build_manifest(
        bundle, agent_name="kronos", includes=["persona"], counts={}, created_at="2026-01-01T00:00:00+00:00"
    )
    later = m.build_manifest(
        bundle, agent_name="kronos", includes=["persona"], counts={}, created_at="2026-07-25T12:00:00+00:00"
    )

    assert early.created_at != later.created_at
    assert early.artifacts == later.artifacts


def test_includes_are_validated(bundle):
    with pytest.raises(m.BundleError, match="unknown bundle sections"):
        m.build_manifest(bundle, agent_name="kronos", includes=["persona", "bitcoin_wallet"], counts={})


def test_verify_manifest_accepts_untouched_bundle(bundle):
    m.write_manifest(bundle, m.build_manifest(bundle, agent_name="kronos", includes=["persona"], counts={}))

    assert m.verify_manifest(bundle) == []


def test_verify_manifest_detects_single_changed_byte(bundle):
    m.write_manifest(bundle, m.build_manifest(bundle, agent_name="kronos", includes=["persona"], counts={}))
    (bundle / "persona" / "SOUL.md").write_text("быть точнык\n", encoding="utf-8")

    problems = m.verify_manifest(bundle)

    assert problems == ["hash mismatch: persona/SOUL.md"]


def test_verify_manifest_detects_missing_and_unlisted_files(bundle):
    m.write_manifest(bundle, m.build_manifest(bundle, agent_name="kronos", includes=["persona"], counts={}))
    (bundle / "memory" / "facts.jsonl").unlink()
    (bundle / "memory" / "smuggled.jsonl").write_text("{}\n", encoding="utf-8")

    problems = m.verify_manifest(bundle)

    assert "missing file: memory/facts.jsonl" in problems
    assert "unlisted file: memory/smuggled.jsonl" in problems


def test_read_manifest_requires_the_file(tmp_path):
    with pytest.raises(m.BundleError, match="not found"):
        m.read_manifest(tmp_path)


def test_manifest_rejects_malformed_json(bundle):
    (bundle / m.MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(m.BundleError, match="not valid JSON"):
        m.read_manifest(bundle)


def test_manifest_rejects_missing_required_fields(bundle):
    (bundle / m.MANIFEST_NAME).write_text(json.dumps({"agent_name": "kronos"}), encoding="utf-8")

    with pytest.raises(m.BundleError, match="missing required fields"):
        m.read_manifest(bundle)


def test_manifest_tolerates_unknown_future_fields(bundle):
    payload = {
        "agent_name": "kronos",
        "created_at": "2026-01-01T00:00:00+00:00",
        "schema_version": m.BUNDLE_SCHEMA_VERSION,
        "artifacts": {},
        "some_future_field": {"nested": True},
    }
    (bundle / m.MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    parsed = m.read_manifest(bundle)

    assert parsed.agent_name == "kronos"


def test_newer_schema_version_is_refused(bundle):
    payload = {
        "agent_name": "kronos",
        "created_at": "2026-01-01T00:00:00+00:00",
        "schema_version": m.BUNDLE_SCHEMA_VERSION + 1,
    }
    (bundle / m.MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(m.BundleError, match="newer than this KAOS supports"):
        m.read_manifest(bundle)
