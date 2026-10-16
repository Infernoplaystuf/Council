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
    assert type(SbPath(".")) is SbPath            # closed under construction
    assert type(SbPath(".") / "x") is SbPath
    assert type(SbPath(".").parent) is SbPath
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
