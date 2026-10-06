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
