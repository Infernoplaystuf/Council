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

import re
from pathlib import Path
from typing import Any, Dict, Tuple

from council_core.deliberation import ToolFn
from council_core.tool_kit import (PathError, SharedNotes, ToolSet, _clip,
                                   _readable, make_extra_tools, read_text,
                                   resolve_in, tool)


def _plain_run_python_reason(err: str) -> str:
    """One plain sentence for a common sandbox refusal, so a model (and the
    transcript) reads words, not the sandbox's own jargon or a traceback.
    "" when none fits — the sandbox's line is left to speak for itself."""
    e = str(err or "")
    low = e.lower()
    if ("import blocked by sandbox" in low or "forbidden import" in low
            or ("blocked unsafe pattern" in low and "import" in low)):
        return ("the sandbox does not allow that import; allowed: pandas, "
                "numpy, scipy, math, re, json, statistics, collections and "
                "pathlib")
    if ("forbidden attribute: savefig" in low or "savefig" in low
            or "print_figure" in low or "writing is blocked" in low
            or "writes a file" in low
            or re.search(r"blocked unsafe pattern.*open", low)
            or ".to_csv" in low or ".to_excel" in low or ".to_parquet" in low
            or re.search(r"\bto_(csv|excel|parquet|pickle)\b", low)):
        return ("run_python cannot write files: print the answer or set "
                "result_df (only the helpers write, into data_out)")
    if "__build_class__" in e:
        return "class definitions are not available in the sandbox"
    if ("read budget exceeded" in low or "unable to allocate" in low
            or "memoryerror" in low):
        return "the sandbox caps memory at 2048 MB"
    if ("outside the data folders" in low
            or "file access is blocked" in low):
        return "the sandbox reads only the data folder (data_in)"
    return ""


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
        # Never the app's own files (tool_kit._readable): question_history
        # .json and verdict_history.jsonl hold every older conversation's
        # questions, and the question being asked — a search for any of its
        # words hit them, and the excerpt went into the member's prompt.
        if not _readable(p, vault_dir):
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


def saved_name(name: str) -> str:
    """The name a file called `name` really gets at the vault root: the
    Librarian's characters (council_engine.safe_name), without trailing dots
    and spaces — Windows drops them when it creates the file, so a file
    saved as "node_routing.json." IS node_routing.json. "" when nothing is
    left."""
    try:
        from council_engine import safe_name
        base = safe_name(str(name or ""), 128)
    except Exception:                                     # noqa: BLE001
        base = str(name or "").strip()
    return base.rstrip(". ")


