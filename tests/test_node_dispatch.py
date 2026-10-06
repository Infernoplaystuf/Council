"""
The old multi-node dispatcher (COUNCIL_REMOTE_NODES=1 + COUNCIL_PI_HOSTS):
which prompts may leave this PC, and what comes back.

THE LEAK (found 2026-10-05, measured with a planted string): every council
member built with a dispatcher carried one shared model label — "gguf:unset"
or "gguf:<file>" — that no node has. best_host_for SUBSTRING-matched it,
found nothing, and fell back to ANY reachable host, least busy first. When a
node won (this PC's Ollama busy or not running), the member's FULL prompt —
system prompt, role memory, user profile, project context, vault excerpts,
the task — was sent there; the node answered 404 for the unknown model and
the member quietly answered locally. The prompt had left the PC for nothing.

Two servers stand in for the machines, both tests/fake_ollama on loopback:
``here`` is this PC's Ollama (busy: one model loaded), ``pi`` is the node,
on 127.0.0.2 where the OS allows it. The engine is told the pi is a remote
machine through the one predicate the dispatcher asks (_is_remote_host);
probing, picking and sending are the real code. A SENTINEL string planted in
the task shows exactly which server saw the prompt. refuse_egress fails the
test on any connection off loopback or to the real Ollama's port 11434.
"""
from __future__ import annotations

import contextlib
import io
import socket
import sys
from types import SimpleNamespace

import pytest

from tests.fake_ollama import FakeOllama, loopback_alias, refuse_egress, tag

SENTINEL = "SENTINEL-7f3a-this-must-not-leave-the-pc"
MEMBERS = ("writer", "peasant", "coder", "sage")


