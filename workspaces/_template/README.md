# Workspace Template

Copy this directory to create a new agent workspace:

```bash
cp -r workspaces/_template workspaces/my-agent
```

Then customize the files:
- `self/IDENTITY.md` — who the agent is
- `self/SOUL.md` — how the agent behaves and communicates
- `self/methodology.md` — decision-making framework

Add the agent to `agents.yaml` with matching name:

```yaml
my-agent:
  username: myagent_bot
  aliases: ["my agent", "мой агент"]
  role: "one-line description for LLM relevance scoring"
```

## Directory Structure

```
workspaces/<agent-name>/
├── self/           <- WHO I AM (loaded into system prompt)
│   ├── IDENTITY.md
│   ├── SOUL.md
│   └── methodology.md
├── notes/          <- WHAT I KNOW (user facts, world knowledge)
│   ├── user/
│   └── world/
└── ops/            <- WHAT I DO (runtime state, tasks)
    ├── HEARTBEAT.md
    └── sessions/
```
