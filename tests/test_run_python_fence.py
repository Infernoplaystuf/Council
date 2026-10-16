"""
The Council's run_python (Tools on: the Coder and the Intern) runs a model's
code behind the analyst's fence — the one data_query and the analyst before
a numbers question use (council_core.analyst_step: vault_analyst's sandbox
in a child process, at most 120 s, a memory cap) — and run_tests no longer
runs vault files around it.

Before this, run_python wrote the model's code to <vault>/workspace/
scratch.py and ran it in a plain child process with the app's environment:
measured on 2026-10-15, it read a canary outside the vault, wrote outside
the vault, wrote node_routing.json at the vault root (the next launch's
opt-in to send a role's prompts to another machine), saw the environment
and started a child process. run_tests then ran any .py in the vault —
including one the Intern saved with vault_save — with full permissions.

Every tool here is built the way the Council tab builds it
(CouncilActions.tools) and called the way a member calls it
(ToolSet.for_role). Every target is in pytest's temp folder; nothing reaches
a network (the offline guard refuses it anyway, in children too).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Council tab's actions need PySide6")

from council_core import analyst_step  # noqa: E402
from council_core import tool_kit  # noqa: E402
from council_qt.tabs.council import CouncilActions  # noqa: E402

SECRET = "TOP-SECRET-OUTSIDE"
ROOT_SECRET = "VAULT-ROOT-SECRET"
ENV_SECRET = "ENV-TOP-SECRET"
ENV_NAME = "COUNCIL_FENCE_TEST_SECRET"


@pytest.fixture
def box(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    data = vault / "data_in"
    data.mkdir(parents=True)
    (data / "runs.csv").write_text(
        "pump,runtime\nA,10\nB,20\nA,30\nC,40\n", encoding="utf-8")
    (vault / "manual.md").write_text(ROOT_SECRET + "\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text(SECRET + "\n", encoding="utf-8")
    (outside / "private.csv").write_text("secret\n" + SECRET + "\n",
                                         encoding="utf-8")
    (outside / "keep.txt").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setenv(ENV_NAME, ENV_SECRET)
    return NS(vault=vault, data=data, outside=outside,
              canary=outside / "canary.txt", keep=outside / "keep.txt",
              routing=vault / "node_routing.json")


def member_tool(box, role, name="run_python"):
    """`name` as the member in `role` gets it from the Council tab."""
    tools = CouncilActions(vault_dir=box.vault).tools()
    return tools.for_role(role)[name]


def fill(code, box):
    return code.format(canary=box.canary, keep=box.keep, out=box.outside,
                       vault=box.vault, private_txt=box.outside / "private.txt",
                       private_csv=box.outside / "private.csv",
                       env=ENV_NAME, py=sys.executable)


def leaked(text):
    return any(s in str(text) for s in (SECRET, ROOT_SECRET, ENV_SECRET))


# What the analyst fence already refuses (tests/test_sandbox_escapes.py and
# the 2026-10-15 audit's probes), each with the harm it would do.
ESCAPES = [
    # writing outside the data folder
    ("write", "open(r'{canary}', 'w').write('x')"),
    ("write", "from pathlib import Path\nPath(r'{canary}').write_text('x')"),
    ("write", "import pandas as pd\npd.DataFrame([1]).to_csv(r'{canary}')"),
    ("write", "import numpy as np\nnp.save(r'{canary}', np.arange(3))"),
    ("write", "split_csv_by_column('runs.csv', 'pump', r'{out}')"),
    # writing a file through a plot's savefig (pandas imports matplotlib)
    ("write", "ax = pd.read_csv('runs.csv').plot()\n"
              "ax.get_figure().savefig(r'{canary}')"),
    ("write", "pd.read_csv('runs.csv').plot().get_figure()"
              ".canvas.print_png(r'{canary}')"),
    # the app's own file at the vault root
    ("routing", "open(r'{vault}/node_routing.json', 'w').write('{{}}')"),
    ("routing", "from pathlib import Path\nPath(r'{vault}')"
                ".joinpath('node_routing.json').write_text('{{}}')"),
    # deleting
    ("delete", "from pathlib import Path\nPath(r'{keep}').unlink()"),
    ("delete", "import os\nos.remove(r'{keep}')"),
    ("delete", "import shutil\nshutil.rmtree(r'{out}')"),
    # processes
    ("process", "import subprocess\nsubprocess.run([r'{py}', '-c', "
                "r\"open(r'{canary}', 'w').write('x')\"])"),
    ("process", "import os\nos.system(r'echo x > \"{canary}\"')"),
    ("process", "from os import system\nsystem(r'echo x > \"{canary}\"')"),
    # native code and sockets (no connection is attempted)
    ("native", "import ctypes\nprint(ctypes.sizeof(ctypes.c_int))"),
    ("socket", "import socket\ns = socket.socket()\nprint('socket', s)"),
    # reading outside the data folder: outside the vault, and the vault root
    ("read", "print(open(r'{private_txt}').read())"),
    ("read", "import pandas as pd\nprint(pd.read_csv(r'{private_csv}'))"),
    ("read", "print(read_text(r'{private_txt}'))"),
    ("read", "print(open(r'{vault}/manual.md').read())"),
    ("read", "import pandas as pd\nprint(pd.read_csv('../manual.md'))"),
    # the environment
    ("env", "import os\nprint(os.environ.get('{env}'))"),
    ("env", "from os import environ\nprint(environ['{env}'])"),
    ("env", "import sys\nprint(sys.modules['os'].environ['{env}'])"),
    ("env", "import pathlib\nprint(pathlib.os.environ['{env}'])"),
]


@pytest.mark.parametrize("i", range(len(ESCAPES)),
                         ids=[f"{k}-{i}" for i, (k, _c) in enumerate(ESCAPES)])
def test_an_escape_fails_through_run_python_as_in_the_analyst(box, i,
                                                              monkeypatch):
    """Each escape fails through the Coder's and the Intern's run_python —
    refused before it prints, writes, deletes, starts or reads anything —
    and with the analyst sandbox's own refusal: the same fence, not a
    copy."""
    import vault_analyst as va
    code = fill(ESCAPES[i][1], box)
    role = ("coder", "intern")[i % 2]
    ok, msg, payload = member_tool(box, role)({"code": code})
    # (np.save adds .npy to the name it is given.)
    assert not list(box.outside.glob("canary*")), \
        "the code wrote outside the vault"
    assert not box.routing.exists(), "the code wrote node_routing.json"
    assert box.keep.exists() and box.outside.is_dir(), "the code deleted"
    assert not leaked(msg) and not leaked(payload), msg
    assert payload["stdout"] == "", payload["stdout"]
    assert not ok, msg
    assert payload["rc"] == 1, msg
    # The analyst's sandbox, asked directly over the same vault, refuses it
    # with the same words.
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(box.vault))
    rep: dict = {}
    df, _log = va.execute_pandas_code(code, [box.data], report=rep)
    assert df is None and not rep["ran"]
    assert rep["error"].strip().splitlines()[-1] in payload["stderr"]


# Rebinding a pathlib method the read containment used to rely on
# (Path.relative_to = lambda self, *a: self never raises) once let model code
# read any file. Each route, through each read surface, fails through
# run_python — refused before it reads or prints, no secret leaked.
REBINDS = [
    "Path.relative_to = lambda self, *a, **k: self",
    "Path.resolve = lambda self, *a, **k: self",
    "Path.is_absolute = lambda self: False",
    "type(Path('.')).relative_to = lambda self, *a, **k: self",
    "import pathlib\npathlib.PurePath.relative_to = lambda self, *a, **k: self",
    "setattr(Path, 'relative_to', lambda self, *a, **k: self)",
    "del Path.relative_to",
]
REBIND_READS = [
    "print(read_text(r'{private_txt}'))",
    "print(open(r'{private_txt}').read())",
    "import pandas as pd\nprint(pd.read_csv(r'{private_csv}'))",
    "print(open(r'{vault}/manual.md').read())",
]


@pytest.mark.parametrize("rd", range(len(REBIND_READS)))
@pytest.mark.parametrize("rb", range(len(REBINDS)))
def test_a_path_rebinding_read_fails_through_run_python(box, rb, rd,
                                                        monkeypatch):
    import vault_analyst as va
    code = fill(REBINDS[rb] + "\n" + REBIND_READS[rd], box)
    role = ("coder", "intern")[(rb + rd) % 2]
    ok, msg, payload = member_tool(box, role)({"code": code})
    assert not leaked(msg) and not leaked(payload), msg
    assert payload["stdout"] == "", payload["stdout"]
    assert not ok, msg
    assert payload["rc"] == 1, msg
    # The analyst sandbox refuses it directly with the same words.
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(box.vault))
    rep: dict = {}
    df, _log = va.execute_pandas_code(code, [box.data], report=rep)
    assert df is None and not rep["ran"]


def test_legitimate_assignments_run_through_run_python(box):
    """df.columns / df.index.name / df['x'] assignments have a plain local
    root, not a sandbox module/class/proxy, so they keep working."""
    ok, msg, payload = member_tool(box, "coder")({"code": (
        "df = pd.read_csv('runs.csv')\n"
        "df.columns = ['p', 'rt']\n"
        "df.index.name = 'idx'\n"
        "df['double'] = df['rt'] * 2\n"
        "print(int(df['double'].sum()))\n")})
    assert ok and payload["rc"] == 0, msg
    assert "200" in payload["stdout"], payload["stdout"]


@pytest.mark.parametrize("role", ["coder", "intern"])
def test_setting_a_pandas_display_option_runs_through_run_python(box, role):
    """`pd.options.display.max_columns = None` ran on 43b317e and was refused
    by the rebinding check (measured 2026-10-15): it changes an option, not
    pandas itself, and the child process throws it away."""
    ok, msg, payload = member_tool(box, role)({"code": (
        "import pandas as pd\n"
        "pd.options.display.max_columns = None\n"
        "pd.options.display.width = 200\n"
        "pd.options.mode.chained_assignment = None\n"
        "df = pd.read_csv('runs.csv')\n"
        "print(int(df['runtime'].sum()))\n")})
    assert ok and payload["rc"] == 0, msg
    assert "100" in payload["stdout"], payload["stdout"]


def test_an_imported_name_reused_for_a_series_runs_through_run_python(box):
    ok, msg, payload = member_tool(box, "coder")({"code": (
        "import pandas as pd\n"
        "from statistics import mean\n"
        "df = pd.read_csv('runs.csv')\n"
        "overall = mean(df['runtime'])\n"
        "mean = df.groupby('pump')['runtime'].mean()\n"
        "mean.name = 'avg_runtime'\n"
        "print(overall, mean.name)\n")})
    assert ok and payload["rc"] == 0, msg
    assert "avg_runtime" in payload["stdout"], payload["stdout"]


def test_an_endless_loop_is_stopped_at_the_limit_the_model_asked_for(box):
    t0 = time.monotonic()
    ok, msg, payload = member_tool(box, "coder")(
        {"code": "while True:\n    pass\n", "timeout_s": 3})
    assert time.monotonic() - t0 < 30
    assert not ok and payload["rc"] == 1 and payload.get("timed_out"), msg
    assert "stopped" in payload["stderr"]


class _Recorder:
    """child_proc.run's stand-in: records the limit, reports a time-out."""

    def __init__(self):
        self.timeouts = []

    def __call__(self, argv, **kw):
        self.timeouts.append(kw["timeout"])
        return NS(timed_out=True, stderr="", error="")


