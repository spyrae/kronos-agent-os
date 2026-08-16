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

## Checking that it still works

Readiness and capability are different questions, and only one of them was being
asked. `sandbox_ready()` reports whether Docker and the image exist — and both of
this subsystem's real failures answered that perfectly while every run inside
failed:

- a temp directory created at `0700` and mounted into a container running as
  another user, so every run died on `Permission denied: '/code/tool.py'`;
- a bind mount passed as a relative path, which Docker reads as a *named volume*
  — broken on every host whose data directory is configured relatively, which is
  every real deployment and no test.

Neither raised at startup. Neither failed the suite. So the check runs code:

```bash
kaos sandbox check          # probe now
kaos sandbox check --json   # same, machine-readable
```

It asks seven questions, in two groups that mean opposite things when they fail.

**Can it work at all** — `docker`, `image`, `execution` (code ran and its output
came back), `workspace` (a file written at `/work` outlived the container). Losing
these costs a capability: `run_code` and dynamic tools then refuse, because there
is no unsandboxed path to fall back to.

**Is it still a sandbox** — `no_network` (the container sees only loopback),
`readonly_root` (its root filesystem refuses a write), `non_root` (it is not
running as uid 0). Losing one of these is the opposite problem: code still runs,
with a wall down. The report says so plainly and names the off switch, because a
message telling you no safety was lost would be exactly backwards.

Every answer comes from inside the container, and none of it sends traffic — the
network question is settled by listing interfaces, not by dialling out to prove a
call cannot be made.

A check that could not be *determined* is left out rather than guessed at. When
nothing can run, the containment guarantees are simply absent from the report;
they return, and are reported if broken, as soon as execution does.

The same probe runs daily as the `sandbox-smoke` cron job, and **only speaks when
something changes** — see [ACQUISITION.md](ACQUISITION.md#checking-that-it-still-works)
for why silence is the design and not an oversight. A host without Docker reports
`off`: opting out of running code is a choice, not a fault.

It probes whether or not `ENABLE_CODE_EXECUTION` is on, and the image is what
gates it: a host that has one wants the sandbox, and learning it works *before*
the flag is flipped beats learning it afterwards from a task that failed. The
code it runs is the probe's own half-dozen lines reporting uid and interfaces —
never anything the agent wrote.

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
- **The daily check proves the walls are up, not that they are sufficient.** It
  confirms `--network=none`, `--read-only` and a non-root uid are actually in
  force; it does not audit the kernel, the daemon's configuration, or a container
  escape. A sandbox is a boundary, not a proof.
