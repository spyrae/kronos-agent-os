# ADR-0017: Direct model admission and explicit billing identity

**Date:** 2026-09-08
**Status:** accepted

## Context

ADR-0015 protects factory-backed calls, but ASO, GEO measurements, Vision and
two scripts dispatch directly through HTTP, the OpenAI SDK or Codex CLI.
Vision also runs before the main Telegram invocation establishes its audit
scope. Their spend was missing from the common budget boundary. Name-only zero
prices incorrectly treated an API model as free when it shared a Codex name.

## Requirements

- Check the existing daily/session budget before each explicit provider attempt.
- Preserve the required modality and measured engine when a downgrade is active.
- Attribute media processing to the same chat budget as the later invocation.
- Record returned usage before client cleanup can fail; account empty responses
  without presenting them as successful work.
- Distinguish subscription billing from API billing without changing configuration.
- Do not claim a hard monetary ceiling or completeness of all model paths.

## Decision

Introduce a small direct-model boundary using the existing admission and cost
recorder. ASO and script text calls may use the configured factory lite model.
GEO must instead return a budget error: substituting another model would corrupt
the meaning of a measurement. OpenAI Vision likewise refuses a soft downgrade
because no compatible lite vision replacement has been configured. Subscription
Codex Vision remains compatible with a soft downgrade but obeys hard admission
refusal. It retains the owned process lifecycle from ADR-0016.

Response usage supports mapping and SDK-object formats. Missing token counts use
the existing text-length estimate, not a claim of zero API spend. Usage is recorded
inside response/client contexts before they close. Empty model output is accounted
and then rejected. The media scope copies/restores audit context and assigns the
chat session ID used by the normal Telegram invocation.

Bind each factory cost callback to its provider's model and billing kind. Only the
Codex adapter defaults to subscription marginal pricing; an identical model name
sent to an API gets API pricing. Reported model metadata takes precedence over the
configured fallback name. Existing environment price overrides remain effective.

## Alternatives

### Force all paths through the text factory

Rejected: text replacement cannot preserve Vision input semantics or the engine
identity of a GEO measurement. It would turn a budget response into incorrect data.

### Add independent guards and pricing to every transport

Rejected: independent copies would drift again. The shared admission, accounting
and explicit fixed-model policy cover these paths without replacing their clients.

### Infer subscription pricing from the model name

Rejected: the same name can be used through a paid API. Billing belongs to the
adapter contract, while provider prices and missing-usage estimates remain separate.

## Consequences

### Positive

- Explicit fallback attempts cannot bypass an updated common budget decision.
- A cleanup error does not erase already returned usage.
- Vision spend is attributed before the main agent starts, including rejected OCR.
- Direct API calls no longer inherit free pricing merely from a model name.

### Negative

- Session tallies are still in memory; admission against completed spend cannot
  cap concurrent in-flight requests across processes.
- Recorder writes remain best effort. SDK-internal retries, timeout/cancellation
  with unknown provider outcomes, durable reservations and reconciliation remain
  required. The helper does not solve or silently discard those requirements.
- Fallback text-length usage excludes reliable image-token accounting. Default
  prices are estimates, not invoices or upper bounds. A zero-cost subscription
  record describes marginal API spend, not subscription quota availability.
- Mem0's internal LLM and Groq Whisper transcription remain separate uncovered
  paths. Adding a media scope does not by itself guard or account transcription.

### Neutral

- No dependencies, runtime configuration, schemas or production services change.
- Standalone script summarization now requires the existing runtime; non-model
  recall indexing/search keeps its lazy-import behavior.
- F12 remains partial until the uncovered paths and durable monetary contracts
  have their own implementation and acceptance evidence.

## Verification

Fake HTTP/SDK/model transports with a real isolated SQLite ledger cover normal
usage, missing usage, soft/hard budget decisions, fallback rechecks, fixed-engine
refusal, adapter-specific pricing, media attribution/context restoration, empty
Vision responses and client-close failures. No real provider is called. Full
regression and local process crash suites are recorded in the remediation log.

## References

- `kronos/security/direct_model.py`, `cost_tracking.py`, `model_budget.py`.
- `tests/test_direct_model_budget.py`, `tests/test_cost_tracking.py`.
- ADR-0015 and ADR-0016.
- `docs/reviews/2026-09-08-remediation.md`: F12 remaining requirements.
