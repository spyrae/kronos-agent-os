---
name: marketplace-compare
description: Compare the same product across marketplaces on landed cost, and read what a seller's rating actually says.
tools: [fetch_page, extract_structured, compare_offers]
tier: standard
---

# Marketplace Compare

## The number that matters

Not the price. The **landed cost**: price plus shipping plus insurance plus any
import duty and payment fee — what leaves the owner's account for the thing to
arrive at their door.

Feed it to `compare_offers` with `price` as recurring (one period) and shipping,
duty and fees as one-off. It will refuse to rank a listing whose shipping nobody
stated, and that refusal is the point: a missing figure is not a zero one, and a
listing that does not state its shipping is not the cheapest one.

Different currencies do not add up. Convert first, say what rate you used, or
compare within one currency and say so.

## Reading a seller

A rating without a volume means nothing. **4.9 across 3,000 sales beats 5.0
across 12**, and the second looks better in every table that sorts on rating
alone. Collect together, and hand them to the owner rather than scoring them:

- rating **and** number of ratings
- how long the store has existed
- how fast it responds
- whether the photographs are the seller's own or the manufacturer's

Do not rank on any of this. The trade between "cheapest" and "a shop that has
been there four years" is the owner's to make, and a weight invented in code
makes it silently.

## Signs of a grey unit

For electronics especially, worth naming:

- priced well below the cluster with nothing explaining it
- warranty from the shop rather than the manufacturer
- "ready stock" on a listing created days ago
- stock photographs only, no photo of the actual box
- a region or model variant that differs from what the local distributor sells —
  the practical cost is that nobody local will service it

None of these means "do not buy". They mean "the owner should know before
buying".

## Method

1. Fetch each product page directly (`fetch_page`). Search pages reorder and go
   stale; a product page is the claim.
2. Extract into one shape (`extract_structured`) — see `structured-extraction`.
3. Total with `compare_offers`.
4. Report the ranking, the sellers, and anything odd.

If a site cannot be read at all, say the source was unavailable. A comparison
missing a marketplace is not a comparison of both.

## Output

- Landed cost per option, cheapest first, with the arithmetic
- Seller facts beside each — not folded into a score
- Anything that looks grey, named
- What you could not check
