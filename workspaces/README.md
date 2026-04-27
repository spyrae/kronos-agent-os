# Workspaces

KAOS workspaces are local runtime state for each agent.

They can contain persona files, notes, skills, memory references, queues,
sessions, and operational scratchpads. Treat them like user data, not source
code.

Tracked public content:

- `_template/` — safe starter workspace used by `kaos init`.
- `README.md` — this file.

Ignored local content:

- `workspaces/<agent>/`
- generated notes and ops state
- private skills/references
- imported or learned memory files

Create a new local workspace:

```bash
kaos init my-agent --role "local research and task agent"
```

Install a bundled agent template:

```bash
kaos templates list
kaos templates install personal-operator my-agent --dry-run
```

If you want to publish an example workspace, remove private data first and put
it under a dedicated public examples directory rather than committing your live
runtime workspace.
