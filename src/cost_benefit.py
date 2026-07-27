"""
Business case for the triage model.

Two rules govern this file:

1. BENEFIT IS CAPACITY REDIRECTED, NOT HEADCOUNT REMOVED. The model defers
   alerts, it does not close them. Every alert is still worked. The saving is
   the difference between working a queue in priority order and working it in
   arrival order: the same investigator hours find the same positives sooner,
   and the hours freed at the top of the queue are redirected to enhanced due
   diligence rather than eliminated. A business case built on firing
   investigators would not survive contact with a compliance officer, and
   would be the wrong recommendation anyway.

2. THE COST SIDE IS MODELLED AS SERIOUSLY AS THE BENEFIT SIDE. Programs of
   this kind fail on change management and model governance, not on modelling.
   Build cost, annual platform cost and annual model-governance cost are all
   included, and the payback period is stated net.

Every assumption is in config/params.yml. The sensitivity table exists because
the point estimate is the least interesting number in this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


def loaded_hourly_rate(p) -> float:
    cb = p["cost_benefit"]
    return (cb["investigator_base_salary_cad"] * cb["loading_factor"]
            / cb["productive_hours_per_year"])


def annualise(n_alerts_in_window: float, window_days: int) -> float:
    return n_alerts_in_window * (365.0 / window_days)


def build_case(p, metrics, recall_target: float = 1.0) -> dict:
    cb = p["cost_benefit"]
    rate = loaded_hourly_rate(p)
    mins = cb["minutes_per_alert_l1"]

    window_days = 180 - p["split"]["valid_end_day"]
    curve = {r["recall_target"]: r for r in metrics["results"]["tradeoff_curve_prospective"]}
    tiers = {t["tier"]: t for t in metrics["results"]["tier_policy_holdout"]}

    policy = curve[recall_target]
    n_holdout = (policy["holdout_alerts_reviewed"]
                 / (1 - policy["holdout_volume_deferred_pct"] / 100))
    annual_alerts = annualise(n_holdout, window_days)

    # ---- Benefit A: conservative deferral at zero true-positive loss ------
    # Deferred alerts move to a 30-day SLA and are worked by junior staff or
    # sampled under QA. Modelled as a 60% reduction in senior-analyst time on
    # that slice, NOT a 100% saving, because the work still happens.
    defer_share = policy["holdout_volume_deferred_pct"] / 100
    deferred_alerts = annual_alerts * defer_share
    hours_freed_a = deferred_alerts * (mins / 60.0) * 0.60
    benefit_a = hours_freed_a * rate

    # ---- Benefit B: earlier detection from priority ordering --------------
    # 81% of positives sit in the top decile. Working that decile first pulls
    # detection forward; valued conservatively as the analyst time no longer
    # spent reaching those cases through an unordered queue.
    t1 = tiers["T1_same_day"]
    # Expected position in queue: unordered = 50% of volume searched on average;
    # ranked = 5% (midpoint of the top decile).
    hours_freed_b = (annual_alerts * 0.45 * (mins / 60.0)
                     * (t1["share_of_true_positives_pct"] / 100) * 0.10)
    benefit_b = hours_freed_b * rate

    gross_benefit = benefit_a + benefit_b

    # ---- Costs ------------------------------------------------------------
    impl = cb["implementation"]
    build_cost = impl["build_effort_person_days"] * impl["blended_day_rate_cad"]
    annual_cost = (impl["annual_platform_cost_cad"]
                   + impl["annual_model_governance_cost_cad"])
    net_annual = gross_benefit - annual_cost
    payback_months = (build_cost / (net_annual / 12)) if net_annual > 0 else float("inf")

    return {
        "assumptions": {
            "loaded_hourly_rate_cad": round(rate, 2),
            "minutes_per_alert_l1": mins,
            "annual_alert_volume_est": int(annual_alerts),
            "holdout_window_days": window_days,
            "recall_policy": recall_target,
            "holdout_recall_achieved_pct": round(policy["holdout_recall"] * 100, 2),
            "deferred_share_pct": round(defer_share * 100, 2),
            "deferred_work_reduction_factor": 0.60,
        },
        "benefit": {
            "hours_freed_deferral": int(hours_freed_a),
            "value_deferral_cad": int(benefit_a),
            "hours_freed_prioritisation": int(hours_freed_b),
            "value_prioritisation_cad": int(benefit_b),
            "gross_annual_benefit_cad": int(gross_benefit),
        },
        "cost": {
            "one_off_build_cad": int(build_cost),
            "annual_platform_cad": impl["annual_platform_cost_cad"],
            "annual_model_governance_cad": impl["annual_model_governance_cost_cad"],
            "total_annual_run_cad": int(annual_cost),
        },
        "result": {
            "net_annual_benefit_cad": int(net_annual),
            "payback_months": round(payback_months, 1),
            "first_year_net_cad": int(net_annual - build_cost),
        },
    }


def sensitivity(p, metrics) -> pd.DataFrame:
    """The point estimate is a story. The sensitivity table is the analysis.

    Three axes are varied: how long an alert takes to work, what an
    investigator costs, and which recall policy the compliance officer
    chooses. The last of these moves the answer by an order of magnitude,
    which is exactly why it is presented as their decision and not mine.
    """
    rows = []
    for mins in [15, 22, 30]:
        for rate_mult, rate_label in [(0.8, "low"), (1.0, "base"), (1.25, "high")]:
            for target in [1.0, 0.99, 0.95]:
                p2 = json.loads(json.dumps(p))
                p2["cost_benefit"]["minutes_per_alert_l1"] = mins
                p2["cost_benefit"]["investigator_base_salary_cad"] = (
                    p["cost_benefit"]["investigator_base_salary_cad"] * rate_mult)
                case = build_case(p2, metrics, recall_target=target)
                rows.append({
                    "minutes_per_alert": mins,
                    "investigator_cost": rate_label,
                    "recall_policy": f"{target*100:.0f}%",
                    "holdout_recall_achieved_pct":
                        case["assumptions"]["holdout_recall_achieved_pct"],
                    "volume_deferred_pct": case["assumptions"]["deferred_share_pct"],
                    "net_annual_benefit_cad": case["result"]["net_annual_benefit_cad"],
                    "payback_months": case["result"]["payback_months"],
                })
    return pd.DataFrame(rows)


def main() -> None:
    p = yaml.safe_load((ROOT / "config" / "params.yml").read_text())
    metrics = json.loads((ROOT / "outputs" / "metrics.json").read_text())

    case = build_case(p, metrics)
    sens = sensitivity(p, metrics)

    (ROOT / "outputs" / "cost_benefit.json").write_text(json.dumps(case, indent=2))
    sens.to_csv(ROOT / "outputs" / "cost_benefit_sensitivity.csv", index=False)

    a, b, c, r = case["assumptions"], case["benefit"], case["cost"], case["result"]
    print("[cba] ---------------- BUSINESS CASE (conservative policy) ----------")
    print(f"[cba] loaded investigator rate     : ${a['loaded_hourly_rate_cad']}/hr")
    print(f"[cba] estimated annual alerts      : {a['annual_alert_volume_est']:,}")
    print(f"[cba] volume deferred (0 TP loss)  : {a['deferred_share_pct']}%")
    print(f"[cba] hours freed, deferral        : {b['hours_freed_deferral']:,}")
    print(f"[cba] hours freed, prioritisation  : {b['hours_freed_prioritisation']:,}")
    print(f"[cba] gross annual benefit         : ${b['gross_annual_benefit_cad']:,}")
    print(f"[cba] one-off build                : ${c['one_off_build_cad']:,}")
    print(f"[cba] annual run cost              : ${c['total_annual_run_cad']:,}")
    print(f"[cba] NET ANNUAL BENEFIT           : ${r['net_annual_benefit_cad']:,}")
    print(f"[cba] payback                      : {r['payback_months']} months")
    print(f"[cba] first-year net               : ${r['first_year_net_cad']:,}")
    print("\n[cba] sensitivity (net annual benefit, CAD):")
    piv = sens.pivot_table(index=["minutes_per_alert", "investigator_cost"],
                           columns="recall_policy", values="net_annual_benefit_cad")
    print(piv.to_string())


if __name__ == "__main__":
    main()
