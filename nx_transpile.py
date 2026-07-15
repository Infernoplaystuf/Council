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


def _enum_member(enums: dict, type_str: str, value: Any) -> Optional[str]:
    """'simplnx.NumericType' + 8 -> 'nx.NumericType.float32'."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    for key, members in (enums or {}).items():
        # key looks like 'simplnx.NumericType'; type_str like 'simplnx.NumericType'
        short = key.split(".", 1)[-1]
        if type_str.endswith(short):
            name = members.get(value) or members.get(str(value))
            if name:
                return f"nx.{short}.{name}"
    return None


def _py_str(s: str) -> str:
    """A string literal for a path.

    Plain repr(), NOT an r-prefix: repr() already escapes each backslash, so
    r + repr() produces r'C:\\\\Users' — a raw literal holding DOUBLED
    backslashes, i.e. a path that does not exist. repr() alone round-trips."""
    return repr(str(s))


def render_value(value: Any, type_str: str, enums: dict) -> Tuple[str, bool]:
    """(python source, ok). ok=False means it needs a human's eyes."""
    t = type_str or ""
    if "DataPath" in t and "list" not in t:
        if isinstance(value, str):
            return f"nx.DataPath({value!r})", True
        if isinstance(value, list):
            return f"nx.DataPath({'/'.join(map(str, value))!r})", True
    if "list[simplnx.DataPath]" in t or ("DataPath" in t and "list" in t):
        if isinstance(value, list):
            inner = ", ".join(f"nx.DataPath({str(x)!r})" for x in value)
            return f"[{inner}]", True
    if "ImportData" in t and isinstance(value, dict):
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
            m = _enum_member(enums, "Dream3dImportParameter.PathImportPolicy",
                             pol)
            parts.append(f"path_import_policy={m}" if m
                         else f"path_import_policy={pol!r}")
        return ("nx.Dream3dImportParameter.ImportData("
                + ", ".join(parts) + ")"), True
    member = _enum_member(enums, t, value)
    if member:
        return member, True
    if "os.PathLike" in t and isinstance(value, str):
        return _py_str(value), True
    if isinstance(value, (bool, int, float, str, list, dict)) or value is None:
        return repr(value), True
    return repr(value), False


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
        filt = step.get("filter") or {}
        uuid = filt.get("uuid")
        jname = filt.get("name")
        if step.get("isDisabled"):
            body.append(f"# [{i}] DISABLED in the pipeline: {jname}")
            body.append("")
            continue
        if nx_policy.is_denied(uuid):
            # Rendering this as a live call would hand the user a script that
            # runs a shell. Emit it visibly instead — the transpiler's output
            # never passes through nx_generate.validate().
            body.append(f"# [{i}] REFUSED — {jname}")
            body.append(f"#      {nx_policy.reason(uuid)}")
            body.append(f"#      Its args were:")
            for k, v in (step.get("args") or {}).items():
                if k not in NON_PARAM_KEYS:
                    body.append(f"#        {k} = {unwrap(v)!r}")
            body.append("")
            warnings.append(f"step {i}: {jname} is not permitted "
                            f"and was commented out, not transpiled.")
            continue
        entry = idx.get(uuid)
        if entry is None:
            # Never guess: an unknown UUID is not in the installed binary.
            body.append(f"# [{i}] UNKNOWN FILTER — not in the installed "
                        f"package.")
            body.append(f"#      name: {jname}")
            body.append(f"#      uuid: {uuid}")
            body.append(f"#      Its args are preserved below for reference:")
            for k, v in (step.get("args") or {}).items():
                if k not in NON_PARAM_KEYS:
                    body.append(f"#        {k} = {unwrap(v)!r}")
            body.append("")
            unknown.append({"index": i, "uuid": uuid, "name": jname})
            continue

        alias = aliases.get(entry["module"], entry.get("alias") or "nx")
        used_modules.add(entry["module"])
        types = _param_types(entry)
        valid = set(types)
        rendered: List[str] = []
        for k, raw in sorted((step.get("args") or {}).items()):
            if k in NON_PARAM_KEYS:
                continue          # bookkeeping, not an execute() parameter
            v = unwrap(raw)
            if k not in valid:
                warnings.append(
                    f"step {i} ({entry['py_attr']}): arg {k!r} is not a "
                    f"parameter of this filter in the installed build — "
                    f"skipped.")
                continue
            src, ok = render_value(v, types.get(k, ""), enums)
            if not ok:
                warnings.append(
                    f"step {i} ({entry['py_attr']}): could not type {k!r} "
                    f"({types.get(k)}) — check this value.")
                src = f"{src}  # TODO: verify type {types.get(k)}"
            rendered.append(f"    {k}={src},")

        body.append(f"# [{i}] {entry.get('human_name') or entry['py_attr']}")
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
