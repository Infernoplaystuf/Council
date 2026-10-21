"""
council_core.turn_context — the context a Qt Council turn is given.

The Qt Council sent only the typed question to the models: a fact planted in a
vault reached 0 of 13 prompts (docs/qt_migration/remaining_scope_2026-10-06.md).
The Tk shell's context code — file and folder injection, vault and value
matches, the analyst step — was 3,186 toolkit-free lines inside the Tk module,
which the Qt shell cannot import. A copy of it lives here, for the Qt app; the
retired Tk shell keeps its own.
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


def test_outside_a_turn_the_vault_is_the_app_s(tmp_path, monkeypatch):
    """The Tk engine's hook that steered this code to ITS vault is gone with
    the Tk edits: outside a prepared turn these functions read the app's
    vault (council_core.paths), inside one the turn's."""
    from council_core import paths
    from council_core import turn_context as tc
    monkeypatch.setattr(paths, "vault_dir", lambda: tmp_path / "app_vault")
    assert tc._vault() == tmp_path / "app_vault"
    assert not hasattr(tc, "use_vault_dir")
    token = tc._ACTIVE.set(tc.TurnState(tmp_path / "turn_vault"))
    try:
        assert tc._vault() == tmp_path / "turn_vault"
    finally:
        tc._ACTIVE.reset(token)


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


class _Writer:
    """A stand-in for the personality that answers: prepare_turn sizes the
    vault blocks around what its call sends besides the question
    (PersonalityModel.prompt_frame) and the reply it asks for."""

    def __init__(self, frame="", predict=100):
        self.frame, self.predict = frame, predict

    def prompt_frame(self):
        return self.frame, self.predict


def _filling(vault, share, question="summarize pumps.csv", window=4096):
    """Prepare ``question`` for a Writer whose own prompt makes its call
    fill ``share`` of the room a 4,096 window leaves beside its answer."""
    import council_engine as ce
    base, _s, _ = _prepare(vault, question, n_ctx=window,
                           writer=_Writer("", 100))
    room, text = base.budget["safe_input"], base.budget["input_tokens"]
    frame = "x" * int((share * room - text) * ce._budget_cpt())
    return _prepare(vault, question, n_ctx=window, writer=_Writer(frame, 100))


def test_a_prompt_that_nearly_fills_the_room_says_so(vault_env):
    """Measured on everything the Writer's call sends (its own prompt and
    the earlier turns as well as the question and the vault) against what
    the window leaves beside its answer. It was measured on the question
    and the vault alone, which put a prompt the engine was about to cut at
    43% of the window (found in review of 217f2d4)."""
    vault, _fake = vault_env
    prepared, said, _ = _filling(vault, 0.95)
    notices = _window_notices(said)
    assert len(notices) == 1, said
    text = notices[0]
    budget = prepared.budget
    pct = budget["input_tokens"] / budget["safe_input"] * 100
    assert f"{pct:.0f}%" in text and "4,096" in text, text
    assert "keeps 100 of them for the answer" in text, text
    assert "too long" not in text and "nearly fills" in text, text
    for word in JARGON:
        assert word not in text, f"{word!r} in {text!r}"


def test_a_prompt_well_inside_the_room_says_nothing_about_it(vault_env):
    vault, _fake = vault_env
    _prepared, said, _ = _filling(vault, 0.5)
    assert not _window_notices(said), said


def test_the_writers_own_prompt_counts_toward_the_fit(vault_env):
    """The question and the vault blocks alone fill a few per cent of the
    room; with the Writer's own prompt the call does not fit, and the
    engine will cut it: that is said."""
    vault, _fake = vault_env
    prepared, said, _ = _filling(vault, 1.2)
    assert prepared.budget["over_window"], prepared.budget
    notices = _window_notices(said)
    assert len(notices) == 1 and "too long" in notices[0], said
    assert "Part of it will be cut out" in notices[0], notices[0]
    assert "ORCHID" in prepared.text, "the file itself still goes in"


