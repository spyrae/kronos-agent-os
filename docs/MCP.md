# MCP and Tool Gateway

KAOS treats tools as capabilities, not as an unbounded execution surface.

## Tool Types

| Type | Examples | Default |
|------|----------|---------|
| Static MCP tools | fetch, search, filesystem when workspace exists | allowed when configured |
| Built-in tools | session search, skill tools, browser tools | allowed when dependency exists |
| Dynamic tools | generated local Python tools | disabled |
| Dynamic MCP servers | persisted runtime MCP registration | disabled |
| Server ops tools | SSH/systemd/docker diagnostics | disabled |

## Capability Gates

```bash
ENABLE_DYNAMIC_TOOLS=false
REQUIRE_DYNAMIC_TOOL_SANDBOX=true
ENABLE_MCP_GATEWAY_MANAGEMENT=false
ENABLE_DYNAMIC_MCP_SERVERS=false
ENABLE_SERVER_OPS=false
```

Keep these disabled for public demos and untrusted environments.

## Static MCP

Static MCP servers are configured in code and loaded by the runtime manager.
Providers that need API keys should be optional: missing keys should skip the
server instead of crashing the runtime.

The filesystem MCP server is only added when the configured workspace path
exists.

Discovery path:

1. `kronos/tools/manager.py` builds configured static MCP connections.
2. Available tools are handed to `KronosAgent`.
3. The ReAct engine binds the tools to the model.
4. Tool calls are executed through the runtime loop and summarized in logs/CLI.

Safe local examples:

- read/search tools scoped to the workspace
- web search tools with explicit query text
- session search over local KAOS history

High-risk examples:

- shell/filesystem writes outside the workspace
- adding or reloading MCP servers at runtime
- server ops, SSH, Docker, or systemd actions
- tools that can spend money, send messages, or mutate external systems

## Dynamic MCP

Runtime server management is powerful and should be treated as a local admin
feature. It is unavailable unless:

```bash
ENABLE_MCP_GATEWAY_MANAGEMENT=true
```

Persisted dynamic servers are ignored unless:

```bash
ENABLE_DYNAMIC_MCP_SERVERS=true
```

## Dynamic Tools

Dynamic tool creation is disabled unless:

```bash
ENABLE_DYNAMIC_TOOLS=true
```

When dynamic tools are enabled, the public-safe expectation is sandboxed
execution:

```bash
REQUIRE_DYNAMIC_TOOL_SANDBOX=true
```

Build the local sandbox image before enabling dynamic tools:

```bash
scripts/build-sandbox.sh
ENABLE_DYNAMIC_TOOLS=true kaos doctor
```

If Docker or the sandbox image is unavailable, dynamic execution fails closed.

## Server Ops

Server ops require explicit opt-in and a private registry:

```bash
ENABLE_SERVER_OPS=true
SERVER_REGISTRY_PATH=/path/to/servers.yaml
```

Use `servers.example.yaml` as the public shape. Do not commit `servers.yaml`.

## Checking that the servers still work

Loading is resilient on purpose: each server is tried on its own, and one that
will not start is skipped so the rest keep working. That is the right behaviour
and it is also how two servers stayed dead for months. The agents came up with
`Loaded 102 tools from 9/11 servers`, nothing errored, and the finance agent went
on answering market questions from news search alone — because its tool filter
also matched `brave`, so it kept being created with no market data in it.

The break arrived without a deploy: `mcp-server-fetch` and `mcp-yahoo-finance`
both declare `mcp>=1.6` with no upper bound, uv installed SDK 2.0, and the API
each was written against was gone. A check that only ran at deploy time would
have missed it too.

```bash
kaos mcp check          # start every server and see which hand over tools
kaos mcp check --json   # same, machine-readable
```

Three outcomes per server:

| | |
|---|---|
| `ok` | handed over N tools |
| `broken` | failed to start, timed out, or **started and exposed nothing** |
| `off` | not configured here — no credentials, or scoped to another agent |

A server that has *vanished from the config* still gets a line. A key dropped
from `.env` makes a server disappear entirely, and something that silently
ceases to exist is exactly what needs saying — so the probe reports against the
full `KNOWN_SERVERS` list rather than only what got built. A test keeps that list
in step with the builder, since drift there would reopen the hole quietly.

"Started" is deliberately not "working". A server that comes up clean and offers
no tools contributes nothing while reading as healthy to any check that stops at
whether the process launched.

Failure details are the exception's own text, and server configs carry API keys
in `env` — so credentials are stripped by value before anything is reported,
including a token embedded inside a larger value (Notion's is inside a JSON
header string). This report goes to Telegram; over-redacting an error message
costs nothing.

The same probe runs daily as the `mcp-smoke` cron job and **only speaks when
something changes** — see [ACQUISITION.md](ACQUISITION.md#checking-that-it-still-works)
for why silence is the design. It is the heaviest of the three capability
checks: eleven servers started one at a time, about forty seconds, each bounded
by a timeout so one that never answers cannot stall the job. Startup keeps no
such timeout — giving up on a slow-but-working server at boot would cost tools
for the whole session, which is a different decision from bounding a daily probe.

## Approvals, Audit, And Errors

The current public posture is capability-gated:

- blocked capabilities should name the env var needed for opt-in
- dynamic/server operations should fail closed when the gate is disabled
- CLI chat prints compact tool events with secret-like args redacted
- dashboard audit views should show tool/runtime events without exposing secrets
- missing optional API keys should skip the integration rather than crash startup

For user-visible commands, prefer a short refusal plus the exact gate to change
over silent no-ops.
