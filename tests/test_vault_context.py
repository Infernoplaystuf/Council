"""
Vault context for the Qt council (council_core.vault_context) and the vault
index's file walk (vault_rag._collect_files).

Pinned: the block names its sources and stays inside its budget; a question
never builds a semantic index or loads an embedding model — keyword search
is used unless the vault already has a semantic index; the app's own
dot-folders and protected files are never handed over as "your vault";
the block is on the models only for the turn and restored after, even on an
error; the engine's per-role gate keys on the same marker; the Council tab
searches before the turn, says what it found, and the Vault switch turns it
off.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import vault_context as vc  # noqa: E402


class FakeRag:
    def __init__(self, chunks=(), backend="keyword", count=5, fail=None):
        self.chunks, self.backend_name = list(chunks), backend
        self.count, self.fail, self.asked = count, fail, []

    def collection_count(self):
        return self.count

    def search(self, q, n_results=8):
        self.asked.append(q)
        if self.fail:
            raise self.fail
        return NS(chunks=self.chunks[:n_results])


CHUNKS = [{"source": "notes/pumps.md", "score": 0.91,
           "text": "The P-200 pump moves 40 litres per minute."},
          {"source": "specs/valves.md", "score": 0.40, "text": "Valve V-9."},
          {"source": "notes/pumps.md", "score": 0.30, "text": "Seals: EPDM."}]


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "notes").mkdir(parents=True)
    (v / "notes" / "pumps.md").write_text(
        "Pump sizing: the P-200 pump moves 40 litres per minute.\n")
    for hidden in (".council_fanout/job/u1", ".council_usage",
                   ".knowledge_graph"):
        (v / hidden).mkdir(parents=True)
        (v / hidden / "copy.md").write_text("pump pump pump secret copy\n")
    (v / "conversations").mkdir()
    (v / "conversations" / "old.md").write_text("pump answered before\n")
    (v / "question_history.json").write_text('["pump?"]')
    vc._keyword.clear()
    yield v
    vc._keyword.clear()


# ---- the block ----------------------------------------------------------------

def test_the_block_names_its_sources_once_and_carries_the_marker(tmp_path):
    b = vc.build("pump size", tmp_path, rag=FakeRag(CHUNKS))
    assert b.text.startswith(vc.MARKER)
    assert b.sources == ["notes/pumps.md", "specs/valves.md"]
    assert "[1] notes/pumps.md (match 0.91)" in b.text
    assert "2 passage(s)" in b.note() and "notes/pumps.md" in b.note()


def test_the_block_stays_inside_its_budget(tmp_path):
    big = [{"source": f"f{i}.md", "score": 1, "text": "x" * 5000}
           for i in range(10)]
    b = vc.build("q", tmp_path, rag=FakeRag(big), max_chars=4000)
    assert len(b.text) <= 4000
    assert "…" in b.text                          # passages are cut


def test_no_match_and_failures_are_said_not_raised(tmp_path):
    assert vc.build("q", tmp_path, rag=FakeRag([])).text == ""
    assert "nothing in the vault matched" in \
        vc.build("q", tmp_path, rag=FakeRag([])).note()
    bad = vc.build("q", tmp_path, rag=FakeRag(fail=OSError("disk")))
    assert bad.text == "" and "disk" in bad.problem
    empty = vc.build("q", tmp_path, rag=FakeRag(CHUNKS, "chromadb", count=0))
    assert empty.text == "" and "Re-index" in empty.problem
    assert vc.build("   ", tmp_path, rag=FakeRag(CHUNKS)).text == ""


# ---- the search: keyword unless a semantic index exists -------------------------

def test_without_a_semantic_index_no_model_is_ever_loaded(vault, monkeypatch):
    from council_core import rag_jobs

    def no(*a, **k):
        raise AssertionError("a question built the semantic index")
    monkeypatch.setattr(rag_jobs.RagIndex, "ensure", no)
    b = vc.build("what pump size", vault)
    assert b.backend == "keyword"
    # Only the user's file. The index names it relative to the vault with
    # this OS's separator (notes\pumps.md on Windows), as every path the app
    # shows there.
    assert [Path(s).as_posix() for s in b.sources] == ["notes/pumps.md"]


def test_an_existing_semantic_index_is_used(vault, monkeypatch):
    from council_core import rag_jobs
    (vault / ".chromadb").mkdir()
    (vault / ".chromadb" / "chroma.sqlite3").write_text("x")
    fake = FakeRag(CHUNKS, backend="chromadb")
    monkeypatch.setattr(rag_jobs.RagIndex, "ensure", lambda self: fake)
    b = vc.build("pump", vault)
    assert b.backend == "chromadb" and fake.asked == ["pump"]


def test_the_index_walk_skips_app_folders_and_protected_files(vault):
    import vault_rag
    found = {p.relative_to(vault).as_posix()
             for p in vault_rag._collect_files(vault)}
    assert found == {"notes/pumps.md"}


def test_a_hit_from_the_apps_own_files_is_never_a_passage(vault):
    """An index built before logs/, memory/ and the root's app files were
    protected still returns them (found in review: the user's own index
    lists memory/ and logs/ files). They are dropped, the search is asked
    for more so the user's files still fill the block, and the numbering
    has no gaps. tests/test_stale_semantic_index.py does it on a real
    chromadb index."""
    app = [{"source": s, "score": 0.99, "text": f"older session {i}"}
           for i, s in enumerate((
               "logs\\council.log", "memory/memory_writer.md",
               "verdict_history.jsonl", "question_history.json",
               "conversations/old.md", ".council_memory/decisions.jsonl",
               "sage_knowledge/facts.jsonl", "trends.md"))]
    # Six of the app's hits rank first; asked for 3 passages, the search
    # is asked for 9, so the user's three still come back.
    rag = FakeRag(app[:6] + CHUNKS)
    b = vc.build("pump", vault, rag=rag, max_passages=3)
    assert b.sources == ["notes/pumps.md", "specs/valves.md"]
    assert "older session" not in b.text
    assert "[1] notes/pumps.md" in b.text and "[3] notes/pumps.md" in b.text
    assert all(vc.is_app_source(c["source"], vault) for c in app)
    assert not vc.is_app_source("notes/pumps.md", vault)
    # A user's own logs/ folder inside data_in/ is theirs.
    assert not vc.is_app_source("data_in/logs/run.log", vault)


# ---- for one turn only ----------------------------------------------------------

def test_applied_adds_and_restores_even_on_error():
    a, b = NS(extra_context="standing primer"), NS(extra_context="")
    shared = a                                      # two roles, one model
    with pytest.raises(RuntimeError):
        with vc.applied([a, b, shared, None, NS()], "VAULT CONTEXT:\nx"):
            assert a.extra_context == "standing primer\n\nVAULT CONTEXT:\nx"
            assert b.extra_context == "VAULT CONTEXT:\nx"
            raise RuntimeError("the turn failed")
    assert a.extra_context == "standing primer" and b.extra_context == ""


def test_an_empty_block_changes_nothing():
    m = NS(extra_context="keep")
    with vc.applied([m], ""):
        assert m.extra_context == "keep"


def test_the_engine_gates_on_the_same_marker():
    import council_engine as ce
    src = inspect.getsource(ce.PersonalityModel._stitched)   # respond's prompt
    assert repr(vc.MARKER)[1:-1] in src or vc.MARKER in src
    profiles = ce.ROLE_CONTEXT_PROFILES
    assert profiles["writer"]["use_vault"] == "full"
    assert profiles["judge"]["use_vault"] == "none"


# ---- through the Council tab's send ---------------------------------------------

class Recording:
    """A council model that records the standing context each call saw."""

    def __init__(self):
        self.extra_context = ""
        self.seen = []

    def respond(self, prompt, **kw):
        self.seen.append(self.extra_context)
        return "7" if "Rate your confidence" in prompt else "an answer"


def _models():
    from council_core import council_turn as ct
    from tests.test_council_turn import FakeJudge
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.coder = Recording(), Recording()
    models.judge = FakeJudge()
    return models


@pytest.fixture
def roomy(monkeypatch):
    """A window with room beside the Writer's own prompt and reply. The
    passages go into what the Writer's call has left after the turn
    context's blocks (CouncilActions._fit_brief), and the test default —
    4,096 tokens, the Writer's ~1,700-token instructions and 2,400-token
    reply — leaves none. (It was COUNCIL_GGUF_N_CTX=16384; that variable is
    no longer read, the window is the model's.)"""
    import council_engine as ce
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: 16384)


