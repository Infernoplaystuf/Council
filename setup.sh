#!/usr/bin/env bash
# ============================================================
#  setup.sh — one-command installer for Data's Inferno / Council
#
#  Usage:    ./setup.sh                (interactive)
#            ./setup.sh --yes          (accept all defaults)
#            ./setup.sh --help         (full options)
#
#  Finds a working Python and hands off to setup_council.py. The
#  Python script handles everything else (hardware probe, conda
#  install if missing, env creation, dependency install, smoke test).
#
#  Works on Linux, macOS, and WSL. The Python script picks the right
#  CUDA wheel tier (cu121 / cu124 / cu128 / cpu) automatically.
# ============================================================
set -e

# Locate Python. Try `python3` first (modern Linux/macOS), then
# `python` (some macOS / Windows setups), then bail with guidance.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$SCRIPT_DIR/setup_council.py" "$@"
fi

if command -v python >/dev/null 2>&1; then
    # Make sure it's actually Python 3.
    if python -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' >/dev/null 2>&1; then
        exec python "$SCRIPT_DIR/setup_council.py" "$@"
    fi
fi

cat >&2 <<'EOF'

[setup.sh] No Python 3.8+ on PATH.

Install Python first:
  Ubuntu / WSL:     sudo apt install -y python3 python3-pip python3-venv
  Fedora:           sudo dnf install -y python3 python3-pip
  macOS (Homebrew): brew install python@3.11
  macOS (no brew):  https://www.python.org/downloads/

Then re-run ./setup.sh

EOF
exit 1
