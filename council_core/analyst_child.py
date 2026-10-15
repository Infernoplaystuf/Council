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

Writes {"ok", "log", "table", "ran", "stdout", "stderr", "error", "note"}
to OUT_JSON. "ok" and "table" are the result frame (pandas' "split" JSON,
or null) for the analyst and data_query; "ran" (the code passed the checks
and ran to its end, result_df or not), what it printed, and why it did not
run are for the Council's run_python, where printing the answer is enough.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

#: What comes back of a script's own output (the tool message is cut much
#: shorter; this only bounds the file and the payload).
MAX_PRINTED = 20_000


def _cut(text: str, n: int = MAX_PRINTED) -> str:
    text = str(text or "")
    return text if len(text) <= n else (
        text[:n] + f"\n… ({len(text) - n} more characters)")


def main(argv) -> int:
    code_file, folder, out_json = argv[1], argv[2], argv[3]
    code = Path(code_file).read_text(encoding="utf-8")
    import vault_analyst
    report: dict = {}
    df, log = vault_analyst.execute_pandas_code(code, [Path(folder)],
                                                report=report)
    out = {"ok": df is not None, "log": str(log or "")[-4000:],
           "table": None, "ran": bool(report.get("ran")),
           "stdout": _cut(report.get("stdout")),
           "stderr": _cut(report.get("stderr")),
           "error": str(report.get("error") or "")[-4000:],
           "note": str(report.get("note") or "")}
    if df is not None:
        try:
            out["table"] = df.head(5000).to_json(orient="split",
                                                 date_format="iso")
        except Exception as exc:                          # noqa: BLE001
            out.update(ok=False, log=f"the result could not be read: {exc}",
                       note=f"the result could not be read: {exc}")
    Path(out_json).write_text(json.dumps(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
