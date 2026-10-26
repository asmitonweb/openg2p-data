#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONUTF8=1
python -m unittest discover -s tests -v
python scripts/packs/validate_pack.py packs/ETH-OAN
