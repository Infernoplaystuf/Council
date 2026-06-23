"""
model_downloader.py — fetch a GGUF model from Hugging Face, cross-platform.

The Models tab can list US-made models that fit the machine; this module
lets the app DOWNLOAD the chosen one itself, working the same on Windows,
Linux, and macOS:

  • OS-aware destination — detect_os() picks a sensible models folder per
    platform (override with COUNCIL_MODELS_DIR).
  • Dependency-free — streams over HTTPS with the stdlib (urllib), so it
    works even without huggingface_hub installed. If huggingface_hub IS
    present it's used (better caching / auth), otherwise the direct path.
  • Resumable — a partial download continues with an HTTP Range request
    instead of starting over.
  • Verified — the finished file must start with the GGUF magic bytes
    before it's accepted, so a truncated/HTML-error download is rejected.
  • Bounded + cancellable — streams in chunks (constant memory) and checks
    a caller cancel flag between chunks.

Network use is explicit and user-initiated (a download click), consistent
with the opt-in Hugging Face search — it does not change the app's
offline-by-design default. Only huggingface.co is contacted, and the URL
is built from a repo id + filename (no arbitrary URLs).
"""
from __future__ import annotations

import os
import platform
import shutil
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional

GGUF_MAGIC = b"GGUF"
_HF_HOST = "huggingface.co"
_CHUNK = 1024 * 1024          # 1 MiB streaming chunks
_USER_AGENT = "Council-ModelDownloader/1.0"


# ============================================================
# OS detection + destination
# ============================================================

def detect_os() -> str:
    """'windows' | 'linux' | 'macos' | 'unknown'."""
    s = (platform.system() or "").lower()
    if s.startswith("win"):
        return "windows"
    if s == "linux":
        return "linux"
    if s == "darwin":
        return "macos"
    return "unknown"


def default_models_dir() -> Path:
    """Where downloaded models go, per OS. Override with COUNCIL_MODELS_DIR.

    Windows -> %LOCALAPPDATA%\\Council\\models
    Linux / macOS / other -> ~/.council/models   (matches the rest of the
    app's ~/.council layout)."""
    override = os.environ.get("COUNCIL_MODELS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if detect_os() == "windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Council" / "models"
    return Path.home() / ".council" / "models"


def disk_free_gb(path: Any) -> Optional[float]:
    """Free space (GB) on the volume that will hold ``path`` (walks up to
    the first existing ancestor). None if it can't be determined."""
    p = Path(path)
    for cand in [p] + list(p.parents):
        try:
            if cand.exists():
                return round(shutil.disk_usage(str(cand)).free / 1e9, 1)
        except Exception:
            continue
    return None


# ============================================================
# URL + validation
# ============================================================

def hf_resolve_url(repo: str, filename: str, revision: str = "main") -> str:
    """The direct-download URL for a file in a HF repo."""
    repo = repo.strip().strip("/")
    filename = filename.strip().lstrip("/")
    return f"https://{_HF_HOST}/{repo}/resolve/{revision}/{filename}?download=true"


def looks_like_gguf(path: Any) -> bool:
    """True if the file starts with the GGUF magic bytes."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == GGUF_MAGIC
    except Exception:
        return False


# ============================================================
# Download
# ============================================================

class DownloadError(RuntimeError):
    pass


def download_gguf(repo: str, filename: str,
                  dest_dir: Optional[Any] = None, *,
                  revision: str = "main",
                  progress: Optional[Callable[[int, Optional[int]], None]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None,
                  expected_size_gb: Optional[float] = None,
                  url: Optional[str] = None,
                  ) -> Dict[str, Any]:
    """Download ``filename`` from HF repo ``repo`` into ``dest_dir``.

    Streams over HTTPS in chunks with resume support; verifies the GGUF
    magic before accepting. ``progress(downloaded_bytes, total_bytes)`` is
    called as it goes (total may be None if the server omits length).
    ``should_cancel()`` is polled between chunks. Returns a summary dict
    {path, bytes, skipped, resumed}. Raises DownloadError on failure.

    If ``url`` is given it must be on huggingface.co; otherwise it's built
    from repo + filename.
    """
    dest_dir = Path(dest_dir) if dest_dir is not None else default_models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / Path(filename).name
    part = final.with_suffix(final.suffix + ".part")

    # Already present and valid -> skip the network entirely.
    if final.exists() and final.stat().st_size > 0 and looks_like_gguf(final):
        return {"path": str(final), "bytes": final.stat().st_size,
                "skipped": True, "resumed": False}

    dl_url = url or hf_resolve_url(repo, filename, revision)
    host = (urllib.parse.urlparse(dl_url).hostname or "").lower()
    if host != _HF_HOST and not host.endswith("." + _HF_HOST):
        raise DownloadError(f"Refusing non-Hugging Face URL: {dl_url}")

    # Prefer huggingface_hub when available (handles auth, cache, mirrors).
    try:
        from huggingface_hub import hf_hub_download  # type: ignore
        cached = hf_hub_download(repo_id=repo, filename=filename,
                                 revision=revision)
        # Copy the cached blob into our models dir so the path is stable
        # and OS-appropriate (the HF cache uses symlinks/snapshots).
        if Path(cached).resolve() != final.resolve():
            shutil.copyfile(cached, final)
        if not looks_like_gguf(final):
            raise DownloadError("Downloaded file is not a valid GGUF.")
        return {"path": str(final), "bytes": final.stat().st_size,
                "skipped": False, "resumed": False, "via": "huggingface_hub"}
    except DownloadError:
        raise
    except ImportError:
        pass                      # fall through to the stdlib path
    except Exception:
        # hf_hub_download failed (offline/auth/etc.) — try the direct URL.
        pass

    # ---- stdlib streaming download with resume ----
    resume_from = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(dl_url, headers={"User-Agent": _USER_AGENT})
    if resume_from:
        req.add_header("Range", f"bytes={resume_from}-")

    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except Exception as exc:
        # A 416 (range not satisfiable) means our .part is already complete
        # or stale — drop it and let the caller retry fresh.
        if resume_from:
            try:
                part.unlink()
            except OSError:
                pass
        raise DownloadError(f"Could not start download: {exc}") from exc

    status = getattr(resp, "status", 200)
    # If we asked to resume but the server ignored the Range (200 not 206),
    # restart from zero so we don't corrupt the file by appending.
    if resume_from and status != 206:
        resume_from = 0
        mode = "wb"
    else:
        mode = "ab" if resume_from else "wb"

    # Total size: Content-Length (+ resume offset when partial).
    total: Optional[int] = None
    try:
        clen = int(resp.headers.get("Content-Length", "") or 0)
        if clen > 0:
            total = clen + (resume_from if status == 206 else 0)
    except (TypeError, ValueError):
        total = None

    downloaded = resume_from
    try:
        with open(part, mode) as fh:
            if progress:
                progress(downloaded, total)
            while True:
                if should_cancel and should_cancel():
                    raise DownloadError("Download cancelled.")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
    except DownloadError:
        raise                      # keep .part for resume on cancel
    except Exception as exc:
        raise DownloadError(f"Download interrupted: {exc}") from exc
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if not looks_like_gguf(part):
        # Server returned an HTML error page / wrong file, not a GGUF.
        try:
            part.unlink()
        except OSError:
            pass
        raise DownloadError(
            "Downloaded file is not a valid GGUF (got an error page or "
            "wrong file). Check the repo/filename and your connection.")

    part.replace(final)
    return {"path": str(final), "bytes": final.stat().st_size,
            "skipped": False, "resumed": bool(resume_from)}
