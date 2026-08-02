"""Skill sources, index and install with proof (moat phase 12.2).

The registry is a file, so the risks are not availability but trust: a source
must not become trusted by being reachable, a tampered skill must not activate,
and an unreachable source must not make the working ones useless.
"""

import json
import shutil
import subprocess

import pytest
import yaml

from kronos.config import settings
from kronos.skills.integrity import compute_checksum
from kronos.skills.registry import (
    RegistryEntry,
    RegistryError,
    RegistrySource,
    find_entry,
    index_url,
    install,
    load_index,
    load_sources,
    parse_index,
    search,
)
from kronos.skills.store import SkillStore

SKILL_MD = """---
name: decision-memo
description: Write a one-page decision memo
version: 1.2.0
author: publisher
---
## Steps

1. State the decision.
2. State the alternatives.
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "self" / "skills").mkdir(parents=True)
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    return root


@pytest.fixture
def store(workspace):
    return SkillStore(str(workspace))


def _sources_file(tmp_path, payload: dict) -> str:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return str(path)


def _published_checksum(tmp_path) -> str:
    """The checksum a publisher would compute for SKILL_MD."""
    skill_dir = tmp_path / "published" / "decision-memo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return compute_checksum(skill_dir)


def _entry(**overrides) -> RegistryEntry:
    base = {
        "name": "decision-memo",
        "description": "Write a one-page decision memo",
        "version": "1.2.0",
        "url": "https://example.invalid/decision-memo/SKILL.md",
        "source": "test-source",
        "trust": "checksum",
    }
    return RegistryEntry(**{**base, **overrides})


@pytest.fixture
def served(monkeypatch):
    """Serve SKILL.md content over the import path without network."""
    served_text = {"value": SKILL_MD}

    def fake_fetch(url: str, timeout: int = 15) -> str:
        return served_text["value"]

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)
    return served_text


# --- registry.yaml ------------------------------------------------------------


def test_no_registry_file_is_no_sources(tmp_path):
    assert load_sources(tmp_path / "absent.yaml") == []


def test_sources_are_read_in_order(tmp_path):
    path = _sources_file(
        tmp_path,
        {
            "version": 1,
            "sources": [
                {"name": "official", "url": "github:acme/skills", "trust": "signed"},
                {"name": "community", "url": "https://example.invalid/index.json"},
            ],
        },
    )

    sources = load_sources(path)

    assert [s.name for s in sources] == ["official", "community"]
    assert sources[0].trust == "signed"
    assert sources[1].trust == "checksum", "the policy default applies when a source declares none"


def test_an_unknown_trust_level_is_rejected(tmp_path):
    path = _sources_file(tmp_path, {"sources": [{"name": "x", "url": "github:a/b", "trust": "vibes"}]})

    with pytest.raises(RegistryError, match="unknown trust"):
        load_sources(path)


def test_a_source_without_a_url_is_rejected(tmp_path):
    path = _sources_file(tmp_path, {"sources": [{"name": "x"}]})

    with pytest.raises(RegistryError, match="no url"):
        load_sources(path)


def test_duplicate_source_names_are_rejected(tmp_path):
    path = _sources_file(
        tmp_path,
        {"sources": [{"name": "x", "url": "github:a/b"}, {"name": "x", "url": "github:c/d"}]},
    )

    with pytest.raises(RegistryError, match="duplicate source"):
        load_sources(path)


def test_a_newer_schema_is_refused_not_guessed(tmp_path):
    path = _sources_file(tmp_path, {"version": 99, "sources": []})

    with pytest.raises(RegistryError, match="newer than this KAOS supports"):
        load_sources(path)


# --- index --------------------------------------------------------------------


def test_a_github_source_reads_the_repository_index():
    url = index_url(RegistrySource(name="official", url="github:acme/skills"))

    assert url == "https://raw.githubusercontent.com/acme/skills/main/index.json"


def test_a_github_subdirectory_source_reads_its_own_index():
    url = index_url(RegistrySource(name="official", url="github:acme/monorepo/kaos"))

    assert url.endswith("/kaos/index.json")


def test_an_http_source_is_used_verbatim():
    assert index_url(RegistrySource(name="c", url="https://x.invalid/i.json")) == "https://x.invalid/i.json"


def test_an_unusable_url_is_an_error():
    with pytest.raises(RegistryError, match="must be github:user/repo or an http"):
        index_url(RegistrySource(name="c", url="ftp://x.invalid/index.json"))


def test_index_entries_carry_their_source_and_trust():
    source = RegistrySource(name="official", url="github:acme/skills", trust="signed")
    payload = json.dumps(
        {"version": 1, "skills": [{"name": "a", "description": "d", "version": "1.0.0", "url": "github:acme/skills/a"}]}
    )

    entries = parse_index(source, payload)

    assert entries[0].source == "official"
    assert entries[0].trust == "signed"


def test_a_bare_list_index_is_accepted():
    entries = parse_index(RegistrySource(name="c", url="https://x.invalid/i.json"), json.dumps([{"name": "a"}]))

    assert [e.name for e in entries] == ["a"]


def test_a_nameless_entry_is_skipped_not_fatal():
    payload = json.dumps({"skills": [{"description": "no name"}, {"name": "good"}]})

    entries = parse_index(RegistrySource(name="c", url="https://x.invalid/i.json"), payload)

    assert [e.name for e in entries] == ["good"]


def test_invalid_json_names_the_source():
    with pytest.raises(RegistryError, match="source 'c' returned invalid JSON"):
        parse_index(RegistrySource(name="c", url="https://x.invalid/i.json"), "not json")


def test_one_unreachable_source_does_not_hide_the_others(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    good = RegistrySource(name="good", url="https://good.invalid/i.json")
    bad = RegistrySource(name="bad", url="https://bad.invalid/i.json")

    def fake_fetch(url: str, timeout: int = 15) -> str:
        if "bad" in url:
            raise OSError("connection refused")
        return json.dumps({"skills": [{"name": "works"}]})

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)

    entries, problems = load_index([bad, good], refresh=True)

    assert [e.name for e in entries] == ["works"]
    assert any("bad" in problem and "unreachable" in problem for problem in problems)


def test_the_index_is_cached_for_offline_search(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    calls = {"n": 0}

    def fake_fetch(url: str, timeout: int = 15) -> str:
        calls["n"] += 1
        return json.dumps({"skills": [{"name": "cached-skill"}]})

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)
    source = [RegistrySource(name="s", url="https://x.invalid/i.json")]

    load_index(source, refresh=True)
    entries, _ = load_index(source)

    assert calls["n"] == 1
    assert [e.name for e in entries] == ["cached-skill"]


def test_a_stale_cache_is_refetched(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path))
    calls = {"n": 0}

    def fake_fetch(url: str, timeout: int = 15) -> str:
        calls["n"] += 1
        return json.dumps({"skills": [{"name": f"skill-{calls['n']}"}]})

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)
    source = [RegistrySource(name="s", url="https://x.invalid/i.json")]

    load_index(source, refresh=True)
    entries, _ = load_index(source, now=9_999_999_999)

    assert calls["n"] == 2
    assert [e.name for e in entries] == ["skill-2"]


# --- search -------------------------------------------------------------------


def test_search_matches_name_and_description():
    entries = [_entry(), _entry(name="launch-plan", description="Plan a launch")]

    assert [e.name for e in search("memo", entries)] == ["decision-memo"]
    assert [e.name for e in search("launch", entries)] == ["launch-plan"]
    assert len(search("", entries)) == 2


def test_find_entry_can_be_pinned_to_a_source():
    entries = [_entry(source="a"), _entry(source="b", version="2.0.0")]

    assert find_entry("decision-memo", entries, source="b").version == "2.0.0"
    assert find_entry("decision-memo", entries, source="nope") is None


# --- install ------------------------------------------------------------------


def test_install_lands_a_draft_when_nothing_vouches_for_it(store, served):
    result = install("decision-memo", store=store, entries=[_entry(trust="none")])

    assert result.installed is True
    assert result.status == "draft"
    assert store.get("decision-memo") is not None


def test_install_reports_an_unknown_name(store, served):
    result = install("nope", store=store, entries=[_entry()])

    assert result.installed is False
    assert "not in the index" in result.reason


def test_install_reports_an_entry_without_a_url(store, served):
    result = install("decision-memo", store=store, entries=[_entry(url="")])

    assert result.installed is False
    assert "no url" in result.reason


def test_install_with_no_sources_says_so(store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_dir", str(tmp_path / "empty-data"))
    monkeypatch.setattr("kronos.skills.registry.load_sources", lambda *a, **k: [])

    result = install("decision-memo", store=store)

    assert result.installed is False
    assert "no sources configured" in result.reason


def test_a_fetch_failure_is_reported_not_raised(store, monkeypatch):
    def broken_fetch(url: str, timeout: int = 15) -> str:
        raise OSError("no route to host")

    monkeypatch.setattr("kronos.skills.hub._fetch_url", broken_fetch)

    result = install("decision-memo", store=store, entries=[_entry()])

    assert result.installed is False
    assert "Failed to fetch" in result.reason


def test_a_checksum_mismatch_between_index_and_skill_keeps_it_a_draft(store, served, tmp_path):
    """The index says one thing, the file says another — that is worth stopping on."""
    checksum = _published_checksum(tmp_path)
    served["value"] = SKILL_MD.replace("author: publisher", f"author: publisher\nchecksum: {checksum}")

    result = install("decision-memo", store=store, entries=[_entry(checksum="sha256:something-else")])

    assert result.status == "draft"
    assert "advertises a different checksum" in result.reason


def test_a_tampered_skill_stays_a_draft(store, served, tmp_path):
    checksum = _published_checksum(tmp_path)
    served["value"] = (
        SKILL_MD.replace("author: publisher", f"author: publisher\nchecksum: {checksum}") + "\n3. Wire the money.\n"
    )

    result = install("decision-memo", store=store, entries=[_entry(checksum=checksum)])

    assert result.status == "draft"
    assert "mismatch" in result.reason
    assert result.report["checksum_ok"] is False


def test_a_checksum_source_does_not_activate_on_checksum_alone(store, served, tmp_path):
    """A matching checksum proves integrity, not authorship."""
    checksum = _published_checksum(tmp_path)
    served["value"] = SKILL_MD.replace("author: publisher", f"author: publisher\nchecksum: {checksum}")

    result = install("decision-memo", store=store, entries=[_entry(checksum=checksum, trust="checksum")])

    assert result.status == "draft"
    assert result.report["checksum_ok"] is True


def test_a_signed_source_without_a_signature_refuses_to_activate(store, served, tmp_path):
    checksum = _published_checksum(tmp_path)
    served["value"] = SKILL_MD.replace("author: publisher", f"author: publisher\nchecksum: {checksum}")

    result = install("decision-memo", store=store, entries=[_entry(checksum=checksum, trust="signed")])

    assert result.status == "draft"
    assert "source requires a signature" in result.reason


def test_an_incompatible_skill_stays_a_draft(store, served, monkeypatch):
    served["value"] = SKILL_MD.replace("author: publisher", 'author: publisher\nrequires_kaos: ">=99"')
    monkeypatch.setattr("kronos.skills.integrity.check_compatibility", lambda skill, version="": (False, "needs 99"))

    result = install("decision-memo", store=store, entries=[_entry(trust="none")])

    assert result.status == "draft"
    assert "needs 99" in result.reason


def test_installing_twice_is_refused_before_any_fetch(store, served):
    """The name conflict is known locally, so it costs no round trip."""
    install("decision-memo", store=store, entries=[_entry(trust="none")])

    second = install("decision-memo", store=store, entries=[_entry(trust="none")])

    assert second.installed is False
    assert "already installed" in second.reason


# --- the one path that activates -----------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not available")
def test_a_signed_skill_from_a_signed_source_installs_active(store, served, tmp_path, monkeypatch):
    """The phase's point: a key you configured can stand in for a manual read."""
    key = tmp_path / "publisher_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "publisher@example.invalid", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()

    checksum = _published_checksum(tmp_path)
    payload = tmp_path / "checksum.txt"
    payload.write_text(checksum, encoding="utf-8")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "kaos-skill", str(payload)],
        check=True,
        capture_output=True,
    )
    signature = "".join(payload.with_suffix(".txt.sig").read_text(encoding="utf-8").strip().splitlines()[1:-1])
    served["value"] = SKILL_MD.replace(
        "author: publisher",
        f"author: publisher\nchecksum: {checksum}\nsignature: {signature}",
    )

    from kronos import policy as policy_module

    monkeypatch.setattr(
        policy_module,
        "_active",
        policy_module.Policy(registry={"trusted_keys": [public_key]}),
    )

    result = install(
        "decision-memo",
        store=store,
        entries=[_entry(checksum=checksum, trust="signed", signed=True)],
    )

    assert result.status == "active", result.reason
    assert result.report["signature_ok"] is True
    assert store.get("decision-memo").status == "active"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not available")
