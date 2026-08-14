---
name: structured-extraction
description: Turn pages that disagree about everything into rows that can be compared, without inventing the parts they left out.
tools: [fetch_page, extract_structured, run_code]
tier: lite
---

# Structured Extraction

Comparing anything means first making the sources agree on shape. Two listing
sites will name the same thing three ways and omit a different field each. The
whole job is doing that without quietly filling the gaps.

## One schema, decided first

Write the fields down before fetching. For an offer of any kind, that is usually:

| Field | Notes |
|---|---|
| `name` | what the owner would call it |
| `url` | the page this row came from — always |
| `price` | as shown |
| `currency` | explicit on every row, never inferred from the site |
| one field per additional cost | `deposit`, `shipping`, `utilities`, `duty` |
| `source` | which site |

Add whatever the specific question needs, and no more. A schema with fields
nobody will compare is a schema that invites guessing.

## The rule that matters

**A field the page did not state is missing, not zero, and not "probably the
usual".** A listing without a stated deposit is not a listing with no deposit.
Leave it out, or mark it absent, and let the comparison refuse to rank that row —
`compare_offers` does exactly that, and the refusal is information.

Same for units and currency. `8.750.000` is a number written the way that site
writes numbers; `Rp 8.750.000` is a price. Carry the currency per row, because
the moment two currencies meet in one total the total is wrong and looks right.

## Method

1. `fetch_page` on the item's own page — not the search listing.
2. `extract_structured` with the field list. It runs a small model against the
   text and returns fields, which is cheaper and steadier than reading prose.
3. Check the rows. If one field is empty on every row from one site, the pattern
   is wrong, not the site.
4. For more than a handful of rows, or any arithmetic beyond adding, use
   `run_code` — a program that sums is checkable, a paragraph that sums is not.

## Reporting

State per row where it came from and what was missing. A table that looks
complete because the gaps were filled is worse than one with holes, because
nobody can see which numbers to distrust.

## Output

- Rows in one shape, each with its source URL
- What was missing, per row
- Which sources could not be read at all