def test_named_files_share_the_room_and_none_is_cut_silently(vault_env):
    """Four large files named at the default 8,192 window, sized around the
    real Writer's prompt (its 4,953-character instructions, 2,400 tokens
    kept for the answer): each gets a part of the room — head and tail —
    and the whole call fits as it will be sent, so the engine cuts
    nothing. Before, the first ones took the room whole and the call was
    larger than the window could take beside the answer."""
    import council_engine as ce
    vault, _fake = vault_env
    filler = "Routine inspection text for the line, nothing unusual. " * 280
    for name in ("alpha", "bravo", "charlie", "delta"):
        (vault / "data_in" / f"{name}.txt").write_text(
            f"{name.upper()}-FIRST-LINE\n" + filler, encoding="utf-8")
    prepared, said, _ = _prepare(
        vault, "Compare alpha.txt, bravo.txt, charlie.txt and delta.txt",
        n_ctx=8192)
    for name in ("alpha", "bravo", "charlie", "delta"):
        assert f"{name.upper()}-FIRST-LINE" in prepared.text, name
    budget = prepared.budget
    assert budget["n_ctx"] == 8192 and budget["reply_reserve"] == 2400
    assert not budget["over_window"], budget
    frame = ce.ROLE_PROMPTS["writer"] + "\nUSER TASK:\n"
    messages = [{"role": "system", "content": frame},
                {"role": "user", "content": prepared.text}]
    kept, reply = ce._clamp_messages_to_ctx(
        messages, 2400, 8192, count_tokens=ce._ollama_counter(3.0))
    assert kept == messages and reply == 2400, "the engine would cut it"


def test_the_context_is_sized_for_the_window_the_model_really_has(
        vault_env, monkeypatch):
    """council_engine.get_n_ctx() is the CONFIGURED size (4,096 with
    COUNCIL_GGUF_N_CTX unset — the launchers leave it unset); the window the
    model really has is effective_n_ctx() (Ollama's num_ctx, 8,192 by
    default; a GGUF loaded at 16,384 on a big card). Its docstring: "Code
    that SIZES a prompt should ask effective_n_ctx() instead." prepare_turn
    sized the vault blocks with get_n_ctx(), so they were cut to fit a
    quarter of a 16 K window, while the notices measured the real one."""
    import council_engine as ce
    vault, _fake = vault_env
    monkeypatch.delenv("COUNCIL_GGUF_N_CTX", raising=False)
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: 16384)
    assert ce.get_n_ctx() == 4096
    prepared, _said, _ = _prepare(vault, f"What is in {vault / 'data_in'} ?")
    assert prepared.breakdown["n_ctx"] == 16384, prepared.breakdown
    labels = [label for label, _ in prepared.breakdown["costs"]]
    # A window of 16 K or more gets the folder's rich listing.
    assert any(label.startswith("[FOLDER: ") for label in labels), labels


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


def test_a_chip_names_the_file_the_match_was_read_from(vault_env):
    """Found in review: the chip was resolved from the block's label — a
    bare name — searched for in data_in/, so a match read from
    projects/notes.txt got the chip (and the read-only preview) of
    data_in/old/notes.txt, another file with nothing of the answer in it."""
    vault, _fake = vault_env
    (vault / "projects").mkdir()
    real = vault / "projects" / "notes.txt"
    real.write_text("The zebrafin valve was replaced on line 3.\n" * 5,
                    encoding="utf-8")
    (vault / "data_in" / "old").mkdir()
    (vault / "data_in" / "old" / "notes.txt").write_text(
        "Unrelated shopping list: eggs, milk.\n", encoding="utf-8")
    from council_core import turn_context as tc
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    prepared, _said, _ = _prepare(vault, "search the vault for zebrafin",
                                  state=state, n_ctx=16384)
    assert "zebrafin valve" in prepared.text
    assert [p.resolve() for p in prepared.source_paths] == [real.resolve()]


def test_a_match_outside_data_in_gets_its_chip(vault_env):
    """A match in the vault but outside data_in/ reached the prompt with no
    chip: its bare name was looked for in data_in/ only."""
    vault, _fake = vault_env
    (vault / "projects").mkdir()
    log = vault / "projects" / "valve_log.txt"
    log.write_text("The zebrafin valve was replaced on line 3.\n" * 5,
                   encoding="utf-8")
    from council_core import turn_context as tc
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    prepared, _said, _ = _prepare(vault, "search the vault for zebrafin",
                                  state=state, n_ctx=16384)
    assert "zebrafin valve" in prepared.text
    assert [p.resolve() for p in prepared.source_paths] == [log.resolve()]


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


# 8,192 gets the compact folder listing, 32,768 the rich one. (A 4,096
# window leaves the listing no room beside the Writer's own prompt and its
# answer: it is left out, and said — test_named_files_share_the_room…)
@pytest.mark.parametrize("n_ctx", [8192, 32768])
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
# The knowledge graph's block (KG4)
# ============================================================

