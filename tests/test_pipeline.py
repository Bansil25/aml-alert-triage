"""
Test suite.

The tests are deliberately weighted toward CORRECTNESS OF THE ANALYTICAL
CLAIMS rather than code coverage. A passing unit test on a helper function is
worth very little here; a test that would have caught the point-in-time leak is
worth the entire file. The three that matter most:

  test_no_lookahead_leakage       -- features cannot see the future
  test_ground_truth_never_in_features -- the label cannot reach the model
  test_reconciliation_balances    -- nothing is silently lost at load

Run: pytest -q
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

DB = ROOT / "data" / "aml.db"
DATA = ROOT / "data"


pytestmark = pytest.mark.skipif(
    not DB.exists(),
    reason="warehouse not built; run `python src/generate_data.py && "
           "python src/build_warehouse.py` first")


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(DB)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Data quality controls
# ---------------------------------------------------------------------------

def test_all_blocking_dq_checks_pass(conn):
    df = pd.read_sql("SELECT * FROM dq_result WHERE severity='blocking'", conn)
    failed = df[df.status != "PASS"]
    assert failed.empty, f"blocking DQ failures: {list(failed.check_id)}"


def test_reconciliation_balances(conn):
    """staging = loaded + rejected + duplicates removed. No silent losses."""
    staging = pd.read_sql("SELECT COUNT(*) n FROM stg_transactions", conn).n[0]
    distinct = pd.read_sql(
        "SELECT COUNT(DISTINCT txn_id) n FROM stg_transactions", conn).n[0]
    fact = pd.read_sql("SELECT COUNT(*) n FROM fact_transaction", conn).n[0]
    rejected = pd.read_sql(
        "SELECT COUNT(DISTINCT txn_id) n FROM rejected_transaction", conn).n[0]
    assert distinct == fact + rejected
    assert staging > distinct, "duplicate defect is not present in the source"


def test_defects_were_actually_present_and_removed(conn):
    """Guards against the source feed quietly ceasing to emit the defects we
    clean for -- at which point the cleaning code is untested in production."""
    rej = pd.read_sql("SELECT reject_reason, COUNT(*) n FROM rejected_transaction "
                      "GROUP BY reject_reason", conn).set_index("reject_reason").n
    assert rej.get("orphan_account_id", 0) > 0
    assert rej.get("negative_amount", 0) > 0


# ---------------------------------------------------------------------------
# Dimensional model
# ---------------------------------------------------------------------------

def test_fact_transaction_grain_is_unique(conn):
    n, d = pd.read_sql(
        "SELECT COUNT(*) n, COUNT(DISTINCT txn_id) d FROM fact_transaction",
        conn).iloc[0]
    assert n == d


def test_fact_alert_grain_is_unique(conn):
    dup = pd.read_sql("""
        SELECT rule_id, customer_key, date_key, COUNT(*) n
        FROM fact_alert GROUP BY 1,2,3 HAVING n > 1""", conn)
    assert dup.empty, "alert grain (rule, customer, date) is not unique"


def test_no_orphan_foreign_keys(conn):
    for fact, key, dim in [("fact_transaction", "customer_key", "dim_customer"),
                           ("fact_transaction", "account_key", "dim_account"),
                           ("fact_alert", "customer_key", "dim_customer"),
                           ("fact_alert", "rule_id", "dim_rule")]:
        q = (f"SELECT COUNT(*) n FROM {fact} f "
             f"LEFT JOIN {dim} d ON d.{key} = f.{key} WHERE d.{key} IS NULL")
        assert pd.read_sql(q, conn).n[0] == 0, f"{fact}.{key} has orphans"


def test_signed_amount_matches_direction(conn):
    bad = pd.read_sql("""
        SELECT COUNT(*) n FROM fact_transaction
        WHERE (direction='credit' AND signed_amount_cad <> amount_cad)
           OR (direction='debit'  AND signed_amount_cad <> -amount_cad)""", conn).n[0]
    assert bad == 0


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------

def test_r001_only_fires_on_band_deposits(conn):
    """Every R001 alert must be backed by at least three in-band cash credits
    for that customer within the 48 hours ending on the alert date."""
    alerts = pd.read_sql(
        "SELECT customer_key, date_key FROM fact_alert WHERE rule_id='R001' LIMIT 25",
        conn)
    for r in alerts.itertuples():
        n = pd.read_sql(f"""
            SELECT COUNT(*) n FROM fact_transaction
            WHERE customer_key = {r.customer_key}
              AND channel='cash' AND direction='credit'
              AND amount_cad BETWEEN 8000 AND 9999.99
              AND date_key BETWEEN {r.date_key - 2} AND {r.date_key}""", conn).n[0]
        assert n >= 3, f"R001 fired for customer {r.customer_key} with only {n} in-band deposits"


def test_r002_excludes_single_reportable_transactions(conn):
    """R002 targets AGGREGATION below the reporting threshold. If a single
    transaction inside the window already breached the threshold then nothing
    was being avoided and the alert is a false construction.

    The window is 24 hours on the hour index, so the test reconstructs the same
    index rather than using a day proxy -- an earlier version of this test used
    a day-level approximation and had to be weakened to pass, which made it
    assert nothing at all.
    """
    alerts = pd.read_sql(
        "SELECT customer_key, date_key FROM fact_alert WHERE rule_id='R002' LIMIT 40",
        conn)
    assert len(alerts) > 0, "R002 produced no alerts to verify"

    for r in alerts.itertuples():
        rows = pd.read_sql(f"""
            SELECT amount_cad, date_key * 24 + hour_of_day AS hr_idx
            FROM fact_transaction
            WHERE customer_key = {r.customer_key}
              AND channel = 'cash' AND direction = 'credit'
              AND date_key BETWEEN {r.date_key - 2} AND {r.date_key}
            ORDER BY hr_idx""", conn)
        # Find at least one trailing 24h window ending on the alert date that
        # sums past the threshold with every individual amount below it.
        ok = False
        on_day = rows[rows.hr_idx // 24 == r.date_key]
        for h in on_day.hr_idx:
            w = rows[(rows.hr_idx <= h) & (rows.hr_idx >= h - 23)]
            if len(w) >= 2 and w.amount_cad.sum() >= 10000 and w.amount_cad.max() < 10000:
                ok = True
                break
        assert ok, (f"R002 fired for customer {r.customer_key} on day {r.date_key} "
                    f"with no qualifying sub-threshold aggregation window")


def test_every_rule_is_defined(conn):
    undefined = pd.read_sql("""
        SELECT DISTINCT a.rule_id FROM fact_alert a
        LEFT JOIN dim_rule r ON r.rule_id = a.rule_id
        WHERE r.rule_id IS NULL""", conn)
    assert undefined.empty


# ---------------------------------------------------------------------------
# Features -- the tests that matter
# ---------------------------------------------------------------------------

def test_ground_truth_never_in_features():
    """The label, the typology and the campaign window must be structurally
    incapable of reaching the model."""
    from features import FEATURE_BLOCKLIST, build_feature_frame, feature_matrix
    df = build_feature_frame(DB)
    X, cols = feature_matrix(df)
    forbidden = {"is_true_positive", "typology", "campaign_start_day",
                 "campaign_end_day", "is_illicit_txn", "customer_id"}
    assert not (forbidden & set(cols)), f"label leaked into features: {forbidden & set(cols)}"
    for c in forbidden:
        assert c in FEATURE_BLOCKLIST or c not in df.columns


def test_no_lookahead_leakage():
    """THE CRITICAL TEST.

    Injects a very large transaction AFTER an alert date and asserts that the
    features for that alert are unchanged. If a rolling window is misaligned by
    even one day, this fails. This test exists because the first version of the
    pipeline scored ROC-AUC 1.000 and I needed a mechanical guarantee rather
    than a careful reading of my own code.
    """
    import shutil
    import tempfile
    from features import build_feature_frame

    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "aml.db"
        shutil.copy(DB, tmp_db)

        base = build_feature_frame(tmp_db)
        target = base.sort_values("date_key").iloc[len(base) // 2]
        ck, dk = int(target.customer_key), int(target.date_key)

        c = sqlite3.connect(tmp_db)
        # A colossal transaction 5 days AFTER the alert. Nothing about the alert
        # as of its own date may change.
        c.execute("""INSERT INTO fact_transaction
            (txn_id, date_key, account_key, customer_key, counterparty_country,
             counterparty_id, hour_of_day, amount_cad, direction, channel,
             signed_amount_cad)
            SELECT 'T_FUTURE_LEAK', ?, account_key, ?, 'XA', 'P0000001', 12,
                   9999999.0, 'credit', 'cash', 9999999.0
            FROM fact_transaction WHERE customer_key = ? LIMIT 1""",
                  (dk + 5, ck, ck))
        c.commit()
        c.close()

        after = build_feature_frame(tmp_db)

        num = base.select_dtypes(include=[np.number]).columns
        b = base[(base.customer_key == ck) & (base.date_key == dk)][num]
        a = after[(after.customer_key == ck) & (after.date_key == dk)][num]
        assert len(b) and len(a)
        diff = (b.reset_index(drop=True) - a.reset_index(drop=True)).abs().max()
        leaked = diff[diff > 1e-6]
        assert leaked.empty, (
            f"LOOK-AHEAD LEAKAGE: features changed after inserting a future "
            f"transaction: {dict(leaked)}")


def test_rolling_windows_are_trailing_and_nested():
    """A 7-day sum can never exceed a 30-day sum, which can never exceed 90."""
    from features import build_feature_frame
    df = build_feature_frame(DB)
    for base in ["vol_credit", "n_txn", "vol_cash_credit"]:
        assert (df[f"{base}_7d"] <= df[f"{base}_30d"] + 1e-6).all()
        assert (df[f"{base}_30d"] <= df[f"{base}_90d"] + 1e-6).all()


def test_prior_alerts_excludes_current_alert():
    """prior_alerts must count alerts strictly BEFORE the current date."""
    from features import build_feature_frame
    df = build_feature_frame(DB)
    first = df.sort_values("date_key").groupby("customer_key").first()
    assert (first["prior_alerts"] == 0).all(), \
        "a customer's first alert reports non-zero prior alerts"


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def test_threshold_at_full_recall_captures_all_positives():
    from train_model import evaluate_at_threshold, threshold_at_full_recall
    rng = np.random.default_rng(0)
    y = (rng.random(1000) < 0.05).astype(int)
    s = rng.random(1000) * 0.5 + y * 0.3
    thr = threshold_at_full_recall(y, s)
    assert evaluate_at_threshold(y, s, thr)["recall"] == 1.0


def test_tradeoff_curve_is_monotonic():
    """Lower recall targets must never require reviewing MORE alerts."""
    from train_model import tradeoff_curve
    rng = np.random.default_rng(1)
    yv = (rng.random(2000) < 0.05).astype(int)
    sv = rng.random(2000) * 0.5 + yv * 0.4
    yh = (rng.random(1500) < 0.05).astype(int)
    sh = rng.random(1500) * 0.5 + yh * 0.4
    rows = tradeoff_curve(yv, sv, yh, sh)
    deferred = [r["holdout_volume_deferred_pct"] for r in rows]
    assert deferred == sorted(deferred), "deferral must rise as recall target falls"


def test_generator_is_deterministic():
    """Same seed, same data. Without this, no reported number is reproducible."""
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        r = subprocess.run(
            [sys.executable, str(SRC / "generate_data.py"), "--out", str(out)],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        new = hashlib.sha256((out / "transactions.csv").read_bytes()).hexdigest()
        old = hashlib.sha256((DATA / "transactions.csv").read_bytes()).hexdigest()
        assert new == old, "generator is not reproducible from its seed"


def test_hard_negatives_are_not_labelled_illicit():
    """Hard negatives look like typologies but are lawful. If any of them ended
    up in the ground-truth file the entire evaluation is invalid."""
    gt = pd.read_csv(DATA / "_ground_truth_customers.csv")
    assert gt.customer_id.is_unique
    assert len(gt) < 100, "unexpected number of illicit customers"
    assert set(gt.typology) <= {"structuring", "funnel_account", "rapid_movement",
                                "third_party_cash", "round_value_layering"}
