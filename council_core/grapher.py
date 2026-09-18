"""
council_core.grapher — the Grapher's session: one dataset, one answer.

WHAT WAS WRONG, AND IT IS THE WHOLE REASON THIS MODULE EXISTS
The Tk Grapher applies transforms and the overlay INSIDE its Plotly render
method. The inline (offline) path and the export path read `self._grapher_
dataset` straight, so they draw the untransformed frame. Normalise a column,
look at the inline chart, and it is the raw data — no error, no note, just a
different chart from the one the interactive view shows.

The stats panel has the same shape: it describes `ds` while the chart beside it
shows `working_ds`.

So: ONE working dataset, built once, used by every renderer. `Session.working()`
is that, and it is the only way any view is meant to get a frame.

AN OVERLAY THAT CANNOT BE DRAWN SAYS SO
The Tk code wraps the overlay render in `except Exception` and falls back to a
plain render. The user picked a second file, got a chart without it, and was
told nothing. `overlay_state()` reports why an overlay is not on screen — wrong
plot type, no y column, failed load — so a view can say it.

NOTHING HERE IMPORTS A TOOLKIT OR A RENDERER
It builds the frame and answers questions about it. Which renderer draws it is
the view's business, and that is what lets the same session feed the embedded
Agg canvas, the browser HTML, and an export.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Plot types the overlay renderer can actually draw a second series on.
#: Anything else gets a plain render — and is TOLD so, rather than silently
#: losing the overlay it was given.
OVERLAY_KINDS = ("line", "timeseries", "scatter", "area")


@dataclass
class Working:
    """The frame every renderer should draw, and how it got that way."""
    dataset: Any = None
    #: One line per transform applied, in order. Shown in the transform list.
    log: List[str] = field(default_factory=list)
    #: True when transforms actually changed the frame.
    transformed: bool = False
    #: The overlay dataset, or None when there is nothing to overlay.
    overlay: Any = None
    #: Why the overlay is not being drawn. Empty when it is, or when none was
    #: asked for.
    overlay_blocked: str = ""

    @property
    def df(self):
        return getattr(self.dataset, "df", None)


class Session:
    """What the Grapher is currently looking at.

    Deliberately not a renderer and not a view: it holds the dataset, the
    transform list and the overlay, and answers "what should be drawn".
    """

    def __init__(self):
        self.dataset: Any = None
        #: [{"op":..., "cols":[...], "params":{...}}]
        self.transforms: List[Dict[str, Any]] = []
        self.overlay: Any = None
        self.overlay_paths: List[Path] = []
        #: The loaded dataset with its string date columns parsed. Computed at
        #: load, because the coercion COPIES the frame and `working()` runs
        #: once per chart. None until something is loaded, and identical to
        #: `dataset` when nothing needed coercing.
        self._dated: Any = None
        #: Set when a load failed, so a view can say what happened rather than
        #: showing an empty file list and letting the user conclude there is
        #: no data.
        self.load_error: str = ""

    # ==================================================================
    # Loading
    # ==================================================================
    def load(self, path: Any, sheet: Optional[str] = None) -> Tuple[bool, str]:
        """Load a data file as the current dataset. (ok, message).

        `sheet` is honoured for spreadsheets. The Tk reload path passes no
        sheet at all, so `_load_excel` keeps `sheet_name=0` and the sheet
        dropdown snaps the user's pick back with no error — the control is
        decorative. Passing it through is the whole fix.
        """
        import graph_data

        path = Path(path)
        try:
            if sheet and _is_spreadsheet(path):
                dataset = graph_data.DataLoader._load_excel(path,
                                                            sheet_name=sheet)
            else:
                dataset = graph_data.DataLoader.load(path)
        except Exception as exc:                          # noqa: BLE001
            self.load_error = f"{path.name}: {exc}"
            return False, self.load_error
        error = getattr(dataset, "load_error", "") or ""
        if error:
            self.load_error = f"{path.name}: {error}"
            return False, self.load_error
        self.dataset = dataset
        self._dated = _dated_copy(dataset)
        self.load_error = ""
        return True, f"loaded {getattr(dataset, 'name', path.name)}"

    def load_overlay(self, path: Any) -> Tuple[bool, str]:
        """Load a second dataset to draw beside the first."""
        import graph_data

        path = Path(path)
        try:
            dataset = graph_data.DataLoader.load(path)
        except Exception as exc:                          # noqa: BLE001
            self.overlay = None
            return False, f"overlay {path.name}: {exc}"
        error = getattr(dataset, "load_error", "") or ""
        if error:
            self.overlay = None
            return False, f"overlay {path.name}: {error}"
        self.overlay = dataset
        return True, f"overlay: {getattr(dataset, 'name', path.name)}"

    def clear_overlay(self) -> None:
        self.overlay = None

    # ==================================================================
    # The one working dataset
    # ==================================================================
    def working(self, spec: Any = None) -> Working:
        """The dataset every renderer should draw.

        Built fresh each call rather than cached: the transform list is edited
        between renders, and a cache would need invalidating from every place
        that touches it. The cost is a shallow copy and one pass of pandas.
        """
        import graph_engine

        if self.dataset is None:
            return Working()

        # The date coercion happened once, at load. Doing it here would copy
        # the whole frame on every render, and `working()` is called per chart.
        base = self._dated if self._dated is not None else self.dataset
        result = Working(dataset=base)
        frame = getattr(base, "df", None)
        if self.transforms and frame is not None:
            # A SHALLOW copy of the dataset with a new df: the DataSet carries
            # its name, path and column summary, and a transform changes none
            # of those. Deep-copying would duplicate the whole frame twice.
            working = copy.copy(base)
            try:
                working.df, log = graph_engine.apply_transforms(
                    frame, self.transforms)
            except Exception as exc:                      # noqa: BLE001
                # A transform that fails must not take the chart with it. The
                # user sees the untransformed frame AND is told why.
                result.log = [f"transform failed: {exc}"]
                return result
            result.dataset = working
            result.log = list(log)
            result.transformed = True

        result.overlay, result.overlay_blocked = self._overlay_for(spec)
        return result

    def _overlay_for(self, spec: Any) -> Tuple[Any, str]:
        """(overlay dataset, why not). Exactly one is meaningful."""
        if self.overlay is None:
            return None, ""
        if getattr(self.overlay, "df", None) is None:
            return None, "the overlay file loaded no data"
        if spec is None:
            return self.overlay, ""
        if not getattr(spec, "y_col", None):
            return None, "an overlay needs a Y column"
        plot_type = getattr(spec, "plot_type", "")
        if plot_type not in OVERLAY_KINDS:
            return None, (f"{plot_type} cannot show an overlay — "
                          f"use {', '.join(OVERLAY_KINDS)}")
        return self.overlay, ""

    # ==================================================================
    # Questions a view asks
    # ==================================================================
    def columns(self) -> List[str]:
        """Every column name, from the WORKING frame.

        A transform can add one (`derive`), and a column picker built from the
        raw dataset cannot offer it — so the user derives a column and it is
        not in the list.
        """
        frame = self.working().df
        if frame is None:
            return []
        return [str(c) for c in frame.columns]

    def describe(self) -> str:
        """The stats panel's text, for the frame that is actually drawn.

        The Tk panel describes the RAW dataset next to a transformed chart.
        """
        import graph_engine

        working = self.working()
        if working.dataset is None:
            return "No file loaded. Select a file first."
        try:
            return graph_engine.DataAnalyser.describe(working.dataset)
        except Exception as exc:                          # noqa: BLE001
            return f"Could not summarise this dataset: {exc}"

    def add_transform(self, op: str, cols: Sequence[str],
                      params: Optional[Dict[str, Any]] = None) -> None:
        self.transforms.append({"op": op, "cols": list(cols),
                                "params": dict(params or {})})

    def remove_transform(self, index: int) -> bool:
        if 0 <= index < len(self.transforms):
            del self.transforms[index]
            return True
        return False

    def clear_transforms(self) -> None:
        self.transforms.clear()


def _is_spreadsheet(path: Path) -> bool:
    return path.suffix.lower() in (".xlsx", ".xls", ".xlsm", ".ods")


def scan(vault_dir: Any) -> List[Path]:
    """Every data file in the vault. Never raises — an unreadable vault is an
    empty list plus a message, not a dead tab."""
    import graph_data

    try:
        return list(graph_data.scan_vault_for_data(Path(vault_dir)))
    except Exception:                                     # noqa: BLE001
        return []


# ============================================================
# The inline (offline) plot path
# ============================================================
# The one that works air-gapped: an Agg figure drawn straight onto the canvas.
# No browser, no JavaScript, nothing to fetch. `plot_registry` decides which
# plots a column selection can actually produce, so the picker cannot offer one
# that will fail.

@dataclass(frozen=True)
class PlotChoice:
    """One offerable plot: what it is called, and what to call for it.

    The KEY travels beside the label rather than being parsed back out of it.
    The Tk picker builds "Density (KDE)  (kde)" and recovers the key with
    `rsplit("(", 1)`, which happens to work for all 31 current labels — checked,
    not assumed — but only because no key contains a parenthesis. It is one
    label away from silently building the wrong chart.
    """
    key: str
    label: str
    group: str = ""
    requires: str = ""

    @property
    def caption(self) -> str:
        return f"{self.label}  ({self.key})"


def _dated_copy(dataset: Any) -> Any:
    """The dataset with its date columns parsed, or the same object.

    The SAME object when nothing changed, so a caller can still tell the
    working dataset is the loaded one — and so a frame with no dates is not
    copied at all.
    """
    frame = getattr(dataset, "df", None)
    dated = _with_real_dates(frame)
    if dated is None or dated is frame:
        return dataset
    try:
        if list(dated.dtypes) == list(frame.dtypes):
            # coerce_datetime_columns always copies; if no dtype changed there
            # was nothing to coerce and the copy is pure cost.
            return dataset
    except Exception:                                     # noqa: BLE001
        pass
    updated = copy.copy(dataset)
    updated.df = dated
    return updated


def _with_real_dates(frame: Any) -> Any:
    """A frame whose string date columns are real datetimes.

    `coerce_datetime_columns` RETURNS A COPY — it does not mutate. Calling it
    and discarding the result leaves every date a string, which classifies as
    categorical and makes the whole time-series half of the registry
    unofferable on CSVs, the file type the Grapher is most used with. I wrote
    exactly that bug and caught it by looking at the roles rather than
    believing the call.

    Done here rather than in the inline path alone, so the export and browser
    renderers get the same frame — which is what `working()` promises.
    """
    import plot_roles

    if frame is None:
        return None
    try:
        return plot_roles.coerce_datetime_columns(frame)
    except Exception:                                     # noqa: BLE001
        return frame


def roles_for(frame: Any) -> Dict[str, str]:
    """Each column's role — numeric, categorical, datetime, boolean, text."""
    import plot_roles

    if frame is None:
        return {}
    try:
        return plot_roles.infer_roles(frame)
    except Exception:                                     # noqa: BLE001
        return {}