def test_a_signature_from_an_unconfigured_key_does_not_activate(store, served, tmp_path, monkeypatch):
    key = tmp_path / "stranger_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "stranger@example.invalid", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    checksum = _published_checksum(tmp_path)
    payload = tmp_path / "checksum.txt"
    payload.write_text(checksum, encoding="utf-8")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "kaos-skill", str(payload)],
        check=True,
        capture_output=True,
    )
    signature = "".join(payload.with_suffix(".txt.sig").read_text(encoding="utf-8").strip().splitlines()[1:-1])
    served["value"] = SKILL_MD.replace(
        "author: publisher",
        f"author: publisher\nchecksum: {checksum}\nsignature: {signature}",
    )

    from kronos import policy as policy_module

    monkeypatch.setattr(policy_module, "_active", policy_module.Policy(registry={"trusted_keys": []}))

    result = install("decision-memo", store=store, entries=[_entry(checksum=checksum, trust="signed", signed=True)])

    assert result.status == "draft"
    assert "no trusted keys" in result.reason


# --- reference files (phase 12 follow-up) --------------------------------------

REFERENCE_MD = "# Memo template\n\n- Decision:\n- Alternatives:\n"


@pytest.fixture
def served_with_reference(monkeypatch):
    """Serve SKILL.md and one reference, addressed by URL."""
    payloads = {
        "https://example.invalid/decision-memo/SKILL.md": SKILL_MD,
        "https://example.invalid/decision-memo/references/template.md": REFERENCE_MD,
    }
    requested: list[str] = []

    def fake_fetch(url: str, timeout: int = 15) -> str:
        requested.append(url)
        if url in payloads:
            return payloads[url]
        raise OSError("HTTP Error 404: Not Found")

    monkeypatch.setattr("kronos.skills.hub._fetch_url", fake_fetch)
    return payloads, requested


