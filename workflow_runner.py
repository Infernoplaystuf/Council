"""
Workflow orchestrator — runs a sequence of Dream3D / Python pipelines.

Modes:
  - Linear:    A → B → C, each pipeline runs once. Used when the
               pipelines are already parameterized for the desired input.
  - Per-file:  For a directory of input files, run the full workflow on
               file 1, then file 2, ... For each file we modify a copy
               of every pipeline (via pipeline_editor) to point at the
               current file, then execute the modified copies in sequence.
  - Per-step:  For a directory of input files, run pipeline A on all
               files, then pipeline B on all files, etc.

Stop-on-first-failure semantics throughout. The runner returns a
WorkflowResult with per-step logs so the GUI can show exactly which step
broke and why.

Pipelines execute as subprocesses. A script that imports simplnx (or its
plugins) runs with the DREAM3D-NX env's interpreter (nx_bridge.find_python):
simplnx lives in that separate env by design, and running every pipeline
with sys.executable — the app's own env — made every simplnx workflow die
with "No module named 'simplnx'". Anything else still runs with
sys.executable.

Before anything runs, every script passes nx_policy.run_reasons for its kind
(nx_policy "Two kinds of script"): no workflow runs Execute Process or the
Python-codegen filter, by name or through a route that hides which filter
runs; a MODEL-written script (stamped, or saved in data_out) must pass
validate_script in full; a USER's own script may run a saved pipeline
(Pipeline.from_file) whose filters pass the policy. Then every simplnx script
and every model-written one runs under nx_guard, in its own interpreter: a
denied filter refuses to execute however it was reached, and a model's
script writes only under the vault's data_out or this run's staging folder,
never over a file the run did not make, never into an input (Containment).

In the directory modes each run's outputs land in ``output_dir`` (one file
per input, never the path baked into the script), so N inputs make N
outputs instead of overwriting one, and nothing lands wherever the app
happened to be started. That holds for EVERY writer a script has, not just
the first: a second WriteDREAM3D (a checkpoint) or a feature CSV is written
to <output_dir>/<input stem>_<its file name>, and each run works in its own
folder, <output_dir>/<input stem>/, so a relative path the runner does not
recognise lands there instead of on top of another input's file.
result.outputs lists every file a writer was pointed at and every file a run
left in its working folder (not a writer's companion, such as the .xdmf
WriteDREAM3D puts next to its .dream3d).

Paths are found in the script's SYNTAX TREE — keyword arguments
(f(file_path=...)), attribute and plain assignments (v.input_file_path = ...)
— never by a text search: a comment or a string that mentions
`file_path = ...` used to be what got rewritten, and every input then read
the same baked file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def _shipped_script(name: str) -> Path:
    """A script another interpreter runs, so it must exist as a FILE: in a
    frozen build, in the bundle folder (sys._MEIPASS, where council.spec's
    datas put it); otherwise beside this module. When neither has it, the
    path beside this module (the caller says it is missing)."""
    here = Path(__file__).resolve().parent / name
    base = getattr(sys, "_MEIPASS", None) \
        if getattr(sys, "frozen", False) else None
    if base and (Path(base) / name).is_file():
        return Path(base) / name
    return here


#: Runs a script inside the containment (see nx_guard's docstring).
GUARD = _shipped_script("nx_guard.py")


# ============================================================
# Result types
# ============================================================

@dataclass
class StepResult:
    step_index: int                # 1-based
    pipeline_name: str
    input_label: str               # e.g. "(static)" or the per-file input name
    success: bool
    return_code: Optional[int]
    duration_s: float
    stdout: str
    stderr: str
    error: Optional[str] = None
    pipeline_path: Optional[Path] = None


@dataclass
class WorkflowResult:
    success: bool
    total_steps: int
    steps_run: int
    duration_s: float
    step_results: List[StepResult] = field(default_factory=list)
    error: Optional[str] = None    # high-level reason if the run aborted
    # Every file the runs wrote (directory modes): each input's final
    # result, every other writer's file, anything written in a run's own
    # working folder.
    outputs: List[Path] = field(default_factory=list)
    # What the runner decided that the user should know (e.g. which of two
    # writers it handed on).
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Workflow {'OK' if self.success else 'FAILED'} — "
            f"{self.steps_run}/{self.total_steps} steps in "
            f"{self.duration_s:.1f}s",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
        for n in self.notes:
            lines.append(f"  note: {n}")
        if self.outputs:
            try:
                root = Path(os.path.commonpath(
                    [str(o.parent) for o in self.outputs]))
            except ValueError:                  # different drives
                root = self.outputs[0].parent
            lines.append(f"  outputs ({len(self.outputs)}) in {root}:")
            for o in self.outputs[:20]:
                try:
                    lines.append(f"    {o.relative_to(root).as_posix()}")
                except ValueError:
                    lines.append(f"    {o}")
            if len(self.outputs) > 20:
                lines.append(f"    ... and {len(self.outputs) - 20} more")
        for s in self.step_results:
            status = "ok " if s.success else "FAIL"
            lines.append(
                f"  [{status}] #{s.step_index} {s.pipeline_name} "
                f"({s.input_label}) — {s.duration_s:.1f}s rc={s.return_code}"
            )
            if s.error:
                lines.append(f"        {s.error}")
            tail = (s.stderr or "").strip().split("\n")[-3:]
            if not s.success and tail:
                for t in tail:
                    if t:
                        lines.append(f"        stderr: {t[:200]}")
        return "\n".join(lines)


# ============================================================
# Subprocess execution
# ============================================================

_NX_MODULES = frozenset({"simplnx", "orientationanalysis",
                         "itkimageprocessing"})


def _read_source(path: Path) -> str:
    """A script's text, without the byte-order mark an editor may save at
    its start: Python runs such a file, but ast.parse refuses a BOM ("invalid
    non-printable character U+FEFF"), and every path below is found with
    ast — so the script would be refused as not valid Python."""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _imports_simplnx(source: str) -> bool:
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in _NX_MODULES for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _NX_MODULES:
                return True
    return False


def interpreter_for(source: str) -> Tuple[Optional[str], Optional[str]]:
    """(python executable, None) to run ``source`` with, or (None, why).

    A simplnx script needs an interpreter that has simplnx: this one if it
    does (the app was started inside the nx env), else the nx env's
    (nx_bridge.find_python, which honours COUNCIL_NX_PYTHON)."""
    if not _imports_simplnx(source):
        return sys.executable, None
    import importlib.util
    if importlib.util.find_spec("simplnx") is not None:
        return sys.executable, None
    try:
        import nx_bridge
        py = nx_bridge.find_python()
    except Exception:                                     # noqa: BLE001
        py = None
    if py:
        return py, None
    return None, ("this script imports simplnx, which this interpreter does "
                  "not have, and the DREAM3D-NX env was not found. Create it "
                  "(conda create -n nxpython python=3.12 dream3dnx -c "
                  "conda-forge) or point COUNCIL_NX_PYTHON at its python.exe.")


@dataclass
class Containment:
    """Where one run of a script may write — what nx_guard enforces inside
    the script's own interpreter for a MODEL-written script (nx_policy "Two
    kinds of script"). For a USER script only ``trust`` matters: nx_guard
    then enforces the capability rule and nothing about paths.

      write_roots  every output must land under one: the vault's data_out
                   and this run's staging folder (with no vault known, the
                   run's own output folder and staging folder);
      own_roots    folders this run made (staging, the working folder, an
                   output folder it created): replacing a file inside them
                   is the run's own business;
      own_files    files the run hands on and may replace;
      read_only    inputs — never written, whatever the roots say;
      app_roots    a script that lives here was written by the app, so it
                   is a MODEL script whatever its first line says (data_out);
      search_dirs  where a USER script's relative Pipeline.from_file path is
                   looked for before the run (its original folder);
      origin       the script the user picked, when what runs is a staged
                   copy of it in a temp folder: a refusal says why the MODEL
                   rules apply from where the ORIGINAL lives.
    """
    trust: Optional[str] = None
    write_roots: List[Path] = field(default_factory=list)
    own_roots: List[Path] = field(default_factory=list)
    own_files: List[Path] = field(default_factory=list)
    read_only: List[Path] = field(default_factory=list)
    app_roots: List[Path] = field(default_factory=list)
    search_dirs: List[Path] = field(default_factory=list)
    origin: Optional[Path] = None


def _refusal_text(head: str, reasons: List[str], note: str = "") -> str:
    """"<head> (<note>): <reasons>" — the note (nx_policy.model_rules_note)
    first, so a MODEL script's refusal opens with why it got those rules and
    how the user hands it back to theirs."""
    return (head + (f" ({note}): " if note else ": ") + "; ".join(reasons[:3])
            + (f" (+{len(reasons) - 3} more)" if len(reasons) > 3 else ""))


def _run_pipeline_subprocess(
    pipeline_path: Path,
    timeout_s: int = 600,
    cwd: Optional[Path] = None,
    contain: Optional[Containment] = None,
) -> StepResult:
    """Execute a single .py pipeline as a subprocess.

    Returns a StepResult that the caller fills in step_index / input_label
    fields on. This function only sets success, return_code, duration,
    stdout, stderr, error.

    Refuses, before launching anything, a script nx_policy says the app must
    never run (nx_policy.run_reasons for its kind). A simplnx script, and
    every MODEL script, is launched through nx_guard with ``contain`` (a
    model script with no containment given is held to its working folder);
    whatever nx_guard refused fails the step, even when the script caught
    the refusal and carried on.
    """
    start = time.monotonic()
    base = StepResult(
        step_index=-1,
        pipeline_name=pipeline_path.name,
        input_label="",
        success=False,
        return_code=None,
        duration_s=0.0,
        stdout="",
        stderr="",
        pipeline_path=pipeline_path,
    )
    if not pipeline_path.exists():
        base.error = f"pipeline file not found: {pipeline_path}"
        base.duration_s = time.monotonic() - start
        return base
    try:
        source = _read_source(pipeline_path)
    except OSError as exc:
        base.error = f"could not read the pipeline: {exc}"
        base.duration_s = time.monotonic() - start
        return base
    import nx_policy
    contain = contain or Containment()
    trust = contain.trust or nx_policy.script_trust(
        source, pipeline_path, contain.app_roots)
    search = list(contain.search_dirs) + [pipeline_path.parent] + (
        [Path(cwd)] if cwd else [])
    note = nx_policy.model_rules_note(
        source, contain.origin or pipeline_path, contain.app_roots) \
        if trust == nx_policy.MODEL else ""
    refused = nx_policy.run_reasons(source, trust=trust, search_dirs=search)
    if refused:
        base.error = _refusal_text("refused, nothing was run", refused, note)
        base.duration_s = time.monotonic() - start
        return base
    python, why = interpreter_for(source)
    if python is None:
        base.error = why
        base.duration_s = time.monotonic() - start
        return base
    model = trust == nx_policy.MODEL
    guarded = model or _imports_simplnx(source)
    if guarded and not Path(GUARD).is_file():
        # Never run such a script unguarded. (A bundle built without the
        # file failed every simplnx step with "can't open file", or "ended
        # without the containment's report" for a model script.)
        base.error = (f"the containment script is missing ({GUARD}), and a "
                      f"simplnx or model-written script is never run without "
                      f"it. A bundled build must ship nx_guard.py, "
                      f"nx_policy.py, nx_introspect.py and path_contain.py "
                      f"beside the app (council.spec datas).")
        base.duration_s = time.monotonic() - start
        return base
    tmp: Optional[Path] = None
    report: Optional[Path] = None
    made_cwd: Optional[Path] = None
    cmd = [python, "-u", str(pipeline_path)]
    env = None
    if guarded:
        if model and cwd is None:
            # Never the app's own folder: relative paths land somewhere
            # this run owns.
            cwd = made_cwd = _default_output_dir()
            cwd.mkdir(parents=True, exist_ok=True)
        own_roots = list(contain.own_roots) + ([Path(cwd)] if model else [])
        tmp = Path(tempfile.mkdtemp(prefix="nxguard_"))
        report = tmp / "report.jsonl"
        policy = {
            "trust": trust,
            "nx": _imports_simplnx(source),
            "write_roots": [str(p) for p in (contain.write_roots
                                             or ([cwd] if cwd else []))],
            "own_roots": [str(p) for p in own_roots],
            "own_files": [str(p) for p in contain.own_files],
            "read_only": [str(p) for p in contain.read_only],
            "report": str(report),
        }
        (tmp / "policy.json").write_text(json.dumps(policy),
                                         encoding="utf-8")
        cmd = [python, "-u", str(GUARD), str(tmp / "policy.json"),
               str(pipeline_path)]
        # An import inside the guard must not try to write a .pyc: the
        # containment would (rightly) refuse it, and fail the step.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            # A generated pipeline prints filter names and file paths, either
            # of which can carry a non-cp1252 byte. Without this the reader
            # thread dies and proc.stdout is None — and `proc.stdout or ""`
            # below turns that into a SILENT empty log for a run that
            # produced plenty of output.
            encoding="utf-8",
            errors="replace",
        )
        base.return_code = proc.returncode
        base.stdout = proc.stdout or ""
        base.stderr = proc.stderr or ""
        base.success = proc.returncode == 0
        if report is not None:
            import nx_guard
            refusals, done = nx_guard.read_report(report)
            if refusals:
                base.success = False
                base.error = _refusal_text("refused at run time", refusals,
                                           note)
            elif model and base.success and not done:
                # The guard writes its last line as the script ends; a
                # script that ended the interpreter under it is not vouched
                # for.
                base.success = False
                base.error = ("the run ended without the containment's "
                              "report, so it is not trusted")
        if not base.success and not base.error:
            base.error = f"pipeline exited with code {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        base.error = f"timed out after {timeout_s}s"
        base.stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except Exception as exc:
        base.error = f"subprocess launch failed: {exc!r}"
    finally:
        base.duration_s = time.monotonic() - start
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        if made_cwd is not None:
            _drop_if_empty(made_cwd)
    return base


# ============================================================
# Pipeline path resolution
# ============================================================

def resolve_pipeline_path(name_or_path: str, vault_dir: Path) -> Optional[Path]:
    """Resolve a user-typed pipeline reference to a Path.

    Accepts:
      - Absolute or relative .py path
      - Bare pipeline name (matched against vault/pipelines/in and
        vault/pipelines/out via case-insensitive substring)
    """
    cand = Path(name_or_path).expanduser()
    if cand.is_file():
        return cand.resolve()

    from pipeline_scanner import (
        vault_pipelines_in_dir, vault_pipelines_out_dir, scan_pipelines,
    )

    q = name_or_path.strip().lower()
    for root in (vault_pipelines_in_dir(vault_dir),
                 vault_pipelines_out_dir(vault_dir)):
        for pl in scan_pipelines(root):
            if pl.path.suffix.lower() != ".py":
                continue
            if pl.name.lower() == q or q in pl.name.lower():
                return pl.path
    return None


# ============================================================
# Linear runner
# ============================================================

def _vault_areas(vault_dir: Optional[Path]
                 ) -> Tuple[Optional[Path], Optional[Path]]:
    """(data_out, data_in) of ``vault_dir``, or (None, None)."""
    if vault_dir is None:
        return None, None
    import data_index
    return (Path(data_index.output_dir(vault_dir)),
            Path(data_index.input_dir(vault_dir)))


def _outside_output_area(out_root: Optional[Path],
                         vault_dir: Optional[Path]) -> Optional[str]:
    """Why ``out_root`` may not be this run's output folder, or None: with a
    vault known it must be inside the vault's data_out, as
    nx_bridge.run_folder requires of a saved pipeline's runs."""
    data_out, _ = _vault_areas(vault_dir)
    if data_out is None or out_root is None:
        return None
    import path_contain
    if path_contain.is_under(out_root, data_out):
        return None
    return (f"refusing to write outside the vault output area: {out_root} "
            f"is not inside {data_out}")


def _containment(trust: str, *, vault_dir: Optional[Path], out_root: Path,
                 stage_dir: Optional[Path], own: List[Path],
                 input_dir: Optional[Path] = None,
                 own_files: Optional[List[Path]] = None,
                 search_dirs: Optional[List[Path]] = None,
                 origin: Optional[Path] = None) -> Containment:
    """The Containment for one step of this run (see Containment)."""
    data_out, data_in = _vault_areas(vault_dir)
    roots = [data_out] if data_out is not None else [out_root]
    if stage_dir is not None:
        roots.append(stage_dir)
    return Containment(
        trust=trust, write_roots=roots, own_roots=list(own),
        own_files=list(own_files or []),
        read_only=[p for p in (input_dir, data_in) if p is not None],
        app_roots=[data_out] if data_out is not None else [],
        search_dirs=list(search_dirs or []), origin=origin)


def _trust(pl: Path, vault_dir: Optional[Path]) -> str:
    """MODEL or USER for the ORIGINAL pipeline file (a staged copy lives in
    a temp folder, so the location rule must be asked of the original)."""
    import nx_policy
    data_out, _ = _vault_areas(vault_dir)
    try:
        source = _read_source(pl)
    except OSError:
        source = ""
    return nx_policy.script_trust(source, pl,
                                  [data_out] if data_out is not None else [])


def _run_output_root(output_dir: Optional[Path],
                     vault_dir: Optional[Path]) -> Path:
    """Where this run's outputs go: ``output_dir``, else a fresh folder under
    the vault's data_out/workflows, else a fresh temp folder."""
    if output_dir is not None:
        return Path(output_dir)
    data_out, _ = _vault_areas(vault_dir)
    if data_out is not None:
        return _fresh(data_out / "workflows" / time.strftime("%Y%m%d_%H%M%S"))
    return _default_output_dir()


def _fresh(path: Path) -> Path:
    """``path``, or path_2, path_3 ... — the first that does not exist."""
    cand, n = path, 2
    while cand.exists():
        cand, n = path.with_name(f"{path.name}_{n}"), n + 1
    return cand


def run_linear(
    pipelines: List[Path],
    *,
    timeout_s: int = 600,
    on_step: Optional[Callable[[StepResult], None]] = None,
    output_dir: Optional[Path] = None,
    vault_dir: Optional[Path] = None,
) -> WorkflowResult:
    """Run each pipeline once, in order. Stop on the first failure.

    A USER script runs as before (its own paths, the app's working folder).
    A MODEL script runs in a fresh working folder under the run's output
    folder and writes only under the vault's data_out (Containment)."""
    overall_start = time.monotonic()
    result = WorkflowResult(success=True, total_steps=len(pipelines), steps_run=0,
                            duration_s=0.0)
    out_root: Optional[Path] = None
    out_fresh = False
    try:
        for i, p in enumerate(pipelines, start=1):
            import nx_policy
            trust = _trust(p, vault_dir)
            work: Optional[Path] = None
            if trust == nx_policy.MODEL:
                if out_root is None:
                    out_root = _run_output_root(output_dir, vault_dir)
                    why = _outside_output_area(out_root, vault_dir)
                    if why:
                        step = _not_run(i, p, "(static)", why)
                        result.step_results.append(step)
                        result.steps_run += 1
                        result.success, result.error = False, why
                        break
                    out_fresh = not out_root.exists()
                    out_root.mkdir(parents=True, exist_ok=True)
                work = _fresh(out_root / p.stem)
                work.mkdir(parents=True)
                contain = _containment(
                    trust, vault_dir=vault_dir, out_root=out_root,
                    stage_dir=None,
                    own=[work] + ([out_root] if out_fresh else []),
                    origin=p)
                step = _run_pipeline_subprocess(p, timeout_s=timeout_s,
                                                cwd=work, contain=contain)
                if step.success:
                    _record_outputs(result, [], work)
                _drop_if_empty(work)
            else:
                step = _run_pipeline_subprocess(
                    p, timeout_s=timeout_s,
                    contain=Containment(trust=trust))
            step.step_index = i
            step.input_label = "(static)"
            result.step_results.append(step)
            result.steps_run += 1
            if on_step:
                try:
                    on_step(step)
                except Exception:
                    pass
            if not step.success:
                result.success = False
                result.error = f"step #{i} ({p.name}) failed" + (
                    f": {step.error}" if step.error and "refused" in step.error
                    else "")
                break
    finally:
        if out_root is not None and output_dir is None:
            _drop_if_empty(out_root)
    result.duration_s = time.monotonic() - overall_start
    return result


# ============================================================
# Per-file directory runner (full workflow per input file)
# ============================================================

def _list_directory_inputs(
    directory: Path, *, pattern: str = "*", recursive: bool = False,
) -> List[Path]:
    if recursive:
        return sorted(p for p in directory.rglob(pattern) if p.is_file())
    return sorted(p for p in directory.glob(pattern) if p.is_file())


def _stage_per_input_pipeline(
    pipeline_path: Path,
    input_file: Path,
    stage_dir: Path,
    *,
    substitution_param: str = "file_path",
) -> Path:
    """Make a temp copy of `pipeline_path` with its input path pointed at
    `input_file`: the first `substitution_param = <value>` when the script
    has one, else the first real reader parameter (_INPUT_PARAM_CANDIDATES).

    A script with neither used to be copied as-is and run — reading the path
    baked into it, for every input. The copy is still made, but the callers
    check it (_input_not_redirected) and refuse to run it.
    """
    return _stage(
        pipeline_path, stage_dir, input_value=input_file,
        input_param=_input_param(_read_source(pipeline_path),
                                 substitution_param)).path


# Parameter names a DREAM3D read/write step uses, discovered from the real
# filters (see nx_worker.path_params). The INPUT is what a Read* filter reads;
# the OUTPUT is what a Write* filter writes. Chaining rewrites A's output to a
# staging file and then B's input to that same file.
#
# Checked 2026-10-06 against the installed catalog (289 filters): the single-
# file readers' path parameters are file_path (inside ReadDREAM3DFilter's
# ImportData), input_file (10 readers), input_file_path (2), stl_file_path
# (ReadStlFileFilter), input_header_file (ReadBinaryCTNorthstarFilter) and
# vg_header_file (ReadVolumeGraphicsFileFilter). The last three were missing,
# and a missing name was not an error: the staged copy kept its baked-in path,
# so a chain over two different .stl files reported success while BOTH runs
# read the same file (measured with the shipped CreateScanVectors pipeline).
# NOT "file_name": ITKImageReaderFilter reads from it, but ITKImageWriterFilter
# WRITES to it, so a script with only the writer would have its output pointed
# at the user's input file.
_INPUT_PARAM_CANDIDATES = ("file_path", "input_file_path", "input_file",
                           "import_file_path", "input_path", "stl_file_path",
                           "input_header_file", "vg_header_file")
_OUTPUT_PARAM_CANDIDATES = ("export_file_path", "output_file_path", "output_file",
                            "output_path", "write_file_path", "feature_data_file")


@dataclass
class _Site:
    """One place a script gives a path parameter its value."""
    param: str
    start: int                    # character offsets of the VALUE
    end: int
    line: int
    literal: Optional[str]        # the value, when it is a string literal


def _param_sites(source: str, names) -> List[_Site]:
    """Every place ``source`` gives one of ``names`` a value, in source
    order: a keyword argument (``f(file_path=...)``), an attribute
    assignment (``v.input_file_path = ...``) or a plain one
    (``input_file = ...``).

    Read from the syntax tree, so a comment or a string that mentions
    ``file_path = ...`` is not a site. The regex this replaces matched
    those: a top comment "Settings: file_path = the .dream3d to read" was
    rewritten instead of the reader, and every input read the same file
    (measured: identical Centroids for two inputs, chain reported 4/4 ok).
    Raises SyntaxError."""
    import ast
    tree = ast.parse(source)
    names = set(names)
    lines = source.split("\n")
    starts = [0]
    for ln in lines[:-1]:
        starts.append(starts[-1] + len(ln) + 1)

    def offset(lineno: int, col: int) -> int:
        # ast columns are UTF-8 byte offsets within the line
        line = lines[lineno - 1]
        return starts[lineno - 1] + len(
            line.encode("utf-8")[:col].decode("utf-8", errors="replace"))

    sites: List[_Site] = []

    def add(param: str, value) -> None:
        lit = value.value if isinstance(value, ast.Constant) \
            and isinstance(value.value, str) else None
        sites.append(_Site(param, offset(value.lineno, value.col_offset),
                           offset(value.end_lineno, value.end_col_offset),
                           value.lineno, lit))

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in names:
            add(node.arg, node.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) \
                and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) \
                else [node.target]
            for t in targets:
                name = t.attr if isinstance(t, ast.Attribute) else (
                    t.id if isinstance(t, ast.Name) else None)
                if name in names:
                    add(name, node.value)
                    break
    sites.sort(key=lambda s: s.start)
    return sites


