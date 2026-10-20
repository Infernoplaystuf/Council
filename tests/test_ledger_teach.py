"""Teach from a sample workbook (council_core/ledger_teach.py).

The user's request (2026-10-20): "taking a sample workbook from the folder and
I line up where they match the templates, or say data found in the images
and what they are in the template. First I want an LLM guess to be made, then
I give an answer — hopefully this will help train the Ledger and knowledge
graph to work."

What is pinned: the guess lists what the rules placed, what they left, the
part number, the inspectors and a picture's text, with where each guess came
from; Council's model is asked only where the rules stop, with the user's
past answers as examples; learning writes the aliases, ignores, inspectors'
labels, people, the part-name pattern and the examples file, and ticks the
knowledge graph's rules; the next guess places by rule what the model had to
guess. Synthetic workbooks only; a fake model; no network.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")
from openpyxl.styles import Font  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import kg_excel  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_llm  # noqa: E402
from council_core import ledger_teach as lt  # noqa: E402
from sample_ledger import LedgerError, ParentCSV  # noqa: E402

INSPECTOR = "[an inspector's initials or name]"
NOT_DATA = "[not data: a title, heading or note]"


class FakeModel:
    """Answers Teach's two questions from fixed answers; keeps the prompts."""

    def __init__(self, mapping=None, part="XYZ9AB123-R2"):
        self.mapping, self.part, self.prompts = dict(mapping or {}), part, []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        if "Which part" in prompt:
            return json.dumps({"part": self.part, "evidence": "Serial"}), "fake-granite"
        labels = schema["properties"]["mappings"]["items"]["properties"]["label"]["enum"]
        return json.dumps({"mappings": [{"label": lab, "field": self.mapping.get(lab)}
                                        for lab in labels]}), "fake-granite"

    def label_prompts(self):
        return [p for p in self.prompts if "template field it means" in p]

    def part_prompts(self):
        return [p for p in self.prompts if "Which part" in p]


@pytest.fixture()
def ledger(tmp_path):
    ps = li.create_parent(lf.make_template(tmp_path / "template.xlsx"))
    return Path(ps.csv), ParentCSV(ps.csv).schema


def _field(schema, word):
    return next(f.name for f in schema.fields if word in f.name)


