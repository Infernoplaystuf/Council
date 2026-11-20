"""
council_core.transcribe — turn a video or audio file into a transcript.

WHAT IT DOES
The Speech tab's "Transcribe a file…" picks a video (or audio) file, runs
OpenAI's Whisper on it through faster-whisper, and saves the words as Markdown
or plain text. Everything here is toolkit-free; the tab only shows it.

WHY THE MODEL RUNS IN A CHILD PROCESS
faster-whisper sits on CTranslate2, and on Windows a GPU run that cannot find
one of its CUDA libraries (cublas64_12.dll, cudnn_ops64_9.dll) does not raise:
the library prints "Could not locate ..." and the PROCESS EXITS. Run in the
Council's own process that would close the app mid-video. In a child it is one
failed attempt, and the parent starts the same job again on the CPU. A child
is also the only clean Cancel: killing it frees the GPU memory at once.

The child talks back on stdout, one JSON object per line behind a marker
(CTranslate2 and PyAV may print their own lines; those are ignored).

OFFLINE
The Council never downloads a model by itself. A model loads only from the
local Hugging Face cache (or a folder), with ``local_files_only``. The one
exception is the tab's "Download model" button, which the user presses.

MODELS
Only OpenAI's Whisper models (US-made, the project rule). No voice-activity
filter: faster-whisper's ``vad_filter`` uses Silero VAD, which is not US-made,
so it stays off.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent

#: (name faster-whisper knows, what the user sees). OpenAI Whisper only.
MODEL_CHOICES: List[tuple] = [
    ("large-v3-turbo", "large-v3-turbo (best speed/quality on a GPU)"),
    ("large-v3", "large-v3 (most accurate, slowest)"),
    ("medium", "medium"),
    ("small", "small"),
    ("base", "base (fast, rough)"),
    ("tiny", "tiny (fastest, roughest)"),
]
DEFAULT_MODEL = "large-v3-turbo"

#: Device preference -> what the user sees.
DEVICE_CHOICES: List[tuple] = [
    ("auto", "GPU, else CPU"),
    ("cuda", "GPU only"),
    ("cpu", "CPU only"),
]

#: Spoken language. None = Whisper detects it from the first 30 s.
LANGUAGE_CHOICES: List[tuple] = [
    (None, "Detect language"), ("en", "English"), ("es", "Spanish"),
    ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("pt", "Portuguese"), ("ja", "Japanese"), ("zh", "Chinese"),
    ("ko", "Korean"), ("ru", "Russian"), ("hi", "Hindi"),
]

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v",
                    ".flv", ".mpg", ".mpeg", ".ts", ".3gp")
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
                    ".wma", ".aiff", ".aif")
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

#: Output formats: key -> (extension, file-dialog label).
FORMATS: Dict[str, tuple] = {
    "md": (".md", "Markdown (*.md)"),
    "txt": (".txt", "Plain text (*.txt)"),
}

SAMPLE_RATE = 16000
MARK = "@@council-transcribe "


def media_filter() -> str:
    """The file dialog's filter string."""
    vids = " ".join("*" + e for e in VIDEO_EXTENSIONS)
    auds = " ".join("*" + e for e in AUDIO_EXTENSIONS)
    return (f"Video or audio ({vids} {auds});;Video ({vids});;"
            f"Audio ({auds});;All files (*)")


def faster_whisper_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


# ── the transcript ──────────────────────────────────────────────────────
@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    source: str
    segments: List[Segment] = field(default_factory=list)
    language: str = ""
    duration: float = 0.0
    model: str = ""
    device: str = ""
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments
                        if s.text.strip()).strip()


