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


# ============================================================
# The Tk module's SILO default is the Tk shell's, not the user's
# ============================================================

_IMPORT_ORDER = """
import os, sys, tempfile
sys.path.insert(0, {root!r})
import council_gui_engine                     # sets the Tk shell's default
from council_core import memory_scope
from council_core.conversations import Conversation
conv = Conversation(tempfile.mkdtemp(), session_id="probe")
print(conv.scope, memory_scope.resolve(), os.environ.get("COUNCIL_DEMO_SILO"))
"""


def _run_after_tk_import(extra_env):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO",
                        getattr(memory_scope, "TK_DEFAULT_MARK",
                                "COUNCIL_DEMO_SILO_SOURCE"))}
    env.update(extra_env)
    done = subprocess.run([sys.executable, "-c",
                           _IMPORT_ORDER.format(root=str(ROOT))],
                          env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1].split()


def test_importing_the_tk_module_first_does_not_turn_qt_memory_off():
    """Found in review: the Tk module sets COUNCIL_DEMO_SILO=1 by setdefault
    when it is imported, and the Qt conversation read the environment when
    it was built — so any import of the Tk module before the Council tab
    silently switched Qt to "Memory: off". (It showed up in this suite: a
    test file that imports the Tk module made a later Qt test read "off".)
    The Tk shell's own default stays exactly as it was."""
    qt_scope, engine_scope, silo = _run_after_tk_import({})
    assert qt_scope == "conversation"
    assert engine_scope == "none", "the Tk shell's default changed"
    assert silo == "1"


def test_a_silo_the_user_set_still_turns_qt_memory_off():
    qt_scope, engine_scope, _silo = _run_after_tk_import(
        {"COUNCIL_DEMO_SILO": "1"})
    assert (qt_scope, engine_scope) == ("none", "none")


def test_the_tk_default_is_marked_only_when_tk_sets_it():
    env = {}
    memory_scope.set_tk_default(env)
    assert env == {"COUNCIL_DEMO_SILO": "1",
                   memory_scope.TK_DEFAULT_MARK: "tk-default"}
    assert memory_scope.for_qt(env) == "conversation"
    assert memory_scope.resolve(env) == "none"
    users = {"COUNCIL_DEMO_SILO": "1"}
    memory_scope.set_tk_default(users)
    assert users == {"COUNCIL_DEMO_SILO": "1"}
    assert memory_scope.for_qt(users) == "none"
