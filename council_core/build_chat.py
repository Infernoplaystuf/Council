"""
council_core.build_chat — the Build tab's chat: what the model is shown each
turn, and the conversation kept per folder.

ASK MODE (this step). The model answers and explains; it changes no file.
Code it suggests comes back in fenced blocks for the user to read or copy.
Edits shown as diffs to accept are the next step (docs/handoff/
build_tab_2026-10-29.md).

WHAT THE MODEL SEES (Context, then build_messages)
  * The open file, its lines numbered, so "line 42" means the same to both.
    A file over FILE_CHARS is cut to a window around the selection (or the
    cursor) and the model is told which lines it is seeing.
  * The selected lines, when there is a selection: "this" in a question
    usually means them.
  * The tail of the last run's output, when the user asks about it (a
    traceback is what "why does it fail?" is about).
  * The conversation so far, newest kept first, within HISTORY_CHARS; older
    turns are dropped and the model is told so.
The numbers are characters, not tokens, so this module needs no tokenizer;
the engine's clamp fits the whole prompt to the loaded model's window, and
the reply length grows with it (council_core.token_budget).

KEPT PER FOLDER. Each folder's conversation is one JSON file in the vault's
.council_build/chats/ (named from the folder's path), written atomically
after every turn and read back when that folder is opened again. A dot-folder,
so the vault's search never indexes the app's own chats (vault_context.
is_app_source); one written to build_chats/ by the first version is still
read. Nothing leaves the PC:
the model is the Council's own local one (the "coder" role, else the main
model).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

#: The open file is cut to a window around the selection beyond this.
FILE_CHARS = 24_000
#: The run output's tail, at most this many lines and characters.
OUTPUT_LINES = 60
OUTPUT_CHARS = 6_000
#: Earlier turns kept in the prompt, newest first, within this.
HISTORY_CHARS = 12_000
#: The other files shown in Edit mode, in all.
OTHERS_CHARS = 30_000
#: Turns kept on disk per folder.
KEEP_TURNS = 200

SYSTEM = (
    "You are the coding assistant in the Build tab of the Council, a desktop "
    "app that runs entirely on the user's own PC. You are in ASK mode: answer, "
    "explain, review and suggest; you cannot change files. When you suggest "
    "code, put it in fenced code blocks with the language (```python), show "
    "only the lines that change with enough around them to find the place, "
    "and name the file and line numbers. The open file is shown with its line "
    "numbers; refer to lines by those numbers. Be direct and concrete. If the "
    "question cannot be answered from what you were shown, say what you would "
    "need to see.")


@dataclass
class Context:
    """What the editor shows when the user asks."""
    path: str = ""               # the open file, or "" when none is open
    text: str = ""               # its text as edited (saved or not)
    language: str = ""
    sel_start: int = 0           # selected lines, 1-based; 0 = no selection
    sel_end: int = 0
    cursor_line: int = 1
    output: str = ""             # the last run's output, "" to leave it out
    folder: str = ""
    # Edit mode (council_core.build_edits)
    root: str = ""               # paths are shown relative to it
    numbered: bool = True        # line numbers on the open file (Ask mode)
    others: List[Tuple[str, str]] = field(default_factory=list)  # (path, text)
    code_map: str = ""           # council_core.code_map, the folder's outline
    extra: str = ""              # more for this turn (a review group's diff)

    def shown_path(self, path: str = "") -> str:
        """``path`` (the open file by default) relative to ``root``."""
        path = path or self.path
        if self.root and path:
            try:
                return Path(path).resolve().relative_to(Path(self.root).resolve()).as_posix()
            except (OSError, ValueError):
                pass
        return path


@dataclass
class Turn:
    role: str                    # "user" or "assistant"
    content: str
    time: float = field(default_factory=time.time)
    file: str = ""               # the file open when it was asked
    mode: str = "ask"            # ask | edit
    kind: str = ""               # assistant: "" | edits | plan; user: "" | retry | go
    steps: List[dict] = field(default_factory=list)   # what the turn did


def numbered(text: str, first: int = 1) -> str:
    lines = text.split("\n")
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{first + i:>{width}}| {ln}" for i, ln in enumerate(lines))


def file_window(text: str, around: int, limit: int = FILE_CHARS):
    """(first line, last line, text) of ``text`` within ``limit`` characters,
    centred on line ``around`` (1-based). The whole text when it fits."""
    lines = text.split("\n")
    if len(text) <= limit:
        return 1, len(lines), text
    i = max(0, min(len(lines) - 1, around - 1))
    lo = hi = i
    size = len(lines[i]) + 1
    while True:
        grew = False
        if lo > 0 and size + len(lines[lo - 1]) + 1 <= limit:
            lo -= 1
            size += len(lines[lo]) + 1
            grew = True
        if hi < len(lines) - 1 and size + len(lines[hi + 1]) + 1 <= limit:
            hi += 1
            size += len(lines[hi]) + 1
            grew = True
        if not grew:
            break
    return lo + 1, hi + 1, "\n".join(lines[lo:hi + 1])


def tail(text: str, lines: int = OUTPUT_LINES, chars: int = OUTPUT_CHARS) -> str:
    out = "\n".join(text.rstrip("\n").split("\n")[-lines:])
    return out[-chars:]


def context_block(ctx: Context) -> str:
    """The editor's state, as the text in front of the question."""
    parts: List[str] = []
    if ctx.folder:
        parts.append(f"Workspace folder: {ctx.folder}")
    if ctx.path:
        lines = ctx.text.split("\n")
        around = ctx.sel_start or ctx.cursor_line or 1
        first, last, shown = file_window(ctx.text, around)
        name = Path(ctx.path).name
        whole = (first, last) == (1, len(lines))
        head = (f"Open file: {ctx.shown_path()} ({ctx.language or 'text'}, "
                f"{len(lines)} lines)")
        if not whole:
            head += f". It is long: you are shown lines {first}-{last} only"
        body = numbered(shown, first) if ctx.numbered else shown
        parts.append(f"{head}\n```{name}\n{body}\n```")
        if ctx.sel_start:
            sel = "\n".join(lines[ctx.sel_start - 1:ctx.sel_end])
            sel = numbered(sel, ctx.sel_start) if ctx.numbered else sel
            parts.append(f"The user has selected lines {ctx.sel_start}-{ctx.sel_end}:\n"
                         f"```\n{sel}\n```")
        else:
            parts.append(f"The cursor is on line {ctx.cursor_line}.")
    else:
        parts.append("No file is open.")
    room = OTHERS_CHARS
    for path, text in ctx.others:
        if len(text) > room:
            parts.append(f"(Also open, not shown: {ctx.shown_path(path)}, too long.)")
            continue
        room -= len(text)
        parts.append(f"Also open: {ctx.shown_path(path)}\n```{Path(path).name}\n{text}\n```")
    if ctx.code_map:
        parts.append(ctx.code_map)
    if ctx.extra.strip():
        parts.append(ctx.extra.strip())
    if ctx.output.strip():
        parts.append(f"The last run's output (its end):\n```\n{tail(ctx.output)}\n```")
    return "\n\n".join(parts)


