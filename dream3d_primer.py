# ============================================================
# dream3d_primer.py  —  Dream3D/simplnx context for the council
# ============================================================
# Injects Dream3D-specific knowledge into Coder and Writer
# system prompts so the models have the right baseline API
# understanding even before RAG retrieves specific filter docs.
#
# Usage: imported by council_engine.py at startup.
#        Call inject_dream3d_context(personalities) after
#        build_personalities() returns.
# ============================================================

from __future__ import annotations
from typing import Any, Dict

# ── Core API primer ───────────────────────────────────────────
# This is injected into Coder and Writer's extra_context.
# Covers: imports, DataStructure, filter execution pattern,
# result checking, pipeline construction, common pitfalls.

SIMPLNX_PRIMER = """
=== DREAM3D-NX / simplnx Python API PRIMER ===

You are working with DREAM3D-NX, a materials science data processing framework.
The Python package is called `simplnx` (import as `import simplnx as nx`).
An optional companion package `dream3dnx` may also be available.

## Core imports
```python
import simplnx as nx
# Optional orientation analysis filters:
import orientationanalysis as oa
# Optional ITK image processing filters:
import itkimageprocessing as itk
```

## Fundamental pattern — ALWAYS follow this structure
```python
import simplnx as nx

# 1. Create a DataStructure (the in-memory data container)
data_structure = nx.DataStructure()

# 2. Execute filters — each returns a Result object
result = nx.SomeFilter.execute(
    data_structure=data_structure,
    parameter_name=value,
    # ... other parameters
)

# 3. ALWAYS check the result
if not result.valid():
    print(f"Filter failed: {result.errors}")
    raise RuntimeError("Pipeline failed")
```

## DataPath — addressing data inside the DataStructure
```python
# DataPath is a dot-separated address to any object in the DataStructure
group_path  = nx.DataPath("MyGroup")
array_path  = nx.DataPath("MyGroup/MyArray")
geom_path   = nx.DataPath("ImageGeometry")
cell_am_path = nx.DataPath("ImageGeometry/CellData")
```

## Creating a DataGroup
```python
result = nx.CreateDataGroupFilter.execute(
    data_structure=data_structure,
    data_object_path=nx.DataPath("MyGroup")
)
```

## Creating a DataArray
```python
result = nx.CreateDataArrayFilter.execute(
    data_structure=data_structure,
    numeric_type=nx.NumericType.float32,  # or int32, uint8, float64, etc.
    num_comps=1,              # components per tuple (1=scalar, 3=vector)
    tuple_dims=[[100]],       # list of dimension sizes
    output_array_path=nx.DataPath("MyGroup/MyArray"),
    initialization_value="0"
)
```

## NumericType values
- nx.NumericType.int8, int16, int32, int64
- nx.NumericType.uint8, uint16, uint32, uint64
- nx.NumericType.float32, float64

## Reading/writing .dream3d files
```python
# Import (read)
result = nx.ReadDREAM3DFilter.execute(
    data_structure=data_structure,
    import_file_data=nx.Dream3dImportParameter.ImportData(
        file_path="path/to/file.dream3d"
    )
)

# Export (write)
result = nx.WriteDREAM3DFilter.execute(
    data_structure=data_structure,
    export_file_path="path/to/output.dream3d",
    write_xdmf_file=True
)
```

## Creating an Image Geometry
```python
result = nx.CreateImageGeometryFilter.execute(
    data_structure=data_structure,
    geometry_path=nx.DataPath("ImageGeometry"),
    dimensions=[100, 100, 100],   # [x, y, z] voxel counts
    origin=[0.0, 0.0, 0.0],       # [x, y, z] origin
    spacing=[1.0, 1.0, 1.0],      # [x, y, z] voxel size
    cell_attribute_matrix_name="CellData"
)
```

## Working with numpy
```python
import numpy as np

# Get a DataArray as a numpy view (zero-copy)
array = data_structure[nx.DataPath("MyGroup/MyArray")]
np_view = array.npview()   # modifying np_view modifies the DataArray in-place
np_view[:] = np.random.rand(*np_view.shape)
```

## Pipeline object (for reusable/serializable pipelines)
```python
pipeline = nx.Pipeline()
pipeline.insert(0, nx.SomeFilter(), {
    "parameter_name": value,
})
result = pipeline.execute(data_structure)
```

## Result checking patterns
```python
# Minimal — raise on failure
assert result.valid(), f"Failed: {result.errors}"

# Verbose — show warnings too
if result.warnings:
    for w in result.warnings:
        print(f"Warning [{w.code}]: {w.message}")
if not result.valid():
    for e in result.errors:
        print(f"Error [{e.code}]: {e.message}")
    raise RuntimeError("Filter failed")
```

## Common pitfalls
1. DataPath strings are case-sensitive and path-separator is "/" not "."
2. Always check result.valid() — filters do NOT raise exceptions on failure
3. tuple_dims is a list-of-lists: [[z, y, x]] for 3D, [[n]] for 1D
4. num_comps is the number of components per tuple (e.g. 3 for RGB or XYZ vector)
5. Filters operate IN PLACE on data_structure — no return value for data
6. Import orientationanalysis before using any EBSD/orientation filters
7. nx.DataPath("") is invalid — always provide a non-empty path string

=== END PRIMER ===
"""

