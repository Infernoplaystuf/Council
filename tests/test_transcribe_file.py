"""
The Speech tab's "Transcribe a file…": a video or audio file in, a Markdown or
plain-text transcript out (council_core.transcribe).

faster-whisper and a Whisper model are not needed here: the child process is
replaced by a small fake that speaks the same line protocol, and the child's
own body is run against a fake faster_whisper module.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import transcribe as tr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# A child that speaks the protocol. On "cuda" it dies with no word, which is
# what CTranslate2 does on Windows when a CUDA library is missing.
FAKE_CHILD = r'''
import json, os, sys
MARK = "@@council-transcribe "
def emit(**d):
    sys.stdout.write(MARK + json.dumps(d) + "\n"); sys.stdout.flush()
job = json.loads(os.environ["COUNCIL_TRANSCRIBE_JOB"])
if job["device"] == "cuda" and "GPU_OK" not in job["path"]:
    sys.stderr.write("Could not locate cudnn_ops64_9.dll\n")
    sys.exit(3)
if "BROKEN" in job["path"]:
    emit(event="error", message="RuntimeError: no sound track"); sys.exit(1)
print("some library chatter that is not ours")
emit(event="stage", message="Reading", duration=10.0)
emit(event="started", device=job["device"], language="en", duration=10.0)
emit(event="segment", start=0.0, end=4.0, text=" Hello there.")
emit(event="segment", start=4.5, end=9.0, text=" This is a test.")
emit(event="done")
'''


def fake_command():
    return [sys.executable, "-c", FAKE_CHILD]


def run(path, device="auto"):
    events = []
    job = tr.TranscribeJob(path, model="tiny", device=device,
                           on_event=events.append, command=fake_command)
    job.start()
    assert job.wait(60)
    return job, events


def kinds(events):
    return [e["event"] for e in events]


# -- the job -----------------------------------------------------------------

def test_a_gpu_that_dies_falls_back_to_the_cpu(tmp_path):
    job, events = run(str(tmp_path / "talk.mp4"))
    assert "fallback" in kinds(events)
    assert kinds(events)[-1] == "done"
    t = job.transcript
    assert t.device == "cpu"
    assert t.language == "en"
    assert t.text == "Hello there. This is a test."
    fb = next(e for e in events if e["event"] == "fallback")
    assert "cudnn_ops64_9.dll" in fb["message"]


def test_a_working_gpu_is_used_with_no_fallback(tmp_path):
    job, events = run(str(tmp_path / "GPU_OK.mp4"))
    assert "fallback" not in kinds(events)
    assert job.transcript.device == "cuda"


def test_progress_follows_the_segments(tmp_path):
    _job, events = run(str(tmp_path / "GPU_OK.mp4"))
    progress = [e["progress"] for e in events if e["event"] == "segment"]
    assert progress == [pytest.approx(0.4), pytest.approx(0.9)]


def test_gpu_only_reports_the_gpu_error_and_does_not_fall_back(tmp_path):
    _job, events = run(str(tmp_path / "talk.mp4"), device="cuda")
    assert "fallback" not in kinds(events)
    assert events[-1]["event"] == "error"
    assert "cudnn" in events[-1]["message"]


def test_an_error_from_the_child_is_passed_on(tmp_path):
    _job, events = run(str(tmp_path / "BROKEN.mp4"), device="cpu")
    assert events[-1] == {"event": "error",
                          "message": "RuntimeError: no sound track"}


SLOW_CHILD = r'''
import json, os, sys, time
job = json.loads(os.environ["COUNCIL_TRANSCRIBE_JOB"])
sys.stdout.write("@@council-transcribe " + json.dumps({"event": "stage", "message": "x"}) + "\n")
sys.stdout.flush()
time.sleep(60)
'''


def test_cancel_kills_the_child_and_says_so(tmp_path):
    events = []
    job = tr.TranscribeJob(str(tmp_path / "a.mp4"), device="auto",
                           on_event=events.append,
                           command=lambda: [sys.executable, "-c", SLOW_CHILD])
    job.start()
    deadline = time.time() + 30
    while not events and time.time() < deadline:
        time.sleep(0.05)
    job.cancel()
    assert job.wait(30)
    assert kinds(events)[-1] == "cancelled"
    assert "fallback" not in kinds(events)


# -- the child's own body, against a fake faster_whisper ----------------------

def _fake_faster_whisper(monkeypatch, seen):
    import numpy as np
    pkg = types.ModuleType("faster_whisper")
    audio = types.ModuleType("faster_whisper.audio")
    audio.decode_audio = lambda path, sampling_rate=16000: np.zeros(
        sampling_rate * 3, dtype=np.float32)

    class Seg:
        def __init__(self, s, e, t):
            self.start, self.end, self.text = s, e, t

    class Model:
        def __init__(self, name, **kw):
            seen["model"] = (name, kw)

        def transcribe(self, audio, **kw):
            seen["transcribe"] = kw
            return iter([Seg(0.0, 1.5, " One."), Seg(1.5, 3.0, " Two.")]), \
                types.SimpleNamespace(language="en")
    pkg.WhisperModel = Model
    pkg.audio = audio
    monkeypatch.setitem(sys.modules, "faster_whisper", pkg)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio)


def test_the_child_runs_offline_and_without_the_silero_vad(monkeypatch, capsys):
    pytest.importorskip("numpy")
    seen = {}
    _fake_faster_whisper(monkeypatch, seen)
    rc = tr.run_job({"path": "x.mp4", "model": "large-v3-turbo",
                     "device": "cpu", "language": None})
    assert rc == 0
    name, kw = seen["model"]
    assert name == "large-v3-turbo"
    assert kw["local_files_only"] is True          # never a download
    assert kw["device"] == "cpu" and kw["compute_type"] == "int8"
    assert seen["transcribe"]["vad_filter"] is False   # Silero is not US-made
    lines = [json.loads(l[len(tr.MARK):]) for l in
             capsys.readouterr().out.splitlines() if l.startswith(tr.MARK)]
    assert [l["event"] for l in lines] == [
        "stage", "stage", "started", "segment", "segment", "done"]
    assert lines[2]["duration"] == pytest.approx(3.0)


def test_the_child_uses_float16_on_the_gpu(monkeypatch, capsys):
    pytest.importorskip("numpy")
    seen = {}
    _fake_faster_whisper(monkeypatch, seen)
    assert tr.run_job({"path": "x.mp4", "model": "tiny", "device": "cuda"}) == 0
    assert seen["model"][1]["compute_type"] == "float16"


def test_only_openai_whisper_models_are_offered():
    names = [n for n, _ in tr.MODEL_CHOICES]
    assert all(not n.startswith("distil") for n in names)
    assert tr.DEFAULT_MODEL in names


def test_a_missing_model_is_explained():
    msg = tr.friendly_error("LocalEntryNotFoundError: Cannot find the "
                            "requested files in the local cache", "small")
    assert "Download model" in msg and "small" in msg


# -- output -------------------------------------------------------------------

def _transcript():
    return tr.Transcript(
        source=r"C:\videos\talk.mp4", language="en", duration=125.0,
        model="large-v3-turbo", device="cuda",
        segments=[tr.Segment(0.0, 4.0, " Hello there."),
                  tr.Segment(4.2, 9.0, " This is a test."),
                  tr.Segment(70.0, 75.0, " After a pause.")])


def test_markdown_has_a_title_facts_and_timestamped_paragraphs():
    md = tr.to_markdown(_transcript())
    assert md.startswith("# Transcript: ")
    assert "- **Language:** en" in md
    assert "- **Length:** 02:05" in md
    assert "Whisper large-v3-turbo on CUDA" in md
    assert "**[00:00]** Hello there. This is a test." in md
    assert "**[01:10]** After a pause." in md


def test_text_with_and_without_timestamps():
    t = _transcript()
    assert tr.to_text(t, True).splitlines()[0] == "[00:00 - 00:04] Hello there."
    plain = tr.to_text(t, False)
    assert plain == "Hello there. This is a test.\n\nAfter a pause.\n"


def test_long_videos_get_hours():
    assert tr.format_timestamp(3725) == "1:02:05"
    assert tr.format_timestamp(65) == "01:05"


@pytest.mark.parametrize("fmt,ext", [("md", ".md"), ("txt", ".txt")])
def test_saving_picks_the_format_from_the_choice_or_the_name(tmp_path, fmt, ext):
    t = _transcript()
    out = tr.save(t, tmp_path / ("out" + ext))
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Transcript") == (fmt == "md")
    assert tr.output_path(r"C:\v\talk.mp4", fmt).suffix == ext
    assert not list(tmp_path.glob("*.tmp"))


def test_the_core_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "transcribe.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


def test_the_built_app_can_start_the_child():
    source = (ROOT / "council_qt.py").read_text(encoding="utf-8")
    assert '"--transcribe-worker"' in source
    assert tr.start_command()[0] == sys.executable


# -- the tab ------------------------------------------------------------------

@pytest.fixture
def tab():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from council_qt.tabs.speech import SpeechActions, SpeechTab
    from council_core import speech as speech_core
    # A player of its own and no vault: this tab touches no user data.
    widget = SpeechTab(actions=SpeechActions(player=speech_core.TtsPlayer()))
    yield widget, app
    widget.deleteLater()


def _pump(app, until, timeout=60):
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def test_the_tab_transcribes_and_saves_beside_the_video(tab, tmp_path):
    widget, app = tab
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"not really a video")
    (tmp_path / "talk.md").write_text("mine", encoding="utf-8")  # kept
    widget.format_box.setCurrentIndex(widget.format_box.findData("md"))
    job = widget.transcribe_path(str(video), command=fake_command)
    assert job is not None
    _pump(app, lambda: widget.last_saved is not None)
    saved = widget.last_saved
    assert saved == tmp_path / "talk (2).md"
    assert (tmp_path / "talk.md").read_text(encoding="utf-8") == "mine"
    assert "Hello there. This is a test." in saved.read_text(encoding="utf-8")
    assert widget.file_btn.isEnabled() and widget.save_as_btn.isEnabled()
    assert not widget.cancel_btn.isEnabled()


def test_the_tab_saves_text_when_text_is_picked(tab, tmp_path):
    widget, app = tab
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"x")
    widget.format_box.setCurrentIndex(widget.format_box.findData("txt"))
    widget.timestamps.setChecked(False)
    widget.transcribe_path(str(video), command=fake_command)
    _pump(app, lambda: widget.last_saved is not None)
    assert widget.last_saved == tmp_path / "clip.txt"
    assert widget.last_saved.read_text(encoding="utf-8") == \
        "Hello there. This is a test.\n"


def test_the_tab_refuses_a_second_run_while_one_is_going(tab, tmp_path):
    widget, app = tab
    first = widget.transcribe_path(
        str(tmp_path / "a.mp4"),
        command=lambda: [sys.executable, "-c", SLOW_CHILD])
    assert first is not None
    assert widget.transcribe_path(str(tmp_path / "b.mp4"),
                                  command=fake_command) is None
    widget.on_cancel()
    assert first.wait(30)
    _pump(app, lambda: widget.file_btn.isEnabled())
    assert "cancelled" in widget.status.text().lower()


def test_the_worker_reads_no_control():
    """Every choice is read on the GUI thread before the job starts: the
    defect the Speech tab's port exists to avoid."""
    import ast
    source = (ROOT / "council_qt" / "tabs" / "speech.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "transcribe_path")
    inner = next(n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef)
                 and n.name == "on_event")
    body = ast.get_source_segment(source, inner) or ""
    for control in ("_box", "timestamps", "isChecked", "currentIndex"):
        assert control not in body


