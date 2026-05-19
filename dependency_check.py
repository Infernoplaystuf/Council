"""
dependency_check.py — surface optional dependencies that aren't installed.

Most of the app's features are gated behind `try / except ImportError`
blocks that silently degrade: speech transcription disables itself,
the Intern's web crawl falls back to "not available", embeddings stay
keyword-only, etc. That's friendly behaviour but it hides the fact
that a feature COULD be enabled with one pip install.

This module gathers all those optional dependencies in one place so
the user can see at a glance what's available and what would unlock
new behaviour. Two surfaces consume it:

  • The Diagnostics tab in the GUI — lists every feature with status,
    description, and a copy-able pip install command.
  • The "check dependencies" / "what's missing" chat intent — same
    info rendered as transcript text for users who prefer the chat.

Nothing here installs anything automatically. The user runs pip in
their own environment so we never get the "pip wrote to the wrong
interpreter" mess that plagues PyInstaller bundles.
"""

from __future__ import annotations

import importlib
import importlib.util
import platform
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================
# Catalog — what's optional, what each unlocks, how to install
# ============================================================
# Each entry maps one logical "feature" to one or more importable
# Python modules. The feature is reported as available if EVERY listed
# module imports cleanly. Description is one short sentence; install
# is a copy-pasteable pip command that gives the user the full feature.
#
# Required-by-the-core packages (PyYAML, numpy, pandas, openpyxl,
# llama-cpp-python, tkinter) are deliberately NOT in this catalog —
# if any of those is missing the app wouldn't have launched at all.

@dataclass
class FeatureSpec:
    name:        str            # human-readable feature label
    modules:     List[str]      # python import names — ALL must resolve
    install:     str            # pip command to install
    description: str            # one short sentence on what it unlocks
    impact:      str = "low"    # "high" | "med" | "low" — for UI sort


@dataclass
class FeatureStatus:
    spec:    FeatureSpec
    ok:      bool
    missing: List[str] = field(default_factory=list)   # missing module names


