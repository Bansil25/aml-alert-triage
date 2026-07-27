-- =============================================================================
-- 05_marts.sql — serving layer for Power BI
--
-- Power BI consumes a STAR, not these views flattened together. The views below
-- shape each table to its grain and let the model do the joining, because a
-- single wide flattened table destroys the ability to filter one fact by
-- another dimension and forces every measure into a bidirectional relationship.
--
-- Grain of each mart:
--   mart_alert          one row per alert (the primary fact)
--   mart_customer_week  one row per customer-week (trend fact)
--   mart_txn_drill      one row per transaction, limited to alerted customers
--   mart_dq             one row per data quality check per run
-- =============================================================================

DROP VIEW IF EXISTS mart_alert;
DROP VIEW IF EXISTS mart_customer_week;
DROP VIEW IF EXISTS mart_txn_drill;
DROP VIEW IF EXISTS mart_dq;

-- Primary alert fact. One row per alert, carrying the model output and the
-- investigator-facing reason codes.
CREATE VIEW mart_alert AS
SELECT
    a.alert_id,
    a.rule_id,
    a.customer_key,
    a.date_key,
    d.calendar_date,
    a.trigger_amount,
    a.trigger_txn_count,
    a.detail,
    s.score,
    s.score_percentile,
    s.tier,
    s.priority,
    s.split,
    s.is_true_positive,
    s.reason_1,
    s.reason_2,
    s.reason_3,
    s.top_mitigant
FROM fact_alert a
JOIN dim_date d   ON d.date_key = a.date_key
LEFT JOIN stg_scores s ON s.alert_id = a.alert_id;

-- Weekly customer trend fact, restricted to customers that generated at least
-- one alert. Restricting here rather than in Power BI keeps the exported model
-- small without losing anything the dashboard can actually reach.
CREATE VIEW mart_customer_week AS
SELECT
    f.customer_key,
    f.date_key / 7 AS week_index,
    MIN(d.calendar_date) AS week_start_date,
    COUNT(*) AS n_txn,
    SUM(f.amount_cad) AS total_value,
    SUM(CASE WHEN f.direction = 'credit' THEN f.amount_cad ELSE 0 END) AS credit_value,
    SUM(CASE WHEN f.direction = 'debit'  THEN f.amount_cad ELSE 0 END) AS debit_value,
    SUM(CASE WHEN f.channel = 'cash' AND f.direction = 'credit'
             THEN f.amount_cad ELSE 0 END) AS cash_credit_value,
    SUM(CASE WHEN c.is_high_risk = 1 THEN f.amount_cad ELSE 0 END) AS high_risk_value,
    COUNT(DISTINCT f.counterparty_id) AS distinct_counterparties
FROM fact_transaction f
JOIN dim_date d    ON d.date_key = f.date_key
JOIN dim_country c ON c.country_code = f.counterparty_country
WHERE f.customer_key IN (SELECT DISTINCT customer_key FROM fact_alert)
GROUP BY f.customer_key, f.date_key / 7;

-- Transaction-level drill-through, limited to customers with a top-tier alert.
-- An investigator needs the underlying records; a reviewer cloning the repo does
-- not need half a million rows. Widening this to T2 roughly doubles the export
-- size for drill paths that are rarely taken, so the cut is deliberate.
CREATE VIEW mart_txn_drill AS
SELECT
    f.txn_id, f.customer_key, f.date_key, d.calendar_date,
    f.amount_cad, f.direction, f.channel,
    f.counterparty_country, f.counterparty_id, f.hour_of_day,
    c.is_high_risk
FROM fact_transaction f
JOIN dim_date d    ON d.date_key = f.date_key
JOIN dim_country c ON c.country_code = f.counterparty_country
WHERE f.customer_key IN (
    SELECT DISTINCT customer_key FROM stg_scores WHERE tier = 'T1_same_day'
);

CREATE VIEW mart_dq AS
SELECT run_ts, check_id, check_name, dimension, severity,
       metric_value, threshold, status,
       CASE WHEN status = 'PASS' THEN 1 ELSE 0 END AS is_pass
FROM dq_result;
