"""
council_core.docs_qa — grounded answers and checked code, with stub models.

Retrieval is REAL: the bundled server, a real subprocess, serving the
benchmark's invented package. Only the model is scripted — each test hands
the replies a model would give and checks what the Council does with them:
the prompts it sent, the schema it asked for, the citations it kept, the
code problems it caught and how it asked for repairs.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import docs_bench, docs_qa as qa, docs_servers as ds  # noqa
from council_core import model_slots  # noqa: E402

PKG = ["glimmerquay"]


@pytest.fixture(scope="module")
def server():
    spec = docs_bench.bench_server()
    yield spec
    ds.release(spec.name)


class Stub:
    """A model that says what it is told, and remembers what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, *, json_schema=None, temperature=0.1,
                 num_predict=600, seed=None):
        self.calls.append({"messages": messages, "schema": json_schema,
                           "seed": seed, "num_predict": num_predict})
        if not self.replies:
            raise AssertionError("the model was called more often than "
                                 "expected")
        r = self.replies.pop(0)
        return r(messages) if callable(r) else r


def q(*queries, package="glimmerquay"):
    return json.dumps({"queries": list(queries), "package": package})


def ans(text, sources=(1,), covered=True, code=None):
    d = {"answer": text, "sources": list(sources), "covered": covered}
    if code is not None:
        d["code"] = code
    return json.dumps(d)


def ask(question, model, server, **kw):
    kw.setdefault("packages", PKG)
    return qa.ask(question, servers=[server], model_call=model, **kw)


def page_number(result, ref):
    return next(s.n for s in result.sources if s.ref == ref)


# ============================================================ answering

def test_a_grounded_answer_cites_the_page_it_came_from(server):
    model = Stub(q("Ledger capacity"),
                 lambda m: ans("The default capacity is 64 [1].", [1]))
    r = ask("What is the default capacity of a Ledger?", model, server)
    assert r.ok and r.covered and r.grounded, r.notes
    assert r.cited == [1]
    assert r.sources[0].ref == "glimmerquay.Ledger"
    assert "capacity: int = 64" in r.sources[0].text
    # the query call was schema-constrained, the answer call too
    assert model.calls[0]["schema"] == qa.QUERY_SCHEMA
    schema = model.calls[1]["schema"]
    assert schema["properties"]["sources"]["items"]["enum"] == list(
        range(1, len(r.sources) + 1))
    prompt = model.calls[1]["messages"][1]["content"]
    assert prompt.startswith("DOCUMENTATION\n[1] glimmerquay.Ledger")
    assert "QUESTION\nWhat is the default capacity" in prompt


def test_the_keyword_query_always_joins_the_models(server):
    model = Stub(q("capacity"), ans("64 [1]"))
    r = ask("What is the default capacity of a glimmerquay Ledger?", model,
            server, packages=())
    assert r.queries[0] == "capacity"
    assert any("Ledger" in x for x in r.queries[1:]), r.queries


def test_junk_instead_of_queries_falls_back_to_keywords(server):
    model = Stub("Sure! You should search for the ledger.",
                 ans("It is 64 [1]."))
    r = ask("What is the default capacity of a Ledger?", model, server)
    assert r.covered and r.sources
    assert any("keywords" in n for n in r.notes)


def test_an_invented_citation_is_removed_and_said(server):
    model = Stub(q("ledger capacity"),
                 ans("It is 64 [1], see also [7].", [1, 7]))
    r = ask("Default Ledger capacity?", model, server)
    assert "[7]" not in r.answer and "[1]" in r.answer
    assert r.cited == [1]
    assert any("Removed citation(s) [7]" in n for n in r.notes)


def test_list_indexing_is_not_a_citation(server):
    model = Stub(q("apply_gain"),
                 ans("Use samples[0] with apply_gain [1]."))
    r = ask("How do I apply gain to the first sample?", model, server)
    assert "samples[0]" in r.answer
    assert r.cited == [1]


def test_an_uncited_answer_is_marked_unverified(server):
    model = Stub(q("ledger"), ans("It is 64.", []))
    r = ask("Default Ledger capacity?", model, server)
    assert r.covered and not r.grounded
    assert any("unverified" in n for n in r.notes)


