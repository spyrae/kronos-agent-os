# Hash usage review

**Reviewed:** 2026-07-13

This review covers every former Bandit `B324` finding in production code.
None of these hashes verifies passwords, credentials, request signatures,
authorization decisions, or untrusted integrity data. They are deterministic
identifiers and change-detection fingerprints. They now use SHA-256 rather
than relying on `usedforsecurity=False`, so the implementation remains safe if
one later becomes security-sensitive.

| Location | Purpose | Security boundary |
| --- | --- | --- |
| `dashboard/api/anomalies.py` | Short, deduplicated dashboard anomaly IDs | Display-only IDs derived from local telemetry. |
| `dashboard/api/audit_trail.py` | Stable IDs for audit and tool-call list entries | UI pagination and rendering only; no authorization. |
| `dashboard/api/overview.py` | Activity-card ID | UI reconciliation only. |
| `kronos/agents/knowledge_pipeline/queue.py` | Task filename suffix | Collision-resistant queue naming; the filesystem path is still validated separately. |
| `kronos/competitors/web_fetchers.py` | Observed-page change fingerprint | Detects content changes; it does not establish provenance. |
| `kronos/security/loop_detector.py` | Equality comparison for loop detection | Runtime comparison key only; never an access-control decision. |
| `kronos/tools/sandbox_platform.py` | Deterministic sandbox-run ID | Correlates request records; it does not grant sandbox access. |

Any new hash use must document its purpose and use a cryptographic primitive
when it influences a security boundary. Secrets, passwords, and message
authentication require a dedicated password KDF or HMAC with a secret key,
not a plain digest.