def choices_for(frame: Any, columns: Sequence[str]) -> List[PlotChoice]:
    """Only the plots this selection can actually draw.

    Offering a plot that cannot be built is a button that produces an error
    message, and the registry already knows the answer.
    """
    import plot_registry

    if frame is None or not columns:
        return []
    try:
        kinds = plot_registry.applicable(roles_for(frame), list(columns))
    except Exception:                                     # noqa: BLE001
        return []
    return [PlotChoice(k.key, k.label, k.group, k.requires) for k in kinds]


def hint_for(columns: Sequence[str], choices: Sequence[PlotChoice]) -> str:
    """What the line under the picker says.

    "No plot fits" alone leaves the user guessing; naming the kind of column
    that would help is the difference between a dead end and a next step.
    """
    if not columns:
        return "Select one or more columns."
    if not choices:
        return (f"No plot fits {len(columns)} column(s) of those types — "
                f"try adding a numeric or category column.")
    return f"{len(choices)} plot(s) fit this selection."


@dataclass
class FigureResult:
    ok: bool
    figure: Any = None
    message: str = ""


def build_figure(frame: Any, key: str, columns: Sequence[str],
                 **options: Any) -> FigureResult:
    """Draw one inline plot. NEVER raises.

    A builder raises a plain-English reason — "Density (KDE) needs seaborn,
    which isn't installed." — and that sentence is the thing worth showing. A
    traceback is not, and a half-drawn chart is worse than either.
    """
    import plot_registry

    if frame is None:
        return FigureResult(False, message="Load a data file first.")
    if not key or not columns:
        return FigureResult(False, message="Pick columns and a plot type "
                                           "first.")
    try:
        figure = plot_registry.build(key, frame, list(columns), **options)
    except Exception as exc:                              # noqa: BLE001
        return FigureResult(False, message=f"✗ {exc}")
    if figure is None:
        return FigureResult(False, message=f"✗ {key} produced no figure.")
    return FigureResult(True, figure,
                        f"{key}: {', '.join(str(c) for c in columns)}")


