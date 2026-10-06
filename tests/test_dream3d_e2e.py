"""
DREAM3D-NX end to end: the script checker, the transpiler, the chained
workflow runner and the capability policy, against the REAL installed build.

Two kinds of test:

  * Offline, against REAL_CATALOG — the installed build's catalog, saved by
    tests/data/dream3d_e2e/make_catalog_fixture.py (its "fixture_note" says
    which dream3dnx it came from). These always run: no nx env, no model.
  * `needs_nx`: the nxpython conda env (found the way the app finds it,
    nx_bridge.find_python) and the example pipelines the dream3dnx package
    ships under share/simplnx/pipelines. They SKIP when either is missing.
    One of them checks the saved catalog still IS the installed one.

History: on 2026-10-06 five of these were strict xfails, each a gap measured
on branch dream3d/e2e — write_script accepted made-up filters, validate()
checked keys but not values, compound values were emitted as dicts, the
workflow runner ran simplnx scripts without simplnx, and the worker could not
load a pipeline using a plugin filter. They are fixed on dream3d/e2e-fixes and
are plain tests now.
"""
from __future__ import annotations

import ast
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import nx_bridge
import nx_generate
import nx_ground
import nx_policy
import nx_transpile
import pipeline_editor
import workflow_runner as wr

HELPERS = Path(__file__).resolve().parent / "data" / "dream3d_e2e"
REAL_CATALOG = json.loads((HELPERS / "nx_catalog.json").read_text(
    encoding="utf-8"))
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
CREATE_DATA_ARRAY = "67041f9b-bdc6-4122-acc6-c9fe9280e90d"
P03 = "OrientationAnalysis/Small_IN100_Processing/(03) Small IN100 Morphological Statistics.d3dpipeline"
P04 = "OrientationAnalysis/Small_IN100_Processing/(04) Small IN100 Crystallographic Statistics.d3dpipeline"

# A script that uses every kind of value the checker types — DataPath, enum,
# list[list[float]], a compound built by setting properties, a threshold set
# holding a subclass, a positional-only constructor, a pathlib path, a plugin
# filter — and that really runs (test_a_script_the_checker_accepts_runs).
GOOD_SCRIPT = '''\
import simplnx as nx
import orientationanalysis as nxor
import numpy as np
from pathlib import Path

ds = nx.DataStructure()
out = nx.DataPath("Values")
r0 = nx.CreateDataArrayFilter.execute(data_structure=ds,
    numeric_type_index=nx.NumericType.float32, output_array_path=out,
    tuple_dimensions=[[5]], component_count=1)
assert not r0.errors, r0.errors
view = ds[nx.DataPath("Values")].npview()
view[:] = np.loadtxt("in.csv", delimiter=",").reshape(view.shape)
v = nx.ReadCSVDataParameter()
v.input_file_path = str(Path("t.csv").resolve())
v.column_data_types = [nx.CSVType.float32, nx.CSVType.float32]
v.header_mode = nx.ReadCSVDataParameter.HeaderMode.Line
v.headers_line = 1
v.start_import_row = 2
v.tuple_dims = [2]
v.delimiters = [","]
v.skipped_array_mask = [False, False]
r1 = nx.ReadCSVFileFilter.execute(data_structure=ds, read_csv_data_object=v,
    created_data_group_path=nx.DataPath("CSV"))
assert not r1.errors, r1.errors
t = nx.ArrayThreshold()
t.array_path = nx.DataPath("CSV/a")
t.comparison = nx.ArrayThreshold.ComparisonType.GreaterThan
t.value = 1.5
ts = nx.ArrayThresholdSet()
ts.thresholds = [t]
r2 = nx.MultiThresholdObjectsFilter.execute(data_structure=ds,
    array_thresholds_object=ts, created_mask_type=nx.DataType.uint8,
    output_data_array_name="Mask")
assert not r2.errors, r2.errors
c = nx.CalculatorParameter.ValueType(nx.DataPath("CSV"), "a+b",
    nx.CalculatorParameter.AngleUnits.Radians)
r3 = nx.ArrayCalculatorFilter.execute(data_structure=ds, calculator_parameter=c,
    calculated_array_path=nx.DataPath("CSV/sum"),
    scalar_type_index=nx.NumericType.float64)
assert not r3.errors, r3.errors
r4 = nx.WriteDREAM3DFilter.execute(data_structure=ds,
    export_file_path=Path("o.dream3d"), write_xdmf_file=False)
assert not r4.errors, r4.errors
print("wrote", Path("o.dream3d").exists(), nxor.ReadAngDataFilter.human_name())
'''


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


def _uuid(attr: str) -> str:
    return next(f["uuid"] for f in REAL_CATALOG["filters"]
                if f["py_attr"] == attr)


# ============================================================
# Offline: the script checker against the REAL catalog (step 4)
# ============================================================

@pytest.mark.parametrize("code, expect", [
    # Each is a failure class llama3.1:8b produced through "Write pipeline"
    # (12 of 12 accepted, 0 of 12 ran), plus the value casts the binding
    # refuses (each measured against the installed build).
    ("ds = nx.DataStructure()\n",
     "add `import simplnx as nx`"),
    ("from simplnx import nx\nds = nx.DataStructure()\n",
     "write `import simplnx as nx`"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "f = nx.CreateDataArrayFilter()\nf.execute(ds)\n",
     "builds a filter object"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "ds.add_filter(nx.CreateDataArrayFilter)\n",
     "has no attribute 'add_filter'"),
    ("import simplnx as nx\nds = nx.DataStructure()\ng = nx.ImageGeometry(ds)\n",
     "Nearest: ImageGeom"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, "
     "numeric_type=nx.NumericType.int32)\n",
     "Did you mean numeric_type_index"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, "
     "output_array_path='Values')\n",
     "wrap the string: nx.DataPath"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, numeric_type_index=8)\n",
     "refuses a plain int"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, component_count=1.0)\n",
     "is an int: write an int"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, tuple_dimensions=[5.0])\n",
     "element 0"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.CreateDataArrayFilter.execute(data_structure=ds, "
     "numeric_type_index=nx.NumericType.float)\n",
     "has no member 'float'"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.ReadCSVFileFilter.execute(data_structure=ds, "
     "read_csv_data_object={'input_file_path': 'a.csv'})\n",
     "a dict is not converted"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.ReadAngDataFilter.execute(data_structure=ds)\n",
     "it is in orientationanalysis"),
    ("import simplnx as nx\nnx.CreateDataArrayFilter.execute(component_count=1)\n",
     "missing data_structure=ds"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "r = nx.CreateDataArrayFilter.execute(data_structure=ds)\nr.valid()\n",
     "has no attribute 'valid'"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "r = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
     "import_data_object=nx.Dream3dImportParameter.ImportData("
     "file_path='a.dream3d', data_paths=['A']))\n",
     "element 0"),
    ("import simplnx as nx\nv = nx.ReadCSVDataParameter()\nv.input_file = 'a'\n",
     "has no attribute 'input_file'"),
    ("import simplnx as nx\nc = nx.CalculatorParameter.ValueType()\n",
     "does not take those arguments"),
    # Three more that an accepted llama3.1:8b script died on at run time
    # (re-run of 2026-10-06), each a way of losing the DataStructure.
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "ds[nx.DataPath('Grid')] = nx.CreateImageGeometryFilter.execute("
     "data_structure=ds)\n",
     "has no item assignment"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "ds = nx.CreateImageGeometryFilter.execute(data_structure=ds)\n"
     "nx.WriteDREAM3DFilter.execute(data_structure=ds)\n",
     "replaces the DataStructure with the filter's result"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "nx.InitializeImageGeomCellDataFilter.execute(data_structure=ds, "
     "input_image_geometry_path=ds[nx.DataPath('Grid')])\n",
     "the object stored at the path"),
    # ...and two from a second re-run: an attribute of an execute() result
    # held in a name reused for every filter, and an invented DataObject
    # method on something read out of ds.
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "result = nx.CreateDataArrayFilter.execute(data_structure=ds)\n"
     "result = nx.WriteDREAM3DFilter.execute(data_structure=ds)\n"
     "x = result.data_structure\n",
     "has no attribute 'data_structure'"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "geom = ds[nx.DataPath('Grid')]\ngeom.add_array('Counts')\n",
     "no simplnx DataObject has an attribute 'add_array'"),
    ("import simplnx as nx\nds = nx.DataStructure()\n"
     "ds[nx.DataPath('Grid')].add_array('Counts')\n",
     "no simplnx DataObject has an attribute 'add_array'"),
])
def test_check_script_catches_what_a_local_model_got_wrong(code, expect):
    errs = nx_ground.check_script(code, REAL_CATALOG)["errors"]
    assert any(expect in e for e in errs), errs


