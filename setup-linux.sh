#!/usr/bin/env bash
# ============================================================
# setup-linux.sh — first-time install for Council on native Linux
# (NOT WSL — use setup-wsl.sh for that).
#
# Targets an RTX 4080 laptop on Ubuntu/Debian/Pop!_OS (Section F
# of installs.txt, minus the WSL-specific parts).
#
# Does:
#   1. apt installs (build-essential + portaudio + GL libs).
#   2. Verifies the NVIDIA driver is installed on this Linux box
#      (NOT inherited from a Windows host — this is native Linux).
#   3. Installs Miniforge (conda) if not already present.
#   4. Creates a `council` conda env with Python 3.11.
#   5. Installs torch + llama-cpp-python with cu124 wheels (matches
#      Ada / RTX 40-series natively, including the RTX 4080 laptop).
#   6. Installs the rest of the Council pip deps.
#   7. Runs the import-check verification.
#
# After this, use ./run-linux.sh to launch the app.
#
# Idempotent — safe to re-run.
# ============================================================

set -e
set -o pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'
say()  { echo -e "${GREEN}[setup-linux]${RESET} $*"; }
warn() { echo -e "${YELLOW}[setup-linux]${RESET} $*"; }
die()  { echo -e "${RED}[setup-linux]${RESET} $*" >&2; exit 1; }

# ── Sanity: not WSL ─────────────────────────────────────────
if grep -qi microsoft /proc/version 2>/dev/null; then
    warn "This is WSL — use setup-wsl.sh instead (it autodetects the"
    warn "Windows-host driver). Continuing anyway since the steps overlap."
fi

# ── STEP 1: apt prerequisites ───────────────────────────────
say "STEP 1/7  — apt install build-essential + portaudio + GL libs"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y \
        build-essential git curl wget \
        libportaudio2 libportaudiocpp0 portaudio19-dev \
        libgl1 libglib2.0-0 \
        ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y \
        @development-tools git curl wget \
        portaudio-devel mesa-libGL glib2 \
        ca-certificates
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm \
        base-devel git curl wget \
        portaudio glib2 mesa \
        ca-certificates
else
    warn "unknown package manager — install build tools, portaudio, libGL by hand."
fi

# ── STEP 2: NVIDIA driver check (native Linux) ──────────────
say "STEP 2/7  — checking NVIDIA driver (native Linux box)"
CUDA_TIER="cpu"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 || true)
    CUDA_FROM_SMI=$(nvidia-smi | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' \
                     | awk '{print $3}' | head -1 || true)
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 || true)
    say "  driver: $DRIVER   reports CUDA up to: $CUDA_FROM_SMI   gpu: $GPU_NAME"
    case "$CUDA_FROM_SMI" in
        12.8|12.9|13.*)            CUDA_TIER="cu128" ;;
        12.4|12.5|12.6|12.7)       CUDA_TIER="cu124" ;;
        12.0|12.1|12.2|12.3)       CUDA_TIER="cu121" ;;
        *)
            warn "Unknown CUDA $CUDA_FROM_SMI — defaulting to cu124 (RTX 4080 target)."
            CUDA_TIER="cu124"
            ;;
    esac
    say "  using CUDA wheel tier: $CUDA_TIER"
else
    warn "nvidia-smi not present or failing."
    warn "Install the NVIDIA driver before re-running:"
    warn "  Ubuntu/Pop: sudo apt install nvidia-driver-550 && sudo reboot"
    warn "  Fedora:     sudo dnf install akmod-nvidia xorg-x11-drv-nvidia-cuda && sudo reboot"
    warn "  Arch:       sudo pacman -S nvidia nvidia-utils && sudo reboot"
    warn "Falling back to CPU-only install. The app will run but slowly."
fi
# Allow override (e.g. force cu124 when we know the target):
if [ -n "${COUNCIL_CUDA_TIER:-}" ]; then
    say "  override via COUNCIL_CUDA_TIER=${COUNCIL_CUDA_TIER}"
    CUDA_TIER="$COUNCIL_CUDA_TIER"
fi

# ── STEP 3: Miniforge ───────────────────────────────────────
say "STEP 3/7  — installing Miniforge (conda) if needed"
CONDA_HOME="$HOME/miniforge3"
if [ ! -d "$CONDA_HOME" ]; then
    TMPSH=$(mktemp --suffix=.sh)
    wget -qO "$TMPSH" \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
    bash "$TMPSH" -b -p "$CONDA_HOME"
    rm -f "$TMPSH"
else
    say "  miniforge already present at $CONDA_HOME — skipping install."
fi
# shellcheck disable=SC1091
source "$CONDA_HOME/etc/profile.d/conda.sh"

# ── STEP 4: conda env ───────────────────────────────────────
say "STEP 4/7  — creating conda env 'council' (Python 3.11)"
if conda env list | grep -q '^council\s'; then
    say "  env 'council' already exists — reusing."
else
    conda config --add channels conda-forge >/dev/null
    conda config --set channel_priority strict >/dev/null
    conda create -n council python=3.11 -y
fi
conda activate council

# ── STEP 5: torch + llama-cpp-python ────────────────────────
say "STEP 5/7  — installing torch + llama-cpp-python ($CUDA_TIER)"
case "$CUDA_TIER" in
    cpu)
        pip install -q torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/cpu
        pip install -q llama-cpp-python
        ;;
    cu121)
        pip install -q torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/cu121
        pip install -q llama-cpp-python \
            --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 \
            --force-reinstall --no-cache-dir
        ;;
    cu124)
        pip install -q torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/cu124
        pip install -q llama-cpp-python \
            --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
            --force-reinstall --no-cache-dir
        ;;
    cu128)
        pip install -q --pre torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/nightly/cu128
        pip install -q llama-cpp-python \
            --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
            --force-reinstall --no-cache-dir
        ;;
esac

# ── STEP 6: rest of the deps ────────────────────────────────
say "STEP 6/7  — installing remaining Council deps"
pip install -q --upgrade-strategy only-if-needed \
    PyYAML requests pandas openpyxl xlrd pyarrow h5py \
    matplotlib plotly tkinterweb \
    chromadb "sentence-transformers>=2.7,<5" "transformers>=4.44,<5" huggingface_hub \
    pypdf python-docx duckdb pymongo "SQLAlchemy>=2.0,<3.0" \
    beautifulsoup4 lxml html2text \
    pyttsx3 faster-whisper sounddevice soundfile \
    paramiko

# ── STEP 7: verify ──────────────────────────────────────────
say "STEP 7/7  — verifying imports"
python - <<'PY'
import torch, llama_cpp, chromadb, sentence_transformers, pandas, openpyxl, duckdb, h5py
print(f"  CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"  device:         {torch.cuda.get_device_name(0)}")
    print(f"  torch.cuda:     {torch.version.cuda}")
print("  all imports OK")
PY

echo
say "All set."
say "Next:"
say "  1. Put a .gguf model in ~/models/ (Granite 3.1 8B Q4_K_M recommended for 16GB VRAM)."
say "  2. ./run-linux.sh   (or  COUNCIL_GGUF_PATH=~/models/foo.gguf ./run-linux.sh)"
