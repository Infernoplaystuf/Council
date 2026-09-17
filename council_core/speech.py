"""
council_core.speech — speaking an answer aloud, safely and once at a time.

WHAT THE TK VERSION DOES WRONG, AND WHY IT MATTERS MORE IN QT

1. THE WORKER READS A TK VARIABLE.
   `_tts_speak_last`'s background thread calls `self._tts_rate_var.get()` to
   find the speech rate. That is a widget read from a worker — the same defect
   class as the thirteen in the Council tab's `_send`, and it survives only
   because Tkinter marshals `Variable.get()` back to the interpreter thread
   internally. Qt does not. So the rate is SNAPSHOTTED by the caller, on the
   GUI thread, and handed in.

2. NOTHING SERIALISES PLAYBACK.
   Every Speak press starts a fresh daemon thread against one shared engine,
   and `runAndWait()` blocks inside it. Press it twice and two threads drive
   the same pyttsx3 engine at once; what happens then is engine-dependent and
   none of the outcomes are good. `TtsPlayer` holds a lock and refuses a
   second speak rather than racing.

3. STOP IS A RACE WITH START.
   `_tts_stop` calls `engine.stop()` on whatever `self._tts_engine` happens to
   be, with no coordination with the thread inside `runAndWait()`. Here, stop
   sets a flag the player checks and asks the engine to stop under the same
   lock.

WHY THE ENGINE IS CREATED ONCE AND KEPT
pyttsx3 initialisation is slow and, on Windows, allocates a COM object. The Tk
code lazily creates it and never disposes it, which is right; this keeps that
and adds only the discipline around using it.

pyttsx3 is optional. Everything here degrades to a message rather than an
import error, because a build without it should lose the Speak button's
function and nothing else.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

#: The Tk spinbox's range and default, kept exactly.
MIN_RATE, MAX_RATE, DEFAULT_RATE = 80, 300, 175

#: How much of an answer is spoken. The Tk code caps here "for sane length" —
#: an unbounded cap means a 20-minute reading of a long report with no way to
#: skip except Stop.
MAX_SPOKEN_CHARS = 4000


def clamp_rate(value: Any) -> int:
    """A usable words-per-minute from whatever the control holds.

    Tolerates a missing control, an empty box and non-numeric text — all three
    happen with an editable spinbox, and the Tk version already guards two of
    them.
    """
    try:
        rate = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return max(MIN_RATE, min(MAX_RATE, rate))


@dataclass
class SpeechResult:
    ok: bool
    message: str
    error: Optional[BaseException] = None


class TtsPlayer:
    """One engine, one speaker at a time.

    Deliberately not a singleton: a test, and a second front end, each want
    their own. The Tk build keeps one per console, which is the same thing.
    """

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        self._speaking = False
        self._stop_asked = False

    # -- the engine ----------------------------------------------------
    def engine(self):
        """The pyttsx3 engine, created once, or None if it is unavailable."""
        if self._engine is None:
            try:
                import pyttsx3
                self._engine = pyttsx3.init()
            except Exception:                             # noqa: BLE001
                return None
        return self._engine

    @property
    def available(self) -> bool:
        return self.engine() is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    # -- speaking ------------------------------------------------------
    def speak(self, text: str, *, rate: Any = DEFAULT_RATE) -> SpeechResult:
        """Say ``text``. BLOCKS until it finishes — call it from a worker.

        ``rate`` is a value, not a control. The caller reads the spinbox on the
        GUI thread and passes the number; this must never reach for a widget.
        """
        text = (text or "").strip()
        if not text:
            return SpeechResult(False, "There is nothing to speak yet.")

        engine = self.engine()
        if engine is None:
            return SpeechResult(
                False,
                "Speech is unavailable — install pyttsx3 to use it "
                "(pip install pyttsx3).")

        with self._lock:
            if self._speaking:
                # Two threads driving one pyttsx3 engine is engine-dependent
                # and none of the outcomes are good.
                return SpeechResult(False, "Already speaking — press Stop first.")
            self._speaking = True
            self._stop_asked = False

        try:
            engine.setProperty("rate", clamp_rate(rate))
            engine.say(text[:MAX_SPOKEN_CHARS])
            engine.runAndWait()
        except Exception as exc:                          # noqa: BLE001
            return SpeechResult(False, f"Speech failed: {exc}", error=exc)
        finally:
            with self._lock:
                self._speaking = False

        if self._stop_asked:
            return SpeechResult(True, "Stopped.")
        spoken = min(len(text), MAX_SPOKEN_CHARS)
        tail = ("" if spoken == len(text) else
                f" (the first {MAX_SPOKEN_CHARS:,} characters)")
        return SpeechResult(True, f"Spoke {spoken:,} characters{tail}.")

    def stop(self) -> SpeechResult:
        """Ask playback to stop. Safe to call when nothing is speaking."""
        with self._lock:
            self._stop_asked = True
            engine = self._engine
        if engine is None:
            return SpeechResult(True, "Nothing to stop.")
        try:
            engine.stop()
        except Exception as exc:                          # noqa: BLE001
            return SpeechResult(False, f"Could not stop: {exc}", error=exc)
        return SpeechResult(True, "Stopped.")
