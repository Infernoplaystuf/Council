"""
council_core.project_tools — what a coding agent may do inside its job's
worktree.

    list_files       files under a folder (code and text)
    read_file        a line range, numbered (at most 400 lines a call)
    find_symbol      where a class / function / method is, with its signature
    search_code      regex over the worktree, file:line hits
    edit_file        replace text that occurs EXACTLY ONCE (or every time,
                     when asked) — the edit, not the file
    create_file      a new file (refuses to overwrite one)
    run_tests        the project's test command, or one test file / -k filter
    gui_check        build a widget offscreen: errors, widget tree, screenshot
    lint             syntax errors, undefined names, unused imports
    search_references the documents attached to the project
    step_done        "this step is finished", with a one-line summary

WHY EDIT AND NOT REWRITE. Asked to return a whole 1,200-line file, a small
model drops functions, re-indents blocks and "tidies" code it was not asked
to touch. An edit names the exact text to replace; if that text is not in
the file, or is there twice, nothing changes and the model is told where the
closest match is — so a wrong edit fails loudly instead of corrupting the
file quietly.

Everything is confined to the worktree (a job's own git checkout): a path
outside it, or inside .git, is refused. Nothing here deletes a file. Tests
and the GUI check run in child processes with time limits.
"""
from __future__ import annotations

import difflib
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .tool_kit import PathError, _obj, lint, tool

Result = Tuple[bool, str, Dict[str, Any]]

_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}

READ_MAX_LINES = 400
TEST_TIMEOUT_S = 600
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".ini", ".cfg",
                 ".yaml", ".yml", ".qss", ".ui", ".js", ".ts", ".html",
                 ".css", ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs",
                 ".sql", ".bat", ".ps1", ".sh", ".rst"}

PARAMS: Dict[str, Dict[str, Any]] = {
    "list_files": _obj([], path=_S, pattern=_S),
    "read_file": _obj(["path"], path=_S, start=_I, end=_I),
    "find_symbol": _obj(["name"], name=_S),
    "search_code": _obj(["pattern"], pattern=_S, glob=_S),
    "edit_file": _obj(["path", "old", "new"], path=_S, old=_S, new=_S,
                      replace_all=_B),
    "create_file": _obj(["path", "content"], path=_S, content=_S),
    "run_tests": _obj([], target=_S, k=_S),
    "gui_check": _obj([], module=_S, cls=_S),
    "lint": _obj(["path"], path=_S),
    "search_references": _obj(["query"], query=_S),
    "step_done": _obj(["summary"], summary=_S),
}


