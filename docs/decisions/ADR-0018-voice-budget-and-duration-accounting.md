# ADR-0018: Voice admission and duration-based accounting

**Date:** 2026-09-08
**Status:** accepted

## Context

The direct-model inventory found that Telegram voice transcription called Groq
Whisper before the ordinary agent budget check and never recorded its spend.
Unlike text models, Whisper is billed by audio duration. The bridge also deleted
its temporary input only on success or Exception, not on task cancellation, and
logged raw provider error bodies that could contain private input.

## Requirements

- Apply the shared daily/session/execution admission before file or HTTP work.
- Preserve ASR capability under a text-tier downgrade.
- Account known duration before response/client cleanup or transcript validation.
- Reject unknown duration explicitly; do not invent free spend or text usage.
- Clean up temporary input on cancellation and avoid raw provider-error logging.
- Keep durable unknown-cost reconciliation and hard monetary reservations open.

## Decision

Use the existing fixed-model boundary with lite compatibility for the already
selected whisper-large-v3-turbo. The model is not replaced with a text model, and
hard daily/session refusal still prevents dispatch. Soft downgrade retains this
low-cost ASR path. This policy is not blanket lite permission for arbitrary future
audio providers; changing the model requires an explicit price/policy decision.

Request verbose_json and validate the returned duration as a finite positive
number. Estimate cost from the published turbo rate ($0.04/hour) with the documented
10-second minimum. Record the request/cost with zero text-token counts, in the
existing media audit scope. Record before checking the text and before any async
cleanup boundary. Empty or non-string text is a failure, not successful ingestion.

Missing or invalid duration produces ModelUsageUnknownError. No zero-cost success
record or automatic retry is fabricated. This exception does not yet create a
durable unknown-cost receipt: that remains a required ledger-stage deliverable.

Use context managers for the open audio file and both HTTP lifetimes. Move bridge
temporary-file deletion into finally so cancellation and failed error delivery
cannot skip it. Report budget refusal separately; generic voice failures expose
only the exception type to logs and a fixed message to the user, not provider text.

## Alternatives

### Estimate audio cost from transcript tokens

Rejected: silence, language and speaking speed disconnect text length from billed
audio duration. This would keep systematically incorrect accounting.

### Substitute the configured text lite model

Rejected: it cannot transcribe the uploaded audio. The configured turbo model is
already the intended low-cost ASR implementation.

### Treat missing duration as zero or trust Telegram metadata as exact billing

Rejected: neither proves the provider's measured duration or invoice. An explicit
unknown result is preferable to a fabricated accounted success.

## Consequences

### Positive

- Voice no longer bypasses pre-dispatch admission or successful-response spend.
- Session attribution matches the subsequent Telegram model invocation.
- Empty text and cleanup failures cannot discard already received valid usage.
- Cancellation does not leave the bridge's temporary recording on disk.

### Negative

- Provider timeout, cancellation before usable usage, invalid duration and recorder
  write failures remain unknown costs without durable receipts/reconciliation.
  The shared recorder remains best effort; this is not a hard-dollar-cap claim.
- Rates are a dated published-price estimate, not a per-account invoice. Live
  provider acceptance of the duration contract still requires a controlled test.
- Rejecting a response without valid duration sacrifices its transcript rather
  than silently accepting an unaccounted success.

### Neutral

- No dependency, schema, runtime configuration or production change is needed.
- Existing aggregate reporting rounds dollars, but SQLite and session tally retain
  the unrounded calculated amount. Tests check both contracts independently.

## Verification

Fake HTTP transport and a real isolated shared ledger cover billing minimum,
fractional/long duration, session/daily/accounting failure admission, soft downgrade,
invalid duration/text, execution stop, HTTP errors, client-close failure and task
cancellation. Registered bridge handlers also exercise cancellation, budget refusal
and generic failures with actual local temporary files. Real Groq is not called.

## References

- [Groq speech-to-text pricing and formats](https://console.groq.com/docs/speech-to-text),
  checked 2026-09-08: turbo rate, 10-second minimum and verbose_json support.
- [Vercel AI SDK Groq response implementation](https://raw.githubusercontent.com/vercel/ai/main/packages/groq/src/groq-transcription-model.ts):
  optional numeric duration from verbose responses; implementation evidence, not
  proof that every provider response contains valid duration.
- `tests/test_voice_budget.py`, ADR-0017 and the F12 remediation register.
