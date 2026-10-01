"""
council_core.model_jobs — which models fit this machine, and switching to one.

THE THING THIS TAB IS NOT
It does not change the PINS. `personality_backends.json` maps a role to a
backend key, and nothing in the Models tab writes it. What this tab changes is
the GGUF PATH — the one file behind every backend key — through
`onboarding.save_gguf_path` plus `council_engine.refresh_backend_config`.

Those are different operations with different live-reload behaviour, and
conflating them is easy: a path change takes effect on the next chat call,
because save_gguf_path also sets COUNCIL_GGUF_PATH in the process environment
and refresh_backend_config clears the cached model instance. A re-PIN would
need every personality rebuilt. Saying which one you are doing is the
difference between "takes effect immediately" being true and being a guess.

THE DETECTION IS SLOW AND THE TK TAB DOES IT IN ITS CONSTRUCTOR
`hardware_detect.detect()` shells out to nvidia-smi and falls back to importing
torch. Measured on this machine with the project interpreter: 5.85 SECONDS.
The Tk build gets away with it because every tab is constructed up front behind
the splash. Qt builds tabs lazily, so the same call in a constructor is nearly
six seconds of frozen window the first time the user clicks the tab.

So detection is a function a caller runs on a worker, never a side effect of
building anything.

US-ORIGIN IS A PRODUCT CONSTRAINT AND IT IS NOT UNIFORMLY VERIFIED
The curated catalog's origin is checked. Hugging Face results are classified by
a NAME HEURISTIC, which is a guess. Both appear in one list, so the source
column is not decoration — it is the difference between a verified claim and an
inferred one, and a front end that drops it is making a promise the data does
not support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The results table's columns, in order, with their headings.
COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("name", "Model"),
    ("org", "Maker"),
    ("params", "Params(B)"),
    ("vram", "VRAM≈GB"),
    ("fits", "Fits GPU?"),
    ("ctx", "Ctx(K)"),
    ("source", "Source"),
    ("local", "On this PC"),
)

CATALOG_NOTE = (
    "Curated US-made GGUF models ranked by fit for this machine. Models that "
    "fit your VRAM run on GPU; \"partial\" ones split between GPU and RAM "
    "(slower); the rest run on CPU.\n"
    "Nothing downloads automatically — pick one and fetch it from the listed "
    "repo, or `ollama pull` its tag. US-origin is verified for the catalog; "
    "online results are a name heuristic.")


@dataclass
class Hardware:
    gpu: str = "no GPU detected"
    vram_gb: Optional[float] = None
    ram_gb: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        line = (f"Your hardware:  GPU: {self.gpu}   "
                f"VRAM: {self.vram_gb or '—'} GB   "
                f"RAM: {self.ram_gb or '—'} GB")
        free = self.raw.get("vram_free_gb")
        if free:
            line += f"   (VRAM free now: {free} GB)"
        p, e = self.raw.get("cpu_p_cores"), self.raw.get("cpu_e_cores")
        if p and e:
            line += f"   CPU: {p} P-cores + {e} E-cores"
        speed = self.raw.get("ram_speed_mts")
        if speed:
            line += f"   RAM speed: {speed} MT/s"
        return line


PENDING_HARDWARE = "Your hardware:  detecting…"


#: The probe's answer, kept for the life of the process.
_DETECTED: Optional["Hardware"] = None


def detect_hardware(*, force: bool = False) -> Hardware:
    """Probe the machine. BLOCKING — measured at 5.85s. Use a worker.

    MEMOISED, because a GPU is not hot-swapped mid-session and the probe is
    expensive: it shells out to nvidia-smi and falls back to importing torch.
    The Tk tab pays once because it is built once; a Qt tab is built whenever
    it is first SHOWN, so without this a user who closes and reopens the tab
    waits again, every time.

    Measured: 4.01s cold, 0.0000s cached. I first wrote that this also
    accounted for the test suite going from ten seconds to twenty-four —
    it does not. Profiling put the time in the StandaloneHost tests, and the
    probe contributes 4s once. The memoisation is right for the app; it was
    not the explanation for the suite.

    ``force`` re-probes, for a machine where something really did change.

    ONE RISK THIS MOVE INTRODUCES, RECORDED RATHER THAN CLAIMED FIXED.
    `hardware_detect._fill_gpu` tries nvidia-smi first and falls back to
    `import torch`. Importing heavy native extensions from a NON-MAIN thread
    has produced a Windows access violation in this environment before — it
    happened once during this very session, in a worker importing numpy. On
    this machine the fallback never runs, because nvidia-smi answers, so I
    could NOT reproduce it here: four worker-thread probes in a row succeeded.
    On a machine without nvidia-smi the torch path would run on the worker.

    `warm()` exists for that: call it once on the main thread during startup
    and every later probe is a cache read. Phase 10 should.
    """
    global _DETECTED
    if _DETECTED is not None and not force:
        return _DETECTED
    try:
        import hardware_detect
        raw = hardware_detect.detect() or {}
    except Exception:                                     # noqa: BLE001
        return Hardware()                  # not cached: a failure may be transient
    _DETECTED = Hardware(gpu=raw.get("gpu_name") or "no GPU detected",
                         vram_gb=raw.get("vram_gb"), ram_gb=raw.get("ram_gb"),
                         raw=raw)
    return _DETECTED


def warm() -> None:
    """Probe once, from wherever it is safe to do so.

    Meant for the startup chain, on the MAIN thread, so the tab's worker never
    triggers the torch fallback — see detect_hardware for why that matters.
    Safe to call more than once, and safe to skip entirely.
    """
    try:
        detect_hardware()
    except Exception:                                     # noqa: BLE001
        pass


@dataclass
class ModelRow:
    """One candidate. `model_id` is the address; the cells are for reading."""
    model_id: str
    cells: Tuple[str, ...]
    repo: str = ""
    filename: str = ""
    verified_origin: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FindResult:
    ok: bool
    message: str
    rows: List[ModelRow] = field(default_factory=list)
    error: Optional[BaseException] = None


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def to_row(found: Dict[str, Any]) -> ModelRow:
    """One finder result as a table row.

    `source` says whether the origin was VERIFIED (the curated catalog) or
    GUESSED from the name (Hugging Face). Dropping that column would turn an
    inference into a claim.
    """
    # Field names READ off a real catalog entry, not guessed. The first
    # version of this invented `vram_gb`, `ctx_k`, `fits_gpu`, `repo` and
    # `file`; the real keys are `vram_gb_q4`, `context_k`, `fits_vram`,
    # `hf_repo` and `hf_file`, so every one of those columns rendered "—" and
    # every model claimed to be CPU-only on a machine with an RTX 4070.
    source = found.get("source") or "catalog"
    fit = found.get("fit")
    if found.get("fits_vram") or fit == "gpu":
        fits = "yes"
    elif fit == "partial":
        fits = "partial"
    else:
        fits = "CPU"
    return ModelRow(
        model_id=found.get("id") or found.get("name", ""),
        cells=(
            _cell(found.get("name")),
            _cell(found.get("org")),
            _cell(found.get("params_b")),
            _cell(found.get("vram_gb_q4") or found.get("size_gb")),
            fits,
            _cell(found.get("context_k")),
            _cell(source),
            found.get("installed") or "no",
        ),
        repo=found.get("hf_repo") or "",
        filename=found.get("hf_file") or "",
        # `origin_verified` is the catalog's OWN claim. An online hit is
        # classified by a name heuristic and does not carry it.
        verified_origin=bool(found.get("origin_verified")),
        raw=dict(found))


def role_for_task(task: str) -> str:
    """The Task box's text as a catalog role ("code", "docs", "general")."""
    try:
        import model_finder
        fn = getattr(model_finder, "role_for_task", None)
        return fn(task) if callable(fn) else "general"
    except Exception:                                     # noqa: BLE001
        return "general"


