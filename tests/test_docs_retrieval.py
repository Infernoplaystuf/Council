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

The review of that fix added: the ranking must not lose the standard
library (shutil.copy for "copy a whole directory tree"); a page after the
first three is read whole or not at all; a code prompt and its repair
rounds fit a 4096 window; a code answer with no def for the function the
task names goes back for repair; a copied "[n]" never takes a real
citation with it, and an ungrammared reply in the asked-for shape is JSON.
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


# ============================================================ other packages
# The bundled server ranks an object whose short name EQUALS a query word
# first (its exact-name boost). For glimmerquay that is often the answer
# (codec.MAGIC for "magic"); for the standard library and numpy it is often
# not: shutil.copy for "copy a whole directory tree", numpy.matrix for "the
# inverse of a matrix". 85695ce's fusion (1/(1 + rank) + 0.1 a word) made
# that first place nearly unbeatable: on the review's 60 such questions an
# expected page was [1] in 28 keyword-only sets (old rule 33) and 36
# model-style sets (38).

def test_one_search_the_page_with_the_question_words_beats_an_exact_name():
    """docs_context sends ONE keyword search. copytree, second with three
    question words, lost page [1] to shutil.copy (first, one word)."""
    terms = qa.content_terms("How do I copy a whole directory tree?",
                             ["shutil"])
    lists = [[_hit("shutil.copy", "Copy data and mode bits. Return the "
                                  "file's destination."),
              _hit("shutil.copytree", "Recursively copy a directory tree "
                                      "and return the destination "
                                      "directory."),
              _hit("shutil.rmtree", "Recursively delete a directory tree."),
              _hit("shutil.move", "Recursively move a file or directory.")]]
    assert qa.fuse(lists, terms)[0][0].ref == "shutil.copytree"


def test_three_searches_a_first_place_with_the_words_beats_one_without():
    """'How do I compute the inverse of a matrix?', model-style queries:
    numpy.matrix (a stub page) is first in two searches on its NAME;
    numpy.linalg.inv is first in the third, holds all three question words,
    and was page [2] under 85695ce's rule."""
    terms = qa.content_terms("How do I compute the inverse of a matrix?",
                             ["numpy"])
    inv = _hit("numpy.linalg.inv", "Compute the inverse of a matrix.")
    matrix = _hit("numpy.matrix", "class numpy.matrix()")
    pinv = _hit("numpy.linalg.pinv", "Compute the (Moore-Penrose) pseudo-"
                                     "inverse of a matrix.")
    other = _hit("numpy.matlib", "")
    lists = [[inv, other],
             [matrix, other, pinv, inv],
             [matrix, inv, pinv]]
    assert qa.fuse(lists, terms)[0][0].ref == "numpy.linalg.inv"


@pytest.fixture(scope="module")
def stdlib():
    spec = ds.bundled_spec("test retrieval pydocs", cache=False)
    yield spec
    ds.release(spec.name)


def test_the_keyword_path_reads_copytree_first(stdlib):
    """The code-behind writer's path (docs_context: one keyword search),
    the real server reading the real standard library."""
    pages = qa.docs_context("How do I copy a whole directory tree?",
                            packages=["shutil"], servers=[stdlib])
    assert pages and pages[0]["source"] == "shutil.copytree", [
        p["source"] for p in pages]


# ============================================================ how many pages