def test_check_script_accepts_a_correct_script():
    """No false positives on real usage of every value kind it types."""
    assert nx_ground.check_script(GOOD_SCRIPT, REAL_CATALOG)["errors"] == []
    assert nx_policy.validate_script(GOOD_SCRIPT)[0]


_PRE = "import simplnx as nx\nds = nx.DataStructure()\n"
_G = "nx.CreateImageGeometryFilter"
_BOGUS = "has no parameter 'bogus'"


@pytest.mark.parametrize("code, expect", [
    # A keyword set passed with **. The checker used to skip every **
    # keyword and switch the required-parameter check off; each of these was
    # accepted and raised TypeError / "Unable to cast" in simplnx.
    (_PRE + "params = {'dims': [2, 2, 2], 'output_image_geometry_path': 'Geom'}\n"
     f"r = {_G}.execute(data_structure=ds, **params)\n",
     "has no parameter 'dims'. Did you mean dimensions?"),
    (_PRE + "params = {'dimensions': [2, 2, 2], 'output_image_geometry_path': 'Geom'}\n"
     f"r = {_G}.execute(data_structure=ds, **params)\n",
     "wrap the string: nx.DataPath"),
    (_PRE + f"r = {_G}.execute(data_structure=ds, **{{'bogus': 1}})\n", _BOGUS),
    (_PRE + f"r = {_G}.execute(data_structure=ds, **{{'dimensions': 'abc'}})\n",
     "dimensions is a list[int]"),
    (_PRE + f"kw = dict(bogus=1)\nr = {_G}.execute(data_structure=ds, **kw)\n",
     _BOGUS),
    (_PRE + f"r = {_G}.execute(**{{'dimensions': [2, 2, 2]}})\n",
     "missing data_structure=ds"),
    # ...and one it cannot read is reported, not passed.
    (_PRE + f"kw = {{}}\nkw['bogus'] = 1\nr = {_G}.execute(data_structure=ds, **kw)\n",
     "cannot be checked"),
    (_PRE + f"def make():\n    return {{}}\n"
     f"r = {_G}.execute(data_structure=ds, **make())\n", "cannot be checked"),
    (_PRE + f"args = [ds]\nr = {_G}.execute(*args)\n", "cannot be checked"),
    # The filter held any way but one plain assignment got no execute()
    # check at all, so not even the keyword NAMES were checked.
    (_PRE + f"F = nx.CreateDataArrayFilter\nF = {_G}\n"
     "r = F.execute(data_structure=ds, bogus=1)\n", _BOGUS),
    (_PRE + f"F, H = {_G}, nx.CreateDataArrayFilter\n"
     "r = F.execute(data_structure=ds, bogus=1)\n", _BOGUS),
    (_PRE + f"F: object = {_G}\nr = F.execute(data_structure=ds, bogus=1)\n",
     _BOGUS),
    (_PRE + f"if (F := {_G}):\n    r = F.execute(data_structure=ds, bogus=1)\n",
     _BOGUS),
    (_PRE + f"fs = [{_G}]\nr = fs[0].execute(data_structure=ds, bogus=1)\n",
     _BOGUS),
    (_PRE + f"fs = {{'g': {_G}}}\nr = fs['g'].execute(data_structure=ds, bogus=1)\n",
     _BOGUS),
    (_PRE + f"r = ({_G} if True else nx.CreateDataArrayFilter).execute("
     "data_structure=ds, bogus=1)\n", _BOGUS),
    (_PRE + f"for F in ({_G},):\n    r = F.execute(data_structure=ds, bogus=1)\n",
     _BOGUS),
    (_PRE + f"fs = [{_G}]\nfor F in fs:\n"
     "    r = F.execute(data_structure=ds, bogus=1)\n", _BOGUS),
    (_PRE + f"for name, F in [('g', {_G})]:\n"
     "    r = F.execute(data_structure=ds, bogus=1)\n", _BOGUS),
    (_PRE + f"rs = [F.execute(data_structure=ds, bogus=1) for F in ({_G},)]\n",
     _BOGUS),
    (_PRE + "def run(f, **k):\n    return f.execute(data_structure=ds, **k)\n"
     f"r = run({_G}, bogus=1)\n", _BOGUS),
    (_PRE + "def run(f):\n    return f.execute(data_structure=ds, bogus=1)\n"
     f"r = run({_G})\n", _BOGUS),
    # One helper, two call sites: each checked with its own filter.
    (_PRE + "def run(f, **k):\n    return f.execute(data_structure=ds, **k)\n"
     "run(nx.CreateDataArrayFilter, component_count=1)\n"
     f"run({_G}, component_count=1)\n",
     "CreateImageGeometryFilter.execute() has no parameter 'component_count'"),
    # A filter it cannot identify is an error, not a pass.
    (_PRE + "def pick():\n    return nx.CreateDataArrayFilter\n"
     "r = pick().execute(data_structure=ds)\n", "cannot identify"),
    (_PRE + "def run(f):\n    return f.execute(data_structure=ds)\n"
     f"for r in map(run, [{_G}]):\n    pass\n", "cannot identify"),
    # execute2 / preflight2 belong to a filter OBJECT: on the class they
    # fail with "incompatible function arguments" whatever they are given.
    (_PRE + f"r = {_G}.execute2(data_structure=ds, dimensions=[2, 2, 2])\n",
     "execute2 is not how a script runs a filter"),
    (_PRE + f"F = {_G}\nF = nx.CreateDataArrayFilter\nr = F.preflight2(ds)\n",
     "preflight2 is not how a script runs a filter"),
])
def test_check_script_follows_every_way_a_script_holds_a_filter(code, expect):
    errs = nx_ground.check_script(code, REAL_CATALOG)["errors"]
    assert any(expect in e for e in errs), errs


# Every indirect form the checker now follows, written correctly: no false
# positive, and it really runs (test_an_indirect_script_the_checker_accepts_runs).
GOOD_INDIRECT_SCRIPT = '''\
import simplnx as nx

ds = nx.DataStructure()

# A keyword set held in a dict, and the filter behind an alias.
geom = {"dimensions": [4, 3, 2], "origin": [0.0, 0.0, 0.0],
        "spacing": [1.0, 1.0, 1.0],
        "output_image_geometry_path": nx.DataPath("Geom")}
Make = nx.CreateImageGeometryFilter
r = Make.execute(data_structure=ds, **geom)
assert not r.errors, r.errors


def run(f, **params):
    result = f.execute(data_structure=ds, **params)
    assert not result.errors, result.errors
    return result


# Each step: a filter and its keywords, run through the helper.
steps = [
    (nx.CreateDataArrayFilter, dict(numeric_type_index=nx.NumericType.float32,
                                    output_array_path=nx.DataPath("A"),
                                    tuple_dimensions=[[5]], component_count=1)),
    (nx.CreateDataArrayFilter, {"numeric_type_index": nx.NumericType.int32,
                                "output_array_path": nx.DataPath("B"),
                                "tuple_dimensions": [[3]],
                                "component_count": 2}),
]
for f, kw in steps:
    run(f, **kw)
run(nx.CreateDataArrayFilter, numeric_type_index=nx.NumericType.uint8,
    output_array_path=nx.DataPath("C"), tuple_dimensions=[[2]],
    component_count=1)

# One name rebound from filter to filter, each call right for the one it
# holds at that point.
F = nx.CreateDataArrayFilter
r = F.execute(data_structure=ds, numeric_type_index=nx.NumericType.int8,
              output_array_path=nx.DataPath("D"), tuple_dimensions=[[1]],
              component_count=1)
assert not r.errors, r.errors
F = nx.CreateImageGeometryFilter
r = F.execute(data_structure=ds, dimensions=[2, 2, 2],
              output_image_geometry_path=nx.DataPath("Geom2"))
assert not r.errors, r.errors
for name in ("A", "B", "C", "D", "Geom2"):
    assert ds.exists(nx.DataPath(name)), name
print("indirect ok")
'''


def test_check_script_accepts_correct_indirect_calls():
    assert nx_ground.check_script(GOOD_INDIRECT_SCRIPT,
                                  REAL_CATALOG)["errors"] == []
    assert nx_policy.validate_script(GOOD_INDIRECT_SCRIPT)[0]


