"""
council_core.frame_codec — a result table as plain JSON and back, exactly.

A model-built tool's, an agent's or a job's result comes back from the
sandbox's process as JSON (no pickle). Through pandas' own "split" JSON it
did not come back as it was (measured 2026-10-15): a float was rounded to 10
digits (1.2345678901234e-12 read back as 0.0, 1/3 as 0.33333333330000003),
column labels "001" and "2024" became the numbers 1 and 2024, a Timestamp
or bytes became text, a tuple a list, a complex number {"real", "imag"}, a
timedelta column ISO text, and a Period could not be written at all. Each
case here is a value a tool returned in the app's own process, as they all
ran before then: what comes back must be that value.

The reader is a trust boundary — the report is written by the model code's
own process — so the last tests feed it damaged and hostile input.
"""
from __future__ import annotations

import datetime as dt
import decimal
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import frame_codec as fc  # noqa: E402


def round_trip(df, max_rows=None):
    """Through the JSON text the child writes and the app reads."""
    return fc.decode(json.loads(json.dumps(fc.encode(df, max_rows))))


def one(value):
    """The one-cell frame a tool call makes of a returned value."""
    return pd.DataFrame([{"result": value}])


FRAMES = {
    "floats": pd.DataFrame({"x": [1 / 3, 3.141592653589793,
                                  1.2345678901234e-12, 123456789.123456789,
                                  0.1 + 0.2, float("nan"), float("inf")]}),
    "numeric-text-labels": pd.DataFrame({"001": [1], "2024": [2]},
                                        index=["007"]),
    "int64-and-uint64": pd.DataFrame({"n": [2 ** 62 + 1, 3],
                                      "u": np.array([2 ** 64 - 1, 0],
                                                    dtype="uint64")}),
    "float32": pd.DataFrame({"f": np.array([1 / 3], dtype="float32")}),
    "bool": pd.DataFrame({"b": [True, False]}),
    "text-and-missing": pd.DataFrame({"s": ["a", None, "007"]}),
    "object-mix": pd.DataFrame({"o": pd.Series(
        [1, "a", None, float("nan"), [1], {"a": 1}, np.float64(1.5),
         np.int64(3)], dtype=object)}),
    "nullable": pd.DataFrame({"i": pd.array([1, None], dtype="Int64"),
                              "b": pd.array([True, None],
                                            dtype="boolean")}),
    "complex-column": pd.DataFrame({"z": [complex(1, 2), complex(0, -1)]}),
    "timedelta-column": pd.DataFrame({"d": pd.to_timedelta([1, 2],
                                                           unit="s")}),
    "timestamps-and-nat": pd.DataFrame({"t": [pd.Timestamp("2026-01-01"),
                                              pd.NaT]}),
    "time-zone": pd.DataFrame({"t": pd.date_range(
        "2026-01-01", periods=3, freq="h", tz="Europe/Berlin")}),
    "date-index-with-freq": pd.DataFrame(
        {"v": [1, 2, 3]}, index=pd.date_range("2026-01-01", periods=3,
                                              freq="D")),
    "periods": pd.DataFrame({"p": pd.period_range("2026-01", periods=2,
                                                  freq="M")}),
    "intervals": pd.DataFrame({"i": pd.IntervalIndex.from_breaks([0, 1, 2])}),
    "category": pd.DataFrame({"c": pd.Categorical(
        ["b", "a", "b"], categories=["b", "a", "z"], ordered=True)}),
    "multi-index-columns": pd.DataFrame(
        {"g": ["a", "b"], "x": [1, 2], "y": [3, 4]}).groupby("g").agg(
            ["sum", "mean"]),
    "duplicate-columns": pd.DataFrame([[1, 2]], columns=["a", "a"]),
    "integer-labels": pd.DataFrame(np.zeros((2, 2))),
    "mixed-index": pd.DataFrame({"a": [1, 2]}, index=[1, "x"]),
    "python-datetimes": pd.DataFrame({"d": pd.Series(
        [dt.datetime(2026, 1, 1, 12, 30)], dtype=object)}),
    "empty": pd.DataFrame(),
    "rows-no-columns": pd.DataFrame(index=range(3)),
    # one returned value, as a tool call wraps it
    "text-007": one("007"),
    "timestamp": one(pd.Timestamp("2026-10-15 12:00")),
    "timestamp-ns": one(pd.Timestamp("2026-10-15 12:00:00.000000001")),
    "numpy-datetime64": one(np.datetime64("2026-10-15T12:00:00.000000000")),
    "bytes": one(b"abc"),
    "tuple": one((1, 2)),
    "complex": one(complex(1, 2)),
    "period": one(pd.Period("2026-10", freq="M")),
    "weekly-period": one(pd.Period("2026-10-14", freq="W-SUN")),
    "set": one({1, 2}),
    "list": one([1, "two", {"k": 3}]),
    "nested": one({"k": [1, (2, 3)], 5: {"z": None}}),
    "date": one(dt.date(2026, 10, 15)),
    "time": one(dt.time(12, 30, 1, 5)),
    "timedelta": one(dt.timedelta(days=1, seconds=2)),
    "decimal": one(decimal.Decimal("1.10")),
    "interval": one(pd.Interval(0, 1, closed="left")),
    "none": one(None),
    "array": one(np.arange(6).reshape(2, 3)),
    "float-array": one(np.array([1 / 3, 1e-12])),
    "date-array": one(np.array(["2026-01-01", "2026-01-02"],
                               dtype="datetime64[D]")),
    "text-array": one(np.array(["ab", "c"])),
    "series-in-a-cell": one(pd.Series([1, 2], name="s")),
    "table-in-a-cell": one(pd.DataFrame({"a": [1.2345678901234e-12]})),
}


