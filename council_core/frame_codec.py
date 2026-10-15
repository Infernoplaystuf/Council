"""
council_core.frame_codec — a result table as plain JSON and back, exactly.

A model-built tool, an agent's run_pandas_analysis and a background job's
code run in a child process (council_core.analyst_child), and the table
they produce comes back to the app (council_core.analyst_step) as JSON —
never pickle. pandas' own JSON ("split") is not exact: it rounds a float to
10 significant digits (1.2345678901234e-12 came back as 0.0, 1/3 as
0.33333333330000003), reads a column label "001" back as the number 1, a
timestamp or bytes back as text, a tuple as a list and a complex number as
{"real", "imag"}, and cannot write a Period at all (measured 2026-10-15) —
while in the app's own process, as these ran before then, the table was
the code's own object.

encode() writes each column with its dtype, a float as json writes it
(repr, which reads back as the same float), and every value json has no
type for as a small tagged object ({"$": "ts", ...}). decode() rebuilds the
frame: the same labels, index, dtypes and values. A value of a type not
named here comes back as its text (str), so a result is never lost over
one odd cell.

decode() reads what the app did not write — the child is the model code's
own process, and that code can change what it reports — so it builds only
the types named here, from plain values, and raises ValueError (or
TypeError) for anything else. Nothing is unpickled, evaluated or imported
by name.
"""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import re
from typing import Any, Dict, List, Optional

#: What encode() writes; decode() reads nothing else.
FORMAT = "council-frame-1"
#: Rows of a result that come back to the app; the full shape always does.
MAX_ROWS = 5000
#: The largest result (the child's whole report, MB) that comes back. A
#: bigger one is refused in plain words — the app would hold it several
#: times over while reading it (an 80 MB returned value raised the app's
#: peak memory by ~320-400 MB, measured 2026-10-15).
MAX_RESULT_MB = 32
#: Nested lists, dicts and tables deeper than this are written as text.
MAX_DEPTH = 40

#: numpy kinds a column, a scalar or an array may be rebuilt as: bool,
#: integers, floats, complex, timedelta, datetime. Never "V" (raw bytes of
#: any size) or a fixed-width text dtype (a forged width is a huge
#: allocation in the app).
_NUMPY_KINDS = "biufcmM"
#: How big a fixed-width text array may be rebuilt (bytes); bigger is
#: rebuilt at the width its values need.
_MAX_TEXT_ARRAY = 16 * 1024 * 1024

_CLOSED = ("left", "right", "both", "neither")
_UNITS = ("s", "ms", "us", "ns")
_NAME = re.compile(r"[A-Za-z0-9_+\-:/\[\], ]{1,80}")
#: A fixed-width text array's dtype ("<U5", "|S3").
_TEXT_DTYPE = re.compile(r"[<>|=][US][0-9]{1,9}")


def _safe_name(text: Any) -> bool:
    """A dtype, time zone or frequency name the app may hand to pandas: a
    plain name, never a path (pandas reads a "dateutil/…" zone from a
    file)."""
    return (isinstance(text, str) and bool(_NAME.fullmatch(text))
            and "dateutil" not in text and ".." not in text
            and "//" not in text and not text.startswith("/")
            and ", /" not in text)


# ============================================================
# Writing (in the child)
# ============================================================

def _text(x: Any) -> str:
    try:
        return str(x)
    except Exception:                                     # noqa: BLE001
        return f"<{type(x).__name__}>"


