"""
council_core.vault_ops — vault operations, with no user interface attached.

The first extraction of the shell port. `_vmgr_build_keyword_index` lived on
CouncilConsole and did four things at once: fetched the index, set a Tk
variable, started a thread, and marshalled progress back with `self.after(0,
...)`. Only one of those four is the actual operation.

WHAT MOVED AND WHAT DID NOT
Moved here: getting the index, running the rebuild, turning the outcome into a
sentence a person can read. Left with the caller: the thread, and the decision
about which thread the progress callback lands on. That split is deliberate —
Tk marshals with `after(0, ...)` and Qt with a queued signal, and a function
that tried to do it for both would have to know which front end it was under,
which is exactly the coupling this package exists to remove.

WHY PROGRESS IS A CALLBACK AND NOT A QUEUE
A queue would force one shape on both front ends (and on any script that wants
to build an index with no UI at all). A callback is what `VaultIndex.rebuild`
already takes, so this layer passes it straight through and adds nothing.

THREADING, STATED PLAINLY
Nothing in here starts a thread. `build_keyword_index` blocks for as long as the
walk takes — minutes on a large vault — so a UI caller runs it on a worker and
delivers `on_progress` and the returned result back to its own UI thread. Both
front ends do exactly that, in five lines each.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

#: progress(done, total, name) — the signature VaultIndex.rebuild already fires.
ProgressFn = Callable[[int, int, str], None]

#: The longest filename a progress line shows before it is trimmed. A vault
#: holds paths long enough to wrap a status bar, and a wrapping status bar
#: resizes the layout under the user's cursor.
NAME_LIMIT = 48


@dataclass
class IndexResult:
    """What happened, in a form either front end can render without deciding
    anything. `message` is the sentence the user sees; `ok` is what decides
    whether it is shown as success or failure."""
    ok: bool
    message: str
    indexed: int = 0
    total: int = 0
    error: Optional[BaseException] = None


def short_name(name: str, limit: int = NAME_LIMIT) -> str:
    """Trim a filename for a one-line status. Kept here rather than in either
    view because both would otherwise trim differently and the same index run
    would read differently depending on which front end ran it."""
    name = str(name)
    return name if len(name) <= limit else name[:limit - 3] + "…"


def progress_line(done: int, total: int, name: str) -> str:
    """The status sentence for one indexed file."""
    return f"Indexing {done}/{total} — {short_name(name)}"


def build_keyword_index(index: Any,
                        on_progress: Optional[ProgressFn] = None) -> IndexResult:
    """Walk the vault and (re)build the keyword index. Fast — no LLM.

    ``index`` is a VaultIndex (or None, which is what the shell's lazy getter
    returns when the index could not be constructed — an unreadable vault, a
    missing dependency). Returning a result for that case rather than raising
    keeps the caller's error path and its success path the same shape.

    BLOCKS. Run it on a worker thread; see the module docstring.
    """
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    try:
        indexed = index.rebuild(progress=on_progress)
    except Exception as exc:                              # noqa: BLE001
        # The operation failing is a normal outcome here — an unreadable file,
        # a vanished folder — and the user needs the reason, not a traceback in
        # a console they are not looking at.
        return IndexResult(False, f"Keyword index failed: {exc!r}", error=exc)
    total = len(getattr(index, "records", ()) or ())
    return IndexResult(
        True,
        f"Keyword index built — {indexed} files (re)indexed, {total} total.",
        indexed=indexed, total=total)
