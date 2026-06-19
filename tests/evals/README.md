# Deploy-gate evals

`pytest -m eval` runs deterministic behavior checks that must pass before
deploying KAOS agents. Keep this suite cheap: no network, no live LLM calls, no
secrets, and no dependence on production state.

## Add a new golden scenario

1. Add a test under `tests/evals/` and rely on the module-level
   `pytestmark = pytest.mark.eval`, or mark the test explicitly.
2. Use local fixtures/stubs for Telegram events, swarm DB, model answers, and
   clocks.
3. Assert properties, not prose snapshots: e.g. `tier == 1`,
   `should_respond is False`, `winner == "kronos"`, cap/cooldown behavior.
4. Keep optional LLM-judge experiments outside this marker so deploys remain
   deterministic.

Run locally:

```bash
uv run pytest -q -m eval
```