@pytest.mark.parametrize("code", [
    # The two write_script accepted on the first attempt (ok=True).
    "import simplnx as nx\nds = nx.DataStructure()\n"
    "params = {'dims': [2, 2, 2], 'output_image_geometry_path': 'Geom'}\n"
    "result = nx.CreateImageGeometryFilter.execute(data_structure=ds, **params)\n",
    "import simplnx as nx\nds = nx.DataStructure()\n"
    "for f in [nx.CreateImageGeometryFilter]:\n"
    "    result = f.execute(data_structure=ds, dims=[2, 2, 2], "
    "output_image_geometry_path='Geom')\n",
])
def test_write_script_refuses_a_script_that_hides_its_filter_or_keywords(code):
    res = nx_generate.write_script("create a 2x2x2 image geometry",
                                   REAL_CATALOG, lambda _p: code,
                                   max_attempts=1, n_ctx=8192)
    assert res["ok"] is False
    assert any("'dims'" in e for e in res["errors"]), res["errors"]


def test_write_script_rejects_a_filter_the_catalog_does_not_have():
    code = ("import simplnx as nx\nds = nx.DataStructure()\n"
            "r = nx.TotallyMadeUpFilter.execute(data_structure=ds, bogus='x')\n")
    res = nx_generate.write_script("create a data array", REAL_CATALOG,
                                   lambda _p: code, max_attempts=1)
    assert res["ok"] is False
    assert any("TotallyMadeUpFilter does not exist" in e
               for e in res["errors"]), res["errors"]


def test_write_script_repairs_with_exact_names_and_real_signatures():
    """The repair round carries the exact unknown names, the nearest real
    ones and the real signature of the filter the model reached for — then
    a corrected script is accepted."""
    bad = ("import simplnx as nx\nds = nx.DataStructure()\n"
           "r = nx.CreateDataArrayFilter.execute(data_structure=ds, "
           "numeric_type=nx.NumericType.float32, output_array_path='Values')\n")
    good = ("import simplnx as nx\nds = nx.DataStructure()\n"
            "r = nx.CreateDataArrayFilter.execute(data_structure=ds, "
            "numeric_type_index=nx.NumericType.float32, "
            "output_array_path=nx.DataPath('Values'))\n"
            "assert not r.errors, r.errors\n")
    prompts = []

    def model(p):
        prompts.append(p)
        return bad if len(prompts) == 1 else good
    res = nx_generate.write_script("add a float32 array named Values",
                                   REAL_CATALOG, model)
    assert res["ok"] and res["attempts"] == 2, res["errors"]
    repair = prompts[1]
    assert "'numeric_type'" in repair and "numeric_type_index" in repair
    assert "wrap the string: nx.DataPath" in repair
    assert "YOUR SCRIPT" in repair and "output_array_path='Values'" in repair
    assert "numeric_type_index: simplnx.NumericType" in repair


def test_write_script_shows_real_signatures_of_an_invented_filters_neighbours():
    bad = ("import simplnx as nx\nds = nx.DataStructure()\n"
           "nx.CreateDataArayFilter.execute(data_structure=ds)\n")
    prompts = []
    nx_generate.write_script("compute feature sizes", REAL_CATALOG,
                             lambda p: prompts.append(p) or bad,
                             max_attempts=2)
    assert "Nearest real filters: nx.CreateDataArrayFilter" in prompts[1]
    assert "REAL FILTERS THE ERRORS POINT AT" in prompts[1]
    assert "class: nx.CreateDataArrayFilter" in prompts[1]


def test_script_prompt_states_the_imports_and_the_call_form():
    cands = nx_generate.retrieve(
        REAL_CATALOG, "read an ang file, then multi threshold objects")
    p = nx_generate.build_script_prompt("x", cands, REAL_CATALOG)
    assert "    import simplnx as nx" in p
    assert "    import orientationanalysis as nxor" in p
    assert "Never create a filter" in p
    assert "a simplnx.ArrayThresholdSet -> a nx.ArrayThresholdSet object" in p
    assert "a simplnx.ArrayThreshold -> " in p          # what goes inside it


def test_retrieve_pins_the_reader_and_writer_the_request_names():
    """ReadDREAM3DFilter ranked 13th and WriteDREAM3DFilter 16th for this
    request, so k=12 dropped both and the model invented a reader."""
    q = ("Read the DREAM3D file C:/x/a.dream3d. It has an image geometry "
         "DataContainer with Cell Data/FeatureIds and a Cell Feature Data "
         "attribute matrix. Compute the feature centroids and feature sizes, "
         "then write the result to out.dream3d in the current folder.")
    got = [e["py_attr"] for e in nx_generate.retrieve(REAL_CATALOG, q, k=12)]
    assert got[:2] == ["ReadDREAM3DFilter", "WriteDREAM3DFilter"], got
    got = [e["py_attr"] for e in nx_generate.retrieve(
        REAL_CATALOG, "read every .dream3d and write an STL", k=12)]
    assert got[:2] == ["ReadDREAM3DFilter", "WriteStlFileFilter"], got
    for q in ("execute a process", "run an external program then write it"):
        assert EXECUTE_PROCESS not in {
            e["uuid"] for e in nx_generate.retrieve(REAL_CATALOG, q)}


def test_a_long_vault_path_does_not_lose_a_validated_script(tmp_path,
                                                           monkeypatch):
    """task_<40-char stem>.py under a deep vault passed 260 characters on a
    PC with LongPathsEnabled=0; the write raised and a script that had
    passed every check was reported 'nx: failed' and lost."""
    from council_core import nx_ops
    vault = tmp_path / ("v" * 40) / ("w" * 40) / ("x" * 20) / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    task = ("Read the DREAM3D file, compute the feature centroids and sizes, "
            "then write out.dream3d")
    folder = nx_ops.out_dir(vault) / nx_ops.SUBFOLDER
    assert len(str(folder / f"task_{nx_ops.script_stem(task)}.py")) > 250

    class Gen:
        def write_script(self, task, catalog, model_call, n_ctx=None):
            return {"ok": True, "code": "x = 1\n", "attempts": 1,
                    "errors": []}

    class Bridge:
        def catalog(self):
            return {"filters": [{"uuid": "u"}]}
    try:
        r = nx_ops.write_script(task, vault, generator=Gen(), bridge=Bridge())
        assert r.ok and r.path is not None and r.path.exists(), r.body
        assert len(str(r.path)) <= 250
        # And when saving fails anyway, the script is shown, not lost.
        monkeypatch.setattr(nx_ops, "safe_out_path",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError("path too long")))
        r = nx_ops.write_script(task, vault, generator=Gen(), bridge=Bridge())
        assert r.ok and r.path is None and "x = 1" in r.body
        assert "could not be saved" in r.body and "not saved" in r.status
    finally:
        nx_ops.invalidate_catalog(vault)


# ============================================================
# Offline: validate() values, the required-param check (honestly)
# ============================================================

def test_validate_rejects_a_value_of_the_wrong_type():
    pipe = {"pipeline": [{
        "filter": {"uuid": CREATE_DATA_ARRAY},
        "args": {"numeric_type_index": "banana", "output_array_path": 12345,
                 "component_count": {"value": 2.5, "version": 1}}}]}
    errs = nx_generate.validate(pipe, REAL_CATALOG)
    assert len(errs) == 3, errs
    assert any("numeric_type_index" in e and "banana" in e for e in errs)
    good = {"pipeline": [{
        "filter": {"uuid": CREATE_DATA_ARRAY},
        "args": {"numeric_type_index": 8, "output_array_path": "A/B",
                 "component_count": 2, "tuple_dimensions": [[10.0]]}}]}
    assert nx_generate.validate(good, REAL_CATALOG) == []


def test_required_is_only_data_structure_and_the_script_check_enforces_it():
    """Measured, not assumed: in the installed build every execute()
    parameter but data_structure has a default. So validate()'s required
    check (JSON never carries data_structure) cannot fire on this catalog —
    its docstring says so — and the one real requirement is enforced where
    it can be missed: a script calling execute() without data_structure."""
    req = {p["name"] for f in REAL_CATALOG["filters"]
           for p in f["execute"]["params"] if p["required"]}
    assert req == {"data_structure"}
    assert "cannot fire against the real catalog" in nx_generate.validate.__doc__
    errs = nx_ground.check_script(
        "import simplnx as nx\nnx.WriteDREAM3DFilter.execute("
        "export_file_path='o.dream3d')\n", REAL_CATALOG)["errors"]
    assert any("missing data_structure=ds" in e for e in errs)


# ============================================================
# Offline: compound values in the transpiler (step 3)
# ============================================================

def _one_step(attr, args):
    return {"pipeline": [{"filter": {"uuid": _uuid(attr)}, "args": {
        k: {"value": v, "version": 1} for k, v in args.items()}}]}


