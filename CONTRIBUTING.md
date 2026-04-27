# Contributing to Kronos Agent OS

Thanks for helping improve KAOS.

KAOS is a self-hosted agent runtime: memory, skills, MCP tools, automations,
dashboard, and optional swarm coordination. Contributions should keep that
broader Agent OS frame clear and avoid turning the project back into a
swarm-only demo.

## Local Setup

```bash
git clone https://github.com/spyrae/kronos-agent-os.git
cd kronos-agent-os

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

## Contribution Map

KAOS is meant to be extended through small, understandable lanes. Pick the lane
that matches what you want to improve.

| Area | Good PRs | Primary files | Tests/docs |
|------|----------|---------------|------------|
| LLM providers | Provider presets, OpenAI-compatible recipes, local model notes | `kronos/llm.py`, `.env.example` | `tests/test_llm_providers.py`, `docs/LLM_PROVIDERS.md` |
| Agent templates | New starter agent profiles for common roles | `templates/agents/` | `tests/test_cli_templates.py`, README/docs examples |
| Skill packs | Reusable procedures for a domain | `templates/skill-packs/` | `tests/test_cli_templates.py`, `docs/SKILLS.md` |
| MCP recipes | Safe static MCP examples and tool docs | `docs/MCP.md`, `servers.example.yaml` | `tests/test_mcp_smoke.py` where practical |
| Deployment targets | Docker/systemd/runner docs and safer deploy checks | `docs/DEPLOYMENT.md`, `.github/workflows/`, `scripts/deploy.sh` | shell syntax checks, deploy docs |
| Dashboard panels | Better local visibility into memory/jobs/tools/config | `dashboard/`, `dashboard-ui/` | dashboard API tests, UI build |
| Safety policies | Clearer gates, warnings, audit trails, blocked actions | `kronos/security/`, `kronos/tools/`, docs | focused safety tests |
| Examples/demos | Public-safe workflows people can copy | `docs/`, `scripts/render_demo_assets.py` | public-surface tests |

Good first contributions:

- Quickstart and installation fixes
- Provider recipes for services you already use
- Agent templates for clear use cases
- Skill packs with safe fixtures
- Documentation for runtime, memory, skills, MCP, automations, dashboard, and swarm mode
- Dashboard observability and blocked-capability explanations

### Provider PR Checklist

- Prefer an OpenAI-compatible preset before adding a custom adapter.
- Add/adjust provider defaults in `kronos/llm.py`.
- Add a copy-paste `.env` recipe in `docs/LLM_PROVIDERS.md`.
- Add a compatibility matrix row.
- Add tests that do not require live API calls.
- Do not include real API keys, account IDs, or private base URLs.

### Template PR Checklist

- Add a directory under `templates/agents/<name>/`.
- Include a `template.yaml` with role, description, memory defaults, capability policy, and example prompts.
- Keep the template public-safe: no private people, companies, chats, servers, or credentials.
- Add docs or README examples if the template is a common first-run path.

### Skill-Pack PR Checklist

- Add a directory under `templates/skill-packs/<name>/`.
- Include `pack.yaml`, at least one skill, and safe fixture notes where useful.
- Keep skills procedural and reusable; avoid private workflows disguised as examples.
- Mention risky external actions and the capability gates that control them.

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