class _Pages:
    """A server with `n` hits whose pages are `size` chars each (sizes[i]
    for hit i when given); counts the pages it is asked for."""

    def __init__(self, n, size, sizes=()):
        self.n, self.size, self.fetched = n, size, []
        self.sizes = list(sizes)
        self.connected = True

    def call_tool(self, name, args, **kw):
        if name == "search":
            return ToolResult([{"type": "text", "text": json.dumps(
                [{"name": f"pkg.f{i}", "summary": "frame magic"}
                 for i in range(self.n)])}])
        self.fetched.append(args["name"])
        i = int(args["name"].rsplit("f", 1)[1])
        size = self.sizes[i] if i < len(self.sizes) else self.size
        para = "frame magic bytes. " * 4
        body = "\n\n".join([para.strip()] * (size // len(para) + 1))
        return ToolResult([{"type": "text", "text": body[:size]}])


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
    # A budget that holds only four of them reads four; the reading stops
    # once less than MIN_PAGE_CHARS is left (200 here).
    client = _Pages(8, 300)
    found = _retrieve(monkeypatch, client, context_chars=1400)
    assert len(found.pages) == 4
    assert sum(len(p.text) for p in found.pages) <= 1400
    assert len(client.fetched) == 4


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


def test_a_page_past_the_first_three_is_read_whole_or_not_at_all(
        monkeypatch):
    """`re`'s module page came 4th behind three short pages and was given
    ALL that was left: 4,882 of its 7,859 chars, 86% of the prompt, for a
    page ranked below every page the old code read. A page past the first
    three is read only whole, and only if it is no longer than a first
    page's share; a short one after it is still read."""
    client = _Pages(5, 300, sizes=[300, 300, 300, 7859, 140])
    found = _retrieve(monkeypatch, client)
    refs = [p.ref for p in found.pages]
    assert refs == ["pkg.f0", "pkg.f1", "pkg.f2", "pkg.f4"], refs
    assert max(len(p.text) for p in found.pages) <= 300
    assert [p.n for p in found.pages] == [1, 2, 3, 4]
    # Whole and within a share: read whole.
    share = qa.CONTEXT_CHARS // qa.BASE_PAGES
    client = _Pages(5, 300, sizes=[300, 300, 300, share - 50, 140])
    found = _retrieve(monkeypatch, client)
    assert [len(p.text) for p in found.pages] == [300, 300, 300, share - 50,
                                                  140]


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
    assert '"sources": [n]' in told and "where n is the number of" in told
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
        {"answer": "It is b'GQ' [n]. Index it as frame[n], rows[0][n] or "
                   "`x [n]`.", "sources": [2]}, 3, notes)
    assert answer == ("It is b'GQ'. Index it as frame[n], rows[0][n] or "
                      "`x [n]`.")
    assert cited == [2]
    assert any("[n]" in n for n in notes)
    notes = []
    assert qa.apply_citation_rules({"answer": "64 [1]", "sources": [1]}, 3,
                                   notes) == ("64 [1]", [1])
    assert notes == []


@pytest.mark.parametrize("answer,sources,want,cited", [
    # A real citation beside the placeholder survives: dropping the space
    # too made "GQ [n][1]" "GQ[1]", whose [1] no longer read as a citation.
    ("The magic bytes are GQ [n][1].", [], "The magic bytes are GQ [1].",
     [1]),
    ("The magic bytes are GQ [1][n].", [], "The magic bytes are GQ [1].",
     [1]),
    ("The magic bytes are GQ [n] [1].", [], "The magic bytes are GQ [1].",
     [1]),
    # After a quote: the shape of q05's own answer.
    ("It is b'GQ'[n].", [1], "It is b'GQ'.", [1]),
    ('It is b"GQ"[n].', [1], 'It is b"GQ".', [1]),
    ("The magic bytes are GQ ([n]).", [1], "The magic bytes are GQ.", [1]),
    ("The magic bytes are GQ [N].", [1], "The magic bytes are GQ.", [1]),
    ("[n] GQ", [1], "GQ", [1]),
])
def test_a_copied_placeholder_goes_and_a_real_citation_stays(
        answer, sources, want, cited):
    notes = []
    got = qa.apply_citation_rules({"answer": answer, "sources": sources}, 5,
                                  notes)
    assert got == (want, cited)
    assert any("[n]" in n for n in notes), notes


def test_the_format_line_copied_without_a_grammar_is_read_as_json(
        monkeypatch):
    """call_supported exists for engines without json_schema. There, a
    reply in exactly the shape asked for ("sources": [n]) is not JSON, and
    it was salvaged and reported as 'cut off by the length limit'."""
    raw = ('{"answer": "The magic bytes are GQ [n]", "sources": [n], '
           '"covered": true}')
    p = qa.parse_answer(raw, 5, False)
    assert (p["format"], p["sources"]) == ("json", [])
    assert qa.apply_citation_rules(p, 5, []) == ("The magic bytes are GQ",
                                                 [])
    # Closed but broken is malformed; only an unclosed reply is cut off.
    assert qa.parse_answer('{"answer": "GQ [1]" "sources": [1]}', 5,
                           False)["format"] == "malformed"
    assert qa.parse_answer('{"answer": "GQ [1]", "sources": [1], "cov', 5,
                           False)["format"] == "cut-off"
    # End to end, on an engine with no json_schema argument.
    monkeypatch.setattr(ds, "pooled", lambda spec, **k: (
        _Pages(3, 300), ds.Roles("search", "query", "", "get", "name")))

    def no_grammar(messages, *, temperature=0.0, num_predict=0, seed=None):
        return raw
    r = qa.ask("frame magic bytes?", servers=[ds.ServerSpec(name="x",
                                                            command="x")],
               model_call=no_grammar, derive="keywords")
    assert r.answer == "The magic bytes are GQ"
    assert not any("cut off" in n for n in r.notes), r.notes
    assert any("[n]" in n for n in r.notes), r.notes


# ============================================================ code tasks

TASKS = {t["id"]: t for t in docs_bench.load_bench()["code_tasks"]}

#: phi3.5's final c02 code on the fix branch (docsfix run): no def, and a
#: `return` at module level — ast.parse accepts it.
PHI35_C02 = ("from glimmerquay.codec import encode_frame, decode_frame, "
             "FrameError, CHECKSUMS\n\ntry:\n    frame = encode_frame("
             "b'hello', checksum='xor8', pad_to=4)\n    return frame\nexcept "
             "FrameError as e:\n    print(f'An error occurred: {e}')\n")
#: llama3.1:8b's final c05 code on the fix branch: a script, no def.
LLAMA_C05 = ("import glimmerquay\nstart = 10.0\nprint(glimmerquay.Cadence(2.5,"
             " anchor='start').windows(start, 3))\n")


def test_the_code_prompt_does_not_ask_for_the_excerpt_stating_the_answer():
    """With "where n is the number of the excerpt whose text states the
    answer" llama3.1's code-task "answer" became an API call in 4 of 5 tasks
    and its c05 code a script; the code line keeps the morning's shape."""
    pages = [qa.Source(n, "s", f"pkg.p{n}", f"pkg.p{n}", f"text {n}")
             for n in (1, 2)]
    code = qa.answer_messages("Write a function f() that adds.", pages, True)
    assert "states the answer" not in code[1]["content"]
    assert qa.WHERE_N_CODE in code[1]["content"]
    asked = qa.answer_messages("What does f return?", pages, False)
    assert qa.WHERE_N in asked[1]["content"]


def test_every_bench_task_names_its_function():
    for t in TASKS.values():
        assert qa.task_functions(t["task"]) == [t["function"]], t["id"]
    assert qa.task_functions("How do I sort an array?") == []


@pytest.mark.parametrize("tid,code,issue", [
    ("c02", PHI35_C02, "'return' outside function — `return frame`"),
    ("c05", LLAMA_C05, "first_three_windows(), but the code never defines"),
])
def test_a_script_where_a_function_was_asked_for_is_not_ok(tid, code,
                                                           issue):
    wanted = qa.task_functions(TASKS[tid]["task"])
    check = qa.check_code(code, [], ["glimmerquay"], wanted=wanted)
    assert not check.ok and any(issue in i for i in check.issues), \
        check.issues
    assert qa.check_code(TASKS[tid]["reference"], [], ["glimmerquay"],
                         wanted=wanted).ok


def test_the_script_answer_gets_a_repair_round(server):
    """Both scripts went out on the first answer call (no repair) and
    failed their hidden tests."""
    task = TASKS["c05"]
    replies = iter([
        json.dumps({"queries": ["Cadence windows"], "package": PKG[0]}),
        json.dumps({"answer": "Cadence.windows [1]", "sources": [1],
                    "covered": True, "code": LLAMA_C05}),
        json.dumps({"answer": "Uses Cadence.windows [1]", "sources": [1],
                    "covered": True, "code": task["reference"]})])

    def model(messages, **kw):
        return next(replies)
    r = qa.ask(task["task"], servers=[server], packages=PKG,
               write_code=True, model_call=model)
    assert r.model_calls == 3 and r.code_ok, (r.model_calls, r.notes)
    assert any("never defines" in n for n in r.notes), r.notes
    assert docs_bench.run_code_test(r.code, task)["passed"]


def test_code_reads_less_and_a_repair_round_is_no_bigger(monkeypatch):
    """A 4096 window: a code reply may run to 1100 tokens, and a repair
    round re-sends the prompt plus the reply. The engine cut the middle out
    of what did not fit — page headers included."""
    client = _Pages(5, 20_000)
    monkeypatch.setattr(ds, "pooled", lambda spec, **k: (
        client, ds.Roles("search", "query", "", "get", "name")))
    reply = json.dumps({"answer": "It frames [1]", "sources": [1],
                        "covered": True,
                        "code": "def f():\n    return nope\n" + "#" * 600})
    sent = []

    def model(messages, **kw):
        sent.append(messages)
        return reply
    r = qa.ask("Write a function f() that frames magic bytes",
               servers=[ds.ServerSpec(name="x", command="x")],
               write_code=True, model_call=model, derive="keywords",
               max_repairs=1)
    assert sum(len(p.text) for p in r.sources) <= qa.CODE_CONTEXT_CHARS
    first, repair = (sum(len(m["content"]) for m in ms) for ms in sent)
    assert repair <= first + 60, (first, repair)
    # Every page is still there, under its own number.
    shown = sent[1][1]["content"]
    assert all(f"[{p.n}] {p.title}\n" in shown for p in r.sources)


# ============================================================ the query cap

def test_the_query_cap_covers_the_longest_valid_reply():
    """120 tokens against a schema whose longest reply is ~198: a reply at
    the schema's limits, pretty-printed as the models write it, is 282
    chars, ~124 tokens at 2.28 chars/token (phi3.5's densest recorded query
    reply, by Ollama's count) — past 120. The most any recorded query reply
    used was 78."""
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
