"""
The memory scope — what a personality may remember when it answers.

The user's decision (6 October 2026): "This conversation only". A follow-up
sees this session's earlier turns; older sessions, learned role/project memory
and the profile stay OUT of prompts. Before this there were only SILO (nothing)
and no-SILO (everything) — and the Qt shell, which never set SILO, put the
memory and profile the Tk shell wrote into every prompt.

These tests plant a different fact in each place a prompt can remember from and
read the REAL prompt `PersonalityModel.respond` stitches (the backend is a
recorder, nothing else is stood in).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import memory_scope  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

FACTS = {
    "turn": "the pump in bay 4 is called ORCHID",
    "older": "the older session's secret is TANGERINE",
    "role": "role memory says the user likes WALNUT",
    "project": "project memory says the plant is in KESTREL",
    "profile": "the profile says always answer in HAIKU",
}


class Recorder:
    """A backend spec: records the prompt, says something back."""

    def __init__(self):
        self.prompts = []

    def generate(self, *, developer_instructions, user_text, **_kwargs):
        self.prompts.append(user_text)
        return "noted"


class Registry:
    def __init__(self, spec):
        self.spec = spec

    def get(self, _key):
        return self.spec

    def best_for(self, **_kwargs):
        return self.spec


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A vault with a fact in every place a prompt can remember from."""
    import council_engine as ce
    for var in ("COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO",
                "COUNCIL_QUIRKS_APPLY"):
        monkeypatch.delenv(var, raising=False)
    vault = tmp_path / "vault"
    convo = ce.ConversationStore(vault / "conversations")
    convo.append("older_session", {"ts": "2026-10-01T10:00:00",
                                   "who": "User", "text": FACTS["older"]})
    convo.append("now", {"ts": "2026-10-12T10:00:00", "who": "User",
                         "text": FACTS["turn"]})
    convo.append("now", {"ts": "2026-10-12T10:00:05", "who": "Writer",
                         "text": "Understood."})
    memory = ce.RoleMemoryManager(vault / "memory")
    memory.update("writer", FACTS["role"])
    memory.update(ce._PROJECT_MEMORY_KEY, FACTS["project"])
    memory.update(ce._USER_PROFILE_KEY, FACTS["profile"])
    spec = Recorder()

    def writer(scope=None, prior=None):
        return ce.PersonalityModel(
            name="writer", system_prompt="be useful", weights={},
            registry=Registry(spec), backend_key="k", memory_manager=memory,
            conversation_store=convo, session_id="now",
            prior_session_id=prior, trace=False, memory_scope=scope)

    return writer, spec


def _found(prompt):
    return {name for name, fact in FACTS.items() if fact in prompt}


def test_this_conversation_only_keeps_the_turns_and_nothing_older(planted):
    writer, spec = planted
    writer("conversation").respond("what is the pump called?")
    assert _found(spec.prompts[-1]) == {"turn"}, spec.prompts[-1]


def test_a_loaded_prior_is_the_one_way_an_older_session_gets_in(planted):
    writer, spec = planted
    writer("conversation", prior="older_session").respond("and the secret?")
    assert _found(spec.prompts[-1]) == {"turn", "older"}, spec.prompts[-1]


def test_none_remembers_nothing_even_a_loaded_prior(planted):
    writer, spec = planted
    writer("none", prior="older_session").respond("anything?")
    assert _found(spec.prompts[-1]) == set(), spec.prompts[-1]


def test_all_is_what_the_engine_did_without_silo(planted):
    writer, spec = planted
    writer("all", prior="older_session").respond("everything?")
    assert _found(spec.prompts[-1]) == set(FACTS), spec.prompts[-1]


def test_an_unknown_scope_on_a_model_remembers_nothing(planted):
    writer, spec = planted
    writer("everything please").respond("?")
    assert _found(spec.prompts[-1]) == set()


def test_silo_still_means_none_for_a_model_that_sets_no_scope(planted,
                                                              monkeypatch):
    """The Tk shell: it sets SILO and never heard of scopes. Unchanged."""
    writer, spec = planted
    monkeypatch.setenv("COUNCIL_DEMO_SILO", "1")
    writer(None, prior="older_session").respond("?")
    assert _found(spec.prompts[-1]) == set()


