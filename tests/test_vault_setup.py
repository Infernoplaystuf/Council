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


def test_a_console_that_cannot_show_the_arrow_does_not_fail_the_move(
        places, monkeypatch):
    """MEASURED while checking the Tk side: the move lines carry "→", and with
    stdout a cp1252 pipe the print raised UnicodeEncodeError AFTER the files
    had moved. Tk's import-time caller swallowed it; prepare() would have
    reported "[Migration] skipped" for a migration that had happened."""
    import io

    app, repo, vault = places
    (app / "node_registry.json").write_text("{}", encoding="utf-8")
    pipe = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", pipe)
    moved = vault_setup.migrate_legacy_paths(vault, app_dir=app,
                                             repo_root=repo)
    pipe.flush()
    assert moved and (vault / "node_registry.json").exists()
    assert b"node_registry.json" in pipe.buffer.getvalue()


def test_a_logger_that_raises_costs_the_line_not_the_work(places):
    app, repo, vault = places
    (app / "node_registry.json").write_text("{}", encoding="utf-8")

    def broken(_message):
        raise OSError("log file gone")

    vault_setup.prepare(vault, app_dir=app, repo_root=repo, log=broken)
    assert (vault / "data_in" / "README.txt").is_file()


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


#: App state the app keeps at the vault ROOT, beside the user's loose data.
#: data_index's own skip list predates all of these.
_STATE = ("backend_settings.json", "model_slots.json", "model_bench.json",
          "vault_index.json", "vault_embeddings.json", "semantic_cache.json",
          "question_history.json", "graph_presets.json")


def test_the_apps_own_settings_are_not_copied_in_as_data(places):
    """Found in review: one Qt launch over a vault holding the app's settings
    copied backend_settings.json (with the GGUF path in it), model_slots.json,
    vault_index.json and vault_embeddings.json into data_in/, where the data
    index listed them as the user's datasets — every Qt user who configures
    a model. Tk did the same through the same function."""
    import data_index

    _app, _repo, vault = places
    for name in _STATE:
        (vault / name).write_text('{"app": "state"}', encoding="utf-8")
    (vault / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    (vault / "customers.json").write_text('[{"id": 1}]', encoding="utf-8")

    done = vault_setup.prepare_data_dirs(vault, log=lambda m: None)

    landed = sorted(p.name for p in (vault / "data_in").iterdir())
    assert landed == ["README.txt", "customers.json", "orders.csv"], landed
    assert sorted(p.name for p in done.copied) == ["customers.json",
                                                   "orders.csv"]
    for name in _STATE:
        assert (vault / name).is_file(), f"{name} was moved"
    found = data_index.DataIndex(
        search_roots=[data_index.input_dir(vault)]).discover()
    names = sorted(Path(getattr(entry, "path", entry)).name for entry in found)
    assert names == ["customers.json", "orders.csv"], names


def test_a_copy_an_earlier_run_made_is_removed_only_when_identical(places):
    """The copies already made (the real vault here has data_in/
    vault_index.json and semantic_cache.json from Tk runs) go when they are
    byte-identical to the app's file at the root — data_index's own proof
    for its stray-config sweep. A different file of that name is the user's,
    and is kept."""
    _app, _repo, vault = places
    (vault / "data_in").mkdir()
    (vault / "model_slots.json").write_text('{"main": "a"}', encoding="utf-8")
    (vault / "data_in" / "model_slots.json").write_text('{"main": "a"}',
                                                        encoding="utf-8")
    (vault / "vault_index.json").write_text('{"v": 2}', encoding="utf-8")
    (vault / "data_in" / "vault_index.json").write_text('{"v": 1}',
                                                        encoding="utf-8")
    done = vault_setup.prepare_data_dirs(vault, log=lambda m: None)
    assert not (vault / "data_in" / "model_slots.json").exists()
    assert (vault / "data_in" / "vault_index.json").read_text() == '{"v": 1}'
    assert (vault / "model_slots.json").is_file()
    assert [p.name for p in done.cleaned] == ["model_slots.json"]


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