def _unlabelled_book(path: Path) -> Path:
    """No 'Part Number' label, an odd serial, labels no rule knows, an
    inspector's label no rule knows, and a note."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Serial: XYZ9AB123-R2"
    ws["A3"], ws["B3"] = "Power (W)", 280
    ws["A4"], ws["B4"] = "Zorp rate", 55
    ws["A5"], ws["B5"] = "Tech Sig", "JB"
    ws["A6"], ws["B6"] = "Remarks to self", "call MK"
    wb.save(path)
    return path


def _labelled_book(path: Path, with_picture: bool = False) -> Path:
    """A part number the rules read, a label they match, one they do not,
    'Inspector: JB', and a flaw table with a 'Flaw ID' column."""
    from openpyxl.drawing.image import Image as XLImage
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"] = "Part Number: LMM1AB101"
    ws["A2"] = "Inspector: JB"
    ws["A3"], ws["B3"] = "Power (W)", 280
    ws["A4"], ws["B4"] = "Zorp rate", 55
    fl = wb.create_sheet("Flaws")
    fl.append(["Flaw ID", "Passes"])
    for item in lf.ITEMS[:3]:
        fl.append([item, 300])
    if with_picture:
        ws.add_image(XLImage(__import__("io").BytesIO(lf._png(lf.CONCEALMENT_IMAGE_ROWS))), "F2")
    wb.save(path)
    return path


def _row(session, label):
    return next(r for r in session.rows if r.label == label)


# ── the dropdown's choices ──────────────────────────────────────────────
def test_choices_hold_every_role_and_field_but_the_part_numbers(ledger):
    _csv, schema = ledger
    keys = [k for k, _t in lt.choices(schema)]
    assert keys[:5] == [lt.ROLE_PART, lt.ROLE_INSPECTOR, lt.ROLE_ITEM, lt.ROLE_IGNORE,
                        lt.ROLE_UNSURE]
    assert "field:Power (W)" in keys and "field:Part Number" not in keys
    texts = dict(lt.choices(schema))
    assert texts[lt.ROLE_ITEM] == "Flaw # code"
    assert "(per Flaw #)" in texts["field:" + _field(schema, "Passes")]
    for key in keys:
        role, fname = lt.parse_choice(key)
        assert lt.choice_key(role, fname) == key
    assert lt.parse_choice("nonsense") == (lt.ROLE_UNSURE, "")
    assert lt.parse_choice(lt.Guess(lt.ROLE_FIELD, "Power (W)")) == (lt.ROLE_FIELD, "Power (W)")
    assert lt.parse_choice(("inspector", "x")) == (lt.ROLE_INSPECTOR, "")


def test_a_part_numbers_shape_becomes_a_pattern():
    assert se.mask_from_part("ABC1DE234") == "@@@#@@###"
    assert se.mask_from_part("XYZ9AB123-R2") == "@@@#@@###-@#"
    assert se.mask_to_regex(se.mask_from_part("P/N-0042")).fullmatch("Q/R-1234")


# ── the guess ───────────────────────────────────────────────────────────
def test_the_rules_guess_lists_matched_unmatched_part_inspector_item_and_picture(
        tmp_path, ledger):
    csv, schema = ledger
    se.save_people(csv, {"JB": "Jordan Blake"})
    src = _labelled_book(tmp_path / "sample.xlsx", with_picture=True)
    s = lt.guess(csv, src, use_images=True,
                 image_reader=lf.fake_reader(lf.CONCEALMENT_IMAGE_ROWS))
    assert not s.cancelled and s.model == "" and not s.model_picked
    # a label the rules matched: shown, so a wrong match can be corrected
    power = _row(s, "Power (W)")
    assert (power.kind, power.guess.key, power.guess.via) == ("label", "field:Power (W)",
                                                              lt.VIA_RULE)
    assert power.where == "Build!A3" and power.example == "280" and power.guess.score >= 0.85
    # a label nothing placed
    zorp = _row(s, "Zorp rate")
    assert (zorp.guess.role, zorp.guess.via, zorp.example) == (lt.ROLE_UNSURE, lt.VIA_NONE,
                                                               "55")
    # the part number's label and the part
    pn = _row(s, "Part Number")
    assert pn.kind == "part" and pn.guess.role == lt.ROLE_PART and pn.example == "LMM1AB101"
    assert s.part.value == "LMM1AB101" and s.part.via == lt.VIA_RULE
    assert s.part.where == "Build!A1"
    # the inspector's label, and who it is from the people list
    insp = _row(s, "Inspector")
    assert (insp.kind, insp.guess.role, insp.example, insp.initials) == (
        "inspector", lt.ROLE_INSPECTOR, "JB", ["JB"])
    assert [(p.initials, p.full_name) for p in s.inspectors] == [("JB", "Jordan Blake")]
    # the flaw-code column
    flaw = _row(s, "Flaw ID")
    assert flaw.kind == "item" and flaw.guess.role == lt.ROLE_ITEM and flaw.example == "1A"
    passes = _row(s, "Passes")
    assert passes.count == 3 and "Flaw # codes (1A … 3A)" in passes.note
    # what the picture's reader read, with the picture named
    media = next(r for r in s.rows if r.picture and r.label == "Media")
    assert media.picture == "picture 1 on Build"
    assert media.where == "picture 1 on Build, row 1 column 1"
    assert "ceramic" in media.picture_text and media.guess.role == lt.ROLE_FIELD
    assert "read from a picture" in media.note
    assert (s.pictures, s.pictures_read) == (1, 1)
    # keys are stable: the same workbook gives the same keys
    again = lt.guess(csv, src, use_images=True,
                     image_reader=lf.fake_reader(lf.CONCEALMENT_IMAGE_ROWS))
    assert [r.key for r in again.rows] == [r.key for r in s.rows]
    assert len({r.key for r in s.rows}) == len(s.rows)
    # what needs an answer first: the part's, the inspector's, the flaw
    # codes', then the unplaced labels before the sure rule matches
    kinds = [r.kind for r in s.rows]
    assert kinds.index("part") < kinds.index("inspector") < kinds.index("item") \
        < kinds.index("label")
    labels = [r for r in s.rows if r.kind == "label"]
    assert labels.index(zorp) < labels.index(power)
    text = s.describe()
    assert "sample.xlsx" in text and "LMM1AB101" in text and "JB (Jordan Blake)" in text


def test_the_model_guesses_where_the_rules_stop(tmp_path, ledger):
    csv, schema = ledger
    freq = _field(schema, "Frequency")
    model = FakeModel({"Zorp rate": freq, "Tech Sig": INSPECTOR,
                       "Remarks to self": NOT_DATA})
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    s = lt.guess(csv, src, chat=model, use_images=False, pattern_mask="@@@#@@###")
    assert s.model == "fake-granite"
    zorp, tech, note = _row(s, "Zorp rate"), _row(s, "Tech Sig"), _row(s, "Remarks to self")
    assert (zorp.guess.key, zorp.guess.via, zorp.guess.score) == (
        f"field:{freq}", lt.VIA_MODEL, ledger_llm.MODEL_SCORE)
    assert (tech.guess.role, tech.guess.via, tech.initials) == (lt.ROLE_INSPECTOR,
                                                                lt.VIA_MODEL, ["JB"])
    assert note.guess.role == lt.ROLE_IGNORE
    # what the rules placed is not asked about; only labels are sent
    asked = model.label_prompts()
    assert len(asked) == 1
    assert json.loads(asked[0].split("Labels:\n", 1)[1]) == [
        "Remarks to self", "Serial", "Tech Sig", "Zorp rate"]
    assert "55" not in asked[0] and "call MK" not in asked[0]
    # the roles are offered beside the fields, never the part number's field
    offered = json.loads(asked[0].split("Template fields:\n", 1)[1].split("\n\n")[0])
    assert INSPECTOR in offered and NOT_DATA in offered and "Part Number" not in offered
    # no part number by the rules: the model's, written in the workbook,
    # with the user's pattern as an example - and its label is the part's
    assert s.part.value == "XYZ9AB123-R2" and s.part.via == lt.VIA_MODEL
    assert s.part.label == "Serial" and "check it" in s.part.how
    serial = _row(s, "Serial")
    assert (serial.guess.role, serial.guess.via) == (lt.ROLE_PART, lt.VIA_MODEL)
    assert "'@@@#@@###'" in model.part_prompts()[0]
    # the inspector the model found is in the People section, to be named
    assert [p.initials for p in s.inspectors] == ["JB"]


def test_a_model_that_is_not_us_origin_is_refused_before_reading(tmp_path, ledger,
                                                                 monkeypatch):
    csv, _schema = ledger
    from council_core import local_models
    monkeypatch.setattr(local_models, "maker_and_origin", lambda name: ("Someone", "non-US"))
    with pytest.raises(LedgerError, match="not a US-origin model"):
        lt.guess(csv, _unlabelled_book(tmp_path / "u.xlsx"), model_id="ollama:qwen2.5:7b",
                 use_images=False)


def test_cancel_stops_the_guess(tmp_path, ledger):
    csv, _schema = ledger
    stop = threading.Event()
    stop.set()
    s = lt.guess(csv, _labelled_book(tmp_path / "s.xlsx"), use_images=False, cancel=stop)
    assert s.cancelled and not s.rows and "cancelled" in s.describe()


def test_cancel_while_the_model_is_asked_keeps_the_rules_guesses(tmp_path, ledger):
    csv, _schema = ledger
    stop = threading.Event()
    lines = []

    def chat(prompt, schema):
        stop.set()                                  # pressed while it was asked
        raise RuntimeError("stopped by the caller")
    s = lt.guess(csv, _unlabelled_book(tmp_path / "u.xlsx"), chat=chat, use_images=False,
                 cancel=stop, progress=lambda line, frac=None: lines.append(line))
    assert s.cancelled and s.rows
    assert _row(s, "Zorp rate").guess.via == lt.VIA_NONE
    assert not any("did not answer" in p for p in s.problems)   # Cancel is no failure
    assert "Cancel stopped Council's model" in s.describe()
    assert any("asking Council's model" in line for line in lines)


def test_a_model_that_does_not_answer_is_said_once(tmp_path, ledger):
    csv, _schema = ledger

    def chat(prompt, schema):
        raise ConnectionRefusedError("nothing listens there")
    s = lt.guess(csv, _unlabelled_book(tmp_path / "u.xlsx"), chat=chat, use_images=False)
    assert s.model.endswith("(did not answer)") and not s.cancelled
    assert sum("did not answer" in p for p in s.problems) == 1
    assert _row(s, "Zorp rate").guess.via == lt.VIA_NONE and s.part is None
    assert "part number: not found" in s.describe()


def test_a_long_list_is_capped_with_a_note(tmp_path, ledger):
    csv, _schema = ledger
    s = lt.guess(csv, _labelled_book(tmp_path / "s.xlsx"), use_images=False, max_rows=2)
    assert len(s.rows) == 2 and s.hidden > 0
    assert s.rows[0].kind == "part"
    assert any("not shown" in p for p in s.problems)


def test_an_unknown_workbook_or_a_ledger_without_schema_is_said(tmp_path, ledger):
    csv, _schema = ledger
    with pytest.raises(LedgerError, match="not found"):
        lt.guess(csv, tmp_path / "nowhere.xlsx", use_images=False)
    plain = tmp_path / "plain.csv"
    plain.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="no schema"):
        lt.guess(plain, _labelled_book(tmp_path / "s.xlsx"), use_images=False)


# ── learning ────────────────────────────────────────────────────────────
def _taught(tmp_path, ledger, vault=None):
    """Guess the unlabelled book with the model, answer every guess as it
    is, name JB, confirm the part and keep its shape."""
    csv, schema = ledger
    freq = _field(schema, "Frequency")
    model = FakeModel({"Zorp rate": freq, "Tech Sig": INSPECTOR,
                       "Remarks to self": NOT_DATA})
    src = _unlabelled_book(tmp_path / "untitled.xlsx")
    s = lt.guess(csv, src, chat=model, use_images=False)
    rep = lt.learn(csv, s, lt.answers_from_guesses(s), {"JB": "Jordan Blake", "MK": ""},
                   part="XYZ9AB123-R2", save_pattern=True, connections_vault=vault)
    return csv, schema, src, s, rep, model


def test_learning_writes_aliases_ignores_inspectors_people_pattern_and_examples(
        tmp_path, ledger):
    csv, schema, _src, _s, rep, _m = _taught(tmp_path, ledger)
    freq = _field(schema, "Frequency")
    data = json.loads(se.aliases_path_for(csv).read_text(encoding="utf-8"))
    assert data["map"] == {"serial": "Part Number", "zorp rate": freq}
    assert data["ignore"] == ["remarks to self"]
    assert data["who_labels"] == ["Tech Sig"]
    assert data["people"] == {"JB": "Jordan Blake"}            # a blank name is no answer
    assert data["patterns"] == ["@@@#@@###-@#"]
    assert se.load_patterns(csv) == ["@@@#@@###-@#"]
    assert rep.mapped == {"Zorp rate": freq} and rep.who_labels == ["Tech Sig"]
    assert rep.part_labels == ["Serial"] and rep.ignored == ["Remarks to self"]
    assert rep.pattern == "@@@#@@###-@#" and rep.people == {"JB": "Jordan Blake"}
    assert rep.unchanged == 1                                  # Power (W): the rules knew
    # the examples: labels and roles; a value only for the part number
    ex = lt.examples(csv)
    by = {(e.label, e.role) for e in ex}
    assert {("Zorp rate", "field"), ("Tech Sig", "inspector"), ("Serial", "part"),
            ("Remarks to self", "ignore"), ("Power (W)", "field")} <= by
    assert {e.value for e in ex if e.role != lt.ROLE_PART} == {""}
    assert {e.value for e in ex if e.role == lt.ROLE_PART} == {"XYZ9AB123-R2"}
    raw = lt.examples_path_for(csv).read_text(encoding="utf-8")
    assert "call MK" not in raw and "55" not in raw.replace('"ts"', "")
    assert all(e.workbook == "untitled.xlsx" for e in ex)
    text = rep.describe()
    assert "Zorp rate → " in text and "Tech Sig" in text and "@@@#@@###-@#" in text


def test_the_same_answers_again_add_no_examples(tmp_path, ledger):
    csv, _schema, src, s, rep, _m = _taught(tmp_path, ledger)
    n = len(lt.examples(csv))
    rep2 = lt.learn(csv, s, lt.answers_from_guesses(s))
    assert rep2.examples == 0 and len(lt.examples(csv)) == n
    assert rep.examples == n


def test_a_second_guess_places_by_rule_what_the_model_had_to_guess(tmp_path, ledger):
    csv, schema, src, before, _rep, _m = _taught(tmp_path, ledger)
    model = FakeModel()
    after = lt.guess(csv, src, chat=model, use_images=False)
    zorp, tech = _row(after, "Zorp rate"), _row(after, "Tech Sig")
    assert (zorp.guess.key, zorp.guess.via) == (f"field:{_field(schema, 'Frequency')}",
                                                lt.VIA_ALIAS)
    assert (tech.kind, tech.guess.role, tech.guess.via) == ("inspector", lt.ROLE_INSPECTOR,
                                                            lt.VIA_ALIAS)
    assert _row(after, "Remarks to self").guess.key == lt.ROLE_IGNORE
    # the part number is read by the rules now (the label is the part's)
    assert after.part.value == "XYZ9AB123-R2" and after.part.via == lt.VIA_ALIAS
    assert after.pattern_saved and after.pattern_mask == "@@@#@@###-@#"
    # nothing left for the model: it was not asked
    assert model.prompts == []
    assert "not needed" in after.describe()
    assert [(p.initials, p.full_name) for p in after.inspectors] == [("JB", "Jordan Blake")]
    said = lt.improvement(before, after)
    assert said.startswith("4 thing(s)") and "Zorp rate → " in said
    assert "XYZ9AB123-R2 is now read by the rules" in said and "nothing left unsure" in said


def test_past_answers_go_in_the_models_prompts(tmp_path, ledger):
    csv, schema, _src, _s, _rep, _m = _taught(tmp_path, ledger)
    other = tmp_path / "other.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Note: no id on this one"
    ws["A2"], ws["B2"] = "Zorp rates", 57
    ws["A3"], ws["B3"] = "Blip count", 3
    wb.save(other)
    model = FakeModel(part="")
    lt.guess(csv, other, chat=model, use_images=False)
    asked = model.label_prompts()[0]
    freq = _field(schema, "Frequency")
    assert "The user answered these labels before" in asked
    assert f'"Zorp rate" -> "{freq}"' in asked                 # most like 'Zorp rates'
    assert f'"Tech Sig" -> "{INSPECTOR}"' in asked
    # the part question shows the part numbers the user confirmed
    assert '"XYZ9AB123-R2" (labelled "Serial")' in model.part_prompts()[0]


def test_extracts_model_gets_the_examples_too(tmp_path, ledger):
    csv, schema, _src, _s, _rep, _m = _taught(tmp_path, ledger)
    other = tmp_path / "other.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = "Blip count", 3
    wb.save(other)
    model = FakeModel(part="")
    li._extract(str(csv), [str(other)], None, "", False, None, _NoProg(), None, None,
                None, True, model)
    asked = model.label_prompts()[0]
    assert f'"Zorp rate" -> "{_field(schema, "Frequency")}"' in asked
    # Extract's question offers fields only: a role's answer is null, said why
    assert '"Tech Sig" -> null (the user said: Inspector (initials or name))' in asked


def test_extract_uses_the_saved_pattern_when_the_field_is_empty(tmp_path, ledger):
    csv, _schema = ledger
    se.save_taught(csv, se.Taught(patterns=["@@@#@@###"]))
    src = lf.make_laser_log(tmp_path / "laser_101.xlsx")
    s = li.run_extract(csv, [str(src)], tmp_path / "review.xlsx", use_images=False)
    assert s.pattern_saved and s.pattern == "@@@#@@###"
    assert "this Ledger's saved one (the pattern field was empty)" in s.describe()
    meta = json.loads(openpyxl.load_workbook(s.review)["_run"]["A1"].value)
    assert meta["pattern_mask"] == "@@@#@@###"                 # Apply reads it the same way
    typed = li.run_extract(csv, [str(src)], tmp_path / "review2.xlsx", use_images=False,
                           pattern_mask="###")
    assert not typed.pattern_saved and typed.pattern == "###"


def test_inspector_labels_sign_readings_in_extract(tmp_path, ledger):
    """'Tech Sig' answered as an inspector's label: the next Extract reads
    the initials beside it as who logged the readings under it, and never
    maps the label to a field."""
    csv, schema = ledger
    book = tmp_path / "ec.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Part 101"
    ws["A1"], ws["B1"] = "Tech Sig", "MK"
    ws["B3"] = "Flaw"
    ws["B4"] = "EC Pre (%FSH)"
    for i, item in enumerate(lf.ITEMS):
        ws.cell(3, 3 + i, item)
        ws.cell(4, 3 + i, 60 + i)
    wb.save(book)
    before = se.extract(schema, [book], use_images=False)
    assert not any(o.inspector == "MK" for o in before.obs)
    s = lt.guess(csv, book, use_images=False)
    tech = _row(s, "Tech Sig")
    rep = lt.learn(csv, s, {tech.key: lt.ROLE_INSPECTOR})
    assert rep.who_labels == ["Tech Sig"]
    taught = se.load_taught(csv)
    after = se.extract(schema, [book], *se.load_aliases(csv), use_images=False, taught=taught)
    ec = [o for o in after.obs if "Pre-Conceal" in o.field]
    assert len(ec) == 9 and {o.inspector for o in ec} == {"MK"}
    assert not any(o.label == "Tech Sig" for o in after.obs + after.unmatched)
    assert se.Matcher(schema, taught=taught).match("Tech Sig").target is None


def test_a_heading_that_is_no_one_is_not_read_as_initials_again(tmp_path, ledger):
    """'NDT — 2026-09-18' over a table is read as an inspector's heading;
    answered Ignore, NDT is no one's initials from then on."""
    csv, schema = ledger
    book = tmp_path / "ndt.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Part 101"
    ws["A1"] = "NDT — 2026-09-18"
    ws["A2"], ws["B2"] = "Flaw", "EC Pre (%FSH)"
    for i, item in enumerate(lf.ITEMS[:3]):
        ws.cell(3 + i, 1, item)
        ws.cell(3 + i, 2, 60 + i)
    wb.save(book)
    s = lt.guess(csv, book, use_images=False)
    head = next(r for r in s.rows if r.kind == "inspector")
    assert head.shape == "heading" and head.initials == ["NDT"]
    rep = lt.learn(csv, s, {head.key: lt.ROLE_IGNORE})
    assert rep.not_who == ["NDT"] and se.load_taught(csv).not_who == ["NDT"]
    assert not rep.ignored                                     # a value, not a label
    again = lt.guess(csv, book, use_images=False)
    assert not any(r.kind == "inspector" for r in again.rows)
    assert again.inspectors == []
    run = se.extract(schema, [book], use_images=False, taught=se.load_taught(csv))
    assert run.obs and not any(o.inspector for o in run.obs)


