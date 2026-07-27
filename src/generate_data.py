"""
Synthetic transaction data generator for AML alert triage.

WHY SYNTHETIC: no real AML transaction data is publicly available, for obvious
reasons. Rather than hand-wave that limitation, this generator is seeded,
version-controlled, and every distributional assumption is stated in
docs/SYNTHETIC_DATA_ASSUMPTIONS.md with its rationale.

The five illicit typologies implemented below are modelled on money laundering
methods described in FINTRAC's published operational alerts and typology
guidance (structuring below the LCTR threshold, funnel accounts, rapid movement
of funds, third-party cash deposits into business accounts, and layering via
round-value transfers). The *behavioural signatures* are modelled; no real case
data was used or is reproduced.

Output: data/customers.csv, data/accounts.csv, data/transactions.csv
Ground truth (is_illicit_*) is written to a SEPARATE file so that it cannot be
accidentally joined into the feature pipeline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Jurisdictions. "High risk" here reflects the general shape of FATF-style
# monitoring lists rather than any current official list, which changes often.
LOW_RISK_COUNTRIES = ["CA", "US", "GB", "FR", "DE", "AU", "JP", "NL", "CH"]
HIGH_RISK_COUNTRIES = ["XA", "XB", "XC", "XD"]  # deliberately fictional codes

CHANNELS = ["cash", "wire", "emt", "cheque", "pos", "preauth"]

PERSONAL_OCCUPATIONS = [
    "salaried_employee", "retired", "student", "self_employed",
    "healthcare_worker", "tradesperson", "public_sector",
]
BUSINESS_TYPES = [
    "restaurant", "convenience_retail", "construction", "consulting",
    "auto_dealer", "money_services", "import_export", "salon", "logistics",
]
# Business types where high cash intensity is legitimate and expected. Without
# this, the model would simply learn "cash = bad", which is wrong and is exactly
# the kind of shortcut that produces unusable false positives in production.
CASH_INTENSIVE = {"restaurant", "convenience_retail", "salon", "auto_dealer"}


@dataclass
class Config:
    seed: int
    n_customers: int
    start_date: pd.Timestamp
    n_days: int
    illicit_rate: float
    lctr_threshold: float
    band_low: float
    band_high: float

    @classmethod
    def load(cls, path: Path) -> "Config":
        p = yaml.safe_load(path.read_text())
        return cls(
            seed=p["seed"],
            n_customers=p["simulation"]["n_customers"],
            start_date=pd.Timestamp(p["simulation"]["start_date"]),
            n_days=p["simulation"]["n_days"],
            illicit_rate=p["simulation"]["illicit_customer_rate"],
            lctr_threshold=p["thresholds"]["lctr_threshold"],
            band_low=p["thresholds"]["structuring_band_low"],
            band_high=p["thresholds"]["structuring_band_high"],
        )


# ---------------------------------------------------------------------------
# Customers and accounts
# ---------------------------------------------------------------------------

def build_customers(cfg: Config, rng: np.random.Generator) -> pd.DataFrame:
    n = cfg.n_customers
    is_business = rng.random(n) < 0.22

    occ = np.where(
        is_business,
        rng.choice(BUSINESS_TYPES, size=n),
        rng.choice(PERSONAL_OCCUPATIONS, size=n),
    )

    # Tenure in months, long tail of established customers.
    tenure = np.clip(rng.gamma(shape=2.2, scale=28, size=n), 1, 420).astype(int)

    # KYC risk rating assigned at onboarding. Correlated with, but far from
    # determinative of, actual illicit behaviour -- if it were determinative
    # there would be no analytics problem to solve.
    risk_roll = rng.random(n)
    risk_rating = np.where(risk_roll < 0.72, "low",
                    np.where(risk_roll < 0.94, "medium", "high"))

    pep = rng.random(n) < 0.015
    # Expected annual income / revenue, drives what "normal" volume looks like.
    income = np.where(
        is_business,
        rng.lognormal(mean=12.6, sigma=0.85, size=n),
        rng.lognormal(mean=11.0, sigma=0.55, size=n),
    ).round(-2)

    return pd.DataFrame({
        "customer_id": [f"C{i:06d}" for i in range(1, n + 1)],
        "customer_segment": np.where(is_business, "business", "personal"),
        "occupation_or_business_type": occ,
        "tenure_months": tenure,
        "kyc_risk_rating": risk_rating,
        "is_pep": pep,
        "declared_annual_income_cad": income,
        "home_province": rng.choice(
            ["ON", "QC", "BC", "AB", "MB", "NS", "SK"],
            size=n, p=[0.40, 0.22, 0.15, 0.13, 0.04, 0.03, 0.03]),
        "onboarding_channel": rng.choice(
            ["branch", "online", "mobile", "broker"],
            size=n, p=[0.42, 0.30, 0.22, 0.06]),
    })


def build_accounts(customers: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for cid, seg in zip(customers["customer_id"], customers["customer_segment"]):
        n_acc = 1 + (rng.random() < (0.45 if seg == "business" else 0.30))
        for k in range(int(n_acc)):
            rows.append({
                "account_id": f"A{len(rows) + 1:07d}",
                "customer_id": cid,
                "account_type": ("business_chequing" if seg == "business"
                                 else rng.choice(["chequing", "savings"], p=[0.75, 0.25])),
                "opened_days_ago": int(rng.integers(30, 3000)),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Baseline (legitimate) transaction behaviour
# ---------------------------------------------------------------------------

def _monthly_volume(row, rng: np.random.Generator) -> float:
    """Expected monthly throughput, anchored to declared income."""
    base = row["declared_annual_income_cad"] / 12.0
    return float(base * rng.uniform(0.55, 1.35))


def build_baseline_transactions(customers, accounts, cfg, rng) -> pd.DataFrame:
    """Vectorised-ish generation of ordinary customer activity."""
    acc_by_cust = accounts.groupby("customer_id")["account_id"].apply(list).to_dict()
    frames = []

    for row in customers.to_dict("records"):
        cid = row["customer_id"]
        accs = acc_by_cust[cid]
        is_biz = row["customer_segment"] == "business"
        cash_heavy = row["occupation_or_business_type"] in CASH_INTENSIVE

        monthly = _monthly_volume(row, rng)
        # Transaction count scales sublinearly with volume.
        n_txn = int(np.clip(rng.poisson(28 if is_biz else 16) * (cfg.n_days / 30), 8, 4000))

        days = rng.integers(0, cfg.n_days, size=n_txn)
        # Weekday bias: fewer transactions on weekends.
        dow = (cfg.start_date + pd.to_timedelta(days, unit="D")).dayofweek
        keep = ~((dow >= 5) & (rng.random(n_txn) < 0.55))
        days = days[keep]
        n_txn = len(days)
        if n_txn == 0:
            continue

        # Channel mix depends on segment and cash intensity.
        if cash_heavy:
            p = [0.34, 0.04, 0.10, 0.12, 0.36, 0.04]
        elif is_biz:
            p = [0.07, 0.14, 0.16, 0.20, 0.35, 0.08]
        else:
            p = [0.09, 0.03, 0.22, 0.06, 0.52, 0.08]
        channel = rng.choice(CHANNELS, size=n_txn, p=p)

        # Amounts: lognormal, scaled so the monthly total lands near `monthly`.
        raw = rng.lognormal(mean=0.0, sigma=1.1, size=n_txn)
        scale = (monthly * (cfg.n_days / 30)) / raw.sum()
        amount = np.round(raw * scale, 2)
        amount = np.clip(amount, 5, None)

        # Direction: businesses take in more than they push out, individuals
        # receive payroll (few large credits) and spend (many small debits).
        direction = np.where(rng.random(n_txn) < (0.55 if is_biz else 0.25),
                             "credit", "debit")

        # Counterparty country: overwhelmingly domestic.
        intl = rng.random(n_txn) < (0.06 if is_biz else 0.02)
        country = np.where(
            intl,
            rng.choice(LOW_RISK_COUNTRIES + HIGH_RISK_COUNTRIES, size=n_txn,
                       p=[0.10] * 9 + [0.025] * 4),
            "CA")

        frames.append(pd.DataFrame({
            "account_id": rng.choice(accs, size=n_txn),
            "customer_id": cid,
            "day_index": days,
            "hour": rng.integers(7, 21, size=n_txn),
            "amount_cad": amount,
            "direction": direction,
            "channel": channel,
            "counterparty_country": country,
            "counterparty_id": [f"P{int(x):07d}" for x in rng.integers(1, 60000, size=n_txn)],
            "is_illicit_txn": False,
            "typology": "",
        }))

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Illicit typologies
# ---------------------------------------------------------------------------

def _mk(account_id, customer_id, day, hour, amount, direction, channel,
        country, counterparty, typology):
    return {
        "account_id": account_id, "customer_id": customer_id, "day_index": day,
        "hour": hour, "amount_cad": round(float(amount), 2), "direction": direction,
        "channel": channel, "counterparty_country": country,
        "counterparty_id": counterparty, "is_illicit_txn": True, "typology": typology,
    }


def inject_typologies(customers, accounts, cfg, rng):
    """Select illicit customers and overlay laundering behaviour on top of their
    ordinary activity. Illicit customers keep their legitimate transactions --
    real laundering hides inside normal activity, it does not replace it."""
    n_illicit = max(4, int(cfg.n_customers * cfg.illicit_rate))
    # Mildly weighted toward higher KYC risk, but deliberately NOT deterministic:
    # roughly a third of illicit customers are rated low risk, which is what
    # makes the analytics problem non-trivial.
    w = customers["kyc_risk_rating"].map({"low": 0.6, "medium": 1.4, "high": 2.6}).values
    w = w / w.sum()
    chosen = rng.choice(customers.index, size=n_illicit, replace=False, p=w)

    acc_by_cust = accounts.groupby("customer_id")["account_id"].apply(list).to_dict()
    typologies = ["structuring", "funnel_account", "rapid_movement",
                  "third_party_cash", "round_value_layering"]

    rows, labels = [], []
    for idx in chosen:
        cust = customers.loc[idx]
        cid = cust["customer_id"]
        accs = acc_by_cust[cid]
        typ = rng.choice(typologies)
        # Campaign window: illicit activity is bursty, not continuous.
        start = int(rng.integers(5, cfg.n_days - 35))
        labels.append({"customer_id": cid, "typology": typ,
                       "campaign_start_day": start, "campaign_end_day": start + 30})

        if typ == "structuring":
            # Repeated cash deposits parked just under the LCTR threshold.
            n_events = int(rng.integers(6, 16))
            for _ in range(n_events):
                d = int(np.clip(start + rng.integers(0, 30), 0, cfg.n_days - 1))
                for _ in range(int(rng.integers(2, 4))):
                    rows.append(_mk(
                        rng.choice(accs), cid, d, int(rng.integers(9, 19)),
                        rng.uniform(cfg.band_low, cfg.band_high), "credit", "cash",
                        "CA", f"P{int(rng.integers(1, 60000)):07d}", typ))

        elif typ == "funnel_account":
            # Many small third-party deposits in, lump withdrawal out elsewhere.
            n_dep = int(rng.integers(30, 70))
            for _ in range(n_dep):
                d = int(np.clip(start + rng.integers(0, 28), 0, cfg.n_days - 1))
                rows.append(_mk(
                    accs[0], cid, d, int(rng.integers(8, 20)),
                    rng.uniform(400, 2800), "credit", "cash", "CA",
                    f"P{int(rng.integers(1, 60000)):07d}", typ))
            for _ in range(int(rng.integers(3, 8))):
                d = int(np.clip(start + rng.integers(3, 30), 0, cfg.n_days - 1))
                rows.append(_mk(
                    accs[0], cid, d, int(rng.integers(9, 17)),
                    rng.uniform(18000, 55000), "debit", "wire",
                    rng.choice(HIGH_RISK_COUNTRIES),
                    f"P{int(rng.integers(1, 60000)):07d}", typ))

        elif typ == "rapid_movement":
            # Funds in, near-identical amount out within 1-3 days. Low retention.
            for _ in range(int(rng.integers(5, 12))):
                d = int(np.clip(start + rng.integers(0, 27), 0, cfg.n_days - 2))
                amt = rng.uniform(15000, 90000)
                cp = f"P{int(rng.integers(1, 60000)):07d}"
                rows.append(_mk(accs[0], cid, d, int(rng.integers(9, 16)),
                                amt, "credit", "wire",
                                rng.choice(LOW_RISK_COUNTRIES), cp, typ))
                rows.append(_mk(accs[0], cid, d + int(rng.integers(0, 3)),
                                int(rng.integers(9, 18)),
                                amt * rng.uniform(0.88, 0.97), "debit", "wire",
                                rng.choice(HIGH_RISK_COUNTRIES),
                                f"P{int(rng.integers(1, 60000)):07d}", typ))

        elif typ == "third_party_cash":
            # Cash deposits into a business account far above what the declared
            # business type and revenue would support.
            for _ in range(int(rng.integers(20, 45))):
                d = int(np.clip(start + rng.integers(0, 30), 0, cfg.n_days - 1))
                rows.append(_mk(
                    rng.choice(accs), cid, d, int(rng.integers(8, 20)),
                    rng.uniform(3000, 14000), "credit", "cash", "CA",
                    f"P{int(rng.integers(1, 60000)):07d}", typ))

        else:  # round_value_layering
            # Chains of round-value transfers between a small counterparty ring.
            ring = [f"P{int(rng.integers(1, 60000)):07d}" for _ in range(4)]
            for _ in range(int(rng.integers(12, 30))):
                d = int(np.clip(start + rng.integers(0, 30), 0, cfg.n_days - 1))
                amt = float(rng.choice([5000, 10000, 15000, 20000, 25000, 50000]))
                rows.append(_mk(
                    rng.choice(accs), cid, d, int(rng.integers(9, 18)), amt,
                    rng.choice(["credit", "debit"]),
                    rng.choice(["wire", "emt"]),
                    rng.choice(LOW_RISK_COUNTRIES + HIGH_RISK_COUNTRIES),
                    rng.choice(ring), typ))

    return pd.DataFrame(rows), pd.DataFrame(labels)


# ---------------------------------------------------------------------------
# Legitimate behaviour that superficially resembles laundering
# ---------------------------------------------------------------------------

def inject_legitimate_noise(customers, accounts, cfg, rng):
    """THIS FUNCTION IS THE POINT OF THE PROJECT.

    False positives in transaction monitoring do not come from random noise.
    They come from lawful customers whose ordinary behaviour trips a rule that
    was written to catch something else: the restaurant that deposits its
    weekend takings in three envelopes, the family sending remittances to a
    monitored corridor, the homebuyer whose down payment lands and leaves the
    same week.

    Without this layer the rules are ~90% precise, the industry premise
    collapses, and there is nothing for a triage model to do. With it, the
    alert population looks like the real thing: overwhelmingly lawful, with
    genuine signal buried inside it.

    Every pattern below is lawful activity.

    CRITICAL: this layer is applied to EVERY customer, including the illicit
    ones. An earlier version excluded illicit customers, which quietly made the
    *absence* of ordinary lawful behaviour a perfect predictor of the label --
    ROC-AUC came out at 1.000, which is how the defect was caught. Real
    launderers also pay rent, send remittances and buy houses; their illicit
    activity hides inside ordinary life rather than replacing it. See
    docs/WHAT_DIDNT_WORK.md.
    """
    acc_by_cust = accounts.groupby("customer_id")["account_id"].apply(list).to_dict()
    rows = []

    for row in customers.to_dict("records"):
        cid = row["customer_id"]
        accs = acc_by_cust[cid]
        biz_type = row["occupation_or_business_type"]
        is_biz = row["customer_segment"] == "business"

        # --- 1. Cash-intensive businesses banking their daily takings -------
        # A busy restaurant or convenience store deposits real cash, often in
        # several tranches, often landing in the 8,000-9,999 band by accident
        # of how much it sold. This is the single largest FP source in practice.
        if biz_type in CASH_INTENSIVE and rng.random() < 0.75:
            n_dep_days = int(rng.integers(40, 130))
            for _ in range(n_dep_days):
                d = int(rng.integers(0, cfg.n_days))
                for _ in range(int(rng.integers(1, 4))):
                    amt = rng.lognormal(mean=8.5, sigma=0.75)
                    rows.append(_mk(rng.choice(accs), cid, d,
                                    int(rng.integers(8, 21)),
                                    np.clip(amt, 200, 25000), "credit", "cash",
                                    "CA", f"P{int(rng.integers(1, 60000)):07d}", ""))

        # --- 2. Remittance corridors ----------------------------------------
        # Sending money to family abroad is lawful and extremely common. Some
        # destination countries sit on monitored lists.
        if rng.random() < 0.14:
            dest = rng.choice(HIGH_RISK_COUNTRIES)
            for _ in range(int(rng.integers(3, 20))):
                d = int(rng.integers(0, cfg.n_days))
                rows.append(_mk(accs[0], cid, d, int(rng.integers(8, 21)),
                                rng.uniform(4500, 22000), "debit", "wire",
                                dest, f"P{int(rng.integers(1, 60000)):07d}", ""))

        # --- 3. Lumpy life events (pass-through that is entirely innocent) ---
        # Property completion, vehicle sale, inheritance, insurance settlement:
        # a large sum arrives and leaves within days because that was the point.
        if rng.random() < 0.16:
            for _ in range(int(rng.integers(1, 4))):
                d = int(rng.integers(0, cfg.n_days - 4))
                amt = rng.uniform(20000, 220000)
                rows.append(_mk(accs[0], cid, d, int(rng.integers(9, 17)), amt,
                                "credit", rng.choice(["wire", "cheque"]),
                                "CA", f"P{int(rng.integers(1, 60000)):07d}", ""))
                # Money moves straight on to the lawyer, dealer or vendor.
                n_out = int(rng.integers(1, 4))
                for _ in range(n_out):
                    rows.append(_mk(accs[0], cid, d + int(rng.integers(0, 3)),
                                    int(rng.integers(9, 18)),
                                    amt * rng.uniform(0.30, 0.48) / max(n_out - 1, 1)
                                    if n_out > 1 else amt * rng.uniform(0.86, 0.97),
                                    "debit", rng.choice(["wire", "cheque", "emt"]),
                                    "CA", f"P{int(rng.integers(1, 60000)):07d}", ""))

        # --- 4. Recurring round-value transfers ------------------------------
        # Rent, loan repayments, transfers to one's own investment account.
        # Humans like round numbers; the rule does not know that.
        if rng.random() < 0.22:
            counterpart = f"P{int(rng.integers(1, 60000)):07d}"
            amt = float(rng.choice([5000, 6000, 8000, 10000, 12000, 15000, 20000]))
            start = int(rng.integers(0, 20))
            for k in range(int(rng.integers(4, 12))):
                d = min(start + k * int(rng.integers(6, 9)), cfg.n_days - 1)
                rows.append(_mk(accs[0], cid, d, int(rng.integers(8, 20)), amt,
                                "debit", rng.choice(["wire", "emt"]),
                                "CA", counterpart, ""))

        # --- 5. Seasonal / project-driven revenue spikes ---------------------
        # Construction and logistics bill in lumps; retail has Q4. A 30-day
        # volume spike over baseline is normal business, not layering.
        if is_biz and rng.random() < 0.30:
            peak = int(rng.integers(60, cfg.n_days - 30))
            base = row["declared_annual_income_cad"] / 12.0
            for _ in range(int(rng.integers(25, 90))):
                d = int(np.clip(peak + rng.integers(0, 30), 0, cfg.n_days - 1))
                rows.append(_mk(rng.choice(accs), cid, d, int(rng.integers(8, 20)),
                                np.clip(rng.lognormal(np.log(max(base / 8, 500)), 0.8),
                                        100, 150000),
                                "credit", rng.choice(["wire", "cheque", "emt"]),
                                "CA", f"P{int(rng.integers(1, 60000)):07d}", ""))

        # --- 6. Dormant-then-active: seasonal or secondary accounts ----------
        if len(accs) > 1 and rng.random() < 0.12:
            d = int(rng.integers(70, cfg.n_days - 8))
            for _ in range(int(rng.integers(2, 6))):
                rows.append(_mk(accs[-1], cid, d + int(rng.integers(0, 6)),
                                int(rng.integers(9, 19)),
                                rng.uniform(8000, 40000),
                                rng.choice(["credit", "debit"]),
                                rng.choice(["wire", "emt", "cheque"]),
                                "CA", f"P{int(rng.integers(1, 60000)):07d}", ""))

    df = pd.DataFrame(rows)
    df["is_illicit_txn"] = False   # every row here is lawful, by construction
    return df


# ---------------------------------------------------------------------------
# Hard negatives
# ---------------------------------------------------------------------------

def inject_hard_negatives(customers, accounts, cfg, rng, illicit_ids, rate=0.035):
    """Lawful customers whose behaviour is shaped like a typology.

    These are the alerts that actually consume investigator time. A cash
    business that splits deposits because its insurance policy caps overnight
    cash holdings produces the same signature as structuring. An import/export
    firm consolidating supplier payments produces the same signature as a
    funnel account. A currency-exchange bureau moves round values to monitored
    corridors all day long, entirely lawfully.

    Without hard negatives the two populations are close to linearly separable
    and the model reports a performance it could never achieve on real data
    (see docs/WHAT_DIDNT_WORK.md -- the first version scored ROC-AUC 0.998).
    These customers are NOT labelled illicit, because they are not.
    """
    acc_by_cust = accounts.groupby("customer_id")["account_id"].apply(list).to_dict()
    pool = customers[~customers["customer_id"].isin(illicit_ids)]
    n = int(len(pool) * rate)
    chosen = rng.choice(pool.index, size=n, replace=False)

    rows = []
    for idx in chosen:
        cust = customers.loc[idx]
        cid = cust["customer_id"]
        accs = acc_by_cust[cid]
        start = int(rng.integers(5, cfg.n_days - 35))
        shape = rng.choice(["split_deposits", "consolidation", "corridor_volume",
                            "high_turnover"])

        if shape == "split_deposits":
            # Cash-handling limits, insurance caps, or simply several tills.
            for _ in range(int(rng.integers(5, 14))):
                d = int(np.clip(start + rng.integers(0, 30), 0, cfg.n_days - 1))
                for _ in range(int(rng.integers(2, 4))):
                    rows.append(_mk(rng.choice(accs), cid, d, int(rng.integers(9, 20)),
                                    rng.uniform(cfg.band_low, cfg.band_high),
                                    "credit", "cash", "CA",
                                    f"P{int(rng.integers(1, 60000)):07d}", ""))

        elif shape == "consolidation":
            # Many inbound customer payments, consolidated outbound to suppliers.
            for _ in range(int(rng.integers(25, 60))):
                d = int(np.clip(start + rng.integers(0, 28), 0, cfg.n_days - 1))
                rows.append(_mk(accs[0], cid, d, int(rng.integers(8, 20)),
                                rng.uniform(400, 3000), "credit",
                                rng.choice(["cash", "emt"]), "CA",
                                f"P{int(rng.integers(1, 60000)):07d}", ""))
            for _ in range(int(rng.integers(3, 8))):
                d = int(np.clip(start + rng.integers(3, 30), 0, cfg.n_days - 1))
                rows.append(_mk(accs[0], cid, d, int(rng.integers(9, 18)),
                                rng.uniform(15000, 50000), "debit", "wire",
                                rng.choice(HIGH_RISK_COUNTRIES + LOW_RISK_COUNTRIES),
                                f"P{int(rng.integers(1, 60000)):07d}", ""))

        elif shape == "corridor_volume":
            # A money services business or importer with sustained lawful
            # exposure to a monitored jurisdiction.
            dest = rng.choice(HIGH_RISK_COUNTRIES)
            for _ in range(int(rng.integers(15, 45))):
                d = int(np.clip(start + rng.integers(0, 30), 0, cfg.n_days - 1))
                amt = float(rng.choice([5000, 10000, 15000, 20000, 25000]))
                rows.append(_mk(rng.choice(accs), cid, d, int(rng.integers(8, 20)),
                                amt, rng.choice(["credit", "debit"]),
                                rng.choice(["wire", "emt"]), dest,
                                f"P{int(rng.integers(1, 60000)):07d}", ""))

        else:  # high_turnover
            # Payroll bureau or agency: money lands and leaves within days,
            # by design, at scale.
            for _ in range(int(rng.integers(6, 15))):
                d = int(np.clip(start + rng.integers(0, 27), 0, cfg.n_days - 2))
                amt = rng.uniform(15000, 80000)
                rows.append(_mk(accs[0], cid, d, int(rng.integers(9, 16)), amt,
                                "credit", "wire", rng.choice(LOW_RISK_COUNTRIES),
                                f"P{int(rng.integers(1, 60000)):07d}", ""))
                rows.append(_mk(accs[0], cid, d + int(rng.integers(0, 3)),
                                int(rng.integers(9, 18)), amt * rng.uniform(0.85, 0.96),
                                "debit", rng.choice(["wire", "emt"]), "CA",
                                f"P{int(rng.integers(1, 60000)):07d}", ""))

    df = pd.DataFrame(rows)
    df["is_illicit_txn"] = False
    return df, n


# ---------------------------------------------------------------------------
# Data quality degradation
# ---------------------------------------------------------------------------

def degrade(txn: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Introduce realistic upstream data defects. A pipeline that only works on
    clean data is not a pipeline. These are the defects the DQ layer must catch."""
    df = txn.copy()

    # 1. Duplicate records from a replayed source feed (~0.4%).
    dupes = df.sample(frac=0.004, random_state=int(rng.integers(1e6)))
    df = pd.concat([df, dupes], ignore_index=True)

    # 2. Missing counterparty country on some wires.
    mask = (df["channel"] == "wire") & (rng.random(len(df)) < 0.03)
    df.loc[mask, "counterparty_country"] = None

    # 3. Inconsistent country casing / whitespace from a legacy feed.
    mask = rng.random(len(df)) < 0.02
    df.loc[mask, "counterparty_country"] = (
        df.loc[mask, "counterparty_country"].astype(str).str.lower() + " ")

    # 4. A handful of orphan account_ids (referential integrity break).
    orphan_idx = df.sample(n=40, random_state=int(rng.integers(1e6))).index
    df.loc[orphan_idx, "account_id"] = "A9999999"

    # 5. Negative amounts on a small number of reversals mis-signed upstream.
    mask = rng.random(len(df)) < 0.001
    df.loc[mask, "amount_cad"] = -df.loc[mask, "amount_cad"].abs()

    return df


