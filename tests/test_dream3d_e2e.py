"""
DREAM3D-NX end to end: the transpiler, the chained workflow runner and the
capability policy, run against the REAL nx env when it is installed.

Everything marked `needs_nx` uses the nxpython conda env (found the way the app
finds it, nx_bridge.find_python) and the example pipelines the dream3dnx
package ships under share/simplnx/pipelines, and SKIPS when either is missing.
The rest is pure and always runs. No test needs a model.

The xfail(strict=True) tests are KNOWN GAPS, measured 2026-10-06 on branch
dream3d/e2e. Each asserts the behaviour the code should have, so it starts
passing (and strict turns that into a failure to look at) the day the gap is
fixed.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

import nx_bridge
import nx_generate
import nx_policy
import nx_transpile
import workflow_runner as wr

HELPERS = Path(__file__).resolve().parent / "data" / "dream3d_e2e"
NXPY = nx_bridge.find_python()


def _shipped_root():
    if not NXPY:
        return None
    env = Path(NXPY).parent
    for cand in (env / "Library" / "share" / "simplnx" / "pipelines",
                 env.parent / "share" / "simplnx" / "pipelines"):
        if cand.is_dir():
            return cand
    return None


SHIPPED = _shipped_root()
needs_nx = pytest.mark.skipif(
    SHIPPED is None,
    reason="needs the DREAM3D-NX conda env (nxpython) and its shipped example "
           "pipelines")

EXECUTE_PROCESS = "fb511a70-2175-4595-8c11-d1b5b6794221"
P03 = "OrientationAnalysis/Small_IN100_Processing/(03) Small IN100 Morphological Statistics.d3dpipeline"
P04 = "OrientationAnalysis/Small_IN100_Processing/(04) Small IN100 Crystallographic Statistics.d3dpipeline"


@pytest.fixture(scope="module")
def catalog():
    if SHIPPED is None:
        pytest.skip("needs the DREAM3D-NX env")
    return nx_bridge.catalog()


def _nx(args, cwd=None, timeout=300):
    """Run one helper in the nx env; its JSON (if any) from stdout."""
    proc = subprocess.run([NXPY, *map(str, args)], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    assert proc.returncode == 0, proc.stderr[-1500:]
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1]) if lines else None


@pytest.fixture(scope="module")
def seeds(tmp_path_factory):
    """Two synthetic SmallIN100-shaped volumes (see nx_seed.py)."""
    if SHIPPED is None:
        pytest.skip("needs the DREAM3D-NX env")
    d = tmp_path_factory.mktemp("in")
    for name, seed in (("a", 7), ("b", 11)):
        _nx([HELPERS / "nx_seed.py", d / f"{name}.dream3d", seed])
    return d


def _transpiled(catalog, rel, dest: Path) -> Path:
    res = nx_transpile.transpile(SHIPPED / rel, catalog)
    assert not res["unknown"], res["unknown"]
    dest.write_text(res["code"], encoding="utf-8")
    return dest


# ============================================================
# Pure: the chain runner's parameter names (step 5)
# ============================================================

@pytest.mark.parametrize("param", [
    "file_path",            # ReadDREAM3DFilter's ImportData
    "input_file",           # 10 readers (ang, ctf, raw binary, text, vtk, ...)
    "input_file_path",
    "stl_file_path",        # ReadStlFileFilter
    "input_header_file",    # ReadBinaryCTNorthstarFilter
    "vg_header_file",       # ReadVolumeGraphicsFileFilter
])
def test_chain_finds_every_real_single_file_reader_param(param):
    src = f"r0 = nx.SomeReader.execute(\n    data_structure=ds,\n    {param}='x',\n)\n"
    assert wr._first_param_present(src, wr._INPUT_PARAM_CANDIDATES) == param


def test_chain_never_takes_an_image_writers_file_name_as_its_input():
    # ITKImageWriterFilter WRITES to file_name: treating it as an input would
    # point the writer at the user's input file.
    src = ("r0 = nxitk.ITKImageWriterFilter.execute(\n    data_structure=ds,\n"
           "    file_name='out.png',\n)\n")
    assert wr._first_param_present(src, wr._INPUT_PARAM_CANDIDATES) is None


def test_chain_stages_an_stl_readers_path(tmp_path):
    p = tmp_path / "p.py"
    p.write_text("r0 = nx.ReadStlFileFilter.execute(\n    data_structure=ds,\n"
                 "    stl_file_path='Data/STL_Models/baked.stl',\n)\n"
                 "r1 = nx.WriteDREAM3DFilter.execute(\n    data_structure=ds,\n"
                 "    export_file_path='Data/out.dream3d',\n)\n", encoding="utf-8")
    stage = tmp_path / "st"
    stage.mkdir()
    staged, out_used = wr._stage_chain_pipeline(
        p, stage, input_value=tmp_path / "mine.stl",
        output_value=tmp_path / "o.dream3d")
    txt = staged.read_text(encoding="utf-8")
    assert "baked.stl" not in txt and repr(str(tmp_path / "mine.stl")) in txt
    assert out_used == "export_file_path"


def test_chain_refuses_a_reader_it_cannot_point_at_the_input(tmp_path,
                                                            monkeypatch):
    """An unrecognised reader parameter used to be staged with its baked-in
    path and run: a 'successful' chain over the wrong file. Now it stops
    before anything runs."""
    def never(*_a, **_k):
        raise AssertionError("nothing should have been run")
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", never)
    p = tmp_path / "p.py"
    p.write_text("r0 = nx.SomeNewReaderFilter.execute(\n    data_structure=ds,\n"
                 "    some_new_path='Data/baked.dat',\n)\n"
                 "r1 = nx.WriteDREAM3DFilter.execute(\n    data_structure=ds,\n"
                 "    export_file_path='Data/out.dream3d',\n)\n", encoding="utf-8")
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "a.dat").write_text("x", encoding="utf-8")
    res = wr.run_chained([p, p], inp, pattern="*.dat",
                         stage_dir=tmp_path / "st")
    assert not res.success
    assert "input-path parameter" in res.error and "a.dat" in res.error


# ============================================================
# Pure: known gaps in the generators (offline, tiny catalogs)
# ============================================================

def _mini_catalog():
    return {"filters": [{
        "module": "simplnx", "alias": "nx", "py_attr": "CreateDataArrayFilter",
        "uuid": "67041f9b-bdc6-4122-acc6-c9fe9280e90d",
        "human_name": "Create Data Array", "default_tags": ["create", "array"],
        "execute": {"params": [
            {"name": "data_structure", "type": "simplnx.DataStructure"},
            {"name": "numeric_type_index", "type": "simplnx.NumericType",
             "default": "<NumericType.float32: 8>", "required": False},
            {"name": "output_array_path", "type": "simplnx.DataPath",
             "default": "DataPath('Data')", "required": False}]}},
        {"module": "simplnx", "alias": "nx", "py_attr": "ArrayCalculatorFilter",
         "uuid": "eea49b17-0db2-5bbc-80ef-f44249cc8d55",
         "human_name": "Attribute Array Calculator", "default_tags": [],
         "execute": {"params": [
             {"name": "data_structure", "type": "simplnx.DataStructure"},
             {"name": "calculator_parameter",
              "type": "simplnx.CalculatorParameter.ValueType",
              "default": "<simplnx.CalculatorParameter.ValueType object>",
              "required": False}]}}],
        "enums": {}}


def test_write_script_rejects_a_filter_the_catalog_does_not_have():
    code = ("import simplnx as nx\nds = nx.DataStructure()\n"
            "r = nx.TotallyMadeUpFilter.execute(data_structure=ds, bogus='x')\n")
    res = nx_generate.write_script("create an array", _mini_catalog(),
                                   lambda _p: code, max_attempts=1)
    assert res["ok"] is False


def test_validate_rejects_a_value_of_the_wrong_type():
    pipe = {"pipeline": [{
        "filter": {"uuid": "67041f9b-bdc6-4122-acc6-c9fe9280e90d"},
        "args": {"numeric_type_index": "banana", "output_array_path": 12345}}]}
    assert nx_generate.validate(pipe, _mini_catalog())


def test_transpile_flags_or_types_a_compound_value():
    pipe = {"pipeline": [{
        "filter": {"uuid": "eea49b17-0db2-5bbc-80ef-f44249cc8d55"},
        "args": {"calculator_parameter": {"value": {
            "equation": "a+a", "selected_group": "", "units": 0}, "version": 1}}}]}
    res = nx_transpile.transpile(pipe, _mini_catalog())
    assert "CalculatorParameter.ValueType(" in res["code"] or res["warnings"]


# ============================================================
# Real env: transpile (step 3)
# ============================================================

@needs_nx
def test_every_shipped_pipeline_transpiles_to_valid_python(catalog):
    shipped = sorted(SHIPPED.rglob("*.d3dpipeline"))
    assert len(shipped) >= 60
    for p in shipped:
        res = nx_transpile.transpile(p, catalog)
        assert not res["unknown"], (p.name, res["unknown"])
        ast.parse(res["code"])
        assert "parameters_version" not in res["code"]
        assert "'version':" not in res["code"], p.name   # envelopes unwrapped


@needs_nx
def test_transpiled_pipeline_computes_what_simplnx_computes(catalog, seeds,
                                                            tmp_path):
    script = _transpiled(catalog, P03, tmp_path / "p03.py")
    rep = _nx([HELPERS / "nx_equiv.py", SHIPPED / P03, script,
               seeds / "a.dream3d", tmp_path / "w"])
    assert rep["native_errors"] == []
    assert rep["reader_literal_found"] and rep["writer_literal_found"]
    assert rep["script_error"] is None and rep["script_wrote"]
    assert rep["hierarchy_identical"]
    assert rep["arrays_different"] == [] and rep["arrays_identical"] >= 15


# ============================================================
# Real env: chained workflow (step 5)
# ============================================================

@pytest.fixture
def chain(catalog, seeds, tmp_path, monkeypatch):
    """P1 = transpiled (03), P2 = transpiled (04) with its final writer pointed
    at final.dream3d; inputs a/b.dream3d; scripts run with the nx interpreter.

    The interpreter is patched because workflow_runner runs every pipeline
    with sys.executable -- the app's own env, which has no simplnx (see the
    xfail below)."""
    p1 = _transpiled(catalog, P03, tmp_path / "P1.py")
    p2 = _transpiled(catalog, P04, tmp_path / "P2.py")
    src = p2.read_text(encoding="utf-8")
    baked = "'Data/Output/Statistics/SmallIN100_CrystalStats.dream3d'"
    assert baked in src
    p2.write_text(src.replace(baked, repr(str(tmp_path / "final.dream3d"))),
                  encoding="utf-8")
    inp = tmp_path / "in"
    shutil.copytree(seeds, inp)
    monkeypatch.setattr(wr.sys, "executable", NXPY)
    monkeypatch.chdir(tmp_path)
    return p1, p2, inp, tmp_path


@needs_nx
@pytest.mark.parametrize("scope", ["per_file", "folder"])
def test_chain_feeds_p1s_staged_output_to_p2(chain, scope):
    p1, p2, inp, tmp = chain
    stage = tmp / f"st_{scope}"
    res = wr.run_chained([p1, p2], inp, scope=scope, pattern="*.dream3d",
                         stage_dir=stage, timeout_s=300)
    assert res.success, res.summary()
    assert res.steps_run == 4
    staged_out = {str(p) for p in stage.rglob("*.dream3d")}
    assert len(staged_out) == 2                   # one P1 output per input
    p2_reads = set()
    for f in stage.rglob("*P2.py"):
        lit = f.read_text(encoding="utf-8").split("file_path=", 1)[1].split(",")[0]
        p2_reads.add(ast.literal_eval(lit))
    assert p2_reads == staged_out                 # P2 read exactly P1's outputs
    rep = _nx([HELPERS / "nx_probe.py", tmp / "final.dream3d"])
    rec = rep[str(tmp / "final.dream3d")]
    assert rec["errors"] == []
    assert all(rec["has"].values()), rec["has"]   # (03)'s AND (04)'s arrays


@needs_nx
def test_chain_reads_each_stl_input_not_the_baked_path(catalog, tmp_path,
                                                       monkeypatch):
    """CreateScanVectors reads stl_file_path. Before it was a candidate, both
    runs below read the SAME baked file and the chain reported success."""
    def box(path, sx, sy, sz):
        v = [(x, y, z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
        quads = [((-1, 0, 0), (0, 1, 3, 2)), ((1, 0, 0), (4, 6, 7, 5)),
                 ((0, -1, 0), (0, 4, 5, 1)), ((0, 1, 0), (2, 3, 7, 6)),
                 ((0, 0, -1), (0, 2, 6, 4)), ((0, 0, 1), (1, 5, 7, 3))]
        tris = [(n, t) for n, (a, b, c, d) in quads
                for t in ((a, b, c), (a, c, d))]
        with open(path, "wb") as f:              # simplnx rejects ASCII STL
            f.write(b"box".ljust(80, b" ") + struct.pack("<I", len(tris)))
            for n, idx in tris:
                f.write(struct.pack("<3f", *map(float, n)))
                for i in idx:
                    f.write(struct.pack("<3f", *map(float, v[i])))
                f.write(struct.pack("<H", 0))

    inp = tmp_path / "in"
    inp.mkdir()
    box(inp / "x.stl", 2, 2, 2)
    box(inp / "y.stl", 6, 3, 4)
    # The dangerous case: the path baked into the pipeline EXISTS.
    (tmp_path / "Data" / "STL_Models").mkdir(parents=True)
    shutil.copy(inp / "x.stl",
                tmp_path / "Data" / "STL_Models" / "Example_Triangle_Geometry.stl")
    p1 = _transpiled(catalog, "SimplnxCore/CreateScanVectors.d3dpipeline",
                     tmp_path / "Q1.py")
    uuid = {f["py_attr"]: f["uuid"] for f in catalog["filters"]}
    passthru = nx_transpile.transpile({"pipeline": [
        {"filter": {"uuid": uuid["ReadDREAM3DFilter"]},
         "args": {"import_data_object": {"value": {
             "data_paths": [], "file_path": "in.dream3d",
             "path_import_policy": 0}, "version": 2}}},
        {"filter": {"uuid": uuid["WriteDREAM3DFilter"]},
         "args": {"export_file_path": {"value": str(tmp_path / "q.dream3d"),
                                       "version": 1},
                  "write_xdmf_file": {"value": False, "version": 1}}}]}, catalog)
    p2 = tmp_path / "Q2.py"
    p2.write_text(passthru["code"], encoding="utf-8")
    monkeypatch.setattr(wr.sys, "executable", NXPY)
    monkeypatch.chdir(tmp_path)
    stage = tmp_path / "st"
    res = wr.run_chained([p1, p2], inp, scope="per_file", pattern="*.stl",
                         stage_dir=stage, timeout_s=300)
    assert res.success, res.summary()
    outs = sorted(stage.rglob("*_step1.dream3d"))
    rep = _nx([HELPERS / "nx_probe.py", *outs])
    extents = {Path(f).stem: rec["tri_max"] for f, rec in rep.items()}
    assert extents == {"x_step1": [2.0, 2.0, 2.0], "y_step1": [6.0, 3.0, 4.0]}


@needs_nx
@pytest.mark.xfail(importlib.util.find_spec("simplnx") is None, strict=True,
                   reason=(
    "KNOWN GAP: workflow_runner runs every pipeline with sys.executable. The "
    "app's env has no simplnx (it lives in the separate nx env by design), so "
    "every linear/per-file/per-step/chained run of a simplnx script dies with "
    "ModuleNotFoundError: No module named 'simplnx'."))
def test_chain_runs_simplnx_scripts_with_an_interpreter_that_has_simplnx(
        catalog, seeds, tmp_path, monkeypatch):
    p1 = _transpiled(catalog, P03, tmp_path / "P1.py")
    monkeypatch.chdir(tmp_path)
    res = wr.run_chained([p1], seeds, scope="per_file", pattern="a.dream3d",
                         stage_dir=tmp_path / "st", timeout_s=300)
    assert res.success, res.summary()


# ============================================================
# Real env: the worker (run_folder / describe / preflight)
# ============================================================

@needs_nx
def test_worker_loads_a_pipeline_that_uses_plugin_filters():
    d = nx_bridge.describe_pipeline(SHIPPED / P03)
    assert d["size"] == 10


# ============================================================
# Real env: the capability policy at both ends (step 6)
# ============================================================

@needs_nx
def test_policy_generation_end_against_the_real_catalog(catalog):
    assert EXECUTE_PROCESS in {f["uuid"] for f in catalog["filters"]}
    for q in ("execute process", "execute an external program"):
        assert EXECUTE_PROCESS not in {
            e["uuid"] for e in nx_generate.retrieve(catalog, q, k=12)}
    errs = nx_generate.validate(
        {"pipeline": [{"filter": {"uuid": EXECUTE_PROCESS}, "args": {}}]}, catalog)
    assert errs and "not permitted" in errs[0]
    t = nx_transpile.transpile(SHIPPED / "SimplnxCore/ExecuteProcess.d3dpipeline",
                               catalog)
    live = [ln for ln in t["code"].splitlines()
            if "ExecuteProcess" in ln and not ln.lstrip().startswith("#")]
    assert live == [] and any("not permitted" in w for w in t["warnings"])
    ok, _why = nx_policy.validate_script(
        "import simplnx as nx\n"
        "nx.ExecuteProcessFilter.execute(data_structure=nx.DataStructure())\n")
    assert not ok


@needs_nx
def test_policy_run_end_refuses_before_anything_executes(seeds, tmp_path):
    vault = tmp_path / "vault"
    out = vault / "data_out" / "dream3d" / "runs"
    ep = SHIPPED / "SimplnxCore/ExecuteProcess.d3dpipeline"
    d = nx_bridge.describe_pipeline(ep)
    assert [x["uuid"] for x in d["denied"]] == [EXECUTE_PROCESS]
    res = nx_bridge.run_folder(ep, seeds, out, glob="*.dream3d",
                               vault_dir=vault)
    assert res["total"] == 2 and res["ok"] == 0
    assert all("is refused" in r["errors"][0] for r in res["runs"])
    with pytest.raises(nx_bridge.NxError, match="is refused"):
        nx_bridge.run_job({"action": "preflight", "pipeline": str(ep)})
    assert not list(tmp_path.rglob("ExecuteProcessOutput.txt"))
