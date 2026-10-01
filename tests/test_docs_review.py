"""
Review of llm/docs: each test pins a defect found by trying to break the
docs feature with stub models, fixture servers and hand-edited files.

No model runs here. Retrieval uses the bundled server over the benchmark's
invented package (a real subprocess); HTTP uses the loopback fixture from
test_mcp_client.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import docs_bench, docs_qa as qa, docs_servers as ds  # noqa
from council_core import mcp_client as mc  # noqa: E402
from council_core.mcp_client import ToolResult  # noqa: E402

PKG = ["glimmerquay"]
FIXTURE = ROOT / "tests" / "data" / "mcp_fixture_server.py"


@pytest.fixture(scope="module")
def server():
    spec = docs_bench.bench_server()
    yield spec
    ds.release(spec.name)


# ============================================================ citations

def test_adjacent_citations_are_each_read_and_an_invented_one_removed():
    """Models write "[1][2]" all the time. The second [n] was not read as a
    citation (it follows a ']'), so an invented "[1][7]" kept its [7] in
    front of the user, and a real "[1][2]" lost page 2 from the cited set."""
    notes: list = []
    text, cited = qa.apply_citation_rules(
        {"answer": "It is 64 [1][7].", "sources": []}, 1, notes)
    assert "[7]" not in text and cited == [1]
    assert notes and "[7]" in notes[0]
    text, cited = qa.apply_citation_rules(
        {"answer": "See [1][2].", "sources": []}, 2, [])
    assert cited == [1, 2] and text == "See [1][2]."
    # Indexing is still not a citation, adjacent or not.
    text, cited = qa.apply_citation_rules(
        {"answer": "Use samples[0][1] as shown [1].", "sources": []}, 1, [])
    assert "samples[0][1]" in text and cited == [1]
    assert qa.citations("x[3][4] and [1][2]") == [1, 2]


def test_adjacent_citations_are_both_links_in_the_tab():
    from council_qt.tabs.docs import render_answer
    a = qa.DocsAnswer(ok=True, covered=True, answer="Yes [1][2]; not x[1][2].",
                      sources=[qa.Source(1, "s", "a", "a", "t"),
                               qa.Source(2, "s", "b", "b", "t")], cited=[1, 2])
    html = render_answer(a)
    assert html.count("href='src:1'") == 2 and html.count("href='src:2'") == 2
    assert "x[1][2]" in html


# ============================================================ parsing

def test_a_long_search_text_with_stray_braces_is_parsed_quickly():
    """_json_any rescanned the rest of the text from every '{': a 39 KB page
    of prose with braces and one unbalanced quote took 14 s per search call,
    and a search runs up to 9 times per server per question."""
    page = "text { a \" b " * 3000
    t0 = time.perf_counter()
    hits = qa.parse_search("srv", ToolResult(content=[{"type": "text",
                                                       "text": page}]))
    assert time.perf_counter() - t0 < 1.0
    assert hits and hits[0].text
    t0 = time.perf_counter()
    assert qa._json_any('"' + "{" * 8000) is None
    assert time.perf_counter() - t0 < 1.0
    assert qa._json_any("[" * 100000) is None, "deep nesting must not raise"


def test_a_markdown_search_result_is_not_mined_for_example_json():
    """A text search result is documentation, not data: an example JSON
    object inside it must not replace the pages it lists."""
    md = ("## Ledger\n\nA ledger keeps entries.\n\n```json\n"
          '{"results": [{"name": "example"}]}\n```\n\n## Codec\n\nEncodes.')
    hits = qa.parse_search("srv", ToolResult(content=[{"type": "text",
                                                       "text": md}]))
    assert "example" not in [h.ref for h in hits]
    assert any(h.title == "Ledger" for h in hits)
    # A reply that IS JSON is still read as data.
    js = json.dumps({"results": [{"name": "pkg.f", "summary": "s"}]})
    assert [h.ref for h in qa.parse_search(
        "srv", ToolResult(content=[{"type": "text", "text": js}]))] == ["pkg.f"]


# ============================================================ the registry

@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("no", False), ("0", False), ("", False), (0, False),
    (None, False), ("true", True), ("yes", True), (True, True), (1, True)])
def test_hand_edited_booleans_mean_what_they_say(raw, expected):
    """bool("false") is True: a hand-edited "allow_remote": "false" turned
    remote access ON — the offline guarantee, undone by a quoting slip."""
    spec = ds.ServerSpec.from_json({"name": "x", "transport": "http",
                                    "url": "http://example.com/mcp",
                                    "allow_remote": raw, "enabled": raw})
    assert spec.allow_remote is expected and spec.enabled is expected
    if not expected:
        assert "not this computer" in ds.validate(spec)


def test_a_damaged_server_file_is_never_overwritten(tmp_path):
    """load() falls back to the default for a damaged file, and add() then
    SAVED that default over it: the user's own servers were gone."""
    cfg = tmp_path / "docs_servers.json"
    cfg.write_text(json.dumps({"format": 1, "servers": [
        {"name": "mine", "command": "python", "args": ["a.py"]},
        {"name": "broken", "command": "x", "timeout": "abc"}]}),
        encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    assert ds.problem(cfg)
    for change in (lambda: ds.add(ds.ServerSpec("new", command="py"), cfg),
                   lambda: ds.remove("mine", cfg),
                   lambda: ds.update(ds.ServerSpec("mine", command="py"), cfg)):
        with pytest.raises(ValueError) as err:
            change()
        assert "could not be read" in str(err.value)
        assert cfg.read_text(encoding="utf-8") == before


# ============================================================ docs_context

@pytest.mark.parametrize("limit", [300, 900, 2000])
def test_docs_context_stays_within_max_chars(server, limit):
    """Each page was given at least 400 chars, so three pages came back as
    778 chars for max_chars=300 — over the caller's prompt budget."""
    out = qa.docs_context("ledger capacity append entries", packages=PKG,
                          servers=[server], max_chars=limit)
    assert out
    assert sum(len(o["text"]) for o in out) <= limit


# ============================================================ tool mode

@pytest.mark.parametrize("bad", [
    {"content": "", "tool_calls": [{"name": "search_docs",
                                    "arguments": ["ledger"]}]},
    {"content": "", "tool_calls": ["search_docs"]},
    {"content": "", "tool_calls": [{"name": "get_doc", "arguments": None}]},
    "The capacity is 64.",
    None,
])
def test_tool_mode_survives_malformed_tool_calls(server, bad):
    """A small model's tool calls are not always well-formed; each of these
    ended the question with "AttributeError: ... has no attribute 'get'"."""
    turns = []

    def chat(messages, tools, **kw):
        turns.append(1)
        return bad if len(turns) == 1 else {"content": "Done [1].",
                                            "tool_calls": []}

    r = qa.ask("What is the default capacity of a Ledger?", servers=[server],
               packages=PKG, mode="tools", chat_tools=chat,
               model_call=lambda *a, **k: "")
    assert r.error == "" and r.ok
    assert "AttributeError" not in " ".join(r.notes)


# ============================================================ code checks

def _pages(server, words):
    return qa.retrieve(words, [words], servers=[server], packages=PKG).pages


def test_builtin_methods_on_a_returned_value_are_not_flagged(server):
    """`rows = led.entries(); rows.copy()` was flagged as
    "glimmerquay.Ledger.entries.copy is not in the documentation", and a
    flag costs two repair calls that rewrite correct code."""
    pages = _pages(server, "Ledger entries total")
    code = ("import glimmerquay as gq\n"
            "led = gq.Ledger()\n"
            "rows = led.entries()\n"
            "snapshot = rows.copy()\n"
            "data = gq.encode_frame(b'x')\n"
            "print(data.hex(), snapshot)\n")
    check = qa.check_code(code, pages + _pages(server, "encode_frame"), PKG)
    assert check.ok, check.issues


def test_invented_methods_are_still_caught(server):
    pages = _pages(server, "Ledger entries total") + _pages(server,
                                                            "encode_frame")
    code = ("import glimmerquay as gq\n"
            "led = gq.Ledger()\n"
            "led.copy()\n"                       # a class instance: strict
            "data = gq.encode_frame(b'x')\n"
            "data.frobnicate()\n")              # not any builtin's method
    issues = " | ".join(qa.check_code(code, pages, PKG).issues)
    assert "Ledger.copy" in issues
    assert "frobnicate" in issues


# ============================================================ the sandbox

def _run(code):
    return docs_bench.run_code_test(code, {"hidden_test": "pass"}, timeout=30)


def test_the_benchmark_sandbox_blocks_low_level_writes_and_processes(
        tmp_path):
    """Model code runs on the user's machine (the Docs tab's check includes a
    code task). os.open, os.truncate, os.mkdir and _winapi.CreateProcess all
    went straight past the audit hook that was meant to stop them."""
    victim = tmp_path / "victim.txt"
    attacks = {
        "truncate via os.open": (
            f"import os\nos.close(os.open({str(victim)!r}, "
            "os.O_WRONLY | os.O_TRUNC))\n"),
        "os.truncate": f"import os\nos.truncate({str(victim)!r}, 0)\n",
        "create via os.open": (
            f"import os\nos.close(os.open({str(tmp_path / 'made.txt')!r}, "
            "os.O_WRONLY | os.O_CREAT))\n"),
        "mkdir outside": f"import os\nos.mkdir({str(tmp_path / 'made')!r})\n",
    }
    if sys.platform == "win32":
        # cmd prints PWNED for "PW^NED"; the source line in a traceback
        # keeps the caret, so PWNED in the output means cmd really ran.
        attacks["CreateProcess"] = (
            "import _winapi\n_winapi.CreateProcess(None, 'cmd /c echo PW^NED',"
            " None, None, False, 0, None, None, None)\n")
    for label, code in attacks.items():
        victim.write_text("precious", encoding="utf-8")
        result = _run(code)
        assert not result["passed"], label
        assert "blocked" in result["output"], (label, result["output"])
        assert victim.read_text(encoding="utf-8") == "precious", label
        assert not (tmp_path / "made.txt").exists(), label
        assert not (tmp_path / "made").exists(), label
        assert "PWNED" not in result["output"], label


def test_the_sandbox_still_lets_a_solution_use_temp_files():
    code = ("import os, tempfile\n"
            "with tempfile.TemporaryDirectory() as d:\n"
            "    p = os.path.join(d, 'x.bin')\n"
            "    open(p, 'wb').write(b'1')\n"
            "fd, q = tempfile.mkstemp()\n"
            "os.close(fd)\n"
            "open('local.txt', 'w').write('ok')\n")
    result = _run(code)
    assert result["passed"], result["output"]


# ============================================================ HTTP sessions

def test_an_expired_http_session_reconnects_on_the_next_question():
    """After a 404 the client kept saying it was connected, so the pool
    reused it and EVERY later question failed until the app restarted."""
    from tests.test_mcp_client import HttpFixture
    fx = HttpFixture("json")
    spec = ds.ServerSpec(name="http-review", transport="http", url=fx.url,
                         timeout=5)
    try:
        client, _roles = ds.pooled(spec)
        fx.expire = True
        with pytest.raises(mc.McpConnectionError):
            client.list_tools()
        assert not client.connected
        fx.expire = False                 # the server restarted
        fx.session_id = "sess-456"
        again, roles = ds.pooled(spec)
        assert again is not client and again.connected
        assert roles.search_tool == "search_docs"
        assert again.call_tool(roles.search_tool, {"query": "x"}) is not None
    finally:
        ds.release(spec.name)
        fx.close()


# ============================================================ Windows

@pytest.mark.skipif(sys.platform != "win32", reason="PATHEXT is Windows")
def test_a_cmd_on_path_starts_without_its_extension(tmp_path, monkeypatch):
    """npx is npx.cmd on Windows, and most published MCP servers start with
    `npx ...`. CreateProcess does not use PATHEXT, so "npx" was reported as
    not found although it is on PATH."""
    bat = tmp_path / "fakedocs.cmd"
    bat.write_text(f'@echo off\r\n"{sys.executable}" "{FIXTURE}" %*\r\n',
                   encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep
                       + os.environ.get("PATH", ""))
    with mc.McpClient.stdio("fakedocs", [], timeout=10).connect() as c:
        assert c.server_info["name"] == "fixture"


# ============================================================ origin

@pytest.mark.parametrize("origin,model,recommend", [
    ("US", "ollama:llama3.1:8b", True),
    ("non-US", "ollama:qwen2.5:7b", False),
    ("non_us", "ollama:qwen2.5:7b", False),
    ("unknown", "C:/m/qwen2.5-coder-7b-instruct-q4_k_m.gguf", False),
    ("", "C:/m/qwen2.5-coder-7b-instruct-q4_k_m.gguf", False),
    ("", "C:/m/mystery-model.gguf", False),
    ("", "C:/m/Phi-3.5-mini-instruct-Q4_K_M.gguf", True),
])
def test_only_a_us_model_is_ever_recommended(origin, model, recommend):
    """Recommendable was "not non-US": an unknown origin, an empty one (a
    GGUF path spelled differently in two places) or model_finder's 'non_us'
    spelling all counted as recommendable — and on this branch every GGUF in
    the picker is 'unknown', qwen included."""
    report = docs_bench.BenchReport(
        model=qa.model_label(model),
        origin=qa.model_origin(model, origin),
        items=[docs_bench.ItemResult("q01", "question", True)])
    assert report.good
    assert report.recommendable is recommend
    if not recommend:
        assert "good for docs questions" not in report.lines()[0]


def test_the_gguf_picker_fallback_classifies_origin(monkeypatch, tmp_path):
    from council_core import model_slots
    files = [tmp_path / "qwen2.5-coder-7b-q4.gguf",
             tmp_path / "Meta-Llama-3.1-8B-Q4.gguf"]
    for f in files:
        f.write_bytes(b"GGUF")
    monkeypatch.setitem(sys.modules, "council_engine", None)
    monkeypatch.setattr(model_slots, "known_files", lambda *a, **k: files)
    origins = {m["name"]: m["origin"] for m in qa.list_models()}
    assert origins == {"qwen2.5-coder-7b-q4": "non-US",
                       "Meta-Llama-3.1-8B-Q4": "US"}


# ============================================================ measurement

def test_the_benchmark_records_which_model_really_answered(server,
                                                           monkeypatch):
    """`--model ollama:X` is passed as local_chat(model=...), which the
    engine on 1fadf49 accepts and IGNORES: the run measured the main model
    and filed the score under X. The report now says who served."""
    import types
    fake = types.ModuleType("council_engine")

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None):
        return docs_bench.oracle_model_call()(messages)

    fake.local_chat = local_chat
    fake.last_call_stats = lambda role=None: {"backend": "gguf",
                                              "model": "main-model.gguf"}
    monkeypatch.setitem(sys.modules, "council_engine", fake)
    call = qa.engine_model_call(model="ollama:llama3.1:8b")
    report = docs_bench.run(call, items=["q01"], server=server,
                            model_label="ollama:llama3.1:8b")
    summary = report.summary()
    assert summary["served_by"] == ["main-model.gguf"]
    assert any("main-model.gguf" in line for line in report.lines())


# ============================================================ the tab

def test_check_this_model_checks_the_model_shown_or_says_why_not(tmp_path):
    """The button sits under the picker, but the check ran the SAVED docs
    model: pick llama, press Check, and the main model was measured instead
    (correctly labelled, but not the model the user was trying to judge)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.docs import DocsActions, DocsTab
    app = QApplication.instance() or QApplication([])
    cfg = tmp_path / "docs_servers.json"
    ds.save([docs_bench.bench_server()], cfg)
    models = [{"id": "ollama:llama3.1:8b", "name": "llama3.1:8b",
               "backend": "ollama", "origin": "US"}]
    actions = DocsActions(config_path=cfg, vault_dir=tmp_path / "vault",
                          checks_path=tmp_path / "checks.json",
                          model_call=docs_bench.oracle_model_call(),
                          models=lambda: list(models))
    tab = DocsTab(None, actions)
    tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        tab._show_models(models)
        tab.model_combo.setCurrentIndex(1)        # picked, not yet used
        tab.on_check()
        assert not tab._busy, "no check may start on the wrong model"
        assert "Use for docs" in tab.status.text()
    finally:
        tab._stop.set()
        deadline = time.time() + 15
        while tab._busy and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        tab.deleteLater()
        app.processEvents()