def test_transpile_builds_compound_values_the_binding_accepts():
    calc = nx_transpile.transpile(_one_step("ArrayCalculatorFilter", {
        "calculator_parameter": {"equation": "a+a", "selected_group": "",
                                 "units": 0}}), REAL_CATALOG)
    assert ("nx.CalculatorParameter.ValueType(nx.DataPath(''), 'a+a', "
            "nx.CalculatorParameter.AngleUnits.Radians)") in calc["code"]
    csv = nx_transpile.transpile(_one_step("ReadCSVFileFilter", {
        "read_csv_data_object": {
            "Consecutive Delimiters": False, "Custom Headers": None,
            "Data Types": [8, 9], "Delimiters": [","], "Header Line": 1,
            "Header Mode": 0, "Input File Path": "Data/x.csv",
            "Skipped Array Mask": [False, False], "Start Import Row": 2,
            "Tuple Dimensions": [3]}}), REAL_CATALOG)
    code = csv["code"]
    assert "v0_read_csv_data_object = nx.ReadCSVDataParameter()" in code
    assert ("v0_read_csv_data_object.column_data_types = "
            "[nx.CSVType.float32, nx.CSVType.float64]") in code
    assert "v0_read_csv_data_object.tuple_dims = [3]" in code
    assert "read_csv_data_object=v0_read_csv_data_object," in code
    thr = nx_transpile.transpile(_one_step("MultiThresholdObjectsFilter", {
        "array_thresholds_object": {"inverted": False, "type": "collection",
                                    "union": 0, "thresholds": [{
                                        "array_path": "A/B", "comparison": 0,
                                        "component_index": 0, "inverted": False,
                                        "type": "array", "union": 0,
                                        "value": 1.5}]}}), REAL_CATALOG)
    assert "= nx.ArrayThreshold()" in thr["code"]
    assert ".thresholds = [v0_array_thresholds_object_thresholds_0]" in \
        thr["code"]
    for res in (calc, csv, thr):
        assert res["warnings"] == [], res["warnings"]
        ast.parse(res["code"])
        assert "{'" not in res["code"]                  # no dict literal left
        assert nx_ground.check_script(res["code"], REAL_CATALOG)["errors"] == []


def test_transpile_warns_on_a_compound_it_cannot_build():
    res = nx_transpile.transpile(_one_step("ReadCSVFileFilter", {
        "read_csv_data_object": {"Input File Path": "x.csv",
                                 "Some Future Key": 1}}), REAL_CATALOG)
    assert any("Some Future Key" in w for w in res["warnings"])
    assert "# TODO: verify type" in res["code"]


# ============================================================
# Offline: the catalog cache is checked against python/version
# ============================================================

FP = {"python_exe": "C:/nx/python.exe",
      "packages": {"python": "3.12.13-h0", "dream3dnx": "26.03.23-py312_0"}}


def _cat(**kw):
    c = {"catalog_schema": 2, "python": "3.12.13 | conda-forge",
         "env": json.loads(json.dumps(FP)), "filters": [{"uuid": "u"}]}
    c.update(kw)
    return c


@pytest.mark.parametrize("cat, why", [
    (_cat(catalog_schema=1), "older version of this app"),
    ({"python": "3.9.0 (fake)", "filters": [{"uuid": "u"}]}, "schema 1"),
    (_cat(python="3.9.0 (default)"), "python 3.9.0"),
    (_cat(env={"python_exe": "C:/nx/python.exe", "packages": {
        "python": "3.12.13-h0", "dream3dnx": "25.01.01-py312_0"}}),
     "dream3dnx 25.01.01-py312_0 -> 26.03.23-py312_0"),
    (_cat(env=None), "does not record which nx env"),
    ({"filters": []}, "empty"),
])
def test_a_stale_catalog_is_recognised(cat, why):
    reason = nx_bridge.catalog_stale_reason(cat, fingerprint=FP)
    assert reason and why in reason, reason
    assert nx_bridge.catalog_stale_reason(_cat(), fingerprint=FP) is None


def test_the_saved_catalog_is_rebuilt_when_the_env_changed(tmp_path,
                                                           monkeypatch):
    from council_core import nx_ops
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))

    class Bridge:
        calls = 0

        def catalog(self):
            Bridge.calls += 1
            return _cat(filters=[{"uuid": "fresh"}])

        def catalog_stale_reason(self, cat):
            return nx_bridge.catalog_stale_reason(cat, fingerprint=FP)
    try:
        # The verification's stale file: python 3.9.0, one fake filter.
        path = nx_ops.safe_out_path(vault, nx_ops.CATALOG_FILE)
        path.write_text(json.dumps({"python": "3.9.0 (fake)",
                                    "filters": [{"uuid": "ghost"}]}),
                        encoding="utf-8")
        got = nx_ops.catalog(vault, bridge=Bridge())
        assert got["filters"][0]["uuid"] == "fresh" and Bridge.calls == 1
        assert "schema 1" in nx_ops.last_rebuild_reason[vault.resolve()]
        # Fresh and matching: served from memory, then from disk, no rebuild.
        assert nx_ops.catalog(vault, bridge=Bridge()) is got
        nx_ops._catalog_mem.clear()
        assert nx_ops.catalog(vault, bridge=Bridge())["filters"][0]["uuid"] \
            == "fresh" and Bridge.calls == 1
        # The env moves on (a dream3dnx upgrade): rebuilt without a button.
        FP2 = json.loads(json.dumps(FP))
        FP2["packages"]["dream3dnx"] = "26.09.01-py312_0"
        Bridge.catalog_stale_reason = (
            lambda self, cat: nx_bridge.catalog_stale_reason(
                cat, fingerprint=FP2))
        nx_ops.catalog(vault, bridge=Bridge())
        assert Bridge.calls == 2
    finally:
        nx_ops.invalidate_catalog(vault)


_NO_SIMPLNX = [{"module": m, "error": f"ModuleNotFoundError: No module named '{m}'"}
               for m in ("simplnx", "orientationanalysis", "itkimageprocessing")]


def test_a_catalog_built_without_simplnx_is_refused(monkeypatch):
    """COUNCIL_NX_PYTHON at an interpreter without simplnx: the worker
    returns 0 filters with every module in modules_missing, and that was
    saved over the good cache and served."""
    monkeypatch.setattr(nx_bridge, "run_job", lambda *a, **k: {
        "catalog_schema": 2, "python": "3.11.14 (main)", "filters": [],
        "modules_loaded": [], "modules_missing": _NO_SIMPLNX})
    with pytest.raises(nx_bridge.NxError, match="simplnx did not import"):
        nx_bridge.catalog()


def test_a_failed_rebuild_keeps_the_saved_catalog_and_says_why(tmp_path,
                                                               monkeypatch):
    from types import SimpleNamespace
    from council_core import nx_ops
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    bad = {"catalog_schema": 2, "python": "3.11.14 (main)", "filters": [],
           "modules_loaded": [], "modules_missing": _NO_SIMPLNX,
           "env": {"python_exe": "C:/council/python.exe", "packages": {}}}

    class Bridge:                     # what the real bridge returned
        def catalog(self):
            return json.loads(json.dumps(bad))

        def catalog_stale_reason(self, cat):
            return "it was built in python 3.12.13, and the nx env now has " \
                   "python 3.11.14"

        def transpile(self, *_a, **_k):
            raise AssertionError("must not transpile against no catalog")
    try:
        path = nx_ops.safe_out_path(vault, nx_ops.CATALOG_FILE)
        saved = json.dumps(_cat(filters=[{"uuid": "good"}]))
        path.write_text(saved, encoding="utf-8")
        with pytest.raises(nx_bridge.NxError, match="simplnx did not import"):
            nx_ops.catalog(vault, bridge=Bridge())
        assert path.read_text(encoding="utf-8") == saved     # not overwritten
        pl = SimpleNamespace(path=str(tmp_path / "p.d3dpipeline"),
                             name="p.d3dpipeline")
        r = nx_ops.transpile(pl, vault, bridge=Bridge())
        assert not r.ok and r.status == "nx: failed"
        assert "simplnx did not import" in r.body
        r = nx_ops.write_script("make a geometry", vault, bridge=Bridge(),
                                model_call=lambda _p: "")
        assert not r.ok and "simplnx did not import" in r.body
    finally:
        nx_ops.invalidate_catalog(vault)


