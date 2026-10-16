"""The analyst sandbox (vault_analyst.execute_pandas_code) must keep model-written
code inside the data folders: no reading elsewhere, no writing anywhere, no
running code through pickle, no network.

Every escape here was reproduced on 2026-10-05 against the code before this
fix (absolute paths through pd.read_csv, pd.read_pickle running a payload,
np.save / df.to_json(path) writing files, helpers taking any path, scipy.io).
The sandbox now runs model-written TOOLS that the council creates and attaches
to agents, so these are the guarantees that make that safe.

Only local temp folders are used; the URL case is refused before any socket
opens (port 9 on loopback would refuse anyway).
"""
from __future__ import annotations

import pickle

import pytest

import vault_analyst as va


@pytest.fixture
def box(tmp_path):
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (data / "ok.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (data / "notes.txt").write_text("inside\n", encoding="utf-8")
    (outside / "private.csv").write_text("secret\nTOP-SECRET\n", encoding="utf-8")
    (outside / "private.txt").write_text("TOP-SECRET\n", encoding="utf-8")

    class Payload:
        def __reduce__(self):
            return (print, ("PAYLOAD-RAN",))
    (data / "evil.pkl").write_bytes(pickle.dumps(Payload()))
    return data, outside


def run(code, data):
    return va.execute_pandas_code(code, [data])


def refused(df, msg):
    return df is None and "TOP-SECRET" not in msg and "PAYLOAD-RAN" not in msg


# ── reading outside the data folders ─────────────────────────────────────
def test_absolute_path_outside_is_refused(box):
    data, outside = box
    df, msg = run(f"result_df = pd.read_csv(r'{outside / 'private.csv'}')", data)
    assert refused(df, msg)


def test_relative_traversal_is_refused(box):
    data, _ = box
    df, msg = run("result_df = pd.read_csv('../outside/private.csv')", data)
    assert refused(df, msg)


def test_url_is_refused(box):
    data, _ = box
    df, msg = run("result_df = pd.read_csv('http://127.0.0.1:9/x.csv')", data)
    assert df is None and "blocked" in msg.lower()


def test_path_read_text_outside_is_refused(box):
    data, outside = box
    df, msg = run(f"result_df = pd.DataFrame([Path(r'{outside / 'private.txt'}')"
                  f".read_text()])", data)
    assert refused(df, msg)


def test_helper_given_an_outside_path_is_refused(box):
    data, outside = box
    for code in (f"result_df = summarize_csv(r'{outside / 'private.csv'}')",
                 f"result_df = read_table(r'{outside / 'private.csv'}')",
                 f"result_df = pd.DataFrame(list_csv_files(r'{outside}'))"):
        df, msg = run(code, data)
        assert refused(df, msg), code


def test_stats_helpers_given_an_outside_path_are_refused(box, tmp_path, monkeypatch):
    # file_stats / column_stats are lambdas defined inside execute_pandas_code,
    # which _contain_helpers skips as "the sandbox's own closures"; they
    # passed the raw path on — MEASURED: TOP-SECRET came back in the 'top'
    # column and a .stats_cache/_ext_* shard was written into the vault.
    data, outside = box
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    for code in (f"result_df = pd.DataFrame([file_stats(r'{outside / 'private.csv'}')])",
                 f"result_df = column_stats(r'{outside}')",
                 f"result_df = column_stats([r'{outside}'])"):
        df, msg = run(code, data)
        assert refused(df, msg), code
        assert "outside" in msg, msg
    assert not list(vault.rglob("_ext_*"))
    # ...while the data folder's own files still get their stats.
    df, msg = run("result_df = column_stats()", data)
    assert df is not None and "ok.csv" in df.to_string(), msg
    df, msg = run("result_df = pd.DataFrame([file_stats('ok.csv')])", data)
    assert df is not None and not df.empty, msg


def test_excel_file_outside_is_refused(box):
    pytest.importorskip("openpyxl")
    import pandas as pd
    data, outside = box
    book = outside / "private.xlsx"
    pd.DataFrame({"secret": ["TOP-SECRET"]}).to_excel(book, index=False)
    df, msg = run(f"x = pd.ExcelFile(r'{book}')\n"
                  "result_df = pd.read_excel(x)", data)
    assert refused(df, msg) and "outside the data folders" in msg
    # ...while a workbook inside the folder still opens.
    pd.DataFrame({"a": [1]}).to_excel(data / "in.xlsx", index=False)
    df2, msg2 = run("result_df = pd.read_excel(pd.ExcelFile('in.xlsx'))", data)
    assert df2 is not None, msg2


# ── code execution through pickle ────────────────────────────────────────
@pytest.mark.parametrize("code", [
    "result_df = pd.DataFrame([str(pd.read_pickle(r'{p}'))])",
    "f = getattr(pd, 'read_pickle')\nresult_df = pd.DataFrame([str(f(r'{p}'))])",
    "import numpy\nresult_df = pd.DataFrame([str(numpy.load(r'{p}', allow_pickle=True))])",
    "from numpy import load\nresult_df = pd.DataFrame([str(load(r'{p}', allow_pickle=True))])",
])
def test_pickle_cannot_run(box, code, capsys):
    data, _ = box
    df, msg = run(code.format(p=data / "evil.pkl"), data)
    assert refused(df, msg)
    assert "PAYLOAD-RAN" not in capsys.readouterr().out


# ── writing anywhere ─────────────────────────────────────────────────────
@pytest.mark.parametrize("code", [
    "import numpy as np\nnp.save(r'{t}', np.arange(3))\nresult_df = pd.DataFrame([1])",
    "import numpy as np\nnp.savetxt(r'{t}', np.arange(3))\nresult_df = pd.DataFrame([1])",
    "import numpy as np\nnp.arange(3).tofile(r'{t}')\nresult_df = pd.DataFrame([1])",
    "df = pd.DataFrame([1])\ndf.to_json(r'{t}')\nresult_df = df",
    "df = pd.DataFrame([1])\ndf.to_html(r'{t}')\nresult_df = df",
    "df = pd.DataFrame([1])\ndf.to_string(buf=r'{t}')\nresult_df = df",
    "result_df = split_csv_by_column('ok.csv', 'a', r'{d}')",
])
def test_writes_are_refused(box, code, tmp_path):
    data, outside = box
    target = outside / "written.out"
    df, _msg = run(code.format(t=target, d=outside / "split"), data)
    assert df is None
    assert not target.exists()
    assert not (outside / "split").exists()


# ── module routes around the guards ──────────────────────────────────────
@pytest.mark.parametrize("code", [
    "import scipy.io\nresult_df = pd.DataFrame([1])",
    "from scipy import io\nresult_df = pd.DataFrame([1])",
    "import numpy.lib\nresult_df = pd.DataFrame([1])",
    "x = pd.io\nresult_df = pd.DataFrame([1])",
    "x = getattr(pd, 'io')\nresult_df = pd.DataFrame([1])",
])
def test_module_routes_are_refused(box, code):
    data, _ = box
    df, _msg = run(code, data)
    assert df is None


# ── vars()/dir() + the unwrapped module behind the proxy (2026-10-15) ────
# vars(x) / x.__dict__ hands back a live namespace, and a dict SUBSCRIPT on
# it is invisible to the AST validator (it checks attribute names, not keys).
# The proxy objects keep the real module in `_real`, so `pd._real` was the
# unwrapped pandas and `vars(pd)['_real']['read_pickle']` ran a pickle.
@pytest.mark.parametrize("code", [
    "m = vars(pd)\nresult_df = pd.DataFrame([1])",
    "m = dir(pd)\nresult_df = pd.DataFrame([1])",
    "rp = pd._real\nresult_df = pd.DataFrame([1])",
    "rp = np._real\nresult_df = pd.DataFrame([1])",
    "result_df = pd.DataFrame([getattr(pd, '_real')])",
    "rp = pd._real\nresult_df = rp.read_csv('ok.csv')",
])
def test_vars_dir_and_the_proxy_real_are_refused(box, code):
    data, _ = box
    df, msg = run(code, data)
    assert df is None, msg


def test_vars_cannot_reach_read_pickle(box, capsys):
    data, _ = box
    code = ("a = vars(pd)['_real']\n"
            "f = vars(a)['read_pickle']\n"
            f"result_df = pd.DataFrame([str(f(r'{data / 'evil.pkl'}'))])\n")
    df, msg = run(code, data)
    assert refused(df, msg)
    assert "PAYLOAD-RAN" not in capsys.readouterr().out


# ── writing a file through a plot's savefig (2026-10-15) ─────────────────
# pandas imports matplotlib internally for df.plot(); its figure/canvas
# writers wrote a file anywhere, around every other write guard.
@pytest.mark.parametrize("code", [
    "ax = pd.DataFrame({{'a': [1, 2]}}).plot()\n"
    "ax.get_figure().savefig(r'{t}')\nresult_df = pd.DataFrame([1])",
    "ax = pd.DataFrame({{'a': [1, 2]}}).plot()\n"
    "ax.figure.savefig(r'{t}', format='png')\nresult_df = pd.DataFrame([1])",
    "fig = pd.DataFrame({{'a': [1, 2]}}).plot().get_figure()\n"
    "fig.canvas.print_png(r'{t}')\nresult_df = pd.DataFrame([1])",
    "w = pd.DataFrame({{'a': [1, 2]}}).plot().get_figure().savefig\n"
    "w(r'{t}')\nresult_df = pd.DataFrame([1])",
])
def test_savefig_is_refused(box, code):
    data, outside = box
    target = outside / "chart.png"
    df, _msg = run(code.format(t=target), data)
    assert df is None
    assert not target.exists()
    assert not list(outside.glob("*.png"))


# ── what must keep working ───────────────────────────────────────────────
@pytest.mark.parametrize("code", [
    "result_df = pd.read_csv('ok.csv')",
    "result_df = pd.read_csv(DATA_FOLDER + '/ok.csv')",
    "result_df = pd.read_csv(Path(DATA_FOLDER) / 'ok.csv')",
    "result_df = summarize_csv('ok.csv')",
    "result_df = pd.DataFrame([read_text('notes.txt')])",
    "import numpy as np\nresult_df = pd.DataFrame({'m': [np.mean(pd.read_csv('ok.csv')['a'])]})",
    "result_df = pd.DataFrame([pd.read_csv('ok.csv').to_json()])",
    "result_df = pd.DataFrame([pd.read_csv('ok.csv').to_string(index=False)])",
    "import scipy.stats as st\nresult_df = pd.DataFrame([st.tmean([1, 2, 3])])",
])
def test_inside_the_data_folder_still_works(box, code):
    data, _ = box
    df, msg = run(code, data)
    assert df is not None, msg


def test_refusal_points_at_the_contained_helper(box):
    data, _ = box
    df, msg = run("result_df = pd.DataFrame([Path(DATA_FOLDER + '/notes.txt').read_text()])",
                  data)
    assert df is None and "read_text(path)" in msg


# ── Path-rebinding read escape (2026-10-15) ───────────────────────────────
# Model code runs with the real pathlib.Path in scope. The read containment
# USED to refuse an outside path only because Path.relative_to RAISED, so
# rebinding it to a no-op made every outside path pass the check and read any
# file:
#     Path.relative_to = lambda self, *a, **k: self
#     print(read_text(r'<file outside the data folder>'))
# Two defences close it: (1) the AST validator refuses assigning to or
# deleting an attribute of a sandbox module/class/proxy; (2) the containment
# resolves and compares with captured os.path functions, never a Path method
# model code can rebind. The first set of tests exercises (1); the monkeypatch
# test exercises (2) in isolation.

_REBINDS = [
    "Path.relative_to = lambda self, *a, **k: self",
    "Path.resolve = lambda self, *a, **k: self",
    "Path.is_absolute = lambda self: False",
    "type(Path('.')).relative_to = lambda self, *a, **k: self",
    "import pathlib\npathlib.PurePath.relative_to = lambda self, *a, **k: self",
    "setattr(Path, 'relative_to', lambda self, *a, **k: self)",
    "del Path.relative_to",
]

_READ_SURFACES = [
    "result_df = pd.DataFrame([read_text(r'{txt}')])",
    "result_df = pd.DataFrame([open(r'{txt}').read()])",
    "result_df = pd.read_csv(r'{csv}')",
    "result_df = summarize_csv(r'{csv}')",
    "result_df = pd.DataFrame(list_dir(r'{out}'))",
]


@pytest.mark.parametrize("rebind", _REBINDS,
                         ids=[r.split("=")[0].split("\n")[-1].strip()[:28]
                              for r in _REBINDS])
@pytest.mark.parametrize("surface", _READ_SURFACES,
                         ids=["read_text", "open", "read_csv", "summarize_csv",
                              "list_dir"])
def test_rebinding_a_path_method_cannot_read_outside(box, rebind, surface):
    data, outside = box
    code = rebind + "\n" + surface.format(
        txt=outside / "private.txt", csv=outside / "private.csv", out=outside)
    df, msg = run(code, data)
    # Refused at the AST validator — nothing read, nothing leaked.
    assert df is None, code
    assert "TOP-SECRET" not in msg and "Forbidden" in msg, code


def test_containment_holds_even_if_a_path_method_is_rebound(box, monkeypatch):
    """Defence (2) in isolation: with the Path methods the old check relied on
    ALREADY rebound in the process (as if a rebind slipped past the AST), the
    containment still refuses outside reads and still allows inside ones,
    because it resolves/compares with captured os.path functions."""
    import pathlib
    data, outside = box
    monkeypatch.setattr(pathlib.Path, "relative_to",
                        lambda self, *a, **k: self, raising=False)
    monkeypatch.setattr(pathlib.Path, "resolve",
                        lambda self, *a, **k: self, raising=False)
    monkeypatch.setattr(pathlib.Path, "is_absolute",
                        lambda self: False, raising=False)
    monkeypatch.setattr(pathlib.PurePath, "relative_to",
                        lambda self, *a, **k: self, raising=False)
    for code in (
        f"result_df = pd.DataFrame([read_text(r'{outside / 'private.txt'}')])",
        f"result_df = pd.DataFrame([open(r'{outside / 'private.txt'}').read()])",
        f"result_df = pd.read_csv(r'{outside / 'private.csv'}')",
        f"result_df = summarize_csv(r'{outside / 'private.csv'}')",
    ):
        df, msg = run(code, data)
        assert refused(df, msg), code
    # ...while reads inside the data folder keep working.
    df, msg = run("result_df = pd.read_csv('ok.csv')", data)
    assert df is not None, msg
    df, msg = run("result_df = pd.DataFrame([read_text('notes.txt')])", data)
    assert df is not None, msg


@pytest.mark.parametrize("code", [
    "Path.relative_to = lambda self, *a, **k: self",
    "Path.resolve = None",
    "type(Path('.')).relative_to = None",
    "import pathlib\npathlib.PurePath.relative_to = None",
    "pd.read_csv = None",
    "pd.contain = lambda self, v: v",
    "import numpy as _n\n_n.save = None",
    "del Path.relative_to",
    "import json as _j\n_j.loads = None",
])
def test_rebinding_a_sandbox_module_class_or_proxy_is_refused(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


@pytest.mark.parametrize("code", [
    "df = pd.read_csv('ok.csv')\ndf.columns = ['x', 'y']\nresult_df = df",
    "df = pd.read_csv('ok.csv')\ndf.index.name = 'idx'\nresult_df = df.reset_index()",
    "df = pd.read_csv('ok.csv')\ndf['c'] = df['a'] + df['b']\nresult_df = df",
    "s = pd.read_csv('ok.csv')['a']\ns.name = 'renamed'\nresult_df = s.reset_index()",
    "df = pd.read_csv('ok.csv')\ndf.attrs['note'] = 'ok'\nresult_df = df",
])
def test_legitimate_attribute_assignments_still_work(box, code):
    data, _ = box
    df, msg = run(code, data)
    assert df is not None, msg


# ── legitimate code the rebinding refusal must not catch ─────────────────
# Measured 2026-10-15 on 430fdb0 with a 40-snippet battery through
# run_python / data_query / validate_generated_code: these ran on 43b317e and
# were refused by the new assignment check.
_PD_OPTION_KEYS = ("display.max_columns", "display.width",
                   "mode.chained_assignment", "display.float_format")


@pytest.fixture
def pd_options_restored():
    """execute_pandas_code runs in THIS process here, so an option the code
    sets would outlive the test: put back every one it may touch."""
    saved = {k: va.pd.get_option(k) for k in _PD_OPTION_KEYS}
    yield
    for k, v in saved.items():
        va.pd.set_option(k, v)


@pytest.mark.parametrize("code", [
    "pd.options.display.max_columns = None\npd.options.display.width = 200",
    "pd.options.mode.chained_assignment = None",
    "pd.options.display.float_format = '{:,.2f}'.format",
    "import pandas as p2\np2.options.display.max_columns = 50",
])
def test_setting_a_pandas_option_by_attribute_still_works(
        box, pd_options_restored, code):
    """`pd.options.display.max_columns = None` is everyday LLM pandas; it
    changes an option pandas checks itself, not a module or class."""
    data, _ = box
    df, msg = run(code + "\nresult_df = pd.read_csv('ok.csv')", data)
    assert df is not None, msg


def test_tool_code_that_sets_a_pandas_option_passes_the_check():
    """validate_generated_code is the gate a Forge / Agent Creator tool is
    approved through, so the same line must pass there too."""
    ok, msg = va.validate_generated_code(
        "def summarise():\n"
        "    pd.options.display.max_columns = None\n"
        "    return pd.read_csv('ok.csv').describe()\n")
    assert ok, msg


@pytest.mark.parametrize("code", [
    "pd.options = None",
    "pd.options.plotting.backend = 'x'",
    "pd.options.io.excel.xlsx.writer = 'x'",
    "pd.options.display = None",
    "del pd.options.display.max_columns",
    "pd.options.display.__class__ = None",
    "pd.read_csv.options.display.x = None",
    "pd.options['display'].max_columns = None",
])
def test_only_display_style_pandas_options_are_let_through(code):
    """The exception is `pd.options.<group>.<name> = value` for the groups
    that only change how pandas shows or computes; anything else on pd is
    still refused."""
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


@pytest.mark.parametrize("code", [
    ("from scipy import stats\n"
     "df = pd.read_csv('ok.csv')\n"
     "t = stats.ttest_1samp(df['a'], 0)\n"
     "stats = df.groupby('a')['b'].describe()\n"
     "stats.index.name = 'a'\n"
     "result_df = stats.reset_index()"),
    ("from statistics import mean\n"
     "df = pd.read_csv('ok.csv')\n"
     "overall = mean(df['b'])\n"
     "mean = df.groupby('a')['b'].mean()\n"
     "mean.name = 'avg_b'\n"
     "result_df = mean.reset_index()"),
])
def test_an_imported_name_reused_for_the_model_s_own_frame_can_be_changed(
        box, code):
    """`stats` / `mean` are imported, then rebound to the model's own
    frame/Series; setting .name on THAT is not changing a module."""
    data, _ = box
    df, msg = run(code, data)
    assert df is not None, msg


@pytest.mark.parametrize("code", [
    "from scipy import stats\nstats.ttest_1samp = None",
    "import json\njson.loads = None",
    "from pathlib import Path as P\nP.resolve = None",
])
def test_an_imported_module_the_code_does_not_rebind_stays_protected(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


@pytest.mark.parametrize("code", [
    "pd.read_csv = None",
    "del Path.relative_to",
    "setattr(Path, 'resolve', None)",
])
def test_the_rebinding_refusal_says_what_to_do_instead(code):
    """No sandbox jargon ("proxy", "rebound") and a way forward."""
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg
    assert "proxy" not in msg and "rebound" not in msg, msg
    assert "your own variables" in msg, msg
    assert "pd.set_option" in msg, msg


# ── the containment is one helper ─────────────────────────────────────────
def test_the_containment_is_one_module_level_helper(box, tmp_path):
    """_contain_helpers no longer takes the resolve_dir it stopped using,
    _BudgetedPandas.contain (no caller) is gone, and the one resolver every
    read surface uses keeps a prefix-sharing sibling (data vs data_old) and
    a `..` route outside."""
    import inspect
    assert "resolve_dir" not in inspect.signature(
        va._contain_helpers).parameters
    assert not hasattr(va._BudgetedPandas, "contain")
    data, outside = box
    sibling = tmp_path / "data_old"
    sibling.mkdir()
    (sibling / "x.csv").write_text("x\n1\n", encoding="utf-8")
    roots = [str(data)]
    reals = [va._sandbox_real_norm(r) for r in roots]
    inside = va._sandbox_resolve_within("ok.csv", roots, reals)
    assert inside is not None and inside.endswith("ok.csv")
    assert va._sandbox_resolve_within(str(sibling / "x.csv"),
                                      roots, reals) is None
    assert va._sandbox_resolve_within("../outside/private.txt",
                                      roots, reals) is None
    assert va._sandbox_resolve_within(str(outside / "private.txt"),
                                      roots, reals) is None


# ── Alias + every store-target form of the rebind (2026-10-16) ────────────
# The assignment check protected the NAMES pd/np/Path/pathlib/… but not an
# ALIAS bound from one (P = Path; P.resolve = ...), nor a store target other
# than a plain `=` (a for-loop / comprehension / with-as target, or
# tuple/list/starred unpacking). Each is now refused by validate_generated_code
# with the same plain wording. Reproduced against 778e401, which let them
# through.
_ALIAS_REBINDS = [
    "P = Path\nP.resolve = lambda self, *a, **k: self",
    "P = Path\nQ = P\nQ.resolve = lambda self, *a, **k: self",
    "C = type(Path('.'))\nC.resolve = lambda self, *a, **k: self",
    "import pathlib\nX = pathlib.Path\nX.relative_to = None",
    "from pathlib import PurePath as Q\nQ.relative_to = None",
    "D = pd\nD.read_csv = None",
    "N = np\nN.save = None",
]


@pytest.mark.parametrize("code", _ALIAS_REBINDS)
def test_an_alias_of_a_protected_root_cannot_be_rebound(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg
    assert "your own variables" in msg, msg


_STORE_TARGETS = [
    # for-loop target
    "for Path.resolve in [lambda self, *a, **k: self]:\n    pass",
    "for type(Path('.')).resolve in [lambda self, *a, **k: self]:\n    pass",
    # comprehension target
    "[0 for Path.resolve in [lambda self, *a, **k: self]]",
    # with ... as target
    "with pd as Path.resolve:\n    pass",
    # tuple / list unpacking
    "Path.resolve, x = (lambda self, *a, **k: self), 1",
    "(pd.read_csv, y) = (None, 1)",
    # starred unpacking
    "Path.resolve, *rest = [lambda self, *a, **k: self, 1, 2]",
    # augmented / annotated
    "Path.relative_to = Path.relative_to\nPath.relative_to += None",
]


@pytest.mark.parametrize("code", _STORE_TARGETS)
def test_every_store_target_form_is_checked(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


_ALIAS_READ_REBINDS = [
    "P = Path\nP.relative_to = lambda self, *a, **k: self",
    "P = Path\nQ = P\nQ.resolve = lambda self, *a, **k: self",
    "C = type(Path('.'))\nC.relative_to = lambda self, *a, **k: self",
    "import pathlib\nX = pathlib.PurePath\nX.relative_to = lambda self, *a, **k: self",
    "from pathlib import Path as P2\nP2.resolve = lambda self, *a, **k: self",
]


@pytest.mark.parametrize("rebind", _ALIAS_READ_REBINDS,
                         ids=[r.split("\n")[-1].split("=")[0].strip()[:24]
                              for r in _ALIAS_READ_REBINDS])
@pytest.mark.parametrize("surface", _READ_SURFACES,
                         ids=["read_text", "open", "read_csv", "summarize_csv",
                              "list_dir"])
def test_an_alias_rebinding_cannot_read_outside(box, rebind, surface):
    """Each alias route, through each read/list surface, against a canary
    outside the data folder: refused at the validator, nothing read, nothing
    leaked."""
    data, outside = box
    code = rebind + "\n" + surface.format(
        txt=outside / "private.txt", csv=outside / "private.csv", out=outside)
    df, msg = run(code, data)
    assert df is None, code
    assert "TOP-SECRET" not in msg and "Forbidden" in msg, code


# ── The per-run copy of pathlib (defence in depth for the libraries) ──────
def test_make_sandbox_pathlib_isolates_the_real_classes():
    """_make_sandbox_pathlib hands out per-run SUBCLASSES: a rebind through
    any of them lands on the copy, the real pathlib classes the helpers,
    pandas and numpy use are untouched, and the subclasses are closed under
    construction (so type(Path('.')) is the copy too). Dunder / module routes
    on the proxy stay blocked."""
    import pathlib
    proxy, SbPath = va._make_sandbox_pathlib()
    assert issubclass(SbPath, pathlib.Path) and SbPath is not pathlib.Path
    assert proxy.Path is SbPath
    # Path('.') builds the concrete copy for this OS (as real pathlib builds
    # a WindowsPath / PosixPath), and the concrete copy is closed under
    # construction (2026-10-16: Path itself is no longer the concrete copy).
    Concrete = type(SbPath("."))
    assert issubclass(Concrete, SbPath) and Concrete is not SbPath
    assert Concrete is getattr(proxy, type(pathlib.Path()).__name__)
    assert type(SbPath(".") / "x") is Concrete
    assert type(SbPath(".").parent) is Concrete
    assert issubclass(proxy.PurePath, pathlib.PurePath)
    assert proxy.PurePath is not pathlib.PurePath
    real_resolve = pathlib.Path.resolve
    SbPath.resolve = lambda self, *a, **k: "HACK"
    assert pathlib.Path.resolve is real_resolve   # real class untouched
    pathlib.PurePath.relative_to  # sanity: attr still present
    # pathlib.os / pathlib.sys are real module attributes (the classic
    # pathlib.sys.modules / pathlib.os.environ routes); the proxy's getattr
    # refuses them. (_real and the dunders are the AST validator's job, as on
    # the pandas/numpy proxies.)
    for blocked in ("sys", "os", "modules", "subprocess", "socket"):
        with pytest.raises(AttributeError):
            getattr(proxy, blocked)


def test_the_sandbox_hands_model_code_a_copy_of_pathlib(box, monkeypatch):
    """Defence (3) in isolation: with the AST rebind check turned OFF, a model
    that rebinds a pathlib method through `import pathlib`, through `Path`, and
    through `type(Path('.'))` changes only the per-run copy — the real
    pathlib.Path in THIS process is untouched, the read still never leaves the
    data folder (captured-os containment), and inside reads keep working."""
    import pathlib
    data, outside = box
    monkeypatch.setattr(va, "validate_generated_code", lambda code: (True, "ok"))
    real_resolve = pathlib.Path.resolve
    real_rel = pathlib.PurePath.relative_to
    code = (
        "import pathlib\n"
        "pathlib.Path.resolve = lambda self, *a, **k: self\n"
        "pathlib.PurePath.relative_to = lambda self, *a, **k: self\n"
        "Path.is_absolute = lambda self: False\n"
        "type(Path('.')).resolve = lambda self, *a, **k: self\n"
        f"result_df = pd.DataFrame([read_text(r'{outside / 'private.txt'}')])\n")
    df, msg = run(code, data)
    assert refused(df, msg)
    assert pathlib.Path.resolve is real_resolve
    assert pathlib.PurePath.relative_to is real_rel
    # inside reads still work, through the real pathlib
    df2, msg2 = run("result_df = pd.read_csv('ok.csv')", data)
    assert df2 is not None, msg2


# ── listing + image/feature helpers hold under a rebound path method ──────
def test_listing_and_image_helpers_given_an_outside_path_are_refused(box):
    data, outside = box
    for code in (
        f"result_df = pd.DataFrame([count_files(r'{outside}')])",
        f"result_df = pd.DataFrame([count_folders(r'{outside}')])",
        f"result_df = pd.DataFrame(list_dir(r'{outside}'))",
        f"result_df = pd.DataFrame([image_pixel_stats(r'{outside / 'private.txt'}')])",
        f"result_df = pd.DataFrame([aggregate_image_folder(r'{outside}')])",
        f"result_df = pd.DataFrame([detect_and_count_features(r'{outside / 'private.txt'}')])",
    ):
        df, msg = run(code, data)
        assert df is None, code
        assert "TOP-SECRET" not in msg, code


def test_listing_holds_even_if_a_path_method_is_rebound(box, monkeypatch):
    """count/list work on the validated string with captured os.scandir, so
    even with the Path methods the old code relied on already rebound in the
    process they refuse an outside folder and still list inside correctly."""
    import pathlib
    data, outside = box
    monkeypatch.setattr(pathlib.Path, "resolve",
                        lambda self, *a, **k: self, raising=False)
    monkeypatch.setattr(pathlib.Path, "relative_to",
                        lambda self, *a, **k: self, raising=False)
    monkeypatch.setattr(pathlib.Path, "is_absolute",
                        lambda self: False, raising=False)
    for code in (f"result_df = pd.DataFrame([count_files(r'{outside}')])",
                 f"result_df = pd.DataFrame(list_dir(r'{outside}'))"):
        df, msg = run(code, data)
        assert df is None and "TOP-SECRET" not in msg, code
    # listing inside still returns the folder's own names
    df, msg = run("result_df = pd.DataFrame({'n': list_dir()})", data)
    assert df is not None and "ok.csv" in df.to_string(), msg
    df, msg = run("result_df = pd.DataFrame([count_files()])", data)
    assert df is not None and int(df.iloc[0, 0]) >= 2, msg


def test_image_stats_reads_an_inside_image_through_a_string(box, tmp_path):
    """image_pixel_stats, handed the sandbox's validated string, opens the
    file through its own captured io.open and returns stats — no pathlib
    object is built from the path."""
    pytest.importorskip("PIL")
    import numpy as _np
    from PIL import Image
    data, _ = box
    arr = (_np.arange(16 * 16 * 3, dtype="uint8") % 255).reshape(16, 16, 3)
    Image.fromarray(arr, "RGB").save(data / "pic.png")
    df, msg = run("result_df = pd.DataFrame([image_pixel_stats('pic.png')])", data)
    assert df is not None and "pic.png" in df.to_string(), msg
    assert "brightness" in df.columns, df.columns.tolist()


# ── legitimate alias + target code must keep working ──────────────────────
@pytest.mark.parametrize("code", [
    # df bound from pd.read_csv(...) is the model's own frame, not an alias
    "df = pd.read_csv('ok.csv')\ndf.columns = ['x', 'y']\nresult_df = df",
    # for-loop + comprehension targets that are plain names
    ("df = pd.read_csv('ok.csv')\ntot = 0\n"
     "for v in df['a']:\n    tot += v\nresult_df = pd.DataFrame([tot])"),
    ("df = pd.read_csv('ok.csv')\n"
     "vals = [v * 2 for v in df['a']]\nresult_df = pd.DataFrame({'v': vals})"),
    # with ... as f, reading inside the data folder
    ("with open('notes.txt') as f:\n    txt = f.read()\n"
     "result_df = pd.DataFrame([txt])"),
    # tuple unpacking of the model's OWN values
    ("df = pd.read_csv('ok.csv')\n"
     "lo, hi = df['a'].min(), df['a'].max()\n"
     "result_df = pd.DataFrame([{'lo': lo, 'hi': hi}])"),
    # a Path instance (not the class) used only for reading
    "p = Path(DATA_FOLDER)\nresult_df = pd.read_csv(p / 'ok.csv')",
    # a name reused: first a frame, then unpacked
    ("rows = [(1, 2), (3, 4)]\n"
     "for a, b in rows:\n    pass\nresult_df = pd.DataFrame(rows)"),
])
def test_legitimate_alias_and_target_code_still_works(box, code):
    data, _ = box
    df, msg = run(code, data)
    assert df is not None, msg


# ── Helpers return the sandbox Path copy, not the real class (2026-10-16) ──
# A helper that handed back a real pathlib.Path (list_csv_files → List[Path])
# gave model code the REAL class through type(returned): a route to rebind
# pathlib.WindowsPath.resolve that the AST check and the per-run copy aim at.
# The wrapped helpers now convert every path they return to the sandbox's own
# Path copy.
def test_the_desandbox_converter_returns_the_sandbox_path(box):
    import pathlib
    proxy, SbPath = va._make_sandbox_pathlib()
    convert = va._desandbox_path_factory(SbPath, proxy._copies)
    # deep=True: what a helper annotated as returning paths gets (nested);
    # each real class becomes its own copy (a pure path stays pure)
    got = convert([pathlib.Path("a"), pathlib.PurePath("b"),
                   {"k": pathlib.Path("c")}, "plain", 7], deep=True)
    assert isinstance(got[0], SbPath) and type(got[0]) is type(SbPath("."))
    assert type(got[1]) is type(proxy.PurePath("b"))
    assert isinstance(got[1], proxy.PurePath)
    assert isinstance(got[2]["k"], SbPath)
    assert got[3] == "plain" and got[4] == 7
    # none of them is the REAL class model code could rebind
    assert all(type(x) is not pathlib.WindowsPath
               for x in (got[0], got[1], got[2]["k"]))


def test_a_list_helper_hands_model_code_the_sandbox_path(box):
    """list_csv_files returns paths; in the sandbox they come back as the
    sandbox Path copy — isinstance of `Path`, and NOT the real class (so a
    rebind through type(returned) lands on the copy, which dies with the
    run)."""
    data, _ = box
    df, msg = run(
        "files = list_csv_files(DATA_FOLDER)\n"
        "c = type(files[0])\n"
        "result_df = pd.DataFrame([{'name': c.__name__, "
        "'inst': bool(isinstance(files[0], Path)), "
        "'is_copy': bool(c is type(Path('.')))}])", data)
    assert df is not None, msg
    row = df.iloc[0]
    assert bool(row["inst"]) is True, msg
    assert bool(row["is_copy"]) is True, msg
    assert row["name"] in ("WindowsPath", "PosixPath"), row["name"]


# ── The per-run pathlib copy keeps the relationships ordinary code needs ──
def test_the_pathlib_copy_keeps_isinstance_issubclass_name_and_repr():
    """The regressions the copy introduced (2026-10-16): the printed type name
    and repr became 'Path' not 'WindowsPath', and isinstance/issubclass among
    the copies and against real pathlib stopped holding. All restored."""
    import pathlib
    proxy, SbPath = va._make_sandbox_pathlib()
    p = SbPath(".")
    # name + repr as the real concrete class
    real_name = type(pathlib.Path()).__name__
    assert type(p).__name__ == real_name
    assert repr(p).startswith(real_name + "(")
    # isinstance against the REAL hierarchy
    assert isinstance(p, pathlib.PurePath)
    assert isinstance(p, pathlib.Path)
    assert isinstance(p, type(pathlib.Path()))            # WindowsPath / PosixPath
    assert isinstance(p, type(pathlib.PurePath()))        # Pure{Windows,Posix}Path
    # isinstance / issubclass against the COPIES model code actually gets
    assert isinstance(p, proxy.PurePath)
    assert isinstance(p, proxy.PureWindowsPath if real_name == "WindowsPath"
                      else proxy.PurePosixPath)
    assert issubclass(SbPath, proxy.PurePath)
    assert issubclass(SbPath, pathlib.PurePath)
    # closed under path operations
    assert type(p / "x") is type(p)
    assert type(p.parent) is type(p)


def test_pathlib_relationships_hold_for_model_code(box):
    """The same, as a model writes it: isinstance(Path('x'), PurePath /
    PureWindowsPath) through `from pathlib import ...`, and the repr."""
    data, _ = box
    df, msg = run(
        "from pathlib import PurePath, PureWindowsPath, WindowsPath\n"
        "p = Path(DATA_FOLDER)\n"
        "result_df = pd.DataFrame([{\n"
        "  'pure': bool(isinstance(p, PurePath)),\n"
        "  'purewin': bool(isinstance(p, PureWindowsPath)),\n"
        "  'win': bool(isinstance(p, WindowsPath)),\n"
        "  'repr': repr(Path('x')),\n"
        "}])", data)
    assert df is not None, msg
    row = df.iloc[0]
    assert bool(row["pure"]) and bool(row["purewin"]) and bool(row["win"]), msg
    assert row["repr"] == "WindowsPath('x')", row["repr"]


# ── Dunder-attribute stores are refused (2026-10-16) ──────────────────────
# A dunder store on ANY object (model's own included) is refused: __fspath__
# makes an object lie about its path, __class__ re-types it, and the object's
# own name is not a protected root, so these slipped past the rebinding check.
@pytest.mark.parametrize("code", [
    "df = pd.read_csv('ok.csv')\ndf.__class__ = int",
    "df = pd.read_csv('ok.csv')\ndf.__fspath__ = lambda: 'x'",
    "class X:\n    pass\nx = X()\nx.__fspath__ = lambda: r'C:\\x'",
    "df = pd.read_csv('ok.csv')\nsetattr(df, '__fspath__', lambda: 'x')",
    "o = object()\ng = getattr(o, '__class__')",
])
def test_dunder_attribute_stores_and_reads_are_refused(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


# ── Alias tracking follows rebinding (the MEDIUM regression) ──────────────
@pytest.mark.parametrize("code", [
    # np.nan makes `summary` look like an alias; the later groupby rebinds it
    # to the model's own frame, so summary.columns = ... must run.
    ("summary = np.nan\n"
     "df = pd.read_csv('ok.csv')\n"
     "summary = df.groupby('a').agg({'b': 'mean'})\n"
     "summary.columns = ['avg_b']\n"
     "result_df = summary.reset_index()"),
    # an alias, then rebound to a value, then a plain attribute store
    ("P = Path\n"
     "P = pd.read_csv('ok.csv')\n"
     "P.columns = ['x', 'y']\n"
     "result_df = P"),
    # an alias of np, then rebound to the model's own frame
    ("x = np\n"
     "x = pd.read_csv('ok.csv')\n"
     "x.index.name = 'i'\n"
     "result_df = x.reset_index()"),
])
def test_alias_tracking_follows_a_later_rebind_to_a_value(box, code):
    data, _ = box
    df, msg = run(code, data)
    assert df is not None, msg


@pytest.mark.parametrize("code", [
    # a branch must not CLEAR an alias: conservatively still refused
    ("P = Path\n"
     "if DATA_FOLDER:\n"
     "    P = 5\n"
     "P.resolve = lambda self, *a, **k: self"),
    # the alias is set in a branch; a store after it is refused
    ("if DATA_FOLDER:\n"
     "    Q = Path\n"
     "Q.resolve = lambda self, *a, **k: self"),
])
def test_alias_tracking_is_conservative_for_branches(code):
    ok, msg = va.validate_generated_code(code)
    assert not ok and "Forbidden" in msg, msg


# ── pandas option: through an alias, augmented, annotated (LOW-MEDIUM) ────
@pytest.fixture
def _pd_opts_restored2():
    keys = ("display.max_columns", "display.width", "display.max_rows")
    saved = {k: va.pd.get_option(k) for k in keys}
    yield
    for k, v in saved.items():
        va.pd.set_option(k, v)


@pytest.mark.parametrize("code", [
    "p = pd\np.options.display.max_columns = None",      # through an alias
    "pd.options.display.max_rows: int = 10",             # annotated
])
def test_pandas_option_through_alias_or_annotation_runs(box, _pd_opts_restored2,
                                                        code):
    data, _ = box
    df, msg = run(code + "\nresult_df = pd.read_csv('ok.csv')", data)
    assert df is not None, msg


@pytest.mark.parametrize("code", [
    "p = pd\np.options.display.max_columns = None",
    "pd.options.display.max_rows: int = 10",
    "pd.options.display.width += 0",
])
def test_pandas_option_alias_and_aug_ann_pass_the_check(code):
    ok, msg = va.validate_generated_code(code)
    assert ok, msg


# ── image_stats / feature_detect error text is plain (2026-10-16) ─────────
def test_image_helpers_error_text_names_the_file_not_an_object_repr(box):
    """A non-image file: the error names the file and the error KIND, never
    the open file object's repr (<_io.BufferedReader ...>)."""
    data, _ = box
    (data / "bad.png").write_text("not an image", encoding="utf-8")
    for helper in ("image_pixel_stats", "detect_and_count_features"):
        df, msg = run(
            f"r = {helper}('bad.png')\n"
            "result_df = pd.DataFrame([{'err': r.get('error', '')}])", data)
        assert df is not None, msg
        err = df.iloc[0]["err"]
        assert "bad.png" in err, err
        assert "BufferedReader" not in err and "_io." not in err, err
        assert "<" not in err, err


# ════════════════════════════════════════════════════════════════════════
# Review of the floor branch (2026-10-16)
# ════════════════════════════════════════════════════════════════════════

# ── Alias tracking: block by block, a function body its own scope ─────────
# The order-sensitive check handled only top-level straight-line code: every
# function body, loop, if, try and `if __name__ == '__main__':` was
# "conditional", so a later rebind there never cleared an alias, and np.nan /
# pd.NA placeholders counted as aliases. Approved/Forge tools are always
# functions, so the review's own example was still refused inside a tool.
# Each of these ran on the base (778e401).
_LEGIT_REBINDS = {
    "import_name_in_function": (
        "from scipy import stats\n"
        "def describe(d):\n"
        "    stats = d.describe()\n"
        "    stats.index.name = 'stat'\n"
        "    return stats\n"
        "result_df = describe(pd.read_csv('ok.csv'))"),
    "import_name_in_loop": (
        "from scipy import stats\n"
        "df = pd.read_csv('ok.csv')\n"
        "for col in ['a', 'b']:\n"
        "    stats = df[col].describe()\n"
        "    stats.name = col + '_stats'\n"
        "result_df = stats.to_frame()"),
    "nan_placeholder_in_if": (
        "df = pd.read_csv('ok.csv')\n"
        "table = np.nan\n"
        "if len(df):\n"
        "    table = df.pivot_table(index='a', values='b', aggfunc='sum')\n"
        "    table.columns = ['total']\n"
        "result_df = table"),
    "nan_placeholder_in_loop": (
        "best = np.nan\n"
        "df = pd.read_csv('ok.csv')\n"
        "for k, g in df.groupby('a'):\n"
        "    if g['b'].sum() > 1:\n"
        "        best = g.copy()\n"
        "best.index.name = 'row'\n"
        "result_df = best"),
    "import_name_in_try": (
        "from scipy import stats\n"
        "df = pd.read_csv('ok.csv')\n"
        "try:\n"
        "    stats = df[['a']].describe()\n"
        "    stats.index.name = 's'\n"
        "except Exception:\n"
        "    pass\n"
        "result_df = stats"),
    "import_name_under_main_guard": (
        "from scipy import stats\n"
        "df = pd.read_csv('ok.csv')\n"
        "if __name__ == '__main__':\n"
        "    stats = df[['a']].describe()\n"
        "    stats.columns = ['rt']\n"
        "    result_df = stats"),
    "pd_na_placeholder_in_if": (
        "df = pd.read_csv('ok.csv')\n"
        "top = pd.NA\n"
        "if not df.empty:\n"
        "    top = df.nlargest(2, 'a')\n"
        "    top.index = ['first', 'second']\n"
        "result_df = top"),
    "nan_placeholder_in_function": (
        "def build(df):\n"
        "    summary = np.nan\n"
        "    summary = df.groupby('a')['b'].agg(['mean', 'sum'])\n"
        "    summary.columns = ['avg', 'tot']\n"
        "    return summary\n"
        "result_df = build(pd.read_csv('ok.csv'))"),
    "import_name_in_while": (
        "from scipy import stats\n"
        "df = pd.read_csv('ok.csv')\n"
        "n = 0\n"
        "while n < 2:\n"
        "    stats = df.describe()\n"
        "    stats.index.name = 's'\n"
        "    n += 1\n"
        "result_df = df"),
    "nested_function": (
        "from scipy import stats\n"
        "def outer(d):\n"
        "    def inner(x):\n"
        "        stats = x.describe()\n"
        "        stats.index.name = 'i'\n"
        "        return stats\n"
        "    return inner(d)\n"
        "result_df = outer(pd.read_csv('ok.csv'))"),
    # the controls the review found unchanged, kept that way
    "with_block": (
        "from scipy import stats\n"
        "with open('ok.csv') as f:\n"
        "    stats = pd.read_csv(f).describe()\n"
        "    stats.index.name = 'stat'\n"
        "result_df = stats"),
    # a parameter is a local: it can never be the module-level import
    "param_shadows_import": (
        "from scipy import stats\n"
        "def label(stats):\n"
        "    stats.name = 'x'\n"
        "    return stats\n"
        "result_df = label(pd.read_csv('ok.csv')['a']).to_frame()"),
}


def test_the_sandbox_s_own_names_stay_protected_even_as_locals():
    """pd, np, Path, math, … are protected everywhere — a local of the same
    name included, as on the base — and the message says it is the name."""
    ok, msg = va.validate_generated_code(
        "import math\n"
        "def summarize(d):\n"
        "    math = d.agg(['sum', 'mean'])\n"
        "    math.name = 'agg'\n"
        "    return math\n")
    assert not ok and "Forbidden: cannot assign to 'name' on 'math'" in msg
    assert "one of the modules and classes the sandbox provides" in msg, msg
    assert "different name" in msg, msg


@pytest.mark.parametrize("name", sorted(_LEGIT_REBINDS))
def test_a_rebind_in_a_block_or_function_clears_the_alias(box, name):
    data, _ = box
    code = _LEGIT_REBINDS[name]
    ok, why = va.validate_generated_code(code)
    assert ok, why
    df, msg = run(code, data)
    assert df is not None, msg


_TOOL_REBINDS = {
    "tool_build": ("def tool_build(by='a'):\n"
                   "    summary = np.nan\n"
                   "    df = pd.read_csv('ok.csv')\n"
                   "    summary = df.groupby(by)['b'].agg(['mean', 'sum'])\n"
                   "    summary.columns = ['avg', 'tot']\n"
                   "    return summary.reset_index()\n", {"by": "a"},
                   ["a", "avg", "tot"]),
    "tool_desc": ("from scipy import stats\n"
                  "def tool_desc(column='a'):\n"
                  "    stats = pd.read_csv('ok.csv')[column].describe()\n"
                  "    stats.name = column\n"
                  "    return stats.to_frame()\n", {"column": "b"}, ["b"]),
}


@pytest.mark.parametrize("entry", sorted(_TOOL_REBINDS))
def test_an_approved_tool_with_a_rebind_saves_and_runs(box, entry):
    """Every approved/Forge tool is a function: a rebind inside it is
    accepted at save time (app_built_tools.check_tool, the validator
    save_tool uses) and runs in the analyst child."""
    import app_built_tools as abt
    from council_core import analyst_step
    data, _ = box
    code, args, cols = _TOOL_REBINDS[entry]
    ok, why, _slug, got_entry = abt.check_tool(entry, code)
    assert ok and got_entry == entry, why
    res = analyst_step.run_tool_code(code, entry, args, [data])
    assert res.ran and res.df is not None, res.message
    assert list(res.df.columns) == cols, res.df.columns.tolist()


# What a block / scope walk must STILL refuse — an alias that may be live
# at the store on SOME path.
_STILL_REFUSED = {
    "free_name_in_function": (
        "P = Path\n"
        "def f():\n"
        "    P.resolve = lambda self, *a, **k: self\n"
        "f()"),
    "alias_made_after_the_def": (
        "P = 1\n"
        "def f():\n"
        "    P.resolve = lambda self, *a, **k: self\n"
        "P = Path\n"
        "f()"),
    "loop_back_edge": (
        "x = 1\n"
        "for i in range(2):\n"
        "    x.resolve = lambda self, *a, **k: self\n"
        "    x = Path"),
    "loop_continue_path": (
        "x = 1\n"
        "for i in range(3):\n"
        "    x.resolve = lambda self, *a, **k: self\n"
        "    x = Path\n"
        "    if i < 5:\n"
        "        continue\n"
        "    x = 2"),
    "while_back_edge": (
        "x = 1\n"
        "n = 0\n"
        "while n < 2:\n"
        "    x.resolve = lambda self, *a, **k: self\n"
        "    x = Path\n"
        "    n += 1"),
    "try_clear_skipped_by_an_exception": (
        "P = Path\n"
        "try:\n"
        "    1 / 0\n"
        "    P = 1\n"
        "except Exception:\n"
        "    pass\n"
        "P.resolve = lambda self, *a, **k: self"),
    "with_clear_skipped_by_a_suppressing_manager": (
        "class S:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *a):\n"
        "        return True\n"
        "P = Path\n"
        "with S():\n"
        "    1 / 0\n"
        "    P = 1\n"
        "P.resolve = lambda self, *a, **k: self"),
    "global_from_a_function": (
        "P = 1\n"
        "def g():\n"
        "    global P\n"
        "    P = Path\n"
        "g()\n"
        "P.resolve = lambda self, *a, **k: self"),
    "enclosing_function_alias": (
        "def outer():\n"
        "    P = Path\n"
        "    def inner():\n"
        "        P.resolve = lambda self, *a, **k: self\n"
        "    inner()\n"
        "outer()"),
    "walrus_in_a_short_circuit_does_not_clear": (
        "P = Path\n"
        "z = 0 and (P := 1)\n"
        "P.resolve = lambda self, *a, **k: self"),
    "loop_target_from_a_literal": (
        "for P in [Path]:\n"
        "    P.resolve = lambda self, *a, **k: self"),
    "module_attribute_is_still_an_alias": (
        "R = np.random\n"
        "R.seed = None"),
    "import_rebound_only_in_a_branch": (
        "from scipy import stats\n"
        "if DATA_FOLDER:\n"
        "    stats = pd.read_csv('ok.csv')\n"
        "stats.name = 'x'"),
}


@pytest.mark.parametrize("name", sorted(_STILL_REFUSED))
def test_a_possibly_live_alias_is_still_refused(name):
    ok, msg = va.validate_generated_code(_STILL_REFUSED[name])
    assert not ok and "Forbidden: cannot assign to" in msg, (name, msg)


def test_the_refusal_says_why_the_name_still_counts_and_what_to_do():
    """`stats` IS the model's own variable on one path, so 'assign only to
    your own variables' alone misled: the message names the line that made
    it an alias and suggests a name of its own."""
    ok, msg = va.validate_generated_code(
        _STILL_REFUSED["import_rebound_only_in_a_branch"])
    assert not ok
    assert "'stats'" in msg and "line 1" in msg, msg
    assert "from scipy import stats" in msg, msg
    assert "different name" in msg, msg


# ── The return converter does not walk or copy what helpers return ───────
def test_the_return_converter_leaves_documents_alone_and_fast():
    """It walked and COPIED every list/dict a helper returned (depth 4), so a
    big read_json_documents result took about twice as long and could pass
    the memory cap. A result with no path in it is now handed back as the
    very same object."""
    import time
    _proxy, sb_path = va._make_sandbox_pathlib()
    conv = va._desandbox_path_factory(sb_path)
    docs = [{"id": i, "m": {"y": [i, i + 1]}} for i in range(200_000)]
    t0 = time.perf_counter()
    out = conv(docs)
    took = time.perf_counter() - t0
    assert out is docs                       # no second copy
    assert took < 0.5, took
    ints = list(range(1_000_000))
    assert conv(ints) is ints


def test_the_return_converter_still_converts_paths_it_is_handed():
    import pathlib
    _proxy, sb_path = va._make_sandbox_pathlib()
    conv = va._desandbox_path_factory(sb_path)
    got = conv([pathlib.Path("a"), "b"])
    assert isinstance(got[0], sb_path) and got[1] == "b"
    assert isinstance(conv(pathlib.Path("x")), sb_path)
    got = conv({"file": pathlib.Path("a"), "n": 1})
    assert isinstance(got["file"], sb_path) and got["n"] == 1


def test_list_helpers_still_hand_back_the_sandbox_path(box):
    data, _ = box
    for lister in ("list_csv_files", "list_data_files"):
        df, msg = run(
            f"files = {lister}(DATA_FOLDER)\n"
            "result_df = pd.DataFrame([{'copy': all(type(f) is "
            "type(Path('.')) for f in files), 'n': len(files)}])", data)
        assert df is not None, msg
        assert bool(df.iloc[0]["copy"]) and int(df.iloc[0]["n"]) >= 1, lister


# ── The pathlib copy answers as real pathlib does ─────────────────────────
_PATHLIB_PROBE = (
    "import pathlib\n"
    "from pathlib import (PurePath, PureWindowsPath, PurePosixPath,\n"
    "                     WindowsPath, PosixPath)\n"
    "x = Path('x.csv')\n"
    "rows = [\n"
    "  isinstance(x, PurePath), isinstance(x, pathlib.PurePath),\n"
    "  isinstance(x, PureWindowsPath), isinstance(x, WindowsPath),\n"
    "  isinstance(x, pathlib.Path), isinstance(x, Path),\n"
    "  issubclass(Path, PurePath), issubclass(type(x), pathlib.PurePath),\n"
    "  issubclass(WindowsPath, pathlib.Path), issubclass(PosixPath, Path),\n"
    "  type(x).__name__, repr(x), str(x),\n"
    "  isinstance(PurePosixPath('a/b'), PurePath),\n"
    "  isinstance(PureWindowsPath('a/b'), PurePath),\n"
    "  isinstance(PurePath('a'), PurePath), isinstance(PurePath('a'), Path),\n"
    "  PurePath('a/b').parts, PurePosixPath('a/b').as_posix(),\n"
    "  Path.__name__, repr(Path), PurePath.__name__, repr(PurePath),\n"
    "  pathlib.PurePath.__name__, WindowsPath.__name__,\n"
    "  type(Path('a')) is Path, type(Path('a')).__qualname__,\n"
    "  type(PurePath('a')).__name__, type(Path('a') / 'b').__name__,\n"
    "  Path is pathlib.Path, PurePath is pathlib.PurePath,\n"
    "  Path('a') == Path('a'), Path('A') == Path('a'),\n"
    "  sorted([Path('b'), Path('a')]) == [Path('a'), Path('b')],\n"
    "  {Path('a'): 1}[Path('a')],\n"
    "]\n"
    "result_df = pd.DataFrame({'v': [repr(r) for r in rows]})\n")


def test_the_pathlib_copy_answers_like_real_pathlib(box):
    """The copy's class names were the concrete ones ('WindowsPath' for
    Path, 'PureWindowsPath' for PurePath) and PurePosixPath('a/b') was not a
    PurePath. Every answer here now matches real pathlib."""
    import os as _os
    import pathlib
    if _os.name != "nt":
        pytest.skip("the probe names the Windows classes")
    data, _ = box
    df, msg = run(_PATHLIB_PROBE, data)
    assert df is not None, msg
    got = df["v"].tolist()
    import pandas as _pd
    ns = {"Path": pathlib.Path, "pd": _pd}
    exec(_PATHLIB_PROBE, ns)                    # real pathlib, outside
    want = ns["result_df"]["v"].tolist()
    assert got == want, [(i, g, w) for i, (g, w) in
                         enumerate(zip(got, want)) if g != w]


def test_the_pathlib_copy_is_still_a_copy():
    """…while every class model code gets is still the per-run copy, so a
    rebind lands there and not on real pathlib."""
    import pathlib
    proxy, sb_path = va._make_sandbox_pathlib()
    for nm in ("Path", "PurePath", "WindowsPath", "PureWindowsPath",
               "PurePosixPath", "PosixPath"):
        assert getattr(proxy, nm) is not getattr(pathlib, nm), nm
        assert issubclass(getattr(proxy, nm), getattr(pathlib, nm)), nm
    assert proxy.Path is sb_path
    p = sb_path("x")
    assert isinstance(p, sb_path)
    assert type(p) is getattr(proxy, type(pathlib.Path()).__name__)
    assert type(p / "y") is type(p) and type(p.parent) is type(p)
    real = pathlib.Path.resolve
    sb_path.resolve = lambda self, *a, **k: "HACK"
    assert pathlib.Path.resolve is real
    assert p.resolve() == "HACK"            # the copy's own instances see it


# ── Image helper errors say what went wrong ───────────────────────────────
def test_image_helper_errors_keep_the_reason_without_the_object_repr(box):
    """'could not open image bad.png (UnidentifiedImageError)' dropped the
    reason. The reason stays — with the open file's repr and the full path
    replaced by the file's name."""
    pytest.importorskip("PIL")
    import io as _io
    import numpy as _np
    from PIL import Image
    data, _ = box
    (data / "bad.png").write_text("not an image", encoding="utf-8")
    buf = _io.BytesIO()
    Image.fromarray((_np.arange(64 * 64 * 3) % 251).astype("uint8")
                    .reshape(64, 64, 3), "RGB").save(buf, "PNG")
    (data / "cut.png").write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    for helper in ("image_pixel_stats", "detect_and_count_features"):
        df, msg = run(
            f"a = {helper}('bad.png')\nb = {helper}('cut.png')\n"
            "result_df = pd.DataFrame([{'a': a.get('error', ''), "
            "'b': b.get('error', '')}])", data)
        assert df is not None, msg
        a, b = df.iloc[0]["a"], df.iloc[0]["b"]
        assert "bad.png" in a and "cannot identify image file" in a, a
        assert "cut.png" in b and "truncated" in b, b
        for err in (a, b):
            assert "<" not in err and "BufferedReader" not in err, err
            assert str(data) not in err, err
