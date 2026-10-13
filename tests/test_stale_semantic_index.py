"""
A semantic (Chroma) index built before logs/, memory/ and the app's own root
files were protected — the Tk shell builds one into <vault>/.chromadb at every
start, and the user's real one lists files under memory/ and logs/ — must not
put an older session, role memory or an older judged question into a prompt
under "This conversation only".

Found in review of the council-map merge: council-map's vault passages
(council_core.vault_context.build) and its semantic_search tool read that
index and handed every chunk over, and the merged Qt Council gives the
passages to the Writer on the default build and to every member and the
Judge in the full council. turn_context already drops such hits for its own
index ("a hit there is never injected"); the passages did not, and a
re-index with the current build kept the stale chunks for good, because it
never removed chunks of files it no longer collects.

The index here is a REAL chromadb index built with the old skip rules
(only conversations/, conversation_logs/ and .knowledge_graph/ were left
out). No embedding model is loaded: hashed words stand in for one. Nothing
reaches a real model: tests/fake_ollama.py on loopback, refuse_egress for
anything else.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("chromadb", reason="the stale index is a chromadb one")
pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

FACTS = {
    "older_log": "TANGERINE",     # logs/council.log: an older session's lines
    "role": "WALNUT",             # memory/: the Writer's role memory
    "verdicts": "MARZIPAN",       # verdict_history.jsonl: an older question
    "project": "QUINCE",          # a dot-folder of the app's (.council_memory)
}
QUESTION = "What is the spare impeller called?"


def _vec(text: str, dim: int = 64):
    v = [0.0] * dim
    for w in re.findall(r"[a-z]{3,}", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _old_collect(vault_rag):
    """vault_rag._collect_files as e0b7a3c had it: only conversation_logs/,
    conversations/ and .knowledge_graph/ were skipped."""
    def collect(vault_dir):
        out = []
        for p in Path(vault_dir).rglob("*"):
            if any(part in vault_rag.SKIP_PATTERNS for part in p.parts):
                continue
            if not p.is_file():
                continue
            rel = p.relative_to(vault_dir).parts
            if rel[0] in ("conversation_logs", "conversations",
                          ".knowledge_graph"):
                continue
            if p.suffix.lower() in vault_rag.INDEXABLE_EXTENSIONS:
                out.append(p)
        return sorted(out)
    return collect


def _sources(rag):
    return {m["source"] for m in
            rag._backend._collection.get(include=["metadatas"])["metadatas"]}


def _app_source(source: str) -> bool:
    top = source.replace("\\", "/").split("/")[0]
    return top in ("logs", "memory", ".council_memory") or \
        source == "verdict_history.jsonl"


@pytest.fixture
def stale(tmp_path, monkeypatch):
    """A vault whose .chromadb was built under the old skip rules."""
    import council_engine as ce
    import vault_rag
    from council_core import rag_jobs, vault_context

    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "logs").mkdir()
    (vault / ".council_memory").mkdir()
    (vault / "data_in" / "notes.md").write_text(
        "The spare parts cabinet is cabinet 3.\n", encoding="utf-8")
    # logs/council.log as the Librarian writes it: every transcript line of
    # every session.
    (vault / "logs" / "council.log").write_text(
        "[2026-10-01 09:00:00] User: what is the spare impeller called\n"
        f"[2026-10-01 09:00:09] Writer: the spare impeller is called "
        f"{FACTS['older_log']}\n", encoding="utf-8")
    (vault / "verdict_history.jsonl").write_text(json.dumps(
        {"ts": "2026-10-01T09:00:10", "session_id": "older",
         "query": f"spare impeller called {FACTS['verdicts']}",
         "confidence": 8, "passed": True}) + "\n", encoding="utf-8")
    (vault / ".council_memory" / "notes.md").write_text(
        f"the spare impeller was decided to be {FACTS['project']}\n",
        encoding="utf-8")
    ce.RoleMemoryManager(vault / "memory").update(
        "writer", f"- the spare impeller is called {FACTS['role']}")

    monkeypatch.setattr(vault_rag, "_ST_OK", False)
    monkeypatch.setattr(vault_rag._ChromaBackend, "_embed",
                        lambda self, texts: [_vec(t) for t in texts])
    with monkeypatch.context() as m:
        m.setattr(vault_rag, "_collect_files", _old_collect(vault_rag))
        old = vault_rag.VaultRAG(vault_dir=vault,
                                 chroma_dir=vault / ".chromadb")
        old.index(force=True)
        built = _sources(old)
    # The old build really did index the app's files.
    assert {s for s in built if _app_source(s)}, built
    del old
    # A fresh process's view: no index object yet, the folder on disk.
    rag_jobs._shared.pop(vault.resolve(), None)
    vault_context._keyword.clear()
    yield SimpleNamespace(vault=vault, built=built, vault_rag=vault_rag)
    rag_jobs._shared.pop(vault.resolve(), None)
    vault_context._keyword.clear()


# ============================================================
# The passages and the tool read the stale index
# ============================================================

def test_the_passages_leave_out_the_apps_files_in_a_stale_index(stale):
    from council_core import vault_context
    brief = vault_context.build(QUESTION, stale.vault)
    assert brief.backend == "chromadb"
    assert not [s for s in brief.sources if _app_source(s)], brief.sources
    for name, fact in FACTS.items():
        assert fact not in brief.text, (name, brief.text)
    assert brief.sources == ["data_in\\notes.md"] or \
        brief.sources == ["data_in/notes.md"], brief.sources


def test_the_semantic_search_tool_leaves_them_out_too(stale):
    from council_qt.tabs.council import CouncilActions
    actions = CouncilActions(vault_dir=stale.vault, demo_mode=False)
    assert actions._scope() == "conversation"
    ok, text, payload = actions.tools()["semantic_search"](
        {"query": "spare impeller", "k": 5})
    assert ok
    for name, fact in FACTS.items():
        assert fact not in text, (name, text)
    assert "notes.md" in text


def test_a_reindex_removes_the_chunks_of_files_it_no_longer_collects(stale):
    """The Agents tab's Re-index Vault Now (rag_jobs) with this build: the
    old chunks of logs/, memory/, the root's app files and a dot-folder go,
    and so do their entries in file_hashes.json (else they would be skipped
    as unchanged and never looked at again)."""
    from council_core import rag_jobs
    index = rag_jobs.for_vault(stale.vault)
    out = index.reindex(force=True)
    assert out.ok, out.message
    left = _sources(index.rag)
    assert not [s for s in left if _app_source(s)], sorted(left)
    assert any(s.replace("\\", "/") == "data_in/notes.md" for s in left)
    hashes = json.loads((stale.vault / ".chromadb" / "file_hashes.json")
                        .read_text())
    assert not [s for s in hashes if _app_source(s)], sorted(hashes)


def test_a_reindex_removes_a_deleted_files_chunks(stale):
    """The same rule for a file the user deleted: its passages are not
    "your vault" any more."""
    from council_core import rag_jobs
    gone = stale.vault / "data_in" / "gone.md"
    gone.write_text("the spare impeller was once SAFFRON\n",
                    encoding="utf-8")
    index = rag_jobs.for_vault(stale.vault)
    assert index.reindex(force=True).ok
    assert any(s.replace("\\", "/") == "data_in/gone.md"
               for s in _sources(index.rag))
    gone.unlink()
    assert index.reindex(force=True).ok
    assert not any(s.replace("\\", "/") == "data_in/gone.md"
                   for s in _sources(index.rag))


# ============================================================
# The Qt Council, end to end, over the stale index
# ============================================================

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def engine(stale, monkeypatch):
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress
    with FakeOllama() as fake:
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(stale.vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_OLLAMA_NUM_CTX",
                    "COUNCIL_OLLAMA_KEEP_ALIVE", "COUNCIL_GGUF_N_CTX",
                    "COUNCIL_AGENT_MEMORY_ENABLE", "COUNCIL_REMOTE_NODES",
                    "COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO",
                    "COUNCIL_QUIRKS_APPLY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refused = refuse_egress(monkeypatch)
        yield SimpleNamespace(vault=stale.vault, fake=fake, refused=refused)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _prompts(fake):
    return ["\n".join(m.get("content") or "" for m in body.get("messages")
                      or [] if m.get("role") != "system")
            for body in fake.state.chats]


def _ask(qapp, vault, *, demo_mode=True, deep=False):
    from council_qt.tabs.council import CouncilActions, CouncilTab
    from council_qt.window import CouncilWindow
    window = CouncilWindow()
    view = CouncilTab(window, actions=CouncilActions(vault_dir=vault,
                                                     demo_mode=demo_mode),
                      demo_mode=demo_mode)
    try:
        assert view.conversation.scope == "conversation"
        if not demo_mode:
            view._checkboxes["deliberate"].setChecked(True)
            if deep:
                view.depth_box.setCurrentIndex(
                    view.depth_box.findData("deep"))
        view.input.setPlainText(QUESTION)
        view.on_send()
        assert _pump(qapp, lambda: not view._turn_active, timeout=240)
        for _ in range(20):
            qapp.processEvents()
        return view.transcript.toPlainText()
    finally:
        window.request_close()
        view.deleteLater()
        qapp.processEvents()


def _leaks(fake):
    found = {}
    for i, prompt in enumerate(_prompts(fake)):
        hit = sorted(k for k, v in FACTS.items() if v in prompt)
        if hit:
            found[i] = hit
    return found


def test_the_default_build_keeps_the_stale_index_out_of_the_prompt(
        qapp, engine):
    transcript = _ask(qapp, engine.vault)
    assert _prompts(engine.fake), "the question reached no model"
    assert not engine.refused
    assert not _leaks(engine.fake), _leaks(engine.fake)
    for line in transcript.splitlines():
        if line.startswith("Vault:") or "passage(s)" in line:
            assert "logs" not in line and "memory" not in line \
                and "verdict_history" not in line, line


def test_a_deep_council_keeps_the_stale_index_out_of_every_prompt(
        qapp, engine):
    _ask(qapp, engine.vault, demo_mode=False, deep=True)
    assert len(_prompts(engine.fake)) > 4
    assert not engine.refused
    assert not _leaks(engine.fake), _leaks(engine.fake)
