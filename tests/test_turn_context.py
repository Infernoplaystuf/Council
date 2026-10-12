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
from types import SimpleNamespace

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


#: Words a context-window notice is not to use: settings jargon the user
#: never typed, and the Tk wording's "truncate the tail" / "injected".
JARGON = ("n_ctx", "truncate", "injected", "analyst data", "context info")


def _window_notices(said):
    return [text for _w, text, _k in said
            if "what the model can read" in text]


def _big_file(vault):
    """A data file large enough that the prompt it makes needs a window of
    a realistic size (thousands of tokens, not a few hundred, where the
    engine's 256-token answer reserve is most of the window)."""
    (vault / "data_in" / "notes.txt").write_text(
        "The supplier says the gasket seats need re-torquing. " * 160 + "\n",
        encoding="utf-8")
    return "summarize notes.txt"


def _prompt_tokens(vault, question):
    """How many tokens the prepared prompt comes to, on a window that is
    large enough for no warning."""
    import council_engine as ce
    prepared, _said, _ = _prepare(vault, question, n_ctx=32768)
    return ce.estimate_tokens(prepared.text)


def test_a_prompt_that_will_not_fit_says_so(vault_env, monkeypatch):
    """Over the window: said, in plain words, with the numbers and what to
    do. The Tk wording was "Prompt is ~N tokens but n_ctx is only 64. The
    model will silently truncate the tail and may miss the injected
    file/analyst data."""
    import council_engine as ce
    vault, _fake = vault_env
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: 64)
    _prepared, said, _ = _prepare(vault, "summarize pumps.csv")
    notices = _window_notices(said)
    assert len(notices) == 1, said
    text = notices[0]
    assert "too long" in text and "64" in text, text
    # The setting that sizes THIS model's window: Ollama serves it here.
    assert "COUNCIL_OLLAMA_NUM_CTX" in text, "the notice does not say what to do"
    for word in JARGON:
        assert word not in text, f"{word!r} in {text!r}"


def test_a_prompt_over_80_percent_of_the_window_says_so(vault_env,
                                                        monkeypatch):
    import council_engine as ce
    vault, _fake = vault_env
    question = _big_file(vault)
    used = _prompt_tokens(vault, question)
    window = int(used / 0.85)                 # the prompt fills ~85% of it
    assert window >= 2048, window
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: window)
    _prepared, said, _ = _prepare(vault, question, n_ctx=32768)
    notices = _window_notices(said)
    assert len(notices) == 1, said
    text = notices[0]
    assert f"{used / window * 100:.0f}%" in text, text
    assert f"{window:,}" in text, text
    assert "too long" not in text and "room" in text, text
    for word in JARGON:
        assert word not in text, f"{word!r} in {text!r}"


def test_a_prompt_well_inside_the_window_says_nothing_about_it(vault_env,
                                                              monkeypatch):
    import council_engine as ce
    vault, _fake = vault_env
    question = _big_file(vault)
    used = _prompt_tokens(vault, question)
    window = used * 2                         # the prompt fills half of it
    assert window >= 2048, window
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: window)
    _prepared, said, _ = _prepare(vault, question, n_ctx=32768)
    assert not _window_notices(said), said


@pytest.mark.parametrize("ollama,slot_n_ctx,named", [
    (False, None, "COUNCIL_GGUF_N_CTX"),
    (True, None, "COUNCIL_OLLAMA_NUM_CTX"),
    # The main slot's own window wins over the variable
    # (council_engine._ollama_num_ctx): naming the variable would be advice
    # that changes nothing.
    (True, 4096, "model_slots.json"),
])
def test_the_notice_names_the_setting_that_sizes_the_window(
        monkeypatch, ollama, slot_n_ctx, named):
    import council_engine as ce
    from council_core import turn_context as tc
    main = SimpleNamespace(name="main", path="ollama:phi3.5",
                           n_ctx=slot_n_ctx)
    monkeypatch.setattr(ce, "_ollama_will_serve", lambda _slot: ollama)
    monkeypatch.setattr(ce, "_slot_config",
                        lambda: SimpleNamespace(slots={"main": main}))
    assert named in tc.window_setting()
    text = tc.context_window_notice({"input_tokens": 5000, "n_ctx": 4096,
                                     "over_window": True,
                                     "safe_input": 3072,
                                     "pct_of_window": 122.0})
    assert named in text, text