def test_send_gives_the_vault_to_the_turn_and_takes_it_back(tmp_path,
                                                            roomy):
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief(
        "VAULT CONTEXT:\n[1] notes/pumps.md\nP-200: 40 l/min",
        ["notes/pumps.md"], "keyword")
    events = []
    options = council_options.CouncilOptions.defaults()
    options.depth = "deep"          # the debate itself is under test
    result = actions.send("pump size?", options, on_event=events.append)
    assert result.ok, result.message
    assert any("notes/pumps.md" in s for s in models.writer.seen)
    assert models.writer.extra_context == "" == models.coder.extra_context
    # The turn context's own lines (the task memo, …) come first.
    said = [e for e in events if e.who == "Librarian"]
    assert said and "1 passage" in said[0].text


def test_with_no_room_left_the_passages_are_left_out_and_said(tmp_path,
                                                             monkeypatch):
    """The Writer's call has no room beside its own prompt and reply: the
    passages are not added (the engine would cut the call to fit), and
    the transcript says why. The Judge still gets them as evidence."""
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    import council_engine as ce
    # 3072, not 4096: at 4096 the room was only used up by the bundled
    # sample data's value matches, which no longer reach a prompt
    # (data_index.search_roots).
    monkeypatch.setattr(ce, "effective_n_ctx", lambda *_a, **_k: 3072)
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    models.judge = EvidenceJudge()
    actions._models = models
    actions.vault_brief = lambda q: vc.build("pump size", tmp_path,
                                             rag=FakeRag(CHUNKS))
    events = []
    options = council_options.CouncilOptions.defaults()
    options.depth = "deep"
    assert actions.send("pump size?", options, on_event=events.append).ok
    assert not any("P-200" in s for s in models.writer.seen)
    assert any(e.who == "Librarian" and "no room left" in e.text
               for e in events)
    assert vc.EVIDENCE_MARKER in models.judge.ranked_with[0]


