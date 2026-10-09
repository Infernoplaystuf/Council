"""
council_core.script_takeover — the user taking a model's script over.

WHAT IT IS
A pipeline script that carries nx_policy's model stamp (MODEL_STAMP, on its
first line) runs under the MODEL rules: the import allowlist, no file changes
of its own, and nx_guard holding every output to the vault's data_out. With
the stamp line deleted the script is the user's and runs under the USER rules
— their machine, their call (nx_policy "Two kinds of script"). Deleting the
line by hand was the only way to do that. This module is the same act, asked
for in words ("take over seg_five.py") or with a button, and it does that and
no more:

  * it removes ONLY the stamp line(s) — every line script_trust() reads as
    one (nx_policy.stamp_lines), so a stamp under a comment the user added
    goes too — and keeps every other byte: line endings, a BOM, the rest.
    A line is what Python, an editor and the runner call one: a bare CR
    ends it, so code a model puts after a CR on its stamp line stays;
  * it checks that what goes is comment and nothing else: Python must read
    the same code before and after (tokenize, comments aside, the source
    encoding included). A stamp inside a string, a "stamp" line that is a
    statement, a take-out that would change the file's coding line or
    join a bare CR above it and an empty line below it into one line break
    (a line the dialog does not mark would go too), or a file Python cannot
    read (before or after) is refused — the dialog's "nothing else" must be
    true — and left for the user's own editor;
  * it keeps a dated copy of the file as it was, in data_out/dream3d/takeover/
    (the app's own output area — where a script is the model's whatever its
    first line says, so the copy can never run as the user's);
  * it replaces the file atomically (a temp file beside it, then os.replace):
    a failure leaves the old script, never half of one, and deletes nothing
    of the user's; every file it makes is spelled to work past MAX_PATH;
  * it logs who and when to data_out/dream3d/takeover/takeover_log.jsonl.

It refuses a file with no stamp (it is the user's already), anything outside
the vault's script folders — pipelines/in and pipelines/out, where the app
saves the model scripts the user runs — a script in data_out (the location
rule keeps it a model script, stamp or not), a read-only file, and anything
but a .py file. A network or device path is refused from its spelling,
before anything asks the network about it.

ONLY THE USER CAN ASK
A take-over loosens what a script may do, so the request must be the user's
own. There are two doors:

  * what the user TYPED into a chat box, read by that box's send handler
    before any other command — Qt CouncilTab.on_send (the Dream3D chat sends
    through it) and Dream3DTab's standalone send, Tk _send_typed (the Send
    button, Ctrl+Enter, the Dream3D box). pipeline_intent.take_over_ref()
    reads the first line of that text and nothing else;
  * the Take over button next to the selected script (Qt and Tk Dream3D tabs).

Nothing else reaches run() or apply(). PipelineChat.plan() — which anything
holding text can call — and Tk's _handle_pipeline_intent — behind every other
route into Tk's _send: a re-ask, the disagree re-run, a speech transcription
— answer a take-over phrase with NOT_HERE and change nothing. A model's reply
goes to the transcript, which nothing parses; a model "modify" result, a
script's text and a file name are only ever shown. No agent tool imports this
module (the analyst sandbox cannot). tests/test_script_takeover.py holds each
of these.

And either door asks first: the confirmation says what changes, shows the
script's first lines, and only an explicit Yes acts. COUNCIL_NO_DIALOGS
(unattended runs) means nobody can say Yes, so nothing is taken over.
"""
from __future__ import annotations

import datetime
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import tokenize
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import nx_policy

#: Where the dated copies and the log go, under the vault's data_out.
SUBFOLDER = "dream3d/takeover"
LOG_NAME = "takeover_log.jsonl"

