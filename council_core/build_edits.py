"""
council_core.build_edits — the Build tab's Edit mode: the edits a model
proposes, matched against the files, shown as diffs the user accepts or
rejects one by one; and the plan card a bigger request gets first.

EDITS ARE SEARCH/REPLACE BLOCKS
The model writes each change as

    app/main.py
    <<<<<<< SEARCH
    exact lines now in the file
    =======
    the lines to put there
    >>>>>>> REPLACE

with one line before it saying why. A small local model copies text far more
reliably than it counts lines or writes a unified diff, and a block that does
not match is refused with the closest text (project_tools.closest) instead of
landing in the wrong place. An empty SEARCH creates a new file.

MATCHING (resolve)
Exact text first, and it must be unique. Then line by line ignoring trailing
spaces, then ignoring one consistent indentation shift (the replacement is
re-indented by the same shift). A block whose text is in the file twice, is
not there at all, or overlaps another block in the same file is refused with
the reason; the rest still go through.

NOTHING IS WRITTEN HERE
`propose` reads through a callable (the editor's buffer when the file is
open, else the disk) and returns a Proposal; the tab applies the accepted
edits to the editor and, in a session, commits them (council_core.
build_session).

THE PLAN CARD (parse_plan, mark)
A request that needs more than one file, or that the model is unsure of,
gets a PLAN block first and no edits. Council marks it green, amber or red:
the model's own CONFIDENCE line sets the start (confidence.HIGH / LOW), and
checks Council can make itself lower it a step each: a file it names that
does not exist (and is not marked new), a name it plans to call that is not
in the code map. Questions keep it at amber at best. Nothing is edited until
the user answers or presses Go.
"""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Tuple

from . import confidence

EDIT_SYSTEM = (
    "You are the coding assistant in the Build tab of the Council, a desktop "
    "app that runs entirely on the user's own PC. You are in EDIT mode: you "
    "change the user's files by writing edit blocks, which the user reviews "
    "and accepts or rejects.\n\n"
    "Write every change as an edit block, exactly like this:\n\n"
    "Why: one line saying what this change does\n"
    "path/to/file.py\n"
    "<<<<<<< SEARCH\n"
    "the exact lines that are in the file now\n"
    "=======\n"
    "the lines that replace them\n"
    ">>>>>>> REPLACE\n\n"
    "Rules: the path is relative to the workspace folder, on its own line. "
    "SEARCH must copy the file's current lines exactly (same spaces), without "
    "the line numbers you are shown, and include enough lines to be unique. "
    "Keep each block small: one block per place that changes. To create a new "
    "file, leave SEARCH empty. Never repeat a whole file to change a few "
    "lines. Before the blocks, say in one or two sentences what you will do.\n\n"
    "If the request needs changes in MORE THAN ONE file, or you are not sure "
    "what the user wants, do not write edit blocks yet. Reply with a plan "
    "instead, exactly like this:\n\n"
    "PLAN\n"
    "Files: a.py, b.py, new_file.py (new)\n"
    "1. what you will change, naming the functions, like `load_rows()`\n"
    "2. ...\n"
    "Questions:\n"
    "- only what you need to know, at most three; leave this out if none\n"
    "CONFIDENCE: <0-100>% — what you are least sure of\n"
    "END PLAN\n\n"
    "When the user says go, write the edit blocks for the plan.")

#: The edit blocks' markers. Models write 5 to 9 marker characters.
_SEARCH = re.compile(r"^\s*<{5,9}\s*SEARCH\s*$")
_DIVIDE = re.compile(r"^\s*={5,9}\s*$")
_REPLACE = re.compile(r"^\s*>{5,9}\s*REPLACE\s*$")
_FENCE = re.compile(r"^\s*```")


@dataclass
class Block:
    """One edit as the model wrote it."""
    path: str
    search: str
    replace: str
    reason: str = ""
    index: int = 0               # its place in the reply, from 1


def _clean_path(line: str) -> str:
    s = line.strip().strip("`*").strip()
    s = re.sub(r"^(?:file|path)\s*:\s*", "", s, flags=re.I).strip("`*' \"")
    return s


