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
import os
import sys
import tempfile
from pathlib import Path

#: The app root, pinned on sys.path so model code's own imports (and the
#: sandbox's lazy ones — analyst_helpers, scipy, duckdb …) still resolve
#: after we chdir away from it.
_APP_ROOT = Path(__file__).resolve().parent.parent

#: What comes back of a script's own output (the tool message is cut much
#: shorter; this only bounds the file and the payload).
MAX_PRINTED = 20_000


def _cut(text: str, n: int = MAX_PRINTED) -> str:
    text = str(text or "")
    return text if len(text) <= n else (
        text[:n] + f"\n… ({len(text) - n} more characters)")


def main(argv) -> int:
    # Resolve everything to absolute BEFORE chdir, and pin the app root on
    # sys.path so imports survive the chdir.
    code_file = str(Path(argv[1]).resolve())
    folder = str(Path(argv[2]).resolve())
    out_json = str(Path(argv[3]).resolve())
    if str(_APP_ROOT) not in sys.path:
        sys.path.insert(0, str(_APP_ROOT))
    code = Path(code_file).read_text(encoding="utf-8")
    import vault_analyst
    # Run from a throwaway folder, never the app's own directory: a relative
    # write the sandbox does not already refuse (e.g. a filter's output
    # path) then lands here and is discarded, not in the app's code folder
    # (reproduced 2026-10-15: a relative savefig wrote into the app root).
    report: dict = {}
    with tempfile.TemporaryDirectory(prefix="council-analyst-cwd-") as _cwd:
        os.chdir(_cwd)
        try:
            df, log = vault_analyst.execute_pandas_code(code, [Path(folder)],
                                                        report=report)
        finally:
            # Leave the throwaway cwd before it is removed (on Windows a
            # directory cannot be deleted while it is the cwd).
            os.chdir(_APP_ROOT)
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
