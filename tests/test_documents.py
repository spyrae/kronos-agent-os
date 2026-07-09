"""Document ingest — text extraction (roadmap 6.1)."""

from types import SimpleNamespace

from telethon.tl.types import DocumentAttributeFilename

from kronos.bridge import _compose_document_agent_message, _is_document_message
from kronos.documents.extract import MAX_EXTRACT_CHARS, extract_text


def test_extract_txt_and_md():
    text, err = extract_text(b"line one\nline two", "note.txt", "text/plain")
    assert err == "" and text == "line one\nline two"
    text, err = extract_text(b"# Title", "doc.md", "")
    assert err == "" and "Title" in text


def test_extract_by_mime_without_extension():
    text, err = extract_text(b"plain body", "attachment", "text/plain")
    assert err == "" and text == "plain body"


def test_extract_pdf_without_dep_gives_hint():
    text, err = extract_text(b"%PDF-1.4 ...", "report.pdf", "application/pdf")
    assert text == ""
    assert "documents" in err.lower()  # install-extra hint


def test_extract_docx_without_dep_gives_hint():
    text, err = extract_text(b"PK\x03\x04", "report.docx", "")
    assert text == ""
    assert "documents" in err.lower()


def test_extract_unsupported_type():
    text, err = extract_text(b"data", "archive.zip", "application/zip")
    assert text == ""
    assert "Неподдерживаемый" in err


def test_extract_empty_document():
    text, err = extract_text(b"   \n  ", "empty.txt", "text/plain")
    assert text == ""
    assert "извлечь" in err.lower()


def test_extract_truncates_huge_document():
    big = b"x" * (MAX_EXTRACT_CHARS + 1000)
    text, err = extract_text(big, "big.txt", "text/plain")
    assert err == ""
    assert "обрезано" in text
    assert len(text) <= MAX_EXTRACT_CHARS + 20


def test_compose_document_agent_message():
    msg = _compose_document_agent_message("что тут важного?", "contract.pdf", "текст договора")
    assert "contract.pdf" in msg
    assert "текст договора" in msg
    assert "что тут важного?" in msg


def _doc_event(filename: str, mime: str):
    doc = SimpleNamespace(
        attributes=[DocumentAttributeFilename(file_name=filename)],
        mime_type=mime,
    )
    media = SimpleNamespace(document=doc)
    return SimpleNamespace(message=SimpleNamespace(media=media, photo=None))


def test_is_document_message_detection():
    assert _is_document_message(_doc_event("report.pdf", "application/pdf")) is True
    assert _is_document_message(_doc_event("notes.txt", "text/plain")) is True
    assert _is_document_message(_doc_event("song.mp3", "audio/mpeg")) is False
    # no media at all
    assert _is_document_message(SimpleNamespace(message=SimpleNamespace(media=None))) is False