def _looks_like_path(s: str) -> bool:
    return bool(s) and " " not in s.strip() and (
        "." in PurePosixPath(s.replace("\\", "/")).name or "/" in s or "\\" in s)


_NUMBERED = re.compile(r"^\s*\d+\| ?")


def _unnumber(lines: List[str]) -> List[str]:
    """The lines without the "12| " the Ask view numbers files with, when the
    model copied them (every non-blank line has one)."""
    full = [ln for ln in lines if ln.strip()]
    if full and all(_NUMBERED.match(ln) for ln in full):
        return [_NUMBERED.sub("", ln, count=1) for ln in lines]
    return lines


def parse_blocks(reply: str, default_path: str = "") -> List[Block]:
    """The edit blocks in a reply, in order. A block with no path line
    before it is for ``default_path`` (the open file). An unfinished block
    (the reply was stopped) is left out."""
    return _scan(reply, default_path)[0]


def _scan(reply: str, default_path: str = ""):
    """(the blocks, the (first, last) line of each, its why line to its
    closing fence)."""
    lines = (reply or "").split("\n")
    blocks: List[Block] = []
    spans: List[Tuple[int, int]] = []
    i = 0
    last_end = 0                       # the line after the previous block
    while i < len(lines):
        if not _SEARCH.match(lines[i]):
            i += 1
            continue
        # the path: the nearest line above that is not a fence or blank
        path, reason, j = "", "", i - 1
        top = i
        while j >= last_end and (not lines[j].strip() or _FENCE.match(lines[j])):
            j -= 1
        if j >= last_end and _looks_like_path(_clean_path(lines[j])):
            path = _clean_path(lines[j])
            top = j
            j -= 1
        while j >= last_end and (not lines[j].strip() or _FENCE.match(lines[j])):
            j -= 1
        if j >= last_end and re.match(r"^\s*\**(?:why|reason)\**\s*:", lines[j], re.I):
            reason = re.sub(r"^\s*\**(?:why|reason)\**\s*:\s*\**", "", lines[j].strip(),
                            flags=re.I).strip("*_ ")
            top = j
        elif j >= last_end and path:
            reason = lines[j].strip().strip("*_ ")
        k = i + 1
        search: List[str] = []
        while k < len(lines) and not _DIVIDE.match(lines[k]):
            search.append(lines[k])
            k += 1
        if k >= len(lines):
            break
        k += 1
        replace: List[str] = []
        while k < len(lines) and not _REPLACE.match(lines[k]):
            replace.append(lines[k])
            k += 1
        if k >= len(lines):
            break                      # stopped half way through a block
        search, replace = _unnumber(search), _unnumber(replace)
        blocks.append(Block(path or default_path, "\n".join(search),
                            "\n".join(replace), reason[:240], len(blocks) + 1))
        last_end = k + 1
        while last_end < len(lines) and _FENCE.match(lines[last_end]):
            last_end += 1              # the block's closing fence
        while top > 0 and _FENCE.match(lines[top - 1]):
            top -= 1
        spans.append((top, last_end - 1))
        i = last_end
    return blocks, spans


def prose(reply: str) -> str:
    """The reply without its edit blocks and its plan: what is left to show
    as text (the blocks are shown as diffs, the plan as its card)."""
    lines = (reply or "").split("\n")
    _blocks, spans = _scan(reply)
    drop = set()
    for a, b in spans:
        drop.update(range(a, b + 1))
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    cut = next((i for i, ln in enumerate(kept) if _SEARCH.match(ln)), None)
    if cut is not None:                # a block still being written, or stopped
        while cut > 0 and (not kept[cut - 1].strip() or _FENCE.match(kept[cut - 1])
                           or _looks_like_path(_clean_path(kept[cut - 1]))
                           or re.match(r"^\s*\**why\**\s*:", kept[cut - 1], re.I)):
            cut -= 1
        kept = kept[:cut]
    text = _PLAN.sub("", "\n".join(kept))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── matching a block against the file ──────────────────────────────────
