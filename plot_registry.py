"""
plot_registry.py — a data-driven catalog of plot types.

Each entry declares (a) which column roles it needs and (b) a builder that
returns a matplotlib Figure. Given a frame's inferred roles and the user's
column selection, `applicable()` returns exactly the plots that can actually be
drawn — so the UI offers only valid charts and never has to grow a
combinatorial menu, and adding a plot type is one `register()` call.

Design rules, all deliberate:

  * Builders construct Figure() directly and NEVER touch pyplot. plt.subplots()
    registers every figure in pyplot's global list, which both leaks memory
    across a long session and can spawn external windows — the exact "popup
    hell" the inline pane exists to avoid. Figure() + add_subplot() stays off
    global state.
  * Builders AGGREGATE where a chart implies it. 'Total revenue by category'
    was previously impossible: plotting a tidy frame with repeated x values
    stacked duplicate rows into a silently wrong bar. Category plots here do a
    real groupby, with the aggregation named on the axis so the reader can see
    what was done to their numbers.
  * Builders never mutate the caller's frame.
  * seaborn and squarify are OPTIONAL. A plot needing a missing library simply
    does not appear in applicable(), rather than erroring at click time.

The name is PlotKind, not PlotSpec: graph_engine.PlotSpec already means "one
configured plot instance", whereas this describes a KIND of plot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:                                    # pragma: no cover
    _PANDAS_OK = False

try:
    import matplotlib
    matplotlib.use("Agg")                              # never open a window
    from matplotlib.figure import Figure
    _MPL_OK = True
except ImportError:                                    # pragma: no cover
    _MPL_OK = False

try:
    import seaborn as sns
    _SEABORN_OK = True
except ImportError:                                    # pragma: no cover
    _SEABORN_OK = False

try:
    import squarify
    _SQUARIFY_OK = True
except ImportError:                                    # pragma: no cover
    _SQUARIFY_OK = False

from plot_roles import (BOOLEAN, CATEGORICAL, DATETIME, NUMERIC, TEXT,
                        count_role)

FIGSIZE = (7.0, 4.5)
AGG_FUNCS = ("sum", "mean", "count", "median", "min", "max")


@dataclass(frozen=True)
class PlotKind:
    """One plot type: what it needs, and how to draw it."""
    key: str
    label: str
    group: str
    requires: str                                   # human-readable requirement
    applies: Callable[[Dict[str, str], Sequence[str]], bool]
    build: Callable[..., Any]
    needs: tuple = ()                               # optional lib names

    def available(self) -> bool:
        for lib in self.needs:
            if lib == "seaborn" and not _SEABORN_OK:
                return False
            if lib == "squarify" and not _SQUARIFY_OK:
                return False
        return _MPL_OK and _PANDAS_OK


REGISTRY: Dict[str, PlotKind] = {}


def register(kind: PlotKind) -> PlotKind:
    if kind.key in REGISTRY:
        raise ValueError(f"duplicate plot key: {kind.key}")
    REGISTRY[kind.key] = kind
    return kind


def applicable(roles: Dict[str, str], cols: Sequence[str]) -> List[PlotKind]:
    """Every plot that can be drawn from ``cols`` given their ``roles``."""
    out = []
    for k in REGISTRY.values():
        if not k.available():
            continue
        try:
            if k.applies(roles, cols):
                out.append(k)
        except Exception:
            continue
    return out


def catalog(roles: Dict[str, str], cols: Sequence[str]) -> List[dict]:
    """The applicable plots as plain dicts — the CLOSED VOCABULARY handed to
    the local model. The model picks a key from this list; it never invents a
    plot type and never writes code, so the worst case is a suboptimal choice
    rather than a hallucinated call."""
    return [{"key": k.key, "label": k.label, "group": k.group,
             "requires": k.requires} for k in applicable(roles, cols)]


def build(key: str, df, cols: Sequence[str], **opts):
    """Render ``key`` over ``cols``. Validates against the registry first, so a
    bad key from the model is a clean error, never an exec()."""
    kind = REGISTRY.get(key)
    if kind is None:
        raise ValueError(f"unknown plot type {key!r}. "
                         f"Known: {', '.join(sorted(REGISTRY))}")
    if not kind.available():
        missing = ", ".join(kind.needs)
        raise ValueError(f"{kind.label} needs {missing}, which isn't installed.")
    if df is None:
        raise ValueError("No data loaded.")
    missing_cols = [c for c in cols if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Column(s) not in this dataset: {missing_cols}")
    return kind.build(df, list(cols), **opts)


# ============================================================
# helpers
# ============================================================

def _fig(figsize=FIGSIZE):
    """A bare Figure + axes, off pyplot's global registry."""
    fig = Figure(figsize=figsize, layout="constrained")
    return fig, fig.add_subplot(111)


def _pick(roles, cols, role, n=1):
    """The first ``n`` of ``cols`` having ``role`` (frame order preserved)."""
    got = [c for c in cols if roles.get(c) == role]
    return got[:n]


def _nums(roles, cols):
    return [c for c in cols if roles.get(c) == NUMERIC]


def _cats(roles, cols):
    return [c for c in cols if roles.get(c) in (CATEGORICAL, BOOLEAN)]


def _times(roles, cols):
    return [c for c in cols if roles.get(c) == DATETIME]


def _aggregate(df, cat_col, num_col, agg="sum"):
    """groupby + aggregate — the step whose absence made 'total revenue by
    category' silently plot stacked duplicate rows instead of a total."""
    if agg not in AGG_FUNCS:
        raise ValueError(f"agg must be one of {AGG_FUNCS}, got {agg!r}")
    g = df[[cat_col, num_col]].dropna().groupby(cat_col)[num_col]
    return getattr(g, agg)()


def _label_agg(agg, col):
    return f"{agg}({col})"


# ============================================================
# The reference plot. Every family below follows this shape: derive the
# columns from the frame's own dtypes (build() never receives roles),
# aggregate rather than stacking duplicate rows, and label the axis with what
# was actually done to the numbers.
# ============================================================

def _build_bar(df, cols, agg="sum", **_):
    if agg not in AGG_FUNCS:
        raise ValueError(f"agg must be one of {', '.join(AGG_FUNCS)}, "
                         f"got {agg!r}.")
    num = [c for c in cols
           if pd.api.types.is_numeric_dtype(df[c])
           and not pd.api.types.is_bool_dtype(df[c])]
    cat = [c for c in cols if c not in num]
    if not cat or not num:
        raise ValueError("A bar chart needs one categorical column and one "
                         "numeric column.")
    c, v = cat[0], num[0]
    series = _aggregate(df, c, v, agg)
    if series.empty:
        raise ValueError(f"No rows where both {c!r} and {v!r} have values.")
    fig, ax = _fig()
    ax.bar([str(i) for i in series.index], series.to_numpy())
    ax.set_xlabel(str(c))
    ax.set_ylabel(_label_agg(agg, v))
    ax.set_title(f"{_label_agg(agg, v)} by {c}")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="bar", label="Bar (aggregated)", group="Categorical",
    requires="1 categorical + 1 numeric",
    applies=lambda r, c: len(_cats(r, c)) >= 1 and len(_nums(r, c)) >= 1,
    build=_build_bar,
))


# ============================================================
# family: Basic — line / multi_line / step / area / scatter / bubble / hexbin
#
# build() never sees roles, so every builder re-derives its columns from the
# frame's own dtypes. The _basic_* helpers below are that derivation, kept
# private to this family so a column whose role says "numeric" but whose dtype
# is object-of-numeric-strings still plots, and a column that is genuinely not
# numeric produces a plain-English ValueError instead of an obscure one from
# deep inside matplotlib.
# ============================================================

def _basic_uniq(cols):
    """`cols` with duplicates removed, selection order preserved."""
    out = []
    for c in cols:
        if c not in out:
            out.append(c)
    return out


def _basic_series(df, col):
    """df[col] as a 1-D Series (duplicate column labels yield a frame)."""
    s = df[col]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return s


def _basic_to_num(s):
    """`s` as a float Series, or None if it does not hold real numbers.

    inf is folded into NaN: a single inf silently blows up every axis limit in
    the figure, which reads as "the chart is broken" rather than "one row is
    bad", so it is treated as missing like any other unusable value.
    """
    if not isinstance(s, pd.Series):
        return None
    if pd.api.types.is_bool_dtype(s):          # bools are categories here
        return None
    if pd.api.types.is_datetime64_any_dtype(s) or \
       pd.api.types.is_timedelta64_dtype(s) or \
       pd.api.types.is_complex_dtype(s):
        return None
    if pd.api.types.is_numeric_dtype(s):
        out = s.astype("float64")
    else:
        try:
            out = pd.to_numeric(s, errors="coerce").astype("float64")
        except (TypeError, ValueError):
            return None
        if not bool(out.notna().any()):
            return None
    return out.replace([np.inf, -np.inf], np.nan)


def _basic_nums(df, cols):
    """Selected columns that actually hold numbers, in selection order."""
    return [c for c in _basic_uniq(cols)
            if _basic_to_num(_basic_series(df, c)) is not None]


def _basic_frame(df, cols):
    """A private copy of `cols`: numeric-looking columns coerced to float,
    anything else left alone. Never touches the caller's frame."""
    data = {}
    for c in _basic_uniq(cols):
        s = _basic_series(df, c)
        num = _basic_to_num(s)
        data[c] = s if num is None else num
    return pd.DataFrame(data)


def _basic_time_col(df, cols):
    """The first datetime column among `cols`, else None."""
    for c in _basic_uniq(cols):
        if pd.api.types.is_datetime64_any_dtype(_basic_series(df, c)):
            return c
    return None


def _basic_marker(n):
    """A lone point draws nothing as a line — give it a marker."""
    return "o" if n <= 1 else None


def _basic_check_agg(agg):
    """Validate `agg` up front, not at the groupby.

    Aggregation only happens when x has repeated values, so an unchecked bad
    agg would pass silently on one dataset and raise on the next — the same
    call, two different answers. Fail the same way every time.
    """
    if agg not in AGG_FUNCS:
        raise ValueError(f"agg must be one of {', '.join(AGG_FUNCS)}, "
                         f"got {agg!r}.")
    return agg


def _basic_need_nums(df, cols, n, what):
    nums = _basic_nums(df, cols)
    if len(nums) < n:
        raise ValueError(
            f"{what} needs {n} numeric column(s); only {len(nums)} of "
            f"{[str(c) for c in _basic_uniq(cols)]} hold usable numbers.")
    return nums


def _basic_xy_series(df, x, y, agg):
    """(series, ylabel, aggregated?) for a y-over-x chart.

    Repeated x values get a real groupby — plotting a tidy frame straight would
    zig-zag back and forth between duplicate x's and look like noise.
    """
    d = _basic_frame(df, [x, y])
    sub = d[[x, y]].dropna()
    if sub.empty:
        raise ValueError(f"No rows where both {x!r} and {y!r} have values.")
    if bool(sub[x].duplicated().any()):
        series = _aggregate(sub, x, y, agg)
        if series.empty:
            raise ValueError(f"Nothing left to plot for {y!r} by {x!r}.")
        return series.sort_index(), _label_agg(agg, y), True
    return sub.set_index(x)[y].sort_index(), str(y), False


