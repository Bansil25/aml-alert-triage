# DAX measures

Paste into a measure table (`_Measures`) in Power BI Desktop. Written as
measures rather than calculated columns throughout — calculated columns are
materialised into the model at refresh and are the usual reason a report that
worked in development crawls in production.

---

## Alert volume

```dax
Total Alerts = COUNTROWS ( fact_alert )
```

```dax
Tier 1 Alerts =
CALCULATE ( [Total Alerts], fact_alert[tier] = "T1_same_day" )
```

```dax
Deferred Alerts =
CALCULATE ( [Total Alerts], fact_alert[priority] = "deferred" )
```

```dax
Volume Deferred % =
DIVIDE ( [Deferred Alerts], [Total Alerts] )
```

```dax
Alerts per Investigator Day =
VAR DaysInContext = DISTINCTCOUNT ( dim_date[date_key] )
RETURN DIVIDE ( [Total Alerts], DaysInContext )
```

---

## Detection quality

`is_true_positive` is the confirmed disposition. In production this becomes the
SAR/STR filing outcome.

```dax
True Positives = SUM ( fact_alert[is_true_positive] )
```

```dax
False Positives = [Total Alerts] - [True Positives]
```

```dax
False Positive Rate =
DIVIDE ( [False Positives], [Total Alerts] )
```

```dax
Precision =
DIVIDE ( [True Positives], [Total Alerts] )
```

```dax
Precision at Priority =
CALCULATE ( [Precision], fact_alert[priority] = "review" )
```

```dax
-- Recall inside the current filter context, measured against ALL true positives
-- in the period. ALLSELECTED on tier is what makes the denominator stay whole
-- when the user filters to a single tier -- without it, recall reads 100% in
-- every tier, which is both wrong and reassuring, the worst combination.
Recall =
VAR PositivesInScope =
    CALCULATE ( [True Positives], ALLSELECTED ( fact_alert[tier] ) )
RETURN
    DIVIDE ( [True Positives], PositivesInScope )
```

```dax
Detection Lift =
DIVIDE (
    [Precision at Priority],
    CALCULATE ( [Precision], ALL ( fact_alert[tier] ), ALL ( fact_alert[priority] ) )
)
```

```dax
Share of True Positives =
DIVIDE (
    [True Positives],
    CALCULATE ( [True Positives], ALLSELECTED ( fact_alert ) )
)
```

```dax
Share of Alert Volume =
DIVIDE (
    [Total Alerts],
    CALCULATE ( [Total Alerts], ALLSELECTED ( fact_alert ) )
)
```

---

## Cost and capacity

Assumptions mirror `config/params.yml`. Held as measures so a reviewer can
change one and watch the report move.

```dax
Investigator Hourly Rate = 58.33   -- 75,000 x 1.4 loading / 1,800 hours
Minutes per Alert = 22
Deferred Effort Factor = 0.60      -- deferred alerts still consume 40% of effort
```

```dax
Investigator Hours =
[Total Alerts] * DIVIDE ( [Minutes per Alert], 60 )
```

```dax
Alert Handling Cost =
[Investigator Hours] * [Investigator Hourly Rate]
```

```dax
Hours Released by Deferral =
[Deferred Alerts] * DIVIDE ( [Minutes per Alert], 60 ) * [Deferred Effort Factor]
```

```dax
Value Released by Deferral =
[Hours Released by Deferral] * [Investigator Hourly Rate]
```

```dax
-- Annualised from whatever period is in filter context.
Annualised Value Released =
VAR DaysInContext = DISTINCTCOUNT ( dim_date[date_key] )
RETURN DIVIDE ( [Value Released by Deferral] * 365, DaysInContext )
```

---

## Rule analysis

```dax
Rule Precision =
DIVIDE ( [True Positives], [Total Alerts] )
```

```dax
Rule Annual Cost =
VAR DaysInContext = DISTINCTCOUNT ( dim_date[date_key] )
RETURN DIVIDE ( [Alert Handling Cost] * 365, DaysInContext )
```

```dax
-- Illicit customers this rule detects that NO other rule detects. The measure
-- that stops an efficiency programme from retiring a rule that is somebody's
-- only line of sight onto a typology.
Sole Detection Count =
VAR ThisRuleCustomers =
    CALCULATETABLE (
        VALUES ( fact_alert[customer_key] ),
        fact_alert[is_true_positive] = 1
    )
VAR OtherRuleCustomers =
    CALCULATETABLE (
        VALUES ( fact_alert[customer_key] ),
        fact_alert[is_true_positive] = 1,
        ALL ( dim_rule ),
        NOT ( dim_rule[rule_id] IN VALUES ( dim_rule[rule_id] ) )
    )
RETURN
    COUNTROWS ( EXCEPT ( ThisRuleCustomers, OtherRuleCustomers ) )
```

```dax
Rule Status =
SWITCH (
    TRUE (),
    [Total Alerts] = 0, "RETIRE - never fires",
    [Sole Detection Count] > 0, "RETAIN - sole detector",
    [Rule Precision] < 0.03, "RECALIBRATE - low precision, covered elsewhere",
    "RETAIN"
)
```

---

## Customer context

```dax
Prior Alerts This Customer =
CALCULATE (
    [Total Alerts],
    ALLEXCEPT ( fact_alert, fact_alert[customer_key] ),
    dim_date[date_key] < MAX ( fact_alert[date_key] )
)
```

```dax
Customer Credit Value 30d =
CALCULATE (
    SUM ( fact_customer_week[credit_value] ),
    DATESINPERIOD ( dim_date[calendar_date], MAX ( dim_date[calendar_date] ), -30, DAY )
)
```

```dax
Cash Share =
DIVIDE (
    SUM ( fact_customer_week[cash_credit_value] ),
    SUM ( fact_customer_week[credit_value] )
)
```

```dax
High Risk Exposure =
DIVIDE (
    SUM ( fact_customer_week[high_risk_value] ),
    SUM ( fact_customer_week[total_value] )
)
```

---

## Data quality

```dax
DQ Checks Run = COUNTROWS ( dq_scorecard )
```

```dax
DQ Blocking Failures =
CALCULATE (
    COUNTROWS ( dq_scorecard ),
    dq_scorecard[status] = "FAIL",
    dq_scorecard[severity] = "blocking"
)
```

```dax
DQ Pass Rate =
DIVIDE ( SUM ( dq_scorecard[is_pass] ), [DQ Checks Run] )
```

```dax
DQ Status =
IF ( [DQ Blocking Failures] > 0, "PIPELINE BLOCKED", "ALL CONTROLS PASSING" )
```

---

## Formatting

| Measure | Format |
|---|---|
| `False Positive Rate`, `Precision*`, `Recall`, `Volume Deferred %`, `Share of *`, `DQ Pass Rate` | Percentage, 1-2 dp |
| `Alert Handling Cost`, `Value Released*`, `Rule Annual Cost` | Currency CAD, 0 dp |
| `Total Alerts`, `True Positives`, counts | Whole number, thousands separator |
| `Detection Lift` | Decimal, 1 dp, suffix "x" |
