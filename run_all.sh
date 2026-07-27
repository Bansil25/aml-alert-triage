#!/usr/bin/env bash
# Full pipeline, start to finish. Reproducible from the seed in config/params.yml.
set -euo pipefail

echo "==> 1/6  generating synthetic data"
python src/generate_data.py

echo "==> 2/6  building warehouse (aborts on blocking DQ failure)"
python src/build_warehouse.py

echo "==> 3/6  training and evaluating triage model"
python src/train_model.py

echo "==> 4/6  generating reason codes and global attribution"
python src/explain.py

echo "==> 5/6  rule effectiveness, overlap and business case"
python src/rule_analysis.py
python src/cost_benefit.py

echo "==> 6/7  exporting Power BI star schema"
python src/export_powerbi.py

echo "==> 7/7  rendering dashboard previews"
python src/make_previews.py

echo ""
echo "Done. Outputs in outputs/ ; Power BI sources in powerbi/exports/ ;"
echo "dashboard previews in powerbi/previews/"