def _first_param_present(source: str, candidates) -> Optional[str]:
    """The first of ``candidates`` (in their order) the script gives a
    value — in its syntax, not in a comment."""
    try:
        present = {s.param for s in _param_sites(source, candidates)}
    except SyntaxError:
        return None
    return next((c for c in candidates if c in present), None)


def _input_param(source: str, substitution_param: str) -> Optional[str]:
    """The user's substitution_param when the script has it, else None (so
    the staging falls back to the real reader names)."""
    return substitution_param \
        if _first_param_present(source, (substitution_param,)) else None


def _main_output(sites: List[_Site]) -> Optional[_Site]:
    """The output a pipeline ENDS with: the first output name (in
    _OUTPUT_PARAM_CANDIDATES order) it has, at its LAST site. A pipeline that
    writes a checkpoint and then its result has export_file_path twice; the
    first one used to be handed on, so the next pipeline read the
    checkpoint — without what was computed after it — and the chain
    reported success."""
    for c in _OUTPUT_PARAM_CANDIDATES:
        mine = [s for s in sites if s.param == c]
        if mine:
            return mine[-1]
    return None


def _side_dest(side_dir: Path, stem: Optional[str], site: _Site,
               used: set) -> Path:
    """Where a writer other than the main output goes for one input:
    <side_dir>/<stem>_<the file name it had> (a.dream3d's checkpoint
    'Data/ckpt.dream3d' -> a_ckpt.dream3d)."""
    base = Path(site.literal).name if site.literal else ""
    base = base or site.param
    dest = side_dir / (f"{stem}_{base}" if stem else base)
    n = 2
    # Not a name this run already gave out, and not a file already there:
    # the runner choosing a destination must never be what overwrites one.
    while dest in used or dest.exists():
        p = Path(base)
        dest = side_dir / (f"{stem}_{p.stem}_{n}{p.suffix}" if stem
                           else f"{p.stem}_{n}{p.suffix}")
        n += 1
    used.add(dest)
    return dest


