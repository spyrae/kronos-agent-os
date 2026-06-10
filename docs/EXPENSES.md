# Expense Tracking

KAOS has one canonical path for expenses: the supervisor must call the direct
`add_expense` tool. Do not create expense rows through generic Notion tools or
Task Agent prompts.

## Canonical Notion schema

| Property | Meaning |
| --- | --- |
| `Description` | Expense title |
| `Date` | Expense date |
| `Category` | One of the allowed expense categories |
| `Amount_IDR` | Original amount in Indonesian rupiah |
| `Amount_RUB` | Calculated ruble amount |
| `Rate` | **IDR per 1 RUB** |
| `Split` | Whether only half of the expense belongs to the user |
| `Status` | Processing status |
| `Ref` | Optional deduplication reference |

Important invariant:

```text
Amount_RUB = round(Amount_IDR / Rate)
Rate = IDR per 1 RUB
```

Example:

```text
Amount_IDR = 411,500
Rate = 233.5
Amount_RUB = round(411,500 / 233.5) = 1,762
```

Do **not** convert `Rate` to `RUB per IDR`, `RUB per 1000 IDR`, or any other
derived unit. `Rate` in Notion must stay in the same unit as `BUDGET.md`
tranches: IDR/RUB.

## FIFO budget rules

- Active tranches live in `expense-tracker/references/BUDGET.md`.
- Each tranche `rate` is IDR per 1 RUB.
- FIFO consumes the oldest active tranche first.
- If an expense spans multiple tranches, `Rate` is the effective IDR/RUB rate
  for that expense.
- `BUDGET.md` is updated only after Notion successfully creates the expense
  page. If Notion fails, the budget must not be deducted.
- After a canonical write, KAOS archives incomplete duplicate Notion pages with
  the same `Date`, `Amount_IDR`, and normalized `Description` when `Amount_RUB`
  or `Rate` is missing. This protects against stale/non-canonical writers that
  still try to create IDR-only rows.

## Required environment

```bash
NOTION_API_KEY=...
NOTION_EXPENSES_DB_ID=...
```

Run `kaos doctor` after editing `.env`; it warns when Notion is configured but
the expenses database ID is missing.

## Regression tests

The guard tests live in `tests/test_expense_tool.py`. They assert:

- Notion failure does not deduct `BUDGET.md`.
- Successful Notion write deducts budget afterward.
- `Rate` stays in IDR/RUB (`411500 / 233.5 = 1762`, `Rate = 233.5`).
- Incomplete IDR-only duplicates are archived while complete rows are preserved.
