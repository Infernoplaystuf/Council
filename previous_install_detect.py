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

def detect(app_dir: Path, vault_dir: Path,
            env_name: str = "council") -> Dict[str, Any]:
    """Snapshot every reusable artifact we can find.

    Returns a dict with:
        conda_env:        {"present": bool, "path": str|None, "tool": str|None,
                            "candidates_checked": list[str], "notes": list[str]}
        vault:            {"present": bool, "path": str, "data_in_files": int,
                            "has_settings": bool, "alternates_checked": list[str],
                            "notes": list[str]}
        gguf_models:      list of {"path": str, "size_gb": float, "valid": bool}
        previous_model:   absolute path of last-used model (or None)
        prior_version:    version string from prior install (or None)
        notes:            list of human-readable observations

    Both conda_env and vault carry candidates_checked / alternates_checked
    so the caller can SHOW the user exactly what was probed. The
    setup script does this — without the diagnostic, a user reporting
    "doesn't find my pre-existing env" has no way to verify their
    install location was on the search list.

    The vault probe also falls back to the canonical ~/.council/vault
    location when the caller-supplied vault_dir comes up empty. This
    catches the common "setup script asked about the repo's local
    ./vault but the real one is in ~/.council/vault" case.
    """
    info: Dict[str, Any] = {
        "conda_env":      _detect_conda_env(env_name=env_name),
        "vault":          _detect_vault(vault_dir),
        "gguf_models":    _detect_gguf_models(),
        "previous_model": _previous_model(vault_dir),
        "prior_version":  _prior_version(app_dir),
        "notes":          [],
    }
    # If the vault detector fell back to ~/.council/vault, also re-
    # check the previous_model against THAT path — the original
    # vault_dir was a bad guess and its backend_settings.json (if any)
    # is not the user's actual one.
    if not info["previous_model"] and info["vault"].get("present"):
        v_actual = Path(info["vault"]["path"])
        if v_actual != vault_dir:
            info["previous_model"] = _previous_model(v_actual)
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

