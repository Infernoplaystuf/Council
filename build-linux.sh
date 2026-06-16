#!/usr/bin/env bash
# ============================================================
# build-linux.sh — produce a standalone ./dist/DatabaseGrabber binary
#
# Run this ONCE on a Linux machine with Python 3.10+ and Tk
# (sudo apt install python3-venv python3-tk on Debian/Ubuntu).
# It builds an isolated venv, installs the deps + PyInstaller, and
# packages everything into a single binary that needs NO Python on
# the machines you run it on.
#
# Note: PyInstaller does not cross-compile — build on Linux for Linux,
# on Windows for Windows, on macOS for macOS.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[build] python3 not found. Install Python 3.10+ and python3-tk."
    exit 1
fi

echo "[build] Creating build virtual environment (.build-venv)..."
python3 -m venv .build-venv
# shellcheck disable=SC1091
source .build-venv/bin/activate

echo "[build] Installing dependencies + PyInstaller..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[build] Packaging with PyInstaller..."
pyinstaller --noconfirm database_grabber.spec

echo
echo "[build] DONE. Your standalone app is:"
echo "        $(pwd)/dist/DatabaseGrabber"
echo "        Copy that single file anywhere — no Python needed to run it."
