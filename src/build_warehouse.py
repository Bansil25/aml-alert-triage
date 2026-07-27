"""
Build the analytical warehouse: raw CSV -> staging -> star schema -> alerts -> DQ.

SQLite is used because it is zero-install, runs identically in CI, and supports
the full window-function syntax the rules engine depends on. The SQL is written
to stay close to portable ANSI so the same logic lifts into T-SQL or Snowflake
with minimal change; the deliberate exceptions are noted in docs/DATA_MODEL.md.

Blocking data quality failures abort the run with a non-zero exit code. That is
the point of a control framework: it has to be able to stop the line.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "src" / "sql"

SQL_STEPS = [
    ("01_schema.sql", "dimensional model"),
    ("02_load_clean.sql", "clean + conform"),
    ("03_rules.sql", "rules engine"),
    ("04_data_quality.sql", "data quality controls"),
]


def load_staging(conn: sqlite3.Connection, data_dir: Path) -> None:
    for name, fname in [("stg_customers", "customers.csv"),
                        ("stg_accounts", "accounts.csv"),
                        ("stg_transactions", "transactions.csv")]:
        df = pd.read_csv(data_dir / fname)
        df.to_sql(name, conn, if_exists="replace", index=False)
        print(f"[whs] staged {name:<18} {len(df):>9,} rows")


def run_sql_file(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text())
    conn.commit()


def report_dq(conn: sqlite3.Connection) -> int:
    df = pd.read_sql("SELECT * FROM dq_result ORDER BY check_id", conn)
    print("\n[dq ] ------------------------------------------------------------")
    for r in df.itertuples():
        flag = "PASS" if r.status == "PASS" else "FAIL"
        print(f"[dq ] {r.check_id}  {flag}  [{r.severity:<8}] "
              f"{r.check_name[:58]:<58} value={r.metric_value}")
    failures = df[(df.status == "FAIL") & (df.severity == "blocking")]
    warns = df[(df.status == "FAIL") & (df.severity == "warning")]
    print(f"[dq ] {len(df)} checks | {len(failures)} blocking failures | "
          f"{len(warns)} warnings")
    return len(failures)


def summarise(conn: sqlite3.Connection) -> None:
    q = """
    SELECT r.rule_id, r.rule_name, COUNT(a.alert_id) AS alerts,
           COUNT(DISTINCT a.customer_key) AS customers
    FROM dim_rule r LEFT JOIN fact_alert a ON a.rule_id = r.rule_id
    GROUP BY r.rule_id ORDER BY alerts DESC
    """
    print("\n[whs] alerts by rule")
    for r in pd.read_sql(q, conn).itertuples():
        print(f"[whs]   {r.rule_id}  {r.rule_name[:42]:<42} "
              f"{r.alerts:>7,} alerts  {r.customers:>5,} customers")
    total = pd.read_sql("SELECT COUNT(*) n FROM fact_alert", conn).n[0]
    print(f"[whs]   TOTAL {total:,} alerts")


def main(db_path: Path, data_dir: Path) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    load_staging(conn, data_dir)
    for fname, label in SQL_STEPS:
        print(f"[whs] running {fname:<22} ({label})")
        run_sql_file(conn, SQL_DIR / fname)

    summarise(conn)
    n_fail = report_dq(conn)
    conn.close()

    if n_fail:
        print(f"\n[whs] ABORT: {n_fail} blocking data quality failure(s).", file=sys.stderr)
        return 1
    print("\n[whs] warehouse built successfully")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "aml.db")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    a = ap.parse_args()
    sys.exit(main(a.db, a.data))