# Shorter version for Writer (synthesis context, not code generation)
WRITER_PRIMER = """
=== DREAM3D-NX context ===
The user is working with DREAM3D-NX (simplnx Python package).
Key concepts: DataStructure (in-memory container), DataPath (address to data),
filters executed via FilterName.execute(data_structure=..., **params),
results checked with result.valid().
Pipelines process materials science data: EBSD, image geometry, orientation analysis.
When synthesizing code proposals, ensure: imports are correct (simplnx as nx),
result checking is present after every filter, DataPaths are properly formed.
=== END ===
"""


# ── Injection ─────────────────────────────────────────────────

def inject_dream3d_context(personalities: Dict[str, Any]) -> None:
    """
    Inject the Dream3D primer into Coder and Writer's
    extra_context so every response is Dream3D-aware.

    Call this after build_personalities():
        personalities = ce.build_personalities(...)
        inject_dream3d_context(personalities)
    """
    coder = personalities.get("coder")
    writer      = personalities.get("writer")
    intern_     = personalities.get("intern")

    if coder is not None:
        existing = getattr(coder, "extra_context", "") or ""
        coder.extra_context = (SIMPLNX_PRIMER + "\n\n" + existing).strip()
        print("[Dream3D] Injected simplnx primer into Coder")

    if writer is not None:
        existing = getattr(writer, "extra_context", "") or ""
        writer.extra_context = (WRITER_PRIMER + "\n\n" + existing).strip()
        print("[Dream3D] Injected simplnx primer into Writer")

    if intern_ is not None:
        existing = getattr(intern_, "extra_context", "") or ""
        # Give intern a lightweight version so it can ask better questions
        intern_.extra_context = (WRITER_PRIMER + "\n\n" + existing).strip()
        print("[Dream3D] Injected simplnx primer into Intern")


# ── Pipeline validator ────────────────────────────────────────

class PipelineValidator:
    """
    Static analysis checks for Dream3D pipeline scripts.
    Runs BEFORE execution in the Coder agent loop to catch
    common structural errors early and give the model better
    feedback than a raw Python traceback.
    """

    CHECKS = [
        # (pattern_that_should_exist, error_message)
        (r"import simplnx",
         "Missing 'import simplnx as nx'. All Dream3D scripts must import simplnx."),

        (r"DataStructure\s*\(",
         "No DataStructure created. Add: data_structure = nx.DataStructure()"),

        (r"\.execute\s*\(",
         "No filter .execute() calls found. Filters must be executed explicitly."),

        (r"result\.valid\(\)|assert result|if not result",
         "No result checking found. Every filter.execute() result must be checked with result.valid()."),
    ]

    ANTIPATTERNS = [
        # (bad_pattern, warning_message)
        (r'DataPath\s*\(\s*["\'][\s]*["\']\s*\)',
         "Empty DataPath detected: nx.DataPath(\"\") is invalid."),

        (r'tuple_dims\s*=\s*\[\s*\d',
         "tuple_dims should be a list-of-lists, e.g. [[100]] not [100]."),

        (r'import dream3d\b(?!nx)',
         "Old 'dream3d' package — use 'import simplnx as nx' for Dream3D-NX."),
    ]

    @classmethod
    def validate(cls, code: str) -> tuple[bool, list[str]]:
        """
        Returns (is_valid, list_of_issues).
        Issues are human-readable strings to feed back to the model.
        """
        issues: list[str] = []

        for pattern, msg in cls.CHECKS:
            import re
            if not re.search(pattern, code, re.IGNORECASE):
                issues.append(f"MISSING: {msg}")

        for pattern, msg in cls.ANTIPATTERNS:
            import re
            if re.search(pattern, code, re.IGNORECASE):
                issues.append(f"WARNING: {msg}")

        return len(issues) == 0, issues

    @classmethod
    def format_feedback(cls, issues: list[str]) -> str:
        if not issues:
            return ""
        lines = ["Static analysis found issues BEFORE execution:"]
        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue}")
        lines.append("\nFix these issues in your next attempt.")
        return "\n".join(lines)
