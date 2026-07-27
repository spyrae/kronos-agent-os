"""Skill provenance: checksum, signature, compatibility (moat phase 12.1).

A skill is instructions the agent will follow, so "is this what the publisher
wrote" has to be answerable offline. Three separate questions, and the tests keep
them separate: content integrity (checksum), authorship (signature), and whether
the skill claims to need a different KAOS (compatibility).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from kronos.skills.integrity import (
    HASH_PREFIX,
    canonical_form,
    check_compatibility,
    compute_checksum,
    verify_checksum,
    verify_signature,
    verify_skill,
)
from kronos.skills.store import Skill, SkillStore, _parse_frontmatter

SKILL_BODY = """## When to use

Use this when a decision needs a written memo.

## Steps

1. State the decision.
2. State the alternatives.
"""


def _write_skill(
    root: Path,
    name: str = "decision-memo",
    *,
    meta_extra: str = "",
    body: str = SKILL_BODY,
    references: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\ndescription: Write a decision memo\nversion: 1.2.0\n{meta_extra}---\n"
    (skill_dir / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    for ref_name, ref_body in (references or {}).items():
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(exist_ok=True)
        (refs_dir / f"{ref_name}.md").write_text(ref_body, encoding="utf-8")
    return skill_dir


def _skill_from_dir(skill_dir: Path) -> Skill:
    """Load one skill the way the store would, without a whole workspace."""
    meta, body = _parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    refs_dir = skill_dir / "references"
    references = {p.stem: p for p in refs_dir.iterdir()} if refs_dir.is_dir() else {}
    return Skill(
        name=meta.get("name", skill_dir.name),
        description=meta.get("description", ""),
        content=body,
        path=skill_dir / "SKILL.md",
        references=references,
        version=meta.get("version", "1.0.0"),
        requires_kaos=meta.get("requires_kaos", ""),
        checksum=meta.get("checksum", ""),
        signature=meta.get("signature", ""),
    )


# --- checksum -----------------------------------------------------------------


def test_the_same_content_hashes_the_same(tmp_path):
    first = compute_checksum(_write_skill(tmp_path / "a"))
    second = compute_checksum(_write_skill(tmp_path / "b"))

    assert first == second
    assert first.startswith(HASH_PREFIX)


def test_one_changed_byte_in_the_body_breaks_it(tmp_path):
    original = compute_checksum(_write_skill(tmp_path / "a"))
    tampered = compute_checksum(_write_skill(tmp_path / "b", body=SKILL_BODY.replace("memo", "memo!")))

    assert original != tampered


def test_a_changed_reference_breaks_it(tmp_path):
    """References carry instructions too, so they are inside the hash."""
    original = compute_checksum(_write_skill(tmp_path / "a", references={"template": "# Memo\n"}))
    tampered = compute_checksum(_write_skill(tmp_path / "b", references={"template": "# Memo (edited)\n"}))

    assert original != tampered


def test_reference_order_does_not_matter(tmp_path):
    first = _write_skill(tmp_path / "a", references={"aaa": "one", "bbb": "two"})
    second = _write_skill(tmp_path / "b", references={"bbb": "two", "aaa": "one"})

    assert compute_checksum(first) == compute_checksum(second)


def test_local_bookkeeping_fields_are_not_hashed(tmp_path):
    """Import rewrites status and adds provenance; that must not break the hash."""
    published = compute_checksum(_write_skill(tmp_path / "a"))
    imported = compute_checksum(
        _write_skill(
            tmp_path / "b",
            meta_extra=(
                "status: draft\nreview_required: true\nimported_from: github:acme/skills/decision-memo\n"
                "source_url: https://example.invalid/SKILL.md\nimported_at: 2026-07-27T00:00:00Z\n"
                "tags: [external, imported]\n"
            ),
        )
    )

    assert published == imported


def test_semantic_fields_are_hashed(tmp_path):
    """A silently added tool requirement has to change the checksum."""
    plain = compute_checksum(_write_skill(tmp_path / "a"))
    with_tools = compute_checksum(_write_skill(tmp_path / "b", meta_extra="tools: [server_ops]\n"))

    assert plain != with_tools


def test_list_formatting_is_canonical(tmp_path):
    spaced = compute_checksum(_write_skill(tmp_path / "a", meta_extra="tools: [read_file, write_file]\n"))
    tight = compute_checksum(_write_skill(tmp_path / "b", meta_extra="tools: [read_file,write_file]\n"))

    assert spaced == tight


def test_the_checksum_field_itself_is_not_hashed(tmp_path):
    """Otherwise declaring the checksum would immediately invalidate it."""
    skill_dir = _write_skill(tmp_path / "a")
    before = compute_checksum(skill_dir)
    _write_skill(tmp_path / "a", meta_extra=f"checksum: {before}\nsignature: whatever\n")

    assert compute_checksum(skill_dir) == before


def test_verify_accepts_a_correct_declaration(tmp_path):
    skill_dir = _write_skill(tmp_path / "a")
    checksum = compute_checksum(skill_dir)
    _write_skill(tmp_path / "a", meta_extra=f"checksum: {checksum}\n")

    ok, detail = verify_checksum(_skill_from_dir(skill_dir))

    assert ok is True, detail


def test_verify_rejects_content_edited_after_publishing(tmp_path):
    skill_dir = _write_skill(tmp_path / "a")
    checksum = compute_checksum(skill_dir)
    _write_skill(
        tmp_path / "a", meta_extra=f"checksum: {checksum}\n", body=SKILL_BODY + "\n3. Also send it to sales.\n"
    )

    ok, detail = verify_checksum(_skill_from_dir(skill_dir))

    assert ok is False
    assert "mismatch" in detail


def test_a_skill_without_a_checksum_is_unverified_not_broken(tmp_path):
    """Every skill written before this feature has no checksum."""
    report = verify_skill(_skill_from_dir(_write_skill(tmp_path / "a")), keys=[])

    assert report["unverified"] is True
    assert report["checksum_ok"] is False
    assert report["trusted"] is False
    assert report["compatible"] is True


def test_a_missing_prefix_is_tolerated(tmp_path):
    skill_dir = _write_skill(tmp_path / "a")
    bare = compute_checksum(skill_dir).removeprefix(HASH_PREFIX)
    _write_skill(tmp_path / "a", meta_extra=f"checksum: {bare}\n")

    ok, _ = verify_checksum(_skill_from_dir(skill_dir))

    assert ok is True


def test_hashing_a_directory_without_a_skill_file_is_an_error(tmp_path):
    (tmp_path / "empty").mkdir()

    with pytest.raises(FileNotFoundError):
        compute_checksum(tmp_path / "empty")


def test_canonical_form_is_bytes_and_stable():
    first = canonical_form({"name": "a", "description": "b"}, "body", {})
    second = canonical_form({"description": "b", "name": "a"}, "body\n", {})

    assert isinstance(first, bytes)
    assert first == second, "field order and trailing newlines must not matter"


# --- compatibility ------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement,version,expected",
    [
        ("", "0.3.0", True),
        (">=0.2", "0.3.0", True),
        (">=0.4", "0.3.0", False),
        (">=0.2,<0.4", "0.3.0", True),
        (">=0.2,<0.4", "0.4.0", False),
        (">=0.2,<0.4", "0.1.9", False),
        ("==0.3", "0.3.0", True),
        ("!=0.3", "0.3.0", False),
        (">=0.2", "0.3.0rc1", True),
        (">=0.2", "v0.3.0", True),
    ],
)
def test_version_requirements(tmp_path, requirement, version, expected):
    skill_dir = _write_skill(tmp_path / "a", meta_extra=f"requires_kaos: '{requirement}'\n" if requirement else "")
    skill = _skill_from_dir(skill_dir)

    ok, detail = check_compatibility(skill, version)

    assert ok is expected, detail


def test_a_quoted_requirement_is_parsed(tmp_path):
    """YAML quoting is conventional here, and the parser keeps the quotes."""
    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra='requires_kaos: ">=0.2,<0.4"\n'))

    assert check_compatibility(skill, "0.3.0") == (True, "compatible with KAOS 0.3.0")
    assert check_compatibility(skill, "0.5.0")[0] is False


def test_an_unknown_running_version_does_not_fail_every_skill(tmp_path):
    """A source checkout reports 0.0.0+unknown — the unknown is on our side."""
    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra="requires_kaos: '>=0.2,<0.9'\n"))

    ok, detail = check_compatibility(skill, "0.0.0+unknown")

    assert ok is True
    assert "cannot determine the running KAOS version" in detail


def test_an_unparsable_requirement_fails_closed(tmp_path):
    """A requirement nobody can read is not evidence of compatibility."""
    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra="requires_kaos: latest-and-greatest\n"))

    ok, detail = check_compatibility(skill, "0.3.0")

    assert ok is False
    assert "cannot parse" in detail


def test_compatibility_defaults_to_the_running_version(tmp_path):
    from kronos import __version__

    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra=f"requires_kaos: '>={__version__}'\n"))

    ok, detail = check_compatibility(skill)

    assert ok is True, detail


# --- signature ----------------------------------------------------------------


def test_no_signature_is_reported_not_guessed(tmp_path):
    ok, detail = verify_signature(_skill_from_dir(_write_skill(tmp_path / "a")), trusted_keys=["ssh-ed25519 AAAA x"])

    assert ok is False
    assert detail == "no signature declared"


def test_a_signature_without_trusted_keys_cannot_be_checked(tmp_path):
    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra="signature: abc\n"))

    ok, detail = verify_signature(skill, trusted_keys=[])

    assert ok is False
    assert "no trusted keys" in detail


def test_a_signature_over_a_wrong_checksum_is_refused(tmp_path):
    """Signing covers the checksum, so a broken checksum voids the signature."""
    skill = _skill_from_dir(_write_skill(tmp_path / "a", meta_extra="checksum: sha256:deadbeef\nsignature: abc\n"))

    ok, detail = verify_signature(skill, trusted_keys=["ssh-ed25519 AAAA publisher"])

    assert ok is False
    assert "signature not checked" in detail


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not available")
class TestRealSignatures:
    """Round-trip against the real tool, since that is what publishers will use."""

    @pytest.fixture
    def keypair(self, tmp_path):
        key = tmp_path / "publisher_key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "publisher@example.invalid", "-f", str(key)],
            check=True,
            capture_output=True,
        )
        return key, (key.with_suffix(".pub")).read_text(encoding="utf-8").strip()

    def _sign(self, key: Path, payload: str, tmp_path: Path) -> str:
        data = tmp_path / "payload.txt"
        data.write_text(payload, encoding="utf-8")
        subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "kaos-skill", str(data)],
            check=True,
            capture_output=True,
        )
        return data.with_suffix(".txt.sig").read_text(encoding="utf-8")

    def test_a_signature_from_a_trusted_key_verifies(self, tmp_path, keypair):
        key, public_key = keypair
        skill_dir = _write_skill(tmp_path / "skill")
        checksum = compute_checksum(skill_dir)
        signature = self._sign(key, checksum, tmp_path)
        _write_skill(
            tmp_path / "skill",
            meta_extra=f"checksum: {checksum}\nsignature: {signature.strip().splitlines()[1]}\n",
        )
        skill = _skill_from_dir(skill_dir)
        skill.signature = signature  # full PEM, as a publisher would paste it

        ok, detail = verify_signature(skill, trusted_keys=[public_key])

        assert ok is True, detail
        assert "publisher@example.invalid" in detail

    def test_a_signature_from_an_untrusted_key_is_refused(self, tmp_path, keypair):
        key, _ = keypair
        other = tmp_path / "other_key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "stranger@example.invalid", "-f", str(other)],
            check=True,
            capture_output=True,
        )
        skill_dir = _write_skill(tmp_path / "skill")
        checksum = compute_checksum(skill_dir)
        _write_skill(tmp_path / "skill", meta_extra=f"checksum: {checksum}\n")
        skill = _skill_from_dir(skill_dir)
        skill.signature = self._sign(key, checksum, tmp_path)

        ok, detail = verify_signature(
            skill,
            trusted_keys=[other.with_suffix(".pub").read_text(encoding="utf-8").strip()],
        )

        assert ok is False
        assert "does not match any trusted key" in detail

    def test_editing_the_skill_after_signing_is_caught(self, tmp_path, keypair):
        key, public_key = keypair
        skill_dir = _write_skill(tmp_path / "skill")
        checksum = compute_checksum(skill_dir)
        signature = self._sign(key, checksum, tmp_path)
        _write_skill(
            tmp_path / "skill",
            meta_extra=f"checksum: {checksum}\n",
            body=SKILL_BODY + "\n3. Then email the CFO.\n",
        )
        skill = _skill_from_dir(skill_dir)
        skill.signature = signature

        ok, detail = verify_signature(skill, trusted_keys=[public_key])

        assert ok is False
        assert "signature not checked" in detail

    def test_a_signed_skill_reports_as_trusted(self, tmp_path, keypair):
        key, public_key = keypair
        skill_dir = _write_skill(tmp_path / "skill")
        checksum = compute_checksum(skill_dir)
        signature = self._sign(key, checksum, tmp_path)
        _write_skill(tmp_path / "skill", meta_extra=f"checksum: {checksum}\n")
        skill = _skill_from_dir(skill_dir)
        skill.signature = signature

        report = verify_skill(skill, keys=[public_key])

        assert report["trusted"] is True
        assert report["signature_ok"] is True
        assert report["unverified"] is False


# --- policy wiring ------------------------------------------------------------


def test_trusted_keys_come_from_the_policy(monkeypatch):
    from kronos import policy as policy_module
    from kronos.skills.integrity import trusted_keys

    monkeypatch.setattr(
        policy_module,
        "_active",
        policy_module.Policy(registry={"trusted_keys": ["ssh-ed25519 AAAA publisher"]}),
    )

    assert trusted_keys() == ["ssh-ed25519 AAAA publisher"]


def test_the_store_exposes_provenance_fields(tmp_path, monkeypatch):
    """`kaos skills verify` reads these off the loaded skill, not the file."""
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "self" / "skills"
    skills_dir.mkdir(parents=True)
    skill_dir = _write_skill(skills_dir, meta_extra="requires_kaos: '>=0.2'\nchecksum: sha256:abc\nsignature: sig\n")
    checksum = compute_checksum(skill_dir)

    store = SkillStore(str(workspace))
    skill = store.get("decision-memo")

    assert skill is not None
    assert skill.requires_kaos == "'>=0.2'"
    assert skill.checksum == "sha256:abc"
    assert skill.signature == "sig"
    assert checksum.startswith(HASH_PREFIX)