def test_blocks_left_out_to_fit_are_said_in_plain_words(vault_env):
    """A small window leaves vault matches out; the notice says what was
    left out and why without the settings jargon."""
    vault, _fake = vault_env
    for i in range(12):
        (vault / "data_in" / f"gasket_{i}.txt").write_text(
            ("zebrafin gasket line %d " % i) * 400 + "\n", encoding="utf-8")
    _prepared, said, _ = _prepare(vault, "search the vault for zebrafin",
                                  n_ctx=1024)
    left_out = [text for _w, text, _k in said if "left out" in text]
    assert left_out, said
    for word in JARGON:
        assert word not in left_out[0], f"{word!r} in {left_out[0]!r}"


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
    "specialists.json": '{"name": "zebrafin gasket PLANTSPEC"}',
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
                 "librarian_wishlist.md", "specialists.json",
                 "council_instructions.json", "content_style.json"):
        assert name in cl.PROTECTED_STATE_FILES
    assert "trends.md" in cl.PROTECTED_ROOT_FILES


def test_trends_md_is_protected_only_at_the_vault_root(tmp_path):
    import conversation_logger as cl
    vault = tmp_path / "vault"
    assert cl.is_protected_path(vault / "trends.md", vault)
    assert not cl.is_protected_path(vault / "data_in" / "trends.md", vault)
    assert not cl.is_protected_path(vault / "data_in" / "logs" / "x.log",
                                    vault)


# ============================================================
# The app's vault-root state is never a vault match or a dataset
# ============================================================

def test_the_vault_root_state_is_app_state_in_both_lists():
    """council_core.vault_setup.VAULT_ROOT_STATE names the files the app
    writes at the vault root. Before: data_index's skip list named none of
    them, and the vault search's list missed model_slots.json and
    model_bench.json."""
    import conversation_logger as cl
    import data_index
    from council_core import vault_setup
    missing_skip = sorted(vault_setup.VAULT_ROOT_STATE
                          - set(data_index._APP_INTERNAL_FILENAMES))
    missing_protected = sorted(vault_setup.VAULT_ROOT_STATE
                               - set(cl.PROTECTED_STATE_FILES))
    assert not missing_skip, f"not in data_index's skip list: {missing_skip}"
    assert not missing_protected, (
        f"not protected from the vault search: {missing_protected}")


#: The app's own vault-root settings that the vault index does not write
#: itself, each with a marker (the index files are its own and are
#: rewritten by a rebuild).
ROOT_STATE_FILES = {
    "backend_settings.json": "PLANTBACKEND",
    "model_slots.json": "PLANTSLOTS",
    "model_bench.json": "PLANTBENCH",
    "graph_presets.json": "PLANTPRESETS",
}


@pytest.mark.parametrize("question", [
    "search the vault for zebrafin",
    "what do we know about the zebrafin gasket?"])
def test_the_app_s_settings_files_never_come_back_as_vault_matches(
        vault_env, question):
    """A vault search must not offer the app's settings as a source: the
    marker must not reach the prompt and the file must not be a chip."""
    import json as _json
    from council_core import turn_context as tc
    vault, _fake = vault_env
    for name, marker in ROOT_STATE_FILES.items():
        (vault / name).write_text(
            _json.dumps({"note": f"zebrafin gasket {marker}"}) + "\n",
            encoding="utf-8")
    (vault / "data_in" / "real.txt").write_text(
        "the zebrafin gasket datasheet PLANTDATA\n", encoding="utf-8")
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    prepared, _said, _ = _prepare(vault, question, state=state)
    leaked = [m for m in ROOT_STATE_FILES.values() if m in prepared.text]
    assert not leaked, f"{leaked} reached the prompt:\n{prepared.text}"
    cited = {Path(s).name for s in prepared.sources} | {
        p.name for p in prepared.source_paths}
    assert not cited & set(ROOT_STATE_FILES), cited
    assert "PLANTDATA" in prepared.text, (
        "the user's own file is no longer found:\n" + prepared.text)


