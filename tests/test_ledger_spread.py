"""
The Ledger's model questions on several machines (council_core.ledger_spread).

Fake models stand in for this PC and another machine (tests run offline).
Pinned: nothing is spread unless a machine in Machines & roles takes shared
work; spread, the review is the SAME as asked on this PC alone (values,
parts, order); questions really run side by side; a machine that fails is
not asked again and its question goes elsewhere; Cancel while answers are
out gives a cancelled run without those workbooks; a table template's
header layouts are asked at once.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_spread as ls  # noqa: E402
from council_core import node_routing as nr  # noqa: E402


class _NoProg:
    def update(self, *a, **k):
        pass

    def stage(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass


class Fake:
    """Answers the part question with the serial in the prompt, and the
    label question with nothing; counts how many it held at once."""
    live = 0
    most = 0
    lock = threading.Lock()

    def __init__(self, name, delay=0.15, fail=False, on_call=None):
        self.name, self.delay, self.fail, self.on_call = name, delay, fail, on_call
        self.calls = 0

    def __call__(self, prompt, schema):
        with Fake.lock:
            Fake.live += 1
            Fake.most = max(Fake.most, Fake.live)
            self.calls += 1
        try:
            if self.on_call:
                self.on_call(self)
            time.sleep(self.delay)
            if self.fail:
                raise RuntimeError(f"{self.name} is down")
            if "Which part" in prompt:
                m = re.search(r"Serial: (PRT\d+)", prompt)
                return json.dumps({"part": m.group(1) if m else "",
                                   "evidence": "Serial"}), "fake-granite"
            return json.dumps({"mappings": []}), "fake-granite"
        finally:
            with Fake.lock:
                Fake.live -= 1


@pytest.fixture(autouse=True)
def _reset():
    Fake.live = Fake.most = 0
    yield


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    monkeypatch.delenv("COUNCIL_LEDGER_SPREAD", raising=False)
    return v


def _machines(vault, *nodes):
    nr.save(vault, nr.Routing(True, {n.name: n for n in nodes}))


def _books(tmp_path, n=6):
    out = []
    for i in range(n):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = f"Serial: PRT{1000 + i}"
        ws["A3"], ws["B3"] = "Power (W)", 200 + i
        p = tmp_path / f"book{i}.xlsx"
        wb.save(p)
        out.append(str(p))
    return out


def _ledger(tmp_path):
    ps = li.create_parent(lf.make_template(tmp_path / "template.xlsx"))
    return str(ps.csv)


def _rows(review):
    return [dict(zip(se.VAL_HEAD, r)) for r in openpyxl.load_workbook(review)[
        "Values"].iter_rows(min_row=2, values_only=True)]


# ── which machines ────────────────────────────────────────────────────────
def test_nothing_is_spread_without_a_machine_for_shared_work(vault):
    assert ls.build(Fake("pc")) is None
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 1))   # no model
    assert ls.build(Fake("pc")) is None
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 2, "gemma3:12b"))
    spread = ls.build(Fake("pc"))
    assert spread.names() == [ls.THIS_PC, "pc2"] and spread.capacity == 3


def test_it_can_be_turned_off(vault, monkeypatch):
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 1, "gemma3:12b"))
    monkeypatch.setenv("COUNCIL_LEDGER_SPREAD", "0")
    assert ls.build(Fake("pc")) is None


# ── the spread chat ───────────────────────────────────────────────────────
def test_a_failing_machine_is_not_asked_again_and_its_question_goes_elsewhere():
    down = Fake("pc2", delay=0, fail=True)
    here = Fake("here", delay=0)
    s = ls.Spread([ls.Machine("pc2", down, 4), ls.Machine(ls.THIS_PC, here, 1)])
    for _ in range(3):
        text, model = s("Which part? Serial: PRT1", {})
        assert json.loads(text)["part"] == "PRT1"
    assert down.calls == 1 and here.calls == 3
    assert "pc2 stopped answering (pc2 is down)" in s.summary()


def test_every_machine_failing_fails_the_question():
    s = ls.Spread([ls.Machine("a", Fake("a", 0, True), 1),
                   ls.Machine("b", Fake("b", 0, True), 1)])
    with pytest.raises(RuntimeError, match="is down"):
        s("x", {})


def test_questions_run_side_by_side_but_their_counting_does_not():
    here, there = Fake("here"), Fake("there")
    s = ls.Spread([ls.Machine(ls.THIS_PC, here, 1), ls.Machine("pc2", there, 2)])
    inside = {"n": 0, "most": 0}

    def question():
        inside["n"] += 1                       # holding the baton
        inside["most"] = max(inside["most"], inside["n"])
        inside["n"] -= 1
        out = s("Which part? Serial: PRT7", {})
        inside["n"] += 1
        inside["most"] = max(inside["most"], inside["n"])
        inside["n"] -= 1
        return out
    start = time.monotonic()
    results = s.map([question] * 6)
    took = time.monotonic() - start
    assert all(e is None for _v, e in results)
    assert Fake.most == 3                      # three calls at once: 1 + 2 slots
    assert inside["most"] == 1                 # never two counting at once
    assert took < 6 * 0.15                     # faster than one at a time
    assert here.calls + there.calls == 6


# ── the template Ledger's part questions ──────────────────────────────────
def _extract(csv, books, model, cancel=None, review=None):
    return li._extract(csv, books, review, "", False, None, _NoProg(), None, cancel,
                       None, True, model)


@pytest.mark.parametrize("helpers", ["0", "2"])
def test_spread_gives_the_same_review_as_this_pc_alone(tmp_path, vault, monkeypatch,
                                                       helpers):
    # "2": files go to helper processes first; the part-less ones come back
    # to be read here, between the helpers' results, still in file order
    monkeypatch.setenv("COUNCIL_LEDGER_WORKERS", helpers)
    csv = _ledger(tmp_path)
    books = _books(tmp_path)
    alone = _extract(csv, books, Fake("here"),
                     review=tmp_path / "alone.xlsx")
    assert Fake.most == 1
    there = Fake("pc2")
    monkeypatch.setattr(ls, "node_chat", lambda *a, **k: there)
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 2, "gemma3:12b"))
    Fake.most = 0
    shared = _extract(csv, books, Fake("here"), review=tmp_path / "shared.xlsx")
    assert Fake.most >= 2 and there.calls >= 1
    a, b = _rows(alone.review), _rows(shared.review)
    assert a == b and len(a) == 6
    assert [r["Part"] for r in b] == [f"PRT{1000 + i}" for i in range(6)]
    assert "Shared across machines: " in shared.assist and "pc2 answered" in shared.assist
    assert not shared.cancelled


def test_a_machine_down_mid_run_costs_nothing(tmp_path, vault, monkeypatch):
    csv = _ledger(tmp_path)
    books = _books(tmp_path, 4)
    monkeypatch.setattr(ls, "node_chat", lambda *a, **k: Fake("pc2", 0.05, fail=True))
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 1, "gemma3:12b"))
    s = _extract(csv, books, Fake("here", 0.05))
    assert [r["Part"] for r in _rows(s.review)] == [f"PRT{1000 + i}" for i in range(4)]
    assert "pc2 stopped answering" in s.assist


def test_cancel_while_answers_are_out_is_a_cancelled_run(tmp_path, vault, monkeypatch):
    csv = _ledger(tmp_path)
    books = _books(tmp_path, 6)
    cancel = threading.Event()

    def slow(fake):
        if fake.calls >= 2:
            cancel.set()
            time.sleep(0.6)
    monkeypatch.setattr(ls, "node_chat", lambda *a, **k: Fake("pc2", 0.3, on_call=slow))
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 2, "gemma3:12b"))
    s = _extract(csv, books, Fake("here", 0.3, on_call=slow), cancel=cancel,
                 review=tmp_path / "r.xlsx")
    assert s.cancelled
    assert any("Cancelled by user" in p for p in s.problems)
    parts = {r["Part"] for r in _rows(s.review)}
    assert len(parts) < 6                     # the ones still out are not in it


# ── a table template's header layouts ─────────────────────────────────────
def test_header_layouts_are_asked_at_once(tmp_path, vault, monkeypatch):
    from tests.test_ledger_records import book, simple_ledger, table
    _tpl, csv = simple_ledger(tmp_path)
    files = [book(tmp_path / f"{n}.xlsx",
                  {"S": table(1, 1, ["Name", h, "Sex"], [[f"{n}1", 1, "M"],
                                                         [f"{n}2", 3, "F"]])})
             for n, h in (("a", "HP"), ("b", "Hit points"), ("c", "Vitality"))]

    class Headers(Fake):
        def __call__(self, prompt, schema):
            super().__call__("", {})
            heads = schema["properties"]["mappings"]["items"]["properties"]["header"]["enum"]
            ans = {"hp": "Health", "hit points": "Health", "vitality": "Health",
                   "sex": "Gender"}
            return json.dumps({"mappings": [{"header": h, "field": ans.get(h)}
                                            for h in heads]}), "fake-granite"
    # each answer takes 1 s: building a question (done one at a time) took
    # over 0.2 s on a slow Windows runner, so 0.2 s answers never overlapped
    monkeypatch.setattr(ls, "node_chat", lambda *a, **k: Headers("pc2", 1.0))
    _machines(vault, nr.Node("pc2", "http://10.0.0.5:11434", True, 2, "gemma3:12b"))
    ex = li._extract_records(csv, files, tmp_path / "r.xlsx", _NoProg(), None,
                             "ollama:none", chat=Headers("here", 1.0))
    assert Fake.most >= 2
    assert "3 header layout(s)" in ex.assist and "Shared across machines" in ex.assist
    assert ex.records == 6
