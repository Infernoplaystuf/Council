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
    from pipeline_editor import apply_edits

    source = pipeline_path.read_text(encoding="utf-8", errors="replace")

    # Find the first occurrence of `<substitution_param>="..."` or `=...`
    import re as _re
    pat = _re.compile(rf"\b{_re.escape(substitution_param)}\s*=\s*", _re.MULTILINE)
    m = pat.search(source)
    if not m:
        # Nothing to substitute — copy as-is.
        target = stage_dir / pipeline_path.name
        target.write_text(source, encoding="utf-8")
        return target

    # Build a replace_text edit so apply_edits validates the result with AST.
    # We need to identify the old value text. Use a tiny scan of the value.
    from pipeline_editor import _value_text_end
    start = m.end()
    end = _value_text_end(source, start)
    old_value = source[start:end]
    new_value = repr(str(input_file))

    result = apply_edits(source, [{
        "op": "replace_text",
        "find": f"{substitution_param}={old_value}",
        "replace": f"{substitution_param}={new_value}",
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
    if stage_dir is None:
        stage_dir = input_dir.parent / "_wf_stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    total_steps = len(pipelines) * len(inputs)
    result = WorkflowResult(success=True, total_steps=total_steps, steps_run=0,
                            duration_s=0.0)
    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        return result

    step_counter = 0
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
    if stage_dir is None:
        stage_dir = input_dir.parent / "_wf_stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    total_steps = len(pipelines) * len(inputs)
    result = WorkflowResult(success=True, total_steps=total_steps, steps_run=0,
                            duration_s=0.0)
    if not inputs:
        result.success = False
        result.error = f"no input files matched {pattern!r} under {input_dir}"
        result.duration_s = time.monotonic() - overall_start
        return result

    step_counter = 0
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
    result.duration_s = time.monotonic() - overall_start
    return result


# ============================================================
# Top-level dispatch + workflow parser
# ============================================================

@dataclass
class WorkflowSpec:
    pipeline_paths: List[Path]
    mode: str = "linear"           # "linear" | "per_file" | "per_step"
    input_dir: Optional[Path] = None
    pattern: str = "*"
    recursive: bool = False
    substitution_param: str = "file_path"
    timeout_s: int = 600


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

    # Pull out mode if present
    mode = "linear"
    if _re.search(r"\bper[\s_-]?file\b", t, _re.IGNORECASE):
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
    elif mode in ("per_file", "per_step"):
        pattern = "*.dream3d"

    # Strip the wrapper phrases so what's left is the pipeline list
    core = _re.sub(r"^\s*run\s+(?:the\s+)?workflow\s*:?\s*", "", t,
                   flags=_re.IGNORECASE)
    core = _re.sub(r"(?:on|from|over)\s+(?:[A-Za-z]:[\\/][^\s,;]+|/[^\s,;]+|[\"'][^\"']+[\"']).*",
                   "", core, flags=_re.IGNORECASE)
    core = _re.sub(r"\bper[\s_-]?(?:file|step)\b.*", "", core, flags=_re.IGNORECASE)
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
        pattern=pattern,
    )