def warm_local_models() -> None:
    """Fill local_models' caches (Ollama tags for 5 s, GGUF headers until a
    file changes). BLOCKING — a few hundred ms on first use; for a worker."""
    try:
        from . import local_models
        local_models.list_local_models()
    except Exception:                                     # noqa: BLE001
        pass


def _mark_installed(items: List[Dict[str, Any]]) -> None:
    """Set "installed" ('Ollama' / 'GGUF') on catalog items already on this
    PC — by Ollama tag or by GGUF file name. Never raises."""
    try:
        from . import local_models
        local = local_models.list_local_models()
    except Exception:                                     # noqa: BLE001
        return
    ollama = {local_models.ollama_name(m["id"]).lower() for m in local
              if m.get("backend") == "ollama"}
    ollama |= {n.split(":", 1)[0] + ":latest" for n in ollama if ":" not in n}
    files = {str(m.get("file") or "").lower() for m in local
             if m.get("backend") == "gguf"}
    for item in items:
        tag = str(item.get("ollama") or "").lower()
        tags = {tag, tag + ":latest"} if tag and ":" not in tag else {tag}
        if tag and tags & ollama:
            item["installed"] = "Ollama"
        elif str(item.get("hf_file") or "").lower() in files:
            item["installed"] = "GGUF"


