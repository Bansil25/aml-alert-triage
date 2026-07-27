-- =============================================================================
-- 03_rules.sql — rules-based transaction monitoring engine
--
-- This file reproduces the kind of deterministic rule set a bank's monitoring
-- platform runs nightly. It is deliberately NOT tuned to be precise: high false
-- positive rates are the industry reality and are the problem the downstream
-- model addresses. Making these rules smarter would be solving the wrong
-- problem -- the point is to triage the output of rules that already exist.
--
-- TECHNIQUE NOTE: rolling time windows use RANGE frames over an integer hour
-- index (date_key * 24 + hour_of_day). RANGE (not ROWS) is essential here --
-- ROWS would count the previous N *records* regardless of when they occurred,
-- which is a genuinely different and wrong question. This distinction is the
-- most common SQL interview probe on this topic.
--
-- Alert grain: one alert per (rule_id, customer_key, date_key).
-- =============================================================================

DELETE FROM fact_alert;

-- ---------------------------------------------------------------------- R001
-- Near-threshold cash clustering: 3+ cash credits in the 8,000-9,999 band
-- within a rolling 48 hours.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R001-' || customer_key || '-' || date_key,
    'R001', customer_key, date_key,
    MAX(win_amt), MAX(win_cnt),
    'cash credits in 8k-9,999 band within 48h: ' || MAX(win_cnt)
FROM (
    SELECT
        customer_key,
        date_key,
        COUNT(*)        OVER w AS win_cnt,
        SUM(amount_cad) OVER w AS win_amt
    FROM (
        SELECT customer_key, date_key, amount_cad,
               date_key * 24 + hour_of_day AS hr_idx
        FROM fact_transaction
        WHERE channel = 'cash'
          AND direction = 'credit'
          AND amount_cad BETWEEN 8000 AND 9999.99
    )
    WINDOW w AS (PARTITION BY customer_key ORDER BY hr_idx
                 RANGE BETWEEN 47 PRECEDING AND CURRENT ROW)
)
WHERE win_cnt >= 3
GROUP BY customer_key, date_key;

-- ---------------------------------------------------------------------- R002
-- LCTR aggregation avoidance: cash credits totalling >= 10,000 within 24 hours
-- where no single transaction reached the reporting threshold.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R002-' || customer_key || '-' || date_key,
    'R002', customer_key, date_key,
    MAX(win_amt), MAX(win_cnt),
    'aggregated cash credits within 24h: $' || CAST(ROUND(MAX(win_amt)) AS TEXT)
FROM (
    SELECT
        customer_key, date_key,
        COUNT(*)         OVER w AS win_cnt,
        SUM(amount_cad)  OVER w AS win_amt,
        MAX(amount_cad)  OVER w AS win_max
    FROM (
        SELECT customer_key, date_key, amount_cad,
               date_key * 24 + hour_of_day AS hr_idx
        FROM fact_transaction
        WHERE channel = 'cash' AND direction = 'credit'
    )
    WINDOW w AS (PARTITION BY customer_key ORDER BY hr_idx
                 RANGE BETWEEN 23 PRECEDING AND CURRENT ROW)
)
WHERE win_amt >= 10000
  AND win_max < 10000
  AND win_cnt >= 2
GROUP BY customer_key, date_key;

-- ---------------------------------------------------------------------- R003
-- Rapid movement of funds: an inbound credit >= 15,000 where >= 80% of the
-- value exits the account within 72 hours.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R003-' || customer_key || '-' || date_key,
    'R003', customer_key, date_key,
    MAX(inflow), MAX(n_out),
    'pass-through ratio ' || CAST(ROUND(MAX(outflow) * 100.0 / MAX(inflow)) AS TEXT) || '% within 72h'
FROM (
    SELECT
        c.customer_key,
        c.date_key,
        c.amount_cad AS inflow,
        COALESCE(SUM(d.amount_cad), 0) AS outflow,
        COUNT(d.txn_id) AS n_out
    FROM (
        SELECT txn_id, customer_key, account_key, date_key, amount_cad,
               date_key * 24 + hour_of_day AS hr_idx
        FROM fact_transaction
        WHERE direction = 'credit' AND amount_cad >= 15000
    ) c
    LEFT JOIN (
        SELECT txn_id, account_key, amount_cad,
               date_key * 24 + hour_of_day AS hr_idx
        FROM fact_transaction
        WHERE direction = 'debit'
    ) d
      ON d.account_key = c.account_key
     AND d.hr_idx > c.hr_idx
     AND d.hr_idx <= c.hr_idx + 72
    GROUP BY c.txn_id
)
WHERE outflow >= 0.80 * inflow
GROUP BY customer_key, date_key;

-- ---------------------------------------------------------------------- R004
-- High-risk jurisdiction wire activity >= 5,000.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R004-' || f.customer_key || '-' || f.date_key,
    'R004', f.customer_key, f.date_key,
    SUM(f.amount_cad), COUNT(*),
    'wire activity with high-risk jurisdiction: ' || GROUP_CONCAT(DISTINCT f.counterparty_country)
FROM fact_transaction f
JOIN dim_country c ON c.country_code = f.counterparty_country
WHERE f.channel = 'wire'
  AND f.amount_cad >= 5000
  AND c.is_high_risk = 1