def test_the_knowledge_graph_hook_adds_nothing_without_a_graph(vault_env):
    """No graph built: no block - and asking makes no store."""
    from council_core import knowledge_graph as kgm
    from council_core import turn_context as tc
    vault, _fake = vault_env
    state = tc.TurnState(vault)
    assert tc.knowledge_graph_block("which pump is ORCHID?", state) is None
    prepared, _said, _ = _prepare(vault, "which pump is in bay 4?",
                                  state=state)
    labels = [label for label, _ in prepared.breakdown.get("costs", [])]
    assert tc.KNOWLEDGE_GRAPH_LABEL not in labels, labels
    assert not kgm.store_path(vault).exists()


def test_a_question_naming_what_the_graph_knows_gets_its_cited_links(vault_env):
    """KG4 built (user, 2026-10-21: "Answers in the Council"): a question
    naming a person the graph knows gets their links, two hops out, each
    cited, in front of the question; the cited file is a source."""
    from council_core import knowledge_graph as kgm
    from council_core import turn_context as tc
    vault, _fake = vault_env
    tracker = vault / "data_in" / "tracker.csv"
    tracker.write_text("Project,Part Number,Owner\nKA-101,WG-2201,Ngozi Okafor\n"
                       "KA-102,LG-0310,Jens Halvorsen\n", encoding="utf-8")
    with kgm.KnowledgeGraph(vault) as kg:
        kg.seed()
    prepared, _said, _ = _prepare(vault, "what does ngozi okafor work on?")
    labels = [label for label, _ in prepared.breakdown["costs"]]
    assert tc.KNOWLEDGE_GRAPH_LABEL in labels
    head, _sep, _tail = prepared.text.partition("what does ngozi okafor work on?")
    assert "Ngozi Okafor (person) — owns → WG-2201 (part)" in head
    assert "KA-101 (project) — uses part → WG-2201 (part)" in head        # hop 2
    assert "tracker.csv, row 2" in head
    assert "Jens Halvorsen" not in head                    # not linked to her
    assert tracker in prepared.source_paths


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


# ============================================================
# "forget WORD" — the command the close-match notice offers
# ============================================================

def _typo_vault(vault):
    from council_core import turn_context as tc
    (vault / "data_in" / "real.txt").write_text(
        "the zebrafin gasket datasheet\n", encoding="utf-8")
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    return state


def test_forget_takes_a_close_match_the_notice_offered(vault_env):
    """The notice under a vault search says "If any are wrong, type: forget
    WORD". The Qt shell now shows that notice, so the command has to work
    there; it takes only words this conversation was offered."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    state = _typo_vault(vault)
    prepared, said, _ = _prepare(vault, "search the vault for zebrafn",
                                 state=state)
    assert "zebrafin" in prepared.fuzzy_matches.get("zebrafn", [("",)])[0]
    assert any("forget" in text for _w, text, _k in said)
    reply = tc.forget_command("forget zebrafin", state)
    assert reply and "zebrafin" in reply, reply
    import json as _json
    assert "zebrafin" in _json.loads(
        (vault / "fuzzy_denylist.json").read_text(encoding="utf-8"))
    again, said, _ = _prepare(vault, "search the vault for zebrafn",
                              state=state)
    assert "zebrafn" not in again.fuzzy_matches, again.fuzzy_matches


@pytest.mark.parametrize("text", [
    "forget what I said earlier and tell me about valves",
    "forget zebrafin",                 # never offered in this conversation
    "please forget zebrafin",
    "what is a zebrafin?"])
def test_forget_leaves_everything_else_to_the_council(vault_env, text):
    """"forget …" that is not a word the notice offered is a question: Tk's
    _send took ANY "forget …" as the command and added "what", "said"…
    to the list."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    state = _typo_vault(vault)
    assert tc.forget_command(text, state) is None
    assert not (vault / "fuzzy_denylist.json").exists()


def test_forget_never_writes_over_a_list_it_cannot_read(vault_env):
    from council_core import turn_context as tc
    vault, _fake = vault_env
    state = _typo_vault(vault)
    _prepare(vault, "search the vault for zebrafn", state=state)
    damaged = b'["kiwi", "walnut"'                # cut off mid-write
    (vault / "fuzzy_denylist.json").write_bytes(damaged)
    state.vault_index._denylist_cache = None
    reply = tc.forget_command("forget zebrafin", state)
    assert reply and "could not" in reply.lower(), reply
    assert (vault / "fuzzy_denylist.json").read_bytes() == damaged