def format_timestamp(seconds: float, hours: bool = False) -> str:
    """``mm:ss``, or ``h:mm:ss`` when ``hours`` or the time needs it."""
    total = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if hours or h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _paragraphs(segments: Sequence[Segment], gap: float = 2.0,
                max_chars: int = 600) -> List[List[Segment]]:
    """Group segments into paragraphs: a new one after a pause of ``gap``
    seconds, or once a paragraph is long."""
    out: List[List[Segment]] = []
    cur: List[Segment] = []
    size = 0
    for seg in segments:
        if not seg.text.strip():
            continue
        if cur and (seg.start - cur[-1].end >= gap or size >= max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += len(seg.text)
    if cur:
        out.append(cur)
    return out


def to_markdown(t: Transcript, timestamps: bool = True) -> str:
    name = Path(t.source).name or "Transcript"
    hours = t.duration >= 3600
    lines = [f"# Transcript: {name}", ""]
    facts = [f"- **Source:** `{t.source}`"]
    if t.duration:
        facts.append(f"- **Length:** {format_timestamp(t.duration, hours)}")
    if t.language:
        facts.append(f"- **Language:** {t.language}")
    if t.model:
        how = t.model + (f" on {t.device.upper()}" if t.device else "")
        facts.append(f"- **Model:** Whisper {how}")
    facts.append(f"- **Transcribed:** {time.strftime('%Y-%m-%d %H:%M')}")
    lines += facts + ["", "---", ""]
    for para in _paragraphs(t.segments):
        body = " ".join(s.text.strip() for s in para)
        if timestamps:
            lines.append(f"**[{format_timestamp(para[0].start, hours)}]** {body}")
        else:
            lines.append(body)
        lines.append("")
    if not t.segments:
        lines += ["_No speech was found._", ""]
    return "\n".join(lines).rstrip() + "\n"


def to_text(t: Transcript, timestamps: bool = True) -> str:
    hours = t.duration >= 3600
    if timestamps:
        rows = [f"[{format_timestamp(s.start, hours)} - "
                f"{format_timestamp(s.end, hours)}] {s.text.strip()}"
                for s in t.segments if s.text.strip()]
        return ("\n".join(rows) + "\n") if rows else ""
    paras = [" ".join(s.text.strip() for s in p)
             for p in _paragraphs(t.segments)]
    return ("\n\n".join(paras) + "\n") if paras else ""


def render(t: Transcript, fmt: str, timestamps: bool = True) -> str:
    return to_markdown(t, timestamps) if fmt == "md" else to_text(t, timestamps)


def output_path(source: str, fmt: str) -> Path:
    """Where the transcript goes by default: beside the video, same name."""
    ext = FORMATS.get(fmt, FORMATS["md"])[0]
    return Path(source).with_suffix(ext)


def save(t: Transcript, path, fmt: Optional[str] = None,
         timestamps: bool = True) -> Path:
    path = Path(path)
    if fmt is None:
        fmt = "txt" if path.suffix.lower() == ".txt" else "md"
    # Atomic: a crash mid-write never leaves half a transcript.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(t, fmt, timestamps), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ── models on this PC ───────────────────────────────────────────────────
def model_is_cached(name: str) -> bool:
    """Whether ``name`` (a model name or a folder) can load with no network."""
    if not name:
        return False
    if Path(name).is_dir():
        return (Path(name) / "model.bin").exists()
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def download(name: str, command: Optional[Callable[[], List[str]]] = None,
             timeout: float = 3 * 3600) -> str:
    """Fetch ``name`` into the Hugging Face cache. Uses the internet: only the
    tab's Download button calls this.

    In a child, because the app process may already have HF_HUB_OFFLINE=1
    (the Connections tab sets it), and huggingface_hub reads that once, at
    import. Returns the model's folder; raises RuntimeError on failure."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               COUNCIL_TRANSCRIBE_JOB=json.dumps({"download": name}))
    flags = 0x08000000 if os.name == "nt" else 0           # CREATE_NO_WINDOW
    proc = subprocess.run((command or start_command)(), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=flags)
    for line in proc.stdout.splitlines():
        if line.startswith(MARK):
            ev = json.loads(line[len(MARK):])
            if ev.get("event") == "downloaded":
                return ev.get("path", "")
            if ev.get("event") == "error":
                raise RuntimeError(ev.get("message", ""))
    raise RuntimeError((proc.stderr or "").strip()[-400:]
                       or f"the download stopped (exit {proc.returncode})")


def prepare_cuda_dlls() -> List[str]:
    """Let CTranslate2 find cuBLAS / cuDNN on Windows.

    It needs cublas64_12.dll and cudnn*64_9.dll. A PC with the CUDA build of
    PyTorch already has both in torch\\lib, and pip's nvidia-cublas-cu12 /
    nvidia-cudnn-cu12 put them in site-packages\\nvidia\\*\\bin. Neither is on
    the DLL search path by default. Returns the folders added.
    """
    if os.name != "nt":
        return []
    import importlib.util
    dirs: List[Path] = []
    spec = importlib.util.find_spec("torch")
    if spec and spec.origin:
        dirs.append(Path(spec.origin).parent / "lib")
    spec = importlib.util.find_spec("nvidia")
    if spec and spec.submodule_search_locations:
        for base in spec.submodule_search_locations:
            dirs += sorted(Path(base).glob("*/bin"))
    if os.environ.get("CUDA_PATH"):
        dirs.append(Path(os.environ["CUDA_PATH"]) / "bin")
    added = []
    for d in dirs:
        if d.is_dir() and any(d.glob("cu*.dll")):
            try:
                os.add_dll_directory(str(d))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(d))
    return added


# ── the child process ───────────────────────────────────────────────────
def _emit(event: str, **data) -> None:
    data["event"] = event
    sys.stdout.write(MARK + json.dumps(data) + "\n")
    sys.stdout.flush()


def _decode_with_ffmpeg(path: str):
    """Decode through the ffmpeg program, for a file PyAV cannot open."""
    import numpy as np
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("PyAV could not read this file and ffmpeg is not "
                           "on PATH.")
    proc = subprocess.run(
        [exe, "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True, timeout=None)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg could not read this file: {tail}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def decode(path: str):
    """The file's sound as 16 kHz mono float32. PyAV first (it ships with
    faster-whisper), the ffmpeg program second."""
    try:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
        if len(audio):
            return audio
    except Exception:                                     # noqa: BLE001
        pass
    return _decode_with_ffmpeg(path)


def run_job(job: Dict[str, Any]) -> int:
    """Body of the child. ``job``: path, model, device ('cuda'|'cpu'),
    language; or {"download": name} for the Download button."""
    if job.get("download"):
        try:
            from faster_whisper.utils import download_model
            _emit("downloaded", path=str(download_model(job["download"])))
            return 0
        except Exception as exc:                          # noqa: BLE001
            _emit("error", message=f"{type(exc).__name__}: {exc}")
            return 1
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    path = job["path"]
    device = job.get("device") or "cpu"
    try:
        if device == "cuda":
            prepare_cuda_dlls()
        _emit("stage", message="Reading the sound from the file…")
        audio = decode(path)
        duration = len(audio) / float(SAMPLE_RATE)
        if duration <= 0:
            raise RuntimeError("The file has no sound track.")
        _emit("stage", message=f"Loading Whisper {job['model']} on "
              f"{device.upper()}…", duration=duration)
        from faster_whisper import WhisperModel
        model = WhisperModel(
            job["model"], device=device,
            compute_type="float16" if device == "cuda" else "int8",
            local_files_only=True)
        segments, info = model.transcribe(
            audio, language=job.get("language") or None, beam_size=5,
            vad_filter=False, condition_on_previous_text=True)
        _emit("started", device=device, language=info.language or "",
              duration=duration)
        for seg in segments:
            _emit("segment", start=float(seg.start), end=float(seg.end),
                  text=seg.text)
        _emit("done")
        return 0
    except Exception as exc:                              # noqa: BLE001
        _emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    """Entry point of the child: the job arrives as JSON in the environment
    (COUNCIL_TRANSCRIBE_JOB), never on the command line."""
    # The built app is windowed, and a windowed Python may start with
    # sys.stdout / sys.stderr set to None even though the parent handed it
    # pipes. The pipes are there at the OS level, so open them by number.
    for name, fd, mode in (("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, os.fdopen(fd, mode, encoding="utf-8",
                                             errors="replace", buffering=1))
            except OSError:
                if name != "stderr":
                    return 3
                sys.stderr = open(os.devnull, "w")
    try:
        job = json.loads(os.environ["COUNCIL_TRANSCRIBE_JOB"])
    except Exception as exc:                              # noqa: BLE001
        _emit("error", message=f"bad job: {exc}")
        return 2
    return run_job(job)


def start_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--transcribe-worker"]
    return [sys.executable, "-c",
            "import sys; sys.path.insert(0, %r); "
            "from council_core.transcribe import main; sys.exit(main())"
            % str(ROOT)]


# ── the parent's side ───────────────────────────────────────────────────
class TranscribeJob:
    """One file, run in a child, with GPU -> CPU fallback.

    ``on_event(dict)`` is called FROM A WORKER THREAD with events:
    stage(message), started(device, language, duration),
    segment(start, end, text, progress), fallback(message),
    done(transcript), error(message), cancelled().
    Exactly one of done / error / cancelled ends a job.
    """

    def __init__(self, path: str, model: str = DEFAULT_MODEL,
                 device: str = "auto", language: Optional[str] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 command: Optional[Callable[[], List[str]]] = None):
        self.path = str(path)
        self.model = model
        self.device = device
        self.language = language
        self.on_event = on_event or (lambda e: None)
        self._command = command or start_command
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.transcript: Optional[Transcript] = None

    # -- control --------------------------------------------------------
    def start(self) -> "TranscribeJob":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="speech-transcribe")
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancelled.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- work -----------------------------------------------------------
    def _send(self, event: str, **data) -> None:
        data["event"] = event
        try:
            self.on_event(data)
        except Exception:                                 # noqa: BLE001
            pass

    def _run(self) -> None:
        t0 = time.monotonic()
        devices = {"auto": ["cuda", "cpu"], "cuda": ["cuda"]}.get(
            self.device, ["cpu"])
        last_error = ""
        for i, dev in enumerate(devices):
            if self._cancelled.is_set():
                break
            transcript, error = self._attempt(dev)
            if transcript is not None:
                transcript.elapsed = time.monotonic() - t0
                self.transcript = transcript
                self._send("done", transcript=transcript)
                return
            last_error = error
            if self._cancelled.is_set() or model_missing(error):
                break           # the CPU would not find the model either
            if i + 1 < len(devices):
                self._send("fallback", message=(
                    "The GPU run did not work, so this is running on the CPU "
                    f"instead (slower). GPU said: {error}"))
        if self._cancelled.is_set():
            self._send("cancelled")
        else:
            self._send("error", message=last_error or "Transcription failed.")

    def _attempt(self, device: str):
        """One child run. Returns (Transcript, "") or (None, why)."""
        job = {"path": self.path, "model": self.model, "device": device,
               "language": self.language}
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   HF_HUB_OFFLINE="1", COUNCIL_TRANSCRIBE_JOB=json.dumps(job))
        try:
            self._proc = proc = subprocess.Popen(
                self._command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, creationflags=flags,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            return None, f"could not start the transcriber: {exc}"
        if self._cancelled.is_set():
            proc.kill()
        err_tail: List[str] = []

        def read_err() -> None:
            for line in proc.stderr:
                err_tail.append(line.rstrip())
                del err_tail[:-20]
        t_err = threading.Thread(target=read_err, daemon=True)
        t_err.start()

        t = Transcript(source=self.path, model=self.model, device=device)
        finished, error = False, ""
        for line in proc.stdout:
            if not line.startswith(MARK):
                continue
            try:
                ev = json.loads(line[len(MARK):])
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "stage":
                if ev.get("duration"):
                    t.duration = float(ev["duration"])
                self._send("stage", message=ev.get("message", ""))
            elif kind == "started":
                t.language = ev.get("language", "")
                t.duration = float(ev.get("duration") or t.duration)
                self._send("started", device=device, language=t.language,
                           duration=t.duration)
            elif kind == "segment":
                seg = Segment(float(ev["start"]), float(ev["end"]),
                              ev.get("text", ""))
                t.segments.append(seg)
                progress = (min(1.0, seg.end / t.duration)
                            if t.duration else 0.0)
                self._send("segment", start=seg.start, end=seg.end,
                           text=seg.text, progress=progress)
            elif kind == "done":
                finished = True
            elif kind == "error":
                error = ev.get("message", "")
        proc.wait()
        t_err.join(2.0)
        self._proc = None
        if finished and not self._cancelled.is_set():
            return t, ""
        if not error:
            # Died with no word: the CUDA-library exit described at the top.
            hint = next((l for l in reversed(err_tail) if l.strip()), "")
            error = (hint or f"the transcriber stopped (exit code "
                     f"{proc.returncode})")
        return None, error


def model_missing(message: str) -> bool:
    """Whether an error means the model is not in the local cache."""
    low = (message or "").lower()
    return ("local_files_only" in low or "localentrynotfound" in low
            or "cannot find the requested files" in low
            or "outgoing traffic has been disabled" in low)


def friendly_error(message: str, model: str) -> str:
    """What to tell the user for the errors people actually hit."""
    if model_missing(message):
        return (f"The Whisper model '{model}' is not on this PC yet. Press "
                "\"Download model\" once (it needs the internet), or pick a "
                "model already downloaded.")
    return message
