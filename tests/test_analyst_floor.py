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

def _run_shim(tmp_path, box, code, data=None, **env_extra):
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
    env.update(env_extra)
    subprocess.run([sys.executable, str(shim), str(ROOT), str(code_file),
                    str(data or box["data"]), str(out_json)],
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


def test_df_plot_fails_as_a_missing_matplotlib_not_a_file_refusal(tmp_path,
                                                                  box):
    """pandas' df.plot() imports matplotlib, which makes/reads its config
    folder in the user's home — outside the fence. The fence keeps matplotlib
    from loading the way a missing package would (an ImportError with a plain
    reason), so an uncaught df.plot() fails with pandas' own "matplotlib is
    required for plotting", not with a file refusal naming a home-folder
    path."""
    out = _run_shim(tmp_path, box,
                    "ax = pd.read_csv('runs.csv').plot()\nprint('plotted')")
    assert out["ran"] is False, out
    err = out.get("error") or ""
    assert "matplotlib" in err, err
    assert "file fence refused" not in err, err
    assert "plotted" not in (out.get("stdout") or "")


# ============================================================
# Review of the floor (2026-10-16): what it refused that it should not, and
# the one route it did not cover
# ============================================================

def _spawn_copy_cmd(src, dst):
    """A python one-liner that copies ``src`` to ``dst`` — run as a NEW
    process, which has no audit hook, so only the spawn itself can be
    refused."""
    return [sys.executable, "-c",
            f"open(r'{dst}', 'w').write(open(r'{src}').read())"]


def test_starting_a_program_is_refused_so_a_spawned_process_reads_nothing(
        tmp_path, box):
    """A process started from the run (os.system, subprocess.Popen,
    os.spawnv) runs in a fresh interpreter WITHOUT the hook: it could read and
    write anything. The floor refuses the spawn itself, so a regression in
    the layers above (which refuse `import subprocess` / `os`) still dies at
    the floor. Each spawn here would copy the canary's secret outside; none
    may run."""
    import subprocess as _sp
    copies = [box["outside"] / f"copy{i}.txt" for i in range(3)]
    argv = [_spawn_copy_cmd(box["canary"], c) for c in copies]
    ops = [
        ["system", _sp.list2cmdline(argv[0])],       # 0
        ["popen", argv[1]],                          # 1
        ["spawnv", argv[2]],                         # 2
        # the events a test must not really perform: raised directly
        ["audit", "os.exec", [sys.executable, ["x"], None]],          # 3
        ["audit", "os.startfile", [str(box["canary"]), "open"]],      # 4
        ["audit", "os.startfile/2",
         [str(box["canary"]), "open", "", None, 1]],                  # 5
        ["audit", "os.posix_spawn", [sys.executable, ["x"], None]],   # 6
        ["audit", "os.fork", []],                                     # 7
        ["audit", "_winapi.CreateProcess", [sys.executable, "x", None]],  # 8
        ["read", str(box["data"] / "runs.csv")],     # 9 still fine
    ]
    res = _run_driver(tmp_path, box, ops)
    for i in range(9):
        assert res[str(i)].startswith("REFUSED"), (i, res[str(i)])
        assert "starting a program" in res[str(i)], (i, res[str(i)])
    assert res["9"] == "OK"
    for c in copies:
        assert not c.exists(), f"a spawned process wrote {c}"


def test_the_floor_does_not_govern_stat_but_does_govern_listing(tmp_path,
                                                                 box):
    """Scope, as the module docstring states it: CPython raises no audit event
    for os.stat / os.path.exists / getsize, so a guessed outside path can be
    asked whether it exists (metadata only: no content, no listing). Listing
    the folder — the probe that finds names — IS refused."""
    ops = [
        ["exists", str(box["canary"])],              # 0 unaudited: answers
        ["stat", str(box["canary"])],                # 1 unaudited: answers
        ["listdir", str(box["outside"])],            # 2 refused
        ["read", str(box["canary"])],                # 3 refused
    ]
    res = _run_driver(tmp_path, box, ops)
    assert res["0"] == "OK" and res["1"] == "OK", res
    assert res["2"].startswith("REFUSED") and res["3"].startswith("REFUSED")
    assert SECRET not in json.dumps(res)
    doc = analyst_floor.__doc__
    assert "os.stat" in doc and "not" in doc


def test_a_library_spilling_to_the_default_temp_folder_still_runs(tmp_path,
                                                                  box):
    """A library that writes a temporary file in the DEFAULT temp folder
    (tempfile.TemporaryFile — openpyxl's writer, pyxlsb's reader, some
    compression paths) wrote into the user's %TEMP%, outside the fence, and
    was refused, ending the run with a path the model cannot act on. The
    child points the temp folder at its own throwaway working folder."""
    out = _run_shim(tmp_path, box,
                    "text, where, env = spill_to_temp()\n"
                    "print(text)\n"
                    "print('same', where == env)\n"
                    "print('where', where)\n",
                    FLOOR_SHIM_EXTRA="spill_to_temp")
    assert out["ran"] is True, out
    assert "spilled" in out["stdout"], out
    # tempfile and %TEMP% agree, and both are the run's own throwaway
    # working folder — not the user's temp folder itself
    assert "same True" in out["stdout"], out
    where = out["stdout"].split("where ", 1)[1].strip()
    assert os.path.basename(where).startswith("council-analyst-cwd-"), where
    assert not os.path.exists(where)          # removed with the run


def test_mkdir_of_a_folder_that_exists_changes_nothing_and_is_allowed(box):
    """Path.mkdir(parents=True, exist_ok=True) calls os.mkdir and swallows
    FileExistsError. On the vault itself (NEVER) the floor refused AND
    recorded it, so the run ended though nothing changed (the SQL / Mongo
    bridge's audit append does exactly this). Making a folder that already
    exists is allowed; making a new one outside is still refused."""
    fl = _floor(box)
    fl.audit("os.mkdir", (str(box["vault"]), 0o777, -1))      # exists
    fl.audit("os.mkdir", (str(box["outside"]), 0o777, -1))    # exists
    assert fl.refusals == []
    with pytest.raises(analyst_floor.Refused):
        fl.audit("os.mkdir", (str(box["outside"] / "new"), 0o777, -1))
    assert not (box["outside"] / "new").exists()


def test_the_sql_and_mongo_bridge_files_are_reachable_and_nothing_else(
        tmp_path, box, monkeypatch):
    """The SQL / Mongo bridge reads <vault>/sql_connections.json and
    mongo_connections.json and appends to <vault>/db_audit.log (rotating to
    .1 … .N). All three sit in the vault root, which is NEVER, so every
    bridge call ended the run. The fence grants the two registries read-only
    and the audit log (with its rotations) read-write — single files, not
    the vault root."""
    from council_core import analyst_child
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(box["vault"]))
    v = box["vault"]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ro, rw, nv = analyst_child._fence_roots(
        [str(box["data"])], str(run_dir / "code.py"),
        str(run_dir / "out.json"), "", str(run_dir))
    fl = analyst_floor.Floor(ro, rw, nv)
    assert fl.why_not(str(v / "sql_connections.json"), False, "reading") == ""
    assert fl.why_not(str(v / "mongo_connections.json"), False, "reading") == ""
    assert fl.why_not(str(v / "sql_connections.json"), True, "writing")
    assert fl.why_not(str(v / "db_audit.log"), True, "writing") == ""
    assert fl.why_not(str(v / "db_audit.log.1"), True, "moving onto") == ""
    # the rest of the vault root stays closed
    assert fl.why_not(str(v / "node_routing.json"), False, "reading")
    assert fl.why_not(str(v / "db_audit.log.evil"), True, "writing")
    assert fl.why_not(str(v / "db_audit.logx"), True, "writing")


