#!/bin/zsh
set -e

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Sparse2Solid needs python3. Install Python 3.10+ and try again."
  read "?Press Return to close."
  exit 1
fi

echo "Starting Sparse2Solid Studio..."
echo "URL: http://127.0.0.1:8765"
echo "Press Control-C in this window to stop the local server."
echo

PYTHONPATH=src python3 scripts/run_gui.py