def test_a_passage_from_a_file_already_in_front_is_not_repeated(tmp_path,
                                                               roomy):
    """The turn context put notes/pumps.md in front of the question (its
    chip): the passages from it are dropped, the others kept."""
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "pumps.md").write_text("P-200: 40 l/min\n")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "valves.md").write_text("Valve V-9.\n")
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    models.judge = EvidenceJudge()             # takes the evidence
    actions._models = models
    real_prepare = actions.prepare

    def prepare(*a, **kw):
        prepared = real_prepare(*a, **kw)
        prepared.source_paths = [tmp_path / "notes" / "pumps.md"]
        return prepared
    actions.prepare = prepare
    actions.vault_brief = lambda q: vc.build("pump size", tmp_path,
                                             rag=FakeRag(CHUNKS))
    options = council_options.CouncilOptions.defaults()
    options.depth = "deep"
    result = actions.send("pump size?", options)
    assert result.ok, result.message
    seen = "\n".join(models.writer.seen)
    assert "Valve V-9." in seen
    assert "40 litres per minute" not in seen and "EPDM" not in seen
    assert [Path(p).name for p in result.source_paths] == ["pumps.md",
                                                           "valves.md"]


@pytest.mark.parametrize("deliberate, think", [(True, "high"),
                                               (False, None)])
