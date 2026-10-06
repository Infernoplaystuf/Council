"""Runs in the nx env: write a small synthetic 'SmallIN100_Final'-shaped .dream3d.

    <nxpython> nx_seed.py <out.dream3d> <seed>

The dream3dnx conda package ships the example PIPELINES but none of their
Data/ inputs. "(03) Small IN100 Morphological Statistics" imports with
policy=All and then needs only DataContainer (an image geometry) with
Cell Data/{FeatureIds,Phases,Quats}, a Cell Feature Data matrix (one tuple per
feature + 1) and Cell Ensemble Data/CrystalStructures; (04) also uses Quats and
CrystalStructures. This writes exactly that.

Grain sizes are deliberately uneven (Voronoi seeds bunched toward x=0). With
equal grains every feature's NeighborhoodList comes out EMPTY under (03)'s
multiples_of_average=1, and this simplnx build writes an all-empty NeighborList
that it then cannot read back ("Error reading neighbor list from DataStore
from HDF5 ... 'NeighborhoodList'"), which breaks (03) -> (04). Measured
2026-10-06; it is simplnx's bug, not the app's.
"""
import sys

import numpy as np
import simplnx as nx

out = sys.argv[1]
rng = np.random.default_rng(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
DX, DY, DZ = 24, 20, 8
NF = 32

ds = nx.DataStructure()


def ok(r, what):
    assert not r.errors, (what, r.errors)


ok(nx.CreateGeometryFilter.execute(
    data_structure=ds, geometry_type_index=0,
    output_geometry_path=nx.DataPath("DataContainer"),
    dimensions=[DX, DY, DZ], origin=[0.0, 0.0, 0.0], spacing=[0.25, 0.25, 0.25],
    cell_attribute_matrix_name="Cell Data", array_handling_index=0), "geom")
cells = [[float(DZ), float(DY), float(DX)]]
for name, nt, comps, init in [("FeatureIds", nx.NumericType.int32, 1, "0"),
                              ("Phases", nx.NumericType.int32, 1, "1"),
                              ("Quats", nx.NumericType.float32, 4, "0")]:
    ok(nx.CreateDataArrayFilter.execute(
        data_structure=ds, numeric_type_index=nt, component_count=comps,
        initialization_value_str=init, set_tuple_dimensions=False,
        tuple_dimensions=cells,
        output_array_path=nx.DataPath(f"DataContainer/Cell Data/{name}")), name)
ok(nx.CreateAttributeMatrixFilter.execute(
    data_structure=ds,
    data_object_path=nx.DataPath("DataContainer/Cell Feature Data"),
    tuple_dimensions=[[float(NF + 1)]]), "feature AM")
ok(nx.CreateAttributeMatrixFilter.execute(
    data_structure=ds,
    data_object_path=nx.DataPath("DataContainer/Cell Ensemble Data"),
    tuple_dimensions=[[2.0]]), "ensemble AM")
ok(nx.CreateDataArrayFilter.execute(
    data_structure=ds, numeric_type_index=nx.NumericType.uint32,
    component_count=1, initialization_value_str="999",
    set_tuple_dimensions=False, tuple_dimensions=[[2.0]],
    output_array_path=nx.DataPath(
        "DataContainer/Cell Ensemble Data/CrystalStructures")), "xtal")

z, y, x = np.meshgrid(np.arange(DZ), np.arange(DY), np.arange(DX), indexing="ij")
seeds = np.stack([rng.uniform(0, DZ, NF), rng.uniform(0, DY, NF),
                  DX * rng.uniform(0, 1, NF) ** 3], 1)
pts = np.stack([z, y, x], -1).reshape(-1, 1, 3).astype(np.float64)
ids = 1 + np.argmin(((pts - seeds[None]) ** 2).sum(-1), axis=1).reshape(z.shape)
ds[nx.DataPath("DataContainer/Cell Data/FeatureIds")].npview()[..., 0] = ids
fq = rng.normal(size=(NF + 1, 4)).astype(np.float32)
fq /= np.linalg.norm(fq, axis=1, keepdims=True)
ds[nx.DataPath("DataContainer/Cell Data/Quats")].npview()[...] = fq[ids]
ds[nx.DataPath("DataContainer/Cell Ensemble Data/CrystalStructures")].npview()[1] = 1

ok(nx.WriteDREAM3DFilter.execute(data_structure=ds, export_file_path=out,
                                 write_xdmf_file=False), "write")
