"""
council_core.backend_settings — the saved main model, vision projector and
engine settings in vault/backend_settings.json, and which main model a launch
starts on.

Moved out of onboarding.py, the retired Tk first-run wizard, when the Tk shell
was deleted (2026-10-23). The Qt app read and wrote these through that module;
the wizard itself was never ported (council_qt.launch shows a "Setup needed"
line instead), so only the helpers below came across, unchanged. The file
name, its keys and the precedence are the same, so a vault saved by any
earlier build reads exactly as before.
"""
from __future__ import annotations

import json
import os as _os
from pathlib import Path
from typing import Callable, Optional

_BACKEND_SETTINGS_FILENAME = "backend_settings.json"


def _backend_settings_path(vault_dir: Path) -> Path:
    return vault_dir / _BACKEND_SETTINGS_FILENAME


def _read_backend_settings(vault_dir: Path) -> Optional[dict]:
    """The backend settings dict; {} when the file does not exist; None when
    it exists but cannot be read as a JSON object (corrupt JSON, a list, not
    UTF-8, a folder of that name, a locked file).

    Read as utf-8-sig, which also accepts a leading BOM. MEASURED 2026-09-29:
    Windows PowerShell 5.1's `Set-Content -Encoding utf8` writes EF BB BF,
    and json.loads rejects the text that plain "utf-8" decodes from it
    ("Unexpected UTF-8 BOM"). The saved main model was then silently lost —
    a launch ran on the launcher's pick with no log line — and
    _merge_backend_settings, reading {}, wrote back ONLY the key it was
    saving: a save_clip_path turned {gguf_path, clip_path, role_models} into
    {clip_path}. A file without a BOM decodes exactly as before."""
    p = _backend_settings_path(vault_dir)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8-sig")
        # An empty or blank file holds nothing to lose, so it reads as {}.
        # Read as "unreadable" it blocked every later save until the user
        # deleted it by hand — and a write interrupted between truncate and
        # write used to leave exactly that 0-byte file.
        if not text.strip():
            return {}
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_backend_settings(vault_dir: Path) -> dict:
    """Read the entire backend settings dict, or return {} on any failure.
    Internal helper — callers use the typed accessors below."""
    return _read_backend_settings(vault_dir) or {}


def _merge_backend_settings(vault_dir: Path, **updates: str) -> Optional[str]:
    """Merge `updates` into the existing backend settings JSON without
    blowing away other keys. Atomic-style: read, mutate, write.

    Previously save_gguf_path() overwrote the entire file with
    `{"gguf_path": ...}` — that destroyed any sibling key like
    `clip_path`. The merge keeps every key the app may have
    written and only touches the ones the caller is updating.

    Returns None once the file is written, else one line saying why it was
    not — for a caller to show (the Models tab's Save does). A file that is
    there but cannot be read is LEFT AS IT IS: MEASURED 2026-09-29, read as
    {} it was rewritten with only the key being saved, so one save_clip_path
    into a UTF-16 file (PowerShell 5.1's Out-File default), a truncated one
    or a cp1252 one turned {gguf_path, clip_path, role_models} into
    {clip_path} — the saved main model and every role assignment gone
    without a word. The user can fix or delete a file they can still read;
    nobody can bring back one this function replaced.
    """
    path = _backend_settings_path(vault_dir)
    data = _read_backend_settings(vault_dir)
    if data is None:
        return (f"{path} could not be read (not a JSON object in UTF-8), so "
                "it was left as it is rather than replaced. Fix or delete "
                "it, then choose the model again.")
    for k, v in updates.items():
        if v is None or v == "":
            # Setting to empty/None means CLEAR the key entirely.
            data.pop(k, None)
        else:
            data[k] = v
    # Written to a temp file beside it and swapped in, so a process killed
    # mid-write leaves either the old file or the new one — never the
    # truncated file path.write_text leaves between its truncate and write.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Windows refuses the swap (WinError 5) while another process holds
        # the file open without share-delete — a second Council window, or
        # the engine reading it at that instant. write_text never hit that,
        # so a few short retries keep a save from failing where it used to
        # succeed; only a lasting lock is reported.
        for attempt in range(5):
            try:
                _os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                import time as _time
                _time.sleep(0.05 * (attempt + 1))
    except Exception as exc:                              # noqa: BLE001
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"could not write {path}: {exc}"
    return None