# ------------------------------------------------------------ line

def _build_basic_line(df, cols, agg="mean", **_):
    cols = _basic_uniq(cols)
    _basic_check_agg(agg)
    nums = _basic_need_nums(df, cols, 1, "A line chart")
    x = _basic_time_col(df, cols)
    if x is None and len(nums) >= 2:
        x, y = nums[0], nums[1]
    else:
        y = nums[0]

    fig, ax = _fig()
    if x is None:
        s = _basic_frame(df, [y])[y].dropna()
        if s.empty:
            raise ValueError(f"No usable numeric values in {y!r}.")
        ax.plot(np.arange(len(s)), s.to_numpy(), marker=_basic_marker(len(s)))
        ax.set_xlabel("row order")
        ax.set_ylabel(str(y))
        ax.set_title(f"{y} by row order")
    else:
        s, ylabel, _agg_used = _basic_xy_series(df, x, y, agg)
        ax.plot(s.index.to_numpy(), s.to_numpy(), marker=_basic_marker(len(s)))
        ax.set_xlabel(str(x))
        ax.set_ylabel(ylabel)
        ax.set_title(f"{ylabel} by {x}")
        ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="line", label="Line", group="Basic",
    requires="1 numeric (optionally + 1 datetime or a second numeric as x)",
    applies=lambda r, c: (
        (len(c) == 1 and len(_nums(r, c)) == 1)
        or (len(c) == 2 and len(_nums(r, c)) == 1 and len(_times(r, c)) == 1)
        or (len(c) == 2 and len(_nums(r, c)) == 2)
    ),
    build=_build_basic_line,
))


# ------------------------------------------------------------ multi_line

def _build_basic_multi_line(df, cols, agg="mean", **_):
    cols = _basic_uniq(cols)
    _basic_check_agg(agg)
    nums = _basic_need_nums(df, cols, 2, "An overlaid line chart")
    x = _basic_time_col(df, cols)
    d = _basic_frame(df, cols)

    fig, ax = _fig()
    if x is None:
        sub = d[nums].dropna(how="all")
        if sub.empty:
            raise ValueError(
                f"No usable numeric values in {[str(c) for c in nums]}.")
        xs = np.arange(len(sub))
        for c in nums:
            col = sub[c]
            ax.plot(xs, col.to_numpy(),
                    marker=_basic_marker(int(col.notna().sum())), label=str(c))
        ax.set_xlabel("row order")
        ax.set_ylabel("value")
        ax.set_title("  vs  ".join(str(c) for c in nums) + " by row order")
    else:
        sub = d[[x] + nums].dropna(subset=[x]).dropna(how="all", subset=nums)
        if sub.empty:
            raise ValueError(
                f"No rows where {x!r} and at least one of "
                f"{[str(c) for c in nums]} have values.")
        aggregated = bool(sub[x].duplicated().any())
        drew = False
        for c in nums:
            if aggregated:
                s = _aggregate(sub, x, c, agg)
            else:
                s = sub.set_index(x)[c].dropna()
            s = s.sort_index()
            if s.empty:
                continue
            ax.plot(s.index.to_numpy(), s.to_numpy(),
                    marker=_basic_marker(len(s)), label=str(c))
            drew = True
        if not drew:
            raise ValueError(f"Nothing left to plot against {x!r}.")
        ax.set_xlabel(str(x))
        ax.set_ylabel(f"{agg} of value" if aggregated else "value")
        ax.set_title("  vs  ".join(str(c) for c in nums) + f" by {x}")
        ax.tick_params(axis="x", rotation=45)
    ax.legend(loc="best", fontsize="small")
    return fig


register(PlotKind(
    key="multi_line", label="Multi-line (overlaid)", group="Basic",
    requires="2+ numeric (optionally + 1 datetime as x)",
    applies=lambda r, c: (
        len(_nums(r, c)) >= 2
        and len(_times(r, c)) <= 1
        and len(c) == len(_nums(r, c)) + len(_times(r, c))
    ),
    build=_build_basic_multi_line,
))


# ------------------------------------------------------------ step

def _build_basic_step(df, cols, agg="mean", **_):
    cols = _basic_uniq(cols)
    _basic_check_agg(agg)
    nums = _basic_need_nums(df, cols, 1, "A step chart")
    y = nums[0]
    x = _basic_time_col(df, cols)
    if x is None and len(nums) >= 2:
        x, y = nums[0], nums[1]

    fig, ax = _fig()
    if x is None:
        s = _basic_frame(df, [y])[y].dropna()
        if s.empty:
            raise ValueError(f"No usable numeric values in {y!r}.")
        ax.step(np.arange(len(s)), s.to_numpy(), where="post",
                marker=_basic_marker(len(s)))
        ax.set_xlabel("row order")
        ax.set_ylabel(str(y))
        ax.set_title(f"{y} by row order (step)")
    else:
        s, ylabel, _agg_used = _basic_xy_series(df, x, y, agg)
        ax.step(s.index.to_numpy(), s.to_numpy(), where="post",
                marker=_basic_marker(len(s)))
        ax.set_xlabel(str(x))
        ax.set_ylabel(ylabel)
        ax.set_title(f"{ylabel} by {x} (step)")
        ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="step", label="Step", group="Basic",
    requires="1 numeric (optionally + 1 datetime as x)",
    applies=lambda r, c: (
        (len(c) == 1 and len(_nums(r, c)) == 1)
        or (len(c) == 2 and len(_nums(r, c)) == 1 and len(_times(r, c)) == 1)
    ),
    build=_build_basic_step,
))


# ------------------------------------------------------------ area

def _build_basic_area(df, cols, agg="sum", **_):
    cols = _basic_uniq(cols)
    _basic_check_agg(agg)
    nums = _basic_need_nums(df, cols, 1, "An area chart")
    x = _basic_time_col(df, cols)
    d = _basic_frame(df, cols)

    # `x is not None`, never `if x`: a column label of 0 or "" is falsy but is
    # still a real column, and every branch below indexes on it.
    use = ([x] + nums) if x is not None else list(nums)
    sub = d[use].dropna()
    if sub.empty:
        raise ValueError(
            "No rows where every selected column has a value: "
            f"{[str(c) for c in use]}.")

    aggregated = False
    if x is not None and bool(sub[x].duplicated().any()):
        # Aggregate each band on the SAME fully-dropna'd rows, so every band
        # lands on one shared x grid — stackplot needs aligned x.
        parts = {c: _aggregate(sub, x, c, agg) for c in nums}
        grid = pd.DataFrame(parts).sort_index()
        aggregated = True
    elif x is not None:
        grid = sub.set_index(x)[nums].sort_index()
    else:
        grid = sub[nums].reset_index(drop=True)
    if grid.empty:
        raise ValueError("Nothing left to plot after dropping missing values.")

    xs = grid.index.to_numpy() if x is not None else np.arange(len(grid))
    xlabel = str(x) if x is not None else "row order"
    ylabel = f"{agg} of value" if aggregated else "value"

    fig, ax = _fig()
    if len(nums) == 1:
        c = nums[0]
        vals = grid[c].to_numpy()
        ax.fill_between(xs, 0, vals, alpha=0.45)
        ax.plot(xs, vals, marker=_basic_marker(len(grid)))
        ylabel = _label_agg(agg, c) if aggregated else str(c)
        ax.set_title(f"{ylabel} by {xlabel}")
    elif bool((grid[nums].to_numpy() < 0).any()):
        # Stacking negatives onto a zero baseline draws bands that cross each
        # other and sum to something meaningless — overlay them instead.
        for c in nums:
            ax.fill_between(xs, 0, grid[c].to_numpy(), alpha=0.35, label=str(c))
        ax.legend(loc="best", fontsize="small")
        ax.set_title("Area (overlaid — values go negative) by " + xlabel)
    else:
        ax.stackplot(xs, *[grid[c].to_numpy() for c in nums],
                     labels=[str(c) for c in nums], alpha=0.85)
        ax.legend(loc="best", fontsize="small")
        ax.set_title("Stacked area by " + xlabel)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if x is not None:
        ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="area", label="Area (stacked when 2+)", group="Basic",
    requires="1+ numeric (optionally + 1 datetime as x)",
    applies=lambda r, c: (
        len(_nums(r, c)) >= 1
        and len(_times(r, c)) <= 1
        and len(c) == len(_nums(r, c)) + len(_times(r, c))
    ),
    build=_build_basic_area,
))


# ------------------------------------------------------------ scatter

def _build_basic_scatter(df, cols, **_):
    cols = _basic_uniq(cols)
    nums = _basic_need_nums(df, cols, 2, "A scatter plot")
    x, y = nums[0], nums[1]
    sub = _basic_frame(df, [x, y])[[x, y]].dropna()
    if sub.empty:
        raise ValueError(f"No rows where both {x!r} and {y!r} have values.")

    fig, ax = _fig()
    ax.scatter(sub[x].to_numpy(), sub[y].to_numpy(),
               s=28, alpha=0.7, edgecolors="none")
    ax.set_xlabel(str(x))
    ax.set_ylabel(str(y))
    ax.set_title(f"{y} vs {x}  (n={len(sub)})")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="scatter", label="Scatter", group="Basic",
    requires="2 numeric",
    applies=lambda r, c: len(c) == 2 and len(_nums(r, c)) == 2,
    build=_build_basic_scatter,
))


# ------------------------------------------------------------ bubble

def _build_basic_bubble(df, cols, **_):
    cols = _basic_uniq(cols)
    nums = _basic_need_nums(df, cols, 3, "A bubble chart")
    x, y, size = nums[0], nums[1], nums[2]
    sub = _basic_frame(df, [x, y, size])[[x, y, size]].dropna()
    if sub.empty:
        raise ValueError(
            f"No rows where {x!r}, {y!r} and {size!r} all have values.")

    raw = sub[size].to_numpy()
    lo, hi = float(np.min(raw)), float(np.max(raw))
    if hi > lo:
        sizes = 20.0 + 580.0 * (raw - lo) / (hi - lo)
    else:
        sizes = np.full(len(raw), 120.0)     # one distinct size -> one radius

    fig, ax = _fig()
    ax.scatter(sub[x].to_numpy(), sub[y].to_numpy(), s=sizes,
               alpha=0.55, edgecolors="black", linewidths=0.5)
    ax.set_xlabel(str(x))
    ax.set_ylabel(str(y))
    ax.set_title(f"{y} vs {x} — bubble size = {size} "
                 f"({lo:g} to {hi:g})")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="bubble", label="Bubble (x, y, size)", group="Basic",
    requires="3 numeric (x, y, size)",
    applies=lambda r, c: len(c) == 3 and len(_nums(r, c)) == 3,
    build=_build_basic_bubble,
))