def test_the_model_saying_not_covered_is_the_standard_sentence(server):
    model = Stub(q("encrypt frame"),
                 ans("The docs mention frames but not encryption.", [],
                     covered=False))
    r = ask("How do I encrypt a frame with AES?", model, server)
    assert r.answer == qa.NOT_COVERED and not r.covered and r.cited == []
    assert any("The model said" in n for n in r.notes)


def test_nothing_relevant_found_means_no_answer_call(server):
    """A model handed no documentation answers from memory — the failure
    this module exists to prevent — so it is not asked at all."""
    model = Stub()                          # any call fails the test
    r = ask("zebra quantum lasagna", model, server, derive="keywords")
    assert r.answer == qa.NOT_COVERED and not r.covered
    assert model.calls == [] and r.model_calls == 0


def test_no_server_configured_is_not_covered_and_does_not_raise():
    r = qa.ask("anything about ledgers", servers=[], model_call=Stub(),
               derive="keywords")
    assert r.answer == qa.NOT_COVERED
    assert "No documentation server is configured." in r.notes


def test_an_unreachable_server_is_an_error_not_an_exception():
    bad = ds.ServerSpec(name="gone", command="no-such-docs-server-31337")
    r = qa.ask("ledger capacity", servers=[bad], model_call=Stub(),
               derive="keywords")
    assert not r.ok
    assert r.error == "No documentation server could be reached."
    assert any("was not found" in n for n in r.notes)


def test_free_text_fields_carry_no_max_length():
    """llama.cpp's converter writes maxLength N as N nested optionals:
    measured 34 KB of grammar for a 3000-char code field."""
    props = qa.answer_schema(3, True)["properties"]
    assert "maxLength" not in props["answer"]
    assert "maxLength" not in props["code"]
    assert props["sources"]["items"]["enum"] == [1, 2, 3]


def test_a_reply_cut_off_by_the_token_limit_is_salvaged(server):
    cut = ('{"answer": "Use encode_frame with checksum=\\"xor8\\" [1]", '
           '"sources": [1], "covered": true, "code": "from glimmerquay '
           'import encode_frame\\n\\ndef frame_hello():\\n    return enc')
    model = Stub(q("encode_frame"), cut, cut, cut)
    r = ask("Write frame_hello() using xor8", model, server, write_code=True)
    assert r.answer.startswith('Use encode_frame with checksum="xor8"')
    assert r.cited == [1]
    assert r.code.startswith("from glimmerquay import encode_frame\n")
    assert any("cut off" in n for n in r.notes)
    assert r.code_ok is False, "a half-written function must not pass"


def test_a_reply_that_is_not_json_is_read_as_text(server):
    model = Stub(q("ledger"),
                 "```json\n{\"answer\": \"64 [1]\", \"sources\": [1], "
                 "\"covered\": true,}\n```")
    r = ask("Default Ledger capacity?", model, server)
    assert r.answer == "64 [1]" and r.cited == [1]


def test_a_server_someone_else_wrote_works_too():
    """Not the bundled server: JSON in the TEXT of its search result, and its
    own page tool. The roles are detected from its tool list."""
    fixture = ROOT / "tests" / "data" / "mcp_fixture_server.py"
    spec = ds.ServerSpec(name="fx", command=sys.executable,
                         args=[str(fixture)], timeout=10)
    model = Stub(q("alpha scale", package=""),
                 ans("The default scale is 2 [1]."))
    try:
        r = qa.ask("What is the default scale of alpha?", servers=[spec],
                   packages=(), model_call=model)
    finally:
        ds.release("fx")
    assert r.covered and r.cited == [1]
    assert r.sources[0].ref == "fx.alpha"
    assert "The default scale is 2." in r.sources[0].text


def test_a_no_results_message_is_not_a_page():
    from council_core.mcp_client import ToolResult
    empty = ToolResult([{"type": "text", "text": "No documentation in pk "
                         "matches 'zebra'."}], {"results": []})
    assert qa.parse_search("s", empty) == []
    plain = ToolResult([{"type": "text", "text": "No results found."}])
    assert qa.parse_search("s", plain) == []
    page = ToolResult([{"type": "text", "text": "## alpha\nalpha(x) doubles "
                        "x.\n\n## beta\nbeta() is beta."}])
    hits = qa.parse_search("s", page)
    assert [h.title for h in hits] == ["alpha", "beta"]
    assert hits[0].text.startswith("## alpha")


