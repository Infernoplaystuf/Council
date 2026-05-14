# ============================================================
# council.spec  —  PyInstaller build specification
# ============================================================
# Build with:
#     pyinstaller council.spec --noconfirm --clean
# Or use the helper scripts:
#     build.bat (Windows)   /   build.sh (mac/linux)
#
# Outputs:
#     build/                 — intermediate work directory (delete after)
#     dist/DatasInferno/     — the bundled application
#     dist/DatasInferno/DatasInferno.exe   (Windows)
#     dist/DatasInferno/DatasInferno       (Unix)
#
# What's bundled:
#   • Python interpreter + every dependency from requirements.txt
#   • Tkinter (with all submodules)
#   • llama-cpp-python native DLLs (so the user doesn't need to
#     compile anything — the .exe runs GGUF models out of the box)
#   • pandas / numpy / openpyxl / pyarrow / duckdb data-handling
#     stack for the vault analyst / index / embeddings layer
#   • Sentence-Transformers (the model weights are downloaded on
#     first run; we don't bundle the ~80 MB MiniLM file)
#   • All app icons, splash, and personality / branding YAMLs
#
# What's NOT bundled:
#   • The GGUF model file (5–15 GB) — user picks one and points
#     COUNCIL_GGUF_PATH at it via the in-app Browse button
#   • The user's vault/ folder — created at %USERPROFILE%\.council\
#     on first run, so it persists across reinstalls/upgrades
#
# Build size (typical):  ~1.3 GB on disk, ~600 MB zipped
# ============================================================

# pyright: reportMissingImports=false
# noqa: F821 — PyInstaller injects Analysis / PYZ / EXE / COLLECT globals

import sys
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    collect_data_files,
)

block_cipher = None
project_root = Path.cwd()

# ---- llama-cpp-python: native DLLs + python files + metadata ----------------
# This is the single most important addition vs. the older spec. Without
# `collect_all('llama_cpp')` the bundle ships the Python wrapper but NOT
# the llama.dll / libllama.so / libllama.dylib that does the actual
# inference, and the app launches but crashes on first model call.
llama_datas, llama_binaries, llama_hidden = collect_all("llama_cpp")

# ---- Sentence-Transformers / huggingface_hub / transformers data ------------
# The vector embedding layer (vault_embeddings.py) uses sentence-transformers.
# The MiniLM weights are downloaded at first use; we just need the Python
# wrapper code + the bundled config templates HF ships in its wheels.
try:
    st_datas, st_binaries, st_hidden = collect_all("sentence_transformers")
except Exception:
    st_datas, st_binaries, st_hidden = [], [], []

try:
    hf_datas, hf_binaries, hf_hidden = collect_all("huggingface_hub")
except Exception:
    hf_datas, hf_binaries, hf_hidden = [], [], []

# ---- pandas / numpy / openpyxl / pyarrow / duckdb data files ----------------
# These libraries each ship .pyi / .json / .csv resources their runtime
# loads via importlib.resources. PyInstaller's collect_data_files grabs them.
extra_datas = []
for pkg in ("pandas", "numpy", "openpyxl", "pyarrow", "duckdb",
            "matplotlib", "tkinterweb"):
    try:
        extra_datas.extend(collect_data_files(pkg))
    except Exception:
        pass