TITLE = "Take over this script?"
#: What a take-over changes, in the words the confirmation uses. The USER
#: rules (nx_policy.run_reasons, nx_guard) hold two filters and nothing
#: else: no process, socket or native-code refusal is left.
WHAT_CHANGES = ("This script will run as yours: it will be able to write and "
                "delete files anywhere your account can, run any program, "
                "use the network and load native code, and the Council stops "
                "checking its outputs. The Council still refuses only two "
                "DREAM3D-NX filters (Execute Process, Create Python Plugin).")
#: The answer to a take-over phrase that did not come through a door.
NOT_HERE = ("Taking a script over is done only from what you type in the chat "
            "box, or with the Take over button in the Dream3D tab — never "
            "from a re-sent, transcribed or generated message. Nothing "
            "changed.")
USAGE = ("Name the script to take over: take over <file name> (it must be in "
         "pipelines/in or pipelines/out) — or select it in the Dream3D tab "
         "and click Take over.")

HEAD_LINES = 12            # how much of the script the confirmation shows
_HEAD_WIDTH = 110
_MAX_BYTES = 8 * 1024 * 1024
_SCRIPT_SUFFIXES = (".py", ".d3dpipeline", ".dream3d")
_EXPLICIT = re.compile(r"\b(?:scripts?|pipelines?|marker|stamp)\b", re.I)

# The temp file the new script is written to, beside it: a short name, and
# room for it in the folder's spelling (mkstemp adds 8 characters).
_TEMP_PREFIX = ".~takeover-"
_TEMP_SUFFIX = ".tmp"
_TEMP_ROOM = 1 + len(_TEMP_PREFIX) + 16 + len(_TEMP_SUFFIX)

# Tokens that are not code: all a comment line may take with it.
_NOT_CODE = frozenset({tokenize.COMMENT, tokenize.NL})
# Tokens placed by the lines around them, not by one line's own text.
_PLACED = frozenset({tokenize.ENCODING, tokenize.INDENT, tokenize.DEDENT,
                     tokenize.ENDMARKER})
_STRINGS = frozenset({tokenize.STRING} | {
    getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE",
                                   "FSTRING_END") if hasattr(tokenize, n)})
# What tokenize raises for a file Python cannot read.
_CANNOT_READ = (tokenize.TokenError, SyntaxError, UnicodeDecodeError,
                LookupError, ValueError)

Confirm = Callable[[str, str], bool]


@dataclass
class Plan:
    """What a take-over would do to one script — or why it will not."""
    path: Optional[Path] = None      # the script (its real path)
    shown: str = ""                  # how to name it to the user
    refusal: str = ""                # why not; '' when it can go ahead
    stamp_lines: Tuple[int, ...] = ()
    head: Tuple[str, ...] = ()       # numbered lines, as the dialog shows them
    digest: str = ""                 # sha256 of the bytes the user is shown

    @property
    def ok(self) -> bool:
        return not self.refusal and self.path is not None


@dataclass
class Result:
    ok: bool
    message: str
    backup: Optional[Path] = None
    log: Optional[Path] = None
    removed: List[int] = field(default_factory=list)


# ============================================================
# Where scripts are
# ============================================================

def script_folders(vault_dir: Path) -> List[Path]:
    """The vault's script folders: the only places a take-over acts."""
    import pipeline_scanner
    return [pipeline_scanner.vault_pipelines_in_dir(vault_dir),
            pipeline_scanner.vault_pipelines_out_dir(vault_dir)]


def _data_out(vault_dir: Path) -> Path:
    from . import nx_ops
    return nx_ops.out_dir(vault_dir)


def _named(path) -> Path:
    """``path`` resolved, to NAME it (never to open it): without the \\\\?\\
    realpath keeps on a long one, so a file deep in a vault is still named
    from the vault (a dated copy's path passes MAX_PATH sooner than the
    vault's does)."""
    import path_contain
    real = path_contain.resolved(path)[1]
    if real.startswith("\\\\?\\") and not real.startswith("\\\\?\\UNC\\"):
        real = real[4:]
    return Path(real)


