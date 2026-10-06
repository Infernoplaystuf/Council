"""
nx_transpile.py — a .d3dpipeline (JSON) rendered as an editable Python script.

Runs in the APP env, not the nx env: it needs only the pipeline JSON and the
catalog produced by nx_introspect, so it is pure string work and testable
without simplnx installed.

Mapping is keyed on UUID, never on name. Two independent reasons, both from the
real install rather than the docs:
  * The Python class carries a 'Filter' suffix the JSON name may not.
  * The JSON name FORMAT drifts between versions — the file written by the
    current build says "nx::core::CreateDataArrayFilter" where the spec (and
    older files) say "simplnx::CreateDataArray". The UUID is stable across
    both.
A step whose UUID is not in the installed catalog becomes a clearly-marked
comment rather than silently-wrong code.

Two things the JSON does that a naive walk gets wrong:
  * Every arg is a VERSIONED ENVELOPE — {"value": 1, "version": 1} — not the
    value. Rendering repr() of the envelope emits component_count={'value': 1,
    'version': 1}, which is broken for every parameter of every filter.
  * args carries parameters_version, a bookkeeping int that is NOT an execute()
    parameter. Passing it through is a TypeError at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import nx_policy

# Present in a saved pipeline's args, but not a parameter of execute().
NON_PARAM_KEYS = {"parameters_version"}
# Never emit these as call kwargs: the renderer always supplies data_structure
# itself, so a pipeline carrying it would produce a duplicate keyword and a
# TypeError. validate() lets it through (it is an implicit arg), so the guard
# belongs here.
SKIP_ARG_KEYS = NON_PARAM_KEYS | {"data_structure"}


def _comment(text, limit: int = 300) -> str:
    """``text`` flattened so it cannot escape the comment it is written into.

    Everything a pipeline carries — filter name, uuid, arg keys and values — is
    attacker-controlled if the .d3dpipeline came from anywhere but this app,
    and it all gets interpolated into `# ...` lines. A newline in a filter name
    ends the comment and the rest becomes live code:

        # [0] UNKNOWN FILTER — not in the installed package.
        #      name: harmless
        import os
        os.system("...")            <- was inside filter.name

    Verified against the real renderer before this existed. So: collapse every
    newline/carriage return, and cap the length so a megabyte of junk cannot
    bury the real output.
    """
    s = str(text)
    s = s.replace("\r", " ").replace("\n", "\\n")
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def load_catalog(catalog: Any) -> dict:
    if isinstance(catalog, dict):
        return catalog
    return json.loads(Path(catalog).read_text(encoding="utf-8"))


def uuid_index(catalog: dict) -> Dict[str, dict]:
    """{uuid: filter entry}. UUID is the only stable key (see module docs)."""
    return {f["uuid"]: f for f in catalog.get("filters", []) if f.get("uuid")}


def unwrap(v: Any) -> Any:
    """The value inside a {"value": ..., "version": n} envelope."""
    if isinstance(v, dict) and "value" in v and "version" in v and len(v) == 2:
        return v["value"]
    return v


def _param_types(entry: dict) -> Dict[str, str]:
    return {p["name"]: (p.get("type") or "")
            for p in (entry.get("execute", {}) or {}).get("params", [])}


ALIASES = {"simplnx": "nx", "orientationanalysis": "nxor",
           "itkimageprocessing": "nxitk"}


def _enum_member(enums: dict, type_str: str, value: Any) -> Optional[str]:
    """'simplnx.NumericType' + 8 -> 'nx.NumericType.float32'.

    The exact key first; the suffix match is for a type string that omits the
    module. The alias is the enum's own module's — an orientationanalysis
    enum is nxor.<...>, not nx.<...>."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    items = list((enums or {}).items())
    exact = [(k, m) for k, m in items if k == type_str]
    for key, members in exact or items:
        # key looks like 'simplnx.NumericType'; type_str like 'simplnx.NumericType'
        mod, _, short = key.partition(".")
        if exact or type_str.endswith(short):
            name = members.get(value) or members.get(str(value))
            if name:
                return f"{ALIASES.get(mod, 'nx')}.{short}.{name}"
    return None


def _py_str(s: str) -> str:
    """A string literal for a path.

    Plain repr(), NOT an r-prefix: repr() already escapes each backslash, so
    r + repr() produces r'C:\\\\Users' — a raw literal holding DOUBLED
    backslashes, i.e. a path that does not exist. repr() alone round-trips."""
    return repr(str(s))