def test_a_flaw_code_column_is_learned(tmp_path, ledger):
    csv, schema = ledger
    book = tmp_path / "site.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Part Number", "Loc", "Passes"])
    ws.append(["LMM1AB101", "Spot one", 300])
    wb.save(book)
    s = lt.guess(csv, book, use_images=False)
    loc = _row(s, "Loc")
    rep = lt.learn(csv, s, {loc.key: lt.ROLE_ITEM})
    assert rep.item_labels == ["Loc"]
    taught = se.load_taught(csv)
    assert taught.items == ["Loc"]
    m = se.Matcher(schema, taught=taught)
    assert m.is_item_header("Loc") and m.match("Loc").target is None


def test_an_answer_overrides_a_wrong_rule_match(tmp_path, ledger):
    """The rules matched 'Laser Power (W)' to Power (W); the user says it
    is the Frequency: the alias wins on the next guess."""
    csv, schema = ledger
    src = lf.make_laser_log(tmp_path / "laser_101.xlsx")
    s = lt.guess(csv, src, use_images=False)
    lp = _row(s, "Laser Power (W)")
    assert lp.guess.key == "field:Power (W)" and lp.guess.via == lt.VIA_RULE
    freq = _field(schema, "Frequency")
    rep = lt.learn(csv, s, {lp.key: lt.choice_key(lt.ROLE_FIELD, freq)})
    assert rep.mapped == {"Laser Power (W)": freq}
    again = _row(lt.guess(csv, src, use_images=False), "Laser Power (W)")
    assert (again.guess.key, again.guess.via) == (f"field:{freq}", lt.VIA_ALIAS)