MISSING_CHILD = r'''
import json, os, sys
job = json.loads(os.environ["COUNCIL_TRANSCRIBE_JOB"])
sys.stdout.write("@@council-transcribe " + json.dumps({"event": "error",
    "message": "LocalEntryNotFoundError: outgoing traffic has been disabled"}) + "\n")
'''


def test_a_missing_model_is_not_retried_on_the_cpu(tmp_path):
    events = []
    job = tr.TranscribeJob(str(tmp_path / "a.mp4"), device="auto",
                           on_event=events.append,
                           command=lambda: [sys.executable, "-c", MISSING_CHILD])
    job.start()
    assert job.wait(30)
    assert kinds(events) == ["error"]


DOWNLOAD_CHILD = r'''
import json, os, sys
job = json.loads(os.environ["COUNCIL_TRANSCRIBE_JOB"])
assert "HF_HUB_OFFLINE" not in os.environ, "the download would be refused"
sys.stdout.write("@@council-transcribe " + json.dumps({"event": "downloaded",
    "path": "/cache/" + job["download"]}) + "\n")
'''


def test_download_runs_in_a_child_with_the_offline_switch_off(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")      # as the Connections tab sets it
    path = tr.download("small",
                       command=lambda: [sys.executable, "-c", DOWNLOAD_CHILD])
    assert path == "/cache/small"
