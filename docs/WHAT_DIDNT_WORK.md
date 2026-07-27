# What didn't work

Every number in this repository is the product of at least one earlier version
that was wrong. This file records the failures, because a project write-up with
no failures in it is either incurious or dishonest, and because two of the
defects below are the kind that survive into production and destroy a model's
credibility six months after deployment.

---

## 1. The model scored ROC-AUC 1.000, and I nearly believed it

**Symptom.** The first complete run reported ROC-AUC 1.000, PR-AUC 0.993, and
94% of alert volume deferrable at 100% recall. Every one of those numbers was
worthless.

**Cause.** The generator applied its legitimate-behaviour layer only to
customers *not* engaged in illicit activity:

```python
clean = customers[~customers["customer_id"].isin(illicit_ids)]
for row in clean.to_dict("records"):
```

The consequence is subtle and total. Illicit customers had no remittance
corridors, no house purchases, no recurring rent payments, no seasonal revenue.
The *absence* of ordinary lawful behaviour became a perfect predictor of the
label. The model was not learning to detect laundering; it was learning to
detect which population a customer had been drawn from.

**Fix.** Apply the legitimate layer to every customer. Real launderers also pay
rent and send money home; illicit activity hides inside ordinary life rather
than replacing it.

**Result.** Volume deferrable at 100% recall fell from 94% to 33%.

**What I take from it.** A metric that looks like a triumph is a bug report
until proven otherwise. Published AML models operate around 0.75-0.90 ROC-AUC;
anything near 1.0 in this domain means the label has leaked. The instinct to
interrogate a good result is worth more than the modelling.

---

## 2. Even after the fix, the populations were still too separable

**Symptom.** ROC-AUC 0.985, PR-AUC 0.694. Better, still not believable.

**Cause.** No lawful customer in the simulation ever produced *typology-shaped*
behaviour. Nobody split deposits for a legitimate reason. Nobody ran sustained
lawful volume through a monitored corridor. The two populations were nearly
linearly separable because the generator had never been asked to make them
overlap.

**Fix.** Added `inject_hard_negatives`: 138 lawful customers whose behaviour is
drawn from typology-shaped templates. A cash business splitting deposits because
its insurance caps overnight holdings. A money services business with sustained
lawful corridor exposure. A payroll bureau whose money lands and leaves within
days by design.

**Result.** PR-AUC fell from 0.951 to 0.694 on the tree model. That is the honest
number, and it is roughly a 25x lift over the 2.82% base rate.

**What I take from it.** Hard negatives are not an adversarial nicety; they are
the population that actually consumes investigator time. A synthetic generator
that omits them is not modelling the problem.

---

## 3. The reconciliation control caught a bug in my own reconciliation

**Symptom.** `DQ01` failed with a value of exactly 1. One transaction was
neither loaded nor quarantined.

**Cause.** Order of operations. I validated before deduplicating, so a natural
key whose first copy was clean and whose duplicate copy had been corrupted into
an orphan was simultaneously loaded (via the clean copy) and counted as rejected
(via the corrupted one). The row-count identity could not balance.

**Fix.** Deduplicate first, then validate, via the `stg_txn_dedup` view.

**What I take from it.** This is the argument for control frameworks in one
incident. I wrote the loader and the check, and the check still caught me. An
off-by-one in a row count is exactly the class of defect that gets discovered
by a regulator rather than by an analyst.

---

## 4. The trade-off curve was an oracle result

**Symptom.** The first version reported 40.8% of volume deferrable at 100%
recall — nearly three times the prospective figure.

**Cause.** The curve was computed directly on the holdout: for each recall
target it found the threshold that achieved that recall *on the holdout itself*.
That assumes knowledge of which holdout alerts were positive, which is precisely
what you do not have on the day you set the threshold.

**Fix.** Thresholds are selected on the validation window and applied unchanged
to the holdout. The reported recall is what the policy actually delivered.

**Result.** 40.8% became 15.9%.

**What I take from it.** "Evaluated on held-out data" is not the same as
"evaluated prospectively". Any hyperparameter, threshold or cut-point chosen
with reference to the test set has spent it.

---

## 5. The gradient boosted model lost to logistic regression

**Not a bug — a finding, and initially an unwelcome one.**

LightGBM has the better ranking by every global metric (PR-AUC 0.694 vs 0.516;
ROC-AUC 0.985 vs 0.951). At the actual operating point it is worse: 10.4% of
volume deferred versus 15.9%.

The reason is that the 100%-recall constraint is set entirely by the
single worst-ranked true positive. Overall ranking quality is close to
irrelevant to it. LightGBM ranks most alerts better and one particular positive
much worse, and that one alert sets the threshold for everything.

I spent time trying to fix this — monotonic constraints, heavier class weights,
more regularisation — before concluding that the honest response was to select
the model that performs better at the decision being made. Logistic regression
is the production recommendation, which has the side benefit of exact rather
than approximated per-alert attribution in model validation.

**What I take from it.** Selecting on AUC would have chosen the wrong model.
The evaluation metric has to be the operating policy, not the leaderboard.

---

## 6. A test that asserted nothing

`test_r002_excludes_single_reportable_transactions` originally compared a
24-hour rule window against a day-level approximation. It failed, and rather
than fix the comparison I weakened it to `assert mx < 10000 or True` — which is
`assert True`.

I caught it on re-read. The test now reconstructs the same hour index the rule
uses and checks for a qualifying sub-threshold aggregation window.

**What I take from it.** A test weakened to make it pass is worse than no test,
because it reports coverage that does not exist. The same is true of the dead
rule in the next section.

---

## 7. R007 has never fired

`R007` (dormant account reactivation) produced zero alerts across 180 days.

I initially treated this as a generator problem to be fixed by loosening the
rule until it produced something. It is not. A rule that never fires is a
finding: on paper the control framework claims coverage of dormancy-based
typologies, and in practice it has none. The rule is either mis-specified or its
threshold is unreachable given the book.

It is retained in the rule set, reported in `rule_effectiveness.csv` with a
`RETIRE` recommendation, and called out in the CCO memo. Deleting it would have
concealed the most interesting control observation in the analysis.

---

## Known limitations I did not resolve

- **Synthetic data caps external validity.** Performance here is an upper bound.
  Real transaction data is messier, real typologies evolve against detection,
  and real labels are investigator dispositions with their own noise and bias.
  Nothing in this repository should be read as a claim about real-world lift.
- **Labels are generator ground truth, not investigator dispositions.** In
  production the target would be SAR/STR filing outcomes, which are themselves a
  biased sample: you only observe dispositions for alerts someone worked.
- **Bootstrap resamples alerts, not customers.** Alerts from one customer are
  correlated, so the intervals are narrower than a customer-level block
  bootstrap would give.
- **Dimensions are SCD Type 1.** An alert should be assessed against the KYC
  risk rating in force on the alert date, not today's rating. Type 2 handling
  on `dim_customer` is the first thing I would add.
- **No model monitoring.** There is no drift detection, no retraining trigger
  and no rollback path. For a regulated deployment that is a gap, not a
  simplification.