# ------------------------------------------------------------ hexbin

def _build_basic_hexbin(df, cols, gridsize=30, log=False, **_):
    cols = _basic_uniq(cols)
    nums = _basic_need_nums(df, cols, 2, "A hexbin plot")
    x, y = nums[0], nums[1]
    sub = _basic_frame(df, [x, y])[[x, y]].dropna()
    if sub.empty:
        raise ValueError(f"No rows where both {x!r} and {y!r} have values.")

    try:
        g = int(gridsize)
    except (TypeError, ValueError):
        raise ValueError(
            f"gridsize must be a whole number, got {gridsize!r}.") from None
    g = max(5, min(100, g))

    fig, ax = _fig()
    hb = ax.hexbin(sub[x].to_numpy(), sub[y].to_numpy(), gridsize=g,
                   cmap="viridis", mincnt=1, bins="log" if log else None)
    fig.colorbar(hb, ax=ax, label="rows per hex" + (" (log)" if log else ""))
    ax.set_xlabel(str(x))
    ax.set_ylabel(str(y))
    ax.set_title(f"Density of {y} vs {x}  (n={len(sub)})")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="hexbin", label="Hexbin (density)", group="Basic",
    requires="2 numeric",
    applies=lambda r, c: len(c) == 2 and len(_nums(r, c)) == 2,
    build=_build_basic_hexbin,
))

_DIST_MAX_CATS = 30            # beyond this a strip/box/violin is unreadable
_DIST_MAX_BINS = 200           # 'auto' on spiky data can ask for millions

# seaborn's two "I gave up" messages. Matched on their own wording, not on the
# bare word 'singular': that also appears in the '`warn_singular=False`' hint
# and in unrelated libraries' noise ("Matrix is singular to working
# precision"), which would abort a perfectly good chart.
_DIST_SINGULAR = ("cannot be estimated", "0 variance", "skipping density estimate")


def _dist_cols(cols):
    """``cols`` de-duplicated, order preserved. A repeated selection would
    otherwise reach ``df[list(cols)]``, come back with duplicate column names,
    and make every role lookup return a DataFrame instead of a Series."""
    seen, out = set(), []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _dist_split(df, cols):
    """(numeric cols, categorical cols) among ``cols``, by the same rules
    applies() was judged on. Datetime and text columns group nothing."""
    cols = _dist_cols(cols)
    roles = {}
    try:
        from plot_roles import infer_roles
        roles = infer_roles(df[cols])
    except Exception:
        roles = {}
    if roles:
        nums = [c for c in cols if roles.get(c) == NUMERIC]
        cats = [c for c in cols if roles.get(c) in (CATEGORICAL, BOOLEAN)]
        return nums, cats
    nums, cats = [], []
    for c in cols:                                  # fallback: dtype only
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            continue
        if pd.api.types.is_bool_dtype(s):           # bool is numeric to pandas
            cats.append(c)
        elif pd.api.types.is_numeric_dtype(s):
            nums.append(c)
        else:
            cats.append(c)
    return nums, cats


def _dist_num(df, cols, n=1):
    """The first ``n`` numeric columns, or a plain-English refusal."""
    nums, _ = _dist_split(df, cols)
    if len(nums) < n:
        raise ValueError(
            f"This plot needs {n} numeric column(s); "
            f"{', '.join(map(repr, _dist_cols(cols))) or 'nothing'} "
            f"gives {len(nums)}.")
    return nums[:n]


def _dist_cat(df, cols):
    """The first categorical column among ``cols``, or None."""
    _, cats = _dist_split(df, cols)
    return cats[0] if cats else None


def _dist_array(df, col):
    """``col`` as a float array of the frame's full length, with every
    unusable entry — blank, non-numeric, ±inf — turned into NaN. ±inf matters:
    matplotlib takes an infinite datum literally and renders a blank axis.

    Always a fresh array. np.asarray on a column can hand back a VIEW of the
    frame's own block, and this then overwrites entries."""
    s = df[col]
    if pd.api.types.is_bool_dtype(s):
        s = s.astype("float64")
    elif not pd.api.types.is_numeric_dtype(s):
        s = pd.to_numeric(s, errors="coerce")
    try:
        arr = np.array(s.astype("float64"), dtype="float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{col!r} does not hold numbers, so it has no distribution "
            f"to plot.") from exc
    arr = np.atleast_1d(arr)
    arr[~np.isfinite(arr)] = np.nan
    return arr


def _dist_values(df, col):
    """``col`` as finite floats only, positionally indexed."""
    arr = _dist_array(df, col)
    return pd.Series(arr[np.isfinite(arr)], name=col)


def _dist_need_values(df, col):
    s = _dist_values(df, col)
    if s.empty:
        raise ValueError(
            f"No usable numbers in {col!r} — every row is blank, "
            f"non-numeric or infinite.")
    return s


def _dist_need_spread(s, col):
    """A density estimate needs variation; a constant column has none."""
    if len(s) < 2:
        raise ValueError(
            f"{col!r} has only {len(s)} usable value(s); a density curve "
            f"needs at least 2.")
    if s.nunique() < 2:
        raise ValueError(
            f"Every row of {col!r} is {s.iloc[0]:g}. A density curve needs "
            f"values that vary — try a bar or a summary instead.")
    return s


def _dist_frame(df, num, cat):
    """A tidy 2-column frame of finite numbers and string labels, plus the
    order its groups should be drawn in, capped at _DIST_MAX_CATS categories
    (most frequent kept). Returns (frame, order, note).

    The order is the point. Ranking purely by frequency threw away a
    Categorical's declared order — an ordered Small/Medium/Large column drew as
    Medium, Large, Small — and left ties resolved by whatever order the rows
    happened to arrive in, so shuffling the same data redrew the same chart
    with its categories rearranged. Declared category order wins where there is
    one (this is what the reference bar chart's groupby already does); with no
    declared order, frequency descending, and the label breaks ties so the
    result is stable."""
    src = df[cat]
    declared = None
    if isinstance(src.dtype, pd.CategoricalDtype):
        declared = [str(c) for c in src.dtype.categories]
    labels = pd.Series(np.asarray(src, dtype=object), name=cat)
    frame = pd.DataFrame({cat: labels, num: _dist_array(df, num)}).dropna()
    if frame.empty:
        raise ValueError(f"No rows have both a {cat!r} and a usable {num!r}.")
    frame[cat] = frame[cat].astype(str)
    counts = frame[cat].value_counts().to_dict()
    by_freq = sorted(counts, key=lambda L: (-counts[L], L))
    if declared:
        order = [L for L in declared if L in counts]
        order += [L for L in by_freq if L not in set(order)]   # unexpected labels
    else:
        order = by_freq
    note = ""
    if len(order) > _DIST_MAX_CATS:
        keep = set(by_freq[:_DIST_MAX_CATS])        # drop the rarest ...
        note = f" (top {_DIST_MAX_CATS} of {len(order)} {cat})"
        order = [L for L in order if L in keep]     # ... but keep the order
        frame = frame[frame[cat].isin(keep)]
    return frame, order, note


def _dist_groups(frame, num, cat, order, min_rows=1, need_spread=False):
    """[(label, values), ...] in ``order``, dropping groups too thin to draw.
    Returns (groups, dropped_labels)."""
    vals_by_label = {str(k): v.to_numpy()
                     for k, v in frame.groupby(cat, observed=True)[num]}
    groups, dropped = [], []
    for label in order:
        vals = vals_by_label.get(str(label))
        if vals is None or len(vals) < min_rows or (
                need_spread and np.unique(vals).size < 2):
            dropped.append(str(label))
            continue
        groups.append((str(label), vals))
    return groups, dropped


def _dist_bin_edges(values, bins):
    """Bin edges that always exist and never explode. numpy's 'auto' rule can
    request millions of bins when the IQR is tiny next to the range, which
    freezes the render — cap it and fall back to a readable 50."""
    if isinstance(bins, str):
        rule = bins if bins in ("auto", "fd", "sturges", "scott",
                                "doane", "rice", "sqrt", "stone") else "auto"
        try:
            edges = np.histogram_bin_edges(values, bins=rule)
        except Exception:
            edges = np.histogram_bin_edges(values, bins=10)
        if len(edges) > _DIST_MAX_BINS + 1:
            edges = np.histogram_bin_edges(values, bins=50)
        return edges
    try:
        n = int(bins)
    except (TypeError, ValueError):
        return np.histogram_bin_edges(values, bins="auto")
    return np.histogram_bin_edges(values, bins=max(1, min(n, 500)))


def _dist_ticks(ax, labels):
    ax.set_xticks(np.arange(1, len(labels) + 1))
    ax.set_xticklabels(labels)
    ax.tick_params(axis="x", rotation=45)


def _dist_seaborn(fn, col, **kw):
    """Run a seaborn density call, translating its internals into a reason the
    user can act on.

    The warning check is the point. Handed perfectly collinear points, seaborn
    does not raise — it WARNS ('KDE cannot be estimated') and returns empty
    axes, so the pane shows a blank chart and the user concludes their data is
    missing. A blank chart that looks drawn is worse than a refusal that
    explains itself.

    Matched against seaborn's own phrasing (_DIST_SINGULAR). Testing for the
    bare word 'singular' also fired on unrelated libraries' warnings, throwing
    away a chart that was about to draw correctly."""
    import warnings
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fn(**kw)
    except Exception as exc:                        # LinAlgError & friends
        raise ValueError(
            f"Could not estimate a density for {col}: {exc}") from exc
    for w in caught:
        if not issubclass(w.category, UserWarning):
            continue
        text = str(w.message).lower()
        if any(hint in text for hint in _DIST_SINGULAR):
            raise ValueError(
                f"No density can be estimated for {col}: the values have no "
                f"spread, or lie exactly on a line, so the fit is singular. "
                f"A scatter or a box plot will still show these rows.")


# ---- histogram -------------------------------------------------------------

def _build_histogram(df, cols, bins="auto", log=False, **_):
    num = _dist_num(df, cols)[0]
    s = _dist_need_values(df, num)
    values = s.to_numpy()
    edges = _dist_bin_edges(values, bins)
    fig, ax = _fig()
    ax.hist(values, bins=edges, color="#4C72B0", edgecolor="white",
            linewidth=0.5)
    if log:
        ax.set_yscale("log")
    ax.set_xlabel(num)
    ax.set_ylabel("count of rows")
    ax.set_title(f"Distribution of {num}  (n={len(values):,}, "
                 f"{len(edges) - 1} bins)")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="histogram", label="Histogram", group="Distribution",
    requires="1 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_histogram,
))


# ---- kde -------------------------------------------------------------------

