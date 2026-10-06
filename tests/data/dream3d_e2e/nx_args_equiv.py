"""Does a transpiled script hand each filter the SAME arguments simplnx itself
loads from the .d3dpipeline? Runs in the nx env; touches no data.

    <nxpython> nx_args_equiv.py <pipeline.d3dpipeline> <transpiled.py> [...]

simplnx's own Pipeline.from_file parses every argument (compound ones
included) through the parameter's JSON reader; get_args() hands them back as
Python objects. The transpiled script is executed with each
`rN = <alias>.<Filter>.execute(` line rewritten to capture its keyword
arguments instead of running the filter, so the objects its prelude builds
(ReadCSVDataParameter, ArrayThresholdSet, ...) are compared property by
property with simplnx's. Nothing executes a filter, so no input file is
needed — which is the point: the dream3dnx package ships the pipelines but
not their data.

Takes any number of (pipeline, script) pairs, one interpreter for all of
them, and prints one JSON line per pair: {"pipeline", "steps", "compared",
"different": [...], "missing": [...], "error"}. "missing" is an argument
simplnx filled in that the script leaves to execute()'s default.
"""
import json
import math
import re
import sys
from pathlib import Path

import simplnx as nx
import orientationanalysis  # noqa: F401  (registers its filters)
import itkimageprocessing   # noqa: F401


def norm(v, depth=0):
    if depth > 8:
        return "<deep>"
    if isinstance(v, nx.DataPath):
        p = v.parts
        return ["DataPath", list(p() if callable(p) else p)]
    t = type(v)
    if hasattr(t, "__members__") and hasattr(v, "value"):
        return [t.__name__, int(v.value)]
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return round(v, 6) if math.isfinite(v) else str(v)
    if isinstance(v, Path):
        return ["path", v.as_posix()]
    if isinstance(v, (list, tuple)):
        return [norm(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {k: norm(x, depth + 1) for k, x in v.items()}
    props = {}
    for k in t.__mro__:
        try:
            items = dict(vars(k))
        except TypeError:
            continue
        for name, d in items.items():
            if isinstance(d, property) and not name.startswith("_") \
                    and name not in props:
                try:
                    props[name] = norm(getattr(v, name), depth + 1)
                except Exception as exc:          # noqa: BLE001
                    props[name] = f"<{type(exc).__name__}>"
    return [t.__name__, props]


def same(a, b):
    """str and os.PathLike compare by text: a path parameter loaded from JSON
    is a pathlib.Path, the script passes the same text as a str."""
    if isinstance(a, list) and len(a) == 2 and a[0] == "path":
        a = a[1]
    if isinstance(b, list) and len(b) == 2 and b[0] == "path":
        b = b[1]
    if isinstance(a, str) and isinstance(b, str):
        return Path(a).as_posix() == Path(b).as_posix() or a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b


def compare(pipe: Path, script: Path) -> dict:
    out = {"pipeline": str(pipe), "steps": 0, "compared": 0,
           "different": [], "missing": [], "error": None}
    try:
        native = nx.Pipeline.from_file(str(pipe))
        out["steps"] = native.size()
        src = script.read_text(encoding="utf-8")
        src = re.sub(r"^(r\d+) = \w+\.\w+\.execute\(",
                     lambda m: f"{m.group(1)} = _cap({m.group(1)[1:]}, ",
                     src, flags=re.M)
        src = re.sub(r"^assert not r\d+\.errors.*$", "", src, flags=re.M)
        captured = {}

        def _cap(i, **kw):
            captured[i] = kw
            return None
        exec(compile(src, str(script), "exec"),
             {"_cap": _cap, "__name__": "__nx_args_equiv__"})
        for i in range(native.size()):
            if i not in captured:
                continue          # disabled / refused / unknown in the script
            args = native[i].get_args()
            for k, v in args.items():
                if k == "parameters_version":
                    continue
                if k not in captured[i]:
                    out["missing"].append(f"step {i} {k}")
                    continue
                out["compared"] += 1
                a, b = norm(v), norm(captured[i][k])
                if not same(a, b):
                    out["different"].append(
                        {"step": i, "arg": k, "native": a, "script": b})
    except Exception as exc:                              # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def main():
    args = sys.argv[1:]
    for pipe, script in zip(args[0::2], args[1::2]):
        print(json.dumps(compare(Path(pipe), Path(script)), default=str))


if __name__ == "__main__":
    main()