def test_two_answers_for_one_label_keep_the_first(tmp_path, ledger):
    csv, schema = ledger
    book = tmp_path / "two.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "One"
    wb.active["A1"], wb.active["B1"] = "Zorp rate", 1
    two = wb.create_sheet("Two")
    two["A1"], two["B1"] = "Zorp rate", 2
    wb.save(book)
    s = lt.guess(csv, book, use_images=False)
    one, other = [r for r in s.rows if r.label == "Zorp rate"]
    freq = _field(schema, "Frequency")
    rep = lt.learn(csv, s, {one.key: f"field:{freq}", other.key: lt.ROLE_IGNORE})
    assert rep.mapped == {"Zorp rate": freq} and not rep.ignored
    assert rep.conflicts and "answered two ways" in rep.conflicts[0]


def test_unsure_and_unknown_fields_teach_nothing(tmp_path, ledger):
    csv, _schema = ledger
    s = lt.guess(csv, _unlabelled_book(tmp_path / "u.xlsx"), use_images=False)
    zorp = _row(s, "Zorp rate")
    rep = lt.learn(csv, s, {zorp.key: lt.ROLE_UNSURE})
    assert rep.unsure == 1 and not rep.learned
    rep = lt.learn(csv, s, {zorp.key: "field:No Such Field"})
    assert "not a field" in rep.problems[0]
    assert not se.aliases_path_for(csv).exists()


