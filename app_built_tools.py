"""
app_built_tools.py — self-authored, UNREVIEWED tools the local models may
create when they find a needed capability is missing.

SECURITY MODEL (unchanged guarantees)
-------------------------------------
A model may CREATE a tool, but it can never gain new powers by doing so:

  * Every tool is validated by ``vault_analyst.validate_generated_code`` — the
    SAME AST validator + blocklist the pandas sandbox uses — BEFORE it is saved
    AND again before every run. So a self-built tool still cannot delete, write
    outside the output folder, use the network, shell out, or reach
    os/sys/subprocess. The "never delete" rule holds structurally.
  * Tools RUN only through ``vault_analyst.execute_pandas_code`` — the existing
    sandbox. No new execution surface is created in this module.
  * Tools are stored under ``<vault>/App_Built_tools/`` and flagged
    APP-BUILT · UNREVIEWED · may be inaccurate. Every result a tool produces is
    labelled UNVERIFIED so it is never mistaken for a human-reviewed answer.
  * The MODEL cannot delete a tool (there is no delete function exposed to it).
    The USER can, by removing the file/folder.

This is a deliberate, bounded relaxation of the "the model cannot register a
tool" invariant in tool_registry.py: the frozen core allow-list stays frozen
and human-owned; app-built tools are a SEPARATE, clearly-marked, sandboxed
store that the user can audit and delete.
"""
from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TOOLS_DIRNAME = "App_Built_tools"
INDEX_FILENAME = "index.json"
README_FILENAME = "README.txt"

# A tool name is a safe python identifier-ish slug (also the on-disk filename).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

_MAX_CODE_BYTES = 20_000
_MAX_TOOLS = 200

_README = (
    "App-built tools\n"
    "===============\n\n"
    "These Python tools were written by the local models (council / agent)\n"
    "when they judged a needed capability was missing. They are:\n\n"
    "  * UNREVIEWED — not checked by a human. They may be INACCURATE.\n"
    "  * Sandbox-validated — each was accepted by the same AST validator the\n"
    "    analyst sandbox uses, so none can delete, write outside the output\n"
    "    folder, use the network, or shell out.\n\n"
    "You can safely delete any file in this folder; the app will drop it from\n"
    "the index on the next run. Nothing here is required for the app to work.\n"
)


def resolve_vault_root(vault_dir: Optional[Any] = None) -> Path:
    """The vault root. Explicit arg wins; else COUNCIL_VAULT_ROOT; else the
    default ~/.council/vault. Kept in sync with the rest of the app."""
    if vault_dir:
        return Path(vault_dir).expanduser()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".council" / "vault"


def tools_dir(vault_dir: Optional[Any] = None) -> Path:
    return resolve_vault_root(vault_dir) / TOOLS_DIRNAME


