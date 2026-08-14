---
name: housing-search
description: Find somewhere to stay across listing sites, compare on total cost, and ask the questions a listing never answers.
tools: [fetch_page, extract_structured, compare_offers, open_site_session, plan_start]
tier: standard
---

# Housing Search

## Before searching

Six answers, and searching without them wastes the search:

1. **Dates and length of stay.** A month is a different market from a week —
   monthly rates on the big platforms run far below thirty nightly rates, and a
   search priced per night answers a question nobody asked.
2. **Total budget, not the nightly figure.** The number that matters is rent for
   the whole stay plus deposit plus utilities plus fees.
3. **Area**, at the granularity the owner actually cares about.
4. **Non-negotiables** — the two or three things whose absence rules a place out
   (air conditioning, a desk, a kitchen, parking).
5. **Who is staying** — children, pets and long-stay guests all change what is
   available and what it costs.
6. **When they need to decide.** It changes whether to shortlist three or thirty.

Anything the owner has told you before lives in `notes/user/`. Ask only for what
is missing, and ask once.

## Searching

Search each site separately and in parallel; they overlap less than people
expect. Fetch listing pages with `fetch_page` — search-result pages reorder
themselves and are worth less than they look.

Signed in, prices and availability differ. If the owner has an account for the
site, open it first (`open_site_session`) and say in the result whether you were
signed in — an unsigned search is a different set of prices, and presenting it
as theirs is a small lie that compounds.

## What a listing does not tell you

Collect these per candidate, and **never fill one in by assumption**. A listing
that does not state the deposit is not a listing with no deposit; a missing
figure is not a zero one, and treating it as one puts the least documented place
at the top of the list:

- deposit: amount, and what returns it
- utilities: who pays, and what they actually run to in the hot or cold season
- minimum stay, and what leaving early costs
- what is not included — cleaning, linen, agency fee, tourist tax
- who repairs what, and how quickly
- internet: the real speed, not the advertised one

## Red flags

Worth naming to the owner rather than silently down-ranking:

- no photo of the bathroom or the kitchen
- reviews that all appear within one week, or none at all
- a price far below the cluster for that area, with nothing in the listing
  explaining why
- the location shown only as a district
- the same photographs appearing on other listings
- pressure to move the conversation or the payment off the platform

## Comparing

Use `compare_offers`: recurring costs are rent and utilities, one-off costs are
deposit, cleaning and fees, and `periods` is the number of months. It ranks on
money and refuses to rank a listing missing a cost — which is the correct
outcome, not a gap to paper over.

Then judge the rest yourself, out loud: a place ten minutes further out for a
third less is a trade the owner makes, not the arithmetic.

## Waiting

Enquiries take days. Do not hold the conversation open — start a plan
(`plan_start`), add a step per landlord, and a comparison step that depends on
all of them. A landlord who never answers fails their step; the comparison still
runs, with that absence stated.

## Output

- Shortlist, ranked by total cost for the whole stay, with the arithmetic shown
- What is unknown per listing, and what you asked
- Red flags, named
- Your recommendation and the tradeoff it accepts
