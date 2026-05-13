"""
Pipeline scanner — finds Dream3D pipelines in the vault, parses them, and
renders each filter step with [inputs] / (outputs) annotations.

Looks under `vault/pipelines/in/` only — `vault/pipelines/out/` is the
versioned-modification destination and is excluded so modified copies never
leak back into the model's context.

Supports two file types:
  - .py    simplnx Python scripts. Parsed via the stdlib `ast` module so we
           don't have to execute user code.
  - .dream3d  HDF5 archives with an embedded pipeline JSON blob. Uses h5py
              if available; gracefully degrades (returns an empty step list
              with an informational status) when h5py is missing.

Input vs. output classification follows the spec:
  inputs  = pre-existing objects passed into the filter (file paths being
            read, existing DataPaths being consumed, scalar config values).
  outputs = anything CREATED by the filter (new DataPaths, files being
            written, attribute matrices being made).

The classification uses parameter-name + filter-name-prefix heuristics
derived from the simplnx API conventions documented in `dream3d_primer.py`.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import h5py  # optional — only needed for .dream3d HDF5 files
    _H5PY_AVAILABLE = True
except Exception:
    h5py = None  # type: ignore
    _H5PY_AVAILABLE = False


# ============================================================
# Classification heuristics
# ============================================================

# Filter-name prefixes that typically CREATE objects. When a filter starts
# with one of these, ambiguous `*_path` parameters get flagged as outputs.
# Kept conservative — Find*/Identify*/Threshold* often CONSUME existing
# paths (e.g. Find uses feature_ids_path as input) and produce their
# result via an explicit output_*_path param, which the EXPLICIT_OUTPUT
# keyword rules catch on their own.
_CREATE_PREFIXES = (
    "Create", "Generate", "Initialize", "Compute", "Map", "Segment",
)
# Filter-name prefixes that READ from disk. The file_path param on these
# is an input.
_READ_PREFIXES = (
    "Read", "Import", "Load",
)
# Filter-name prefixes that WRITE to disk. The file_path / export_*
# parameter is an output.
_WRITE_PREFIXES = (
    "Write", "Export", "Save",
)

# Parameter name patterns — explicit win over filter-name heuristics.
_EXPLICIT_OUTPUT_KEYWORDS = (
    "output_", "created_", "export_",
    # NOTE: "write_" is intentionally excluded — `write_xdmf_file=True`
    # and similar boolean flags would falsely match. The filter-name
    # heuristic below catches genuine output paths on Write* filters
    # via the `file_path` rule.
)
_EXPLICIT_INPUT_KEYWORDS = (
    "input_", "import_", "read_", "source_",
)
# Specific param names that are always outputs regardless of filter
_ALWAYS_OUTPUT = {
    "output_array_path",
    "output_data_path",
    "output_image_geometry_path",
    "cell_attribute_matrix_name",
    "feature_attribute_matrix_name",
    "ensemble_attribute_matrix_name",
    "created_image_geometry_path",
    "destination_data_path",
}
# Specific param names that are always inputs
_ALWAYS_INPUT = {
    "data_structure",
    "selected_data_path",
    "input_data_path",
    "input_array_path",
    "input_image_geometry_path",
    "import_file_data",
}


def _classify_param(param_name: str, filter_name: str) -> str:
    """Return 'input', 'output', or 'config' for one keyword argument."""
    pn = param_name.lower()

    # Explicit allowlists win
    if pn in _ALWAYS_OUTPUT:
        return "output"
    if pn in _ALWAYS_INPUT:
        return "input"

    # Explicit name prefixes
    for kw in _EXPLICIT_OUTPUT_KEYWORDS:
        if pn.startswith(kw):
            return "output"
    for kw in _EXPLICIT_INPUT_KEYWORDS:
        if pn.startswith(kw):
            return "input"

    # Filter-name based heuristics for ambiguous params
    fn = filter_name
    if pn == "file_path":
        # Read* → input file, Write*/Export* → output file
        if fn.startswith(_WRITE_PREFIXES):
            return "output"
        return "input"
    if pn in ("export_file_path", "output_file_path"):
        return "output"
    if pn in ("geometry_path", "data_object_path"):
        # On Create*/Generate* filters → output (newly created), else input.
        if fn.startswith(_CREATE_PREFIXES):
            return "output"
        return "input"

    # Other path-like params on Create* filters → output
    if pn.endswith("_path") and fn.startswith(_CREATE_PREFIXES):
        return "output"
    if pn.endswith("_path"):
        return "input"

    # Anything else is a configuration scalar/struct (dimensions, origin,
    # numeric_type, initialization_value, ...). Treated as input per the
    # user spec: "any pre-existing object in the args".
    return "config"


# ============================================================
# Data classes
# ============================================================

@dataclass
class FilterStep:
    index: int
    filter_name: str             # e.g. "CreateImageGeometryFilter"
    raw_call: str                # short rendering of the call site
    inputs:  List[Tuple[str, str]] = field(default_factory=list)   # [(param, value_repr)]
    outputs: List[Tuple[str, str]] = field(default_factory=list)
    configs: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class Pipeline:
    path: Path
    name: str
    format: str                  # "py" | "dream3d" | "unknown"
    steps: List[FilterStep] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # parser warnings/info

    @property
    def input_summary(self) -> List[str]:
        seen, out = set(), []
        for s in self.steps:
            for _, v in s.inputs:
                if v not in seen:
                    seen.add(v); out.append(v)
        return out

    @property
    def output_summary(self) -> List[str]:
        seen, out = set(), []
        for s in self.steps:
            for _, v in s.outputs:
                if v not in seen:
                    seen.add(v); out.append(v)
        return out


# ============================================================
# .py parser (simplnx scripts) — uses stdlib ast
# ============================================================

# Match `nx.SomeFilter.execute(...)` or `simplnx.SomeFilter.execute(...)`
def _is_filter_execute_call(node: ast.Call) -> Optional[str]:
    """If node is an Attribute call like `nx.X.execute(...)`, return X.
    Otherwise None."""
    func = node.func
    # `nx.X.execute(...)` -> Attribute(value=Attribute(value=Name('nx'),attr='X'),attr='execute')
    if (isinstance(func, ast.Attribute) and func.attr == "execute"
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id in ("nx", "simplnx", "oa", "orientationanalysis",
                                          "itk", "itkimageprocessing")):
        return func.value.attr
    return None


# Match `pipeline.insert(IDX, nx.SomeFilter(), {...})`
def _is_pipeline_insert_call(node: ast.Call) -> Optional[Tuple[str, ast.Dict]]:
    """If node is `pipeline.insert(_, nx.X(), {...})`, return (X, dict_node)."""
    func = node.func
    if (isinstance(func, ast.Attribute) and func.attr == "insert"
            and len(node.args) >= 3):
        filter_arg = node.args[1]
        params_arg = node.args[2]
        if (isinstance(filter_arg, ast.Call)
                and isinstance(filter_arg.func, ast.Attribute)
                and isinstance(filter_arg.func.value, ast.Name)
                and filter_arg.func.value.id in ("nx", "simplnx")):
            if isinstance(params_arg, ast.Dict):
                return filter_arg.func.attr, params_arg
    return None


def _value_repr(node: ast.AST) -> str:
    """Compact human-readable rendering of an AST value node."""
    try:
        return ast.unparse(node)
    except Exception:
        return "<value>"


def parse_py_pipeline(path: Path) -> Pipeline:
    pipeline = Pipeline(path=path, name=path.name, format="py")
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except Exception as exc:
        pipeline.notes.append(f"parse error: {exc}")
        return pipeline

    step_idx = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        filter_name = _is_filter_execute_call(node)
        kwargs_dict: Optional[Dict[str, ast.AST]] = None
        if filter_name:
            kwargs_dict = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        else:
            insert_match = _is_pipeline_insert_call(node)
            if insert_match:
                filter_name, dict_node = insert_match
                kwargs_dict = {}
                for k, v in zip(dict_node.keys, dict_node.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        kwargs_dict[k.value] = v
        if not filter_name or not kwargs_dict:
            continue

        step_idx += 1
        step = FilterStep(
            index=step_idx,
            filter_name=filter_name,
            raw_call=f"nx.{filter_name}.execute(...)",
        )
        for pname, vnode in kwargs_dict.items():
            cls = _classify_param(pname, filter_name)
            entry = (pname, _value_repr(vnode))
            if cls == "input":
                step.inputs.append(entry)
            elif cls == "output":
                step.outputs.append(entry)
            else:
                step.configs.append(entry)
        pipeline.steps.append(step)

    if not pipeline.steps:
        pipeline.notes.append(
            "no simplnx filter calls detected — the file may not be a "
            "Dream3D pipeline, or uses an unusual call pattern."
        )
    return pipeline


# ============================================================
# .dream3d parser (HDF5)
# ============================================================

# Standard locations for the pipeline JSON inside a .dream3d HDF5
_DREAM3D_PIPELINE_PATHS = (
    "/Pipeline",
    "/PipelineV2",
    "/Pipeline/Pipeline",
)


def parse_dream3d_pipeline(path: Path) -> Pipeline:
    pipeline = Pipeline(path=path, name=path.name, format="dream3d")
    if not _H5PY_AVAILABLE:
        pipeline.notes.append(
            "h5py not installed — install it with `pip install h5py` to "
            "parse .dream3d files. Showing filename only."
        )
        return pipeline

    try:
        with h5py.File(str(path), "r") as fh:
            blob = None
            for candidate in _DREAM3D_PIPELINE_PATHS:
                if candidate in fh:
                    node = fh[candidate]
                    # Could be a dataset with the JSON string, or a group with
                    # an attribute named "Pipeline" / "Pipeline_JSON".
                    if isinstance(node, h5py.Dataset):
                        try:
                            blob = node[()]
                        except Exception:
                            blob = None
                    elif isinstance(node, h5py.Group):
                        for attr in ("Pipeline", "Pipeline_JSON",
                                     "PipelineV2", "Pipeline_Version"):
                            if attr in node.attrs:
                                blob = node.attrs[attr]
                                break
                    if blob is not None:
                        break

            if blob is None:
                # Walk attrs of the root and look for anything that decodes as JSON
                for attr_name in fh.attrs:
                    val = fh.attrs[attr_name]
                    if isinstance(val, (bytes, str)) and "Pipeline" in str(attr_name):
                        blob = val
                        break

            if blob is None:
                pipeline.notes.append(
                    "no pipeline metadata found in HDF5 file at the standard "
                    "locations (/Pipeline, /PipelineV2). The file may be a "
                    "data-only dream3d export."
                )
                return pipeline

            if isinstance(blob, (bytes, bytearray)):
                try:
                    blob = blob.decode("utf-8", errors="replace")
                except Exception:
                    blob = str(blob)

            try:
                data = json.loads(blob)
            except Exception as exc:
                pipeline.notes.append(f"pipeline JSON parse failed: {exc}")
                return pipeline

            _populate_steps_from_dream3d_json(pipeline, data)
    except Exception as exc:
        pipeline.notes.append(f"HDF5 read failed: {exc}")
    return pipeline


def _populate_steps_from_dream3d_json(pipeline: Pipeline, data: Any) -> None:
    """Convert a parsed pipeline JSON object into FilterStep records."""
    # Two known shapes:
    #   1. Legacy: dict with numeric-string keys "0", "1", ... and
    #      "PipelineBuilder" metadata
    #   2. Modern: dict with key "pipeline" -> list of step dicts
    steps_iter: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if "pipeline" in data and isinstance(data["pipeline"], list):
            steps_iter = data["pipeline"]
        else:
            # legacy: gather numeric-string keys in order
            numeric_keys = sorted(
                [k for k in data.keys() if str(k).isdigit()],
                key=lambda k: int(k),
            )
            for k in numeric_keys:
                v = data[k]
                if isinstance(v, dict):
                    steps_iter.append(v)
    elif isinstance(data, list):
        steps_iter = [s for s in data if isinstance(s, dict)]

    for i, step in enumerate(steps_iter, start=1):
        filter_name = (
            step.get("name")
            or step.get("filter_name")
            or step.get("Filter_Name")
            or step.get("FilterName")
            or "UnknownFilter"
        )
        params = (
            step.get("args")
            or step.get("parameters")
            or step.get("Parameters")
            or {}
        )
        if not isinstance(params, dict):
            params = {}

        fs = FilterStep(
            index=i, filter_name=filter_name,
            raw_call=f"{filter_name}(...)",
        )
        for pname, val in params.items():
            cls = _classify_param(str(pname), str(filter_name))
            entry = (str(pname), json.dumps(val) if not isinstance(val, str) else val)
            if cls == "input":
                fs.inputs.append(entry)
            elif cls == "output":
                fs.outputs.append(entry)
            else:
                fs.configs.append(entry)
        pipeline.steps.append(fs)


# ============================================================
# Folder scan + render
# ============================================================

def parse_pipeline_file(path: Path) -> Pipeline:
    """Dispatch on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return parse_py_pipeline(path)
    if suffix == ".dream3d":
        return parse_dream3d_pipeline(path)
    return Pipeline(path=path, name=path.name, format="unknown",
                    notes=["unsupported extension"])


