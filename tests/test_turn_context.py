"""
council_core.turn_context — the context a turn is given, shared by both shells.

The Qt Council sent only the typed question to the models: a fact planted in a
vault reached 0 of 13 prompts (docs/qt_migration/remaining_scope_2026-10-06.md).
The Tk shell's context code — file and folder injection, vault and value
matches, the analyst step — was 3,186 toolkit-free lines inside the Tk module,
which the Qt shell cannot import. It moved here; the Tk module re-exports it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "turn_context.py").read_text(
        encoding="utf-8")
    for toolkit in ("import tkinter", "from tkinter", "PySide6",
                    "council_gui_engine import", "import council_gui_engine"):
        assert toolkit not in source, toolkit


def test_the_tk_engine_calls_the_same_functions():
    """Re-exported, not copied: one body, two shells."""
    import council_gui_engine as cge
    from council_core import turn_context as tc
    for name in ("_inject_file_contents", "_inject_file_contents_impl",
                 "_run_analyst_step", "_extract_file_paths",
                 "_read_file_for_injection", "_get_vault_index",
                 "_get_data_index", "_register_data_index",
                 "_FILE_INJECT_CACHE", "looks_like_file_count"):
        assert getattr(cge, name) is getattr(tc, name), name


def test_the_rebound_instances_are_not_copied_into_the_tk_engine():
    """A copy of a name rebound later goes stale silently — so they are not
    re-exported at all, and a stale read fails loudly."""
    import council_gui_engine as cge
    for name in ("_VAULT_INDEX_INSTANCE", "_DATA_INDEX_INSTANCE",
                 "_REFERENCED_FILES_MAP"):
        assert not hasattr(cge, name), name


def test_setting_the_tk_vault_still_steers_the_moved_code(tmp_path,
                                                         monkeypatch):
    import council_gui_engine as cge
    from council_core import turn_context as tc
    monkeypatch.setattr(cge, "VAULT_DIR", tmp_path / "tk_vault")
    assert tc._vault() == tmp_path / "tk_vault"


def test_no_vault_directory_name_is_left_in_the_moved_code():
    """Every VAULT_DIR read became _vault(); a bare VAULT_DIR here would be a
    NameError on the first turn that reached it."""
    import re
    source = (ROOT / "council_core" / "turn_context.py").read_text(
        encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert not re.search(r"\bVAULT_DIR\b", code)


# ============================================================
# prepare_turn — the seam both shells call
# ============================================================

@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """A temp vault, the engine pointed at a FakeOllama (nothing reaches a
    real model or the network), and a planted fact in data_in/."""
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "pumps.csv").write_text(
        "pump,bay,name,pressure_bar\nP1,4,ORCHID,12.5\nP2,5,LILAC,9.1\n",
        encoding="utf-8")
    with FakeOllama() as fake:
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refuse_egress(monkeypatch)
        yield vault, fake
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _prepare(vault, question, state=None, **kwargs):
    from council_core import turn_context as tc
    state = state or tc.TurnState(vault)
    said = []
    prepared = tc.prepare_turn(question, state,
                               on_notice=lambda *a: said.append(a), **kwargs)
    return prepared, said, state


def test_a_named_file_reaches_the_text_the_models_are_sent(vault_env):
    vault, _fake = vault_env
    prepared, said, _ = _prepare(vault, "summarize pumps.csv")
    assert "ORCHID" in prepared.text
    assert prepared.question == "summarize pumps.csv"
    assert prepared.text.rstrip().endswith("summarize pumps.csv")
    assert [p.name for p in prepared.source_paths] == ["pumps.csv"]
    assert prepared.notices == said, "notices are reported as they happen"


def test_a_value_question_finds_the_row(vault_env):
    vault, _fake = vault_env
    prepared, _said, _ = _prepare(vault, "which pump is in bay 4?")
    assert "ORCHID" in prepared.text
    assert "pumps.csv" in [p.name for p in prepared.source_paths]


def test_a_count_is_answered_from_the_data(vault_env):
    vault, _fake = vault_env
    prepared, said, _ = _prepare(vault, "how many files are in the vault?")
    assert prepared.answerable_from_data, prepared.fast_answer
    assert "file(s)" in prepared.fast_answer
    assert any("Computing from data" in text for _w, text, _k in said)


def test_each_conversation_reads_its_own_vault(vault_env, tmp_path):
    """The indexes belong to the conversation: a second vault in the same
    process is not searched through the first vault's index."""
    vault, _fake = vault_env
    other = tmp_path / "other"
    (other / "data_in").mkdir(parents=True)
    (other / "data_in" / "valves.csv").write_text(
        "valve,name\nV1,PEONY\n", encoding="utf-8")
    first, _s, _ = _prepare(vault, "which pump is in bay 4?")
    second, _s, _ = _prepare(other, "which valve is V1?")
    assert "ORCHID" in first.text and "PEONY" not in first.text
    assert "PEONY" in second.text and "ORCHID" not in second.text