def _tag(name):
    return tag(name, size=2_000_000_000, family="llama", params="3.2B",
               quant="Q4_K_M")


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    import council_engine as ce
    refused = refuse_egress(monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    for var in ("COUNCIL_GGUF_PATH", "COUNCIL_BACKEND", "COUNCIL_OLLAMA_MODEL",
                "COUNCIL_DEMO_SILO"):
        monkeypatch.delenv(var, raising=False)
    with FakeOllama(tags=[_tag("llama3.1:8b")]) as here, \
            FakeOllama(tags=[_tag("llama3.2:3b")],
                       host=loopback_alias()) as pi:
        # This PC's Ollama is busy (a model loaded, /api/ps lists it), so
        # the old sort — (active models, latency) — preferred the idle pi.
        here.state.loaded.add("llama3.1:8b")
        pi.state.reply = "Answered on the pi."
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", here.url)
        monkeypatch.setattr(ce, "DEFAULT_OLLAMA_HOST", here.url)
        monkeypatch.setattr(ce, "DEFAULT_PI_HOSTS", [pi.url])  # COUNCIL_PI_HOSTS
        monkeypatch.setenv("COUNCIL_REMOTE_NODES", "1")
        real_is_remote = ce._is_remote_host
        monkeypatch.setattr(
            ce, "_is_remote_host",
            lambda h: (h or "").rstrip("/") == pi.url or real_is_remote(h))
        local = []

        def answer_locally(messages, **_kw):
            local.append(messages)
            return "LOCAL ANSWER"

        # The member's local path (its role's slot) — what a member falls
        # back to. Stubbed so no model loads and nothing reaches Ollama.
        monkeypatch.setattr(ce, "_route_chat", answer_locally)
        yield SimpleNamespace(ce=ce, vault=vault, here=here, pi=pi,
                              local=local, refused=refused)
    assert refused == [], f"a connection left loopback: {refused}"


def _council(env, monkeypatch, label):
    """The council the Tk console builds with a dispatcher, every role's
    model being ``label`` (DEFAULT_MODELS fills every slot with one label)."""
    ce = env.ce
    monkeypatch.setattr(ce, "DEFAULT_MODELS",
                        {slot: label for slot in ce._ROLE_SLOTS})
    return ce.build_personalities(pins={}, vault_dir=env.vault,
                                  session_id="t", trace=False,
                                  dispatcher=ce.build_dispatcher())


def _chats(server):
    return [r for r in server.state.requests if r[:2] == ("POST", "/api/chat")]


def _saw_sentinel(server):
    return any(SENTINEL in (m.get("content") or "")
               for body in server.state.chats
               for m in body.get("messages") or [])


# ============================================================
# The leak: a model no node has
# ============================================================

@pytest.mark.parametrize("label", ["gguf:unset",
                                   "gguf:granite-3.1-8b-instruct-Q4_K_M.gguf",
                                   "ollama:auto"])
def test_a_member_on_the_shared_label_sends_nothing_to_a_node(
        nodes, monkeypatch, label):
    members = _council(nodes, monkeypatch, label)
    for role in MEMBERS:
        out = members[role].respond(f"Summarise the notes. {SENTINEL}")
        assert out == "LOCAL ANSWER", role
    assert _chats(nodes.pi) == [], "the prompt was sent to the node"
    assert not _saw_sentinel(nodes.pi)
    assert len(nodes.local) == len(MEMBERS)
    assert all(SENTINEL in m[1]["content"] for m in nodes.local)


@pytest.mark.parametrize("label,on_the_pi", [
    ("ollama:llama3", "llama3.2:3b"),
    ("ollama:phi3", "phi3.5:latest"),
    ("ollama:llama3.1", "llama3.1:8b-instruct-q4_K_M"),
    # A GGUF label is never a node's model, even if a node happens to list
    # a model spelled the same: "gguf" with the tag "unset" is an Ollama
    # name, but this label means the file loaded in THIS process.
    ("gguf:unset", "gguf:unset"),
])
def test_a_name_that_matches_only_in_part_sends_nothing(
        nodes, monkeypatch, label, on_the_pi):
    nodes.pi.state.tags = [_tag(on_the_pi)]
    members = _council(nodes, monkeypatch, label)
    assert members["writer"].respond(f"Plan it. {SENTINEL}") == "LOCAL ANSWER"
    assert _chats(nodes.pi) == []
    assert not _saw_sentinel(nodes.pi)


def test_with_remote_nodes_off_nothing_is_probed_or_sent(nodes, monkeypatch):
    """The opt-in: without COUNCIL_REMOTE_NODES no node is even asked for
    its model list — even when it has the member's exact model."""
    monkeypatch.delenv("COUNCIL_REMOTE_NODES")
    members = _council(nodes, monkeypatch, "ollama:llama3.2:3b")
    assert members["writer"].respond(f"Plan it. {SENTINEL}") == "LOCAL ANSWER"
    assert nodes.pi.state.requests == []
    assert nodes.here.state.requests == []


# ============================================================
# The opt-in path still works: the node really has the model
# ============================================================

@pytest.mark.parametrize("label,on_the_pi", [
    ("ollama:llama3.2:3b", "llama3.2:3b"),
    ("ollama:phi3.5", "phi3.5:latest"),      # Ollama's implicit :latest
])
def test_a_member_whose_exact_model_is_on_the_node_is_served_there(
        nodes, monkeypatch, label, on_the_pi):
    nodes.pi.state.tags = [_tag(on_the_pi)]
    members = _council(nodes, monkeypatch, label)
    out = members["writer"].respond(f"Plan it. {SENTINEL}")
    assert out == "Answered on the pi."
    sent = nodes.pi.state.chats
    assert len(sent) == 1
    # The node's own name for it — never the council's "ollama:" label,
    # which the node answered with 404 after reading the prompt.
    assert sent[0]["model"] == on_the_pi
    assert _saw_sentinel(nodes.pi)            # opted in, and it can serve it
    assert nodes.local == []
    assert _chats(nodes.here) == []


# ============================================================
# best_host_for on its own
# ============================================================

def _dead_url():
    """A loopback port nothing listens on (refused at once)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_best_host_for_matches_the_whole_name(nodes):
    ce, here, pi = nodes.ce, nodes.here, nodes.pi
    pi.state.tags = [_tag("llama3.2:3b"), _tag("phi3.5:latest")]
    disp = ce.LoadAwareDispatcher([here.url, pi.url])
    assert disp.best_host_for("llama3") is None        # not "llama3.2:3b"
    assert disp.best_host_for("phi3") is None          # not "phi3.5:latest"
    assert disp.best_host_for("gpt-oss:20b") is None   # nobody has it
    assert disp.best_host_for("gguf:unset") is None
    assert disp.best_host_for("") is None
    assert disp.best_host_for("llama3.2:3b") == pi.url
    assert disp.best_host_for("phi3.5") == pi.url
    # Only this PC has it: this PC, busy or not — never the idle pi.
    assert disp.best_host_for("llama3.1:8b") == here.url
    assert _chats(pi) == [] and _chats(here) == []      # probing only


def test_with_no_reachable_host_it_says_run_locally(nodes):
    """It used to answer "http://localhost:11434" — a guess at a server
    that, by then, was known not to answer."""
    disp = nodes.ce.LoadAwareDispatcher([_dead_url()])
    assert disp.best_host_for("llama3.2:3b") is None


def test_two_nodes_with_the_model_the_less_busy_wins(nodes):
    ce, here, pi = nodes.ce, nodes.here, nodes.pi
    here.state.tags = [_tag("llama3.2:3b")]
    here.state.loaded = {"llama3.2:3b"}                 # busy
    disp = ce.LoadAwareDispatcher([here.url, pi.url])
    assert disp.best_host_for("llama3.2:3b") == pi.url


# ============================================================
# A node that dies mid-answer
# ============================================================

def _msgs():
    return [{"role": "system", "content": "be brief"},
            {"role": "user", "content": f"hello {SENTINEL}"}]


@pytest.mark.parametrize("stream", [False, True])
def test_a_node_that_closes_mid_answer_is_an_error_not_an_answer(
        nodes, stream):
    """Two chunks ("Answ", "ered") then a clean close, no final packet. The
    old client returned "Answered" as the whole reply."""
    ce, pi = nodes.ce, nodes.pi
    pi.state.drop_after = 2
    got = []
    with pytest.raises(RuntimeError, match="final packet"):
        if stream:
            ce._ollama_chat_stream(pi.url, "llama3.2:3b", _msgs(),
                                   temperature=0.2, num_predict=50,
                                   allow_remote=True,
                                   token_callback=got.append)
        else:
            ce._ollama_chat(pi.url, "llama3.2:3b", _msgs(), temperature=0.2,
                            num_predict=50, allow_remote=True)
    assert pi.state.dropped == 1
    assert len(pi.state.chats) == 1                    # sent once, not retried


def test_a_member_whose_node_dies_mid_answer_answers_locally(
        nodes, monkeypatch):
    nodes.pi.state.drop_after = 2
    members = _council(nodes, monkeypatch, "ollama:llama3.2:3b")
    out = members["writer"].respond(f"Plan it. {SENTINEL}")
    assert out == "LOCAL ANSWER"                       # not "Answered"
    assert nodes.pi.state.dropped == 1
    assert len(nodes.local) == 1


# ============================================================
# Printing with stdout redirected to a cp1252 pipe
# ============================================================

@contextlib.contextmanager
def cp1252_stdout():
    """sys.stdout as Python makes it on Windows when the output goes to a
    file or a pipe (a launcher's log, `> out.txt`): the ANSI code page with
    strict errors. A console launch prints UTF-8 and hides the problem.

    Swapped inside the test body, not in a fixture: pytest puts its own
    capture back on sys.stdout when the call phase starts."""
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", errors="strict",
                           write_through=True)
    old, sys.stdout = sys.stdout, out
    try:
        yield buf
    finally:
        sys.stdout = old
        out.detach()                  # keep buf open for the assertions


def test_the_dispatchers_lines_survive_a_cp1252_stdout(nodes):
    disp = nodes.ce.LoadAwareDispatcher([nodes.here.url, nodes.pi.url])
    with cp1252_stdout() as buf:
        assert disp.best_host_for("llama3.2:3b") == nodes.pi.url  # was "→"
        assert disp.best_host_for("nothing:here") is None
    text = buf.getvalue().decode("ascii")
    assert "[DISPATCHER] model=llama3.2:3b -> " + nodes.pi.url in text
    assert "no node has it" in text


def test_a_node_error_in_another_language_does_not_break_the_member(
        nodes, monkeypatch):
    """The fallback line prints the node's error; an arrow or any other
    character outside cp1252 in it raised UnicodeEncodeError out of the
    member instead of falling back."""
    nodes.pi.state.chat_error = (500, "modèle introuvable → réessayez")
    members = _council(nodes, monkeypatch, "ollama:llama3.2:3b")
    members["writer"].trace = True
    with cp1252_stdout() as buf:
        out = members["writer"].respond(f"Plan it. {SENTINEL}")
    assert out == "LOCAL ANSWER"
    text = buf.getvalue().decode("ascii")
    assert "[REMOTE] node " + nodes.pi.url + " failed" in text
    assert "falling back to local model" in text


# ============================================================
# _is_remote_host: the whole host decides
# ============================================================

@pytest.mark.parametrize("host,remote", [
    ("http://localhost:11434", False),
    ("http://127.0.0.1:11434", False),
    ("http://127.0.0.2:11434", False),
    ("http://[::1]:11434", False),
    ("http://0.0.0.0:11434", False),
    ("", False),
    ("http://192.168.1.50:11434", True),
    ("http://pi-kitchen.lan:11434", True),
    ("http://localhost.evil.example:11434", True),
    ("http://127.0.0.1.evil.example:11434", True),
])
def test_is_remote_host_reads_the_whole_host(host, remote):
    import council_engine as ce
    assert ce._is_remote_host(host) is remote