def test_a_damaged_aliases_file_is_not_overwritten(tmp_path, ledger):
    csv, _schema = ledger
    se.aliases_path_for(csv).write_text("{not json", encoding="utf-8")
    session = lt.TeachSession(csv=str(csv), workbook="x.xlsx", rows=[lt.TeachRow(
        key="S|zorp rate", kind="label", label="Zorp rate", where="S!A1", sheet="S")])
    with pytest.raises(LedgerError, match="could not be read"):
        lt.learn(csv, session, {"S|zorp rate": lt.ROLE_IGNORE})
    assert se.aliases_path_for(csv).read_text(encoding="utf-8") == "{not json"
    with pytest.raises(LedgerError):
        se.save_taught(csv, se.Taught(who=["Tech Sig"]))


# ── the knowledge graph ─────────────────────────────────────────────────
def test_the_graphs_part_rules_are_ticked(tmp_path, ledger):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    _csv, _schema, _src, _s, rep, _m = _taught(tmp_path, ledger, vault=vault)
    with kgm.KnowledgeGraph(vault) as kg:
        rules = {r.label: (r, st) for r, st in kg.field_rules()}
    assert rules["Serial"][1] == "confirmed" and rules["Serial"][0].type == "PART"
    assert rules["Part Number"][1] == "confirmed"
    assert any("added 'Serial' as another name for 'Part Number'" in k for k in rep.kg)
    # an existing rule is ticked, not added again
    book = tmp_path / "sn.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "S/N: ABC1DE234"
    wb.save(book)
    csv = _csv
    s = lt.guess(csv, book, use_images=False)
    sn = _row(s, "S/N")
    assert sn.kind == "part" and sn.guess.via == lt.VIA_RULE
    rep2 = lt.learn(csv, s, {sn.key: lt.ROLE_PART}, connections_vault=vault)
    assert rep2.unchanged == 1 and rep2.part_labels == ["S/N"]
    with kgm.KnowledgeGraph(vault) as kg:
        assert dict((r.label, st) for r, st in kg.field_rules())["S/N"] == "confirmed"
    assert any("ticked 'S/N'" in k for k in rep2.kg)


