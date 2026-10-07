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
    assert b.sources == ["notes/pumps.md"]           # only the user's file


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
    src = inspect.getsource(ce.PersonalityModel.respond)
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


def test_send_gives_the_vault_to_the_turn_and_takes_it_back(tmp_path):
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
    assert events[0].who == "Librarian" and "1 passage" in events[0].text


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
    src = inspect.getsource(ce.PersonalityModel.respond)
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