def test_the_loose_file_copy_leaves_the_app_s_state_at_the_root(tmp_path):
    """data_index.migrate_loose_vault_files copies the user's loose root
    data into data_in/. Before: it filed backend_settings.json,
    model_slots.json and the vault indexes there as the user's datasets."""
    import data_index
    from council_core import vault_setup
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    for name in vault_setup.VAULT_ROOT_STATE:
        (vault / name).write_text('{"app": true}\n', encoding="utf-8")
    (vault / "orders.json").write_text('[{"order": 1}]\n', encoding="utf-8")
    copied = {p.name for p in data_index.migrate_loose_vault_files(vault)}
    assert copied == {"orders.json"}, copied
    assert sorted(p.name for p in (vault / "data_in").iterdir()) == [
        "orders.json"]
    for name in vault_setup.VAULT_ROOT_STATE:
        assert (vault / name).read_text(encoding="utf-8") == '{"app": true}\n'


def test_a_user_s_file_named_like_app_state_is_kept(tmp_path):
    """cleanup_misplaced_internals knows the root-state names too; it still
    takes out only a byte-identical copy of the root file. A data_in/ file
    of that name with other content is the user's, and is kept."""
    import data_index
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "model_slots.json").write_text('{"app": 1}\n', encoding="utf-8")
    (vault / "data_in" / "model_slots.json").write_text(
        '{"mine": 1}\n', encoding="utf-8")
    removed = [p.name for p in data_index.cleanup_misplaced_internals(vault)]
    assert removed == []
    assert (vault / "data_in" / "model_slots.json").read_text(
        encoding="utf-8") == '{"mine": 1}\n'
    assert (vault / "model_slots.json").read_text(
        encoding="utf-8") == '{"app": 1}\n'


#: Older sessions and learned memory, planted where the app keeps them, each
#: with a marker that must not reach the prompt when the user names the vault
#: folder itself in a question.
VAULT_ROOT_APP_FILES = {
    "conversations/spare_impeller_tangerine.jsonl":
        '{"who": "User", "text": "the spare impeller is OLDTANGERINE"}',
    "conversation_logs/session_20261001_090000.jsonl":
        '{"who": "User", "text": "OLDLOGLINE"}',
    "logs/council.log": "[2026-10-01] User: OLDCOUNCILLOG",
    "memory/writer.md": "- the user likes WALNUTMEMORY",
    "memory/_user_profile.md": "- always HAIKUPROFILE",
}


@pytest.mark.parametrize("n_ctx", [4096, 32768])
def test_naming_the_vault_folder_never_lists_older_sessions_or_memory(
        vault_env, n_ctx):
    """Found in review: "What is in <vault> ?" listed conversations/,
    conversation_logs/, logs/ and memory/ — their file names at a 4 K window
    (an older session is named after its first question) and their CONTENTS
    at 32 K. Both folder renderers checked the protection against the named
    folder's PARENT, which for the vault root is the folder above the vault,
    so none of the protected sub-folders was recognised."""
    vault, _fake = vault_env
    for rel, text in VAULT_ROOT_APP_FILES.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    prepared, _said, _ = _prepare(vault, f"What is in {vault} ?",
                                  n_ctx=n_ctx)
    assert "pumps.csv" in prepared.text, (
        "the user's own file is no longer listed:\n" + prepared.text)
    for needle in ("tangerine", "OLDTANGERINE", "session_2026", "OLDLOGLINE",
                   "council.log", "OLDCOUNCILLOG", "writer.md",
                   "WALNUTMEMORY", "_user_profile", "HAIKUPROFILE"):
        assert needle not in prepared.text, (
            f"{needle!r} reached the prompt at n_ctx={n_ctx}:\n"
            + prepared.text)