# ============================================================
# The source preview's reads are bounded
# ============================================================
# Found in review: SourcePreview.show_file (a chip click) read the WHOLE file
# to show 4,096 characters — _text_peek did read_bytes()[:max_bytes], JSON
# and parquet were parsed whole — 300-512 MB allocated and up to 4.8 s on the
# GUI thread for a CT stack, a PDF or a 53 MB JSON.

def _peak_bytes(fn):
    import tracemalloc
    import pandas  # noqa: F401 — imported first: its import is not the read
    tracemalloc.start()
    try:
        result = fn()
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def _big(path, size):
    with open(path, "wb") as fh:
        fh.truncate(size)              # zeros: a binary file, written fast
    return path


@pytest.mark.parametrize("name", ["scan_0001.tif", "part.dream3d",
                                  "report.pdf", "blob.bin"])
def test_a_large_binary_file_is_previewed_from_its_start(tmp_path, name):
    from council_core import turn_context as tc
    path = _big(tmp_path / name, 40 * 1024 * 1024)
    (schema, rows), peak = _peak_bytes(lambda: tc._data_preview_text(path))
    assert peak < 4 * 1024 * 1024, f"{peak:,} bytes held to preview it"
    assert "binary" in rows.lower() and "41,943,040" in rows, rows


def test_a_text_peek_reads_only_what_it_shows(tmp_path):
    from council_core import turn_context as tc
    path = tmp_path / "log.txt"
    path.write_text("line of the log\n" * 2_000_000, encoding="utf-8")
    peek, peak = _peak_bytes(lambda: tc._text_peek(path, max_bytes=4096))
    assert peek.startswith("line of the log") and len(peek) <= 4096
    assert peak < 1024 * 1024, f"{peak:,} bytes held for a 4 KB peek"


def test_a_large_json_file_is_not_parsed_whole(tmp_path):
    import json as _json
    from council_core import turn_context as tc
    path = tmp_path / "big.json"
    path.write_text(_json.dumps([{"id": i, "name": f"part {i}"}
                                 for i in range(400_000)]), encoding="utf-8")
    assert path.stat().st_size > tc.PREVIEW_PARSE_MAX_BYTES
    (schema, rows), peak = _peak_bytes(lambda: tc._data_preview_text(path))
    assert peak < 8 * 1024 * 1024, f"{peak:,} bytes held to preview it"
    assert rows.startswith('[{"id": 0'), rows[:80]
    assert "too large" in schema.lower(), schema


def test_a_small_json_file_is_still_a_table(tmp_path):
    import json as _json
    from council_core import turn_context as tc
    path = tmp_path / "small.json"
    path.write_text(_json.dumps([{"pump": "P1", "name": "ORCHID"}]),
                    encoding="utf-8")
    schema, rows = tc._data_preview_text(path)
    assert "pump" in schema and "ORCHID" in rows


# ============================================================
# Which files reach the prompt and the chips (review, 2026-10-12)
# ============================================================

def _app_readmes(vault):
    from council_core import vault_setup
    vault_setup.prepare_data_dirs(vault)        # writes the two READMEs
    assert (vault / "data_in" / "README.txt").is_file()
    assert (vault / "data_out" / "README.txt").is_file()


@pytest.mark.parametrize("question", ["tell me about the zebrafin",
                                      "search the vault for zebrafin"])
