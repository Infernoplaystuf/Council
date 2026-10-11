"""Start the LLM server for one job and stop it afterwards (saves RAM on a
Pi). Configure ``llm.server.command`` — e.g. llama.cpp's

    [~/llama.cpp/build/bin/llama-server, -m, ~/models/model.gguf,
     --host, 127.0.0.1, --port, "8080", -c, "2048", -t, "4"]

If a server already answers at ``llm.url`` (Ollama on a PC, say) it is used
as is and left running. With no command configured, nothing is started.
Callers hold the machine-wide LLM lock around this, so two LLM jobs never
run at once.
"""
from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .client import LLMError


def _base(url: str) -> str:
    u = url.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def is_up(url: str, timeout_s: float = 2.0) -> bool:
    """True if the server answers its health or model-list endpoint."""
    for path in ("/health", "/v1/models", "/api/tags"):
        try:
            with urllib.request.urlopen(_base(url) + path,
                                        timeout=timeout_s) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def _command(server: Dict[str, Any]) -> Optional[List[str]]:
    cmd = server.get("command")
    if not cmd:
        return None
    if isinstance(cmd, str):
        raise LLMError("llm.server.command must be a list of arguments, "
                       "not one string (no shell is used)")
    return [os.path.expanduser(str(c)) for c in cmd]


@contextmanager
def llm_server(llm_cfg: Dict[str, Any], log_path: Optional[Path] = None
               ) -> Iterator[Dict[str, Any]]:
    """Yield ``{"started": bool}``; raise ``LLMError`` if a configured
    server will not come up within ``llm.server.start_timeout_s``."""
    url = llm_cfg["url"]
    server = llm_cfg.get("server") or {}
    if is_up(url):
        yield {"started": False}
        return
    cmd = _command(server)
    if cmd is None:
        yield {"started": False}          # the client reports it as down
        return
    if not Path(cmd[0]).exists() and "/" in cmd[0]:
        raise LLMError(f"LLM server binary not found: {cmd[0]}")
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        if log_path:
            out.close()
        raise LLMError(f"could not start the LLM server: {exc}") from exc
    try:
        deadline = time.monotonic() + float(server.get("start_timeout_s", 120))
        while not is_up(url):
            if proc.poll() is not None:
                raise LLMError(f"LLM server exited with code {proc.returncode}"
                               " while starting")
            if time.monotonic() > deadline:
                raise LLMError("LLM server did not come up in time")
            time.sleep(0.5)
        yield {"started": True}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=float(server.get("stop_timeout_s", 20)))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if log_path:
            out.close()
