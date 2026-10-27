#!/usr/bin/env bash
# Runs the 7 views one after another; each view gets its own folder with train/valid/test + not_parsed txt.
# Usage (from ~/CANON-SCT, env canon-sct active):
#   bash scripts/run_all_views.sh                 # primevul, 8 workers
#   bash scripts/run_all_views.sh primevul 6      # dataset name, workers
# Safe to stop (Ctrl+C) and re-run: finished samples are skipped.

DATASET="${1:-primevul}"
WORKERS="${2:-8}"
VIEWS=("ast" "cfg" "sdfg" "ast+cfg" "ast+sdfg" "cfg+sdfg" "ast+cfg+sdfg")

for V in "${VIEWS[@]}"; do
  echo "=================== VIEW: $V ==================="
  caffeinate -i python scripts/run_graphs.py --dataset "$DATASET" --view "$V" --workers "$WORKERS" || exit 1
done

echo "All 7 views done. Folders:"
ls -d output/"$DATASET"/*/