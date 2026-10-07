"""council_core/kg_extract.py (the checks on model-proposed links) and
council_core/kg_bench.py (the KG0 benchmark), with scripted models only."""
from __future__ import annotations

import json

import pytest

from council_core import kg_bench as kb
from council_core import kg_extract as kx

KNOWN = kb.known()
NAME = {k.key: k.name for k in KNOWN}


def scripted(links):
    return lambda messages, **kw: json.dumps({"links": links})


def as_raw(gold, quote):
    return [{"subject": NAME[s], "predicate": p, "object": NAME[o], "quote": quote}
            for s, p, o in gold]


def test_a_perfect_model_scores_one():
    """The benchmark's own consistency: every gold link survives the checks."""
    for s in kb.SNIPPETS:
        res = kx.extract(s.text, KNOWN, scripted(as_raw(s.gold, s.text)))
        assert sorted((l.subject, l.predicate, l.object) for l in res.links) == sorted(s.gold), s.id
        assert not res.rejected, (s.id, res.rejected)
    rep = kb.run_model("perfect", chat=None if False else _perfect_chat())
    assert (rep.precision, rep.recall, rep.f1) == (1.0, 1.0, 1.0)


def _perfect_chat():
    by_text = {s.text: s for s in kb.SNIPPETS}

    def chat(messages, **kw):
        user = messages[-1]["content"]
        s = next(sn for t, sn in by_text.items() if t in user)
        return json.dumps({"links": as_raw(s.gold, s.text)})
    return chat


def test_reversed_types_are_turned_round():
    text = "The BRG-7720 bearing pack will be fitted to the Helios rig."
    res = kx.extract(text, KNOWN, scripted([{"subject": "BRG-7720", "predicate": "USES_PART",
                                             "object": "Helios", "quote": text}]))
    assert [(l.subject, l.object) for l in res.links] == [("helios", "brg")]
    assert "reversed" in res.links[0].fixed


def test_supersedes_follows_the_revision_letters():
    text = "PN-1234/A has been replaced by PN-1234/B."
    res = kx.extract(text, KNOWN, scripted([{"subject": "PN-1234/A", "predicate": "SUPERSEDES",
                                             "object": "PN-1234/B", "quote": text}]))
    assert [(l.subject, l.object) for l in res.links] == [("shim_b", "shim_a")]


@pytest.mark.parametrize("raw, why", [
    ({"subject": "BRG-7720", "predicate": "LEADS", "object": "Helios"}, "cannot link"),
    ({"subject": "Zed Nobody", "predicate": "LEADS", "object": "Helios"}, "not known"),
    ({"subject": "Dana Whitfield", "predicate": "LIKES", "object": "Helios"}, "unknown relationship"),
    ({"subject": "Dana Whitfield", "predicate": "LEADS", "object": "Helios",
      "quote": "Dana leads Helios"}, "quote is not in the text"),
    ({"subject": "D. Whitfield", "predicate": "WORKS_ON", "object": "Helios"}, "not known"),
])
def test_bad_links_are_rejected_with_a_reason(raw, why):
    text = "Dana Whitfield leads the Helios Turbine Upgrade. D. Whitfield reviewed it."
    raw = {"quote": "Dana Whitfield leads the Helios Turbine Upgrade.", **raw}
    res = kx.extract(text, KNOWN, scripted([raw]))
    assert not res.links and why in res.rejected[0].why


def test_council_finds_the_line_not_the_model():
    text = "Header\n\nNotes:\nTomás Echeverría and Bob Smith\nattended the Northwind review."
    quote = "Tomás Echeverría and Bob Smith attended the Northwind review"
    res = kx.extract(text, KNOWN, scripted([{"subject": "Bob Smith", "predicate": "WORKS_ON",
                                             "object": "Northwind", "quote": quote}]))
    assert res.links[0].line == 4


def test_garbage_reply_is_an_error_not_links():
    res = kx.extract("x" * 20, KNOWN, lambda m, **k: "I think Dana leads Helios.")
    assert res.error and not res.links


def test_prompt_lists_known_entities_and_types():
    msgs = kx.build_prompt("Some text.", KNOWN)
    user = msgs[-1]["content"]
    assert "PERSON: Dana Whitfield (also: Whitfield, Dana, D. Whitfield)" in user
    assert "SUPERSEDES: newer PART supersedes" in user and '"""\nSome text.\n"""' in user


def test_scores_count_false_links_on_empty_snippets():
    rep = kb.score("m", [kb.SnippetResult("s", "t", [], [("a", "LEADS", "b")], 0, 0, "", 1.0),
                         kb.SnippetResult("t", "t", [("a", "OWNS", "c")], [("a", "OWNS", "c")],
                                          1, 0, "", 3.0)])
    assert rep.precision == 0.5 and rep.recall == 1.0 and rep.no_link_false_positives == 1
    assert rep.seconds_per_snippet == 2.0 and rep.rejected == 1