def scan_pipelines(folder: Path) -> List[Pipeline]:
    """Walk a folder (recursively) and parse every .py and .dream3d file."""
    folder = Path(folder)
    if not folder.exists():
        return []
    pipelines: List[Pipeline] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".py", ".dream3d"):
            continue
        pipelines.append(parse_pipeline_file(p))
    pipelines.sort(key=lambda pl: pl.name.lower())
    return pipelines


def render_pipeline(pipeline: Pipeline, *, max_lines: int = 200) -> str:
    """Format a Pipeline as plain text with [inputs] and (outputs)."""
    lines: List[str] = [f"Pipeline: {pipeline.name}  [{pipeline.format}]"]
    lines.append(f"Path: {pipeline.path}")
    if pipeline.notes:
        for n in pipeline.notes:
            lines.append(f"  note: {n}")
    if not pipeline.steps:
        return "\n".join(lines)

    # Per-step listing
    lines.append("")
    for s in pipeline.steps:
        lines.append(f"Step {s.index}: {s.filter_name}")
        for pname, val in s.inputs:
            lines.append(f"  [{pname}] = {_truncate(val)}")
        for pname, val in s.outputs:
            lines.append(f"  ({pname}) = {_truncate(val)}")
        for pname, val in s.configs:
            lines.append(f"   {pname} = {_truncate(val)}")
        if len(lines) > max_lines:
            lines.append("  ... (truncated)")
            break

    # Aggregate inputs/outputs at the end
    lines.append("")
    if pipeline.input_summary:
        lines.append("All inputs (brackets in spec):")
        for v in pipeline.input_summary[:20]:
            lines.append(f"  [{_truncate(v)}]")
    if pipeline.output_summary:
        lines.append("All outputs (parentheses in spec):")
        for v in pipeline.output_summary[:20]:
            lines.append(f"  ({_truncate(v)})")
    return "\n".join(lines)


