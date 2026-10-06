"""Runs in the nx env: report what .dream3d files contain, as JSON on stdout.

    <nxpython> nx_probe.py <file.dream3d> [...]

For each file: whether it read cleanly, the data paths the tests ask about,
and the max xyz of the first triangle geometry's vertices (if any).
"""
import json
import sys

import simplnx as nx
import orientationanalysis  # noqa: F401  (registers its array types)

ASK = ["DataContainer/Cell Feature Data/Centroids",
       "DataContainer/Cell Feature Data/EquivalentDiameters",
       "DataContainer/Cell Feature Data/NeighborhoodList",
       "DataContainer/Cell Feature Data/AvgQuats",
       "DataContainer/Cell Feature Data/Schmids",
       "DataContainer/Cell Data/KernelAverageMisorientations"]
out = {}
for f in sys.argv[1:]:
    ds = nx.DataStructure()
    rec = {}
    try:
        r = nx.ReadDREAM3DFilter.execute(
            data_structure=ds,
            import_data_object=nx.Dream3dImportParameter.ImportData(
                file_path=f,
                path_import_policy=nx.Dream3dImportParameter.PathImportPolicy.All))
        rec["errors"] = [str(e) for e in r.errors]
    except Exception as exc:
        rec["errors"] = [f"{type(exc).__name__}: {exc}"]
    rec["has"] = {p: bool(ds.exists(nx.DataPath(p))) for p in ASK}
    v = nx.DataPath("TriangleDataContainer/Shared Vertex List")
    rec["tri_max"] = ds[v].npview().max(axis=0).tolist() if ds.exists(v) else None
    out[f] = rec
print(json.dumps(out))