def test_a_prompt_that_will_not_fit_says_so(vault_env, monkeypatch):
    import council_engine as ce
    vault, _fake = vault_env
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: 64)
    _prepared, said, _ = _prepare(vault, "summarize pumps.csv")
    assert any("n_ctx is only 64" in text for _w, text, _k in said), said


#: App files that hold OLDER conversations or learned memory, each with a
#: marker; data_in/real.txt is the user's own file, which must still be found.
APP_FILES = {
    "logs/council.log": "User: the zebrafin gasket is PLANTLOG",
    "memory/memory_writer.md": "- zebrafin gasket PLANTROLE",
    "memory/memory__project.md": "- zebrafin gasket PLANTPROJECT",
    "memory/memory__user_profile.md": "- zebrafin gasket PLANTPROFILE",
    "verdict_history.jsonl": '{"query": "zebrafin gasket PLANTVERDICT"}',
    "trends.md": "- zebrafin gasket PLANTTRENDS",
    "librarian_wishlist.md": "- [ ] zebrafin gasket PLANTWISH",
    "vault_rag_misses.txt": "2026\tzebrafin gasket PLANTMISS",
    "sage_knowledge/notes.md": "zebrafin gasket PLANTSAGE",
}


@pytest.mark.parametrize("question", [
    "search the vault for zebrafin",
    "what do we know about the zebrafin gasket?",
    "find files about the zebrafin gasket"])
def test_a_vault_search_never_brings_back_older_conversations(vault_env,
                                                             question):
    """Measured before the fix: "search the vault for zebrafin" put
    vault_rag_misses.txt and verdict_history.jsonl — older sessions'
    questions — into the prompt, and the memory files, trends.md, the
    wishlist and the Sage notes were all in the index waiting for the right
    words. Under "This conversation only" none of them may come back."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    for rel, text in APP_FILES.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    (vault / "data_in" / "real.txt").write_text(
        "the zebrafin gasket datasheet PLANTDATA\n", encoding="utf-8")
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    prepared, _said, _ = _prepare(vault, question, state=state)
    leaked = [m for m in (t.split()[-1] for t in APP_FILES.values())
              if m.strip('"}') in prepared.text]
    assert not leaked, f"{leaked} reached the prompt"


def test_the_protected_names_are_where_the_index_looks():
    import conversation_logger as cl
    for sub in ("logs", "memory", "sage_knowledge", "conversations",
                "conversation_logs"):
        assert sub in cl.PROTECTED_SUBDIRS
    for name in ("verdict_history.jsonl", "vault_rag_misses.txt",
                 "librarian_wishlist.md"):
        assert name in cl.PROTECTED_STATE_FILES
    assert "trends.md" in cl.PROTECTED_ROOT_FILES


def test_trends_md_is_protected_only_at_the_vault_root(tmp_path):
    import conversation_logger as cl
    vault = tmp_path / "vault"
    assert cl.is_protected_path(vault / "trends.md", vault)
    assert not cl.is_protected_path(vault / "data_in" / "trends.md", vault)
    assert not cl.is_protected_path(vault / "data_in" / "logs" / "x.log",
                                    vault)
