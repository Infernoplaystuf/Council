"""
The main model a launch starts on: the one saved in the app, not the
launcher's first-found guess.

MEASURED 2026-09-29: whatever main model the user picked in the Models tab,
the next launch started on granite-3.1-8b from ~/models. run-windows.bat
exports the first *.gguf it finds when COUNCIL_GGUF_PATH is unset, and
onboarding.load_gguf_path let any env value beat the saved one — so the
launcher's guess outranked the user's choice on every launch after the first.

The launchers now mark their guess with COUNCIL_GGUF_PATH_AUTO=1, and the
entry points (onboarding.apply_saved_gguf_path) let the saved model beat a
MARKED guess while the saved file is on disk. A path the user exported still
beats both.

Nothing here loads a model, touches a GPU or reads the real vault: every vault
is a tmp_path, the "models" are empty files, and the launcher runs below start
stub entry scripts that only write down the environment they were given.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import onboarding  # noqa: E402

AUTO = onboarding.GGUF_AUTO_ENV


@pytest.fixture
def clean_env(monkeypatch):
    """COUNCIL_GGUF_PATH / _AUTO absent, and put back exactly as they were.

    setenv-then-delenv on purpose: a bare delenv of an ABSENT variable records
    nothing, so a save_gguf_path in the test would leak its os.environ write
    into every later test."""
    for name in ("COUNCIL_GGUF_PATH", AUTO):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    return monkeypatch


def _gguf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF")
    return path


def _vault(tmp_path: Path, saved: str = None) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    if saved is not None:
        (vault / "backend_settings.json").write_text(
            json.dumps({"gguf_path": saved, "clip_path": "keep-me"}),
            encoding="utf-8")
    return vault


# ============================================================
# The precedence, as a pure function
# ============================================================

def test_an_exported_path_beats_the_saved_one(tmp_path):
    saved = _gguf(tmp_path / "saved.gguf")
    assert onboarding.resolve_gguf_path("C:/mine.gguf", False, str(saved)) \
        == ("C:/mine.gguf", "env")


def test_the_saved_path_beats_the_launchers_guess(tmp_path):
    saved = _gguf(tmp_path / "saved.gguf")
    assert onboarding.resolve_gguf_path("C:/guess.gguf", True, str(saved)) \
        == (str(saved), "saved")


def test_a_deleted_saved_file_falls_back_to_the_guess(tmp_path):
    gone = tmp_path / "deleted.gguf"
    assert onboarding.resolve_gguf_path("C:/guess.gguf", True, str(gone)) \
        == ("C:/guess.gguf", "auto-saved-missing")


def test_the_guess_stands_when_nothing_is_saved():
    assert onboarding.resolve_gguf_path("C:/guess.gguf", True, "") \
        == ("C:/guess.gguf", "auto")


def test_no_env_value_uses_the_saved_path(tmp_path):
    saved = _gguf(tmp_path / "saved.gguf")
    assert onboarding.resolve_gguf_path("", False, str(saved)) \
        == (str(saved), "saved")
    # The AUTO flag without a path is meaningless and changes nothing.
    assert onboarding.resolve_gguf_path("", True, str(saved)) \
        == (str(saved), "saved")


def test_no_env_value_and_a_deleted_saved_file_still_names_it(tmp_path):
    gone = tmp_path / "deleted.gguf"
    assert onboarding.resolve_gguf_path("", False, str(gone)) \
        == (str(gone), "saved-missing")


def test_nothing_anywhere():
    assert onboarding.resolve_gguf_path("", False, "") == ("", "none")
    assert onboarding.resolve_gguf_path("  ", True, "  ") == ("", "none")


# ============================================================
# apply_saved_gguf_path — what the entry points run before the engine
# ============================================================

def test_apply_replaces_the_guess_with_the_saved_model(tmp_path):
    saved = _gguf(tmp_path / "chosen.gguf")
    guess = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    env = {"COUNCIL_GGUF_PATH": str(guess), AUTO: "1"}
    lines = []
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(saved)), environ=env, log=lines.append)
    assert source == "saved"
    assert env["COUNCIL_GGUF_PATH"] == str(saved)
    # The value is the user's choice now; a child process must not treat it
    # as a guess and re-resolve it.
    assert AUTO not in env
    assert len(lines) == 1
    assert str(saved) in lines[0] and str(guess) in lines[0]


def test_apply_keeps_the_guess_when_the_saved_file_is_gone(tmp_path):
    guess = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    gone = tmp_path / "deleted.gguf"
    env = {"COUNCIL_GGUF_PATH": str(guess), AUTO: "1"}
    lines = []
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(gone)), environ=env, log=lines.append)
    assert source == "auto-saved-missing"
    # The guess stays — and is no longer marked as one (see
    # test_apply_drops_the_marker_whatever_it_decides).
    assert env == {"COUNCIL_GGUF_PATH": str(guess)}
    assert len(lines) == 1 and str(gone) in lines[0], (
        "the log must say WHY the saved model was not used")
    assert "no longer on disk" in lines[0]


def test_apply_never_overrides_an_exported_path(tmp_path):
    saved = _gguf(tmp_path / "chosen.gguf")
    env = {"COUNCIL_GGUF_PATH": "D:/mine.gguf"}
    lines = []
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(saved)), environ=env, log=lines.append)
    assert source == "env"
    assert env == {"COUNCIL_GGUF_PATH": "D:/mine.gguf"}
    assert lines == []


def test_apply_sets_the_saved_model_when_nothing_is_exported(tmp_path):
    """`python council_qt.py` with no launcher: nothing in the Qt build copied
    the saved path into the env, so the engine said "not set"."""
    saved = _gguf(tmp_path / "chosen.gguf")
    env = {}
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(saved)), environ=env, log=lambda _m: None)
    assert source == "saved"
    assert env == {"COUNCIL_GGUF_PATH": str(saved)}


def test_apply_does_not_point_the_env_at_a_missing_file(tmp_path):
    gone = tmp_path / "deleted.gguf"
    env = {}
    lines = []
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(gone)), environ=env, log=lines.append)
    assert source == "saved-missing"
    assert env == {}
    assert len(lines) == 1 and str(gone) in lines[0]


def test_apply_is_quiet_when_the_guess_already_is_the_saved_model(tmp_path):
    saved = _gguf(tmp_path / "chosen.gguf")
    env = {"COUNCIL_GGUF_PATH": str(saved), AUTO: "1"}
    lines = []
    assert onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(saved)), environ=env, log=lines.append) == "saved"
    assert env == {"COUNCIL_GGUF_PATH": str(saved)}
    assert lines == []


def test_apply_with_nothing_saved_leaves_the_guess_alone(tmp_path):
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"}
    lines = []
    assert onboarding.apply_saved_gguf_path(
        _vault(tmp_path), environ=env, log=lines.append) == "auto"
    assert env == {"COUNCIL_GGUF_PATH": "C:/guess.gguf"}
    assert lines == [], "no settings file is a first run, not a problem"


def _drop_vault_dir(monkeypatch):
    """council_core.paths.vault_dir() raising: the except path."""
    from council_core import paths

    def broken():
        raise RuntimeError("no vault")
    monkeypatch.setattr(paths, "vault_dir", broken)


@pytest.mark.parametrize("case, expected", [
    ("saved", "saved"),
    ("nothing-saved", "auto"),
    ("saved-deleted", "auto-saved-missing"),
    ("saved-deleted-no-env", "saved-missing"),
    ("nothing-anywhere", "none"),
    ("vault-unresolvable", "error"),
])
def test_apply_drops_the_marker_whatever_it_decides(tmp_path, monkeypatch,
                                                    case, expected):
    """MEASURED 2026-09-29: only "saved" removed COUNCIL_GGUF_PATH_AUTO. After
    "auto", "auto-saved-missing" or an error it stayed in os.environ, so every
    child the app spawned inherited it, and a `python council_qt.py` run from
    that environment treated the path it was handed as a launcher's guess.
    Once decided, the env value is what this process runs on — not a guess."""
    saved = _gguf(tmp_path / "chosen.gguf")
    gone = str(tmp_path / "deleted.gguf")
    vault = None
    if case != "vault-unresolvable":
        vault = _vault(tmp_path, {"saved": str(saved),
                                  "saved-deleted": gone,
                                  "saved-deleted-no-env": gone}.get(case))
    env = {AUTO: "1"}
    if case not in ("saved-deleted-no-env", "nothing-anywhere"):
        env["COUNCIL_GGUF_PATH"] = "C:/guess.gguf"
    if vault is None:
        _drop_vault_dir(monkeypatch)
    before = env.get("COUNCIL_GGUF_PATH")
    lines = []
    assert onboarding.apply_saved_gguf_path(
        vault, environ=env, log=lines.append) == expected
    assert AUTO not in env, f"{case}: the marker outlived the decision"
    if expected != "saved":
        assert env.get("COUNCIL_GGUF_PATH") == before, (
            "only the saved model may replace the value")


def test_apply_error_path_logs_and_drops_the_marker_on_os_environ(
        clean_env):
    """The default environ is os.environ itself, the one children inherit."""
    _drop_vault_dir(clean_env)
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    lines = []
    assert onboarding.apply_saved_gguf_path(log=lines.append) == "error"
    assert AUTO not in os.environ
    assert os.environ["COUNCIL_GGUF_PATH"] == "C:/guess.gguf"
    assert len(lines) == 1 and "no vault" in lines[0]


# ------------------------------------------------------------
# A settings file that is there but cannot be read
# ------------------------------------------------------------

def _settings_bytes(vault: Path, data: bytes) -> Path:
    path = vault / "backend_settings.json"
    path.write_bytes(data)
    return path


def test_apply_reads_a_settings_file_written_with_a_bom(tmp_path):
    """MEASURED 2026-09-29: Windows PowerShell 5.1's `Set-Content -Encoding
    utf8` writes EF BB BF first; read as plain utf-8, json.loads rejected the
    text and the saved model was lost without a word (source "auto")."""
    saved = _gguf(tmp_path / "chosen.gguf")
    vault = _vault(tmp_path)
    _settings_bytes(vault, b"\xef\xbb\xbf"
                    + json.dumps({"gguf_path": str(saved)}).encode("utf-8"))
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"}
    assert onboarding.apply_saved_gguf_path(
        vault, environ=env, log=lambda _m: None) == "saved"
    assert env == {"COUNCIL_GGUF_PATH": str(saved)}


def test_a_merge_into_a_bom_file_keeps_the_other_keys(tmp_path, monkeypatch):
    """The same BOM made _merge_backend_settings read {} and write back only
    its own key: MEASURED 2026-09-29, a save_clip_path turned {gguf_path,
    clip_path, role_models} into {clip_path}."""
    vault = _vault(tmp_path)
    keep = {"gguf_path": "C:/chosen.gguf", "clip_path": "C:/old-mmproj.gguf",
            "role_models": {"sage": "C:/sage.gguf"}}
    _settings_bytes(vault, b"\xef\xbb\xbf" + json.dumps(keep).encode("utf-8"))
    monkeypatch.setenv("COUNCIL_GGUF_CLIP_PATH", "x")
    onboarding.save_clip_path(vault, "C:/new-mmproj.gguf")
    data = json.loads((vault / "backend_settings.json").read_text("utf-8-sig"))
    assert data == dict(keep, clip_path="C:/new-mmproj.gguf")


_KEEP = {"gguf_path": "C:/chosen.gguf", "clip_path": "C:/old-mmproj.gguf",
         "role_models": {"sage": "C:/sage.gguf"}}


@pytest.mark.parametrize("content", [
    json.dumps(_KEEP).encode("utf-16"),      # PowerShell 5.1 Out-File / `>`
    json.dumps(_KEEP).encode("utf-8")[:-9],  # cut off mid-write
    json.dumps(_KEEP).replace("chosen", "caf\u00e9").encode("cp1252"),
    b"[1, 2]",                               # JSON, but not an object
], ids=["utf16", "truncated", "cp1252", "a-list"])
@pytest.mark.parametrize("which", ["gguf", "clip"])
def test_a_merge_never_replaces_a_file_it_could_not_read(tmp_path, clean_env,
                                                         content, which):
    """MEASURED 2026-09-29: _merge_backend_settings read such a file as {}
    and wrote back only the key it was saving — save_clip_path turned
    {gguf_path, clip_path, role_models} into {clip_path} for all four of
    these. The file is now left as it is and the caller is told why, with
    the path, so it can say so (the Models tab's Save does)."""
    vault = _vault(tmp_path)
    path = _settings_bytes(vault, content)
    clean_env.setenv("COUNCIL_GGUF_CLIP_PATH", "x")
    clean_env.delenv("COUNCIL_GGUF_CLIP_PATH")
    if which == "gguf":
        why = onboarding.save_gguf_path(vault, "C:/new.gguf")
    else:
        why = onboarding.save_clip_path(vault, "C:/new-mmproj.gguf")
    assert path.read_bytes() == content, "a file it could not read was replaced"
    assert why and str(path) in why and "left as it is" in why
    # Still the user's choice for THIS process; only the next launch misses it.
    if which == "gguf":
        assert os.environ["COUNCIL_GGUF_PATH"] == "C:/new.gguf"
        assert AUTO not in os.environ
    else:
        assert os.environ["COUNCIL_GGUF_CLIP_PATH"] == "C:/new-mmproj.gguf"


def test_a_merge_says_when_it_cannot_write(tmp_path, clean_env):
    """A write that failed used to be swallowed (`except: pass`) — the caller
    reported "saved" either way."""
    vault = _vault(tmp_path, "C:/old.gguf")
    path = vault / "backend_settings.json"
    os.chmod(path, stat.S_IREAD)                  # read-only on Windows too
    try:
        why = onboarding.save_gguf_path(vault, "C:/new.gguf")
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert why and why.startswith(f"could not write {path}:")
    assert json.loads(path.read_text("utf-8"))["gguf_path"] == "C:/old.gguf"


@pytest.mark.parametrize("content", [b"", b"  \r\n", b"\xef\xbb\xbf"],
                         ids=["empty", "blank", "bom-only"])
def test_an_empty_settings_file_is_saved_over(tmp_path, clean_env, content):
    """It holds nothing to lose. Read as unreadable, it refused every later
    save until the user deleted it by hand — and an interrupted write used to
    leave exactly that 0-byte file."""
    vault = _vault(tmp_path)
    path = _settings_bytes(vault, content)
    assert onboarding.save_gguf_path(vault, "C:/new.gguf") is None
    assert json.loads(path.read_text("utf-8")) == {"gguf_path": "C:/new.gguf"}


def test_a_merge_swaps_the_file_in_whole(tmp_path, clean_env, monkeypatch):
    """Written beside it and swapped in: a write that fails part-way leaves
    the old file whole, where path.write_text's truncate-then-write left an
    empty one, and no temp file is left behind."""
    vault = _vault(tmp_path, "C:/old.gguf")
    path = vault / "backend_settings.json"
    before = path.read_bytes()

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(onboarding._os, "replace", fail)
    why = onboarding.save_gguf_path(vault, "C:/new.gguf")
    assert why and "disk full" in why
    assert path.read_bytes() == before
    assert not list(vault.glob("*.tmp"))


@pytest.mark.parametrize("content", [
    b"{not json",
    b"[1, 2]",                               # JSON, but not an object
    b'{"gguf_path": "caf\xe9.gguf"}',        # cp1252, not UTF-8
], ids=["corrupt", "a-list", "not-utf8"])
def test_apply_says_when_the_settings_file_cannot_be_read(tmp_path, content):
    """The file is there, so there may be a saved choice this launch ignores:
    one line says so, instead of starting on the guess in silence."""
    vault = _vault(tmp_path)
    path = _settings_bytes(vault, content)
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"}
    lines = []
    assert onboarding.apply_saved_gguf_path(
        vault, environ=env, log=lines.append) == "auto"
    assert env == {"COUNCIL_GGUF_PATH": "C:/guess.gguf"}
    assert lines == [f"[startup] main model: could not read {path}; "
                     f"using the launcher's pick (C:/guess.gguf)"]


def test_an_unreadable_settings_file_with_no_pick_says_so_too(tmp_path):
    vault = _vault(tmp_path)
    path = _settings_bytes(vault, b"{not json")
    lines = []
    assert onboarding.apply_saved_gguf_path(
        vault, environ={}, log=lines.append) == "none"
    assert len(lines) == 1
    assert lines[0].startswith(f"[startup] main model: could not read {path};")
    assert "launcher's pick" not in lines[0], "there is no launcher's pick"


def test_an_unreadable_settings_file_is_irrelevant_to_an_exported_path(
        tmp_path):
    vault = _vault(tmp_path)
    _settings_bytes(vault, b"{not json")
    env = {"COUNCIL_GGUF_PATH": "D:/mine.gguf"}
    lines = []
    assert onboarding.apply_saved_gguf_path(
        vault, environ=env, log=lines.append) == "env"
    assert env == {"COUNCIL_GGUF_PATH": "D:/mine.gguf"} and lines == []


@pytest.mark.parametrize("with_guess", [True, False])
def test_a_saved_path_that_is_a_folder_is_not_called_deleted(tmp_path,
                                                            with_guess):
    """A folder at the saved path is still on disk; "no longer on disk" sent
    the user looking for a deleted file."""
    folder = tmp_path / "models" / "phi-4.gguf"
    folder.mkdir(parents=True)
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"} if with_guess else {}
    lines = []
    source = onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(folder)), environ=env, log=lines.append)
    assert source == ("auto-saved-missing" if with_guess else "saved-missing")
    assert len(lines) == 1 and str(folder) in lines[0]
    assert "is not a file" in lines[0]
    assert "no longer on disk" not in lines[0]


def test_apply_never_raises_even_when_logging_does(tmp_path):
    saved = _gguf(tmp_path / "chosen.gguf")
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"}

    def broken_console(_message):
        raise UnicodeEncodeError("cp1252", "x", 0, 1, "no")

    assert onboarding.apply_saved_gguf_path(
        _vault(tmp_path, str(saved)), environ=env,
        log=broken_console) == "saved"
    assert env == {"COUNCIL_GGUF_PATH": str(saved)}


def test_apply_finds_the_vault_the_way_the_app_does(tmp_path, monkeypatch):
    """No vault_dir: council_core.paths.vault_dir() — $COUNCIL_VAULT_ROOT here
    — not a path of its own. A second resolver is how the Qt build once read
    ~/council_vault while the real vault was ~/.council/vault."""
    saved = _gguf(tmp_path / "chosen.gguf")
    vault = _vault(tmp_path, str(saved))
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    from council_core import paths
    assert paths.vault_dir() == vault.resolve()
    env = {"COUNCIL_GGUF_PATH": "C:/guess.gguf", AUTO: "1"}
    assert onboarding.apply_saved_gguf_path(
        environ=env, log=lambda _m: None) == "saved"
    assert env["COUNCIL_GGUF_PATH"] == str(saved)


# ============================================================
# load_gguf_path / save_gguf_path agree with it
# ============================================================

def test_load_prefers_the_saved_model_over_a_marked_guess(tmp_path, clean_env):
    saved = _gguf(tmp_path / "chosen.gguf")
    vault = _vault(tmp_path, str(saved))
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    assert onboarding.load_gguf_path(vault) == str(saved)


def test_load_still_lets_an_exported_path_win(tmp_path, clean_env):
    saved = _gguf(tmp_path / "chosen.gguf")
    vault = _vault(tmp_path, str(saved))
    clean_env.setenv("COUNCIL_GGUF_PATH", "D:/mine.gguf")
    assert onboarding.load_gguf_path(vault) == "D:/mine.gguf"


def test_load_keeps_the_guess_when_the_saved_file_is_gone(tmp_path, clean_env):
    vault = _vault(tmp_path, str(tmp_path / "deleted.gguf"))
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    assert onboarding.load_gguf_path(vault) == "C:/guess.gguf"


def test_load_with_no_env_returns_the_saved_path(tmp_path, clean_env):
    gone = str(tmp_path / "deleted.gguf")
    assert onboarding.load_gguf_path(_vault(tmp_path, gone)) == gone
    assert onboarding.load_gguf_path(tmp_path / "empty-vault") == ""


def test_save_makes_the_env_value_the_users_choice(tmp_path, clean_env):
    """Picking a model in the app replaces the guess in this process too, and
    must drop the marker: the value is no longer the launcher's."""
    vault = _vault(tmp_path, "C:/old.gguf")
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    assert onboarding.save_gguf_path(vault, "C:/picked.gguf") is None, (
        "None means saved")
    assert os.environ["COUNCIL_GGUF_PATH"] == "C:/picked.gguf"
    assert AUTO not in os.environ
    data = json.loads((vault / "backend_settings.json").read_text("utf-8"))
    assert data == {"gguf_path": "C:/picked.gguf", "clip_path": "keep-me"}
    onboarding.save_gguf_path(vault, "")
    assert "COUNCIL_GGUF_PATH" not in os.environ and AUTO not in os.environ


# ============================================================
# The entry points apply it before the engine is imported
# ============================================================

def _code_lines(text: str):
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_the_qt_entry_applies_it_before_anything_else():
    source = (ROOT / "council_qt.py").read_text(encoding="utf-8")
    body = source[source.index("def main()"):source.index("def _shutdown")]
    code = "\n".join(_code_lines(body))
    assert "onboarding.apply_saved_gguf_path()" in code
    assert code.index("apply_saved_gguf_path") < code.index("council_qt.launch")


def _run_qt_main(monkeypatch) -> dict:
    """council_qt.py main() run for real, with council_qt.launch replaced by a
    fake that records the env it was launched with — no Qt, no window."""
    import importlib.util
    import types

    import branding

    seen = {}

    def launch(argv, register=None, shutdown=None):
        seen.update(path=os.environ.get("COUNCIL_GGUF_PATH"),
                    auto=os.environ.get(AUTO))
        return 0

    fake = types.ModuleType("council_qt.launch")
    fake.launch = launch
    monkeypatch.setitem(sys.modules, "council_qt.launch", fake)
    monkeypatch.setattr(branding, "set_app_user_model_id", lambda *a: True)
    # council_qt.py shares its name with the council_qt package, so it is
    # loaded from its file under another name.
    spec = importlib.util.spec_from_file_location("council_qt_entry_under_test",
                                                  ROOT / "council_qt.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.main() == 0
    return seen


def test_the_qt_entry_hands_launch_the_saved_model(tmp_path, clean_env):
    saved = _gguf(tmp_path / "chosen.gguf")
    clean_env.setenv("COUNCIL_VAULT_ROOT", str(_vault(tmp_path, str(saved))))
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    assert _run_qt_main(clean_env) == {"path": str(saved), "auto": None}


def test_the_qt_entry_drops_the_marker_when_onboarding_cannot_load(
        tmp_path, clean_env, capsys):
    """council_qt.py main()'s except path: apply_saved_gguf_path never ran,
    so nothing dropped COUNCIL_GGUF_PATH_AUTO and the app — and every child
    it spawns — was launched still marked as running on a guess. The launch
    goes on, on the launcher's pick, as before."""
    clean_env.setenv("COUNCIL_VAULT_ROOT", str(_vault(tmp_path)))
    clean_env.setenv("COUNCIL_GGUF_PATH", "C:/guess.gguf")
    clean_env.setenv(AUTO, "1")
    # None in sys.modules: `import onboarding` raises ImportError.
    clean_env.setitem(sys.modules, "onboarding", None)
    assert _run_qt_main(clean_env) == {"path": "C:/guess.gguf", "auto": None}
    assert "[startup] saved main model not applied" in capsys.readouterr().out


def test_the_tk_entry_applies_it_before_council_engine_is_imported():
    source = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    code = _code_lines(source)
    at_engine = code.index("import council_engine as ce")
    apply_at = next(i for i, ln in enumerate(code)
                    if "apply_saved_gguf_path()" in ln)
    assert apply_at < at_engine
    guard = next(i for i in range(apply_at, -1, -1)
                 if code[i].startswith("if "))
    assert code[guard].strip() == 'if __name__ == "__main__":', (
        "importing council_gui_engine (tests do) must not rewrite the env")


# ============================================================
# The launchers mark their guess — and only their guess
# ============================================================

def test_the_bat_sets_auto_only_with_its_own_pick():
    raw = (ROOT / "run-windows.bat").read_bytes()
    # cmd finds `goto :do_check` labels by seeking; an LF-only .bat mis-parses.
    assert raw.count(b"\n") == raw.count(b"\r\n"), "run-windows.bat lost CRLF"
    lines = raw.decode("utf-8").splitlines()
    code = [(i, ln) for i, ln in enumerate(lines)
            if not ln.lstrip().upper().startswith("REM")]
    sets = [i for i, ln in code if "COUNCIL_GGUF_PATH_AUTO=1" in ln]
    clears = [i for i, ln in code if 'set "COUNCIL_GGUF_PATH_AUTO="' in ln]
    assert len(sets) == 1 and len(clears) == 1
    # Set on the one line that hands the pick out, and only when there is a
    # pick: the whole `set ... & set ...` is the IF's command.
    assert lines[sets[0]].strip() == (
        'endlocal & if not "%GGUF_PICK%"=="" '
        'set "COUNCIL_GGUF_PATH=%GGUF_PICK%" & set "COUNCIL_GGUF_PATH_AUTO=1"')
    # The search itself only runs while the user has set no path.
    searches = [i for i, ln in code if "*.gguf" in ln]
    assert len(searches) == 2
    for s in searches:
        assert lines[s].startswith("if not defined COUNCIL_GGUF_PATH ")
        assert clears[0] < s < sets[0]
    check_jump = next(i for i, ln in code if "goto :do_check" in ln)
    assert check_jump < clears[0], "--check must still exit before the pick"


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_the_sh_launchers_set_auto_only_in_their_auto_pick(name):
    raw = (ROOT / name).read_bytes()
    assert b"\r" not in raw, f"{name} must keep LF line endings"
    text = raw.decode("utf-8")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    exports = [i for i, ln in enumerate(code)
               if "COUNCIL_GGUF_PATH_AUTO=1" in ln]
    assert len(exports) == 1
    e = exports[0]
    assert code[e].strip() == "export COUNCIL_GGUF_PATH_AUTO=1"
    assert code[e - 1].strip() == 'export COUNCIL_GGUF_PATH="$candidate"'
    start = next(i for i, ln in enumerate(code)
                 if ln.strip() == 'if [ -z "${COUNCIL_GGUF_PATH:-}" ]; then')
    end = next(i for i in range(start, len(code)) if code[i].strip() == "fi"
               and code[i].startswith("fi"))
    assert start < e < end
    unset = [i for i, ln in enumerate(code)
             if ln.strip() == "unset COUNCIL_GGUF_PATH_AUTO"]
    assert unset and unset[0] < start


# ============================================================
# The launchers, run for real — with stub entry scripts
# ============================================================
# The real launcher text runs; the "app" it starts is a stub that appends the
# environment it was given to a file and exits with $STUB_EXIT (default 0) —
# or, on the launcher's CPU retry (GPU layers 0), with $STUB_EXIT_CPU when that
# is set. No app, no model, no GPU; a "crash" is only its exit code.
# On a GPU run (layers not 0) it can also do to $STUB_SENTINEL what the engine
# does to its GPU-crash sentinel: write it, as before a GPU load
# (council_engine._gpu_mark_attempt), and with $STUB_ANSWERED delete it again,
# as after the first answer (_gpu_confirm_success). With $STUB_SLEEP it then
# waits to be killed, having written its pid to $STUB_OUT.pid.

_STUB_ENTRY = """\
import json, os, sys, time
keys = ("COUNCIL_GGUF_PATH", "COUNCIL_GGUF_PATH_AUTO", "COUNCIL_BACKEND",
        "COUNCIL_GGUF_GPU_LAYERS")
out = {k: os.environ.get(k) for k in keys}
out["entry"] = os.path.basename(sys.argv[-1] if len(sys.argv) > 1 else sys.argv[0])
with open(os.environ["STUB_OUT"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(out) + "\\n")
on_gpu = os.environ.get("COUNCIL_GGUF_GPU_LAYERS") != "0"
sentinel = os.environ.get("STUB_SENTINEL")
if sentinel and on_gpu:
    os.makedirs(os.path.dirname(sentinel), exist_ok=True)
    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write("n_gpu_layers=99\\n")
    if os.environ.get("STUB_ANSWERED"):
        os.remove(sentinel)
if os.environ.get("STUB_SLEEP") and on_gpu:
    with open(os.environ["STUB_OUT"] + ".pid.tmp", "w") as fh:
        fh.write(str(os.getpid()))
    os.replace(os.environ["STUB_OUT"] + ".pid.tmp", os.environ["STUB_OUT"] + ".pid")
    time.sleep(60)
code = os.environ.get("STUB_EXIT") or 0
if os.environ.get("COUNCIL_GGUF_GPU_LAYERS") == "0" and os.environ.get("STUB_EXIT_CPU"):
    code = os.environ["STUB_EXIT_CPU"]
sys.exit(int(code))
"""
#: The same stub, but it first does what council_qt.py main() does — so a
#: launcher run shows which model the app would really start on.
_APPLY_ENTRY = _STUB_ENTRY.replace(
    "import json, os, sys, time\n",
    "import json, os, sys, time\n"
    "sys.path.insert(0, os.environ['STUB_REPO'])\n"
    "import onboarding\n"
    "onboarding.apply_saved_gguf_path()\n", 1)
assert _APPLY_ENTRY != _STUB_ENTRY
_STUB_GPU_CHECK = """\
import os
open(os.environ["STUB_OUT"] + ".gpu_check", "w").close()
"""
#: Never inherited by a launcher under test.
_SCRUB = ("COUNCIL_GGUF_PATH", AUTO, "COUNCIL_UI", "COUNCIL_PYTHON",
          "COUNCIL_SKIP_GPU_CHECK", "COUNCIL_GGUF_GPU_LAYERS", "BASH_ENV",
          "ENV", "STUB_EXIT", "STUB_EXIT_CPU", "GGUF_PICK", "COUNCIL_APP_DIR",
          "STUB_SENTINEL", "STUB_ANSWERED", "STUB_SLEEP")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _base_env(**extra) -> dict:
    """The test's environment minus _SCRUB, plus ``extra`` — where a value of
    None REMOVES the variable (to launch with it unset)."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _SCRUB}
    for k, v in extra.items():
        if v is None:
            for name in [n for n in env if n.upper() == k.upper()]:
                del env[name]
        else:
            env[k] = str(v)
    return env


def _runs(out: Path) -> list:
    assert out.is_file(), "the launcher never reached its entry script"
    return [json.loads(ln) for ln in
            out.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _read(out: Path) -> dict:
    runs = _runs(out)
    assert len(runs) == 1, f"the entry ran {len(runs)} times"
    return runs[0]


win_only = pytest.mark.skipif(os.name != "nt", reason="cmd.exe launcher")


def _prepare_bat(tmp_path: Path, *args, entry_src: str = _STUB_ENTRY,
                 **env_extra):
    """(argv, subprocess kwargs, entry.json) for one launcher run."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "run-windows.bat", app / "run-windows.bat")
    # The marker is checked first, so the launcher uses THIS interpreter and
    # never goes looking for conda.
    (app / ".council_python").write_text(sys.executable + "\n",
                                         encoding="utf-8")
    for entry in ("council_qt.py", "council_gui_engine.py"):
        (app / entry).write_text(entry_src, encoding="utf-8")
    (app / "gpu_check.py").write_text(_STUB_GPU_CHECK, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    out = tmp_path / "entry.json"
    env_extra.setdefault("COUNCIL_VAULT_ROOT", tmp_path / "vault")
    extra = dict(USERPROFILE=home, COUNCIL_SKIP_GPU_CHECK="1", STUB_OUT=out,
                 STUB_REPO=ROOT)
    extra.update(env_extra)
    # stdin closed: after a child exits 0xC000013A (Ctrl+C) cmd asks
    # "Terminate batch job (Y/N)?" and would otherwise wait on the keyboard
    # for good (measured 2026-09-29: a hang, until killed).
    kwargs = dict(cwd=str(app), env=_base_env(**extra), text=True,
                  errors="replace", creationflags=_NO_WINDOW,
                  stdin=subprocess.DEVNULL)
    return ["cmd.exe", "/d", "/c", str(app / "run-windows.bat"), *args], \
        kwargs, out


def _run_bat(tmp_path: Path, *args, entry_src: str = _STUB_ENTRY,
             **env_extra):
    argv, kwargs, out = _prepare_bat(tmp_path, *args, entry_src=entry_src,
                                     **env_extra)
    proc = subprocess.run(argv, capture_output=True, timeout=180, **kwargs)
    return proc, out


@win_only
def test_bat_marks_a_guess_from_userprofile_models(tmp_path):
    guess = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    proc, out = _run_bat(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(guess)
    assert got["COUNCIL_GGUF_PATH_AUTO"] == "1"
    assert got["COUNCIL_BACKEND"] == "gguf"
    assert "first .gguf found" in proc.stdout


@win_only
def test_bat_marks_a_guess_from_the_repo_models_folder(tmp_path):
    guess = _gguf(tmp_path / "app" / "models" / "phi-4.gguf")
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    proc, out = _run_bat(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(guess)
    assert got["COUNCIL_GGUF_PATH_AUTO"] == "1"


@win_only
def test_bat_never_marks_a_path_the_user_set(tmp_path):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    mine = _gguf(tmp_path / "elsewhere" / "mine.gguf")
    # A stray AUTO=1 in the calling shell must not turn it into a guess.
    proc, out = _run_bat(tmp_path, COUNCIL_GGUF_PATH=mine,
                         COUNCIL_GGUF_PATH_AUTO="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(mine)
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None
    assert "first .gguf found" not in proc.stdout


@win_only
def test_bat_with_no_model_anywhere_sets_neither(tmp_path):
    proc, out = _run_bat(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] is None
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None


# The engine's GPU-crash sentinel, which the launcher reads back before a CPU
# retry: see _engine_sentinel_path and the tests below.
_SENTINEL_NAME = ".gpu_attempt"


def _engine_sentinel_path(monkeypatch, **env) -> Path:
    """Where council_engine._gpu_sentinel_path() puts the GPU-crash sentinel
    under ``env`` (None unsets a variable).

    The engine's OWN function, lifted out of council_engine.py with ast and
    run on its own: importing the engine preloads CUDA DLLs
    (_windll_bootstrap), and a copy of its rule here would go on passing
    after the engine had moved the file."""
    tree = ast.parse((ROOT / "council_engine.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef)
                  and n.name == "_gpu_sentinel_path")
              or (isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name)
                          and t.id == "_GPU_SENTINEL_NAME" for t in n.targets))]
    assert len(wanted) == 2, "council_engine's sentinel code has moved"
    ns = {"os": os, "Path": Path}
    exec(compile(ast.Module(body=wanted, type_ignores=[]),
                 str(ROOT / "council_engine.py"), "exec"), ns)
    with monkeypatch.context() as m:
        for k, v in env.items():
            if v is None:
                m.delenv(k, raising=False)
            else:
                m.setenv(k, str(v))
        return Path(ns["_gpu_sentinel_path"]())


def _crash_bat(tmp_path, monkeypatch, *args, **env_extra):
    """_run_bat whose stub, on its GPU run, writes the engine's sentinel
    exactly where the engine would under the launcher's environment — a
    GPU load still in flight when it exits. STUB_SENTINEL=None: no sentinel."""
    env_extra.setdefault("COUNCIL_VAULT_ROOT", tmp_path / "vault")
    if "STUB_SENTINEL" not in env_extra:
        env_extra["STUB_SENTINEL"] = _engine_sentinel_path(
            monkeypatch, USERPROFILE=tmp_path / "home",
            COUNCIL_VAULT_ROOT=env_extra["COUNCIL_VAULT_ROOT"])
    return _run_bat(tmp_path, *args, **env_extra)


def _layers(out: Path) -> list:
    return [r["COUNCIL_GGUF_GPU_LAYERS"] for r in _runs(out)]


@win_only
def test_bat_a_crashed_run_is_retried_once_on_cpu(tmp_path, monkeypatch):
    """The retry block never ran: `(CPU only)` in an echo inside it closed
    the block early, cmd rejected it with "... was unexpected at this time."
    and the launcher exited 255 after every run, clean or not."""
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    proc, out = _crash_bat(tmp_path, monkeypatch, STUB_EXIT="3")
    assert "was unexpected" not in proc.stderr, proc.stderr
    assert _layers(out) == ["99", "0"]
    assert proc.returncode == 3
    assert "Retrying once with COUNCIL_GGUF_GPU_LAYERS=0 (CPU only)..." \
        in proc.stdout


# What ERRORLEVEL shows for a native crash on Windows: the NTSTATUS as a
# signed 32-bit number (measured 2026-09-29: sys.exit(-1073741819) in the
# entry -> ERRORLEVEL -1073741819, and the launcher's own exit code, read by
# subprocess, 3221225477 = 0xC0000005).
_ACCESS_VIOLATION = -1073741819       # 0xC0000005
_ILLEGAL_INSTRUCTION = -1073741795    # 0xC000001D
_FAST_FAIL = -1073740791              # 0xC0000409, the CRT's abort()
_CTRL_C = -1073741510                 # 0xC000013A, unhandled KeyboardInterrupt
_STOP_PROCESS = -1                    # 0xFFFFFFFF, PowerShell's Stop-Process


def _u32(code: int) -> int:
    """A Windows exit code as subprocess reports it (unsigned)."""
    return code & 0xFFFFFFFF


@win_only
@pytest.mark.parametrize("gpu_exit, cpu_exit, retried", [
    (0, None, False),                  # a clean run
    (1, None, False),                  # a Python exception at startup; taskkill /F
    (2, None, False),
    (_CTRL_C, None, False),            # the user pressed Ctrl+C
    (_STOP_PROCESS, None, False),      # the user ended it from PowerShell
    (-1073741825, None, False),        # 0xBFFFFFFF, just below the range
    (-1073676288, None, False),        # 0xC0010000, facility 1, just above it
    (_ACCESS_VIOLATION, 0, True),
    (_ACCESS_VIOLATION, _ACCESS_VIOLATION, True),
    (_ILLEGAL_INSTRUCTION, 0, True),
    (_FAST_FAIL, 0, True),
    (-1073741824, 0, True),            # 0xC0000000, the range's first code
    (-1073676289, 0, True),            # 0xC000FFFF, its last
    (3, 0, True),                      # abort() / GGML_ABORT
], ids=["clean", "exit-1", "exit-2", "ctrl-c", "stop-process",
        "0xBFFFFFFF", "0xC0010000", "access-violation", "crashes-on-cpu-too",
        "illegal-instruction", "fast-fail", "0xC0000000", "0xC000FFFF",
        "abort"])
def test_bat_retries_on_the_cpu_only_after_a_native_crash(
        tmp_path, monkeypatch, gpu_exit, cpu_exit, retried):
    """MEASURED 2026-09-29: once the retry block parsed, it relaunched the app
    on the CPU after ANY non-zero exit — exit 1 from a Python exception at
    startup, and exit 1 from taskkill /F, so a user closing a stuck app saw
    it open again — and then after ANY negative one: PowerShell's
    Stop-Process leaves 0xFFFFFFFF, ERRORLEVEL -1, and the app reopened on
    the CPU. It now retries only on an NTSTATUS error of facility 0
    (0xC0000000-0xC000FFFF, less Ctrl+C's 0xC000013A) or 3, as run-linux.sh
    and run-wsl.sh retry only on signals 132-139, and the launcher exits with
    the code of the run that ended last. Every run here leaves the GPU
    sentinel behind, so the exit code alone decides."""
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    extra = {"STUB_EXIT": str(gpu_exit)}
    if cpu_exit is not None:
        extra["STUB_EXIT_CPU"] = str(cpu_exit)
    proc, out = _crash_bat(tmp_path, monkeypatch, **extra)
    assert "was unexpected" not in proc.stderr, proc.stderr
    if retried:
        assert _layers(out) == ["99", "0"], proc.stdout
        assert "(CPU only)" in proc.stdout
        assert proc.returncode == _u32(cpu_exit)
    else:
        assert _layers(out) == ["99"], proc.stdout
        assert "Retrying" not in proc.stdout
        assert proc.returncode == _u32(gpu_exit)


@win_only
def test_bat_does_not_retry_a_crash_that_was_already_on_the_cpu(tmp_path,
                                                                monkeypatch):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    # A sentinel left by an earlier run: only the layers stop the retry here.
    _gguf(tmp_path / "vault" / _SENTINEL_NAME)
    proc, out = _crash_bat(tmp_path, monkeypatch,
                           STUB_EXIT=str(_ACCESS_VIOLATION),
                           COUNCIL_GGUF_GPU_LAYERS="0")
    assert _layers(out) == ["0"]
    assert proc.returncode == _u32(_ACCESS_VIOLATION)


@win_only
@pytest.mark.parametrize("code", [_ACCESS_VIOLATION, 3],
                         ids=["access-violation", "abort"])
@pytest.mark.parametrize("sentinel", ["cleared-by-an-answer", "never-written"])
def test_bat_does_not_reopen_the_app_after_a_crash_with_no_gpu_load_pending(
        tmp_path, monkeypatch, code, sentinel):
    """A native crash while CLOSING — the model had answered, so the engine
    had deleted its sentinel (council_engine._gpu_confirm_success) — is not
    the GPU failing. MEASURED 2026-09-29 before the launcher read the
    sentinel: all four cases here ran ["99", "0"], the app the user had just
    closed reopening on the CPU."""
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    extra = {"STUB_EXIT": str(code), "STUB_EXIT_CPU": "0"}
    if sentinel == "cleared-by-an-answer":
        extra["STUB_ANSWERED"] = "1"
    else:
        extra["STUB_SENTINEL"] = None
    proc, out = _crash_bat(tmp_path, monkeypatch, **extra)
    assert _layers(out) == ["99"], proc.stdout
    assert "Retrying" not in proc.stdout
    assert "no GPU load was waiting to be confirmed" in proc.stdout
    assert proc.returncode == _u32(code)
    assert not (tmp_path / "vault" / _SENTINEL_NAME).exists()


def test_the_bat_looks_for_the_sentinel_where_the_engine_writes_it(
        tmp_path, monkeypatch):
    """The launcher decides on a CPU retry by reading the engine's sentinel
    back, so it must look where council_engine._gpu_sentinel_path writes it:
    COUNCIL_VAULT_ROOT, else USERPROFILE\\.council\\vault. That function,
    unlike council_core.paths.vault_dir, ignores COUNCIL_APP_DIR, so the
    launcher looks under both when only COUNCIL_APP_DIR is set. If this
    fails, the engine moved its sentinel: move the launcher's `if exist`
    lines with it."""
    home, vault, app_dir = (tmp_path / "home", tmp_path / "vault",
                            tmp_path / "appdir")
    assert _engine_sentinel_path(
        monkeypatch, USERPROFILE=home, COUNCIL_VAULT_ROOT=vault,
        COUNCIL_APP_DIR=app_dir) == vault / _SENTINEL_NAME
    assert _engine_sentinel_path(
        monkeypatch, USERPROFILE=home, COUNCIL_VAULT_ROOT=None,
        COUNCIL_APP_DIR=None) == home / ".council" / "vault" / _SENTINEL_NAME
    assert _engine_sentinel_path(
        monkeypatch, USERPROFILE=home, COUNCIL_VAULT_ROOT=None,
        COUNCIL_APP_DIR=app_dir) in (
            home / ".council" / "vault" / _SENTINEL_NAME,
            app_dir / "vault" / _SENTINEL_NAME)
    code = [ln.strip() for ln in
            (ROOT / "run-windows.bat").read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().upper().startswith("REM")]
    for probe in ('if exist "!COUNCIL_VAULT_ROOT!\\.gpu_attempt"',
                  'if exist "!USERPROFILE!\\.council\\vault\\.gpu_attempt"',
                  'if exist "!COUNCIL_APP_DIR!\\vault\\.gpu_attempt"'):
        assert any(probe in ln for ln in code), probe


@win_only
@pytest.mark.parametrize("where", [
    "vault-root", "no-vault-root", "app-dir-only", "odd-folder-name",
    "trailing-backslash"])
def test_bat_finds_the_sentinel_where_the_engine_puts_it(tmp_path,
                                                         monkeypatch, where):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    env = {"COUNCIL_VAULT_ROOT": tmp_path / "vault"}
    if where == "no-vault-root":
        env["COUNCIL_VAULT_ROOT"] = None
    elif where == "app-dir-only":
        env.update(COUNCIL_VAULT_ROOT=None, COUNCIL_APP_DIR=tmp_path / "appdir")
    elif where == "odd-folder-name":
        # What cmd would mangle through %VAR% or a second ! pass.
        env["COUNCIL_VAULT_ROOT"] = tmp_path / "v!a^u&l)t (1) %OS%"
    elif where == "trailing-backslash":
        env["COUNCIL_VAULT_ROOT"] = str(tmp_path / "vault") + "\\"
    sentinel = _engine_sentinel_path(
        monkeypatch, USERPROFILE=tmp_path / "home",
        COUNCIL_VAULT_ROOT=env["COUNCIL_VAULT_ROOT"],
        COUNCIL_APP_DIR=env.get("COUNCIL_APP_DIR"))
    proc, out = _crash_bat(tmp_path, monkeypatch, STUB_SENTINEL=sentinel,
                           STUB_EXIT=str(_ACCESS_VIOLATION),
                           STUB_EXIT_CPU="0", **env)
    assert sentinel.is_file()
    assert _layers(out) == ["99", "0"], proc.stdout + proc.stderr
    assert proc.returncode == 0


@win_only
@pytest.mark.parametrize("case", ["another-vaults-sentinel", "no-vault-at-all"])
def test_bat_does_not_guess_where_the_sentinel_is(tmp_path, monkeypatch, case):
    """A sentinel the engine would not have written is not this launch's;
    and with neither COUNCIL_VAULT_ROOT nor USERPROFILE there is no vault to
    look in, so no retry — the safe direction: a GPU crash then waits for the
    next launch, which the engine itself puts on the CPU."""
    stray = _gguf(tmp_path / "home" / ".council" / "vault" / _SENTINEL_NAME)
    model = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    env = {"STUB_SENTINEL": None, "STUB_EXIT": str(_ACCESS_VIOLATION),
           "STUB_EXIT_CPU": "0"}
    if case == "no-vault-at-all":
        # COUNCIL_GGUF_PATH set so the pick does not search \models on the
        # drive root for a missing USERPROFILE.
        env.update(COUNCIL_VAULT_ROOT=None, USERPROFILE=None,
                   COUNCIL_GGUF_PATH=model)
    proc, out = _crash_bat(tmp_path, monkeypatch, **env)
    assert stray.is_file()
    assert _layers(out) == ["99"], proc.stdout
    assert "no GPU load was waiting to be confirmed" in proc.stdout


@win_only
@pytest.mark.parametrize("killer", ["Stop-Process", "taskkill /F"])
def test_bat_does_not_reopen_an_app_the_user_killed(tmp_path, monkeypatch,
                                                    killer):
    """Killed during a GPU load — the sentinel IS on disk — and still not a
    crash. MEASURED 2026-09-29: PowerShell's Stop-Process (.NET
    Process.Kill) leaves exit code 0xFFFFFFFF, ERRORLEVEL -1, and the
    launcher's "any negative exit" test reopened the app on the CPU
    (["99", "0"]); taskkill /F leaves 1."""
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    argv, kwargs, out = _prepare_bat(
        tmp_path, COUNCIL_VAULT_ROOT=tmp_path / "vault", STUB_SLEEP="1",
        STUB_SENTINEL=_engine_sentinel_path(
            monkeypatch, USERPROFILE=tmp_path / "home",
            COUNCIL_VAULT_ROOT=tmp_path / "vault"))
    pid_file = Path(str(out) + ".pid")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kwargs)
    try:
        deadline = time.monotonic() + 60
        while not pid_file.is_file():
            assert proc.poll() is None, proc.communicate()[0]
            assert time.monotonic() < deadline, "the stub never started"
            time.sleep(0.05)
        pid = pid_file.read_text().strip()
        kill = (["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-Command", f"Stop-Process -Id {pid} -Force"]
                if killer == "Stop-Process" else
                ["taskkill", "/F", "/PID", pid])
        subprocess.run(kill, capture_output=True, timeout=60,
                       creationflags=_NO_WINDOW)
        stdout, _ = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_NO_WINDOW)
            proc.wait(30)
    assert (tmp_path / "vault" / _SENTINEL_NAME).is_file()
    assert _layers(out) == ["99"], stdout
    assert "Retrying" not in stdout
    assert proc.returncode == (_u32(_STOP_PROCESS)
                               if killer == "Stop-Process" else 1), stdout


@win_only
@pytest.mark.parametrize("name", [
    "wow!model.gguf",
    "two!!bangs.gguf",
    "a^caret.gguf",
    "a^caret!and-bang.gguf",
    "phi-4 (q4) & more.gguf",
    "100%OS%.gguf",
])
def test_bat_exports_an_auto_picked_name_exactly(tmp_path, name):
    """MEASURED 2026-09-29: with delayed expansion on during the search, a !
    in the found name was dropped — wow!model.gguf was exported as
    wowmodel.gguf, a file that does not exist. The other names are the
    characters cmd could plausibly mangle on the way out of the search."""
    guess = _gguf(tmp_path / "home" / "models" / name)
    proc, out = _run_bat(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(guess)
    assert got["COUNCIL_GGUF_PATH_AUTO"] == "1"
    assert f"model: {guess}" in proc.stdout


@win_only
def test_bat_check_mode_still_launches_nothing(tmp_path):
    """--check jumps to gpu_check BEFORE the pick; it must not start the app."""
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    proc, out = _run_bat(tmp_path, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not out.exists(), "--check started the app"
    assert Path(str(out) + ".gpu_check").exists(), "--check skipped gpu_check"


def _git_bash():
    """Git's bash on Windows — never System32\\bash.exe or the WindowsApps
    alias, which start a WSL distro rather than a shell."""
    if os.name != "nt":
        return shutil.which("bash")
    for base in {os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"),
                 r"C:\Program Files"}:
        if base:
            cand = Path(base) / "Git" / "bin" / "bash.exe"
            if cand.is_file():
                return str(cand)
    return None


_CONDA_STUB = """\
conda() { return 0; }
python() {
    if [ "${1:-}" = "-c" ]; then return 0; fi
    "$STUB_PY" "$STUB_ENTRY" "$@"
}
"""


def _run_sh(tmp_path: Path, name: str, entry_src: str = _STUB_ENTRY,
            **env_extra):
    bash = _git_bash()
    if not bash:
        pytest.skip("no bash (Git for Windows) to run the launcher with")
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / name, app / name)
    home = tmp_path / "home"
    profile = home / "miniforge3" / "etc" / "profile.d"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "conda.sh").write_bytes(_CONDA_STUB.encode("utf-8"))
    entry = tmp_path / "stub_entry.py"
    entry.write_text(entry_src, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    out = tmp_path / "entry.json"
    env_extra.setdefault("COUNCIL_VAULT_ROOT", tmp_path / "vault")
    env = _base_env(HOME=home, USER="council-test", DISPLAY=":0",
                    STUB_PY=Path(sys.executable).as_posix(),
                    STUB_ENTRY=entry.as_posix(), STUB_OUT=out.as_posix(),
                    STUB_REPO=ROOT.as_posix(), **env_extra)
    proc = subprocess.run(
        [bash, (app / name).as_posix()], cwd=str(work), env=env,
        capture_output=True, text=True, errors="replace", timeout=120,
        creationflags=_NO_WINDOW)
    return proc, out


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_sh_marks_its_guess(tmp_path, name):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    proc, out = _run_sh(tmp_path, name)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert Path(got["COUNCIL_GGUF_PATH"]).name == "granite-3.1-8b.gguf"
    assert got["COUNCIL_GGUF_PATH_AUTO"] == "1"
    assert got["entry"] == "council_qt.py"


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_sh_never_marks_a_path_the_user_set(tmp_path, name):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    mine = _gguf(tmp_path / "elsewhere" / "mine.gguf")
    proc, out = _run_sh(tmp_path, name, COUNCIL_GGUF_PATH=mine.as_posix(),
                        COUNCIL_GGUF_PATH_AUTO="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert Path(got["COUNCIL_GGUF_PATH"]).name == "mine.gguf"
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None


# ============================================================
# End to end: launcher, then what the entry point does
# ============================================================
# The measured bug, replayed: a model saved in the app, a different .gguf in
# the folder the launcher searches, and a fresh launch. The stub entry runs the
# real onboarding.apply_saved_gguf_path against a tmp vault, then writes down
# the COUNCIL_GGUF_PATH the engine would have loaded.

@win_only
def test_a_bat_launch_starts_on_the_saved_model(tmp_path):
    guess = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    chosen = _gguf(tmp_path / "picked" / "phi-4.gguf")
    _vault(tmp_path, str(chosen))
    proc, out = _run_bat(tmp_path, entry_src=_APPLY_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(chosen), (
        "the launcher's guess still beats the saved model")
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None
    assert "[startup] main model:" in proc.stdout
    assert str(guess) in proc.stdout        # the log names what it replaced


@win_only
def test_a_bat_launch_keeps_the_guess_if_the_saved_model_was_deleted(tmp_path):
    guess = _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    _vault(tmp_path, str(tmp_path / "picked" / "deleted.gguf"))
    proc, out = _run_bat(tmp_path, entry_src=_APPLY_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(guess)
    # Decided: the app's children must not inherit the guess marker.
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None
    assert "deleted.gguf" in proc.stdout


@win_only
def test_a_bat_launch_with_an_exported_path_ignores_the_saved_model(tmp_path):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    _vault(tmp_path, str(_gguf(tmp_path / "picked" / "phi-4.gguf")))
    mine = _gguf(tmp_path / "elsewhere" / "mine.gguf")
    proc, out = _run_bat(tmp_path, entry_src=_APPLY_ENTRY,
                         COUNCIL_GGUF_PATH=mine)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _read(out)["COUNCIL_GGUF_PATH"] == str(mine)


@pytest.mark.parametrize("name", ["run-linux.sh", "run-wsl.sh"])
def test_an_sh_launch_starts_on_the_saved_model(tmp_path, name):
    _gguf(tmp_path / "home" / "models" / "granite-3.1-8b.gguf")
    chosen = _gguf(tmp_path / "picked" / "phi-4.gguf")
    _vault(tmp_path, str(chosen))
    proc, out = _run_sh(tmp_path, name, entry_src=_APPLY_ENTRY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _read(out)
    assert got["COUNCIL_GGUF_PATH"] == str(chosen)
    assert got["COUNCIL_GGUF_PATH_AUTO"] is None
