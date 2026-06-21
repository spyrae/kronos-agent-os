# Documents Collector V2 Design

Documents Collector is a future, safety-gated pipeline for contracts,
invoices, acts, PDFs, DOCX files, images, and other legal/financial
attachments. It is deliberately separate from Observer MVP because raw
documents often contain PII, payments, signatures, bank data, tax IDs, and
legally meaningful clauses.

This document defines the implementation plan only. RB-1286 does **not**
download real documents, change Telegram bridge runtime behavior, add new
dependencies, or send document contents to external LLMs.

## Goals

- Detect explicit document attachments from approved sources.
- Download locally only after a gated collector is enabled.
- Compute checksum before classification/storage.
- Classify type and confidence without logging document body.
- Route confident matches to project document folders.
- Quarantine uncertain or sensitive candidates by default.
- Keep an audit/index trail with source metadata, checksum, confidence, and
  review status.

## Non-goals for v1

- Background auto-enrichment of existing workspaces.
- External LLM processing of raw contracts or images.
- OCR/DOCX/PDF dependencies as hard runtime requirements.
- Email attachment ingestion in the first implementation slice.
- Automatic deletion of originals before manual review policy exists.

## Pipeline

```text
detect attachment
  ↓
download locally (gated, local-only)
  ↓
compute sha256 checksum + normalized filename
  ↓
classify document type + confidence
  ↓
extract safe metadata only
  ↓
match to project/company
  ↓
if confident: store file + index note
else: quarantine + notify user
```

## Workspace layout

```text
workspace/notes/world/projects/{project}/documents/
workspace/ops/documents/index.jsonl
workspace/ops/documents/quarantine/
workspace/ops/documents/audit.jsonl
```

- `notes/world/projects/{project}/documents/` contains accepted files and
  optional markdown index notes.
- `ops/documents/index.jsonl` is an append-only inventory with checksums,
  source metadata, classification confidence, and review status.
- `ops/documents/quarantine/` stores uncertain files until manual review.
- `ops/documents/audit.jsonl` records collector actions without document body.

## Models

The pure model contracts live in `kronos/documents/models.py`.

### DocumentCandidate

Pre-classification attachment candidate:

- `source_kind`: `telegram_attachment` now, `email_attachment` later.
- `source_id`: stable source message/email id.
- `filename`: normalized path-safe filename.
- `checksum_sha256`: checksum computed before storage.
- `content_type`, `size_bytes`, `detected_at`.
- `source_metadata`: PII-minimized metadata such as chat id/message id, not
  raw document text.

### DocumentClassification

Conservative classifier output:

- `document_type`: contract, invoice, act, receipt, pdf, docx, image, unknown.
- `confidence`: `0.0..1.0`; confident threshold is `>= 0.8`.
- `reasons`: short non-PII reasons, e.g. `filename_contains_invoice`.
- `extracted_metadata`: safe metadata only, e.g. dates, currency, invoice id,
  counterparty names if already visible in filename/metadata.

### ProjectMatch

Project/company routing decision:

- `project_slug`: normalized project folder slug.
- `company`: optional display name.
- `confidence`: `0.0..1.0`; confident threshold is `>= 0.8`.
- `reason`: non-PII routing reason.

### StoredDocument

Append-only index entry for accepted or quarantined files:

- original `candidate`;
- `classification`;
- `project_match`;
- `storage_path` and optional `index_note_path`;
- `review_status`: quarantined, stored, needs_review, rejected;
- `stored_at`.

## Storage policy

1. Normalize filename with basename-only rules. Path separators and traversal are
   discarded.
2. Prefix stored filenames with the first 12 chars of `sha256`:
   `{checksum12}-{normalized_filename}`.
3. If classification or project match is not confident, destination is always:

   ```text
   workspace/ops/documents/quarantine/{checksum12}-{filename}
   ```

4. Only confident classification **and** confident project match can route to:

   ```text
   workspace/notes/world/projects/{project}/documents/{checksum12}-{filename}
   ```

5. Never log or index raw document body. Index records may include checksum,
   filename, content type, size, source id, confidence, and review status.
6. Duplicate checksum should dedupe or link to existing file in a future storage
   service.

## Sources v1

### Telegram attachments

Initial detector should observe only allowed DM/group contexts and emit
`DocumentCandidate` records. Downloading is gated behind a separate collector
setting and must not happen in RB-1286.

Candidate source metadata may include:

- chat id/message id;
- sender id (masked in logs);
- Telegram document id/access hash only if needed to re-fetch;
- original filename/content type/size.

### Email attachments (later)

Email ingestion is a separate source adapter. It must use the same model,
checksum, quarantine, and audit policy.

## Extractor strategy

Extraction is staged and optional. All extractors must be local-first by
default and produce safe metadata, not raw body logs.

1. PDF text: built-in/local extractor when available; otherwise classify by
   filename/MIME and quarantine.
2. Images: OCR only behind an explicit OCR feature flag. Do not send images to
   external OCR/LLM by default.
3. DOCX/Office: local conversion/extraction if available. MarkItDown can be
   evaluated later but is not a hard dependency.
4. Unknown binary files: quarantine.

## Review flow

```text
candidate + checksum
  ↓
classification confidence >= 0.8?
  ├─ no → quarantine + notify
  └─ yes
      ↓
project match confidence >= 0.8?
  ├─ no → quarantine + notify
  └─ yes → store under project documents + append index
```

Notifications should include only filename, source, type guess, confidence, and
review path. They must not include document text.

## Privacy and security rules

- Raw contracts never go to external LLMs without a separate explicit mode.
- No document contents in logs, traces, run metadata, or Telegram replies.
- PII-safe summaries only; if unsure, quarantine.
- All writes are local workspace writes.
- The collector must be opt-in and auditable.
- Unknown MIME, low confidence, missing project, path traversal attempts, or
  suspicious filenames go to quarantine.

## Implementation plan

1. RB-1286: design doc and pure model contracts (this step).
2. Attachment detector: identify Telegram document/media messages and create
   `DocumentCandidate` without downloading.
3. Local download gate: explicit setting + checksum + quarantine-only storage.
4. Classifier v1: filename/MIME heuristics with confidence and reasons.
5. Project matcher v1: configured project aliases; uncertain goes quarantine.
6. Review command: list quarantine, approve/reject/move to project.
7. Optional extractors: PDF text, OCR, DOCX conversion, each behind explicit
   feature flags.

## Acceptance checks

- Unit tests cover serialization, checksum, and path safety.
- No network tests by default.
- No runtime side effects from importing models.
- No changes to Telegram bridge in RB-1286.