# ============================================================
# The interactive (browser) path
# ============================================================
# Plotly writes HTML and the system browser draws it. NOT embedded: the Tk
# build's embedded HTML widget has no JavaScript engine — its own comment says
# it "can only ever show a static shell" — so the browser is the only route
# that has ever produced an interactive chart here.

def plotly_types() -> frozenset:
    """Plot types the Plotly renderer can actually draw.

    Read from its dispatch rather than listed here, so a renderer that gains a
    type is offered it without anyone remembering, and one that loses a type
    stops being offered it. The alternative is a picker that offers a chart
    whose only output is "Unknown plot type".
    """
    import inspect
    import re

    import graph_engine

    try:
        source = inspect.getsource(graph_engine.PlotlyRenderer._dispatch)
    except (OSError, TypeError):                          # pragma: no cover
        return frozenset()
    return frozenset(re.findall(r'if t == "([a-z_0-9]+)"', source))


def spec_for(key: str, frame: Any, columns: Sequence[str], *,
             title: str = "", theme: str = "plotly_dark") -> Any:
    """A PlotSpec from a column selection.

    The two halves of this tab speak different vocabularies: the offline pane
    picks a `plot_registry` key, the interactive one wants a PlotSpec with
    named x/y/colour columns. Mapping the first onto the second is the only
    reason a user can press either button with one selection.

    X is the first column, Y the second. That is the order the pickers list
    them in and the order every plot in the registry reads them.
    """
    import graph_engine

    columns = list(columns)
    roles = roles_for(frame)
    # A datetime column is the X axis whatever position it was picked in — a
    # time series plotted with time on Y is not a chart anyone wanted.
    dated = [c for c in columns if roles.get(c) == "datetime"]
    ordered = dated + [c for c in columns if c not in dated]
    return graph_engine.PlotSpec(
        plot_type=key,
        x_col=ordered[0] if ordered else None,
        y_col=ordered[1] if len(ordered) > 1 else None,
        color_col=ordered[2] if len(ordered) > 2 else None,
        columns=list(ordered),
        title=title,
        theme=theme,
        renderer="plotly")


