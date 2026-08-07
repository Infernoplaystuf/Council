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

Pipelines execute as subprocesses using sys.executable so they pick up
the same conda env the council was launched from (simplnx, h5py, etc.).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


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

    def summary(self) -> str:
        lines = [
            f"Workflow {'OK' if self.success else 'FAILED'} — "
            f"{self.steps_run}/{self.total_steps} steps in "
            f"{self.duration_s:.1f}s",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
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

def _run_pipeline_subprocess(
    pipeline_path: Path,
    timeout_s: int = 600,
    cwd: Optional[Path] = None,
) -> StepResult:
    """Execute a single .py pipeline as a subprocess.

    Returns a StepResult that the caller fills in step_index / input_label
    fields on. This function only sets success, return_code, duration,
    stdout, stderr, error.
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
        proc = subprocess.run(
            [sys.executable, "-u", str(pipeline_path)],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        base.return_code = proc.returncode
        base.stdout = proc.stdout or ""
        base.stderr = proc.stderr or ""
        base.success = proc.returncode == 0
        if not base.success and not base.error:
            base.error = f"pipeline exited with code {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        base.error = f"timed out after {timeout_s}s"
        base.stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except Exception as exc:
        base.error = f"subprocess launch failed: {exc!r}"
    finally:
        base.duration_s = time.monotonic() - start
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

def run_linear(
    pipelines: List[Path],
    *,
    timeout_s: int = 600,
    on_step: Optional[Callable[[StepResult], None]] = None,
) -> WorkflowResult:
    """Run each pipeline once, in order. Stop on the first failure."""
    overall_start = time.monotonic()
    result = WorkflowResult(success=True, total_steps=len(pipelines), steps_run=0,
                            duration_s=0.0)
    for i, p in enumerate(pipelines, start=1):
        step = _run_pipeline_subprocess(p, timeout_s=timeout_s)
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
            result.error = f"step #{i} ({p.name}) failed"
            break
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
    """Make a temp copy of `pipeline_path` with the configured input path
    substituted. The substitution is the simplest reasonable default:
    replace the value of the first parameter named `substitution_param` on
    the first ReadDREAM3DFilter / ReadCSVFile / Import* filter.
    """
    from pipeline_editor import apply_edits, _value_text_end

    source = pipeline_path.read_text(encoding="utf-8", errors="replace")

    # Find the first occurrence of `<substitution_param> [whitespace] = [whitespace] <value>`.
    # We capture the EXACT matched prefix (including any spaces around `=`)
    # so the apply_edits find-string matches the real source text — building
    # a synthetic prefix with bare `=` misses `param = "value"` style.
    import re as _re
    pat = _re.compile(rf"\b{_re.escape(substitution_param)}\s*=\s*", _re.MULTILINE)
    m = pat.search(source)
    if not m:
        # Nothing to substitute — copy as-is.
        target = stage_dir / pipeline_path.name
        target.write_text(source, encoding="utf-8")
        return target

    matched_prefix = m.group(0)              # e.g. 'file_path = '
    start = m.end()
    end = _value_text_end(source, start)
    old_value_text = source[start:end]
    new_value_text = repr(str(input_file))   # repr handles backslashes safely

    result = apply_edits(source, [{
        "op": "replace_text",
        "find": matched_prefix + old_value_text,
        "replace": matched_prefix + new_value_text,
        "max_count": 1,
    }])
    if not result.succeeded:
        # Fall back to raw copy with a comment header so the user can see
        # what went wrong without crashing the whole workflow.
        target = stage_dir / pipeline_path.name
        target.write_text(
            f"# [workflow_runner] could not substitute {substitution_param}: "
            f"{result.error or 'unknown'}\n" + source,
            encoding="utf-8",
        )
        return target

    target = stage_dir / pipeline_path.name
    target.write_text(result.new_source, encoding="utf-8")
    return target


# Parameter names a DREAM3D read/write step uses, discovered from the real
# filters (see nx_worker.path_params). The INPUT is what a Read* filter reads;
# the OUTPUT is what a Write* filter writes. Chaining rewrites A's output to a
# staging file and then B's input to that same file.
_INPUT_PARAM_CANDIDATES = ("file_path", "input_file_path", "input_file",
                           "import_file_path", "input_path")
_OUTPUT_PARAM_CANDIDATES = ("export_file_path", "output_file_path", "output_file",
                            "output_path", "write_file_path", "feature_data_file")


def _first_param_present(source: str, candidates) -> Optional[str]:
    """The first `param = ` assignment present in the pipeline source."""
    import re as _re
    for c in candidates:
        if _re.search(rf"\b{_re.escape(c)}\s*=\s*", source, _re.MULTILINE):
            return c
    return None


def _sub_param(source: str, param: str, value: Any) -> Tuple[str, bool]:
    """Replace ``param = <value>`` in ``source``. Returns (new_source, matched).

    Same find-the-real-prefix approach as _stage_per_input_pipeline so the
    apply_edits find-string matches the exact source text (including the spaces
    around ``=``)."""
    import re as _re
    from pipeline_editor import apply_edits, _value_text_end
    pat = _re.compile(rf"\b{_re.escape(param)}\s*=\s*", _re.MULTILINE)
    m = pat.search(source)
    if not m:
        return source, False
    matched_prefix = m.group(0)
    start = m.end()
    end = _value_text_end(source, start)
    old_value_text = source[start:end]
    result = apply_edits(source, [{
        "op": "replace_text",
        "find": matched_prefix + old_value_text,
        "replace": matched_prefix + repr(str(value)),
        "max_count": 1,
    }])
    if not result.succeeded:
        return source, False
    return result.new_source, True


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
    source = pipeline_path.read_text(encoding="utf-8", errors="replace")
    if input_value is not None:
        ip = input_param or _first_param_present(source, _INPUT_PARAM_CANDIDATES) \
            or "file_path"
        source, _ = _sub_param(source, ip, input_value)
    out_used: Optional[str] = None
    if output_value is not None:
        op = _first_param_present(source, _OUTPUT_PARAM_CANDIDATES)
        if op:
            source, ok = _sub_param(source, op, output_value)
            if ok:
                out_used = op
    target = stage_dir / (dest_name or pipeline_path.name)
    target.write_text(source, encoding="utf-8")
    return target, out_used


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
    on_step: Optional[Callable[[StepResult], None]] = None,
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
    the wrong input.
    """
    overall_start = time.monotonic()
    inputs = _list_directory_inputs(input_dir, pattern=pattern,
                                    recursive=recursive)
    owned_stage = stage_dir is None
    if stage_dir is None:
        stage_dir = _default_stage_dir()
    stage_dir.mkdir(parents=True, exist_ok=True)

    result = WorkflowResult(success=True,
                            total_steps=len(pipelines) * max(1, len(inputs)),
                            steps_run=0, duration_s=0.0)
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
                 file_stage: Path, label: str, idx: int) -> StepResult:
        staged, out_used = _stage_chain_pipeline(
            pl, file_stage, input_value=src_in, output_value=out_path,
            dest_name=f"{idx:02d}_{pl.name}")
        # A non-final pipeline whose output we could not redirect leaves us not
        # knowing what to feed onward — fail loudly instead of chaining garbage.
        if out_path is not None and out_used is None:
            bad = StepResult(step_index=idx, pipeline_name=pl.name,
                             input_label=label, success=False, return_code=None,
                             duration_s=0.0, stdout="", stderr="",
                             pipeline_path=pl)
            bad.error = (f"{pl.name} has no recognized output-path parameter "
                         f"(looked for {', '.join(_OUTPUT_PARAM_CANDIDATES)}), "
                         f"so its result can't be chained into the next "
                         f"pipeline.")
            return bad
        step = _run_pipeline_subprocess(staged, timeout_s=timeout_s)
        step.step_index = idx
        step.input_label = label
        return step

    try:
        if scope == "folder":
            # Each pipeline runs over the previous stage's whole output set.
            cur_inputs = list(inputs)
            for j, pl in enumerate(pipelines, start=1):
                is_last = (j == len(pipelines))
                out_dir = stage_dir / f"step{j}"
                out_dir.mkdir(parents=True, exist_ok=True)
                next_inputs: List[Path] = []
                for src_in in cur_inputs:
                    step_counter += 1
                    out_path = None if is_last else out_dir / f"{src_in.stem}.dream3d"
                    step = _run_one(pl, src_in, out_path, out_dir,
                                    f"{pl.name} <- {src_in.name}", step_counter)
                    _emit(step)
                    if not step.success:
                        result.success = False
                        result.error = step.error or f"step #{step_counter} failed"
                        result.duration_s = time.monotonic() - overall_start
                        return result
                    if out_path is not None:
                        next_inputs.append(out_path)
                cur_inputs = next_inputs
        else:
            # per_file: each file flows through the entire chain on its own.
            for src_file in inputs:
                file_stage = stage_dir / src_file.stem
                file_stage.mkdir(parents=True, exist_ok=True)
                prev = src_file
                for j, pl in enumerate(pipelines, start=1):
                    step_counter += 1
                    is_last = (j == len(pipelines))
                    out_path = None if is_last else \
                        file_stage / f"{src_file.stem}_step{j}.dream3d"
                    step = _run_one(pl, prev, out_path, file_stage,
                                    f"{pl.name} <- {prev.name}", step_counter)
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


