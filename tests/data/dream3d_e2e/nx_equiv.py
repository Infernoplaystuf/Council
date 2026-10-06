"""Runs in the nx env: does a TRANSPILED script compute what simplnx's own
Pipeline computes from the same .d3dpipeline? JSON on stdout.

    <nxpython> nx_equiv.py <orig.d3dpipeline> <transpiled.py> <seed.dream3d> <workdir>

Native : nx.Pipeline.from_file(orig); the reader's ImportData.file_path -> seed,
         every export_file_path -> <workdir>/n.dream3d; execute.
Script : the transpiled source with its baked reader/writer path literals
         swapped for the seed and <workdir>/t.dream3d (the edit a user, or the
         chain runner, makes), run with runpy.
Then both DataStructures are compared: hierarchy text, and every array's values.

Output names are short on purpose: simplnx's AtomicFile writes through a
24-character temp subfolder, and with LongPathsEnabled=0 a long work dir
pushes that past MAX_PATH ("Failed to create DREAM3D file").
"""
import json
import os
import re
import runpy
import sys
from pathlib import Path

import numpy as np
import simplnx as nx
# Pipeline.from_file cannot create a plugin's filters unless the plugin module
# has been imported: without these, (03) fails with "Failed to create filter
# 'nx::core::ComputeShapesFilter' from UUID ...".
import orientationanalysis  # noqa: F401
import itkimageprocessing  # noqa: F401

orig, script, seed, work = (Path(a).resolve() for a in sys.argv[1:5])
work.mkdir(parents=True, exist_ok=True)
report = {}

p = nx.Pipeline.from_file(str(orig))
read_rel = write_rel = None
for i in range(p.size()):
    pf = p[i]
    args = pf.get_args()
    if "import_data_object" in args:
        obj = args["import_data_object"]
        read_rel = Path(str(obj.file_path)).as_posix()
        obj.file_path = str(seed)
        args["import_data_object"] = obj
        pf.set_args(args)
    elif "export_file_path" in args:
        write_rel = Path(str(args["export_file_path"])).as_posix()
        args["export_file_path"] = str(work / "n.dream3d")
        pf.set_args(args)
ds_n = nx.DataStructure()
res = p.execute(ds_n)
report["native_errors"] = [str(e) for e in res.errors]

src = script.read_text(encoding="utf-8")
report["reader_literal_found"] = repr(read_rel) in src
src = src.replace(repr(read_rel), repr(str(seed)), 1)
if write_rel:
    report["writer_literal_found"] = repr(write_rel) in src
    src = src.replace(repr(write_rel), repr(str(work / "t.dream3d")), 1)
(work / "t.py").write_text(src, encoding="utf-8")
cwd = os.getcwd()
os.chdir(work)
try:
    ds_t = runpy.run_path(str(work / "t.py"), run_name="__main__")["ds"]
    report["script_error"] = None
except BaseException as exc:                   # noqa: BLE001
    ds_t = None
    report["script_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
finally:
    os.chdir(cwd)
report["script_wrote"] = (work / "t.dream3d").exists()

if ds_t is not None:
    hn, ht = ds_n.hierarchy_to_str(), ds_t.hierarchy_to_str()
    report["hierarchy_identical"] = hn == ht
    paths, stack = [], []
    for line in hn.splitlines():
        name = line.strip().lstrip("|-").strip()
        if not name:
            continue
        depth = len(line) - len(line.lstrip(" |"))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack.append((depth, name))
        paths.append("/".join(n for _d, n in stack))
    same, diff = 0, []
    for path in paths:
        try:
            a = np.asarray(ds_n[nx.DataPath(path)].npview())
            b = np.asarray(ds_t[nx.DataPath(path)].npview())
        except Exception:                      # noqa: BLE001  (not an array)
            continue
        if a.shape == b.shape and np.array_equal(a, b, equal_nan=True):
            same += 1
        else:
            diff.append(path)
    report["arrays_identical"] = same
    report["arrays_different"] = diff
print(json.dumps(report))