def _build_kde(df, cols, fill=True, **_):
    num = _dist_num(df, cols)[0]
    s = _dist_need_spread(_dist_need_values(df, num), num)
    fig, ax = _fig()
    _dist_seaborn(sns.kdeplot, repr(num), x=s.to_numpy(), ax=ax,
                  fill=bool(fill), color="#4C72B0")
    ax.set_xlabel(num)
    ax.set_ylabel("density")
    ax.set_title(f"Density of {num}  (n={len(s):,})")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="kde", label="Density (KDE)", group="Distribution",
    requires="1 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_kde, needs=("seaborn",),
))


# ---- ecdf ------------------------------------------------------------------

def _build_ecdf(df, cols, **_):
    num = _dist_num(df, cols)[0]
    s = _dist_need_values(df, num)
    fig, ax = _fig()
    _dist_seaborn(sns.ecdfplot, repr(num), x=s.to_numpy(), ax=ax,
                  color="#4C72B0")
    ax.set_xlabel(num)
    ax.set_ylabel("proportion of rows at or below")
    ax.set_title(f"Cumulative distribution of {num}  (n={len(s):,})")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="ecdf", label="Cumulative distribution (ECDF)", group="Distribution",
    requires="1 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_ecdf, needs=("seaborn",),
))


# ---- kde_2d ----------------------------------------------------------------

def _build_kde_2d(df, cols, fill=True, **_):
    x, y = _dist_num(df, cols, 2)
    frame = pd.DataFrame({x: _dist_array(df, x),
                          y: _dist_array(df, y)}).dropna()
    if frame.empty:
        raise ValueError(f"No rows have usable numbers in both {x!r} and {y!r}.")
    if len(frame) < 3:
        raise ValueError(
            f"Only {len(frame)} row(s) have both {x!r} and {y!r}; a 2-D "
            f"density needs at least 3.")
    for col in (x, y):
        if frame[col].nunique() < 2:
            raise ValueError(
                f"Every row of {col!r} is {frame[col].iloc[0]:g}. A 2-D "
                f"density needs both columns to vary.")
    fig, ax = _fig()
    _dist_seaborn(sns.kdeplot, f"{x!r} vs {y!r}", data=frame, x=x, y=y,
                  ax=ax, fill=bool(fill), cmap="Blues", thresh=0.05)
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(f"Joint density of {y} vs {x}  (n={len(frame):,})")
    return fig


register(PlotKind(
    key="kde_2d", label="2-D density", group="Distribution",
    requires="2 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 2,
    build=_build_kde_2d, needs=("seaborn",),
))


# ---- box -------------------------------------------------------------------

def _build_box(df, cols, showfliers=True, **_):
    num = _dist_num(df, cols)[0]
    cat = _dist_cat(df, cols)
    fig, ax = _fig()
    if cat is None:
        s = _dist_need_values(df, num)
        ax.boxplot([s.to_numpy()], showfliers=bool(showfliers),
                   patch_artist=True,
                   boxprops={"facecolor": "#AEC7E8", "edgecolor": "#33506B"},
                   medianprops={"color": "#B03A2E"})
        _dist_ticks(ax, [num])
        ax.set_xlabel("all rows")
        ax.set_title(f"Spread of {num}  (n={len(s):,})")
    else:
        frame, order, note = _dist_frame(df, num, cat)
        groups, _dropped = _dist_groups(frame, num, cat, order, min_rows=1)
        if not groups:
            raise ValueError(f"No {cat!r} group has a usable {num!r} value.")
        ax.boxplot([g[1] for g in groups], showfliers=bool(showfliers),
                   patch_artist=True,
                   boxprops={"facecolor": "#AEC7E8", "edgecolor": "#33506B"},
                   medianprops={"color": "#B03A2E"})
        _dist_ticks(ax, [g[0] for g in groups])
        ax.set_xlabel(cat)
        ax.set_title(f"Spread of {num} by {cat}{note}")
    ax.set_ylabel(num)
    return fig


register(PlotKind(
    key="box", label="Box plot", group="Distribution",
    requires="1 numeric (+ optional 1 categorical to group by)",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_box,
))


# ---- violin ----------------------------------------------------------------

def _build_violin(df, cols, showmedians=True, **_):
    num = _dist_num(df, cols)[0]
    cat = _dist_cat(df, cols)
    fig, ax = _fig()
    if cat is None:
        s = _dist_need_spread(_dist_need_values(df, num), num)
        labels, data, dropped = [num], [s.to_numpy()], []
        title = f"Distribution of {num}  (n={len(s):,})"
        ax.set_xlabel("all rows")
    else:
        frame, order, note = _dist_frame(df, num, cat)
        # A violin is a KDE per group: a group that never varies is singular,
        # so it is left out by name rather than crashing the whole figure.
        groups, dropped = _dist_groups(frame, num, cat, order, min_rows=2,
                                       need_spread=True)
        if not groups:
            raise ValueError(
                f"No {cat!r} group has 2+ differing {num!r} values, so no "
                f"violin can be estimated. Try a box plot.")
        labels = [g[0] for g in groups]
        data = [g[1] for g in groups]
        title = f"Distribution of {num} by {cat}{note}"
        ax.set_xlabel(cat)
    try:
        parts = ax.violinplot(data, showmedians=bool(showmedians),
                              showextrema=True)
    except Exception as exc:
        raise ValueError(f"Could not estimate a density for {num!r}: {exc}") from exc
    for body in parts["bodies"]:
        body.set_facecolor("#AEC7E8")
        body.set_edgecolor("#33506B")
        body.set_alpha(0.85)
    _dist_ticks(ax, labels)
    if dropped:
        title += f" — {len(dropped)} flat group(s) omitted"
    ax.set_ylabel(num)
    ax.set_title(title)
    return fig


register(PlotKind(
    key="violin", label="Violin plot", group="Distribution",
    requires="1 numeric (+ optional 1 categorical to group by)",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_violin,
))


# ---- strip -----------------------------------------------------------------

def _build_strip(df, cols, jitter=True, **_):
    num = _dist_num(df, cols)[0]
    cat = _dist_cat(df, cols)
    if cat is None:
        raise ValueError("A strip plot needs a categorical column to place "
                         "the points along.")
    frame, order, note = _dist_frame(df, num, cat)
    fig, ax = _fig()
    sns.stripplot(data=frame, x=cat, y=num, order=order, ax=ax,
                  jitter=bool(jitter), size=4, alpha=0.65, color="#4C72B0")
    ax.set_xlabel(cat)
    ax.set_ylabel(num)
    ax.set_title(f"{num} by {cat}{note}  (n={len(frame):,} points)")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="strip", label="Strip (all points)", group="Distribution",
    requires="1 numeric + 1 categorical",
    applies=lambda r, c: len(_nums(r, c)) >= 1 and len(_cats(r, c)) >= 1,
    build=_build_strip, needs=("seaborn",),
))

# ============================================================
# Categorical family
# ============================================================

def _catfam_split(df, cols, include_text=False):
    """(categoricals, numerics) among ``cols``, in selection order.

    build() is handed only (df, cols) — never the role map applies() saw — so
    the split is re-derived here with plot_roles.infer_roles, the SAME function
    the caller classified with, rather than a private lookalike that could
    disagree with the menu. Falls back to dtype when roles are unavailable, and
    testing bool BEFORE numeric matters: is_numeric_dtype() is True for a bool
    Series, so the obvious order turns every True/False column into a measure.
    """
    seen, ordered = set(), []
    for c in cols:                                    # dedupe, keep order
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    roles = {}
    try:
        from plot_roles import infer_roles
        roles = infer_roles(df[ordered])
    except Exception:
        roles = {}
    cats, nums = [], []
    for c in ordered:
        role = roles.get(c)
        if role is None:
            s = df[c]
            if pd.api.types.is_datetime64_any_dtype(s):
                role = DATETIME
            elif pd.api.types.is_bool_dtype(s):       # BEFORE numeric
                role = BOOLEAN
            elif pd.api.types.is_numeric_dtype(s):
                role = NUMERIC
            else:
                role = CATEGORICAL
        if role == NUMERIC:
            nums.append(c)
        elif role in (CATEGORICAL, BOOLEAN):
            cats.append(c)
        elif role == TEXT and include_text:
            cats.append(c)
    return cats, nums


def _catfam_cats(df, cols, want):
    """The first ``want`` categoricals of ``cols`` plus every numeric.

    Retries with text columns allowed: a high-cardinality column reads as TEXT
    here but may have been CATEGORICAL to the caller (roles depend on the frame
    it was inferred from), and a menu entry that errors on click is worse than
    a crowded axis."""
    cats, nums = _catfam_split(df, cols)
    if len(cats) < want:
        cats, nums = _catfam_split(df, cols, include_text=True)
    return cats, nums


def _catfam_values(series):
    """Plot-ready float array — a column that sneaks past the role check as
    object/complex becomes NaN (a missing bar) instead of a cast crash."""
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def _catfam_labels(index):
    return [str(i) for i in index]


def _catfam_names(cols):
    """Column names joined for a message/title. str() each one: a frame read
    without a header row has INTEGER column labels, and ', '.join([0, 1]) is a
    TypeError, not a chart."""
    return ", ".join(str(c) for c in cols)


