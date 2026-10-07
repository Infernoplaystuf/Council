"""
council_core.council_tools — the tools a council member can call in a turn.

run_python, vault_save / vault_list / vault_read / vault_search, api_search
and api_signature live here; the other twenty (calc, quote_check,
data_query, code_grep, run_tests, shared_notes, …) are in
council_core.tool_kit. Each is a ToolFn: it takes the model's args dict and
returns (ok, message for the model, payload). ModelAgent.act runs them when
a member answers with tool JSON, and the payloads reach the synthesizer as
PRIOR TOOL OUTPUTS.

make_tools returns a tool_kit.ToolSet: the tools by name, plus which role
gets which (ROLE_TOOLS), a per-question cache and the usage meter. The
Council tab builds it (CouncilActions.tools) and hands it to
council_turn.run_turn when its Tools switch is on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from council_core.deliberation import ToolFn
from council_core.tool_kit import (SharedNotes, ToolSet, make_extra_tools,
                                   tool)


def vault_search_impl(vault_dir: Path, query: str, *, max_files: int = 80) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"query": q, "matches": []}
    qlow = q.lower()
    matches = []
    files = sorted(vault_dir.iterdir(), key=lambda p: p.name.lower())
    scanned = 0
    for p in files:
        if scanned >= max_files or not p.is_file():
            continue
        scanned += 1
        try:
            if p.stat().st_size > 250_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        tlow = text.lower()
        idx = tlow.find(qlow)
        if idx == -1:
            continue
        start = max(0, idx - 140)
        end = min(len(text), idx + 260)
        excerpt = text[start:end].replace("\n", " ")
        matches.append({"file": p.name, "excerpt": excerpt})
        if len(matches) >= 12:
            break
    return {"query": q, "matches": matches, "scanned": scanned}


def free_name(folder: Path, name: str) -> str:
    """`name`, or `name_2`, `name_3`… — the first one not already in
    `folder`. vault_save never overwrites a vault file."""
    try:
        from council_engine import safe_name
    except Exception:                                     # noqa: BLE001
        def safe_name(n, _limit=128):
            return n
    base = safe_name(name, 128)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    cand, i = base, 2
    while (Path(folder) / cand).exists():
        cand = f"{stem}_{i}" + (f".{ext}" if ext else "")
        i += 1
    return cand


def make_tools(runner: Any, librarian: Any, vault_dir: Path, *,
               roots: Any = ()) -> ToolSet:
    """Every tool, by name, as a ToolSet (see tool_kit.ROLE_TOOLS for who
    gets which).

    `runner` is a council_engine.LocalRunner (run_python) and `librarian` a
    council_engine.Librarian (vault_save / vault_list / vault_read); each is
    used only when its tool is called, so a caller that has neither can still
    list the tools and use the vault search and API lookups. `roots` are
    extra folders (a code project) the reading tools may look in."""
    @tool('{"code": "print(1)", "filename": "scratch.py"} — runs Python in '
          "the workspace; stdout, stderr and exit code")
    def run_python(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        code = str(args.get("code", "")).strip()
        if not code:
            return False, "No code provided.", {}
        rc, out, err, path = runner.run_code(code, filename_hint=str(args.get("filename", "scratch.py")), timeout_s=int(args.get("timeout_s", 120)))
        return True, f"rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\nfile={path}", {"rc": rc, "stdout": out, "stderr": err, "path": str(path)}

    @tool('{"name": "note.md", "content": "..."} — saves a NEW vault file '
          "(an existing name gets _2, _3…; nothing is overwritten)")
    def vault_save(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "note.txt")).strip() or "note.txt"
        content = str(args.get("content", ""))
        if not content:
            return False, "No content provided.", {}
        folder = Path(getattr(librarian, "vault_dir", vault_dir))
        name = free_name(folder, name)
        p = librarian.save_text(name, content)
        return True, f"Saved to vault as '{p.name}'.", {"path": str(p)}

    @tool("{} — the files at the top of the vault")
    def vault_list(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        items = librarian.list_items()
        return True, "\n".join(items) if items else "(empty)", {"items": items}

    @tool('{"name": "file.txt"} — one whole vault file')
    def vault_read(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "")).strip()
        if not name:
            return False, "Provide {'name': 'file.txt'}", {}
        txt = librarian.read_text(name)
        return True, txt, {"name": name}

    @tool('{"query": "exact words"} — vault files containing the words, '
          "with an excerpt")
    def vault_search(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        res = vault_search_impl(vault_dir, str(args.get("query", "")))
        lines = [f"Vault search: {res.get('query','')}"]
        for m in res.get("matches", []):
            lines.append(f"- {m['file']}: {m['excerpt']}")
        if not res.get("matches"):
            lines.append("(no matches)")
        return True, "\n".join(lines), res

    @tool('{"query": "what it does", "k": 6} — functions in the vault\'s '
          "own Python, with real signatures")
    def api_search(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """Find callables in the vault's own Python by what they do.

        LOOKUP, NOT EXECUTION — see api_catalogue's module docstring. Calling a
        vault function would mean importing arbitrary customer or vendor code,
        and an import runs its top level. The agent gets the REAL signature and
        writes code the user runs, which prevents the failure this exists for
        anyway: a model recalling `size=` when the parameter is `element_size=`.
        """
        try:
            import api_catalogue as _ac
        except Exception as exc:
            return False, f"api catalogue unavailable: {exc!r}", {}
        query = str(args.get("query", "")).strip()
        if not query:
            return False, "No query provided.", {}
        cat = _ac.build(vault_dir)
        try:
            k = max(1, min(20, int(args.get("k", 6))))
        except (TypeError, ValueError):
            k = 6
        hits = cat.search(query, k=k)
        if not hits:
            return True, (
                f"No callable in the vault matches {query!r}. "
                f"({len(cat)} indexed from {cat.files_scanned} file(s).)"
            ), {"matches": [], "indexed": len(cat)}
        body = "\n\n".join(_ac.describe(h) for h in hits)
        head = (f"{len(hits)} of {len(cat)} catalogued callable(s) matching "
                f"{query!r}:")
        return True, head + "\n\n" + body, {"matches": hits,
                                            "indexed": len(cat)}

    @tool('{"name": "export_stl"} — the exact parameters of one function '
          "and a call template")
    def api_signature(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """The exact parameter list for one callable, plus a call template.

        Use this before writing any call into generated code: the template
        shows the call SHAPE with the blanks marked, so only argument values
        have to be supplied and the structure cannot be invented."""
        try:
            import api_catalogue as _ac
        except Exception as exc:
            return False, f"api catalogue unavailable: {exc!r}", {}
        name = str(args.get("name", "")).strip()
        if not name:
            return False, "No name provided.", {}
        cat = _ac.build(vault_dir)
        spec = cat.get(name)
        if spec is None:
            near = [h["name"] for h in cat.search(name, k=5)]
            msg = f"{name!r} is not in the vault catalogue."
            if near:
                msg += " Did you mean: " + ", ".join(near) + "?"
            return True, msg, {"found": False, "near": near}
        return True, _ac.describe(spec), {"found": True, "spec": spec}

    notes = SharedNotes()
    tools: Dict[str, ToolFn] = {
        "run_python": run_python, "vault_save": vault_save,
        "vault_list": vault_list, "vault_read": vault_read,
        "vault_search": vault_search,
        "api_search": api_search, "api_signature": api_signature}
    tools.update(make_extra_tools(vault_dir, notes=notes, roots=roots or ()))
    return ToolSet(tools, notes=notes, vault_dir=vault_dir)
