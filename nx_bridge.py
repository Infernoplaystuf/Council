"""
nx_bridge.py — the APP side of the DREAM3D-NX bridge.

Builds a JSON job, runs nx_worker.py in the nx conda env as a subprocess, and
reads the JSON result back. The app process never imports simplnx: it is a
compiled pybind11 package in its own env (Python 3.12, bluequartzsoftware), and
importing it here would mean ABI conflicts and pinning the whole app to that
interpreter.

Interpreter discovery, in order:
  1. $COUNCIL_NX_PYTHON — an explicit python.exe (wins if set)
  2. <conda root>/envs/<env>/python.exe for the usual conda roots
  3. `conda run -n <env> python` as a last resort

Direct python.exe is preferred over `conda run` deliberately: `conda run` was
observed crashing into its own error-reporting prompt on this machine, and it
also buffers/steals output. The direct path is what actually works.

Building the env — install numpy from conda-forge, NEVER from pip:

    conda create -n nxpython python=3.12 dream3dnx -c conda-forge
    conda install -n nxpython -c conda-forge numpy

A pip numpy in this env is not a version quibble, it is a hard crash. numpy
2.4.3 from pip died with Windows fatal exception 0xc06d007f (missing DLL) in
blas_fpe_check at numpy/__init__.py:878 — its BLAS DLLs do not match the conda
env's. There is no traceback and no Python error: the interpreter exits 127 in
silence, so a generated script "fails" with nothing to read. simplnx itself
imports fine, which makes it look like the pipeline is at fault. conda-forge
numpy 2.5.1 fixed it, with simplnx unaffected.

This matters beyond tidiness: the pipeline scripts need numpy for the copy the
API forces (there is no zero-copy wrap — `npview[:] = np.loadtxt(...)`), so a
broken numpy silently removes CSV ingestion.

Safety: run_folder REFUSES to write anywhere but the vault's output area. The
worker takes an absolute out_dir and would happily write wherever it is told,
so the check belongs here, on the side that knows what the vault is. Inputs are
only ever read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

NX_ENV = os.environ.get("COUNCIL_NX_ENV", "nxpython")
WORKER = Path(__file__).resolve().parent / "nx_worker.py"

_CONDA_ROOTS = [
    Path.home() / "miniforge3",
    Path.home() / "miniconda3",
    Path.home() / "anaconda3",
    Path("C:/ProgramData/miniforge3"),
    Path("C:/ProgramData/Anaconda3"),
    Path("/opt/conda"),
]


class NxError(RuntimeError):
    """The bridge could not run, or the worker reported a failure."""


def find_python(env: str = NX_ENV) -> Optional[str]:
    """The nx env's interpreter, or None if it isn't installed."""
    explicit = os.environ.get("COUNCIL_NX_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit
    for root in _CONDA_ROOTS:
        for rel in (f"envs/{env}/python.exe", f"envs/{env}/bin/python"):
            p = root / rel
            if p.exists():
                return str(p)
    return None


def available(env: str = NX_ENV) -> bool:
    return find_python(env) is not None or shutil.which("conda") is not None


def _command(env: str) -> List[str]:
    py = find_python(env)
    if py:
        return [py, str(WORKER)]
    conda = shutil.which("conda")
    if conda:
        # Last resort: slower, and observed to crash into its own error
        # reporter on at least one machine.
        return [conda, "run", "--no-capture-output", "-n", env,
                "python", str(WORKER)]
    raise NxError(
        f"The DREAM3D-NX env {env!r} was not found. Create it with:\n"
        f"  conda create -n {env} python=3.12 dream3dnx -c conda-forge\n"
        f"or point COUNCIL_NX_PYTHON at that env's python.exe.")


def run_job(job: Dict[str, Any], *, env: str = NX_ENV,
            timeout: int = 1800) -> Dict[str, Any]:
    """Run one job in the nx env and return its result payload.

    Both job and result travel as FILES. The worker's stdout is not the
    protocol: simplnx warns on stderr at import and the C++ filters print
    progress of their own, so anything parsed off a stream would be one
    library update away from breaking.
    """
    if not WORKER.exists():
        raise NxError(f"worker missing: {WORKER}")
    cmd = _command(env)
    tmp = Path(tempfile.mkdtemp(prefix="nxjob_"))
    try:
        job_path, out_path = tmp / "job.json", tmp / "result.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        try:
            proc = subprocess.run(cmd + [str(job_path), str(out_path)],
                                  capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise NxError(f"nx job {job.get('action')!r} timed out after "
                          f"{timeout}s")
        if not out_path.exists():
            raise NxError(
                f"nx worker produced no result (exit {proc.returncode}).\n"
                f"stderr: {(proc.stderr or '').strip()[:800]}")
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NxError(f"nx worker wrote unreadable result: {exc}")
        if not payload.get("ok"):
            raise NxError(payload.get("error") or "nx worker failed")
        return payload.get("result")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- convenience wrappers -------------------------------------------------

def ping(*, env: str = NX_ENV, timeout: int = 300) -> Dict[str, Any]:
    """Is the nx env alive, and which plugin modules import?"""
    return run_job({"action": "ping"}, env=env, timeout=timeout)


def catalog(*, env: str = NX_ENV, timeout: int = 900) -> Dict[str, Any]:
    """The full filter catalog of the INSTALLED binary — the model's only
    source of truth about simplnx. Cache it; regenerate when the env changes."""
    return run_job({"action": "catalog"}, env=env, timeout=timeout)


def describe_pipeline(pipeline: Any, *, env: str = NX_ENV,
                      timeout: int = 300) -> Dict[str, Any]:
    """What a .d3dpipeline contains, and where its file paths actually live."""
    return run_job({"action": "describe", "pipeline": str(pipeline)},
                   env=env, timeout=timeout)


def transpile(pipeline: Any, *, catalog_cache: Any = None,
              env: str = NX_ENV, timeout: int = 900) -> Dict[str, Any]:
    """A .d3dpipeline rendered as editable Python (spec B.3 Mode 2).

    Needs a catalog, not the nx env: the rendering itself is pure. Pass
    ``catalog_cache`` (a path or a dict) to skip the subprocess entirely —
    the catalog only changes when the nx install does.
    """
    import nx_transpile
    if catalog_cache is None:
        cat = catalog(env=env, timeout=timeout)
    elif isinstance(catalog_cache, dict):
        cat = catalog_cache
    else:
        cat = json.loads(Path(catalog_cache).read_text(encoding="utf-8"))
    return nx_transpile.transpile(pipeline, cat)


def run_folder(pipeline: Any, in_dir: Any, out_dir: Any, *,
               glob: str = "*.dream3d", read_index: Optional[int] = None,
               write_index: Optional[int] = None,
               out_suffix: str = "_out.dream3d", limit: int = 0,
               vault_dir: Any = None, env: str = NX_ENV,
               timeout: int = 3600) -> Dict[str, Any]:
    """Run ``pipeline`` over every file in ``in_dir`` matching ``glob``.

    ``out_dir`` must be inside the vault's output area. The worker writes
    wherever it is told, so the containment check lives here — the app side is
    the only side that knows what the vault is.
    """
    out_dir = Path(out_dir).resolve()
    write_root = out_dir
    if vault_dir is not None:
        try:
            import data_index
            allowed = Path(data_index.output_dir(vault_dir)).resolve()
        except Exception as exc:
            raise NxError(f"could not resolve the vault output dir: {exc}")
        if not (out_dir == allowed or allowed in out_dir.parents):
            raise NxError(
                f"refusing to write outside the vault output area.\n"
                f"  asked for: {out_dir}\n  allowed   : {allowed}")
        write_root = allowed
    job = {
        "action": "run_folder",
        "pipeline": str(pipeline),
        "in_dir": str(Path(in_dir).resolve()),
        "out_dir": str(out_dir),
        "write_root": str(write_root),
        "glob": glob,
        "read_index": read_index,
        "write_index": write_index,
        "out_suffix": out_suffix,
        "limit": limit,
    }
    return run_job(job, env=env, timeout=timeout)
