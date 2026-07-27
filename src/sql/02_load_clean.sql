-- =============================================================================
-- 02_load_clean.sql — staging -> conformed star schema
--
-- Every cleaning decision below corresponds to a defect deliberately injected
-- by the generator (see generate_data.degrade). Rejected rows are quarantined
-- in rejected_transaction rather than silently dropped, because "where did my
-- 40 rows go" is the question that ends careers in a regulated environment.
-- =============================================================================

-- ------------------------------------------------------------- dim_customer
INSERT INTO dim_customer (
    customer_key, customer_id, customer_segment, occupation_or_business_type,
    is_cash_intensive_business, tenure_months, tenure_band, kyc_risk_rating,
    is_pep, declared_annual_income_cad, home_province, onboarding_channel)
SELECT
    ROW_NUMBER() OVER (ORDER BY customer_id),
    customer_id,
    customer_segment,
    occupation_or_business_type,
    CASE WHEN occupation_or_business_type
              IN ('restaurant','convenience_retail','salon','auto_dealer')
         THEN 1 ELSE 0 END,
    tenure_months,
    CASE WHEN tenure_months <  12 THEN '00-11m'
         WHEN tenure_months <  36 THEN '12-35m'
         WHEN tenure_months <  84 THEN '36-83m'
         ELSE '84m+' END,
    kyc_risk_rating,
    CASE WHEN LOWER(CAST(is_pep AS TEXT)) IN ('true','1') THEN 1 ELSE 0 END,
    declared_annual_income_cad,
    home_province,
    onboarding_channel
FROM stg_customers;

-- -------------------------------------------------------------- dim_account
INSERT INTO dim_account (account_key, account_id, customer_key, account_type, opened_days_ago)
SELECT
    ROW_NUMBER() OVER (ORDER BY a.account_id),
    a.account_id,
    c.customer_key,
    a.account_type,
    a.opened_days_ago
FROM stg_accounts a
JOIN dim_customer c ON c.customer_id = a.customer_id;

-- ----------------------------------------------------------------- dim_date
INSERT INTO dim_date (date_key, calendar_date, year, month, day_of_week, is_weekend, month_name)
SELECT DISTINCT
    day_index,
    DATE(txn_date),
    CAST(STRFTIME('%Y', txn_date) AS INTEGER),
    CAST(STRFTIME('%m', txn_date) AS INTEGER),
    CAST(STRFTIME('%w', txn_date) AS INTEGER),
    CASE WHEN STRFTIME('%w', txn_date) IN ('0','6') THEN 1 ELSE 0 END,
    CASE CAST(STRFTIME('%m', txn_date) AS INTEGER)
         WHEN 1 THEN 'Jan' WHEN 2 THEN 'Feb' WHEN 3 THEN 'Mar' WHEN 4 THEN 'Apr'
         WHEN 5 THEN 'May' WHEN 6 THEN 'Jun' WHEN 7 THEN 'Jul' WHEN 8 THEN 'Aug'
         WHEN 9 THEN 'Sep' WHEN 10 THEN 'Oct' WHEN 11 THEN 'Nov' ELSE 'Dec' END
FROM stg_transactions
WHERE day_index IS NOT NULL;

-- -------------------------------------------------------------- dim_country
-- 'UNK' is an explicit member, not a NULL. Unknown counterparty jurisdiction on
-- a wire is itself a risk signal and must be countable, so it gets a key.
INSERT INTO dim_country (country_code, risk_tier, is_high_risk)
VALUES
    ('CA','domestic',0),('US','standard',0),('GB','standard',0),('FR','standard',0),
    ('DE','standard',0),('AU','standard',0),('JP','standard',0),('NL','standard',0),
    ('CH','standard',0),
    ('XA','high_risk',1),('XB','high_risk',1),('XC','high_risk',1),('XD','high_risk',1),
    ('UNK','standard',0);

