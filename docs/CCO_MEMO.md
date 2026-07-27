# Memorandum

**To:** Chief Compliance Officer; Head of Financial Crime Operations
**From:** Analytics
**Re:** Alert triage model — recommendation and rule set findings
**Classification:** Internal — synthetic pilot data

---

## Recommendation

Adopt risk-based prioritisation of transaction monitoring alerts, and recalibrate
two rules in the current set.

Nothing in this proposal closes an alert. Every alert continues to be worked. The
change is the **order** in which they are worked and the **service standard**
attached to each tier. That distinction is deliberate: a model that suppresses
alerts creates regulatory exposure that no efficiency gain justifies.

---

## What the analysis found

**Alert prioritisation works.** Ranking alerts by modelled risk concentrates
detection heavily at the top of the queue:

| Tier | Share of alert volume | Share of confirmed positives | Precision |
|---|---|---|---|
| Tier 1 — same day | 10% | **81.2%** | 22.9% |
| Tier 2 — five days | 30% | 16.7% | 1.6% |
| Tier 3 — thirty days | 60% | 2.2% | 0.1% |

Baseline precision across all alerts is 2.8%. The top decile runs at 22.9% — an
**8x concentration** of productive work.

**A conservative policy is available with no detection loss.** Setting the
threshold to retain every confirmed positive in the validation period, then
applying it unchanged to a later unseen period, deferred **15.9% of alert volume
at 100% recall**. No confirmed positive was deprioritised.

**More capacity is available, at a price you should set, not us.** The trade
between recall and capacity is a policy decision for this office and the
regulator, not an analytical one. Priced honestly:

| Recall policy | Recall actually achieved | Volume deferred |
|---|---|---|
| 100% | 100.0% | 15.9% |
| 99% | 97.8% | 55.0% |
| 95% | 96.4% | 72.5% |
| 90% | 90.6% | 86.0% |

The step from 100% to 99% is the material one: it more than triples deferrable
volume for a measured cost of roughly 2 percentage points of recall. **We do not
recommend adopting it without regulatory consultation.**

---

## Rule set findings

Three observations independent of the model, each actionable now.

**1. R007 has never fired.** Dormant account reactivation produced zero alerts in
180 days. The control framework documents coverage of dormancy-based typologies
that in practice does not exist. Recommend recalibration or formal retirement —
on paper it is providing assurance it is not delivering.

**2. R002 is expensive and duplicative.** LCTR aggregation avoidance generates
7,643 alerts at 2.55% precision — an estimated **$327,000 per year** in
investigator time — and detects no illicit customer that another rule does not
already catch. Recommend threshold review, not retirement.

**3. R001 is 87-92% redundant.** Near-threshold cash clustering overlaps almost
entirely with R008 and R002. It has the best precision in the set (22.0%), so the
right move is to examine whether R008's threshold can be tightened toward R001's
behaviour rather than running all three.

**One important counter-finding.** R004 (high-risk jurisdiction wires) runs at
2.52% precision and costs an estimated $253,000 per year, which makes it an
obvious efficiency target. It is also the **sole detector** for one illicit
customer. A rule that is the only line of sight onto a typology cannot be retired
at any precision. This is the distinction that efficiency programmes get wrong,
and it is why the recommendation is prioritisation rather than rule removal.

---

## Financial case

| | |
|---|---|
| Estimated annual alert volume | 59,592 |
| Gross annual benefit | $168,000 |
| Annual run cost (platform + model governance) | $43,000 |
| **Net annual benefit** | **$125,000** |
| One-off build | $40,500 |
| **Payback** | **3.9 months** |

Benefit is **capacity redirected, not headcount removed**. Deferred alerts move
to a longer service standard and lighter-touch review; the hours released at the
top of the queue are redirected to enhanced due diligence. The model assumes
deferred alerts still consume 40% of their current effort.

Sensitivity across investigator cost, alert handling time and recall policy is in
`outputs/cost_benefit_sensitivity.csv`. Net benefit ranges from $49,000 to
$981,000 depending mainly on the recall policy chosen — which is why that choice
sits with this office.

---

## Model governance position

- **Production model is logistic regression**, not the gradient boosted
  alternative. The boosted model ranks better on aggregate metrics but performs
  worse at the operating point we actually use. The linear model also produces
  **exact** per-alert attribution — contribution equals coefficient times
  standardised value, summing to the log-odds — rather than an approximation,
  which materially simplifies model validation.
- **Every alert carries reason codes** naming the top three drivers of its rank
  and the strongest mitigating factor.
- **No alert is auto-closed.** The model assigns priority only.
- **Threshold selection is prospective.** Chosen on one time period, applied
  unchanged to a later unseen one.

## Limitations this office should be aware of

1. **This pilot uses synthetic data.** No real transaction data was used.
   Reported performance is an upper bound and will not transfer directly.
2. **Labels are constructed ground truth**, not investigator dispositions. Real
   labels carry analyst-to-analyst inconsistency and survivorship bias.
3. **No model monitoring exists yet.** Drift detection, retraining triggers and
   a rollback path are prerequisites for production, not enhancements.
4. **Customer risk ratings are current, not point-in-time.** Alerts should be
   assessed against the rating in force on the alert date.

## Recommended next steps

1. Validate on a sample of real historical alerts with known dispositions.
2. Run in **shadow mode** for one quarter — model scores recorded, queue order
   unchanged — and compare against actual investigator outcomes.
3. Consult the regulator before adopting any policy below 100% recall.
4. Recalibrate R007 and R002; leave R004 alone.
5. Stand up model monitoring before any production decision.
