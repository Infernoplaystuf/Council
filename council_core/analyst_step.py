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


def run_code(code: str, folder: Path, *, timeout: float = TIME_LIMIT_S,
             runner: Optional[Callable[..., Any]] = None):
    """(DataFrame or None, log): the code in a child process under the
    sandbox, killed at `timeout` seconds."""
    import pandas as pd
    from . import child_proc
    runner = runner or child_proc.run
    with tempfile.TemporaryDirectory(prefix="council-analyst-") as tmp:
        code_file = Path(tmp) / "code.py"
        out_file = Path(tmp) / "out.json"
        code_file.write_text(code, encoding="utf-8")
        res = runner([sys.executable, "-m", "council_core.analyst_child",
                      str(code_file), str(folder), str(out_file)],
                     cwd=str(APP_ROOT), env=child_proc.child_env(),
                     timeout=timeout, memory_limit_mb=MEMORY_MB)
        if getattr(res, "timed_out", False):
            return None, (f"the code ran longer than {timeout:.0f} s and "
                          "was stopped")
        if not out_file.exists():
            err = (getattr(res, "stderr", "") or getattr(res, "error", "")
                   or "the analysis process ended without a result")
            return None, str(err)[-2000:]
        out = json.loads(out_file.read_text(encoding="utf-8"))
    if not out.get("ok") or not out.get("table"):
        return None, str(out.get("log") or "the code produced no table")
    import io
    df = pd.read_json(io.StringIO(out["table"]), orient="split")
    return df, str(out.get("log") or "")


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
        prompt = va.build_pandas_code_prompt(
            question, [folder], inventory,
            filename_hints=hints_text or None)
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


__all__ = ["AnalystResult", "run", "run_code", "data_folder", "enabled"]
