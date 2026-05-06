# ============================================================
# council.spec  —  PyInstaller build specification
# ============================================================
# Build with:    pyinstaller council.spec
# Or use the helper scripts:    build.bat (Windows) / build.sh (mac/linux)
#
# Outputs:
#   build/  - intermediate work directory (delete after)
#   dist/Council/    - the bundled application
#   dist/Council/Council.exe (or `Council` on Unix)
# ============================================================

# pyright: reportMissingImports=false
# noqa: F821 — PyInstaller injects globals (Analysis, EXE, etc.)

import sys
from pathlib import Path

block_cipher = None
project_root = Path.cwd()

# ---- Hidden imports (pulled in dynamically; PyInstaller can't see them) ----
hidden = [
    # Tkinter optional submodules
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.scrolledtext",
    "tkinter.colorchooser",
    "tkinter.font",
    # Anything imported lazily inside try/except blocks
    "ssl",
    "json",
    # Third-party that may be present
    "yaml",
    "chromadb",
    "sentence_transformers",
    "tkinterweb",
    "paramiko",
    "pyttsx3",
]

# ---- Data files to ship alongside the executable ----
# `assets/` contains icons, splash, and `sample_data/` (3 demo CSVs).
datas = [
    ("assets",                    "assets"),
    ("personality_config.yaml",   "."),
    ("personality_backends.json", "."),
    ("README.md",                 "."),
    ("USER_GUIDE.md",             "."),
]

# ---- Modules to deliberately exclude (slim down the bundle) ----
excludes = [
    "matplotlib.tests",
    "pandas.tests",
    "numpy.tests",
    "test",
    "tests",
    "unittest",
    # Other developers' test suites that pip pulls in
    "pytest",
    "py",
]

a = Analysis(
    ["council_gui_engine.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DatasInferno",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX trips antivirus on some Windows boxes
    console=False,              # Set True to keep a console for debugging
    disable_windowed_traceback=False,
    icon=str(project_root / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="DatasInferno",
)