@dataclass
class _Staging:
    path: Path
    out_param: Optional[str] = None      # the parameter pointed at output_value
    out_line: Optional[int] = None
    side_outputs: List[Path] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None          # the script is not valid Python


def _stage(pipeline_path: Path, stage_dir: Path, *,
           input_value: Optional[Path] = None,
           input_param: Optional[str] = None,
           output_value: Optional[Path] = None,
           side_dir: Optional[Path] = None,
           stem: Optional[str] = None,
           used: Optional[set] = None,
           dest_name: Optional[str] = None) -> _Staging:
    """Copy a pipeline with its paths pointed where this run needs them.

      * the input: the first site of ``input_param`` (else of the first
        reader name in _INPUT_PARAM_CANDIDATES it has) -> ``input_value``;
      * the main output (_main_output: the LAST writer) -> ``output_value``;
      * with ``side_dir``: every OTHER writer -> <side_dir>/<stem>_<name>.
        Those kept their baked path, so each input overwrote the previous
        one's file (one features.csv for two inputs, not in the outputs).

    Only the value expressions are replaced; comments, strings and the
    rest of the script are untouched."""
    source = _read_source(pipeline_path)
    target = stage_dir / (dest_name or pipeline_path.name)
    st = _Staging(target)
    names = set(_INPUT_PARAM_CANDIDATES) | set(_OUTPUT_PARAM_CANDIDATES)
    if input_param:
        names.add(input_param)
    try:
        sites = _param_sites(source, names)
    except SyntaxError as exc:
        target.write_text(source, encoding="utf-8")
        st.error = (f"{pipeline_path.name} is not valid Python (line "
                    f"{exc.lineno}: {exc.msg}), so its file paths can't be "
                    f"found or pointed anywhere.")
        return st
    edits: List[Tuple[_Site, Any]] = []
    if input_value is not None:
        order = (input_param,) if input_param else _INPUT_PARAM_CANDIDATES
        ip = next((n for n in order if any(s.param == n for s in sites)),
                  None)
        if ip:
            edits.append((next(s for s in sites if s.param == ip),
                          input_value))
    outs = [s for s in sites if s.param in _OUTPUT_PARAM_CANDIDATES
            and s.param != input_param]
    main = _main_output(outs) if output_value is not None else None
    if main is not None:
        edits.append((main, output_value))
        st.out_param, st.out_line = main.param, main.line
    if side_dir is not None:
        used = used if used is not None else set()
        for s in outs:
            if s is not main:
                dest = _side_dest(side_dir, stem, s, used)
                edits.append((s, dest))
                st.side_outputs.append(dest)
    if main is not None:
        same = [s for s in outs if s.param == main.param]
        if len(same) > 1:
            others = [Path(s.literal).name if s.literal else s.param
                      for s in same if s is not main]
            st.notes.append(
                f"{pipeline_path.name}: {main.param} is written "
                f"{len(same)} times (lines "
                f"{', '.join(str(s.line) for s in same)}); the last, line "
                f"{main.line}, is what it ends with, so that is the one "
                f"this run follows"
                + (f"; the others are kept per input as "
                   f"{', '.join('<input>_' + o for o in others)}"
                   if side_dir is not None else ""))
    new, limit = source, len(source)
    for site, value in sorted(edits, key=lambda e: e[0].start, reverse=True):
        if site.end > limit:
            continue                     # nested in a value already replaced
        new = new[:site.start] + repr(str(value)) + new[site.end:]
        limit = site.start
    target.write_text(new, encoding="utf-8")
    return st