# ---------------------------------------------------------------------------

def main(out_dir: Path, cfg_path: Path) -> None:
    cfg = Config.load(cfg_path)
    rng = np.random.default_rng(cfg.seed)

    print(f"[gen] seed={cfg.seed}  customers={cfg.n_customers}  days={cfg.n_days}")
    customers = build_customers(cfg, rng)
    accounts = build_accounts(customers, rng)
    print(f"[gen] {len(customers):,} customers / {len(accounts):,} accounts")

    baseline = build_baseline_transactions(customers, accounts, cfg, rng)
    print(f"[gen] {len(baseline):,} baseline transactions")

    illicit, labels = inject_typologies(customers, accounts, cfg, rng)
    print(f"[gen] {len(labels):,} illicit customers, {len(illicit):,} illicit transactions")

    noise = inject_legitimate_noise(customers, accounts, cfg, rng)
    print(f"[gen] {len(noise):,} legitimate-but-alert-triggering transactions")

    hard, n_hard = inject_hard_negatives(
        customers, accounts, cfg, rng, set(labels["customer_id"]))
    print(f"[gen] {n_hard:,} hard-negative customers, {len(hard):,} transactions")

    txn = pd.concat([baseline, illicit, noise, hard], ignore_index=True)
    txn["txn_date"] = cfg.start_date + pd.to_timedelta(txn["day_index"], unit="D")
    txn["txn_ts"] = txn["txn_date"] + pd.to_timedelta(txn["hour"], unit="h")
    txn = txn.sort_values("txn_ts").reset_index(drop=True)

    # txn_id is assigned BEFORE degradation so that duplicated rows carry the
    # SAME id -- that is what makes them duplicates rather than distinct records,
    # and it is what the dedup logic in 02_load_clean.sql is tested against.
    txn.insert(0, "txn_id", [f"T{i:09d}" for i in range(1, len(txn) + 1)])
    txn = degrade(txn, rng)

    out_dir.mkdir(parents=True, exist_ok=True)
    customers.to_csv(out_dir / "customers.csv", index=False)
    accounts.to_csv(out_dir / "accounts.csv", index=False)

    # Ground truth is written separately and is NEVER joined in the feature
    # pipeline. It is used only to label alerts and to score the model.
    txn.drop(columns=["is_illicit_txn", "typology"]).to_csv(
        out_dir / "transactions.csv", index=False)
    (txn[["txn_id", "customer_id", "is_illicit_txn", "typology"]]
        .drop_duplicates(subset="txn_id")
        .to_csv(out_dir / "_ground_truth_txn.csv", index=False))
    labels.to_csv(out_dir / "_ground_truth_customers.csv", index=False)

    print(f"[gen] wrote {len(txn):,} transactions to {out_dir}")
    print(f"[gen] illicit share of transactions: "
          f"{txn['is_illicit_txn'].mean() * 100:.3f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "data")
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "params.yml")
    main(ap.parse_args().out, ap.parse_args().config)