def _truncate(s: str, n: int = 100) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


# ============================================================
# Vault integration helpers
# ============================================================

def export_pipeline_to_markdown(pipeline: Pipeline) -> str:
    """Render a parsed pipeline as a Markdown doc — headings, inputs in
    backtick code, outputs in **bold**. Drops into vault/data_out/ or
    can be saved by the caller. Useful for sharing pipelines or pasting
    into a README."""
    out: List[str] = [
        f"# {pipeline.name}",
        "",
        f"_Format: {pipeline.format} — {len(pipeline.steps)} steps_",
        f"_Path: `{pipeline.path}`_",
        "",
    ]
    if pipeline.notes:
        for n in pipeline.notes:
            out.append(f"> {n}")
        out.append("")

    if pipeline.input_summary:
        out.append("## Pipeline inputs")
        out.append("")
        for v in pipeline.input_summary:
            out.append(f"- `{v}`")
        out.append("")

    if pipeline.output_summary:
        out.append("## Pipeline outputs")
        out.append("")
        for v in pipeline.output_summary:
            out.append(f"- **`{v}`**")
        out.append("")

    out.append("## Steps")
    out.append("")
    for s in pipeline.steps:
        out.append(f"### Step {s.index}: `{s.filter_name}`")
        out.append("")
        if s.inputs:
            out.append("Inputs:")
            for pn, vv in s.inputs:
                out.append(f"  - `{pn}` = `{vv}`")
            out.append("")
        if s.outputs:
            out.append("Outputs:")
            for pn, vv in s.outputs:
                out.append(f"  - **`{pn}`** = **`{vv}`**")
            out.append("")
        if s.configs:
            out.append("Config:")
            for pn, vv in s.configs:
                out.append(f"  - `{pn}` = `{vv}`")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def vault_pipelines_in_dir(vault_dir: Path) -> Path:
    """Standard input pipeline location under the vault."""
    p = Path(vault_dir) / "pipelines" / "in"
    p.mkdir(parents=True, exist_ok=True)
    return p


