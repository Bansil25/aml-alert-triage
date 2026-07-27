# Data model

## Grain declarations

Grain is declared before anything else because every measure, every join and
every duplicate-row bug traces back to an unstated or misunderstood grain.

| Table | Grain | Rows |
|---|---|---|
| `fact_transaction` | one settled transaction line | ~469,000 |
| `fact_alert` | one (rule, customer, alert date) | 28,276 |
| `dim_customer` | one customer, SCD Type 1 | 4,000 |
| `dim_account` | one account, SCD Type 1 | 5,349 |
| `dim_date` | one calendar day | 180 |
| `dim_rule` | one detection rule | 8 |
| `dim_country` | one jurisdiction | 14 |
| `dq_result` | one control check per run | 13 |
| `rejected_transaction` | one quarantined source row | ~180 |

## Schema

```
                    dim_date
                       |
      dim_customer ----+---- dim_rule
           |           |         |
           |      fact_alert ----+
           |           |
      dim_account      |
           |           |
      fact_transaction-+
           |
      dim_country
```

`fact_alert` and `fact_transaction` share `dim_customer` and `dim_date` as
conformed dimensions, which is what allows the dashboard to filter alert
volumes and underlying transaction behaviour by the same date and customer
slicers without bidirectional relationships.

## Why a star and not one wide table

A single flattened table would make the two most useful questions in the
dashboard impossible to ask together: "how many alerts did cash-intensive
businesses generate last month" and "what was total cash volume for those same
customers over the same period" are measures at different grains. Flattening
forces one of them into a fan-out, and the usual fix — bidirectional
relationships — produces ambiguous filter paths that silently return wrong
numbers rather than errors.

## Key decisions

**Surrogate keys on dimensions.** `customer_key` and `account_key` are integers
generated at load rather than the natural business identifiers. This is habit
rather than necessity at this scale, but it is the habit that makes Type 2
history possible later without rewriting every fact join.

**`UNK` is a dimension member, not a NULL.** Roughly 2.9% of wires arrive with
no counterparty jurisdiction. Coding those as NULL would drop them from every
country-filtered visual, quietly understating exposure. Unknown jurisdiction on
a wire is itself a risk signal and has to be countable, so it gets a key.

**Rejected rows are quarantined, not dropped.** `rejected_transaction` retains
the identifier, the reason and the raw payload. "Where did those 40 rows go" has
to have an answer.

**`signed_amount_cad` is stored, not computed.** Credit positive, debit
negative. Net-flow measures are then simple sums rather than DAX conditionals
evaluated per row, which matters for performance on the transaction fact.

## Known simplifications

These are simplifications I made deliberately and would not make in production.

1. **SCD Type 1 on `dim_customer`.** The simulation emits one snapshot, so
   dimensions overwrite. In production `dim_customer` must be Type 2 on
   `kyc_risk_rating` and address, because an alert has to be assessed against
   the risk rating that was in force on the alert date. Using today's rating to
   evaluate a six-month-old alert is a correctness error, not a nicety, and it
   is the first thing I would change.

2. **No account-level balance fact.** Balance is inferable from signed flows but
   is not carried. Several genuinely useful features — balance retention curves,
   peak-to-trough ratios — are unavailable as a result.

3. **Counterparties are identifiers, not a dimension.** There is no
   `dim_counterparty`, so counterparty-ring analysis is limited to counts and
   concentration. Entity resolution across name variants is the substantial
   piece of work this omits, and it is where a real programme finds networks.

4. **Single currency.** Everything is CAD. Multi-currency introduces conversion
   timing, rate sourcing and a whole additional class of reconciliation break.

## Portability

The SQL targets SQLite so the repository runs anywhere with no server. Three
deliberate exceptions to portable ANSI:

- `STRFTIME` for date parts (`DATEPART` in T-SQL, `EXTRACT` in Snowflake)
- `GROUP_CONCAT` (`STRING_AGG` in both T-SQL and Snowflake)
- integer division on `date_key / 7` for week bucketing

The window functions — `RANGE BETWEEN n PRECEDING AND CURRENT ROW`, `LAG`,
`ROW_NUMBER` — are standard and lift unchanged.