def _value(x: Any, depth: int = 0) -> Any:
    """One value as plain JSON: itself when json has its type, else a
    tagged object, else its text."""
    import numpy as np
    import pandas as pd
    if x is None or type(x) in (bool, str, int, float):
        return x
    try:
        if x is pd.NaT:
            return {"$": "nat"}
        if x is pd.NA:
            return {"$": "na"}
        if isinstance(x, np.generic):         # before float: np.float64 is one
            kind, name = x.dtype.kind, x.dtype.name
            if kind == "b":
                return {"$": "np", "t": name, "v": bool(x)}
            if kind in "iu":
                return {"$": "np", "t": name, "v": int(x)}
            if kind == "f":
                return {"$": "np", "t": name, "v": float(x)}
            if kind == "c":
                return {"$": "np", "t": name,
                        "v": [float(x.real), float(x.imag)]}
            if kind in "mM":
                return {"$": "np", "t": name, "v": int(x.view("i8"))}
            if isinstance(x, np.bytes_):
                return _value(bytes(x), depth)
            return _text(x)                   # np.str_ is text
        if isinstance(x, (bool, str)):
            return bool(x) if isinstance(x, bool) else str.__str__(x)
        if isinstance(x, int):
            return int(x)
        if isinstance(x, float):
            return float(x)
        if isinstance(x, pd.Timestamp):
            out = {"$": "ts", "v": x.isoformat()}
            if x.tz is not None:
                out["tz"] = str(x.tz)
            if getattr(x, "unit", None):
                out["u"] = x.unit
            return out
        if isinstance(x, pd.Timedelta):
            out = {"$": "td", "v": int(x.value)}
            if getattr(x, "unit", None):
                out["u"] = x.unit
            return out
        if isinstance(x, _dt.datetime):
            return {"$": "datetime", "v": x.isoformat()}
        if isinstance(x, _dt.date):
            return {"$": "date", "v": x.isoformat()}
        if isinstance(x, _dt.time):
            return {"$": "time", "v": x.isoformat()}
        if isinstance(x, _dt.timedelta):
            return {"$": "timedelta",
                    "v": [x.days, x.seconds, x.microseconds]}
        if isinstance(x, pd.Period):
            return {"$": "period", "o": int(x.ordinal), "f": x.freqstr}
        if isinstance(x, pd.Interval):
            return {"$": "interval", "l": _value(x.left, depth + 1),
                    "r": _value(x.right, depth + 1), "c": x.closed}
        if isinstance(x, complex):
            return {"$": "complex", "v": [x.real, x.imag]}
        if isinstance(x, (bytes, bytearray)):
            return {"$": "bytearray" if isinstance(x, bytearray) else "bytes",
                    "v": base64.b64encode(bytes(x)).decode("ascii")}
        if isinstance(x, decimal.Decimal):
            return {"$": "decimal", "v": str(x)}
        if depth >= MAX_DEPTH:
            return _text(x)
        if isinstance(x, list):
            return [_value(v, depth + 1) for v in x]
        if isinstance(x, tuple):
            return {"$": "tuple", "v": [_value(v, depth + 1) for v in x]}
        if isinstance(x, (set, frozenset)):
            return {"$": "frozenset" if isinstance(x, frozenset) else "set",
                    "v": [_value(v, depth + 1) for v in x]}
        if isinstance(x, dict):
            return {"$": "dict", "v": [[_value(k, depth + 1),
                                        _value(v, depth + 1)]
                                       for k, v in x.items()]}
        if isinstance(x, np.ndarray):
            return _array(x, depth)
        if isinstance(x, pd.DataFrame):
            return {"$": "frame", "v": encode(x, depth=depth + 1)}
        if isinstance(x, pd.Series):
            return {"$": "series", "n": _value(x.name, depth + 1),
                    "v": encode(x.to_frame(name=0), depth=depth + 1)}
    except MemoryError:
        raise
    except Exception:                                     # noqa: BLE001
        pass
    return _text(x)


def _array(x: Any, depth: int) -> Dict[str, Any]:
    kind = x.dtype.kind
    if kind in "mM":
        values: List[Any] = x.view("i8").ravel().tolist()
    else:
        values = [_value(v, depth + 1) for v in x.ravel().tolist()]
    name = (x.dtype.name if kind in _NUMPY_KINDS
            else x.dtype.str if kind in "US" else "object")
    return {"$": "ndarray", "t": name, "s": list(x.shape), "v": values}


def _index(idx: Any, depth: int) -> Dict[str, Any]:
    import pandas as pd
    if isinstance(idx, pd.RangeIndex):
        return {"range": [int(idx.start), int(idx.stop), int(idx.step)],
                "name": _value(idx.name, depth + 1)}
    if isinstance(idx, pd.MultiIndex):
        return {"multi": [_value(tuple(t), depth + 1) for t in idx.tolist()],
                "names": [_value(n, depth + 1) for n in idx.names]}
    out = _column(idx, depth)
    out["name"] = _value(idx.name, depth + 1)
    freq = getattr(idx, "freqstr", None)
    if freq:
        out["f"] = freq
    return out


