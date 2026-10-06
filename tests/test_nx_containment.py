"""
Model-written pipeline scripts cannot delete or overwrite the user's files.

Before this, nx_policy.validate_script allowed pathlib, so a model-written
script could Path(doc).unlink(), write_text over a document, rename one, or
np.save over it; and a writer filter (WriteDREAM3DFilter.export_file_path,
ITKImageWriterFilter.file_name, any os.PathLike output in the catalog) could
be pointed at ANY path — the vault root, the Desktop, a UNC share, a path
with .., a junction out of the output area. Scripts had no data_out
containment at all, unlike nx_bridge.run_folder.

Three layers, each tested here on its own:

  * STATIC — nx_policy.validate_script (the MODEL rules) refuses
    file-changing calls, routes to modules the allowlist keeps out, dunder
    names, held builtins;
  * RUN TIME — nx_guard, inside the script's own interpreter, holds every
    filter OUTPUT path and every file change the script makes to the output
    area, never over a file the run did not make, never into an input. These
    tests run it DIRECTLY, past the static check, as a script that evaded it
    would;
  * the WORKFLOW RUNNER — which kind of script it is (nx_policy.script_trust),
    which rules apply, and that a refusal fails the step.

Tests that need simplnx run it in the real nxpython env (nx_bridge.
find_python) and SKIP without it. Everything happens under tmp_path: no test
can reach a real document, and the Desktop/UNC cases are checked in process
(no file can be written there whatever the result).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import nx_bridge
import nx_guard
import nx_policy
import path_contain
import workflow_runner as wr

HELPERS = Path(__file__).resolve().parent / "data" / "dream3d_e2e"
REAL_CATALOG = json.loads((HELPERS / "nx_catalog.json").read_text(
    encoding="utf-8"))
NXPY = nx_bridge.find_python()
needs_nx = pytest.mark.skipif(NXPY is None,
                              reason="needs the DREAM3D-NX env (nxpython)")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows paths")
EXECUTE_PROCESS = "fb511a70-2175-4595-8c11-d1b5b6794221"


# ============================================================
# A vault to attack, under tmp_path
# ============================================================

class Vault:
    def __init__(self, root: Path):
        self.root = root / "vault"
        self.out = self.root / "data_out"
        self.inp = self.root / "data_in"
        self.work = self.out / "work"
        self.docs = self.root / "docs"
        for d in (self.out, self.inp, self.work, self.docs):
            d.mkdir(parents=True, exist_ok=True)
        self.notes = self.root / "notes.txt"
        self.notes.write_text("the user's notes", encoding="utf-8")
        (self.docs / "a.txt").write_text("doc a", encoding="utf-8")
        self.old = self.out / "old.dream3d"
        self.old.write_text("an earlier run's result", encoding="utf-8")
        self.input = self.inp / "in.dream3d"
        self.input.write_text("input data", encoding="utf-8")

    def snapshot(self):
        """Every file under the vault (outside data_out/work) and its bytes.
        Walked by hand: rglob on 3.11 follows a junction (it is not a
        "symlink" there), and the test's junction points back at the vault."""
        import stat
        snap = {}

        def walk(d: Path) -> None:
            for e in os.scandir(d):
                p = Path(e.path)
                attrs = getattr(e.stat(follow_symlinks=False),
                                "st_file_attributes", 0)
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if p != self.work:
                        walk(p)
                elif e.is_file(follow_symlinks=False):
                    snap[p.relative_to(self.root).as_posix()] = p.read_bytes()
        walk(self.root)
        return snap

    def policy(self, report: Path, *, trust="model", nx=True) -> dict:
        return {"trust": trust, "nx": nx, "write_roots": [str(self.out)],
                "own_roots": [str(self.work)], "read_only": [str(self.inp)],
                "own_files": [], "report": str(report)}


