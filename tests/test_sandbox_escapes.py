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
