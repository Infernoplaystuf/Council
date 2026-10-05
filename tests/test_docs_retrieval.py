"""
council_core.docs_qa — which pages a question reads, and how the answer
prompt asks for citations. Pinned by the 2026-10-05 bench run.

q05 ("What two magic bytes does every glimmerquay frame start with?") failed
as wrong_citation on llama3.1:8b, phi3.5 and both qwen2.5 models, and q03 on
two of them, with the RIGHT answer each time:

  * the fusion made glimmerquay.FrameError — third in all three searches,
    "wrong magic bytes" in its summary — page [1], and cut
    glimmerquay.codec.MAGIC (first in two searches) and glimmerquay.codec,
    the pages that hold b"GQ", at a fixed three pages;
  * the answer prompt said "Cite ... like [1]" and showed
    {"answer": "... [1]", "sources": [1]}, and the small models cited [1];
  * the query call's 120-token cap was below the longest reply its own
    schema allows (the engine warned every run).

The queries below are the ones the models really sent (re-derived from the
recorded replies). Retrieval is real: the bundled server, a subprocess,
serving the benchmark package. No model runs here.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import docs_bench, docs_qa as qa, docs_servers as ds  # noqa
from council_core import structured_output as so  # noqa: E402
from council_core.mcp_client import ToolResult  # noqa: E402

PKG = ["glimmerquay"]

Q05 = "What two magic bytes does every glimmerquay frame start with?"
#: llama3.1:8b's (and phi4:14b's) queries for q05.
Q05_QUERIES = ["glimmerquay frame magic bytes", "glimmerquay frame header",
               "magic bytes frame start"]
Q03 = "Which overflow modes does a glimmerquay Ledger support?"
#: phi3.5's queries for q03.
Q03_QUERIES = ["glimmerquay overflow modes",
               "GlimmerQuay supported overflow types",
               "overflow modes Ledger support"]


@pytest.fixture(scope="module")
def server():
    spec = docs_bench.bench_server()
    yield spec
    ds.release(spec.name)


# ============================================================ ranking

def test_q05_the_page_a_search_ranks_first_is_read_first(server):
    """FrameError scored 0.400 (in every list, third) against MAGIC's 0.250
    (first in two lists) and became page [1]; MAGIC and codec, both holding
    b"GQ", were 4th and 5th and never read."""
    found = qa.retrieve(Q05, Q05_QUERIES, servers=[server], packages=PKG)
    refs = [p.ref for p in found.pages]
    assert refs[0] == "glimmerquay.codec.MAGIC", refs
    assert "b'GQ'" in found.pages[0].text
    assert "glimmerquay.codec" in refs, refs
    assert "glimmerquay.encode_frame" in refs, refs


def test_q03_the_class_is_read_before_its_exception(server):
    """phi3.5's queries put LedgerFullError second in two lists and third in
    one; Ledger was first in one. The exception became page [1]."""
    found = qa.retrieve(Q03, Q03_QUERIES, servers=[server], packages=PKG)
    assert found.pages[0].ref == "glimmerquay.Ledger", [
        p.ref for p in found.pages]


def _hit(ref, summary=""):
    return qa.Hit("s", ref, ref, summary)


def test_a_first_place_outranks_a_page_merely_present_everywhere():
    """The q05 shape with invented names: `hub` is third in every search
    and its summary holds three question words; `answer` is first in two
    searches and its summary holds one."""
    terms = qa.content_terms(Q05, PKG)
    hub = _hit("pkg.FrameError", "Raised when a frame has wrong magic bytes")
    answer = _hit("pkg.MAGIC", "pkg.MAGIC = b'GQ'")
    other = _hit("pkg.decode", "Return the payload of a frame")
    noise = _hit("pkg", "a toolkit")
    lists = [[answer, noise, hub, other],
             [noise, other, hub],
             [answer, other, hub]]
    ranked = qa.fuse(lists, terms)
    order = [h.ref for h, _score, _words in ranked]
    assert order[0] == "pkg.MAGIC", ranked
    words = {h.ref: w for h, _s, w in ranked}
    assert words["pkg.FrameError"] > words["pkg.MAGIC"], words
    # The question words still order pages the searches rank alike.
    tie = qa.fuse([[_hit("a", "nothing"), _hit("b", "magic bytes")],
                   [_hit("b", "magic bytes"), _hit("a", "nothing")]], terms)
    assert [h.ref for h, _s, _w in tie] == ["b", "a"]


# ============================================================ how many pages

class _Pages:
    """A server with `n` hits whose pages are `size` chars each; counts the
    pages it is asked for."""

    def __init__(self, n, size):
        self.n, self.size, self.fetched = n, size, []
        self.connected = True

    def call_tool(self, name, args, **kw):
        if name == "search":
            return ToolResult([{"type": "text", "text": json.dumps(
                [{"name": f"pkg.f{i}", "summary": "frame magic"}
                 for i in range(self.n)])}])
        self.fetched.append(args["name"])
        para = "frame magic bytes. " * 4
        body = "\n\n".join([para.strip()] * (self.size // len(para) + 1))
        return ToolResult([{"type": "text", "text": body[:self.size]}])


def _retrieve(monkeypatch, client, **kw):
    monkeypatch.setattr(ds, "pooled", lambda spec, **k: (
        client, ds.Roles("search", "query", "", "get", "name")))
    return qa.retrieve("frame magic bytes", ["frame magic"],
                       servers=[ds.ServerSpec(name="x", command="x")], **kw)


def test_short_pages_are_read_past_three_while_they_fit(monkeypatch):
    """MAGIC's page is one line. q05's three pages used 1,175 of the 6,000
    chars while the answer sat on the 4th and 5th."""
    client = _Pages(8, 300)
    found = _retrieve(monkeypatch, client)
    assert len(found.pages) == 5 == qa.MAX_PAGES
    assert [p.n for p in found.pages] == [1, 2, 3, 4, 5]
    assert len(client.fetched) == 5
    # A budget that holds only four of them reads four.
    client = _Pages(8, 300)
    found = _retrieve(monkeypatch, client, context_chars=1200)
    assert len(found.pages) == 4
    assert sum(len(p.text) for p in found.pages) <= 1200


def test_long_pages_are_read_as_before(monkeypatch):
    """Pages that fill the budget: the first three share it exactly as they
    did, and a page past them costs at most one more fetch. (This passes on
    the old code too — it pins what must not change.)"""
    client = _Pages(8, 20_000)
    found = _retrieve(monkeypatch, client)
    assert len(found.pages) == 3
    assert sum(len(p.text) for p in found.pages) <= qa.CONTEXT_CHARS
    assert len(client.fetched) <= 4
    assert len(found.pages[0].text) <= qa.CONTEXT_CHARS // 3


# ============================================================ the prompt

def _instructions(messages):
    """Everything the model reads that is not documentation."""
    user = messages[1]["content"]
    tail = re.split(r"\n\n(?=QUESTION\n|TASK\n)", user, maxsplit=1)[1]
    return messages[0]["content"] + "\n" + tail


@pytest.mark.parametrize("write_code", [False, True])
def test_the_answer_prompt_has_no_page_number_to_copy(write_code):
    """phi3.5 and qwen2.5:7b cited [1] for all 10 questions — the number in
    "Cite ... like [1]" and {"answer": "... [1]", "sources": [1]}."""
    pages = [qa.Source(n, "s", f"pkg.p{n}", f"pkg.p{n}", f"text {n}")
             for n in (1, 2, 3)]
    messages = qa.answer_messages("What is it?", pages, write_code)
    told = _instructions(messages)
    assert not re.search(r"\[\s*\d+\s*\]", told), told
    assert '"sources": [n]' in told and "number of the excerpt" in told
    assert "whose text states" in messages[0]["content"]
    # The shape is the one the schema enforces, and a reply in it is valid.
    for key in qa.answer_schema(3, write_code)["required"]:
        assert f'"{key}":' in told
    reply = {"answer": "It is 7 [2].", "sources": [2], "covered": True}
    if write_code:
        reply["code"] = "x = 7"
    ok, errors = so.check_text(json.dumps(reply),
                               qa.answer_schema(3, write_code))
    assert ok, errors
    # The documentation keeps its own numbers.
    assert "[2] pkg.p2" in messages[1]["content"]


def test_a_copied_placeholder_is_removed_not_shown():
    notes = []
    answer, cited = qa.apply_citation_rules(
        {"answer": "It is b'GQ' [n]. Index it as frame[n], or `x [n]`.",
         "sources": [2]}, 3, notes)
    assert answer == "It is b'GQ'. Index it as frame[n], or `x [n]`."
    assert cited == [2]
    assert any("[n]" in n for n in notes)
    notes = []
    assert qa.apply_citation_rules({"answer": "64 [1]", "sources": [1]}, 3,
                                   notes) == ("64 [1]", [1])
    assert notes == []


# ============================================================ the query cap

def test_the_query_cap_covers_the_longest_valid_reply():
    """120 tokens against a schema whose longest reply is ~198: a reply at
    the schema's limits, pretty-printed as the models write it, is ~116
    tokens at phi3.5's 2.44 chars/token — and is cut with a few escapes."""
    worst = so.worst_case_tokens(qa.QUERY_SCHEMA)
    assert worst is not None
    assert qa.QUERY_NUM_PREDICT >= worst
    seen = {}

    def model(messages, *, json_schema=None, temperature=0.0,
              num_predict=0, seed=None):
        seen["num_predict"] = num_predict
        return json.dumps({"queries": ["frame magic"], "package": "pkg"})

    queries, _pkg, info = qa.derive_queries("frame magic bytes?", (), model)
    assert info["ok"] and seen["num_predict"] == qa.QUERY_NUM_PREDICT
