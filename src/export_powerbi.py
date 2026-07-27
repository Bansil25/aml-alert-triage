"""
Export the serving layer to CSV for Power BI Desktop.

Power BI connects to these files and builds a star schema over them. The
relationships, measures and page layout are specified in
powerbi/DASHBOARD_SPEC.md and powerbi/dax_measures.md.

WHY CSV AND NOT A LIVE CONNECTION: a reviewer cloning this repo has no access
to a database server. Flat files make the model reproducible on any machine
with Power BI Desktop installed, which is the point of a portfolio artifact.
In production this layer would be a set of views in the warehouse consumed by
a shared semantic model with incremental refresh.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "exports"

EXPORTS = {
    "dim_customer": "SELECT * FROM dim_customer",
    "dim_date": "SELECT * FROM dim_date",
    "dim_rule": "SELECT * FROM dim_rule",
    "dim_country": "SELECT * FROM dim_country",
    "fact_alert": "SELECT * FROM mart_alert",
    "fact_customer_week": "SELECT * FROM mart_customer_week",
    "fact_txn_drill": "SELECT * FROM mart_txn_drill",
    "dq_scorecard": "SELECT * FROM mart_dq",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "data" / "aml.db")

    # Load model output so the marts can join to it.
    scores = pd.read_csv(ROOT / "outputs" / "scored_alerts_explained.csv")
    scores.to_sql("stg_scores", conn, if_exists="replace", index=False)

    conn.executescript((ROOT / "src" / "sql" / "05_marts.sql").read_text())
    conn.commit()

    for name, q in EXPORTS.items():
        df = pd.read_sql(q, conn)
        path = OUT / f"{name}.csv"
        df.to_csv(path, index=False)
        size_mb = path.stat().st_size / 1e6
        print(f"[pbi] {name:<22} {len(df):>9,} rows  {size_mb:>6.2f} MB")

    # Analysis outputs the dashboard also surfaces.
    for src in ["rule_effectiveness.csv", "rule_overlap.csv",
                "cost_benefit_sensitivity.csv", "feature_importance.csv"]:
        s = ROOT / "outputs" / src
        if s.exists():
            df = pd.read_csv(s)
            df.to_csv(OUT / src, index=False)
            print(f"[pbi] {src:<22} {len(df):>9,} rows")

    conn.close()
    total = sum(p.stat().st_size for p in OUT.glob("*.csv")) / 1e6
    print(f"[pbi] total export size: {total:.1f} MB -> {OUT}")


if __name__ == "__main__":
    main()
