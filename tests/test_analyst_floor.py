"""
The analyst child's file fence (council_core.analyst_floor) — the FLOOR under
the sandbox's Python-level checks.

The sandbox (vault_analyst.execute_pandas_code) keeps model code inside the
data folders with an AST validator, a guarded import/getattr, module proxies
and contained helpers. Each of those guards one ROUTE to a file, and every
round of fixes found another route a guard missed (a helper that returned a
real pathlib object, a dunder the validator did not know). The floor does not
depend on any of them: a PEP 578 audit hook, installed in the child BEFORE any
model code runs and impossible to remove, checks the real path of every file
open, directory listing and file-changing call against a fixed allow-list —
read-only (the data folders, Python's own libraries, the app's code),
read-write (data_out, the stats cache, the run's temp folders), nothing else.

These tests exercise the floor three ways: the Floor object in-process; the
hook installed in a CHILD running raw file operations (tests/_floor_driver.py);
and the REAL analyst child with the Python containment neutralised, where the
floor is the only thing left (tests/_floor_shim.py). Every refusal is checked
against a temp canary the test creates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import analyst_floor  # noqa: E402

SECRET = "TOP-SECRET-FLOOR"


@pytest.fixture
def box(tmp_path):
    """A vault with data_in/data_out/.stats_cache, Python's lib dir, and an
    OUTSIDE folder holding a canary and a keeper."""
    vault = tmp_path / "vault"
    data = vault / "data_in"
    out = vault / "data_out"
    sc = vault / ".stats_cache"
    for d in (data, out, sc):
        d.mkdir(parents=True)
    (data / "runs.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (out / "prev.txt").write_text("made earlier\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text(SECRET + "\n", encoding="utf-8")
    keep = outside / "keep.txt"
    keep.write_text("keep me\n", encoding="utf-8")
    pylib = Path(os.path.dirname(os.__file__))
    return dict(vault=vault, data=data, out=out, sc=sc, outside=outside,
                canary=canary, keep=keep, pylib=pylib)


# ============================================================
# The Floor object, in-process
# ============================================================

def _floor(box):
    return analyst_floor.Floor(
        read_only=[str(box["data"]), str(box["pylib"])],
        read_write=[str(box["out"]), str(box["sc"])],
        never=[str(box["vault"])])


def test_reads_inside_the_allow_list_are_allowed(box):
    fl = _floor(box)
    assert fl.why_not(str(box["data"] / "runs.csv"), False, "reading") == ""
    assert fl.why_not(str(box["out"] / "prev.txt"), False, "reading") == ""
    assert fl.why_not(str(box["pylib"] / "os.py"), False, "reading") == ""


def test_writes_into_data_out_and_stats_cache_are_allowed(box):
    fl = _floor(box)
    assert fl.why_not(str(box["out"] / "new.txt"), True, "writing") == ""
    assert fl.why_not(str(box["sc"] / "c.csv"), True, "writing") == ""


def test_pythons_libraries_are_read_only_not_write(box):
    fl = _floor(box)
    why = fl.why_not(str(box["pylib"] / "evil.py"), True, "writing")
    assert why and "read-only" in why


def test_outside_the_allow_list_is_refused_with_a_plain_reason(box):
    fl = _floor(box)
    for write in (False, True):
        why = fl.why_not(str(box["canary"]), write, "reading")
        assert why
        assert "not allowed" in why
        # a plain reason, no jargon, no object repr
        assert "<" not in why and "0x" not in why


def test_the_vault_itself_is_not_readable_even_though_data_out_inside_is(box):
    """data_out and .stats_cache are read-write; the vault AROUND them (where
    node_routing.json and the conversation log live) is off-limits — the
    DEEPEST matching root decides."""
    fl = _floor(box)
    assert fl.why_not(str(box["vault"] / "node_routing.json"), False,
                      "reading")
    assert fl.why_not(str(box["vault"] / "manual.md"), True, "writing")
    # while the data_out file inside it is writable
    assert fl.why_not(str(box["out"] / "ok.csv"), True, "writing") == ""


def test_a_network_share_or_device_is_refused_from_the_spelling(box):
    fl = _floor(box)
    if os.name == "nt":
        assert fl.why_not(r"\\server\share\x.csv", False, "reading")
        assert fl.why_not(r"\\.\PhysicalDrive0", True, "writing")
        assert fl.why_not(str(box["data"] / "NUL"), True, "writing")


def test_check_raises_refused_and_records_it(box):
    fl = _floor(box)
    with pytest.raises(analyst_floor.Refused):
        fl.check(str(box["canary"]), False, "reading")
    assert fl.refusals and "file fence refused" in fl.first_refusal


def test_canonical_follows_a_symlink_or_junction_out(box, tmp_path):
    """A link inside the data folder that points OUT resolves to its target,
    so the floor judges it by where it really leads."""
    link = box["data"] / "link"
    try:
        os.symlink(str(box["outside"]), str(link),
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    fl = _floor(box)
    # a file reached THROUGH the link resolves to the outside canary → refused
    assert fl.why_not(str(link / "canary.txt"), False, "reading")


# ============================================================
# The hook installed in a child, on raw file operations
# ============================================================

def _run_driver(tmp_path, box, ops, probe_read=None):
    job = {
        "read_only": [str(box["data"]), str(box["pylib"])],
        "read_write": [str(box["out"]), str(box["sc"])],
        "never": [str(box["vault"])],
        "ops": ops,
    }
    if probe_read:
        job["probe_read"] = str(probe_read)
    jf = tmp_path / "job.json"
    jf.write_text(json.dumps(job), encoding="utf-8")
    driver = ROOT / "tests" / "_floor_driver.py"
    proc = subprocess.run([sys.executable, str(driver), str(ROOT), str(jf)],
                          check=True, capture_output=True, text=True)
    line = next(ln for ln in proc.stdout.splitlines()
                if ln.startswith("FLOOR_RESULT:"))
    return json.loads(line[len("FLOOR_RESULT:"):])


def test_in_the_child_outside_ops_are_refused_inside_ops_run(tmp_path, box):
    ops = [
        ["read", str(box["data"] / "runs.csv")],       # 0 inside  -> OK
        ["read", str(box["out"] / "prev.txt")],        # 1 data_out-> OK
        ["read", str(box["pylib"] / "os.py")],         # 2 pylib   -> OK
        ["write", str(box["out"] / "made.txt")],       # 3 data_out-> OK
        ["write", str(box["sc"] / "c.csv")],           # 4 cache   -> OK
        ["read", str(box["canary"])],                  # 5 outside -> REFUSED
        ["write", str(box["outside"] / "n.txt")],      # 6 outside -> REFUSED
        ["listdir", str(box["outside"])],              # 7 outside -> REFUSED
        ["scandir", str(box["outside"])],              # 8 outside -> REFUSED
        ["remove", str(box["keep"])],                  # 9 outside -> REFUSED
        ["rename", str(box["keep"]),
         str(box["keep"]) + "2"],                      # 10 outside-> REFUSED
        ["mkdir", str(box["outside"] / "sub")],        # 11 outside-> REFUSED
        ["listdir", str(box["data"])],                 # 12 inside -> OK
    ]
    res = _run_driver(tmp_path, box, ops, probe_read=box["data"] / "runs.csv")
    for i in (0, 1, 2, 3, 4, 12):
        assert res[str(i)] == "OK", (i, res[str(i)])
    for i in (5, 6, 7, 8, 9, 10, 11):
        assert res[str(i)].startswith("REFUSED"), (i, res[str(i)])
        assert "file fence refused" in res[str(i)]
        assert SECRET not in res[str(i)]
    # the refused delete/rename did not touch the canary files
    assert box["keep"].exists()
    assert not (box["outside"] / "n.txt").exists()
    assert not (box["outside"] / "sub").exists()
    assert not (box["canary"].with_name("canary.txt2")).exists()
    # the allowed writes landed
    assert (box["out"] / "made.txt").exists()
    assert (box["sc"] / "c.csv").exists()


def test_the_per_open_cost_and_start_up_stay_small(tmp_path, box):
    """The hook resolves and compares with os.path (realpath dominates), and
    memoises per path, so a repeated open is cheap and the hook's install is a
    few milliseconds. Loose bounds — this only guards against a gross
    regression (e.g. resolving uncached on every open)."""
    res = _run_driver(tmp_path, box, [["read", str(box["data"] / "runs.csv")]],
                      probe_read=box["data"] / "runs.csv")
    assert res["per_open_cached_us"] < 400.0, res["per_open_cached_us"]
    assert res["build_install_ms"] < 2000.0, res["build_install_ms"]


# ============================================================
# The real analyst child, Python containment neutralised
# ============================================================

def _run_shim(tmp_path, box, code):
    # The code and the child's report go in a dir of their OWN, DISJOINT from
    # the vault and the canary — the real child writes its report into a
    # private temp folder, and that folder (not the vault's parent) is what it
    # makes writable. A shared parent would put the canary inside the
    # allow-list and defeat the test.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    code_file = run_dir / "code.py"
    code_file.write_text(code, encoding="utf-8")
    out_json = run_dir / "shim_out.json"
    shim = ROOT / "tests" / "_floor_shim.py"
    env = dict(os.environ, COUNCIL_VAULT_ROOT=str(box["vault"]),
               QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1")
    subprocess.run([sys.executable, str(shim), str(ROOT), str(code_file),
                    str(box["data"]), str(out_json)],
                   check=True, capture_output=True, text=True, env=env)
    return json.loads(out_json.read_text(encoding="utf-8"))


@pytest.mark.parametrize("code_t", [
    "print(read_text(r'{canary}'))",
    "print(open(r'{canary}').read())",
    "result_df = pd.read_csv(r'{canary}')",
])
def test_floor_holds_in_the_real_child_with_containment_off(tmp_path, box,
                                                            code_t):
    """With the AST validator, the path resolver and the pandas containment
    all neutralised, the fence still refuses an outside read, ends the run,
    leaks no secret, and the caller sees a plain error."""
    code = code_t.format(canary=box["canary"])
    out = _run_shim(tmp_path, box, code)
    assert out["ran"] is False and out["ok"] is False
    blob = json.dumps(out)
    assert SECRET not in blob
    assert "file fence refused" in (out.get("error") or "")
    # the refusal is plain, not an object repr or traceback dump
    assert "Traceback" not in (out.get("error") or "")


def test_inside_reads_still_work_in_the_real_child(tmp_path, box):
    out = _run_shim(tmp_path, box,
                    "print(read_text('runs.csv'))\nresult_df = pd.read_csv('runs.csv')")
    assert out["ran"] is True, out
    assert "1,2" in out.get("stdout", "") or out["ok"]


def test_a_refusal_ends_the_run_even_if_the_code_catches_it(tmp_path, box):
    """Model code that swallows the PermissionError must not turn a refusal
    into a successful run: the fence records it and the child ends the run."""
    code = ("ok = True\n"
            "try:\n"
            f"    read_text(r'{box['canary']}')\n"
            "except Exception:\n"
            "    pass\n"
            "print('kept going')\n"
            "result_df = pd.read_csv('runs.csv')\n")
    out = _run_shim(tmp_path, box, code)
    assert out["ran"] is False, out
    assert "file fence refused" in (out.get("error") or "")
    assert SECRET not in json.dumps(out)


def test_df_plot_ends_the_run_with_a_plain_chart_message(tmp_path, box):
    """pandas' df.plot() imports matplotlib, which makes/reads its config
    folder in the user's home — outside the fence. The fence stops the import
    and the run ends with a plain, model-readable reason (no chart path)."""
    out = _run_shim(tmp_path, box,
                    "ax = pd.read_csv('runs.csv').plot()\nprint('plotted')")
    assert out["ran"] is False, out
    assert "charts are not available" in (out.get("error") or "")