OPTIONAL_FEATURES: List[FeatureSpec] = [
    # ── HIGH IMPACT — these change what the app can actually answer
    FeatureSpec(
        name="Vector embeddings (semantic vault search)",
        modules=["sentence_transformers"],
        install="pip install sentence-transformers",
        description=(
            "Build vector embeddings via 'build embeddings' chat command. "
            "Lets vault search match meaning, not just keywords — 'metals' "
            "finds files with 'iron' or 'steel' even when 'metals' isn't "
            "in the file."
        ),
        impact="high",
    ),
    FeatureSpec(
        name="ChromaDB (alternative RAG store)",
        modules=["chromadb"],
        install="pip install chromadb",
        description=(
            "Alternative vector store some vault features use. Required "
            "by certain RAG paths the app may fall back to."
        ),
        impact="high",
    ),
    FeatureSpec(
        name="Parquet file support",
        modules=["pyarrow"],
        install="pip install pyarrow",
        description=(
            "Read .parquet files in the analyst and vault index. Common "
            "for big tabular data dumps."
        ),
        impact="high",
    ),
    FeatureSpec(
        name="DuckDB (analytical SQL on local files)",
        modules=["duckdb"],
        install="pip install duckdb",
        description=(
            "Query .duckdb databases and run SQL directly over CSV/Parquet "
            "files without loading them. The analyst step can use this."
        ),
        impact="high",
    ),
    FeatureSpec(
        name="MongoDB BSON dumps",
        modules=["bson"],
        install="pip install pymongo",
        description=(
            "Read .bson files (Mongo exports) — surface their schema in "
            "the vault index and let the analyst load them."
        ),
        impact="high",
    ),
    FeatureSpec(
        name="PDF text extraction",
        modules=["pypdf"],
        install="pip install pypdf",
        description=(
            "Read .pdf files for vault search and prompt injection."
        ),
        impact="high",
    ),

    # ── MED IMPACT — quality-of-life upgrades
    FeatureSpec(
        name="Word document support (.docx)",
        modules=["docx"],
        install="pip install python-docx",
        description=(
            "Read .docx files for vault search."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Excel files (.xlsx / .xls)",
        modules=["openpyxl"],
        install="pip install openpyxl xlrd",
        description=(
            "Read Excel workbooks. openpyxl handles .xlsx; xlrd is needed "
            "for legacy .xls."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="HDF5 (Dream3D pipelines)",
        modules=["h5py"],
        install="pip install h5py",
        description=(
            "Read .dream3d HDF5 pipeline files. Only relevant to Dream3D "
            "users."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Plotly HTML viewer inside Tk",
        modules=["tkinterweb"],
        install="pip install tkinterweb",
        description=(
            "Render interactive Plotly charts in the Grapher tab. Without "
            "this, Plotly opens charts in your default browser instead."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Fast JSON parser (orjson)",
        modules=["orjson"],
        install="pip install orjson",
        description=(
            "3-5x faster JSON parsing during vault indexing. Speeds up "
            "the small-file tier; tier 2/3 use regex regardless."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Physical CPU core detection",
        modules=["psutil"],
        install="pip install psutil",
        description=(
            "Lets llama-cpp default to the right thread count on "
            "hyperthreaded / hybrid CPUs (12th-gen Intel etc). Without "
            "it the app falls back to logical-core count — typically "
            "10-30%% slower on hyperthreaded CPUs."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Speech transcription (voice input)",
        modules=["faster_whisper", "sounddevice", "soundfile"],
        install="pip install faster-whisper sounddevice soundfile",
        description=(
            "Voice input via the Speech tab — record audio, transcribe "
            "locally with faster-whisper, route the text into chat."
        ),
        impact="med",
    ),
    FeatureSpec(
        name="Text-to-speech output",
        modules=["pyttsx3"],
        install="pip install pyttsx3",
        description=(
            "Spoken response mode — the council's answer is also read "
            "aloud."
        ),
        impact="low",
    ),

    # ── LOW IMPACT — niche features / advanced
    FeatureSpec(
        name="Web crawling (Intern's web research)",
        modules=["crawl4ai"],
        install="pip install crawl4ai && crawl4ai-setup",
        description=(
            "Intern personality's web research path. Without it the "
            "Intern's web steps fall back to 'not available'."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="HTML parsing helpers",
        modules=["bs4", "lxml"],
        install="pip install beautifulsoup4 lxml",
        description=(
            "Used by the vault scraper and reference-doc crawler."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="Remote SQL bridge (SQLAlchemy)",
        modules=["sqlalchemy"],
        install="pip install SQLAlchemy",
        description=(
            "Connect to remote PostgreSQL / MySQL / MSSQL databases via "
            "a JSON connection file in the vault. Read-only SELECTs."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="PostgreSQL driver",
        modules=["psycopg2"],
        install="pip install psycopg2-binary",
        description=(
            "Required by SQLAlchemy for PostgreSQL connections."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="MySQL / MariaDB driver",
        modules=["pymysql"],
        install="pip install PyMySQL",
        description=(
            "Required by SQLAlchemy for MySQL / MariaDB connections."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="MSSQL / ODBC driver",
        modules=["pyodbc"],
        install="pip install pyodbc",
        description=(
            "Required by SQLAlchemy for MSSQL / ODBC connections."
        ),
        impact="low",
    ),
    FeatureSpec(
        name="SSH compute nodes (Apothecary)",
        modules=["paramiko"],
        install="pip install paramiko",
        description=(
            "Advanced — provision remote compute nodes over SSH. Only "
            "needed if you've enabled the Apothecary tab via "
            "COUNCIL_ADVANCED=1."
        ),
        impact="low",
    ),
]


# ============================================================
# Check
# ============================================================

def _module_available(name: str) -> bool:
    """True iff `import name` would succeed (no actual import).

    Uses importlib.util.find_spec which doesn't execute the module's
    top-level code — quick and side-effect-free. Falls back to a real
    import on packages that don't ship the spec (rare; some namespace
    packages).
    """
    try:
        spec = importlib.util.find_spec(name)
        if spec is not None:
            return True
    except Exception:
        pass
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def check_feature(spec: FeatureSpec) -> FeatureStatus:
    """Return availability status for one feature."""
    missing = [m for m in spec.modules if not _module_available(m)]
    return FeatureStatus(spec=spec, ok=(not missing), missing=missing)


def check_all() -> List[FeatureStatus]:
    """Run every check. Returns the list in catalog order; the UI can
    sort by impact / status if it wants."""
    return [check_feature(s) for s in OPTIONAL_FEATURES]


# ============================================================
# Render helpers — used by both the GUI tab and the chat intent
# ============================================================

def system_summary() -> List[str]:
    """A few lines about the running environment (Python version,
    platform, n_ctx if available). Always shown above the feature list
    so users have the diagnostic basics to share when reporting bugs."""
    lines = [
        f"Python:       {platform.python_version()}",
        f"Platform:     {platform.platform()}",
        f"Architecture: {platform.machine()}",
    ]
    try:
        import os as _os
        cpu = _os.cpu_count() or "?"
        lines.append(f"Logical CPUs: {cpu}")
    except Exception:
        pass
    try:
        import psutil as _ps   # noqa: F401
        n_phys = _ps.cpu_count(logical=False)
        if n_phys:
            lines.append(f"Physical CPUs: {n_phys}")
    except Exception:
        pass
    # GGUF settings if the backend is reachable
    try:
        import council_engine as _ce
        lines.append(f"GGUF n_ctx:   {_ce.get_n_ctx():,} tokens")
        max_ctx = _ce.get_model_max_context()
        if max_ctx:
            lines.append(f"Model max:    {max_ctx:,} tokens")
    except Exception:
        pass
    return lines


def render_as_text(statuses: Optional[List[FeatureStatus]] = None) -> str:
    """Format the dependency check as a plain-text block — used by the
    'check dependencies' chat intent.
    """
    if statuses is None:
        statuses = check_all()
    available = [s for s in statuses if s.ok]
    missing   = [s for s in statuses if not s.ok]

    out: List[str] = []
    out.append("System summary")
    out.append("──────────────")
    out.extend("  " + ln for ln in system_summary())
    out.append("")

    if missing:
        # Group missing by impact so high-impact ones surface first
        by_impact = {"high": [], "med": [], "low": []}
        for s in missing:
            by_impact.setdefault(s.spec.impact, []).append(s)
        out.append(f"Missing optional dependencies ({len(missing)})")
        out.append("─────────────────────────────────────")
        for impact_key in ("high", "med", "low"):
            bucket = by_impact.get(impact_key, [])
            if not bucket:
                continue
            label = {"high": "High impact",
                     "med":  "Medium impact",
                     "low":  "Low impact"}[impact_key]
            out.append(f"\n  {label}:")
            for s in bucket:
                out.append(f"    ✗ {s.spec.name}")
                out.append(f"        {s.spec.description}")
                out.append(f"        Missing: {', '.join(s.missing)}")
                out.append(f"        Install: {s.spec.install}")
        out.append("")
    else:
        out.append("All optional dependencies are installed. ✓")
        out.append("")

    if available:
        out.append(f"Available optional features ({len(available)})")
        out.append("─────────────────────────────")
        for s in available:
            out.append(f"  ✓ {s.spec.name}")
        out.append("")

    out.append("To install a missing feature, run its pip command in the "
               "same Python environment that runs this app. Restart the "
               "app afterward to pick up the change.")
    return "\n".join(out)
