"""
council_core.analyst_child — run one piece of model-written pandas code in
its own process, for council_core.analyst_step.

    python -m council_core.analyst_child CODE_FILE DATA_FOLDER OUT_JSON [CALL_JSON]

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
"shape" is the result frame's full [rows, cols] (at most
frame_codec.MAX_ROWS rows come back).

CALL_JSON, when given, asks for more. {"exact": true}: the result comes
back as "frame" (council_core.frame_codec) instead of "table" — the code's
own frame, exactly: every float as it was, text stays text ("007", not 7),
a timestamp a timestamp. pandas' "split" JSON rounds a float to 10 digits
(1e-12 came back as 0.0) and turns "001" labels, dates, bytes and tuples
into other things; the agents' run_pandas_analysis and every tool call
read the result in the app's own process until 2026-10-15, exactly.

A TOOL CALL ({"entry": name, "args": {...}, "folders": [...]}, always
exact) is a model-built tool's code run the way app_built_tools always ran
one: its one function called with the arguments, and what it returns made
the result — a frame, Series or dict as a table, anything else as a
one-cell "result" frame. The arguments arrive as JSON and reach the code as
plain values (no pickle, and nothing is pasted into the code as source).
"folders" are further data folders the code may read.

A report bigger than frame_codec.MAX_RESULT_MB comes back without its
result, saying so: the app would hold it several times over while reading
it.
"""
from __future__ import annotations

import json
import keyword
import os
import sys
import tempfile
from pathlib import Path

from council_core import frame_codec

#: The app root, pinned on sys.path so model code's own imports (and the
#: sandbox's lazy ones — analyst_helpers, scipy, duckdb …) still resolve
#: after we chdir away from it.
_APP_ROOT = Path(__file__).resolve().parent.parent

#: json's dumps as it was before any model code ran. The code may rebind a
#: module's attributes inside this process (`json.dumps = …` replaced it,
#: measured 2026-10-15) — that dies with the process, but it must not
#: garble the report written after it. (Code that goes deeper — json's
#: encoder class — can still change its own report; the app checks every
#: field of it, council_core.analyst_step._script_run.)
_dumps = json.dumps

#: What comes back of a script's own output (the tool message is cut much
#: shorter; this only bounds the file and the payload).
MAX_PRINTED = 20_000


def _cut(text: str, n: int = MAX_PRINTED) -> str:
    text = str(text or "")
    return text if len(text) <= n else (
        text[:n] + f"\n… ({len(text) - n} more characters)")


#: The name a tool call's arguments have in the sandbox (a plain dict).
TOOL_ARGS = "_tool_args"

#: What a tool call appends to the tool's code: call its function with the
#: arguments and make what it returns the result (as app_built_tools did).
_CALL = ("\n\n_ret = {entry}(**" + TOOL_ARGS + ")\n"
         "result_df = _ret if isinstance(_ret, (pd.DataFrame, pd.Series, "
         "dict)) else pd.DataFrame([{{'result': _ret}}])\n")


def _read_call(call_file: str):
    """(code to append, extra sandbox globals, further folders, exact,
    refusal)."""
    call = json.loads(Path(call_file).read_text(encoding="utf-8"))
    if not isinstance(call, dict):
        return "", {}, [], True, "the tool has no entry function to call"
    if "entry" not in call:                  # a script, its result exact
        return "", {}, [], bool(call.get("exact")), ""
    entry = str(call.get("entry") or "")
    if not entry.isidentifier() or keyword.iskeyword(entry):
        return "", {}, [], True, "the tool has no entry function to call"
    args = call.get("args")
    if not isinstance(args, dict):
        return "", {}, [], True, "the tool's arguments must be an object"
    folders = [str(Path(f).resolve()) for f in call.get("folders") or []
               if isinstance(f, str) and f.strip()]
    return _CALL.format(entry=entry), {TOOL_ARGS: args}, folders, True, ""


def main(argv) -> int:
    # Resolve everything to absolute BEFORE chdir, and pin the app root on
    # sys.path so imports survive the chdir.
    code_file = str(Path(argv[1]).resolve())
    folder = str(Path(argv[2]).resolve())
    out_json = str(Path(argv[3]).resolve())
    call_file = str(Path(argv[4]).resolve()) if len(argv) > 4 else ""
    if str(_APP_ROOT) not in sys.path:
        sys.path.insert(0, str(_APP_ROOT))
    code = Path(code_file).read_text(encoding="utf-8")
    extra, folders, exact, refusal = {}, [], False, ""
    if call_file:
        call_code, extra, folders, exact, refusal = _read_call(call_file)
        code += call_code
    import vault_analyst
    # Run from a throwaway folder, never the app's own directory: a relative
    # write the sandbox does not already refuse (e.g. a filter's output
    # path) then lands here and is discarded, not in the app's code folder
    # (reproduced 2026-10-15: a relative savefig wrote into the app root).
    report: dict = {}
    if refusal:
        df, log = None, refusal
        report = {"error": refusal}
    else:
        with tempfile.TemporaryDirectory(prefix="council-analyst-cwd-") as _cwd:
            os.chdir(_cwd)
            try:
                df, log = vault_analyst.execute_pandas_code(
                    code, [Path(folder)] + [Path(f) for f in folders],
                    extra_globals=extra or None, report=report)
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
    out_of_memory = False
    if df is not None:
        out["shape"] = [int(df.shape[0]), int(df.shape[1])]
        try:
            if exact:
                out["frame"] = frame_codec.encode(df)
            else:
                out["table"] = df.head(frame_codec.MAX_ROWS).to_json(
                    orient="split", date_format="iso")
        except MemoryError:
            out_of_memory = True
            out.update(frame=None, table=None)
        except Exception as exc:                          # noqa: BLE001
            out.update(ok=False, frame=None,
                       log=f"the result could not be read: {exc}",
                       note=f"the result could not be read: {exc}")
    text = ""
    if not out_of_memory:
        try:
            text = _dumps(out)
        except MemoryError:
            out_of_memory = True
    if out_of_memory or len(text) > frame_codec.MAX_RESULT_MB * 2**20:
        # ensure_ascii: one character is one byte of the file.
        why = frame_codec.too_large(None if out_of_memory else len(text))
        text = ""
        out.update(ok=False, table=None, frame=None, log=why, note=why)
        text = _dumps(out)
    Path(out_json).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