def vault_pipelines_out_dir(vault_dir: Path) -> Path:
    """Standard output (modified-version) pipeline location."""
    p = Path(vault_dir) / "pipelines" / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ============================================================
# Validation
# ============================================================

# Known required parameters for common simplnx filters. The list isn't
# exhaustive — it covers the filters most users hit. Filters not listed
# fall through with no errors, only warnings about unrecognized params.
_REQUIRED_PARAMS: Dict[str, set] = {
    "CreateDataArrayFilter": {
        "data_structure", "numeric_type", "num_comps", "tuple_dims",
        "output_array_path",
    },
    "CreateImageGeometryFilter": {
        "data_structure", "geometry_path", "dimensions", "origin",
        "spacing", "cell_attribute_matrix_name",
    },
    "CreateDataGroupFilter": {
        "data_structure", "data_object_path",
    },
    "ReadDREAM3DFilter": {
        "data_structure", "import_file_data",
    },
    "WriteDREAM3DFilter": {
        "data_structure", "export_file_path",
    },
    "ReadCSVFile": {
        "data_structure",
    },
}


def validate_pipeline_params(pipeline: Pipeline) -> List[str]:
    """Static checks against the simplnx schema we know about.

    Returns a list of human-readable issues. Empty list = pipeline looks
    well-formed against our partial schema. Issues are tagged 'error:'
    for missing required params and 'warning:' for less critical things
    like duplicate output paths.
    """
    issues: List[str] = []
    seen_outputs: Dict[str, int] = {}
    for s in pipeline.steps:
        # Required-param check
        required = _REQUIRED_PARAMS.get(s.filter_name)
        if required:
            present = {p for p, _ in s.inputs} | {p for p, _ in s.outputs} \
                      | {p for p, _ in s.configs}
            missing = required - present
            if missing:
                issues.append(
                    f"error: step {s.index} {s.filter_name} missing required "
                    f"parameter(s): {', '.join(sorted(missing))}"
                )
        # Duplicate output detection (e.g., two filters writing to the
        # same DataPath — usually a copy-paste bug).
        for pname, vrep in s.outputs:
            key = vrep.strip()
            if not key:
                continue
            if key in seen_outputs:
                issues.append(
                    f"warning: step {s.index} {s.filter_name} ({pname}={vrep}) "
                    f"overwrites the output of step {seen_outputs[key]}"
                )
            seen_outputs[key] = s.index
    return issues


