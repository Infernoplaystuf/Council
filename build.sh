#!/usr/bin/env bash
# ============================================================
# Council — macOS / Linux build script
# ============================================================
# Produces dist/Council/Council and supporting files.
# Pre-reqs:
#     - Active Python 3.11 environment
#     - PyInstaller installed: pip install pyinstaller
# ============================================================

set -euo pipefail

echo "=== Cleaning previous build artefacts ==="
rm -rf build dist

echo "=== Verifying PyInstaller is available ==="
if ! python -m PyInstaller --version >/dev/null 2>&1; then
    echo "PyInstaller not found. Installing..."
    python -m pip install pyinstaller
fi

echo "=== Running PyInstaller ==="
python -m PyInstaller council.spec --noconfirm --clean

echo
echo "=== Build complete ==="
echo "Bundle location: $(pwd)/dist/Council"
case "$(uname)" in
    Darwin) echo "Executable:      $(pwd)/dist/Council/Council" ;;
    Linux)  echo "Executable:      $(pwd)/dist/Council/Council" ;;
esac

echo
echo "Next step: package for distribution"
echo "  macOS:   create-dmg or hdiutil to make a .dmg"
echo "  Linux:   AppImageTool or similar to make an .AppImage"
