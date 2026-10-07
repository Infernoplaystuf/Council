"""
Documentation for the council's Coder (council_core.docs_brief).

Pinned: only a question the Judge routes to the coding panel fetches docs;
the block names the pages and stays inside its budget; a missing or failing
documentation server means no block, never an error; the Coder and the
Writer read it for the question and no one else; the switch turns it off.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import docs_brief as db  # noqa: E402

PAGES = [{"server": "bundled", "source": "shutil.copytree",
          "title": "shutil.copytree",
          "text": "copytree(src, dst, symlinks=False, ignore=None, "
                  "dirs_exist_ok=False) — recursively copy a directory."},
         {"server": "bundled", "source": "shutil.ignore_patterns",
          "title": "shutil.ignore_patterns",
          "text": "ignore_patterns(*patterns) — a callable for copytree."}]


def fetch(pages=PAGES, calls=None):
    def f(question, max_chars=6000):
        if calls is not None:
            calls.append(question)
        return pages
    return f


def test_only_coding_questions_fetch_docs():
    calls = []
    assert db.build("copy a folder", "chat", fetch=fetch(calls=calls)).text \
        == ""
    assert calls == []
    b = db.build("copy a folder tree", "ide", fetch=fetch(calls=calls))
    assert calls == ["copy a folder tree"]
    assert b.text.startswith("DOCUMENTATION")
    assert "dirs_exist_ok=False" in b.text
    assert b.titles == ["shutil.copytree", "shutil.ignore_patterns"]
    assert "2 page(s) for the Coder" in b.note()


def test_the_block_stays_inside_its_budget():
    big = [{"title": f"p{i}", "text": "x" * 5000} for i in range(6)]
    b = db.build("q", "coder", fetch=fetch(big), max_chars=3000)
    assert len(b.text) <= 3000 + len(db.HEADER)


def test_no_server_or_a_failing_one_means_no_block():
    assert db.build("q", "ide", fetch=fetch([])).text == ""

    def boom(question, max_chars=0):
        raise OSError("server down")
    assert db.build("q", "ide", fetch=boom).text == ""


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("COUNCIL_DOCS_FOR_CODER", "0")
    assert db.build("q", "ide", fetch=fetch()).text == ""


def test_route_of_asks_the_judge_and_survives_a_bad_one():
    assert db.route_of(NS(judge=NS(route=lambda q: "ide")), "q") == "ide"
    assert db.route_of(NS(judge=None), "q") == ""

    def broken(q):
        raise RuntimeError("no")
    assert db.route_of(NS(judge=NS(route=broken)), "q") == ""


class Member:
    def __init__(self):
        self.extra_context, self.seen = "", []

    def respond(self, prompt, **kw):
        self.seen.append(self.extra_context)
        return "7" if "Rate your confidence" in prompt else "code"


def test_the_coder_and_writer_read_the_docs_for_the_question(tmp_path,
                                                            monkeypatch):
    from council_core import council_options, council_turn as ct
    from council_core import docs_qa
    from council_core import vault_context as vc
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge
    monkeypatch.setattr(docs_qa, "docs_context", fetch())
    actions = CouncilActions(vault_dir=tmp_path)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    for role in ("writer", "coder", "intern", "skeptic"):
        setattr(models, role, Member())
    models.judge = FakeJudge(route="ide")       # coder, intern, skeptic
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    actions.memo_llm = None
    events = []
    result = actions.send("copy a folder tree, skipping .pyc files",
                          council_options.CouncilOptions.defaults(),
                          on_event=events.append)
    assert result.ok, result.message
    assert any("DOCUMENTATION" in s for s in models.coder.seen)
    assert any("DOCUMENTATION" in s for s in models.writer.seen)
    assert not any("DOCUMENTATION" in s for s in models.intern.seen)
    assert not any("DOCUMENTATION" in s for s in models.skeptic.seen)
    assert models.coder.extra_context == ""
    assert any(e.text.startswith("Docs: 2 page(s)") for e in events)
