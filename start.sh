#!/usr/bin/env bash
# WhoDunChat local launcher for macOS/Linux.
# Usage: ./start.sh

set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  WhoDunChat - Local Launcher"
echo "============================================"
echo

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 was not found on PATH."
    echo "Please install Python 3.10+ first."
    exit 1
fi

python3 --version

if [ ! -f "venv/bin/activate" ]; then
    echo
    echo "[INFO] Creating virtual environment in ./venv ..."
    python3 -m venv venv
fi

source venv/bin/activate

echo
echo "[INFO] Installing dependencies (this can take a while the first time)..."
pip install --upgrade pip -q
pip install -r requirements.txt

echo
echo "[INFO] Running local logic tests (no network required)..."
PYTHONPATH="$(pwd)" python3 tests/test_pipeline_logic.py || true
echo

echo "[INFO] Starting server at http://localhost:8000"
echo "[INFO] Press CTRL+C to stop the server."
echo
python3 -m uvicorn app.main:app --reload --port 8000