@dataclass
class HtmlResult:
    ok: bool
    path: Optional[Path] = None
    message: str = ""


def render_html(session: "Session", key: str, columns: Sequence[str],
                out_dir: Any, *, title: str = "") -> HtmlResult:
    """Write an interactive chart and say where it went. NEVER raises.

    Drawn from `working()`, so the interactive chart is the SAME data as the
    offline one. In the Tk tab only this path applies the transforms, which is
    how the two views come to disagree.
    """
    import datetime

    import graph_engine

    columns = list(columns)
    if not key or not columns:
        return HtmlResult(False, message="Pick columns and a plot type first.")
    if key not in plotly_types():
        return HtmlResult(
            False, message=f"{key} has no interactive version — it is an "
                           f"offline plot. Use Plot for it.")

    working = session.working()
    if working.df is None:
        return HtmlResult(False, message="Load a data file first.")

    spec = spec_for(key, working.df, columns, title=title)
    overlay, blocked = session._overlay_for(spec)
    try:
        renderer = graph_engine.PlotlyRenderer()
        if overlay is not None:
            html = renderer._overlay_render(spec, working.dataset, overlay)
        else:
            html = renderer.render(spec, working.dataset)
    except Exception as exc:                              # noqa: BLE001
        return HtmlResult(False, message=f"Could not render: {exc}")

    try:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H%M%S")
        path = out_dir / f"plot_{stamp}_{key}.html"
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        return HtmlResult(False, message=f"Could not save the chart: {exc}")

    note = f" — {blocked}" if blocked else ""
    return HtmlResult(True, path, f"opened {path.name}{note}")
