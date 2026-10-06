"""
council_core.nx_ops — the four DREAM3D-NX jobs, as plain functions.

simplnx is a compiled package in its own conda env, so every call here is a
subprocess through `nx_bridge` and takes seconds to minutes. Callers run these
on a worker; each returns an `NxResult` — the status line and the text the
pipeline pane shows — and touches no widget.

Moved from the Tk engine's `_nx_check_env` / `_nx_catalog` /
`_nx_transpile_selected` / `_nx_run_folder` / `_nx_write_script`
(council_gui_engine.py:9458-9690). Defects designed out
(docs/qt_migration/remaining_tabs_requirements.md §dream3d):

  * THE CATALOG NEVER EXPIRED. It was cached in memory and in
    data_out/dream3d/nx_catalog.json with no way to clear either, so after an
    nx reinstall every generated script was grounded on filters that might no
    longer exist. `invalidate_catalog()` clears both, and `check_env()` — the
    button a user presses after reinstalling — calls it. That still relied on
    the user pressing it: `catalog()` now also refuses a cached copy whose
    schema, python or dream3dnx version no longer matches the env
    (nx_bridge.catalog_stale_reason), with no subprocess.
  * A REFUSED SCRIPT WAS "SAVED". write_script returns code=None when no filter
    matches; nothing was written, yet the report said "It is saved for you to
    read at <path>". The report now names a path only when a file exists there.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

CATALOG_FILE = "nx_catalog.json"
SUBFOLDER = "dream3d"

ENV_HELP = ("Create the env with:\n"
            "  conda create -n nxpython python=3.12 dream3dnx -c conda-forge\n"
            "  conda install -n nxpython -c conda-forge numpy\n\n"
            "Install numpy from conda-forge, never pip: a pip numpy dies at "
            "import with a missing-DLL fault and no traceback, which silently "
            "removes CSV loading.")


@dataclass
class NxResult:
    status: str          # the one-line "nx: ..." status
    body: str            # what the pipeline pane shows
    ok: bool = True
    path: Optional[Path] = None


# ============================================================
# Paths
# ============================================================

def out_dir(vault_dir: Path) -> Path:
    import data_index
    return Path(data_index.output_dir(vault_dir))


def safe_out_path(vault_dir: Path, filename: str,
                  subfolder: str = SUBFOLDER) -> Path:
    """A path under the vault's write root, through DataIndex's own guard.

    Tk called `self.data_index.safe_write_path` on the console's instance; a
    DataIndex built with the same roots gives the same answer and the same
    refusals (no traversal, never inside an input root).
    """
    import data_index
    index = data_index.DataIndex(
        search_roots=[data_index.input_dir(vault_dir)],
        write_root=data_index.output_dir(vault_dir))
    return index.safe_write_path(filename, subfolder=subfolder)


# ============================================================
# The filter catalog
# ============================================================

_catalog_mem: Dict[Path, dict] = {}
_catalog_lock = threading.Lock()


def _catalog_path(vault_dir: Path) -> Optional[Path]:
    try:
        return safe_out_path(vault_dir, CATALOG_FILE)
    except Exception:                                     # noqa: BLE001
        return None


def _stale(cat: dict, bridge: Any) -> Optional[str]:
    """Why ``cat`` no longer matches the installed env (None = still good).

    Asks the bridge, because only it knows where the env is; a bridge without
    the check (a test double) keeps the old behaviour."""
    check = getattr(bridge, "catalog_stale_reason", None)
    if check is None:
        return None
    try:
        return check(cat)
    except Exception:                                     # noqa: BLE001
        return None


# Why the last catalog() call rebuilt instead of using its cache, per vault.
last_rebuild_reason: Dict[Path, str] = {}


def catalog(vault_dir: Path, *, force: bool = False,
            bridge: Any = None) -> dict:
    """The INSTALLED package's filter catalog: memory, then disk, then nx.

    A cached copy is used only while it still describes the installed env
    (nx_bridge.catalog_stale_reason: schema, python and dream3dnx version).
    It used to be used for good, so after an nx reinstall every script was
    checked against filters that might no longer exist."""
    if bridge is None:
        import nx_bridge as bridge
    key = Path(vault_dir).resolve()
    reason = "forced" if force else "nothing cached"
    with _catalog_lock:
        mem = None if force else _catalog_mem.get(key)
        if mem:
            why = _stale(mem, bridge)
            if why is None:
                return mem
            reason = f"the cached catalog is stale: {why}"
            _catalog_mem.pop(key, None)
        path = _catalog_path(vault_dir)
        if not force and path is not None and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                why = _stale(cached, bridge) if cached.get("filters") \
                    else "it is empty"
                if why is None:
                    _catalog_mem[key] = cached
                    return cached
                reason = f"the saved catalog is stale: {why}"
            except Exception:                             # noqa: BLE001
                reason = "the saved catalog is unreadable"
    last_rebuild_reason[key] = reason
    fresh = bridge.catalog()
    with _catalog_lock:
        _catalog_mem[key] = fresh
        if path is not None:
            try:
                path.write_text(json.dumps(fresh), encoding="utf-8")
            except Exception:                             # noqa: BLE001
                pass
    return fresh


def invalidate_catalog(vault_dir: Path) -> None:
    """Forget the cached catalog, in memory and on disk."""
    with _catalog_lock:
        _catalog_mem.pop(Path(vault_dir).resolve(), None)
        path = _catalog_path(vault_dir)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ============================================================
# The four jobs
# ============================================================

def check_env(vault_dir: Path, *, bridge: Any = None) -> NxResult:
    """Is the nx env there, and which plugins import? Also drops the catalog
    cache, since this is what a user runs after changing the install."""
    invalidate_catalog(vault_dir)
    try:
        if bridge is None:
            import nx_bridge as bridge
        info = bridge.ping()
        mods = [m for m, ok in (info.get("modules") or {}).items() if ok]
        return NxResult(
            f"nx env: python {info.get('python')} · {len(mods)} module(s)",
            "DREAM3D-NX is reachable.\n\n"
            f"python : {info.get('python')}\n"
            f"modules: {', '.join(mods) or 'none'}\n")
    except Exception as exc:                              # noqa: BLE001
        return NxResult("nx env: unavailable",
                        f"DREAM3D-NX is not reachable.\n\n{exc}\n\n{ENV_HELP}",
                        ok=False)


def _transpile_notes(res: dict):
    notes = []
    if res.get("unknown"):
        notes.append(f"{len(res['unknown'])} step(s) are not in the installed "
                     "package and were commented out rather than guessed.")
    notes.extend(res.get("warnings") or [])
    return notes


def transpile(pipeline, vault_dir: Path, *, bridge: Any = None,
              heading: str = "saved to") -> NxResult:
    """A saved .d3dpipeline, rendered as editable Python and written out."""
    src = Path(pipeline.path)
    if src.suffix.lower() == ".py":
        return NxResult("nx: nothing to do",
                        f"{pipeline.name} is already a Python script — open it "
                        "from the in/ folder. This converts a saved "
                        ".d3dpipeline into Python.", ok=False)
    try:
        if bridge is None:
            import nx_bridge as bridge
        res = bridge.transpile(src, catalog_cache=catalog(vault_dir,
                                                          bridge=bridge))
        code = res.get("code") or ""
        out = safe_out_path(vault_dir, f"{Path(pipeline.name).stem}.py")
        out.write_text(code, encoding="utf-8")
        head = f"# {heading}: {out}\n" + "".join(
            f"# note: {n}\n" for n in _transpile_notes(res))
        return NxResult("nx: transpiled", head + "\n" + code, path=out)
    except Exception as exc:                              # noqa: BLE001
        return NxResult("nx: failed", f"Transpile failed.\n\n{exc}", ok=False)


def run_report(name: str, pattern: str, out: Path, res: dict) -> str:
    lines = [f"{name} over {pattern}",
             f"  files : {res['total']}",
             f"  ok    : {res['ok']}",
             f"  failed: {res['failed']}",
             f"  output: {out}", ""]
    for r in res.get("runs") or []:
        lines.append(f"[{'ok ' if r['ok'] else 'FAIL'}] {Path(r['file']).name}")
        if r.get("write_set"):
            lines.append(f"        -> {r['write_set']['dest']}")
        for e in (r.get("errors") or [])[:2]:
            lines.append(f"        {e.splitlines()[0]}")
    return "\n".join(lines)


def run_folder(pipeline, in_dir: Path, pattern: str, vault_dir: Path, *,
               bridge: Any = None) -> NxResult:
    """The selected pipeline over every matching file in ``in_dir``.

    Outputs go to data_out/dream3d/runs/; passing vault_dir makes the bridge
    refuse any pipeline that would write elsewhere.
    """
    if Path(pipeline.path).suffix.lower() == ".py":
        return NxResult("nx: nothing to do",
                        f"{pipeline.name} is a Python script — run it "
                        "directly. The folder runner drives a saved "
                        ".d3dpipeline.", ok=False)
    out = out_dir(vault_dir) / SUBFOLDER / "runs"
    try:
        if bridge is None:
            import nx_bridge as bridge
        res = bridge.run_folder(Path(pipeline.path), in_dir, out,
                                glob=pattern, vault_dir=vault_dir)
        status = (f"nx: {res['ok']}/{res['total']} ok" if res["total"]
                  else "nx: no matching files")
        return NxResult(status, run_report(pipeline.name, pattern, out, res),
                        ok=bool(res["total"]) and not res["failed"])
    except Exception as exc:                              # noqa: BLE001
        return NxResult("nx: run failed",
                        f"The run was refused or failed.\n\n{exc}", ok=False)


def default_model_call(prompt: str) -> str:
    import council_engine
    return council_engine.local_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2, num_predict=900, timeout=180)


def _n_ctx() -> Optional[int]:
    """The window default_model_call's prompt will be clamped to: the main
    model's REAL n_ctx. get_n_ctx() is the configured value, 4096 with
    COUNCIL_GGUF_N_CTX unset, so on a 16k Phi-4 the filter shortlist was cut
    to a 9,408-char budget where 48,729 fit (nx_generate's 3.2 chars/token
    over n_ctx - 1156)."""
    try:
        import council_engine
        return int(council_engine.effective_n_ctx("main"))
    except Exception:                                     # noqa: BLE001
        return None


def script_stem(task: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", task.lower())[:40] or "pipeline"


# Windows without LongPathsEnabled refuses a path of 260+ characters; keep
# the saved script's full path under this.
_MAX_PATH = 250


def _script_name(task: str, vault_dir: Path) -> str:
    """task_<stem>.py, the stem cut so the whole path stays under _MAX_PATH.

    The 40-character stem under a deep vault made the path 260+ characters
    on a PC with LongPathsEnabled=0: the write raised, and a script that had
    passed every check was reported "nx: failed" and lost."""
    stem = script_stem(task)
    try:
        folder = len(str(out_dir(vault_dir) / SUBFOLDER)) + 1
    except Exception:                                     # noqa: BLE001
        folder = 0
    room = _MAX_PATH - folder - len("task_.py")
    if len(stem) > room:
        import hashlib
        tag = hashlib.sha1(task.encode("utf-8")).hexdigest()[:6]
        stem = (stem[:max(0, room - 7)].rstrip("_") + "_" + tag)[-max(6, room):]
    return f"task_{stem}.py"


def write_script(task: str, vault_dir: Path, *,
                 model_call: Callable[[str], str] = default_model_call,
                 generator: Any = None, bridge: Any = None) -> NxResult:
    """Plain English -> a simplnx script, grounded on the installed catalog
    (nx_ground) and gated by nx_policy. Saved and shown whenever there is
    code — and shown even when saving fails, rather than lost."""
    task = (task or "").strip()
    if not task:
        return NxResult("nx: waiting",
                        "Describe what the pipeline should do, e.g. 'read "
                        "every .dream3d and write an STL'.", ok=False)
    try:
        if generator is None:
            import nx_generate as generator
        res = generator.write_script(task, catalog(vault_dir, bridge=bridge),
                                     model_call, n_ctx=_n_ctx())
    except Exception as exc:                              # noqa: BLE001
        return NxResult("nx: failed", f"Could not write a pipeline.\n\n{exc}",
                        ok=False)
    code = res.get("code") or ""
    out = None
    save_error = ""
    if code:
        try:
            out = safe_out_path(vault_dir, _script_name(task, vault_dir))
            out.write_text(code, encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            out = None
            save_error = (f"\n\n(It could not be saved: {exc}. The script is "
                          f"below — copy it from here.)")
    if res.get("ok"):
        head = f"# saved to: {out}" if out is not None else \
            "# NOT saved" + save_error.replace("\n\n", " ")
        return NxResult(f"nx: written ({res.get('attempts')} attempt(s))"
                        + ("" if out is not None else ", not saved"),
                        f"{head}\n\n{code}", path=out)
    body = ("The generated script was NOT accepted, so the app will not "
            "run it:\n\n"
            + "\n".join(f"  - {e}" for e in res.get("errors", [])))
    if out is not None:
        body += f"\n\nIt is saved for you to read at:\n  {out}\n\n{code}"
    elif code:
        body += f"{save_error}\n\n{code}"
    else:
        body += "\n\nNo script was produced, so nothing was saved."
    return NxResult("nx: refused", body, ok=False, path=out)