def _same(a, b):
    if isinstance(a, np.ndarray):
        return isinstance(b, np.ndarray) and a.dtype == b.dtype \
            and np.array_equal(a, b)
    if isinstance(a, (pd.Series, pd.DataFrame)):
        return type(a) is type(b) and a.equals(b)
    return type(a) is type(b) and a == b


@pytest.mark.parametrize("name", list(FRAMES))
def test_a_table_comes_back_as_it_was(name):
    df = FRAMES[name]
    back = round_trip(df)
    pd.testing.assert_frame_equal(back, df)
    if list(df.columns) == ["result"] and len(df) == 1:
        # the returned value itself: its type and value, not only equal
        assert _same(df["result"].iloc[0], back["result"].iloc[0]), (
            df["result"].iloc[0], back["result"].iloc[0])


def test_floats_are_exact_where_pandas_json_rounded_them():
    """The measured cases: 1e-12 read back as 0.0, 1/3 as
    0.33333333330000003, 2.123456789012345 as 2.123456789."""
    df = pd.DataFrame({"eps": [1.2345678901234e-12], "third": [1 / 3],
                       "b": [2.123456789012345]})
    lossy = pd.read_json(io.StringIO(df.to_json(orient="split")),
                         orient="split")
    assert lossy["eps"][0] == 0.0                     # what it did
    back = round_trip(df)
    assert back["eps"][0] == 1.2345678901234e-12
    assert back["third"][0] == 1 / 3
    assert back["b"][0] == 2.123456789012345


def test_labels_like_001_stay_text():
    back = round_trip(pd.DataFrame({"001": [1], "2024": [2]}))
    assert list(back.columns) == ["001", "2024"]
    assert back["001"][0] == 1                       # df['001'] still works


def test_only_the_first_rows_come_back_with_the_full_shape():
    df = pd.DataFrame({"i": range(10_000)})
    enc = json.loads(json.dumps(fc.encode(df)))
    assert fc.shape(enc) == (10_000, 1)
    assert len(fc.decode(enc)) == fc.MAX_ROWS


def test_a_value_it_has_no_type_for_comes_back_as_its_text():
    class Odd:
        def __str__(self):
            return "odd one"

    class Broken:
        def __str__(self):
            raise RuntimeError("no")

    loop = []
    loop.append(loop)
    back = round_trip(pd.DataFrame({"o": pd.Series(
        [Odd(), Broken(), loop], dtype=object)}))
    assert back["o"].tolist()[:2] == ["odd one", "<Broken>"]
    assert isinstance(back["o"][2], list)            # cut to text, deep in


# ============================================================
# Damaged or hostile input: refused, nothing built from it
# ============================================================

GOOD = fc.encode(pd.DataFrame({"a": [1.5, 2.5]}))


def _with(**changes):
    obj = json.loads(json.dumps(GOOD))
    obj.update(changes)
    return obj


@pytest.mark.parametrize("bad", [
    None, [], "text", {"format": "something-else"},
    _with(data="not a list"),
    _with(data=[{"t": "float64", "v": [1.0]}]),            # wrong length
    _with(data=[{"t": "object", "v": [{"$": "pickle", "v": "gASV"},
                                      1]}]),                # unknown tag
    _with(data=[{"t": "object", "v": [{"$": "np", "t": "V99", "v": 1},
                                      1]}]),                # raw-bytes dtype
    _with(index={"range": ["0", 2, 1], "name": None}),
    _with(columns={"multi": [[1]], "names": [None]}),
], ids=lambda b: str(b)[:40])
def test_damaged_input_is_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        fc.decode(bad)


def test_a_huge_text_width_is_not_allocated():
    """A forged fixed-width text array would be a gigabytes allocation in
    the app: it is rebuilt at the width its values need."""
    t0 = time.monotonic()
    arr = fc._decode_value({"$": "ndarray", "t": "<U999999999", "s": [1],
                            "v": ["a"]})
    assert arr.dtype.itemsize < 100 and arr.tolist() == ["a"]
    assert time.monotonic() - t0 < 5


def test_a_time_zone_name_is_never_a_file_path(monkeypatch):
    """pandas reads a "dateutil/<path>" zone from that file: such a name
    is refused, never handed to it."""
    seen = []
    real = pd.Timestamp.tz_convert
    monkeypatch.setattr(pd.Timestamp, "tz_convert",
                        lambda self, tz: seen.append(tz) or real(self, tz))
    for tz in ("dateutil//etc/passwd", "dateutil/C:/x", "../x", "/etc/x",
               "C:\\x"):
        with pytest.raises(ValueError):
            fc._decode_value({"$": "ts", "v": "2026-01-01T00:00:00+00:00",
                              "tz": tz})
        frame = _with(data=[{"t": f"datetime64[ns, {tz}]",
                             "v": [None, None]}])
        assert fc.decode(frame)["a"].dtype == object   # not that dtype
    assert seen == []
    fc._decode_value({"$": "ts", "v": "2026-01-01T00:00:00+00:00",
                      "tz": "Europe/Berlin"})
    assert seen == ["Europe/Berlin"]


def test_too_large_says_so_plainly():
    assert fc.too_large(85 * 2 ** 20).startswith(
        "the result was too large to bring back (85 MB; at most "
        f"{fc.MAX_RESULT_MB} MB)")
