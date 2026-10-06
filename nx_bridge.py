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
from typing import Any, Dict, List, Optional, Tuple

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
                                  timeout=timeout,
                                  # The worker's traceback is what the error
                                  # below reports. Under the locale encoding a
                                  # non-cp1252 byte makes .stdout None, so the
                                  # report itself would raise instead of saying
                                  # what went wrong.
                                  encoding="utf-8", errors="replace")
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
    source of truth about simplnx. Cache it; regenerate when the env changes
    (catalog_stale_reason says when: the result carries the env fingerprint
    it was built from)."""
    cat = run_job({"action": "catalog"}, env=env, timeout=timeout)
    # An interpreter without simplnx (COUNCIL_NX_PYTHON at the wrong python)
    # still answers: 0 filters, every module in modules_missing. That was
    # saved over the good cache and served — transpile "succeeded" with every
    # step commented out. Refuse it, and say why.
    why = catalog_unusable_reason(cat)
    if why:
        raise NxError(f"The DREAM3D-NX filter catalog could not be built: "
                      f"{why}. Point COUNCIL_NX_PYTHON at the nx env's "
                      f"python (found: {find_python(env) or 'none'}).")
    cat["env"] = env_fingerprint(env)
    return cat


# ---- is a cached catalog still the installed one? --------------------------

# The conda packages whose versions decide what the catalog contains. numpy
# and the rest do not change a filter or a parameter.
_FINGERPRINT_PACKAGES = ("python", "dream3dnx", "simplnx")


def env_fingerprint(env: str = NX_ENV) -> Optional[Dict[str, Any]]:
    """What the nx env's catalog depends on, read WITHOUT starting it.

    A cached catalog is only worth its 0.006 s if checking it is just as
    cheap, so this never launches the interpreter (ping costs ~1 s): for a
    conda env it reads the package records in conda-meta (python-3.12.13-...,
    dream3dnx-26.03.23-...), otherwise the size and mtime of the compiled
    simplnx module. None when the env cannot be found."""
    py = find_python(env)
    if not py:
        return None
    root = Path(py).parent
    if root.name.lower() == "bin":                # posix layout
        root = root.parent
    fp: Dict[str, Any] = {"python_exe": str(Path(py)), "packages": {}}
    meta = root / "conda-meta"
    try:
        records = sorted(p.name[:-5] for p in meta.glob("*.json"))
    except OSError:
        records = []
    for rec in records:
        # 'python-3.12.13-h0159041_0_cpython', not 'python-dateutil-2.9.0-...'
        for pkg in _FINGERPRINT_PACKAGES:
            version = rec[len(pkg) + 1:]
            if rec.startswith(pkg + "-") and version[:1].isdigit():
                fp["packages"][pkg] = version
    if not fp["packages"]:
        # Not conda (a venv via COUNCIL_NX_PYTHON): the binary itself.
        for pat in ("Lib/site-packages/simplnx*", "lib/python*/site-packages/simplnx*"):
            for p in sorted(root.glob(pat)):
                try:
                    st = p.stat()
                except OSError:
                    continue
                fp["packages"][p.name] = f"{st.st_size}:{st.st_mtime_ns}"
    return fp


def catalog_unusable_reason(cat: Any) -> Optional[str]:
    """Why a catalog cannot be used at all (simplnx itself did not import,
    or it lists no filters), or None. A fresh one like that is refused, not
    saved; a cached one is stale."""
    if not isinstance(cat, dict):
        return "the nx worker returned no catalog"
    for m in cat.get("modules_missing") or []:
        if isinstance(m, dict) and m.get("module") == "simplnx":
            py = str(cat.get("python") or "?").split()[0]
            return (f"simplnx did not import in that interpreter (python "
                    f"{py}): {m.get('error')}")
    if not cat.get("filters"):
        return "it lists no filters"
    return None


def plugin_import_failures(cat: Any) -> List[Tuple[str, str]]:
    """[(module, error)] for each plugin that is INSTALLED but did not import
    when ``cat`` was built. "No module named '<that module>'" means it is not
    installed (this build has no simplnxreview) — not a failure; anything
    else (a DLL that would not load, a dependency missing) is."""
    out: List[Tuple[str, str]] = []
    if not isinstance(cat, dict):
        return out
    for m in cat.get("modules_missing") or []:
        if not isinstance(m, dict):
            continue
        mod, err = str(m.get("module") or ""), str(m.get("error") or "")
        if mod == "simplnx" or err.startswith(
                f"ModuleNotFoundError: No module named '{mod}'"):
            continue
        out.append((mod, err))
    return out


def catalog_stale_reason(cat: Any, *, env: str = NX_ENV,
                         fingerprint: Any = "probe") -> Optional[str]:
    """Why a cached catalog no longer describes the installed env, or None.

    Checked against the catalog's own fields: its schema (an older Council
    built it without what the script checker needs), its "python" (the
    interpreter it was built in) and its "env" fingerprint (the dream3dnx
    package it was built from). Before this nothing compared them: a catalog
    claiming python 3.9.0 and one fake filter was served for good, and every
    generated script was grounded on it.

    When the env cannot be found the cache is kept (None): the transpiler only
    needs a catalog, and a run fails loudly on its own."""
    try:
        from nx_introspect import CATALOG_SCHEMA
    except Exception:                                     # noqa: BLE001
        CATALOG_SCHEMA = 2
    if not isinstance(cat, dict) or not cat.get("filters"):
        return "it is empty"
    schema = cat.get("catalog_schema") or 1
    if schema < CATALOG_SCHEMA:
        return (f"it was built by an older version of this app (schema "
                f"{schema}, now {CATALOG_SCHEMA})")
    why = catalog_unusable_reason(cat)
    if why:
        return why
    # A plugin that failed to import when the catalog was built: its filters
    # are missing from it, and nothing about the fingerprint changes when the
    # import starts working again — so it was served for good. Rebuilt on
    # every use until the plugin imports (check_env shows the error).
    failed = plugin_import_failures(cat)
    if failed:
        mod, err = failed[0]
        return (f"it was built while {mod} failed to import ({err[:160]}), so "
                f"{mod}'s filters are missing from it")
    fp = env_fingerprint(env) if fingerprint == "probe" else fingerprint
    if not fp:
        return None
    want_py = (fp.get("packages") or {}).get("python", "").split("-")[0]
    have_py = str(cat.get("python") or "").split()[0] if cat.get("python") else ""
    if want_py and have_py != want_py:
        return (f"it was built in python {have_py or '?'}, and the nx env now "
                f"has python {want_py}")
    built = cat.get("env")
    if not isinstance(built, dict):
        return "it does not record which nx env it was built from"
    if built.get("python_exe") != fp.get("python_exe"):
        return (f"it was built from {built.get('python_exe')}, and the nx env "
                f"is now {fp.get('python_exe')}")
    if (built.get("packages") or {}) != (fp.get("packages") or {}):
        old, new = built.get("packages") or {}, fp.get("packages") or {}
        diff = ", ".join(f"{k} {old.get(k, '-')} -> {new.get(k, '-')}"
                         for k in sorted(set(old) | set(new))
                         if old.get(k) != new.get(k))
        return f"the nx env changed since it was built ({diff})"
    return None


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
