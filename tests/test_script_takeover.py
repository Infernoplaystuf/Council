"""
Taking a model's script over: council_core.script_takeover, the typed command
(pipeline_intent.take_over_ref), the Take over buttons in the Qt and Tk
Dream3D tabs, and the invariant that only the user can ask for it.

The user asked: "Model edits of your own script: the edited copy is marked as
model-written and runs under the strict rules. Deleting its first line (the
marker) makes it yours again. OK? Can the user say to delete that line?" They
can now — typed, or with a button; asked first; never by anything a model
wrote. The Qt confirmations here are the real QMessageBox, answered by a
click, offscreen.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import nx_policy  # noqa: E402
import path_contain  # noqa: E402
from council_core import dream3d  # noqa: E402
from council_core import pipeline_intent as pi  # noqa: E402
from council_core import script_takeover as st  # noqa: E402

# The user's own script, CRLF on purpose: everything but the stamp must
# come back byte for byte.
USER_CODE = "import os\r\nprint(os.getcwd())\r\n"


def stamped(code: str = USER_CODE) -> str:
    return nx_policy.stamp_model_script(code, "the pipeline chat",
                                        edited=True)


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "data_out").mkdir(parents=True)
    (v / "data_in").mkdir()
    return v


@pytest.fixture
def dialogs_on(monkeypatch):
    """A person is there to answer: COUNCIL_NO_DIALOGS unset."""
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)


def put(vault: Path, rel: str, text: str, bom: bool = False) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return p


def takeover_dir(vault: Path) -> Path:
    return vault / "data_out" / "dream3d" / "takeover"


def backups(vault: Path):
    d = takeover_dir(vault)
    return sorted(d.glob("*.py")) if d.is_dir() else []


def log_records(vault: Path):
    p = takeover_dir(vault) / st.LOG_NAME
    if not p.is_file():
        return []
    return [json.loads(line) for line in
            p.read_text(encoding="utf-8").splitlines()]


def untouched(vault: Path, path: Path, before: bytes) -> bool:
    return path.read_bytes() == before and not backups(vault) \
        and not log_records(vault)


def YES(title, text):
    return True


def NO(title, text):
    return False


def never(*_a, **_k):
    raise AssertionError("nothing may be asked here")


# ============================================================
# The typed command
# ============================================================

@pytest.mark.parametrize("text, ref", [
    ("take over seg_five.py", "seg_five.py"),
    ("Take over the script seg_five.py.", "seg_five.py"),
    ("take seg_five.py over", "seg_five.py"),
    ("make seg_five.py mine", "seg_five.py"),
    ("make 'seg_five.py' my own!", "seg_five.py"),
    ("remove the model marker from seg_five.py", "seg_five.py"),
    ("delete the model stamp line of pipelines/out/seg_five.py",
     "pipelines/out/seg_five.py"),
    ("delete the first line of seg_five.py", "seg_five.py"),
    ("please take over seg_five", "seg_five"),
    ("take it over", ""),
    ("remove the model stamp", ""),
])
def test_the_phrasings_name_the_script(text, ref):
    assert pi.take_over_ref(text) == ref


@pytest.mark.parametrize("text", [
    "how do I take over a script?",
    "what does the model stamp mean",
    "list pipelines",
    "",
    "hello\ntake over seg_five.py",          # only the first line, as Tk
])
def test_other_text_is_not_a_take_over(text):
    assert pi.take_over_ref(text) is None


def test_the_pipeline_intents_try_a_take_over_first():
    """So "make pipeline seg mine" is never read as "create a pipeline"."""
    actions = [i.action for i in pi.candidates("make pipeline seg mine")]
    assert actions[0] == "take_over"
    assert pi.ACTIONS[0] == "take_over"


# ============================================================
# nx_policy: which lines are the stamp
# ============================================================

def test_the_stamp_is_the_first_line_and_only_it_goes():
    code = stamped()
    assert nx_policy.stamp_lines(code) == [1]
    assert nx_policy.without_stamp(code) == USER_CODE


def test_a_stamp_under_the_users_own_lines_is_found_where_it_is():
    code = "# my notes\n\n" + stamped("x = 1\n")
    assert nx_policy.stamp_lines(code) == [3]
    assert nx_policy.without_stamp(code) == "# my notes\n\nx = 1\n"


def test_every_stamp_goes_even_one_spread_over_lines():
    """script_trust's \\s crosses line breaks, so "#" then "council: model-
    written" on the next line is a stamp too; removing every one leaves
    text it reads as the user's."""
    code = ("#\ncouncil: model-written\nx = 1\n"
            "# council: model-edited (y)\r\nprint(1)\n")
    assert nx_policy.script_trust(code) == nx_policy.MODEL
    assert nx_policy.stamp_lines(code) == [1, 2, 4]
    left = nx_policy.without_stamp(code)
    assert left == "x = 1\nprint(1)\n"
    assert nx_policy.script_trust(left) == nx_policy.USER


def test_text_without_a_stamp_has_no_stamp_lines():
    assert nx_policy.stamp_lines(USER_CODE) == []
    assert nx_policy.without_stamp(USER_CODE) == USER_CODE
    assert nx_policy.stamp_lines("") == []


# ============================================================
# The take-over itself
# ============================================================

def test_yes_removes_only_the_stamp_keeps_a_copy_and_logs(vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped(), bom=True)
    before = p.read_bytes()
    plan = st.decide(vault, "seg_five.py", "take over seg_five.py")
    assert plan.ok and plan.stamp_lines == (1,)
    asked = []
    said = st.run(plan, vault, lambda t, m: asked.append((t, m)) or True,
                  via="a test")
    # Every byte but the stamp line: the BOM, the CRLFs, the code.
    assert p.read_bytes() == b"\xef\xbb\xbf" + USER_CODE.encode()
    assert "Took over pipelines/out/seg_five.py" in said
    assert "removed line 1" in said
    # It now runs as the user's, by the runner's own test.
    text = p.read_text(encoding="utf-8-sig")
    assert nx_policy.script_trust(text, p, [vault / "data_out"]) \
        == nx_policy.USER
    # The confirmation said what changes, plainly, and showed the lines.
    title, body = asked[0]
    assert title == st.TITLE
    assert "Take over pipelines/out/seg_five.py?" in body
    assert "write and delete files anywhere your account can" in body
    assert "the Council stops checking its outputs" in body
    assert "-    1 | # council: model-edited" in body
    assert "     2 | import os" in body
    # A dated copy of the file as it was, in data_out — never next to it.
    [copy] = backups(vault)
    assert copy.read_bytes() == before
    assert copy.name.startswith("seg_five.before-takeover-")
    assert nx_policy.script_trust(copy.read_text(encoding="utf-8-sig"),
                                  copy, [vault / "data_out"]) \
        == nx_policy.MODEL
    assert sorted(x.name for x in p.parent.iterdir()) == ["seg_five.py"]
    # Who and when.
    [rec] = log_records(vault)
    assert rec["via"] == "a test" and rec["removed_lines"] == [1]
    assert rec["who"] and rec["when"][:4].isdigit()
    assert Path(rec["script"]).name == "seg_five.py"
    assert Path(rec["backup"]) == copy
    assert rec["removed"][0].startswith("# council: model-edited")


