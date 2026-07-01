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
| `Amount_IDR` | Original amount in Indonesian rupiah; empty for RUB/USD-native expenses |
| `Amount_RUB` | Calculated ruble amount for IDR, or original RUB amount for RUB-native expenses |
| `Amount_USD` | Calculated dollar amount for IDR, or original USD amount for USD-native expenses |
| `Rate` | **IDR per 1 RUB**; empty for RUB/USD-native expenses |
| `Rate_USD` | **IDR per 1 USD**; empty for RUB/USD-native expenses |
| `Split` | Whether only half of the expense belongs to the user |
| `Status` | Processing status |
| `Ref` | Optional deduplication reference |

Important IDR invariant — a rupiah expense converts to **both** RUB and USD at once:

```text
Amount_RUB = round(Amount_IDR / Rate)          Rate     = IDR per 1 RUB
Amount_USD = round(Amount_IDR / Rate_USD, 2)   Rate_USD = IDR per 1 USD
```

Example:

```text
Amount_IDR = 411,500
Rate       = 233.5    → Amount_RUB = round(411,500 / 233.5)      = 1,762
Rate_USD   = 16,300   → Amount_USD = round(411,500 / 16,300, 2)  = 25.25
```

Do **not** invert the rates (`RUB per IDR`, `USD per 1000 IDR`, etc.). Both `Rate`
and `Rate_USD` in Notion stay in the same unit as the `BUDGET.md` tranches:
IDR per 1 unit.

Legacy tranches without a USD rate still produce `Amount_RUB`; `Amount_USD` and
`Rate_USD` are left empty until the consumed tranche carries an IDR/USD rate.

Important RUB invariant (only RUB, tranches untouched):

```text
Amount_RUB = original RUB amount
Amount_IDR = empty      Amount_USD = empty
Rate = empty            Rate_USD = empty
```

Example:

```text
496 ₽ JourneyBay hosting
Amount_RUB = 496 ; everything else empty
```

Important USD invariant (only USD, tranches untouched):

```text
Amount_USD = original USD amount
Amount_IDR = empty      Amount_RUB = empty
Rate = empty            Rate_USD = empty
```

Example:

```text
$12.50 ChatGPT subscription
Amount_USD = 12.5 ; everything else empty
```

## FIFO budget rules

- Active tranches live in `expense-tracker/references/BUDGET.md`.
- Each tranche carries **two** rates: `Курс (IDR/RUB)` (IDR per 1 RUB) and
  `Курс (IDR/USD)` (IDR per 1 USD). Table columns:

  ```text
  | # | Дата | Сумма IDR | Остаток IDR | Курс (IDR/RUB) | Курс (IDR/USD) | Заметка |
  ```

- The USD-rate column is optional for backward compatibility. A legacy 6-column
  tranche (no USD rate) is still parsed; it just yields no `Amount_USD`.
- FIFO applies only to IDR expenses. RUB-native and USD-native expenses never
  read or update tranches.
- FIFO consumes the oldest active tranche first.
- If an expense spans multiple tranches, `Rate`/`Rate_USD` are the **effective**
  IDR/RUB and IDR/USD rates for that expense.
- `Amount_USD` is only written when **every** consumed tranche has a USD rate;
  otherwise the RUB conversion still happens and USD is left empty.
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
- IDR converts to both RUB and USD when the tranche carries a USD rate
  (`Rate_USD = 16300`, `Amount_USD = round(411500 / 16300, 2)`).
- A legacy tranche without a USD rate still yields `Amount_RUB`, but no
  `Amount_USD`/`Rate_USD` (backward compatibility).
- USD-native expenses write only `Amount_USD` and never read the budget.
- Incomplete IDR-only duplicates are archived while complete rows are preserved.