def _column(values: Any, depth: int) -> Dict[str, Any]:
    """A Series' or an Index's values with their dtype."""
    import numpy as np
    import pandas as pd
    dtype = values.dtype
    out: Dict[str, Any] = {"t": str(dtype)}
    if isinstance(dtype, np.dtype) and dtype.kind in "biuf":
        # Python bools, ints and floats: json writes each exactly.
        out["v"] = np.asarray(values).tolist()
        return out
    if isinstance(dtype, pd.CategoricalDtype):
        out["cat"] = _index(dtype.categories, depth + 1)
        out["ordered"] = bool(dtype.ordered)
    out["v"] = [_value(v, depth + 1) for v in values.tolist()]
    return out


def encode(df: Any, max_rows: Optional[int] = MAX_ROWS, *,
           depth: int = 0) -> Dict[str, Any]:
    """`df` (its first `max_rows` rows; None for all) as plain JSON:
    {"format", "shape" (the full [rows, cols]), "index", "columns",
    "data" (one {"t": dtype, "v": values} per column)}."""
    head = df if max_rows is None else df.head(max_rows)
    return {"format": FORMAT,
            "shape": [int(df.shape[0]), int(df.shape[1])],
            "index": _index(head.index, depth),
            "columns": _index(df.columns, depth),
            "data": [_column(head.iloc[:, i], depth)
                     for i in range(head.shape[1])]}


# ============================================================
# Reading (in the app)
# ============================================================

