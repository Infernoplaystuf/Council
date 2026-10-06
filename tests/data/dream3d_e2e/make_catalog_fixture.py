"""Rebuild nx_catalog.json — the REAL catalog the offline Dream3D tests use.

    <council python> tests/data/dream3d_e2e/make_catalog_fixture.py

Runs in the APP env: it asks the installed nx env for its catalog through
nx_bridge, exactly as the app does, then keeps only what the code reads
(filters with their parsed signatures, enums, module names, filter
attributes, parameter classes). Dropped: each filter's raw execute()
docstring and its one-line signature and the pipeline_api probe (they only
repeat what is kept), and the env fingerprint (it names this machine's
paths; the staleness tests build their own).

Regenerate it after a DREAM3D-NX upgrade; the tests that use it say which
build it came from (its "python" and "fixture_note").
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

import nx_bridge  # noqa: E402


def main() -> None:
    cat = nx_bridge.catalog()
    env = cat.pop("env", None) or {}
    for k in ("pipeline_api",):
        cat.pop(k, None)
    for f in cat.get("filters", []):
        f.pop("execute_doc", None)
        f.pop("params_api", None)
        (f.get("execute") or {}).pop("signature", None)
    cat["fixture_note"] = (
        "Real catalog of the installed nx env, trimmed by "
        "make_catalog_fixture.py; packages: "
        + ", ".join(f"{k} {v}" for k, v in sorted(
            (env.get("packages") or {}).items())))
    out = HERE / "nx_catalog.json"
    # One filter per line: a DREAM3D-NX upgrade shows up as a readable diff.
    filters = cat.pop("filters")
    head = json.dumps(cat, sort_keys=True)
    body = ",\n".join(json.dumps(f, sort_keys=True) for f in filters)
    out.write_text(head[:-1] + ', "filters": [\n' + body + "\n]}\n",
                   encoding="utf-8", newline="\n")
    json.loads(out.read_text(encoding="utf-8"))          # it must round-trip
    print(f"wrote {out}: {len(filters)} filters, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