@dataclass
class Edit:
    block: Block
    start: int = 0               # offsets in the file's text before the turn
    end: int = 0
    new: str = ""                # what goes in [start, end)
    error: str = ""              # why it was refused; "" = it can be applied
    accepted: Optional[bool] = None
    how: str = "exact"           # exact | spaces | indent

    @property
    def ok(self) -> bool:
        return not self.error


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip(" \t"))]


def _trim_blank(lines: List[str]) -> List[str]:
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _indent_shift(window: List[str], want: List[str]) -> Optional[Tuple[str, str]]:
    """(the file's indent, the block's indent) when ``want`` is ``window``
    with every line's indentation changed by the same amount, else None."""
    first = next(((a, b) for a, b in zip(window, want) if b.strip()), None)
    if first is None:
        return None
    ia, ib = _indent(first[0]), _indent(first[1])
    for a, b in zip(window, want):
        if not b.strip():
            if a.strip():
                return None
            continue
        eb = _indent(b)
        if a.strip() != b.strip() or not eb.startswith(ib) or \
                _indent(a) != ia + eb[len(ib):]:
            return None
    return ia, ib


def resolve(text: str, block: Block) -> Edit:
    """Where ``block`` goes in ``text``, or why it cannot."""
    edit = Edit(block)
    search, replace = block.search, block.replace
    if not search.strip():
        if text.strip():
            edit.error = (f"SEARCH is empty but {block.path} already has text; "
                          "copy the lines to replace into SEARCH.")
            return edit
        edit.start, edit.end = 0, len(text)
        edit.new = replace + ("\n" if replace and not replace.endswith("\n") else "")
        return edit
    count = text.count(search)
    if count == 1:
        edit.start = text.index(search)
        edit.end = edit.start + len(search)
        edit.new = replace
        return edit
    if count > 1:
        at = [text[:m.start()].count("\n") + 1
              for m in re.finditer(re.escape(search), text)]
        edit.error = (f"The SEARCH text is in {block.path} {count} times (lines "
                      f"{', '.join(map(str, at[:8]))}); include more lines so "
                      "it is unique.")
        return edit
    lines = text.split("\n")
    want = _trim_blank(search.split("\n"))
    rep_lines = _trim_blank(replace.split("\n"))
    starts = _line_starts(text)
    n = len(want)
    for how in ("spaces", "indent"):
        hits = []
        for i in range(0, len(lines) - n + 1):
            window = lines[i:i + n]
            if how == "spaces":
                if all(a.rstrip() == b.rstrip() for a, b in zip(window, want)):
                    hits.append((i, None))
            else:
                shift = _indent_shift(window, want)
                if shift is not None:
                    hits.append((i, shift))
        if len(hits) == 1:
            i, shift = hits[0]
            last = i + n - 1
            edit.start, edit.end = starts[i], starts[last] + len(lines[last])
            if shift is not None:
                ia, ib = shift
                rep_lines = [ia + ln[len(ib):] if ln.strip() and ln.startswith(ib)
                             else (ia + ln.lstrip(" \t") if ln.strip() else ln)
                             for ln in rep_lines]
            edit.new = "\n".join(rep_lines)
            edit.how = how
            return edit
        if len(hits) > 1:
            edit.error = (f"The SEARCH text matches {len(hits)} places in "
                          f"{block.path}; include more lines so it is unique.")
            return edit
    from .project_tools import closest
    edit.error = f"The SEARCH text is not in {block.path}. " + closest(text, search)
    return edit


