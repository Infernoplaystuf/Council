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

import tempfile
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QSpinBox, QVBoxLayout, QWidget)

from council_core import speech as speech_core
from council_core import transcribe

from .. import dialogs, theme
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
    """Record, transcribe a video or audio file, and read answers aloud."""

    def __init__(self, window=None, actions: Optional[SpeechActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or SpeechActions()
        self._tokens = theme.tokens("dark")
        self._job = None
        self._downloading = False
        self._model_cached = False
        self.last_transcript = None
        self.last_saved = None
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

        self._build_file_box(outer)

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

    # -- transcribing a file ------------------------------------------
    def _build_file_box(self, outer) -> None:
        box = QGroupBox("Transcribe a video or audio file")
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        self.file_btn = self._button(row, "🎬 Transcribe a file…",
                                     self.on_transcribe_file)
        self.cancel_btn = self._button(row, "Cancel", self.on_cancel)
        self.cancel_btn.setEnabled(False)
        self.save_as_btn = self._button(row, "Save as…", self.on_save_as)
        self.save_as_btn.setEnabled(False)
        row.addStretch(1)
        lay.addLayout(row)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Model:"))
        self.model_box = QComboBox()
        for name, label in transcribe.MODEL_CHOICES:
            self.model_box.addItem(label, name)
        self.model_box.currentIndexChanged.connect(self._refresh_model_note)
        opts.addWidget(self.model_box)
        self.download_btn = self._button(opts, "Download model",
                                         self.on_download_model)
        opts.addWidget(QLabel("Run on:"))
        self.device_box = QComboBox()
        for key, label in transcribe.DEVICE_CHOICES:
            self.device_box.addItem(label, key)
        opts.addWidget(self.device_box)
        opts.addWidget(QLabel("Language:"))
        self.language_box = QComboBox()
        for key, label in transcribe.LANGUAGE_CHOICES:
            self.language_box.addItem(label, key)
        opts.addWidget(self.language_box)
        opts.addWidget(QLabel("Save as:"))
        self.format_box = QComboBox()
        self.format_box.addItem("Markdown (.md)", "md")
        self.format_box.addItem("Text (.txt)", "txt")
        opts.addWidget(self.format_box)
        self.timestamps = QCheckBox("Timestamps")
        self.timestamps.setChecked(True)
        opts.addWidget(self.timestamps)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        self.model_note = QLabel("")
        self.model_note.setWordWrap(True)
        self.model_note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        lay.addWidget(self.model_note)
        outer.addWidget(box)

        if not transcribe.faster_whisper_installed():
            self.file_btn.setEnabled(False)
            self.download_btn.setEnabled(False)
            self.file_btn.setToolTip(
                "Install faster-whisper to transcribe (pip install "
                "faster-whisper).")
            self.model_note.setText(
                "faster-whisper is not installed: pip install faster-whisper")
        else:
            self._refresh_model_note()

    def _choice(self, box: QComboBox):
        return box.itemData(box.currentIndex())

    def _refresh_model_note(self, *_a) -> None:
        """Whether the chosen model is on this PC. Checked on a thread: the
        cache lookup can take a moment the first time."""
        if not transcribe.faster_whisper_installed():
            return
        name = self._choice(self.model_box)

        def work() -> None:
            cached = transcribe.model_is_cached(name)

            def show() -> None:
                if self._choice(self.model_box) != name:
                    return
                self._model_cached = cached
                self.download_btn.setEnabled(not cached and not self._downloading)
                self.model_note.setText(
                    f"Whisper {name} is on this PC; transcribing stays "
                    "offline." if cached else
                    f"Whisper {name} is not on this PC yet. \"Download "
                    "model\" fetches it once (internet); after that it "
                    "runs offline.")
            self._to_ui(show)
        threading.Thread(target=work, name="speech-whisper-cache", daemon=True).start()

    def on_download_model(self) -> None:
        name = self._choice(self.model_box)
        self._downloading = True
        self.download_btn.setEnabled(False)
        self.model_note.setText(f"Downloading Whisper {name}… (this is the "
                                "only step that uses the internet)")

        def work() -> None:
            try:
                transcribe.download(name)
                msg = f"Downloaded Whisper {name}."
            except Exception as exc:                      # noqa: BLE001
                msg = f"Could not download Whisper {name}: {exc}"

            def done() -> None:
                self._downloading = False
                self.status.setText(msg)
                self._refresh_model_note()
            self._to_ui(done)
        threading.Thread(target=work, name="speech-whisper-download",
                         daemon=True).start()

    def on_transcribe_file(self) -> None:
        path = dialogs.askopenfilename(
            title="Pick a video or audio file to transcribe",
            filetypes=[("Video or audio",
                        " ".join("*" + e for e in transcribe.MEDIA_EXTENSIONS)),
                       ("Video", " ".join("*" + e for e in
                                          transcribe.VIDEO_EXTENSIONS)),
                       ("Audio", " ".join("*" + e for e in
                                          transcribe.AUDIO_EXTENSIONS))],
            parent=self)
        if path:
            self.transcribe_path(path)

    def transcribe_path(self, path: str, *, save: bool = True,
                        command=None) -> Optional["transcribe.TranscribeJob"]:
        """Start transcribing ``path``. Everything the worker needs is read
        from the controls HERE, on the GUI thread."""
        if self._job is not None and self._job.running:
            self.status.setText("A transcription is already running; Cancel "
                                "it first.")
            return None
        model = self._choice(self.model_box)
        fmt = self._choice(self.format_box)
        stamps = self.timestamps.isChecked()
        self._live = []
        self.transcription.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.file_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.save_as_btn.setEnabled(False)
        self.status.setText(f"Transcribing {Path(path).name}…")

        def on_event(ev) -> None:            # worker thread
            self._to_ui(self._on_job_event, ev, model, fmt, stamps, save)

        self._job = transcribe.TranscribeJob(
            path, model=model, device=self._choice(self.device_box),
            language=self._choice(self.language_box), on_event=on_event,
            command=command)
        return self._job.start()

    def _on_job_event(self, ev, model, fmt, stamps, save) -> None:
        kind = ev.get("event")
        if kind == "stage":
            self.status.setText(ev.get("message", ""))
        elif kind == "started":
            lang = ev.get("language") or "?"
            self.status.setText(
                f"Transcribing on {ev.get('device', '').upper()} "
                f"(language: {lang})…")
        elif kind == "segment":
            self.progress.setValue(int(1000 * ev.get("progress", 0)))
            stamp = transcribe.format_timestamp(ev.get("start", 0))
            self.transcription.appendPlainText(
                f"[{stamp}] {ev.get('text', '').strip()}")
        elif kind == "fallback":
            self.transcription.clear()
            self.status.setText(ev.get("message", ""))
        elif kind in ("done", "error", "cancelled"):
            self.progress.setVisible(False)
            self.file_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            if kind == "done":
                self._finish(ev["transcript"], fmt, stamps, save)
            elif kind == "error":
                self.status.setText("Transcription failed: " +
                                    transcribe.friendly_error(
                                        ev.get("message", ""), model))
            else:
                self.status.setText("Transcription cancelled.")

    def _finish(self, t, fmt, stamps, save) -> None:
        self.last_transcript = t
        self.transcription.setPlainText(transcribe.render(t, fmt, stamps))
        self.save_as_btn.setEnabled(True)
        took = transcribe.format_timestamp(t.elapsed)
        if not save:
            self.status.setText(f"Transcribed in {took}.")
            return
        target = _free_name(transcribe.output_path(t.source, fmt))
        try:
            transcribe.save(t, target, fmt, stamps)
            self.last_saved = target
            self.status.setText(f"Transcribed in {took} on "
                                f"{t.device.upper()}. Saved {target}")
        except OSError as exc:
            self.status.setText(f"Transcribed in {took}, but it could not be "
                                f"saved beside the file ({exc}). Use "
                                "Save as….")

    def on_save_as(self) -> None:
        t = getattr(self, "last_transcript", None)
        if t is None:
            self.status.setText("There is no transcript to save yet.")
            return
        fmt = self._choice(self.format_box)
        start = transcribe.output_path(t.source, fmt)
        path = dialogs.asksaveasfilename(
            title="Save the transcript", initialdir=str(start.parent),
            initialfile=start.name,
            defaultextension=transcribe.FORMATS[fmt][0],
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt")], parent=self)
        if not path:
            return
        out = transcribe.save(t, path, None, self.timestamps.isChecked())
        self.last_saved = out
        self.status.setText(f"Saved {out}")

    def on_cancel(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self.status.setText("Cancelling…")

    # -- dictation -------------------------------------------------------
    def on_record(self) -> None:
        try:
            import sounddevice as sd
            import soundfile as sf
        except Exception:                                 # noqa: BLE001
            self.status.setText("Recording needs sounddevice and soundfile "
                                "(pip install sounddevice soundfile).")
            return
        out = Path(tempfile.gettempdir()) / "council_dictation.wav"
        self.status.setText("Recording 5 seconds…")

        def work() -> None:
            try:
                rate = 16000
                audio = sd.rec(5 * rate, samplerate=rate, channels=1,
                               dtype="float32")
                sd.wait()
                sf.write(str(out), audio, rate)
                msg = "Recorded 5 s. Press Transcribe."
                self._recording = out
            except Exception as exc:                      # noqa: BLE001
                msg = f"Recording failed: {exc}"
            self._to_ui(lambda: self.status.setText(msg))
        threading.Thread(target=work, name="speech-mic-record", daemon=True).start()

    def on_transcribe(self) -> None:
        rec = getattr(self, "_recording", None)
        if not rec or not Path(rec).exists():
            self.status.setText("Record something first, or use "
                                "\"Transcribe a file…\" for a video.")
            return
        self.transcribe_path(str(rec), save=False)

    def on_send_to_council(self) -> None:
        t = getattr(self, "last_transcript", None)
        text = t.text if t is not None else self.transcription.toPlainText()
        text = (text or "").strip()
        if not text:
            self.status.setText("There is no transcription to send yet.")
            return
        win = self.window
        council = win.tab("⚖ Council") if hasattr(win, "tab") else None
        box = getattr(council, "input", None)
        if box is None:
            self.status.setText("Open the Council tab once, then send again.")
            return
        box.setPlainText(text)
        if hasattr(win, "show_tab"):
            win.show_tab("⚖ Council")
        self.status.setText("Put the transcription in the Council's box.")


def _free_name(path: Path) -> Path:
    """``path``, or ``name (2).ext`` … when it exists: never overwrite."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def build_speech(window) -> QWidget:
    """Factory for the tab registry."""
    return SpeechTab(window)