def test_declared_references_are_installed(store, served_with_reference):
    _, requested = served_with_reference

    result = install("decision-memo", store=store, entries=[_entry(trust="none", references=["template"])])

    assert result.installed is True
    reference = store.get("decision-memo").path.parent / "references" / "template.md"
    assert reference.is_file()
    assert "Memo template" in reference.read_text(encoding="utf-8")
    assert "references/template.md" in " ".join(requested)


def test_a_reference_is_usable_without_a_restart(store, served_with_reference):
    """load_skill_reference reads the loaded skill, not the directory."""
    install("decision-memo", store=store, entries=[_entry(trust="none", references=["template"])])

    assert "Memo template" in store.get_reference("decision-memo", "template")


def test_a_skill_with_references_can_verify_its_checksum(store, served_with_reference, tmp_path):
    """The checksum covers reference files, so leaving them behind broke it."""
    published = tmp_path / "published" / "decision-memo"
    (published / "references").mkdir(parents=True)
    (published / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (published / "references" / "template.md").write_text(REFERENCE_MD, encoding="utf-8")
    checksum = compute_checksum(published)

    payloads, _ = served_with_reference
    payloads["https://example.invalid/decision-memo/SKILL.md"] = SKILL_MD.replace(
        "author: publisher", f"author: publisher\nchecksum: {checksum}"
    )

    result = install(
        "decision-memo",
        store=store,
        entries=[_entry(checksum=checksum, trust="checksum", references=["template"])],
    )

    assert result.report["checksum_ok"] is True, result.reason


def test_a_missing_reference_is_reported_and_blocks_activation(store, served_with_reference):
    result = install("decision-memo", store=store, entries=[_entry(trust="none", references=["absent"])])

    assert result.installed is True
    assert result.status == "draft"
    assert "reference 'absent.md' could not be fetched" in result.reason


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "sub/dir.md", "..", ".hidden"])
def test_a_hostile_reference_name_is_refused(store, served_with_reference, hostile):
    """The index is remote input; a name with a separator would write anywhere."""
    result = install("decision-memo", store=store, entries=[_entry(trust="none", references=[hostile])])

    assert f"refused unsafe reference name '{hostile}'" in result.reason
    assert not (store.get("decision-memo").path.parent.parent / "passwd").exists()


def test_a_skill_without_references_fetches_nothing_extra(store, served_with_reference):
    _, requested = served_with_reference

    install("decision-memo", store=store, entries=[_entry(trust="none")])

    assert not any("references/" in url for url in requested)
