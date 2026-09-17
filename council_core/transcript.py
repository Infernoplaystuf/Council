"""
council_core.transcript — what the transcript SAYS, separated from what shows it.

THE TRANSCRIPT IS THE HARDEST THING IN THE PORT, AND THIS IS WHY
It is not one widget. It is two (the Council tab's and the Dream3D tab's
mirror), written to from several threads, carrying five kinds of content that
each look different. `_append_transcript` in the Tk shell does six unrelated
things in thirty lines: session logging, last-turn tracking, provenance
capture, formatting, widget writing, and long-term storage.

Only two of those six are actually about widgets. The other four are policy —
*which* kinds get logged, *which* speaker counts as an answer, *what* prefix a
line gets — and policy that lives inside a widget-writing method is policy that
has to be re-derived, correctly, by whoever writes the second front end. That
is how two front ends start disagreeing about whether a "thought" is part of
the conversation.

ONE PIECE OF GOOD NEWS, CHECKED RATHER THAN ASSUMED
Nothing reads the transcript's contents back. Every `.get("1.0", "end")` in the
engine is on an INPUT box; the eleven references to `self.transcript` are
construction, tag setup, and one `tag_add`. The widget is write-only, so the
port does not have to make a Qt view answer questions about what it contains —
which is the thing that usually makes a rich-text port expensive.

So this module owns the policy and the formatting, and returns SEGMENTS: a list
of (text, tag) pairs. Tk turns a segment into `insert(END, text, tag)`; Qt turns
it into a QTextCharFormat and an insertText. Neither decides what the segments
are, and a test can read them without a display.

TAGS ARE DESCRIBED, NOT DRAWN
TAGS below says a tag is "#a5d6a7, bold, monospace 10". It does not say how to
make that happen, because `tag_configure(foreground=...)` and
`QTextCharFormat.setForeground(QColor(...))` have nothing in common except the
colour. Both front ends read the same description and build their own.

This also fixes the Dream3D mirror, which currently copies the Council
transcript's tags by reading back only the foreground and then forcing
`("Consolas", 10, "bold")` on every one of them — so in that tab a phase marker
renders bold instead of italic-9, and a streamed token renders bold instead of
plain. Two widgets configured from one description cannot drift like that.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ============================================================
# The palette
# ============================================================
# Moved verbatim from council_gui_engine so both front ends colour a speaker
# the same way. These are the council's personalities; the colour is how a user
# tells them apart at a glance in a long scroll, so the values are part of the
# product, not a theme detail.

ROLE_COLORS: Dict[str, str] = {
    "User":         "#4fc3f7",   # light blue
    "Judge":        "#ef9a9a",   # red-ish
    "Writer":       "#a5d6a7",   # green
    "Coder":        "#ce93d8",   # purple
    "Intern":       "#ffe082",   # yellow
    "Peasant":      "#ffcc80",   # orange
    "Artist":       "#f48fb1",   # pink
    "Orchestrator": "#b0bec5",   # grey
    "Librarian":    "#80cbc4",   # teal
    "Apothecary":   "#bcaaa4",   # brown-ish
}

PHASE_COLOR = "#78909c"
TOKEN_COLOR = "#e0e0e0"
DEFAULT_COLOR = "#cfd8dc"
ERROR_COLOR = "#f38ba8"

MONO_FAMILY = "Consolas"
BODY_SIZE = 10
PHASE_SIZE = 9


@dataclass(frozen=True)
class TagStyle:
    """How one tag looks, described rather than applied."""
    name: str
    foreground: str
    bold: bool = False
    italic: bool = False
    family: str = MONO_FAMILY
    size: int = BODY_SIZE


def _tag_name(role: str) -> str:
    return "who_" + role.lower().replace("-", "_").replace(" ", "_")


def _build_tags() -> Dict[str, TagStyle]:
    tags = {
        "phase": TagStyle("phase", PHASE_COLOR, italic=True, size=PHASE_SIZE),
        "token": TagStyle("token", TOKEN_COLOR),
        "error": TagStyle("error", ERROR_COLOR),
        "who_default": TagStyle("who_default", DEFAULT_COLOR, bold=True),
    }
    for role, colour in ROLE_COLORS.items():
        name = _tag_name(role)
        tags[name] = TagStyle(name, colour, bold=True)
    return tags


#: tag name -> how it looks. Both front ends configure from this.
TAGS: Dict[str, TagStyle] = _build_tags()


def role_tag(who: str) -> str:
    """The tag for a speaker, or ``who_default``.

    MATCHES CASE-INSENSITIVELY, WHICH THE TK VERSION DID NOT. Its test was::

        if tag in ROLE_COLORS or who in ROLE_COLORS:

    and the first half is dead: `tag` is "who_writer" while the keys are
    "Writer", so it can never be true. That left only an exact, case-sensitive
    match — so a speaker arriving as "writer" (which is exactly how the names
    in council_modules.MODEL_ROLES are spelled) fell through to the grey
    default and lost its colour, in a widget whose whole job is telling
    speakers apart.

    Changed rather than preserved because the blast radius is one colour: this
    can only turn the default grey into the role's own colour, never one role's
    colour into another's, and never touches anything on disk.
    """
    if not who:
        return "who_default"
    name = _tag_name(who)
    return name if name in TAGS else "who_default"


# ============================================================
# What a line looks like
# ============================================================

@dataclass(frozen=True)
class Segment:
    """One run of text and the tag it carries. ``tag=None`` is the body."""
    text: str
    tag: Optional[str] = None


#: The kinds `_append_transcript` accepts. Anything else is treated as "final",
#: which is the Tk behaviour and the safe one — an unknown kind shows up in
#: full rather than vanishing.
KINDS = ("final", "phase", "token", "observation", "thought", "error")


def render(who: str, text: str, kind: str = "final") -> List[Segment]:
    """The segments one transcript entry becomes.

    Three shapes, exactly as the Tk shell writes them:

      phase   two leading spaces, the text, a newline — all in the phase tag
      token   the raw token, no newline, no header (this is a live stream)
      else    a blank line, "Who:", a newline in the speaker's colour, then the
              body stripped and newline-terminated, untagged

    The body is deliberately untagged. Colouring a whole answer in the
    speaker's colour looks like a chat app and reads badly over hundreds of
    lines; the colour is on the name, which is what you scan for.
    """
    if kind == "phase":
        return [Segment(f"  {text}\n", "phase")]
    if kind == "token":
        return [Segment(text, "token")]
    if kind == "error":
        # The Tk shell gets here by writing a normal entry and then painting
        # over it: `_append_transcript("ERROR", msg, "final")` followed by
        # `transcript.tag_add("error", "end-2l", "end")`. That works only
        # because the entry happens to be exactly two lines, and it needs the
        # widget to support tagging a range after the fact — which is the one
        # Tk Text idiom with no cheap QTextEdit equivalent. Saying "this entry
        # is an error" up front produces the same red text, survives an entry
        # that is not two lines, and asks nothing of the widget.
        return [
            Segment(f"\n{who}:\n", "error"),
            Segment(text.strip() + "\n", "error"),
        ]
    return [
        Segment(f"\n{who}:\n", role_tag(who)),
        Segment(text.strip() + "\n", None),
    ]


def stream_segments(who: str, token: str,
                    speakers_seen: Iterable[str]) -> List[Segment]:
    """The segments one streamed token becomes in the live preview box.

    A speaker gets a header the first time they say anything and never again,
    which is why the caller passes what it has already seen. The Tk version
    keeps that set in ``_stream_buffers`` — a dict used only for membership,
    after the per-token `+=` that built its values turned out to be an O(n²)
    string realloc on the hottest path in the app. The comment recording that
    is worth keeping: it is the kind of thing that gets "tidied" back in.
    """
    if who in set(speakers_seen):
        return [Segment(token, None)]
    return [Segment(f"\n{who}: ", role_tag(who)), Segment(token, None)]


# ============================================================
# Policy: who hears about an entry
# ============================================================
# Four separate decisions that `_append_transcript` made inline. They are here
# as named predicates so the Qt front end cannot make a different call by
# accident, and so each one can be read without reading the widget code around
# it.

#: Kinds that are part of the conversation rather than progress noise. These
#: reach the librarian and the long-term store; tokens and phases do not,
#: because a stream of 2,000 tokens is the same answer written 2,000 times.
CONVERSATIONAL_KINDS = frozenset({"final", "observation"})

def logs_to_session(kind: str) -> bool:
    """Whether the per-session debug log records this entry.

    Everything except the token stream: the log exists to reconstruct what
    happened, and the tokens are already in the final text they build up to.
    """
    return kind != "token"


def stores_in_history(kind: str) -> bool:
    """Whether this entry joins the durable conversation record.

    The Tk test is ``kind not in ("token", "phase", "thought")``, which is the
    same set as CONVERSATIONAL_KINDS plus any unknown kind. Written that way
    round deliberately: an entry of some kind nobody anticipated is more useful
    kept than dropped, and dropping it is silent.
    """
    return kind not in ("token", "phase", "thought")


def records_provenance(who: str, kind: str) -> bool:
    """Whether "where did this come from?" should be able to find this later.

    Model output only. The user's own message is not provenance for an answer,
    and neither is a phase marker.
    """
    return kind in ("final", "observation") and who != "User"


def is_user_question(who: str, kind: str) -> bool:
    """Whether this entry is the turn "⤓ Defer to Vault" should capture."""
    return who == "User"


def is_final_answer(who: str, kind: str) -> bool:
    return who == "Writer" and kind == "final"


# ============================================================
# Reading it back
# ============================================================

def plain_text(entries: Sequence[Tuple[str, str, str]]) -> str:
    """The transcript as text, from the entries rather than from a widget.

    Nothing needs this today — the Tk widget is write-only, which is checked
    above. It is here because "export this conversation" and "copy the
    transcript" are the obvious next asks, and the cheap way to answer them is
    the way that does not make either view the system of record. Reaching into
    a widget for the answer is what would make the NEXT port expensive.
    """
    out: List[str] = []
    for who, text, kind in entries:
        for segment in render(who, text, kind):
            out.append(segment.text)
    return "".join(out)
