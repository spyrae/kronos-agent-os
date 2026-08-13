# Sandbox

Two things want to run code the agent wrote: **one-off analysis** (total these
forty listings, normalise this export) and **dynamic tools** (a named function
the agent keeps and reuses). Both run in the same container, and only there.

## What runs, and where it cannot go

```
docker run --network=none --read-only --cap-drop=ALL --user=10001:10001
           --pids-limit=50 --memory=256m --cpus=1
           --security-opt=no-new-privileges
           --tmpfs=/tmp:noexec,nosuid,nodev,size=64m
           -v <code>:/code:ro  -v <session files>:/work:rw  --workdir=/work
```

The standard library only — the image installs nothing, and packages stay
refused unless a deployment lists them in `policy.yaml`.

**There is no unsandboxed mode.** An earlier version fell back to `exec()` in the
agent's own process when Docker was missing; it logged "unsafe, dev only" and was
one environment variable away from production. Missing Docker now means the code
does not run. `REQUIRE_DYNAMIC_TOOL_SANDBOX=false` turns dynamic tools **off**
rather than running them unprotected.

**There is no import blocklist.** The container is the boundary. A regex
rejecting `import os` inside a network-less, capability-less container as a
non-root user would refuse working code and buy nothing, while suggesting a
protection that is not there.

## `run_code`

```
run_code(code, session="", files="", timeout=30)
```

Print the answer — stdout is what comes back. Off by default:

```bash
ENABLE_CODE_EXECUTION=true          # or capabilities.code_execution in policy.yaml
bash scripts/build-sandbox.sh       # deploy.sh does this when the image is missing
```

Errors come back separately from output, because code that printed a total and
then crashed did not produce a total.

## Files and sessions

One writable directory per session, at `/work`, which is also the working
directory — so `open("offers.csv", "w")` works and the file is still there next
time:

```
data/<agent>/sandbox/<session>/
  files/            ← shared by every run of the session, mounted read-write
  <run-id>/         ← that run's manifest
```

The session name defaults to the current thread. For a plan step that is
`plan:<id>`, so **a plan accumulates its own working files** without anyone
naming them: a step fetches and normalises today, a step three days later reads
the result.

`files` takes a JSON object of name → text to drop in before the run. Names are
reduced to a bare filename, so nothing can be written outside the session.

## Limits, and which ones are real

| Limit | Enforced by |
|---|---|
| Time | `asyncio.wait_for`, then the container is killed |
| Memory, CPU, processes | Docker, at the kernel |
| Network | `--network=none`; nothing to configure around it |
| Disk | a watchdog **outside** the container measuring the session directory twice a second |

The disk one is worth explaining: a read-write bind mount has no size of its own,
so `storage_mb` would otherwise be a number in a manifest while a loop writing
bytes filled the host disk. The watchdog is what makes the declared budget a
budget.

Ceilings come from `policy.yaml`:

```yaml
capabilities:
  code_execution: false
sandbox:
  network_domains: []      # empty = no network, which is also what Docker enforces
  packages: []             # empty = stdlib only
  secret_capabilities: []
  max_timeout_seconds: 60
  max_storage_mb: 64
  max_memory_mb: 256
```

A run asking for more than a ceiling is refused before the container starts, and
the refusal is recorded.

## The trail

Every run and every refusal appends to `sandbox_runs.jsonl` with the policy
decision, the resources declared, and redacted output. The dashboard's **Sandbox**
page reads it. Secrets and PII are masked on the way in — a run's stdout is
untrusted content like any other.

## Honest limits

- **Input names are not a security boundary.** They are validated for the audit
  trail; what bounds a run is the container, the clock and the watchdog. The
  allowlists that matter — network, packages, secret capabilities — start empty.
- **A run can write up to its budget before the watchdog notices**, since the
  check is periodic rather than a filesystem quota. The window is under a second
  at default settings.
- **`secret_capabilities` is declared and not yet implemented.** No secret is
  ever passed into a container today; the field exists so that when one is, it
  has to be declared first rather than injected.
- **The image has no third-party packages.** For the arithmetic this exists for,
  `csv`, `json` and `statistics` are enough; pandas would add hundreds of
  megabytes to an image whose point is being small and boring.
