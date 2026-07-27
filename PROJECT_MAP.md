# Project map

Where everything lives, and the order to read it in.

## Read first (for a reviewer with 5 minutes)
1. `README.md` — results, architecture, how to run
2. `powerbi/previews/*.png` — the two headline dashboard pages
3. `docs/CCO_MEMO.md` — the one-page executive deliverable
4. `docs/WHAT_DIDNT_WORK.md` — the failures, including ROC-AUC 1.000

## Pipeline (the order it runs)
| Step | File | Produces |
|---|---|---|
| 1 | `src/generate_data.py` | `data/*.csv` synthetic ledger + ground truth |
| 2 | `src/build_warehouse.py` + `src/sql/01-04` | `data/aml.db` star schema, alerts, DQ results |
| 3 | `src/features.py` | 81 point-in-time features (imported, not run standalone) |
| 4 | `src/train_model.py` | `outputs/metrics.json`, `model.pkl`, `scored_alerts.csv` |
| 5 | `src/explain.py` | reason codes, `shap_global.csv` |
| 6 | `src/rule_analysis.py` | `rule_effectiveness.csv`, `rule_overlap.csv` |
| 7 | `src/cost_benefit.py` | `cost_benefit.json`, sensitivity table |
| 8 | `src/export_powerbi.py` + `src/sql/05_marts.sql` | `powerbi/exports/*.csv` |
| 9 | `src/make_previews.py` | `powerbi/previews/*.png` |

Run all of it: `./run_all.sh` (~4 min). Test it: `pytest -q` (18 tests).

## Documentation
| File | What it covers |
|---|---|
| `docs/CCO_MEMO.md` | Executive recommendation and rule findings |
| `docs/DATA_MODEL.md` | Grain, star schema, SCD decisions, known simplifications |
| `docs/SYNTHETIC_DATA_ASSUMPTIONS.md` | Every generator assumption + rationale |
| `docs/WHAT_DIDNT_WORK.md` | Failures, dead ends, unresolved limitations |
| `powerbi/DASHBOARD_SPEC.md` | Four-page build: relationships, visuals, RLS |
| `powerbi/dax_measures.md` | Every DAX measure, copy-paste ready |

## Config
`config/params.yml` — every assumption that drives a headline number. Change the
seed, the illicit rate, the investigator cost, the split days here.