@pytest.mark.parametrize("asked,limit", [
    (100_000, analyst_step.TIME_LIMIT_S), (121, analyst_step.TIME_LIMIT_S),
    ("forever", analyst_step.TIME_LIMIT_S), (None, analyst_step.TIME_LIMIT_S),
    (0, 1), (-5, 1), (5, 5), ("7", 7)])
def test_the_model_may_lower_the_time_limit_but_never_raise_it(
        box, monkeypatch, asked, limit):
    from council_core import child_proc
    rec = _Recorder()
    monkeypatch.setattr(child_proc, "run", rec)
    ok, msg, payload = member_tool(box, "intern")(
        {"code": "print(1)", "timeout_s": asked})
    # The CODE budget is `limit` (capped at TIME_LIMIT_S, a model may lower
    # but never raise it); the child's wall timeout adds a fixed start-up
    # grace so importing the sandbox is not charged to the code.
    assert rec.timeouts == [limit + analyst_step.SANDBOX_STARTUP_S]
    assert not ok and payload.get("timed_out")


def test_data_query_has_the_same_limit(box, monkeypatch):
    """The analyst's path (run_code: data_query, the analyst before a
    numbers question) is the same fence: a caller cannot raise its limit
    past TIME_LIMIT_S either (plus the same fixed start-up grace)."""
    from council_core import child_proc
    rec = _Recorder()
    monkeypatch.setattr(child_proc, "run", rec)
    df, log = analyst_step.run_code("result_df = 1", box.data,
                                    timeout=10_000)
    assert df is None and "stopped" in log
    assert rec.timeouts == [analyst_step.TIME_LIMIT_S
                            + analyst_step.SANDBOX_STARTUP_S]