# ── a turn's edits, per file ───────────────────────────────────────────
@dataclass
class FileChange:
    path: str                    # relative to the workspace, with /
    before: str
    exists: bool
    edits: List[Edit] = field(default_factory=list)
    problem: str = ""            # it could not be read (binary, too big)

    def after(self, only_accepted: bool = True) -> str:
        chosen = [e for e in self.edits if e.ok and
                  (e.accepted if only_accepted else e.accepted is not False)]
        text = self.before
        for e in sorted(chosen, key=lambda e: e.start, reverse=True):
            text = text[:e.start] + e.new + text[e.end:]
        return text

    def diff(self, edit: Edit, context: int = 3) -> str:
        """``edit`` alone, as a unified diff of the file."""
        one = self.before[:edit.start] + edit.new + self.before[edit.end:]
        name = self.path
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), one.splitlines(keepends=True),
            "a/" + name if self.exists else "/dev/null", "b/" + name,
            n=context)).rstrip("\n")

    def first_line(self, edit: Edit) -> int:
        return self.before.count("\n", 0, edit.start) + 1


@dataclass
class Proposal:
    changes: List[FileChange] = field(default_factory=list)

    def edits(self) -> List[Tuple[FileChange, Edit]]:
        return sorted(((c, e) for c in self.changes for e in c.edits),
                      key=lambda ce: ce[1].block.index)

    def refused(self) -> List[Edit]:
        return [e for _c, e in self.edits() if not e.ok]

    def pending(self) -> List[Edit]:
        return [e for _c, e in self.edits() if e.ok and e.accepted is None]

    def accept_all(self) -> None:
        for _c, e in self.edits():
            if e.ok and e.accepted is None:
                e.accepted = True

    def accepted_changes(self) -> List[FileChange]:
        return [c for c in self.changes if any(e.ok and e.accepted for e in c.edits)]