def _guard_run(vault: Vault, tmp: Path, code: str, *, python: str,
               trust="model", nx=True, timeout=300):
    """Run ``code`` under nx_guard directly (no static check first)."""
    script = vault.work / f"s_{uuid.uuid4().hex[:8]}.py"
    script.write_text(code, encoding="utf-8")
    report = tmp / f"report_{uuid.uuid4().hex[:8]}.jsonl"
    pol = tmp / f"policy_{uuid.uuid4().hex[:8]}.json"
    pol.write_text(json.dumps(vault.policy(report, trust=trust, nx=nx)),
                   encoding="utf-8")
    proc = subprocess.run(
        [python, "-u", str(wr.GUARD), str(pol), str(script)],
        cwd=str(vault.work), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    refusals, done = nx_guard.read_report(report)
    return proc, refusals, done


_ATTEMPT = '''
def attempt(name, fn):
    try:
        fn()
    except PermissionError as exc:
        print("REFUSED", name, flush=True)
    except Exception as exc:
        print("ERROR", name, type(exc).__name__, str(exc)[:200], flush=True)
    else:
        print("OK", name, flush=True)
'''


def _outcomes(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] in ("REFUSED", "OK", "ERROR"):
            out[parts[1]] = parts[0]
    return out


# ============================================================
# 1. STATIC: the MODEL rules refuse these before anything runs
# ============================================================

_PRE = "import simplnx as nx\nfrom pathlib import Path\nimport numpy as np\n"
DOC = "C:/Users/someone/vault/notes.txt"

STATIC_ATTACKS = {
    "unlink a document": _PRE + f"Path({DOC!r}).unlink()\n",
    "overwrite a document": _PRE + f"Path({DOC!r}).write_text('')\n",
    "write bytes": _PRE + f"Path({DOC!r}).write_bytes(b'')\n",
    "rename a document": _PRE + f"Path({DOC!r}).rename('x.txt')\n",
    "rmdir": _PRE + "Path('C:/Users/someone/vault').rmdir()\n",
    "touch": _PRE + f"Path({DOC!r}).touch()\n",
    "open for writing": _PRE + f"open({DOC!r}, 'w').write('')\n",
    "open held, then called": _PRE + f"f = open\nf({DOC!r}, 'w')\n",
    "Path.open held": _PRE + f"w = Path({DOC!r}).open\nw('w')\n",
    "write_text held": _PRE + f"w = Path({DOC!r}).write_text\nw('')\n",
    "numpy save": _PRE + f"np.save({DOC!r}, np.zeros(3))\n",
    "numpy savetxt": _PRE + f"np.savetxt({DOC!r}, np.zeros(3))\n",
    "ndarray.tofile": _PRE + f"np.zeros(3).tofile({DOC!r})\n",
    "numpy memmap": _PRE + f"np.memmap({DOC!r}, mode='w+', shape=(3,))\n",
    "from numpy import save": "from numpy import save\nsave('x', 1)\n",
    "import shutil": "import shutil\nshutil.rmtree('C:/Users/someone/vault')\n",
    "import os": "import os\nos.remove('x')\n",
    "importlib": "import importlib\nimportlib.import_module('shutil')"
                 ".rmtree('v')\n",
    "__import__": "__import__('shutil').rmtree('v')\n",
    "__import__ held": "i = __import__\ni('shutil').rmtree('v')\n",
    "getattr on builtins": "getattr(__builtins__, '__import__')('os')\n",
    "__builtins__ subscript": "__builtins__['open']('x', 'w')\n",
    "pathlib.os": "import pathlib\npathlib.os.remove('x')\n",
    "typing.sys.modules": "import typing\ntyping.sys.modules['shutil']"
                          ".rmtree('v')\n",
    "json.codecs.open": "import json\njson.codecs.open('x', 'w')\n",
    "typing.types": "import typing\ntyping.types.FunctionType\n",
    "re.functools": "import re\nre.functools.partial\n",
    "dunder class walk": "x = ().__class__.__base__.__subclasses__()\n",
    "vars of a module": "import simplnx as nx\nvars(nx)\n",
    "__dict__": "import simplnx as nx\nnx.__dict__\n",
    "a __fspath__ that changes its mind":
        "class P:\n    def __fspath__(self):\n        return 'x'\n",
    "exec": "exec('import os')\n",
    "eval": "eval('1')\n",
    "compile": "compile('1', 'x', 'eval')\n",
    "get_filters": "import simplnx as nx\nnx.get_filters()[7].execute("
                   "data_structure=None)\n",
    "a saved pipeline": "import simplnx as nx\nnx.Pipeline.from_file("
                        "'x.d3dpipeline').execute(nx.DataStructure())\n",
    "Execute Process by name": "import simplnx as nx\n"
                               "nx.ExecuteProcessFilter.execute("
                               "data_structure=None)\n",
    "Execute Process by uuid text": f"x = '{EXECUTE_PROCESS}'\n",
    "ctypes": "import ctypes\nctypes.CDLL('kernel32')\n",
    "subprocess": "import subprocess\nsubprocess.run(['cmd'])\n",
    "append_to_dream3d_file": "import simplnx as nx\n"
                              f"nx.append_to_dream3d_file({DOC!r}, None, "
                              "None)\n",
    "test_filter": "import simplnx as nx\n"
                   "nx.test_filter(nx.WriteDREAM3DFilter())\n",
}


@pytest.mark.parametrize("name", sorted(STATIC_ATTACKS))
def test_the_model_rules_refuse_it_before_it_runs(name):
    code = STATIC_ATTACKS[name]
    ok, reasons = nx_policy.validate_script(code)
    assert not ok and reasons, f"{name} passed validate_script"
    # ...and the workflow runner applies exactly that to a MODEL script.
    assert nx_policy.run_reasons(code, trust=nx_policy.MODEL) == reasons


def test_the_model_rules_still_allow_a_real_pipeline():
    """Reading inputs, joining paths, exists(), glob(), mkdir for an output
    folder, str.replace, DataStructure.remove, ndarray.copy, `if __name__`."""
    code = (
        "import simplnx as nx\nimport numpy as np\nfrom pathlib import Path\n"
        "ds = nx.DataStructure()\n"
        "src = Path('C:/data/in') / 'a.dream3d'\n"
        "out = Path('C:/data/out')\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "for f in sorted(src.parent.glob('*.csv')):\n"
        "    if f.exists():\n"
        "        a = np.loadtxt(str(f), delimiter=',').copy()\n"
        "name = src.name.replace('.dream3d', '_out.dream3d')\n"
        "ds.remove(nx.DataPath('A'))\n"
        "r = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
        "export_file_path=str(out / name))\n"
        "if __name__ == '__main__':\n    print(r.errors)\n")
    assert nx_policy.validate_script(code) == (True, [])


def _shipped_root():
    if not NXPY:
        return None
    env = Path(NXPY).parent
    for cand in (env / "Library" / "share" / "simplnx" / "pipelines",
                 env.parent / "share" / "simplnx" / "pipelines"):
        if cand.is_dir():
            return cand
    return None


@pytest.mark.skipif(_shipped_root() is None,
                    reason="needs the nxpython env's shipped pipelines")
def test_every_shipped_pipeline_transpiles_to_a_script_the_model_rules_take():
    """A transpile saved in data_out is a MODEL script by location, so the
    tighter rules must not refuse what the transpiler writes (67 shipped
    pipelines on the installed build)."""
    import nx_transpile
    pipes = sorted(_shipped_root().rglob("*.d3dpipeline"))
    assert pipes
    refused = {}
    for p in pipes:
        ok, reasons = nx_policy.validate_script(
            nx_transpile.transpile(p, REAL_CATALOG)["code"])
        if not ok:
            refused[p.name] = reasons[:2]
    assert not refused, refused


# ============================================================
# 2. RUN TIME: nx_guard, past the static check
# ============================================================

# Each is an attempt() in ONE guarded nx run; the run is shared.
FILTER_ATTACKS = {
    "vault_root": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                  "export_file_path=V['vault_new'])",
    "overwrite_vault_document": "nx.WriteDREAM3DFilter.execute("
                                "data_structure=ds, "
                                "export_file_path=V['notes'])",
    "overwrite_earlier_output": "nx.WriteDREAM3DFilter.execute("
                                "data_structure=ds, export_file_path=V['old'])",
    "dot_dot_escape": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                      "export_file_path=V['dotdot'])",
    "junction_out_of_data_out": "nx.WriteDREAM3DFilter.execute("
                                "data_structure=ds, "
                                "export_file_path=V['via_junction'])",
    "unc_share": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                 "export_file_path=V['unc'])",
    "over_an_input": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                     "export_file_path=V['input'])",
    "positional_argument": "nx.WriteDREAM3DFilter.execute(ds, "
                           "V['vault_pos'])",
    "through_execute2": "nx.WriteDREAM3DFilter().execute2(ds, "
                        "export_file_path=V['vault_e2'])",
    "image_writer_file_name": "nxitk.ITKImageWriterFilter.execute("
                              "data_structure=ds, file_name=V['vault_png'])",
    "csv_writer_to_vault": "nx.WriteFeatureDataCSVFilter.execute("
                           "data_structure=ds, feature_data_file=V['csv'])",
    "stl_prefix_climbs_out": "nx.WriteStlFileFilter.execute("
                             "data_structure=ds, output_stl_directory="
                             "V['stl_dir'], output_stl_prefix='..\\\\..\\\\x_')",
    "default_into_a_full_folder": "nx.WriteASCIIDataFilter.execute("
                                  "data_structure=ds, output_dir=V['out'])",
    # Rebinding the class attribute cannot unwrap the filter: the binding's
    # own function is held only inside the wrapper, so whatever the script
    # rebinds can at most call the wrapper again.
    "rebind_then_call": "rebind_and_call()",
    "a_stream_inside_a_file": "nx.WriteDREAM3DFilter.execute("
                              "data_structure=ds, "
                              "export_file_path=V['fresh'] + ':hidden')",
    # vtk is installed in the nx env, and its writers write from C++.
    "a_native_writer_package": "__import__('vtk')",
    # simplnx's own module functions that write or run a filter past the
    # class wrappers.
    "append_into_a_vault_file": "nx.append_to_dream3d_file(V['notes'], ds, "
                                "nx.DataPath('A'))",
    "append_into_an_earlier_output": "nx.append_to_dream3d_file("
                                     "path=V['old'], data_structure=ds, "
                                     "data_path=nx.DataPath('A'))",
    "test_filter_runs_unseen": "nx.test_filter(nx.WriteDREAM3DFilter())",
    "load_a_python_plugin": "nx.load_python_plugin(nx)",
    "reimport_simplnx_unwrapped": "reimport()",
}
FILTER_OK = {
    "fresh_output": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path=V['fresh'], write_xdmf_file=True)",
    "its_own_output_again": "nx.WriteDREAM3DFilter.execute("
                            "data_structure=ds, export_file_path=V['fresh'])",
    "pathlike_read_once": "nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                          "export_file_path=Flip())",
}


