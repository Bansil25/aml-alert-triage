"""
Explainability layer.

Two audiences, two artifacts:

1. THE INVESTIGATOR needs to know why THIS alert is where it is in the queue,
   in a sentence, before they open the case. That is the reason-code file.
2. THE MODEL RISK / VALIDATION FUNCTION needs to know what the model has
   learned globally, and whether it is relying on anything it should not.
   That is the SHAP summary.

An unexplainable model is unusable in a regulated process regardless of its
accuracy. The production model is logistic regression precisely because its
per-alert contributions are exact rather than approximated: contribution =
coefficient x standardised feature value, which sums to the log-odds. There is
no estimation step to argue with in a model validation review.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from features import CATEGORICAL, build_feature_frame, build_labels, feature_matrix

ROOT = Path(__file__).resolve().parents[1]

# Human-readable names. A reason code that says "vol_to_declared_30d" is not a
# reason code; it is a variable name with ambition.
FEATURE_LABELS = {
    "vol_to_declared_30d": "30-day credit volume vs declared income",
    "vol_to_declared_90d": "90-day credit volume vs declared income",
    "cash_share_30d": "share of 30-day credits arriving as cash",
    "cash_share_90d": "share of 90-day credits arriving as cash",
    "highrisk_share_30d": "share of 30-day value with high-risk jurisdictions",
    "vol_highrisk_30d": "30-day value with high-risk jurisdictions",
    "wire_share_30d": "share of 30-day value moved by wire",
    "band_share_30d": "share of cash deposits in the near-threshold band",
    "n_band_cash_30d": "count of near-threshold cash deposits in 30 days",
    "retention_30d": "proportion of 30-day inflow retained",
    "retention_90d": "proportion of 90-day inflow retained",
    "velocity_ratio": "30-day volume vs 90-day run rate",
    "txn_velocity_ratio": "30-day transaction count vs 90-day run rate",
    "round_share_30d": "share of transactions at round values",
    "offhours_share_30d": "share of activity outside banking hours",
    "n_counterparties_30d": "distinct counterparties in 30 days",
    "vol_per_counterparty_30d": "average value per counterparty",
    "prior_alerts": "prior alerts on this customer",
    "n_rules_same_day": "distinct rules triggered on the same day",
    "days_since_prior_alert": "days since the previous alert",
    "kyc_risk_rating": "KYC risk rating at onboarding",
    "is_pep": "politically exposed person flag",
    "tenure_months": "relationship tenure",
    "is_cash_intensive_business": "cash-intensive business type",
    "occupation_or_business_type": "occupation or business type",
    "newest_account_age": "age of newest account",
    "n_accounts": "number of accounts held",
    "vol_credit_7d": "7-day credit volume",
    "vol_credit_30d": "30-day credit volume",
    "vol_cash_credit_30d": "30-day cash credit volume",
    "vol_cash_credit_7d": "7-day cash credit volume",
    "n_wire_90d": "90-day wire count",
    "txn_per_active_day": "transactions per active day",
}


BASE_LABELS = {
    "vol_total": "total value", "vol_credit": "credit value",
    "vol_debit": "debit value", "vol_cash_credit": "cash credit value",
    "vol_wire": "wire value", "vol_highrisk": "value with high-risk jurisdictions",
    "n_txn": "transaction count", "n_cash_credit": "cash credit count",
    "n_wire": "wire count", "n_highrisk": "high-risk jurisdiction count",
    "n_band_cash": "near-threshold cash deposit count",
    "n_round": "round-value transaction count",
    "n_offhours": "out-of-hours transaction count",
    "n_counterparties": "distinct counterparties", "max_amt": "largest transaction",
}


def pretty(name: str) -> str:
    """Human-readable feature label.

    Falls back to decomposing the '<measure>_<window>d' convention so a new
    rolling window does not silently produce a raw variable name in an
    investigator-facing field.
    """
    if name in FEATURE_LABELS:
        return FEATURE_LABELS[name]
    parts = name.rsplit("_", 1)
    if len(parts) == 2 and parts[1].endswith("d") and parts[1][:-1].isdigit():
        base, window = parts[0], parts[1][:-1]
        if base in BASE_LABELS:
            return f"{window}-day {BASE_LABELS[base]}"
    return name.replace("_", " ")


def logistic_contributions(pipeline, X: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Exact per-alert contribution to the log-odds for a linear model.

    contribution_j = coefficient_j * transformed_value_j
    The sum of contributions plus the intercept IS the log-odds. Nothing is
    approximated, which is what makes this defensible in model validation.
    """
    prep = pipeline.named_steps["prep"]
    clf = pipeline.named_steps["clf"]
    Xt = prep.transform(X)
    if hasattr(Xt, "toarray"):
        Xt = Xt.toarray()
    contrib = Xt * clf.coef_[0]
    return contrib, list(prep.get_feature_names_out())


