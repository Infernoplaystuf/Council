"""
nx_worker.py — the DREAM3D-NX side of the bridge.

    <nxpython>/python.exe nx_worker.py <job.json> <result.json>

RUNS IN THE nx CONDA ENV, NOT THE APP ENV. simplnx is a compiled pybind11
package pinned to its own Python (3.12, bluequartzsoftware channel); importing
it in the Tkinter app's interpreter means fighting ABI conflicts forever and
pinning the whole app to that Python. So the app hands over a JSON job and
reads a JSON result, and nothing in this file may import an app module.

The result goes to a FILE, not stdout, on purpose: simplnx writes a
preferences warning to stderr on import, and the C++ filters emit progress
messages of their own. A stdout protocol would work today and break the first
time a filter printed something.

Everything here is grounded in nx_introspect's catalog of the INSTALLED binary
— see that module for what the docs get wrong. In particular:
  * A filter has NO .parameters(); parameters come from the execute docstring.
  * There is no read_key/write_key convention. File-path parameters are
    DISCOVERED per filter ('import_file_path' exists on no filter at all), and
    ReadDREAM3DFilter takes no path — it takes a Dream3dImportParameter.
    ImportData compound whose .file_path must be mutated instead.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nx_introspect  # noqa: E402  (same directory; pure stdlib)


# ============================================================
# parameter discovery
# ============================================================

def _execute_params(filter_obj) -> list:
    """The filter's real parameters, from pybind11's execute docstring."""
    doc = getattr(type(filter_obj).execute, "__doc__", None) \
        or getattr(filter_obj.execute, "__doc__", None)
    return nx_introspect.parse_execute_signature(doc).get("params", [])


def path_params(filter_obj) -> dict:
    """{param_name: kind} for the filter's file-path parameters.

    kind is 'path' for a plain os.PathLike, or 'import_data' for a
    Dream3dImportParameter.ImportData compound (which holds .file_path).

    Discovered, never assumed: the real keys are all over the place —
    input_file, input_file_path, stl_file_path, input_header_file,
    vg_header_file, output_file, output_path, export_file_path,
    feature_data_file — and the spec's 'import_file_path' exists on no filter.
    """
    out = {}
    for p in _execute_params(filter_obj):
        t = (p.get("type") or "")
        if "os.PathLike" in t:
            out[p["name"]] = "path"
        elif "ImportData" in t:
            out[p["name"]] = "import_data"
    return out


def _attr(obj, name: str, default=""):
    """Read ``name`` whether the binding exposes it as a method or a property.

    The API is inconsistent about this: Pipeline.name is a plain str attribute
    while PipelineFilter.name() is a method. Calling the wrong one is a
    TypeError, so never assume which."""
    try:
        v = getattr(obj, name)
    except Exception:
        return default
    try:
        return v() if callable(v) else v
    except Exception:
        return default


def _names(pf) -> str:
    return f"{_attr(pf, 'name')} {_attr(pf, 'human_name')}".lower()


def _is_reader(pf) -> bool:
    n = _names(pf)
    return "read" in n or "import" in n


def _is_writer(pf) -> bool:
    n = _names(pf)
    return "write" in n or "export" in n


def _describe(pipeline) -> list:
    """Every step: index, name, whether it reads/writes, and its path params."""
    steps = []
    for i in range(pipeline.size()):
        pf = pipeline[i]
        try:
            filt = pf.get_filter()
            pp = path_params(filt)
        except Exception as exc:
            pp = {}
        steps.append({
            "index": i,
            "name": _attr(pf, "name"),
            "human_name": _attr(pf, "human_name"),
            "is_reader": _is_reader(pf),
            "is_writer": _is_writer(pf),
            "path_params": pp,
            "arg_keys": sorted(pf.get_args().keys()),
        })
    return steps


def _get_path(pf, key: str, kind: str):
    """The path a parameter currently points at, or None."""
    args = pf.get_args()
    if key not in args:
        return None
    v = args[key]
    if kind == "import_data":
        return getattr(v, "file_path", None)
    return v


def _under(root: Path, p) -> bool:
    """Is ``p`` inside ``root``? Resolved, so ../ cannot walk out."""
    try:
        rp = Path(str(p)).resolve()
        rr = Path(str(root)).resolve()
    except Exception:
        return False
    return rp == rr or rr in rp.parents


def _check_writers(pipeline, steps, redirected, write_root) -> None:
    """Every writer must target ``write_root``. Raises otherwise.

    Redirecting the reader and the writer is NOT enough. A pipeline can
    contain more than one writer, and only one gets redirected — the other
    keeps whatever path it was saved with, which for a model-generated
    pipeline is a path the model chose. Verified: a two-writer pipeline wrote
    into the read-only input area while the runner reported success, because
    the out_dir guard only governs where the RUNNER writes, not where a
    writer filter's own argument points.
    """
    if not write_root:
        return
    for s in steps:
        if not s["is_writer"]:
            continue
        for key, kind in (s["path_params"] or {}).items():
            if (s["index"], key) in redirected:
                continue
            cur = _get_path(pipeline[s["index"]], key, kind)
            if cur in (None, ""):
                continue
            if not _under(write_root, cur):
                raise ValueError(
                    f"step {s['index']} ({s['name']}) would write outside the "
                    f"allowed output area.\n  its {key!r} points at: {cur}\n"
                    f"  allowed root: {write_root}\n"
                    f"This pipeline is refused rather than run.")


def _set_path(pf, key: str, kind: str, value: str) -> bool:
    """Point one path parameter at ``value``. True if it took."""
    args = pf.get_args()
    if key not in args:
        return False
    if kind == "import_data":
        # A compound: swap the file_path on it, leave data_paths/policy alone.
        obj = args[key]
        try:
            obj.file_path = str(value)
        except Exception:
            return False
        args[key] = obj
    else:
        args[key] = str(value)
    pf.set_args(args)
    return True


# ============================================================
# handlers
# ============================================================

def h_ping(job) -> dict:
    import simplnx as nx
    mods = {}
    for m in ("simplnx", "orientationanalysis", "itkimageprocessing"):
        try:
            __import__(m)
            mods[m] = True
        except Exception:
            mods[m] = False
    return {"python": sys.version.split()[0], "modules": mods,
            "has_pipeline": hasattr(nx, "Pipeline")}


def h_catalog(job) -> dict:
    return nx_introspect.catalog()


def h_describe(job) -> dict:
    """What a .d3dpipeline contains, and where its file paths live."""
    import simplnx as nx
    p = nx.Pipeline.from_file(str(job["pipeline"]))
    return {"name": _attr(p, "name"), "size": p.size(), "steps": _describe(p)}


def h_run_folder(job) -> dict:
    """Mode 1 — run a pipeline over every file in a folder.

    Load the pipeline fresh per file and mutate ONLY the I/O paths, which is
    the documented Tutorial-2 path and keeps simplnx's own preflight as the
    gate on every run.
    """
    import simplnx as nx

    pipeline_path = Path(job["pipeline"])
    in_dir = Path(job["in_dir"])
    out_dir = Path(job["out_dir"])
    pattern = job.get("glob", "*.dream3d")
    read_index = job.get("read_index")
    write_index = job.get("write_index")
    out_suffix = job.get("out_suffix", "_out.dream3d")
    limit = int(job.get("limit", 0)) or None
    # Every writer in the pipeline must land inside this, not just the one the
    # runner redirects. The bridge always supplies it.
    write_root = job.get("write_root") or job.get("out_dir")

    out_dir.mkdir(parents=True, exist_ok=True)
    srcs = sorted(q for q in in_dir.glob(pattern) if q.is_file())
    if limit:
        srcs = srcs[:limit]

    runs = []
    for src in srcs:
        rec = {"file": str(src), "ok": False, "errors": [], "warnings": 0,
               "read_set": None, "write_set": None}
        try:
            p = nx.Pipeline.from_file(str(pipeline_path))
            steps = _describe(p)

            # Resolve the reader: an explicit index, else the first step that
            # both looks like a reader and actually has a path parameter.
            r_idx = read_index
            if r_idx is None:
                cands = [s for s in steps if s["is_reader"] and s["path_params"]]
                r_idx = cands[0]["index"] if cands else None
            if r_idx is None:
                raise ValueError(
                    "No reader with a file-path parameter found in this "
                    "pipeline; pass read_index explicitly.")
            r_step = steps[r_idx]
            if not r_step["path_params"]:
                raise ValueError(
                    f"Step {r_idx} ({r_step['name']}) has no file-path "
                    "parameter to point at the input file.")
            r_key, r_kind = next(iter(r_step["path_params"].items()))
            if not _set_path(p[r_idx], r_key, r_kind, str(src)):
                raise ValueError(f"Could not set {r_key!r} on step {r_idx}.")
            rec["read_set"] = {"index": r_idx, "key": r_key, "kind": r_kind}
            redirected = {(r_idx, r_key)}

            # Resolve the writer the same way (last matching step).
            w_idx = write_index
            if w_idx is None:
                cands = [s for s in steps if s["is_writer"] and s["path_params"]]
                w_idx = cands[-1]["index"] if cands else None
            if w_idx is not None:
                w_step = steps[w_idx]
                if w_step["path_params"]:
                    w_key, w_kind = next(iter(w_step["path_params"].items()))
                    dest = out_dir / (src.stem + out_suffix)
                    if _set_path(p[w_idx], w_key, w_kind, str(dest)):
                        rec["write_set"] = {"index": w_idx, "key": w_key,
                                            "dest": str(dest)}
                        redirected.add((w_idx, w_key))

            # Refuse the whole run if ANY other writer aims outside the
            # allowed output area — checked BEFORE execute, so nothing is
            # written at all.
            _check_writers(p, steps, redirected, write_root)

            result = p.execute(nx.DataStructure())
            errs = [str(e) for e in getattr(result, "errors", [])]
            rec["errors"] = errs
            rec["warnings"] = len(list(getattr(result, "warnings", [])))
            rec["ok"] = not errs
        except Exception as exc:
            rec["errors"] = [f"{type(exc).__name__}: {exc}"]
        runs.append(rec)

    return {"total": len(srcs), "ok": sum(1 for r in runs if r["ok"]),
            "failed": sum(1 for r in runs if not r["ok"]), "runs": runs}


def h_preflight(job) -> dict:
    """Per-filter ARGUMENT validation for a saved pipeline.

    What this is NOT: a whole-pipeline dry-run. There is no pipeline-level
    preflight in this build — nx.Pipeline exposes none — and
    IFilter.preflight2 does NOT propagate the data structure (it comes back
    empty), so preflighting step 2 cannot see what step 1 would have created.
    A later step that consumes an earlier step's output will therefore report
    a missing path here even when the pipeline is fine.

    So: errors here are real for step 0 and for any step whose inputs already
    exist, and 'missing path' errors on later steps are inconclusive. The
    reliable gate is a limit=1 trial run (run_folder), where simplnx executes
    and reports its own errors. This is reported honestly rather than dressed
    up as a dry-run.
    """
    import simplnx as nx
    p = nx.Pipeline.from_file(str(job["pipeline"]))
    ds = nx.DataStructure()
    steps = []
    for i in range(p.size()):
        pf = p[i]
        rec = {"index": i, "name": _attr(pf, "human_name"), "errors": []}
        try:
            filt = pf.get_filter()
            args = {k: v for k, v in pf.get_args().items()
                    if k != "parameters_version"}
            res = filt.preflight2(ds, **args)
            # get_result() returns a LIST of errors ([] == valid), not a
            # Result object with .errors.
            rec["errors"] = [str(e) for e in (res.get_result() or [])]
        except Exception as exc:
            rec["errors"] = [f"{type(exc).__name__}: {exc}"]
        rec["ok"] = not rec["errors"]
        steps.append(rec)
    return {"steps": steps,
            "first_step_ok": steps[0]["ok"] if steps else False,
            "note": "per-filter argument check only; preflight does not "
                    "propagate the data structure, so 'missing path' on a "
                    "later step is inconclusive. Use a limit=1 run to be sure."}


HANDLERS = {
    "ping": h_ping,
    "catalog": h_catalog,
    "describe": h_describe,
    "preflight": h_preflight,
    "run_folder": h_run_folder,
}


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: nx_worker.py <job.json> <result.json>", file=sys.stderr)
        return 2
    job_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out = {"ok": False, "error": f"bad job file: {exc}"}
        out_path.write_text(json.dumps(out), encoding="utf-8")
        return 2
    action = job.get("action")
    try:
        fn = HANDLERS[action]
    except KeyError:
        out = {"ok": False,
               "error": f"unknown action {action!r}; "
                        f"known: {sorted(HANDLERS)}"}
        out_path.write_text(json.dumps(out), encoding="utf-8")
        return 2
    try:
        out = {"ok": True, "action": action, "result": fn(job)}
        code = 0
    except Exception as exc:
        out = {"ok": False, "action": action,
               "error": f"{type(exc).__name__}: {exc}",
               "traceback": traceback.format_exc()}
        code = 1
    out_path.write_text(json.dumps(out, indent=2, default=str),
                        encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
