#!/usr/bin/env bash
# ============================================================
# run-wsl.sh — one-command launcher for the Council app on WSL.
#
# After setup-wsl.sh has run once, this script handles every
# subsequent launch:
#   • activates the council conda env
#   • finds a .gguf model in standard locations (or honours
#     $COUNCIL_GGUF_PATH if you set it)
#   • exports a few sensible defaults for WSL (UI scale,
#     timing-debug, etc.)
#   • launches the GUI
#   • if the GPU launch crashes with SIGILL / 'illegal
#     instruction', retries on CPU automatically and tells you.
#
# Usage:
#   ./run-wsl.sh
#   ./run-wsl.sh --tk                                     # the classic Tk UI
#   ./run-wsl.sh --advanced                  # any other argument goes to the app
#   COUNCIL_GGUF_PATH=~/models/phi-4.gguf  ./run-wsl.sh
#   COUNCIL_GGUF_GPU_LAYERS=0              ./run-wsl.sh   # force CPU
#   COUNCIL_UI_SCALE=1.8                   ./run-wsl.sh   # bigger text
# ============================================================

set -u

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'
say()  { echo -e "${GREEN}[run-wsl]${RESET} $*"; }
warn() { echo -e "${YELLOW}[run-wsl]${RESET} $*"; }
die()  { echo -e "${RED}[run-wsl]${RESET} $*" >&2; exit 1; }