def _catfam_top(series, limit, noun="categories"):
    """Top ``limit-1`` slices with the tail folded into a LABELLED 'Other'.

    Named, not dropped: a chart that quietly omits the long tail misstates the
    whole it claims to divide."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return series
    if n < 2 or len(series) <= n:
        return series
    head, rest = series.iloc[:n - 1], series.iloc[n - 1:]
    other = pd.Series([rest.to_numpy(dtype=float).sum()],
                      index=[f"Other ({len(rest)} {noun})"])
    return pd.concat([head, other])


def _build_grouped_bar(df, cols, agg="sum", **_):
    cats, nums = _catfam_cats(df, cols, 1)
    if not cats or len(nums) < 2:
        raise ValueError("Grouped bar needs 1 categorical column and at least "
                         "2 numeric columns.")
    cat = cats[0]
    # Each measure is aggregated on its own, then aligned: a category missing
    # from one measure leaves a gap there rather than dropping the whole row.
    frame = pd.concat([_aggregate(df, cat, n, agg) for n in nums],
                      axis=1, keys=nums)
    try:
        frame = frame.sort_index()
    except TypeError:                                  # mixed-type index
        pass
    frame = frame.dropna(how="all")
    if frame.empty:
        raise ValueError(f"No rows with a value in {cat!r} and any of: "
                         f"{_catfam_names(nums)}.")
    labels = _catfam_labels(frame.index)
    x = np.arange(len(labels), dtype=float)
    width = 0.8 / len(nums)
    fig, ax = _fig()
    for i, num in enumerate(nums):
        offset = (i - (len(nums) - 1) / 2.0) * width
        ax.bar(x + offset, _catfam_values(frame[num]), width=width,
               label=str(num))
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.tick_params(axis="x", rotation=45)
    ax.set_xlabel(str(cat))
    ax.set_ylabel(f"{agg} of each measure")
    measures = (_catfam_names(nums) if len(nums) <= 3
                else f"{len(nums)} measures")
    ax.set_title(f"{agg} of {measures} by {cat}")
    ax.legend(title="Measure", fontsize="small")
    return fig


def _build_stacked_bar(df, cols, agg="sum", max_series=30, **_):
    cats, nums = _catfam_cats(df, cols, 2)
    if len(cats) < 2 or not nums:
        raise ValueError("Stacked bar needs 2 categorical columns and "
                         "1 numeric column.")
    if agg not in AGG_FUNCS:
        raise ValueError(f"agg must be one of {AGG_FUNCS}, got {agg!r}")
    x_col, stack_col, num = cats[0], cats[1], nums[0]
    sub = df.loc[:, [x_col, stack_col, num]].dropna()
    if sub.empty:
        raise ValueError(f"No rows with a value in all of {x_col!r}, "
                         f"{stack_col!r} and {num!r}.")
    grouped = getattr(sub.groupby([x_col, stack_col])[num], agg)()
    pivot = grouped.unstack(level=-1)
    if pivot.empty or pivot.shape[1] == 0:
        raise ValueError(f"Nothing to plot for {num!r} by {x_col!r} "
                         f"and {stack_col!r}.")
    try:
        limit = int(max_series)
    except (TypeError, ValueError):
        limit = 30
    if pivot.shape[1] > limit:
        raise ValueError(f"{stack_col!r} has {pivot.shape[1]} distinct values "
                         f"— too many to stack readably (limit {limit}). "
                         f"Pick a column with fewer categories.")
    labels = _catfam_labels(pivot.index)
    x = np.arange(len(labels), dtype=float)
    bottom = np.zeros(len(labels), dtype=float)
    fig, ax = _fig()
    for col in pivot.columns:
        # Absent (x, stack) pairs are a real zero contribution to the stack;
        # left as NaN they would break the running bottom.
        vals = np.nan_to_num(_catfam_values(pivot[col]), nan=0.0)
        ax.bar(x, vals, bottom=bottom, label=str(col))
        bottom = bottom + vals
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.tick_params(axis="x", rotation=45)
    ax.set_xlabel(str(x_col))
    ax.set_ylabel(_label_agg(agg, num))
    ax.set_title(f"{_label_agg(agg, num)} by {x_col}, stacked by {stack_col}")
    ax.legend(title=str(stack_col), fontsize="small")
    return fig


def _build_count(df, cols, sort="count", top=None, **_):
    cats, _nums = _catfam_cats(df, cols, 1)
    if not cats:
        raise ValueError("Count needs a categorical column.")
    cat = cats[0]
    series = df[cat].dropna()
    if series.empty:
        raise ValueError(f"Every value in {cat!r} is missing — "
                         "nothing to count.")
    counts = series.value_counts()                     # already count-descending
    total = len(counts)
    # Cut to the top N BEFORE any re-sort. 'top' means "the N biggest"; applied
    # after sort_index() it would instead keep the N alphabetically-first and
    # silently discard the largest category.
    if top is not None:
        try:
            n = int(top)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            counts = counts.iloc[:n]
    kept = len(counts)
    if str(sort) == "index":
        try:
            counts = counts.sort_index()
        except TypeError:                              # mixed-type index
            pass
    if counts.empty:
        raise ValueError(f"No values left to count in {cat!r}.")
    fig, ax = _fig()
    ax.bar(_catfam_labels(counts.index), _catfam_values(counts))
    ax.set_xlabel(str(cat))
    ax.set_ylabel("count of rows")
    title = f"Row count by {cat}"
    if kept < total:                                   # say so, don't just cut
        title += f" (top {kept} of {total})"
    ax.set_title(title)
    ax.tick_params(axis="x", rotation=45)
    return fig


def _build_pie(df, cols, agg="sum", max_slices=12, **_):
    cats, nums = _catfam_cats(df, cols, 1)
    if not cats:
        raise ValueError("Pie needs a categorical column.")
    cat = cats[0]
    if nums:
        num = nums[0]
        series = _aggregate(df, cat, num, agg)         # never stack duplicates
        value_label = _label_agg(agg, num)
        empty_msg = f"No rows with both {cat!r} and {num!r}."
    else:
        # The no-numeric case: a pie of value_counts. Previously impossible —
        # the pie demanded a measure, so 'share of rows per category' had no chart.
        series = df[cat].dropna().value_counts()
        value_label = "count of rows"
        empty_msg = f"Every value in {cat!r} is missing — nothing to count."
    if series.empty:
        raise ValueError(empty_msg)
    series = pd.to_numeric(series, errors="coerce").dropna()
    if series.empty:
        raise ValueError(f"{value_label} is not numeric for any {cat} value.")
    if (series < 0).any():
        raise ValueError(f"{value_label} is negative for some {cat} values, "
                         "and a pie can only show non-negative parts of a "
                         "whole. Use a bar chart instead.")
    series = series[series > 0].sort_values(ascending=False)
    if series.empty:
        raise ValueError(f"Every {value_label} is zero — a pie needs at least "
                         "one slice with a positive value.")
    series = _catfam_top(series, max_slices)
    fig, ax = _fig()
    ax.pie(series.to_numpy(dtype=float), labels=_catfam_labels(series.index),
           autopct="%1.1f%%", startangle=90, normalize=True,
           textprops={"fontsize": 8})
    ax.set_aspect("equal")
    ax.set_xlabel(str(cat))
    ax.set_ylabel(value_label)
    ax.set_title(f"{value_label} share by {cat}")
    return fig


def _build_treemap(df, cols, agg="sum", max_tiles=40, **_):
    import matplotlib
    cats, nums = _catfam_cats(df, cols, 1)
    if not cats or not nums:
        raise ValueError("Treemap needs 1 categorical column and "
                         "1 numeric column.")
    cat, num = cats[0], nums[0]
    series = _aggregate(df, cat, num, agg)
    if series.empty:
        raise ValueError(f"No rows with both {cat!r} and {num!r}.")
    series = pd.to_numeric(series, errors="coerce").dropna()
    if (series < 0).any():
        raise ValueError(f"{_label_agg(agg, num)} is negative for some {cat} "
                         "values, and a treemap sizes tiles by area — it "
                         "cannot show a negative. Use a bar chart instead.")
    series = series[series > 0].sort_values(ascending=False)  # squarify wants desc
    if series.empty:
        raise ValueError(f"Every {_label_agg(agg, num)} is zero — there is no "
                         "area to divide into tiles.")
    series = _catfam_top(series, max_tiles)
    sizes = series.to_numpy(dtype=float)
    labels = _catfam_labels(series.index) if len(series) <= 25 else None
    cycle = matplotlib.rcParams["axes.prop_cycle"].by_key().get("color")
    if not cycle:
        cycle = ["#4C72B0"]
    colors = [cycle[i % len(cycle)] for i in range(len(sizes))]
    fig, ax = _fig()
    try:
        squarify.plot(sizes=sizes, label=labels, color=colors, ax=ax,
                      alpha=0.85, text_kwargs={"fontsize": 8})
    except TypeError:                                  # older squarify
        squarify.plot(sizes=sizes, label=labels, color=colors, ax=ax,
                      alpha=0.85)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xlabel(str(cat))
    ax.set_ylabel(_label_agg(agg, num))
    ax.set_title(f"{_label_agg(agg, num)} by {cat} (tile area = share)")
    return fig


register(PlotKind(
    key="grouped_bar", label="Grouped bar (aggregated)", group="Categorical",
    requires="1 categorical + 2 or more numeric",
    applies=lambda r, c: len(_cats(r, c)) >= 1 and len(_nums(r, c)) >= 2,
    build=_build_grouped_bar,
))

register(PlotKind(
    key="stacked_bar", label="Stacked bar (aggregated)", group="Categorical",
    requires="2 categorical + 1 numeric",
    applies=lambda r, c: len(_cats(r, c)) >= 2 and len(_nums(r, c)) >= 1,
    build=_build_stacked_bar,
))

register(PlotKind(
    key="count", label="Count of rows", group="Categorical",
    requires="1 categorical",
    applies=lambda r, c: len(_cats(r, c)) >= 1,
    build=_build_count,
))

register(PlotKind(
    key="pie", label="Pie (share of total)", group="Categorical",
    requires="1 categorical (+ optional 1 numeric)",
    applies=lambda r, c: len(_cats(r, c)) >= 1 and len(set(_nums(r, c))) <= 1,
    build=_build_pie,
))

register(PlotKind(
    key="treemap", label="Treemap", group="Categorical",
    requires="1 categorical + 1 numeric",
    applies=lambda r, c: len(_cats(r, c)) >= 1 and len(_nums(r, c)) >= 1,
    build=_build_treemap,
    needs=("squarify",),
))

# ============================================================
# Time
# ============================================================

import warnings  # local to this family: pandas' date-parsing chatter is noise

#
# build() receives only (df, cols) — never roles — so every builder below
# re-derives its columns from the frame. The derivation must agree EXACTLY with
# the roles applies() was judged on, or applies()'s promise stops describing
# what build() draws:
#   * plot_roles calls a column NUMERIC only when its dtype is numeric and
#     non-bool. It never coerces strings to numbers. So neither do we — a
#     tolerant "does anything in here parse as a number?" probe would let a
#     text column with a stray "12" outrank the real value columns and quietly
#     take the y/error/open slots.
#   * plot_roles DOES coerce for dates (a column of date strings is
#     role=datetime while its dtype is still object), but only when most of the
#     column parses. So the date probe below carries the same 80% rule and the
#     same all-digit guard, rather than accepting one date-ish token.


def _t_uniq(cols):
    """cols with duplicates dropped, order preserved — df[["a","a"]] is a trap."""
    seen, out = set(), []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _t_num(df, col):
    """``col`` as float, with strings coerced and +/-inf treated as missing."""
    s = df[col]
    if pd.api.types.is_bool_dtype(s) or not pd.api.types.is_numeric_dtype(s):
        s = pd.to_numeric(s, errors="coerce")
    s = s.astype(float)
    return s.replace([np.inf, -np.inf], np.nan)


def _t_time(df, col):
    """``col`` as datetime64, coercing date strings; unparseable rows -> NaT."""
    s = df[col]
    if not pd.api.types.is_datetime64_any_dtype(s):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = pd.to_datetime(s, errors="coerce")
    return s


_T_DATE_RATIO = 0.8   # mirrors plot_roles._DATE_PARSE_RATIO


def _t_num_cols(df, cols, exclude=()):
    """Selected columns whose ROLE is numeric — i.e. real numbers.

    As strict as plot_roles.infer_roles on purpose: numeric dtype, not bool,
    not datetime. Anything looser and build() would plot columns _nums() never
    counted, silently shifting value/error and open/high/low/close onto the
    wrong series."""
    out = []
    for c in _t_uniq(cols):
        if c in exclude:
            continue
        s = df[c]
        if pd.api.types.is_bool_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
            continue
        if pd.api.types.is_numeric_dtype(s):
            out.append(c)
    return out


def _t_looks_like_dates(s):
    """Mirror of plot_roles._looks_like_dates — a STRING column counts as dates
    only when most of it parses. All-digit strings ("2024") parse as 1970-era
    timestamps but are not dates, and one date-ish token inside a free-text
    column is not a time axis."""
    if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
        return False
    non_null = s.dropna()
    if non_null.empty:
        return False
    sample = non_null.head(200)
    try:
        text = sample.astype(str)
        if bool(text.str.fullmatch(r"\s*\d+(\.\d+)?\s*").all()):
            return False
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(sample, errors="coerce")
    except Exception:
        return False
    return (int(parsed.notna().sum()) / len(sample)) >= _T_DATE_RATIO


def _t_time_col(df, cols):
    """The first selected column whose ROLE is datetime, else None."""
    cols = _t_uniq(cols)
    for c in cols:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            return c
    for c in cols:
        if _t_looks_like_dates(df[c]):
            return c
    return None


def _t_frame(data):
    """A frame from {name: series} without touching the caller's df."""
    return pd.DataFrame(data).replace([np.inf, -np.inf], np.nan).dropna()


