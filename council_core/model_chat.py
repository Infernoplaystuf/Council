"""
council_core.model_chat — telling the user, in chat, which models they can get.

Two commands, answered without a model call:

  "what models can I download?" / "which models fit my machine?" /
  "recommend a model" / "list downloadable models"
      -> the curated catalog ranked for THIS machine's GPU and RAM (the same
         ranking the Models tab shows), with the phrase to download each.

  "download granite 3.1 8b"            -> a catalog model, by id or name
  "download <owner/repo> <file>.gguf"  -> any GGUF on Hugging Face (Tk's
                                          chat command, which the Qt port had
                                          not carried over)

WHY IN CHAT AS WELL AS THE MODELS TAB
The Models tab answers "what fits?" only for someone who knows to open it. The
Council is where people ask. And the direction of travel is several models at
once — different roles on different models, and tasks handed between them —
which starts with the app being able to say what is available and fetch it.

US ORIGIN IS ENFORCED FOR A RAW REPO
The catalog is curated US-origin. A free-form "download <repo> <file>" is not,
so it goes through model_finder.classify_origin: a name it recognises as
non-US is refused with the reason; an unrecognised one downloads with a note
that its origin was not verified — the same distinction the Models tab's
Source column draws.

Downloading does NOT switch the active model. The file lands in the models
folder, and the Models tab's "Download & switch" on the same model is then
instant (the downloader skips a file it already has).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from . import model_jobs

Say = Callable[[str, str, str], None]
_I = re.IGNORECASE

AVAILABLE_RES = (
    re.compile(r"^\s*(?:what|which)\s+(?:\w+\s+){0,2}models?\s+(?:can|could|"
               r"should|do|would)\s+i\s+(?:download|get|use|run|install|try)"
               r"\b.*$", _I),
    re.compile(r"^\s*(?:what|which)\s+models?\s+(?:are\s+)?(?:available|"
               r"downloadable|fit|would\s+fit|run\s+on)\b.*$", _I),
    re.compile(r"^\s*(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:available\s+|"
               r"downloadable\s+)models?\b.*$", _I),
    re.compile(r"^\s*(?:list|show)\s+(?:me\s+)?(?:the\s+)?models?\s+(?:i\s+"
               r"can\s+download|to\s+download|that\s+fit)\b.*$", _I),
    re.compile(r"^\s*(?:recommend|suggest)\s+(?:me\s+)?(?:a\s+|some\s+)?"
               r"(?:\w+\s+){0,2}models?\b.*$", _I),
)
#: Tk's pattern (council_gui_engine.py:5147), verbatim.
DOWNLOAD_HF_RE = re.compile(
    r"^\s*(?:download|fetch|pull)\s+(?:from\s+)?(?:huggingface\s+|hf\s+)?"
    r"([A-Za-z0-9_\-./]+)\s+(\S+\.gguf)\s*[.!?]?\s*$", _I)
DOWNLOAD_NAME_RE = re.compile(
    r"^\s*(?:download|fetch|pull|get)\s+(?:the\s+)?(?:model\s+)?(.+?)"
    r"(?:\s+model)?\s*[.!?]?\s*$", _I)

#: How many ranked models the answer lists.
LIST_LIMIT = 9


@dataclass(frozen=True)
class Target:
    repo: str
    filename: str
    name: str
    size_gb: Optional[float] = None
    verified: bool = False       # curated catalog: origin checked


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def catalog_matches(query: str, models: Optional[List[Any]] = None) -> List[Any]:
    """Catalog entries whose id+name contain every word of ``query``."""
    if models is None:
        import model_catalog
        models = list(model_catalog.MODELS)
    words = _norm(query).split()
    if not words:
        return []
    exact = [m for m in models if _norm(m.id) == _norm(query)]
    if exact:
        return exact
    # Whole words, so "3" does not match every model with a 3 in it.
    hay = [(m, set(f"{_norm(m.id)} {_norm(m.name)}".split())) for m in models]
    return [m for m, tokens in hay if all(w in tokens for w in words)]


def origin(repo: str, filename: str) -> str:
    try:
        import model_finder
        return model_finder.classify_origin(f"{repo} {filename}")
    except Exception:                                     # noqa: BLE001
        return "unknown"


class ModelChat:
    """Answers the two commands. ``say`` must be safe from a worker."""

    _download_lock = threading.Lock()      # one download per process

    def __init__(self, say: Say, *, catalog: Optional[List[Any]] = None,
                 jobs: Any = model_jobs):
        self.say = say
        self._catalog = catalog
        self.jobs = jobs

    # -- routing -----------------------------------------------------------
    def plan(self, text: str) -> Optional[Callable[[], None]]:
        if not text:
            return None
        line = text.split("\n", 1)[0]
        if any(rx.match(line) for rx in AVAILABLE_RES):
            return self.available
        m = DOWNLOAD_HF_RE.match(line)
        if m:
            repo, filename = m.group(1).strip(), m.group(2).strip()
            return lambda: self.download(Target(repo, filename,
                                                name=filename))
        m = DOWNLOAD_NAME_RE.match(line)
        if m:
            hits = catalog_matches(m.group(1), self._catalog)
            if hits:
                # Only claimed when it names a catalog model: "download the
                # report as csv" is somebody else's command.
                return lambda: self.download_named(m.group(1), hits)
        return None

    # -- what is available ---------------------------------------------------
    def available(self) -> None:
        self.say("Council", "Checking this machine's GPU and memory…",
                 "observation")
        hardware = self.jobs.detect_hardware()
        found = self.jobs.find(hardware)
        if not found.ok:
            self.say("Writer", found.message, "final")
            return
        if not found.rows:
            self.say("Writer", f"{hardware.summary}\n\n{found.message}",
                     "final")
            return
        lines = [hardware.summary, "",
                 "Models you can download, best fit first:"]
        for row in found.rows[:LIST_LIMIT]:
            lines.append("  • " + self.describe(row))
        lines += ["", 'To get one, say "download <name>" — for example '
                  f'"download {found.rows[0].model_id}" — or use the Models '
                  "tab, which can also switch to it.",
                  "Curated models are US-made and checked; anything else "
                  "you name by repo is checked by name only."]
        self.say("Writer", "\n".join(lines), "final")

    @staticmethod
    def describe(row) -> str:
        raw = row.raw
        size = raw.get("size_gb")
        where = "fits your GPU" if raw.get("fits_vram") else "runs on CPU"
        role = raw.get("role")
        bits = [row.cells[0]]
        if size:
            bits.append(f"{size:g} GB")
        bits.append(where)
        if role and role != "general":
            bits.append(f"good for {role}")
        return " — ".join(bits) + f"   [{row.model_id}]"

    # -- downloading -------------------------------------------------------
    def download_named(self, query: str, hits: List[Any]) -> None:
        if len(hits) > 1:
            options = "\n".join(f"  • {m.name}   [{m.id}]" for m in hits)
            self.say("Writer", f"'{query}' matches {len(hits)} models:\n"
                     f"{options}\n\nSay \"download <id>\" with one of the "
                     "ids in brackets.", "final")
            return
        m = hits[0]
        self.download(Target(m.hf_repo, m.hf_file, name=m.name,
                             size_gb=m.size_gb, verified=True))

    def download(self, target: Target) -> None:
        note = ""
        if not target.verified:
            kind = origin(target.repo, target.filename)
            if kind == "non_us":
                self.say("Writer", f"Not downloading {target.filename}: its "
                         "name marks it as a non-US model, and this app only "
                         "uses US-made models. The Models tab lists the ones "
                         "that are checked.", "final")
                return
            if kind == "unknown":
                note = ("\nIts origin was not verified — only the curated "
                        "catalog is checked.")
        if not self._download_lock.acquire(blocking=False):
            self.say("Writer", "A model download is already running — wait "
                     "for it to finish.", "final")
            return
        try:
            try:
                dest = self.jobs.models_dir()
            except Exception:                             # noqa: BLE001
                dest = None
            problem = self.jobs.check_space(dest, target.size_gb) \
                if dest is not None else None
            if problem:
                self.say("Writer", problem, "final")
                return
            self.say("Council", f"Downloading {target.name} from "
                     f"{target.repo}" + (f" into {dest}" if dest else "")
                     + " …", "observation")
            path = self.jobs.download(target.repo, target.filename,
                                      on_progress=self._progress(target.name),
                                      size_gb=target.size_gb)
        except Exception as exc:                          # noqa: BLE001
            self.say("Writer", f"Download failed: {exc}", "final")
            return
        finally:
            self._download_lock.release()
        self.say("Writer", f"Downloaded {target.name} to:\n  {path}{note}\n\n"
                 "It is not the active model yet. To use it, open the Models "
                 "tab, select it and choose Download & switch — the file is "
                 "already here, so that is instant.", "final")

    def _progress(self, name: str) -> Callable[[int, Optional[int]], None]:
        """Every 10%, not every chunk — a line per 1 MB chunk of a 5 GB file
        would bury the transcript."""
        last = {"step": -1}

        def progress(done: int, total: Optional[int]) -> None:
            if not total:
                return
            step = int(10 * done / total)
            if step > last["step"]:
                last["step"] = step
                self.say("Workflow", "  " + self.jobs.progress_line(
                    done, total, name), "observation")

        return progress
