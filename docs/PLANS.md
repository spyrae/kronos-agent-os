# Plans

A durable turn protects minutes of work: up to twenty-five model steps, resumable
after a crash. Some requests do not fit in that shape at all.

> Find me a flat on Airbnb and Booking, ask the landlords about the deposit and
> the minimum term, and tell me which one is worth taking.

That is days, and most of it is waiting. A plan is the object for it: a goal, a
set of steps, and for each step what it waits for. The poller runs each step as
an ordinary agent turn when its time comes, so durable turns, the effects ledger,
approvals and the cost guardian all apply — nothing about plans re-implements
them.

```
plan   → goal, state (active | done | failed | cancelled), expiry, summary
 step  → prompt, depends_on, waiting condition, state, result
```

## What a step waits for

| Condition | Fires when | For |
|---|---|---|
| `{"kind":"at","seconds":86400}` | the time comes | check back tomorrow |
| `{"kind":"manual","note":"after the viewing"}` | you release it | only you know when |
| `{"kind":"reply"}` | you write in the chat the plan came from | the plan needs your answer |
| `{"kind":"page_matches","url":…,"pattern":"in stock","absent":false}` | the page starts (or stops) matching | back in stock, listing gone |
| `{"kind":"page_number","url":…,"pattern":"Rp ([0-9.]+)","op":"below","value":9000000}` | the number crosses the threshold | watch a price |

Page conditions take `every_seconds` — minimum 300, default 3600. None of these
call a model: a condition that cost a model call every few minutes would make
waiting the expensive part, when waiting is meant to be the cheap part.

Conditions are validated when the step is written, not when it wakes. A bad
regex, a missing `op`, a host the egress allowlist forbids — all refused in the
turn that asked, with the reason, so the agent fixes it immediately instead of
the plan looking fine and never firing.

## What the agent does

| Tool | Does |
|---|---|
| `plan_start(goal, first_step)` | opens a plan and queues its first step |
| `plan_add_step(plan_id, task, after_steps, wait, notify)` | appends a step, optionally parked and/or dependent |
| `plan_status(plan_id)` | what is running and what it waits for |
| `plan_cancel(plan_id)` | stop it |

Steps run in the plan's own thread (`plan:<id>`), not your chat: a dozen machine
turns do not belong in a conversation, and it is what lets a `reply` condition
tell you speaking from the agent's own work.

## What you see

```bash
kaos plans list            # running plans, and what each waits for
kaos plans list --all      # including finished ones
kaos plans show 7          # steps, results, how many times a condition was checked
kaos plans resume 7        # release steps that were waiting for you
kaos plans resume 7 --step 12
kaos plans cancel 7
```

The dashboard has the same under **Plans**, including a per-step release button.

## What reaches you, and when

A step is silent unless it was created with `notify`. What always arrives is the
plan closing: one message, written by the agent from the step results — that is
the thing you waited days for, and the raw results are a log, not an answer.

The reason for the default is arithmetic. A week-long price watch that messaged
every hour would be muted by day two, and then nothing would reach you at all.

## Bounds

Every one of these exists because its absence is a way for a plan to cost money
forever, or to look alive when it is not:

| Bound | Value | Why |
|---|---|---|
| Steps per plan | 50 | a plan that keeps adding steps is a loop |
| Attempts per step | 3 | a transient failure must not end a week-long plan; a permanent one must not retry forever |
| Checks per condition | 5000 | a page that stopped existing, a threshold nothing will reach |
| Plan expiry | 90 days | "still waiting" must eventually become "this did not work" |
| Step runs per cycle | 3 | each is a model call, and a minute is not a reason to run everything ready |
| Summaries per cycle | 2 | leftovers are picked up next cycle; a finished plan is owed one until it has one |
| Condition checks per cycle | 20 | a page fetch takes seconds |

A plan that expires is closed and reported, not silently dropped: silence would
leave you believing it was still watching.

## Honest limits

- **A dependency counts as satisfied when it finished, not when it succeeded.**
  Three landlords asked, one never replies: the comparison step still runs, with
  two answers and one stated absence. This is deliberate — cascading skips would
  turn one silent landlord into no answer at all.
- **`reply` means you wrote in that chat**, not that a particular question was
  answered. Waiting on a reply *inside a site's own inbox* needs an inbox for
  that site, which does not exist yet; use `manual` or `at` for those.
- **A page condition is a small crawler.** Five-minute floor, one URL per step,
  and a site that blocks the fetch reads as "check did not happen" — never as a
  fired condition. Whether watching a given site is acceptable is your call, not
  something the code decides.
- **Steps do not share a conversation with your chat.** A step only knows the
  goal, its own prompt, and the results of the steps it depends on. Write step
  prompts that carry what they need.
