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

The same fence runs every other piece of model-written code the app runs
(2026-10-15): run_tool_code — a model-built tool's one function, called
with its arguments (passed as JSON) and its return value brought back
(app_built_tools: the Forge tab's test runs and Run Selected, tool tests,
the Council's approved tools; agent_profiles: an agent's attached tools);
and run_script for the agents' and background jobs' run_pandas_analysis
(safe_agent). In-process, with no limit, a tool's `while True:` hung its
worker for good, a huge allocation took the app's own memory, and a tool
could replace the app's json.dumps or Path.home for the rest of the
session.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

from . import token_budget

APP_ROOT = Path(__file__).resolve().parent.parent
TIME_LIMIT_S = 120
#: Wall-clock grace added to the model's code budget so the sandbox child's
#: own start-up — importing pandas, numpy, scipy and vault_analyst, ~1–2 s at
#: idle and more under load — is not counted against it. Without it a model
#: that asked for a small timeout_s saw trivial code "stopped" and was told
#: its code ran too long (reproduced 2026-10-15), and time_limit() clamps a
#: request to at least 1 s, so timeout_s=1 could never succeed.
SANDBOX_STARTUP_S = 8
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


#: Exits that say the child could not get memory: STATUS_NO_MEMORY,
#: STATUS_COMMITMENT_LIMIT.
_MEMORY_EXITS = (0xC0000017, 0xC000012D)
#: A C library's abort (STATUS_STACK_BUFFER_OVERRUN, fail-fast): how a
#: failed allocation inside pandas' own C code ends under the memory cap —
#: no traceback, nothing on stderr. A 200 MB text through pandas' to_json
#: died so at 1019-1423 MB of the 2048 MB cap (the request that failed was
#: the next doubling of its buffer), and was reported only as "ended
#: without a result" (measured 2026-10-15). The peak alone cannot tell: a
#: sandbox child that only prints already commits ~820 MB (numpy's
#: buffers). So a SILENT abort is read as the memory cap — sandboxed code
#: has no other way to one (no ctypes, no native code of its own) — unless
#: the process was still small (under a quarter of the cap), and the
#: message gives the exit and the peak.
_ABORT_EXIT = 0xC0000409

#: Why a run has no answer when the report the child wrote cannot be used.
DAMAGED = ("the code's result could not be read (the sandbox process's "
           "report was damaged)")


def _memory_sentence() -> str:
    return (f"the code needed more memory than the sandbox allows "
            f"({MEMORY_MB} MB) and was stopped")


