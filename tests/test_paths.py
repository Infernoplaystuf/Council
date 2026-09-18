"""
Where the app's data lives — one answer, checked against the engine's.

THE DEFECT THIS SUITE EXISTS FOR
Ten Qt tabs defaulted the vault to `~/council_vault`; the Vault tab used
`~/.council/vault`; the engine's real answer is `$COUNCIL_VAULT_ROOT` or
`$COUNCIL_APP_DIR/vault`, falling back to `~/.council/vault`.

So the Qt build read and wrote a directory it had created itself, while the
user's actual vault — GUI projects, conversation logs, datasets — sat
elsewhere untouched. Nothing raised and nothing warned, because a vault with
nothing in it looks exactly like a fresh install.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("COUNCIL_VAULT_ROOT", raising=False)
    monkeypatch.delenv("COUNCIL_APP_DIR", raising=False)
    monkeypatch.delitem(sys.modules, "council_gui_engine", raising=False)


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "paths.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


# ============================================================
# It matches the engine
# ============================================================

def test_the_default_is_the_engines_default():
    """Not `~/council_vault`. That directory is one the Qt build invented, and
    ten tabs read it as though it were the user's."""
    assert paths.vault_dir() == Path.home() / ".council" / "vault"


def test_the_vault_lives_under_the_app_directory():
    assert paths.vault_dir().parent == paths.app_dir()


def test_the_vault_override_wins(monkeypatch, tmp_path):
    """It is how the vault gets put on another drive. The Tk build honours it
    and the Qt build ignored it entirely."""
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "elsewhere"))
    assert paths.vault_dir() == (tmp_path / "elsewhere").resolve()


def test_the_app_directory_override_moves_the_vault_with_it(monkeypatch,
                                                            tmp_path):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    assert paths.vault_dir() == (tmp_path / "app").resolve() / "vault"


def test_the_vault_override_beats_the_app_directory(monkeypatch, tmp_path):
    """It names the vault directly. An app-dir that still won would make
    COUNCIL_VAULT_ROOT a setting that sometimes does nothing."""
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    assert paths.vault_dir() == (tmp_path / "vault").resolve()


def test_a_tilde_is_expanded(monkeypatch):
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", "~/somewhere")
    assert "~" not in str(paths.vault_dir())


def test_an_empty_override_is_not_an_override(monkeypatch):
    """An unset variable and one set to "" reach os.environ differently, and
    "" would resolve to the current working directory."""
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", "")
    assert paths.vault_dir() == Path.home() / ".council" / "vault"


def test_a_loaded_engine_is_authoritative(monkeypatch, tmp_path):
    """Two builds in one process must not disagree about where the data is.
    The engine's answer ran at import; ours has to match it."""
    engine = type("E", (), {"VAULT_DIR": tmp_path / "engine-says"})
    monkeypatch.setitem(sys.modules, "council_gui_engine", engine)
    assert paths.vault_dir() == tmp_path / "engine-says"


def test_the_environment_still_beats_a_loaded_engine(monkeypatch, tmp_path):
    """Because that is how the engine itself resolves it — so agreeing with
    the variable IS agreeing with the engine."""
    engine = type("E", (), {"VAULT_DIR": tmp_path / "engine-says"})
    monkeypatch.setitem(sys.modules, "council_gui_engine", engine)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "env-says"))
    assert paths.vault_dir() == (tmp_path / "env-says").resolve()


def test_the_variables_are_read_live_not_cached_at_import(monkeypatch,
                                                          tmp_path):
    """A test that sets one and a launcher that sets one are the same case,
    and neither controls import order."""
    first = paths.vault_dir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "later"))
    assert paths.vault_dir() != first


# ============================================================
# Asking must not create
# ============================================================

def test_asking_where_the_vault_is_does_not_make_one(monkeypatch, tmp_path):
    """A function that answers "where is the vault" and silently creates one
    is how a typo in an environment variable becomes a new empty vault rather
    than an error — which is exactly what happened here."""
    target = tmp_path / "not-yet"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(target))
    paths.vault_dir()
    assert not target.exists()


def test_ensure_creates_it(tmp_path):
    target = tmp_path / "a" / "b" / "vault"
    assert paths.ensure(target) == target
    assert target.is_dir()


def test_ensure_is_happy_with_one_that_already_exists(tmp_path):
    paths.ensure(tmp_path)          # must not raise


# ============================================================
# Nothing in the Qt build may hard-code it again
# ============================================================

def _string_constants(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    return [(node.value, getattr(node, "lineno", 0))
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_no_qt_module_hard_codes_a_vault_path():
    """Ten of them did, and every one was wrong. An AST walk so the prose in
    this file's own docstrings does not count."""
    offenders = []
    for path in sorted((ROOT / "council_qt").rglob("*.py")):
        for value, line in _string_constants(path):
            if "council_vault" in value or value.endswith(".council/vault"):
                offenders.append(f"{path.name}:{line}  {value!r}")
    assert not offenders, (
        "these hard-code a vault path instead of asking council_core.paths:\n"
        + "\n".join(offenders))


def test_every_actions_class_resolves_the_same_vault():
    """The thing that actually went wrong: eleven answers to one question."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    from council_qt.tabs.council import CouncilActions
    from council_qt.tabs.designer import DesignerActions
    from council_qt.tabs.forge import ForgeActions
    from council_qt.tabs.jobs import JobsActions
    from council_qt.tabs.models import ModelsActions
    from council_qt.tabs.sessions import SessionsActions
    from council_qt.tabs.specialists import SpecialistsActions
    from council_qt.tabs.vault import _default_vault_dir

    expected = paths.vault_dir()
    for factory in (CouncilActions, DesignerActions, ForgeActions,
                    JobsActions, ModelsActions, SessionsActions,
                    SpecialistsActions):
        assert Path(factory().vault_dir) == expected, factory.__name__
    assert _default_vault_dir() == expected