# ------------------------------------------------------------
# Which main model a launch starts on
# ------------------------------------------------------------
# MEASURED 2026-09-29: whatever main model the user picked in the Models
# tab or the old first-run wizard, the next launch started on granite-3.1-8b from
# ~/models. run-windows.bat, finding COUNCIL_GGUF_PATH unset, exports the
# FIRST *.gguf in <repo>\models then %USERPROFILE%\models (run-linux.sh /
# run-wsl.sh do the same with their own folders), and load_gguf_path let
# ANY env value beat the saved one. save_gguf_path's os.environ write lasts
# only until the process exits, so from the second launch on the launcher's
# first-found guess outranked the user's explicit choice — every time.
#
# The env var alone cannot say whether a person or a launcher set it, so
# the launchers now say so: COUNCIL_GGUF_PATH_AUTO=1, exported ONLY in
# their auto-pick branch (and cleared first, so it can never be inherited
# onto a path the user exported). The order is then:
#   1. COUNCIL_GGUF_PATH the user exported     — always wins
#   2. gguf_path saved in backend_settings.json — while the file is on disk
#   3. the launcher's auto-pick                 — first launch, or the saved
#                                                 file was deleted / moved
# A saved path that is gone falls back to the auto-pick rather than to
# nothing, because a model that loads beats an error naming a lost file.

#: Set to "1" by run-windows.bat / run-linux.sh / run-wsl.sh when THEY chose
#: COUNCIL_GGUF_PATH. Never set when the user exported it.
GGUF_AUTO_ENV = "COUNCIL_GGUF_PATH_AUTO"