# ---- Hidden imports: pulled in dynamically / inside try/except / by name ----
# PyInstaller's static analyser cannot see these. If you add a new optional
# import inside the app, add it here too or the .exe will silently fall back
# to the "X not available" branch even though the user *did* install it.
hidden = [
    # ── Tkinter optional submodules ──
    "tkinter", "tkinter.filedialog", "tkinter.messagebox",
    "tkinter.scrolledtext", "tkinter.colorchooser", "tkinter.font",
    "tkinter.ttk", "tkinter.simpledialog",
    # ── stdlib used through lazy / runtime imports ──
    "ssl", "json", "csv", "sqlite3", "queue", "threading",
    "urllib.request", "urllib.parse", "urllib.error",
    "hmac", "hashlib", "base64", "ctypes", "ctypes.wintypes",
    "concurrent.futures",
    # ── Third-party data stack ──
    "pandas", "pandas.io.formats.style", "pandas.io.excel",
    "pandas.io.parsers", "pandas.compat.numpy",
    "numpy", "numpy.core._methods", "numpy.lib.format",
    "openpyxl", "xlrd", "pyarrow", "duckdb",
    "bson",                          # provided by pymongo
    "sqlalchemy", "sqlalchemy.dialects.sqlite",
    "h5py",                          # for .dream3d HDF5 pipelines
    "yaml",                          # personality_config.yaml
    # ── Plotting ──
    "matplotlib", "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_agg",
    "plotly", "plotly.graph_objects", "plotly.express",
    "tkinterweb",
    # ── LLM backend ──
    "llama_cpp", "llama_cpp.llama", "llama_cpp.llama_chat_format",
    # ── RAG / embeddings ──
    "sentence_transformers", "transformers", "tokenizers", "safetensors",
    "huggingface_hub", "chromadb",
    # ── Document parsing ──
    "pypdf", "docx",                  # python-docx is imported as `docx`
    "bs4", "lxml",                    # vault scraper / web crawl
    # ── Optional integrations ──
    "paramiko",                       # SSH compute nodes
    "pyttsx3",                        # text-to-speech
    "faster_whisper", "sounddevice", "soundfile",
    # ── Local modules — should be picked up automatically, listed here
    #    defensively so a missing import in an edge code path doesn't kill
    #    the bundle. Add new modules to this list as they're added to the
    #    repo. ──
    "council_engine", "council_gui_engine", "council_modules",
    "council_agents", "agent_core",
    "coordinator", "coder_agent", "intern_agent", "sage_agent",
    "vault_agent", "apothecary_engine",
    "vault_index", "vault_embeddings", "vault_tools", "vault_analyst",
    "vault_scraper", "vault_rag",
    "conversation_logger", "provenance",
    "pipeline_scanner", "pipeline_editor", "workflow_runner",
    "data_index", "hf_download", "branding",
    "model_backends", "openai_responses_backend", "phase1_ai_model_council",
    "graph_engine", "graph_data", "graph_personality", "grapher_app",
    "tab_grapher",
    "specialists", "splash", "onboarding",
    "activation_dialog", "licensing", "device_fingerprint",
    "crash_reporter", "updater",
    "dream3d_crawl", "dream3d_primer",
] + llama_hidden + st_hidden + hf_hidden

# ---- Data files to ship alongside the executable ----------------------------
# Source path is relative to project root; destination is relative to the
# bundle root (where the .exe lives). Anything the running app reads via
# Path(__file__).parent / "assets" needs to land in the same relative spot
# inside the bundle.
datas = [
    ("assets",                    "assets"),
    ("personality_config.yaml",   "."),
    ("personality_backends.json", "."),
    ("README.md",                 "."),
    ("USER_GUIDE.md",             "."),
    ("BUILDING.md",               "."),
    ("requirements.txt",          "."),
    ("installs.txt",              "."),
] + llama_datas + st_datas + hf_datas + extra_datas

# ---- Modules to deliberately exclude (slim down the bundle) -----------------
# Tests + scientific notebook deps can shave 100–200 MB off the bundle.
excludes = [
    "matplotlib.tests", "pandas.tests", "numpy.tests",
    "scipy.tests", "sklearn.tests",
    "test", "tests", "unittest",
    "pytest", "py", "_pytest",
    "IPython", "jupyter", "notebook", "ipykernel",
    "PyQt5", "PyQt6", "PySide2", "PySide6",   # we use Tk, not Qt
    "wx",                                       # no wxPython use
    # Large optional ML libs we don't use
    "torchvision", "torchaudio",
]

a = Analysis(
    ["council_gui_engine.py"],
    pathex=[str(project_root)],
    binaries=llama_binaries + st_binaries + hf_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---- Optional splash screen (uses assets/splash.png if present) -------------
# PyInstaller's Splash works on Windows + Linux (not macOS). If the splash
# file is missing we skip it silently so the build still succeeds.
splash_file = project_root / "assets" / "splash.png"
splash = None
if splash_file.exists() and sys.platform != "darwin":
    try:
        splash = Splash(
            str(splash_file),
            binaries=a.binaries,
            datas=a.datas,
            text_pos=(10, 280),
            text_size=10,
            text_color="white",
        )
    except Exception:
        splash = None

icon_path = project_root / "assets" / (
    "icon.ico" if sys.platform == "win32" else "icon.png"
)

if splash is not None:
    exe = EXE(
        pyz,
        a.scripts,
        splash,
        [],
        exclude_binaries=True,
        name="DatasInferno",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,            # UPX trips antivirus heuristics on some Windows boxes
        console=False,        # set True to keep a console window for debugging
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(icon_path),
    )
    coll = COLLECT(
        exe,
        splash.binaries,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="DatasInferno",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="DatasInferno",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(icon_path),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="DatasInferno",
    )