def _stage_chain_pipeline(pipeline_path: Path, stage_dir: Path, *,
                          input_value: Optional[Path] = None,
                          input_param: Optional[str] = None,
                          output_value: Optional[Path] = None,
                          dest_name: Optional[str] = None
                          ) -> Tuple[Path, Optional[str]]:
    """Copy a pipeline with its input and/or output path substituted.

    Returns (staged_path, output_param_used). output_param_used is None when no
    output-path parameter could be found to override — the caller needs that to
    know whether A's output was actually redirected to the staging file it will
    hand to B."""
    st = _stage(pipeline_path, stage_dir, input_value=input_value,
                input_param=input_param, output_value=output_value,
                dest_name=dest_name)
    return st.path, st.out_param


def _output_dest(pipeline_path: Path, out_dir: Path, stem: str,
                 used: set) -> Optional[Path]:
    """Where this pipeline's output goes for one input: <out_dir>/<stem> with
    the suffix the script's own output path had (.dream3d, .stl, ...). None
    when the script has no output parameter to point there."""
    source = _read_source(pipeline_path)
    try:
        main = _main_output(_param_sites(source, _OUTPUT_PARAM_CANDIDATES))
    except SyntaxError:
        return None
    if main is None:
        return None
    suffix = Path(main.literal or "").suffix or ".dream3d"
    dest, n = out_dir / f"{stem}{suffix}", 2
    # a.dream3d and a.stl in one folder; or a file already there, which the
    # runner never chooses to overwrite.
    while dest in used or dest.exists():
        dest, n = out_dir / f"{stem}_{n}{suffix}", n + 1
    used.add(dest)
    return dest