# ---- compound parameter values ---------------------------------------------
#
# A saved pipeline stores a compound parameter (ReadCSVDataParameter,
# ArrayThresholdSet, CalculatorParameter.ValueType, ...) as a JSON object. The
# renderer used to emit that object as a Python dict with ok=True and no
# warning, and the binding refuses a dict: 26 of the 67 pipelines the
# dream3dnx package ships died with "Unable to cast Python instance of type
# <class 'dict'>" before touching any data. They are now built the way the
# binding takes them — default-construct, then set each property (or, for a
# class with no default constructor, call the real one) — from the catalog's
# record of each class (nx_introspect.describe_class).
#
# Where a saved key is not just the property name with spaces as
# underscores. Verified on the installed build by comparing each shipped
# pipeline's argument as simplnx itself loads it (Pipeline.from_file ->
# get_args()) with the transpiled one: tests/data/dream3d_e2e/nx_args_equiv.py.
COMPOUND_KEY_MAP: Dict[str, Dict[str, str]] = {
    "simplnx.ReadCSVDataParameter": {
        "Data Types": "column_data_types", "Header Line": "headers_line",
        "Tuple Dimensions": "tuple_dims"},
    "orientationanalysis.ReadH5EbsdFileParameter.ValueType": {
        "hdf5_data_paths": "selected_array_names"},
    "simplnx.ArrayThresholdSet": {"union": "union_op"},
    "simplnx.ArrayThreshold": {"union": "union_op"},
}
# JSON bookkeeping, not a property: which concrete threshold a node is.
COMPOUND_SKIP_KEYS: Dict[str, set] = {
    "simplnx.ArrayThresholdSet": {"type"},
    "simplnx.ArrayThreshold": {"type"},
}
# A list typed as an abstract base holds concrete subclasses; the JSON says
# which by a discriminator key.
POLYMORPHIC: Dict[str, Tuple[str, Dict[str, str]]] = {
    "simplnx.IArrayThreshold": ("type", {
        "array": "simplnx.ArrayThreshold",
        "collection": "simplnx.ArrayThresholdSet"}),
}
# No default constructor: the JSON keys, in the constructor's order.
COMPOUND_CTOR_ARGS: Dict[str, Tuple[str, ...]] = {
    "simplnx.CalculatorParameter.ValueType": ("selected_group", "equation",
                                              "units"),
}