def run_per_file(
    pipelines: List[Path],
    input_dir: Path,
    *,
    pattern: str = "*.dream3d",
    recursive: bool = False,
    substitution_param: str = "file_path",
    timeout_s: int = 600,
    stage_dir: Optional[Path] = None,
    on_step: Optional[Callable[[StepResult], None]] = None,
) -> WorkflowResult:
    """For each input file in directory: run the whole pipeline list."""
    overall_start = time.monotonic()
    inputs = _list_directory_inputs(input_dir, pattern=pattern, recursive=recursive)
    owned_stage = stage_dir is None
    if stage_dir is None:
        stage_dir = _default_stage_dir()
    stage_dir.mkdir(parents=True, exist_ok=True)

    total_steps = len(pipelines) * len(inputs)
    result = WorkflowResult(success=True, total_steps=total_steps, steps_run=0,
                            duration_s=0.0)
    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        return result

    step_counter = 0
    try:
        for input_file in inputs:
            file_stage = stage_dir / input_file.stem
            file_stage.mkdir(parents=True, exist_ok=True)
            for j, pipeline_path in enumerate(pipelines, start=1):
                step_counter += 1
                staged = _stage_per_input_pipeline(
                    pipeline_path, input_file, file_stage,
                    substitution_param=substitution_param,
                )
                step = _run_pipeline_subprocess(staged, timeout_s=timeout_s)
                step.step_index = step_counter
                step.input_label = input_file.name
                result.step_results.append(step)
                result.steps_run += 1
                if on_step:
                    try:
                        on_step(step)
                    except Exception:
                        pass
                if not step.success:
                    result.success = False
                    result.error = (f"step #{step_counter} ({pipeline_path.name}) "
                                    f"failed on input {input_file.name}")
                    result.duration_s = time.monotonic() - overall_start
                    return result
    finally:
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
    result.duration_s = time.monotonic() - overall_start
    return result


