"""
nx_introspect.py — dump a JSON catalog of every filter the INSTALLED
DREAM3D-NX / simplnx binary actually exposes.

    conda run -n nxpython python nx_introspect.py catalog.json
    (or: C:\\Users\\<you>\\miniconda3\\envs\\nxpython\\python.exe nx_introspect.py catalog.json)

RUNS IN THE nx ENV, NOT THE APP ENV. simplnx is a compiled pybind11 package
pinned to its own Python; importing it in the Tkinter app's interpreter means
fighting ABI conflicts forever. Nothing here may import an app module.

Why introspect instead of reading the docs:

  * The docs site omits bindings the binary has. dir() + duck-typing finds
    everything actually present.
  * Filter NAMING IS INCONSISTENT — the pipeline JSON says
    "simplnx::CreateDataArray" while the Python class is
    nx.CreateDataArrayFilter, and other filters match exactly. So the catalog
    records .uuid() on every filter: UUID is the only stable key to transpile
    against.
  * The Parameters object's API isn't reliably documented, so describe_params
    probes several access patterns AND keeps execute.__doc__ (pybind11 stashes
    the real call signature there). Where the object's own introspection is
    thin, the docstring signature still tells you the parameter names.

This file is the model's ONLY source of truth about simplnx. Regenerate it
whenever the nx env changes; nothing downstream may guess.
"""
from __future__ import annotations

import importlib
import json
import sys
import traceback

# (module, alias). Misses are recorded, not fatal — the optional plugins vary
# by install, and 'simplnxreview' is a guess.
CANDIDATE_MODULES = [
    ("simplnx", "nx"),
    ("orientationanalysis", "nxor"),
    ("itkimageprocessing", "nxitk"),
    ("simplnxreview", "nxrev"),
]


def looks_like_filter(obj) -> bool:
    """A filter duck-types as uuid + human_name + execute."""
    return all(hasattr(obj, a) for a in ("uuid", "human_name", "execute"))


def _split_top_level(s: str) -> list:
    """Split on commas that are NOT nested inside (), [], <> or quotes.

    Defaults in these signatures are full of commas that must not split:
        numeric_type_index: simplnx.NumericType = <NumericType.int32: 4>
        tuple_dimensions: list[list[float]] = [[0.0]]
    """
    parts, depth, quote, buf = [], 0, None, []
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def parse_execute_signature(doc: str) -> dict:
    """The REAL parameter list, parsed out of pybind11's execute docstring.

    This build exposes NO .parameters() on a filter (verified: every one of the
    289 raises AttributeError), so the docstring signature is not a fallback —
    it is the only machine-readable source of parameter names, types and
    defaults, e.g.

      execute(data_structure: simplnx.DataStructure, component_count: int = 1,
              output_array_path: simplnx.DataPath = DataPath('Data'), ...)
          -> simplnx.IFilter.ExecuteResult
    """
    out = {"params": [], "returns": None, "signature": None, "error": None}
    if not doc:
        out["error"] = "no docstring"
        return out
    line = doc.strip().splitlines()[0].strip()
    out["signature"] = line
    if "(" not in line:
        out["error"] = "no signature line"
        return out
    inner = line[line.index("(") + 1:]
    depth = 1
    end = None
    for i, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        out["error"] = "unbalanced signature"
        return out
    args_str, tail = inner[:end], inner[end + 1:]
    if "->" in tail:
        out["returns"] = tail.split("->", 1)[1].strip()
    for raw in _split_top_level(args_str):
        name, typ, default = raw, None, None
        if "=" in raw:
            head, default = raw.split("=", 1)
            head, default = head.strip(), default.strip()
        else:
            head = raw.strip()
        if ":" in head:
            name, typ = head.split(":", 1)
            name, typ = name.strip(), typ.strip()
        else:
            name = head
        out["params"].append({
            "name": name,
            "type": typ,
            "default": default,
            "required": default is None and name != "data_structure",
        })
    return out


def describe_params(inst) -> dict:
    """Record whether the documented .parameters() API exists at all.

    The spec assumed inst.parameters() and probed accessors on it. On this
    install it does not exist on ANY filter, so this only records that fact;
    parse_execute_signature() is where the real parameters come from."""
    out = {"has_parameters_method": hasattr(inst, "parameters"),
           "accessor": None, "items": []}
    if not out["has_parameters_method"]:
        return out
    try:
        params = inst.parameters()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for accessor in ("get_keys", "keys", "get_parameter_keys", "names"):
        if hasattr(params, accessor):
            try:
                out["items"] = [str(k) for k in getattr(params, accessor)()]
                out["accessor"] = accessor
                break
            except Exception:
                continue
    return out


def catalog() -> dict:
    result = {
        "python": sys.version,
        "modules_loaded": [],
        "modules_missing": [],
        "filters": [],
        # The two things the spec says to confirm from real data rather than
        # memory: how a pipeline is executed, and what it returns.
        "pipeline_api": {},
    }
    for mod_name, alias in CANDIDATE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            result["modules_missing"].append(
                {"module": mod_name, "error": f"{type(e).__name__}: {e}"})
            continue
        result["modules_loaded"].append(mod_name)
        for attr in dir(mod):
            try:
                obj = getattr(mod, attr)
            except Exception:
                continue
            if not looks_like_filter(obj):
                continue
            entry = {"module": mod_name, "alias": alias, "py_attr": attr}
            try:
                inst = obj()          # most metadata methods need an instance
            except Exception:
                inst = obj
            for meth in ("uuid", "human_name", "name", "class_name",
                         "default_tags"):
                try:
                    v = getattr(inst, meth)()
                    entry[meth] = (list(v) if meth == "default_tags"
                                   else str(v))
                except Exception:
                    entry[meth] = None
            try:
                entry["parameters_version"] = inst.parameters_version()
            except Exception:
                entry["parameters_version"] = None
            entry["params_api"] = describe_params(inst)
            try:
                doc = getattr(obj.execute, "__doc__", None)
            except Exception:
                doc = None
            entry["execute_doc"] = doc
            entry["execute"] = parse_execute_signature(doc)
            result["filters"].append(entry)

    # Probe the pipeline surface itself.
    try:
        import simplnx as nx
        api = result["pipeline_api"]
        api["module_level"] = [d for d in dir(nx)
                               if "pipeline" in d.lower()
                               or "execute" in d.lower()][:40]
        if hasattr(nx, "Pipeline"):
            api["Pipeline_dir"] = [d for d in dir(nx.Pipeline)
                                   if not d.startswith("_")]
            for m in ("execute", "from_file", "to_file"):
                f = getattr(nx.Pipeline, m, None)
                api[f"Pipeline.{m}.__doc__"] = getattr(f, "__doc__", None)
        for cls in ("DataStructure", "IFilter", "Result"):
            if hasattr(nx, cls):
                api[f"{cls}_dir"] = [d for d in dir(getattr(nx, cls))
                                     if not d.startswith("_")][:40]
    except Exception as e:
        result["pipeline_api"]["error"] = f"{type(e).__name__}: {e}"
    return result


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"
    try:
        cat = catalog()
    except Exception:
        cat = {"fatal": traceback.format_exc()}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, default=str)
    n = len(cat.get("filters", []))
    print(f"wrote {out_path}: {n} filters, "
          f"loaded={cat.get('modules_loaded')}, "
          f"missing={[m['module'] for m in cat.get('modules_missing', [])]}")
