"""
council_core.vault_setup — the vault folders and the legacy-path migration,
moved out of the Tk engine so the Qt launch runs them too.

Every test here passes its own app folder and repo root. The real defaults are
the user's ~/.council and this checkout, and a migration MOVES files: run
against them, a test would carry the user's node registry into a temp vault
that is deleted when the run ends (measured once, in a scratch home — that is
why tests/sandbox_vault.py sets COUNCIL_SKIP_PATH_MIGRATION).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import vault_setup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def places(tmp_path):
    app = tmp_path / "app"
    repo = tmp_path / "repo"
    vault = tmp_path / "vault"
    for folder in (app, repo, vault):
        folder.mkdir()
    return app, repo, vault


def test_an_upgraders_files_are_moved_into_the_vault(places):
    app, repo, vault = places
    (app / "node_registry.json").write_text('{"nodes": []}', encoding="utf-8")
    (app / "personality_backends.json").write_text("{}", encoding="utf-8")
    (app / "council.log").write_text("old log\n", encoding="utf-8")
    (app / ".chromadb").mkdir()
    (app / ".chromadb" / "chroma.sqlite3").write_bytes(b"x")
    (repo / "vault" / "dream3d_docs").mkdir(parents=True)
    (repo / "vault" / "dream3d_docs" / "filters.md").write_text("docs")

    moved = vault_setup.migrate_legacy_paths(vault, app_dir=app,
                                             repo_root=repo, log=lambda m: None)

    assert (vault / "node_registry.json").read_text() == '{"nodes": []}'
    assert (vault / "personality_backends.json").exists()
    assert (vault / "logs" / "council.log").read_text() == "old log\n"
    assert (vault / ".chromadb" / "chroma.sqlite3").exists()
    assert (vault / "dream3d_docs" / "filters.md").exists()
    assert not (app / "node_registry.json").exists(), "copied, not moved"
    assert len(moved) == 5


def test_the_vaults_own_copy_is_never_overwritten(places):
    app, repo, vault = places
    (app / "node_registry.json").write_text("OLD", encoding="utf-8")
    (vault / "node_registry.json").write_text("CURRENT", encoding="utf-8")
    vault_setup.migrate_legacy_paths(vault, app_dir=app, repo_root=repo,
                                     log=lambda m: None)
    assert (vault / "node_registry.json").read_text() == "CURRENT"
    assert (app / "node_registry.json").read_text() == "OLD"


def test_running_it_again_does_nothing(places):
    app, repo, vault = places
    (app / "node_registry.json").write_text("{}", encoding="utf-8")
    first = vault_setup.migrate_legacy_paths(vault, app_dir=app,
                                             repo_root=repo, log=lambda m: None)
    second = vault_setup.migrate_legacy_paths(vault, app_dir=app,
                                              repo_root=repo,
                                              log=lambda m: None)
    assert first and second == []


def test_the_data_folders_get_their_readmes(places):
    _app, _repo, vault = places
    vault_setup.prepare_data_dirs(vault, log=lambda m: None)
    assert (vault / "data_in" / "README.txt").is_file()
    assert (vault / "data_out" / "README.txt").is_file()
    assert (vault / "data_out" / "charts").is_dir()


def test_a_readme_the_user_edited_is_kept(places):
    _app, _repo, vault = places
    (vault / "data_in").mkdir()
    (vault / "data_in" / "README.txt").write_text("my notes", encoding="utf-8")
    vault_setup.prepare_data_dirs(vault, log=lambda m: None)
    assert (vault / "data_in" / "README.txt").read_text() == "my notes"


def test_loose_data_at_the_vault_root_is_copied_into_data_in(places):
    _app, _repo, vault = places
    (vault / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    (vault / "specialists.json").write_text("{}", encoding="utf-8")
    done = vault_setup.prepare_data_dirs(vault, log=lambda m: None)
    assert (vault / "data_in" / "orders.csv").is_file()
    assert (vault / "orders.csv").is_file(), "the original was moved"
    assert not (vault / "data_in" / "specialists.json").exists(), (
        "app config was treated as user data")
    assert [p.name for p in done.copied] == ["orders.csv"]


def test_the_skip_switch_is_honoured(places, monkeypatch):
    app, repo, vault = places
    (app / "node_registry.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COUNCIL_SKIP_PATH_MIGRATION", "1")
    vault_setup.prepare(vault, app_dir=app, repo_root=repo, log=lambda m: None)
    assert (app / "node_registry.json").exists()
    assert not (vault / "node_registry.json").exists()
    # The folders are made either way.
    assert (vault / "data_in" / "README.txt").is_file()
    assert (vault / "logs").is_dir()


def test_prepare_runs_the_moves_when_allowed(places, monkeypatch):
    app, repo, vault = places
    (app / "node_registry.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COUNCIL_SKIP_PATH_MIGRATION", "0")
    vault_setup.prepare(vault, app_dir=app, repo_root=repo, log=lambda m: None)
    assert (vault / "node_registry.json").exists()


def test_the_tk_engine_calls_the_same_functions():
    """One implementation, so the two front ends cannot drift about what an
    upgrade moves or what a fresh vault contains."""
    import ast

    from tests.source_checks import code_of

    source = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    body = code_of(source, "_migrate_old_paths_to_vault")
    assert "vault_setup.migrate_legacy_paths(" in body
    assert "shutil.move" not in body, "Tk still has its own copy of the moves"
    console = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "CouncilConsole")
    init = ast.unparse(next(node for node in console.body
                            if isinstance(node, ast.FunctionDef)
                            and node.name == "__init__"))
    assert "vault_setup.prepare_data_dirs(" in init
    assert "data_index.init_data_dirs(" not in init


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "vault_setup.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "import tk"):
        assert toolkit not in source
