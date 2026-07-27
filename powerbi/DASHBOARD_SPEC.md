# Power BI dashboard specification

**This repository does not contain a .pbix file.** Power BI Desktop is
Windows-only and the pipeline was built on Linux. What is provided instead is the
complete star schema in `exports/`, every DAX measure in `dax_measures.md`, and
the page-by-page build below. Following it reproduces the report exactly.

---

## Import and model setup

Load all eight CSVs from `exports/` via **Get Data → Text/CSV → Transform Data**.

### Relationships

| From | To | Cardinality | Direction |
|---|---|---|---|
| `fact_alert[customer_key]` | `dim_customer[customer_key]` | Many-to-one | Single |
| `fact_alert[date_key]` | `dim_date[date_key]` | Many-to-one | Single |
| `fact_alert[rule_id]` | `dim_rule[rule_id]` | Many-to-one | Single |
| `fact_customer_week[customer_key]` | `dim_customer[customer_key]` | Many-to-one | Single |
| `fact_txn_drill[customer_key]` | `dim_customer[customer_key]` | Many-to-one | Single |
| `fact_txn_drill[date_key]` | `dim_date[date_key]` | Many-to-one | Single |
| `fact_txn_drill[counterparty_country]` | `dim_country[country_code]` | Many-to-one | Single |

**All relationships single-direction.** Bidirectional filtering across two facts
sharing a dimension creates ambiguous filter paths, and Power BI resolves
ambiguity by returning a number rather than an error — which is worse than
failing. If a visual needs cross-fact filtering, use `TREATAS` in the measure.

Mark `dim_date` as the date table on `calendar_date`.

### Model hygiene

- Hide every `*_key` column from report view.
- Hide `fact_alert[score]` and expose it only through measures.
- Sort `dim_customer[tenure_band]` by a numeric sort column.
- Set `fact_alert[is_true_positive]` summarisation to **Do not summarize**.

---

## Page 1 — Investigator queue (the default page)

**This page opens first, and that is a deliberate choice.** An earlier design led
with the executive scorecard. The primary user of this system is an investigator
at 9am with a queue to work, not an executive reviewing a monthly number. The
scorecard is page 2.

**Layout**

- **Slicers (top bar):** date range, tier, rule, KYC risk rating, customer segment.
- **Left, 70% width — the queue.** Table visual, sorted by `Alert Score`
  descending:
  `alert_id` · `calendar_date` · customer name/segment · `rule_id` ·
  `Alert Score` · `tier` · `reason_1` · `reason_2` · `reason_3` · `top_mitigant`
  - Conditional formatting: data bar on `Alert Score`; tier coloured
    T1 red / T2 amber / T3 grey.
- **Right, 30% — selected alert context.**
  - Cards: `Total Alerts`, `Tier 1 Alerts`, `Alerts per Investigator Day`.
  - Line chart from `fact_customer_week`: credit value, cash credit value and
    high-risk value by week for the selected customer, with the alert week
    marked.
  - Card: `Prior Alerts This Customer`.
- **Drill-through page — transaction detail.** Configure a drill-through on
  `customer_key` into a table over `fact_txn_drill`. This is what an
  investigator opens when they want the underlying records.

---

## Page 2 — Compliance scorecard

For the Head of Financial Crime and the CCO.

- **KPI row:** `Total Alerts` · `False Positive Rate` · `Precision at Priority` ·
  `Volume Deferred %` · `Recall at Threshold` · `Net Annual Benefit`.
- **Column chart:** alert volume by month, split by tier.
- **Scatter:** rules plotted as alert volume (x) against precision (y), bubble
  size = annual cost. The visual argument for recalibrating R002 is immediate:
  high volume, low precision, large bubble.
- **Table:** rule effectiveness from `rule_effectiveness.csv`, including the
  `recommendation` column.
- **Callout card:** dead-rule count, with R007 named.

---

## Page 3 — Model performance

For model validation and for anyone who wants to argue with the numbers.

- **Trade-off table** from the metrics: recall target, holdout recall achieved,
  volume deferred, precision. Reproduces the CCO memo table.
- **Tier concentration:** stacked bar showing share of volume against share of
  true positives per tier — the 10%/81% contrast is the single most persuasive
  visual in the report.
- **Feature importance:** horizontal bar from `feature_importance.csv`, top 15.
- **Text box, prominent:** synthetic data notice and the statement that
  performance is an upper bound.

---

## Page 4 — Data quality scorecard

Directly answers the "measure and ensure data quality" requirement that appears
in Canadian analyst job descriptions almost verbatim.

- **Cards:** total checks, blocking failures, warnings.
- **Table** from `dq_scorecard.csv`: check id, name, dimension, severity, metric,
  threshold, status — with red/green status formatting.
- **Donut:** checks by dimension (reconciliation, uniqueness, integrity,
  validity, completeness).
- **Text box:** the quarantine policy, and the count of rows in
  `rejected_transaction` by reason.

---

## Row-level security

Define a role **Investigator_Region** so an investigator sees only their book:

```dax
[home_province] = USERPRINCIPALNAME()
```

In production this joins to a user-to-region mapping table rather than matching
the province directly. Test with **Modeling → View as → Investigator_Region**.

RLS matters here beyond the demonstration: alert data is among the most
access-restricted data in a bank, and a monitoring dashboard without row-level
security would not pass an access review.

---

## Performance notes

- Import mode, not DirectQuery. The dataset is small and refresh is not a
  constraint.
- `fact_txn_drill` is restricted to Tier 1 customers. Widening it to Tier 2
  roughly doubles the model size for drill paths rarely taken.
- Avoid calculated columns on the fact tables; every derived value here is
  either computed upstream in SQL or expressed as a measure.