def test_a_field_named_like_a_confirmed_rule_gets_the_label_as_a_synonym(tmp_path):
    """A template field 'Project' (a graph rule the user ticked): a label
    mapped to it is added as that rule's other name."""
    tpl = tmp_path / "tpl.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Attributes"
    ws["A1"].font = Font(size=16, bold=True)
    for col, label in enumerate(["Part Number", "Project", "Power (W)"], start=1):
        ws.cell(2, col, label)
    wb.save(tpl)
    csv = Path(li.create_parent(tpl).csv)
    vault = tmp_path / "vault"
    with kgm.KnowledgeGraph(vault) as kg:
        kg.set_rule_status("Project", "confirmed")
    book = tmp_path / "b.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["B1"] = "Prog. Code", "ORION"
    wb.save(book)
    s = lt.guess(csv, book, use_images=False)
    row = _row(s, "Prog. Code")
    rep = lt.learn(csv, s, {row.key: "field:Project"}, connections_vault=vault)
    assert rep.mapped == {"Prog. Code": "Project"}
    with kgm.KnowledgeGraph(vault) as kg:
        rules = {r.label: (r, st) for r, st in kg.field_rules()}
    assert rules["Prog. Code"] == (rules["Project"][0].renamed("Prog. Code"), "confirmed")