@pytest.mark.parametrize("missing, stale", [
    # installed, but its import failed when the catalog was built
    ([{"module": "itkimageprocessing",
       "error": "ImportError: DLL load failed while importing "
                "itkimageprocessing"}], "itkimageprocessing"),
    ([{"module": "orientationanalysis",
       "error": "ModuleNotFoundError: No module named 'numpy'"}],
     "orientationanalysis"),
    # simply not installed (this build has no simplnxreview): still good
    ([{"module": "simplnxreview",
       "error": "ModuleNotFoundError: No module named 'simplnxreview'"}], None),
])
def test_a_catalog_built_while_a_plugin_failed_to_import_is_stale(missing,
                                                                  stale):
    """Staleness was the fingerprint alone: a catalog saved while one
    plugin failed to import was served for good (207 filters, and a real
    ITKImageReaderFilter call refused as not installed)."""
    reason = nx_bridge.catalog_stale_reason(_cat(modules_missing=missing),
                                            fingerprint=FP)
    if stale is None:
        assert reason is None
    else:
        assert reason and stale in reason, reason


def test_a_catalog_built_by_the_wrong_interpreter_is_refused_for_real(
        tmp_path, monkeypatch):
    """The review's case, with a real subprocess: COUNCIL_NX_PYTHON at the
    app's own interpreter (no simplnx). Transpile reported 'nx: transpiled'
    with every step commented out, and the saved catalog became empty."""
    from types import SimpleNamespace
    from council_core import nx_ops
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_NX_PYTHON", sys.executable)
    try:
        path = nx_ops.safe_out_path(vault, nx_ops.CATALOG_FILE)
        saved = json.dumps(_cat(filters=[{"uuid": "good"}]))
        path.write_text(saved, encoding="utf-8")
        src = tmp_path / "p.d3dpipeline"
        src.write_text(json.dumps({"pipeline": [
            {"filter": {"uuid": CREATE_DATA_ARRAY}, "args": {}}]}),
            encoding="utf-8")
        r = nx_ops.transpile(SimpleNamespace(path=str(src),
                                             name="p.d3dpipeline"), vault)
        assert not r.ok and r.status == "nx: failed", (r.status, r.body)
        assert "simplnx did not import" in r.body
        assert path.read_text(encoding="utf-8") == saved
    finally:
        nx_ops.invalidate_catalog(vault)


def test_env_fingerprint_reads_conda_meta_without_starting_python(tmp_path,
                                                                  monkeypatch):
    env = tmp_path / "envs" / "nxpython"
    (env / "conda-meta").mkdir(parents=True)
    (env / "python.exe").write_text("", encoding="utf-8")
    for rec in ("python-3.12.13-h0_cpython", "python-dateutil-2.9.0-pyh_2",
                "dream3dnx-26.03.23-py312_0", "numpy-2.5.1-py312_0"):
        (env / "conda-meta" / f"{rec}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COUNCIL_NX_PYTHON", str(env / "python.exe"))
    fp = nx_bridge.env_fingerprint()
    assert fp["packages"] == {"python": "3.12.13-h0_cpython",
                              "dream3dnx": "26.03.23-py312_0"}


# ============================================================
# Offline: the capability policy's routes and gates (step 6)
# ============================================================

@pytest.mark.parametrize("code", [
    # nx.get_filters() holds ExecuteProcessFilter (measured on the installed
    # build); none of these names the denied class.
    "import simplnx as nx\nnx.get_filters()[7].execute(data_structure=None)\n",
    "import simplnx as nx\np = nx.Pipeline.from_file('x.d3dpipeline')\n",
    "from simplnx import get_filters\n",
    "import simplnx as nx\nnx.load_python_plugin(nx)\n",
    "import simplnx as nx\nx = 'fb511a70-2175-4595-8c11-d1b5b6794221'\n",
])
def test_the_policy_refuses_routes_that_hide_which_filter_runs(code):
    assert not nx_policy.validate_script(code)[0]
    assert nx_policy.capability_reasons(code)


@pytest.mark.parametrize("code", [
    "import numpy as np\nnp.load('x.npy', allow_pickle=True)\n",
    "import numpy as np\nnp.ctypeslib.load_library('x', '.')\n",
    "import numpy.ctypeslib\n",
])
def test_the_script_gate_refuses_numpys_native_code_routes(code):
    assert not nx_policy.validate_script(code)[0]


def test_capability_check_leaves_a_users_own_imports_alone():
    """The run-end check is the capability rule only: a user's script may
    import os (their call), but may not reach Execute Process."""
    assert nx_policy.capability_reasons("import os\nprint(os.getcwd())\n") == []
    assert nx_policy.capability_reasons(
        "import simplnx as nx\nnx.ExecuteProcessFilter.execute()\n")
    assert nx_policy.capability_reasons("def (:\n")      # cannot vouch for it


def test_the_workflow_runner_refuses_a_denied_filter_before_running(
        tmp_path, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("nothing should have been launched")
    monkeypatch.setattr(wr.subprocess, "run", never)
    p = tmp_path / "evil.py"
    p.write_text("import simplnx as nx\nds = nx.DataStructure()\n"
                 "nx.ExecuteProcessFilter.execute(data_structure=ds, "
                 "arguments='cmd /c echo hi')\n", encoding="utf-8")
    res = wr.run_linear([p])
    assert not res.success
    assert "refused, nothing was run" in res.step_results[0].error


def test_a_simplnx_script_runs_with_the_nx_interpreter(monkeypatch):
    monkeypatch.setattr(nx_bridge, "find_python", lambda *a, **k: "C:/nx/py.exe")
    import importlib.util as ilu
    real = ilu.find_spec
    monkeypatch.setattr(ilu, "find_spec",
                        lambda n, *a, **k: None if n == "simplnx"
                        else real(n, *a, **k))
    assert wr.interpreter_for("import simplnx as nx\n") == ("C:/nx/py.exe",
                                                            None)
    assert wr.interpreter_for("from orientationanalysis import X\n")[0] == \
        "C:/nx/py.exe"
    assert wr.interpreter_for("import numpy\n") == (sys.executable, None)
    monkeypatch.setattr(nx_bridge, "find_python", lambda *a, **k: None)
    py, why = wr.interpreter_for("import simplnx\n")
    assert py is None and "DREAM3D-NX env was not found" in why


def test_pipeline_chat_create_is_grounded_and_gated(tmp_path):
    """The chat's 'create pipeline' was a second, ungated model-writes-Python
    path that saved a script naming ExecuteProcessFilter into pipelines/in.
    It now goes through write_script."""
    vault = tmp_path / "vault"
    evil = ("import simplnx as nx\nds = nx.DataStructure()\n"
            "nx.ExecuteProcessFilter.execute(data_structure=ds, arguments='x')\n")
    path, log = pipeline_editor.generate_pipeline_from_description(
        "create a data array, then run a process on it", vault,
        suggested_name="s6probe",
        model_call=lambda _p: evil, catalog=REAL_CATALOG)
    assert path is None and "refused" in log and "ExecuteProcessFilter" in log
    assert not list((vault / "pipelines" / "in").glob("*.py"))
    made_up = ("import simplnx as nx\nds = nx.DataStructure()\n"
               "nx.MakeArrayFilter.execute(data_structure=ds)\n")
    path, log = pipeline_editor.generate_pipeline_from_description(
        "create a data array", vault, model_call=lambda _p: made_up,
        catalog=REAL_CATALOG)
    assert path is None and "MakeArrayFilter does not exist" in log
    good = ("import simplnx as nx\nds = nx.DataStructure()\n"
            "r = nx.CreateDataArrayFilter.execute(data_structure=ds, "
            "output_array_path=nx.DataPath('A'))\nassert not r.errors\n")
    path, log = pipeline_editor.generate_pipeline_from_description(
        "create a data array", vault, suggested_name="ok",
        model_call=lambda _p: good, catalog=REAL_CATALOG)
    assert path is not None and path.read_text(encoding="utf-8") == good.strip()


def test_a_model_edit_may_not_add_a_denied_filter():
    before = "import simplnx as nx\nimport os\nds = nx.DataStructure()\n"
    after = before + "nx.ExecuteProcessFilter.execute(data_structure=ds)\n"
    added = pipeline_editor._new_policy_reasons(before, after)
    assert added and all("ExecuteProcessFilter" in r for r in added)
    # What the user's script already did (import os) is not the edit's doing.
    assert pipeline_editor._new_policy_reasons(before, before + "x = 1\n") == []


# ============================================================
# Offline: the workflow runner's parameter names and outputs (step 5)
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


def test_a_compound_property_path_is_staged_without_eating_the_next_lines(
        tmp_path):
    """A transpiled ReadCSV step holds its path in a property assignment;
    the value scan used to run on past the newline."""
    p = tmp_path / "p.py"
    p.write_text("v0 = nx.ReadCSVDataParameter()\n"
                 "v0.input_file_path = 'Data/baked.csv'\n"
                 "v0.tuple_dims = [3]\n"
                 "r0 = nx.ReadCSVFileFilter.execute(data_structure=ds, "
                 "read_csv_data_object=v0)\n", encoding="utf-8")
    staged, _ = wr._stage_chain_pipeline(p, tmp_path, dest_name="s.py",
                                         input_value=tmp_path / "x.csv")
    txt = staged.read_text(encoding="utf-8")
    assert f"v0.input_file_path = {str(tmp_path / 'x.csv')!r}\n" in txt
    assert "v0.tuple_dims = [3]\n" in txt
    ast.parse(txt)


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
                         stage_dir=tmp_path / "st", output_dir=tmp_path / "o")
    assert not res.success
    assert "input-path parameter" in res.error and "a.dat" in res.error


