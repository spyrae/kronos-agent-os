---
name: price-watch
description: Watch a price for weeks and say something only when it means something.
tools: [plan_start, plan_add_step, fetch_page]
tier: lite
---

# Price Watch

## Set it up as a plan, not a reminder

A watch is a plan step parked on a condition. `plan_start` with the goal in the
owner's words, then `plan_add_step` with:

```json
{"kind": "page_number", "url": "<product page>",
 "pattern": "Rp ([0-9.]+)", "op": "below", "value": 9000000,
 "every_seconds": 3600}
```

and `notify: true` on that step — a price crossing the line is exactly what the
owner asked to be interrupted for.

Three details decide whether this works:

- **Watch the product page**, never a search or category page. Search results
  reorder, and the number you captured yesterday belonged to a different item.
- **Check the pattern once by hand** with `fetch_page` before parking the step. A
  pattern that matches nothing waits forever and reports nothing, which is
  indistinguishable from a price that never moved.
- **Give the plan an expiry.** Most watches stop being wanted long before they
  stop running.

## What counts as worth saying

A threshold, not a change. Marketplace prices jitter by a percent or two daily,
and a message for each jitter is a message the owner turns off — after which the
real drop arrives in a muted channel.

- If the owner named a target price, that is the threshold.
- If they said "let me know if it gets cheaper", propose one — around 10% below
  today's price is a starting point, and confirm it rather than guessing
  silently.
- Fire on **crossing**, not on being below. The condition fires once, the step
  runs once, and that is the end of it unless the owner asks for another watch.

## When it fires

The condition hands you what it saw — the price, and where. Do not re-fetch to
find out why you woke; the page may have changed in between and you would report
something the owner was not alerted about.

Say: the price now, the price when the watch started, the threshold, and the
link. Then stop. A watch that fires and then keeps watching without being asked
is a subscription nobody agreed to.

## When it does not fire

A site that cannot be read is not a price that held steady. If checks keep
failing, say so — silence reads as "nothing happened", and after a fortnight of
silence the owner is entitled to assume it is still watching.

## Output

- What crossed what, with both numbers and the link
- How long it took
- Whether to watch again — asked, not assumed
