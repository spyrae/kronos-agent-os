"""Document text extraction for ingest (roadmap 6.1).

PDF/DOCX need optional deps (pip install kronos-agent-os[documents]); .txt/.md
work without them. All extractors fail soft — a missing dep or unreadable file
returns a user-facing hint, never an exception.
"""

import io

# Cap extracted text so a huge document can't blow up LLM context / cost.
MAX_EXTRACT_CHARS = 40_000


def extract_text(data: bytes, filename: str, mime_type: str = "") -> tuple[str, str]:
    """Extract plain text from a document. Returns (text, error).

    error is a non-empty user-facing hint when extraction isn't possible.
    """
    lower = filename.lower()
    mime = mime_type.lower()

    if lower.endswith(".pdf") or "pdf" in mime:
        text, error = _extract_pdf(data)
    elif lower.endswith(".docx") or "wordprocessingml" in mime:
        text, error = _extract_docx(data)
    elif lower.endswith((".txt", ".md", ".markdown")) or mime.startswith("text/"):
        text, error = data.decode("utf-8", errors="replace"), ""
    else:
        return "", f"Неподдерживаемый тип документа: {filename or mime or 'unknown'}."

    if error:
        return "", error
    text = text.strip()
    if not text:
        return "", "Не удалось извлечь текст (документ пустой или отсканирован как картинка)."
    if len(text) > MAX_EXTRACT_CHARS:
        text = text[:MAX_EXTRACT_CHARS] + "\n…[обрезано]"
    return text, ""


def _extract_pdf(data: bytes) -> tuple[str, str]:
    try:
        import pypdf
    except ImportError:
        return "", "PDF-инжест недоступен: pip install kronos-agent-os[documents]."
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages), ""
    except Exception as e:
        return "", f"Не удалось прочитать PDF: {e}"


def _extract_docx(data: bytes) -> tuple[str, str]:
    try:
        import docx
    except ImportError:
        return "", "DOCX-инжест недоступен: pip install kronos-agent-os[documents]."
    try:
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs), ""
    except Exception as e:
        return "", f"Не удалось прочитать DOCX: {e}"