@pytest.mark.parametrize("run", [wr.run_per_file, wr.run_per_step])
def test_per_file_and_per_step_refuse_an_input_they_cannot_redirect(
        run, tmp_path, monkeypatch):
    """They copied such a script as-is and ran it: every input read the
    same baked file."""
    def never(*_a, **_k):
        raise AssertionError("nothing should have been run")
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", never)
    p = tmp_path / "p.py"
    p.write_text("r0 = nx.SomeNewReaderFilter.execute(data_structure=ds, "
                 "some_new_path='Data/baked.dat')\n", encoding="utf-8")
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "a.dat").write_text("x", encoding="utf-8")
    res = run([p], inp, pattern="*.dat", output_dir=tmp_path / "o")
    assert not res.success and "a.dat" in res.error


@pytest.mark.parametrize("mode", ["chained", "per_file", "per_step"])
def test_every_input_gets_its_own_output(mode, tmp_path, monkeypatch):
    """The final output used to keep the script's baked path (resolved
    against wherever the app was started): two inputs, one surviving file,
    outside the vault's output area."""
    ran = []

    def fake(staged, timeout_s=600, cwd=None):
        src = staged.read_text(encoding="utf-8")
        out = ast.literal_eval(src.split("export_file_path=", 1)[1]
                               .split(",")[0].split(")")[0])
        ran.append((staged.name, out, cwd))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("x", encoding="utf-8")
        return wr.StepResult(-1, staged.name, "", True, 0, 0.0, "", "",
                             pipeline_path=staged)
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", fake)
    script = ("import simplnx as nx\nds = nx.DataStructure()\n"
              "r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
              "import_data_object=nx.Dream3dImportParameter.ImportData("
              "file_path='baked_in.dream3d'))\n"
              "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
              "export_file_path='Data/Output/baked.stl')\n")
    p1, p2 = tmp_path / "P1.py", tmp_path / "P2.py"
    p1.write_text(script, encoding="utf-8")
    p2.write_text(script, encoding="utf-8")
    inp = tmp_path / "in"
    inp.mkdir()
    for n in ("a", "b"):
        (inp / f"{n}.dream3d").write_text("d", encoding="utf-8")
    out = tmp_path / "vault_out"
    spec = wr.WorkflowSpec([p1, p2], mode=mode, input_dir=inp,
                           pattern="*.dream3d", output_dir=out)
    res = wr.run_workflow(spec)
    assert res.success, res.summary()
    assert all("baked" not in o for _n, o, _c in ran)
    # Each run works in its own folder under output_dir, so a relative path
    # a script still holds cannot collide with another input's.
    assert all(c.parent in (out, out / "P1", out / "P2") and c.name in "ab"
               for _n, _o, c in ran), ran
    finals = sorted(o.relative_to(out).as_posix() for o in res.outputs)
    if mode == "chained":
        assert finals == ["a.stl", "b.stl"]           # the baked suffix kept
    else:
        assert finals == ["P1/a.stl", "P1/b.stl", "P2/a.stl", "P2/b.stl"]
    assert all(o.exists() for o in res.outputs)
    assert "outputs (" in res.summary()


def _writes(src):
    """Every export/feature/file path a staged script now holds, in order."""
    tree = ast.parse(src)
    return [(k.arg, k.value.value) for k in ast.walk(tree)
            if isinstance(k, ast.keyword) and isinstance(k.value, ast.Constant)
            and k.arg in ("export_file_path", "feature_data_file",
                          "file_path", "file_name")]


def _fake_runner(ran, write=True):
    """Stands in for simplnx: records each staged script and creates every
    file it writes (a relative one under the step's working folder)."""
    def fake(staged, timeout_s=600, cwd=None):
        src = staged.read_text(encoding="utf-8")
        ran.append((staged.name, _writes(src), cwd))
        for param, val in _writes(src):
            if param in ("file_path",) or not write:
                continue
            dest = Path(val) if Path(val).is_absolute() else Path(cwd) / val
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x", encoding="utf-8")
        return wr.StepResult(-1, staged.name, "", True, 0, 0.0, "", "",
                             pipeline_path=staged)
    return fake


_READ = ("r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
         "import_data_object=nx.Dream3dImportParameter.ImportData("
         "file_path='Data/baked_in.dream3d'))\n")


def _inputs(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    for n in ("a", "b"):
        (inp / f"{n}.dream3d").write_text("d", encoding="utf-8")
    return inp


def test_chain_hands_on_the_last_writer_and_keeps_every_other_per_input(
        tmp_path, monkeypatch):
    """P1 wrote a checkpoint, then its final file. Only the FIRST
    export_file_path was redirected, so P2 read the checkpoint (it lacked
    what P1 computes after it) and the chain reported success, while P1's
    real output went to its baked path, once for every input."""
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", _fake_runner(ran))
    p1 = tmp_path / "P1.py"
    p1.write_text("import simplnx as nx\nds = nx.DataStructure()\n" + _READ
                  + "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path='Data/Output/ckpt.dream3d')\n"
                    "r2 = nx.ComputeFeatureSizesFilter.execute(data_structure=ds)\n"
                    "r3 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path='Data/Output/final.dream3d')\n",
                  encoding="utf-8")
    p2 = tmp_path / "P2.py"
    p2.write_text("import simplnx as nx\nds = nx.DataStructure()\n" + _READ
                  + "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path='Data/Output/p2.dream3d')\n",
                  encoding="utf-8")
    out = tmp_path / "o"
    stage = tmp_path / "st"
    res = wr.run_chained([p1, p2], _inputs(tmp_path), scope="per_file",
                         stage_dir=stage, output_dir=out)
    assert res.success, res.summary()
    p1_runs = [w for n, w, _c in ran if n.endswith("P1.py")]
    p2_runs = [w for n, w, _c in ran if n.endswith("P2.py")]
    for p1w, p2w, stem in zip(p1_runs, p2_runs, "ab"):
        ckpt, final = [v for k, v in p1w if k == "export_file_path"]
        assert Path(final).name == f"{stem}_step1.dream3d"      # handed on
        assert Path(ckpt) == out / f"{stem}_ckpt.dream3d"        # kept, per input
        assert dict(p2w)["file_path"] == final                   # P2 reads it
    assert sorted(o.name for o in res.outputs) == [
        "a.dream3d", "a_ckpt.dream3d", "b.dream3d", "b_ckpt.dream3d"]
    assert not (out / "Data").exists()
    assert any("written 2 times" in n for n in res.notes), res.notes
    assert "written 2 times" in res.summary()


def test_a_parameter_named_in_a_comment_is_not_a_parameter(tmp_path):
    """The path rewrite ran a regex over the raw text: a comment naming
    file_path / export_file_path was rewritten instead of the real call."""
    p = tmp_path / "p.py"
    baked = str(tmp_path / "in" / "a.dream3d")
    p.write_text("# Settings: file_path = the .dream3d to read\n"
                 "# Output: export_file_path = Data/x.dream3d\n"
                 "import simplnx as nx\nds = nx.DataStructure()\n"
                 "r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
                 "import_data_object=nx.Dream3dImportParameter.ImportData("
                 f"file_path={baked!r}))\n"
                 "note = 'export_file_path = not this either'\n"
                 "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                 "export_file_path='Data/x.dream3d')\n", encoding="utf-8")
    staged, used = wr._stage_chain_pipeline(
        p, tmp_path, dest_name="s.py", input_value=tmp_path / "in" / "b.dream3d",
        output_value=tmp_path / "hand.dream3d")
    txt = staged.read_text(encoding="utf-8")
    assert txt.startswith("# Settings: file_path = the .dream3d to read\n"
                          "# Output: export_file_path = Data/x.dream3d\n")
    assert "note = 'export_file_path = not this either'" in txt
    w = dict(_writes(txt))
    assert w["file_path"] == str(tmp_path / "in" / "b.dream3d")
    assert w["export_file_path"] == str(tmp_path / "hand.dream3d")
    assert used == "export_file_path"
    ast.parse(txt)


@pytest.mark.parametrize("mode", ["chained", "per_file"])
def test_a_reader_named_only_in_a_comment_is_refused(mode, tmp_path,
                                                     monkeypatch):
    """The input guard looked for the path ANYWHERE in the staged text. A
    script whose only `file_path =` is in a comment passed it: every input
    read the same baked file and the run reported success."""
    def never(*_a, **_k):
        raise AssertionError("nothing should have been run")
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", never)
    p = tmp_path / "p.py"
    p.write_text("# file_path = the .dream3d to read\n"
                 "import simplnx as nx\nds = nx.DataStructure()\n"
                 "r0 = nx.ReadDREAM3DFilter.execute(data_structure=ds, "
                 "import_data_object=nx.Dream3dImportParameter.ImportData("
                 "'Data/baked.dream3d'))\n"
                 "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                 "export_file_path='Data/x.dream3d')\n", encoding="utf-8")
    spec = wr.WorkflowSpec([p, p] if mode == "chained" else [p], mode=mode,
                           input_dir=_inputs(tmp_path), pattern="*.dream3d",
                           output_dir=tmp_path / "o")
    res = wr.run_workflow(spec)
    assert not res.success
    assert "input-path parameter" in res.error and "a.dream3d" in res.error


def test_a_step_that_does_not_write_its_hand_off_fails_and_names_itself(
        tmp_path, monkeypatch):
    """P1 "succeeded" without writing the staged file; the runner went on
    and P2 failed with "Path ... does not exist" — the error named P2."""
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess",
                        _fake_runner(ran, write=False))
    p1, p2 = tmp_path / "P1.py", tmp_path / "P2.py"
    for p in (p1, p2):
        p.write_text("import simplnx as nx\nds = nx.DataStructure()\n" + _READ
                     + "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                       "export_file_path='Data/o.dream3d')\n", encoding="utf-8")
    res = wr.run_chained([p1, p2], _inputs(tmp_path), output_dir=tmp_path / "o")
    assert not res.success
    assert [n for n, _w, _c in ran] == ["01_P1.py"]          # P2 never ran
    bad = res.step_results[-1]
    assert bad.pipeline_name.endswith("P1.py") and "a_step1.dream3d" in bad.error
    assert "P1.py" in res.error