def test_the_sql_bridge_lists_its_saved_connections_in_the_real_child(
        tmp_path, box):
    """End to end in the real child: list_sql_connections reads the registry
    in the vault root (on the floor branch it got {} and the run ended)."""
    (box["vault"] / "sql_connections.json").write_text(json.dumps(
        {"plant": "sqlite:///" + (box["data"] / "plant.db").as_posix()}),
        encoding="utf-8")
    out = _run_shim(tmp_path, box, "print(list_sql_connections(DATA_FOLDER))")
    assert out["ran"] is True, out
    assert "plant" in out["stdout"], out


def test_the_bridge_audit_append_runs_in_the_real_child(tmp_path, box):
    """After a query the bridge appends to <vault>/db_audit.log
    (db_connections._audit: mkdir of the vault, then an append). That
    ended the run; the record now lands and the run completes."""
    out = _run_shim(tmp_path, box, "print('lines', audit_db())",
                    FLOOR_SHIM_EXTRA="audit_db")
    assert out["ran"] is True, out
    assert "lines 1" in out["stdout"], out
    log = (box["vault"] / "db_audit.log").read_text(encoding="utf-8")
    assert '"sql.query"' in log


def test_a_caught_df_plot_does_not_end_the_run(tmp_path, box):
    """An import the sandbox does not offer fails like a missing package: an
    ImportError the code can catch, and NOT recorded as a run-ending
    refusal (no file was touched). A df.plot() / df.hist() made in passing,
    caught, leaves the run's answer standing."""
    out = _run_shim(tmp_path, box,
                    "df = pd.read_csv('runs.csv')\n"
                    "try:\n    df.plot()\nexcept Exception as e:\n"
                    "    print('no chart:', type(e).__name__)\n"
                    "try:\n    df.hist()\nexcept ImportError:\n"
                    "    print('no hist')\n"
                    "print('total', int(df['a'].sum()))\n")
    assert out["ran"] is True, out
    assert "no chart: ImportError" in out["stdout"], out
    assert "no hist" in out["stdout"] and "total 1" in out["stdout"], out


