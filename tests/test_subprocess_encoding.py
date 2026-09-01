"""
Every captured subprocess must name its encoding.

FOUND BY RUNNING THE APP, not by the suite. The Changelog tab shells out to
`git log --pretty=format:...%s` with text=True and no encoding. text=True
decodes with the LOCALE encoding — cp1252 on Windows — while git speaks UTF-8,
so the first commit subject containing an em-dash or a curly quote raised
UnicodeDecodeError *inside subprocess's reader thread*.

That is the nasty part. The exception does not reach the caller. The thread
dies, communicate() returns stdout=None, and the caller gets None where it
expected a string. So the tab silently showed nothing, and any caller that then
does .strip() raises AttributeError somewhere unrelated to the real cause.

Reproduced on this machine:
    subprocess.run(["git","log","--oneline","-3"], capture_output=True,
                   text=True)
    -> UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d
       (the tail of a UTF-8 right curly quote), stdout=None

Run:  python -m pytest tests/test_subprocess_encoding.py -q
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Modules whose captured output is TEXT WE DO NOT CONTROL: git speaks UTF-8,
# generated code can print anything, a worker's traceback carries any byte.
GUARDED = [
    "council_engine.py",
    "council_gui_engine.py",
    "gui_runner.py",
    "nx_bridge.py",
    "workflow_runner.py",
]


def _captures_text(call: ast.Call) -> bool:
    """A subprocess call that DECODES its output — the ones at risk."""
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    decodes = any(
        isinstance(kw.get(name), ast.Constant) and kw[name].value is True
        for name in ("text", "universal_newlines"))
    if not decodes:
        return False
    captures = (
        (isinstance(kw.get("capture_output"), ast.Constant)
         and kw["capture_output"].value is True)
        or "stdout" in kw or "stderr" in kw)
    return bool(captures)


def _is_subprocess_call(call: ast.Call) -> bool:
    f = call.func
    if not isinstance(f, ast.Attribute) or f.attr not in ("run", "Popen",
                                                          "check_output"):
        return False
    base = f.value
    return isinstance(base, ast.Name) and "subprocess" in base.id or (
        isinstance(base, ast.Name) and base.id in ("_sp", "sp"))


@pytest.mark.parametrize("name", GUARDED)
def test_captured_subprocess_output_names_its_encoding(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
            continue
        if not _captures_text(node):
            continue
        kw = {k.arg for k in node.keywords if k.arg}
        if "encoding" not in kw:
            offenders.append(node.lineno)

    assert not offenders, (
        f"{name} decodes captured output with the LOCALE encoding at line(s) "
        f"{offenders}. On Windows that is cp1252, and one UTF-8 byte kills "
        f"subprocess's reader thread — stdout becomes None instead of raising. "
        f"Pass encoding=\"utf-8\", errors=\"replace\".")


def test_git_log_is_actually_readable_now():
    """The end-to-end version: this repo's own commit subjects contain the
    characters that broke it, so the repo IS the fixture."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run(
        ["git", "log", "--pretty=format:%h|%s", "-40"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace")
    assert r.stdout is not None, "the reader thread died again"
    assert r.stdout.strip(), "expected some history"


def test_the_locale_default_still_breaks_on_this_repo():
    """Guards the guard. If this ever stops failing, the repo no longer
    contains a subject that reproduces the bug, and the test above has quietly
    stopped proving anything — so it should be re-pointed at a crafted input
    rather than deleted."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    import locale
    if (locale.getpreferredencoding() or "").lower().replace("-", "") in (
            "utf8", "cp65001"):
        pytest.skip("locale is already UTF-8; nothing to break")
    r = subprocess.run(
        ["git", "log", "--pretty=format:%h|%s", "-40"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30)
    assert r.stdout is None, (
        "expected the locale decode to kill the reader thread on this repo's "
        "own history; if the history changed, re-point this at a crafted input")