-- ------------------------------------------------------- deduplicated source
-- ORDER OF OPERATIONS MATTERS: deduplicate FIRST, then validate. Validating
-- before deduplication lets a single natural key be both loaded (via its clean
-- copy) and quarantined (via its corrupted copy), which makes the row-count
-- reconciliation unbalanceable. This exact defect was caught by DQ01 during
-- development -- see docs/WHAT_DIDNT_WORK.md.
DROP VIEW IF EXISTS stg_txn_dedup;
CREATE VIEW stg_txn_dedup AS
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY txn_id ORDER BY ROWID) AS rn
    FROM stg_transactions
) WHERE rn = 1;

-- --------------------------------------------------- quarantine bad records
-- Defect 5: negative amounts (mis-signed reversals upstream).
INSERT INTO rejected_transaction (txn_id, reject_reason, raw_payload)
SELECT txn_id, 'negative_amount',
       'amount_cad=' || CAST(amount_cad AS TEXT)
FROM stg_txn_dedup WHERE amount_cad <= 0;

-- Defect 4: orphan account_ids that resolve to no account master record.
INSERT INTO rejected_transaction (txn_id, reject_reason, raw_payload)
SELECT s.txn_id, 'orphan_account_id', 'account_id=' || s.account_id
FROM stg_txn_dedup s
LEFT JOIN dim_account a ON a.account_id = s.account_id
WHERE a.account_key IS NULL
  AND s.amount_cad > 0;   -- avoid double-counting a row already quarantined

-- --------------------------------------------------------- fact_transaction
-- Defect 1 (duplicates): a replayed source feed emits byte-identical rows with
-- the same txn_id. Deduplicated by keeping the first occurrence per natural key.
-- Defect 2 (missing country) -> 'UNK'.  Defect 3 (casing/whitespace) -> normalised.
INSERT INTO fact_transaction (
    txn_id, date_key, account_key, customer_key, counterparty_country,
    counterparty_id, hour_of_day, amount_cad, direction, channel, signed_amount_cad)
SELECT
    s.txn_id,
    s.day_index,
    a.account_key,
    c.customer_key,
    COALESCE(dc.country_code, 'UNK'),
    s.counterparty_id,
    s.hour,
    s.amount_cad,
    s.direction,
    s.channel,
    CASE WHEN s.direction = 'credit' THEN s.amount_cad ELSE -s.amount_cad END
FROM stg_txn_dedup s
JOIN dim_account  a ON a.account_id  = s.account_id
JOIN dim_customer c ON c.customer_id = s.customer_id
LEFT JOIN dim_country dc
       ON dc.country_code = UPPER(TRIM(COALESCE(s.counterparty_country, 'UNK')))
WHERE s.amount_cad > 0;

-- ----------------------------------------------------------------- dim_rule
INSERT INTO dim_rule (rule_id, rule_name, typology_target, rule_family, severity, rule_description) VALUES
('R001','Near-threshold cash clustering','structuring','threshold','high',
 'Three or more cash credits in the 8,000-9,999 band by one customer within a rolling 48-hour window.'),
('R002','LCTR aggregation avoidance','structuring','threshold','high',
 'Cash credits summing to 10,000 or more within 24 hours where no single transaction reaches the reporting threshold.'),
('R003','Rapid movement of funds','rapid_movement','flow','high',
 'Inbound credit of 15,000 or more where 80 percent or more exits the account within 72 hours.'),
('R004','High-risk jurisdiction wire','layering','geography','medium',
 'Wire transfer of 5,000 or more to or from a jurisdiction on the high-risk list.'),
('R005','Volume velocity spike','general','behavioural','medium',
 'Rolling 30-day credit volume exceeds three times the customer prior 90-day daily average.'),
('R006','Round-value transfer clustering','layering','pattern','medium',
 'Three or more round-thousand wire or e-transfer movements of 5,000 or more within seven days.'),
('R007','Dormant account reactivation','general','behavioural','low',
 'Account with no activity for 60 or more days followed by 25,000 or more of throughput within seven days.'),
('R008','Sustained near-threshold cash','structuring','threshold','medium',
 'Five or more cash credits in the 8,000-9,999 band by one customer within a rolling 30-day window.');
