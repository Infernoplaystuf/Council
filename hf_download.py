"""
Hugging Face model download helper — drives `huggingface-cli` so users
can pull a GGUF from inside the council instead of dropping to a shell.

Used by the "download <repo> <file>" chat intent. Streams progress lines
back through a callback so the GUI can mirror them in the transcript.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple


def hf_cli_available() -> bool:
    """Best-effort check that huggingface-cli is importable. We do this
    via `python -m huggingface_hub` so we don't depend on a separate
    binary being on PATH."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "huggingface_hub", "--help"],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


def download_gguf(
    repo: str,
    filename: str,
    *,
    dest_dir: Path,
    on_progress: Optional[Callable[[str], None]] = None,
    timeout_s: int = 1800,
) -> Tuple[bool, str, Optional[Path]]:
    """Download a single GGUF file from a Hugging Face repo.

    Args:
      repo:      "org/name" — e.g. "bartowski/granite-3.0-8b-instruct-GGUF"
      filename:  the specific .gguf file in that repo
      dest_dir:  local folder to write into
      on_progress: optional callback receiving each stdout/stderr line
      timeout_s: max wall clock; default 30 minutes (multi-GB files)

    Returns (success, message, downloaded_path). Streams progress lines
    via on_progress so the GUI can show download status.
    """
    if not repo.strip() or "/" not in repo:
        return False, f"invalid repo '{repo}' — expected 'org/name'", None
    if not filename.strip():
        return False, "missing filename", None

    dest = Path(dest_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "huggingface_hub", "download",
        repo.strip(), filename.strip(),
        "--local-dir", str(dest),
    ]
    if on_progress:
        on_progress(f"$ {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(dest),
            # On Windows, hide the console window
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32"
                           and hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
        )
    except Exception as exc:
        return False, f"failed to start huggingface-cli: {exc!r}", None

    # Stream output line by line
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line and on_progress:
                on_progress(line)
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, f"timed out after {timeout_s}s", None
    except Exception as exc:
        return False, f"download stream failed: {exc!r}", None

    if rc != 0:
        return False, f"huggingface-cli exited rc={rc}", None

    final = dest / filename
    if final.exists():
        return True, f"downloaded to {final}", final
    # huggingface_hub sometimes puts the file in a versioned subdir.
    # Find it by name and report the actual path.
    for p in dest.rglob(filename):
        if p.is_file():
            return True, f"downloaded to {p}", p
    return True, f"download finished but file not found by exact name", None
