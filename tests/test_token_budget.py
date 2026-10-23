"""Reply lengths follow the model serving the call (user, 2026-10-23: "auto
adjust the max token counts based on the model being used"): a reply KIND
grows with the window, only into room the prompt leaves, and is exactly its
old fixed number on a small window."""
from __future__ import annotations

import copy
import json

import pytest

import council_engine as ce
from council_core import token_budget as tb
from council_core.token_budget import Reply


def _count(text: str) -> int:
    return len(text) // 4


@pytest.fixture(autouse=True)
def _auto_on(monkeypatch):
    monkeypatch.delenv("COUNCIL_REPLY_AUTO", raising=False)
    monkeypatch.delenv("COUNCIL_GGUF_N_CTX", raising=False)


def test_a_reply_is_its_floor_wherever_kinds_are_not_known():
    r = tb.reply("long", 2400)
    assert r == 2400 and int(r) == 2400 and isinstance(r, int)
    assert json.dumps({"num_predict": r}) == '{"num_predict": 2400}'
    assert repr(r) == "Reply('long', 2400)"
    again = copy.deepcopy(r)
    assert isinstance(again, Reply) and again.kind == "long" and again == 2400
    with pytest.raises(ValueError):
        tb.reply("huge", 100)


def test_the_target_grows_with_the_window_between_floor_and_ceiling():
    long_ = tb.reply("long", 2400)
    assert tb.target(long_, 4096) == 2400          # 4096 / 6 is under the floor
    assert tb.target(long_, 32768) == 32768 // 6
    assert tb.target(long_, 131072) == 6144        # the ceiling
    assert tb.target(tb.reply("short", 400), 16384) == 16384 // 20
    assert tb.target(tb.reply("answer", 600), 16384) == 16384 // 10
    assert tb.target(700, 131072) == 700           # a plain number is itself


def test_the_growth_only_takes_room_the_prompt_leaves():
    r = tb.reply("long", 2400)
    assert tb.fit(r, 32768, 1000, 80) == 32768 // 6
    # a prompt that leaves 3,000 tokens: the reply takes those, not its target
    assert tb.fit(r, 32768, 32768 - 80 - 3000, 80) == 3000
    # a prompt that leaves less than the floor: the floor, as before
    assert tb.fit(r, 32768, 32000, 80) == 2400


def test_headroom_stays_on_top_and_unscaled():
    r = tb.plus(tb.reply("short", 400), 512)
    assert isinstance(r, Reply) and r == 912
    assert tb.target(r, 32768) == 1024 + 512
    assert tb.plus(300, 50) == 350


def test_the_switch_puts_every_reply_back_to_its_old_length(monkeypatch):
    monkeypatch.setenv("COUNCIL_REPLY_AUTO", "0")
    assert tb.target(tb.reply("long", 2400), 131072) == 2400
    assert tb.fit(tb.reply("long", 2400), 131072, 100, 80) == 2400


@pytest.mark.parametrize("floor", [300, 600, 1400, 2400])
@pytest.mark.parametrize("prompt_chars", [400, 8000, 40000])
def test_never_shorter_than_the_old_number_and_the_prompt_is_kept(floor,
                                                                 prompt_chars):
    msgs = [{"role": "system", "content": "be brief"},
            {"role": "user", "content": "q" * prompt_chars}]
    for window in (2048, 4096, 8192):
        for kind in tb.KINDS:
            old_out, old_reply = ce._clamp_messages_to_ctx(
                msgs, floor, window, count_tokens=_count)
            new_out, new_reply = ce._clamp_messages_to_ctx(
                msgs, tb.reply(kind, floor), window, count_tokens=_count)
            assert new_out == old_out and new_reply >= old_reply
    # where the kind's share is under the floor, exactly the old call
    assert (ce._clamp_messages_to_ctx(msgs, tb.reply("short", floor), 4096,
                                      count_tokens=_count)
            == ce._clamp_messages_to_ctx(msgs, floor, 4096, count_tokens=_count))


def test_a_big_window_gives_the_reply_room_without_trimming_the_prompt():
    msgs = [{"role": "system", "content": "be thorough"},
            {"role": "user", "content": "q" * 8000}]           # 2,000 tokens
    out, reply = ce._clamp_messages_to_ctx(msgs, tb.reply("long", 2400), 32768,
                                           count_tokens=_count)
    assert out == msgs and reply == 32768 // 6
    # a prompt that nearly fills the window keeps every token it had with the
    # old fixed number: the reply grows only into what is left
    full = [{"role": "user", "content": "q" * 4 * (32768 - 3500)}]
    old_out, old_reply = ce._clamp_messages_to_ctx(full, 2400, 32768,
                                                   count_tokens=_count)
    new_out, new_reply = ce._clamp_messages_to_ctx(full, tb.reply("long", 2400),
                                                   32768, count_tokens=_count)
    assert new_out == old_out and 2400 <= new_reply <= old_reply + 1100
    assert _count(new_out[0]["content"]) + new_reply + ce._template_margin(1) <= 32768


def test_the_council_s_roles_ask_by_kind_with_their_old_numbers():
    assert ce.PersonalityModel.max_output_tokens == 1400
    assert ce.PersonalityModel.max_output_tokens.kind == "answer"
    assert ce.ROLE_TOKEN_BUDGETS["writer"] == 2400       # turn_context reads ints
    assert ce.ROLE_REPLY_KINDS.get("writer", "long") == "long"


def test_a_backend_hands_the_kind_to_the_router(monkeypatch):
    seen = {}

    def fake_route(messages, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(ce, "_route_chat", fake_route)
    spec = ce.LocalBackendSpec(key="k", host="http://127.0.0.1:11434",
                               model="m", tags={})
    spec.generate(developer_instructions="sys", user_text="hi",
                  max_tokens=tb.reply("short", 600), trace=False)
    assert isinstance(seen["num_predict"], Reply)
    assert seen["num_predict"].kind == "short"
    spec.generate(developer_instructions="sys", user_text="hi",
                  max_tokens=500, trace=False)
    assert seen["num_predict"] == 500 and not isinstance(seen["num_predict"], Reply)
