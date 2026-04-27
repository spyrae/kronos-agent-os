.PHONY: setup run test test-all lint format deploy clean

# Setup development environment
setup:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev,memory]"
	@echo "Done. Activate with: source .venv/bin/activate"

# Run single agent (set AGENT_NAME in .env or env)
run:
	python -m kronos

# Run unit tests (no external services needed)
test:
	pytest -m "not integration" -v

# Run all tests including integration (requires API keys)
test-all:
	pytest -v

# Lint
lint:
	ruff check kronos/ dashboard/ aso/ tests/

# Auto-format
format:
	ruff format kronos/ dashboard/ aso/ tests/
	ruff check --fix kronos/ dashboard/ aso/ tests/

# Deploy to remote host (requires KAOS_REMOTE in .env)
deploy:
	bash scripts/deploy.sh

# First-time remote setup
deploy-init:
	bash scripts/deploy.sh --first-run

# Remove build artifacts
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ .pytest_cache/
