"""
tool_forge.py — model-driven authoring of app-built tools.

Given a plain-language task, ask the LOCAL model to write ONE Python function,
then validate + save it through app_built_tools (same sandbox rules; saved
UNREVIEWED under <vault>/App_Built_tools/). Both the GUI "Tool Creation" tab
and the council/agent "I need a tool that doesn't exist" route call
generate_tool().

Robustness: the model is given a precise API reference for what's available in
the sandbox (so it uses read_text/read_table instead of raw open + manual
decoding), and generate_tool runs a bounded SELF-CORRECTION loop — it test-runs
the saved tool and, on any validation or runtime error (e.g. "name 'open' is
not defined", UnicodeDecodeError), feeds the error back to the model to fix.

Nothing here relaxes security: the generated code passes the exact same
validator the analyst sandbox uses (no delete / write-outside-output / network
/ shell), and it runs only through vault_analyst.execute_pandas_code.
"""
from __future__ import annotations

import ast
import re
from typing import Any, Callable, List, Optional, Tuple

# A precise reference of what the authored tool may use — kept in sync with the
# sandbox namespace in vault_analyst.execute_pandas_code + app_built_tools.run_tool.
_SANDBOX_API = (
    "AVAILABLE INSIDE THE FUNCTION (already provided — do NOT re-implement or "
    "import these):\n"
    "  pd                             pandas module\n"
    "  np                             numpy module (may be None)\n"
    "  Path                           pathlib.Path\n"
    "  DATA_FOLDER (str)              the data folder path (DATA_FOLDERS = list)\n"
    "  list_dir(folder=None)          -> list[str] of names in a folder\n"
    "  count_files(folder=None)       -> int;  count_folders(folder=None) -> int\n"
    "  read_text(path, max_chars=200000) -> str  (ENCODING-SAFE; never raises)\n"
    "  read_lines(path)               -> list[str]\n"
    "  read_table(path)               -> DataFrame (CSV/TSV/Excel/Parquet/JSON)\n"
    "  list_csv_files(folder)         -> list[Path]\n"
    "  folder_data_summary(folder)    -> DataFrame (per file: type/rows/columns)\n"
    "  column_stats(folder=None)      -> DataFrame of per-column stats\n"
    "  image_pixel_stats(path)        -> dict of per-image pixel stats "
    "(brightness/contrast/channels/dominant colours)\n"
    "  aggregate_image_folder(folder=None) -> dict rollup over a folder of images\n"
    "  open(path)                     -> READ-ONLY file handle (writing blocked)\n"
    "\n"
    "FORBIDDEN (the sandbox will REJECT the tool): importing os / sys / "
    "subprocess / shutil / requests / socket; ANY file write, delete, mkdir, "
    "network, or shell; eval / exec. You MAY `import math / re / json / "
    "statistics`.\n"
    "To read a file's text, call read_text(path) — do NOT open in binary and "
    "decode yourself (that causes UnicodeDecodeError)."
)

_EXAMPLE = (
    "Example of the SHAPE (do not copy verbatim):\n"
    "def count_rows_with_zero(column='qty', folder=None):\n"
    "    total = 0\n"
    "    for name in list_dir(folder):\n"
    "        if not name.endswith('.csv'):\n"
    "            continue\n"
    "        df = read_table(Path(DATA_FOLDER) / name)\n"
    "        if column in df.columns:\n"
    "            total += int((df[column] == 0).sum())\n"
    "    return total\n"
)


def build_tool_prompt(task: str, existing: Optional[List[str]] = None) -> str:
    """The instruction handed to the local model to author a tool."""
    existing_note = ""
    if existing:
        existing_note = ("\nTools that already exist (reuse one instead of "
                         "duplicating if it fits): "
                         + ", ".join(str(e) for e in existing[:40]) + "\n")
    return (
        "You are writing ONE reusable Python tool for an OFFLINE, READ-ONLY "
        "data app.\nWrite EXACTLY ONE top-level function that performs this "
        "task:\n\n"
        f"TASK: {task}\n\n"
        "Rules:\n"
        "- Output ONLY Python code — no markdown fences, no prose, no example "
        "calls.\n"
        "- Define EXACTLY ONE top-level function; its name is the tool name "
        "(snake_case).\n"
        "- Give it keyword arguments with defaults so it runs with NO arguments.\n"
        "- RETURN the result (number / str / dict / list / DataFrame). Do not "
        "print.\n"
        "- Read-only. Handle missing files/columns gracefully (return an empty "
        "result or a short message instead of raising).\n\n"
        f"{_SANDBOX_API}\n\n"
        f"{_EXAMPLE}"
        f"{existing_note}"
        "\nWrite the function now:"
    )


def _retry_prompt(task: str, code: str, error: str) -> str:
    return (
        "You are FIXING one Python tool for an offline, read-only data app.\n"
        f"TASK: {task}\n\n"
        "Your previous code was:\n"
        f"{code or '(none)'}\n\n"
        f"It failed with: {error}\n\n"
        "Fix it. Reminders:\n"
        "- To read a file's TEXT use read_text(path) (encoding-safe). To read a "
        "TABLE use read_table(path). Do NOT decode bytes yourself.\n"
        "- open(path) is READ-ONLY (no writing/appending).\n"
        "- Use list_dir(folder=None) to enumerate files; DATA_FOLDER is the "
        "folder path.\n"
        "- Define EXACTLY ONE top-level function with defaulted keyword args, "
        "and RETURN the result.\n"
        f"\n{_SANDBOX_API}\n"
        "\nOutput ONLY the corrected Python function — no prose, no fences."
    )