def _t_int_opt(value, name, minimum=1):
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}.")
    if out < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {out}.")
    return out


def _t_date_ticks(ax, x, stamps):
    """Label integer positions with dates, thinned so they stay readable."""
    n = len(x)
    step = max(1, n // 10)
    pos = list(x[::step])
    labels = [pd.Timestamp(t).strftime("%Y-%m-%d") for t in stamps[::step]]
    ax.set_xticks(pos)
    ax.set_xticklabels(labels, rotation=45, ha="right")


# ---- time series -------------------------------------------------------

def _build_timeseries(df, cols, **_):
    cols = _t_uniq(cols)
    tcol = _t_time_col(df, cols)
    if tcol is None:
        raise ValueError("A time series needs a date/time column, and none of "
                         "the selected columns hold dates.")
    ncols = _t_num_cols(df, cols, exclude=(tcol,))
    if not ncols:
        raise ValueError("A time series needs at least one numeric column "
                         f"besides {tcol!r}.")
    t = _t_time(df, tcol)
    fig, ax = _fig()
    drawn = []
    for c in ncols:
        # kind="stable": tied timestamps keep row order, so the same frame
        # always draws the same line.
        sub = _t_frame({"t": t, "v": _t_num(df, c)}).sort_values("t", kind="stable")
        if sub.empty:
            continue
        ax.plot(sub["t"].to_numpy(), sub["v"].to_numpy(), label=str(c),
                linewidth=1.5, marker="o" if len(sub) == 1 else "",
                markersize=4)
        drawn.append(c)
    if not drawn:
        raise ValueError(f"No rows have both a valid date in {tcol!r} and a "
                         f"number in {', '.join(map(repr, ncols))}.")
    ax.set_xlabel(tcol)
    ax.set_ylabel(str(drawn[0]) if len(drawn) == 1 else "value")
    if len(drawn) == 1:
        ax.set_title(f"{drawn[0]} over {tcol}")
    else:
        ax.set_title(f"{', '.join(map(str, drawn))} over {tcol}")
        ax.legend(fontsize="small")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="timeseries", label="Time series", group="Time",
    requires="1 datetime + 1 or more numeric",
    applies=lambda r, c: len(_times(r, c)) >= 1 and len(_nums(r, c)) >= 1,
    build=_build_timeseries,
))


# ---- rolling mean ------------------------------------------------------

def _build_rolling_mean(df, cols, window=10, **_):
    cols = _t_uniq(cols)
    tcol = _t_time_col(df, cols)
    if tcol is None:
        raise ValueError("A rolling mean needs a date/time column, and none of "
                         "the selected columns hold dates.")
    ncols = _t_num_cols(df, cols, exclude=(tcol,))
    if not ncols:
        raise ValueError("A rolling mean needs a numeric column besides "
                         f"{tcol!r}.")
    num = ncols[0]
    w = _t_int_opt(window, "window")
    sub = _t_frame({"t": _t_time(df, tcol), "v": _t_num(df, num)}).sort_values(
        "t", kind="stable")
    if sub.empty:
        raise ValueError(f"No rows have both a valid date in {tcol!r} and a "
                         f"number in {num!r}.")
    # A window longer than the data would otherwise yield an all-NaN line and
    # an empty-looking chart; clamp and say so on the title.
    eff = min(w, len(sub))
    roll = sub["v"].rolling(eff, min_periods=1).mean()
    x = sub["t"].to_numpy()
    fig, ax = _fig()
    ax.plot(x, sub["v"].to_numpy(), color="tab:gray", alpha=0.35, linewidth=1.0,
            marker="o" if len(sub) == 1 else "", markersize=3, label=f"{num} (raw)")
    ax.plot(x, roll.to_numpy(), color="tab:blue", linewidth=2.0,
            marker="o" if len(sub) == 1 else "", markersize=3,
            label=f"{eff}-point rolling mean")
    ax.set_xlabel(tcol)
    ax.set_ylabel(str(num))
    title = f"{num} over {tcol} — {eff}-point rolling mean"
    if eff != w:
        title += f" (window shortened from {w}; only {len(sub)} rows)"
    ax.set_title(title)
    ax.legend(fontsize="small")
    ax.tick_params(axis="x", rotation=45)
    return fig


register(PlotKind(
    key="rolling_mean", label="Rolling mean", group="Time",
    requires="1 datetime + 1 numeric",
    applies=lambda r, c: len(_times(r, c)) >= 1 and len(_nums(r, c)) >= 1,
    build=_build_rolling_mean,
))


# ---- lag plot ----------------------------------------------------------

def _build_lag(df, cols, lag=1, **_):
    cols = _t_uniq(cols)
    nums = _t_num_cols(df, cols)
    if not nums:
        raise ValueError("A lag plot needs a numeric column.")
    num = nums[0]
    k = _t_int_opt(lag, "lag")
    tcol = _t_time_col(df, cols)
    if tcol is not None:
        # Order is the whole point of a lag plot; if the user gave us a time
        # column, order by it rather than trusting row order.
        sub = _t_frame({"t": _t_time(df, tcol), "v": _t_num(df, num)}).sort_values(
            "t", kind="stable")
        s = sub["v"]
    else:
        s = _t_num(df, num).dropna()
    n = int(s.size)
    if n < 2:
        raise ValueError(f"A lag plot needs at least 2 numeric rows in {num!r}; "
                         f"found {n}.")
    if k >= n:
        raise ValueError(f"lag={k} is too large: {num!r} only has {n} usable "
                         f"rows, so no pair of points is {k} apart.")
    y = s.to_numpy(dtype=float)
    x0, x1 = y[:-k], y[k:]
    fig, ax = _fig()
    ax.scatter(x0, x1, s=18, alpha=0.7, edgecolor="none", color="tab:blue")
    lo = float(min(x0.min(), x1.min()))
    hi = float(max(x0.max(), x1.max()))
    if hi > lo:
        ax.plot([lo, hi], [lo, hi], color="tab:gray", linestyle="--",
                linewidth=1.0, label="y = x")
        ax.legend(fontsize="small")
    ax.set_xlabel(f"{num}[t]")
    ax.set_ylabel(f"{num}[t+{k}]")
    ax.set_title(f"Lag plot of {num} (lag {k})")
    return fig


register(PlotKind(
    key="lag", label="Lag plot", group="Time",
    requires="1 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_lag,
))


# ---- autocorrelation ---------------------------------------------------

def _build_autocorr(df, cols, max_lag=None, **_):
    cols = _t_uniq(cols)
    nums = _t_num_cols(df, cols)
    if not nums:
        raise ValueError("An autocorrelation plot needs a numeric column.")
    num = nums[0]
    tcol = _t_time_col(df, cols)
    if tcol is not None:
        sub = _t_frame({"t": _t_time(df, tcol), "v": _t_num(df, num)}).sort_values(
            "t", kind="stable")
        s = sub["v"]
    else:
        s = _t_num(df, num).dropna()
    n = int(s.size)
    if n < 3:
        raise ValueError(f"Autocorrelation needs at least 3 numeric rows in "
                         f"{num!r}; found {n}.")
    y = s.to_numpy(dtype=float)
    y = y - y.mean()
    denom = float(np.dot(y, y))
    if not np.isfinite(denom) or denom <= 0:
        raise ValueError(f"{num!r} never changes value, so its autocorrelation "
                         f"is undefined (it would divide by zero variance).")
    hi = n - 1
    hi = min(hi, 40) if max_lag is None else min(hi, _t_int_opt(max_lag, "max_lag"))
    lags = np.arange(1, hi + 1)
    ac = np.array([np.dot(y[:-k], y[k:]) / denom for k in lags], dtype=float)
    fig, ax = _fig()
    ax.vlines(lags, 0.0, ac, color="tab:blue", linewidth=1.5)
    ax.plot(lags, ac, "o", color="tab:blue", markersize=3)
    ax.axhline(0.0, color="black", linewidth=0.8)
    band = 1.96 / np.sqrt(n)
    ax.axhline(band, color="tab:red", linestyle="--", linewidth=0.9,
               label="95% confidence")
    ax.axhline(-band, color="tab:red", linestyle="--", linewidth=0.9)
    ax.set_ylim(-1.05, 1.05)
    ax.set_xlabel("lag (rows)")
    ax.set_ylabel("autocorrelation")
    ax.set_title(f"Autocorrelation of {num}")
    ax.legend(fontsize="small")
    return fig


register(PlotKind(
    key="autocorr", label="Autocorrelation", group="Time",
    requires="1 numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 1,
    build=_build_autocorr,
))


# ---- error bars --------------------------------------------------------

def _build_errorbar(df, cols, **_):
    cols = _t_uniq(cols)
    nums = _t_num_cols(df, cols)
    if len(nums) < 2:
        raise ValueError("An error-bar chart needs 2 numeric columns: the value "
                         "and its error.")
    ycol, ecol = nums[0], nums[1]
    tcol = _t_time_col(df, cols)
    data = {"y": _t_num(df, ycol), "e": _t_num(df, ecol)}
    if tcol is not None:
        data["t"] = _t_time(df, tcol)
    sub = _t_frame(data)
    if sub.empty:
        raise ValueError(f"No rows have a number in both {ycol!r} and {ecol!r}.")
    if tcol is not None:
        sub = sub.sort_values("t", kind="stable")
    y = sub["y"].to_numpy(dtype=float)
    # errorbar() rejects negative yerr; a magnitude is what was meant anyway.
    err = np.abs(sub["e"].to_numpy(dtype=float))
    x = np.arange(len(sub), dtype=float)
    fig, ax = _fig()
    ax.errorbar(x, y, yerr=err, fmt="o-", color="tab:blue", ecolor="tab:gray",
                elinewidth=1.2, capsize=3, markersize=4, linewidth=1.2)
    if tcol is not None:
        _t_date_ticks(ax, x, list(sub["t"]))
        ax.set_xlabel(str(tcol))
    else:
        ax.set_xlabel("row order")
    ax.set_ylabel(f"{ycol} (± {ecol})")
    ax.set_title(f"{ycol} with {ecol} error bars")
    return fig


