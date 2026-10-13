"""
Role → machine routing (council_core.node_routing), through the real engine.

Two fake Ollama servers stand in for the machines (tests/fake_ollama, both on
loopback): ``eng.fake`` is this PC, ``pi`` is the node (127.0.0.2 where the
OS allows). refuse_egress fails the test on any connection off loopback or to
the real Ollama's port.

Pinned: routing is off unless the user turns it on, registers and enables a
machine and binds a role; the engine's localhost guard lets a non-local host
through only when it is such a machine; a node that fails BEFORE answering
falls back (or fails, as chosen) and rests; output already produced is never
re-sent; each machine runs at most `parallel` calls at once.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from council_core import node_routing as nr
from tests.fake_ollama import FakeOllama, loopback_alias, refuse_egress, tag
from tests.test_llm_engine import MSGS, _slots, eng, fake  # noqa: F401


def _tag(name):
    return tag(name, size=2_000_000_000, family="llama", params="3.2B",
               quant="Q4_K_M")


@pytest.fixture
def pi(eng, monkeypatch):
    refused = refuse_egress(monkeypatch)
    nr.invalidate()
    nr.reset_cooldowns()
    with FakeOllama(tags=[_tag("llama3.2:3b")], host=loopback_alias()) as srv:
        srv.state.reply = "Answered on the pi."
        eng.fake.state.reply = "Answered here."
        yield srv
    nr.invalidate()
    nr.reset_cooldowns()
    assert refused == [], f"a connection left loopback: {refused}"


def _routing(eng, pi, *, enabled=True, routing_enabled=True,
             fallback="here", parallel=1, roles=("peasant",)):
    r = nr.Routing(routing_enabled,
                   {"pi": nr.Node("pi", pi.url, enabled, parallel)},
                   {role: nr.Binding("pi", fallback) for role in roles})
    nr.save(eng.vault, r)
    return r


def _peasant_on_3b(eng):
    _slots(eng, {"fast": {"path": "ollama:llama3.2:3b"}},
           {"peasant": "fast"})


# ---- the file ---------------------------------------------------------------

def test_off_by_default_and_a_damaged_file_keeps_everything_here(tmp_path):
    assert nr.load(tmp_path).routing_enabled is False
    nr.path_for(tmp_path).write_text("{not json", encoding="utf-8")
    assert nr.load(tmp_path).routing_enabled is False
    assert "stays on this PC" in nr.problem(tmp_path)


def test_parse_rejects_nonsense():
    good = {"nodes": {"pi": {"url": "http://192.168.1.50:11434"}}}
    nr.parse(good)
    for bad in ({"nodes": {"pi": {"url": "ftp://x"}}},
                {"nodes": {"pi": {"url": "http://x", "parallel": 99}}},
                {"nodes": {"../etc": {"url": "http://x"}}},
                {**good, "roles": {"peasant": {"node": "mars"}}},
                {**good, "roles": {"peasant": {"node": "pi",
                                               "fallback": "maybe"}}}):
        with pytest.raises(nr.RoutingError):
            nr.parse(bad)


def test_save_is_atomic_and_round_trips(tmp_path):
    r = nr.Routing(True, {"pi": nr.Node("pi", "http://10.0.0.5:11434", True,
                                        2)},
                   {"intern": nr.Binding("pi", "fail")})
    path = nr.save(tmp_path, r)
    assert not path.with_suffix(".json.tmp").exists()
    back = nr.load(tmp_path)
    assert back.to_json() == r.to_json()


def test_route_needs_all_three_opt_ins():
    node = nr.Node("pi", "http://10.0.0.5:11434", True)
    bound = {"peasant": nr.Binding("pi")}
    assert nr.route("peasant", nr.Routing(True, {"pi": node}, bound)).node \
        == "pi"
    assert nr.route("peasant", nr.Routing(False, {"pi": node}, bound)) is None
    off = nr.Node("pi", "http://10.0.0.5:11434", False)
    assert nr.route("peasant", nr.Routing(True, {"pi": off}, bound)) is None
    assert nr.route("writer", nr.Routing(True, {"pi": node}, bound)) is None


# ---- the guard ----------------------------------------------------------------

def test_the_guard_lets_through_only_a_registered_enabled_node(eng,
                                                               monkeypatch):
    refuse_egress(monkeypatch)
    lan = "http://192.168.1.50:11434"
    nr.set_current(nr.Routing(True, {"pi": nr.Node("pi", lan, True)}, {}))
    try:
        assert eng.ce._routing_allows(lan)
        assert eng.ce._routing_allows(lan + "/")
        assert not eng.ce._routing_allows("http://192.168.1.51:11434")
        with pytest.raises(RuntimeError, match="Refusing non-local"):
            eng.ce.local_chat(MSGS, host="http://192.168.1.51:11434",
                              model="ollama:llama3.2:3b")
        nr.set_current(nr.Routing(False, {"pi": nr.Node("pi", lan, True)},
                                  {}))
        assert not eng.ce._routing_allows(lan)
    finally:
        nr.invalidate()


# ---- calls through the engine --------------------------------------------------

def test_a_bound_role_answers_on_its_machine(eng, pi):
    _peasant_on_3b(eng)
    pi.state.tags.append(_tag("llama3.1:8b"))
    _routing(eng, pi)
    out = eng.ce.local_chat(MSGS, role="peasant", num_predict=20)
    assert out == "Answered on the pi."
    assert len(pi.state.chats) == 1 and pi.state.chats[0]["model"] == \
        "llama3.2:3b"
    assert eng.fake.state.chats == []
    assert eng.ce.last_call_stats("peasant")["host"] == pi.url
    # Another role is not bound: it stays here.
    eng.fake.state.tags.append(_tag("llama3.2:3b"))
    assert eng.ce.local_chat(MSGS, role="writer", num_predict=20,
                             model="ollama:llama3.2:3b") == "Answered here."


def test_routing_off_keeps_the_role_here(eng, pi):
    _peasant_on_3b(eng)
    eng.fake.state.tags.append(_tag("llama3.2:3b"))
    _routing(eng, pi, routing_enabled=False)
    assert eng.ce.local_chat(MSGS, role="peasant") == "Answered here."
    assert pi.state.chats == []


def test_a_node_without_the_model_falls_back_and_rests(eng, pi):
    _slots(eng, {"big": {"path": "ollama:llama3.1:8b"}}, {"peasant": "big"})
    pi.state.tags[:] = [_tag("phi3")]              # not the role's model
    _routing(eng, pi)
    assert eng.ce.local_chat(MSGS, role="peasant") == "Answered here."
    assert pi.state.chats == []
    assert nr.cooling(pi.url) > 0
    seen = len(pi.state.requests)
    assert eng.ce.local_chat(MSGS, role="peasant") == "Answered here."
    assert len(pi.state.requests) == seen          # resting: not asked


def test_fallback_fail_raises_instead(eng, pi):
    _slots(eng, {"big": {"path": "ollama:llama3.1:8b"}}, {"peasant": "big"})
    pi.state.tags[:] = [_tag("phi3")]
    _routing(eng, pi, fallback="fail")
    with pytest.raises(eng.ce.BackendUnavailable, match="fallback is 'fail'"):
        eng.ce.local_chat(MSGS, role="peasant")
    assert eng.fake.state.chats == []
    with pytest.raises(eng.ce.BackendUnavailable, match="resting"):
        eng.ce.local_chat(MSGS, role="peasant")


def test_output_already_produced_is_never_re_sent(eng, pi):
    _peasant_on_3b(eng)
    pi.state.reply = "a long answer that the node never finishes " * 3
    pi.state.drop_after = 3                         # dies mid-answer
    eng.fake.state.tags.append(_tag("llama3.2:3b"))
    _routing(eng, pi)
    with pytest.raises(RuntimeError):
        eng.ce.local_chat(MSGS, role="peasant")
    assert pi.state.dropped == 1                    # output was produced
    assert eng.fake.state.chats == []               # and never re-sent here
    assert nr.cooling(pi.url) == 0                  # not "unreachable"


def test_a_node_that_recovers_is_used_again(eng, pi):
    _peasant_on_3b(eng)
    _routing(eng, pi)
    nr.mark_failed(pi.url)
    eng.fake.state.tags.append(_tag("llama3.2:3b"))
    assert eng.ce.local_chat(MSGS, role="peasant") == "Answered here."
    nr.mark_ok(pi.url)
    assert eng.ce.local_chat(MSGS, role="peasant") == "Answered on the pi."


def test_cooldown_doubles_to_a_ceiling():
    nr.reset_cooldowns()
    url = "http://10.9.9.9:11434"
    waits = [nr.mark_failed(url, now=0.0) for _ in range(6)]
    assert waits[:4] == [30.0, 60.0, 120.0, 240.0]
    assert waits[-1] == nr.COOLDOWN_MAX_S
    nr.mark_ok(url)
    assert nr.cooling(url) == 0
    nr.reset_cooldowns()


# ---- concurrency ---------------------------------------------------------------

def _peak(url, n, routing):
    lock, now, peak = threading.Lock(), [0], [0]

    def call():
        with nr.host_slot(url, routing):
            with lock:
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1
        return True
    results = nr.run_parallel([call] * n)
    assert all(r.ok for r in results)
    return peak[0]


def test_each_machine_runs_at_most_its_parallel_calls():
    url = "http://10.0.0.5:11434"
    one = nr.Routing(True, {"pi": nr.Node("pi", url, True, 1)}, {})
    two = nr.Routing(True, {"pi": nr.Node("pi", url, True, 2)}, {})
    assert _peak(url, 4, one) == 1
    assert _peak(url, 4, two) == 2


def test_run_parallel_overlaps_and_keeps_order_and_errors():
    def slow(i):
        def fn():
            time.sleep(0.1)
            if i == 2:
                raise ValueError("boom")
            return i
        return fn
    t0 = time.monotonic()
    out = nr.run_parallel([slow(i) for i in range(4)])
    assert time.monotonic() - t0 < 0.35            # not 0.4 s in series
    assert [o.value for o in out] == [0, 1, None, 3]
    assert isinstance(out[2].error, ValueError)


def test_two_roles_on_two_machines_really_run_side_by_side(eng, pi):
    _peasant_on_3b(eng)
    _slots(eng, {"fast": {"path": "ollama:llama3.2:3b"}},
           {"peasant": "fast", "intern": "fast"})
    eng.fake.state.tags.append(_tag("llama3.2:3b"))
    _routing(eng, pi, roles=("peasant",))
    for srv in (pi, eng.fake):
        srv.state.first_delay = 0.4
    t0 = time.monotonic()
    out = nr.run_parallel([
        lambda: eng.ce.local_chat(MSGS, role="peasant"),
        lambda: eng.ce.local_chat(MSGS, role="intern")])
    took = time.monotonic() - t0
    assert [o.value for o in out] == ["Answered on the pi.", "Answered here."]
    assert took < 0.75, took                        # not 0.8 s in series


def test_the_file_never_holds_secrets(tmp_path):
    r = nr.Routing(True, {"pi": nr.Node("pi", "http://10.0.0.5:11434", True)},
                   {})
    data = json.loads(nr.save(tmp_path, r).read_text())
    assert set(data["nodes"]["pi"]) == {"url", "enabled", "parallel"}
