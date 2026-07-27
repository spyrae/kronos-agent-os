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
- optional `SHARED_WORKSPACE_PATH` for a common skill pool

The shared ledger prevents duplicate implicit replies, records coordination
state, and provides recent shared topic context to every agent invocation.

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

## Organization As Config

`agents.yaml` is the org chart, not just a username table. Identity fields make
agents distinguishable; the organisational fields make them a team. Every one of
them is optional — a registry written before they existed loads unchanged, which
is what makes this safe to roll out to a running swarm.

```yaml
strategist:
  username: strategist_bot
  aliases: ["strategist"]
  role: "strategy, prioritization, and tradeoff analysis"
  owns: ["planning", "priorities"]   # subjects this agent answers on sight
  escalates_to: analyst              # who covers a silence here
  sla_minutes: 15                    # how long "silent" is
  budget_usd_daily: 1.5              # personal slice of the swarm budget
  dissent: require                   # answers here need a peer's review
  max_implicit_replies: 2            # this agent's own tolerance for peers
```

Validation is split by consequence (`kronos/swarm_config.py`). A broken
`escalates_to` **raises at startup** — it names a delivery path that does not
exist. Contested ownership and per-agent budgets summing above the swarm cap only
**warn**: both are legitimate transitional states, and refusing to start would be
worse than saying so in the log. A contested topic has no owner, so routing falls
back to plain relevance.

### Ownership and escalation

Two different things are called "topic" and they do not mix:

| | Telegram forum topic | `owns` subject |
|---|---|---|
| Identified by | topic id in env (`TOPIC_*`) | recognised from the message |
| Enforcement | hard — non-owners never reach the router | soft — owner answers first |
| Where it applies | configured topics | the shared stream and DMs |

An owned subject gets three things:

- **Owner-first.** The owner answers without the relevance threshold and on the
  fast lane (1–4s), so it wins arbitration against agents who merely scored the
  topic highly.
- **Deference.** A non-owner that would have answered waits 90s instead of
  talking over the specialist. Not the full SLA: a claim older than
  `CLAIM_EXPIRY_SECONDS` (120) can never win arbitration, so a longer sleep would
  mean "never answer" while still costing a task and an LLM call. Inside the
  window a live owner always wins; past it, an owner whose process is down no
  longer leaves the user waiting.
- **A deadline.** The first agent to recognise the subject registers an
  `sla_watch` row — including a non-owner, and including when it has nothing to
  say, because the case worth covering is the owner's process being down. The
  `sla-escalation` job (60s) hands a missed deadline to the owner's
  `escalates_to` through the existing hand-off queue. All six processes poll it;
  a compare-and-set on the watch row means exactly one creates the hand-off.

Subject recognition costs nothing when no agent declares `owns` (the map is
empty, every branch is a no-op) and nothing for @-addressed messages. Otherwise
it is one lite-tier call, cached per process for five minutes.

### Budgets and quiet mode

An agent may have a personal daily slice — `budget_usd_daily` here, or
`budgets.per_agent_daily_usd` in `policy.yaml`, which wins. Exhausting it does
**not** block: the agent enters quiet mode, still answering when addressed
(Tier 1) and no longer volunteering (Tier 2/3). A hard stop would mean the user's
direct question goes unanswered because the agent spent its allowance on opinions
nobody asked for. `should_degrade()` also fires at 80% of the personal slice, so
the soft brake exists for the agent doing the spending and not only for the swarm.
`/stats` shows the personal slice and whether quiet mode is on.

### Dissent

With `dissent: require`, the owner's draft answer goes to an agent with a
different role (its `escalates_to`, by default) before it is sent. An objection is
appended to the answer; agreement changes nothing visible, so the absence of the
"без ревью" mark is what says the answer passed a second pair of eyes. A silent
reviewer never eats the answer — on timeout (90s) it is sent with the mark.

The reviewer side rides the council intake pass rather than a fourth poller: the
queue shape is identical, and a review is a one-participant council with the
opposite exit (a council ends in synthesis posted to the chat, a review ends in a
verdict handed back to the author).

### Post-mortem

```bash
kaos swarm report --week          # markdown digest
kaos swarm report --day --json    # machine-readable
```

Who answered and at which tier, spend and cost per reply per agent, 👍/👎,
owned-topic activity and missed SLAs, hand-offs, councils, reviews and
objections. The same report is pushed every Sunday by the `swarm-weekly-report`
job, which claims the ISO week in the ledger first so the digest arrives once
rather than six times.

There is no cost-per-topic column on purpose: spend is recorded per agent and per
day, never attributed to a subject, so that table would be an invention.

### Seeing it without Telegram

```bash
kaos demo --swarm
```

Three agents on an in-process bus over a temporary `swarm.db`: cap arbitration,
the ownership shortcut, a Tier 3 reaction, a non-owner registering the watch for a
dead owner, escalation through the hand-off queue, and the weekly report. Every
routing decision goes through the production `GroupRouter` and every claim
through the production ledger; only the transport, the delays and the model calls
are local (see `kronos/swarm_local.py` for exactly what is not reproduced).

## Telegram Topic Configuration

Telegram forum topics can be made explicit so personal topics do not run
through the smart group router:

```bash
TELEGRAM_SWARM_CHAT_ID=<your_forum_chat_id>
TELEGRAM_GENERAL_TOPIC_ID=<general_topic_id>
TELEGRAM_KRONOS_TOPIC_ID=<kronos_topic_id>
TELEGRAM_FINANCE_TOPIC_ID=<finance_topic_id>
TELEGRAM_DIGEST_TOPIC_ID=<digest_topic_id>
TELEGRAM_KRONOS_AGENT=kronos
TELEGRAM_FINANCE_AGENT=kronos
TELEGRAM_DIGEST_AGENT=kronos
TOPIC_DIGEST_NEWS=<digest_news_topic_id>
TOPIC_JB_COMPETITORS=<jb_competitors_topic_id>
TOPIC_JB_SYSTEM=<jb_system_topic_id>
TELEGRAM_DIGEST_NEWS_AGENT=kronos
TELEGRAM_JB_COMPETITORS_AGENT=nexus
TELEGRAM_JB_SYSTEM_AGENT=nexus
```

Only the general topic uses relevance-based multi-agent routing. Owner topics
are answered only by their configured agent; every other agent records the
message to the shared ledger and stands down before the smart router. Owner
agent values can be comma-separated (for example `kronos,nexus`) when both
agents are intentionally allowed in the same topic.

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