def test_the_app_s_own_readmes_are_not_vault_matches(vault_env, question):
    """Found in review: after prepare_data_dirs the app's own
    data_in/README.txt and data_out/README.txt came back as [VAULT MATCH]
    blocks and source chips beside the user's real match."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    _app_readmes(vault)
    (vault / "data_in" / "real.txt").write_text(
        "the zebrafin gasket datasheet\n", encoding="utf-8")
    state = tc.TurnState(vault)
    state.get_vault_index().rebuild()
    prepared, _said, _ = _prepare(vault, question, state=state, n_ctx=16384)
    assert "read-only by Data's Inferno" not in prepared.text
    assert "app-managed output folder" not in prepared.text
    assert "README.txt" not in [p.name for p in prepared.source_paths]


def test_a_readme_the_user_rewrote_is_theirs(vault_env):
    """Only the app's own text is protected — not every README.txt, and
    not one the user has made their own."""
    import conversation_logger as cl
    vault, _fake = vault_env
    _app_readmes(vault)
    readme = vault / "data_in" / "README.txt"
    assert cl.is_protected_path(readme, vault)
    assert cl.is_protected_path(vault / "data_out" / "README.txt", vault)
    assert not cl.is_protected_path(vault / "data_in" / "jobs" / "README.txt",
                                    vault)
    readme.write_text("zebrafin gasket notes of my own\n", encoding="utf-8")
    assert not cl.is_protected_path(readme, vault)


def test_a_file_outside_the_vault_named_like_app_state_is_read(vault_env,
                                                              tmp_path):
    """Found in review (895976c): "summarize <somewhere>/model_bench.json"
    read nothing and said nothing — the app-state names are protected by
    basename, a rule for the vault's content, before the path was checked
    to be in the vault at all."""
    vault, _fake = vault_env
    other = tmp_path / "my_project"
    other.mkdir()
    named = other / "model_bench.json"
    named.write_text('[{"run": 1, "note": "QUOKKA"}]', encoding="utf-8")
    prepared, _said, _ = _prepare(vault, f"summarize {named}")
    assert prepared.explicit_paths == [str(named)]
    assert "QUOKKA" in prepared.text


def test_naming_one_of_the_app_s_files_is_said_and_not_read(vault_env):
    """A named file in the vault that is the app's own (older conversations,
    memory) is still not read — and the transcript says so, instead of the
    question going on as if the file had not been named."""
    vault, _fake = vault_env
    (vault / "memory").mkdir()
    named = vault / "memory" / "memory_writer.md"
    named.write_text("- the user likes WALNUTMEMORY\n", encoding="utf-8")
    prepared, said, _ = _prepare(vault, f"summarize {named}")
    assert "WALNUTMEMORY" not in prepared.text
    assert not prepared.source_paths
    assert any("memory_writer.md" in text and "app's own" in text
               for _w, text, _k in said), said


def test_a_url_is_not_a_missing_file(vault_env):
    """Found in review: "summarize https://example.invalid/reports/data.csv"
    became the explicit path 's://example.invalid/…' (the drive-letter
    branch of the path pattern) and the Writer was told the file does not
    exist on this machine."""
    vault, _fake = vault_env
    for question in ("summarize https://example.invalid/reports/data.csv",
                     "see http://intranet/x/y.csv and ftp://host/z.txt"):
        prepared, _said, _ = _prepare(vault, question)
        assert prepared.explicit_paths == [], prepared.explicit_paths
        assert "[NO DATA AVAILABLE" not in prepared.text


def test_a_file_url_is_the_file(vault_env):
    vault, _fake = vault_env
    named = vault / "data_in" / "pumps.csv"
    prepared, _said, _ = _prepare(vault, f"summarize {named.as_uri()}")
    assert [Path(p).resolve() for p in prepared.explicit_paths] == [
        named.resolve()]
    assert "ORCHID" in prepared.text


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows share lock")
def test_a_named_file_that_cannot_be_read_is_not_called_missing(vault_env):
    """Found in review: a file held open by another program (no sharing)
    was reported to the model as "does NOT exist on this machine", beside a
    chip for it."""
    import ctypes
    from ctypes import wintypes
    vault, _fake = vault_env
    named = vault / "data_in" / "locked.csv"
    named.write_text("a,b\n1,ZEBRA\n", encoding="utf-8")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    handle = k32.CreateFileW(str(named), 0x80000000, 0, None, 3, 0x80, None)
    assert handle and handle != wintypes.HANDLE(-1).value
    try:
        prepared, said, _ = _prepare(vault, f"summarize {named}")
    finally:
        k32.CloseHandle(handle)
    assert "do NOT exist on this machine" not in prepared.text
    assert "could not be read" in prepared.text
    assert not prepared.source_paths, "a chip for a file nobody read"
    notes = [text for _w, text, _k in said if "locked.csv" in text
             and "could not be read" in text]
    assert notes, said


def test_a_missing_named_file_is_still_said_to_be_missing(vault_env):
    vault, _fake = vault_env
    named = vault / "data_in" / "gone.csv"
    prepared, _said, _ = _prepare(vault, f"summarize {named}")
    assert "do NOT exist on this machine" in prepared.text


def test_stale_app_state_copies_in_data_in_are_not_value_matches(
        vault_env):
    """Found in review: copies of the app's state files an earlier launch
    put in data_in/, since changed at the root (so the byte-identity sweep
    keeps them), came back as [VALUE MATCHES] rows — the vault search
    already treats every file of those names as the app's."""
    import json as _json
    from council_core import vault_setup
    vault, _fake = vault_env
    for name in sorted(vault_setup.VAULT_ROOT_STATE):
        (vault / name).write_text(_json.dumps({"now": 1}), encoding="utf-8")
        (vault / "data_in" / name).write_text(_json.dumps(
            [{"note": f"zebrafin stale copy of {name}", "k": 1}]),
            encoding="utf-8")
    (vault / "data_in" / "real.json").write_text(_json.dumps(
        [{"note": "zebrafin real record", "k": 2}]), encoding="utf-8")
    vault_setup.prepare_data_dirs(vault)
    prepared, _said, _ = _prepare(vault, "which rows mention zebrafin",
                                  n_ctx=16384)
    assert "zebrafin real record" in prepared.text
    assert "stale copy of" not in prepared.text, prepared.text