@pytest.fixture(scope="module")
def filter_run(tmp_path_factory):
    if NXPY is None:
        pytest.skip("needs the DREAM3D-NX env (nxpython)")
    import _winapi
    tmp = tmp_path_factory.mktemp("filters")
    v = Vault(tmp)
    link = v.out / "link"
    _winapi.CreateJunction(str(v.root), str(link))
    names = {
        "vault_new": str(v.root / "new.dream3d"),
        "notes": str(v.notes),
        "old": str(v.old),
        "dotdot": str(v.work / ".." / ".." / "escaped.dream3d"),
        "via_junction": str(link / "via_junction.dream3d"),
        "unc": "\\\\council-no-such-host\\share\\x.dream3d",
        "input": str(v.input),
        "vault_pos": str(v.root / "pos.dream3d"),
        "vault_e2": str(v.root / "e2.dream3d"),
        "vault_rebind": str(v.root / "rebind.dream3d"),
        "vault_png": str(v.root / "img.png"),
        "csv": str(v.root / "features.csv"),
        "stl_dir": str(v.out / "stl"),
        "out": str(v.out),
        "fresh": str(v.out / "fresh.dream3d"),
        "safe": str(v.out / "flip_safe.dream3d"),
        "evil": str(v.root / "flip_evil.dream3d"),
    }
    before = v.snapshot()
    lines = ["import simplnx as nx", "import itkimageprocessing as nxitk",
             f"V = {names!r}", _ATTEMPT,
             "ds = nx.DataStructure()",
             "nx.CreateDataArrayFilter.execute(data_structure=ds, "
             "output_array_path=nx.DataPath('A'), tuple_dimensions=[[3]])",
             "class Flip:",
             "    n = 0",
             "    def __fspath__(self):",
             "        Flip.n += 1",
             "        return V['safe'] if Flip.n == 1 else V['evil']",
             "def rebind_and_call():",
             "    held = nx.WriteDREAM3DFilter.execute",
             "    nx.WriteDREAM3DFilter.execute = staticmethod(",
             "        lambda **k: held(**k))",
             "    try:",
             "        nx.WriteDREAM3DFilter.execute(data_structure=ds, "
             "export_file_path=V['vault_rebind'])",
             "    finally:",
             "        nx.WriteDREAM3DFilter.execute = staticmethod(held)",
             "def reimport():",
             "    mods = __import__('sys').modules",
             "    saved = mods.pop('simplnx')",
             "    try:",
             "        __import__('simplnx')",
             "    finally:",
             "        mods['simplnx'] = saved"]
    for name, call in list(FILTER_ATTACKS.items()) + list(FILTER_OK.items()):
        lines.append(f"attempt({name!r}, lambda: {call})")
    t0 = time.perf_counter()
    proc, refusals, done = _guard_run(v, tmp, "\n".join(lines) + "\n",
                                      python=NXPY)
    took = time.perf_counter() - t0
    try:
        yield {"vault": v, "names": names, "before": before, "proc": proc,
               "refusals": refusals, "done": done, "took": took,
               "outcomes": _outcomes(proc.stdout)}
    finally:
        os.rmdir(link)                  # the junction only, never its target


@needs_nx
@windows_only
@pytest.mark.parametrize("name", sorted(FILTER_ATTACKS))
def test_a_filter_output_outside_the_rules_is_refused_at_run_time(filter_run,
                                                                  name):
    got = filter_run["outcomes"].get(name)
    assert got == "REFUSED", (name, got, filter_run["proc"].stdout[-2000:],
                              filter_run["proc"].stderr[-2000:])