def _shown(path: Path, vault_dir: Path) -> str:
    try:
        return _named(path).relative_to(_named(vault_dir)).as_posix()
    except (OSError, ValueError, TypeError):
        return str(path)


def _as_spelled(path: Path, vault_dir: Path) -> str:
    """How to name ``path`` without resolving it — for a path that must
    not be resolved (a network or device path: resolving asks the network)."""
    try:
        return Path(path).relative_to(vault_dir).as_posix()
    except ValueError:
        return str(path)


def _fileish(ref: str) -> bool:
    return ("/" in ref or "\\" in ref
            or ref.lower().endswith(_SCRIPT_SUFFIXES))


def _matches(vault_dir: Path, ref: str) -> List[Path]:
    """Scripts in the script folders that ``ref`` names: a path (absolute, or
    under the vault, pipelines/ or either folder), else a file name — exact
    (with or without .py) before a part of one."""
    if _fileish(ref) and ("/" in ref or "\\" in ref):
        p = Path(ref).expanduser()
        if p.is_absolute():
            return [p]
        base = Path(vault_dir)
        for root in [base, base / "pipelines"] + script_folders(vault_dir):
            if (root / p).is_file():
                return [root / p]
        return [base / p]
    q = ref.strip().lower()
    found: List[Path] = []
    for folder in script_folders(vault_dir):
        try:
            found.extend(sorted(p for p in folder.rglob("*.py")
                                if p.is_file()))
        except OSError:
            continue
    exact = [p for p in found if q in (p.name.lower(), p.stem.lower())]
    if exact:
        return exact
    return [p for p in found if q and q in p.name.lower()]


def is_stamped(path) -> bool:
    """Does the .py file at ``path`` carry the model stamp? For list labels;
    False for anything unreadable."""
    p = Path(path)
    if p.suffix.lower() != ".py":
        return False
    try:
        if p.stat().st_size > _MAX_BYTES:
            return False
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return bool(nx_policy.stamp_lines(text))


# ============================================================
# Deciding
# ============================================================

def decide(vault_dir: Path, ref: str, typed: str = "") -> Optional[Plan]:
    """The plan for a take-over phrase ``typed`` that names ``ref``
    (pipeline_intent.take_over_ref), or None when the phrase is not about a
    script after all — "take over the planning" names no script and says
    neither script nor pipeline, so it is a question for the Council."""
    explicit = _fileish(ref) or bool(_EXPLICIT.search(
        (typed or "").split("\n", 1)[0]))
    if not ref:
        return Plan(refusal=USAGE) if explicit else None
    found = _matches(vault_dir, ref)
    if not found:
        if not explicit:
            return None
        return Plan(shown=ref, refusal=(
            f"No script named '{ref}' in pipelines/in or pipelines/out. "
            f"Nothing changed."))
    if len(found) > 1:
        names = "\n".join(f"  • {_shown(p, vault_dir)}" for p in found[:20])
        return Plan(shown=ref, refusal=(
            f"More than one script matches '{ref}':\n{names}\n\nType the one "
            f"you mean, e.g. take over {_shown(found[0], vault_dir)}. "
            f"Nothing changed."))
    return prepare(vault_dir, found[0])