def _detect_conda_env(env_name: str = "council") -> Dict[str, Any]:
    """Look for the named conda env across the common conda install
    flavours. Default name is 'council' (used by setup-wsl.sh and
    setup_council.py); callers can override for custom env names.

    Returns:
        {
          "present":             bool,
          "path":                str | None,
          "tool":                str | None,   # which CLI listed it
          "candidates_checked":  list[str],    # every path we probed
          "notes":               list[str],    # diagnostics for the UI
        }

    The candidates_checked + notes fields are SHOWN to the user in
    the setup script so they can verify their actual install location
    was probed. Previously the detector returned a silent False on
    miss, which made "doesn't see my pre-existing env" reports
    impossible to diagnose.
    """
    out: Dict[str, Any] = {
        "present": False, "path": None, "tool": None,
        "candidates_checked": [], "notes": [],
    }

    # ── Try the canonical CLIs ──────────────────────────────────────
    # 20 s timeout per call (was 5 s — too tight for cold conda on
    # spinning disks or slow VMs). conda env list on a freshly-booted
    # WSL routinely takes 8-12 s.
    for tool in ("conda", "mamba", "micromamba"):
        if not shutil.which(tool):
            out["notes"].append(f"{tool!r} not on PATH")
            continue
        out["notes"].append(f"running `{tool} env list`")
        try:
            r = subprocess.run(
                [tool, "env", "list"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                out["notes"].append(
                    f"`{tool} env list` returned {r.returncode}; "
                    f"stderr: {r.stderr[:200].strip()!r}")
                continue
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Lines look like:
                #   base                  *  /opt/conda
                #   council                  /opt/conda/envs/council
                # The active env has '*' in column 2; we don't care
                # about activation state, just the name match.
                parts = line.split()
                if parts and parts[0] == env_name:
                    return {
                        "present":             True,
                        "path":                parts[-1] if len(parts) > 1 else None,
                        "tool":                tool,
                        "candidates_checked":  out["candidates_checked"],
                        "notes":               out["notes"]
                                                + [f"matched line: {line!r}"],
                    }
        except subprocess.TimeoutExpired:
            out["notes"].append(
                f"`{tool} env list` timed out after 20s — conda may "
                "be busy. Try running `conda env list` from a fresh "
                "terminal to confirm.")
        except Exception as exc:
            out["notes"].append(f"`{tool} env list` raised {exc!r}")

    # ── Manual filesystem scan ──────────────────────────────────────
    # Covers every flavour of conda install I've seen on Windows
    # (Miniforge / Miniconda / Anaconda Distribution / Mambaforge,
    # both user-local and machine-wide), Linux/WSL, and macOS.
    # Every probed path goes into candidates_checked so the user
    # can see whether their actual install location was missed.
    home  = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA")) if os.environ.get("LOCALAPPDATA") else None

    candidates: List[Path] = [
        # User-local Miniforge / Miniconda / Anaconda
        home / "miniforge3"  / "envs" / env_name,
        home / "miniconda3"  / "envs" / env_name,
        home / "anaconda3"   / "envs" / env_name,
        home / "mambaforge"  / "envs" / env_name,
        # Windows newer-installer locations (Anaconda Distribution 2024+)
        *([
            local / "miniforge3" / "envs" / env_name,
            local / "anaconda3"  / "envs" / env_name,
            local / "miniconda3" / "envs" / env_name,
        ] if local else []),
        # Windows machine-wide installs
        Path("C:/ProgramData/miniforge3") / "envs" / env_name,
        Path("C:/ProgramData/Miniconda3") / "envs" / env_name,
        Path("C:/ProgramData/Anaconda3")  / "envs" / env_name,
        Path("C:/miniforge3") / "envs" / env_name,
        Path("C:/Miniconda3") / "envs" / env_name,
        Path("C:/Anaconda3")  / "envs" / env_name,
        # Linux machine-wide
        Path("/opt/conda")   / "envs" / env_name,
        Path("/opt/miniconda3") / "envs" / env_name,
        Path("/opt/miniforge3") / "envs" / env_name,
        Path("/usr/local/miniforge3") / "envs" / env_name,
        # macOS Homebrew
        Path("/opt/homebrew/Caskroom/miniforge/base/envs") / env_name,
    ]
    for c in candidates:
        out["candidates_checked"].append(str(c))
        if c.is_dir():
            return {
                "present":            True,
                "path":               str(c),
                "tool":               None,    # filesystem-only find
                "candidates_checked": out["candidates_checked"],
                "notes":              out["notes"]
                                       + [f"filesystem match: {c}"],
            }

    out["notes"].append(
        f"env {env_name!r} not found in any of "
        f"{len(out['candidates_checked'])} candidate paths.")
    return out


# ============================================================
# Vault
# ============================================================

def _detect_vault(vault_dir: Path) -> Dict[str, Any]:
    """Probe the vault directory.

    Returns:
        {
          "present":            bool,
          "path":               str (the path we checked),
          "data_in_files":      int (bounded count),
          "has_settings":       bool (backend_settings.json present),
          "alternates_checked": list[str] (other plausible vault paths
                                            we probed before giving up),
          "notes":              list[str] (diagnostics for the UI),
        }

    The alternates_checked + notes were added after a report that the
    setup script "can't find a pre-existing vault that stays
    consistent normally on where it saves at." That report was caused
    by setup_council passing the WRONG path to this function — the
    in-app vault lives at ~/.council/vault but setup was probing
    <repo>/vault. Now we also probe the canonical ~/.council/vault
    location AS A FALLBACK if the caller's path comes up empty,
    instead of silently reporting no vault.
    """
    out: Dict[str, Any] = {
        "present":             vault_dir.is_dir(),
        "path":                str(vault_dir),
        "data_in_files":       0,
        "has_settings":        False,
        "alternates_checked":  [],
        "notes":               [],
    }

    def _populate_from(p: Path) -> None:
        out["path"]      = str(p)
        out["present"]   = True
        data_in = p / "data_in"
        if data_in.is_dir():
            try:
                n = 0
                for _ in data_in.rglob("*"):
                    n += 1
                    if n >= 1000:
                        break
                out["data_in_files"] = n
            except Exception:
                pass
        if (p / "backend_settings.json").is_file():
            out["has_settings"] = True

    if out["present"]:
        _populate_from(vault_dir)
        out["notes"].append(f"vault found at caller-supplied path: {vault_dir}")
        return out

    # Fallback — check the canonical ~/.council/vault that the GUI
    # actually uses. The setup script may have asked us to look in
    # the wrong place (the repo's own ./vault, an installer scratch
    # dir, etc.). This catches the common case where a user has run
    # the app before and has a real vault elsewhere.
    canonical = Path.home() / ".council" / "vault"
    out["alternates_checked"].append(str(canonical))
    out["notes"].append(
        f"caller path {vault_dir} is empty; "
        f"trying canonical ~/.council/vault → {canonical}")
    if canonical.is_dir():
        _populate_from(canonical)
        out["notes"].append("found canonical vault — using that one")
        return out

    out["notes"].append("no vault at canonical location either")
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