def is_app_file_name(name: str, vault_dir: Path) -> bool:
    """Whether a file called `name` at the vault root would be one of the
    app's own: a dot-file, one of council_core.vault_setup.VAULT_ROOT_STATE
    (the settings the app reads at the root), or what conversation_logger
    protects (older conversations, learned memory, settings). Case does not
    matter (Windows' file names do not), and neither do trailing dots and
    spaces (Windows drops them: "node_routing.json." is node_routing.json)."""
    low = str(name or "").strip().rstrip(". ").lower()
    if not low or low.startswith("."):
        return True
    try:
        from council_core.vault_setup import VAULT_ROOT_STATE
        if low in {n.lower() for n in VAULT_ROOT_STATE}:
            return True
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import conversation_logger
        return bool(conversation_logger.is_protected_path(
            Path(vault_dir) / low, vault_dir))
    except Exception:                                     # noqa: BLE001
        return True


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

    `librarian` is a council_engine.Librarian (vault_save / vault_list /
    vault_read), used only when its tool is called, so a caller that has
    none can still list the tools and use the vault search and API lookups.
    `runner` is no longer used — run_python runs in the analyst's sandbox,
    not through council_engine.LocalRunner — and is kept so callers need
    not change. `roots` are extra folders (a code project) the reading
    tools may look in, and the only places run_tests runs tests."""
    @tool('{"code": "df = pd.read_csv(\'runs.csv\'); print(df.describe())"}'
          " — Python in the analyst's sandbox (a child process, at most "
          "120 s; timeout_s may lower it, but a value under a few seconds is "
          "not useful — the sandbox's start-up does not count against it): "
          "pandas, numpy, scipy, math, re, json, statistics, collections "
          "and pathlib, reading the data folder (data_in) only; print() the "
          "answer or set result_df. No charts (matplotlib is not available; "
          "the Artist makes charts). Only the sandbox's helpers write, into "
          "data_out (and the app's stats cache); it starts no processes")
    def run_python(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """The model's code under the fence data_query and the analyst use
        (analyst_step.run_script: the sandbox in a child process, the time
        limit capped at analyst_step.TIME_LIMIT_S, a memory cap, the tree
        killed at the limit). It ran in a plain child process with the
        app's environment and full access to the user's files: it could
        write node_routing.json at the vault root, read anything and start
        processes. Nothing is written for it — no workspace/scratch.py — and
        a `filename` argument is ignored."""
        code = str(args.get("code", "")).strip()
        if not code:
            return False, "No code provided.", {}
        from council_core import analyst_step
        import data_index
        run = analyst_step.run_script(
            code, data_index.input_dir(Path(vault_dir)),
            timeout=analyst_step.time_limit(
                args.get("timeout_s", analyst_step.TIME_LIMIT_S)),
            vault=Path(vault_dir))
        rc = 0 if run.ran else 1
        err = "\n".join(t for t in (run.stderr.rstrip(), run.error.rstrip(),
                                    run.note) if t)
        # One plain sentence before the sandbox's own line, so a refusal
        # reads as words and not jargon — kept ahead of the original, which
        # stays for detail.
        if not run.ran:
            hint = _plain_run_python_reason(run.error or err)
            if hint:
                err = hint + "\n" + err
        payload: Dict[str, Any] = {"rc": rc, "stdout": run.stdout,
                                   "stderr": err}
        # A traceback's last lines say what went wrong: keep its tail.
        shown_err = err if len(err) <= 3000 else "…" + err[-3000:]
        msg = (f"rc={rc}\n--- stdout ---\n{_clip(run.stdout, 4000)}\n"
               f"--- stderr ---\n{shown_err}")
        if run.df is not None:
            import vault_analyst as va
            table = va.format_result_for_prompt(run.df, max_rows=100,
                                                max_chars=5000)
            # The table's full size: run.df holds only its first rows.
            rows, cols = run.shape or run.df.shape
            payload.update(table=table, rows=int(rows), cols=int(cols))
            msg += f"\n--- result_df ---\n{table}"
            if run.cut:
                msg += f"\n({run.cut})"
        if run.timed_out:
            payload["timed_out"] = True
        return run.ran, msg, payload

    @tool('{"name": "note.md", "content": "..."} — saves a NEW vault file '
          "(an existing name gets _2, _3…; nothing is overwritten)")
    def vault_save(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "note.txt")).strip() or "note.txt"
        content = str(args.get("content", ""))
        if not content:
            return False, "No content provided.", {}
        folder = Path(getattr(librarian, "vault_dir", vault_dir))
        # Never one of the app's files, present or not: a model-written
        # node_routing.json was the next launch's opt-in to send a role's
        # prompts to another machine (found in review), and the others are
        # settings or older conversations the app reads back. Checked, and
        # saved, under the name the file really gets (saved_name): Windows
        # drops a trailing dot, so "node_routing.json." was node_routing
        # .json past a check of the name as given.
        saved_as = saved_name(name)
        if is_app_file_name(saved_as, folder):
            return False, (f"'{saved_as or name}' is one of the app's own "
                           "files; save under another name."), {}
        name = free_name(folder, saved_as)
        p = librarian.save_text(name, content)
        return True, f"Saved to vault as '{p.name}'.", {"path": str(p)}

    @tool("{} — the files at the top of the vault")
    def vault_list(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        # The user's files only (tool_kit._readable): not the app's own at
        # the root — older conversations' questions, settings, dot-files.
        items = [n for n in librarian.list_items()
                 if _readable(Path(vault_dir) / n, vault_dir)]
        return True, "\n".join(items) if items else "(empty)", {"items": items}

    @tool('{"name": "file.txt"} — one whole vault file')
    def vault_read(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "")).strip()
        if not name:
            return False, "Provide {'name': 'file.txt'}", {}
        # Through tool_kit.resolve_in, as the other reading tools: inside
        # the vault, never a dot-folder or one of the app's own files.
        try:
            p = resolve_in(name, [Path(vault_dir).resolve()], vault_dir)
        except PathError as exc:
            return False, str(exc), {}
        if not p.is_file():
            return False, f"{name} is not a file", {}
        return True, read_text(p), {"name": p.name}

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
    # Tools Forge built and the USER approved (council_core.tool_review),
    # for the roles they were approved for.
    from council_core.tool_kit import ROLE_TOOLS
    role_tools = {r: tuple(t) for r, t in ROLE_TOOLS.items()}
    try:
        from council_core import tool_review
        for name, fn, roles in tool_review.approved_tools(vault_dir):
            tools[name] = fn
            for r in roles:
                role_tools[r] = role_tools.get(r, ()) + (name,)
    except Exception:                                     # noqa: BLE001
        pass
    return ToolSet(tools, notes=notes, vault_dir=vault_dir,
                   role_tools=role_tools)
