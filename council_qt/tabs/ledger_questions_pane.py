"""
council_qt.tabs.ledger_questions_pane — the Ledger's questions, as a chat
in the Ledger tab (user, 2026-10-30: "add a chat function in the ledger
process directly ... the ledger finds it and requests info from me").

Shown only when the Ledger has something to ask, after an Extract, Update
or Re-run: one question at a time, its answers as buttons, or a reply typed
in words (read back to be confirmed before anything is done). Each answer
is kept for the Ledger at once (council_core.ledger_questions). When the
round's questions are answered the tab re-checks the review against them
by itself - up to ROUNDS_MAX rounds - and the new questions, if any, come
in as the next round.

The work is the tab's (it runs the re-check on its worker); this is only
the conversation.
"""
from __future__ import annotations

import html
from typing import Callable, List, Optional

from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from council_core import ledger_questions as lq


class LedgerQuestionsPane(QGroupBox):
    """``recheck()`` is called when a round's questions are all answered or
    skipped and at least one was answered; ``read_reply(q, text, done)``
    works out a typed reply off the GUI thread and calls ``done((key,
    value) | None)`` on it (the tab: the rules, then Council's model)."""

    def __init__(self, recheck: Callable[[], None],
                 read_reply: Callable[[lq.Question, str, Callable], None],
                 parent: Optional[QWidget] = None):
        super().__init__("Ledger questions", parent)
        self._recheck = recheck
        self._read_reply = read_reply
        self.csv = ""
        self.review = ""
        self.queue: List[lq.Question] = []
        self.round = 0
        self.answered = 0                  # this round
        #: questions skipped for this review: not asked again in its rounds
        self.skipped: set = set()
        self._pending: Optional[tuple] = None   # (question, key, value) to confirm
        v = QVBoxLayout(self)
        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setMinimumHeight(140)
        v.addWidget(self.transcript, 1)
        self.choice_row = QHBoxLayout()
        v.addLayout(self.choice_row)
        confirm = QHBoxLayout()
        self.confirm_label = QLabel("")
        self.confirm_label.setWordWrap(True)
        confirm.addWidget(self.confirm_label, 1)
        self.confirm_yes = QPushButton("Yes, that's it")
        self.confirm_yes.clicked.connect(self._on_confirm)
        self.confirm_no = QPushButton("No")
        self.confirm_no.clicked.connect(self._on_unconfirm)
        confirm.addWidget(self.confirm_yes)
        confirm.addWidget(self.confirm_no)
        v.addLayout(confirm)
        row = QHBoxLayout()
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("Or type an answer in your own words…")
        self.reply.returnPressed.connect(self._on_send)
        row.addWidget(self.reply, 1)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        row.addWidget(self.send_btn)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setToolTip("Leave this one for the review: not asked again about this "
                                 "review, but asked again on the next run.")
        self.skip_btn.clicked.connect(self._on_skip)
        row.addWidget(self.skip_btn)
        v.addLayout(row)
        self._show_confirm(False)
        self.setVisible(False)

    # ── what the tab calls ──────────────────────────────────────────────
    def start(self, csv: str, review: str, questions: List[lq.Question]) -> None:
        """A new review (Extract, Update, Re-run): round 1."""
        self.csv, self.review = csv, review
        self.round = 1
        self.skipped = set()
        self.transcript.clear()
        self._set(questions, first=True)

    def next_round(self, rnd: "lq.Round") -> None:
        """The re-check's result: what changed, then the next round's
        questions - or the end."""
        self.round += 1
        self._ledger(f"Re-checked the review with your answers (round {self.round - 1}): "
                     + rnd.outcome.describe() + "."
                     + (f" The filled templates were written again ({rnd.filled})."
                        if rnd.filled else ""))
        for p in rnd.problems:
            self._ledger(p)
        if self.round > lq.ROUNDS_MAX and rnd.questions:
            self._ledger(f"I stop re-checking by myself after {lq.ROUNDS_MAX} rounds. "
                         f"{len(rnd.questions)} question(s) are still open: they are in "
                         "the review, and I'll ask again on the next run.")
            self._set([], first=False)
            return
        self._set([q for q in rnd.questions if q.id not in self.skipped], first=False)

    def recheck_failed(self, err: str) -> None:
        self._ledger(f"The re-check did not finish: {err.splitlines()[0] if err else ''}")
        self._set([], first=False)

    # ── the conversation ────────────────────────────────────────────────
    def _set(self, questions: List[lq.Question], first: bool) -> None:
        self.queue = list(questions)
        self.answered = 0
        if first and not self.queue:
            self.setVisible(False)
            return
        self.setVisible(True)
        if self.queue:
            self._ledger(f"I have {len(self.queue)} question(s) about this review."
                         if first else f"{len(self.queue)} new question(s):")
        elif not first:
            self._ledger("Nothing more to ask. Open the review or the filled template to "
                         "check the rest, then Apply.")
        self._ask()

    @property
    def current(self) -> Optional[lq.Question]:
        return self.queue[0] if self.queue else None

    def _ask(self) -> None:
        self._show_confirm(False)
        self._clear_choices()
        q = self.current
        busy = q is None
        self.reply.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)
        self.skip_btn.setEnabled(not busy)
        if q is None:
            return
        self._ledger(q.text)
        for c in q.choices:
            b = QPushButton(c.label)
            b.clicked.connect(lambda _=False, k=c.key: self._answer(k, ""))
            self.choice_row.addWidget(b)
        self.choice_row.addStretch(1)

    def _clear_choices(self) -> None:
        while self.choice_row.count():
            w = self.choice_row.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def _answer(self, key: str, value: str) -> None:
        q = self.current
        if q is None:
            return
        self._you(lq.said(q, key, value))
        try:
            lq.save_answer(self.csv, q.id, key, value, question=q)
        except Exception as e:                            # noqa: BLE001
            self._ledger(f"Your answer could not be kept beside the Ledger: {e}")
            return
        self.answered += 1
        self._next()

    def _on_skip(self) -> None:
        if self.current is not None:
            self.skipped.add(self.current.id)
            self._you("Skip")
            self._next()

    def _next(self) -> None:
        self.queue.pop(0)
        if self.queue:
            self._ask()
            return
        self._ask()
        if self.answered:
            self._ledger("Thanks - re-checking the review with your answers…")
            self._recheck()

    def _on_send(self) -> None:
        q, text = self.current, self.reply.text().strip()
        if q is None or not text:
            return
        self.reply.clear()
        self._you(text)
        self.reply.setEnabled(False)
        self.send_btn.setEnabled(False)

        def done(got) -> None:
            if self.current is not q:
                return
            self.reply.setEnabled(True)
            self.send_btn.setEnabled(True)
            if got is None:
                self._ledger("I couldn't tell which answer that is - pick one of the "
                             "buttons, or say it another way.")
                return
            key, value = got
            self._pending = (q, key, value)
            self.confirm_label.setText(f"I read that as: {lq.said(q, key, value)}. Right?")
            self._show_confirm(True)
        self._read_reply(q, text, done)

    def _on_confirm(self) -> None:
        if self._pending is None:
            return
        q, key, value = self._pending
        self._pending = None
        self._show_confirm(False)
        if q is self.current:
            self._answer(key, value)

    def _on_unconfirm(self) -> None:
        self._pending = None
        self._show_confirm(False)
        self._ledger("OK - pick one of the buttons, or say it another way.")

    def _show_confirm(self, on: bool) -> None:
        for w in (self.confirm_label, self.confirm_yes, self.confirm_no):
            w.setVisible(on)

    def _ledger(self, text: str) -> None:
        self.transcript.append(f"<p><b>Ledger:</b> {html.escape(text)}</p>")

    def _you(self, text: str) -> None:
        self.transcript.append(f"<p style='margin-left:24px'><b>You:</b> "
                               f"{html.escape(text)}</p>")
