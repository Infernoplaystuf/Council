"""
council_qt.tabs.speech — dictation in, answers read out.

Small in widgets and not small in care: the Tk version's Speak worker reads the
rate SPINBOX from a background thread, which is the same defect class as the
thirteen Tk-variable reads in the Council tab's `_send` and survives only
because Tkinter marshals `Variable.get()` internally. Qt does not.

So the rate is read here, on the GUI thread, before the worker starts — and
`council_core.speech.TtsPlayer.speak` takes a NUMBER, with no way to reach a
control even by accident.

The player also serialises playback. Pressing Speak twice in the Tk build
starts two threads against one pyttsx3 engine; here the second press is
refused with a reason.
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget)

from council_core import speech as speech_core

from .. import theme
from ..view import ViewHelpers


class SpeechActions:
    """What the Speech tab can ask the application to do."""

    def __init__(self, player=None, last_answer=None):
        self.player = player or speech_core.TtsPlayer()
        self._last_answer = last_answer

    def last_answer(self) -> str:
        """The text Speak Last Answer reads.

        Supplied by whoever owns the transcript; a tab that reached into
        another tab's widget for it would be the coupling this port is
        removing.
        """
        if callable(self._last_answer):
            return self._last_answer() or ""
        return self._last_answer or ""

    def speak(self, text: str, rate: int):
        return self.player.speak(text, rate=rate)

    def stop(self):
        return self.player.stop()


class SpeechTab(ViewHelpers, QWidget):
    """Record, transcribe, and read answers aloud."""

    def __init__(self, window=None, actions: Optional[SpeechActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or SpeechActions()
        self._tokens = theme.tokens("dark")
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        row = QHBoxLayout()
        self._button(row, "Record 5s", self.on_record)
        self._button(row, "Transcribe", self.on_transcribe)
        self._button(row, "Send to Council", self.on_send_to_council)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        row.addWidget(divider)

        self.speak_btn = self._button(row, "🔊 Speak Last Answer", self.on_speak)
        self._button(row, "⏹ Stop", self.on_stop)
        self.auto = QCheckBox("Auto-speak answers")
        row.addWidget(self.auto)

        rate_label = QLabel("Rate:")
        rate_label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(rate_label)
        self.rate = QSpinBox()
        self.rate.setRange(speech_core.MIN_RATE, speech_core.MAX_RATE)
        self.rate.setSingleStep(10)
        self.rate.setValue(speech_core.DEFAULT_RATE)
        row.addWidget(self.rate)
        row.addStretch(1)
        outer.addLayout(row)

        outer.addWidget(QLabel("Transcription"))
        self.transcription = QPlainTextEdit()
        outer.addWidget(self.transcription, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

        if not self.actions.player.available:
            self.speak_btn.setEnabled(False)
            self.speak_btn.setToolTip(
                "Install pyttsx3 to read answers aloud (pip install pyttsx3).")

    # ------------------------------------------------------------------
    def on_speak(self) -> None:
        text = self.actions.last_answer().strip()
        if not text:
            self.status.setText("There is nothing to speak yet.")
            return

        # ON THE GUI THREAD, before the worker exists. The Tk worker reads the
        # spinbox itself, from the background thread.
        rate = self.rate.value()
        self.status.setText("Speaking…")

        def work() -> None:
            result = self.actions.speak(text, rate)
            self._to_ui(lambda: self.status.setText(result.message))

        threading.Thread(target=work, name="tts-speak", daemon=True).start()

    def on_stop(self) -> None:
        self.status.setText(self.actions.stop().message)

    def speak_if_auto(self, text: str) -> None:
        """Read an answer aloud when auto-speak is on.

        Called by whoever finishes a turn. The checkbox is read HERE, on the
        GUI thread, for the same reason the rate is.
        """
        if self.auto.isChecked():
            self.actions._last_answer = text
            self.on_speak()

    # -- dictation, not yet extracted -----------------------------------
    def _not_yet(self, what: str) -> None:
        self.status.setText(
            f"{what} is not ported yet — the speech-to-text path is the "
            "remaining half of this tab.")

    def on_record(self) -> None:
        self._not_yet("Recording")

    def on_transcribe(self) -> None:
        self._not_yet("Transcription")

    def on_send_to_council(self) -> None:
        self._not_yet("Sending to the Council")


def build_speech(window) -> QWidget:
    """Factory for the tab registry."""
    return SpeechTab(window)