def _ensure_dir(vault_dir: Optional[Any] = None) -> Path:
    d = tools_dir(vault_dir)
    d.mkdir(parents=True, exist_ok=True)
    rd = d / README_FILENAME
    if not rd.exists():
        try:
            rd.write_text(_README, encoding="utf-8")
        except Exception:
            pass
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_name(name: str) -> str:
    """Lower-case, non-[a-z0-9_] -> _, collapse repeats, trim. Not guaranteed
    valid (caller still checks _NAME_RE), just normalised."""
    s = re.sub(r"[^a-z0-9_]+", "_", str(name or "").strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s


def entry_function(code: str) -> Optional[str]:
    """Return the name of the tool's entry point: the single top-level
    function def. Returns None if there is not exactly one."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    return fns[0] if len(fns) == 1 else None


def _index_path(vault_dir: Optional[Any] = None) -> Path:
    return _ensure_dir(vault_dir) / INDEX_FILENAME


def _load_index(vault_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    p = _index_path(vault_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_index(items: List[Dict[str, Any]], vault_dir: Optional[Any] = None) -> None:
    try:
        _index_path(vault_dir).write_text(
            json.dumps(items, indent=2), encoding="utf-8")
    except Exception:
        pass


def _make_header(name: str, description: str, entry: str, author: str) -> str:
    return (
        "# ============================================================\n"
        "# APP-BUILT TOOL — UNREVIEWED · may be INACCURATE.\n"
        f"# name:        {name}\n"
        f"# description: {(description or '').strip()[:200]}\n"
        f"# entry:       {entry}\n"
        f"# author:      {author}\n"
        f"# created:     {_now_iso()}\n"
        "# Validated by the analyst sandbox (no delete/write-outside-output/\n"
        "# network/shell), but NOT checked for correctness. Review before you\n"
        "# trust its output. Delete this file to remove the tool.\n"
        "# ============================================================\n"
    )


def save_tool(name: str, description: str, code: str, *,
              author: str = "model",
              vault_dir: Optional[Any] = None
              ) -> Tuple[bool, str, Optional[str]]:
    """Validate + persist a model-authored tool. Returns (ok, message, name).

    The code must define EXACTLY ONE top-level function (the entry point) and
    must pass the analyst sandbox validator (no delete/write/network/shell).
    """
    slug = sanitize_name(name)
    if not _NAME_RE.match(slug):
        return (False, "invalid tool name — use 3-40 chars of a-z, 0-9, _ "
                "(start with a letter)", None)
    code = (code or "").strip()
    if not code:
        return False, "no code provided", None
    if len(code.encode("utf-8", "replace")) > _MAX_CODE_BYTES:
        return False, f"tool code exceeds {_MAX_CODE_BYTES} bytes", None
    # Same validator the pandas sandbox uses.
    try:
        from vault_analyst import validate_generated_code
    except Exception as exc:
        return False, f"validator unavailable: {exc!r}", None
    ok, why = validate_generated_code(code)
    if not ok:
        return False, f"rejected by sandbox validator: {why}", None
    entry = entry_function(code)
    if entry is None:
        return (False, "the tool must define EXACTLY ONE top-level function "
                "(its entry point)", None)
    existing = _load_index(vault_dir)
    if len(existing) >= _MAX_TOOLS and all(it.get("name") != slug for it in existing):
        return False, f"tool limit reached ({_MAX_TOOLS})", None
    d = _ensure_dir(vault_dir)
    path = d / f"{slug}.py"
    try:
        path.write_text(
            _make_header(slug, description, entry, author) + "\n" + code + "\n",
            encoding="utf-8")
    except Exception as exc:
        return False, f"could not write tool file: {exc!r}", None
    items = [it for it in existing if it.get("name") != slug]
    items.append({
        "name": slug,
        "description": (description or "").strip()[:300],
        "entry": entry,
        "author": author,
        "created": _now_iso(),
        "filename": path.name,
    })
    _save_index(items, vault_dir)
    return True, f"saved app-built tool '{slug}' (entry: {entry})", slug


def list_tools(vault_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The tool index, reconciled against files actually on disk."""
    d = tools_dir(vault_dir)
    items = _load_index(vault_dir)
    return [it for it in items if (d / it.get("filename", "")).exists()]


def get_tool(name: str, vault_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    slug = sanitize_name(name)
    for it in _load_index(vault_dir):
        if it.get("name") == slug:
            return it
    return None


def get_tool_code(name: str, vault_dir: Optional[Any] = None) -> Optional[str]:
    it = get_tool(name, vault_dir)
    if not it:
        return None
    p = tools_dir(vault_dir) / it.get("filename", "")
    try:
        return p.read_text(encoding="utf-8") if p.exists() else None
    except Exception:
        return None


def run_tool(name: str, args: Optional[Dict[str, Any]] = None, *,
             allowed_folders: Optional[List[Any]] = None,
             vault_dir: Optional[Any] = None) -> Tuple[Any, str]:
    """Run an app-built tool through the analyst sandbox. Returns
    ``(result_df_or_None, message)`` where the message is prefixed
    ``[app-built tool — UNVERIFIED]``. args must be a JSON-ish dict; it is
    embedded as a literal into the call so no extra namespace is injected."""
    it = get_tool(name, vault_dir)
    if not it:
        return None, f"[app-built tool — UNVERIFIED] tool '{name}' not found"
    code = get_tool_code(name, vault_dir)
    if code is None:
        return None, f"[app-built tool — UNVERIFIED] tool '{name}' file missing"
    entry = it.get("entry") or entry_function(code)
    if not entry:
        return None, f"[app-built tool — UNVERIFIED] tool '{name}' has no entry function"
    args = args or {}
    if not isinstance(args, dict):
        return None, "[app-built tool — UNVERIFIED] args must be a dict"
    # Build a call that assigns `result_df` (what execute_pandas_code returns).
    # Non-frame returns are wrapped into a 1-cell frame so scalars/strings work.
    call = (
        f"\n\n_ret = {entry}(**{args!r})\n"
        "result_df = _ret if isinstance(_ret, (pd.DataFrame, pd.Series, dict)) "
        "else pd.DataFrame([{'result': _ret}])\n"
    )
    script = code + call
    if allowed_folders is None:
        try:
            import data_index
            allowed_folders = [data_index.input_dir(resolve_vault_root(vault_dir))]
        except Exception:
            allowed_folders = [resolve_vault_root(vault_dir)]
    try:
        from vault_analyst import execute_pandas_code
    except Exception as exc:
        return None, f"[app-built tool — UNVERIFIED] sandbox unavailable: {exc!r}"
    df, msg = execute_pandas_code(script, allowed_folders)
    return df, "[app-built tool — UNVERIFIED] " + msg
