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

# Bumped whenever the catalog gains a field something downstream relies on.
# The app refuses a cached catalog with a lower number and builds a fresh one
# (council_core.nx_ops.catalog via nx_bridge.catalog_stale_reason).
#   1  filters, enums, pipeline_api
#   2  + module_names, filter_attrs, classes, dataobject_attrs: what a
#      model-written script is checked against (nx_ground) and what the
#      transpiler builds compound parameter values from (nx_transpile)
CATALOG_SCHEMA = 2

# Classes a script or a compound value touches that no filter signature names
# directly: thresholds sit in an ArrayThresholdSet typed as IArrayThreshold,
# and ds / an execute() result / a DataPath are what every script handles.
EXTRA_CLASSES = (
    "simplnx.DataStructure",
    "simplnx.DataPath",
    "simplnx.ArrayThreshold",
    "simplnx.IFilter.ExecuteResult",
)


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
            # No default = the call fails without it. On the installed build
            # that is data_structure and nothing else (the other 1972
            # parameters all have one). It used to exclude data_structure as
            # well, which made the flag False on every parameter of every
            # filter, so every check built on it was dead.
            "required": default is None,
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


def collect_enums(mod, mod_name: str, out: dict) -> None:
    """Every enum's int -> member-name map, including enums nested one level
    inside parameter classes (Dream3dImportParameter.PathImportPolicy).

    The transpiler needs these: a saved pipeline stores an enum as its INTEGER
    (numeric_type_index is 8, not 'float32'), so rendering runnable Python
    means mapping 8 back to NumericType.float32."""
    def members(cls):
        try:
            return {int(m.value): m.name for m in cls.__members__.values()}
        except Exception:
            return None

    for attr in dir(mod):
        if attr.startswith("_"):
            continue
        try:
            obj = getattr(mod, attr)
        except Exception:
            continue
        if not isinstance(obj, type):
            continue
        m = members(obj)
        if m:
            out[f"{mod_name}.{attr}"] = m
            continue
        # Nested enums (Dream3dImportParameter.PathImportPolicy) via vars(),
        # NOT dir()+getattr(). getattr on a pybind11 class attribute HARD
        # CRASHES this interpreter — probing simplnx.BoolArray kills the
        # process with no traceback (exit 127), taking the catalog with it.
        # vars() hands back the raw entry without invoking any descriptor.
        try:
            raw = dict(vars(obj))
        except Exception:
            continue
        for sub, so in raw.items():
            if sub.startswith("_") or not isinstance(so, type):
                continue
            sm = members(so)
            if sm:
                out[f"{mod_name}.{attr}.{sub}"] = sm


def _return_type(doc) -> str:
    """'(self: X) -> list[str]' -> 'list[str]'."""
    line = (doc or "").strip().splitlines()[0] if (doc or "").strip() else ""
    return line.split("->", 1)[1].strip() if "->" in line else ""


def _init_signatures(cls) -> list:
    """Every __init__ overload's parameters (self dropped), from pybind11's
    docstring. [] when the docstring carries no signature."""
    try:
        doc = cls.__init__.__doc__ or ""
    except Exception:
        return []
    plain, numbered = [], []
    for line in doc.splitlines():
        s = line.strip()
        bucket = plain
        if s[:1].isdigit() and ". " in s:
            s = s.split(". ", 1)[1]          # '2. __init__(...)' overloads
            bucket = numbered
        if not s.startswith("__init__("):
            continue
        params = parse_execute_signature(s).get("params", [])
        bucket.append([p for p in params if p["name"] != "self"])
    # An overloaded constructor's docstring opens with a generic
    # '__init__(*args, **kwargs)' line; the numbered ones are the real ones.
    return numbered or plain


def describe_class(cls) -> dict:
    """A parameter class as a script has to use it: its constructor
    overloads, its properties (with type and whether they can be set) and its
    methods. Walks the MRO, so ArrayThresholdSet shows the inverted / union_op
    it inherits from IArrayThreshold.

    vars(), never getattr: see collect_enums for the pybind11 class attribute
    that kills this interpreter when read with getattr."""
    props, readonly, methods, dunders = {}, [], [], set()
    try:
        mro = [k for k in cls.__mro__
               if k.__name__ not in ("object", "pybind11_object")]
    except Exception:
        mro = [cls]
    for k in mro:
        try:
            items = dict(vars(k))
        except Exception:
            continue
        # Which protocols it supports: ds[path] reads (getitem) but
        # ds[path] = ... does not exist (no setitem) — a model wrote that.
        dunders |= {n for n in items
                    if n in ("__getitem__", "__setitem__", "__delitem__",
                             "__iter__", "__len__", "__contains__")}
        for name, v in items.items():
            if name.startswith("_") or name in props or name in methods:
                continue
            if isinstance(v, property):
                props[name] = _return_type(getattr(v.fget, "__doc__", ""))
                if v.fset is None:
                    readonly.append(name)
            elif isinstance(v, type):
                continue                     # a nested enum or class
            elif callable(v) or isinstance(v, (staticmethod, classmethod)):
                methods.append(name)
    bases = []
    for k in mro[1:]:
        try:
            bases.append(f"{k.__module__}.{k.__qualname__}")
        except Exception:
            continue
    return {"init": _init_signatures(cls), "props": props,
            "readonly": sorted(readonly), "methods": sorted(methods),
            # An ArrayThreshold goes where an IArrayThreshold is asked for.
            "bases": bases, "dunders": sorted(dunders)}