def _no_report(res: Any) -> Tuple[str, str]:
    """(why the child left no report, "memory" when the memory cap stopped
    it, else "")."""
    infra = str(getattr(res, "infra", "") or "")
    text = str(getattr(res, "stderr", "") or getattr(res, "error", "") or "")
    rc = getattr(res, "returncode", None)
    code = rc & 0xFFFFFFFF if isinstance(rc, int) else None
    peak = getattr(res, "peak_mb", None)
    peak = peak if isinstance(peak, int) else None
    named = (code in _MEMORY_EXITS
             or any(w in infra.lower() for w in _MEMORY_WORDS))
    silent_abort = (code == _ABORT_EXIT and not text.strip()
                    and (peak is None or peak >= MEMORY_MB // 4))
    exit_text = f"exit 0x{code:08X}" if code and code >= 0xC0000000 else ""
    if named or silent_abort:
        facts = ", ".join(t for t in (
            f"the sandbox process ended at {peak} MB" if peak else "",
            exit_text) if t)
        why = _memory_sentence() + (f" ({facts})" if facts else "")
        return why + (f"\n{text[-1500:]}" if text.strip() else ""), "memory"
    if text.strip():
        return text[-2000:], ""
    detail = infra or exit_text           # child_proc's infra names the exit
    return ("the analysis process ended without a result"
            + (f" ({detail})" if detail else "")), ""


def _run_child(code: str, folder: Optional[Path], *, timeout: Any,
               runner: Optional[Callable[..., Any]],
               vault: Optional[Path] = None, no_data_ok: bool = False,
               call: Optional[Dict[str, Any]] = None):
    """(the child's report as a dict, or None; why there is none; what
    stopped it — "time" at the time limit, "memory" at the memory cap, or
    ""). The one
    way model code runs here: council_core.analyst_child — the sandbox — in
    a child process under child_proc, at most TIME_LIMIT_S (time_limit) and
    MEMORY_MB, the code written to a temporary folder. `vault`: the vault
    whose data_out the sandbox's helpers may write (council_core.paths
    .vault_dir() in the child). `no_data_ok`: a missing `folder` (or
    None) is an empty one (in the temporary folder) rather than a refusal.
    `call`: a tool call ({"entry", "args", "folders"}) or {"exact": true}
    (see analyst_child), written beside the code as JSON — a TypeError when
    it is not plain JSON. A report over frame_codec.MAX_RESULT_MB is not
    read (the child refuses to write one; this does not trust it to)."""
    from . import frame_codec
    from . import child_proc
    runner = runner or child_proc.run
    limit = time_limit(timeout)
    extra = ({"COUNCIL_VAULT_ROOT": str(Path(vault).resolve())}
             if vault is not None else None)
    with tempfile.TemporaryDirectory(prefix="council-analyst-") as tmp:
        code_file = Path(tmp) / "code.py"
        out_file = Path(tmp) / "out.json"
        code_file.write_text(code, encoding="utf-8")
        folder = Path(folder) if folder else Path(tmp) / "no_data"
        if no_data_ok and not folder.is_dir():
            folder = Path(tmp) / "no_data"
            folder.mkdir()
        argv = [sys.executable, "-m", "council_core.analyst_child",
                str(code_file), str(folder), str(out_file)]
        if call is not None:
            call_file = Path(tmp) / "call.json"
            call_file.write_text(json.dumps(call), encoding="utf-8")
            argv.append(str(call_file))
        # The wall-clock timeout is the model's code budget PLUS a fixed
        # start-up grace, so importing the sandbox is not charged to the
        # code. retries=0: model code runs ONCE — child_proc's default
        # re-runs an "infra" exit (a commit-limit death near the memory cap
        # counts as one), which would run the model's code, and its data_out
        # writes, again (up to three times) for one run_python call.
        res = runner(argv,
                     cwd=str(APP_ROOT), env=child_proc.child_env(extra),
                     timeout=limit + SANDBOX_STARTUP_S, retries=0,
                     memory_limit_mb=MEMORY_MB)
        if getattr(res, "timed_out", False):
            return None, (f"the code ran longer than {limit} s and was "
                          "stopped"), "time"
        if not out_file.exists():
            return (None,) + _no_report(res)
        try:
            size = out_file.stat().st_size
        except OSError:
            size = 0
        if size > frame_codec.MAX_RESULT_MB * 2**20:
            return None, frame_codec.too_large(size), ""
        try:
            report = json.loads(out_file.read_bytes())
        except (OSError, ValueError, RecursionError):
            report = None
        if not isinstance(report, dict):
            return None, DAMAGED, ""
        return report, "", ""


def _frame(table: str):
    """The child's "split" JSON as a DataFrame."""
    import io
    import pandas as pd
    return pd.read_json(io.StringIO(table), orient="split")


#: The child's report, field by field (council_core.analyst_child): what
#: each may hold. A field that is there must be of its kind; one that is
#: not there reads as empty.
_REPORT_FIELDS = {"ok": (bool,), "ran": (bool,), "log": (str,),
                  "stdout": (str,), "stderr": (str,), "error": (str,),
                  "note": (str,), "table": (str, type(None)),
                  "frame": (dict, type(None)), "shape": (list,)}


def _report_ok(out: Dict[str, Any]) -> bool:
    """Whether the child's report is one it could have written. Model code
    can change what its own process reports (rebinding json's encoder
    class rewrote it, measured 2026-10-15): a field of the wrong kind made
    the app raise. Such a report is not used at all."""
    from . import frame_codec
    for key, kinds in _REPORT_FIELDS.items():
        if key in out and not isinstance(out[key], kinds):
            return False
    return "shape" not in out or frame_codec.shape(out) is not None


def _text(value: Any) -> str:
    """A report's text field as text."""
    return value if isinstance(value, str) else ""


def run_code(code: str, folder: Path, *, timeout: float = TIME_LIMIT_S,
             runner: Optional[Callable[..., Any]] = None,
             vault: Optional[Path] = None):
    """(DataFrame or None, log): the code in a child process under the
    sandbox, killed at `timeout` seconds (at most TIME_LIMIT_S)."""
    out, why, _stop = _run_child(code, folder, timeout=timeout,
                                 runner=runner, vault=vault)
    if out is None:
        return None, why
    if not _report_ok(out):
        return None, DAMAGED
    if out.get("ok") is not True or not out.get("table"):
        return None, _text(out.get("log")) or "the code produced no table"
    try:
        return _frame(_text(out["table"])), _text(out.get("log"))
    except Exception as exc:                              # noqa: BLE001
        return None, f"the result could not be read: {exc}"


#: What the sandbox, or the process around it, says when the memory cap
#: (MEMORY_MB) stopped the code.
_MEMORY_WORDS = ("memoryerror", "unable to allocate", "commitment_limit",
                 "status_no_memory")


@dataclass
class ScriptRun:
    ran: bool = False          # passed the checks and ran to its end
    stdout: str = ""
    stderr: str = ""
    df: Any = None             # its result_df, when it set a usable one
    error: str = ""            # the refusal, traceback or stop when not ran
    note: str = ""             # why a result_df it set could not be used
    timed_out: bool = False
    #: The sandbox's own log (what execute_pandas_code returns): what the
    #: code printed, or the refusal or traceback with it.
    log: str = ""
    #: The result's full (rows, cols); `df` holds at most
    #: frame_codec.MAX_ROWS rows of it (`cut` says so).
    shape: Optional[Tuple[int, int]] = None
    #: The memory cap stopped the process before it could report (its
    #: words for it, when it had any, are in `error`).
    out_of_memory: bool = False

    @property
    def stopped_by_memory(self) -> bool:
        if self.ran:
            return False
        low = f"{self.error}\n{self.log}".lower()
        return self.out_of_memory or any(w in low for w in _MEMORY_WORDS)

    @property
    def cut(self) -> str:
        """"showing the first N of M rows" when `df` is the start of a
        longer table, else ""."""
        if self.df is None or not self.shape:
            return ""
        shown, total = len(self.df), self.shape[0]
        return (f"showing the first {shown} of {total} rows"
                if total > shown else "")

    @property
    def message(self) -> str:
        """For a person or a model: the log when the code ran (with why its
        result did not come back, or that only its first rows did); when it
        did not run, why — one plain sentence first when the time limit or
        the memory cap stopped it."""
        if self.ran:
            parts = [self.log or "ok"]
            if self.df is None and self.note and self.note not in parts[0]:
                parts.append(self.note)
            if self.cut:
                parts.append(f"({self.cut})")
            return "\n".join(parts)
        detail = (self.log or self.error or "the code did not run").strip()
        if self.timed_out:
            return self.error or detail
        if self.stopped_by_memory:
            head = _memory_sentence()
            return detail if detail.startswith(head) else f"{head}\n{detail}"
        return detail

    def brief(self, limit: int = 300) -> str:
        """What went wrong, short: "" when it ran (unless its result could
        not be brought back: then why); the plain sentence for a stop; else
        the error's tail — a traceback's last lines say what to fix."""
        if self.ran:
            return self.note[:limit] if self.df is None else ""
        if self.timed_out:
            return self.message[:limit]
        if self.stopped_by_memory:
            return self.message.splitlines()[0][:limit]
        text = (self.error or self.log or "the code did not run").strip()
        return text if len(text) <= limit else "…" + text[-(limit - 1):]


def _script_run(out: Optional[Dict[str, Any]], why: str,
                stop: str) -> ScriptRun:
    """The child's report (from _run_child) as a ScriptRun. Every field is
    checked before use — model code can change what its own process
    reports (rebinding json's encoder class rewrote it, measured
    2026-10-15) — and a report that cannot be used is DAMAGED, never an
    exception."""
    if out is None:
        return ScriptRun(error=why, timed_out=stop == "time",
                         out_of_memory=stop == "memory")
    try:
        return _read_report(out)
    except Exception:                                     # noqa: BLE001
        return ScriptRun(error=DAMAGED)


def _read_report(out: Dict[str, Any]) -> ScriptRun:
    from . import frame_codec
    if not _report_ok(out):
        return ScriptRun(error=DAMAGED)
    run = ScriptRun(ran=out.get("ran") is True,
                    stdout=_text(out.get("stdout")),
                    stderr=_text(out.get("stderr")),
                    error=_text(out.get("error")),
                    note=_text(out.get("note")),
                    log=_text(out.get("log")))
    if not run.ran and not run.error:
        run.error = run.log or "the code did not run"
    run.shape = frame_codec.shape(out)
    if out.get("ok") is not True:
        return run
    try:
        if out.get("frame") is not None:
            run.df = frame_codec.decode(out["frame"])
        elif out.get("table"):
            run.df = _frame(_text(out["table"]))
    except Exception as exc:                              # noqa: BLE001
        run.note = f"the result could not be read: {exc}"[:500]
        return run
    if run.df is None:
        # "ok" with no table: the child always sends one with it.
        run.note = DAMAGED
        return run
    if run.shape is not None and (
            run.shape[1] != run.df.shape[1]
            or run.shape[0] < run.df.shape[0]):
        run.shape = (int(run.df.shape[0]), int(run.df.shape[1]))
    return run


def run_script(code: str, folder: Path, *, timeout: Any = TIME_LIMIT_S,
               runner: Optional[Callable[..., Any]] = None,
               vault: Optional[Path] = None,
               exact: bool = False) -> ScriptRun:
    """The code as a script, under exactly run_code's fence (the sandbox in
    a child process, at most TIME_LIMIT_S and MEMORY_MB): for the Council's
    run_python, where code that prints its answer has worked even without
    a result_df, and the agents' run_pandas_analysis. A missing data folder
    is an empty one. `exact`: the result_df comes back as the code made it
    (council_core.frame_codec: every float, text stays text, a date a
    date), not through pandas' rounding JSON. Never raises."""
    try:
        out, why, stop = _run_child(code, folder, timeout=timeout,
                                    runner=runner, vault=vault,
                                    no_data_ok=True,
                                    call={"exact": True} if exact else None)
    except Exception as exc:                              # noqa: BLE001
        return ScriptRun(error=f"the code could not run: {exc}")
    return _script_run(out, why, stop)


def run_tool_code(code: str, entry: str, args: Any,
                  folders: Union[Path, str, Sequence[Any], None], *,
                  timeout: Any = TIME_LIMIT_S,
                  runner: Optional[Callable[..., Any]] = None,
                  vault: Optional[Path] = None) -> ScriptRun:
    """A model-built tool's code, under run_code's fence: its function
    `entry` called with `args` (a dict, handed to the child as JSON — no
    pickle, and nothing pasted into the code), and what it returns as `df`
    — the table it returned, or a one-cell "result" frame holding any
    other value, exactly as app_built_tools always gave it (the same
    floats, labels, dtypes and values: council_core.frame_codec).
    `folders`: the data folders it may read (the first is DATA_FOLDER; a
    missing one is an empty one). Never raises."""
    if not isinstance(args, dict):
        return ScriptRun(error="the tool's arguments must be an object "
                               "(names and values)")
    try:
        json.dumps(args)
    except (TypeError, ValueError) as exc:
        return ScriptRun(error="the tool's arguments must be plain values "
                               f"(text, numbers, true/false, lists, "
                               f"objects): {exc}")
    if isinstance(folders, (str, Path)):
        folders = [folders]
    folders = [Path(f) for f in (folders or []) if f]
    first = folders[0] if folders else None
    call = {"entry": str(entry or ""), "args": args,
            "folders": [str(f) for f in folders[1:]]}
    try:
        out, why, stop = _run_child(code, first, timeout=timeout,
                                    runner=runner, vault=vault,
                                    no_data_ok=True, call=call)
    except Exception as exc:                              # noqa: BLE001
        return ScriptRun(error=f"the tool could not run: {exc}")
    return _script_run(out, why, stop)


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
                       role="coder", temperature=0.0,
                       num_predict=token_budget.reply("answer", 700),
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
            # vault=vault_dir: the sandbox's helpers write into THIS turn's
            # data_out, not whatever vault the child's environment resolves
            # (they differ when the turn's vault is not the process's).
            df, log = run_code(code, folder, runner=runner, vault=vault_dir)
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
           "run_tool_code", "time_limit", "data_folder", "enabled"]