# ============================================================ code

GOOD_C02 = ("from glimmerquay import encode_frame\n\n"
            "def frame_hello():\n"
            "    return encode_frame(b'hello', checksum='xor8', pad_to=4)\n")


def test_good_code_passes_the_checks_first_time(server):
    model = Stub(q("encode_frame xor8"), ans("Use encode_frame [1].",
                                             code=GOOD_C02))
    r = ask("Write frame_hello() using xor8 and pad_to 4", model, server,
            write_code=True)
    assert r.code_ok is True and r.code_issues == []
    assert r.code.startswith("from glimmerquay import encode_frame")
    assert r.code_block.startswith("```python\n")
    assert r.model_calls == 2
    assert model.calls[1]["schema"]["required"][-1] == "code"


def test_a_syntax_error_is_repaired_with_its_exact_text(server):
    model = Stub(q("encode_frame"),
                 ans("x [1]", code="def frame_hello(:\n    pass\n"),
                 ans("x [1]", code=GOOD_C02))
    r = ask("Write frame_hello() using xor8", model, server, write_code=True)
    repair = model.calls[2]["messages"][-1]["content"]
    assert "line 1: SyntaxError" in repair
    assert model.calls[2]["messages"][-2]["role"] == "assistant"
    assert r.code_ok and r.model_calls == 3
    assert model.calls[2]["seed"] != model.calls[1]["seed"], \
        "a repair must not replay the same sample"


def test_an_invented_method_is_caught_with_a_suggestion(server):
    bad = ("from glimmerquay import Ledger\n\ndef make_ledger():\n"
           "    led = Ledger(3, overflow='raise')\n    led.apend(1)\n"
           "    return led\n")
    good = ("from glimmerquay import Ledger\n\ndef make_ledger():\n"
            "    return Ledger(3, overflow='raise')\n")
    model = Stub(q("Ledger overflow raise"), ans("x [1]", code=bad),
                 ans("x [1]", code=good))
    r = ask("Write make_ledger() returning a Ledger of 3 that raises when "
            "full", model, server, write_code=True)
    repair = model.calls[2]["messages"][-1]["content"]
    assert "line 5: glimmerquay.Ledger.apend is not in the documentation" \
           in repair, repair
    assert "did you mean append?" in repair
    assert r.code_ok


def test_an_undocumented_keyword_is_caught(server):
    bad = GOOD_C02.replace("checksum='xor8'", "crc='xor8'")
    model = Stub(q("encode_frame"), ans("x [1]", code=bad),
                 ans("x [1]", code=GOOD_C02))
    r = ask("Write frame_hello() using xor8", model, server, write_code=True)
    repair = model.calls[2]["messages"][-1]["content"]
    assert "encode_frame() has no parameter 'crc'" in repair
    assert "checksum" in repair, "the documented parameters are listed"
    assert r.code_ok


def test_a_missing_import_is_caught(server):
    bad = "def make_ledger():\n    return Ledger(3, overflow='raise')\n"
    model = Stub(q("Ledger"), ans("x [1]", code=bad),
                 ans("x [1]", code="from glimmerquay import Ledger\n" + bad))
    r = ask("Write make_ledger()", model, server, write_code=True)
    assert "name 'Ledger' is used but never defined or imported" in \
        model.calls[2]["messages"][-1]["content"]
    assert r.code_ok


def test_problems_left_after_the_repairs_are_shown_not_hidden(server):
    bad = GOOD_C02.replace("encode_frame(", "encode_frames(")
    model = Stub(q("encode_frame"), *[ans("x [1]", code=bad)] * 3)
    r = ask("Write frame_hello() using xor8", model, server, write_code=True)
    assert r.code_ok is False and r.code
    assert any("encode_frames" in i for i in r.code_issues)
    assert r.model_calls == 1 + 1 + qa.MAX_REPAIRS


