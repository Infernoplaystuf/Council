#!/usr/bin/env bash
# ============================================================
# Council / Datas Inferno - macOS / Linux build script
# ============================================================
# Produces dist/Anvil/Anvil and supporting files.
#
# Pre-reqs:
#     - Python 3.11 on PATH (or a conda env activated — see
#       installs.txt for the recommended env setup).
#     - Runtime deps:  pip install -r requirements.txt
#     - PyInstaller:   pip install pyinstaller
#       (the script will pip-install it for you if missing).
#
# The resulting dist/Anvil/ folder is fully self-contained.
# Tar/zip it to ship — no Python install needed on the target box.
#
# What is NOT bundled (by design):
#     - The GGUF model file. Users point COUNCIL_GGUF_PATH at one
#       after install (or use the in-app Browse button).
#     - The vault. Created at $HOME/.council/ on first run.
# ============================================================

set -euo pipefail

echo
echo "=== Cleaning previous build artefacts ==="
rm -rf build dist

echo
echo "=== Verifying Python is on PATH ==="
if ! command -v python >/dev/null 2>&1; then
    echo "ERROR: python not found on PATH."
    echo "Activate your Council env or install Python 3.11 first."
    exit 1
fi
python --version

echo
echo "=== Verifying PyInstaller is available ==="
if ! python -m PyInstaller --version >/dev/null 2>&1; then
    echo "PyInstaller not found. Installing into the current env..."
    python -m pip install pyinstaller
fi
python -m PyInstaller --version

echo
echo "=== Verifying critical runtime deps are importable ==="
fail=0
for mod in llama_cpp pandas numpy openpyxl tkinter yaml requests; do
    if ! python -c "import $mod" >/dev/null 2>&1; then
        echo "  missing: $mod"
        fail=1
    fi
done
if [ "$fail" = "1" ]; then
    echo
    echo "One or more required modules are missing."
    echo "Run:  pip install -r requirements.txt"
    exit 1
fi
echo "All critical deps OK."

echo
echo "=== Running PyInstaller (this takes 5-15 minutes) ==="
python -m PyInstaller anvil.spec --noconfirm --clean

# ---- Bundle size --------------------------------------------------------
bundle_path="dist/Anvil"
if command -v du >/dev/null 2>&1; then
    echo
    echo "=== Reporting bundle size ==="
    du -sh "$bundle_path"
fi

exe_suffix=""
case "$(uname)" in
    Darwin) exe_suffix="" ;;
    Linux)  exe_suffix="" ;;
    MINGW*|MSYS*|CYGWIN*) exe_suffix=".exe" ;;
esac

echo
echo "============================================================"
echo " BUILD COMPLETE"
echo "============================================================"
echo " Bundle:      $(pwd)/${bundle_path}"
echo " Executable:  $(pwd)/${bundle_path}/Anvil${exe_suffix}"
echo
echo " First-run setup on the target machine:"
echo "   1. Copy/extract dist/Anvil/ anywhere"
echo "   2. Run ./Anvil"
echo "   3. When prompted, point COUNCIL_GGUF_PATH at a .gguf model"
echo "      (Browse button inside the app does this for you)."
echo
echo " To package for distribution:"
echo "   macOS:   create-dmg or hdiutil to make a .dmg"
echo "   Linux:   AppImageTool or similar to make an .AppImage"
echo "   either:  tar -czf anvil.tar.gz dist/Anvil/"
echo "============================================================"