register(PlotKind(
    key="errorbar", label="Error bars", group="Time",
    requires="2 numeric (value + error)",
    applies=lambda r, c: len(_nums(r, c)) >= 2,
    build=_build_errorbar,
))


# ---- OHLC candlesticks -------------------------------------------------

def _build_ohlc(df, cols, **_):
    cols = _t_uniq(cols)
    nums = _t_num_cols(df, cols)
    if len(nums) < 4:
        raise ValueError("A candlestick chart needs 4 numeric columns, in "
                         "open / high / low / close order.")
    ocol, hcol, lcol, ccol = nums[0], nums[1], nums[2], nums[3]
    tcol = _t_time_col(df, cols)
    data = {"o": _t_num(df, ocol), "h": _t_num(df, hcol),
            "l": _t_num(df, lcol), "c": _t_num(df, ccol)}
    if tcol is not None:
        data["t"] = _t_time(df, tcol)
    sub = _t_frame(data)
    if sub.empty:
        raise ValueError("No rows have a number in all four of "
                         f"{ocol!r}, {hcol!r}, {lcol!r}, {ccol!r}.")
    if tcol is not None:
        sub = sub.sort_values("t", kind="stable")
    o = sub["o"].to_numpy(dtype=float)
    h = sub["h"].to_numpy(dtype=float)
    lo = sub["l"].to_numpy(dtype=float)
    c = sub["c"].to_numpy(dtype=float)
    x = np.arange(len(sub), dtype=float)
    up = c >= o
    body = c - o
    # A doji (open == close) has zero body height and would draw nothing;
    # give it a hairline so the bar stays visible.
    span = float(np.max(h) - np.min(lo))
    eps = span * 0.003 if span > 0 else (abs(float(o[0])) * 0.001 or 0.01)
    body = np.where(np.abs(body) < eps, np.where(body < 0, -eps, eps), body)
    colors = np.where(up, "tab:green", "tab:red")
    fig, ax = _fig()
    ax.vlines(x, lo, h, color=list(colors), linewidth=1.0)
    width = 0.6
    for mask, color in ((up, "tab:green"), (~up, "tab:red")):
        if mask.any():
            ax.bar(x[mask], body[mask], bottom=o[mask], width=width,
                   color=color, edgecolor=color, linewidth=0.5)
    if tcol is not None:
        _t_date_ticks(ax, x, list(sub["t"]))
        ax.set_xlabel(str(tcol))
    else:
        ax.set_xlabel("row order")
    ax.set_ylabel("price")
    ax.set_title(f"Candlestick — {ocol} / {hcol} / {lcol} / {ccol}")
    return fig


register(PlotKind(
    key="ohlc", label="Candlestick (OHLC)", group="Time",
    requires="4 numeric (open, high, low, close)",
    applies=lambda r, c: len(_nums(r, c)) >= 4,
    build=_build_ohlc,
))

# ============================================================
# Multivariate
# ============================================================

# A fixed palette: no dependence on matplotlib's colormap-registry API, which
# has moved twice (cm.get_cmap -> matplotlib.colormaps) across versions we may
# meet on an air-gapped box.
_MV_PALETTE = ("#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
               "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD",
               "#1F77B4", "#FF7F0E")

# Radar spokes start here rather than at the origin: see _mv_unit().
_MV_RADAR_FLOOR = 0.12


def _mv_nums(df, cols):
    """The genuinely numeric columns of ``cols``, by dtype — build() never sees
    roles, so it re-derives them. Booleans are excluded to match _cats(), which
    counts BOOLEAN as categorical. Duplicate labels (df[c] -> DataFrame) are
    skipped, so a dup-column frame fails the length check with a plain reason
    instead of an obscure shape error deep in numpy."""
    out = []
    for c in cols:
        try:
            s = df[c]
        except Exception:
            continue
        if getattr(s, "ndim", 1) != 1:
            continue
        if pd.api.types.is_bool_dtype(s):
            continue
        if pd.api.types.is_numeric_dtype(s):
            out.append(c)
    return out


def _mv_cats(df, cols):
    """Class-like columns of ``cols``, by dtype. Datetime/timedelta are excluded
    so this mirrors _cats() (CATEGORICAL or BOOLEAN), which build() can't ask."""
    nums = set(_mv_nums(df, cols))
    out = []
    for c in cols:
        if c in nums:
            continue
        try:
            s = df[c]
        except Exception:
            continue
        if getattr(s, "ndim", 1) != 1:
            continue
        if (pd.api.types.is_datetime64_any_dtype(s)
                or pd.api.types.is_timedelta64_dtype(s)):
            continue
        out.append(c)
    return out


def _mv_class_col(df, cols, limit):
    """The first selected column usable as a colour class, plus a note when we
    decline one. applies() only promises the numeric columns, so a 5000-value
    text column must DEGRADE to an uncoloured chart with the reason on the
    title — never raise, or applies() would be offering a plot that dies."""
    cats = _mv_cats(df, cols)
    for c in cats:
        try:
            k = int(df[c].nunique(dropna=True))
        except Exception:
            continue
        if 1 <= k <= limit:
            return c, ""
    if cats:
        try:
            k = int(df[cats[0]].nunique(dropna=True))
            return None, (f"{cats[0]} not used for colour: {k} distinct "
                          f"values, limit {limit}")
        except Exception:
            pass
    return None, ""


def _mv_frame(df, num_cols, extra=None, what="plot"):
    """Numeric-coerced, inf-free, complete-case copy of the columns we need.
    Never mutates the caller's frame."""
    keep = list(num_cols) + ([extra] if extra else [])
    data = df.loc[:, keep].copy()
    for c in num_cols:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    data[num_cols] = data[num_cols].replace([np.inf, -np.inf], np.nan)
    data = data.dropna()
    # Positional index. Callers do label lookups (norm.loc[shown.index]) and a
    # frame with REPEATED index labels turns those into a cartesian blow-up —
    # 50 rows became 2500, the colour mask then misaligned and pandas raised a
    # bare IndexError from its internals. Nothing here needs the caller's
    # index, so drop it once, at the source, for every builder.
    data = data.reset_index(drop=True)
    if data.empty:
        raise ValueError(
            f"No rows left for the {what}: every row is missing (or "
            f"non-numeric in) at least one of {', '.join(map(str, keep))}.")
    return data


