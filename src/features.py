"""
Feature engineering for alert triage.

POINT-IN-TIME DISCIPLINE IS THE WHOLE GAME HERE.
Every feature attached to an alert dated D is computed from transactions and
alerts occurring on or before D. An investigator working alert D cannot see
what happens on D+1, so neither can the model. Getting this wrong produces a
model that scores beautifully offline and fails completely in production, and
it is the first thing a competent reviewer will probe.

The ground-truth files (data/_ground_truth_*.csv) are read ONLY by
build_labels(). They are never joined into the feature frame.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

WINDOWS = [7, 30, 90]


# ---------------------------------------------------------------------------

def load_transactions(conn: sqlite3.Connection) -> pd.DataFrame:
    q = """
    SELECT f.customer_key, f.date_key, f.amount_cad, f.direction, f.channel,
           f.hour_of_day, f.counterparty_id, f.counterparty_country,
           c.is_high_risk
    FROM fact_transaction f
    JOIN dim_country c ON c.country_code = f.counterparty_country
    """
    return pd.read_sql(q, conn)


def daily_aggregates(txn: pd.DataFrame) -> pd.DataFrame:
    """Collapse transactions to one row per customer-day with the measures the
    rolling windows are built from."""
    t = txn
    credit = t["direction"].eq("credit")
    cash = t["channel"].eq("cash")
    wire = t["channel"].eq("wire")
    amt = t["amount_cad"]

    d = pd.DataFrame({
        "customer_key": t["customer_key"],
        "date_key": t["date_key"],
        "n_txn": 1,
        "vol_total": amt,
        "vol_credit": amt.where(credit, 0.0),
        "vol_debit": amt.where(~credit, 0.0),
        "n_cash_credit": (cash & credit).astype(int),
        "vol_cash_credit": amt.where(cash & credit, 0.0),
        "n_wire": wire.astype(int),
        "vol_wire": amt.where(wire, 0.0),
        "n_highrisk": t["is_high_risk"].astype(int),
        "vol_highrisk": amt.where(t["is_high_risk"].eq(1), 0.0),
        # Deposits parked in the near-threshold band.
        "n_band_cash": (cash & credit & amt.between(8000, 9999.99)).astype(int),
        # Round-thousand values of material size.
        "n_round": ((amt % 1000 == 0) & (amt >= 5000)).astype(int),
        # Activity outside normal banking hours.
        "n_offhours": (~t["hour_of_day"].between(8, 18)).astype(int),
        "max_amt": amt,
    })

    agg = d.groupby(["customer_key", "date_key"], as_index=False).agg(
        n_txn=("n_txn", "sum"),
        vol_total=("vol_total", "sum"),
        vol_credit=("vol_credit", "sum"),
        vol_debit=("vol_debit", "sum"),
        n_cash_credit=("n_cash_credit", "sum"),
        vol_cash_credit=("vol_cash_credit", "sum"),
        n_wire=("n_wire", "sum"),
        vol_wire=("vol_wire", "sum"),
        n_highrisk=("n_highrisk", "sum"),
        vol_highrisk=("vol_highrisk", "sum"),
        n_band_cash=("n_band_cash", "sum"),
        n_round=("n_round", "sum"),
        n_offhours=("n_offhours", "sum"),
        max_amt=("max_amt", "max"),
    )

    # Distinct counterparties per customer-day (separate pass; nunique is costly).
    cp = (txn.groupby(["customer_key", "date_key"])["counterparty_id"]
             .nunique().rename("n_counterparties").reset_index())
    return agg.merge(cp, on=["customer_key", "date_key"], how="left")


def rolling_features(daily: pd.DataFrame, n_days: int) -> pd.DataFrame:
    """Expand to a dense customer x day grid and compute trailing-window sums.

    A dense grid matters: a customer with no activity for 20 days must have
    those 20 zero-days counted in the denominator, otherwise 'volume in the
    last 30 days' silently becomes 'volume in the last 30 ACTIVE days', which
    is a different and much less useful quantity.
    """
    customers = daily["customer_key"].unique()
    grid = pd.MultiIndex.from_product(
        [customers, range(0, n_days)], names=["customer_key", "date_key"]
    ).to_frame(index=False)

    dense = grid.merge(daily, on=["customer_key", "date_key"], how="left").fillna(0.0)
    dense = dense.sort_values(["customer_key", "date_key"])

    measures = [c for c in daily.columns if c not in ("customer_key", "date_key")]
    out = [dense[["customer_key", "date_key"]].reset_index(drop=True)]

    g = dense.groupby("customer_key")
    for w in WINDOWS:
        # closed='right' with min_periods=1: the trailing window INCLUDES the
        # current day and never reaches forward.
        r = g[measures].rolling(window=w, min_periods=1).sum().reset_index(drop=True)
        r.columns = [f"{c}_{w}d" for c in measures]
        out.append(r)

    # Active-day counts and a 90-day baseline for velocity comparison.
    active = (dense["n_txn"] > 0).astype(int)
    dense = dense.assign(_active=active)
    act = (dense.groupby("customer_key")["_active"]
                .rolling(window=90, min_periods=1).sum().reset_index(drop=True)
                .rename("active_days_90d"))
    out.append(act)

    return pd.concat(out, axis=1)


def alert_context(conn: sqlite3.Connection) -> pd.DataFrame:
    """Alert-level context an investigator would genuinely have on the day."""
    alerts = pd.read_sql("SELECT * FROM fact_alert", conn)

    # How many distinct rules fired for this customer on this date. A customer
    # tripping four rules at once is a materially different proposition from one
    # tripping a single rule, and the triage queue should know that.
    same_day = (alerts.groupby(["customer_key", "date_key"])["rule_id"]
                      .nunique().rename("n_rules_same_day").reset_index())

    # Prior alert history, STRICTLY before the current date.
    a = alerts[["customer_key", "date_key"]].drop_duplicates().sort_values(
        ["customer_key", "date_key"])
    counts = (alerts.groupby(["customer_key", "date_key"])
                    .size().rename("n").reset_index()
                    .sort_values(["customer_key", "date_key"]))
    counts["prior_alerts"] = (counts.groupby("customer_key")["n"]
                                    .cumsum() - counts["n"])
    counts["days_since_prior_alert"] = (
        counts["date_key"] - counts.groupby("customer_key")["date_key"].shift(1))
    counts["days_since_prior_alert"] = counts["days_since_prior_alert"].fillna(999)

    ctx = (alerts
           .merge(same_day, on=["customer_key", "date_key"], how="left")
           .merge(counts[["customer_key", "date_key", "prior_alerts",
                          "days_since_prior_alert"]],
                  on=["customer_key", "date_key"], how="left"))
    return ctx


def build_feature_frame(db_path: Path, n_days: int = 180) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    txn = load_transactions(conn)
    daily = daily_aggregates(txn)
    roll = rolling_features(daily, n_days)
    ctx = alert_context(conn)
    cust = pd.read_sql("SELECT * FROM dim_customer", conn)
    acc = pd.read_sql(
        "SELECT customer_key, COUNT(*) n_accounts, MIN(opened_days_ago) newest_account_age "
        "FROM dim_account GROUP BY customer_key", conn)
    rules = pd.read_sql("SELECT rule_id, rule_family, severity FROM dim_rule", conn)
    conn.close()

    df = (ctx
          .merge(roll, on=["customer_key", "date_key"], how="left")
          .merge(cust, on="customer_key", how="left")
          .merge(acc, on="customer_key", how="left")
          .merge(rules, on="rule_id", how="left"))

    # ---- derived ratios -------------------------------------------------
    eps = 1e-9
    df["monthly_declared"] = df["declared_annual_income_cad"] / 12.0

    # Volume relative to what the customer told us they earn. A restaurant
    # moving 3x its declared revenue is interesting; a restaurant moving 0.9x
    # is a restaurant.
    df["vol_to_declared_30d"] = df["vol_credit_30d"] / (df["monthly_declared"] + eps)
    df["vol_to_declared_90d"] = df["vol_credit_90d"] / (3 * df["monthly_declared"] + eps)

    df["cash_share_30d"] = df["vol_cash_credit_30d"] / (df["vol_credit_30d"] + eps)
    df["cash_share_90d"] = df["vol_cash_credit_90d"] / (df["vol_credit_90d"] + eps)
    df["highrisk_share_30d"] = df["vol_highrisk_30d"] / (df["vol_total_30d"] + eps)
    df["wire_share_30d"] = df["vol_wire_30d"] / (df["vol_total_30d"] + eps)
    df["offhours_share_30d"] = df["n_offhours_30d"] / (df["n_txn_30d"] + eps)
    df["round_share_30d"] = df["n_round_30d"] / (df["n_txn_30d"] + eps)
    df["band_share_30d"] = df["n_band_cash_30d"] / (df["n_cash_credit_30d"] + eps)

    # Retention ratio: how much of what came in stayed. Near zero means the
    # account is a conduit -- which is also what a payroll account looks like,
    # hence it is a feature and not a rule.
    df["retention_30d"] = ((df["vol_credit_30d"] - df["vol_debit_30d"])
                           / (df["vol_credit_30d"] + eps))
    df["retention_90d"] = ((df["vol_credit_90d"] - df["vol_debit_90d"])
                           / (df["vol_credit_90d"] + eps))

    # Acceleration: recent 30 days against the 90-day run rate.
    df["velocity_ratio"] = (df["vol_credit_30d"] / ((df["vol_credit_90d"] / 3) + eps))
    df["txn_velocity_ratio"] = (df["n_txn_30d"] / ((df["n_txn_90d"] / 3) + eps))

    # Counterparty concentration: few counterparties moving a lot is a
    # different shape from many counterparties moving a little.
    df["vol_per_counterparty_30d"] = (df["vol_total_30d"]
                                      / (df["n_counterparties_30d"] + eps))
    df["txn_per_active_day"] = df["n_txn_90d"] / (df["active_days_90d"] + eps)

    df["is_pep"] = df["is_pep"].astype(int)
    df["is_cash_intensive_business"] = df["is_cash_intensive_business"].astype(int)

    return df


def build_labels(feature_df: pd.DataFrame, data_dir: Path) -> pd.Series:
    """Attach ground truth. An alert is a TRUE POSITIVE if it belongs to a
    customer conducting illicit activity AND falls within their campaign window
    (with a 7-day grace either side, reflecting that an investigation opened
    slightly outside the window would still find the activity).

    Alerts on illicit customers OUTSIDE their campaign window are labelled
    negative: at that point in time there was nothing to find, so treating them
    as positives would teach the model to score the customer rather than the
    behaviour.
    """
    gt = pd.read_csv(data_dir / "_ground_truth_customers.csv")
    import sqlite3
    conn = sqlite3.connect(data_dir / "aml.db")
    keys = pd.read_sql("SELECT customer_key, customer_id FROM dim_customer", conn)
    conn.close()

    gt = gt.merge(keys, on="customer_id", how="left")
    m = feature_df[["customer_key", "date_key"]].merge(
        gt[["customer_key", "campaign_start_day", "campaign_end_day"]],
        on="customer_key", how="left")

    label = (m["campaign_start_day"].notna()
             & (m["date_key"] >= m["campaign_start_day"] - 7)
             & (m["date_key"] <= m["campaign_end_day"] + 7))
    return label.astype(int).rename("is_true_positive")


FEATURE_BLOCKLIST = {
    # Identifiers and anything that could carry the answer.
    "alert_id", "customer_key", "date_key", "customer_id", "detail",
    "typology", "campaign_start_day", "campaign_end_day",
    "is_true_positive", "monthly_declared",
}

CATEGORICAL = [
    "rule_id", "rule_family", "severity", "customer_segment",
    "occupation_or_business_type", "kyc_risk_rating", "home_province",
    "onboarding_channel", "tenure_band",
]


def feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    cols = [c for c in df.columns if c not in FEATURE_BLOCKLIST]
    X = df[cols].copy()
    for c in CATEGORICAL:
        if c in X.columns:
            X[c] = X[c].astype("category")
    num = X.select_dtypes(include=[np.number]).columns
    X[num] = X[num].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return X, list(X.columns)


if __name__ == "__main__":
    df = build_feature_frame(ROOT / "data" / "aml.db")
    y = build_labels(df, ROOT / "data")
    print(f"alerts: {len(df):,}  features: {df.shape[1]}  positives: {y.sum():,} "
          f"({y.mean() * 100:.2f}%)")