# ============================================================
# What keeps working
# ============================================================

@pytest.mark.parametrize("role", ["coder", "intern"])
def test_a_computation_on_the_vault_s_csv_returns_its_answer(box, role):
    ok, msg, payload = member_tool(box, role)({"code": (
        "import pandas as pd\n"
        "df = pd.read_csv('runs.csv')\n"
        "print(df.groupby('pump')['runtime'].mean().to_dict())\n")})
    assert ok and payload["rc"] == 0, msg
    assert "{'A': 20.0, 'B': 20.0, 'C': 40.0}" in payload["stdout"]
    assert "{'A': 20.0, 'B': 20.0, 'C': 40.0}" in msg


def test_a_result_df_comes_back_as_a_table(box):
    ok, msg, payload = member_tool(box, "intern")({"code": (
        "result_df = pd.read_csv('runs.csv').groupby('pump', "
        "as_index=False)['runtime'].mean()")})
    assert ok and payload["rows"] == 3 and payload["cols"] == 2, msg
    assert "runtime" in payload["table"] and "40" in payload["table"]
    assert "--- result_df ---" in msg


def test_numpy_scipy_and_the_standard_helpers_still_work(box):
    ok, msg, payload = member_tool(box, "coder")({"code": (
        "import numpy as np\nimport statistics, math, json, re\n"
        "from collections import Counter\nimport scipy.stats as st\n"
        "print(np.mean([1, 2, 3]), statistics.median([1, 5, 9]),\n"
        "      math.sqrt(16), json.dumps({'k': 1}),\n"
        "      Counter('aab')['a'], round(float(st.tmean([1, 2, 3])), 1))\n"
        "print(len(list_dir()))\n")})
    assert ok, msg
    assert "2.0 5 4.0 {\"k\": 1} 2 2.0" in payload["stdout"], msg