def test_the_turn_context_is_sized_with_the_blocks_set_before_it(
        tmp_path, roomy, monkeypatch, deliberate, think):
    """prepare_turn sizes the vault blocks for the Writer's call, which it
    measures with the Writer's standing context (prompt_frame folds it):
    the docs and the past decisions council-map sets on the Writer are on
    it when prepare_turn runs, the passages — fitted to what is left after
    — are not yet; and the call is measured at the thinking level it is
    made at (the council's synthesis thinks "high", a plain answer not)."""
    from council_core import council_options
    from council_core import past_decisions
    from council_core.docs_brief import DocsBrief
    from council_qt.tabs.council import CouncilActions
    monkeypatch.setenv("COUNCIL_MEMORY_SCOPE", "all")
    past_decisions.record(tmp_path, "which pump for 40 litres per minute?",
                          "The P-200.", "PASS", ["writer"])
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    models.judge = EvidenceJudge()
    actions._models = models
    actions.memo_llm = None
    actions.docs_brief = lambda q, m: DocsBrief(
        "DOCUMENTATION — pages\n[1] pumps API\npump.size(lpm)", ["pumps API"])
    actions.vault_brief = lambda q: vc.build("pump", tmp_path,
                                             rag=FakeRag(CHUNKS))
    at_prepare = []
    real = actions.prepare

    def prepare(*a, **kw):
        at_prepare.append((kw["writer"].extra_context, kw.get("think")))
        return real(*a, **kw)
    actions.prepare = prepare
    options = council_options.CouncilOptions.defaults()
    options.deliberate = deliberate
    options.depth = "deep"
    result = actions.send("which pump for 40 litres per minute?", options)
    assert result.ok, result.message
    (standing, seen_think), = at_prepare
    assert "DOCUMENTATION" in standing and "PAST DECISIONS" in standing
    assert vc.MARKER not in standing
    assert seen_think == think
    assert any(vc.MARKER in s for s in models.writer.seen)
    assert models.writer.extra_context == ""


def test_the_vault_switch_turns_it_off(tmp_path):
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    actions = CouncilActions(vault_dir=tmp_path)
    actions._models = _models()
    asked = []
    actions.vault_brief = lambda q: asked.append(q) or vc.Brief()
    options = council_options.CouncilOptions.defaults()
    options.depth = "deep"          # the debate itself is under test
    options.vault = False
    events = []
    actions.send("pump size?", options, on_event=events.append)
    assert asked == []
    assert not any(e.who == "Librarian" for e in events)


# ---- the Judge's evidence ---------------------------------------------------------

def test_evidence_is_the_same_passages_under_the_judges_marker(tmp_path):
    b = vc.build("pump size", tmp_path, rag=FakeRag(CHUNKS))
    ev = vc.evidence(b)
    assert ev.startswith(vc.EVIDENCE_MARKER)
    assert vc.MARKER not in ev
    assert "[1] notes/pumps.md" in ev and "40 litres" in ev
    assert vc.evidence(vc.Brief()) == ""


def test_the_engine_strips_only_the_members_marker_from_the_judge():
    import council_engine as ce
    src = inspect.getsource(ce.PersonalityModel._stitched)   # respond's prompt
    assert '"VAULT CONTEXT:"' in src
    assert vc.EVIDENCE_MARKER not in src            # never stripped


class EvidenceJudge:
    """Records what the Judge was given, for each job."""

    def __init__(self):
        self.ranked_with, self.critiqued_with, self.routed = [], [], []

    def route(self, text):
        self.routed.append(text)
        return "chat"

    def rank_candidates(self, user_text, candidates, *, extra_context=""):
        self.ranked_with.append(extra_context)
        return '{"winner": "writer", "confidence": 8}'

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        self.critiqued_with.append(extra_context)
        return "Verdict: PASS"


def test_the_judge_ranks_and_critiques_with_the_evidence(tmp_path):
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    models.judge = EvidenceJudge()
    actions._models = models
    actions.vault_brief = lambda q: vc.build("pump size", tmp_path,
                                             rag=FakeRag(CHUNKS))
    opts = council_options.CouncilOptions.defaults()
    opts.depth = "deep"
    result = actions.send("pump size?", opts)
    assert result.ok, result.message
    judge = models.judge
    assert judge.ranked_with and vc.EVIDENCE_MARKER in judge.ranked_with[0]
    assert vc.EVIDENCE_MARKER in judge.critiqued_with[0]
    assert "Ranking:" in judge.critiqued_with[0]
    # Routing never sees it.
    assert all(vc.EVIDENCE_MARKER not in r for r in judge.routed)
    # Members never get the Judge's copy.
    assert not any(vc.EVIDENCE_MARKER in s for s in models.writer.seen)