def _mv_unit(series, floor=0.0):
    """Min-max to ``floor``-1 along one axis. A constant column has no spread to
    show, so it sits at mid-scale rather than dividing by zero.

    ``floor`` > 0 matters for the radar: at floor 0 the lowest group lands on
    the origin on every axis and its polygon collapses to an invisible dot.
    Lifting the base keeps it a readable shape without changing the ordering."""
    v = series.astype(float)
    lo, hi = float(v.min()), float(v.max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return pd.Series(0.5, index=v.index)
    return floor + (1.0 - floor) * (v - lo) / (hi - lo)


def _build_corr_heatmap(df, cols, method="pearson", **_):
    nums = _mv_nums(df, cols)
    if len(nums) < 2:
        raise ValueError("A correlation heatmap needs at least 2 numeric "
                         f"columns; got {len(nums)}.")
    if method not in ("pearson", "spearman", "kendall"):
        method = "pearson"
    data = _mv_frame(df, nums, what="correlation heatmap")
    if len(data) < 2:
        raise ValueError(f"Only {len(data)} complete row(s) across those "
                         "columns — correlation needs at least 2.")

    corr = data.corr(method=method)
    m = corr.to_numpy(dtype=float)
    n = len(nums)
    side = max(FIGSIZE[0], 1.05 * n + 2.5)
    fig, ax = _fig(figsize=(side, max(FIGSIZE[1], 0.95 * n + 2.0)))
    # Constant columns give NaN correlations; mask them so they read as blank
    # cells labelled n/a instead of colouring as if they were zero.
    im = ax.imshow(np.ma.masked_invalid(m), cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(np.arange(n))
    ax.set_xticklabels([str(c) for c in nums])
    ax.set_yticks(np.arange(n))
    ax.set_yticklabels([str(c) for c in nums])
    ax.tick_params(axis="x", rotation=45)
    for lbl in ax.get_xticklabels():
        lbl.set_ha("right")
    for i in range(n):
        for j in range(n):
            v = m[i, j]
            if np.isfinite(v):
                txt, colour = f"{v:.2f}", ("white" if abs(v) > 0.6 else "black")
            else:
                txt, colour = "n/a", "0.4"
            ax.text(j, i, txt, ha="center", va="center",
                    fontsize=8, color=colour)
    fig.colorbar(im, ax=ax, label=f"{method} r")
    ax.set_xlabel("column")
    ax.set_ylabel("column")
    ax.set_title(f"{method.capitalize()} correlation "
                 f"({len(data)} complete rows)")
    return fig


register(PlotKind(
    key="corr_heatmap", label="Correlation heatmap", group="Multivariate",
    requires="2 or more numeric",
    applies=lambda r, c: len(_nums(r, c)) >= 2,
    build=_build_corr_heatmap,
))


def _build_pair(df, cols, hue=None, max_vars=6, **_):
    if not _SEABORN_OK:                                # pragma: no cover
        raise ValueError("The pair plot needs seaborn, which isn't installed.")
    nums = _mv_nums(df, cols)
    if len(nums) < 2:
        raise ValueError("A pair plot needs at least 2 numeric columns; "
                         f"got {len(nums)}.")
    try:
        max_vars = max(2, int(max_vars))
    except (TypeError, ValueError):
        max_vars = 6
    nums = nums[:max_vars]                             # an n x n grid, keep it sane

    hue_col = hue if hue in _mv_cats(df, cols) else None
    cat_note = ""
    if hue_col is None:
        hue_col, cat_note = _mv_class_col(df, cols, len(_MV_PALETTE))

    data = _mv_frame(df, nums, extra=hue_col, what="pair plot")
    if len(data) < 2:
        raise ValueError(f"Only {len(data)} complete row(s) across those "
                         "columns — a pair plot needs at least 2.")
    if hue_col:
        data[hue_col] = data[hue_col].astype(str)

    # diag_kind="hist": seaborn's default ("auto") picks KDE once a hue is set,
    # and a KDE over a constant or near-constant column raises a linalg error.
    grid = sns.pairplot(data, vars=nums, hue=hue_col,
                        diag_kind="hist", corner=False)
    # PairGrid owns its figure. .figure is seaborn >= 0.11.2; .fig is the older
    # name, and an air-gapped box is exactly where the old one turns up.
    fig = getattr(grid, "figure", None) or grid.fig
    # pairplot is a FIGURE-level function: PairGrid builds the grid with
    # plt.figure() internally, so ALONE among these builders it hands back a
    # figure that pyplot's global registry is holding forever — the leak this
    # module's rule 1 exists to prevent (matplotlib starts warning at 20 open
    # figures). Drop it from that registry; the Figure keeps its axes and
    # re-acquires a canvas on draw, so it stays fully renderable.
    try:
        import matplotlib.pyplot as plt
        plt.close(fig)
    except Exception:                                  # pragma: no cover
        pass
    title = f"Pairwise relationships ({len(data)} rows)"
    if hue_col:
        title += f", coloured by {hue_col}"
    elif cat_note:
        title += f"\n({cat_note})"
    fig.suptitle(title)
    for ax in np.ravel(np.asarray(grid.axes, dtype=object)):
        if ax is None:
            continue
        ax.tick_params(axis="x", rotation=45)
    try:
        fig.tight_layout()
    except Exception:                                  # pragma: no cover
        pass
    return fig


register(PlotKind(
    key="pair", label="Pair plot (scatter matrix)", group="Multivariate",
    requires="2 or more numeric (+ optional categorical for colour)",
    applies=lambda r, c: len(_nums(r, c)) >= 2,
    build=_build_pair,
    needs=("seaborn",),
))


def _build_parallel_coords(df, cols, max_lines=400, **_):
    nums = _mv_nums(df, cols)
    if len(nums) < 3:
        raise ValueError("Parallel coordinates needs at least 3 numeric "
                         f"columns; got {len(nums)}.")
    cat, cat_note = _mv_class_col(df, cols, len(_MV_PALETTE))
    data = _mv_frame(df, nums, extra=cat, what="parallel coordinates plot")

    levels = []
    if cat is not None:
        data[cat] = data[cat].astype(str)
        levels = sorted(data[cat].unique())

    try:
        max_lines = max(1, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 400
    shown, note = data, ""
    if len(data) > max_lines:                          # a deterministic sample
        shown = data.sample(n=max_lines, random_state=0).sort_index()
        note = f", {max_lines} of {len(data)} rows shown"

    # Normalise each axis over the FULL complete-case data, not the sample, so
    # the picture doesn't shift when the sample does. _mv_frame guarantees a
    # unique positional index, so this label lookup selects exactly the sampled
    # rows rather than fanning out.
    norm = pd.DataFrame({c: _mv_unit(data[c]) for c in nums},
                        index=data.index).loc[shown.index]
    x = np.arange(len(nums))
    fig, ax = _fig()
    for xi in x:
        ax.axvline(xi, color="0.85", lw=0.8, zorder=0)

    alpha = 0.7 if len(shown) <= 40 else (0.4 if len(shown) <= 200 else 0.2)
    if cat is None:
        ax.plot(x, norm[nums].to_numpy(dtype=float).T,
                color=_MV_PALETTE[0], lw=1.0, alpha=alpha)
    else:
        groups = shown[cat]
        for level, colour in zip(levels, _MV_PALETTE):
            block = norm.loc[groups == level, nums]
            if block.empty:
                continue
            ax.plot(x, block.to_numpy(dtype=float).T,
                    color=colour, lw=1.0, alpha=alpha)
            ax.plot([], [], color=colour, lw=2.0, label=str(level))
        ax.legend(title=str(cat), fontsize=8, loc="best")

    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in nums])
    ax.tick_params(axis="x", rotation=45)
    for lbl in ax.get_xticklabels():
        lbl.set_ha("right")
    ax.set_xlim(-0.15, len(nums) - 0.85)
    ax.set_ylim(-0.05, 1.05)
    ax.set_xlabel("column")
    ax.set_ylabel("normalised value (0-1 per axis)")
    title = "Parallel coordinates" + (f" by {cat}" if cat else "") + note
    if cat_note:
        title += f"\n({cat_note})"
    ax.set_title(title, fontsize=10)
    return fig


register(PlotKind(
    key="parallel_coords", label="Parallel coordinates", group="Multivariate",
    requires="3 or more numeric (+ optional categorical for colour)",
    applies=lambda r, c: len(_nums(r, c)) >= 3,
    build=_build_parallel_coords,
))


def _mv_radar_scale(table, data, nums):
    """Aggregated ``table`` scaled to _MV_RADAR_FLOOR-1 per axis.

    The preferred reference is the spread ACROSS groups: that is what makes the
    polygons visibly differ. When an axis has no across-group spread — one
    group, or every group landing on the same value — that reference is empty,
    so fall back to the column's own observed range and show where the
    aggregate sits inside it. Without the fallback a single-group radar scales
    against itself, every spoke lands on 0.5, and the chart is an identical
    featureless circle whatever the numbers say — while deselecting the label
    column would have drawn the informative version of the same data.

    The no-cat case is just a one-row table, so it takes the fallback and
    reproduces the single-polygon scaling exactly. (``sum`` can land outside
    the per-row range, hence the clip.)"""
    unit = {}
    for c in nums:
        col = table[c].astype(float)
        lo, hi = float(col.min()), float(col.max())
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
            lo, hi = float(data[c].min()), float(data[c].max())
            if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
                unit[c] = pd.Series(0.5, index=table.index)   # no spread at all
                continue
            frac = ((col - lo) / (hi - lo)).clip(0.0, 1.0)
        else:
            frac = (col - lo) / (hi - lo)
        unit[c] = _MV_RADAR_FLOOR + (1.0 - _MV_RADAR_FLOOR) * frac
    return pd.DataFrame(unit, index=table.index)


def _build_radar(df, cols, agg="mean", **_):
    nums = _mv_nums(df, cols)
    if len(nums) < 3:
        raise ValueError(f"A radar chart needs at least 3 numeric columns "
                         f"(axes); got {len(nums)}.")
    if agg not in AGG_FUNCS:
        agg = "mean"
    cat, cat_note = _mv_class_col(df, cols, 8)   # past ~8 polygons it's mush
    data = _mv_frame(df, nums, extra=cat, what="radar chart")

    # A radar plots one polygon per group over repeated rows, so it implies an
    # aggregation: do it explicitly and name it, never stack duplicate rows.
    if cat is not None:
        groups = data[cat].astype(str)
        levels = sorted(groups.unique())
        table = data[nums].groupby(groups, sort=True).agg(agg).loc[levels]
    else:
        vals = data[nums].agg(agg).astype(float)
        table = pd.DataFrame([vals.to_numpy(dtype=float)],
                             columns=nums, index=["all rows"])
    unit = _mv_radar_scale(table, data, nums)
    scale_note = (f"scaled per axis across {cat}" if len(table) > 1
                  else "scaled per axis over each column's range")

    m = unit.to_numpy(dtype=float)
    if not np.isfinite(m).all():
        raise ValueError("The aggregated values are not finite — check the "
                         "selected numeric columns.")

    # Start at 12 o'clock and go clockwise — how people read a radar.
    angles = np.linspace(0.0, 2.0 * np.pi, len(nums), endpoint=False)
    closed = np.concatenate([angles, angles[:1]])      # close the polygon
    fig = Figure(figsize=(8.0, 5.0), layout="constrained")
    ax = fig.add_subplot(111, polar=True)
    ax.set_theta_offset(np.pi / 2.0)
    ax.set_theta_direction(-1)
    for i, label in enumerate(table.index):
        v = np.concatenate([m[i], m[i][:1]])
        colour = _MV_PALETTE[i % len(_MV_PALETTE)]
        ax.plot(closed, v, color=colour, lw=1.8, label=str(label))
        ax.fill(closed, v, color=colour, alpha=0.15)
    ax.set_xticks(angles)
    ax.set_xticklabels([str(c) for c in nums], fontsize=8)
    ax.tick_params(axis="x", pad=6)
    ax.set_ylim(0.0, 1.05)
    ax.set_yticks([_MV_RADAR_FLOOR, 1.0])
    ax.set_yticklabels(["low", "high"], fontsize=7, color="0.4")
    # Park the radial labels between two spokes; on the default 0 deg they sit
    # on top of the first column's label.
    ax.set_rlabel_position(float(np.degrees(angles[1] / 2.0)) if len(nums) > 1
                           else 0.0)
    ax.set_xlabel("axis = column", labelpad=12)
    ax.set_ylabel("normalised value (per axis)", labelpad=46, fontsize=9)
    title = f"{agg} of {len(nums)} measures ({scale_note})"
    if cat_note:
        title += f"\n({cat_note})"
    ax.set_title(title, fontsize=10)
    if cat is not None:
        ax.legend(title=str(cat), fontsize=8,
                  loc="upper left", bbox_to_anchor=(1.08, 1.05))
    return fig


register(PlotKind(
    key="radar", label="Radar (spider)", group="Multivariate",
    requires="3 or more numeric (+ optional categorical for one polygon each)",
    applies=lambda r, c: len(_nums(r, c)) >= 3,
    build=_build_radar,
))


def _build_scatter_3d(df, cols, **_):
    nums = _mv_nums(df, cols)
    if len(nums) < 3:
        raise ValueError("A 3-D scatter needs 3 numeric columns "
                         f"(x, y, z); got {len(nums)}.")
    x, y, z = nums[:3]
    data = _mv_frame(df, [x, y, z], what="3-D scatter")

    # Importing mplot3d is what registers the '3d' projection; without pyplot
    # doing it for us, the add_subplot below would raise ValueError otherwise.
    try:
        from mpl_toolkits.mplot3d import Axes3D          # noqa: F401
    except ImportError:                                  # pragma: no cover
        raise ValueError("3-D plotting (mpl_toolkits.mplot3d) isn't available "
                         "in this matplotlib install.")

    fig = Figure(figsize=(8.0, 5.0), layout="constrained")
    ax = fig.add_subplot(111, projection="3d")
    zv = data[z].to_numpy(dtype=float)
    sc = ax.scatter(data[x].to_numpy(dtype=float),
                    data[y].to_numpy(dtype=float), zv,
                    c=zv, cmap="viridis", s=22, depthshade=True)
    # All three axes are numeric, so no tick rotation here: on a 3-D projection
    # rotated ticks run straight through the axis label.
    ax.set_xlabel(str(x), labelpad=10)
    ax.set_ylabel(str(y), labelpad=10)
    ax.set_zlabel(str(z), labelpad=8)
    ax.tick_params(labelsize=8)
    fig.colorbar(sc, ax=ax, shrink=0.65, pad=0.12, label=str(z))
    ax.set_title(f"{x} vs {y} vs {z} ({len(data)} rows)")
    return fig


register(PlotKind(
    key="scatter_3d", label="3-D scatter", group="Multivariate",
    requires="3 numeric (x, y, z)",
    applies=lambda r, c: len(_nums(r, c)) >= 3,
    build=_build_scatter_3d,
))