def test_the_matplotlib_refusal_is_an_import_error_not_a_record(box):
    fl = analyst_floor.Floor(refused_imports={"matplotlib": "no charts"})
    with pytest.raises(ImportError) as info:
        fl.audit("import", ("matplotlib.pyplot", None, [], [], []))
    assert isinstance(info.value, ModuleNotFoundError)
    assert "no charts" in str(info.value)
    assert fl.refusals == []


def test_pd_show_versions_runs_under_the_floor(tmp_path, box):
    """pd.show_versions() probes optional dependencies (matplotlib among
    them) in try/except ImportError; it ran before the floor."""
    out = _run_shim(tmp_path, box, "pd.show_versions()\nprint('done')")
    assert out["ran"] is True, out
    assert "done" in out["stdout"]


# ---- a vault on a network share -------------------------------------------
def _loopback_unc(p: Path) -> str:
    """``p`` spelled through this PC's own admin share (\\\\localhost\\C$\\…),
    loopback only."""
    drive, rest = os.path.splitdrive(str(p))
    return "\\\\localhost\\" + drive[0] + "$" + rest


def _unc_or_skip(p: Path) -> str:
    if os.name != "nt":
        pytest.skip("UNC paths are Windows-only")
    unc = _loopback_unc(p)
    if not os.path.isdir(unc):
        pytest.skip("this PC's admin share is not reachable here")
    return unc


def test_a_root_on_a_network_share_is_kept_and_reads_inside_it_pass(box):
    """A vault on a share: the floor dropped every network root and refused
    every network path, so nothing in the vault could be read. A network
    ROOT is now kept, and a network path inside it is allowed like any
    other; outside it, it is refused."""
    unc_vault = _unc_or_skip(box["vault"])
    fl = analyst_floor.Floor(
        read_only=[unc_vault + "\\data_in", str(box["pylib"])],
        read_write=[unc_vault + "\\data_out"],
        never=[unc_vault])
    assert fl.why_not(unc_vault + "\\data_in\\runs.csv", False, "reading") == ""
    assert fl.why_not(unc_vault + "\\data_out\\new.csv", True, "writing") == ""
    # the rest of the vault on the share stays closed
    assert fl.why_not(unc_vault + "\\node_routing.json", False, "reading")
    # a data_in file is still read-only
    assert fl.why_not(unc_vault + "\\data_in\\runs.csv", True, "writing")
    # the canary outside the vault, through the share, is refused
    why = fl.why_not(_loopback_unc(box["canary"]), False, "reading")
    assert why and "not allowed" in why


def test_a_network_root_does_not_open_other_shares_or_devices(box,
                                                              monkeypatch):
    """With a root on one share, a path on ANOTHER share is refused from its
    spelling, without resolving it (resolving asks the network for the host
    first); devices stay refused."""
    unc_vault = _unc_or_skip(box["vault"])
    fl = analyst_floor.Floor(read_only=[unc_vault + "\\data_in"])
    asked = []
    real = analyst_floor._REALPATH
    monkeypatch.setattr(analyst_floor, "_REALPATH",
                        lambda s: asked.append(s) or real(s))
    for p in (r"\\otherhost\share\x.csv", r"\\?\UNC\otherhost\share\x.csv",
              r"\\.\PhysicalDrive0", r"\\?\GLOBALROOT\Device\x",
              str(box["data"] / "NUL")):
        why = fl.why_not(p, False, "reading")
        assert why and "network share or a device" in why, (p, why)
    assert not any("otherhost" in a or "PhysicalDrive" in a for a in asked)


