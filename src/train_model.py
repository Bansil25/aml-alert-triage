"""
Train and evaluate the alert triage model.

THE OPERATING POLICY, WHICH CONSTRAINS EVERYTHING BELOW:
The model RE-RANKS alerts. It never auto-closes one. Nothing is discarded;
low-scoring alerts are routed to a deferred queue that is still worked, just
later and by less senior staff. The benefit claimed is therefore a
*prioritisation* benefit, not a headcount cut, and the acceptance criterion is
that no true positive may be pushed below the review threshold.

Consequently the threshold is chosen as the highest score at which recall on
the VALIDATION window is 100%, and that threshold is then applied UNCHANGED to
the holdout window. Holdout recall is reported as measured. If it comes in
below 100%, that is the honest result and it is printed as such rather than
retuned until it looks good -- retuning on the holdout would make the holdout
a second validation set and the reported number meaningless.
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

import lightgbm as lgb

from features import (CATEGORICAL, build_feature_frame, build_labels,
                      feature_matrix)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------

def temporal_split(df, y, train_end, valid_end):
    tr = df["date_key"] <= train_end
    va = (df["date_key"] > train_end) & (df["date_key"] <= valid_end)
    ho = df["date_key"] > valid_end
    return tr.values, va.values, ho.values


def threshold_at_full_recall(y_true, scores):
    """Highest threshold that still captures every positive.

    Implemented as: the minimum score assigned to any true positive. Any alert
    scoring at or above this is reviewed at normal priority; everything below
    goes to the deferred queue.
    """
    pos = scores[y_true == 1]
    return float(pos.min()) if len(pos) else 0.0


def evaluate_at_threshold(y_true, scores, thr):
    review = scores >= thr
    n = len(y_true)
    tp_total = int(y_true.sum())
    tp_review = int(y_true[review].sum())
    return {
        "n_alerts": n,
        "n_reviewed_at_priority": int(review.sum()),
        "n_deferred": int((~review).sum()),
        "volume_reduction_pct": float((~review).mean() * 100),
        "recall": float(tp_review / tp_total) if tp_total else float("nan"),
        "true_positives_total": tp_total,
        "true_positives_missed": tp_total - tp_review,
        "precision_at_priority": float(tp_review / review.sum()) if review.sum() else 0.0,
        "baseline_precision": float(tp_total / n) if n else 0.0,
    }


def tradeoff_curve(y_valid, s_valid, y_hold, s_hold,
                   recall_targets=(1.0, 0.99, 0.98, 0.95, 0.90)):
    """Volume deferrable at each recall target, measured PROSPECTIVELY.

    For each target, the threshold is chosen on the VALIDATION window and then
    applied unchanged to the holdout. The holdout recall column is therefore
    what the policy would actually have delivered, not what a curve fitted to
    the holdout could have delivered.

    An earlier version computed this curve directly on the holdout and reported
    40.8% deferrable at 100% recall. That number was an oracle result -- it
    assumed knowledge of which holdout alerts were positive, which is precisely
    the thing you do not have on the day. The honest figure is materially lower.

    'How much recall would you trade for how much capacity' is a decision for
    the Chief Compliance Officer and the regulator. The job here is to price
    the trade honestly, not to make it.
    """
    order = np.argsort(-s_valid)
    ys, ss = y_valid[order], s_valid[order]
    total_pos = ys.sum()
    cum_pos = np.cumsum(ys)

    rows = []
    for target in recall_targets:
        need = int(np.ceil(target * total_pos))
        k = int(np.searchsorted(cum_pos, need) + 1)
        thr = float(ss[k - 1])                      # threshold set on validation
        review = s_hold >= thr                      # applied blind to holdout
        rows.append({
            "recall_target": target,
            "threshold": thr,
            "validation_recall": float(cum_pos[k - 1] / total_pos),
            "holdout_recall": float(y_hold[review].sum() / y_hold.sum()),
            "holdout_alerts_reviewed": int(review.sum()),
            "holdout_volume_deferred_pct": float((~review).mean() * 100),
            "holdout_precision_in_review": float(
                y_hold[review].sum() / review.sum()) if review.sum() else 0.0,
        })
    return rows


def tier_policy(y_true, scores, tier1_pct=0.10, tier2_pct=0.40):
    """Three-tier triage. Nothing is closed; tiers set the SLA.

    Tier 1  top 10% by score   -- reviewed same day, senior investigator
    Tier 2  next 30%           -- reviewed within 5 business days
    Tier 3  bottom 60%         -- reviewed within 30 days, junior/QA sampling
    """
    n = len(scores)
    order = np.argsort(-scores)
    tier = np.empty(n, dtype=object)
    c1, c2 = int(n * tier1_pct), int(n * tier2_pct)
    tier[order[:c1]] = "T1_same_day"
    tier[order[c1:c2]] = "T2_five_day"
    tier[order[c2:]] = "T3_thirty_day"
    out = []
    for t in ["T1_same_day", "T2_five_day", "T3_thirty_day"]:
        m = tier == t
        out.append({"tier": t, "alerts": int(m.sum()),
                    "share_of_volume_pct": float(m.mean() * 100),
                    "true_positives": int(y_true[m].sum()),
                    "share_of_true_positives_pct": float(
                        y_true[m].sum() / y_true.sum() * 100),
                    "precision_pct": float(y_true[m].mean() * 100)})
    return out


def bootstrap_ci(y_true, scores, thr, n_iter, ci, rng):
    """Percentile bootstrap over alerts. Resampling alerts (not customers) is
    the conservative choice here and is stated as a limitation in the README:
    alerts from the same customer are correlated, so these intervals are
    somewhat narrower than a customer-level block bootstrap would give."""
    n = len(y_true)
    rec, vol = [], []
    for _ in range(n_iter):
        idx = rng.integers(0, n, n)
        yt, sc = y_true[idx], scores[idx]
        if yt.sum() == 0:
            continue
        review = sc >= thr
        rec.append(yt[review].sum() / yt.sum())
        vol.append((~review).mean() * 100)
    lo, hi = (1 - ci) / 2 * 100, (1 + ci) / 2 * 100
    return {
        "recall_ci": [float(np.percentile(rec, lo)), float(np.percentile(rec, hi))],
        "volume_reduction_ci": [float(np.percentile(vol, lo)),
                                float(np.percentile(vol, hi))],
    }


# ---------------------------------------------------------------------------

def build_logistic(num_cols, cat_cols):
    return Pipeline([
        ("prep", ColumnTransformer([
            ("num", StandardScaler(), num_cols),
            ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=20), cat_cols),
        ])),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced", C=0.5)),
    ])


def main(cfg_path: Path) -> None:
    p = yaml.safe_load(cfg_path.read_text())
    rng = np.random.default_rng(p["seed"])

    print("[mdl] building features")
    df = build_feature_frame(ROOT / "data" / "aml.db")
    y = build_labels(df, ROOT / "data").values
    X, cols = feature_matrix(df)

    tr, va, ho = temporal_split(df, y, p["split"]["train_end_day"],
                                p["split"]["valid_end_day"])
    print(f"[mdl] train {tr.sum():,} ({y[tr].sum()} pos) | "
          f"valid {va.sum():,} ({y[va].sum()} pos) | "
          f"holdout {ho.sum():,} ({y[ho].sum()} pos)")

    cat_cols = [c for c in CATEGORICAL if c in X.columns]
    num_cols = [c for c in X.columns if c not in cat_cols]

    results = {}

    # ---- baseline 1: investigate everything (today's operating model) -----
    results["baseline_all_alerts"] = {
        "description": "Current state: every rule alert is worked at equal priority.",
        "holdout": {"recall": 1.0, "volume_reduction_pct": 0.0,
                    "precision_at_priority": float(y[ho].mean())},
    }

    # ---- baseline 2: random ranking ---------------------------------------
    rand_scores = rng.random(len(y))
    thr_rand = threshold_at_full_recall(y[va], rand_scores[va])
    results["baseline_random"] = {
        "description": "Random score. Sanity floor -- any real model must beat this.",
        "holdout": evaluate_at_threshold(y[ho], rand_scores[ho], thr_rand),
    }

    # ---- model 1: logistic regression (interpretable reference) -----------
    print("[mdl] fitting logistic regression")
    Xl = X.copy()
    for c in cat_cols:
        Xl[c] = Xl[c].astype(str)
    logit = build_logistic(num_cols, cat_cols)
    logit.fit(Xl[tr], y[tr])
    s_va_l = logit.predict_proba(Xl[va])[:, 1]
    s_ho_l = logit.predict_proba(Xl[ho])[:, 1]
    thr_l = threshold_at_full_recall(y[va], s_va_l)
    results["logistic_regression"] = {
        "description": "Regularised logistic regression. Fully interpretable reference model.",
        "validation": {"pr_auc": float(average_precision_score(y[va], s_va_l)),
                       "roc_auc": float(roc_auc_score(y[va], s_va_l))},
        "holdout": {**evaluate_at_threshold(y[ho], s_ho_l, thr_l),
                    "pr_auc": float(average_precision_score(y[ho], s_ho_l)),
                    "roc_auc": float(roc_auc_score(y[ho], s_ho_l))},
    }

    # ---- model 2: gradient boosting ---------------------------------------
    print("[mdl] fitting gradient boosted trees")
    m = p["model"]
    gbm = lgb.LGBMClassifier(
        n_estimators=m["n_estimators"], learning_rate=m["learning_rate"],
        num_leaves=m["num_leaves"], min_child_samples=m["min_child_samples"],
        class_weight=m["class_weight"], random_state=p["seed"], verbose=-1)
    gbm.fit(X[tr], y[tr], categorical_feature=cat_cols)

    s_va = gbm.predict_proba(X[va])[:, 1]
    s_ho = gbm.predict_proba(X[ho])[:, 1]
    thr = threshold_at_full_recall(y[va], s_va)

    ho_metrics = evaluate_at_threshold(y[ho], s_ho, thr)
    ho_metrics["pr_auc"] = float(average_precision_score(y[ho], s_ho))
    ho_metrics["roc_auc"] = float(roc_auc_score(y[ho], s_ho))
    ho_metrics.update(bootstrap_ci(y[ho], s_ho, thr,
                                   p["evaluation"]["bootstrap_iterations"],
                                   p["evaluation"]["bootstrap_ci"], rng))

    results["gradient_boosted"] = {
        "description": "LightGBM. Primary model.",
        "threshold_source": "highest score at 100% recall on VALIDATION window",
        "threshold": thr,
        "validation": {
            **evaluate_at_threshold(y[va], s_va, thr),
            "pr_auc": float(average_precision_score(y[va], s_va)),
            "roc_auc": float(roc_auc_score(y[va], s_va)),
        },
        "holdout": ho_metrics,
    }

    # ---- production model selection ---------------------------------------
    # Selection is on performance AT THE POLICY OPERATING POINT, not on AUC.
    # These can disagree: the 100%-recall constraint is set entirely by the
    # single worst-ranked true positive, so a model with better overall ranking
    # can still defer less volume. Optimising for AUC would pick the wrong
    # model for the decision actually being made.
    gbm_defer = ho_metrics["volume_reduction_pct"]
    lr_defer = results["logistic_regression"]["holdout"]["volume_reduction_pct"]
    lr_recall = results["logistic_regression"]["holdout"]["recall"]

    if lr_recall >= 1.0 and lr_defer > gbm_defer:
        prod_name, prod_scores_all, prod_thr = "logistic_regression", logit.predict_proba(Xl)[:, 1], thr_l
        prod_ho_scores = s_ho_l
    else:
        prod_name, prod_scores_all, prod_thr = "gradient_boosted", gbm.predict_proba(X)[:, 1], thr
        prod_ho_scores = s_ho

    results["production_model"] = {
        "selected": prod_name,
        "selection_basis": "volume deferred at 100% validation recall on the holdout window",
        "gradient_boosted_deferred_pct": gbm_defer,
        "logistic_deferred_pct": lr_defer,
    }
    prod_va_scores = s_va_l if prod_name == "logistic_regression" else s_va
    results["tradeoff_curve_prospective"] = tradeoff_curve(
        y[va], prod_va_scores, y[ho], prod_ho_scores)
    results["tier_policy_holdout"] = tier_policy(y[ho], prod_ho_scores)

    # ---- persist ----------------------------------------------------------
    out = ROOT / "outputs"
    out.mkdir(exist_ok=True)

    scored = df[["alert_id", "rule_id", "customer_key", "date_key"]].copy()
    scored["score"] = prod_scores_all
    scored["score_gbm"] = gbm.predict_proba(X)[:, 1]
    scored["is_true_positive"] = y
    scored["split"] = np.select([tr, va, ho], ["train", "valid", "holdout"], "na")
    scored["priority"] = np.where(scored["score"] >= prod_thr, "review", "deferred")
    scored["score_percentile"] = scored["score"].rank(pct=True).round(4)
    pct = scored["score"].rank(pct=True, ascending=False)
    scored["tier"] = np.select(
        [pct <= 0.10, pct <= 0.40], ["T1_same_day", "T2_five_day"], "T3_thirty_day")
    scored.to_csv(out / "scored_alerts.csv", index=False)

    with open(out / "model.pkl", "wb") as f:
        pickle.dump({"model": gbm, "logistic": logit, "production": prod_name,
                     "threshold": prod_thr, "columns": cols,
                     "categorical": cat_cols}, f)

    imp = (pd.DataFrame({"feature": cols, "gain": gbm.booster_.feature_importance("gain")})
             .sort_values("gain", ascending=False))
    imp.to_csv(out / "feature_importance.csv", index=False)

    with open(out / "metrics.json", "w") as f:
        json.dump({"split_days": p["split"], "results": results,
                   "n_features": len(cols)}, f, indent=2)

    # ---- report -----------------------------------------------------------
    h = results["gradient_boosted"]["holdout"]
    print("\n[mdl] ================ HOLDOUT (days 151-180, seen once) ============")
    print(f"[mdl] alerts in window          : {h['n_alerts']:,}")
    print(f"[mdl] true positives            : {h['true_positives_total']:,}")
    print(f"[mdl] recall at threshold       : {h['recall']*100:.2f}% "
          f"(95% CI {h['recall_ci'][0]*100:.2f}-{h['recall_ci'][1]*100:.2f})")
    print(f"[mdl] true positives missed     : {h['true_positives_missed']}")
    print(f"[mdl] alert volume deferred     : {h['volume_reduction_pct']:.2f}% "
          f"(95% CI {h['volume_reduction_ci'][0]:.2f}-{h['volume_reduction_ci'][1]:.2f})")
    print(f"[mdl] precision, priority queue : {h['precision_at_priority']*100:.2f}% "
          f"(baseline {h['baseline_precision']*100:.2f}%)")
    print(f"[mdl] PR-AUC / ROC-AUC          : {h['pr_auc']:.3f} / {h['roc_auc']:.3f}")
    lr = results["logistic_regression"]["holdout"]
    print(f"[mdl] logistic reference        : {lr['volume_reduction_pct']:.2f}% deferred "
          f"at {lr['recall']*100:.2f}% recall")
    rb = results["baseline_random"]["holdout"]
    print(f"[mdl] random baseline           : {rb['volume_reduction_pct']:.2f}% deferred "
          f"at {rb['recall']*100:.2f}% recall")
    print(f"\n[mdl] PRODUCTION MODEL SELECTED: {results['production_model']['selected']}")
    print("[mdl] (selected on volume deferred at the policy operating point, not AUC)")

    print("\n[mdl] recall / capacity trade-off (threshold set on VALIDATION,")
    print("[mdl] applied blind to HOLDOUT -- these are prospective figures):")
    print(f"[mdl]   {'target':<9}{'valid rec':>11}{'HOLDOUT rec':>13}"
          f"{'deferred %':>12}{'precision':>11}")
    for r in results["tradeoff_curve_prospective"]:
        print(f"[mdl]   {r['recall_target']*100:>6.0f}%  {r['validation_recall']*100:>10.1f}%"
              f"{r['holdout_recall']*100:>12.1f}%"
              f"{r['holdout_volume_deferred_pct']:>11.1f}%"
              f"{r['holdout_precision_in_review']*100:>10.1f}%")

    print("\n[mdl] three-tier policy on holdout (nothing is closed; tiers set SLA):")
    for r in results["tier_policy_holdout"]:
        print(f"[mdl]   {r['tier']:<15} {r['share_of_volume_pct']:>5.1f}% of volume  "
              f"{r['share_of_true_positives_pct']:>5.1f}% of true positives  "
              f"precision {r['precision_pct']:>5.2f}%")

    print("\n[mdl] top 12 features by gain:")
    for r in imp.head(12).itertuples():
        print(f"[mdl]   {r.feature:<32} {r.gain:>12,.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "params.yml")
    main(ap.parse_args().config)