def find(hardware: Hardware, *, task: str = "", online: bool = False,
         role: Optional[str] = None, limit: int = 8,
         mark_installed: bool = True) -> FindResult:
    """Rank models by fit. BLOCKING when ``online`` — it searches the network.

    `find_models` returns a DICT — {"hardware", "catalog", "online",
    "online_available"} — not a list, and its parameters are `role`,
    `prefer_online` and `query`. I wrote this against an invented signature
    first and it silently produced an empty table on a machine with an RTX
    4070. That is the third time in this port I have assumed an API instead of
    reading it, after `council_engine`'s model slots and `load_pins`.

    ROLE-AWARE: the Task text picks the role ("coding" -> code, "docs" ->
    docs) — it used to reach only the online query, so the Coder role was
    never offered a coder. Rows already on this PC (an Ollama tag or a GGUF
    in the model folders) say so in "On this PC".

    The catalog list is authoritative; online results augment it and are
    origin-classified by a name heuristic rather than verified.
    """
    role = role or role_for_task(task)
    try:
        import model_finder
        found = model_finder.find_models(
            hardware=hardware.raw or None, query=task or "",
            prefer_online=bool(online), role=role, limit=limit) or {}
    except Exception as exc:                              # noqa: BLE001
        return FindResult(False, f"Could not rank the models: {exc!r}",
                          error=exc)

    catalog = list(found.get("catalog") or [])
    online_hits = list(found.get("online") or []) if online else []
    for item in catalog:
        item.setdefault("source", "catalog")
    for item in online_hits:
        item.setdefault("source", "online")
    if mark_installed:
        _mark_installed(catalog)
    rows = [to_row(item) for item in catalog + online_hits]
    verified = sum(1 for r in rows if r.verified_origin)
    if not rows:
        return FindResult(True, "Nothing in the catalog fits this machine.")
    tail = ("" if verified == len(rows) else
            f" — {verified} with verified US origin, "
            f"{len(rows) - verified} inferred from the name")
    for_role = "" if role in ("general", "", None) else f" for {role}"
    return FindResult(True, f"{len(rows)} model(s){for_role}{tail}.",
                      rows=rows)


def current_model() -> Tuple[str, Optional[float]]:
    """(the main model's name, its size in B when known): the main slot's
    Ollama model, else COUNCIL_GGUF_PATH's file. The Tk shell passed this to
    assess_upgrade all along; the Qt banner passed nothing, so can_upgrade
    was always False."""
    import os
    try:
        from . import model_slots
        main = model_slots.current().slots.get(model_slots.MAIN)
        if main is not None and model_slots.is_ollama(main.path):
            from . import local_models
            name = local_models.ollama_name(main.path)
            entry = local_models.ollama_model(name, max_age=60.0) or {}
            return name, entry.get("params_b")
    except Exception:                                     # noqa: BLE001
        pass
    path = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    if not path:
        return "", None
    try:
        from . import local_models
        entry = local_models.gguf_entry(path) or {}
        return Path(path).name, entry.get("params_b")
    except Exception:                                     # noqa: BLE001
        return Path(path).name, None