def test_check_code_leaves_other_libraries_alone():
    pages = [qa.Source(1, "s", "glimmerquay.Ledger", "glimmerquay.Ledger",
                       "class glimmerquay.Ledger(capacity: int = 64)")]
    code = ("import os, json\nimport numpy as np\nfrom glimmerquay import "
            "Ledger\n\ndef f(p):\n    x = np.zeros(3)\n"
            "    return Ledger(int(os.cpu_count() or 1)), json.dumps({})\n")
    check = qa.check_code(code, pages, ["glimmerquay"])
    assert check.ok, check.issues


# ============================================================ engines

def test_an_engine_without_json_schema_still_works(server):
    calls = []

    def old_engine(messages, *, temperature=0.2, num_predict=600):
        calls.append(messages)
        return (q("ledger") if len(calls) == 1 else ans("64 [1]"))

    r = ask("Default Ledger capacity?", old_engine, server)
    assert r.covered and r.cited == [1]
    assert r.constrained is False


def test_call_supported_drops_only_what_is_not_taken():
    def fn(messages, *, temperature=0.1, **kwargs):
        if "seed" in kwargs:
            raise TypeError("fn() got an unexpected keyword argument 'seed'")
        return "ok"

    out, dropped = qa.call_supported(fn, [], temperature=0.0, seed=3)
    assert out == "ok" and dropped == ["seed"]

    def broken(messages, **kwargs):
        raise TypeError("unsupported operand type(s) for +")

    with pytest.raises(TypeError, match="operand"):
        qa.call_supported(broken, [], seed=1)


def test_engine_model_call_uses_the_docs_role_and_degrades(monkeypatch):
    seen = {}

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None):
        seen.update(role=role, timeout=timeout, num_predict=num_predict)
        return "hi"

    fake = types.ModuleType("council_engine")
    fake.local_chat = local_chat
    monkeypatch.setitem(sys.modules, "council_engine", fake)
    call = qa.engine_model_call()
    assert call([{"role": "user", "content": "x"}],
                json_schema={"type": "object"}, seed=1,
                should_stop=lambda: False) == "hi"
    assert seen["role"] == qa.answering_role()
    info = qa.last_call_info()
    assert set(info["dropped"]) == {"json_schema", "seed", "should_stop"}
    assert info["stats"]["constrained"] is False


def test_engine_model_call_passes_everything_to_a_newer_engine(monkeypatch):
    seen = {}

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None, json_schema=None,
                   seed=None, stop=None, should_stop=None):
        seen.update(json_schema=json_schema, seed=seed, role=role)
        return "{}"

    fake = types.ModuleType("council_engine")
    fake.local_chat = local_chat
    fake.last_call_stats = lambda role=None: {"constrained": True,
                                              "gen_tok_s": 40.0}
    monkeypatch.setitem(sys.modules, "council_engine", fake)
    qa.engine_model_call(role="docs")([], json_schema={"type": "object"},
                                      seed=5)
    assert seen == {"json_schema": {"type": "object"}, "seed": 5,
                    "role": "docs"}
    assert qa.last_call_info()["stats"]["constrained"] is True


# ============================================================ stop

def test_stop_ends_the_question_quickly(server):
    flag = threading.Event()

    def slow_model(messages, *, should_stop=None, **kw):
        flag.set()
        while not should_stop():
            time.sleep(0.01)
        raise RuntimeError("generation cancelled")

    stop = {"now": False}
    threading.Timer(0.3, lambda: stop.update(now=True)).start()
    t0 = time.monotonic()
    r = ask("Default Ledger capacity?", slow_model, server,
            should_stop=lambda: stop["now"])
    assert r.stopped and r.error == "Stopped."
    assert time.monotonic() - t0 < 2.0


# ============================================================ tools mode

def test_tools_mode_cites_only_pages_the_model_read(server):
    steps = [
        {"content": "", "tool_calls": [{"name": "search_docs",
                                        "arguments": {"query": "ledger"}}]},
        {"content": "", "tool_calls": [{"name": "get_doc", "arguments": {
            "name": "glimmerquay.Ledger"}}]},
        {"content": "The capacity is 64 [1]; see [3].", "tool_calls": []},
    ]
    seen = []

    def chat_tools(messages, tools, *, role=None, temperature=0.2,
                   num_predict=800, timeout=120, seed=None):
        seen.append([m["role"] for m in messages])
        assert [t["function"]["name"] for t in tools] == ["search_docs",
                                                          "get_doc"]
        return steps.pop(0)

    r = qa.ask("Default Ledger capacity?", servers=[server], packages=PKG,
               model_call=Stub(), mode="tools", chat_tools=chat_tools)
    assert r.covered and r.cited == [1]
    assert [s.ref for s in r.sources] == ["glimmerquay.Ledger"]
    assert "[3]" not in r.answer
    assert seen[-1][-1] == "tool"
    assert r.model_calls == 3