class _Renderer:
    """Renders one step's argument values. A compound value needs
    statements before the call (``prelude``); everything else is an
    expression. ``modules`` collects the modules the rendered source names,
    so the script imports them."""

    def __init__(self, catalog: dict):
        self.cat = catalog or {}
        self.enums = self.cat.get("enums") or {}
        self.classes = self.cat.get("classes") or {}
        self.prelude: List[str] = []
        self.modules: set = set()
        self.notes: List[str] = []

    def _alias(self, path: str) -> str:
        mod, _, rest = path.partition(".")
        self.modules.add(mod)
        return f"{ALIASES.get(mod, mod)}.{rest}"

    def value(self, value: Any, type_str: str, var: str) -> Tuple[str, bool]:
        """(python source, ok). ok=False means it needs a human's eyes."""
        import nx_ground
        t = (type_str or "").strip()
        kind, arg = nx_ground.classify(t, self.cat)
        if kind == "optional":
            if value is None:
                return "None", True
            return self.value(value, arg, var)
        if kind == "datapath" or ("DataPath" in t and "list" not in t):
            if isinstance(value, str):
                self.modules.add("simplnx")
                return f"nx.DataPath({value!r})", True
            if isinstance(value, list):
                self.modules.add("simplnx")
                return f"nx.DataPath({'/'.join(map(str, value))!r})", True
        if "ImportData" in t and isinstance(value, dict):
            return self._import_data(value), True
        if kind == "enum" or (kind == "any" and self.enums):
            member = _enum_member(self.enums, t, value)
            if member:
                alias = member.split(".", 1)[0]
                self.modules.add(next((m for m, a in ALIASES.items()
                                       if a == alias), "simplnx"))
                return member, True
        if kind == "list" and isinstance(value, list):
            parts, ok = [], True
            for i, x in enumerate(value):
                src, x_ok = self.value(x, arg, f"{var}_{i}")
                parts.append(src)
                ok = ok and x_ok
            return f"[{', '.join(parts)}]", ok
        if kind == "object" and isinstance(value, dict):
            return self._compound(value, arg, var)
        if kind == "path" and isinstance(value, str):
            return _py_str(value), True
        if kind == "object" or isinstance(value, dict):
            # A compound this build does not describe, or a value that is not
            # one: say so instead of emitting something the binding refuses.
            return repr(value), False
        if isinstance(value, (bool, int, float, str, list)) or value is None:
            return repr(value), True
        return repr(value), False

    def _import_data(self, value: dict) -> str:
        parts = []
        fp = value.get("file_path")
        if fp is not None:
            parts.append(f"file_path={_py_str(fp)}")
        dps = value.get("data_paths")
        if dps:
            inner = ", ".join(f"nx.DataPath({str(x)!r})" for x in dps)
            parts.append(f"data_paths=[{inner}]")
        pol = value.get("path_import_policy")
        if pol is not None:
            m = _enum_member(self.enums,
                             "simplnx.Dream3dImportParameter.PathImportPolicy",
                             pol)
            parts.append(f"path_import_policy={m}" if m
                         else f"path_import_policy={pol!r}")
        self.modules.add("simplnx")
        return ("nx.Dream3dImportParameter.ImportData("
                + ", ".join(parts) + ")")

    def _compound(self, value: dict, cls: str, var: str) -> Tuple[str, bool]:
        if cls in POLYMORPHIC:
            key, choices = POLYMORPHIC[cls]
            cls = choices.get(str(value.get(key)), cls)
        rec = self.classes.get(cls)
        if not rec:
            self.notes.append(f"{cls} is not described by the catalog, so "
                              f"its value cannot be built")
            return repr(value), False
        name = self._alias(cls)
        props = rec.get("props") or {}
        readonly = set(rec.get("readonly") or [])
        keymap = COMPOUND_KEY_MAP.get(cls, {})
        ok = True
        if cls in COMPOUND_CTOR_ARGS:
            args = []
            for key in COMPOUND_CTOR_ARGS[cls]:
                src, a_ok = self.value(value.get(key), props.get(key, ""),
                                       f"{var}_{key}")
                args.append(src)
                ok = ok and a_ok
            self.prelude.append(f"{var} = {name}({', '.join(args)})")
            return var, ok
        if [] not in (rec.get("init") or []):
            self.notes.append(f"{cls} has no default constructor this "
                              f"renderer knows how to call")
            return repr(value), False
        self.prelude.append(f"{var} = {name}()")
        skip = COMPOUND_SKIP_KEYS.get(cls, set())
        for key, raw in value.items():
            if key in skip or raw is None:
                continue        # bookkeeping, or unset: keep the default
            prop = keymap.get(key) or key.strip().lower().replace(" ", "_")
            if prop not in props or prop in readonly \
                    or not prop.isidentifier():
                self.notes.append(f"{cls} has no settable property for the "
                                  f"saved key {key!r}; it keeps its default")
                ok = False
                continue
            src, p_ok = self.value(raw, props[prop], f"{var}_{prop}")
            ok = ok and p_ok
            self.prelude.append(f"{var}.{prop} = {src}"
                                + ("" if p_ok else
                                   f"  # TODO: verify type {props[prop]}"))
        return var, ok


def render_value(value: Any, type_str: str, enums: dict,
                 catalog: Optional[dict] = None) -> Tuple[str, bool]:
    """(python source, ok) for one value as an EXPRESSION. ok=False means it
    needs a human's eyes — including a compound value, which needs
    statements (transpile() renders those)."""
    r = _Renderer(dict(catalog or {}, enums=enums or (catalog or {})
                       .get("enums") or {}))
    src, ok = r.value(value, type_str, "v")
    if r.prelude:
        return repr(value), False
    return src, ok


