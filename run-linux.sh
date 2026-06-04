#!/usr/bin/env bash
# ============================================================
# run-linux.sh — one-command launcher for Council on native Linux
# (NOT WSL — use run-wsl.sh for that).
#
# After setup-linux.sh has run once, this script handles launches:
#   • activates the council conda env
#   • finds a .gguf model in ~/models, ~/Downloads, ./models
#   • exports COUNCIL_BACKEND=gguf and sensible defaults
#   • launches the Tkinter GUI
#   • falls back to CPU offload if the GPU launch SIGILLs
#
# Usage:
#   ./run-linux.sh
#   COUNCIL_GGUF_PATH=~/models/granite-3.1-8b.gguf ./run-linux.sh
#   COUNCIL_GGUF_GPU_LAYERS=0 ./run-linux.sh   # force CPU
# ============================================================

set -u

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'
say()  { echo -e "${GREEN}[run-linux]${RESET} $*"; }
warn() { echo -e "${YELLOW}[run-linux]${RESET} $*"; }
die()  { echo -e "${RED}[run-linux]${RESET} $*" >&2; exit 1; }

# ── Activate conda env ──────────────────────────────────────
if [ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniforge3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif command -v conda >/dev/null 2>&1; then
    :
else
    die "conda not found. Run ./setup-linux.sh first."
fi
conda activate council 2>/dev/null || die "conda env 'council' missing. Run ./setup-linux.sh."

# ── Find a GGUF model ───────────────────────────────────────
if [ -z "${COUNCIL_GGUF_PATH:-}" ]; then
    for candidate in \
        "$HOME/models"/*.gguf \
        "$HOME/Downloads"/*.gguf \
        "$PWD/models"/*.gguf; do
        if [ -f "$candidate" ]; then
            export COUNCIL_GGUF_PATH="$candidate"
            say "found model: $COUNCIL_GGUF_PATH"
            break
        fi
    done
fi
if [ -z "${COUNCIL_GGUF_PATH:-}" ]; then
    warn "No GGUF found in ~/models, ~/Downloads, or ./models."
    warn "Download one (Granite 3.1 8B Q4_K_M recommended for 16 GB VRAM):"
    warn "  python -c \"from huggingface_hub import hf_hub_download as h; \\"
    warn "    h(repo_id='bartowski/granite-3.1-8b-instruct-GGUF', \\"
    warn "      filename='granite-3.1-8b-instruct-Q4_K_M.gguf', \\"
    warn "      local_dir='$HOME/models')\""
elif [ ! -f "$COUNCIL_GGUF_PATH" ]; then
    die "COUNCIL_GGUF_PATH points at a nonexistent file: $COUNCIL_GGUF_PATH"
fi

# ── Backend + sensible defaults ─────────────────────────────
export COUNCIL_BACKEND=gguf
: "${COUNCIL_GGUF_GPU_LAYERS:=99}"
: "${COUNCIL_GGUF_N_CTX_DEBUG:=1}"
export COUNCIL_GGUF_GPU_LAYERS COUNCIL_GGUF_N_CTX_DEBUG

# ── Launch ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

say "GPU layers: $COUNCIL_GGUF_GPU_LAYERS"
[ -n "${COUNCIL_GGUF_PATH:-}" ] && say "model: $COUNCIL_GGUF_PATH"

python council_gui_engine.py
EXIT=$?

if [ "$EXIT" = "132" ] || [ "$EXIT" = "139" ]; then
    # 132 = SIGILL (illegal instruction), 139 = SIGSEGV
    warn "Process exited with code $EXIT (likely SIGILL from the GPU path)."
    if [ "${COUNCIL_GGUF_GPU_LAYERS}" != "0" ]; then
        warn "Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 (CPU only)..."
        export COUNCIL_GGUF_GPU_LAYERS=0
        python council_gui_engine.py
        EXIT=$?
    fi
fi

exit "$EXIT"