def test_the_folder_renderers_protect_the_vault_s_own_folders(vault_env):
    """Both renderers, called directly on the vault root."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    for rel, text in VAULT_ROOT_APP_FILES.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    state = tc.TurnState(vault)
    token = tc._ACTIVE.set(state)
    try:
        compact = tc._render_folder_summary_compact(vault) or ""
        rich = tc._render_folder_for_injection(vault) or ""
    finally:
        tc._ACTIVE.reset(token)
    for text in (compact, rich):
        assert "pumps.csv" in text
        for folder in ("conversations", "conversation_logs", "logs",
                       "memory"):
            assert f"{folder}/" not in text.replace("\\", "/"), text


# ============================================================
# The seam's hook for the knowledge graph's block (KG4, not built)
# ============================================================

def test_the_knowledge_graph_hook_adds_nothing_until_kg4_is_built(vault_env):
    from council_core import turn_context as tc
    vault, _fake = vault_env
    state = tc.TurnState(vault)
    assert tc.knowledge_graph_block("which pump is ORCHID?", state) is None
    prepared, _said, _ = _prepare(vault, "which pump is in bay 4?",
                                  state=state)
    labels = [label for label, _ in prepared.breakdown.get("costs", [])]
    assert tc.KNOWLEDGE_GRAPH_LABEL not in labels, labels


def test_a_knowledge_graph_block_goes_in_front_of_the_question(vault_env,
                                                               monkeypatch):
    """What KG4 will return - a cited neighbourhood - is one more block in
    the same assembly: budgeted with the others, in front of the question
    (so every role and the judge see it), and the files it cites are the
    answer's sources."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    cited = vault / "data_in" / "pumps.csv"
    seen = []

    def neighbourhood(question, state):
        seen.append((question, state))
        return tc.ContextBlock(
            text="Pump P1 (ORCHID) is serviced by Dana Whitfield "
                 "KGMARKER [pumps.csv, row 2]",
            sources=[str(cited)])

    monkeypatch.setattr(tc, "knowledge_graph_block", neighbourhood)
    prepared, _said, state = _prepare(vault, "who services pump P1?")
    assert seen and seen[0][0] == "who services pump P1?"
    assert seen[0][1] is state
    head, _sep, tail = prepared.text.partition("KGMARKER")
    assert _sep, "the block did not reach the text the models are sent"
    assert tc.KNOWLEDGE_GRAPH_LABEL.strip("]") in head
    assert tail.rstrip().endswith("who services pump P1?")
    labels = [label for label, _ in prepared.breakdown["costs"]]
    assert tc.KNOWLEDGE_GRAPH_LABEL in labels
    assert cited in prepared.source_paths


def test_a_knowledge_graph_block_left_out_cites_nothing(vault_env,
                                                        monkeypatch):
    """Left out to fit the window (eviction, condensing switched off), the
    block's files are not the answer's sources."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    monkeypatch.setenv("COUNCIL_CONDENSE_OVERFLOW", "0")
    (vault / "data_in" / "big_notes.txt").write_text(
        "notes " * 4000 + "\n", encoding="utf-8")
    monkeypatch.setattr(tc, "knowledge_graph_block",
                        lambda q, s: tc.ContextBlock(
                            text="KGMARKER " + "neighbour " * 3000,
                            sources=[str(vault / "data_in" / "pumps.csv")]))
    prepared, _said, _ = _prepare(vault, "summarize big_notes.txt",
                                  n_ctx=1024)
    dropped = [label for label, _ in prepared.breakdown.get("dropped", [])]
    assert any(label.startswith(tc.KNOWLEDGE_GRAPH_LABEL)
               for label in dropped), dropped
    assert "KGMARKER" not in prepared.text
    assert "pumps.csv" not in [p.name for p in prepared.source_paths]


def test_a_failing_knowledge_graph_hook_does_not_stop_the_turn(vault_env,
                                                              monkeypatch):
    from council_core import turn_context as tc
    vault, _fake = vault_env

    def broken(_question, _state):
        raise RuntimeError("the graph store is locked")

    monkeypatch.setattr(tc, "knowledge_graph_block", broken)
    prepared, _said, _ = _prepare(vault, "summarize pumps.csv")
    assert "ORCHID" in prepared.text
    assert prepared.text.rstrip().endswith("summarize pumps.csv")