def test_no_changes_nothing(vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    said = st.run(st.decide(vault, "seg_five.py"), vault, NO, via="t")
    assert "Not taken over" in said
    assert untouched(vault, p, before)


@pytest.mark.parametrize("answer", ["yes", 1, None, object()])
def test_only_an_explicit_yes_is_yes(vault, dialogs_on, answer):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    st.run(st.decide(vault, "seg_five.py"), vault, lambda t, m: answer,
           via="t")
    assert untouched(vault, p, before)


def test_a_confirmation_that_fails_changes_nothing(vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()

    def broken(title, text):
        raise RuntimeError("no display")
    said = st.run(st.decide(vault, "seg_five.py"), vault, broken, via="t")
    assert "could not be shown" in said and "Nothing changed" in said
    assert untouched(vault, p, before)


def test_no_dialogs_means_no_take_over_never_an_auto_yes(vault, monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    said = st.run(st.decide(vault, "seg_five.py"), vault, never, via="t")
    assert "COUNCIL_NO_DIALOGS" in said and "Nothing changed" in said
    assert untouched(vault, p, before)


def test_a_script_without_the_stamp_is_refused_and_says_so(vault,
                                                           dialogs_on):
    p = put(vault, "pipelines/in/mine.py", USER_CODE)
    before = p.read_bytes()
    plan = st.decide(vault, "mine.py", "take over mine.py")
    assert not plan.ok
    assert "carries no model stamp" in plan.refusal
    assert "already runs as yours" in plan.refusal
    assert st.run(plan, vault, never, via="t") == plan.refusal
    assert untouched(vault, p, before)


def test_paths_outside_the_script_folders_are_refused(vault, tmp_path,
                                                      dialogs_on):
    outside = tmp_path / "elsewhere" / "theirs.py"
    outside.parent.mkdir()
    outside.write_text(stamped(), encoding="utf-8")
    data_out = put(vault, "data_out/dream3d/task_x.py", stamped())
    loose = put(vault, "loose.py", stamped())                # vault root
    d3d = put(vault, "pipelines/in/seg.d3dpipeline", '{"pipeline": []}')
    cases = [
        (str(outside), "outside the vault's script folders"),
        ("pipelines/in/../../elsewhere/theirs.py",
         "outside the vault's script folders"),
        (str(loose), "outside the vault's script folders"),
        (str(data_out), "data_out"),
        (str(d3d), "not a Python script"),
    ]
    for ref, why in cases:
        plan = st.decide(vault, ref, f"take over {ref}")
        assert plan is not None and not plan.ok, ref
        assert why in plan.refusal, (ref, plan.refusal)
        assert st.run(plan, vault, never, via="t") == plan.refusal
    for f in (outside, data_out, loose):
        assert nx_policy.script_trust(f.read_text(encoding="utf-8")) \
            == nx_policy.MODEL
    assert not backups(vault) and not log_records(vault)


def test_the_data_out_refusal_says_how_to_run_it_as_your_own(vault):
    p = put(vault, "data_out/dream3d/task_x.py", stamped())
    plan = st.prepare(vault, p)
    assert "copy it into pipelines/in" in plan.refusal


def test_scripts_are_found_by_name_or_path_in_either_folder(vault):
    a = put(vault, "pipelines/in/seg.py", stamped())
    b = put(vault, "pipelines/out/seg_five.py", stamped())
    assert st.decide(vault, "seg_five.py").path == b.resolve()
    assert st.decide(vault, "seg_five").path == b.resolve()
    assert st.decide(vault, "out/seg_five.py").path == b.resolve()
    assert st.decide(vault, "pipelines/in/seg.py").path == a.resolve()
    # "seg" is a's name exactly, and only part of b's.
    assert st.decide(vault, "seg").path == a.resolve()


def test_two_scripts_of_one_name_are_not_guessed_between(vault):
    put(vault, "pipelines/in/seg.py", stamped())
    put(vault, "pipelines/out/seg.py", stamped())
    plan = st.decide(vault, "seg.py", "take over seg.py")
    assert not plan.ok and "More than one script" in plan.refusal
    assert "pipelines/in/seg.py" in plan.refusal
    assert "pipelines/out/seg.py" in plan.refusal


def test_a_phrase_that_names_no_script_is_left_for_the_council(vault):
    assert st.decide(vault, "world", "take over the world") is None
    assert st.decide(vault, "", "take it over") is None
    # ...but one that says it is about a script is answered.
    assert "No script named 'nope.py'" in \
        st.decide(vault, "nope.py", "take over nope.py").refusal
    assert st.decide(vault, "", "take the script over").refusal == st.USAGE
    assert st.decide(vault, "", "remove the model stamp").refusal \
        == st.USAGE


def test_a_file_changed_after_the_question_is_left_alone(vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())

    def edit_then_yes(title, text):
        p.write_text(stamped("x = 2\n"), encoding="utf-8")
        return True
    said = st.run(st.decide(vault, "seg_five.py"), vault, edit_then_yes,
                  via="t")
    assert "changed after you were asked" in said
    assert p.read_text(encoding="utf-8") == stamped("x = 2\n")
    assert not backups(vault) and not log_records(vault)


def test_a_failed_rewrite_leaves_the_script_and_no_temp_file(
        vault, dialogs_on, monkeypatch):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()

    def locked(src, dst):
        raise PermissionError("the file is open in another program")
    monkeypatch.setattr(st.os, "replace", locked)
    said = st.run(st.decide(vault, "seg_five.py"), vault, YES, via="t")
    assert "could not be rewritten" in said and "Nothing changed" in said
    assert p.read_bytes() == before
    assert sorted(x.name for x in p.parent.iterdir()) == ["seg_five.py"]
    [copy] = backups(vault)                  # the copy is kept, not deleted
    assert copy.read_bytes() == before
    assert not log_records(vault)


def test_the_refusal_and_the_modify_warning_say_how_to_take_over(vault):
    """Where the app tells the user a script runs under the model rules, it
    now names the command as well as the hand edit."""
    code = stamped()
    note = nx_policy.model_rules_note(code)
    assert "delete that line" in note and "take over" in note
    import pipeline_editor
    out = pipeline_editor._model_rules_warnings(
        USER_CODE, vault / "pipelines" / "in" / "mine.py", code, vault,
        vault / "pipelines" / "out" / "mine_five.py")
    assert "delete its first line" in out[-1]
    assert "take over mine_five.py" in out[-1]


def test_the_confirmation_shows_a_models_text_safely(vault):
    """The first lines are a model's text: a bidi override (which can make
    a line read as something else) is spelled out, a long line is cut, and
    a stamp below the first lines is shown too."""
    lines = [f"# note {n}" for n in range(1, 19)]
    lines[2] = "x = 1  # ‮evil"
    lines[3] = "y = '" + "a" * 300 + "'"
    code = "\n".join(lines) + "\n" + stamped("z = 3\n")
    put(vault, "pipelines/in/long.py", code)
    plan = st.decide(vault, "long.py")
    assert plan.stamp_lines == (19,)
    body = st.confirmation(plan)
    assert "‮" not in body and "\\u202e" in body
    rows = [line for line in body.splitlines() if " | " in line]
    assert rows and max(len(row) for row in rows) < 130
    assert "line 19 (the model stamp) is removed" in body
    assert "-   19 | # council: model-edited" in body


def test_the_confirmation_says_all_that_a_users_script_may_do():
    """A taken-over script runs under the USER rules, which hold only two
    filters — not processes, the network or native code. The confirmation
    says so, and does not say arbitrary code stays blocked."""
    for words in ("write and delete files anywhere your account can",
                  "run any program", "use the network", "load native code",
                  "the Council stops checking its outputs",
                  "The Council still refuses only two DREAM3D-NX filters "
                  "(Execute Process, Create Python Plugin)."):
        assert words in st.WHAT_CHANGES, words
    assert "arbitrary code" not in st.WHAT_CHANGES
    # ...because that is what the USER rules are.
    code = ("import ctypes, shutil, socket, subprocess\n"
            "subprocess.run(['tool'])\n"
            "socket.create_connection(('host', 1))\n"
            "ctypes.CDLL('native.dll')\n"
            "shutil.rmtree('anywhere')\n")
    assert nx_policy.run_reasons(code, trust=nx_policy.USER) == []
    assert nx_policy.run_reasons(code, trust=nx_policy.MODEL)


# ============================================================
# Only the stamp goes — its lines as Python and an editor read them
# ============================================================

def defines(data: bytes) -> dict:
    """What running ``data`` defines. compile() reads a bare CR as a line
    break, as an editor and the workflow runner (read_text) do."""
    ns: dict = {}
    exec(compile(data.decode("utf-8-sig"), "<script>", "exec"), ns)
    return {k: v for k, v in ns.items()
            if not k.startswith("__") and not hasattr(v, "__spec__")}


def rows(plan) -> list:
    return [line for line in st.confirmation(plan).splitlines()
            if " | " in line]


def test_a_bare_cr_ends_a_stamp_line_as_it_ends_a_python_line():
    code = "x = 1\r# council: model-edited\ry = 2\r"
    assert nx_policy.script_trust(code) == nx_policy.MODEL
    assert nx_policy.stamp_lines(code) == [2]
    assert nx_policy.without_stamp(code) == "x = 1\ry = 2\r"
    assert nx_policy.script_trust(nx_policy.without_stamp(code)) \
        == nx_policy.USER
    # A model's own stamp after a bare CR is a stamp: the app adds none,
    # and the runner gives the MODEL rules either way.
    assert nx_policy.stamp_model_script(code, "t") == code


def test_code_after_a_bare_cr_on_the_stamp_line_stays(vault, dialogs_on):
    """A model's stamp line can carry code after a bare CR. Python and an
    editor read that code as a line of its own, so it stays: only the stamp
    goes, and the script does what it did — the dialog's "nothing else" is
    true."""
    code = nx_policy.stamp_model_script(
        "DRY_RUN = False\n# council: model-edited\rDRY_RUN = True\n"
        "ACTION = 'would delete' if DRY_RUN else 'DELETING'\n",
        "the pipeline chat", edited=True)
    assert code.startswith("DRY_RUN = False")     # the model's stamp, alone
    p = put(vault, "pipelines/in/dry.py", code)
    before = p.read_bytes()
    plan = st.prepare(vault, p)
    assert plan.ok and plan.stamp_lines == (2,)
    assert rows(plan)[1:3] == ["-    2 | # council: model-edited",
                               "     3 | DRY_RUN = True"]
    assert "line 2 (the model stamp) is removed" in st.confirmation(plan)
    said = st.run(plan, vault, YES, via="t")
    assert "Took over" in said and "removed line 2" in said
    assert p.read_bytes() == (
        b"DRY_RUN = False\nDRY_RUN = True\n"
        b"ACTION = 'would delete' if DRY_RUN else 'DELETING'\n")
    assert defines(before)["ACTION"] == defines(p.read_bytes())["ACTION"] \
        == "would delete"


def test_a_script_of_bare_crs_loses_only_its_stamp_line(vault, dialogs_on):
    p = put(vault, "pipelines/in/cr.py",
            "# council: model-written\rimport math\rA = 1\rB = 2\r")
    plan = st.prepare(vault, p)
    assert plan.ok and plan.stamp_lines == (1,)
    assert rows(plan) == ["-    1 | # council: model-written",
                          "     2 | import math", "     3 | A = 1",
                          "     4 | B = 2"]
    st.run(plan, vault, YES, via="t")
    assert p.read_bytes() == b"import math\rA = 1\rB = 2\r"
    assert defines(p.read_bytes()) == {"A": 1, "B": 2}
    [rec] = log_records(vault)
    assert rec["removed"] == ["# council: model-written"]


@pytest.mark.parametrize("data", [
    b"A = 1\r# council: model-written\n\nB = 2\n",
    b"A = 1\r# council: model-written\r\n# council: model-edited\n\nB = 2\n",
])
def test_a_take_out_that_would_join_two_line_breaks_is_left_for_the_hand(
        vault, dialogs_on, data):
    """A bare CR ending the line above the stamp and a line that is only a
    line feed below it would meet as one CRLF: a line the dialog does not
    mark would go too, so "nothing else" would not be true. Found by a
    fuzz of mixed line endings; the code is the same, the lines are not."""
    p = vault / "pipelines" / "in" / "j.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    plan = st.prepare(vault, p)
    assert not plan.ok
    assert "join" in plan.refusal and "by hand" in plan.refusal, plan.refusal
    assert "Nothing changed" in plan.refusal
    assert st.run(plan, vault, YES, via="t") == plan.refusal
    assert p.read_bytes() == data and not backups(vault)


def test_a_line_separator_in_the_stamp_line_is_shown_spelled_out(
        vault, dialogs_on):
    """U+2028 is no line break to Python — the rest of that line is still
    the comment — but a dialog may draw one: it is spelled out."""
    p = put(vault, "pipelines/in/ls.py",
            "# council: model-written DRY_RUN = True\nx = 1\n")
    before = p.read_bytes()
    plan = st.prepare(vault, p)
    assert plan.ok and plan.stamp_lines == (1,)
    assert rows(plan)[0] == \
        "-    1 | # council: model-written\\u2028DRY_RUN = True"
    st.run(plan, vault, YES, via="t")
    assert p.read_bytes() == b"x = 1\n"
    assert defines(before) == defines(p.read_bytes()) == {"x": 1}


def test_the_take_over_reads_the_stamp_as_the_runner_does(vault,
                                                          dialogs_on):
    """The runner reads a script with universal newlines; the take-over
    reads it the same way, so "it now runs as yours" is true and a stamp
    the runner sees is one it can take out."""
    import workflow_runner
    q = put(vault, "pipelines/in/q.py",
            "A = 1  \r# council: model-written\nB = 2\n")
    assert workflow_runner._trust(q, vault) == nx_policy.MODEL
    assert st.is_stamped(q)
    plan = st.prepare(vault, q)
    assert plan.ok and plan.stamp_lines == (2,)
    assert "now runs as yours" in st.run(plan, vault, YES, via="t")
    assert q.read_bytes() == b"A = 1  \rB = 2\n"
    assert workflow_runner._trust(q, vault) == nx_policy.USER

    r = put(vault, "pipelines/in/r.py",
            "# council: model-written (x).\nA = 1  \r"
            "# council: model-written\nB = 2\n")
    plan = st.prepare(vault, r)
    assert plan.ok and plan.stamp_lines == (1, 3)
    assert "now runs as yours" in st.run(plan, vault, YES, via="t")
    assert r.read_bytes() == b"A = 1  \rB = 2\n"
    assert workflow_runner._trust(r, vault) == nx_policy.USER


@pytest.mark.parametrize("data, why", [
    # The stamp text inside a string: taking it out changes the string.
    (b"# council: model-written (x).\nMSG = '''\n# council: model-edited\n"
     b"'''\n", "line 3 is inside a string"),
    # 'council: model-written' is an annotated name — a statement.
    (b"#\ncouncil: model-written\nx = 1\n", "line 2 is part of the code"),
    # The comment ends the line a backslash continued: without it, the
    # next line joins the statement (DRY = True and False).
    (b"DRY = True and \\\n# council: model-written\nFalse\n",
     "line 2 is part of the code"),
])
def test_a_stamp_that_is_not_only_a_comment_is_left_for_the_hand(
        vault, dialogs_on, data, why):
    p = vault / "pipelines" / "in" / "s.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    plan = st.decide(vault, "s.py", "take over s.py")
    assert not plan.ok
    assert why in plan.refusal, plan.refusal
    assert "would change what the script does" in plan.refusal
    assert "by hand" in plan.refusal and "Nothing changed" in plan.refusal
    assert st.run(plan, vault, never, via="t") == plan.refusal
    assert untouched(vault, p, data)


def test_a_coding_line_moving_up_is_left_for_the_hand(vault, dialogs_on):
    """Python honours a coding line only in a file's first two lines: taking
    line 1 out would move one there and change what 'é' is."""
    data = ("# council: model-written (x).\n#\n# -*- coding: latin-1 -*-\n"
            "s = 'é'\n").encode("utf-8")
    p = vault / "pipelines" / "in" / "enc.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    plan = st.prepare(vault, p)
    assert not plan.ok and "coding line" in plan.refusal, plan.refusal
    assert untouched(vault, p, data)


def test_the_coding_line_is_read_where_python_reads_it(vault, dialogs_on):
    """In a file of bare CRs Python's line 2 is no coding line (it starts
    with code); read with "\\n" as the only break, the whole file is line 1
    and its "coding: latin-1" would look like one. Nothing about the coding
    changes, so the take-over goes ahead."""
    data = b"# council: model-written\rx = 1  # coding: latin-1\r"
    p = vault / "pipelines" / "in" / "crc.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    plan = st.prepare(vault, p)
    assert plan.ok, plan.refusal
    st.run(plan, vault, YES, via="t")
    assert p.read_bytes() == b"x = 1  # coding: latin-1\r"
    assert defines(data) == defines(p.read_bytes()) == {"x": 1}


def test_a_stamp_that_carries_the_coding_line_is_left_for_the_hand(
        vault, dialogs_on):
    """Python reads the file fine now; without its first line (stamp and
    coding line both) it could not — the refusal says that, not that the
    file is unreadable as it stands."""
    data = (b"# council: model-written -*- coding: latin-1 -*-\n"
            b"s = '\xe9'\n")
    p = vault / "pipelines" / "in" / "enc1.py"
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    plan = st.prepare(vault, p)
    assert not plan.ok
    assert "would leave a file Python cannot read" in plan.refusal, \
        plan.refusal
    assert "as it stands" not in plan.refusal
    assert "by hand" in plan.refusal and "Nothing changed" in plan.refusal
    assert untouched(vault, p, data)


def test_a_script_python_cannot_read_is_left_for_the_hand(vault,
                                                         dialogs_on):
    p = put(vault, "pipelines/in/open.py", stamped("x = (1,\n"))
    before = p.read_bytes()
    plan = st.prepare(vault, p)
    assert not plan.ok
    assert "Python cannot read it as it stands" in plan.refusal
    assert "delete line 1 by hand" in plan.refusal
    assert untouched(vault, p, before)


# ============================================================
# Files that cannot simply be rewritten
# ============================================================

def make_read_only(p: Path):
    os.chmod(p, stat.S_IREAD)


def make_writable(folder: Path):
    for x in [folder, *folder.rglob("*")]:
        try:
            os.chmod(x, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass


def test_a_read_only_script_is_refused_up_front(vault, dialogs_on):
    p = put(vault, "pipelines/in/ro.py", stamped())
    before = p.read_bytes()
    make_read_only(p)
    try:
        plan = st.prepare(vault, p)
        assert not plan.ok and "read-only" in plan.refusal
        assert "Nothing changed" in plan.refusal
        assert st.run(plan, vault, never, via="t") == plan.refusal
        assert untouched(vault, p, before)
        assert sorted(x.name for x in p.parent.iterdir()) == ["ro.py"]
    finally:
        make_writable(p.parent)


def test_a_script_made_read_only_after_the_question_is_left(vault,
                                                            dialogs_on):
    p = put(vault, "pipelines/in/ro.py", stamped())
    before = p.read_bytes()

    def read_only_then_yes(title, text):
        make_read_only(p)
        return True
    try:
        said = st.run(st.prepare(vault, p), vault, read_only_then_yes,
                      via="t")
        assert "read-only" in said and "Nothing changed" in said
        assert untouched(vault, p, before)
        assert sorted(x.name for x in p.parent.iterdir()) == ["ro.py"]
    finally:
        make_writable(p.parent)


@pytest.mark.skipif(os.name != "nt",
                    reason="only Windows refuses to replace a read-only file")
def test_a_failed_replace_of_a_read_only_file_leaves_no_temp_file(tmp_path):
    """The temp file takes the script's mode, read-only included; a failed
    replace must still take it away."""
    p = tmp_path / "ro.py"
    p.write_bytes(b"A = 1\n")
    make_read_only(p)
    try:
        with pytest.raises(OSError):
            st._replace(p, b"B = 2\n")
        assert sorted(x.name for x in tmp_path.iterdir()) == ["ro.py"]
        assert p.read_bytes() == b"A = 1\n"
    finally:
        make_writable(tmp_path)


def path_of_length(root: Path, length: int, name_end: str = ".py") -> Path:
    """A path under ``root`` whose real path is exactly ``length``
    characters: folders of d's, then a name of s's ending in ``name_end``."""
    base = os.path.realpath(root)
    need = length - len(base)          # each part: a separator and a name
    parts = []
    while need > 60:
        parts.append("d" * 40)
        need -= 41
    parts.append("s" * (need - 1 - len(name_end)) + name_end)
    p = Path(base, *parts)
    assert len(str(p)) == length
    return p


@pytest.mark.parametrize("length", [240, 250, 259])
def test_a_script_near_max_path_can_be_taken_over(vault, dialogs_on, length):
    """Its temp file must be makeable beside it, whatever the length (an
    8.3 short-name vault's long form is long)."""
    pin = vault / "pipelines" / "in"
    pin.mkdir(parents=True)
    p = path_of_length(pin, length)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(stamped().encode())
    said = st.run(st.prepare(vault, p), vault, YES, via="t")
    assert "Took over" in said, said
    assert p.read_bytes() == USER_CODE.encode()
    assert [x.name for x in p.parent.iterdir()] == [p.name]


def log_records_at(vault: Path):
    p = path_contain.resolved(takeover_dir(vault) / st.LOG_NAME)[1]
    with open(p, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines()]


def test_a_deep_vault_keeps_its_copy_and_log(tmp_path, dialogs_on):
    """A vault deep enough that the dated copy's path passes MAX_PATH while
    the script's does not."""
    v = path_of_length(tmp_path, 205, name_end="v")
    (v / "data_out").mkdir(parents=True)
    p = put(v, "pipelines/in/x.py", stamped())
    before = p.read_bytes()
    said = st.run(st.prepare(v, p), v, YES, via="t")
    assert "Took over" in said, said
    assert "could not be written" not in said
    assert p.read_bytes() == USER_CODE.encode()
    [copy] = backups(v)
    assert len(os.path.realpath(copy)) > 259
    with open(path_contain.resolved(copy)[1], "rb") as fh:
        assert fh.read() == before
    assert log_records_at(v)[0]["removed_lines"] == [1]
    # Named from the vault, as the script is — not by a 260-character
    # spelling of the whole path (realpath keeps \\?\ on a long one).
    assert f"is kept at data_out/{st.SUBFOLDER}/{copy.name}." in said, said


@pytest.mark.skipif(os.name != "nt", reason="UNC paths are Windows paths")
@pytest.mark.parametrize("ref", [
    "\\\\tko-nohost-qq7\\share\\x.py",
    "//tko-nohost-qq7/share/x.py",
    "\\\\?\\UNC\\tko-nohost-qq7\\share\\x.py",
])
def test_a_network_path_is_refused_from_its_spelling_alone(vault, ref,
                                                           monkeypatch):
    """Resolving \\\\host\\share asks the network for the host (seconds, and
    a connection attempt to a host that answers) before it can say no — on
    the GUI thread. The refusal comes from the spelling."""
    asked = []
    real = os.path.realpath

    def no_network(p, *a, **k):
        if path_contain.network_or_device(p):
            asked.append(os.fspath(p))
            raise OSError("the network was asked")
        return real(p, *a, **k)
    monkeypatch.setattr(os.path, "realpath", no_network)
    typed = f"take over {ref}"
    assert pi.take_over_ref(typed) == ref
    plan = st.decide(vault, ref, typed)
    assert not plan.ok
    assert "not a script in the vault's script folders" in plan.refusal
    assert "x.py" in plan.refusal
    # Every route holding text asks decide() too: the pipeline chat.
    said = []
    job = chat_for(vault, said).plan(typed)
    job()
    assert said == [("Council", st.NOT_HERE, "observation")]
    assert asked == []


# ============================================================
# The pipeline chat — every route that is not a door
# ============================================================

def chat_for(vault, said, **kw):
    return dream3d.PipelineChat(vault, say=lambda w, t, k: said.append(
        (w, t, k)), **kw)


def test_the_pipeline_chat_answers_a_take_over_and_changes_nothing(vault):
    """plan() is reachable by anything holding text; it never acts."""
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    said = []
    job = chat_for(vault, said).plan("take over seg_five.py")
    assert job is not None
    job()
    assert said == [("Council", st.NOT_HERE, "observation")]
    assert untouched(vault, p, before)
    # A phrase about no script is the Council's, as at the typed door.
    assert chat_for(vault, said).plan("take over the world") is None


def test_model_output_and_file_text_never_take_over(vault, dialogs_on,
                                                    monkeypatch):
    """A model "modify" result, a script's own text and a file's name are
    shown in the transcript — and nothing reads the transcript."""
    monkeypatch.setattr(st, "apply", never)
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    put(vault, "pipelines/in/take over seg_five.py",
        "# take over seg_five.py\nimport simplnx as nx\n")
    said = []

    class Editor:
        def modify_pipeline_by_request(self, path, req, v):
            return SimpleNamespace(
                success=True, error="", new_path=p,
                log=["take over seg_five.py"],
                warnings=["take over seg_five.py",
                          "remove the model marker from seg_five.py"])
    chat = chat_for(vault, said, editor=Editor())
    for text in ("list pipelines", "show pipeline take over",
                 "modify pipeline take over to use 4 threads"):
        job = chat.plan(text)
        assert job is not None, text
        job()
    shown = "\n".join(t for _w, t, _k in said)
    assert "take over seg_five.py" in shown        # it was shown...
    assert untouched(vault, p, before)              # ...and nothing acted


# ============================================================
# Qt: the typed door, the button, the real confirmation
# ============================================================

pytest.importorskip("PySide6", reason="the Qt doors need PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from council_qt import dialogs  # noqa: E402
from council_qt.tabs.council import CouncilActions, CouncilTab  # noqa: E402
from council_qt.tabs.dream3d import DreamActions, Dream3DTab  # noqa: E402

_Btn = QMessageBox.StandardButton


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


def answer(button, seen, seconds=5.0):
    """Click ``button`` on the next message box that opens — the real one,
    modal, offscreen — and record what it showed."""
    deadline = time.time() + seconds

    def poke():
        box = next((w for w in QApplication.topLevelWidgets()
                    if isinstance(w, QMessageBox) and w.isVisible()), None)
        if box is not None:
            seen.append(SimpleNamespace(
                title=box.windowTitle(), text=box.text(),
                plain=box.textFormat() == Qt.TextFormat.PlainText,
                default_no=box.defaultButton() is box.button(_Btn.No)))
            box.button(button).click()
        elif time.time() < deadline:
            QTimer.singleShot(10, poke)
    QTimer.singleShot(0, poke)


def no_box(monkeypatch):
    """Fail if any message box is built."""
    monkeypatch.setattr(dialogs, "_box", never)


class NoTurn(CouncilActions):
    def __init__(self, vault_dir):
        super().__init__(vault_dir=vault_dir)
        self.sent = []

    def send(self, typed, *a, **k):
        self.sent.append(typed)
        return None


@pytest.fixture
def council(qapp, vault):
    tab = CouncilTab(actions=NoTurn(vault))
    tab.changed = []
    tab.pipelines_changed.append(lambda: tab.changed.append(1))
    yield tab
    deadline = time.time() + 5
    while tab._turn_active and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    tab.deleteLater()
    qapp.processEvents()


def transcript(tab) -> str:
    return tab.transcript.toPlainText()


def test_typed_take_over_asks_and_acts_on_a_click_of_yes(qapp, council,
                                                         vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    seen = []
    answer(_Btn.Yes, seen)
    council.input.setPlainText("take over seg_five.py")
    council.on_send()
    [box] = seen
    assert box.title == st.TITLE and box.plain and box.default_no
    assert "Take over pipelines/out/seg_five.py?" in box.text
    assert "write and delete files anywhere your account can" in box.text
    assert "     2 | import os" in box.text
    assert p.read_bytes() == USER_CODE.encode()
    assert "Took over pipelines/out/seg_five.py" in transcript(council)
    assert council.input.toPlainText() == ""
    assert council.changed == [1]
    assert council.actions.sent == []           # never a Council turn
    [rec] = log_records(vault)
    assert rec["via"].startswith("typed in the chat")


def test_typed_take_over_no_changes_nothing(qapp, council, vault,
                                            dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    seen = []
    answer(_Btn.No, seen)
    council.input.setPlainText("take over seg_five.py")
    council.on_send()
    assert len(seen) == 1
    assert untouched(vault, p, before)
    assert "Not taken over" in transcript(council)


def test_typed_take_over_under_no_dialogs_builds_no_box(qapp, council,
                                                        vault, monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    no_box(monkeypatch)
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    council.input.setPlainText("take over seg_five.py")
    council.on_send()
    assert untouched(vault, p, before)
    assert "COUNCIL_NO_DIALOGS" in transcript(council)


def test_confirm_under_no_dialogs_is_no(monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    no_box(monkeypatch)
    assert dialogs.confirm("t", "m") is False


def test_typed_unstamped_and_outside_are_refused_without_asking(
        qapp, council, vault, tmp_path, dialogs_on, monkeypatch):
    no_box(monkeypatch)
    mine = put(vault, "pipelines/in/mine.py", USER_CODE)
    outside = put(tmp_path, "theirs.py", stamped())
    before = mine.read_bytes(), outside.read_bytes()
    council.input.setPlainText("take over mine.py")
    council.on_send()
    council.input.setPlainText(f"take over {outside}")
    council.on_send()
    text = transcript(council)
    assert "carries no model stamp" in text
    assert "outside the vault's script folders" in text
    assert (mine.read_bytes(), outside.read_bytes()) == before
    assert not backups(vault) and not log_records(vault)
    assert council.actions.sent == []


def test_a_typed_phrase_naming_no_script_goes_to_the_council(qapp, council,
                                                             dialogs_on,
                                                             monkeypatch):
    no_box(monkeypatch)
    council.input.setPlainText("take over the world")
    council.on_send()
    pump(qapp, lambda: council.actions.sent == ["take over the world"])


def test_a_models_reply_never_takes_over(qapp, council, vault, dialogs_on,
                                        monkeypatch):
    """A model reply is appended to the transcript — as an event, as the
    turn's answer, as a notice — and nothing reads the transcript."""
    no_box(monkeypatch)
    council.confirm = never
    monkeypatch.setattr(st, "apply", never)
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    reply = "take over seg_five.py"
    council.on_event(SimpleNamespace(kind="final", who="Writer", text=reply))
    council.finish_turn(SimpleNamespace(
        ok=True, message="", answer=f"{reply}\nremove the model marker from "
                                    f"seg_five.py", critique="",
        route="direct", verdict_id=None))
    council.receive_notice("Writer", reply, source="IDE")
    council.append("Writer", reply)
    qapp.processEvents()
    assert reply in transcript(council)
    assert untouched(vault, p, before)


def test_expand_with_council_is_not_a_door(qapp, council, vault, dialogs_on,
                                           monkeypatch):
    """Expand re-sends an earlier question through on_send; it is answered
    by the pipeline chat (NOT_HERE), never asked about or acted on."""
    no_box(monkeypatch)
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    council._last_fast_question = "take over seg_five.py"
    council.on_expand_with_council()
    pump(qapp, lambda: st.NOT_HERE in transcript(council))
    assert untouched(vault, p, before)
    assert council.actions.sent == []


# -- the Dream3D tab --------------------------------------------------------

@pytest.fixture
def d3d(qapp, vault):
    made = []

    def make(**kw):
        tab = Dream3DTab(actions=DreamActions(vault), **kw)
        tab.refresh()
        pump(qapp, lambda: not tab._busy)
        made.append(tab)
        return tab

    yield make
    for tab in made:
        pump(qapp, lambda t=tab: not t._busy)
        tab.deleteLater()
    qapp.processEvents()


def row_of(tab, name):
    return next(i for i, pl in enumerate(tab._pipelines) if pl.name == name)


def test_the_list_marks_model_scripts_and_the_button_follows(qapp, d3d,
                                                             vault):
    put(vault, "pipelines/in/mine.py", USER_CODE)
    put(vault, "pipelines/in/theirs.py", stamped())
    tab = d3d()
    mine, theirs = row_of(tab, "mine.py"), row_of(tab, "theirs.py")
    assert tab.pipelines.item(theirs).text().endswith(dream3d.MODEL_MARK)
    assert not tab.pipelines.item(mine).text().endswith(dream3d.MODEL_MARK)
    assert not tab.take_over_btn.isEnabled()
    tab.pipelines.setCurrentRow(theirs)
    assert tab.take_over_btn.isEnabled()
    tab.pipelines.setCurrentRow(mine)
    assert not tab.take_over_btn.isEnabled()


def test_the_button_asks_and_acts_on_a_click_of_yes(qapp, d3d, vault,
                                                    dialogs_on):
    p = put(vault, "pipelines/in/theirs.py", stamped())
    tab = d3d()
    tab.pipelines.setCurrentRow(row_of(tab, "theirs.py"))
    seen = []
    answer(_Btn.Yes, seen)
    tab.on_take_over()
    [box] = seen
    assert box.plain and box.default_no
    assert "Take over pipelines/in/theirs.py?" in box.text
    assert p.read_bytes() == USER_CODE.encode()
    assert "Took over pipelines/in/theirs.py" in tab.view.toPlainText()
    pump(qapp, lambda: not tab._busy)
    assert not tab.pipelines.item(row_of(tab, "theirs.py")).text() \
        .endswith(dream3d.MODEL_MARK)
    [rec] = log_records(vault)
    assert rec["via"] == "the Take over button (Dream3D tab)"


def test_the_button_no_changes_nothing(qapp, d3d, vault, dialogs_on):
    p = put(vault, "pipelines/in/theirs.py", stamped())
    before = p.read_bytes()
    tab = d3d()
    tab.pipelines.setCurrentRow(row_of(tab, "theirs.py"))
    seen = []
    answer(_Btn.No, seen)
    tab.on_take_over()
    assert len(seen) == 1
    assert untouched(vault, p, before)


def test_the_button_under_no_dialogs_builds_no_box(qapp, d3d, vault,
                                                   monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    no_box(monkeypatch)
    p = put(vault, "pipelines/in/theirs.py", stamped())
    before = p.read_bytes()
    tab = d3d()
    tab.pipelines.setCurrentRow(row_of(tab, "theirs.py"))
    tab.on_take_over()
    assert untouched(vault, p, before)
    assert "COUNCIL_NO_DIALOGS" in tab.view.toPlainText()


def test_the_dream3d_chat_without_a_council_takes_typed_text(qapp, d3d,
                                                             vault,
                                                             dialogs_on):
    p = put(vault, "pipelines/in/theirs.py", stamped())
    tab = d3d()
    seen = []
    answer(_Btn.Yes, seen)
    tab.input.setPlainText("take over theirs.py")
    tab.on_send()
    assert len(seen) == 1
    assert p.read_bytes() == USER_CODE.encode()
    assert "Took over pipelines/in/theirs.py" in tab.transcript.toPlainText()


# ============================================================
# Tk: the same doors, and every other route into _send
# ============================================================

class FakeText:
    def __init__(self, text=""):
        self.text = text

    def get(self, *_a):
        return self.text


@pytest.fixture
def tk(vault, monkeypatch):
    """The real Tk methods on a stand-in console; tkinter's askyesno
    answers ``env.answer`` and records what it was asked."""
    import tkinter.messagebox
    import council_gui_engine as cge
    monkeypatch.setattr(cge, "VAULT_DIR", vault)
    env = SimpleNamespace(answer=True, asked=[])

    def askyesno(title, text, **kw):
        env.asked.append((title, text, kw))
        return env.answer
    monkeypatch.setattr(tkinter.messagebox, "askyesno", askyesno)
    C = cge.CouncilConsole

    class Console:
        _send_typed = C._send_typed
        _take_over_typed = C._take_over_typed
        _take_over_confirm = C._take_over_confirm
        _handle_pipeline_intent = C._handle_pipeline_intent
        _dream3d_take_over_selected = C._dream3d_take_over_selected
        _dream3d_send_from_input = C._dream3d_send_from_input
        _stt_send_to_council = C._stt_send_to_council

        def __init__(self, text=""):
            self.input = FakeText(text)
            self.dream3d_input = FakeText()
            self.stt_out = FakeText()
            self.nb = SimpleNamespace(select=lambda *_a: None)
            self.tab_council = None
            self.said, self.sent, self.typed, self.view = [], [], [], ""
            self.selection = None

        def _set_text(self, widget, text):
            widget.text = text

        def _append_transcript(self, who, text, kind="final"):
            self.said.append((who, text, kind))

        def _send(self):
            self.sent.append(self.input.text)

        def _set_status(self, *_a):
            pass

        def _dream3d_refresh_pipelines(self):
            pass

        def _dream3d_set_view(self, text):
            self.view = text

        def _nx_selected_pipeline(self):
            return self.selection

    env.Console = Console
    return env


def said_text(console) -> str:
    return "\n".join(t for _w, t, _k in console.said)


def test_tk_typed_take_over_asks_and_acts_on_yes(tk, vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    c = tk.Console("take over seg_five.py")
    c._send_typed()
    [(title, text, kw)] = tk.asked
    assert title == st.TITLE and kw["default"] == "no"
    assert "write and delete files anywhere your account can" in text
    assert p.read_bytes() == USER_CODE.encode()
    assert "Took over pipelines/out/seg_five.py" in said_text(c)
    assert c.sent == []                          # never a deliberation
    assert c.input.text == ""


def test_tk_typed_take_over_no_changes_nothing(tk, vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    tk.answer = False
    c = tk.Console("take over seg_five.py")
    c._send_typed()
    assert len(tk.asked) == 1
    assert untouched(vault, p, before)


def test_tk_under_no_dialogs_never_asks(tk, vault, monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    c = tk.Console("take over seg_five.py")
    c._send_typed()
    assert tk.asked == []
    assert untouched(vault, p, before)
    assert "COUNCIL_NO_DIALOGS" in said_text(c)


def test_tk_other_text_goes_on_to_send(tk, dialogs_on):
    c = tk.Console("take over the world")
    c._send_typed()
    assert c.sent == ["take over the world"] and tk.asked == []


def test_tk_every_other_route_answers_without_acting(tk, vault, dialogs_on):
    """_send's own intent handler — behind the history re-ask, the disagree
    re-run, the CPU retry, Expand and a speech transcription — answers the
    words and acts on nothing."""
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    before = p.read_bytes()
    c = tk.Console()
    assert c._handle_pipeline_intent("take over seg_five.py") is True
    assert c.said == [("Council", st.NOT_HERE, "observation")]
    assert tk.asked == []
    assert untouched(vault, p, before)


def test_tk_a_transcription_is_not_typed_text(tk, dialogs_on):
    """Speech-to-text is a model's output: it goes to _send, not the door."""
    c = tk.Console()
    c._send_typed = never
    c.stt_out.text = "take over seg_five.py"
    c._stt_send_to_council()
    assert c.sent == ["take over seg_five.py"]


def test_tk_the_dream3d_box_is_typed_text(tk, vault, dialogs_on):
    p = put(vault, "pipelines/out/seg_five.py", stamped())
    c = tk.Console()
    c.dream3d_input.text = "take over seg_five.py"
    c._dream3d_send_from_input()
    assert len(tk.asked) == 1 and p.read_bytes() == USER_CODE.encode()


def test_tk_the_button(tk, vault, dialogs_on):
    p = put(vault, "pipelines/in/theirs.py", stamped())
    c = tk.Console()
    c._dream3d_take_over_selected()
    assert "Select a model-written script" in c.view and tk.asked == []
    c.selection = SimpleNamespace(name="theirs.py", path=str(p))
    c._dream3d_take_over_selected()
    assert len(tk.asked) == 1 and p.read_bytes() == USER_CODE.encode()
    assert "Took over pipelines/in/theirs.py" in c.view
    [rec] = log_records(vault)
    assert rec["via"] == "the Take over button (Tk Dream3D tab)"


# ============================================================
# The invariant, read from the source
# ============================================================

def _sources():
    """(repo-relative path, AST) of every app source that mentions a
    take-over. Not the tests; not other worktrees under .claude/."""
    skip = {"tests", "Version_History", "docs", "__pycache__", "build",
            "dist", "node_modules", "venv", "env"}
    for top, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(top) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "take_over" in text or "script_takeover" in text:
                yield path.relative_to(ROOT).as_posix(), ast.parse(text)


def _uses(tree, wanted):
    """(enclosing def path, name) for every use of a name in ``wanted`` —
    as an attribute (x.name) or a bare name."""
    found = []

    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            here = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                here = f"{where}.{child.name}" if where else child.name
            if isinstance(child, ast.Attribute) and child.attr in wanted:
                found.append((here, child.attr))
            elif isinstance(child, ast.Name) and child.id in wanted:
                found.append((here, child.id))
            walk(child, here)
    walk(tree, "")
    return found


def _calls(tree, module_names, funcs):
    """Enclosing defs of every <module alias>.<func>(...) call."""
    found = []

    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            here = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                here = f"{where}.{child.name}" if where else child.name
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in funcs
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id in module_names):
                found.append(here)
            walk(child, here)
    walk(tree, "")
    return found


def _aliases(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "script_takeover":
                    names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.endswith("script_takeover"):
                    names.add(a.asname or a.name)
    return names


def test_only_the_doors_can_take_a_script_over():
    """Every call that can change a script — script_takeover.run / apply —
    is in a door: a chat box's typed-text handler or a Take over button.
    And each door is reached only from the user's own send or click."""
    acts = set()
    for rel, tree in _sources():
        for where in _calls(tree, _aliases(tree), {"run", "apply"}):
            acts.add((rel, where))
        # ...and nothing reaches them by importing them bare.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and \
                    (node.module or "").endswith("script_takeover"):
                assert not {a.name for a in node.names} & {"run", "apply",
                                                           "*"}, rel
    assert acts == {
        ("council_qt/tabs/council.py", "CouncilTab._take_over"),
        ("council_qt/tabs/dream3d.py", "Dream3DTab._take_over_typed"),
        ("council_qt/tabs/dream3d.py", "Dream3DTab.on_take_over"),
        ("council_gui_engine.py", "CouncilConsole._take_over_typed"),
        ("council_gui_engine.py",
         "CouncilConsole._dream3d_take_over_selected"),
    }, acts


def test_each_door_is_reached_only_from_the_users_send_or_click():
    doors = {"_take_over", "_take_over_typed", "_send_typed", "on_take_over",
             "_dream3d_take_over_selected", "take_over_ref"}
    uses = set()
    for rel, tree in _sources():
        for where, name in _uses(tree, doors):
            uses.add((rel, where, name))
    assert uses == {
        # Qt: the Council box (the Dream3D chat sends through it).
        ("council_qt/tabs/council.py", "CouncilTab.on_send", "_take_over"),
        ("council_qt/tabs/council.py", "CouncilTab._take_over",
         "take_over_ref"),
        # Qt: the Dream3D box with no Council tab, and the button.
        ("council_qt/tabs/dream3d.py", "Dream3DTab._send_standalone",
         "_take_over_typed"),
        ("council_qt/tabs/dream3d.py", "Dream3DTab._take_over_typed",
         "take_over_ref"),
        ("council_qt/tabs/dream3d.py", "Dream3DTab._pipeline_side",
         "on_take_over"),
        # Tk: Send / Ctrl+Enter, the Dream3D box, the button.
        ("council_gui_engine.py", "CouncilConsole._build_council_tab",
         "_send_typed"),
        ("council_gui_engine.py", "CouncilConsole._dream3d_send_from_input",
         "_send_typed"),
        ("council_gui_engine.py", "CouncilConsole._send_typed",
         "_take_over_typed"),
        ("council_gui_engine.py", "CouncilConsole._take_over_typed",
         "take_over_ref"),
        ("council_gui_engine.py", "CouncilConsole._build_dream3d_tab",
         "_dream3d_take_over_selected"),
        # Answered, never acted on.
        ("council_gui_engine.py", "CouncilConsole._handle_pipeline_intent",
         "take_over_ref"),
        ("council_core/pipeline_intent.py", "candidates", "take_over_ref"),
    }, sorted(uses)


def test_the_qt_send_paths_reach_the_door_only_from_the_input_box():
    """CouncilTab.on_send and Dream3DTab.on_send read the box; nothing else
    calls _send_standalone."""
    for rel, tree in _sources():
        for where, _n in _uses(tree, {"_send_standalone"}):
            assert (rel, where) in {
                ("council_qt/tabs/dream3d.py", "Dream3DTab.on_send"),
            }, (rel, where)


def test_no_agent_tool_can_take_a_script_over(tmp_path):
    import safe_agent
    import vault_analyst
    policy = safe_agent.AgentPolicy(allowed_tools=(), file_root=tmp_path,
                                    output_dir=tmp_path)
    names = " ".join(safe_agent.default_tools(policy)).lower()
    assert "take" not in names and "stamp" not in names
    # The analyst sandbox (model-written tools) cannot import the app.
    with pytest.raises(ImportError):
        vault_analyst._safe_import("council_core.script_takeover")
