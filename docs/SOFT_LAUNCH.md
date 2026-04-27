# KAOS Optional Feedback Plan

This is an optional feedback checklist for maintainers. It is not a required gate before publishing KAOS on GitHub.

Use it only if you want structured feedback after the repo is public. The default launch path is simpler: publish the repo, ship the README/demo/release notes, and let GitHub users open issues organically.

## Optional Target

- A few trusted technical users, or whoever tries the public repo after launch.
- Mix of Python/backend engineers, AI-agent builders, self-hosted users, and technical founders.
- Give them only public repo links, README, docs, and demo commands. Do not add private instructions.

## Invite Copy

```text
I am preparing a public release of Kronos Agent OS (KAOS), a self-hosted runtime for durable AI agents.

Could you spend 20-30 minutes trying the public quickstart from the README and tell me where you get stuck?

Please use only the repo/docs, not private instructions:
https://github.com/spyrae/kronos-agent-os

Useful checks:
- Does the Agent OS framing make sense?
- Can you run `kaos demo`?
- Can you understand what `kaos demo-seed --reset` and the dashboard are for?
- Are the security defaults clear?
- What would you try building first?
```

## Feedback Form

Capture one row per tester:

| Field | Notes |
|-------|-------|
| Tester | Name or handle |
| Profile | Backend, AI agents, self-hosted, founder, etc. |
| OS | macOS/Linux/Windows/Docker |
| Time to first demo | Minutes until `kaos demo` works |
| Setup blocker | Exact command and error |
| Confusing doc | README/docs section |
| Security concern | Capability, dashboard, tools, MCP, memory, etc. |
| Desired example | Template/skill/use case they expected |
| Severity | blocker / confusing / nice-to-have |
| Follow-up issue | Linear/GitHub ID |

## What Counts As A Blocker

- Fresh clone cannot install with documented commands.
- `kaos demo` fails on a supported Python version.
- `kaos doctor` gives unclear or wrong guidance.
- Dashboard seed commands do not run.
- Security defaults are misunderstood as unsafe or too hidden.
- README makes KAOS look swarm-only instead of Agent OS.

## Triage Rules

- Fix install and safety blockers before public launch.
- Fix README quickstart confusion before feature requests.
- Convert repeated desired examples into docs/templates/skill-pack tasks.
- Defer non-critical feature requests to post-launch roadmap.
- Never ask testers to paste secrets, private logs, Telegram sessions, or live workspace data into public issues.

## Status Template

```markdown
## Feedback Status

Testers contacted:
Testers completed:
Median time to `kaos demo`:
Install blockers:
Docs blockers:
Security concerns:
Top requested examples:
Public launch readiness:
```

## Related Linear Tasks

- RB-1153: fix launch blockers from soft launch.
- RB-1155: publish v0.1.0 release with safety-first notes.
- RB-1156: monitor first 72 hours and triage issues.
- RB-1157: convert early feedback into roadmap decisions.
