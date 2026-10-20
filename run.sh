#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 pipeline/generate_metrics.py
python3 detect.py
python3 evaluate.py
echo "triage queue: data/triage_queue.json  |  eval: data/eval.json"
