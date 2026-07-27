"""
Rule effectiveness and overlap analysis.

This is the part of the project that produces a RECOMMENDATION rather than a
dashboard. Three questions a Head of Financial Crime actually has to answer:

  1. Which rules earn their keep, and which are burning investigator hours?
  2. Which rules are redundant -- catching the same customers as another rule,
     so that retiring one loses nothing?
  3. Which rules would we lose coverage on if we switched them off? A rule with
     terrible precision that is the SOLE detector of a typology cannot be
     retired at any precision, and conflating those two things is how coverage
     gaps get created by well-meaning efficiency programs.

Output: outputs/rule_effectiveness.csv, outputs/rule_overlap.csv
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(conn, data_dir: Path) -> pd.DataFrame:
    alerts = pd.read_sql("""
        SELECT a.alert_id, a.rule_id, a.customer_key, a.date_key,
               r.rule_name, r.severity, r.rule_family, c.customer_id
        FROM fact_alert a
        JOIN dim_rule r ON r.rule_id = a.rule_id
        JOIN dim_customer c ON c.customer_key = a.customer_key
    """, conn)
    gt = pd.read_csv(data_dir / "_ground_truth_customers.csv")
    m = alerts.merge(gt, on="customer_id", how="left")
    m["is_true_positive"] = (
        m["typology"].notna()
        & (m["date_key"] >= m["campaign_start_day"] - 7)
        & (m["date_key"] <= m["campaign_end_day"] + 7)).astype(int)
    return m


def effectiveness(m: pd.DataFrame, all_rules_df: pd.DataFrame,
                  minutes: float, rate: float) -> pd.DataFrame:
    g = m.groupby(["rule_id", "rule_name", "severity"], as_index=False).agg(
        alerts=("alert_id", "size"),
        true_positives=("is_true_positive", "sum"),
        customers_alerted=("customer_key", "nunique"),
    )
    # Rules that produced no alerts vanish from a groupby. They must not vanish
    # from the analysis -- a rule that never fires is the single most important
    # thing to surface, because on paper it is providing coverage it is not
    # actually providing.
    g = all_rules_df.merge(g.drop(columns=["rule_name", "severity"]),
                           on="rule_id", how="left")
    for c in ["alerts", "true_positives", "customers_alerted"]:
        g[c] = g[c].fillna(0).astype(int)

    g["precision_pct"] = np.where(
        g.alerts > 0, (g.true_positives / g.alerts.replace(0, np.nan) * 100), 0).round(2)
    g["false_positives"] = g.alerts - g.true_positives
    g["investigator_hours"] = (g.alerts * minutes / 60).round(1)
    g["annual_cost_cad"] = (g.investigator_hours * rate * 2).round(0)  # 180d -> ~annual

    # Distinct illicit customers this rule detects, and how many ONLY it detects.
    tp = m[m.is_true_positive == 1]
    by_rule = tp.groupby("rule_id")["customer_id"].apply(set).to_dict()
    all_rules = list(g.rule_id)
    unique_cover = {}
    for r in all_rules:
        mine = by_rule.get(r, set())
        others = set().union(*[by_rule.get(o, set()) for o in all_rules if o != r]) \
            if len(all_rules) > 1 else set()
        unique_cover[r] = len(mine - others)
    g["illicit_customers_detected"] = g.rule_id.map(
        lambda r: len(by_rule.get(r, set())))
    g["sole_detector_for"] = g.rule_id.map(unique_cover)

    # Recommendation logic, stated explicitly so it can be argued with.
    def recommend(row):
        if row.alerts == 0:
            return "RETIRE - has produced zero alerts; no detection value"
        if row.sole_detector_for > 0:
            return (f"RETAIN - sole detector for {row.sole_detector_for} "
                    f"illicit customer(s); cannot be retired at any precision")
        if row.precision_pct < 3.0:
            return "RECALIBRATE - low precision and fully covered by other rules"
        return "RETAIN - contributes detection at acceptable precision"

    g["recommendation"] = g.apply(recommend, axis=1)
    return g.sort_values("alerts", ascending=False)


def overlap(m: pd.DataFrame) -> pd.DataFrame:
    """Pairwise overlap on (customer, date). High overlap with lower precision
    is the signature of a redundant rule."""
    keys = m.assign(k=m.customer_key.astype(str) + "|" + m.date_key.astype(str))
    by_rule = keys.groupby("rule_id")["k"].apply(set).to_dict()
    rules = sorted(by_rule)
    rows = []
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            A, B = by_rule[a], by_rule[b]
            inter = len(A & B)
            if not A or not B:
                continue
            rows.append({
                "rule_a": a, "rule_b": b,
                "alerts_a": len(A), "alerts_b": len(B),
                "overlapping_events": inter,
                "jaccard": round(inter / len(A | B), 4),
                "pct_of_a_covered_by_b": round(inter / len(A) * 100, 2),
                "pct_of_b_covered_by_a": round(inter / len(B) * 100, 2),
            })
    return pd.DataFrame(rows).sort_values("jaccard", ascending=False)


def main() -> None:
    p = yaml.safe_load((ROOT / "config" / "params.yml").read_text())
    cb = p["cost_benefit"]
    rate = (cb["investigator_base_salary_cad"] * cb["loading_factor"]
            / cb["productive_hours_per_year"])

    conn = sqlite3.connect(ROOT / "data" / "aml.db")
    m = load(conn, ROOT / "data")
    conn.close()

    conn2 = sqlite3.connect(ROOT / "data" / "aml.db")
    all_rules_df = pd.read_sql(
        "SELECT rule_id, rule_name, severity FROM dim_rule ORDER BY rule_id", conn2)
    conn2.close()
    eff = effectiveness(m, all_rules_df, cb["minutes_per_alert_l1"], rate)
    ov = overlap(m)
    eff.to_csv(ROOT / "outputs" / "rule_effectiveness.csv", index=False)
    ov.to_csv(ROOT / "outputs" / "rule_overlap.csv", index=False)

    print("[rul] rule effectiveness")
    print(f"[rul] {'rule':<6}{'alerts':>8}{'TPs':>6}{'prec%':>8}{'sole':>6}"
          f"{'annual $':>12}  recommendation")
    for r in eff.itertuples():
        print(f"[rul] {r.rule_id:<6}{r.alerts:>8,}{r.true_positives:>6}"
              f"{r.precision_pct:>8.2f}{r.sole_detector_for:>6}"
              f"{r.annual_cost_cad:>12,.0f}  {r.recommendation[:58]}")

    print("\n[rul] highest rule overlap (candidate redundancy):")
    for r in ov.head(5).itertuples():
        print(f"[rul]   {r.rule_a} / {r.rule_b}  jaccard={r.jaccard:.3f}  "
              f"{r.pct_of_a_covered_by_b:.0f}% of {r.rule_a} also caught by {r.rule_b}")

    dead = eff[eff.alerts == 0]
    if len(dead):
        print(f"\n[rul] DEAD RULES ({len(dead)}): "
              f"{', '.join(dead.rule_id)} produced zero alerts in the period.")
        print("[rul] A rule that never fires is not a control. It is either "
              "mis-specified or")
        print("[rul] its threshold is unreachable; either way it should be "
              "recalibrated or retired,")
        print("[rul] because on paper it is providing coverage it does not "
              "actually provide.")


if __name__ == "__main__":
    main()