def test_an_error_in_the_code_comes_back_for_the_model_to_fix(box):
    ok, msg, payload = member_tool(box, "coder")(
        {"code": "print('before')\nprint(1 / 0)"})
    assert not ok and payload["rc"] == 1
    assert "before" in payload["stdout"]
    assert "ZeroDivisionError" in payload["stderr"] and \
        "ZeroDivisionError" in msg


def test_the_sandbox_helpers_write_only_the_turn_s_data_out(box):
    """split_csv_by_column may write — into THIS vault's data_out, the
    turn's vault reaching the child (not whatever vault the app process
    resolves)."""
    ok, msg, payload = member_tool(box, "intern")({"code": (
        "r = split_csv_by_column('runs.csv', 'pump', 'split')\n"
        "print(int(r['rows'].sum()))")})
    assert ok and payload["stdout"].strip() == "4", msg
    written = sorted(p.name for p in (box.vault / "data_out" / "split")
                     .glob("*.csv"))
    assert written == ["runs_A.csv", "runs_B.csv", "runs_C.csv"], msg


def test_nothing_is_written_for_the_code_itself(box):
    """No <vault>/workspace/scratch.py (run_tests could import it) and a
    `filename` argument is ignored."""
    ok, msg, payload = member_tool(box, "coder")(
        {"code": "print(6 * 7)", "filename": "../evil.py"})
    assert ok and "42" in payload["stdout"], msg
    assert not (box.vault / "workspace").exists()
    assert not (box.vault / "evil.py").exists()
    assert "path" not in payload