@pytest.mark.parametrize("mode", ["chained", "per_file", "per_step"])
def test_every_writer_gets_its_own_file_per_input(mode, tmp_path, monkeypatch):
    """Only the first output parameter was redirected: a second writer kept
    its baked path, so each input overwrote the previous one's file (one
    features.csv for two inputs) and it was not in result.outputs."""
    ran = []
    monkeypatch.setattr(wr, "_run_pipeline_subprocess", _fake_runner(ran))
    p1 = tmp_path / "P1.py"
    p1.write_text("import simplnx as nx\nds = nx.DataStructure()\n" + _READ
                  + "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path='Data/Output/p1.dream3d')\n",
                  encoding="utf-8")
    p2 = tmp_path / "P2.py"
    p2.write_text("import simplnx as nx\nimport itkimageprocessing as nxitk\n"
                  "ds = nx.DataStructure()\n" + _READ
                  + "r1 = nx.WriteDREAM3DFilter.execute(data_structure=ds, "
                    "export_file_path='Data/Output/p2.dream3d')\n"
                    "r2 = nx.WriteFeatureDataCSVFilter.execute(data_structure=ds, "
                    "feature_data_file='features.csv')\n"
                    # not an output-path name the runner knows: lands in the
                    # run's own working folder
                    "r3 = nxitk.ITKImageWriterFilter.execute(data_structure=ds, "
                    "file_name='slice.png')\n", encoding="utf-8")
    out = tmp_path / "o"
    spec = wr.WorkflowSpec([p1, p2], mode=mode, input_dir=_inputs(tmp_path),
                           pattern="*.dream3d", output_dir=out)
    res = wr.run_workflow(spec)
    assert res.success, res.summary()
    got = sorted(o.relative_to(out).as_posix() for o in res.outputs)
    if mode == "chained":
        assert got == ["a.dream3d", "a/slice.png", "a_features.csv",
                       "b.dream3d", "b/slice.png", "b_features.csv"]
    else:
        assert got == ["P1/a.dream3d", "P1/b.dream3d",
                       "P2/a.dream3d", "P2/a/slice.png", "P2/a_features.csv",
                       "P2/b.dream3d", "P2/b/slice.png", "P2/b_features.csv"]
    assert all(o.is_file() for o in res.outputs)
    assert not list(out.rglob("features.csv"))
    assert not (out / "Data").exists()


# ============================================================
# Real env: the saved catalog is the installed one
# ============================================================

@needs_nx
def test_the_saved_catalog_is_the_installed_catalog(catalog):
    def sig(cat):
        return {(f["module"], f["py_attr"], f["uuid"]): [
            (p["name"], p["type"], p["required"])
            for p in f["execute"]["params"]] for f in cat["filters"]}
    assert sig(REAL_CATALOG) == sig(catalog), (
        "the installed DREAM3D-NX changed: rerun "
        "tests/data/dream3d_e2e/make_catalog_fixture.py")
    assert REAL_CATALOG["classes"] == catalog["classes"]
    assert REAL_CATALOG["module_names"] == catalog["module_names"]
    assert nx_bridge.catalog_stale_reason(catalog) is None


