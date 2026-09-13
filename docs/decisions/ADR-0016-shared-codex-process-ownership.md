# ADR-0016: Shared Codex process ownership for text and vision

**Date:** 2026-09-08
**Status:** accepted

## Context

The F12 caller inventory found a second Codex subprocess implementation in
Vision. The text adapter already owned spawn/communication/cleanup, but Vision
only awaited communicate with a timeout, then deleted temporary files. A timed
out or cancelled Vision invocation could leave its process tree running.

## Requirements

- Keep ownership when cancellation arrives before the process handle does.
- Terminate, then kill and reap the invocation's isolated POSIX process group.
- Repeated caller cancellation cannot interrupt cleanup or release input files.
- Do not signal an unrelated process group.
- Preserve existing Vision arguments and successful OCR output.

## Decision

Extract the existing async text-adapter lifecycle into run_codex_command in the
same module. The caller supplies an argv builder; the helper owns the private
output file, process group, communication task and shielded cleanup. No shell
is introduced. ChatCodexCLI delegates its async path to this helper unchanged.

Vision retains ownership of the image file around the entire helper call, so
the image is deleted only after process cleanup completes. Its flags and model
selection remain unchanged. The shared result reader also rejects empty output
instead of presenting an empty OCR result as success.

## Alternatives

### Copy the cleanup logic into Vision

Rejected: the bug itself resulted from two implementations drifting. Reusing the
existing tested ownership primitive avoids another independent cancellation path.

### Subclass the chat model only to attach an image

Rejected: Vision does not need chat-to-tool-output parsing or a new prompt format.
Sharing the process primitive preserves both callers' existing argv contracts.

## Consequences

### Positive

- Text and Vision share spawn-race, timeout and repeated-cancellation behavior.
- Temporary input and output lifetimes cover the complete owned operation.
- Actual local descendant-process tests complement fake transport tests.

### Negative

- POSIX process groups cannot contain a deliberately escaped daemon; this is not
  a new OS sandbox or an assertion that arbitrary subprocess code is contained.
- Linux/live Codex acceptance remains required before production closure.

### Neutral

- No dependency, configuration or systemd changes are introduced.
- Budget admission for Vision remains a separate F12 requirement.

## Verification

Local Python subprocesses replace Codex at the launch boundary. Tests exercise
timeout with live/exited leader, repeated cancellation, cancellation during
launch, unrelated process survival, missing executable, nonzero/empty/successful
output, and deletion of both input image and output file. No provider is called.

## References

- `tests/test_vision_cleanup.py`, `tests/test_llm_codex_cleanup.py`.
- `kronos/llm_codex.py`, `kronos/vision.py`.
- `docs/reviews/2026-09-08-remediation.md`: F16 and F12.
