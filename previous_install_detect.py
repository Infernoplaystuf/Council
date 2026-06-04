"""
previous_install_detect.py — find traces of a prior Council install
on the user's machine so the setup wizard can offer to reuse them
instead of asking for everything from scratch.

What we look for:
  * Existing `council` conda env (any flavour: miniconda / miniforge
    / anaconda / mamba).
  * Existing vault directory with content (data_in/ has user files,
    or backend_settings.json points at a model).
  * Existing GGUF model file in standard locations.
  * Persisted backend settings from a previous run (in case the
    user reinstalls the app on the same vault).

Never modifies anything. Pure probe + report. The wizard owns the
decision of what to reuse.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Public API
# ============================================================

def detect(app_dir: Path, vault_dir: Path) -> Dict[str, Any]:
    """Snapshot every reusable artifact we can find.

    Returns a dict with:
        conda_env:        {"present": bool, "path": str|None, "tool": str|None}
        vault:            {"present": bool, "path": str, "data_in_files": int,
                            "has_settings": bool}
        gguf_models:      list of {"path": str, "size_gb": float, "valid": bool}
        previous_model:   absolute path of last-used model (or None)
        prior_version:    version string from prior install (or None)
        notes:            list of human-readable observations
    """
    info: Dict[str, Any] = {
        "conda_env":      _detect_conda_env(),
        "vault":          _detect_vault(vault_dir),
        "gguf_models":    _detect_gguf_models(),
        "previous_model": _previous_model(vault_dir),
        "prior_version":  _prior_version(app_dir),
        "notes":          [],
    }
    # Cross-reference: if the previous model still exists on disk, the
    # wizard can offer "use the model you used last time" as a one-click.
    pm = info["previous_model"]
    if pm and not Path(pm).exists():
        info["notes"].append(
            f"Previous model {pm!r} is no longer on disk — it was "
            "probably moved or the drive was unmounted.")
        info["previous_model"] = None
    return info


# ============================================================
# Conda env
# ============================================================

def _detect_conda_env() -> Dict[str, Any]:
    """Look for a `council` conda env across the common conda install
    flavours. The setup wizard / setup-wsl.sh both use this name."""
    # Try the canonical CLIs in order. micromamba is the lightest;
    # mamba and conda both work the same way for env listing.
    for tool in ("conda", "mamba", "micromamba"):
        if not shutil.which(tool):
            continue
        try:
            r = subprocess.run([tool, "env", "list"],
                                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if parts[0] == "council":
                        return {
                            "present": True,
                            "path":    parts[-1] if len(parts) > 1 else None,
                            "tool":    tool,
                        }
        except Exception:
            continue
    # Last-chance manual scan of common install dirs
    home = Path.home()
    candidates = [
        home / "miniforge3"  / "envs" / "council",
        home / "miniconda3"  / "envs" / "council",
        home / "anaconda3"   / "envs" / "council",
        home / "mambaforge"  / "envs" / "council",
        Path("/opt/conda")   / "envs" / "council",
    ]
    for c in candidates:
        if c.is_dir():
            return {"present": True, "path": str(c), "tool": None}
    return {"present": False, "path": None, "tool": None}


# ============================================================
# Vault
# ============================================================

def _detect_vault(vault_dir: Path) -> Dict[str, Any]:
    out = {
        "present":        vault_dir.is_dir(),
        "path":           str(vault_dir),
        "data_in_files":  0,
        "has_settings":   False,
    }
    if not out["present"]:
        return out
    data_in = vault_dir / "data_in"
    if data_in.is_dir():
        try:
            # Cheap count — bounded so an enormous vault doesn't make
            # the wizard hang. We just need to know "is it non-empty?"
            n = 0
            for _ in data_in.rglob("*"):
                n += 1
                if n >= 1000:
                    break
            out["data_in_files"] = n
        except Exception:
            pass
    if (vault_dir / "backend_settings.json").is_file():
        out["has_settings"] = True
    return out


# ============================================================
# GGUF models
# ============================================================

_GGUF_SEARCH_ROOTS = (
    Path.home() / "models",
    Path.home() / "Downloads",
    Path.home() / ".cache" / "huggingface" / "hub",
)


def _detect_gguf_models() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # Plus /mnt/c/Users/<user>/models on WSL.
    roots = list(_GGUF_SEARCH_ROOTS)
    try:
        username = os.environ.get("USER") or os.environ.get("USERNAME") or ""
        if username and sys.platform.startswith("linux"):
            roots.append(Path("/mnt/c/Users") / username / "models")
    except Exception:
        pass
    seen_paths: set = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for p in root.rglob("*.gguf"):
                rp = str(p.resolve())
                if rp in seen_paths:
                    continue
                seen_paths.add(rp)
                try:
                    size_gb = round(p.stat().st_size / (1024 ** 3), 2)
                except Exception:
                    size_gb = 0.0
                out.append({
                    "path":     rp,
                    "size_gb":  size_gb,
                    "valid":    _quick_gguf_validate(p),
                    "name":     p.name,
                })
                if len(out) >= 20:
                    return out
        except Exception:
            continue
    return out


def _quick_gguf_validate(p: Path) -> bool:
    """Cheap header check — just read the first 4 bytes and confirm
    they match the GGUF magic. Doesn't validate the rest of the file
    (full parse is in council_engine.read_gguf_metadata), but catches
    the most common failure: a partial download from huggingface-cli
    that was interrupted mid-stream and left a zero-byte file or an
    HTML error page on disk with the .gguf extension."""
    try:
        if p.stat().st_size < 1024:
            return False
        with open(p, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except Exception:
        return False


# ============================================================
# Previous-model pointer
# ============================================================

def _previous_model(vault_dir: Path) -> Optional[str]:
    settings = vault_dir / "backend_settings.json"
    if not settings.is_file():
        return None
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        p = str(data.get("gguf_path", "")).strip()
        return p or None
    except Exception:
        return None


# ============================================================
# Prior version
# ============================================================

def _prior_version(app_dir: Path) -> Optional[str]:
    """Look for a previous app version marker. We don't currently write
    one explicitly — but a future branding.VERSION pin could land in
    vault/.app_version. For now, return whatever's there."""
    for candidate in (
        app_dir / "vault" / ".app_version",
        app_dir / ".app_version",
    ):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip() or None
        except Exception:
            continue
    return None


# ============================================================
# CLI for ad-hoc inspection
# ============================================================

if __name__ == "__main__":
    import json as _json
    here = Path(__file__).parent.resolve()
    print(_json.dumps(
        detect(here, here / "vault"),
        indent=2,
        default=str,
    ))