def prepare(vault_dir: Path, path) -> Plan:
    """What taking ``path`` over would do, read from the file now — or why
    it will not be done."""
    import path_contain
    path = Path(path)
    # From the spelling alone, before anything resolves it: resolving
    # \\host\share asks the network for the host (path_contain).
    if path_contain.network_or_device(path) or \
            path_contain.has_stream_name(path):
        shown = _as_spelled(path, vault_dir)
        return Plan(shown=shown, refusal=(
            f"{shown} is not a script in the vault's script folders "
            f"(pipelines/in, pipelines/out). Nothing changed."))
    shown = _shown(path, vault_dir)
    try:
        real = Path(path_contain.resolved(path)[1])
    except (OSError, ValueError, TypeError) as exc:
        return Plan(shown=shown, refusal=f"{shown}: {exc}. Nothing changed.")
    if path_contain.is_under(real, _data_out(vault_dir)):
        return Plan(shown=shown, refusal=(
            f"{shown} is in the vault's data_out, where the app saves what a "
            f"model writes — a script there runs under the model-script "
            f"rules with or without its stamp. To run it as your own, read "
            f"it, copy it into pipelines/in, then take that copy over. "
            f"Nothing changed."))
    if not any(path_contain.is_under(real, f)
               for f in script_folders(vault_dir)):
        return Plan(shown=shown, refusal=(
            f"{shown} is outside the vault's script folders (pipelines/in, "
            f"pipelines/out); a take-over acts only on a script there. "
            f"Nothing changed."))
    if real.suffix.lower() != ".py":
        return Plan(shown=shown, refusal=(
            f"{shown} is not a Python script; only a .py script carries the "
            f"model stamp. Nothing changed."))
    try:
        if not real.is_file():
            return Plan(shown=shown, refusal=(
                f"There is no script at {shown}. Nothing changed."))
        if real.stat().st_size > _MAX_BYTES:
            return Plan(shown=shown, refusal=(
                f"{shown} is too large to be a pipeline script. Nothing "
                f"changed."))
        data = real.read_bytes()
    except OSError as exc:
        return Plan(shown=shown, refusal=(
            f"{shown} could not be read: {exc}. Nothing changed."))
    text, numbers, new = _take_out(data)
    if not numbers:
        return Plan(path=real, shown=shown, refusal=(
            f"{shown} carries no model stamp, so it already runs as yours "
            f"— there is nothing to take over. Nothing changed."))
    why = _changes_code(data, new, numbers, shown) or _read_only(real, shown)
    if why:
        return Plan(path=real, shown=shown, refusal=why)
    return Plan(path=real, shown=shown, stamp_lines=tuple(numbers),
                head=tuple(_head(text, numbers)),
                digest=hashlib.sha256(data).hexdigest())


def _decode(data: bytes) -> Tuple[bytes, str]:
    """(BOM, text): the text round-trips to the same bytes (surrogateescape)
    and splits into the lines the runner sees (it reads utf-8-sig)."""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return bom, data[len(bom):].decode("utf-8", "surrogateescape")


def _take_out(data: bytes) -> Tuple[str, List[int], bytes]:
    """(text, stamp line numbers, the file's bytes without those lines) —
    lines as nx_policy.lines_with_ends counts them, every other byte kept."""
    bom, text = _decode(data)
    numbers = nx_policy.stamp_lines(text)
    new = bom + nx_policy.without_stamp(text).encode("utf-8",
                                                     "surrogateescape")
    return text, numbers, new


def _python_reads(data: bytes) -> List[tokenize.TokenInfo]:
    """``data`` as Python reads a file it runs: the BOM and a coding line
    honoured, a bare CR a line break. Raises what tokenize raises for a
    file Python cannot read."""
    flat = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return list(tokenize.tokenize(io.BytesIO(flat).readline))


def _code(tokens: List[tokenize.TokenInfo]) -> List[Tuple[int, str]]:
    """The tokens Python runs: all but comments and blank-line breaks."""
    return [(t.type, t.string) for t in tokens if t.type not in _NOT_CODE]


def _unreadable(exc: Exception) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return f"byte {exc.start} is not {exc.encoding} text"
    if isinstance(exc, SyntaxError):
        return exc.msg or "a syntax error"
    if isinstance(exc, tokenize.TokenError) and exc.args:
        return str(exc.args[0])
    return str(exc) or type(exc).__name__


