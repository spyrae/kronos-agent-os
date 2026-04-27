# Contributing to Kronos Agent OS

Thanks for helping improve KAOS.

KAOS is a self-hosted agent runtime: memory, skills, MCP tools, automations,
dashboard, and optional swarm coordination. Contributions should keep that
broader Agent OS frame clear and avoid turning the project back into a
swarm-only demo.

## Local Setup

```bash
git clone https://github.com/spyrae/kronos-swarm.git
cd kronos-swarm

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

kaos demo
kaos doctor
```

Optional memory stack:

```bash
pip install -e ".[dev,memory]"
```

## Development Checks

Run before opening a PR:

```bash
ruff check kronos/ tests/
pytest -m "not integration"
```

For quickstart-sensitive changes, also run:

```bash
kaos demo
kaos init smoke-agent --dry-run
kaos connect telegram
```

Integration tests may require API keys, local MCP binaries, Docker, Telegram
sessions, or provider network access. Mark those tests with `@pytest.mark.integration`.

## Public-Safe Rules

Do not commit:

- `.env`, `.env.*`, real API keys, tokens, session files, or private keys
- `data/`, SQLite files, Qdrant state, logs, or generated runtime state
- live `workspaces/<agent>/` directories
- `agents.yaml`, `servers.yaml`, private hosts, IPs, usernames, or Telegram IDs

Use safe placeholders in examples. Keep risky capability gates disabled by
default:

```bash
ENABLE_DYNAMIC_TOOLS=false
ENABLE_MCP_GATEWAY_MANAGEMENT=false
ENABLE_DYNAMIC_MCP_SERVERS=false
ENABLE_SERVER_OPS=false
```

## Contribution Areas

Good first contributions:

- Quickstart and installation fixes
- Tests for CLI, config, Docker, dashboard, and safety defaults
- Documentation for runtime, memory, skills, MCP, automations, dashboard, and swarm mode
- New safe static MCP integrations
- Dashboard observability and blocked-capability explanations

Changes that need design discussion first:

- Runtime architecture changes
- New dynamic execution paths
- Shell, filesystem, browser, network, server ops, or deployment automation changes
- New default-enabled tools
- Public examples that include realistic user data

## Pull Request Checklist

- Explain the user-facing behavior change.
- Add or update tests when behavior changes.
- Update README/docs when setup, env vars, commands, or safety defaults change.
- Run `ruff check` and `pytest -m "not integration"`.
- Confirm no private data or live workspace files are included.

## Security

Do not open public issues for vulnerabilities. See `SECURITY.md`.

## License

By contributing, you agree that your contributions are licensed under the MIT License.
