"""
The launchers pass the app's arguments on.

THE GAP
run-windows.bat, run-linux.sh and run-wsl.sh each read their FIRST argument
for their own flags (--check, --tk) and then started the app with no arguments
at all — `"!PYEXE!" !COUNCIL_ENTRY!`, `python "$COUNCIL_ENTRY"`. So
`run-windows.bat --advanced` opened the default build: the flag the README
gives for the advanced tabs (Librarian, Nodes, Agents, Vault Health,
Apothecary, IDE) was silently dropped, in both shells, and launch_council.bat
— which forwards %* to run-windows.bat — inherited the loss. --tk was also
recognised only in first position.

The real launcher text runs here, through the harness in
tests/test_saved_model_path.py: the "app" is a stub that writes down the
arguments it was started with.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_saved_model_path import (ROOT, _NO_WINDOW, _prepare_bat,
                                         _run_bat, _run_sh, win_only)

#: Records (entry script, arguments). Under the .bat the stub IS the entry
#: (argv[0]); under the .sh harness `python` is a function that runs the stub
#: with the entry as its first argument.
_ARGV_ENTRY = """\
import json, os, sys
argv = list(sys.argv)
names = ("council_qt.py", "council_gui_engine.py")
if os.path.basename(argv[0]) in names:
    entry, args = os.path.basename(argv[0]), argv[1:]
else:
    entry, args = os.path.basename(argv[1]), argv[2:]
with open(os.environ["STUB_OUT"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"entry": entry, "args": args}) + "\\n")
"""


def _started(out: Path) -> list:
    assert out.is_file(), "the launcher never started the app"
    runs = [json.loads(line) for line in
            out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(runs) == 1, f"the app started {len(runs)} times"
    return runs[0]


# ============================================================
# run-windows.bat
# ============================================================

@win_only
def test_bat_passes_advanced_to_the_app(tmp_path):
    proc, out = _run_bat(tmp_path, "--advanced", entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _started(out)
    assert got == {"entry": "council_qt.py", "args": ["--advanced"]}


@win_only
def test_bat_keeps_its_own_flags_and_passes_the_rest(tmp_path):
    """--tk is the launcher's, wherever it is on the line; it picks the entry
    and is not handed on. Everything else is, in order."""
    proc, out = _run_bat(tmp_path, "--advanced", "--tk", "--verbose",
                         entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _started(out)
    assert got == {"entry": "council_gui_engine.py",
                   "args": ["--advanced", "--verbose"]}


@win_only
def test_bat_passes_an_argument_with_spaces_whole(tmp_path):
    proc, out = _run_bat(tmp_path, "--note=two words", "--advanced",
                         entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out)["args"] == ["--note=two words", "--advanced"]


@win_only
def test_bat_passes_cmds_special_characters_as_typed(tmp_path):
    """The arguments are scanned with delayed expansion OFF and handed on
    through a delayed expansion, which cmd does not re-parse — so a ! ^ or &
    inside a quoted argument reaches the app as typed. (Each has a space, so
    subprocess quotes it the way a user would. Not %VAR%: the CALLING cmd
    expands that before the launcher ever sees the line.)"""
    typed = ["wow!what a name", "a^b c", "R&D folder"]
    proc, out = _run_bat(tmp_path, *typed, entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out)["args"] == typed


@win_only
def test_bat_with_no_arguments_passes_none(tmp_path):
    proc, out = _run_bat(tmp_path, entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out) == {"entry": "council_qt.py", "args": []}


@win_only
def test_bat_check_still_launches_nothing_wherever_it_is(tmp_path):
    proc, out = _run_bat(tmp_path, "--advanced", "--check",
                         entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not out.exists(), "--check started the app"


@win_only
def test_the_legacy_launcher_passes_them_through_too(tmp_path):
    """launch_council.bat forwards %* to run-windows.bat; the arguments were
    lost one step later, in run-windows.bat."""
    argv, kwargs, out = _prepare_bat(tmp_path, entry_src=_ARGV_ENTRY)
    app = Path(kwargs["cwd"])
    shutil.copyfile(ROOT / "launch_council.bat", app / "launch_council.bat")
    proc = subprocess.run(
        ["cmd.exe", "/d", "/c", str(app / "launch_council.bat"),
         "--advanced"], capture_output=True, timeout=180, **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out) == {"entry": "council_qt.py", "args": ["--advanced"]}


# ============================================================
# run-linux.sh / run-wsl.sh
# ============================================================

@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_sh_passes_advanced_to_the_app(tmp_path, name):
    proc, out = _run_sh(tmp_path, name, entry_src=_ARGV_ENTRY,
                        args=("--advanced",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out) == {"entry": "council_qt.py", "args": ["--advanced"]}


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_sh_keeps_tk_and_passes_the_rest(tmp_path, name):
    proc, out = _run_sh(tmp_path, name, entry_src=_ARGV_ENTRY,
                        args=("--advanced", "--tk", "two words"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out) == {"entry": "council_gui_engine.py",
                             "args": ["--advanced", "two words"]}


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_sh_with_no_arguments_passes_none(tmp_path, name):
    """`set -u` is on in both scripts: an empty array expanded carelessly is
    an "unbound variable" error on older bash, which would stop every
    argument-less launch."""
    proc, out = _run_sh(tmp_path, name, entry_src=_ARGV_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _started(out) == {"entry": "council_qt.py", "args": []}


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_the_sh_launchers_keep_lf(name):
    assert b"\r" not in (ROOT / name).read_bytes()


def test_the_bat_launchers_keep_crlf():
    """cmd finds `goto` labels by seeking; an LF-only .bat mis-parses."""
    for name in ("run-windows.bat", "launch_council.bat"):
        raw = (ROOT / name).read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} lost CRLF"


def test_the_retry_relaunch_passes_them_too():
    """The CPU retry starts the app a second time; it must be the same app
    with the same arguments, not the default build."""
    code = [ln for ln in (ROOT / "run-windows.bat").read_text(
        encoding="utf-8").splitlines()
        if not ln.lstrip().upper().startswith("REM")]
    launches = [ln.strip() for ln in code
                if ln.strip().startswith('"!PYEXE!" !COUNCIL_ENTRY!')]
    assert len(launches) == 2, launches
    assert all(ln.endswith("!APP_ARGS!") for ln in launches), launches
    for name in ("run-linux.sh", "run-wsl.sh"):
        sh = [ln.strip() for ln in (ROOT / name).read_text(
            encoding="utf-8").splitlines()
            if ln.strip().startswith('python "$COUNCIL_ENTRY"')]
        assert len(sh) == 2, (name, sh)
        assert all("APP_ARGS" in ln for ln in sh), (name, sh)