def build_messages(history: List[Turn], question: str, ctx: Context,
                   system: str = SYSTEM, label: str = "Question") -> List[Dict[str, str]]:
    """The chat messages for one turn: the system prompt, as much of the
    conversation as fits HISTORY_CHARS (newest kept), then the editor's
    state and the question."""
    kept: List[Turn] = []
    size = 0
    for t in reversed(history):
        size += len(t.content)
        if size > HISTORY_CHARS:
            break
        kept.append(t)
    kept.reverse()
    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
    if len(kept) < len(history):
        messages.append({"role": "system", "content":
                         f"(The {len(history) - len(kept)} earliest messages of this "
                         "conversation are not shown.)"})
    messages += [{"role": t.role, "content": t.content} for t in kept]
    messages.append({"role": "user",
                     "content": f"{context_block(ctx)}\n\n{label}: {question.strip()}"})
    return messages


_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def code_blocks(text: str) -> List[str]:
    """The fenced code blocks in a reply, in order."""
    return [m.group(1).rstrip("\n") for m in _FENCE.finditer(text or "")]


#: A folder with more entries than this gets no code map (a drive's root
#: would take minutes to walk at every turn).
MAP_MAX_ENTRIES = 4000
MAP_CHARS = 3_000


def code_map_text(root: Path, focus: str, cache_dir: Optional[Path] = None) -> str:
    """council_core.code_map's outline of ``root`` for an Edit-mode prompt,
    the files relevant to ``focus`` first; "" for a folder too large to walk
    quickly, or on any error."""
    return code_map_for(root, focus, cache_dir)[0]


