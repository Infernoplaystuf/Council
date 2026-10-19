"""
council_core.ollama_install — download a US-origin vision model into the
local Ollama instance, on the user's deliberate request.

Why this file exists at all: the rest of the Council carries a repeated
rule — "the council never downloads a model" — because models arrive from
machines the user has not agreed to pay bytes to. This module is the one
place that CAN download a model, and it only runs when the user clicks
Install in the dialog. The policy is still: the Council will not pull
anything on its own.

The pull itself streams through Ollama's HTTP ``/api/pull`` endpoint; the
response is newline-delimited JSON with progress for each layer. Everything
runs on the user's machine against ``http://localhost:11434``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Optional

OLLAMA = "http://localhost:11434"


@dataclass(frozen=True)
class VisionModel:
    """A single catalog entry for a US-origin vision model.

    ``approx_vram_gb`` and ``download_gb`` are documented so the install
    dialog can show both. See ``catalog()`` for sourcing and
    ``[[target-hardware]]`` for the 4080 (16 GB) budget."""
    name: str                      # Ollama tag, e.g. "granite3.2-vision:2b"
    label: str                     # Human label, e.g. "IBM Granite Vision 2B"
    family: str                    # One of US_VISION_FAMILIES
    approx_vram_gb: float          # Rough VRAM at Q4 (what Ollama ships).
    download_gb: float             # Download size in GB.
    description: str               # One-line, user-facing.
    recommended: bool = False      # Highlighted in the dialog.


#: US-origin vision models recommended for the Council's 4080 / 16 GB target.
#: Granite is the user's preferred family (see memory [[us-models-only]]).
CATALOG: List[VisionModel] = [
    VisionModel(
        name="granite3.2-vision:2b",
        label="IBM Granite Vision 3.2 (2B)",
        family="granite",
        approx_vram_gb=2.5,
        download_gb=1.9,
        description="Small, fast, fits any machine. Good first choice for "
                    "scanned forms and clean tables.",
        recommended=True,
    ),
    VisionModel(
        name="granite3.2-vision:8b",
        label="IBM Granite Vision 3.2 (8B)",
        family="granite",
        approx_vram_gb=8.0,
        download_gb=5.1,
        description="Strongest Granite vision model that fits a 4080. "
                    "Preferred for messy or handwritten inspection images.",
        recommended=False,
    ),
    VisionModel(
        name="llama3.2-vision:11b",
        label="Meta Llama 3.2 Vision (11B)",
        family="llama",
        approx_vram_gb=9.5,
        download_gb=7.9,
        description="Fits a 4080. Slower than Granite 8B but a different "
                    "style of extraction — worth a try if Granite misses "
                    "numbers you can see.",
        recommended=False,
    ),
]


US_VISION_FAMILIES = ("llama", "gemma", "phi", "granite", "olmo")


class OllamaUnavailable(RuntimeError):
    """Ollama is not reachable on this machine."""


def _request(path: str, payload: Optional[dict] = None,
             *, timeout: float = 10) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        OLLAMA + path, method="POST" if payload is not None else "GET",
        data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OllamaUnavailable(
            f"Ollama is not reachable at {OLLAMA}: {exc}") from exc


def installed_models() -> List[str]:
    """Every model name the local Ollama has, newest first."""
    try:
        tags = _request("/api/tags").get("models") or []
    except OllamaUnavailable:
        return []
    return [t["name"] for t in tags if isinstance(t, dict) and t.get("name")]


def is_installed(name: str, installed: Optional[Iterable[str]] = None) -> bool:
    """True when ``name`` (optionally with its tag) is already pulled."""
    names = list(installed) if installed is not None else installed_models()
    if name in names:
        return True
    # Allow "granite3.2-vision" to match "granite3.2-vision:latest" / ":2b".
    base = name.split(":")[0]
    return any(n.split(":")[0] == base for n in names)


def catalog() -> List[VisionModel]:
    """The curated list of installable US-origin vision models."""
    return list(CATALOG)


@dataclass
class PullProgress:
    """One progress line from ``/api/pull``.

    ``fraction`` is None when the current step has no measurable size (e.g.
    ``"pulling manifest"``); otherwise it is in ``[0, 1]``. Zero-sized
    layers report ``fraction == 1.0``, not NaN."""
    status: str
    digest: str = ""
    completed: int = 0
    total: int = 0
    fraction: Optional[float] = None

    @classmethod
    def from_json(cls, obj: dict) -> "PullProgress":
        status = str(obj.get("status") or "")
        digest = str(obj.get("digest") or "")
        completed = int(obj.get("completed") or 0)
        total = int(obj.get("total") or 0)
        fraction: Optional[float] = None
        if total > 0:
            fraction = min(1.0, max(0.0, completed / total))
        elif completed and not total:
            fraction = None
        elif status in ("success", "complete"):
            fraction = 1.0
        return cls(status=status, digest=digest, completed=completed,
                   total=total, fraction=fraction)

    def is_terminal(self) -> bool:
        return self.status.lower() == "success"


def iter_pull(name: str, *, timeout: float = 600) -> Iterator[PullProgress]:
    """Stream ``/api/pull`` for ``name``; yield one PullProgress per line.

    The caller is responsible for consuming the iterator — closing it stops
    the download. Raises OllamaUnavailable if the socket cannot be opened;
    a mid-stream HTTP error raises urllib.error.HTTPError, which the UI
    surfaces as a line like "pull failed: ...".
    """
    req = urllib.request.Request(
        OLLAMA + "/api/pull", method="POST",
        data=json.dumps({"name": name, "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        raise OllamaUnavailable(
            f"Ollama is not reachable at {OLLAMA}: {exc}") from exc
    try:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            yield PullProgress.from_json(obj)
    finally:
        resp.close()


def pull(name: str, on_progress: Optional[Callable[[PullProgress], None]] = None,
         *, timeout: float = 600) -> PullProgress:
    """Download ``name`` and return the final progress line.

    ``on_progress`` is called from the current thread for every streamed
    update; the caller passes a thread-safe shim (``_to_ui``) when driving
    from the GUI."""
    final = PullProgress(status="(no update)")
    for prog in iter_pull(name, timeout=timeout):
        final = prog
        if on_progress is not None:
            on_progress(prog)
        if prog.is_terminal():
            break
    return final
