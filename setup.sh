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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Locate Python. Try python3 first (modern Linux/macOS), then plain
# python (some macOS / Windows setups), then bail with guidance.
PYTHON_EXE=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE=python3
elif command -v python >/dev/null 2>&1 \
        && python -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
        >/dev/null 2>&1; then
    PYTHON_EXE=python
fi

if [ -z "$PYTHON_EXE" ]; then
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
fi

# Run the Python orchestrator. We deliberately do NOT use `exec`
# here so the trailing message below runs even on failure.
"$PYTHON_EXE" "$SCRIPT_DIR/setup_council.py" "$@"
RC=$?

echo
if [ "$RC" = "0" ]; then
    echo "[setup.sh] Setup finished cleanly."
else
    echo "[setup.sh] Setup exited with code $RC."
    case "$RC" in
        2) echo "             (code 2 = conda missing — see message above)" ;;
        3) echo "             (code 3 = a step failed — see the 'Install summary' table above)" ;;
    esac
    cat <<'EOF'

  - Scroll up to read the "Install summary" — failed steps are
    marked with a red X.
  - Each failed step prints a "suggested fix" block with the
    most common causes.
  - Re-running ./setup.sh skips steps that already succeeded.
  - To start clean:  ./setup.sh --reinstall

EOF
fi
exit "$RC"