def transpile(pipeline: Any, catalog: Any) -> dict:
    """Render a .d3dpipeline as runnable Python.

    Returns {"code", "steps", "unknown", "warnings"}."""
    cat = load_catalog(catalog)
    idx = uuid_index(cat)
    enums = cat.get("enums", {})
    pj = pipeline if isinstance(pipeline, dict) else json.loads(
        Path(pipeline).read_text(encoding="utf-8"))
    steps = pj.get("pipeline") if isinstance(pj, dict) else pj
    steps = steps or []

    aliases = {"simplnx": "nx", "orientationanalysis": "nxor",
               "itkimageprocessing": "nxitk"}
    used_modules = set()
    body: List[str] = []
    unknown: List[dict] = []
    warnings: List[str] = []

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            body.append(f"# [{i}] SKIPPED — not an object")
            body.append("")
            continue
        filt = step.get("filter") or {}
        if not isinstance(filt, dict):
            filt = {}
        uuid = filt.get("uuid")
        if not isinstance(uuid, (str, type(None))):
            uuid = str(uuid)      # a list/dict uuid must not reach idx.get()
        jname = filt.get("name")
        if step.get("isDisabled"):
            body.append(f"# [{i}] DISABLED in the pipeline: {_comment(jname)}")
            body.append("")
            continue
        if nx_policy.is_denied(uuid):
            # Rendering this as a live call would hand the user a script that
            # runs a shell. Emit it visibly instead — the transpiler's output
            # never passes through nx_generate.validate().
            body.append(f"# [{i}] REFUSED — {_comment(jname)}")
            body.append(f"#      {_comment(nx_policy.reason(uuid))}")
            body.append(f"#      Its args were:")
            for k, v in (step.get("args") or {}).items():
                if k not in NON_PARAM_KEYS:
                    body.append(f"#        {_comment(k, 80)} = "
                                f"{_comment(repr(unwrap(v)))}")
            body.append("")
            warnings.append(f"step {i}: {jname} is not permitted "
                            f"and was commented out, not transpiled.")
            continue
        entry = idx.get(uuid)
        if entry is None:
            # Never guess: an unknown UUID is not in the installed binary.
            body.append(f"# [{i}] UNKNOWN FILTER — not in the installed "
                        f"package.")
            body.append(f"#      name: {_comment(jname)}")
            body.append(f"#      uuid: {_comment(uuid, 80)}")
            body.append(f"#      Its args are preserved below for reference:")
            for k, v in (step.get("args") or {}).items():
                if k not in NON_PARAM_KEYS:
                    body.append(f"#        {_comment(k, 80)} = "
                                f"{_comment(repr(unwrap(v)))}")
            body.append("")
            unknown.append({"index": i, "uuid": uuid, "name": jname})
            continue

        alias = aliases.get(entry["module"], entry.get("alias") or "nx")
        used_modules.add(entry["module"])
        types = _param_types(entry)
        valid = set(types)
        rendered: List[str] = []
        renderer = _Renderer(cat)
        for k, raw in sorted((step.get("args") or {}).items()):
            if k in SKIP_ARG_KEYS:
                continue      # bookkeeping, or supplied by the renderer itself
            v = unwrap(raw)
            if k not in valid:
                warnings.append(
                    f"step {i} ({entry['py_attr']}): arg {k!r} is not a "
                    f"parameter of this filter in the installed build — "
                    f"skipped.")
                continue
            # k is a real parameter name (checked above), so the variable a
            # compound value is built in is a plain identifier.
            renderer.notes = []
            src, ok = renderer.value(v, types.get(k, ""), f"v{i}_{k}")
            if not ok:
                why = "; ".join(dict.fromkeys(renderer.notes))
                warnings.append(
                    f"step {i} ({entry['py_attr']}): could not type {k!r} "
                    f"({types.get(k)}) — check this value."
                    + (f" ({why})" if why else ""))
                src = f"{src}  # TODO: verify type {types.get(k)}"
            rendered.append(f"    {k}={src},")
        used_modules |= renderer.modules

        body.append(f"# [{i}] "
                    f"{_comment(entry.get('human_name') or entry['py_attr'], 80)}")
        body.extend(renderer.prelude)
        body.append(f"r{i} = {alias}.{entry['py_attr']}.execute(")
        body.append("    data_structure=ds,")
        body.extend(rendered)
        body.append(")")
        body.append(f"assert not r{i}.errors, r{i}.errors")
        body.append("")

    header = ["# Generated from a .d3dpipeline by nx_transpile.",
              "# Filters are resolved by UUID against the INSTALLED package,",
              "# so this matches the binary you have, not the docs.",
              "#",
              "# Run with the nx env's interpreter:",
              "#   conda run -n nxpython python this_script.py",
              ""]
    for mod in ("simplnx", "orientationanalysis", "itkimageprocessing"):
        if mod in used_modules:
            header.append(f"import {mod} as {aliases[mod]}")
    if "simplnx" not in used_modules:
        header.append("import simplnx as nx")
    header += ["", "ds = nx.DataStructure()", ""]

    return {"code": "\n".join(header + body).rstrip() + "\n",
            "steps": len(steps), "unknown": unknown, "warnings": warnings}