@needs_nx
def test_a_script_the_checker_accepts_runs(tmp_path):
    (tmp_path / "in.csv").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    (tmp_path / "t.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (tmp_path / "good.py").write_text(GOOD_SCRIPT, encoding="utf-8")
    proc = subprocess.run([NXPY, "good.py"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "wrote True" in proc.stdout


@needs_nx
def test_an_indirect_script_the_checker_accepts_runs(tmp_path):
    (tmp_path / "ind.py").write_text(GOOD_INDIRECT_SCRIPT, encoding="utf-8")
    proc = subprocess.run([NXPY, "ind.py"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "indirect ok" in proc.stdout


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
        assert not [w for w in res["warnings"] if "could not type" in w], (
            p.name, res["warnings"])


@needs_nx
def test_the_checker_accepts_every_transpiled_shipped_pipeline():
    """No false positives: 67 real pipelines' worth of simplnx calls, every
    compound value included, pass both the script checker and the policy
    gate (which the workflow runner now applies at run time)."""
    for p in sorted(SHIPPED.rglob("*.d3dpipeline")):
        code = nx_transpile.transpile(p, REAL_CATALOG)["code"]
        assert nx_ground.check_script(code, REAL_CATALOG)["errors"] == [], \
            p.name
        assert nx_policy.validate_script(code)[0], p.name


@needs_nx
def test_transpiled_arguments_are_what_simplnx_itself_loads(catalog,
                                                            tmp_path):
    """Every argument of every shipped pipeline, compound ones included,
    compared with simplnx's own Pipeline.from_file — without running a
    filter, so without the data the package does not ship. 26 of these
    pipelines died on 'Unable to cast ... dict' before."""
    pairs = []
    for i, p in enumerate(sorted(SHIPPED.rglob("*.d3dpipeline"))):
        dest = tmp_path / f"p{i:02d}.py"
        dest.write_text(nx_transpile.transpile(p, catalog)["code"],
                        encoding="utf-8")
        pairs += [p, dest]
    proc = subprocess.run([NXPY, HELPERS / "nx_args_equiv.py", *pairs],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    assert proc.returncode == 0, proc.stderr[-1500:]
    reps = [json.loads(ln) for ln in proc.stdout.splitlines()
            if ln.startswith("{")]
    assert len(reps) == len(pairs) // 2
    assert [r for r in reps if r["error"] or r["different"]] == []
    assert sum(r["compared"] for r in reps) > 3000


@needs_nx
def test_a_transpiled_compound_pipeline_runs(catalog, tmp_path):
    """ArrayCalculatorExample needs no input data and carries a
    CalculatorParameter: it died with 'Unable to cast ... dict'."""
    script = _transpiled(catalog, "SimplnxCore/ArrayCalculatorExample.d3dpipeline",
                         tmp_path / "calc.py")
    proc = subprocess.run([NXPY, script], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]


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
    """P1 = transpiled (03), P2 = transpiled (04); inputs a/b.dream3d. The
    scripts run with whatever interpreter the runner picks (the nx env's —
    the app's has no simplnx)."""
    p1 = _transpiled(catalog, P03, tmp_path / "P1.py")
    p2 = _transpiled(catalog, P04, tmp_path / "P2.py")
    inp = tmp_path / "in"
    shutil.copytree(seeds, inp)
    monkeypatch.chdir(tmp_path)
    return p1, p2, inp, tmp_path


@needs_nx
@pytest.mark.parametrize("scope", ["per_file", "folder"])
def test_chain_feeds_p1s_staged_output_to_p2(chain, scope):
    p1, p2, inp, tmp = chain
    stage = tmp / f"st_{scope}"
    out = tmp / f"out_{scope}"
    res = wr.run_chained([p1, p2], inp, scope=scope, pattern="*.dream3d",
                         stage_dir=stage, output_dir=out, timeout_s=300)
    assert res.success, res.summary()
    assert res.steps_run == 4
    staged_out = {str(p) for p in stage.rglob("*.dream3d")}
    assert len(staged_out) == 2                   # one P1 output per input
    p2_reads = set()
    for f in stage.rglob("*P2.py"):
        lit = f.read_text(encoding="utf-8").split("file_path=", 1)[1].split(",")[0]
        p2_reads.add(ast.literal_eval(lit))
    assert p2_reads == staged_out                 # P2 read exactly P1's outputs
    # One final file PER INPUT, in output_dir — not one baked path that the
    # second input overwrote.
    assert sorted(o.name for o in res.outputs) == ["a.dream3d", "b.dream3d"]
    assert not (tmp / "Data").exists()
    rep = _nx([HELPERS / "nx_probe.py", *res.outputs])
    for o in res.outputs:
        rec = rep[str(o)]
        assert rec["errors"] == []
        assert all(rec["has"].values()), rec["has"]   # (03)'s AND (04)'s arrays


def _variant(catalog, rel, dest: Path, edit) -> Path:
    """A shipped pipeline changed by ``edit(pipeline_json)``, transpiled."""
    d = json.loads((SHIPPED / rel).read_text(encoding="utf-8"))
    edit(d)
    res = nx_transpile.transpile(d, catalog)
    assert not res["unknown"] and not res["warnings"], res
    dest.write_text(res["code"], encoding="utf-8")
    return dest


def _with_checkpoint(after: int):
    """(03) with a second WriteDREAM3D (a checkpoint) after step ``after``."""
    def edit(d):
        ck = json.loads(json.dumps(d["pipeline"][-1]))
        ck["args"]["export_file_path"]["value"] = \
            "Data/Output/Statistics/ckpt.dream3d"
        ck["args"]["write_xdmf_file"]["value"] = False
        d["pipeline"].insert(after + 1, ck)
    return edit


@needs_nx
def test_chain_hands_on_what_p1_ends_with(chain, catalog):
    """(03) with a checkpoint after step [6], chained into (04): P2 read the
    checkpoint, so the final files lacked Sphericity (computed by (03) after
    it) — reported 4/4 ok."""
    _p1, p2, inp, tmp = chain
    p1 = _variant(catalog, P03, tmp / "P1ck.py", _with_checkpoint(6))
    out = tmp / "out"
    res = wr.run_chained([p1, p2], inp, scope="per_file", pattern="*.dream3d",
                         stage_dir=tmp / "st", output_dir=out, timeout_s=300)
    assert res.success, res.summary()
    names = sorted(o.name for o in res.outputs)
    assert names == ["a.dream3d", "a_ckpt.dream3d", "b.dream3d",
                     "b_ckpt.dream3d"], names
    assert not (out / "Data").exists() and not (tmp / "Data").exists()
    finals = [out / "a.dream3d", out / "b.dream3d"]
    rep = _nx([HELPERS / "nx_probe.py", *finals])
    for f in finals:
        rec = rep[str(f)]
        assert rec["errors"] == [] and all(rec["has"].values()), rec


@needs_nx
def test_chain_reads_each_input_with_a_parameter_named_in_a_comment(
        chain, catalog):
    """A top comment `Settings: file_path = ...` above a reader with an
    absolute baked path (as model scripts write them): the comment was
    rewritten, every input read the baked file — identical Centroids for a
    and b, 4/4 ok."""
    p1, p2, inp, tmp = chain
    src = p1.read_text(encoding="utf-8").replace(
        "file_path='Data/Output/Reconstruction/SmallIN100_Final.dream3d'",
        f"file_path={str(inp / 'a.dream3d')!r}")
    p1.write_text("# Morphological statistics. Settings: file_path = the "
                  ".dream3d to read (edit below)\n" + src, encoding="utf-8")
    out = tmp / "out"
    res = wr.run_chained([p1, p2], inp, scope="per_file", pattern="*.dream3d",
                         stage_dir=tmp / "st", output_dir=out, timeout_s=300)
    assert res.success, res.summary()
    rep = _nx([HELPERS / "nx_probe.py", out / "a.dream3d", out / "b.dream3d"])
    shas = {Path(f).stem: r["centroids_sha"] for f, r in rep.items()}
    assert shas["a"] and shas["b"] and shas["a"] != shas["b"], shas


@needs_nx
def test_chain_writes_each_inputs_csv(chain, catalog):
    """(04) plus the shipped WriteFeatureDataCSV step: one features.csv in
    output_dir for two inputs (b's overwrote a's), not in result.outputs."""
    p1, _p2, inp, tmp = chain
    csv_uuid = _uuid("WriteFeatureDataCSVFilter")

    def add_csv(d):
        ap = json.loads((SHIPPED / "OrientationAnalysis/EBSD_File_Processing/"
                         "aptr12_Analysis.d3dpipeline").read_text(
                             encoding="utf-8"))
        node = next(n for n in ap["pipeline"]
                    if n["filter"]["uuid"] == csv_uuid)
        node["args"]["cell_feature_attribute_matrix_path"]["value"] = \
            "DataContainer/Cell Feature Data"
        node["args"]["feature_data_file"]["value"] = "features.csv"
        d["pipeline"].append(node)
    p2 = _variant(catalog, P04, tmp / "P2csv.py", add_csv)
    out = tmp / "out"
    res = wr.run_chained([p1, p2], inp, scope="per_file", pattern="*.dream3d",
                         stage_dir=tmp / "st", output_dir=out, timeout_s=300)
    assert res.success, res.summary()
    names = sorted(o.name for o in res.outputs)
    assert names == ["a.dream3d", "a_features.csv", "b.dream3d",
                     "b_features.csv"], names
    a = (out / "a_features.csv").read_text(encoding="utf-8")
    b = (out / "b_features.csv").read_text(encoding="utf-8")
    assert a and b and a != b
    assert not (out / "features.csv").exists()


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
         "args": {"export_file_path": {"value": "q.dream3d", "version": 1},
                  "write_xdmf_file": {"value": False, "version": 1}}}]}, catalog)
    p2 = tmp_path / "Q2.py"
    p2.write_text(passthru["code"], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    stage = tmp_path / "st"
    res = wr.run_chained([p1, p2], inp, scope="per_file", pattern="*.stl",
                         stage_dir=stage, output_dir=tmp_path / "o",
                         timeout_s=300)
    assert res.success, res.summary()
    outs = sorted(stage.rglob("*_step1.dream3d"))
    rep = _nx([HELPERS / "nx_probe.py", *outs])
    extents = {Path(f).stem: rec["tri_max"] for f, rec in rep.items()}
    assert extents == {"x_step1": [2.0, 2.0, 2.0], "y_step1": [6.0, 3.0, 4.0]}


@needs_nx
def test_chain_runs_simplnx_scripts_with_an_interpreter_that_has_simplnx(
        catalog, seeds, tmp_path, monkeypatch):
    """As shipped every simplnx workflow died with 'No module named simplnx':
    the runner used sys.executable, the app's env."""
    p1 = _transpiled(catalog, P03, tmp_path / "P1.py")
    monkeypatch.chdir(tmp_path)
    res = wr.run_chained([p1], seeds, scope="per_file", pattern="a.dream3d",
                         stage_dir=tmp_path / "st", output_dir=tmp_path / "o",
                         timeout_s=300)
    assert res.success, res.summary()


# ============================================================
# Real env: the worker (run_folder / describe / preflight)
# ============================================================

@needs_nx
def test_worker_loads_a_pipeline_that_uses_plugin_filters(seeds, tmp_path):
    """It imported simplnx only, so every pipeline naming an
    OrientationAnalysis / ITKImageProcessing filter failed to load."""
    d = nx_bridge.describe_pipeline(SHIPPED / P03)
    assert d["size"] == 10
    vault = tmp_path / "vault"
    out = vault / "data_out" / "dream3d" / "runs"
    res = nx_bridge.run_folder(SHIPPED / P03, seeds, out, glob="*.dream3d",
                               vault_dir=vault)
    assert (res["total"], res["ok"]) == (2, 2), res
    assert all(Path(r["write_set"]["dest"]).exists() for r in res["runs"])


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
