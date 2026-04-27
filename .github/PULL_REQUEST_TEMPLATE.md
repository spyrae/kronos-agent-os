## Summary

Describe what changed and why.

## Checks

- [ ] `ruff check kronos/ dashboard/ aso/ tests/`
- [ ] `pytest -m "not integration"`
- [ ] `npm audit --audit-level=moderate`, `npm run lint`, and `npm run build` in `dashboard-ui/` when UI changed
- [ ] README/docs updated when commands, env vars, setup, or safety defaults changed
- [ ] No secrets, sessions, private hosts, Telegram IDs, or live `workspaces/<agent>/` files included

## Risk

- [ ] No risky capability default changed
- [ ] Dynamic tools / dynamic MCP / server ops remain opt-in
- [ ] New tool or automation behavior is documented

## Notes

Add screenshots, logs, or follow-up tasks when useful.
