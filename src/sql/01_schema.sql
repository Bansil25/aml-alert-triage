-- =============================================================================
-- 01_schema.sql — dimensional model
--
-- GRAIN DECLARATIONS (the single most important thing in this file):
--   fact_transaction : one row per settled transaction line
--   fact_alert       : one row per (rule, customer, alert_date)
--   dim_*            : one row per business entity, SCD Type 1
--
-- SCD note: dimensions are Type 1 (overwrite) because the source simulation
-- emits a single snapshot. In production, dim_customer would be Type 2 on
-- kyc_risk_rating and address, since an alert must be evaluated against the
-- risk rating that was in force on the alert date, not today's rating.
-- That is a correctness issue, not a nicety, and is called out in
-- docs/DATA_MODEL.md under "known simplifications".
-- =============================================================================

DROP TABLE IF EXISTS fact_alert;
DROP TABLE IF EXISTS fact_transaction;
DROP TABLE IF EXISTS dim_customer;
DROP TABLE IF EXISTS dim_account;
DROP TABLE IF EXISTS dim_date;
DROP TABLE IF EXISTS dim_rule;
DROP TABLE IF EXISTS dim_country;
DROP TABLE IF EXISTS dq_result;
DROP TABLE IF EXISTS rejected_transaction;

-- ---------------------------------------------------------------- dimensions

CREATE TABLE dim_customer (
    customer_key                INTEGER PRIMARY KEY,
    customer_id                 TEXT NOT NULL UNIQUE,
    customer_segment            TEXT NOT NULL,      -- personal | business
    occupation_or_business_type TEXT NOT NULL,
    is_cash_intensive_business  INTEGER NOT NULL,   -- legitimising context
    tenure_months               INTEGER NOT NULL,
    tenure_band                 TEXT NOT NULL,
    kyc_risk_rating             TEXT NOT NULL,      -- low | medium | high
    is_pep                      INTEGER NOT NULL,
    declared_annual_income_cad  REAL NOT NULL,
    home_province               TEXT NOT NULL,
    onboarding_channel          TEXT NOT NULL
);

CREATE TABLE dim_account (
    account_key      INTEGER PRIMARY KEY,
    account_id       TEXT NOT NULL UNIQUE,
    customer_key     INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    account_type     TEXT NOT NULL,
    opened_days_ago  INTEGER NOT NULL
);

CREATE TABLE dim_date (
    date_key      INTEGER PRIMARY KEY,   -- day_index from simulation start
    calendar_date TEXT NOT NULL UNIQUE,
    year          INTEGER NOT NULL,
    month         INTEGER NOT NULL,
    day_of_week   INTEGER NOT NULL,
    is_weekend    INTEGER NOT NULL,
    month_name    TEXT NOT NULL
);

CREATE TABLE dim_country (
    country_code   TEXT PRIMARY KEY,
    risk_tier      TEXT NOT NULL,        -- domestic | standard | high_risk
    is_high_risk   INTEGER NOT NULL
);

CREATE TABLE dim_rule (
    rule_id          TEXT PRIMARY KEY,
    rule_name        TEXT NOT NULL,
    typology_target  TEXT NOT NULL,
    rule_family      TEXT NOT NULL,
    severity         TEXT NOT NULL,
    rule_description TEXT NOT NULL
);

-- --------------------------------------------------------------------- facts

CREATE TABLE fact_transaction (
    txn_id               TEXT PRIMARY KEY,
    date_key             INTEGER NOT NULL REFERENCES dim_date(date_key),
    account_key          INTEGER NOT NULL REFERENCES dim_account(account_key),
    customer_key         INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    counterparty_country TEXT NOT NULL REFERENCES dim_country(country_code),
    counterparty_id      TEXT NOT NULL,
    hour_of_day          INTEGER NOT NULL,
    amount_cad           REAL NOT NULL,
    direction            TEXT NOT NULL,   -- credit | debit
    channel              TEXT NOT NULL,
    signed_amount_cad    REAL NOT NULL    -- +credit / -debit, for net-flow measures
);

CREATE TABLE fact_alert (
    alert_id          TEXT PRIMARY KEY,
    rule_id           TEXT NOT NULL REFERENCES dim_rule(rule_id),
    customer_key      INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    date_key          INTEGER NOT NULL REFERENCES dim_date(date_key),
    trigger_amount    REAL,
    trigger_txn_count INTEGER,
    detail            TEXT
);

-- ------------------------------------------------------- control / exceptions

CREATE TABLE dq_result (
    run_ts        TEXT NOT NULL,
    check_id      TEXT NOT NULL,
    check_name    TEXT NOT NULL,
    dimension     TEXT NOT NULL,   -- completeness | validity | uniqueness | integrity | reconciliation
    severity      TEXT NOT NULL,   -- blocking | warning
    metric_value  REAL,
    threshold     REAL,
    status        TEXT NOT NULL    -- PASS | FAIL
);

CREATE TABLE rejected_transaction (
    txn_id        TEXT,
    reject_reason TEXT NOT NULL,
    raw_payload   TEXT
);

CREATE INDEX idx_ft_cust_date ON fact_transaction(customer_key, date_key);
CREATE INDEX idx_ft_date      ON fact_transaction(date_key);
CREATE INDEX idx_ft_chan      ON fact_transaction(channel, direction);
CREATE INDEX idx_fa_cust_date ON fact_alert(customer_key, date_key);
