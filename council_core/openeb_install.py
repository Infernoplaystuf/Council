"""
council_core.openeb_install — build and install OpenEB for an EVK4, in one go.

    install_openeb.bat                      (beside an exported Typhon)
    python -m council_core.openeb_install   (in the Council)
    python openeb_install.py [--dir C:\\openeb] [--driver wdi-simple.exe]
                             [--all-python-packages] [--dry-run]

Andy (16 November 2026): "for EVK support lets also include the install for
the OpenEB stuff in there to help users easily add the needed packages".
OpenEB is Prophesee's open-source Metavision SDK: the EVK4's software with
no account to request, but on Windows only as source to build. This runs
Prophesee's own steps for OpenEB 5.2.0 on Windows
(docs.prophesee.ai/stable/installation/windows_openeb.html), with the
Python it is started with, so the bindings it builds import in that Python
— Typhon's:

  1. Checks: Windows, Python 3.10-3.12 (the versions OpenEB builds for),
     Git, CMake (3.26 is the version Prophesee tested), Visual Studio 2022
     with its C++ tools, and long paths.
  2. OpenEB 5.2.0's source (git clone).
  3. vcpkg 2024.11.16 and OpenEB's dependencies through it (boost, opencv,
     hdf5, libusb ...: the longest step, an hour or more the first time).
  4. The Python packages the bindings need (numpy, pybind11 comes from
     vcpkg). OpenEB's whole requirements_openeb.txt pins old versions
     (opencv-python 4.5.5, numba < 0.60) that would replace newer ones in
     this Python, so it is installed only with --all-python-packages.
  5. CMake configure, build (Release) and install into <dir>\\install, the
     Python bindings into <dir>\\install\\site-packages.
  6. %LOCALAPPDATA%\\Council\\openeb.json says where, and Typhon finds the
     build by itself (council_core.openeb_env): no PATH editing.
  7. The EVK4's USB driver (WinUSB, wdi-simple.exe, as administrator) when
     --driver names wdi-simple.exe or it is in <dir>; else it says where to
     get it and the commands to run.

Each step is skipped when it is already done, so after a failure (a missing
tool, a dropped download) running it again carries on where it stopped.
Needs internet while it runs; it downloads only from github.com and, for
the packages, PyPI. Nothing is changed outside <dir> but the pointer file,
the Python packages and, with --driver, the USB driver.

Standard library only, and runnable as a file (an exported app copies it
into _council\\council_core).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence

OPENEB_TAG = "5.2.0"
OPENEB_GIT = "https://github.com/prophesee-ai/openeb.git"
VCPKG_TAG = "2024.11.16"
VCPKG_ZIP = (f"https://github.com/microsoft/vcpkg/archive/refs/tags/"
             f"{VCPKG_TAG}.zip")
PYTHONS = ((3, 10), (3, 11), (3, 12))
CMAKE_TESTED = (3, 26)
GUIDE = "https://docs.prophesee.ai/stable/installation/windows_openeb.html"
CMAKE_URL = "https://cmake.org/files/v3.26/cmake-3.26.6-windows-x86_64.msi"
VS_URL = "https://visualstudio.microsoft.com/downloads/"
GIT_URL = "https://git-scm.com/download/win"
WDI_URL = ("https://kdrive.infomaniak.com/app/share/975517/"
           "cb164518-e68f-49fd-a6a1-eea693783bd2")
#: Binding the EVK4 (and the EVK3, and the bootloader mode a camera drops
#: into mid-update) to WinUSB, as Prophesee's page gives them.
WDI_ARGS = tuple(("-n", "EVK", "-m", "Prophesee", "-v", "0x04b4", "-p", pid)
                 for pid in ("0x00f4", "0x00f5", "0x00f3"))
POINTER_NAME = "openeb.json"
DONE = ".council_done"


def is_windows() -> bool:
    return os.name == "nt"


class Stop(Exception):
    """A step that cannot go on: what is missing and what to do."""


def say(line: str = "") -> None:
    print(line, flush=True)


def heading(n: int, text: str) -> None:
    say()
    say(f"== {n}. {text}")


# ----------------------------------------------------------------------
# Where
# ----------------------------------------------------------------------
def default_root() -> Path:
    drive = os.environ.get("SystemDrive", "C:")
    return Path(drive + "\\") / "openeb" if os.name == "nt" \
        else Path.home() / "openeb"


def pointer_file() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Council" / POINTER_NAME
    return Path.home() / ".council" / POINTER_NAME


class Layout:
    def __init__(self, root: Path):
        self.root = root
        self.src = root / "src"
        self.vcpkg = root / "vcpkg"
        self.build = root / "build"
        self.install = root / "install"
        self.site = self.install / "site-packages"

    def marker(self, step: str) -> Path:
        return self.root / f"{DONE}.{step}"

    def done(self, step: str) -> bool:
        return self.marker(step).is_file()

    def mark(self, step: str) -> None:
        self.marker(step).write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"),
                                     encoding="utf-8")


# ----------------------------------------------------------------------
# Running things
# ----------------------------------------------------------------------
def run(argv: Sequence[str], *, cwd: Optional[Path] = None,
        dry: bool = False) -> None:
    shown = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in argv)
    say(f"   > {shown}")
    if dry:
        return
    code = subprocess.call([str(a) for a in argv],
                           cwd=str(cwd) if cwd else None)
    if code != 0:
        raise Stop(f"that command stopped with code {code}. Read the lines "
                   f"above for why, fix it, and run this again: it carries "
                   f"on from this step.")


def download(url: str, target: Path, dry: bool = False) -> None:
    say(f"   downloading {url}")
    if dry:
        return
    part = target.with_name(target.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as response, \
            open(part, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    os.replace(part, target)


# ----------------------------------------------------------------------
# 1. Checks
# ----------------------------------------------------------------------
def version_of(text: str) -> Optional[tuple]:
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in found.groups() if g is not None) \
        if found else None


def find_visual_studio() -> Optional[str]:
    """Visual Studio 2022 (17.x) with the C++ x64 tools, by vswhere."""
    base = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    vswhere = Path(base) / "Microsoft Visual Studio" / "Installer" / \
        "vswhere.exe"
    if not vswhere.is_file():
        return None
    try:
        out = subprocess.run(
            [str(vswhere), "-products", "*", "-version", "[17.0,18.0)",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[0] if lines else None


def long_paths_enabled() -> Optional[bool]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") \
                as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except Exception:                                     # noqa: BLE001
        return None


def checks(layout: Layout, *, force_os: bool = False) -> None:
    heading(1, "What this PC has")
    problems: List[str] = []
    if not is_windows() and not force_os:
        raise Stop("this installer is for Windows. On Linux, Prophesee's "
                   "own packages and build steps are at " + GUIDE.replace(
                       "windows_openeb", "linux_openeb"))
    if platform.machine().lower() not in ("amd64", "x86_64", ""):
        problems.append(f"OpenEB builds for 64-bit x86 Windows; this is "
                        f"{platform.machine()}.")
    py = sys.version_info[:2]
    say(f"   Python {py[0]}.{py[1]} at {sys.executable}")
    if py not in PYTHONS:
        problems.append(
            f"OpenEB's Python bindings build for Python "
            f"{', '.join('%d.%d' % v for v in PYTHONS)}; this is "
            f"{py[0]}.{py[1]}. Run this with the Python Typhon uses, and "
            f"give Typhon one of those (python.org, 64-bit).")
    git = shutil.which("git")
    say(f"   Git: {git or 'not found'}")
    if not git:
        problems.append(f"Git is needed to fetch OpenEB: {GIT_URL}")
    cmake = shutil.which("cmake")
    if not cmake:
        problems.append(f"CMake is needed (3.26 is the version Prophesee "
                        f"tested): {CMAKE_URL} - tick 'Add CMake to the "
                        f"PATH' in its installer.")
        say("   CMake: not found")
    else:
        try:
            text = subprocess.run([cmake, "--version"], capture_output=True,
                                  text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        found = version_of(text)
        say(f"   CMake: {'.'.join(map(str, found)) if found else '?'} "
            f"at {cmake}")
        if found and tuple(found[:2]) != CMAKE_TESTED:
            say(f"   note: Prophesee tested CMake 3.26 and says newer ones "
                f"may not work. If the configure step fails, install "
                f"{CMAKE_URL} and run this again.")
    vs = find_visual_studio()
    say(f"   Visual Studio 2022 with C++: {vs or 'not found'}")
    if not vs:
        problems.append(
            f"Visual Studio 2022 (Community is free) with the 'Desktop "
            f"development with C++' workload is needed: {VS_URL} (Build "
            f"Tools alone also work with the Windows 11 SDK and the English "
            f"language pack).")
    longs = long_paths_enabled()
    if longs is False:
        say("   note: Windows long paths are off. The build uses a short "
            "folder, which is usually enough; if a step fails with a path "
            "too long, turn them on (gpedit.msc > Computer Configuration > "
            "Administrative Templates > System > Filesystem > Enable Win32 "
            "long paths) and run this again.")
    if len(str(layout.root)) > 20:
        say(f"   note: {layout.root} is a long folder; vcpkg nests deep, so "
            f"a short one such as C:\\openeb is safer.")
    if problems and force_os:
        say("   (dry run) would stop here for:\n   - "
            + "\n   - ".join(problems))
        return
    if problems:
        raise Stop("install these first, then run this again:\n  - "
                   + "\n  - ".join(problems))
    say("   Everything needed is here.")


# ----------------------------------------------------------------------
# 2-5. The build
# ----------------------------------------------------------------------
def fetch_source(layout: Layout, dry: bool) -> None:
    heading(2, f"OpenEB {OPENEB_TAG} source")
    if (layout.src / "CMakeLists.txt").is_file():
        say(f"   already here: {layout.src}")
        return
    run(["git", "clone", "--branch", OPENEB_TAG, "--depth", "1", OPENEB_GIT,
         layout.src], dry=dry)


def vcpkg_manifest(layout: Layout) -> Optional[Path]:
    for found in sorted((layout.src / "utils" / "windows").rglob(
            "vcpkg-openeb.json")):
        return found
    return None


def fetch_dependencies(layout: Layout, dry: bool) -> None:
    heading(3, f"vcpkg {VCPKG_TAG} and OpenEB's dependencies "
               f"(the long one: an hour or more the first time)")
    if layout.done("vcpkg"):
        say("   already done")
        return
    exe = layout.vcpkg / "vcpkg.exe"
    if not (layout.vcpkg / "bootstrap-vcpkg.bat").is_file():
        archive = layout.root / f"vcpkg-{VCPKG_TAG}.zip"
        if not archive.is_file():
            download(VCPKG_ZIP, archive, dry)
        if not dry:
            say(f"   unpacking into {layout.vcpkg}")
            unpack = layout.root / "vcpkg.unpack"
            shutil.rmtree(unpack, ignore_errors=True)
            with zipfile.ZipFile(archive) as z:
                z.extractall(unpack)
            inner = next(p for p in unpack.iterdir() if p.is_dir())
            os.replace(inner, layout.vcpkg)
            shutil.rmtree(unpack, ignore_errors=True)
    if not exe.is_file():
        run(["cmd", "/c", layout.vcpkg / "bootstrap-vcpkg.bat"],
            cwd=layout.vcpkg, dry=dry)
    manifest = vcpkg_manifest(layout)
    if manifest is None and not dry:
        raise Stop(f"OpenEB's vcpkg-openeb.json was not found under "
                   f"{layout.src}\\utils\\windows; remove {layout.src} and "
                   f"run this again to fetch the source afresh.")
    if not dry:
        shutil.copyfile(manifest, layout.vcpkg / "vcpkg.json")
    run([exe, "install", "--triplet", "x64-windows",
         "--x-install-root", "installed"], cwd=layout.vcpkg, dry=dry)
    if not dry:
        layout.mark("vcpkg")


def python_packages(layout: Layout, everything: bool, dry: bool) -> None:
    heading(4, f"Python packages, into {sys.executable}")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
        dry=dry)
    if everything:
        reqs = layout.src / "utils" / "python" / "requirements_openeb.txt"
        run([sys.executable, "-m", "pip", "install", "-r", reqs], dry=dry)
    else:
        run([sys.executable, "-m", "pip", "install", "numpy"], dry=dry)
        say("   (OpenEB's whole requirements_openeb.txt pins old versions "
            "such as opencv-python 4.5.5; add --all-python-packages to "
            "install it anyway. The camera itself needs only numpy.)")


def build(layout: Layout, dry: bool) -> None:
    heading(5, "Configure, build and install OpenEB (Release)")
    if layout.done("install"):
        say(f"   already installed in {layout.install}")
        return
    toolchain = layout.src / "cmake" / "toolchains" / "vcpkg.cmake"
    if not layout.done("configure"):
        if not dry:
            layout.build.mkdir(parents=True, exist_ok=True)
        run(["cmake", layout.src, "-A", "x64",
             "-DCMAKE_BUILD_TYPE=Release",
             f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
             f"-DVCPKG_DIRECTORY={layout.vcpkg}",
             "-DBUILD_TESTING=OFF",
             f"-DCMAKE_INSTALL_PREFIX={layout.install}",
             f"-DPYTHON3_SITE_PACKAGES={layout.site}",
             f"-DPython3_EXECUTABLE={sys.executable}"],
            cwd=layout.build, dry=dry)
        if not dry:
            layout.mark("configure")
    jobs = str(max(1, min(8, (os.cpu_count() or 4) // 2)))
    run(["cmake", "--build", ".", "--config", "Release", "--parallel", jobs],
        cwd=layout.build, dry=dry)
    run(["cmake", "--build", ".", "--config", "Release", "--target",
         "install"], cwd=layout.build, dry=dry)
    if not dry:
        layout.mark("install")


# ----------------------------------------------------------------------
# 6. Where Typhon finds it
# ----------------------------------------------------------------------
def write_pointer(layout: Layout, dry: bool) -> Path:
    heading(6, "Telling Typhon where it is")
    target = pointer_file()
    doc = {"root": str(layout.root), "openeb": OPENEB_TAG,
           "python": sys.executable,
           "installed": time.strftime("%Y-%m-%d %H:%M:%S")}
    say(f"   {target}: {json.dumps(doc)}")
    if not dry:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".tmp")
        part.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(part, target)
    return target


def check_import(layout: Layout, dry: bool) -> bool:
    """Import metavision_hal in a fresh Python the way Typhon will."""
    if dry:
        return True
    here = Path(__file__).resolve().parent
    code = ("import sys; sys.path.insert(0, %r); import openeb_env; "
            "openeb_env.prepare(); import metavision_hal; "
            "print('   metavision_hal imports from', metavision_hal.__file__)"
            % str(here))
    ok = subprocess.call([sys.executable, "-c", code]) == 0
    if not ok:
        say("   metavision_hal did not import. Read the error above; "
            "running this again rebuilds nothing that is done.")
    return ok


# ----------------------------------------------------------------------
# 7. The USB driver
# ----------------------------------------------------------------------
def find_wdi(layout: Layout, given: Optional[str]) -> Optional[Path]:
    if given:
        return Path(given)
    for folder in (layout.root, Path.home() / "Downloads"):
        found = folder / "wdi-simple.exe"
        if found.is_file():
            return found
    return None


def driver(layout: Layout, given: Optional[str], dry: bool) -> None:
    heading(7, "The EVK4's USB driver (once per PC)")
    commands = [[str(a) for a in args] for args in WDI_ARGS]
    wdi = find_wdi(layout, given)
    if wdi is None or not (dry or wdi.is_file()):
        say("   Windows has no driver for the EVK4: without one it is not "
            "found, as if unplugged.")
        say(f"   Download wdi-simple.exe ('Camera Plugins' section) from:")
        say(f"     {WDI_URL}")
        say(f"   save it in {layout.root} or Downloads, and run this again; "
            f"or, in a Command Prompt opened as administrator:")
        for args in commands:
            say("     wdi-simple.exe " + " ".join(
                f'"{a}"' if a in ("EVK", "Prophesee") else a for a in args))
        say("   Not needed if Prophesee's own SDK installer was run on this "
            "PC (it installs the driver).")
        return
    lines = " & ".join(
        '"%s" %s' % (wdi, " ".join(f'"{a}"' if a in ("EVK", "Prophesee")
                                    else a for a in args))
        for args in commands)
    say(f"   {wdi}: asking Windows to run it as administrator (a prompt "
        f"appears)")
    ps = ("Start-Process -FilePath cmd.exe -Verb RunAs -Wait "
          "-ArgumentList '/c', '%s'" % lines.replace("'", "''"))
    run(["powershell", "-NoProfile", "-Command", ps], dry=dry)


# ----------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_openeb",
        description="Build and install OpenEB (the EVK4's open-source SDK) "
                    "for the Python this runs in.")
    parser.add_argument("--dir", default=None,
                        help="where to build and install (default "
                             f"{default_root()}); keep it short")
    parser.add_argument("--driver", default=None,
                        help="path to wdi-simple.exe, to install the EVK4's "
                             "USB driver")
    parser.add_argument("--all-python-packages", action="store_true",
                        help="also install OpenEB's whole "
                             "requirements_openeb.txt (old pinned versions)")
    parser.add_argument("--skip-driver", action="store_true",
                        help="do not install or explain the USB driver")
    parser.add_argument("--dry-run", action="store_true",
                        help="say every step and command, run none")
    args = parser.parse_args(argv)
    root = Path(args.dir) if args.dir else default_root()
    layout = Layout(root)
    dry = args.dry_run
    say(f"OpenEB {OPENEB_TAG} for the EVK4, into {root}")
    say(f"Prophesee's guide for these steps: {GUIDE}")
    try:
        checks(layout, force_os=dry)
        if not dry:
            root.mkdir(parents=True, exist_ok=True)
        fetch_source(layout, dry)
        fetch_dependencies(layout, dry)
        python_packages(layout, args.all_python_packages, dry)
        build(layout, dry)
        write_pointer(layout, dry)
        ok = check_import(layout, dry)
        if not args.skip_driver:
            driver(layout, args.driver, dry)
    except Stop as why:
        say()
        say(f"STOPPED: {why}")
        return 1
    except KeyboardInterrupt:
        say("\nStopped. Run this again to carry on.")
        return 1
    except Exception as exc:                              # noqa: BLE001
        say()
        say(f"STOPPED: {type(exc).__name__}: {exc}. Run this again to carry "
            f"on; a step that finished is not repeated.")
        return 1
    say()
    if ok:
        say("Done. Start Typhon, click Camera setup, tick Prophesee EVK4 and "
            "press Check: it finds this build by itself.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
