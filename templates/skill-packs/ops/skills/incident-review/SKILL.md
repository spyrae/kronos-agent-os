---
name: incident-review
description: Summarize an incident with timeline, likely cause, impact, and follow-up actions.
tools: [logs]
tier: medium
---

# Incident Review

## Protocol

1. Build a timeline from the provided evidence.
2. Separate facts from hypotheses.
3. Identify impact, likely cause, and detection gaps.
4. Recommend follow-up tasks and tests.

## Safety

Do not restart services, mutate infrastructure, or run server ops unless the
operator explicitly enabled the required capability gate.
