"""
council_qt.widgets.transcript — the Qt transcript, and the streaming box.

WHAT THIS IS AND IS NOT
It is a view. It decides nothing about what a line says, which speaker gets
which colour, or whether an entry is part of the conversation — all of that is
council_core.transcript, and both front ends read it from there. This file
turns a list of Segments into characters on a screen, and does the two things
Qt needs done carefully.

THING ONE: APPENDING WITHOUT REDRAWING THE WORLD
The obvious translation of `text.insert(END, s, tag)` is
`edit.setHtml(edit.toHtml() + more)`, and it is quadratic — at a few hundred
entries the app visibly stalls. The right move is a QTextCursor parked at the
end with an explicit QTextCharFormat, which appends in constant time and never
re-lays-out what is already there.

THING TWO: THE TOKEN STREAM
Tokens arrive at 100+/s. The Tk shell learned this the expensive way and left
the lesson in a comment: it does the cheap insert per token but defers the
scroll to once per queue drain, after a per-token `+=` on a buffer turned out
to be an O(n²) string realloc on the hottest path in the app. Qt has exactly
the same problem and the same fix, so the same split is kept here —
``append_token`` never scrolls, ``flush`` does.

Scrolling is also only done when the view is ALREADY at the bottom. A user who
has scrolled up to read something is reading it; yanking them back to the end
every time a token arrives makes a streaming answer impossible to read, and it
is the kind of thing that is obvious in use and invisible in a test.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from council_core import transcript as core

#: How close to the bottom still counts as "at the bottom", in pixels. A
#: strict equality test fails on fractional scrollbar positions.
BOTTOM_SLACK = 4


def _format_for(style: core.TagStyle) -> QTextCharFormat:
    """One core TagStyle, as the thing Qt actually applies."""
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(style.foreground))
    font = QFont(style.family, style.size)
    font.setBold(style.bold)
    font.setItalic(style.italic)
    fmt.setFont(font)
    return fmt


def build_formats() -> dict:
    """Every tag core.TAGS describes, as a QTextCharFormat.

    Built once per widget rather than per insert: constructing a QFont on the
    token path would put font resolution in the 100/s hot loop.
    """
    return {name: _format_for(style) for name, style in core.TAGS.items()}


class TranscriptView(QTextEdit):
    """The conversation, appended to and never read back.

    QTextEdit rather than QPlainTextEdit because the transcript is genuinely
    rich — per-run colour, bold names, italic phase markers — and
    QPlainTextEdit's appendHtml would mean building and re-parsing HTML for
    every entry. The Vault tab's activity log uses appendHtml and should keep
    doing so: it writes a line at a time on a user action, not 100 runs a
    second.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)          # nothing here is ever undone
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self._formats = build_formats()
        self._body = QTextCharFormat()
        self._body.setFont(QFont(core.MONO_FAMILY, core.BODY_SIZE))

    # -- the one way in ------------------------------------------------
    def write(self, segments: Sequence[core.Segment]) -> None:
        """Append segments. The only method that puts text in this widget."""
        stick = self.at_bottom()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        for segment in segments:
            cursor.insertText(segment.text, self._format(segment.tag))
        if stick:
            self.scroll_to_end()

    def append_entry(self, who: str, text: str, kind: str = "final") -> None:
        """One transcript entry, formatted by the shared policy."""
        self.write(core.render(who, text, kind))

    def _format(self, tag: Optional[str]) -> QTextCharFormat:
        if tag is None:
            return self._body
        return self._formats.get(tag, self._body)

    # -- scrolling -----------------------------------------------------
    def at_bottom(self) -> bool:
        """Whether the view is scrolled to the end.

        If it is not, the user is reading something further up and must not be
        dragged away from it.
        """
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - BOTTOM_SLACK

    def scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_all(self) -> None:
        self.clear()


class StreamView(TranscriptView):
    """The live token preview.

    Same widget, different write discipline: tokens go in without scrolling and
    ``flush`` is called once per queue drain. Keeps its own set of speakers
    seen, so a speaker's name is printed once and not per token.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._speakers: set = set()
        self._dirty = False
        self._stick = True

    def append_token(self, who: str, token: str) -> None:
        """Add one token. Deliberately does NOT scroll — see the module note."""
        if not self._dirty:
            # First token of this drain: ask now whether we were at the bottom.
            # Asking in flush() would always answer "no", because by then the
            # tokens just inserted have moved the scrollbar's maximum past us.
            self._stick = self.at_bottom()
        segments = core.stream_segments(who, token, self._speakers)
        self._speakers.add(who)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        for segment in segments:
            cursor.insertText(segment.text, self._format(segment.tag))
        self._dirty = True

    def flush(self) -> None:
        """Scroll once, at the end of a drain, and only if a token arrived."""
        if not self._dirty:
            return
        self._dirty = False
        if self._stick:
            self.scroll_to_end()

    def forget_speaker(self, who: str) -> None:
        """Drop a speaker so their next token prints a fresh header."""
        self._speakers.discard(who)

    def clear_all(self) -> None:
        self._speakers.clear()
        self._dirty = False
        self._stick = True
        super().clear_all()


class MirroredTranscript:
    """The Council transcript and the Dream3D one, written together.

    The Tk shell mirrors by looping over two widget attributes and skipping
    whichever is None, so a tab that does not exist yet costs nothing. Same
    here — views are added as their tabs are built, and an entry written before
    the Dream3D tab exists simply does not reach it, exactly as today.

    (That is a real behavioural quirk, not an oversight to fix: the mirror
    starts from whenever the tab was first opened, not from the start of the
    session. Worth knowing before someone "fixes" it into a replay.)
    """

    def __init__(self, views: Optional[Iterable[TranscriptView]] = None):
        self._views: List[TranscriptView] = list(views or [])

    def add(self, view: TranscriptView) -> None:
        if view not in self._views:
            self._views.append(view)

    def remove(self, view: TranscriptView) -> None:
        if view in self._views:
            self._views.remove(view)

    def append_entry(self, who: str, text: str, kind: str = "final") -> None:
        segments = core.render(who, text, kind)      # rendered once, not per view
        for view in list(self._views):
            try:
                view.write(segments)
            except RuntimeError:
                # The C++ side is gone (tab closed). The Tk shell catches
                # TclError here for the same reason.
                self._views.remove(view)

    def clear_all(self) -> None:
        for view in list(self._views):
            try:
                view.clear_all()
            except RuntimeError:
                self._views.remove(view)

    def __len__(self) -> int:
        return len(self._views)