def test_no_vault_no_evidence_and_old_judges_still_work(tmp_path):
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge
    actions = CouncilActions(vault_dir=tmp_path)
    models = _models()
    models.judge = FakeJudge()             # rank_candidates(user, cands)
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    result = actions.send("pump size?", council_options.CouncilOptions
                          .defaults())
    assert result.ok, result.message


# ---- the Sage's knowledge base ----------------------------------------------------

def _teach(vault):
    import sage_agent
    kb = sage_agent.SageKnowledge(vault / "sage_knowledge")
    kb.add_fact("pumps", "The P-200 is rated for 40 litres per minute.")
    kb.add_domain("pumps", "industrial pump sizing")
    return kb


def test_the_sage_block_reads_what_was_taught(tmp_path):
    _teach(tmp_path)
    block = vc.sage_block("which pump for 40 litres per minute", tmp_path)
    assert "SAGE RELEVANT KNOWLEDGE" in block and "P-200" in block
    assert vc.sage_block("anything", tmp_path / "nowhere") == ""


def test_only_the_sage_reads_its_knowledge_for_the_turn(tmp_path):
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    _teach(tmp_path)
    actions = CouncilActions(vault_dir=tmp_path)
    from tests.test_council_turn import FakeJudge
    models = _models()
    models.sage = Recording()
    models.judge = FakeJudge(route="sage")      # panel: sage, writer, …
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    events = []
    result = actions.send("which pump for 40 litres per minute?",
                          council_options.CouncilOptions.defaults(),
                          on_event=events.append)
    assert result.ok and "sage" in result.panel, result.message
    assert any("Sage: 2 item(s)" in e.text for e in events)
    assert models.sage.seen
    assert any("P-200" in s for s in models.sage.seen)
    assert not any("P-200" in s for s in models.writer.seen)
    assert models.sage.extra_context == ""


@pytest.mark.parametrize("scope, given", [("none", False),
                                          ("conversation", True),
                                          ("all", True)])
def test_the_sage_s_knowledge_follows_the_memory_scope(tmp_path, monkeypatch,
                                                       scope, given):
    """Found in review: the Sage's knowledge base reached the Sage in every
    scope, "Memory: off" ("none" — nothing remembered) included; its
    corrections quote older questions. Decided here: left out in "none";
    in "This conversation only" it is kept as what the user taught the
    Sage on purpose (the Agents tab's Teach and Correct), like a document
    — a call the user may still change (the scope doc, section 8)."""
    from council_core import council_options
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge
    monkeypatch.setenv("COUNCIL_MEMORY_SCOPE", scope)
    _teach(tmp_path)
    actions = CouncilActions(vault_dir=tmp_path)
    assert actions._scope() == scope
    models = _models()
    models.sage = Recording()
    models.judge = FakeJudge(route="sage")
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    actions.memo_llm = None
    events = []
    result = actions.send("which pump for 40 litres per minute?",
                          council_options.CouncilOptions.defaults(),
                          on_event=events.append)
    assert result.ok and "sage" in result.panel, result.message
    assert models.sage.seen
    assert any("P-200" in s for s in models.sage.seen) is given
    assert any("Sage: 2 item(s)" in e.text for e in events) is given


def test_a_gap_the_sage_admits_is_logged_to_its_knowledge(tmp_path):
    import sage_agent
    from council_core import after_turn
    from council_core.deliberation import AgentEvent
    kb = _teach(tmp_path)
    result = NS(ok=True, answer="a", critique="", verdict="PASS", panel=[],
                low_conf_gaps=[], question="what seals for acid?",
                events=[AgentEvent("Sage", "final",
                                   "Probably EPDM.\nGAP: no data on acid "
                                   "resistance — provide the seal datasheet")])
    out = after_turn.learn(NS(), result, tmp_path, memory=False)
    assert out.sage_gap
    gaps = sage_agent.SageKnowledge(tmp_path / "sage_knowledge").get_gaps()
    assert gaps and gaps[-1]["query"] == "what seals for acid?"
    assert "Sage noted a gap" in out.summary()
    confident = NS(**{**result.__dict__, "events": [
        AgentEvent("Sage", "final", "EPDM resists dilute acid.")]})
    assert after_turn.learn(NS(), confident, tmp_path,
                            memory=False).sage_gap == ""
    assert kb is not None