def test_a_main_guard_script_runs(box):
    """`if __name__ == "__main__":` — which a Coder asked to "write a script
    and check it runs" very often writes — worked on the old unsandboxed
    run_python; the sandbox now sets __name__ so it keeps working."""
    ok, msg, payload = member_tool(box, "coder")({"code": (
        "def main():\n    print('hello from main')\n\n"
        "if __name__ == '__main__':\n    main()\n")})
    assert ok and payload["rc"] == 0, msg
    assert "hello from main" in payload["stdout"]


@pytest.mark.parametrize("t", [1, 2, 3])
def test_a_small_timeout_still_runs_trivial_code(box, t):
    """The sandbox's own start-up (importing pandas/numpy/scipy) must not be
    charged to the model's code budget: a small timeout_s used to fail
    trivial code and blame the code (reproduced 2026-10-15)."""
    ok, msg, payload = member_tool(box, "coder")(
        {"code": "print(6 * 7)", "timeout_s": t})
    assert ok and payload["rc"] == 0, msg
    assert "42" in payload["stdout"]
    assert not payload.get("timed_out")


@pytest.mark.parametrize("code,plain", [
    ("import os\nprint(os.getcwd())",
     "the sandbox does not allow that import"),
    ("import pandas as pd\npd.DataFrame([1]).to_csv(r'x.csv')",
     "run_python cannot write files"),
    ("class Pump:\n    pass\nprint(Pump())",
     "class definitions are not available in the sandbox"),
    ("import pandas as pd\npd.read_csv = None",
     "the sandbox does not let code change pandas"),
])
def test_a_refusal_comes_back_in_plain_words(box, code, plain):
    """A refusal reaches the model and the transcript as one plain sentence
    before the sandbox's own line (2026-10-15)."""
    ok, msg, payload = member_tool(box, "coder")({"code": code})
    assert not ok
    assert plain in payload["stderr"], payload["stderr"]
    assert plain in msg


def test_run_code_writes_only_the_vault_it_is_given(box, tmp_path,
                                                    monkeypatch):
    """analyst_step.run_code(..., vault=V) — the data_query and analyst
    path — puts the sandbox helpers' output in V's data_out, not the vault
    the child's environment names (they differ for a turn whose vault is not
    the process's)."""
    env_vault = tmp_path / "env_vault"
    (env_vault / "data_in").mkdir(parents=True)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(env_vault))
    df, log = analyst_step.run_code(
        "result_df = split_csv_by_column('runs.csv', 'pump', 'split')",
        box.data, vault=box.vault)
    assert df is not None, log
    written = sorted(p.name for p in (box.vault / "data_out" / "split")
                     .glob("*.csv"))
    assert written == ["runs_A.csv", "runs_B.csv", "runs_C.csv"], log
    assert not list((env_vault / "data_out").glob("**/*.csv"))