def _resolve(modules: dict, path: str):
    """'simplnx.CalculatorParameter.ValueType' -> the class, or None."""
    parts = path.split(".")
    obj = modules.get(parts[0])
    for i, part in enumerate(parts[1:]):
        if obj is None:
            return None
        try:
            obj = (getattr(obj, part, None) if i == 0
                   else dict(vars(obj)).get(part))
        except Exception:
            return None
    return obj if isinstance(obj, type) else None


def _class_refs(type_str: str) -> list:
    """Dotted class names inside a type string: 'list[simplnx.X.ValueType]'
    -> ['simplnx.X.ValueType']."""
    import re
    return re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", type_str or "")


def collect_classes(modules: dict, result: dict) -> dict:
    """Every non-enum class a filter parameter is typed as, plus EXTRA_CLASSES,
    plus whatever their properties are typed as, described by describe_class.

    The transpiler builds compound values (ReadCSVDataParameter,
    ArrayThresholdSet, CalculatorParameter.ValueType...) from this, and the
    script checker checks a model's attribute assignments against it. Without
    it both used to emit a dict, which simplnx refuses at run time."""
    enums = result.get("enums", {})
    todo = list(EXTRA_CLASSES)
    for f in result.get("filters", []):
        for p in (f.get("execute") or {}).get("params", []):
            todo.extend(_class_refs(p.get("type")))
    out = {}
    while todo:
        path = todo.pop(0)
        if path in out or path in enums \
                or path.split(".")[0] not in modules:
            continue
        cls = _resolve(modules, path)
        if cls is None:
            continue
        out[path] = describe_class(cls)
        for t in out[path]["props"].values():
            todo.extend(_class_refs(t))
    return out


def dataobject_attrs(nx) -> list:
    """Every public attribute of every simplnx DataObject class (ImageGeom,
    AttributeMatrix, the DataArray types, ...): what `ds[path].<attr>` can
    possibly be, whatever is stored at the path. vars() over the MRO, never
    getattr (see collect_enums)."""
    if nx is None:
        return []
    base = dict(vars(nx)).get("DataObject")
    names = set()
    for obj in dict(vars(nx)).values():
        if not isinstance(obj, type):
            continue
        try:
            mro = obj.__mro__
        except Exception:
            continue
        if base is None or base not in mro:
            continue
        for k in mro:
            try:
                names |= {n for n in dict(vars(k)) if not n.startswith("_")}
            except Exception:
                continue
    return sorted(names)


def catalog() -> dict:
    result = {
        "catalog_schema": CATALOG_SCHEMA,
        "python": sys.version,
        "modules_loaded": [],
        "modules_missing": [],
        "enums": {},
        "filters": [],
        # Every public name each module exports: a script that writes
        # nx.ImageGeometry (the real one is ImageGeom) is told so before it
        # runs, instead of dying with AttributeError after it was "accepted".
        "module_names": {},
        # What a filter CLASS exposes (execute, uuid, ...). The same binding
        # backs every filter.
        "filter_attrs": [],
        "classes": {},
        # The two things the spec says to confirm from real data rather than
        # memory: how a pipeline is executed, and what it returns.
        "pipeline_api": {},
    }
    modules = {}
    for mod_name, alias in CANDIDATE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            result["modules_missing"].append(
                {"module": mod_name, "error": f"{type(e).__name__}: {e}"})
            continue
        modules[mod_name] = mod
        result["modules_loaded"].append(mod_name)
        result["module_names"][mod_name] = sorted(
            d for d in dir(mod) if not d.startswith("_"))
        collect_enums(mod, mod_name, result["enums"])
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
            if not result["filter_attrs"]:
                result["filter_attrs"] = sorted(
                    d for d in dir(obj) if not d.startswith("_"))

    try:
        result["classes"] = collect_classes(modules, result)
    except Exception as e:
        result["classes_error"] = f"{type(e).__name__}: {e}"
    try:
        result["dataobject_attrs"] = dataobject_attrs(modules.get("simplnx"))
    except Exception as e:
        result["dataobject_attrs_error"] = f"{type(e).__name__}: {e}"

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