def test_a_name_the_text_never_says_is_refused():
    """KG0 2026-10-06: phi4:14b and gemma3:12b answered 'Dana Whitfield' for a
    passage that only says 'D. Whitfield' (Dana and Dan are both known)."""
    text = "D. Whitfield reviewed the shim drawings for Helios."
    res = kx.extract(text, KNOWN, scripted([{"subject": "Dana Whitfield", "predicate": "WORKS_ON",
                                             "object": "Helios", "quote": text}]))
    assert not res.links and "never names Dana Whitfield" in res.rejected[0].why


@pytest.mark.parametrize("text, key", [
    ("Atlas runs on the CTL-5005 board.", "atlas"),            # first word of 'Atlas Test Rig'
    ("Point of contact: Raman, Priya.", "priya"),              # Last, First
    ("Tomas Echeverria joined.", "tomas"),                     # accents dropped
])
def test_named_in_accepts_the_ways_text_names_things(text, key):
    k = next(k for k in KNOWN if k.key == key)
    assert kx.named_in(text, k)


def test_a_name_far_from_the_quote_does_not_count():
    """Ironbridge, 2026-10-06: phi4:14b credited line 13's 'D. Whitfield will
    review the shim drawings' to Dana, who is named only on line 4."""
    text = ("Owner: Whitfield, Dana\n" + "Filler line.\n" * 8 +
            "D. Whitfield will review the shim drawings for Helios next week.")
    res = kx.extract(text, KNOWN, scripted([{"subject": "Dana Whitfield", "predicate": "WORKS_ON",
                                             "object": "Helios",
                                             "quote": "D. Whitfield will review the shim drawings for Helios next week."}]))
    assert not res.links and "near that quote" in res.rejected[0].why


def test_a_pronoun_in_the_next_sentence_is_fine():
    text = "Priya Raman owns the SL-0450 seal.\nShe is also the contact for it."
    res = kx.extract(text, KNOWN, scripted([{"subject": "Priya Raman", "predicate": "CONTACT_FOR",
                                             "object": "SL-0450",
                                             "quote": "She is also the contact for it."}]))
    assert [(l.subject, l.object) for l in res.links] == [("priya", "seal")]


# ── review fixes (2026-10-07) ─────────────────────────────────────────────
def test_a_quote_wrapped_over_many_lines_keeps_the_names_inside_it():
    """A 195-character quote hard-wrapped over lines 3-6 (PDF page text has
    short lines) named Hana Kowalski on line 6; the 'near' window was
    anchored at the quote's FIRST line (lines 2-5), so the name INSIDE the
    quote was "never named near that quote"."""
    known = [kx.Known("hana", "PERSON", "Hana Kowalski"),
             kx.Known("ctl", "PART", "CTL-5005"),
             kx.Known("atlas", "PROJECT", "PRJ-0944", ["Atlas Test Rig"])]
    text = ("Atlas sync, 2 April\n"
            "\n"
            "After the second firmware fault on the bench the team agreed that the\n"
            "CTL-5005 controller board, including its spares, its calibration\n"
            "records and the open firmware tickets, will from now on be the\n"
            "responsibility of one person, and that person is Hana Kowalski.\n")
    quote = ("the CTL-5005 controller board, including its spares, its calibration records "
             "and the open firmware tickets, will from now on be the responsibility of one "
             "person, and that person is Hana Kowalski.")
    assert kx.find_quote_span(text, quote) == (3, 6)
    assert kx.find_quote(text, quote) == 3
    links, rejected = kx.check([{"subject": "Hana Kowalski", "predicate": "OWNS",
                                 "object": "CTL-5005", "quote": quote}], text, known)
    assert [(l.subject, l.predicate, l.object, l.line) for l in links] == [
        ("hana", "OWNS", "ctl", 3)], [r.why for r in rejected]


def test_the_near_window_still_ends_two_lines_after_the_quote():
    known = [kx.Known("hana", "PERSON", "Hana Kowalski"),
             kx.Known("ctl", "PART", "CTL-5005")]
    text = ("The CTL-5005 board goes to\none owner from Monday.\n"
            "a\nb\nc\nHana Kowalski is named only here.\n")
    links, rejected = kx.check([{"subject": "Hana Kowalski", "predicate": "OWNS",
                                 "object": "CTL-5005",
                                 "quote": "The CTL-5005 board goes to one owner from Monday."}],
                               text, known)
    assert not links and "near that quote" in rejected[0].why
