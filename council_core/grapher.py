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

        result = Working(dataset=self.dataset)
        frame = getattr(self.dataset, "df", None)
        if self.transforms and frame is not None:
            # A SHALLOW copy of the dataset with a new df: the DataSet carries
            # its name, path and column summary, and a transform changes none
            # of those. Deep-copying would duplicate the whole frame twice.
            working = copy.copy(self.dataset)
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