def _flag_on(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_gguf_path(env_path: str, env_auto: bool, saved_path: str,
                      is_file: Callable[[str], bool] = _os.path.isfile
                      ) -> tuple:
    """The main-model precedence, with no I/O but ``is_file``.

    Returns ``(path, source)``. ``source`` is one of:
      "env"                 the user's own COUNCIL_GGUF_PATH
      "saved"               the saved path, which is on disk
      "auto"                the launcher's pick; nothing is saved
      "auto-saved-missing"  the launcher's pick; the saved file is gone
      "saved-missing"       no env value; the saved file is gone (the path
                            is still returned so an error can name it)
      "none"                nothing anywhere
    """
    env_path = (env_path or "").strip()
    saved_path = (saved_path or "").strip()
    if env_path and not env_auto:
        return env_path, "env"
    saved_ok = bool(saved_path) and is_file(saved_path)
    if saved_ok:
        return saved_path, "saved"
    if env_path:
        return env_path, ("auto-saved-missing" if saved_path else "auto")
    if saved_path:
        return saved_path, "saved-missing"
    return "", "none"


def _same_path(a: str, b: str) -> bool:
    norm = lambda p: _os.path.normcase(_os.path.abspath(p))  # noqa: E731
    try:
        return norm(a) == norm(b)
    except Exception:
        return a == b


def apply_saved_gguf_path(vault_dir: Optional[Path] = None,
                          environ=None,
                          log: Optional[Callable[[str], None]] = None) -> str:
    """Point COUNCIL_GGUF_PATH at the model this launch should start on.

    Called by the entry points (council_qt.py main) BEFORE council_engine is imported: the engine reads the
    env var to label its role slots at import and to load the model later,
    so a fix applied after the import would load the right file under the
    auto-pick's name in the log.

    ``vault_dir`` defaults to council_core.paths.vault_dir() — the same
    resolution the app itself uses ($COUNCIL_VAULT_ROOT, else
    $COUNCIL_APP_DIR/vault, else ~/.council/vault). Returns the ``source``
    from resolve_gguf_path, or "error". Whatever it decides, it removes
    COUNCIL_GGUF_PATH_AUTO: the marker describes the launch, not the value
    this process and its children then run on. ``log`` gets the one line
    worth printing (default: stdout, flushed so it lands in order with the
    engine's own startup lines). Never raises: a launch must not die over
    which model it would have preferred.
    """
    env = _os.environ if environ is None else environ
    message = ""
    try:
        if vault_dir is None:
            from council_core import paths as _paths
            vault_dir = _paths.vault_dir()
        settings = _read_backend_settings(Path(vault_dir))
        saved = str((settings or {}).get("gguf_path", "") or "").strip()
        current = str(env.get("COUNCIL_GGUF_PATH", "") or "").strip()
        auto = _flag_on(env.get(GGUF_AUTO_ENV))
        path, source = resolve_gguf_path(current, auto, saved)
        if source == "saved":
            if not _same_path(path, current):
                env["COUNCIL_GGUF_PATH"] = path
                message = (f"[startup] main model: {path} — the one saved in "
                           f"the app" + (f", not the launcher's pick "
                                         f"({current})" if current else ""))
        elif source in ("auto-saved-missing", "saved-missing"):
            # A folder (or anything else that is not a regular file) at the
            # saved path is not "gone", and telling the user it is sends them
            # looking for a deleted file that is still there.
            why = ("is not a file" if _os.path.exists(saved)
                   else "is no longer on disk")
            if source == "auto-saved-missing":
                message = (f"[startup] main model: {current} — the launcher's "
                           f"pick; the one saved in the app {why} ({saved})")
            else:
                message = (f"[startup] main model: the one saved in the app "
                           f"{why} ({saved}). Pick one in the Models tab.")
        elif settings is None and source in ("auto", "none"):
            # The file is there but unreadable, so there may well be a saved
            # choice this launch is ignoring — say so rather than start on
            # the guess in silence. (With source "env" the saved choice
            # would not have been used anyway.)
            where = _backend_settings_path(Path(vault_dir))
            message = (f"[startup] main model: could not read {where}; "
                       + (f"using the launcher's pick ({current})"
                          if source == "auto" else
                          "no main model is set. Pick one in the Models "
                          "tab."))
    except Exception as exc:                              # noqa: BLE001
        source = "error"
        message = f"[startup] could not apply the saved main model: {exc!r}"
    # Dropped for EVERY outcome, not only "saved": from here on the env
    # value IS the model this process runs on, whoever picked it. MEASURED
    # 2026-09-29: kept after "auto" / "auto-saved-missing" / an error, the
    # marker stayed in os.environ, so every child the app spawns inherited
    # COUNCIL_GGUF_PATH_AUTO=1 — a `python council_qt.py` started from that
    # environment treated the path it was handed as a guess and swapped in
    # the saved model — and once the saved file came back mid-session,
    # load_gguf_path named a different model from the one the engine had
    # loaded.
    try:
        env.pop(GGUF_AUTO_ENV, None)
    except Exception:                                     # noqa: BLE001
        pass
    if message:
        try:
            (log or (lambda m: print(m, flush=True)))(message)
        except Exception:                                 # noqa: BLE001
            pass          # a console that cannot print the path is not fatal
    return source


def load_gguf_path(vault_dir: Path) -> str:
    """Return the GGUF model path this process should use, or "".

    Same precedence as apply_saved_gguf_path: a COUNCIL_GGUF_PATH the user
    exported wins; one a launcher auto-picked (COUNCIL_GGUF_PATH_AUTO=1)
    loses to the path saved in vault/backend_settings.json while that file
    is on disk. With no env value the saved path is returned even if the
    file is gone, so the caller's error can name it.
    """
    path, _source = resolve_gguf_path(
        _os.environ.get("COUNCIL_GGUF_PATH", ""),
        _flag_on(_os.environ.get(GGUF_AUTO_ENV)),
        str(_load_backend_settings(vault_dir).get("gguf_path", "") or ""))
    return path


def save_gguf_path(vault_dir: Path, path: str) -> Optional[str]:
    """Persist the GGUF model path so the next launch picks it up.

    Also sets COUNCIL_GGUF_PATH in os.environ for the current process so
    the choice is live immediately — no app restart needed.
    Preserves any sibling keys (e.g. clip_path) already in the file.
    Clears COUNCIL_GGUF_PATH_AUTO: the env value is the user's choice now,
    not the launcher's guess.

    Returns None when it was saved, else why not (_merge_backend_settings).
    The choice is live in this process either way; only the next launch
    would miss it.
    """
    not_saved = _merge_backend_settings(vault_dir, gguf_path=path)
    _os.environ.pop(GGUF_AUTO_ENV, None)
    if path:
        _os.environ["COUNCIL_GGUF_PATH"] = path
    else:
        _os.environ.pop("COUNCIL_GGUF_PATH", None)
    return not_saved


def load_clip_path(vault_dir: Path) -> str:
    """Return the persisted vision mmproj (CLIP) path, or empty string.

    Same precedence as load_gguf_path: env var COUNCIL_GGUF_CLIP_PATH
    wins, then the persisted backend_settings.json key. The engine's
    _get_gguf_model checks the env var directly, so a save
    sets it too, for live effect within the same process.
    """
    env = _os.environ.get("COUNCIL_GGUF_CLIP_PATH", "").strip()
    if env:
        return env
    return str(_load_backend_settings(vault_dir).get("clip_path", "")).strip()


def save_clip_path(vault_dir: Path, path: str) -> Optional[str]:
    """Persist the vision mmproj path. Empty string clears it (text-only
    mode after a user had picked vision in a previous run).
    Returns None when it was saved, else why not (_merge_backend_settings).
    """
    not_saved = _merge_backend_settings(vault_dir, clip_path=path)
    if path:
        _os.environ["COUNCIL_GGUF_CLIP_PATH"] = path
    else:
        _os.environ.pop("COUNCIL_GGUF_CLIP_PATH", None)
    return not_saved


def clip_file_status(path: str) -> tuple:
    """Inspect a candidate mmproj .gguf path. Same shape as
    gguf_file_status: (ok: bool, message: str). The validation is the
    SAME — an mmproj IS a GGUF file (just a smaller one with vision
    encoder weights rather than language-model weights) — so the
    magic-byte check applies unchanged. We keep a separate name for
    clarity at call sites.
    """
    return gguf_file_status(path)


def gguf_file_status(path: str) -> tuple:
    """Inspect a candidate .gguf path. Returns (ok: bool, message: str).

    Beyond the extension check, we read the first 4 bytes and confirm
    they match the GGUF magic. This catches the most common "I downloaded
    it but it doesn't work" failure mode: a huggingface-cli download
    that was interrupted mid-stream and left a partial file (or an HTML
    error page) on disk with the .gguf extension. Without the magic
    check the user clicks Finish, the app tries to load the file, and
    llama-cpp crashes deep in C with no actionable error.
    """
    if not path:
        return False, "(no model selected)"
    p = Path(path)
    if not p.exists():
        return False, f"✗ File does not exist: {p}"
    if not p.is_file():
        return False, f"✗ Not a file: {p}"
    if p.suffix.lower() != ".gguf":
        return False, f"⚠ Not a .gguf extension: {p.name}"
    # Magic-byte check
    try:
        if p.stat().st_size < 1024:
            return False, (f"⚠ {p.name} is suspiciously small "
                            f"({p.stat().st_size} bytes) — likely a failed "
                            "download. Re-fetch the file.")
        with open(p, "rb") as fh:
            magic = fh.read(4)
        if magic != b"GGUF":
            return False, (f"⚠ {p.name} doesn't start with the GGUF magic "
                            f"bytes (got {magic!r}). Likely a corrupted or "
                            "partial download — re-fetch the file.")
    except Exception as exc:
        return False, f"⚠ Could not read {p.name}: {exc!r}"
    try:
        size_gb = p.stat().st_size / (1024 ** 3)
    except Exception:
        return True, f"✓ {p.name}"
    return True, f"✓ {p.name} ({size_gb:.1f} GB)"


def needs_onboarding(vault_dir: Path) -> bool:
    """True while vault/.onboarded is missing: first-run setup has not been
    completed or skipped."""
    return not (Path(vault_dir) / ".onboarded").exists()
