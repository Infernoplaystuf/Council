"""
What the council keeps from an answered question (council_core.after_turn):
low-confidence gaps on the librarian's wishlist, and role / project memory.

Pinned: gaps reach the wishlist file; only roles that took part AND may
write memory (council_engine.MEMORY_WRITE_ROLES — never the Writer or the
Judge) update it; one observer writes the project memory; the switch turns
memory off but not the wishlist; a failure is reported, never raised;
run_turn carries the gaps and the question out.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import after_turn as at  # noqa: E402
from council_core import council_turn as ct  # noqa: E402
from tests.test_council_turn import FakeJudge, FakeModel  # noqa: E402

GAP = {"who": "sage", "topic": "sage answer to: how big is the pump",
       "reason": "sage self-reported confidence 3/10"}


class Engine:
    MEMORY_WRITE_ROLES = {"coder", "intern", "sage", "strategist"}

    def __init__(self, fail=()):
        self.roles, self.project, self.fail = [], [], set(fail)

    def update_role_memory_after_pass(self, *, role_name, **kw):
        if role_name in self.fail:
            raise RuntimeError(f"{role_name} broke")
        self.roles.append((role_name, kw["user_text"], kw["passed"]))

    def update_project_memory_after_pass(self, *, role_name, **kw):
        self.project.append(role_name)


class Librarian:
    def __init__(self):
        self.gaps = []

    def log_gap(self, who, topic, reason):
        self.gaps.append((who, topic, reason))


def _models(scope="all"):
    """Personalities in memory scope ``scope`` — "all" unless a test says
    otherwise: the memory updates read the memory into a prompt, and only
    "all" lets it in (council_core.memory_scope)."""
    mgr = object()
    names = ("writer", "judge", "coder", "sage", "intern", "peasant")
    return NS(**{n: NS(memory_manager=mgr, memory_scope=scope)
                 for n in names})


def _result(**kw):
    base = dict(ok=True, answer="the answer", critique="Verdict: PASS",
                verdict="PASS", panel=["coder", "sage", "writer", "judge"],
                low_conf_gaps=[GAP], question="how big is the pump")
    base.update(kw)
    return NS(**base)


def test_only_writing_roles_that_took_part_update_memory(tmp_path):
    eng, lib = Engine(), Librarian()
    out = at.learn(_models(), _result(), tmp_path, engine=eng, librarian=lib,
                   memory=True)
    assert [r for r, _q, _p in eng.roles] == ["coder", "sage"]
    assert eng.roles[0][1:] == ("how big is the pump", True)
    assert eng.project == ["coder"]                 # one observer
    assert lib.gaps == [("sage", GAP["topic"], GAP["reason"])]
    assert out.gaps_logged == 1 and out.roles_updated == ["coder", "sage"]
    assert "memory updated for coder, sage" in out.summary()


def test_this_conversation_only_writes_no_memory(tmp_path):
    """The Qt shell's scope, "This conversation only", keeps role and
    project memory out of every prompt — and the update would put the
    memory a role has into its call (RoleMemoryManager.merge_update). So
    nothing is read or written; the wishlist, a file append, still is."""
    for scope in ("conversation", "none"):
        eng, lib = Engine(), Librarian()
        out = at.learn(_models(scope), _result(), tmp_path, engine=eng,
                       librarian=lib, memory=True)
        assert eng.roles == [] and eng.project == [], scope
        assert out.roles_updated == [] and out.project_updated_by == ""
        assert out.gaps_logged == 1, scope


def test_the_memory_scope_of_the_loaded_personalities_is_honoured(
        tmp_path):
    """The real door: load_personalities gives every personality the Qt
    scope, and learn reads it from the model, as stitch() does."""
    import council_engine as ce
    mgr = ce.RoleMemoryManager(tmp_path / "memory")
    mgr.update("coder", "- an older conversation's lesson")
    asked = []

    class Model:
        memory_manager = mgr
        memory_scope = "conversation"

        def respond(self, prompt, **kw):
            asked.append(prompt)
            return "- a new lesson"

    models = NS(writer=Model(), judge=Model(), coder=Model(), sage=Model())
    out = at.learn(models, _result(panel=["coder"]), tmp_path,
                   librarian=Librarian(), memory=True)
    assert asked == [], "a memory update was asked for"
    assert out.roles_updated == []
    assert mgr.read("coder").strip() == "- an older conversation's lesson"


def test_memory_off_still_logs_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_MEMORY_AFTER_TURN", "0")
    eng, lib = Engine(), Librarian()
    out = at.learn(_models(), _result(), tmp_path, engine=eng, librarian=lib)
    assert eng.roles == [] and eng.project == []
    assert out.gaps_logged == 1


def test_a_failed_turn_keeps_nothing_but_its_gaps(tmp_path):
    eng, lib = Engine(), Librarian()
    out = at.learn(_models(), _result(ok=False, answer=""), tmp_path,
                   engine=eng, librarian=lib, memory=True)
    assert eng.roles == [] and out.gaps_logged == 1


def test_a_failure_is_reported_not_raised(tmp_path):
    eng = Engine(fail={"coder"})
    out = at.learn(_models(), _result(), tmp_path, engine=eng,
                   librarian=Librarian(), memory=True)
    assert out.roles_updated == ["sage"]
    assert "coder broke" in out.errors[0]
    assert "problem" in out.summary()


def test_gaps_reach_the_real_wishlist_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert at.log_gaps(vault, [GAP]) == 1
    text = (vault / "librarian_wishlist.md").read_text(encoding="utf-8")
    assert "sage" in text and "how big is the pump" in text


class LowConfidence(FakeModel):
    def respond(self, prompt, **kw):
        if "CONFIDENCE: <0-100>%" in prompt:
            return "a guess\nCONFIDENCE: 30% — the pump's size"
        return super().respond(prompt, **kw)


def test_run_turn_carries_the_gaps_and_the_question_out():
    class Models:
        pass
    models = Models()
    for name in ct.AGENT_NAMES:
        setattr(models, name, None)
    models.writer = LowConfidence()
    models.coder = LowConfidence()
    models.judge = FakeJudge()
    result = ct.run_turn("how big is the pump", models, max_rounds=1,
                         debate_turns=1)
    assert result.ok, result.message
    assert result.question == "how big is the pump"
    assert result.low_conf_gaps
    assert {g["who"] for g in result.low_conf_gaps} <= set(result.panel)


def test_a_gap_names_the_typed_question_not_the_text_in_front_of_it(
        tmp_path, monkeypatch):
    """Found in review: the Qt Council sends the augmented text (the task
    memo and the vault in front of the question) to run_turn, and a gap's
    topic was its first 80 characters — "writer answer to: [TASK MEMO — the
    user's ORIGINAL request …\\ngoal: wha" — so the wishlist line named the
    memo, and its newline split the list item in two. The topic names what
    was typed, on one line."""
    from council_core import council_options
    from council_core import vault_context as vc
    from council_core.docs_brief import DocsBrief
    from council_qt.tabs.council import CouncilActions
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "pumps.csv").write_text(
        "bay,pump,size\n4,P-17,large\n5,P-9,small\n", encoding="utf-8")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.delenv("COUNCIL_MEMORY_SCOPE", raising=False)
    actions = CouncilActions(vault_dir=vault)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.peasant = LowConfidence(), LowConfidence()
    models.judge = FakeJudge()
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    actions.docs_brief = lambda q, m: DocsBrief()
    actions.memo_llm = None
    opts = council_options.CouncilOptions(deliberate=True)
    opts.depth = "deep"                 # the Judge ranks only past Quick
    typed = "How big is the pump\nin bay 4 (see pumps.csv)?"
    result = actions.send(typed, opts)
    assert result.ok, result.message
    topics = [g["topic"] for g in result.low_conf_gaps]
    assert topics, "no gaps"
    for topic in topics:
        assert "TASK MEMO" not in topic and "\n" not in topic, topic
        assert topic.endswith("answer to: How big is the pump in bay 4 "
                              "(see pumps.csv)?"), topic
    assert at.log_gaps(vault, result.low_conf_gaps) == len(topics)
    lines = (vault / "librarian_wishlist.md").read_text(
        encoding="utf-8").splitlines()
    items = [ln for ln in lines if ln.startswith("- [ ]")]
    assert len(items) == len(topics), lines
    assert all("How big is the pump in bay 4" in ln for ln in items), items


def test_with_the_real_engine_memory_files_are_written(tmp_path):
    import council_engine as ce
    mgr = ce.RoleMemoryManager(tmp_path / "memory")

    class Model:
        memory_manager = mgr
        memory_scope = "all"            # the scope that keeps memory

        def respond(self, prompt, **kw):
            return "- Pumps here are sized in litres per minute."

    models = NS(writer=Model(), judge=Model(), coder=Model(), sage=Model())
    out = at.learn(models, _result(panel=["coder", "writer"]), tmp_path,
                   librarian=Librarian(), memory=True)
    assert out.roles_updated == ["coder"], out.errors
    assert "litres per minute" in mgr.read("coder")
    assert mgr.read("writer") == ""                  # read-only role
    assert out.project_updated_by == "coder"
    assert "litres per minute" in mgr.read(ce._PROJECT_MEMORY_KEY)
