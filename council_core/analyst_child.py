"""
council_core.analyst_child — run one piece of model-written pandas code in
its own process, for council_core.analyst_step.

    python -m council_core.analyst_child CODE_FILE DATA_FOLDER OUT_JSON

The code runs through vault_analyst.execute_pandas_code — the sandbox
(validated imports and names, reads only inside DATA_FOLDER, no writes, no
pickle; see tests/test_sandbox_escapes.py) — and the parent runs this under
council_core.child_proc with a time limit and a memory cap, because the
sandbox itself has neither: a generated `while True:` would otherwise hang
the question for good.

Writes {"ok", "log", "table"} to OUT_JSON; "table" is the result frame as
pandas' "split" JSON, or null.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv) -> int:
    code_file, folder, out_json = argv[1], argv[2], argv[3]
    code = Path(code_file).read_text(encoding="utf-8")
    import vault_analyst
    df, log = vault_analyst.execute_pandas_code(code, [Path(folder)])
    out = {"ok": df is not None, "log": str(log or "")[-4000:],
           "table": None}
    if df is not None:
        try:
            out["table"] = df.head(5000).to_json(orient="split",
                                                 date_format="iso")
        except Exception as exc:                          # noqa: BLE001
            out.update(ok=False, log=f"the result could not be read: {exc}")
    Path(out_json).write_text(json.dumps(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