def test_the_tool_says_what_it_is():
    params = tool_kit.PARAMS["run_python"]["properties"]
    assert set(params) == {"code", "timeout_s"}
    from council_core import council_tools
    help_text = council_tools.make_tools(None, None, Path("."))[
        "run_python"].help
    assert "sandbox" in help_text and "data_in" in help_text
    assert "starts no processes" in help_text
    assert "workspace" not in help_text
    assert "no charts" in help_text.lower()


# ============================================================
# run_tests: no side door around the fence
# ============================================================

PLANTED = ("from pathlib import Path\n"
           "Path(r'{canary}').write_text('ran')\n\n"
           "def test_planted():\n    assert True\n")


def test_run_tests_is_not_on_the_council_tab_menu(box):
    """The Council tab names no code roots, so run_tests could only ever
    refuse there; it is dropped from the Coder's menu entirely, so an Intern
    who saved a test file has no Coder tool that could run it."""
    tools = CouncilActions(vault_dir=box.vault).tools()
    assert "run_tests" not in tools
    assert "run_tests" not in tools.for_role("coder")


def test_run_tests_refuses_the_vault_even_inside_a_code_root(box, tmp_path):
    from council_core import council_tools
    (box.vault / "test_planted.py").write_text(
        PLANTED.format(canary=box.canary), encoding="utf-8")
    tools = council_tools.make_tools(None, None, box.vault, roots=[tmp_path])
    ok, msg, _p = tools.for_role("coder")["run_tests"](
        {"path": str(box.vault / "test_planted.py")})
    assert not ok and "never runs files in the vault" in msg, msg
    assert not box.canary.exists()


def test_run_tests_refuses_a_directory_that_contains_the_vault(box, tmp_path):
    """The target is a code root that CONTAINS the vault (or '.'). pytest
    would otherwise collect, import and run a model's vault_save'd test files
    and load a vault-root conftest.py — the full-permission run the fence is
    there to stop. --ignore drops the whole vault subtree (found 2026-10-15,
    probe_run_tests_dir.py)."""
    from council_core import council_tools
    code = tmp_path / "code"
    vault = code / "vault"
    (vault / "data_in").mkdir(parents=True)
    (code / "test_real.py").write_text("def test_real():\n    assert True\n",
                                       encoding="utf-8")
    (vault / "test_planted.py").write_text(
        PLANTED.format(canary=box.canary), encoding="utf-8")
    # A conftest.py at the vault root is a file vault_save could create.
    (vault / "conftest.py").write_text(
        f"from pathlib import Path\n"
        f"Path(r'{box.canary.parent / 'conftest_ran.txt'}').write_text('x')\n",
        encoding="utf-8")
    tools = council_tools.make_tools(None, None, vault, roots=[code])
    rt = tools.for_role("coder")["run_tests"]
    for path in (".", str(code)):
        ok, msg, _p = rt({"path": path})
        # the project's own test still runs; the vault's does not
        assert not box.canary.exists(), f"{path}: a vault test ran\n{msg}"
        assert not (box.canary.parent / "conftest_ran.txt").exists(), \
            f"{path}: a vault conftest loaded\n{msg}"
    # and -k cannot reach into the vault either
    ok, msg, _p = rt({"path": ".", "k": "planted"})
    assert not box.canary.exists(), msg


def test_run_tests_still_runs_a_code_project_s_tests(box, tmp_path):
    from council_core import council_tools
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                  encoding="utf-8")
    (proj / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    tools = council_tools.make_tools(None, None, box.vault, roots=[proj])
    ok, msg, payload = tools.for_role("coder")["run_tests"](
        {"path": "test_calc.py"})
    assert ok and payload["passed"], msg