def test_making_an_existing_folder_on_the_vaults_own_share_is_allowed(box):
    """The SQL / Mongo bridge's audit append makes the vault folder first
    (mkdir(exist_ok=True) — a no-op when it exists). For a vault on a share
    that mkdir was refused, and recorded, because a network spelling was
    never asked whether it exists — so every bridge call on a network vault
    ended the run. A folder on the roots' own share may be asked about; a
    new folder there outside the writable roots is still refused."""
    unc_vault = _unc_or_skip(box["vault"])
    fl = analyst_floor.Floor(
        read_only=[unc_vault + "\\data_in"],
        read_write=[unc_vault + "\\data_out"],
        never=[unc_vault])
    fl.audit("os.mkdir", (unc_vault, 0o777, -1))             # exists
    fl.audit("os.mkdir", (unc_vault + "\\data_in", 0o777, -1))
    assert fl.refusals == []
    with pytest.raises(analyst_floor.Refused):
        fl.audit("os.mkdir", (unc_vault + "\\new_folder", 0o777, -1))
    assert not (box["vault"] / "new_folder").exists()


def test_the_bridge_audit_append_runs_on_a_network_vault(tmp_path, box):
    """End to end in the real child, the vault reached as \\\\localhost\\C$:
    the bridge's audit append (mkdir of the vault, then the append) lands
    and the run completes."""
    unc_vault = _unc_or_skip(box["vault"])
    out = _run_shim(tmp_path, box, "print('lines', audit_db())",
                    data=unc_vault + "\\data_in",
                    COUNCIL_VAULT_ROOT=unc_vault,
                    FLOOR_SHIM_EXTRA="audit_db")
    assert out["ran"] is True, out
    assert "lines 1" in out["stdout"], out
    log = (box["vault"] / "db_audit.log").read_text(encoding="utf-8")
    assert '"sql.query"' in log


def test_a_vault_on_a_mapped_network_drive_is_judged_by_its_share(
        monkeypatch):
    """A mapped drive letter (Z:\\vault) resolves to the share behind it
    (\\\\fileserver\\share\\vault) — realpath answers with the UNC form. The
    roots then sit on that share, so a Z:\\ path inside them is allowed and
    one outside them refused like any other, and a path on another share is
    still refused from its spelling. realpath is stood in for here (no drive
    is mapped on this PC, and no share is asked about): Z:\\ maps to the
    share, anything else spelled \\\\ must never reach it."""
    if os.name != "nt":
        pytest.skip("drive letters are Windows-only")

    def fake_realpath(s):
        s = str(s)
        if s.startswith("\\\\"):
            raise AssertionError(f"a network path was resolved: {s}")
        if s[:3].upper() == "Z:\\":
            return "\\\\fileserver\\share\\" + s[3:]
        return s
    monkeypatch.setattr(analyst_floor, "_REALPATH", fake_realpath)
    fl = analyst_floor.Floor(read_only=[r"Z:\vault\data_in"],
                             read_write=[r"Z:\vault\data_out"],
                             never=[r"Z:\vault"])
    assert fl.why_not(r"Z:\vault\data_in\runs.csv", False, "reading") == ""
    assert fl.why_not(r"Z:\vault\data_out\new.csv", True, "writing") == ""
    assert "outside the folders" in fl.why_not(
        r"Z:\vault\node_routing.json", False, "reading")
    assert "outside the folders" in fl.why_not(
        r"Z:\elsewhere\secret.txt", False, "reading")
    assert "read-only" in fl.why_not(
        r"Z:\vault\data_in\runs.csv", True, "writing")
    why = fl.why_not(r"\\otherhost\share\x.csv", False, "reading")
    assert "network share or a device" in why, why


def test_a_vault_on_a_network_share_still_runs_code_in_the_real_child(
        tmp_path, box):
    """End to end: run_python's child on a vault reached as \\\\localhost\\C$
    reads its data (it did before the floor; on the floor branch every read
    was refused)."""
    unc_vault = Path(_unc_or_skip(box["vault"]))
    from council_core import analyst_step
    run = analyst_step.run_script(
        "print('rows', len(pd.read_csv('runs.csv')), count_files())",
        unc_vault / "data_in", vault=unc_vault)
    assert run.ran, run.error
    assert "rows 1" in run.stdout, run.stdout
