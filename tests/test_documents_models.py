from pathlib import Path

from kronos.documents.models import (
    DocumentCandidate,
    DocumentClassification,
    DocumentSourceKind,
    DocumentType,
    ProjectMatch,
    ReviewStatus,
    StoredDocument,
    normalize_filename,
    normalize_project_slug,
    planned_storage_path,
    sha256_bytes,
    should_quarantine,
)


def test_document_candidate_from_bytes_computes_checksum_and_normalizes_filename():
    candidate = DocumentCandidate.from_bytes(
        source_kind=DocumentSourceKind.TELEGRAM_ATTACHMENT,
        source_id="chat:1/msg:2",
        filename="../Invoice June 2026!!.PDF",
        content=b"invoice-content",
        content_type="application/pdf",
        detected_at="2026-06-19T12:00:00Z",
    )

    assert candidate.filename == "Invoice-June-2026.pdf"
    assert candidate.checksum_sha256 == sha256_bytes(b"invoice-content")
    assert candidate.size_bytes == len(b"invoice-content")
    assert candidate.content_type == "application/pdf"
    assert ".." not in candidate.filename
    assert "/" not in candidate.filename


def test_document_model_roundtrips_serialization():
    candidate = DocumentCandidate.from_bytes(
        source_kind="telegram_attachment",
        source_id="chat:1/msg:2",
        filename="contract.pdf",
        content=b"contract",
        detected_at="2026-06-19T12:00:00Z",
        source_metadata={"chat_id": "1"},
    )
    classification = DocumentClassification(
        document_type=DocumentType.CONTRACT,
        confidence=0.91,
        reasons=("filename_contains_contract",),
        extracted_metadata={"currency": "USD"},
    )
    project_match = ProjectMatch(
        project_slug="example-project",
        company="Example Inc",
        confidence=0.85,
        reason="alias_match",
    )
    stored = StoredDocument(
        candidate=candidate,
        classification=classification,
        project_match=project_match,
        storage_path="notes/world/projects/example-project/documents/file.pdf",
        index_note_path="notes/world/projects/example-project/documents/file.md",
        review_status=ReviewStatus.STORED,
        stored_at="2026-06-19T12:01:00Z",
    )

    reloaded = StoredDocument.from_dict(stored.to_dict())

    assert reloaded == stored
    assert reloaded.to_dict()["classification"]["document_type"] == "contract"
    assert reloaded.to_dict()["review_status"] == "stored"


def test_uncertain_classification_or_project_match_goes_to_quarantine(tmp_path):
    candidate = DocumentCandidate.from_bytes(
        source_kind=DocumentSourceKind.TELEGRAM_ATTACHMENT,
        source_id="msg:1",
        filename="unknown.bin",
        content=b"binary",
    )
    classification = DocumentClassification(document_type=DocumentType.UNKNOWN, confidence=0.2)
    project_match = ProjectMatch(project_slug="Client A", confidence=0.95)

    path = planned_storage_path(
        candidate,
        classification,
        project_match,
        workspace_root=tmp_path,
    )

    assert should_quarantine(classification, project_match) is True
    assert path.parent == tmp_path / "ops" / "documents" / "quarantine"
    assert path.name.startswith(candidate.checksum_sha256[:12])


def test_confident_classification_and_project_match_routes_to_project_documents(tmp_path):
    candidate = DocumentCandidate.from_bytes(
        source_kind=DocumentSourceKind.TELEGRAM_ATTACHMENT,
        source_id="msg:1",
        filename="contract.pdf",
        content=b"contract",
    )
    classification = DocumentClassification(document_type=DocumentType.CONTRACT, confidence=0.95)
    project_match = ProjectMatch(project_slug="Client A / Core", confidence=0.9)

    path = planned_storage_path(
        candidate,
        classification,
        project_match,
        workspace_root=tmp_path,
    )

    assert should_quarantine(classification, project_match) is False
    assert path.parent == tmp_path / "notes" / "world" / "projects" / "client-a-core" / "documents"
    assert path.name.endswith("-contract.pdf")
    assert path.resolve(strict=False).is_relative_to(Path(tmp_path).resolve(strict=False))


def test_filename_and_project_slug_normalization_are_path_safe():
    assert normalize_filename("../../secret договор 2026.docx") == "secret-2026.docx"
    assert normalize_filename("\x00") == "document"
    assert normalize_project_slug("Client A / Core") == "client-a-core"
    assert normalize_project_slug("../../etc/passwd") == "etc-passwd"