def _changes_code(data: bytes, new: bytes, numbers: List[int],
                  shown: str) -> str:
    """Why taking lines ``numbers`` out of ``data`` (which gives ``new``)
    would change more than the stamp — '' when they are comments and
    nothing else, so Python runs the same code before and after: the
    confirmation's "nothing else" must be true."""
    lines = _line_list(numbers)
    try:
        before = _python_reads(data)
    except _CANNOT_READ as exc:
        return (f"{shown} was not taken over: Python cannot read it as it "
                f"stands ({_unreadable(exc)}), so the Council cannot check "
                f"that taking out {lines} (the model stamp) would leave its "
                f"code as it is. If you have read it and want it as yours, "
                f"delete {lines} by hand. Nothing changed.")
    gone = set(numbers)
    found = {}
    for tok in before:
        if tok.type in _NOT_CODE or tok.type in _PLACED:
            continue
        for n in range(tok.start[0], tok.end[0] + 1):
            if n in gone and n not in found:
                found[n] = ("inside a string" if tok.type in _STRINGS
                            else "part of the code, not a comment")
    if found:
        what = "; ".join(f"line {n} is {why}"
                         for n, why in sorted(found.items()))
        reads = "reads" if len(numbers) == 1 else "read"
        it = "it" if len(numbers) == 1 else "them"
        return (f"{shown} was not taken over: {lines} {reads} as the model "
                f"stamp, but {what} — taking {it} out would change what "
                f"the script does, not only whose it is. Read it, and edit "
                f"the stamp out by hand if you want it as yours. Nothing "
                f"changed.")
    try:
        after = _python_reads(new)
    except _CANNOT_READ as exc:
        why = f"would leave a file Python cannot read ({_unreadable(exc)})"
    else:
        if before[0].string != after[0].string:
            # The ENCODING tokens: a coding line counts only in a file's
            # first two lines, so taking a line out can bring one in or out.
            why = ("would change the coding line Python reads its "
                   "characters by (one counts only in a file's first two "
                   "lines)")
        elif _code(before) != _code(after):
            why = "would change the code Python runs"
        else:
            joined = _joined(_decode(data)[1], gone)
            if not joined:
                return ""
            a, b = joined
            return (f"{shown} was not taken over: taking out {lines} (the "
                    f"model stamp) would join the bare CR that ends line {a} "
                    f"and the empty line {b} into one line break, so line "
                    f"{b} would go as well — more than the stamp. Edit the "
                    f"stamp out by hand if you want it as yours. Nothing "
                    f"changed.")
    return (f"{shown} was not taken over: taking out {lines} (the model "
            f"stamp) {why} — it would change what the script does, not "
            f"only whose it is. Read it, and edit the stamp out by hand if "
            f"you want it as yours. Nothing changed.")


def _joined(text: str, gone) -> Optional[Tuple[int, int]]:
    """(a, b) when taking lines ``gone`` out of ``text`` would put line a's
    bare CR next to line b, which is only a line feed: "\\r" + "\\n" is one
    line break, so line b would go too. None when every line kept stays a
    line of its own — then the file loses exactly the lines taken out."""
    lines = nx_policy.lines_with_ends(text)
    kept = [n for n in range(1, len(lines) + 1) if n not in gone]
    for a, b in zip(kept, kept[1:]):
        if b - a > 1 and lines[a - 1].endswith("\r") \
                and lines[b - 1].startswith("\n"):
            return a, b
    return None


def _read_only(real: Path, shown: str) -> str:
    """Why a read-only ``real`` is left as it is ('' when it is not): the
    take-over removes the stamp and changes nothing else — not the mark
    the user (or a tool of theirs) put on the file."""
    if os.access(real, os.W_OK):
        return ""
    return (f"{shown} is marked read-only, so it was not taken over. "
            f"Nothing changed. To take it over, clear its read-only mark "
            f"first, then ask again.")


def _printable(line: str) -> str:
    """A line of a model's file, safe to show: control and format characters
    (a bidi override can make a line read as something else) and line or
    paragraph separators (a dialog may break the row there, though Python
    does not) spelled out, and cut to the dialog's width."""
    line = line.rstrip("\r\n").replace("\t", "    ")
    out = "".join(ch if unicodedata.category(ch)[0] != "C"
                  and unicodedata.category(ch) not in ("Zl", "Zp")
                  else f"\\u{ord(ch):04x}" for ch in line)
    return out if len(out) <= _HEAD_WIDTH else out[:_HEAD_WIDTH - 1] + "…"


