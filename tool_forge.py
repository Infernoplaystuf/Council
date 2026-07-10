"""
tool_forge.py — model-driven authoring of app-built tools.

Given a plain-language task, ask the LOCAL model to write ONE Python function,
then validate + save it through app_built_tools (same sandbox rules; saved
UNREVIEWED under <vault>/App_Built_tools/). Both the GUI "Tool Creation" tab
and the council/agent "I need a tool that doesn't exist" route call
generate_tool().

Nothing here relaxes security: the generated code passes the exact same
validator the analyst sandbox uses (no delete / write-outside-output / network
/ shell), and it runs only through vault_analyst.execute_pandas_code.
"""
from __future__ import annotations

import re
from typing import Any, Callable, List, Optional, Tuple

# What the authored tool may use — kept in sync with the sandbox namespace in
# vault_analyst.execute_pandas_code + app_built_tools.run_tool.
_SANDBOX_HELPERS_DOC = (
    "Available INSIDE the function without importing: pd (pandas), np (numpy), "
    "Path (pathlib), DATA_FOLDER / DATA_FOLDERS (str paths to the data folder), "
    "and read-only helpers list_dir(folder=None), count_files(folder=None), "
    "count_folders(folder=None), folder_file_counts(folder), read_table(path), "
    "list_csv_files(folder), folder_data_summary(folder), column_stats(folder=None). "
    "You MAY `import pandas / numpy / math / re / json / statistics / pathlib` "
    "but NOT os / sys / subprocess / shutil / requests. NO file writes, deletes, "
    "network, or shell — the tool must be READ-ONLY."
)


def build_tool_prompt(task: str, existing: Optional[List[str]] = None) -> str:
    """The instruction handed to the local model to author a tool."""
    existing_note = ""
    if existing:
        existing_note = ("\nTools that already exist (reuse one instead of "
                         "duplicating if it fits): "
                         + ", ".join(str(e) for e in existing[:40]) + "\n")
    return (
        "You are writing ONE reusable Python tool for an OFFLINE data app.\n"
        "Write EXACTLY ONE top-level function that performs this task:\n\n"
        f"TASK: {task}\n\n"
        "Rules:\n"
        "- Output ONLY Python code — no markdown fences, no prose, no example calls.\n"
        "- Define EXACTLY ONE top-level function; its name is the tool name "
        "(snake_case, e.g. count_orders_over).\n"
        "- The function RETURNS its result (a number, string, dict, list, or a "
        "pandas DataFrame). Do not print.\n"
        "- Give the function sensible keyword arguments with defaults so it can "
        "run with no arguments.\n"
        f"- {_SANDBOX_HELPERS_DOC}\n"
        "- Read-only. Handle missing files/columns gracefully (return an empty "
        "result or a short message rather than raising).\n"
        f"{existing_note}"
        "\nWrite the function now:"
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


def generate_tool(task: str,
                  model_call: Callable[[str], str],
                  *,
                  description: Optional[str] = None,
                  author: str = "model",
                  vault_dir: Optional[Any] = None
                  ) -> Tuple[bool, str, Optional[str], str]:
    """Ask the model to author a tool for ``task``, then validate + save it.

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
    try:
        reply = model_call(prompt)
    except Exception as exc:
        return False, f"model call failed: {exc!r}", None, ""

    code = extract_code(reply)
    if not code.strip():
        return False, "The model produced no code — try rephrasing the task.", \
            None, (reply or "")

    entry = abt.entry_function(code)
    if entry is None:
        return (False,
                "The model's code did not define exactly one top-level function. "
                "Edit it to a single function, then Save.",
                None, code)

    ok, msg, saved = abt.save_tool(
        entry, (description or task), code, author=author, vault_dir=vault_dir)
    return ok, msg, saved, code


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
