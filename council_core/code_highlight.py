"""
council_core.code_highlight — what colour each piece of a source file is.

The Build tab's editor colours code with Pygments. This module is the part
that knows nothing about Qt: given a file's text and its name, it returns
spans (start, length, kind) over the WHOLE text, and the editor's highlighter
paints the spans that fall in each line.

WHY THE WHOLE TEXT AND NOT LINE BY LINE
Qt highlights one block (line) at a time. Lexing a line on its own loses
every construct that spans lines - a triple-quoted string, a docstring, a
/* comment */ - and the rest of the file is painted as code. Lexing the
whole text once (it takes a few milliseconds for a few thousand lines) and
handing each line the spans that overlap it keeps them right.

OFFSETS ARE THE TEXT'S OWN
`get_tokens_unprocessed` lexes the string as given. `get_tokens` strips
leading newlines, expands tabs and adds a final newline, so its offsets drift
from the document's after the first such change.

KINDS, NOT COLOURS
A span says "keyword" or "string"; the theme picks the colour (the editor's
FORMATS), so a light theme later needs no change here.

Files over MAX_CHARS are not coloured: an editor that freezes on a 20 MB log
is worse than one that shows it plain.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional, Sequence, Tuple

#: Above this many characters a file is shown plain.
MAX_CHARS = 500_000

Span = Tuple[int, int, str]

#: Every kind a span can have. The editor has a format for each.
KINDS = ("keyword", "constant", "string", "comment", "number", "definition",
         "builtin", "decorator", "operator", "heading", "tag", "attribute")


def _kind(token) -> Optional[str]:
    """The kind of one Pygments token type, or None (plain text)."""
    from pygments import token as t
    if token in t.Comment:
        return "comment"
    if token in t.Literal.String:
        return "string"
    if token in t.Literal.Number:
        return "number"
    if token in t.Keyword.Constant:
        return "constant"
    if token in t.Keyword:
        return "keyword"
    if token in t.Name.Decorator:
        return "decorator"
    if token in t.Name.Function or token in t.Name.Class:
        return "definition"
    if token in t.Name.Builtin or token in t.Name.Exception:
        return "builtin"
    if token in t.Name.Tag:
        return "tag"
    if token in t.Name.Attribute:
        return "attribute"
    if token in t.Generic.Heading or token in t.Generic.Subheading:
        return "heading"
    if token in t.Operator.Word:
        return "keyword"
    if token in t.Operator:
        return "operator"
    return None


def lexer_for(filename: str):
    """The Pygments lexer for a file name, or None when Pygments is missing
    or does not know the type (the file is then shown plain)."""
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        return get_lexer_for_filename(filename or "", stripnl=False,
                                      ensurenl=False)
    except ClassNotFound:
        return None
    except Exception:                                     # noqa: BLE001
        return None


def language(filename: str) -> str:
    """The language's name for the status line ('Python'), or 'Plain text'."""
    lexer = lexer_for(filename)
    return lexer.name if lexer is not None else "Plain text"


def spans(text: str, filename: str, lexer=None) -> List[Span]:
    """(start, length, kind) for every coloured piece of ``text``, in order
    and not overlapping. Plain text, an unknown type, a lexer error or a text
    over MAX_CHARS give []."""
    if not text or len(text) > MAX_CHARS:
        return []
    lexer = lexer if lexer is not None else lexer_for(filename)
    if lexer is None:
        return []
    out: List[Span] = []
    try:
        for start, token, value in lexer.get_tokens_unprocessed(text):
            kind = _kind(token)
            if kind is None or not value:
                continue
            if out and out[-1][2] == kind and out[-1][0] + out[-1][1] == start:
                prev = out.pop()                          # one span, not many
                out.append((prev[0], prev[1] + len(value), kind))
            else:
                out.append((start, len(value), kind))
    except Exception:                                     # noqa: BLE001
        return []
    return out


def in_range(all_spans: Sequence[Span], ends: Sequence[int], start: int,
             length: int) -> List[Span]:
    """The spans that overlap [start, start + length), clipped to it and made
    relative to ``start`` - one line's share. ``ends`` is each span's end
    (start + length), in order, so the first overlapping span is found by
    bisection instead of a walk from the top of the file on every line."""
    stop = start + length
    out: List[Span] = []
    i = bisect_right(ends, start)
    while i < len(all_spans):
        s, n, kind = all_spans[i]
        if s >= stop:
            break
        a, b = max(s, start), min(s + n, stop)
        if b > a:
            out.append((a - start, b - a, kind))
        i += 1
    return out
