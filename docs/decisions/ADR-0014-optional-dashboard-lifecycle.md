# ADR-0014: Optional dashboard lifecycle and explicit service failure

**Date:** 2026-09-08
**Status:** accepted

## Context

F17 reproduced a startup failure with the real application and dashboard runner:
the dashboard returned when no authentication password was available, causing
FIRST_COMPLETED supervision to cancel the bridge and scheduler. The process then
returned success, so an on-failure restart policy could not distinguish it from
a requested shutdown. ADR-0013 already keeps one-shot recovery supervised.

## Requirements

- A missing dashboard password must never expose an unauthenticated HTTP server.
- Explicitly disabled dashboard startup must not stop the agent.
- Unexpected service return, cancellation or exception must not appear successful.
- Signal/caller cancellation must still unwind services and the MCP context.
- Standalone dashboard CLI must not hang or report success without a server.

## Decision

The dashboard runner returns False only for its explicit no-password disabled
state, and True after an enabled server finishes serving. Exceptions propagate.
The application wraps the runner: only False becomes a cancellable idle wait,
consistent with the existing disabled Discord and completed recovery lifecycle.
It does not treat arbitrary None/clean returns as disabled.

After cancelling and joining services, the supervisor propagates original
exceptions first. Without a requested signal, an unexpected service return or
self-cancellation raises RuntimeError. A requested signal plus a clean service
completion remains success; an actual exception is not masked by the signal.
The standalone CLI returns failure for False and does not install an idle wait.

## Alternatives

### Keep every dashboard return alive

Rejected: this would hide unexpected shutdown of a previously enabled server.
The supervisor needs an explicit disabled outcome, not an assumption based on
the absence of an exception.

### Make the dashboard runner always sleep when disabled

Rejected: that also makes the standalone dashboard command hang with no server.
The application owns the long-running accessory lifecycle, not the CLI runner.

### Replace supervision with a generic service framework

Deferred: it adds unrelated lifecycle policy and deployment scope. A small
explicit outcome preserves the current supervision and dependency structure.

## Consequences

### Positive

- No-password startup stays fail-closed without stopping useful agent services.
- Unexpected service exits are visible as failures after coordinated cleanup.
- CLI users receive a nonzero result when the dashboard cannot start.

### Negative

- Once enabled, dashboard failure still stops the process; automatic accessory
  restart/degraded readiness is not implemented by this fix.
- Code callers of run_dashboard must respect the explicit boolean contract.

### Neutral

- No new dependency, configuration setting, systemd change or port is introduced.
- Readiness/alerts for disabled accessories remain part of PROD-08.
- Local tests do not authorize or prove production rollout/systemd acceptance.

## Verification

Real main/run_dashboard with synthetic services tests no-password startup,
signals, caller cancellation and resource cleanup. Every long-running service
is exercised with return, exception and self-cancellation. The standalone CLI
and the enabled dashboard return contract have separate regression cases.

## References

- `tests/test_app_supervision.py`, `tests/test_recovery_startup.py`.
- ADR-0013: Transactional recovery delivery.
- `docs/reviews/2026-09-08-remediation.md`: F17 and PROD-08.
