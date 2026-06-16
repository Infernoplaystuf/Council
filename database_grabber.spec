# -*- mode: python ; coding: utf-8 -*-
# ============================================================
# PyInstaller spec for Database Grabber — a self-contained,
# read-only database connector + exporter.
#
# Build (after `pip install -r requirements.txt pyinstaller`):
#     pyinstaller database_grabber.spec
#
# Output: dist/DatabaseGrabber(.exe) — a single file that bundles
# Python, Tk, pandas, SQLAlchemy, pymongo and the DB drivers. The
# target machine needs NO pre-installed Python.
#
# Cross-platform: run this spec on Windows to get a .exe, on Linux to
# get an ELF binary, on macOS to get a .app/binary. PyInstaller does
# not cross-compile — build on each OS you want to ship to.
# ============================================================

block_cipher = None

# Hidden imports: SQLAlchemy dialects, the DB drivers, and pandas'
# Excel writer are imported lazily/by-string, so PyInstaller's static
# analysis misses them unless we name them here.
hidden = [
    "sqlalchemy", "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.mysql", "sqlalchemy.dialects.mssql",
    "sqlalchemy.dialects.sqlite",
    "pymongo", "bson",
    "pandas", "numpy",
    "openpyxl",                       # Excel export
    # DB drivers — present only if installed at build time; harmless if
    # listed but absent (PyInstaller warns and skips).
    "psycopg2", "pymysql", "pyodbc", "duckdb",
]

a = Analysis(
    ["database_grabber.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim weight: this tool never needs these.
    excludes=[
        "matplotlib", "scipy", "sklearn", "torch", "torchvision",
        "torchaudio", "sentence_transformers", "transformers",
        "chromadb", "tkinterweb", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter", "notebook", "pytest", "test", "tests",
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DatabaseGrabber",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI app — no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