def safe_rel(root: Path, path: str) -> Optional[str]:
    """``path`` relative to ``root`` with forward slashes, or None when it is
    outside it (an absolute path elsewhere, or one that climbs out)."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return None
    root = Path(root).resolve()
    p = Path(raw)
    full = (p if p.is_absolute() else root / p).resolve()
    try:
        rel = full.relative_to(root)
    except ValueError:
        return None
    return rel.as_posix() if str(rel) != "." else None


def propose(blocks: List[Block], root: Path,
            read: Callable[[str], Optional[str]]) -> Proposal:
    """Match every block against its file. ``read(rel)`` is the file's text
    now (the editor's buffer when it is open), or None when it does not
    exist; an error it raises (a binary file) refuses that file's edits."""
    prop = Proposal()
    by_path: Dict[str, FileChange] = {}
    for b in blocks:
        rel = safe_rel(root, b.path)
        if rel is None:
            fc = FileChange(b.path or "(no file)", "", False)
            e = Edit(b, error=(f"{b.path or 'No file was named'}: an edit must be "
                               "to a file inside the workspace folder."))
            fc.edits.append(e)
            prop.changes.append(fc)
            continue
        b.path = rel
        fc = by_path.get(rel)
        if fc is None:
            try:
                text, problem = read(rel), ""
            except Exception as exc:                      # noqa: BLE001
                text, problem = "", f"{rel} cannot be edited here: {exc}"
            fc = FileChange(rel, text or "", text is not None or bool(problem))
            fc.problem = problem
            by_path[rel] = fc
            prop.changes.append(fc)
        if fc.problem:
            fc.edits.append(Edit(b, error=fc.problem))
            continue
        e = resolve(fc.before, b)
        if e.ok:
            for other in fc.edits:
                if other.ok and e.start < other.end and other.start < e.end:
                    e.error = (f"It overlaps edit {other.block.index} in {rel}; "
                               "put both changes in one block.")
                    break
                if other.ok and not fc.before.strip():
                    e.error = f"{rel} is new; put all of it in one block."
                    break
        fc.edits.append(e)
    return prop


def retry_note(refused: List[Edit]) -> str:
    """The message that asks the model to resend the edits it got wrong."""
    parts = ["Some of your edit blocks could not be applied:"]
    for e in refused:
        parts.append(f"Block {e.block.index} ({e.block.path}): {e.error}")
    parts.append("Send corrected edit blocks for those changes only, copying "
                 "SEARCH exactly from the file.")
    return "\n\n".join(parts)


# ── where an edit sits, for the step list and the review ────────────────
def enclosing(text: str, line: int) -> str:
    """The innermost def or class around ``line`` (1-based) in Python
    ``text``, as a dotted name, or "" (module level, or not Python)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ""
    best, best_size = "", None

    def walk(node, prefix):
        nonlocal best, best_size
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                end = getattr(child, "end_lineno", child.lineno)
                if start <= line <= end and (best_size is None or end - start < best_size):
                    best, best_size = name, end - start
                walk(child, name + ".")
            else:
                walk(child, prefix)
    walk(tree, "")
    return best


# ── the plan card ──────────────────────────────────────────────────────
@dataclass
class Plan:
    files: List[str]
    steps: List[str]
    questions: List[str]
    confidence: Optional[int]
    unsure: str
    raw: str

    def new_files(self) -> List[str]:
        return [f for f in self.files if f.endswith("(new)")]


_PLAN = re.compile(r"^\s*\**PLAN\**\s*:?\s*$(.*?)(?:^\s*\**END PLAN\**\s*$|\Z)",
                   re.M | re.S)


def parse_plan(reply: str) -> Optional[Plan]:
    """The PLAN block in a reply, or None."""
    m = _PLAN.search(reply or "")
    if not m:
        return None
    body = m.group(1)
    body, pct, unsure = confidence.split_draft(body)
    files: List[str] = []
    steps: List[str] = []
    questions: List[str] = []
    in_q = False
    for ln in body.split("\n"):
        s = ln.strip()
        if not s:
            continue
        low = s.lower().strip("*")
        if low.startswith("files:") or low.startswith("files :"):
            files = [f.strip().strip("`") for f in s.split(":", 1)[1].split(",") if f.strip()]
            in_q = False
        elif low.startswith("questions"):
            in_q = True
            rest = s.split(":", 1)[1].strip() if ":" in s else ""
            if rest and rest.lower() not in ("none", "-", "none."):
                questions.append(rest)
        elif in_q:
            q = re.sub(r"^[-*\d.)\s]+", "", s).strip()
            if q and q.lower() not in ("none", "none."):
                questions.append(q)
        else:
            steps.append(re.sub(r"^[-*]\s+", "", s))
    if not files and not steps:
        return None
    return Plan(files, steps, questions[:3], pct, unsure, m.group(0).strip())


_CALLED = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)\(\)?[^`]*`")

GREEN, AMBER, RED = "green", "amber", "red"
_LEVELS = (GREEN, AMBER, RED)


def mark(plan: Plan, exists: Callable[[str], bool],
         known: Callable[[str], bool]) -> Tuple[str, List[str]]:
    """(green | amber | red, the reasons) for a plan."""
    pct = plan.confidence
    if pct is None:
        level, reasons = 1, ["The model gave no confidence."]
    elif pct >= confidence.HIGH:
        level, reasons = 0, [f"The model is {pct}% sure."]
    elif pct > confidence.LOW:
        level, reasons = 1, [f"The model is {pct}% sure."]
    else:
        level, reasons = 2, [f"The model is only {pct}% sure."]
    if plan.unsure:
        reasons[0] = reasons[0].rstrip(".") + f" (least sure of: {plan.unsure})."
    missing = [f for f in plan.files
               if not f.endswith("(new)") and not exists(f)]
    if missing:
        level += 1
        reasons.append("Not in the folder: " + ", ".join(missing) + ".")
    names = []
    for step in plan.steps:
        for m in _CALLED.finditer(step):
            name = m.group(1).split(".")[-1]
            if name not in names:
                names.append(name)
    unknown = [n for n in names if not known(n)]
    if unknown:
        level += 1
        reasons.append("Not in the code: " + ", ".join(f"`{n}`" for n in unknown)
                       + " (new, or a guess).")
    if plan.questions:
        level = max(level, 1)
        reasons.append("It has questions for you.")
    return _LEVELS[min(level, 2)], reasons


__all__ = ["EDIT_SYSTEM", "Block", "Edit", "FileChange", "Proposal", "Plan",
           "parse_blocks", "prose", "resolve", "propose", "safe_rel",
           "retry_note", "enclosing", "parse_plan", "mark",
           "GREEN", "AMBER", "RED"]
