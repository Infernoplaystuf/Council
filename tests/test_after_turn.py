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


def _models():
    mgr = object()
    names = ("writer", "judge", "coder", "sage", "intern", "peasant")
    return NS(**{n: NS(memory_manager=mgr) for n in names})


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
        if "Rate your confidence" in prompt:
            return "3"
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


def test_with_the_real_engine_memory_files_are_written(tmp_path):
    import council_engine as ce
    mgr = ce.RoleMemoryManager(tmp_path / "memory")

    class Model:
        memory_manager = mgr

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