# ============================================================
# Per-step directory runner (full directory per pipeline)
# ============================================================

def run_per_step(
    pipelines: List[Path],
    input_dir: Path,
    *,
    pattern: str = "*.dream3d",
    recursive: bool = False,
    substitution_param: str = "file_path",
    timeout_s: int = 600,
    stage_dir: Optional[Path] = None,
    on_step: Optional[Callable[[StepResult], None]] = None,
) -> WorkflowResult:
    """For each pipeline: run it on every input file. Then move to next pipeline."""
    overall_start = time.monotonic()
    inputs = _list_directory_inputs(input_dir, pattern=pattern, recursive=recursive)
    owned_stage = stage_dir is None
    if stage_dir is None:
        stage_dir = _default_stage_dir()
    stage_dir.mkdir(parents=True, exist_ok=True)

    total_steps = len(pipelines) * len(inputs)
    result = WorkflowResult(success=True, total_steps=total_steps, steps_run=0,
                            duration_s=0.0)
    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
        return result

    step_counter = 0
    try:
        for j, pipeline_path in enumerate(pipelines, start=1):
            for input_file in inputs:
                step_counter += 1
                file_stage = stage_dir / input_file.stem
                file_stage.mkdir(parents=True, exist_ok=True)
                staged = _stage_per_input_pipeline(
                    pipeline_path, input_file, file_stage,
                    substitution_param=substitution_param,
                )
                step = _run_pipeline_subprocess(staged, timeout_s=timeout_s)
                step.step_index = step_counter
                step.input_label = f"{pipeline_path.name} <- {input_file.name}"
                result.step_results.append(step)
                result.steps_run += 1
                if on_step:
                    try:
                        on_step(step)
                    except Exception:
                        pass
                if not step.success:
                    result.success = False
                    result.error = (f"step #{step_counter}: pipeline "
                                    f"{pipeline_path.name} failed on "
                                    f"input {input_file.name}")
                    result.duration_s = time.monotonic() - overall_start
                    return result
    finally:
        if owned_stage:
            _cleanup_stage_dir(stage_dir)
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


def run_workflow(spec: WorkflowSpec,
                 on_step: Optional[Callable[[StepResult], None]] = None,
                 ) -> WorkflowResult:
    if spec.mode == "linear":
        return run_linear(spec.pipeline_paths, timeout_s=spec.timeout_s,
                          on_step=on_step)
    if not spec.input_dir:
        r = WorkflowResult(success=False, total_steps=0, steps_run=0, duration_s=0.0)
        r.error = "directory mode requires input_dir"
        return r
    if spec.mode == "per_file":
        return run_per_file(
            spec.pipeline_paths, spec.input_dir, pattern=spec.pattern,
            recursive=spec.recursive, substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, on_step=on_step,
        )
    if spec.mode == "per_step":
        return run_per_step(
            spec.pipeline_paths, spec.input_dir, pattern=spec.pattern,
            recursive=spec.recursive, substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, on_step=on_step,
        )
    if spec.mode == "chained":
        return run_chained(
            spec.pipeline_paths, spec.input_dir, scope=spec.chain_scope,
            pattern=spec.pattern, recursive=spec.recursive,
            substitution_param=spec.substitution_param,
            timeout_s=spec.timeout_s, on_step=on_step,
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