def test_a_busy_graph_is_said_and_the_rest_is_kept(tmp_path, ledger):
    class Busy:
        def open_graph(self):
            raise kgm.KnowledgeGraphBusy("being rebuilt")
    _csv, _schema, _src, _s, rep, _m = _taught(tmp_path, ledger, vault=Busy())
    assert rep.kg and "being rebuilt elsewhere" in rep.kg[0]
    assert rep.mapped and rep.who_labels                        # the answers are kept


def test_the_ledgers_reading_follows_its_inspector_labels(tmp_path):
    """kg_excel.read_ledger: a template field whose label the Ledger's Teach
    learned is an inspector's is a person column, and the fingerprint
    changes so the graph reads the Ledger again."""
    tpl = tmp_path / "tpl.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Attributes"
    ws["A1"].font = Font(size=16, bold=True)
    for col, label in enumerate(["Part Number", "Power (W)", "Tech Sig"], start=1):
        ws.cell(2, col, label)
    wb.save(tpl)
    csv = Path(li.create_parent(tpl).csv)
    ParentCSV(csv).add_samples([{"Part Number": "101", "Power (W)": "280",
                                 "Tech Sig": "JB"}])
    before = kg_excel.read_ledger(csv, "ledger.csv", [])
    fp = kg_excel.ledger_fingerprint(csv)
    assert "Tech Sig" not in before.summary["person_columns"]
    se.save_taught(csv, se.Taught(who=["Tech Sig"]))
    after = kg_excel.read_ledger(csv, "ledger.csv", [])
    assert "Tech Sig" in after.summary["person_columns"]
    assert after.summary["people"] == ["JB"]
    assert kg_excel.ledger_fingerprint(csv) != fp


# ── ledger_llm's examples ───────────────────────────────────────────────
def test_examples_most_like_the_labels_come_first():
    ex = [("Zorp rate", "Frequency (kHz)", ""), ("Power", "Power (W)", ""),
          ("Tech Sig", None, "inspector"), ("Zorp rate", "Date", "")]   # newest wins
    got = ledger_llm.pick_examples(ex, ["Zorp rates", "Blip"], limit=2)
    assert got[0] == ("Zorp rate", "Date", "") and len(got) == 2
    assert ledger_llm.pick_examples(ex, ["Power"], limit=1) == [("Power", "Power (W)", "")]
    text = ledger_llm._examples_text(ex, ["Power (W)"])
    assert '"Power" -> "Power (W)"' in text and '"Tech Sig" -> null (inspector)' in text
    assert "Frequency" not in text                              # not a field offered


class _NoProg:
    def __getattr__(self, _name):
        return lambda *a, **k: None
