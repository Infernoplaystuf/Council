#!/usr/bin/env bash
# ============================================================
# setup-wsl.sh — first-time install for the Council app on WSL.
#
# Run this ONCE per machine. It does:
#   1. apt installs (build-essential + portaudio for sounddevice).
#   2. Verifies the Windows-side NVIDIA driver via nvidia-smi.
#   3. Installs Miniforge (conda) if not already present.
#   4. Creates the `council` conda env with Python 3.11.
#   5. Installs torch + llama-cpp-python with the right CUDA wheels
#      for your GPU (autodetects via nvidia-smi's reported CUDA).
#   6. Installs the rest of the Council pip deps.
#   7. Runs the import-check verification.
#
# After this, use ./run-wsl.sh to launch the app any time.
#
# Idempotent — safe to re-run if a step failed. Existing env
# is reused; only missing pieces install.
# ============================================================

set -e
set -o pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

say()  { echo -e "${GREEN}[setup-wsl]${RESET} $*"; }
warn() { echo -e "${YELLOW}[setup-wsl]${RESET} $*"; }
die()  { echo -e "${RED}[setup-wsl]${RESET} $*" >&2; exit 1; }

# ── Sanity: are we actually in WSL? ─────────────────────────
if ! grep -qi microsoft /proc/version 2>/dev/null; then
    warn "This script targets WSL but /proc/version doesn't mention"
    warn "Microsoft. Continuing anyway (it works on plain Linux too)."
fi

# ── STEP 1: apt prerequisites ───────────────────────────────
say "STEP 1/7  — apt install build-essential + portaudio + GL libs"
sudo apt update
sudo apt install -y \
    build-essential git curl wget \
    libportaudio2 libportaudiocpp0 portaudio19-dev \
    libgl1 libglib2.0-0

# ── STEP 2: NVIDIA driver passthrough check ─────────────────
say "STEP 2/7  — checking Windows host NVIDIA driver (via nvidia-smi)"
NVIDIA_OK=0
DRIVER_CUDA=""
if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi >/dev/null 2>&1; then
        NVIDIA_OK=1
        DRIVER_CUDA=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
                       | head -1 || true)
        CUDA_FROM_SMI=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' \
                         | awk '{print $3}' | head -1 || true)
        say "  driver: $DRIVER_CUDA   supports CUDA up to: $CUDA_FROM_SMI"
    else
        warn "nvidia-smi found but failed to run — GPU passthrough may be broken."
    fi
else
    warn "nvidia-smi not found. Will install CPU-only torch + llama-cpp."
    warn "If you DO have an NVIDIA GPU on the Windows host:"
    warn "  1. Update the NVIDIA driver from https://www.nvidia.com/Download"
    warn "  2. From Windows PowerShell: wsl --shutdown && wsl --update"
    warn "  3. Re-run this script."
fi

# Pick CUDA wheel tier based on the driver's reported support.
CUDA_TIER="cpu"
if [ "$NVIDIA_OK" = "1" ] && [ -n "$CUDA_FROM_SMI" ]; then
    case "$CUDA_FROM_SMI" in
        12.8|12.9|13.*) CUDA_TIER="cu128" ;;
        12.4|12.5|12.6|12.7) CUDA_TIER="cu124" ;;
        12.0|12.1|12.2|12.3) CUDA_TIER="cu121" ;;
        *) CUDA_TIER="cu121"
           warn "Unknown CUDA $CUDA_FROM_SMI — defaulting to cu121 wheels." ;;
    esac
    say "  using CUDA wheel tier: $CUDA_TIER"
fi
# Allow override:
if [ -n "${COUNCIL_CUDA_TIER:-}" ]; then
    say "  override via COUNCIL_CUDA_TIER=${COUNCIL_CUDA_TIER}"
    CUDA_TIER="$COUNCIL_CUDA_TIER"
fi

# ── STEP 3: Miniforge install ────────────────────────────────
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

# ── STEP 4: create conda env ────────────────────────────────
say "STEP 4/7  — creating conda env 'wizardCouncil' (Python 3.11)"
if conda env list | grep -q '^wizardCouncil\s'; then
    say "  env 'wizardCouncil' already exists — reusing."
else
    conda config --add channels conda-forge >/dev/null
    conda config --set channel_priority strict >/dev/null
    conda create -n wizardCouncil python=3.11 -y
fi
conda activate wizardCouncil

# ── STEP 5: torch + llama-cpp-python (CUDA-tier-specific) ──
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
    beautifulsoup4 chromadb crawl4ai duckdb faster-whisper h5py \
    html2text huggingface_hub kaleido matplotlib openpyxl paramiko \
    plotly pyarrow pymongo pypdf python-docx PyYAML requests \
    scikit-learn scipy sentence-transformers sounddevice soundfile \
    SQLAlchemy tkinterweb
# crawl4ai-setup is a separate post-install step.
crawl4ai-setup >/dev/null 2>&1 || warn "crawl4ai-setup failed (non-fatal)"

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

# ── done ─────────────────────────────────────────────────────
echo
say "All set."
say "Next steps:"
say "  1. Put a .gguf model somewhere accessible (e.g. ~/models/)."
say "  2. ./run-wsl.sh   (or  COUNCIL_GGUF_PATH=~/models/foo.gguf ./run-wsl.sh)"
say
say "If the app crashes with 'illegal instruction' on first run, see"
say "the 'Illegal instruction (core dumped)' block in installs.txt."