def test_no_silo_and_no_scope_is_everything_as_before(planted):
    writer, spec = planted
    writer(None, prior="older_session").respond("?")
    assert _found(spec.prompts[-1]) == set(FACTS)


def test_the_environment_can_choose_conversation(planted, monkeypatch):
    writer, spec = planted
    monkeypatch.setenv("COUNCIL_MEMORY_SCOPE", "conversation")
    monkeypatch.setenv("COUNCIL_DEMO_SILO", "1")      # the explicit one wins
    writer(None).respond("?")
    assert _found(spec.prompts[-1]) == {"turn"}


def test_quirks_apply_off_still_keeps_the_profile_out_in_all(planted,
                                                            monkeypatch):
    writer, spec = planted
    monkeypatch.setenv("COUNCIL_QUIRKS_APPLY", "0")
    writer("all").respond("?")
    assert "profile" not in _found(spec.prompts[-1])


# ============================================================
# The rule itself
# ============================================================

@pytest.mark.parametrize("env, default, want", [
    ({}, "all", "all"),
    ({}, "conversation", "conversation"),
    ({"COUNCIL_DEMO_SILO": "1"}, "all", "none"),
    ({"COUNCIL_DEMO_SILO": " Yes "}, "conversation", "none"),
    ({"COUNCIL_DEMO_SILO": "0"}, "conversation", "conversation"),
    ({"COUNCIL_MEMORY_SCOPE": "ALL", "COUNCIL_DEMO_SILO": "1"}, "none", "all"),
    ({"COUNCIL_MEMORY_SCOPE": "conversation"}, "all", "conversation"),
    ({"COUNCIL_MEMORY_SCOPE": "bogus"}, "conversation", "conversation"),
    ({"COUNCIL_MEMORY_SCOPE": "bogus", "COUNCIL_DEMO_SILO": "on"}, "all",
     "none"),
])
def test_how_the_scope_is_decided(env, default, want):
    assert memory_scope.resolve(env, default=default) == want


def test_the_qt_default_is_this_conversation_only():
    assert memory_scope.for_qt({}) == "conversation"
    assert memory_scope.for_qt({"COUNCIL_DEMO_SILO": "1"}) == "none"


# ============================================================
# The retired Tk module cannot turn the Qt app's memory off
# ============================================================
#
# Importing council_gui_engine runs os.environ.setdefault('COUNCIL_DEMO_SILO',
# '1') (its e0b7a3c code, which is not edited any more). Batches 1-2 guarded
# this with memory_scope.set_tk_default; the merge removed it with the Tk
# edits, and nothing covered the case (found in review). The Qt entry point
# now pins the user's settings first.

@pytest.fixture
def unpinned(monkeypatch):
    for var in ("COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO"):
        monkeypatch.delenv(var, raising=False)
    memory_scope.unpin_launch_environment()
    yield
    memory_scope.unpin_launch_environment()


def test_the_launch_pins_the_scope_the_user_started_with(unpinned,
                                                         monkeypatch):
    memory_scope.pin_launch_environment()
    # What the Tk module's import does, after the app started:
    monkeypatch.setenv("COUNCIL_DEMO_SILO", "1")
    assert memory_scope.for_qt() == "conversation"
    # An environment handed in is still read as it is.
    assert memory_scope.for_qt({"COUNCIL_DEMO_SILO": "1"}) == "none"
    memory_scope.unpin_launch_environment()
    assert memory_scope.for_qt() == "none"


@pytest.mark.parametrize("env, want", [
    ({"COUNCIL_DEMO_SILO": "1"}, "none"),
    ({"COUNCIL_MEMORY_SCOPE": "all"}, "all"),
    ({}, "conversation"),
])
def test_what_the_user_started_with_is_kept(unpinned, monkeypatch, env,
                                            want):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    memory_scope.pin_launch_environment()
    for key in env:
        monkeypatch.delenv(key)
    assert memory_scope.for_qt() == want