def test_tools_mode_without_tool_calling_uses_the_orchestrated_path(
        server, monkeypatch):
    monkeypatch.setattr(qa, "_engine_chat_tools", lambda: None)
    model = Stub(q("ledger"), ans("64 [1]"))
    r = qa.ask("Default Ledger capacity?", servers=[server], packages=PKG,
               model_call=model, mode="tools")
    assert r.covered and r.mode == "orchestrated"
    assert "no native tool calling" in r.notes[0]


# ============================================================ docs_context

def test_docs_context_keeps_the_contract(server, monkeypatch):
    monkeypatch.setattr(qa, "engine_model_call", lambda **kw: (_ for _ in (
        )).throw(AssertionError("docs_context must not call a model")))
    snippets = qa.docs_context("encode a frame with a checksum",
                               packages=PKG, servers=[server],
                               max_chars=1500)
    assert snippets and set(snippets[0]) == {"server", "source", "title",
                                             "text"}
    assert snippets[0]["source"] == "glimmerquay.encode_frame"
    assert sum(len(s["text"]) for s in snippets) <= 1500 + 400


def test_docs_context_never_raises():
    assert qa.docs_context("anything", servers=[]) == []
    bad = ds.ServerSpec(name="gone", command="no-such-docs-server-27182")
    assert qa.docs_context("anything", servers=[bad]) == []


# ============================================================ small parts

def test_focus_keeps_the_head_and_the_paragraphs_that_match():
    text = "\n\n".join(["SIGNATURE line"] + [f"filler {i} " * 20
                                              for i in range(20)]
                       + ["the pad_to parameter pads"])
    out = qa.focus(text, qa.content_terms("what does pad_to do"), 600)
    assert len(out) <= 600
    assert out.startswith("SIGNATURE line")
    assert "the pad_to parameter pads" in out


def test_keyword_query_puts_identifiers_first():
    kw = qa.keyword_query("How do I call `encode_frame` with pad_to in "
                          "glimmerquay?", PKG)
    assert kw.split()[:2] == ["encode_frame", "pad_to"]
    assert "glimmerquay" not in kw


def test_answering_role_prefers_docs_then_coder():
    cfg = model_slots.SlotConfig()
    assert qa.answering_role(cfg) == "docs"
    cfg.slots["c"] = model_slots.Slot("c", "C:/m/coder.gguf")
    cfg.roles["coder"] = "c"
    assert qa.answering_role(cfg) == "coder"
    cfg.roles["docs"] = "c"
    assert qa.answering_role(cfg) == "docs"


def test_choosing_a_docs_model_writes_the_slot_map(tmp_path):
    vault = tmp_path / "vault"
    msg = qa.assign_docs_model("ollama:llama3.1:8b", vault)
    assert msg == "Docs questions now go to llama3.1:8b (Ollama)."
    cfg = model_slots.load(vault)
    assert cfg.slots[cfg.roles["docs"]].path == "ollama:llama3.1:8b"
    assert qa.role_model("docs", vault) == "ollama:llama3.1:8b"
    qa.assign_docs_model("C:/models/phi-3.5-mini.Q5_K_M.gguf", vault)
    cfg = model_slots.load(vault)
    assert [s.path for s in cfg.slots.values()] == [
        "", "C:/models/phi-3.5-mini.Q5_K_M.gguf"], "the old slot was left"
    qa.assign_docs_model("", vault)
    cfg = model_slots.load(vault)
    assert "docs" not in cfg.roles and list(cfg.slots) == ["main"]


@pytest.mark.parametrize("model_id,label", [
    ("ollama:phi3.5", "phi3.5 (Ollama)"),
    ("C:\\models\\x\\Llama-3.2-3B.gguf", "Llama-3.2-3B.gguf"),
    ("", "the main model")])
def test_model_labels(model_id, label):
    assert qa.model_label(model_id) == label