def _head(text: str, removed: List[int]) -> List[str]:
    lines = nx_policy.lines_with_ends(text)
    gone = set(removed)

    def row(n: int) -> str:
        mark = "-" if n in gone else " "
        return f"{mark} {n:4d} | {_printable(lines[n - 1])}"
    shown = [row(n) for n in range(1, min(len(lines), HEAD_LINES) + 1)]
    if len(lines) > HEAD_LINES:
        shown.append(f"       … {len(lines) - HEAD_LINES} more line(s)")
        shown += [row(n) for n in removed if n > HEAD_LINES]
    return shown


def _line_list(numbers) -> str:
    numbers = list(numbers)
    if len(numbers) == 1:
        return f"line {numbers[0]}"
    return ("lines " + ", ".join(str(n) for n in numbers[:-1])
            + f" and {numbers[-1]}")


def confirmation(plan: Plan) -> str:
    """The confirmation's text: what changes, plainly, then the script's
    first lines. Plain text — it quotes a file a model wrote."""
    verb = "is" if len(plan.stamp_lines) == 1 else "are"
    lines = [f"Take over {plan.shown}?", "", WHAT_CHANGES, "",
             f"What changes in the file: {_line_list(plan.stamp_lines)} "
             f"(the model stamp) {verb} removed, nothing else. A dated copy "
             f"of the file as it is now is kept in data_out/{SUBFOLDER}/.",
             "", "Read it before you say Yes. Its first lines (- = removed):"]
    lines += list(plan.head)
    return "\n".join(lines)


# ============================================================
# Doing — only after an explicit Yes
# ============================================================

def dialogs_off() -> bool:
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


def run(plan: Plan, vault_dir: Path, confirm: Confirm, *, via: str) -> str:
    """Ask, then act on an explicit Yes; what to tell the user either way.

    ``confirm(title, text)`` shows the question; only a return of exactly
    True is a Yes. Under COUNCIL_NO_DIALOGS it is never asked — nobody is
    there to say Yes, and a take-over is never assumed."""
    if not plan.ok:
        return plan.refusal or USAGE
    if dialogs_off():
        return (f"{plan.shown} was not taken over: a take-over is done only "
                f"on your explicit Yes, and dialogs are switched off here "
                f"(COUNCIL_NO_DIALOGS). Nothing changed.")
    try:
        yes = confirm(TITLE, confirmation(plan)) is True
    except Exception as exc:                              # noqa: BLE001
        return (f"{plan.shown} was not taken over: the confirmation could "
                f"not be shown ({exc!r}). Nothing changed.")
    if not yes:
        return (f"Not taken over: {plan.shown} is unchanged and still runs "
                f"under the model-script rules.")
    return apply(plan, vault_dir, via=via).message