def _input_not_redirected(pl: Path, staged: Path, src_in: Path,
                          params=()) -> Optional[str]:
    """Why the staged copy would NOT read ``src_in``, or None.

    Checked on the staged copy's syntax tree: a reader parameter (one of
    ``params`` or _INPUT_PARAM_CANDIDATES) must now hold exactly
    ``src_in``. A text search for the path passed when the path landed in a
    comment. A reader whose parameter is not a recognised name is staged
    with its baked-in path untouched, so the run "succeeds" on the wrong
    file -- every input the same file. Measured: a chain over two different
    .stl files reported 4/4 ok while both runs read the path saved in the
    pipeline."""
    names = tuple(p for p in params if p) + _INPUT_PARAM_CANDIDATES
    staged_src = _read_source(staged)
    try:
        sites = _param_sites(staged_src, names)
    except SyntaxError as exc:
        return (f"{pl.name} is not valid Python (line {exc.lineno}: "
                f"{exc.msg}), so it can't be pointed at {src_in.name}.")
    if any(s.literal == str(src_in) for s in sites):
        return None
    return (f"{pl.name} has no recognized input-path parameter "
            f"(looked for {', '.join(names)}), "
            f"so it can't be pointed at {src_in.name}; it would "
            f"read the path saved in it instead.")


def _work_dir(base: Path, used: set) -> Path:
    """A run's own working folder, not shared with another input's, and not
    a folder that was there before the run (it is the run's own: what is
    inside may be replaced)."""
    d, n = base, 2
    while d in used or d.exists():
        d, n = base.with_name(f"{base.name}_{n}"), n + 1
    used.add(d)
    return d


def _unique_stems(inputs: List[Path], input_dir: Path) -> Dict[Path, str]:
    """A name for each input that no other input shares: its stem, or — for
    inputs with the same stem in different folders (a recursive scan of
    x/a.dream3d and y/a.dream3d) — its folders too, x_a and y_a.

    Every staged file and every output is named from this. Named from the
    bare stem, a folder-scope chain staged both inputs to step1/a.dream3d,
    the second overwrote the first, and the next pipeline read the second
    input twice."""
    counts: Dict[str, int] = {}
    for p in inputs:
        counts[p.stem.lower()] = counts.get(p.stem.lower(), 0) + 1
    names: Dict[Path, str] = {}
    taken: set = set()
    for p in inputs:
        name = p.stem
        if counts[p.stem.lower()] > 1:
            try:
                rel = p.relative_to(input_dir).with_suffix("")
                name = "_".join(rel.parts)
            except ValueError:
                name = f"{p.parent.name}_{p.stem}"
        cand, n = name, 2
        while cand.lower() in taken:
            cand, n = f"{name}_{n}", n + 1
        taken.add(cand.lower())
        names[p] = cand
    return names