@needs_nx
@windows_only
def test_nothing_of_the_users_was_touched_by_the_filter_attacks(filter_run):
    v = filter_run["vault"]
    after = v.snapshot()
    before = filter_run["before"]
    for rel, data in before.items():
        assert after.get(rel) == data, f"{rel} was changed or deleted"
    new = sorted(set(after) - set(before))
    # The only new files are the allowed outputs inside data_out.
    assert all(n.startswith("data_out/") for n in new), new
    assert not (v.root / "flip_evil.dream3d").exists()
    assert not any(v.root.glob("*.dream3d"))
    assert not (v.root / "escaped.dream3d").exists()


@needs_nx
@windows_only
def test_the_run_reports_every_refusal_and_fails(filter_run):
    assert filter_run["proc"].returncode == nx_guard.EXIT_REFUSED
    assert filter_run["done"]
    assert len(filter_run["refusals"]) >= len(FILTER_ATTACKS)
    # The UNC path was refused from its spelling, not after a network lookup.
    assert any("network share" in r for r in filter_run["refusals"])


@needs_nx
@windows_only
@pytest.mark.parametrize("name", sorted(FILTER_OK))
def test_writing_inside_the_output_area_still_works(filter_run, name):
    assert filter_run["outcomes"].get(name) == "OK", (
        name, filter_run["proc"].stdout[-2000:],
        filter_run["proc"].stderr[-2000:])
    v = filter_run["vault"]
    assert (v.out / "fresh.dream3d").is_file()
    assert (v.out / "fresh.xdmf").is_file()
    assert (v.out / "flip_safe.dream3d").is_file()     # read ONCE, used once


# Python-level attacks: the audit hook. Plain Python is enough (the guard
# runs in either interpreter), so these run with this one.
PY_ATTACKS = {
    "unlink_document": "Path(V['notes']).unlink()",
    "overwrite_document": "Path(V['notes']).write_text('gone')",
    "append_to_document": "open(V['notes'], 'a').write('x')",
    "rename_document": "Path(V['doc_a']).rename(V['moved'])",
    "replace_document": "Path(V['doc_a']).replace(V['moved'])",
    "rename_into_data_out": "os.rename(V['notes'], V['in_out'])",
    "rmtree_via_importlib": "importlib.import_module('shutil')"
                            ".rmtree(V['docs'])",
    "rmtree_via_getattr": "getattr(__import__('shutil'), 'rmtree')(V['docs'])",
    "os_remove_via_getattr": "getattr(__import__('os'), 'remove')(V['notes'])",
    "remove_an_earlier_output": "os.remove(V['old'])",
    "truncate_an_earlier_output": "open(V['old'], 'r+').truncate(0)",
    "numpy_save_over_document": "np.save(V['notes'], np.zeros(3))",
    "write_into_input": "open(V['input'], 'wb').write(b'')",
    "mkdir_outside": "os.mkdir(V['newdir'])",
    "make_a_junction": "__import__('_winapi').CreateJunction(V['root'], "
                       "V['newlink'])",
    "symlink": "os.symlink(V['root'], V['newlink'])",
    "shell": "os.system('echo council-probe')",
    "subprocess": "__import__('subprocess').run(['cmd', '/c', 'echo', 'x'])",
    "ctypes": "__import__('ctypes').CDLL('kernel32')",
    "chmod_document": "os.chmod(V['notes'], 0o444)",
    "native_writer_h5py": "__import__('h5py')",
    "sqlite_database": "__import__('sqlite3').connect(V['notes'] + '.db')",
}
PY_OK = {
    "write_new_in_data_out": "Path(V['in_out_new']).write_text('mine')",
    "rewrite_own_file": "Path(V['in_out_new']).write_text('mine again')",
    "delete_own_file": "Path(V['in_out_new']).unlink()",
    "mkdir_in_data_out": "Path(V['out_sub']).mkdir(parents=True, "
                         "exist_ok=True)",
    "mkdir_existing_data_out": "Path(V['out']).mkdir(exist_ok=True)",
    "read_a_document": "Path(V['notes']).read_text()",
    "write_in_working_folder": "open('scratch.txt', 'w').write('x')",
}


@pytest.fixture(scope="module")
def py_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("python")
    v = Vault(tmp)
    names = {"root": str(v.root), "notes": str(v.notes),
             "doc_a": str(v.docs / "a.txt"), "docs": str(v.docs),
             "moved": str(v.out / "moved.txt"),
             "in_out": str(v.out / "notes_moved.txt"),
             "old": str(v.old), "input": str(v.input),
             "newdir": str(v.root / "newdir"),
             "newlink": str(v.out / "newlink"),
             "in_out_new": str(v.out / "mine.txt"),
             "out_sub": str(v.out / "sub" / "deeper"), "out": str(v.out)}
    before = v.snapshot()
    lines = ["import os, importlib", "from pathlib import Path",
             "import numpy as np", f"V = {names!r}", _ATTEMPT]
    for name, call in list(PY_ATTACKS.items()) + list(PY_OK.items()):
        lines.append(f"attempt({name!r}, lambda: {call})")
    proc, refusals, done = _guard_run(v, tmp, "\n".join(lines) + "\n",
                                      python=sys.executable, nx=False)
    return {"vault": v, "before": before, "proc": proc,
            "refusals": refusals, "done": done,
            "outcomes": _outcomes(proc.stdout)}


@windows_only
@pytest.mark.parametrize("name", sorted(PY_ATTACKS))
def test_a_file_change_the_script_makes_itself_is_refused(py_run, name):
    assert py_run["outcomes"].get(name) == "REFUSED", (
        name, py_run["outcomes"].get(name), py_run["proc"].stdout[-2000:],
        py_run["proc"].stderr[-2000:])


@windows_only
@pytest.mark.parametrize("name", sorted(PY_OK))
def test_what_a_pipeline_legitimately_does_is_allowed(py_run, name):
    assert py_run["outcomes"].get(name) == "OK", (
        name, py_run["outcomes"].get(name), py_run["proc"].stderr[-2000:])


@windows_only
def test_nothing_of_the_users_was_touched_by_the_python_attacks(py_run):
    v = py_run["vault"]
    after = v.snapshot()
    for rel, data in py_run["before"].items():
        assert after.get(rel) == data, f"{rel} was changed or deleted"
    assert not (v.root / "newdir").exists()
    assert not (v.out / "newlink").exists()
    assert (v.out / "sub" / "deeper").is_dir()
    # A refusal the script swallowed still fails the run.
    assert py_run["proc"].returncode == nx_guard.EXIT_REFUSED
    assert py_run["done"] and len(py_run["refusals"]) >= len(PY_ATTACKS)