def code_map_for(root: Path, focus: str, cache_dir: Optional[Path] = None):
    """(the outline's text, the CodeMap) — the map checks a plan's names;
    ("", None) when there is none."""
    from . import code_map
    root = Path(root)
    seen = 0
    try:
        for _dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in code_map.SKIP_DIRS]
            seen += len(files) + len(dirs)
            if seen > MAP_MAX_ENTRIES:
                return "", None
        cache = None
        if cache_dir is not None:
            key = hashlib.sha1(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()[:16]
            cache = Path(cache_dir) / f"{key}.json"
        cmap = code_map.for_project(root, cache)
        return cmap.render(focus, max_chars=MAP_CHARS), cmap
    except Exception:                                     # noqa: BLE001
        return "", None


# ── the conversation on disk ──────────────────────────────────────────────
def store_path(vault_dir: Path, folder: str) -> Path:
    key = os.path.normcase(os.path.abspath(folder or "scratch"))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(folder or "scratch").name)[:40] or "folder"
    return Path(vault_dir) / ".council_build" / "chats" / f"{stem}-{digest}.json"


def _old_store_path(vault_dir: Path, folder: str) -> Path:
    """Where the first version kept it (searched by the vault index)."""
    p = store_path(vault_dir, folder)
    return Path(vault_dir) / "build_chats" / p.name


class Conversation:
    """One folder's chat: its turns, read from and written to the vault."""

    def __init__(self, vault_dir: Path, folder: str):
        self.folder = str(folder or "")
        self.path = store_path(vault_dir, self.folder)
        self._old = _old_store_path(vault_dir, self.folder)
        self.turns: List[Turn] = self._load()

    def _load(self) -> List[Turn]:
        src = self.path if self.path.exists() or not self._old.exists() else self._old
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            return [Turn(**{k: t[k] for k in ("role", "content", "time", "file", "mode",
                                              "kind", "steps") if k in t})
                    for t in data.get("turns", [])
                    if t.get("role") in ("user", "assistant")]
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"folder": self.folder, "turns": [asdict(t) for t in self.turns[-KEEP_TURNS:]]}
        fd, tmp = tempfile.mkstemp(prefix=".chat.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        self.turns = []
        self.save()

    def ask(self, question: str, ctx: Context,
            chat: Callable[..., str],
            on_token: Optional[Callable[[str], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None,
            system: str = SYSTEM, mode: str = "ask", kind: str = "") -> str:
        """One turn: the model's answer to ``question`` about ``ctx``.
        ``chat(messages, on_token=, should_stop=)`` is the model call. The
        question and the answer are kept and saved; a stopped answer is kept
        as far as it got, marked as stopped. An error is raised after the
        question is kept."""
        messages = build_messages(self.turns, question, ctx, system,
                                  "Request" if mode == "edit" else "Question")
        self.turns.append(Turn("user", question.strip(), file=ctx.path, mode=mode,
                               kind=kind))
        partial: List[str] = []

        def token(piece: str) -> None:
            partial.append(piece)
            if on_token is not None:
                on_token(piece)
        try:
            answer = chat(messages, on_token=token, should_stop=should_stop)
        except Exception as exc:
            got = getattr(exc, "partial", "") or "".join(partial)
            if got.strip() and type(exc).__name__ == "GenerationCancelled":
                self.turns.append(Turn("assistant", got.strip() + "\n\n(stopped)",
                                       file=ctx.path, mode=mode))
            self.save()
            raise
        self.turns.append(Turn("assistant", (answer or "").strip(), file=ctx.path,
                               mode=mode))
        self.save()
        return answer
