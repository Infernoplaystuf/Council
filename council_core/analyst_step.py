"""
council_core.analyst_step — figures computed from the user's data files,
before the council answers a numbers question.

A council of language models asked "what is the average pump runtime across
the logs?" otherwise answers from memory — it invents a plausible number.
When a question reads as a computation (vault_analyst.looks_computational)
and the vault's data folder (<vault>/data_in) holds data files, this:

  1. resolves the files the question names (resolve_filename_hints) and lists
     the folder's tables and columns (preview_csv_inventory);
  2. asks the Coder's model for pandas code (build_pandas_code_prompt);
  3. runs that code in a CHILD PROCESS — the hardened sandbox
     (execute_pandas_code: reads only the data folder, no writes, no pickle)
     inside council_core.child_proc's time limit and memory cap, since the
     sandbox has neither; a failure goes back to the model once;
  4. hands the council the result as an [ANALYST RESULT] block — answer from
     these figures, do not recompute or invent numbers — or, if it failed,
     an [ANALYST FAILED] block telling it not to invent a number either.

Every member and the Judge see the block (it is not vault-gated: figures are
for checking). The transcript gets the table. Nothing is written to the
vault. COUNCIL_ANALYST=0 turns it off.

THE FENCE FOR MODEL CODE IN A COUNCIL TURN
run_code (the analyst above, the data_query tool) and run_script (the
Coder's and the Intern's run_python, council_core.council_tools) are one
fence: _run_child — the sandbox in council_core.analyst_child, under
child_proc with at most TIME_LIMIT_S (a caller or a model may ask for less,
never more: time_limit) and a MEMORY_MB commit cap, the whole process tree
killed at the limit. They differ only in what counts as an answer: a
result_df for run_code; for run_script, code that ran to its end, with what
it printed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

APP_ROOT = Path(__file__).resolve().parent.parent
TIME_LIMIT_S = 120
MEMORY_MB = 2048
MAX_TRIES = 2


def enabled() -> bool:
    return os.environ.get("COUNCIL_ANALYST", "1").strip().lower() \
        not in ("0", "false", "no", "off")


@dataclass
class AnalystResult:
    block: str = ""            # for the council; "" = not a data question
    note: str = ""             # one line for the transcript
    table: str = ""            # the result, for the transcript
    code: str = ""
    error: str = ""


def data_folder(vault_dir: Path) -> Path:
    import data_index
    return data_index.input_dir(Path(vault_dir))


def time_limit(requested: Any = TIME_LIMIT_S) -> int:
    """The limit for one run, in whole seconds: what was asked for, but
    never more than TIME_LIMIT_S and never less than 1 — a model may
    shorten its own limit, never lengthen it. Anything that is not a number
    gets TIME_LIMIT_S."""
    try:
        t = int(float(requested))
    except (TypeError, ValueError, OverflowError):
        return TIME_LIMIT_S
    return max(1, min(t, TIME_LIMIT_S))


def _run_child(code: str, folder: Path, *, timeout: Any,
               runner: Optional[Callable[..., Any]],
               vault: Optional[Path] = None, no_data_ok: bool = False):
    """(the child's report as a dict, or None; why there is none; whether
    it was stopped at the time limit). The one
    way model code runs here: council_core.analyst_child — the sandbox — in
    a child process under child_proc, at most TIME_LIMIT_S (time_limit) and
    MEMORY_MB, the code written to a temporary folder. `vault`: the vault
    whose data_out the sandbox's helpers may write (council_core.paths
    .vault_dir() in the child). `no_data_ok`: a missing `folder` is an
    empty one (in the temporary folder) rather than a refusal."""
    from . import child_proc
    runner = runner or child_proc.run
    limit = time_limit(timeout)
    extra = ({"COUNCIL_VAULT_ROOT": str(Path(vault).resolve())}
             if vault is not None else None)
    with tempfile.TemporaryDirectory(prefix="council-analyst-") as tmp:
        code_file = Path(tmp) / "code.py"
        out_file = Path(tmp) / "out.json"
        code_file.write_text(code, encoding="utf-8")
        folder = Path(folder)
        if no_data_ok and not folder.is_dir():
            folder = Path(tmp) / "no_data"
            folder.mkdir()
        res = runner([sys.executable, "-m", "council_core.analyst_child",
                      str(code_file), str(folder), str(out_file)],
                     cwd=str(APP_ROOT), env=child_proc.child_env(extra),
                     timeout=limit, memory_limit_mb=MEMORY_MB)
        if getattr(res, "timed_out", False):
            return None, (f"the code ran longer than {limit} s and was "
                          "stopped"), True
        if not out_file.exists():
            err = (getattr(res, "stderr", "") or getattr(res, "error", "")
                   or "the analysis process ended without a result")
            return None, str(err)[-2000:], False
        return json.loads(out_file.read_text(encoding="utf-8")), "", False


def _frame(table: str):
    import io
    import pandas as pd
    return pd.read_json(io.StringIO(table), orient="split")


def run_code(code: str, folder: Path, *, timeout: float = TIME_LIMIT_S,
             runner: Optional[Callable[..., Any]] = None,
             vault: Optional[Path] = None):
    """(DataFrame or None, log): the code in a child process under the
    sandbox, killed at `timeout` seconds (at most TIME_LIMIT_S)."""
    out, why, _stopped = _run_child(code, folder, timeout=timeout,
                                    runner=runner, vault=vault)
    if out is None:
        return None, why
    if not out.get("ok") or not out.get("table"):
        return None, str(out.get("log") or "the code produced no table")
    return _frame(out["table"]), str(out.get("log") or "")


@dataclass
class ScriptRun:
    ran: bool = False          # passed the checks and ran to its end
    stdout: str = ""
    stderr: str = ""
    df: Any = None             # its result_df, when it set a usable one
    error: str = ""            # the refusal, traceback or stop when not ran
    note: str = ""             # why a result_df it set could not be used
    timed_out: bool = False


def run_script(code: str, folder: Path, *, timeout: Any = TIME_LIMIT_S,
               runner: Optional[Callable[..., Any]] = None,
               vault: Optional[Path] = None) -> ScriptRun:
    """The code as a script, under exactly run_code's fence (the sandbox in
    a child process, at most TIME_LIMIT_S and MEMORY_MB): for the Council's
    run_python, where code that prints its answer has worked even without
    a result_df. A missing data folder is an empty one. Never raises."""
    try:
        out, why, stopped = _run_child(code, folder, timeout=timeout,
                                       runner=runner, vault=vault,
                                       no_data_ok=True)
    except Exception as exc:                              # noqa: BLE001
        return ScriptRun(error=f"the code could not run: {exc}")
    if out is None:
        return ScriptRun(error=why, timed_out=stopped)
    run = ScriptRun(ran=bool(out.get("ran")),
                    stdout=str(out.get("stdout") or ""),
                    stderr=str(out.get("stderr") or ""),
                    error=str(out.get("error") or ""),
                    note=str(out.get("note") or ""))
    if not run.ran and not run.error:
        run.error = str(out.get("log") or "the code did not run")
    if out.get("ok") and out.get("table"):
        try:
            run.df = _frame(out["table"])
        except Exception as exc:                          # noqa: BLE001
            run.note = f"the result could not be read: {exc}"
    return run


def run(question: str, vault_dir: Path, *,
        chat: Optional[Callable[..., str]] = None,
        runner: Optional[Callable[..., Any]] = None,
        folder: Optional[Path] = None) -> AnalystResult:
    """The analyst for one question. Blocking (a model call and a child
    process) — call it from a worker. Never raises."""
    if not enabled() or not (question or "").strip():
        return AnalystResult()
    try:
        import vault_analyst as va
    except Exception as exc:                              # noqa: BLE001
        return AnalystResult(error=f"analyst unavailable: {exc}")
    try:
        if not va.looks_computational(question):
            return AnalystResult()
        folder = Path(folder) if folder else data_folder(vault_dir)
        if not folder.is_dir() or not va.list_data_files([folder]):
            return AnalystResult(note=(
                "Analyst: this reads as a numbers question, but the data "
                f"folder ({folder.name}) holds no data files."))
        hints_text = ""
        try:
            hints = va.resolve_filename_hints(question, [folder])
            if hints:
                hints_text = va.format_filename_hints(hints,
                                                      base_folder=folder)
        except Exception:                                 # noqa: BLE001
            pass
        inventory = va.preview_csv_inventory([folder], max_files=15,
                                             max_cols=30)
        # No save_app_tool / run_app_tool: the child has none to give.
        prompt = va.build_pandas_code_prompt(
            question, [folder], inventory,
            filename_hints=hints_text or None, app_tools=False)
    except Exception as exc:                              # noqa: BLE001
        return AnalystResult(error=f"analyst setup failed: {exc}")

    if chat is None:
        import council_engine
        chat = council_engine.local_chat
    code, log, df = "", "", None
    feedback = ""
    for _ in range(MAX_TRIES):
        try:
            raw = chat([{"role": "user", "content": prompt + feedback}],
                       role="coder", temperature=0.0, num_predict=700,
                       timeout=120)
        except Exception as exc:                          # noqa: BLE001
            log = f"code generation failed: {exc}"
            break
        code = va.extract_python_code(raw)
        if not code.strip():
            log = "the model wrote no code"
            feedback = "\n\nYou wrote no code. Reply with one python block."
            continue
        try:
            df, log = run_code(code, folder, runner=runner)
        except Exception as exc:                          # noqa: BLE001
            df, log = None, f"the analysis could not run: {exc}"
        if df is not None:
            break
        feedback = (f"\n\nYOUR CODE FAILED:\n{log[-1200:]}\nFix it and "
                    "reply with the corrected python block only.")

    if df is None:
        first = (log.strip().splitlines() or ["unknown error"])[-1][:200]
        block = ("[ANALYST FAILED — no figures]\n"
                 "# The council tried to compute this from the user's data "
                 "files and could not. Do NOT invent a number: say the "
                 f"computation failed ({first}) and what would fix it.")
        return AnalystResult(block=block, code=code, error=first,
                             note=f"Analyst: could not compute this "
                                  f"({first}).")
    table = va.format_result_for_prompt(df, max_rows=250, max_chars=8000)
    block = ("[ANALYST RESULT — computed from the user's data files]\n"
             "# Answer from these figures. Do NOT recompute them or invent "
             "other numbers; say which file(s) they came from.\n" + table)
    rows, cols = df.shape
    shown = df.head(40).to_string(index=False, max_colwidth=60)
    return AnalystResult(
        block=block, code=code,
        table=shown + (f"\n… ({rows - 40} more rows)" if rows > 40 else ""),
        note=f"Analyst: computed a {rows}×{cols} table from {folder.name}.")


__all__ = ["AnalystResult", "ScriptRun", "run", "run_code", "run_script",
           "time_limit", "data_folder", "enabled"]