@windows_only
def test_the_desktop_unc_and_device_paths_are_outside(tmp_path):
    """In process: whatever the outcome, nothing can be written there."""
    v = Vault(tmp_path)
    g = nx_guard.Guard(v.policy(None))
    probe = f"council_probe_{uuid.uuid4().hex}.dream3d"
    for target in (Path.home() / "Desktop" / probe,
                   Path.home() / probe,
                   "\\\\127.0.0.1\\c$\\" + probe,
                   "\\\\?\\UNC\\council-no-such-host\\share\\" + probe,
                   "\\\\.\\PhysicalDrive0", "NUL", str(v.out / "CON"),
                   str(v.out / "x.dream3d:stream")):
        t0 = time.perf_counter()
        with pytest.raises(nx_guard.Refused):
            g.check_write(target, "test")
        assert time.perf_counter() - t0 < 1.0, f"{target} looked something up"
    assert not (Path.home() / "Desktop" / probe).exists()


@windows_only
def test_a_symlink_out_of_the_output_area_is_outside(tmp_path):
    """A directory symlink needs a privilege (or Developer Mode); skipped
    when this account cannot make one. The junction case runs always."""
    v = Vault(tmp_path)
    link = v.out / "sym"
    try:
        os.symlink(v.root, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot make a symlink here: {exc}")
    try:
        g = nx_guard.Guard(v.policy(None))
        with pytest.raises(nx_guard.Refused, match="outside the output area"):
            g.check_write(link / "via_symlink.dream3d", "test")
        with pytest.raises(nx_guard.Refused):
            g.check_write(link / "notes.txt", "test")
    finally:
        os.rmdir(link)


@needs_nx
def test_every_installed_filter_is_wrapped():
    """All 289 filters of the installed build (and IFilter.execute2 and
    Pipeline.execute) go through the guard — the plugins' too."""
    code = ("import sys\nsys.path.insert(0, %r)\nimport nx_guard\n"
            "g = nx_guard.Guard({'trust': 'user'})\n"
            "g.patch(nx_guard._import_modules(True, False))\n"
            "names = sorted(c.__name__ for c in g.patched)\n"
            "print(len(names), 'IFilter' in names, 'Pipeline' in names)\n"
            % str(Path(wr.GUARD).parent))
    proc = subprocess.run([NXPY, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    n, has_base, has_pipe = proc.stdout.split()[-3:]
    assert int(n) == len(REAL_CATALOG["filters"]) + 2      # + IFilter, Pipeline
    assert has_base == "True" and has_pipe == "True"


def test_path_role_finds_every_output_in_the_installed_catalog():
    """Every os.PathLike output on the installed build, by the fail-safe
    rule — including writers whose names do not say so."""
    outs = sorted(
        f"{f['py_attr']}.{p['name']}" for f in REAL_CATALOG["filters"]
        for p in f["execute"]["params"]
        if nx_guard.path_role(f["py_attr"], p["name"], p.get("type"))
        == "output")
    assert outs == sorted([
        "ComputeGBCDMetricBasedFilter.dist_output_file",
        "ComputeGBCDMetricBasedFilter.err_output_file",
        "ComputeGBPDMetricBasedFilter.dist_output_file",
        "ComputeGBPDMetricBasedFilter.err_output_file",
        "ConvertHexGridToSquareGridFilter.output_path",
        "CreatePythonSkeletonFilter.plugin_output_directory",
        "EbsdToH5EbsdFilter.output_file_path",
        "ExecuteProcessFilter.output_log_file",
        "ExtractPipelineToFileFilter.output_file_path",
        "ITKImageWriterFilter.file_name",
        "WriteASCIIDataFilter.output_dir",
        "WriteASCIIDataFilter.output_path",
        "WriteAbaqusHexahedronFilter.output_path",
        "WriteAvizoRectilinearCoordinateFilter.output_file",
        "WriteAvizoUniformCoordinateFilter.output_file",
        "WriteBinaryDataFilter.output_path",
        "WriteDREAM3DFilter.export_file_path",
        "WriteFeatureDataCSVFilter.feature_data_file",
        "WriteGBCDGMTFileFilter.output_file",
        "WriteGBCDTriangleDataFilter.output_file",
        "WriteINLFileFilter.output_file",
        "WriteLAMMPSFileFilter.output_file",
        "WriteLosAlamosFFTFilter.output_file",
        "WriteNodesAndElementsFilesFilter.element_file_path",
        "WriteNodesAndElementsFilesFilter.node_file_path",
        "WritePoleFigureFilter.output_path",
        "WriteSPParksSitesFilter.output_file",
        "WriteStatsGenOdfAngleFileFilter.output_file",
        "WriteStlFileFilter.output_stl_directory",
        "WriteStlFileFilter.output_stl_file",
        "WriteVtkRectilinearGridFilter.output_file",
        "WriteVtkStructuredPointsFilter.output_file",
    ])
    # The readers' paths are inputs, never outputs.
    assert nx_guard.path_role("ITKImageReaderFilter", "file_name",
                              "os.PathLike") == "input"
    assert nx_guard.path_role("CombineStlFilesFilter", "stl_files_path",
                              "os.PathLike") == "input"


# ============================================================
# 3. USER scripts: their call — but no Execute Process, even at run time
# ============================================================

@needs_nx
def test_a_user_script_reaching_execute_process_at_run_time_is_refused(
        tmp_path):
    """get_filters() hides which filter runs from the text check; the guard
    sees the class when it executes."""
    v = Vault(tmp_path)
    code = (_ATTEMPT + "import simplnx as nx\nds = nx.DataStructure()\n"
            "ep = [c for c in nx.get_filters() "
            "if c.__name__.startswith('Execute')][0]\n"
            "attempt('by_list', lambda: ep.execute(data_structure=ds, "
            "arguments='cmd /c echo council-probe', blocking=True))\n"
            "attempt('by_instance', lambda: ep().execute2(ds, "
            "arguments='cmd /c echo council-probe'))\n"
            "p = nx.Pipeline()\np.append(ep(), {})\n"
            "attempt('in_a_pipeline', lambda: p.execute(ds))\n"
            "from pathlib import Path\n"
            "Path('mine.txt').write_text('a user may write files')\n")
    proc, refusals, done = _guard_run(v, tmp_path, code, python=NXPY,
                                      trust=nx_policy.USER)
    got = _outcomes(proc.stdout)
    assert got == {"by_list": "REFUSED", "by_instance": "REFUSED",
                   "in_a_pipeline": "REFUSED"}, proc.stdout + proc.stderr
    assert "council-probe" not in proc.stdout
    assert (v.work / "mine.txt").read_text(encoding="utf-8") \
        == "a user may write files"
    assert proc.returncode == nx_guard.EXIT_REFUSED


def _make_pipeline(path: Path, filters_code: str) -> None:
    """Write a .d3dpipeline with simplnx itself (plain NXPY, no guard)."""
    code = ("import simplnx as nx\np = nx.Pipeline()\n" + filters_code
            + f"p.to_file({str(path)!r})\n")
    proc = subprocess.run([NXPY, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    assert proc.returncode == 0 and path.is_file(), proc.stderr[-1500:]


SAFE_STEPS = ("p.append(nx.CreateDataArrayFilter(), {'output_array_path': "
              "nx.DataPath('A'), 'tuple_dimensions': [[3]], "
              "'component_count': 1, 'numeric_type_index': "
              "nx.NumericType.float32})\n"
              "p.append(nx.WriteDREAM3DFilter(), {'export_file_path': "
              "'baked.dream3d', 'write_xdmf_file': False})\n")
TUTORIAL = ("import simplnx as nx\n"
            "pipeline = nx.Pipeline.from_file({pipe!r})\n"
            "args = pipeline[1].get_args()\n"
            "args['export_file_path'] = {out!r}\n"
            "pipeline[1].set_args(args)\n"
            "result = pipeline.execute(nx.DataStructure())\n"
            "assert not result.errors, result.errors\n")


@needs_nx
def test_a_users_tutorial_style_saved_pipeline_script_runs(tmp_path):
    """0822b06 refused ANY script naming Pipeline — the simplnx tutorials'
    own Pipeline.from_file script included. A USER script may run a saved
    pipeline whose filters pass the policy."""
    pipe = tmp_path / "pipelines" / "safe.d3dpipeline"
    pipe.parent.mkdir()
    _make_pipeline(pipe, SAFE_STEPS)
    out = tmp_path / "result.dream3d"
    script = tmp_path / "pipelines" / "tutorial.py"
    script.write_text(TUTORIAL.format(pipe=str(pipe), out=str(out)),
                      encoding="utf-8")
    res = wr.run_linear([script])
    assert res.success, res.summary()
    assert out.is_file()
    # The same text from a model is refused before it runs.
    stamped = tmp_path / "pipelines" / "model.py"
    stamped.write_text(nx_policy.stamp_model_script(
        script.read_text(encoding="utf-8"), "a test"), encoding="utf-8")
    res = wr.run_linear([stamped])
    assert not res.success
    assert "refused, nothing was run" in res.step_results[0].error
    assert "Pipeline" in res.step_results[0].error


@needs_nx
def test_a_saved_pipeline_holding_execute_process_is_refused(tmp_path):
    """Checked twice: the file a literal path names, before anything runs;
    and every step when Pipeline.execute runs, whatever the path was."""
    pipe = tmp_path / "evil.d3dpipeline"
    _make_pipeline(pipe, "p.append(nx.ExecuteProcessFilter(), "
                         "{'arguments': 'cmd /c echo council-probe'})\n")
    literal = tmp_path / "literal.py"
    literal.write_text(TUTORIAL.replace("args = pipeline[1]", "#")
                       .replace("pipeline[1].set_args(args)", "")
                       .replace("args['export_file_path'] = {out!r}", "")
                       .format(pipe=str(pipe)), encoding="utf-8")
    res = wr.run_linear([literal])
    assert not res.success
    err = res.step_results[0].error
    assert "refused, nothing was run" in err and "Execute Process" in err
    # A path the text check cannot read: nx_guard refuses the step.
    computed = tmp_path / "computed.py"
    computed.write_text(
        "import simplnx as nx\nfrom pathlib import Path\n"
        f"name = 'ev' + 'il.d3dpipeline'\n"
        f"p = nx.Pipeline.from_file(str(Path({str(tmp_path)!r}) / name))\n"
        "p.execute(nx.DataStructure())\n", encoding="utf-8")
    res = wr.run_linear([computed])
    assert not res.success
    err = res.step_results[0].error
    assert "refused at run time" in err and "Execute Process" in err
    assert "council-probe" not in res.step_results[0].stdout


def test_the_user_rules_check_a_literal_pipeline_file(tmp_path):
    good = tmp_path / "good.d3dpipeline"
    good.write_text(json.dumps({"name": "g", "pipeline": [
        {"filter": {"name": "x", "uuid": "67041f9b-bdc6-4122-acc6-"
                                          "c9fe9280e90d"}, "args": {}}]}),
        encoding="utf-8")
    bad = tmp_path / "bad.d3dpipeline"
    bad.write_text(json.dumps({"name": "b", "pipeline": [
        {"filter": {"name": "x", "uuid": EXECUTE_PROCESS}, "args": {}}]}),
        encoding="utf-8")
    junk = tmp_path / "junk.d3dpipeline"
    junk.write_text("not json", encoding="utf-8")
    run = "import simplnx as nx\nnx.Pipeline.from_file('{}').execute(None)\n"
    U = nx_policy.USER
    assert nx_policy.run_reasons(run.format("good.d3dpipeline"), trust=U,
                                 search_dirs=[tmp_path]) == []
    assert nx_policy.run_reasons(run.format("bad.d3dpipeline"), trust=U,
                                 search_dirs=[tmp_path])
    assert nx_policy.run_reasons(run.format("junk.d3dpipeline"), trust=U,
                                 search_dirs=[tmp_path])
    # A user script may still not use get_filters or name the filter.
    assert nx_policy.run_reasons("import simplnx as nx\nnx.get_filters()\n",
                                 trust=U)
    assert nx_policy.run_reasons("import simplnx as nx\n"
                                 "nx.ExecuteProcessFilter\n", trust=U)
    # ...and its own imports stay its own.
    assert nx_policy.run_reasons("import os, shutil\nos.getcwd()\n",
                                 trust=U) == []


# ============================================================
# 4. The workflow runner: which rules, and a refusal fails the step
# ============================================================

def test_who_wrote_a_script(tmp_path):
    stamped = nx_policy.stamp_model_script("x = 1\n", "nx_generate")
    assert nx_policy.script_trust(stamped) == nx_policy.MODEL
    assert nx_policy.script_trust("# my notes\n" + stamped) == nx_policy.MODEL
    assert nx_policy.script_trust("x = 1\n") == nx_policy.USER
    edited = nx_policy.stamp_model_script("x = 1\n", "chat", edited=True)
    assert nx_policy.script_trust(edited) == nx_policy.MODEL
    assert nx_policy.stamp_model_script(stamped, "again") == stamped
    # Unstamped, but in the app's own output area: the app wrote it.
    out = tmp_path / "data_out"
    assert nx_policy.script_trust("x = 1\n", out / "dream3d" / "t.py",
                                  [out]) == nx_policy.MODEL
    assert nx_policy.script_trust("x = 1\n", tmp_path / "in" / "t.py",
                                  [out]) == nx_policy.USER


def test_a_script_in_data_out_gets_the_model_rules(tmp_path, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("nothing should have been launched")
    monkeypatch.setattr(wr.subprocess, "run", never)
    vault = tmp_path / "vault"
    folder = vault / "data_out" / "dream3d"
    folder.mkdir(parents=True)
    p = folder / "task_x.py"
    p.write_text("import os\nos.remove('x')\n", encoding="utf-8")
    res = wr.run_linear([p], vault_dir=vault)
    assert not res.success
    assert "refused, nothing was run" in res.step_results[0].error
    # The same script of the user's own: their call. It gets as far as the
    # launch (the stub above), where the model's copy was refused.
    mine = tmp_path / "mine.py"
    mine.write_text("import os\nos.remove('x')\n", encoding="utf-8")
    res = wr.run_linear([mine], vault_dir=vault)
    assert "nothing should have been launched" in res.step_results[0].error


@needs_nx
def test_the_runner_fails_a_model_script_that_writes_to_the_vault_root(
        tmp_path):
    vault = tmp_path / "vault"
    (vault / "data_out").mkdir(parents=True)
    (vault / "notes.txt").write_text("mine", encoding="utf-8")
    script = tmp_path / "pipelines" / "task.py"
    script.parent.mkdir()
    target = vault / "notes.txt"
    script.write_text(nx_policy.stamp_model_script(
        "import simplnx as nx\nds = nx.DataStructure()\n"
        "r = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
        f"export_file_path={str(target)!r})\n", "a test"), encoding="utf-8")
    assert nx_policy.validate_script(script.read_text(encoding="utf-8"))[0]
    res = wr.run_linear([script], vault_dir=vault)
    assert not res.success
    err = res.step_results[0].error
    assert "refused at run time" in err and "notes.txt" in err
    assert target.read_text(encoding="utf-8") == "mine"
    # Nothing written; the run's empty working folders were dropped.
    assert not [p for p in (vault / "data_out").rglob("*") if p.is_file()]


@needs_nx
def test_a_model_written_chain_runs_inside_data_out(tmp_path):
    """The containment does not get in a real model script's way: read each
    input, write the hand-off and the result, all under data_out."""
    vault = tmp_path / "vault"
    inp = vault / "data_in" / "set"
    inp.mkdir(parents=True)
    (vault / "data_out").mkdir()
    # Two real .dream3d inputs, made by simplnx.
    for n in ("a", "b"):
        proc = subprocess.run(
            [NXPY, "-c", "import simplnx as nx\nds = nx.DataStructure()\n"
             "nx.CreateDataArrayFilter.execute(data_structure=ds, "
             "output_array_path=nx.DataPath('A'), tuple_dimensions=[[3]])\n"
             "r = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
             f"export_file_path={str(inp / (n + '.dream3d'))!r})\n"
             "assert not r.errors, r.errors\n"],
            capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr[-1500:]
    step = nx_policy.stamp_model_script(
        "import simplnx as nx\nds = nx.DataStructure()\n"
        "r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
        "import_data_object=nx.Dream3dImportParameter.ImportData("
        "file_path='C:/baked/in.dream3d'))\n"
        "assert not r0.errors, r0.errors\n"
        "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
        "export_file_path='Data/out.dream3d')\n"
        "assert not r1.errors, r1.errors\n", "a test")
    p1, p2 = tmp_path / "P1.py", tmp_path / "P2.py"
    p1.write_text(step, encoding="utf-8")
    p2.write_text(step, encoding="utf-8")
    spec = wr.WorkflowSpec([p1, p2], mode="chained", input_dir=inp,
                           pattern="*.dream3d", vault_dir=vault)
    res = wr.run_workflow(spec)
    assert res.success, res.summary()
    assert sorted(o.name for o in res.outputs) == ["a.dream3d", "b.dream3d"]
    assert all(path_contain.is_under(o, vault / "data_out")
               for o in res.outputs)


def test_an_output_folder_outside_data_out_is_refused(tmp_path, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("nothing should have been run")
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", never)
    vault = tmp_path / "vault"
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "a.dream3d").write_text("d", encoding="utf-8")
    p = tmp_path / "p.py"
    p.write_text("x = 1\n", encoding="utf-8")
    for mode in ("chained", "per_file", "per_step"):
        spec = wr.WorkflowSpec([p], mode=mode, input_dir=inp,
                               pattern="*.dream3d", output_dir=vault,
                               vault_dir=vault)
        res = wr.run_workflow(spec)
        assert not res.success and "outside the vault output area" \
            in res.error, (mode, res.error)


def _fake_runner(ran):
    def fake(staged, timeout_s=600, cwd=None, contain=None):
        src = staged.read_text(encoding="utf-8")
        import ast
        tree = ast.parse(src)
        vals = {k.arg: k.value.value for k in ast.walk(tree)
                if isinstance(k, ast.keyword)
                and isinstance(k.value, ast.Constant)}
        ran.append((staged.name, vals, contain))
        out = vals.get("export_file_path")
        if out and Path(out).is_absolute():
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(f"from {vals.get('file_path')}",
                                 encoding="utf-8")
        return wr.StepResult(-1, staged.name, "", True, 0, 0.0, "", "",
                             pipeline_path=staged)
    return fake


_STEP = ("import simplnx as nx\nds = nx.DataStructure()\n"
         "r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
         "import_data_object=nx.Dream3dImportParameter.ImportData("
         "file_path='baked.dream3d'))\n"
         "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
         "export_file_path='Data/o.dream3d')\n")


def test_the_runner_never_picks_an_output_name_that_exists(tmp_path,
                                                          monkeypatch):
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", _fake_runner(ran))
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "a.dream3d").write_text("d", encoding="utf-8")
    p = tmp_path / "P.py"
    p.write_text(_STEP, encoding="utf-8")
    out = tmp_path / "out"
    (out / "P").mkdir(parents=True)
    (out / "P" / "a.dream3d").write_text("an earlier result", encoding="utf-8")
    res = wr.run_per_file([p], inp, output_dir=out)
    assert res.success, res.summary()
    assert (out / "P" / "a.dream3d").read_text(encoding="utf-8") \
        == "an earlier result"
    assert [o.name for o in res.outputs] == ["a_2.dream3d"]


def test_folder_chain_stages_same_named_inputs_apart(tmp_path, monkeypatch):
    """recursive=True over x/a.dream3d and y/a.dream3d staged both to
    step1/a.dream3d: the second overwrote the first, and P2 read the second
    input twice."""
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", _fake_runner(ran))
    inp = tmp_path / "in"
    for sub in ("x", "y"):
        (inp / sub).mkdir(parents=True)
        (inp / sub / "a.dream3d").write_text(sub, encoding="utf-8")
    p1, p2 = tmp_path / "P1.py", tmp_path / "P2.py"
    p1.write_text(_STEP, encoding="utf-8")
    p2.write_text(_STEP, encoding="utf-8")
    out = tmp_path / "out"
    for scope in ("folder", "per_file"):
        ran.clear()
        o = out / scope
        res = wr.run_chained([p1, p2], inp, scope=scope, recursive=True,
                             output_dir=o)
        assert res.success, res.summary()
        p1_reads = [v["file_path"] for n, v, _c in ran if n.endswith("P1.py")]
        p1_writes = [v["export_file_path"] for n, v, _c in ran
                     if n.endswith("P1.py")]
        p2_reads = [v["file_path"] for n, v, _c in ran if n.endswith("P2.py")]
        assert sorted(p1_reads) == sorted(str(inp / s / "a.dream3d")
                                          for s in "xy")
        assert len(set(p1_writes)) == 2, p1_writes        # staged apart
        assert sorted(p2_reads) == sorted(p1_writes)       # each read once
        # The outputs say which input they came from.
        assert sorted(x.name for x in res.outputs) == ["x_a.dream3d",
                                                       "y_a.dream3d"]
        finals = {x.name: x.read_text(encoding="utf-8") for x in res.outputs}
        assert len(set(finals.values())) == 2


def test_each_step_is_handed_its_containment(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", _fake_runner(ran))
    vault = tmp_path / "vault"
    inp = vault / "data_in" / "set"
    inp.mkdir(parents=True)
    (inp / "a.dream3d").write_text("d", encoding="utf-8")
    p = tmp_path / "P.py"
    p.write_text(nx_policy.stamp_model_script(_STEP, "t"), encoding="utf-8")
    res = wr.run_workflow(wr.WorkflowSpec([p], mode="per_file", input_dir=inp,
                                          pattern="*.dream3d",
                                          vault_dir=vault))
    assert res.success, res.summary()
    c = ran[0][2]
    assert c.trust == nx_policy.MODEL
    assert path_contain.is_under(c.write_roots[0], vault / "data_out") \
        and path_contain.is_under(vault / "data_out", c.write_roots[0])
    assert any(path_contain.is_under(inp, r) for r in c.read_only)
    assert all(path_contain.is_under(o, vault / "data_out")
               for o in res.outputs)


# ============================================================
# 5. The Tk "Write pipeline" saves MAX_PATH-safe, stamped
# ============================================================

def test_the_tk_writer_uses_the_max_path_safe_name_and_the_stamp(
        tmp_path, monkeypatch):
    import council_gui_engine as cge
    import data_index
    import nx_generate
    vault = tmp_path / ("v" * 40) / ("w" * 40) / ("x" * 20) / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_out").mkdir()
    task = ("Read the DREAM3D file, compute the feature centroids and sizes, "
            "then write out.dream3d")
    from council_core import nx_ops
    assert len(str(vault / "data_out" / "dream3d"
                   / f"task_{nx_ops.script_stem(task)}.py")) > 250

    class Sync:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(cge.threading, "Thread", Sync)
    monkeypatch.setattr(nx_generate, "write_script",
                        lambda *a, **k: {"ok": True, "code": "x = 1\n",
                                         "attempts": 1, "errors": []})

    class Var:
        def __init__(self, v=""):
            self.v = v

        def set(self, x):
            self.v = x

        def get(self):
            return self.v

    f = type("F", (), {})()
    f.data_index = data_index.DataIndex(search_roots=[vault / "data_in"],
                                        write_root=vault / "data_out")
    f.shown = []
    f._nx_show = f.shown.append
    f._nx_status_var = Var()
    f._nx_task_var = Var(task)
    f.after = lambda ms, fn: fn()
    f._nx_catalog = lambda: {"filters": [{"uuid": "u"}]}
    cge.CouncilConsole._nx_write_script.__get__(f)()
    saved = list((vault / "data_out" / "dream3d").glob("task_*.py"))
    assert len(saved) == 1, f.shown
    assert len(str(saved[0])) <= 250
    assert saved[0].name == nx_ops.script_name_in(
        task, vault / "data_out" / "dream3d")
    text = saved[0].read_text(encoding="utf-8")
    assert nx_policy.script_trust(text) == nx_policy.MODEL
    assert text.endswith("\nx = 1\n")
    assert f._nx_status_var.get().startswith("nx: written")