def test_a_knowledge_graph_block_cites_only_vault_files(vault_env, tmp_path,
                                                       monkeypatch):
    """KG4's hook: a citation outside the vault, or of one of the app's own
    files, must not become a chip (a click previews it)."""
    from council_core import turn_context as tc
    vault, _fake = vault_env
    outside = tmp_path / "private" / "diary.txt"
    outside.parent.mkdir()
    outside.write_text("private\n", encoding="utf-8")
    (vault / "memory").mkdir()
    memory = vault / "memory" / "memory_writer.md"
    memory.write_text("- remembered\n", encoding="utf-8")
    pumps = vault / "data_in" / "pumps.csv"
    monkeypatch.setattr(tc, "knowledge_graph_block",
                        lambda q, s: tc.ContextBlock(
                            text="Pump P1 KGMARKER [pumps.csv, row 2]",
                            sources=[str(outside), str(memory), str(pumps),
                                     "../memory/memory_writer.md"]))
    prepared, _said, _ = _prepare(vault, "who services pump P1?",
                                  n_ctx=16384)
    assert "KGMARKER" in prepared.text
    names = {p.resolve() for p in prepared.source_paths}
    assert pumps.resolve() in names
    assert outside.resolve() not in names and memory.resolve() not in names


# ============================================================
# A folder linked into the vault (decided 2026-10-12, review)
# ============================================================
# Found in review: a junction inside the vault that points outside it is
# searched, and the outside file's content reaches the prompt. DECIDED: a
# folder the user links into the vault is part of the vault — that is how a
# folder is put in without copying it — and it stays on this PC. What keeps
# it safe is that protection is decided on the RESOLVED path: a link to the
# app's own memory/ is still the app's memory. Both are pinned here.

def _junction(link, target):
    import subprocess
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link),
                           str(target)], capture_output=True, text=True)
    if done.returncode:
        pytest.skip(f"no junction: {done.stderr.strip()}")


def _unlink_junction(link):
    import subprocess
    # rmdir removes the junction itself, never what it points at.
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_a_folder_linked_into_the_vault_is_searched(vault_env, tmp_path):
    from council_core import turn_context as tc
    vault, _fake = vault_env
    outside = tmp_path / "linked_project"
    outside.mkdir()
    (outside / "gasket_notes.txt").write_text(
        "The zebrafin gasket was replaced in June.\n" * 3, encoding="utf-8")
    link = vault / "data_in" / "linked"
    _junction(link, outside)
    try:
        state = tc.TurnState(vault)
        state.get_vault_index().rebuild()
        prepared, _said, _ = _prepare(vault, "search the vault for zebrafin",
                                      state=state, n_ctx=16384)
    finally:
        _unlink_junction(link)
    assert "zebrafin gasket was replaced" in prepared.text
    assert (outside / "gasket_notes.txt").is_file(), "the target was touched"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_a_link_to_the_app_s_memory_is_still_the_app_s(vault_env):
    from council_core import turn_context as tc
    vault, _fake = vault_env
    memory = vault / "memory"
    memory.mkdir()
    (memory / "memory_writer.md").write_text(
        "- the zebrafin merger with ACME is PLANTMEMORY\n", encoding="utf-8")
    link = vault / "data_in" / "archive"
    _junction(link, memory)
    try:
        state = tc.TurnState(vault)
        state.get_vault_index().rebuild()
        prepared, _said, _ = _prepare(vault, "search the vault for zebrafin",
                                      state=state, n_ctx=16384)
        named, _said, _ = _prepare(
            vault, f"summarize {link / 'memory_writer.md'}", n_ctx=16384)
    finally:
        _unlink_junction(link)
    assert "PLANTMEMORY" not in prepared.text
    assert "PLANTMEMORY" not in named.text
    assert (memory / "memory_writer.md").is_file(), "the target was touched"