def apply(plan: Plan, vault_dir: Path, *, via: str) -> Result:
    """Remove the stamp lines from ``plan.path``: keep a dated copy first,
    then replace the file atomically, then log. Call it only after the
    user's explicit Yes (run)."""
    if not plan.ok:
        return Result(False, plan.refusal or USAGE)
    real = plan.path
    try:
        data = real.read_bytes()
    except OSError as exc:
        return Result(False, f"{plan.shown} could not be read: {exc}. Nothing "
                             f"changed.")
    if hashlib.sha256(data).hexdigest() != plan.digest:
        return Result(False, f"{plan.shown} changed after you were asked, so "
                             f"it was not taken over. Nothing changed — ask "
                             f"again to see it as it is now.")
    text, numbers, new = _take_out(data)
    why = (_changes_code(data, new, numbers, plan.shown)
           or _read_only(real, plan.shown))
    if why:
        return Result(False, why)
    # As the runner will read it (utf-8-sig; script_trust reads a bare CR
    # as the line break read_text makes of it).
    after = new.decode("utf-8-sig", errors="replace")
    if nx_policy.script_trust(after, real, [_data_out(vault_dir)]) \
            != nx_policy.USER:
        return Result(False, f"{plan.shown} would still run under the "
                             f"model-script rules without its stamp, so it "
                             f"was not changed.")

    when = datetime.datetime.now().astimezone()
    try:
        backup = _keep_copy(vault_dir, real, data, when)
    except Exception as exc:                              # noqa: BLE001
        return Result(False, f"{plan.shown} was not taken over: a copy of it "
                             f"could not be kept ({exc}). Nothing changed.")
    try:
        _replace(real, new)
    except Exception as exc:                              # noqa: BLE001
        return Result(False, f"{plan.shown} was not taken over: it could not "
                             f"be rewritten ({exc}). Nothing changed; a copy "
                             f"is at {_shown(backup, vault_dir)}.",
                      backup=backup)
    message = (f"Took over {plan.shown}: removed {_line_list(numbers)} (the "
               f"model stamp), nothing else. It now runs as yours, under your "
               f"own rules. The file as it was is kept at "
               f"{_shown(backup, vault_dir)}.")
    log = None
    try:
        log = _log(vault_dir, {
            "when": when.isoformat(timespec="seconds"),
            "who": _account(),
            "via": via,
            "script": str(real),
            "removed_lines": numbers,
            "removed": [_printable(nx_policy.lines_with_ends(text)[n - 1])
                        for n in numbers],
            "backup": str(backup),
            "sha256_before": plan.digest,
            "sha256_after": hashlib.sha256(new).hexdigest(),
        })
    except Exception as exc:                              # noqa: BLE001
        message += f" (The take-over log could not be written: {exc}.)"
    return Result(True, message, backup=backup, log=log, removed=numbers)


def _account() -> str:
    try:
        return getpass.getuser()
    except Exception:                                     # noqa: BLE001
        return ""


def _keep_copy(vault_dir: Path, real: Path, data: bytes,
               when: datetime.datetime) -> Path:
    """The file as it was, under data_out — a new file, never one replaced."""
    from . import nx_ops
    stamp = when.strftime("%Y%m%d-%H%M%S")
    stem = real.stem[:80]           # a deep vault stays under MAX_PATH
    for n in range(1, 1000):
        tail = "" if n == 1 else f"_{n}"
        name = f"{stem}.before-takeover-{stamp}{tail}.py"
        target = nx_ops.safe_out_path(vault_dir, name, subfolder=SUBFOLDER)
        try:
            with open(_fs(target), "xb") as fh:
                fh.write(data)
            return target
        except FileExistsError:
            continue
    raise OSError("no free name for the copy")


def _fs(path: Path, room: int = 0) -> str:
    """``path`` spelled so the file system takes it at any length — the
    \\\\?\\ form once it (with ``room`` more characters) passes MAX_PATH, as
    in an 8.3 short-name vault whose long form is long (path_contain,
    "Comparing is not asking")."""
    import path_contain
    return path_contain.resolved(path, room)[1]


def _replace(real: Path, new: bytes) -> None:
    """``real`` becomes ``new`` all at once: written beside it, then
    os.replace. On a failure the temp file (ours) goes — its read-only mark,
    copied from ``real``, taken off first; ``real`` is as it was."""
    fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX,
                               dir=_fs(real.parent, _TEMP_ROOM))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            shutil.copymode(real, tmp)
        except OSError:
            pass
        os.replace(tmp, real)
    except BaseException:
        try:
            os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _log(vault_dir: Path, record: dict) -> Path:
    from . import nx_ops
    path = nx_ops.safe_out_path(vault_dir, LOG_NAME, subfolder=SUBFOLDER)
    with open(_fs(path), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