def upgrade_banner(hardware: Hardware, *, current: Optional[str] = None,
                   current_params_b: Optional[float] = None,
                   role: str = "general"
                   ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(the banner text, the model to offer) — or ("", None).

    BLOCKING. Separate from `find` because a machine with no headroom should
    not be told to look for one.

    The text is assess_upgrade's "reason" — the key it really returns. This
    read "message" / "summary" / "headline", none of which exist, so the Qt
    banner was always blank (measured: the keys are budget_gb, can_upgrade,
    current_model, current_params_b, current_vram_gb, headroom_gb, reason,
    upgrades). A model is offered only when can_upgrade: the button used to
    offer the first fitting model whatever was already running.
    """
    if current is None:
        current, guessed = current_model()
        current_params_b = current_params_b or guessed
    try:
        import model_finder
        assessment = model_finder.assess_upgrade(
            hardware=hardware.raw or None, current_model=current or "",
            current_params_b=current_params_b, role=role) or {}
    except Exception:                                     # noqa: BLE001
        return "", None
    text = (assessment.get("reason") or assessment.get("message")
            or assessment.get("summary") or "")
    if not assessment.get("can_upgrade"):
        return str(text), None
    candidates = assessment.get("upgrades") or []
    return str(text), (candidates[0] if candidates else None)


# ============================================================
# "Check this PC" — the benchmark hook
# ============================================================
# The benchmark harness lives on another branch (council_core.llm_bench). The
# Models tab's button calls check_this_pc(); it resolves the runner at CALL
# time, so the merge has to add nothing here: a registered runner wins, else
# council_core.llm_bench.check_this_pc, else a clear "not in this build".

@dataclass
class CheckResult:
    ok: bool
    message: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    report_path: Optional[Path] = None


_BENCH_RUNNER: Optional[Callable[..., Any]] = None


def register_bench(runner: Optional[Callable[..., Any]]) -> None:
    """Install the "Check this PC" runner: runner(models=, vault_dir=,
    on_progress=, should_stop=) -> CheckResult | dict | list of row dicts.
    None removes it."""
    global _BENCH_RUNNER
    _BENCH_RUNNER = runner


def bench_runner() -> Optional[Callable[..., Any]]:
    if _BENCH_RUNNER is not None:
        return _BENCH_RUNNER
    try:
        from . import llm_bench  # type: ignore[attr-defined]
    except Exception:                                     # noqa: BLE001
        return None
    fn = getattr(llm_bench, "check_this_pc", None)
    return fn if callable(fn) else None


def check_this_pc(*, models: Optional[Sequence[str]] = None,
                  vault_dir: Any = None,
                  on_progress: Optional[Callable[[str], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None
                  ) -> CheckResult:
    """Measure the installed models on this PC. BLOCKING — use a worker.
    ``models`` are list_local_models() ids (None: every US one)."""
    runner = bench_runner()
    if runner is None:
        return CheckResult(False, "The \"Check this PC\" benchmark is not in "
                                  "this build yet (council_core.llm_bench).")
    try:
        result = runner(models=list(models) if models else None,
                        vault_dir=vault_dir, on_progress=on_progress,
                        should_stop=should_stop)
    except Exception as exc:                              # noqa: BLE001
        return CheckResult(False, f"The benchmark failed: {exc!r}")
    if isinstance(result, CheckResult):
        return result
    if isinstance(result, dict):
        return CheckResult(bool(result.get("ok", True)),
                           str(result.get("message") or "Done."),
                           list(result.get("rows") or []),
                           result.get("report_path"))
    if isinstance(result, list):
        return CheckResult(True, f"Measured {len(result)} model(s).",
                           list(result))
    return CheckResult(True, str(result or "Done."))


# ============================================================
# Captions — the three that carry an ampersand
# ============================================================
# Qt reads "&" in any caption as a mnemonic and Tk does not, so every one of
# these needs escaping by the view. They are here because two of the three are
# RE-captions applied at run time, which a port that escapes only the
# build-time strings will miss — and the failure is silent and cosmetic, which
# is how it survives review.

DOWNLOAD_IDLE = "⬇ Download & switch (no upgrade available yet)"
DOWNLOAD_NONE = "⬇ Download & switch (no upgrade available)"


def download_caption(model_name: Optional[str]) -> str:
    return (f"⬇ Download & switch to {model_name}" if model_name
            else DOWNLOAD_NONE)


# ============================================================
# Downloading and switching
# ============================================================

@dataclass
class SwitchResult:
    ok: bool
    message: str
    path: Optional[Path] = None
    error: Optional[BaseException] = None


def check_space(target_dir: Any, needed_gb: Optional[float]) -> Optional[str]:
    """Why there is not room, or None.

    Checked BEFORE the download rather than discovered part-way through it:
    a multi-gigabyte download that dies at 90% has cost the user real time and
    told them nothing they could have known first.
    """
    if not needed_gb:
        return None
    try:
        import shutil
        free_gb = shutil.disk_usage(str(target_dir)).free / (1024 ** 3)
    except Exception:                                     # noqa: BLE001
        return None
    if free_gb < needed_gb * 1.1:
        return (f"Not enough room: {needed_gb:.1f} GB needed, "
                f"{free_gb:.1f} GB free.")
    return None


def confirm_download_text(name: str, size_gb: Optional[float],
                          dest: Optional[Path] = None) -> str:
    size = f"about {size_gb:.1f} GB" if size_gb else "a large file"
    where = f"It downloads to {dest}" if dest else "It downloads"
    return (f"Download {name} ({size}) and switch to it?\n\n"
            f"{where} and becomes the active model. "
            "Nothing else is changed — your existing model file is left "
            "where it is.")


def models_dir() -> Path:
    """Where downloads land — model_downloader's per-OS folder, NOT the vault
    (%LOCALAPPDATA%\\Council\\models on Windows, ~/.council/models elsewhere,
    COUNCIL_MODELS_DIR to override). The space check has to look here."""
    import model_downloader
    return model_downloader.default_models_dir()


def download(repo: str, filename: str, *,
             on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None,
             size_gb: Optional[float] = None) -> Path:
    """Fetch one GGUF into models_dir(). BLOCKING. Returns the file's path.

    THE FIRST VERSION NEVER DOWNLOADED ANYTHING. It called
    `download_gguf(vault_dir=..., on_progress=...)`; the real parameters are
    `dest_dir` and `progress`, so every call died with a TypeError, and the
    function returns a summary DICT, not a path. The test that should have
    caught it used a stand-in accepting `**kw` and returning a Path — a fake
    shaped like the assumption instead of the code. The test now binds the
    call against the real signature.
    """
    import model_downloader
    result = model_downloader.download_gguf(
        repo, filename, models_dir(), progress=on_progress,
        should_cancel=should_cancel, expected_size_gb=size_gb)
    return Path(result["path"])


def download_and_switch(row: ModelRow, vault_dir: Any, *,
                        on_progress: Optional[Callable[[int, Optional[int]],
                                                       None]] = None,
                        should_cancel: Optional[Callable[[], bool]] = None
                        ) -> SwitchResult:
    """Fetch a GGUF and make it the active model. BLOCKING — use a worker.

    Changes the PATH, not the pins. See this module's header for why that
    distinction decides whether "takes effect immediately" is true.
    ``on_progress(done_bytes, total_bytes)`` is the downloader's own shape.
    """
    if not row or not row.repo:
        return SwitchResult(False, "That model has no download source listed.")
    vault_dir = Path(vault_dir)
    try:
        path = download(row.repo, row.filename, on_progress=on_progress,
                        should_cancel=should_cancel,
                        size_gb=row.raw.get("size_gb"))
    except Exception as exc:                              # noqa: BLE001
        return SwitchResult(False, f"Download failed: {exc}", error=exc)

    if not path or not Path(path).exists():
        return SwitchResult(False, "The download did not produce a file.")

    try:
        import onboarding
        not_saved = onboarding.save_gguf_path(vault_dir, str(path))
        import council_engine
        council_engine.refresh_backend_config()
    except Exception as exc:                              # noqa: BLE001
        return SwitchResult(
            False,
            f"Downloaded to {path}, but switching to it failed: {exc}. "
            "Set the model path in Engine settings.",
            path=Path(path), error=exc)

    message = (f"Switched to {row.cells[0]}. It takes effect on the next "
               "question.")
    if not_saved:
        # The switch is live for this session either way (save_gguf_path sets
        # the environment); only the file for the NEXT launch was refused.
        # Saying "Switched" alone was measured with an unreadable settings
        # file: the next launch then silently started on another model.
        message = (f"Switched to {row.cells[0]} for this session, but NOT "
                   f"saved for the next launch: {not_saved}")
    return SwitchResult(True, message, path=Path(path))


def progress_line(done_bytes: int, total_bytes: Optional[int],
                  name: str = "") -> str:
    """One download progress line. A pure string builder.

    Pure on purpose: it is called from inside the downloader's callback, which
    runs on the worker, and anything that touched a widget there would be the
    defect this whole layer exists to prevent.
    """
    megs = done_bytes / (1024 ** 2)
    if not total_bytes:
        return f"{name} — {megs:,.0f} MB"
    percent = 100.0 * done_bytes / total_bytes
    total_megs = total_bytes / (1024 ** 2)
    return f"{name} — {megs:,.0f} / {total_megs:,.0f} MB ({percent:.0f}%)"


def copy_text(row: ModelRow) -> str:
    """What "Copy download info" puts on the clipboard.

    The repo and file, because the app is offline by design and the supported
    path is still fetching it yourself.
    """
    if not row:
        return ""
    lines = [row.cells[0]]
    if row.repo:
        lines.append(f"repo: {row.repo}")
    if row.filename:
        lines.append(f"file: {row.filename}")
    return "\n".join(lines)
