# Swarm and Sub-Agents

Swarm mode is an optional coordination layer inside KAOS. It is not the whole
product.

For a release-safe walkthrough, see [Swarm Mode Demo](SWARM_DEMO.md).

## When to Use Swarm Mode

Use swarm/sub-agents when a task benefits from:

- independent perspectives
- role specialization
- parallel research
- debate and synthesis
- group-chat style coordination

Do not use swarm mode for simple tasks where one agent can answer directly.

## Process Model

Each agent can run as a separate process with:

- its own `AGENT_NAME`
- its own workspace
- its own session file/account when using Telegram
- its own memory and local data directory
- shared swarm ledger when coordination is enabled

The shared ledger prevents duplicate implicit replies and records coordination
state.

## Roles And Delegation

Sub-agents should have narrow, explicit roles. Examples:

| Role | Good For | Avoid |
|------|----------|-------|
| Researcher | gathering sources and options | making final product decisions alone |
| Critic | finding risks, contradictions, missing tests | blocking simple tasks with debate |
| Operator | turning a decision into concrete steps | broad strategy without context |
| Synthesizer | merging multiple outputs into one answer | hiding disagreement |

Delegation should produce one synthesized answer to the user. The goal is
coordination, not uncontrolled multi-agent chatter.

## Arbitration

Group-chat coordination uses `kronos/swarm_store.py`:

- inbound messages are recorded in `swarm_messages`
- candidate responders create reply claims
- SQLite `IMMEDIATE` transactions arbitrate duplicate implicit replies
- sent replies and peer feedback are recorded for later diagnostics

This keeps multi-agent chats readable while preserving enough state for
debugging and metrics.

## Configuration

Agent profiles are configured in a local, gitignored `agents.yaml`.

Use `agents.example.yaml` as the public template.

```yaml
strategist:
  username: strategist_bot
  aliases: ["strategist"]
  role: "strategy, prioritization, and tradeoff analysis"
```

## Safety

- Keep live agent workspaces private.
- Do not commit Telegram sessions or IDs.
- Make each agent's role explicit.
- Prefer synthesis over uncontrolled multi-agent chatter.
- Keep server ops and dynamic tool creation gated even in swarm mode.
- Set cost and frequency limits before adding many agents.
- Keep high-risk tools disabled unless every participating agent is trusted.
- Do not store peer-reaction context as long-term user memory.

## Cost And Latency

Swarm mode can multiply LLM calls. A three-agent debate can be 3-5x slower and
more expensive than a direct answer, especially if agents call tools. Use it
for tasks where independent reasoning is worth the cost:

- launch planning
- research synthesis
- incident review
- product strategy tradeoffs

Use single-agent mode for quick answers, simple edits, and deterministic local
tasks.

## Dashboard Requirements

The control room should expose swarm/coordination state without making it the
whole product:

- active agents and roles
- recent coordination runs
- claim arbitration outcomes
- duplicate replies avoided
- cost/latency rollups per agent
- blocked high-risk capabilities

The dashboard endpoint `/api/swarm/runs` builds inspectable runs from
`swarm.db` reply claims and messages. If no live swarm data exists, it returns
synthetic demo data for screenshots and onboarding. The visualizer shows roles,
claim status, intermediate steps, winner/synthesis, and coordination metrics.

## Example

A launch-planning request can be split into:

1. Researcher: finds comparable open-source launch patterns.
2. Critic: identifies safety, setup, and positioning risks.
3. Operator: converts the plan into issues and commands.
4. Synthesizer: returns one final plan with disagreements resolved or called out.

For a simple factual question, use one agent directly.

## Relationship to KAOS

KAOS is the operating layer:

- runtime
- memory
- skills
- tools/MCP
- automations
- dashboard
- swarm coordination

Swarm mode uses those same primitives rather than replacing them.
