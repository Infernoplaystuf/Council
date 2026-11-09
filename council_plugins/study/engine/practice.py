"""
Practising with the study documents: flash cards in Leitner boxes, and the
quiz taken and scored.

FLASH CARDS (``Deck``)
Every card starts in box 1. Right moves it up a box, wrong puts it back in
box 1; a card in box b is due again BOX_DAYS[b] days after it was last
answered (box 1: the same day). A session takes the due cards, lowest box
first, and a card missed in the session comes round again at its end, until
it is answered right once ("the ones I missed, again"). Cards are known by
their front, so making the study documents again keeps the boxes of cards
that are still there.

THE QUIZ (``QuizRun``)
Multiple choice is marked by the choice picked. A short answer is shown
beside the right one and the reader marks it (``suggest`` gives a first
guess from the words they share); a missed question can be taken again in
a round of its own. Each finished quiz's score is kept.

What is kept per notebook (<notebook>.practice.json) is plain JSON
(``Deck.to_dict``).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import common as cm

DAY = 86400.0
#: Days until a card in box b is due again (box 1: the same session).
BOX_DAYS = {1: 0, 2: 1, 3: 3, 4: 7, 5: 16}
TOP_BOX = 5


def card_key(card: Dict[str, Any]) -> str:
    return " ".join(str(card.get("front", "")).lower().split())


class Deck:
    """The boxes of one notebook's cards, and its quiz scores."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.boxes: Dict[str, Dict[str, Any]] = dict(data.get("cards") or {})
        self.quizzes: List[Dict[str, Any]] = list(data.get("quizzes") or [])

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "cards": self.boxes, "quizzes": self.quizzes}

    def state(self, card: Dict[str, Any]) -> Dict[str, Any]:
        return self.boxes.get(card_key(card)) or {"box": 1, "due": 0.0,
                                                  "right": 0, "wrong": 0}

    def due(self, cards: Sequence[Dict[str, Any]], now: Optional[float] = None
            ) -> List[Dict[str, Any]]:
        """The cards due now, lowest box first (new cards are due)."""
        now = time.time() if now is None else now
        out = [c for c in cards if self.state(c)["due"] <= now]
        return sorted(out, key=lambda c: (self.state(c)["box"], self.state(c)["due"]))

    def answer(self, card: Dict[str, Any], right: bool,
               now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        st = dict(self.state(card))
        if right:
            st["box"] = min(TOP_BOX, int(st["box"]) + 1)
            st["right"] = int(st.get("right", 0)) + 1
        else:
            st["box"] = 1
            st["wrong"] = int(st.get("wrong", 0)) + 1
        st["due"] = now + BOX_DAYS[st["box"]] * DAY
        self.boxes[card_key(card)] = st
        return st

    def box_counts(self, cards: Sequence[Dict[str, Any]]) -> Dict[int, int]:
        out = {b: 0 for b in BOX_DAYS}
        for c in cards:
            out[int(self.state(c)["box"])] += 1
        return out

    def add_quiz(self, right: int, total: int, now: Optional[float] = None) -> None:
        self.quizzes.append({"when": time.time() if now is None else now,
                             "right": int(right), "total": int(total)})


@dataclass
class Session:
    """One sitting with the flash cards: the due cards, then the missed
    ones again until each is answered right."""
    queue: List[Dict[str, Any]]
    done: int = 0
    right: int = 0
    missed: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def start(cls, deck: Deck, cards: Sequence[Dict[str, Any]], *,
              everything: bool = False, now: Optional[float] = None) -> "Session":
        return cls(list(cards) if everything else deck.due(cards, now))

    def current(self) -> Optional[Dict[str, Any]]:
        return self.queue[0] if self.queue else None

    def answer(self, deck: Deck, right: bool, now: Optional[float] = None) -> None:
        card = self.queue.pop(0)
        deck.answer(card, right, now)
        self.done += 1
        if right:
            self.right += 1
            self.missed = [m for m in self.missed if card_key(m) != card_key(card)]
        else:
            if all(card_key(m) != card_key(card) for m in self.missed):
                self.missed.append(card)
            self.queue.append(card)            # again, at the end

    @property
    def left(self) -> int:
        return len(self.queue)


def suggest(given: str, answer: str) -> bool:
    """A first guess at a short answer: most of the right answer's words
    are in what was typed."""
    if not cm.content_words(given):
        return False
    return cm.support(answer, [given]) >= 0.6


@dataclass
class QuizRun:
    questions: List[Dict[str, Any]]
    at: int = 0
    marks: List[Optional[bool]] = field(default_factory=list)
    given: List[str] = field(default_factory=list)

    @classmethod
    def start(cls, questions: Sequence[Dict[str, Any]], *, shuffle: bool = False,
              seed: Optional[int] = None) -> "QuizRun":
        qs = [dict(q) for q in questions]
        if shuffle:
            rnd = random.Random(seed)
            rnd.shuffle(qs)
            for q in qs:
                if q.get("choices"):
                    q["choices"] = list(q["choices"])
                    rnd.shuffle(q["choices"])
        return cls(qs, marks=[None] * len(qs), given=[""] * len(qs))

    def current(self) -> Optional[Dict[str, Any]]:
        return self.questions[self.at] if self.at < len(self.questions) else None

    def submit(self, given: str, mark: Optional[bool] = None) -> bool:
        """Answer the current question. A choice is marked here; a short
        answer takes ``mark`` (the reader's), else ``suggest``."""
        q = self.current()
        if q is None:
            raise IndexError("the quiz is over")
        if q.get("choices"):
            right = given.strip().lower() == q["answer"].strip().lower()
        else:
            right = bool(mark) if mark is not None else suggest(given, q["answer"])
        self.marks[self.at] = right
        self.given[self.at] = given
        return right

    def remark(self, right: bool) -> None:
        """The reader overrides the mark of the question just answered."""
        self.marks[self.at] = bool(right)

    def next(self) -> Optional[Dict[str, Any]]:
        self.at += 1
        return self.current()

    @property
    def finished(self) -> bool:
        return self.at >= len(self.questions)

    def score(self) -> Dict[str, int]:
        return {"right": sum(1 for m in self.marks if m),
                "total": len(self.questions)}

    def missed(self) -> List[Dict[str, Any]]:
        return [q for q, m in zip(self.questions, self.marks) if not m]