def _record_outputs(result: "WorkflowResult", paths, work: Optional[Path]
                    ) -> None:
    """List every file a run wrote: the files its writers were pointed at
    (a writer pointed at a folder: the files in it) and anything it wrote in
    its own working folder."""
    seen = set(result.outputs)

    def add(p: Path) -> None:
        if p not in seen:
            seen.add(p)
            result.outputs.append(p)
    for p in paths:
        if p.is_file():
            add(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    add(f)
    if work is not None and work.is_dir():
        for f in sorted(work.rglob("*")):
            if f.is_file():
                add(f)


def _add_notes(result: "WorkflowResult", notes) -> None:
    for n in notes:
        if n not in result.notes:
            result.notes.append(n)


def _not_run(idx: int, pl: Path, label: str, error: str) -> StepResult:
    bad = StepResult(step_index=idx, pipeline_name=pl.name, input_label=label,
                     success=False, return_code=None, duration_s=0.0,
                     stdout="", stderr="", pipeline_path=pl)
    bad.error = error
    return bad


def _default_output_dir() -> Path:
    """A fresh folder for a run's outputs when the caller names none. NOT the
    staging area: that is deleted when the run ends."""
    import tempfile
    import uuid
    return (Path(tempfile.gettempdir()) / "council_wf_out"
            / uuid.uuid4().hex[:12])


def _drop_if_empty(folder: Path) -> None:
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def run_chained(
    pipelines: List[Path],
    input_dir: Path,
    *,
    scope: str = "per_file",           # "per_file" | "folder"
    pattern: str = "*.dream3d",
    recursive: bool = False,
    substitution_param: str = "file_path",
    timeout_s: int = 600,
    stage_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    on_step: Optional[Callable[[StepResult], None]] = None,
    vault_dir: Optional[Path] = None,
) -> WorkflowResult:
    """Chain pipelines so each one reads the PREVIOUS pipeline's OUTPUT.

    This is the piece the other modes do not do: run pipeline 1, then run
    pipeline 2 on what pipeline 1 wrote (not on the original files).

      scope="per_file" (default): each input file flows through the whole chain
        independently — file -> P1 -> out1 -> P2 -> out2 -> ...  Best when each
        file is an independent sample.
      scope="folder": pipeline 1 runs over every input file into a stage
        directory, then pipeline 2 runs over ALL of pipeline 1's outputs, and
        so on. Best when a later pipeline needs the whole previous set.

    A non-final pipeline MUST expose an output-path parameter (so its output can
    be redirected to a staging file and handed on); if none is found the chain
    stops with a clear error rather than silently running the next pipeline on
    the wrong input. What is handed on is the LAST writer of that parameter —
    what the pipeline ends with, not a checkpoint written halfway — and the
    step fails, naming itself, if it did not write that file.

    The FINAL pipeline's output goes to ``output_dir``/<input stem><suffix>
    (a fresh temp folder when None; result.outputs lists the files). It used
    to keep the path baked into the script, resolved against wherever the app
    was started — outside the vault's output area — and in per_file mode every
    input overwrote the same file: two inputs, one surviving output. Every
    other writer of every pipeline goes to ``output_dir``/<stem>_<its file
    name>, and each input's steps run in their own working folder,
    ``output_dir``/<stem>/, so a relative path a script still holds lands
    there, per input, too. <stem> is unique per input (_unique_stems): two
    inputs named a.dream3d in different folders become x_a and y_a.

    With ``vault_dir``, ``output_dir`` must be inside its data_out, and a
    model-written pipeline writes nowhere else (Containment, nx_guard).
    """
    overall_start = time.monotonic()
    inputs = _list_directory_inputs(input_dir, pattern=pattern,
                                    recursive=recursive)
    result = WorkflowResult(success=True,
                            total_steps=len(pipelines) * max(1, len(inputs)),
                            steps_run=0, duration_s=0.0)
    out_root = _run_output_root(output_dir, vault_dir)
    why = _outside_output_area(out_root, vault_dir)
    if why:
        result.success, result.error = False, why
        return result
    owned_stage = stage_dir is None
    if stage_dir is None:
        stage_dir = _default_stage_dir()
    stage_dir.mkdir(parents=True, exist_ok=True)

    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        return result
    if not pipelines:
        result.success = False
        result.error = "a chained workflow needs at least one pipeline"
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        return result

    out_fresh = not out_root.exists()
    out_root.mkdir(parents=True, exist_ok=True)
    stems = _unique_stems(inputs, input_dir)
    trusts = {pl: _trust(pl, vault_dir) for pl in pipelines}
    used: set = set()
    works: Dict[str, Path] = {}         # input stem -> its working folder
    step_counter = 0

    def _emit(step: StepResult) -> None:
        result.step_results.append(step)
        result.steps_run += 1
        if on_step:
            try:
                on_step(step)
            except Exception:
                pass

    def _run_one(pl: Path, src_in: Path, out_path: Optional[Path],
                 file_stage: Path, label: str, idx: int, stem: str,
                 final: bool) -> StepResult:
        if final:
            out_path = _output_dest(pl, out_root, stem, used)
        st = _stage(pl, file_stage, input_value=src_in, output_value=out_path,
                    input_param=_input_param(_read_source(pl),
                                             substitution_param),
                    side_dir=out_root, stem=stem, used=used,
                    dest_name=f"{idx:02d}_{pl.name}")
        if st.error:
            return _not_run(idx, pl, label, st.error)
        # A non-final pipeline whose output we could not redirect leaves us not
        # knowing what to feed onward — fail loudly instead of chaining garbage.
        if not final and out_path is not None and st.out_param is None:
            return _not_run(idx, pl, label,
                            f"{pl.name} has no recognized output-path parameter "
                            f"(looked for {', '.join(_OUTPUT_PARAM_CANDIDATES)}), "
                            f"so its result can't be chained into the next "
                            f"pipeline.")
        why = _input_not_redirected(pl, st.path, src_in, (substitution_param,))
        if why:
            return _not_run(idx, pl, label, why)
        _add_notes(result, st.notes)
        if stem not in works:
            works[stem] = _work_dir(out_root / stem, used)
        work = works[stem]
        work.mkdir(parents=True, exist_ok=True)
        contain = _containment(
            trusts[pl], vault_dir=vault_dir, out_root=out_root,
            stage_dir=stage_dir, input_dir=input_dir,
            own=[stage_dir, work] + ([out_root] if out_fresh else []),
            own_files=[p for p in [out_path] + st.side_outputs if p],
            search_dirs=[pl.parent], origin=pl)
        step = _run_pipeline_subprocess(st.path, timeout_s=timeout_s,
                                        cwd=work, contain=contain)
        step.step_index = idx
        step.input_label = label
        if step.success and not final and not out_path.is_file():
            # The next pipeline would fail on a missing file and the error
            # would name IT; the fault is here.
            step.success = False
            step.error = (f"{pl.name} finished without writing "
                          f"{out_path.name}, the file the next pipeline "
                          f"reads: its {st.out_param} (line {st.out_line}) "
                          f"was pointed there and nothing was written")
        if step.success:
            _record_outputs(result, ([out_path] if final and st.out_param
                                     else []) + st.side_outputs, work)
        _drop_if_empty(work)
        return step

    try:
        if scope == "folder":
            # Each pipeline runs over the previous stage's whole output set.
            # Each input keeps ITS unique name through the chain, so the
            # staged files never collide and the outputs say which input
            # they came from.
            cur_inputs = [(p, stems[p]) for p in inputs]
            for j, pl in enumerate(pipelines, start=1):
                is_last = (j == len(pipelines))
                out_dir = stage_dir / f"step{j}"
                out_dir.mkdir(parents=True, exist_ok=True)
                next_inputs: List[Tuple[Path, str]] = []
                for src_in, stem in cur_inputs:
                    step_counter += 1
                    out_path = None if is_last else out_dir / f"{stem}.dream3d"
                    step = _run_one(pl, src_in, out_path, out_dir,
                                    f"{pl.name} <- {src_in.name}", step_counter,
                                    stem, is_last)
                    _emit(step)
                    if not step.success:
                        result.success = False
                        result.error = step.error or f"step #{step_counter} failed"
                        result.duration_s = time.monotonic() - overall_start
                        return result
                    if out_path is not None:
                        next_inputs.append((out_path, stem))
                cur_inputs = next_inputs
        else:
            # per_file: each file flows through the entire chain on its own.
            for src_file in inputs:
                stem = stems[src_file]
                file_stage = stage_dir / stem
                file_stage.mkdir(parents=True, exist_ok=True)
                prev = src_file
                for j, pl in enumerate(pipelines, start=1):
                    step_counter += 1
                    is_last = (j == len(pipelines))
                    out_path = None if is_last else \
                        file_stage / f"{stem}_step{j}.dream3d"
                    step = _run_one(pl, prev, out_path, file_stage,
                                    f"{pl.name} <- {prev.name}", step_counter,
                                    stem, is_last)
                    _emit(step)
                    if not step.success:
                        result.success = False
                        result.error = step.error or f"step #{step_counter} failed"
                        result.duration_s = time.monotonic() - overall_start
                        return result
                    if out_path is not None:
                        prev = out_path
    finally:
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        if output_dir is None:
            _drop_if_empty(out_root)
    result.duration_s = time.monotonic() - overall_start
    return result


def _default_stage_dir() -> Path:
    """Return a fresh staging directory under the system temp area.

    Each invocation gets its own subdirectory so concurrent runs don't
    stomp on each other. The caller is expected to clean it up.
    """
    import tempfile, uuid
    base = Path(tempfile.gettempdir()) / "council_wf_stage" / uuid.uuid4().hex[:12]
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cleanup_stage_dir(stage_dir: Path) -> None:
    try:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
    except Exception:
        pass


def _run_over_inputs(pairs, *, substitution_param: str, timeout_s: int,
                     stage_dir: Path, out_root: Path,
                     result: WorkflowResult, label_of, fail_msg,
                     on_step, stems: Optional[Dict[Path, str]] = None,
                     contain_for: Optional[Callable[..., Containment]] = None
                     ) -> bool:
    """Run each (pipeline, input) with its input pointed at the input file,
    its output at <out_root>/<pipeline stem>/<input stem><suffix>, every
    other writer at <out_root>/<pipeline stem>/<input stem>_<file name>, in
    its own working folder <out_root>/<pipeline stem>/<input stem>/. False
    on the first failure (result.error says which). <input stem> is the
    input's unique name (``stems``, see _unique_stems)."""
    used: set = set()
    stems = stems or {}
    for step_counter, (pipeline_path, input_file) in enumerate(pairs, start=1):
        stem = stems.get(input_file, input_file.stem)
        file_stage = stage_dir / stem
        file_stage.mkdir(parents=True, exist_ok=True)
        label = label_of(pipeline_path, input_file)
        pdir = out_root / pipeline_path.stem
        dest = _output_dest(pipeline_path, pdir, stem, used)
        source = _read_source(pipeline_path)
        st = _stage(pipeline_path, file_stage, input_value=input_file,
                    input_param=_input_param(source, substitution_param),
                    output_value=dest, side_dir=pdir, stem=stem,
                    used=used)
        why = st.error or _input_not_redirected(
            pipeline_path, st.path, input_file, (substitution_param,))
        if why:
            step = _not_run(step_counter, pipeline_path, label, why)
        else:
            _add_notes(result, st.notes)
            work = _work_dir(pdir / stem, used)
            work.mkdir(parents=True, exist_ok=True)
            contain = contain_for(pipeline_path, work,
                                  [p for p in [dest] + st.side_outputs if p]) \
                if contain_for is not None else None
            step = _run_pipeline_subprocess(st.path, timeout_s=timeout_s,
                                            cwd=work, contain=contain)
            step.step_index = step_counter
            step.input_label = label
            if step.success:
                _record_outputs(result, ([dest] if st.out_param else [])
                                + st.side_outputs, work)
            _drop_if_empty(work)
            _drop_if_empty(pdir)
        result.step_results.append(step)
        result.steps_run += 1
        if on_step:
            try:
                on_step(step)
            except Exception:
                pass
        if not step.success:
            result.success = False
            result.error = step.error if why else fail_msg(
                step_counter, pipeline_path, input_file) + (
                f": {step.error}" if step.error and "refused" in step.error
                else "")
            return False
    return True


def run_per_file(
    pipelines: List[Path],
    input_dir: Path,
    *,
    pattern: str = "*.dream3d",
    recursive: bool = False,
    substitution_param: str = "file_path",
    timeout_s: int = 600,
    stage_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    on_step: Optional[Callable[[StepResult], None]] = None,
    vault_dir: Optional[Path] = None,
) -> WorkflowResult:
    """For each input file in directory: run the whole pipeline list.

    Each run reads that input and writes <output_dir>/<pipeline stem>/<input
    stem><suffix>: one output per input, never the script's baked path (which
    every input used to overwrite)."""
    return _run_directory_mode(
        pipelines, input_dir, by_file=True, pattern=pattern,
        recursive=recursive, substitution_param=substitution_param,
        timeout_s=timeout_s, stage_dir=stage_dir, output_dir=output_dir,
        on_step=on_step, vault_dir=vault_dir)


def run_per_step(
    pipelines: List[Path],
    input_dir: Path,
    *,
    pattern: str = "*.dream3d",
    recursive: bool = False,
    substitution_param: str = "file_path",
    timeout_s: int = 600,
    stage_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    on_step: Optional[Callable[[StepResult], None]] = None,
    vault_dir: Optional[Path] = None,
) -> WorkflowResult:
    """For each pipeline: run it on every input file. Then move to next
    pipeline. Outputs as run_per_file."""
    return _run_directory_mode(
        pipelines, input_dir, by_file=False, pattern=pattern,
        recursive=recursive, substitution_param=substitution_param,
        timeout_s=timeout_s, stage_dir=stage_dir, output_dir=output_dir,
        on_step=on_step, vault_dir=vault_dir)


def _run_directory_mode(pipelines, input_dir, *, by_file: bool, pattern,
                        recursive, substitution_param, timeout_s, stage_dir,
                        output_dir, on_step, vault_dir=None) -> WorkflowResult:
    overall_start = time.monotonic()
    inputs = _list_directory_inputs(input_dir, pattern=pattern, recursive=recursive)
    total_steps = len(pipelines) * len(inputs)
    result = WorkflowResult(success=True, total_steps=total_steps, steps_run=0,
                            duration_s=0.0)
    out_root = _run_output_root(output_dir, vault_dir)
    why = _outside_output_area(out_root, vault_dir)
    if why:
        result.success, result.error = False, why
        return result
    owned_stage = stage_dir is None
    if stage_dir is None:
        stage_dir = _default_stage_dir()
    stage_dir.mkdir(parents=True, exist_ok=True)

    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        return result

    out_fresh = not out_root.exists()
    out_root.mkdir(parents=True, exist_ok=True)
    trusts = {pl: _trust(pl, vault_dir) for pl in pipelines}

    def contain_for(pl: Path, work: Path, own_files: List[Path]
                    ) -> Containment:
        return _containment(
            trusts[pl], vault_dir=vault_dir, out_root=out_root,
            stage_dir=stage_dir, input_dir=input_dir,
            own=[stage_dir, work] + ([out_root] if out_fresh else []),
            own_files=own_files, search_dirs=[pl.parent], origin=pl)

    if by_file:
        pairs = [(p, f) for f in inputs for p in pipelines]

        def label_of(p, f):
            return f.name

        def fail_msg(n, p, f):
            return f"step #{n} ({p.name}) failed on input {f.name}"
    else:
        pairs = [(p, f) for p in pipelines for f in inputs]

        def label_of(p, f):
            return f"{p.name} <- {f.name}"

        def fail_msg(n, p, f):
            return (f"step #{n}: pipeline {p.name} failed on input "
                    f"{f.name}")
    try:
        _run_over_inputs(pairs, substitution_param=substitution_param,
                         timeout_s=timeout_s, stage_dir=stage_dir,
                         out_root=out_root, result=result,
                         label_of=label_of, fail_msg=fail_msg,
                         on_step=on_step,
                         stems=_unique_stems(inputs, input_dir),
                         contain_for=contain_for)
    finally:
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        if output_dir is None:
            _drop_if_empty(out_root)
    result.duration_s = time.monotonic() - overall_start
    return result


# ============================================================
# Top-level dispatch + workflow parser
# ============================================================

@dataclass
class WorkflowSpec:
    pipeline_paths: List[Path]
    mode: str = "linear"           # "linear" | "per_file" | "per_step" | "chained"
    input_dir: Optional[Path] = None
    pattern: str = "*"
    recursive: bool = False
    substitution_param: str = "file_path"
    timeout_s: int = 600
    chain_scope: str = "per_file"  # for mode="chained": "per_file" | "folder"
    # Where the run's outputs go (a temp folder if None, or a fresh folder
    # under the vault's data_out/workflows when vault_dir is set). The app
    # passes a folder under the vault's data_out.
    output_dir: Optional[Path] = None
    # The vault the run belongs to: output_dir must be inside its data_out,
    # and a model-written pipeline writes nowhere else (Containment).
    vault_dir: Optional[Path] = None


def run_workflow(spec: WorkflowSpec,
                 on_step: Optional[Callable[[StepResult], None]] = None,
                 ) -> WorkflowResult:
    if spec.mode == "linear":
        return run_linear(spec.pipeline_paths, timeout_s=spec.timeout_s,
                          on_step=on_step, output_dir=spec.output_dir,
                          vault_dir=spec.vault_dir)
    if not spec.input_dir:
        r = WorkflowResult(success=False, total_steps=0, steps_run=0, duration_s=0.0)
        r.error = "directory mode requires input_dir"
        return r
    if spec.mode == "per_file":
        return run_per_file(
            spec.pipeline_paths, spec.input_dir, pattern=spec.pattern,
            recursive=spec.recursive, substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, output_dir=spec.output_dir,
            on_step=on_step, vault_dir=spec.vault_dir,
        )
    if spec.mode == "per_step":
        return run_per_step(
            spec.pipeline_paths, spec.input_dir, pattern=spec.pattern,
            recursive=spec.recursive, substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, output_dir=spec.output_dir,
            on_step=on_step, vault_dir=spec.vault_dir,
        )
    if spec.mode == "chained":
        return run_chained(
            spec.pipeline_paths, spec.input_dir, scope=spec.chain_scope,
            pattern=spec.pattern, recursive=spec.recursive,
            substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, output_dir=spec.output_dir,
            on_step=on_step, vault_dir=spec.vault_dir,
        )
    r = WorkflowResult(success=False, total_steps=0, steps_run=0, duration_s=0.0)
    r.error = f"unknown workflow mode: {spec.mode}"
    return r


def parse_workflow_request(
    text: str, vault_dir: Path,
) -> WorkflowSpec:
    """Heuristic parse of a natural-language workflow command.

    Patterns understood:
      run workflow A, B, C
      run workflow A then B then C
      run [A, B, C] on /path/to/dir per-file
      run workflow A, B on /path per-step pattern=*.dream3d
    """
    import re as _re
    t = text.strip()

    # Pull out mode if present. "chained" is checked first because a chained
    # request often also says "on <folder>", and chaining (each pipeline reads
    # the previous one's OUTPUT) is a stronger, more specific intent than the
    # per-file/per-step folder sweeps (which all read the original files).
    mode = "linear"
    chain_scope = "per_file"
    if _re.search(r"\bchain(?:ed|ing)?\b|\bfeed(?:s|ing)?\s+(?:the\s+)?"
                  r"(?:output|result)\b|\boutput\s+of\b|\bpipe(?:d|s|line)?\s+"
                  r"into\b|\bon\s+the\s+output\b|\bthen\s+run\b.*\boutput\b",
                  t, _re.IGNORECASE):
        mode = "chained"
        # Folder-level chaining: the next pipeline needs ALL of the previous
        # one's outputs at once ("folder", "folder-level", "all outputs").
        if _re.search(r"\bfolder(?:[\s-]?level)?\b|\ball\s+(?:the\s+)?outputs?\b",
                      t, _re.IGNORECASE):
            chain_scope = "folder"
    elif _re.search(r"\bper[\s_-]?file\b", t, _re.IGNORECASE):
        mode = "per_file"
    elif _re.search(r"\bper[\s_-]?step\b", t, _re.IGNORECASE):
        mode = "per_step"

    # Input directory: look for "on <path>" or "from <path>"
    input_dir: Optional[Path] = None
    m = _re.search(r"(?:on|from|over)\s+([A-Za-z]:[\\/][^\s,;]+|/[^\s,;]+|[\"'][^\"']+[\"'])",
                   t, _re.IGNORECASE)
    if m:
        raw = m.group(1).strip("\"'")
        cand = Path(raw)
        if cand.exists():
            input_dir = cand.resolve()

    # Pattern: look for pattern=...
    pattern = "*"
    m = _re.search(r"pattern\s*=\s*([^\s,]+)", t, _re.IGNORECASE)
    if m:
        pattern = m.group(1)
    elif mode in ("per_file", "per_step", "chained"):
        pattern = "*.dream3d"

    # Strip the wrapper phrases so what's left is the pipeline list
    core = _re.sub(r"^\s*run\s+(?:the\s+)?workflow\s*:?\s*", "", t,
                   flags=_re.IGNORECASE)
    core = _re.sub(r"(?:on|from|over)\s+(?:[A-Za-z]:[\\/][^\s,;]+|/[^\s,;]+|[\"'][^\"']+[\"']).*",
                   "", core, flags=_re.IGNORECASE)
    core = _re.sub(r"\bper[\s_-]?(?:file|step)\b.*", "", core, flags=_re.IGNORECASE)
    # Chaining phrases are workflow directives, not pipeline names — drop them
    # (and anything after) so "A then B feeding the output into..." leaves "A,
    # B", not "B feeding the output into".
    core = _re.sub(r"\b(?:chain(?:ed|ing)?|feed(?:s|ing)?\s+(?:the\s+)?"
                   r"(?:output|result)|(?:on\s+the\s+|the\s+)?output\s+of|"
                   r"pipe(?:d|s|line)?\s+into|folder[\s-]?level|all\s+outputs?)"
                   r"\b.*", "", core, flags=_re.IGNORECASE)
    core = _re.sub(r"pattern\s*=\s*\S+", "", core, flags=_re.IGNORECASE)
    core = core.strip().strip("[]")

    # Split on "then" / "," / ";"
    raw_names = _re.split(r"\s+then\s+|,|;", core, flags=_re.IGNORECASE)
    names = [n.strip().strip("'\"") for n in raw_names if n.strip()]

    paths: List[Path] = []
    for n in names:
        p = resolve_pipeline_path(n, vault_dir)
        if p:
            paths.append(p)

    return WorkflowSpec(
        pipeline_paths=paths, mode=mode, input_dir=input_dir,
        pattern=pattern, chain_scope=chain_scope,
    )