# ============================================================
# Dependency graph
# ============================================================

def pipeline_dependency_graph(pipeline: Pipeline) -> str:
    """Render which filter's outputs feed which other filter's inputs.

    Returns a text-tree showing data flow per the parsed pipeline.
    Uses the value text (e.g. nx.DataPath("Features")) as the matching
    token; same value across steps = a dependency edge.
    """
    if not pipeline.steps:
        return "(no steps to graph)"

    # Build value -> producer step index, and consumer index -> [values]
    producer: Dict[str, int] = {}
    consumers: Dict[int, List[Tuple[str, int]]] = defaultdict(list)
    for s in pipeline.steps:
        for _, vrep in s.outputs:
            key = vrep.strip()
            if key:
                producer.setdefault(key, s.index)
        for pname, vrep in s.inputs:
            key = vrep.strip()
            if key and key in producer and producer[key] != s.index:
                consumers[s.index].append((key, producer[key]))

    lines = [f"Data flow for {pipeline.name}:"]
    for s in pipeline.steps:
        deps = consumers.get(s.index, [])
        if deps:
            lines.append(f"  Step {s.index} {s.filter_name}")
            for key, src in deps:
                lines.append(f"    <- step {src}: {_truncate(key, 60)}")
        else:
            lines.append(f"  Step {s.index} {s.filter_name}  (no upstream deps)")
    return "\n".join(lines)


def find_pipeline_by_name(vault_dir: Path, query: str) -> Optional[Pipeline]:
    """Look up a pipeline by case-insensitive substring match on filename.
    Only scans the `in/` folder so modified copies in `out/` don't show up.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    in_dir = vault_pipelines_in_dir(vault_dir)
    matches: List[Pipeline] = []
    for pl in scan_pipelines(in_dir):
        if q in pl.name.lower():
            matches.append(pl)
    if not matches:
        return None
    # Prefer exact filename match, otherwise shortest name
    exact = [m for m in matches if m.name.lower() == q]
    if exact:
        return exact[0]
    return min(matches, key=lambda m: len(m.name))
