"""
council_core.ide_trust — what a script is about to do, before it does it.

The IDE runs the buffer as a real Python subprocess with the user's own
permissions. Before that it scans for patterns that can affect the machine —
shelling out, deleting files, opening sockets, unpickling — and asks.

THIS IS THE ONE SECURITY-RELEVANT PIECE IN THE TAB, AND IT LIVED IN A WIDGET
It has no toolkit references at all and never did, so the only way to test it
was to open a window. Here it is unit-testable, which matters more for this
module than for any other in the port: a scanner nobody can test is a scanner
nobody knows the coverage of.

IT IS A SPEED BUMP, NOT A SANDBOX
A regex scan over source lines cannot be complete and does not claim to be —
`getattr(os, "sys" + "tem")` sails past it, and so does anything the script
downloads and runs. What it buys is that the OBVIOUS cases are shown to the
user before the subprocess starts. The forge's sandbox is the thing that
actually constrains; this is a prompt.

TRUST IS PER-SESSION AND PER-EXACT-TEXT
Keyed on the sha256 of the code, so editing one character asks again. That is
deliberate: the thing the user approved was that text, and "trust this file"
would carry approval across a change they did not read.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Sequence, Set, Tuple

#: (pattern, what it means in words). The wording is the product: "subprocess
#: calls" tells a user what to look for; the regex would not.
RISKY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bos\.system\b", "shell command execution"),
    (r"\bsubprocess\.\w+", "subprocess calls"),
    (r"\bshutil\.(rmtree|move)\b", "directory delete/move"),
    (r"\bos\.(remove|unlink|rmdir)\b", "file/directory deletion"),
    (r"\.unlink\(\)", "Path.unlink (file deletion)"),
    (r"\beval\s*\(", "dynamic code evaluation"),
    (r"\bexec\s*\(", "dynamic code execution"),
    (r"\b__import__\s*\(", "dynamic imports"),
    (r"\brequests\.(get|post|put|delete|patch)\b", "outbound HTTP"),
    (r"\burllib\.(request|urlopen)", "outbound HTTP"),
    (r"\bsocket\.(socket|connect)", "raw socket access"),
    (r"\bos\.environ\[", "environment variable access"),
    (r"\bpickle\.(load|loads)\b", "pickle deserialisation (RCE risk)"),
)

#: How many hits the prompt lists before saying "and N more". A dialog with
#: eighty bullet points is one nobody reads.
SHOWN = 10

#: How much of the offending line is quoted.
SNIPPET = 120

_COMPILED = tuple((re.compile(pattern), label)
                  for pattern, label in RISKY_PATTERNS)


@dataclass(frozen=True)
class RiskHit:
    label: str
    line: int
    snippet: str


def scan(code: str) -> List[RiskHit]:
    """Every risky pattern in `code`, at most one per line.

    One per line because a line doing two risky things is still one line to
    look at, and listing it twice makes the prompt look worse than the code is.

    Comment lines are skipped: a commented-out `os.system` is not a thing the
    script does, and flagging it teaches the user that the warning is noise.
    """
    hits: List[RiskHit] = []
    for number, line in enumerate(str(code or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern, label in _COMPILED:
            if pattern.search(line):
                hits.append(RiskHit(label, number, stripped[:SNIPPET]))
                break
    return hits


def message(hits: Sequence[RiskHit]) -> str:
    """The prompt, in the words the user reads.

    The line number and the source line are both there because "this script
    does subprocess calls" is not actionable and "line 42" is not either.
    """
    lines = ["This script contains operations that can affect your system:", ""]
    for hit in list(hits)[:SHOWN]:
        lines.append(f"  • Line {hit.line}: {hit.label}")
        lines.append(f"      {hit.snippet}")
    if len(hits) > SHOWN:
        lines.append(f"  …and {len(hits) - SHOWN} more")
    lines.append("")
    lines.append("Review the script carefully before running.")
    lines.append("")
    lines.append("Run this script anyway?")
    return "\n".join(lines)


def digest(code: str) -> str:
    """The key a trust decision is stored under."""
    return hashlib.sha256(str(code or "").encode("utf-8",
                                                 errors="replace")).hexdigest()


@dataclass
class TrustStore:
    """Which exact scripts the user has approved, this session.

    PER SESSION, ON PURPOSE. Nothing is written to disk: an approval that
    survived a restart would be a permission the user granted once and could
    not see or revoke.
    """
    approved: Set[str] = field(default_factory=set)

    def is_trusted(self, code: str) -> bool:
        return digest(code) in self.approved

    def trust(self, code: str) -> None:
        self.approved.add(digest(code))

    def forget(self) -> None:
        self.approved.clear()


@dataclass
class Verdict:
    """Whether to run, and what to ask if not yet."""
    allowed: bool
    #: Empty when nothing needs asking.
    prompt: str = ""
    hits: List[RiskHit] = field(default_factory=list)

    @property
    def needs_asking(self) -> bool:
        return not self.allowed and bool(self.prompt)


def check(code: str, store: TrustStore) -> Verdict:
    """Whether this code may run without asking.

    Returns a verdict rather than calling a dialog, which is what lets the
    scanner be tested at all — the Tk version reaches for messagebox from
    inside the same method that does the scanning.
    """
    if store.is_trusted(code):
        return Verdict(True)
    hits = scan(code)
    if not hits:
        return Verdict(True)
    return Verdict(False, message(hits), hits)
