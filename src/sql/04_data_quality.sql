-- =============================================================================
-- 04_data_quality.sql — control framework
--
-- Runs after load. Every check writes a PASS/FAIL row to dq_result, which feeds
-- the DQ scorecard page of the dashboard. Blocking checks fail the pipeline
-- (enforced in build_warehouse.py); warnings are surfaced but do not stop the run.
--
-- The distinction matters: an unreconciled control total is a stop-the-line
-- event in a regulated environment. An elevated share of unknown counterparty
-- jurisdictions is a data-sourcing problem to chase, not a reason to withhold
-- the day's monitoring run.
-- =============================================================================

DELETE FROM dq_result;

-- ---- RECONCILIATION -------------------------------------------------------
-- DQ01: row count. staging = loaded + rejected + deduplicated.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ01', 'Row count reconciliation: staging = fact + rejected + duplicates',
       'reconciliation', 'blocking',
       ABS(
         (SELECT COUNT(*) FROM stg_transactions)
         - (SELECT COUNT(*) FROM fact_transaction)
         - (SELECT COUNT(DISTINCT txn_id) FROM rejected_transaction)
         - (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM stg_transactions)
       ),
       0,
       CASE WHEN ABS(
         (SELECT COUNT(*) FROM stg_transactions)
         - (SELECT COUNT(*) FROM fact_transaction)
         - (SELECT COUNT(DISTINCT txn_id) FROM rejected_transaction)
         - (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM stg_transactions)
       ) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- DQ02: control total on accepted transaction value.
INSERT INTO dq_result
WITH accepted_staging AS (
    SELECT s.txn_id, s.amount_cad
    FROM stg_txn_dedup s
    JOIN dim_account  a ON a.account_id  = s.account_id
    JOIN dim_customer c ON c.customer_id = s.customer_id
    WHERE s.amount_cad > 0
)
SELECT DATETIME('now'), 'DQ02', 'Control total reconciliation on accepted transaction value',
       'reconciliation', 'blocking',
       ROUND(ABS((SELECT SUM(amount_cad) FROM fact_transaction)
                 - (SELECT SUM(amount_cad) FROM accepted_staging)), 2),
       0.01,
       CASE WHEN ABS((SELECT SUM(amount_cad) FROM fact_transaction)
                     - (SELECT SUM(amount_cad) FROM accepted_staging)) < 0.01
            THEN 'PASS' ELSE 'FAIL' END;

-- ---- UNIQUENESS -----------------------------------------------------------
-- DQ03: no duplicate transaction ids survived into the fact table.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ03', 'Transaction id uniqueness in fact_transaction',
       'uniqueness', 'blocking',
       (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM fact_transaction), 0,
       CASE WHEN (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM fact_transaction) = 0
            THEN 'PASS' ELSE 'FAIL' END;

-- DQ04: duplicates were actually detected upstream (guards against a silent
-- change in the source feed that stops emitting the defect we clean for).
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ04', 'Duplicate records detected and removed at load',
       'uniqueness', 'warning',
       (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM stg_transactions), 1,
       CASE WHEN (SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM stg_transactions) > 0
            THEN 'PASS' ELSE 'FAIL' END;

-- ---- INTEGRITY ------------------------------------------------------------
-- DQ05: every fact row resolves to a real account.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ05', 'Referential integrity: fact_transaction to dim_account',
       'integrity', 'blocking',
       (SELECT COUNT(*) FROM fact_transaction f
        LEFT JOIN dim_account a ON a.account_key = f.account_key
        WHERE a.account_key IS NULL), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_transaction f
                  LEFT JOIN dim_account a ON a.account_key = f.account_key
                  WHERE a.account_key IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- DQ06: every fact row resolves to a real customer.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ06', 'Referential integrity: fact_transaction to dim_customer',
       'integrity', 'blocking',
       (SELECT COUNT(*) FROM fact_transaction f
        LEFT JOIN dim_customer c ON c.customer_key = f.customer_key
        WHERE c.customer_key IS NULL), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_transaction f
                  LEFT JOIN dim_customer c ON c.customer_key = f.customer_key
                  WHERE c.customer_key IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- DQ07: every alert resolves to a customer that exists.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ07', 'Referential integrity: fact_alert to dim_customer',
       'integrity', 'blocking',
       (SELECT COUNT(*) FROM fact_alert a
        LEFT JOIN dim_customer c ON c.customer_key = a.customer_key
        WHERE c.customer_key IS NULL), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_alert a
                  LEFT JOIN dim_customer c ON c.customer_key = a.customer_key
                  WHERE c.customer_key IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- ---- VALIDITY -------------------------------------------------------------
-- DQ08: no non-positive amounts in the fact table.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ08', 'Transaction amount is strictly positive',
       'validity', 'blocking',
       (SELECT COUNT(*) FROM fact_transaction WHERE amount_cad <= 0), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_transaction WHERE amount_cad <= 0) = 0
            THEN 'PASS' ELSE 'FAIL' END;

-- DQ09: direction is a known domain value.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ09', 'Direction conforms to domain (credit/debit)',
       'validity', 'blocking',
       (SELECT COUNT(*) FROM fact_transaction WHERE direction NOT IN ('credit','debit')), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_transaction
                  WHERE direction NOT IN ('credit','debit')) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- DQ10: counterparty country resolves to the country dimension.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ10', 'Counterparty country conforms to dim_country',
       'validity', 'blocking',
       (SELECT COUNT(*) FROM fact_transaction f
        LEFT JOIN dim_country c ON c.country_code = f.counterparty_country
        WHERE c.country_code IS NULL), 0,
       CASE WHEN (SELECT COUNT(*) FROM fact_transaction f
                  LEFT JOIN dim_country c ON c.country_code = f.counterparty_country
                  WHERE c.country_code IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- ---- COMPLETENESS ---------------------------------------------------------
-- DQ11: share of wires with unknown counterparty jurisdiction. Warning only --
-- this is a sourcing gap to chase with the upstream system owner, not a reason
-- to halt the monitoring run.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ11', 'Share of wire transactions with unknown counterparty jurisdiction',
       'completeness', 'warning',
       ROUND(100.0 * SUM(CASE WHEN counterparty_country='UNK' THEN 1 ELSE 0 END)
             / NULLIF(COUNT(*), 0), 4),
       5.0,
       CASE WHEN 100.0 * SUM(CASE WHEN counterparty_country='UNK' THEN 1 ELSE 0 END)
                 / NULLIF(COUNT(*), 0) <= 5.0 THEN 'PASS' ELSE 'FAIL' END
FROM fact_transaction WHERE channel = 'wire';

-- DQ12: calendar completeness -- every day in range has at least one record.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ12', 'Calendar completeness: no missing activity days',
       'completeness', 'warning',
       (SELECT MAX(date_key) - MIN(date_key) + 1 - COUNT(DISTINCT date_key) FROM fact_transaction), 0,
       CASE WHEN (SELECT MAX(date_key) - MIN(date_key) + 1 - COUNT(DISTINCT date_key)
                  FROM fact_transaction) = 0 THEN 'PASS' ELSE 'FAIL' END;

-- DQ13: customer master completeness.
INSERT INTO dq_result
SELECT DATETIME('now'), 'DQ13', 'Customer master row count reconciliation',
       'reconciliation', 'blocking',
       ABS((SELECT COUNT(*) FROM stg_customers) - (SELECT COUNT(*) FROM dim_customer)), 0,
       CASE WHEN (SELECT COUNT(*) FROM stg_customers) = (SELECT COUNT(*) FROM dim_customer)
            THEN 'PASS' ELSE 'FAIL' END;