def extract_code(reply: str) -> str:
    """Pull Python out of a model reply. Prefers vault_analyst.extract_python_code
    (handles the app's fenced/plain conventions); falls back to a fence strip."""
    try:
        from vault_analyst import extract_python_code
        c = extract_python_code(reply or "")
        if c and c.strip():
            return c.strip()
    except Exception:
        pass
    s = (reply or "").strip()
    m = re.search(r"```(?:python)?\s*(.*?)```", s, re.DOTALL)
    return (m.group(1) if m else s).strip()


def _entry_has_no_required_args(code: str) -> bool:
    """True if the single entry function can be called with NO arguments (so we
    can safely test-run it). False if it has required params or isn't parseable."""
    try:
        tree = ast.parse(code)
    except Exception:
        return False
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(fns) != 1:
        return False
    a = fns[0].args
    required_pos = (len(a.posonlyargs) + len(a.args)) - len(a.defaults)
    required_kw = sum(1 for d in a.kw_defaults if d is None)
    return required_pos <= 0 and required_kw == 0


def _test_run_error(name: str, code: str, vault_dir: Any) -> str:
    """Run the saved tool with no args (only if it needs none) and return an
    error string if it failed, else ''. Used to drive the self-correction loop."""
    if not _entry_has_no_required_args(code):
        return ""   # needs arguments — can't auto-test; accept as saved
    try:
        import app_built_tools as abt
        _df, msg = abt.run_tool(name, {}, vault_dir=vault_dir)
    except Exception as exc:
        return repr(exc)[:300]
    m = str(msg)
    if "EXECUTION ERROR" in m:
        return m.split("EXECUTION ERROR:", 1)[-1].strip()[:300]
    if "SAFETY CHECK FAILED" in m:
        return m[:300]
    return ""


def generate_tool(task: str,
                  model_call: Callable[[str], str],
                  *,
                  description: Optional[str] = None,
                  author: str = "model",
                  vault_dir: Optional[Any] = None,
                  max_attempts: int = 3,
                  ) -> Tuple[bool, str, Optional[str], str]:
    """Ask the model to author a tool for ``task``, validate + save it, test-run
    it, and self-correct on error (up to ``max_attempts``).

    ``model_call(prompt) -> reply`` is injected so this is testable offline and
    reuses whatever local-model entry point the caller has (council_engine.
    local_chat in the app). Returns ``(ok, message, saved_name, code)`` — the
    code is returned even on failure so the UI can show what the model wrote.
    """
    task = (task or "").strip()
    if not task:
        return False, "Describe what the tool should do.", None, ""
    try:
        import app_built_tools as abt
    except Exception as exc:
        return False, f"app_built_tools unavailable: {exc!r}", None, ""
    try:
        existing = [t.get("name") for t in abt.list_tools(vault_dir=vault_dir)]
    except Exception:
        existing = []

    prompt = build_tool_prompt(task, existing)
    last_msg, last_code = "", ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            reply = model_call(prompt)
        except Exception as exc:
            return False, f"model call failed: {exc!r}", None, last_code

        code = extract_code(reply)
        if code.strip():
            last_code = code
        if not code.strip():
            last_msg = "The model produced no code."
            prompt = _retry_prompt(task, last_code, "no code was produced")
            continue

        entry = abt.entry_function(code)
        if entry is None:
            last_msg = "The code must define EXACTLY ONE top-level function."
            prompt = _retry_prompt(task, code, last_msg)
            continue

        ok, msg, saved = abt.save_tool(
            entry, (description or task), code, author=author,
            vault_dir=vault_dir)
        if not ok:
            last_msg = msg
            prompt = _retry_prompt(task, code, f"the sandbox rejected it: {msg}")
            continue

        # Validated + saved. Test-run (when it needs no args); on error, fix.
        err = _test_run_error(saved, code, vault_dir)
        if err:
            last_msg = f"it saved but errored when run: {err}"
            prompt = _retry_prompt(task, code, err)
            continue

        suffix = "" if attempt == 1 else f" (fixed in {attempt} attempts)"
        return True, msg + suffix, saved, code

    return (False,
            (last_msg or "could not produce a working tool")
            + f" — after {max_attempts} attempts.",
            None, last_code)


def save_edited_tool(code: str, *, description: str = "",
                     author: str = "user", vault_dir: Optional[Any] = None
                     ) -> Tuple[bool, str, Optional[str]]:
    """Save code the user hand-edited in the Tool Creation tab. Same validation
    + single-entry-function requirement as generate_tool. Returns (ok, msg, name)."""
    try:
        import app_built_tools as abt
    except Exception as exc:
        return False, f"app_built_tools unavailable: {exc!r}", None
    entry = abt.entry_function(code or "")
    if entry is None:
        return (False, "The code must define exactly one top-level function "
                "(its entry point).", None)
    return abt.save_tool(entry, description or f"user tool {entry}", code,
                         author=author, vault_dir=vault_dir)