def test_council_qt_pins_it_before_anything_else(unpinned):
    """council_qt.py's main() pins the scope before its first other import
    (onboarding, branding, the launch chain)."""
    import ast
    tree = ast.parse((ROOT / "council_qt.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    first = [ast.unparse(n) for n in main.body[:2]]
    assert first == ["from council_core import memory_scope",
                     "memory_scope.pin_launch_environment()"], first


def test_importing_the_tk_module_after_the_pin_keeps_qt_memory(tmp_path):
    """The real thing, in a child process (the import is not undone): pin as
    council_qt.py does, import the Tk module, and the Qt scope is still
    "This conversation only"."""
    import os
    import subprocess
    code = ("import os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from council_core import memory_scope\n"
            "memory_scope.pin_launch_environment()\n"
            "import council_gui_engine\n"
            "print(os.environ.get('COUNCIL_DEMO_SILO'), "
            "memory_scope.for_qt())\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO")}
    env["COUNCIL_VAULT_ROOT"] = str(tmp_path / "vault")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    silo, scope = out.stdout.strip().splitlines()[-1].split()
    assert silo == "1", "the Tk module no longer sets SILO: update this test"
    assert scope == "conversation"


def test_the_qt_app_imports_neither_tk_module(tmp_path):
    """Every Qt tab, default and advanced, builds without importing the Tk
    shell (council_gui_engine, council_modules) — in a child process, so no
    other test's import counts."""
    import os
    import subprocess
    code = ("import os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from PySide6.QtWidgets import QApplication\n"
            "app = QApplication([])\n"
            # No model server here: some tabs ask this PC's Ollama at its
            # usual port, and a test must never reach the real one.
            "from council_core import local_models\n"
            "def _no_server(req, timeout):\n"
            "    raise OSError('no model server in this test')\n"
            "local_models.open_direct = _no_server\n"
            "from council_qt import tabs\n"
            "from council_qt.window import CouncilWindow\n"
            "window = CouncilWindow()\n"
            "failed, built = [], 0\n"
            "for title, factory, _eager in tabs.registry(advanced=True):\n"
            "    try:\n"
            "        factory(window)\n"
            "        built += 1\n"
            "    except Exception as exc:\n"
            "        failed.append((title, repr(exc)[:100]))\n"
            "    app.processEvents()\n"
            "window.request_close()\n"
            "app.processEvents()\n"
            "from council_core import memory_scope\n"
            "print('BUILT', built, len(tabs.registry(advanced=True)))\n"
            "print('FAILED', failed)\n"
            "print('LOADED', sorted(m for m in ('council_gui_engine', "
            "'council_modules') if m in sys.modules))\n"
            "print('SCOPE', memory_scope.for_qt())\n")
    import socket
    with socket.socket() as sock:              # a loopback port nobody has:
        sock.bind(("127.0.0.1", 0))            # the Models and Nodes tabs
        dead = sock.getsockname()[1]           # ask Ollama, and must not
    env = {k: v for k, v in os.environ.items()  # reach the real one
           if k not in ("COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO")}
    env.update(COUNCIL_VAULT_ROOT=str(tmp_path / "vault"),
               COUNCIL_OLLAMA_HOST=f"http://127.0.0.1:{dead}",
               QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = dict(line.split(" ", 1) for line in out.stdout.splitlines()
                 if line.split(" ", 1)[0] in ("BUILT", "FAILED", "LOADED",
                                              "SCOPE"))
    built, total = map(int, lines["BUILT"].split())
    assert built == total, lines["FAILED"]
    assert lines["LOADED"] == "[]", out.stdout[-2000:]
    assert lines["SCOPE"] == "conversation"


def test_the_module_imports_no_toolkit_and_no_engine():
    source = (ROOT / "council_core" / "memory_scope.py").read_text(
        encoding="utf-8")
    for name in ("tkinter", "PySide6", "import council_engine"):
        assert name not in source


def test_the_engine_has_no_second_silo_rule():
    """SILO is read in ONE place now (memory_scope.silo); a second copy in
    respond() would blank the conversation turns the user asked to keep."""
    source = (ROOT / "council_engine.py").read_text(encoding="utf-8")
    assert "os.environ.get('COUNCIL_DEMO_SILO'" not in source
    assert 'os.environ.get("COUNCIL_DEMO_SILO"' not in source


def test_conversation_files_are_still_jsonl_lines(planted, tmp_path):
    """The planted store wrote what Tk writes: one JSON object per line."""
    lines = (tmp_path / "vault" / "conversations" / "now.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert [json.loads(line)["who"] for line in lines] == ["User", "Writer"]