GROUP BY f.customer_key, f.date_key;

-- ---------------------------------------------------------------------- R005
-- Volume velocity spike: rolling 30-day credit volume exceeds 3x the customer's
-- prior 90-day daily average. Uses RANGE frames over date_key.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R005-' || customer_key || '-' || date_key,
    'R005', customer_key, date_key, vol_30d, NULL,
    'rolling 30d credit volume ' || CAST(ROUND(vol_30d / NULLIF(baseline_30d, 0), 1) AS TEXT)
        || 'x the prior 90d baseline'
FROM (
    SELECT
        customer_key,
        date_key,
        SUM(credit_vol) OVER (PARTITION BY customer_key ORDER BY date_key
                              RANGE BETWEEN 29 PRECEDING AND CURRENT ROW) AS vol_30d,
        -- Prior 90 days, excluding the current 30-day window, scaled to 30 days.
        AVG(credit_vol) OVER (PARTITION BY customer_key ORDER BY date_key
                              RANGE BETWEEN 119 PRECEDING AND 30 PRECEDING) * 30 AS baseline_30d,
        COUNT(*)        OVER (PARTITION BY customer_key ORDER BY date_key
                              RANGE BETWEEN 119 PRECEDING AND 30 PRECEDING) AS baseline_days
    FROM (
        SELECT customer_key, date_key,
               SUM(CASE WHEN direction = 'credit' THEN amount_cad ELSE 0 END) AS credit_vol
        FROM fact_transaction
        GROUP BY customer_key, date_key
    )
)
WHERE baseline_days >= 20          -- require a real baseline before judging
  AND baseline_30d > 0
  AND vol_30d > 3.0 * baseline_30d
  AND vol_30d > 25000;             -- suppress trivial-value spikes

-- ---------------------------------------------------------------------- R006
-- Round-value transfer clustering: 3+ round-thousand wire/e-transfer movements
-- of >= 5,000 within a rolling 7 days.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R006-' || customer_key || '-' || date_key,
    'R006', customer_key, date_key, MAX(win_amt), MAX(win_cnt),
    'round-value transfers in 7d: ' || MAX(win_cnt)
FROM (
    SELECT
        customer_key, date_key,
        COUNT(*)        OVER w AS win_cnt,
        SUM(amount_cad) OVER w AS win_amt
    FROM (
        SELECT customer_key, date_key, amount_cad,
               date_key * 24 + hour_of_day AS hr_idx
        FROM fact_transaction
        WHERE channel IN ('wire', 'emt')
          AND amount_cad >= 5000
          AND CAST(amount_cad AS INTEGER) % 1000 = 0
          AND amount_cad = CAST(amount_cad AS INTEGER)
    )
    WINDOW w AS (PARTITION BY customer_key ORDER BY hr_idx
                 RANGE BETWEEN 167 PRECEDING AND CURRENT ROW)
)
WHERE win_cnt >= 3
GROUP BY customer_key, date_key;

-- ---------------------------------------------------------------------- R007
-- Dormant account reactivation: 60+ days of inactivity followed by >= 25,000
-- of throughput within seven days. LAG supplies the previous activity date.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R007-' || customer_key || '-' || date_key,
    'R007', customer_key, date_key, MAX(vol_7d), NULL,
    'reactivated after ' || MAX(gap_days) || ' dormant days'
FROM (
    SELECT
        customer_key,
        date_key,
        date_key - LAG(date_key) OVER (PARTITION BY account_key ORDER BY date_key) AS gap_days,
        SUM(day_vol) OVER (PARTITION BY account_key ORDER BY date_key
                           RANGE BETWEEN CURRENT ROW AND 6 FOLLOWING) AS vol_7d
    FROM (
        SELECT account_key, customer_key, date_key, SUM(amount_cad) AS day_vol
        FROM fact_transaction
        GROUP BY account_key, customer_key, date_key
    )
)
WHERE gap_days >= 60
  AND vol_7d >= 25000
GROUP BY customer_key, date_key;

-- ---------------------------------------------------------------------- R008
-- Sustained near-threshold cash: 5+ band deposits within a rolling 30 days.
-- Deliberately overlaps R001. The overlap is measured, not assumed away --
-- see outputs/rule_overlap.csv.
INSERT INTO fact_alert (alert_id, rule_id, customer_key, date_key, trigger_amount, trigger_txn_count, detail)
SELECT
    'R008-' || customer_key || '-' || date_key,
    'R008', customer_key, date_key, MAX(win_amt), MAX(win_cnt),
    'band cash credits in 30d: ' || MAX(win_cnt)
FROM (
    SELECT
        customer_key, date_key,
        COUNT(*)        OVER w AS win_cnt,
        SUM(amount_cad) OVER w AS win_amt
    FROM (
        SELECT customer_key, date_key, amount_cad
        FROM fact_transaction
        WHERE channel = 'cash' AND direction = 'credit'
          AND amount_cad BETWEEN 8000 AND 9999.99
    )
    WINDOW w AS (PARTITION BY customer_key ORDER BY date_key
                 RANGE BETWEEN 29 PRECEDING AND CURRENT ROW)
)
WHERE win_cnt >= 5
GROUP BY customer_key, date_key;