class Workspace:
    """The worktree a job edits, and what it may do there."""

    def __init__(self, root: Path, *, test_command: str = "",
                 gui_checks: Sequence[Any] = (), references: Any = None,
                 code_map: Optional[Callable[[], Any]] = None,
                 out_dir: Optional[Path] = None,
                 runner: Any = None):
        self.root = Path(root).resolve()
        self.test_command = test_command
        self.gui_checks = list(gui_checks)
        self.references = references
        self._code_map = code_map
        self.out_dir = Path(out_dir) if out_dir else self.root.parent
        self.runner = runner
        #: Files changed by edit_file / create_file since the last commit.
        self.touched: List[str] = []
        self.done: Optional[str] = None

    # -- paths ----------------------------------------------------------
    def path(self, raw: str, *, must_exist: bool = True) -> Path:
        raw = str(raw or "").strip().strip('"').strip("'").replace("\\", "/")
        if not raw:
            raise PathError("no path given")
        p = (self.root / raw).resolve() if not Path(raw).is_absolute() \
            else Path(raw).resolve()
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            raise PathError(f"{raw} is outside the project") from None
        if rel.parts and rel.parts[0] == ".git":
            raise PathError("the .git folder is not for editing")
        if must_exist and not p.exists():
            raise PathError(f"{raw} does not exist")
        return p

    def rel(self, p: Path) -> str:
        return Path(p).resolve().relative_to(self.root).as_posix()

    def _touch(self, p: Path) -> None:
        r = self.rel(p)
        if r not in self.touched:
            self.touched.append(r)

    # -- tools ----------------------------------------------------------
    def tools(self) -> Dict[str, Callable[[Dict[str, Any]], Result]]:
        ws = self

        def guard(fn):
            def call(args):
                try:
                    return fn(args)
                except PathError as exc:
                    return False, str(exc), {}
            call.help = fn.help
            call.params = PARAMS[fn.__name__]
            call.__name__ = fn.__name__
            return call

        @tool('{"path": "app", "pattern": "*.py"} — the files under a '
              "folder (the whole project when no path)")
        def list_files(args):
            base = ws.path(args.get("path") or ".")
            pat = str(args.get("pattern") or "*")
            out = []
            for f in sorted(base.rglob(pat)):
                rel = f.relative_to(ws.root).parts
                if f.is_file() and not any(x.startswith(".") or x in (
                        "__pycache__", "node_modules", "venv") for x in rel):
                    out.append(ws.rel(f))
                if len(out) >= 300:
                    out.append("… (more; narrow the path)")
                    break
            return True, "\n".join(out) or "(no files)", {"count": len(out)}

        @tool('{"path": "app/camera.py", "start": 1, "end": 120} — lines of '
              "a file, numbered (400 at most per call)")
        def read_file(args):
            p = ws.path(args.get("path"))
            lines = p.read_text(encoding="utf-8",
                                errors="replace").splitlines()
            start = max(1, int(args.get("start") or 1))
            end = int(args.get("end") or start + 199)
            end = min(len(lines), end, start + READ_MAX_LINES - 1)
            body = "\n".join(f"{i:5d}  {lines[i - 1]}"
                             for i in range(start, end + 1))
            more = (f"\n… {len(lines) - end} more lines (read with start="
                    f"{end + 1})" if end < len(lines) else "")
            return True, (f"{ws.rel(p)} lines {start}-{end} of {len(lines)}:"
                          f"\n{body}{more}"), {"total": len(lines)}

        @tool('{"name": "set_exposure"} — where a class, function or method '
              "is defined, with its signature")
        def find_symbol(args):
            name = str(args.get("name") or "").strip()
            if not name:
                return False, "No name given.", {}
            if ws._code_map is None:
                return False, "No code map for this project.", {}
            hits = ws._code_map().find(name)
            if not hits:
                return True, f"No definition named like {name!r}.", {"hits": 0}
            return True, "\n".join(hits[:40]), {"hits": len(hits)}

        @tool('{"pattern": "def on_save", "glob": "*.py"} — regex search, '
              "file:line hits (80 at most)")
        def search_code(args):
            try:
                rx = re.compile(str(args.get("pattern") or ""))
            except re.error as exc:
                return False, f"Bad regex: {exc}", {}
            if not rx.pattern:
                return False, "No pattern given.", {}
            glob = str(args.get("glob") or "")
            hits = []
            for f in sorted(ws.root.rglob("*")):
                rel = f.relative_to(ws.root)
                if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES \
                        or any(x.startswith(".") or x == "__pycache__"
                               for x in rel.parts):
                    continue
                if glob and not f.match(glob):
                    continue
                try:
                    for n, ln in enumerate(f.read_text(
                            encoding="utf-8", errors="replace")
                            .splitlines(), 1):
                        if len(ln) < 2000 and rx.search(ln):
                            hits.append(f"{rel.as_posix()}:{n}: "
                                        f"{ln.strip()[:160]}")
                            if len(hits) >= 80:
                                break
                except OSError:
                    continue
                if len(hits) >= 80:
                    hits.append("… (stopped at 80; narrow the pattern)")
                    break
            return True, "\n".join(hits) or "(no matches)", {"hits": len(hits)}

        @tool('{"path": "app/camera.py", "old": "exact text now in the file",'
              ' "new": "its replacement"} — change one place in a file; the '
              "old text must appear exactly once (copy it from read_file, "
              "without the line numbers)")
        def edit_file(args):
            p = ws.path(args.get("path"))
            old = str(args.get("old") or "")
            new = str(args.get("new") or "")
            if not old:
                return False, "Give the exact old text to replace.", {}
            text = p.read_text(encoding="utf-8", errors="replace")
            count = text.count(old)
            if count == 0:
                return False, ("The old text is not in " + ws.rel(p) + ". "
                               + closest(text, old)), {}
            if count > 1 and not args.get("replace_all"):
                lines = [text[:m.start()].count("\n") + 1
                         for m in re.finditer(re.escape(old), text)]
                return False, (f"The old text appears {count} times (lines "
                               f"{', '.join(map(str, lines[:10]))}). Include "
                               "more surrounding lines so it is unique, or "
                               "set replace_all."), {}
            updated = text.replace(old, new) if args.get("replace_all") \
                else text.replace(old, new, 1)
            write(p, updated)
            ws._touch(p)
            msg = f"Edited {ws.rel(p)} ({count if args.get('replace_all') else 1} place(s))."
            if p.suffix == ".py":
                problems = lint(updated, ws.rel(p))
                syntax = [x for x in problems if "SyntaxError" in x]
                if syntax:
                    msg += " WARNING — the file no longer parses: " + syntax[0]
            return True, msg, {"path": ws.rel(p)}

        @tool('{"path": "app/new_tab.py", "content": "..."} — a NEW file '
              "(refuses to overwrite an existing one; use edit_file)")
        def create_file(args):
            p = ws.path(args.get("path"), must_exist=False)
            if p.exists():
                return False, (f"{ws.rel(p)} already exists; change it with "
                               "edit_file."), {}
            content = str(args.get("content") or "")
            p.parent.mkdir(parents=True, exist_ok=True)
            write(p, content)
            ws._touch(p)
            msg = f"Created {ws.rel(p)} ({content.count(chr(10)) + 1} lines)."
            if p.suffix == ".py":
                syntax = [x for x in lint(content, ws.rel(p))
                          if "SyntaxError" in x]
                if syntax:
                    msg += " WARNING — it does not parse: " + syntax[0]
            return True, msg, {"path": ws.rel(p)}

        @tool('{"target": "tests/test_camera.py", "k": "exposure"} — run the '
              "project's tests (all, one file, or a -k filter)")
        def run_tests(args):
            ok, out = ws.run_tests(str(args.get("target") or ""),
                                   str(args.get("k") or ""))
            return True, out, {"passed": ok}

        @tool('{} or {"module": "app.camera_tab", "cls": "CameraTab"} — build '
              "the widget offscreen: errors, the widget tree, a screenshot")
        def gui_check(args):
            results = ws.gui(str(args.get("module") or ""),
                             str(args.get("cls") or ""))
            if not results:
                return False, ("No GUI check is set up for this project; "
                               "give module and cls."), {}
            ok = all(r.ok for r in results)
            return True, "\n\n".join(r.summary(3000) for r in results), \
                {"ok": ok, "screenshots": [r.screenshot for r in results]}

        @tool('{"path": "app/camera.py"} — syntax errors, undefined names and '
              "unused imports, without running it")
        def lint_tool(args):
            p = ws.path(args.get("path"))
            problems = lint(p.read_text(encoding="utf-8", errors="replace"),
                            ws.rel(p))
            return True, ("\n".join(problems) if problems
                          else f"{ws.rel(p)}: no problems."), \
                {"problems": problems}
        lint_tool.__name__ = "lint"

        @tool('{"query": "exposure limits"} — passages from the documents '
              "attached to this project")
        def search_references(args):
            if ws.references is None:
                return True, "No references are attached to this project.", {}
            block = ws.references.block(str(args.get("query") or ""))
            return True, block or "(nothing in the references matches)", {}

        @tool('{"summary": "what this step changed"} — call when the step is '
              "finished and its check passes")
        def step_done(args):
            ws.done = str(args.get("summary") or "done").strip()[:500]
            return True, "Step marked done.", {}

        fns = [list_files, read_file, find_symbol, search_code, edit_file,
               create_file, run_tests, gui_check, lint_tool,
               search_references, step_done]
        return {f.__name__: guard(f) for f in fns}

    # -- runners ----------------------------------------------------------
    def run_tests(self, target: str = "", k: str = "",
                  timeout: float = TEST_TIMEOUT_S) -> Tuple[bool, str]:
        """(passed, the last 60 lines of output)."""
        if not self.test_command and not target:
            return False, "This project has no test command set."
        from . import child_proc
        argv = shlex.split(self.test_command or "python -m pytest -q",
                           posix=(sys.platform != "win32"))
        if argv and argv[0] in ("python", "python3", "py"):
            argv[0] = sys.executable
        if target:
            target_path = self.path(target)
            argv.append(self.rel(target_path))
        if k:
            argv += ["-k", k]
        runner = self.runner or child_proc.run
        res = runner(argv, cwd=str(self.root), timeout=timeout,
                     env=child_proc.child_env({
                         "PYTHONDONTWRITEBYTECODE": "1",
                         "QT_QPA_PLATFORM": "offscreen",
                         "COUNCIL_NO_DIALOGS": "1"}),
                     memory_limit_mb=4096)
        if getattr(res, "timed_out", False):
            return False, f"The tests ran longer than {timeout:.0f} s and were stopped."
        out = ((getattr(res, "stdout", "") or "") + "\n"
               + (getattr(res, "stderr", "") or "")).strip()
        tail = "\n".join(out.splitlines()[-60:])
        passed = getattr(res, "returncode", 1) == 0
        return passed, f"TESTS {'PASSED' if passed else 'FAILED'}:\n{tail}"

    def gui(self, module: str = "", cls: str = ""):
        from . import gui_check
        checks = [(module, cls, {})] if module and cls else [
            (g.module, g.cls, getattr(g, "kwargs", {}) or {})
            for g in self.gui_checks]
        out = []
        for i, (m, c, kw) in enumerate(checks):
            out.append(gui_check.check(
                self.root, m, c, kw,
                out_dir=self.out_dir / "gui" / f"{i}-{m}.{c}"))
        return out


def write(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".council-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def closest(text: str, old: str) -> str:
    """Where the file is most like `old` — so the model can copy the real
    text."""
    lines = text.splitlines()
    want = old.strip().splitlines() or [old]
    n = max(1, len(want))
    best, at = 0.0, 0
    first = want[0].strip()
    for i in range(0, max(1, len(lines) - n + 1)):
        r = difflib.SequenceMatcher(None, first, lines[i].strip()).ratio()
        if r > best:
            best, at = r, i
    if best < 0.5:
        return "Nothing in the file looks like it; read the file first."
    seg = "\n".join(f"{j + 1:5d}  {lines[j]}"
                    for j in range(at, min(len(lines), at + n + 2)))
    return ("The closest text is at line " + str(at + 1)
            + " (copy it exactly, without the numbers):\n" + seg)


__all__ = ["Workspace", "PARAMS", "closest"]