# ── Locate conda + activate wizardCouncil env ───────────────
if [ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniforge3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif command -v conda >/dev/null 2>&1; then
    # conda already on PATH from .bashrc — just `conda activate` works.
    :
else
    die "conda not found. Run ./setup-wsl.sh first."
fi
conda activate wizardCouncil 2>/dev/null || die "conda env 'wizardCouncil' not found. Run ./setup-wsl.sh."

# ── Find the GGUF model ─────────────────────────────────────
# COUNCIL_GGUF_PATH_AUTO=1 tells the app the path is OUR guess, not the
# user's choice, so the main model saved in the app wins over it while that
# file is on disk (onboarding.apply_saved_gguf_path). Without it the guess
# beat the saved choice on every launch — measured 2026-09-29 through
# run-windows.bat: always granite-3.1-8b from ~/models, whatever was picked.
# Unset first: only the auto-pick below sets it, so an inherited value can
# never turn a path the user exported into a guess.
unset COUNCIL_GGUF_PATH_AUTO
if [ -z "${COUNCIL_GGUF_PATH:-}" ]; then
    for candidate in \
        "$HOME/models"/*.gguf \
        "$HOME/Downloads"/*.gguf \
        "$PWD/models"/*.gguf \
        "/mnt/c/Users/$USER/models"/*.gguf; do
        if [ -f "$candidate" ]; then
            export COUNCIL_GGUF_PATH="$candidate"
            export COUNCIL_GGUF_PATH_AUTO=1
            say "found model: $COUNCIL_GGUF_PATH"
            say "(the first .gguf found — a main model saved in the app is used instead while it is on disk)"
            break
        fi
    done
fi
if [ -z "${COUNCIL_GGUF_PATH:-}" ]; then
    warn "No GGUF model found in ~/models, ~/Downloads, ./models, or"
    warn "/mnt/c/Users/$USER/models. The app will open but the model"
    warn "won't load until you set one via Browse... in the UI."
    warn "Faster: COUNCIL_GGUF_PATH=/path/to/file.gguf ./run-wsl.sh"
elif [ ! -f "$COUNCIL_GGUF_PATH" ]; then
    die "COUNCIL_GGUF_PATH is set to a file that doesn't exist: $COUNCIL_GGUF_PATH"
fi

# ── WSL-friendly defaults (only set if not already set) ─────
# These keep the launch comfortable on a fresh WSL env. None of
# them override values the user has already exported.
: "${COUNCIL_UI_SCALE:=1.5}"        # WSLg reports 96 DPI → text is tiny
: "${COUNCIL_GGUF_GPU_LAYERS:=99}"  # offload everything to GPU; harmless on CPU
: "${COUNCIL_GGUF_N_CTX_DEBUG:=1}"  # dump the n_ctx ladder so users can debug
# Embeddings on CPU by default — matches run-windows.bat. With the GGUF
# fully offloaded (GPU_LAYERS=99) AND sentence-transformers also on the
# GPU, a smaller-VRAM card (≤ ~8 GB) runs both + the KV cache + per-turn
# GPU encoding in the same VRAM. After a couple of messages that
# exhausts VRAM and llama-cpp aborts with a CUDA out-of-memory — a
# NATIVE crash (core dump, no Python traceback). Pinning embeddings to
# the CPU costs a little embedding speed but leaves the whole GPU for
# the model. Set COUNCIL_EMBED_DEVICE=cuda before launch to override on
# a big-VRAM card.
: "${COUNCIL_EMBED_DEVICE:=cpu}"
export COUNCIL_UI_SCALE COUNCIL_GGUF_GPU_LAYERS COUNCIL_GGUF_N_CTX_DEBUG
export COUNCIL_EMBED_DEVICE

# Tk needs DISPLAY on Windows 10 (no WSLg). WSLg sets it for us
# on Windows 11. If we're on Win 10 and DISPLAY is unset, use the
# nameserver IP pattern that VcXsrv / X410 expects.
if [ -z "${DISPLAY:-}" ]; then
    NS=$(grep -m1 nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}')
    if [ -n "$NS" ]; then
        export DISPLAY="$NS:0"
        say "exported DISPLAY=$DISPLAY (Windows 10 / X server path)"
    fi
fi

# ── Launch ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

say "GPU layers: $COUNCIL_GGUF_GPU_LAYERS   UI scale: $COUNCIL_UI_SCALE"
if [ -n "${COUNCIL_GGUF_PATH:-}" ]; then
    say "model:      $COUNCIL_GGUF_PATH"
fi

# Run the app. On a NATIVE crash from the GPU path (the common WSL failure
# mode for prebuilt CUDA wheels) retry once with GPU offload disabled, so
# the user gets the UI + a clear diagnostic instead of a silent core dump.
# Signals that mean "the C extension aborted", any of which a bad CUDA
# build/driver can raise:
#   132 SIGILL · 133 SIGTRAP · 134 SIGABRT (CUDA assert/abort — the
#   "CUDA error … core dumped" case) · 135 SIGBUS · 136 SIGFPE · 139 SIGSEGV
# Which UI: the Qt app by default; the classic Tk app with --tk or
# COUNCIL_UI=tk, or automatically when PySide6 is not installed.
# --tk is the launcher's own, wherever it is on the line; every OTHER argument
# is the app's (--advanced, ...) and is passed on, in order, as typed. They
# used to be dropped: only $1 was looked at and the app was started with no
# arguments, so `./run-wsl.sh --advanced` opened the default build.
APP_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --tk) COUNCIL_UI=tk ;;
        *) APP_ARGS+=("$arg") ;;
    esac
done
COUNCIL_ENTRY=council_qt.py
[ "${COUNCIL_UI:-}" = "tk" ] && COUNCIL_ENTRY=council_gui_engine.py
if [ "$COUNCIL_ENTRY" = "council_qt.py" ] && ! python -c "import PySide6" >/dev/null 2>&1; then
    warn "PySide6 is not installed in this environment, so the classic Tk UI is starting."
    warn "For the new UI:  python -m pip install PySide6"
    COUNCIL_ENTRY=council_gui_engine.py
fi
say "launching $COUNCIL_ENTRY ..."
# ${APP_ARGS[@]+...}: under `set -u`, a bare "${APP_ARGS[@]}" of an EMPTY
# array is an "unbound variable" error on bash before 4.4.
python "$COUNCIL_ENTRY" ${APP_ARGS[@]+"${APP_ARGS[@]}"}
EXIT=$?

case "$EXIT" in
  132|133|134|135|136|139)
    warn "Process exited with code $EXIT (native crash from the GPU path —"
    warn "SIGILL/SIGABRT/SIGSEGV etc., typically a CUDA wheel/driver issue)."
    if [ "${COUNCIL_GGUF_GPU_LAYERS}" != "0" ]; then
        warn "Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 (CPU only)..."
        warn "If this works, the issue is a CUDA wheel / driver mismatch."
        warn "See installs.txt — 'Illegal instruction / CUDA core dumped'."
        export COUNCIL_GGUF_GPU_LAYERS=0
        python "$COUNCIL_ENTRY" ${APP_ARGS[@]+"${APP_ARGS[@]}"}
        EXIT=$?
    fi
    ;;
esac

exit "$EXIT"