def build_reason_codes(contrib, names, top_n=3):
    """Top drivers pushing each alert UP the queue, and the top mitigant."""
    rows = []
    # Strip the ColumnTransformer prefixes ("num__", "cat__") for display.
    clean = [n.split("__", 1)[-1] for n in names]
    for i in range(contrib.shape[0]):
        c = contrib[i]
        up = np.argsort(-c)[:top_n]
        down = np.argsort(c)[:1]
        drivers = "; ".join(
            f"{pretty(clean[j].split('_')[0] if False else clean[j])}"
            for j in up if c[j] > 0)
        mitigant = (f"{pretty(clean[down[0]])}" if c[down[0]] < 0 else "")
        rows.append({
            "reason_1": pretty(clean[up[0]]) if c[up[0]] > 0 else "",
            "reason_2": pretty(clean[up[1]]) if len(up) > 1 and c[up[1]] > 0 else "",
            "reason_3": pretty(clean[up[2]]) if len(up) > 2 and c[up[2]] > 0 else "",
            "top_mitigant": mitigant,
            "reason_summary": drivers,
        })
    return pd.DataFrame(rows)


def main() -> None:
    with open(ROOT / "outputs" / "model.pkl", "rb") as f:
        art = pickle.load(f)

    df = build_feature_frame(ROOT / "data" / "aml.db")
    y = build_labels(df, ROOT / "data").values
    X, cols = feature_matrix(df)

    cat_cols = art["categorical"]
    Xl = X.copy()
    for c in cat_cols:
        Xl[c] = Xl[c].astype(str)

    print("[exp] computing exact linear contributions")
    contrib, names = logistic_contributions(art["logistic"], Xl)
    reasons = build_reason_codes(contrib, names)

    scored = pd.read_csv(ROOT / "outputs" / "scored_alerts.csv")
    out = pd.concat([scored.reset_index(drop=True), reasons], axis=1)
    out.to_csv(ROOT / "outputs" / "scored_alerts_explained.csv", index=False)
    print(f"[exp] wrote reason codes for {len(out):,} alerts")

    # ---- global attribution from the tree model (model-risk artifact) ------
    print("[exp] computing SHAP values on the tree model (sampled)")
    import shap
    samp = X.sample(n=min(4000, len(X)), random_state=7)
    expl = shap.TreeExplainer(art["model"])
    sv = expl.shap_values(samp)
    if isinstance(sv, list):
        sv = sv[1]
    mean_abs = np.abs(sv).mean(axis=0)
    shap_df = (pd.DataFrame({"feature": samp.columns, "mean_abs_shap": mean_abs})
                 .sort_values("mean_abs_shap", ascending=False))
    shap_df.to_csv(ROOT / "outputs" / "shap_global.csv", index=False)

    print("\n[exp] top 10 global drivers (mean |SHAP|):")
    for r in shap_df.head(10).itertuples():
        print(f"[exp]   {pretty(r.feature):<48} {r.mean_abs_shap:.4f}")

    print("\n[exp] sample reason codes, highest-scoring alerts:")
    top = out.nlargest(5, "score")
    for r in top.itertuples():
        print(f"[exp]   {r.alert_id:<22} score={r.score:.3f} tier={r.tier}")
        print(f"[exp]     -> {r.reason_summary}")


if __name__ == "__main__":
    main()
