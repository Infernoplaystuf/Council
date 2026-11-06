"""
council_core.token_budget — how long a reply may be, sized from the model
that serves it. One rule for the whole Council (user, 2026-10-23: "auto adjust
the max token counts based on the model being used").

WHAT WAS WRONG
The window already follows the model: the VRAM-aware loader gives Phi-4 16384
on a 16 GB card, and Ollama is sent its own num_ctx. The REPLY caps did not.
About thirty call sites passed a literal — 400 for a cross-fire message, 600
for a rebuttal, 2400 for the writer — and the engine's clamp
(council_engine._clamp_messages_to_ctx) only ever cut a cap down to fit a
small window, never let it grow on a big one. A 32k model was stopped at the
same length as a 4k one.

HOW IT WORKS
A caller asks for a KIND of reply with today's number as its floor:

    model.respond(prompt, max_tokens=token_budget.reply("short", 600))

`reply()` returns a `Reply`, an int whose value IS the floor, so any code that
does not know about kinds (an int() on the way, a bench, a remote node) sees
exactly the number it saw before. The clamp, which knows the real window of
the instance serving the call, resolves it with `fit()`:

    target = max(floor, min(ceiling, window * share))
    reply  = max(floor, min(target, what the prompt leaves))

So the growth only takes room the prompt does not need: a reply never costs
the prompt a token it had before, and is never shorter than the old number.
Where the share is under the floor (a short reply on a 4096 window) the call
is exactly what it was.

    kind     share of the window   ceiling   used for
    short    1/20                  1024      rebuttals, cross-fire, challenges
    answer   1/10                  3072      a normal answer, code fixes
    long     1/6                   6144      writer, sage, judge, whole files

A FILL reply (num_predict 0, the Ledger's) is not a kind here: it already
takes all the window the prompt leaves, in the clamp.

COUNCIL_REPLY_AUTO=0 turns the growth off (every Reply is its floor), for
anyone who wants the old fixed lengths back.
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

#: kind -> (share of the window, ceiling in tokens).
KINDS: Dict[str, Tuple[float, int]] = {
    "short": (1 / 20, 1024),
    "answer": (1 / 10, 3072),
    "long": (1 / 6, 6144),
}


class Reply(int):
    """A reply length that grows with the serving model's window.

    Its int value is ``floor + extra``: what any code that does not know
    about kinds gets, which is the fixed number the call used before.
    ``extra`` is headroom added on top whatever the window (an Ollama model's
    thinking tokens), kept out of the scaling."""

    kind: str
    floor: int
    extra: int

    def __new__(cls, kind: str, floor: int, extra: int = 0) -> "Reply":
        if kind not in KINDS:
            raise ValueError(f"unknown reply kind {kind!r}; "
                             f"one of {', '.join(KINDS)}")
        floor, extra = max(1, int(floor)), max(0, int(extra))
        self = super().__new__(cls, floor + extra)
        self.kind, self.floor, self.extra = kind, floor, extra
        return self

    def __repr__(self) -> str:
        more = f", extra={self.extra}" if self.extra else ""
        return f"Reply({self.kind!r}, {self.floor}{more})"

    def __reduce__(self):
        return (Reply, (self.kind, self.floor, self.extra))


def reply(kind: str, floor: int) -> Reply:
    """A ``kind`` reply that is never shorter than ``floor`` tokens."""
    return Reply(kind, floor)


def plus(num_predict, extra: int):
    """``num_predict`` with ``extra`` tokens of headroom on top. A Reply
    stays a Reply (the headroom is not scaled); a plain number is added to."""
    if isinstance(num_predict, Reply):
        return Reply(num_predict.kind, num_predict.floor,
                     num_predict.extra + int(extra))
    return int(num_predict) + int(extra)


def auto_enabled() -> bool:
    """False when COUNCIL_REPLY_AUTO is 0 / off / false / no."""
    raw = os.environ.get("COUNCIL_REPLY_AUTO", "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def target(num_predict, window: int) -> int:
    """What ``num_predict`` asks for on a ``window``-token model, before the
    prompt is counted. A plain number is itself; a Reply is its kind's share
    of the window, between its floor and its ceiling, plus its extra."""
    if not isinstance(num_predict, Reply):
        return int(num_predict)
    if not auto_enabled():
        return int(num_predict)
    share, ceiling = KINDS[num_predict.kind]
    grown = min(ceiling, int(max(0, int(window or 0)) * share))
    return max(num_predict.floor, grown) + num_predict.extra


def fit(num_predict, window: int, prompt_tokens: int, margin: int) -> int:
    """The reply ``num_predict`` gets on a ``window``-token model whose prompt
    is ``prompt_tokens`` long, with ``margin`` tokens for the chat template.

    A plain number is returned unchanged (the clamp's own rules then apply).
    A Reply grows toward target() only into room the prompt leaves, and is
    never less than its floor: the clamp then fits prompt and reply exactly
    as it did for that floor."""
    if not isinstance(num_predict, Reply):
        return int(num_predict)
    want = target(num_predict, window)
    left = int(window or 0) - int(margin) - int(prompt_tokens)
    return max(int(num_predict), min(want, left))

