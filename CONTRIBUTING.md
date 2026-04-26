# Contributing to Kronos Swarm

Thanks for your interest in contributing!

## Getting Started

```bash
git clone https://github.com/spyrae/kronos-swarm.git
cd kronos-swarm
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,memory]"
```

## Running Tests

```bash
# Unit tests (no external services needed)
pytest -m "not integration"

# All tests (requires API keys in .env)
pytest
```

## Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for linting:

```bash
ruff check kronos/
ruff format kronos/
```

Config is in `pyproject.toml` (line length 100, Python 3.11+).

## Making Changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Add tests if applicable
4. Run `ruff check` and `pytest`
5. Open a PR with a clear description

## What to Contribute

- Bug fixes and stability improvements
- New transport bridges (Slack, WhatsApp, Matrix)
- New MCP tool integrations
- Documentation improvements
- Test coverage

## Architecture Decisions

Major architectural changes should be discussed in an issue first. See `docs/` for existing architecture documentation.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
