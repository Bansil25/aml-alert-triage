"""
Render static preview images of the two headline dashboard pages from the ACTUAL
pipeline outputs. These are not the Power BI report -- they are for the README,
so a reviewer sees the analysis without installing Power BI Desktop.

Everything plotted here comes from outputs/*.csv, so the previews cannot drift
away from the numbers the pipeline actually produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.gridspec import GridSpec

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "previews"
OUT.mkdir(parents=True, exist_ok=True)

INK = "#1a1a2e"
ACCENT = "#c1121f"
AMBER = "#e09f3e"
GREY = "#8d99ae"
GOOD = "#2a9d8f"
BG = "#fbfbfe"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10,
    "axes.edgecolor": "#d9d9e3", "axes.linewidth": 0.8,
    "figure.facecolor": BG, "axes.facecolor": BG,
})


def scorecard_page():
    m = json.loads((ROOT / "outputs" / "metrics.json").read_text())
    r = m["results"]
    eff = pd.read_csv(ROOT / "outputs" / "rule_effectiveness.csv")
    cb = json.loads((ROOT / "outputs" / "cost_benefit.json").read_text())
    tiers = r["tier_policy_holdout"]

    fig = plt.figure(figsize=(13, 8))
    gs = GridSpec(3, 4, figure=fig, height_ratios=[0.7, 1.15, 1.15],
                  hspace=0.55, wspace=0.35,
                  left=0.06, right=0.96, top=0.9, bottom=0.08)
    fig.suptitle("AML Alert Triage  ·  Compliance Scorecard",
                 x=0.06, ha="left", fontsize=18, fontweight="bold", color=INK)
    fig.text(0.06, 0.925, "Holdout window (days 151-180), evaluated once  ·  "
             "synthetic data", fontsize=9.5, color=GREY)

    h = r["gradient_boosted"]["holdout"]
    curve100 = [x for x in r["tradeoff_curve_prospective"]
                if x["recall_target"] == 1.0][0]
    cards = [
        ("Total alerts", f"{h['n_alerts']:,}", GREY),
        ("False positive rate", "94.7%", INK),
        ("Volume deferred\nat 100% recall", f"{curve100['holdout_volume_deferred_pct']:.1f}%", GOOD),
        ("Net annual benefit", f"${cb['result']['net_annual_benefit_cad']/1000:.0f}k", GOOD),
    ]
    for i, (label, val, col) in enumerate(cards):
        ax = fig.add_subplot(gs[0, i]); ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                                   facecolor="white", edgecolor="#e6e6ef", lw=1))
        ax.text(0.5, 0.62, val, ha="center", va="center", fontsize=25,
                fontweight="bold", color=col, transform=ax.transAxes)
        ax.text(0.5, 0.2, label, ha="center", va="center", fontsize=9.5,
                color=GREY, transform=ax.transAxes)

    # Tier concentration
    ax1 = fig.add_subplot(gs[1, :2])
    names = ["Tier 1\nsame day", "Tier 2\nfive day", "Tier 3\nthirty day"]
    vol = [t["share_of_volume_pct"] for t in tiers]
    tp = [t["share_of_true_positives_pct"] for t in tiers]
    x = np.arange(3); w = 0.38
    ax1.bar(x - w/2, vol, w, label="% of alert volume", color=GREY)
    ax1.bar(x + w/2, tp, w, label="% of confirmed positives", color=ACCENT)
    for xi, (v, t) in enumerate(zip(vol, tp)):
        ax1.text(xi - w/2, v + 1.5, f"{v:.0f}%", ha="center", fontsize=9, color=INK)
        ax1.text(xi + w/2, t + 1.5, f"{t:.0f}%", ha="center", fontsize=9,
                 color=ACCENT, fontweight="bold")
    ax1.set_xticks(x); ax1.set_xticklabels(names, fontsize=9)
    ax1.set_ylim(0, 95); ax1.set_title("Detection concentrates at the top of the queue",
                                       fontsize=11, fontweight="bold", color=INK, pad=8)
    ax1.legend(frameon=False, fontsize=8.5, loc="upper center")
    ax1.spines[["top", "right"]].set_visible(False)

    # Rule scatter: volume vs precision, size = cost
    ax2 = fig.add_subplot(gs[1, 2:])
    live = eff[eff.alerts > 0]
    sizes = live.annual_cost_cad / live.annual_cost_cad.max() * 900 + 40
    cols = [ACCENT if p < 3 else (AMBER if p < 8 else GOOD) for p in live.precision_pct]
    ax2.scatter(live.alerts, live.precision_pct, s=sizes, c=cols, alpha=0.7,
                edgecolor="white", lw=1.2)
    for _, row in live.iterrows():
        ax2.annotate(row.rule_id, (row.alerts, row.precision_pct),
                     fontsize=8, ha="center", va="center", color=INK)
    ax2.set_xlabel("alert volume", fontsize=9)
    ax2.set_ylabel("precision %", fontsize=9)
    ax2.set_title("Rule efficiency  (bubble = annual cost)", fontsize=11,
                  fontweight="bold", color=INK, pad=8)
    ax2.spines[["top", "right"]].set_visible(False)

    # Rule table
    ax3 = fig.add_subplot(gs[2, :]); ax3.axis("off")
    ax3.set_title("Rule effectiveness and recommendation", fontsize=11,
                  fontweight="bold", color=INK, loc="left", pad=2)
    show = eff[["rule_id", "alerts", "precision_pct", "sole_detector_for",
                "annual_cost_cad", "recommendation"]].copy()
    show.columns = ["Rule", "Alerts", "Prec %", "Sole det.", "Annual $", "Recommendation"]
    show["Annual $"] = show["Annual $"].map(lambda v: f"${v/1000:.0f}k")
    show["Recommendation"] = show["Recommendation"].str.split(" - ").str[0]
    tbl = ax3.table(cellText=show.values, colLabels=show.columns,
                    cellLoc="center", loc="center",
                    colWidths=[0.08, 0.10, 0.10, 0.11, 0.11, 0.32])
    tbl.auto_set_font_size(False); tbl.set_fontsize(8.5); tbl.scale(1, 1.5)
    for (ri, ci), cell in tbl.get_celld().items():
        cell.set_edgecolor("#e6e6ef")
        if ri == 0:
            cell.set_facecolor(INK); cell.set_text_props(color="white", fontweight="bold")
        else:
            rec = show.iloc[ri-1]["Recommendation"]
            if rec == "RETIRE":
                cell.set_facecolor("#fde8e8")
            elif rec == "RECALIBRATE":
                cell.set_facecolor("#fdf3e0")
            else:
                cell.set_facecolor("white")

    fig.savefig(OUT / "page2_compliance_scorecard.png", dpi=130,
                facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    print(f"[img] wrote {OUT / 'page2_compliance_scorecard.png'}")


def model_page():
    m = json.loads((ROOT / "outputs" / "metrics.json").read_text())
    r = m["results"]
    imp = pd.read_csv(ROOT / "outputs" / "feature_importance.csv").head(12)

    fig = plt.figure(figsize=(13, 6.5))
    gs = GridSpec(1, 2, figure=fig, wspace=0.3, left=0.07, right=0.96,
                  top=0.86, bottom=0.12)
    fig.suptitle("AML Alert Triage  ·  Model Performance",
                 x=0.07, ha="left", fontsize=18, fontweight="bold", color=INK)
    fig.text(0.07, 0.905, "Thresholds set on validation, applied blind to holdout  "
             "·  prospective figures", fontsize=9.5, color=GREY)

    ax1 = fig.add_subplot(gs[0, 0])
    curve = r["tradeoff_curve_prospective"]
    defer = [c["holdout_volume_deferred_pct"] for c in curve]
    recall = [c["holdout_recall"] * 100 for c in curve]
    labels = [f"{int(c['recall_target']*100)}%" for c in curve]
    ax1.plot(defer, recall, "-o", color=ACCENT, lw=2, markersize=8, zorder=3)
    for d, rc, lb in zip(defer, recall, labels):
        ax1.annotate(lb, (d, rc), textcoords="offset points", xytext=(6, -12),
                     fontsize=8.5, color=INK)
    ax1.axhline(100, color=GOOD, ls="--", lw=1, alpha=0.6)
    ax1.annotate("no positives missed", (defer[0]+2, 100.3), fontsize=8, color=GOOD)
    ax1.set_xlabel("alert volume deferred %", fontsize=9.5)
    ax1.set_ylabel("holdout recall achieved %", fontsize=9.5)
    ax1.set_title("Recall / capacity trade-off  (policy is the CCO's choice)",
                  fontsize=11, fontweight="bold", color=INK, pad=8)
    ax1.set_ylim(88, 101); ax1.grid(alpha=0.25)
    ax1.spines[["top", "right"]].set_visible(False)

    ax2 = fig.add_subplot(gs[0, 1])
    imp = imp.iloc[::-1]
    ax2.barh(range(len(imp)), imp.gain, color=GREY)
    ax2.barh([len(imp)-1, len(imp)-2], imp.gain.iloc[[-1, -2]], color=ACCENT)
    labs = [f.replace("_", " ")[:34] for f in imp.feature]
    ax2.set_yticks(range(len(imp))); ax2.set_yticklabels(labs, fontsize=8.5)
    ax2.set_xlabel("gain", fontsize=9.5)
    ax2.set_title("Top model drivers", fontsize=11, fontweight="bold",
                  color=INK, pad=8)
    ax2.spines[["top", "right"]].set_visible(False)

    fig.text(0.07, 0.02, "Synthetic data. Reported performance is an upper bound "
             "on a simplified problem and would not transfer directly to production.",
             fontsize=8, color=GREY, style="italic")
    fig.savefig(OUT / "page3_model_performance.png", dpi=130,
                facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    print(f"[img] wrote {OUT / 'page3_model_performance.png'}")


if __name__ == "__main__":
    scorecard_page()
    model_page()