def _need(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(f"the result is damaged ({what})")


def _int(v: Any) -> int:
    _need(isinstance(v, int) and not isinstance(v, bool), "not a whole number")
    return v


def _num(v: Any) -> float:
    _need(isinstance(v, (int, float)) and not isinstance(v, bool),
          "not a number")
    return float(v)


def _str(v: Any) -> str:
    _need(isinstance(v, str), "not text")
    return v


def _np_dtype(name: Any, kinds: str = _NUMPY_KINDS):
    import numpy as np
    _need(_safe_name(name), "a dtype name")
    dtype = np.dtype(name)
    _need(dtype.kind in kinds and dtype.shape == (), f"dtype {name}")
    return dtype


def _objects(values: List[Any]):
    """An object array holding exactly these values (numpy would otherwise
    turn a run of equal-length lists into a 2-D array)."""
    import numpy as np
    arr = np.empty(len(values), dtype=object)
    for i, v in enumerate(values):
        arr[i] = v
    return arr


def _d_np(v, depth):
    import numpy as np
    dtype = _np_dtype(v.get("t"))
    x = v.get("v")
    if dtype.kind == "b":
        _need(isinstance(x, bool), "a bool")
        return np.bool_(x)
    if dtype.kind in "iu":
        return dtype.type(_int(x))
    if dtype.kind == "f":
        return dtype.type(_num(x))
    if dtype.kind == "c":
        _need(isinstance(x, list) and len(x) == 2, "a complex number")
        return dtype.type(complex(_num(x[0]), _num(x[1])))
    return np.int64(_int(x)).view(dtype)


def _tz(ts, v):
    tz = v.get("tz")
    if tz is None:
        return ts
    _need(_safe_name(tz), "a time zone")
    try:
        return ts.tz_convert(tz)
    except Exception:                                     # noqa: BLE001
        return ts                      # keeps the written UTC offset


def _unit(x, v):
    u = v.get("u")
    if u in _UNITS and hasattr(x, "as_unit"):
        try:
            return x.as_unit(u)
        except Exception:                                 # noqa: BLE001
            return x
    return x


def _d_ts(v, depth):
    import pandas as pd
    return _unit(_tz(pd.Timestamp(_str(v.get("v"))), v), v)


def _d_td(v, depth):
    import pandas as pd
    return _unit(pd.Timedelta(_int(v.get("v")), unit="ns"), v)


def _d_timedelta(v, depth):
    x = v.get("v")
    _need(isinstance(x, list) and len(x) == 3, "a timedelta")
    return _dt.timedelta(days=_int(x[0]), seconds=_int(x[1]),
                         microseconds=_int(x[2]))


def _d_period(v, depth):
    import pandas as pd
    freq = v.get("f")
    _need(_safe_name(freq), "a period's frequency")
    return pd.Period(ordinal=_int(v.get("o")), freq=freq)


def _d_interval(v, depth):
    import pandas as pd
    closed = v.get("c")
    _need(closed in _CLOSED, "an interval")
    return pd.Interval(_decode_value(v.get("l"), depth + 1),
                       _decode_value(v.get("r"), depth + 1), closed=closed)


def _d_bytes(v, depth):
    data = base64.b64decode(_str(v.get("v")), validate=True)
    return bytearray(data) if v.get("$") == "bytearray" else data


def _items(v, depth):
    x = v.get("v")
    _need(isinstance(x, list), "a list")
    return [_decode_value(i, depth + 1) for i in x]


def _d_dict(v, depth):
    out = {}
    for pair in v.get("v") if isinstance(v.get("v"), list) else [None]:
        _need(isinstance(pair, list) and len(pair) == 2, "a dict")
        out[_decode_value(pair[0], depth + 1)] = _decode_value(pair[1],
                                                              depth + 1)
    return out


def _d_ndarray(v, depth):
    import numpy as np
    name, shape, values = v.get("t"), v.get("s"), v.get("v")
    _need(isinstance(shape, list) and all(
        isinstance(n, int) and not isinstance(n, bool) and n >= 0
        for n in shape) and len(shape) <= 32, "an array's shape")
    _need(isinstance(values, list), "an array's values")
    size = 1
    for n in shape:
        size *= n
    _need(size == len(values), "an array's size")
    if name == "object":
        arr = _objects([_decode_value(x, depth + 1) for x in values])
    elif isinstance(name, str) and _TEXT_DTYPE.fullmatch(name):
        items = [_decode_value(x, depth + 1) for x in values]
        _need(all(isinstance(x, (str, bytes)) for x in items), "text")
        itemsize = int(name[2:]) * (4 if name[1] == "U" else 1)
        arr = (np.array(items, dtype=name)
               if itemsize * max(len(items), 1) <= _MAX_TEXT_ARRAY
               else np.array(items))
    else:
        dtype = _np_dtype(name)
        if dtype.kind in "mM":
            arr = np.array([_int(x) for x in values],
                           dtype="i8").view(dtype)
        else:
            arr = np.array([_decode_value(x, depth + 1) for x in values],
                           dtype=dtype)
    return arr.reshape(shape)


def _d_series(v, depth):
    frame = decode(v.get("v"), depth=depth + 1)
    _need(frame.shape[1] == 1, "a series")
    s = frame.iloc[:, 0]
    s.name = _decode_value(v.get("n"), depth + 1)
    return s


_DECODERS = {
    "nat": lambda v, d: __import__("pandas").NaT,
    "na": lambda v, d: __import__("pandas").NA,
    "np": _d_np,
    "ts": _d_ts,
    "td": _d_td,
    "datetime": lambda v, d: _dt.datetime.fromisoformat(_str(v.get("v"))),
    "date": lambda v, d: _dt.date.fromisoformat(_str(v.get("v"))),
    "time": lambda v, d: _dt.time.fromisoformat(_str(v.get("v"))),
    "timedelta": _d_timedelta,
    "period": _d_period,
    "interval": _d_interval,
    "complex": lambda v, d: complex(*[_num(x) for x in (
        v.get("v") if isinstance(v.get("v"), list) and len(v.get("v")) == 2
        else [None, None])]),
    "bytes": _d_bytes,
    "bytearray": _d_bytes,
    "decimal": lambda v, d: decimal.Decimal(_str(v.get("v"))),
    "tuple": lambda v, d: tuple(_items(v, d)),
    "set": lambda v, d: set(_items(v, d)),
    "frozenset": lambda v, d: frozenset(_items(v, d)),
    "dict": _d_dict,
    "ndarray": _d_ndarray,
    "frame": lambda v, d: decode(v.get("v"), depth=d + 1),
    "series": _d_series,
}


def _decode_value(v: Any, depth: int = 0) -> Any:
    _need(depth <= MAX_DEPTH * 2 + 8, "nested too deeply")
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        return [_decode_value(x, depth + 1) for x in v]
    _need(isinstance(v, dict), "a value")
    reader = _DECODERS.get(v.get("$"))
    _need(reader is not None, f"an unknown value {str(v.get('$'))[:40]!r}")
    return reader(v, depth)


def _dtype(spec: Dict[str, Any], depth: int):
    """The dtype a column or an index is rebuilt as; None to keep the
    values as they are (object)."""
    import pandas as pd
    name = spec.get("t")
    if name == "object" or name is None:
        return None
    if "cat" in spec:
        return pd.CategoricalDtype(_decode_index(spec["cat"], depth + 1),
                                   ordered=bool(spec.get("ordered")))
    if not _safe_name(name):
        return None
    try:
        dtype = pd.api.types.pandas_dtype(name)
    except Exception:                                     # noqa: BLE001
        return None
    import numpy as np
    if isinstance(dtype, np.dtype) and (dtype.kind not in _NUMPY_KINDS
                                        or dtype.shape != ()):
        return None
    return dtype


def _decode_values(spec: Any, depth: int):
    """(values as an array, the dtype to rebuild them as or None)."""
    import numpy as np
    _need(isinstance(spec, dict) and isinstance(spec.get("v"), list),
          "a column")
    dtype = _dtype(spec, depth)
    values = spec["v"]
    if isinstance(dtype, np.dtype) and dtype.kind in "biuf" and all(
            type(x) in (bool, int, float) for x in values):
        try:
            return np.array(values, dtype=dtype), None
        except (TypeError, ValueError, OverflowError):
            pass
    return _objects([_decode_value(x, depth + 1) for x in values]), dtype


def _decode_column(spec: Any, rows: int, depth: int):
    import pandas as pd
    arr, dtype = _decode_values(spec, depth)
    _need(len(arr) == rows, "a column's length")
    s = pd.Series(arr, dtype=arr.dtype, copy=False)
    if dtype is not None:
        try:
            s = s.astype(dtype)
        except Exception:                                 # noqa: BLE001
            pass                       # the values, as they came
    return s


def _decode_index(spec: Any, depth: int):
    import pandas as pd
    _need(isinstance(spec, dict), "an index")
    name = _decode_value(spec.get("name"), depth + 1)
    if "range" in spec:
        r = spec["range"]
        _need(isinstance(r, list) and len(r) == 3, "a range index")
        return pd.RangeIndex(_int(r[0]), _int(r[1]), _int(r[2]), name=name)
    if "multi" in spec:
        _need(isinstance(spec["multi"], list)
              and isinstance(spec.get("names"), list), "a multi-index")
        tuples = [_decode_value(t, depth + 1) for t in spec["multi"]]
        names = [_decode_value(n, depth + 1) for n in spec["names"]]
        _need(all(isinstance(t, tuple) and len(t) == len(names)
                  for t in tuples), "a multi-index")
        if not tuples:
            return pd.MultiIndex.from_arrays([[] for _ in names],
                                             names=names)
        return pd.MultiIndex.from_tuples(tuples, names=names)
    arr, dtype = _decode_values(spec, depth)
    idx = pd.Index(arr, dtype=arr.dtype, name=name, tupleize_cols=False)
    if dtype is not None and idx.dtype != dtype:
        try:
            idx = idx.astype(dtype)
        except Exception:                                 # noqa: BLE001
            pass
    freq = spec.get("f")
    if freq is not None and _safe_name(freq):
        try:
            idx = type(idx)(idx, freq=freq, name=name)
        except Exception:                                 # noqa: BLE001
            pass
    return idx


def decode(obj: Any, *, depth: int = 0):
    """The DataFrame encode() wrote. ValueError / TypeError (or pandas'
    own error) when `obj` is not one."""
    import pandas as pd
    _need(isinstance(obj, dict) and obj.get("format") == FORMAT, "format")
    _need(depth <= MAX_DEPTH * 2 + 8, "nested too deeply")
    index = _decode_index(obj.get("index"), depth)
    columns = _decode_index(obj.get("columns"), depth)
    data = obj.get("data")
    _need(isinstance(data, list) and len(data) == len(columns), "columns")
    rows = len(index)
    cols = [_decode_column(c, rows, depth) for c in data]
    df = pd.DataFrame(dict(enumerate(cols)), index=pd.RangeIndex(rows))
    df.columns = columns
    df.index = index
    return df


def too_large(size: Optional[int]) -> str:
    """Why a result does not come back, plainly. `size`: its report's
    bytes, or None when writing it needed more memory than the sandbox
    has."""
    how = (f"{size / 2**20:.0f} MB" if size else
           "writing it needed more memory than the sandbox allows")
    return (f"the result was too large to bring back ({how}; at most "
            f"{MAX_RESULT_MB} MB) — return fewer rows or a summary")


def shape(obj: Any) -> Optional[tuple]:
    """The full (rows, cols) encode() wrote, or None."""
    s = obj.get("shape") if isinstance(obj, dict) else None
    if (isinstance(s, list) and len(s) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0
                    for n in s)):
        return int(s[0]), int(s[1])
    return None


__all__ = ["FORMAT", "MAX_ROWS", "MAX_RESULT_MB", "encode", "decode",
           "shape", "too_large"]
