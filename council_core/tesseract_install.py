"""
council_core.tesseract_install — get Tesseract (the Ledger's and the
Connections graph's image reader) onto this PC from the Models tab.

User 2026-10-22: "please also add into the Models tab an easy way to install
tesseract".

Tesseract is a program, not a model: classic OCR, begun at HP and developed
by Google (Apache-2.0, US origin). The Council does not download it itself.
It asks the system's own installer, when the user clicks, for the official
package:

* Windows - ``winget`` (built into Windows 10 and 11), package
  ``UB-Mannheim.TesseractOCR``: the build Tesseract's own documentation
  points Windows users to. It installs to ``Program Files\\Tesseract-OCR``,
  where ``source_extract.find_tesseract`` looks. Windows asks the user to
  allow it.
* Without winget, or on any system with conda - a conda environment named
  ``tesseract`` from conda-forge (no administrator rights), also where
  ``find_tesseract`` looks.
* macOS - Homebrew (``brew install tesseract``).
* Linux - the package manager needs the user's password: the command to type
  is shown, not run.

``locate(path)`` remembers a tesseract executable installed anywhere else
(``<app dir>/tesseract.json``; ``find_tesseract`` reads it).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

WINGET_ID = "UB-Mannheim.TesseractOCR"
SAVED_NAME = "tesseract.json"


@dataclass
class Plan:
    """How Tesseract would be installed here."""
    kind: str                         # winget | conda | brew | manual
    argv: List[str] = field(default_factory=list)   # [] for manual
    says: str = ""                    # one sentence for the confirm dialog
    manual: str = ""                  # what to type, when it cannot be run

    @property
    def runnable(self) -> bool:
        return bool(self.argv)


@dataclass
class Result:
    ok: bool
    found: Optional[Path]
    log: List[str]
    message: str


# ── where it is ─────────────────────────────────────────────────────────
def saved_file() -> Path:
    from council_core import paths
    return paths.app_dir() / SAVED_NAME


def saved_path() -> Optional[Path]:
    """The tesseract executable the user located (Locate…), if it is still
    there."""
    try:
        p = Path(json.loads(saved_file().read_text(encoding="utf-8"))["path"])
        return p if p.is_file() else None
    except Exception:                                     # noqa: BLE001
        return None


def locate(path) -> Path:
    """Remember ``path`` as the tesseract to use. Raises ValueError when it
    is not a tesseract that runs."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{p} is not a file")
    if not version(p):
        raise ValueError(f"{p.name} did not answer as Tesseract does (tesseract --version)")
    f = saved_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps({"path": str(p)}, indent=1), encoding="utf-8")
    os.replace(tmp, f)
    return p


def find() -> Optional[Path]:
    import source_extract as se
    return se.find_tesseract()


def version(exe: Optional[Path] = None) -> str:
    """'tesseract 5.4.0' from ``exe --version`` (the found one when None);
    '' when it is not there or does not answer."""
    exe = exe or find()
    if exe is None:
        return ""
    try:
        done = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                              timeout=20, creationflags=_no_window())
        text = (done.stdout or done.stderr or "").strip().splitlines()
        return text[0].strip() if text else ""
    except Exception:                                     # noqa: BLE001
        return ""


def status() -> str:
    """One line for the Models tab."""
    exe = find()
    if exe is None:
        return ("Tesseract (reads text and tables in pictures, for the Ledger and "
                "Connections): not installed")
    v = version(exe) or "installed"
    return f"Tesseract: {v} — {exe}"


# ── how to install it ───────────────────────────────────────────────────
def _conda() -> Optional[str]:
    for name in ("conda", "mamba", "micromamba"):
        hit = shutil.which(name)
        if hit:
            return hit
    for base in ("miniconda3", "anaconda3", "miniforge3"):
        for sub in (("Scripts", "conda.exe"), ("condabin", "conda.bat"), ("bin", "conda")):
            cand = Path.home().joinpath(base, *sub)
            if cand.is_file():
                return str(cand)
    return None


def plan(platform: Optional[str] = None) -> Plan:
    """How Tesseract would be installed on this system."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        winget = shutil.which("winget")
        if winget:
            return Plan("winget", [winget, "install", "--id", WINGET_ID, "--exact",
                                   "--source", "winget", "--accept-source-agreements",
                                   "--accept-package-agreements"],
                        says=("Windows' own installer (winget) will download Tesseract OCR "
                              f"({WINGET_ID}) and install it. Windows may ask you to allow "
                              "it."))
    conda = _conda()
    if conda and not platform.startswith("darwin"):
        return Plan("conda", [conda, "create", "-y", "-n", "tesseract", "-c", "conda-forge",
                              "tesseract"],
                    says=("conda will download Tesseract OCR from conda-forge into its own "
                          "environment named 'tesseract' (no administrator rights needed)."))
    if platform.startswith("darwin"):
        brew = shutil.which("brew")
        if brew:
            return Plan("brew", [brew, "install", "tesseract"],
                        says="Homebrew will download Tesseract OCR and install it.")
        return Plan("manual", manual="Install Homebrew (https://brew.sh), then run:  "
                                     "brew install tesseract")
    if platform.startswith("win"):
        return Plan("manual", manual=(
            "winget is not on this PC. Install 'App Installer' from the Microsoft Store "
            f"(it brings winget), then:  winget install --id {WINGET_ID}  — or download the "
            "installer from https://github.com/UB-Mannheim/tesseract/wiki and use Locate… "
            "if it went somewhere else."))
    return Plan("manual", manual=("Install it with your package manager, e.g.:  sudo apt "
                                  "install tesseract-ocr   (Debian/Ubuntu)  or  sudo dnf "
                                  "install tesseract  (Fedora)"))


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def install(p: Optional[Plan] = None, *, on_line: Optional[Callable[[str], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None,
            timeout: float = 1800) -> Result:
    """Run the plan (blocking: the tab runs it on a worker), streaming its
    output to ``on_line``; then look for Tesseract again."""
    p = p or plan()
    log: List[str] = []
    if not p.runnable:
        return Result(False, find(), log, p.manual)
    import time
    try:
        proc = subprocess.Popen(p.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, errors="replace",
                                creationflags=_no_window())
    except Exception as exc:                              # noqa: BLE001
        return Result(False, find(), log, f"{p.kind} could not be started: {exc}")
    end = time.monotonic() + timeout
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.strip():
            log.append(line)
            if on_line is not None:
                on_line(line)
        if (should_stop is not None and should_stop()) or time.monotonic() > end:
            proc.kill()
            proc.wait(30)
            return Result(False, find(), log, "stopped before the install finished")
    code = proc.wait()
    found = find()
    if found is not None:
        v = version(found) or "Tesseract"
        return Result(True, found, log, f"{v} is installed: {found}. The Ledger and the "
                                        "Connections graph read pictures with it now.")
    why = (f"{p.kind} exited {code}" if code else
           f"{p.kind} finished, but Tesseract was not found where the Council looks")
    return Result(False, None, log, why + ". If it went somewhere else, use Locate….")
