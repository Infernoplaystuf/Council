# ============================================================
# graph_engine.py  —  Plot engine (Plotly + Matplotlib)
# ============================================================
# Takes a PlotSpec and renders to either:
#   - Plotly HTML  (interactive, embedded in GUI browser)
#   - Matplotlib Figure (static, exportable PNG/SVG/PDF)
#
# Install:
#   pip install plotly matplotlib scipy scikit-learn kaleido
#   kaleido is needed for static Plotly export
# ============================================================

from __future__ import annotations

import io
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:
    _PANDAS_OK = False

try:
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio
    _PLOTLY_OK = True
except ImportError:
    _PLOTLY_OK = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm
    from matplotlib.figure import Figure
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

# Probe-only: find_spec locates scipy without executing it (importing
# scipy.signal costs ~1.6 s and this module loads at app startup). The
# two call sites that need it (_distribution's gaussian_kde and
# _spectrogram) import locally on first use.
import importlib.util as _ilu_sp
_SCIPY_OK = _ilu_sp.find_spec("scipy") is not None

try:
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False

from graph_data import DataSet

# How plotly.js gets into the emitted HTML.
#
# This was "cdn", which made EVERY interactive chart a blank div on the
# air-gapped machines this app is built for: the page's only <script src>
# pointed at https://cdn.plot.ly/, which never resolves offline, while
# to_html() still returned successfully — so the app reported a rendered chart
# and showed nothing, with no error anywhere.
#
# True inlines plotly.js into each file (~3 MB), so a chart is self-contained
# and works offline, opened from anywhere, forever. "directory" would be
# smaller but writes a sidecar plotly.min.js the HTML must sit next to, and
# render() returns a STRING without knowing where the caller will put it.
# Correctness first: keep it self-contained.
_PLOTLY_JS: Any = True


# ============================================================
# PlotSpec — the declarative plot description
# ============================================================

PLOT_TYPES = [
    # Basic
    "line", "bar", "scatter", "histogram", "pie", "area",
    # Statistical
    "box", "violin", "heatmap", "correlation", "distribution",
    "density_2d", "parallel_coords",
    # Scientific
    "fft", "spectrogram", "polar", "contour", "surface_3d",
    "scatter_3d",
    # Time series
    "timeseries", "rolling_mean", "trend", "anomaly",
    # Dimensional reduction
    "pca",
    # Faceted
    "facet",
]

class UnsupportedPlotType(ValueError):
    """A renderer has no implementation for this plot type.

    Distinct from a render failure: the static (matplotlib) exporter covers a
    subset of PLOT_TYPES, and used to fall through to a generic 5-line numeric
    plot for the rest — returning a truthy Figure, so the GUI reported
    '✓ Exported' over a chart that was not the one requested."""


# Plot types the matplotlib (static export) renderer actually implements.
# Anything in PLOT_TYPES but not here raises UnsupportedPlotType rather than
# silently exporting something else.
MPL_SUPPORTED = frozenset({
    "line", "bar", "scatter", "histogram", "box", "violin", "heatmap",
    "correlation", "fft", "spectrogram", "surface_3d", "timeseries",
    "rolling_mean", "trend", "anomaly", "pca",
})


@dataclass
class PlotSpec:
    """
    Declarative specification for a plot.
    Produced either by the GUI controls or by the AI.
    """
    plot_type: str                    # one of PLOT_TYPES
    x_col: Optional[str] = None
    y_col: Optional[str] = None
    z_col: Optional[str] = None       # for 3D / contour
    color_col: Optional[str] = None   # for grouping by color
    size_col: Optional[str] = None    # for bubble charts
    columns: List[str] = field(default_factory=list)  # multi-column plots

    # Appearance
    title: str = ""
    x_label: str = ""
    y_label: str = ""
    color_scheme: str = "viridis"
    theme: str = "plotly_dark"       # Plotly theme
    width: int = 900
    height: int = 550

    # Plot-type specific options
    bins: int = 30                    # histogram bins
    window: int = 10                  # rolling mean window
    fft_log: bool = True              # FFT: log scale y-axis
    fft_sample_rate: float = 1.0      # FFT: sample rate in Hz
    trend_degree: int = 1             # trend: polynomial degree
    anomaly_threshold: float = 3.0    # anomaly: std devs
    show_grid: bool = True
    show_legend: bool = True
    opacity: float = 0.85
    marker_size: int = 6
    line_width: int = 2

    # Faceting / grouping (#6)
    facet_col: Optional[str] = None   # split into subplots by categorical column
    facet_row: Optional[str] = None   # split rows by categorical column (optional)

    # Export
    renderer: str = "plotly"          # "plotly" or "matplotlib"

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlotSpec":
        valid = {k: v for k, v in d.items()
                 if k in cls.__dataclass_fields__}
        return cls(**valid)


# ============================================================
# Shared column/alignment helpers
#
# Both renderers used to resolve trend columns with
#     y = spec.y_col or num[1] if len(num) > 1 else x
# which Python parses as (spec.y_col or num[1]) if len(num) > 1 else x — so a
# single-numeric-column frame DISCARDED an explicit y_col and fitted x against
# itself, reporting a perfect r=1.0 trend that looked entirely correct. They
# also dropna()'d x and y INDEPENDENTLY and truncated to the shorter, which
# silently misaligns every (x, y) pair when either column has a gap.
# ============================================================

def _resolve_trend_cols(spec, df):
    """(x_col, y_col) for a trend fit. An explicit y_col is never discarded,
    and x is never fitted against itself."""
    num = df.select_dtypes(include="number").columns
    if len(num) == 0:
        raise ValueError("A trend needs at least one numeric column.")
    x_col = spec.x_col or num[0]
    if spec.y_col:
        y_col = spec.y_col
    elif len(num) > 1:
        y_col = num[1]
    else:
        y_col = num[0]
    if x_col == y_col:
        raise ValueError(
            f"A trend needs two different columns — got '{x_col}' for both x "
            "and y. Pick a y column, or add a second numeric column. "
            "(Fitting a column against itself always reports a perfect "
            "r=1.0, so this refuses rather than showing a fake trend.)")
    for c in (x_col, y_col):
        if c not in df.columns:
            raise ValueError(f"Column {c!r} is not in this dataset.")
    return x_col, y_col


def _paired_xy(df, x_col, y_col, degree=1):
    """x/y values with NaNs dropped PAIRWISE, so the points stay aligned."""
    pair = df[[x_col, y_col]].dropna()
    try:
        xv = pair[x_col].to_numpy(dtype=float)
        yv = pair[y_col].to_numpy(dtype=float)
    except (TypeError, ValueError):
        raise ValueError(
            f"A trend needs numeric x and y — '{x_col}' and/or '{y_col}' "
            "is not numeric.")
    if len(xv) < degree + 1:
        raise ValueError(
            f"Not enough paired points to fit a degree-{degree} trend: "
            f"{len(xv)} row(s) have both '{x_col}' and '{y_col}'.")
    return xv, yv


def _selected_numeric(spec, num_df):
    """``num_df`` narrowed to spec.columns when the user chose some.

    Shared so the interactive and export paths cannot drift: the Plotly
    heatmap/correlation honoured spec.columns and the matplotlib EXPORT of the
    same chart did not, so picking 2 of 6 columns showed 2 on screen and
    exported all 6 — a different chart, saved as though it were the one you
    were looking at.
    """
    if not getattr(spec, "columns", None):
        return num_df
    cols = [c for c in spec.columns if c in num_df.columns]
    return num_df[cols] if cols else num_df


def _pca_matrix(spec, df):
    """(matrix, kept_index, note) for PCA — selected columns, THEN dropna.

    Both PCA paths did `df.select_dtypes("number").dropna()` and only
    subselected spec.columns AFTERWARDS, so a row was discarded because of a
    column the user never chose. One optional sensor logged 5% of the time
    dropped a 500-row study to 25 rows — and the explained-variance ratios were
    still reported as if the whole dataset had been used, with nothing on
    screen saying otherwise. Measured, not hypothesised.

    Selecting first means only the chosen columns can drop a row. The kept
    index comes back so a colour column can be aligned to the same rows, and
    the note states any drop on the chart rather than hiding it.
    """
    num_df = df.select_dtypes(include="number")
    if spec.columns:
        cols = [c for c in spec.columns if c in num_df.columns]
        if cols:
            num_df = num_df[cols]
    total = len(num_df)
    num_df = num_df.dropna()
    if num_df.shape[1] < 2:
        raise ValueError(
            "PCA needs at least two numeric columns — "
            f"got {num_df.shape[1]}.")
    if len(num_df) < 2:
        raise ValueError(
            "Not enough complete rows for PCA: only "
            f"{len(num_df)} row(s) have every selected column.")
    lost = total - len(num_df)
    note = (f"  ({lost} incomplete row(s) dropped)" if lost else "")
    return num_df, num_df.index, note


def _anomaly_parts(spec, df):
    """(x_values, y_values, mask) for an anomaly plot, all aligned to the rows
    that survive dropna() — the old code built the mask from the dropna()'d
    column but indexed the FULL-length frame with it, so one NaN broke it."""
    num = df.select_dtypes(include="number").columns
    if len(num) == 0:
        raise ValueError("An anomaly plot needs a numeric column.")
    col = spec.y_col or num[0]
    if col not in df.columns:
        raise ValueError(f"Column {col!r} is not in this dataset.")
    data = df[col].dropna()
    if data.empty:
        raise ValueError(f"Column {col!r} has no non-empty values.")
    mean, std = data.mean(), data.std()
    if not std or np.isnan(std):
        std = 0.0
    mask = (np.abs(data - mean) > spec.anomaly_threshold * std).to_numpy()
    if spec.x_col:
        if spec.x_col not in df.columns:
            raise ValueError(f"Column {spec.x_col!r} is not in this dataset.")
        x_vals = df.loc[data.index, spec.x_col]
    else:
        x_vals = data.index.to_series(index=data.index)
    return x_vals, data, mask, mean, std, col


# ============================================================
# Plotly renderer
# ============================================================

class PlotlyRenderer:
    """Renders a PlotSpec → Plotly HTML string."""

    def render(self, spec: PlotSpec, ds: DataSet) -> str:
        if not _PLOTLY_OK:
            return self._error_html("pip install plotly")
        if ds.df is None:
            return self._error_html(f"No data: {ds.load_error}")

        df = ds.df.copy()

        try:
            fig = self._dispatch(spec, df)
            # A plot method may set a title carrying COMPUTED information —
            # PCA's explained-variance ratios, the row count actually used.
            # This used to overwrite it unconditionally, so the most
            # informative thing about a PCA never reached the user. Precedence:
            # an explicit spec.title, else what the method computed, else a
            # generic label.
            try:
                computed = fig.layout.title.text
            except Exception:
                computed = None
            fig.update_layout(
                title=(spec.title or computed
                       or f"{spec.plot_type.replace('_',' ').title()} — {ds.name}"),
                template=spec.theme,
                width=spec.width,
                height=spec.height,
                showlegend=spec.show_legend,
                margin=dict(l=50, r=30, t=60, b=50),
            )
            return pio.to_html(fig, full_html=True, include_plotlyjs=_PLOTLY_JS)
        except Exception as e:
            return self._error_html(f"{spec.plot_type} failed: {e}")

    def _dispatch(self, spec: PlotSpec, df: Any) -> Any:
        t = spec.plot_type
        if t == "line":         return self._line(spec, df)
        if t == "bar":          return self._bar(spec, df)
        if t == "scatter":      return self._scatter(spec, df)
        if t == "histogram":    return self._histogram(spec, df)
        if t == "pie":          return self._pie(spec, df)
        if t == "area":         return self._area(spec, df)
        if t == "box":          return self._box(spec, df)
        if t == "violin":       return self._violin(spec, df)
        if t == "heatmap":      return self._heatmap(spec, df)
        if t == "correlation":  return self._correlation(spec, df)
        if t == "distribution": return self._distribution(spec, df)
        if t == "density_2d":   return self._density_2d(spec, df)
        if t == "parallel_coords": return self._parallel_coords(spec, df)
        if t == "fft":          return self._fft(spec, df)
        if t == "spectrogram":  return self._spectrogram(spec, df)
        if t == "polar":        return self._polar(spec, df)
        if t == "contour":      return self._contour(spec, df)
        if t == "surface_3d":   return self._surface_3d(spec, df)
        if t == "scatter_3d":   return self._scatter_3d(spec, df)
        if t == "timeseries":   return self._timeseries(spec, df)
        if t == "rolling_mean": return self._rolling_mean(spec, df)
        if t == "trend":        return self._trend(spec, df)
        if t == "anomaly":      return self._anomaly(spec, df)
        if t == "pca":          return self._pca(spec, df)
        if t == "facet":        return self._facet(spec, df)
        raise ValueError(f"Unknown plot type: {t!r}")

    # ── Basic ────────────────────────────────────────────────

    def _line(self, spec, df):
        # If facet_col set, delegate to px.line for faceting support
        if spec.facet_col and spec.y_col:
            return px.line(df, x=spec.x_col, y=spec.y_col,
                           color=spec.color_col,
                           facet_col=spec.facet_col,
                           facet_row=spec.facet_row or None,
                           line_group=spec.color_col,
                           labels={spec.x_col or "": spec.x_label,
                                   spec.y_col: spec.y_label or spec.y_col})
        cols = spec.columns or ([spec.y_col] if spec.y_col else df.select_dtypes(include="number").columns.tolist()[:4])
        x    = spec.x_col or df.index
        fig = go.Figure()
        for col in cols:
            if col in df.columns:
                fig.add_trace(go.Scatter(x=df[x] if spec.x_col else df.index,
                                         y=df[col], mode="lines",
                                         name=col, line=dict(width=spec.line_width)))
        fig.update_xaxes(title_text=spec.x_label or (spec.x_col or "Index"))
        fig.update_yaxes(title_text=spec.y_label)
        return fig

    def _bar(self, spec, df):
        x   = spec.x_col or df.columns[0]
        y   = spec.y_col or (df.select_dtypes(include="number").columns[0] if len(df.select_dtypes(include="number").columns) > 0 else df.columns[1])
        return px.bar(df, x=x, y=y, color=spec.color_col,
                      facet_col=spec.facet_col or None,
                      facet_row=spec.facet_row or None,
                      color_discrete_sequence=px.colors.qualitative.Plotly,
                      opacity=spec.opacity,
                      labels={x: spec.x_label or x, y: spec.y_label or y})

    def _scatter(self, spec, df):
        x = spec.x_col or df.select_dtypes(include="number").columns[0]
        y = spec.y_col or df.select_dtypes(include="number").columns[1]
        return px.scatter(df, x=x, y=y, color=spec.color_col,
                          size=spec.size_col,
                          facet_col=spec.facet_col or None,
                          facet_row=spec.facet_row or None,
                          color_continuous_scale=spec.color_scheme,
                          opacity=spec.opacity,
                          labels={x: spec.x_label or x, y: spec.y_label or y})

    def _histogram(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()[:3]
        fig = go.Figure()
        for col in cols:
            if col in df.columns:
                fig.add_trace(go.Histogram(x=df[col], name=col,
                                           nbinsx=spec.bins, opacity=spec.opacity))
        fig.update_layout(barmode="overlay")
        return fig

    def _pie(self, spec, df):
        vals  = spec.y_col or df.select_dtypes(include="number").columns[0]
        names = spec.x_col or df.columns[0]
        return px.pie(df, values=vals, names=names,
                      color_discrete_sequence=px.colors.qualitative.Plotly)

    def _area(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()[:4]
        x    = spec.x_col or df.index
        fig  = go.Figure()
        for col in cols:
            if col in df.columns:
                fig.add_trace(go.Scatter(x=df[x] if spec.x_col else df.index,
                                         y=df[col], fill="tozeroy",
                                         name=col, opacity=spec.opacity))
        return fig

    # ── Statistical ──────────────────────────────────────────

    def _box(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()
        fig  = go.Figure()
        for col in cols:
            if col in df.columns:
                fig.add_trace(go.Box(y=df[col], name=col,
                                     boxpoints="outliers"))
        return fig

    def _violin(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()
        fig  = go.Figure()
        for col in cols:
            if col in df.columns:
                fig.add_trace(go.Violin(y=df[col], name=col,
                                        box_visible=True,
                                        meanline_visible=True))
        return fig

    def _heatmap(self, spec, df):
        num_df = df.select_dtypes(include="number")
        if spec.columns:
            num_df = num_df[spec.columns]
        return px.imshow(num_df, color_continuous_scale=spec.color_scheme,
                         aspect="auto")

    def _correlation(self, spec, df):
        # Same helper as the matplotlib export, so the two cannot drift apart
        # again — they already had, and the export drew the wrong chart.
        num_df = _selected_numeric(spec, df.select_dtypes(include="number"))
        corr = num_df.corr()
        fig  = px.imshow(corr, color_continuous_scale="RdBu_r",
                         zmin=-1, zmax=1, text_auto=".2f",
                         aspect="equal")
        return fig

    def _distribution(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()[:3]
        fig  = go.Figure()
        for col in cols:
            if col not in df.columns:
                continue
            x = df[col].dropna()
            if _SCIPY_OK:
                from scipy import stats  # deferred — see probe at module top
                kde_x = np.linspace(x.min(), x.max(), 300)
                kde   = stats.gaussian_kde(x)
                fig.add_trace(go.Scatter(x=kde_x, y=kde(kde_x),
                                         name=f"{col} KDE", mode="lines"))
            fig.add_trace(go.Histogram(x=x, histnorm="probability density",
                                       name=col, opacity=0.4,
                                       nbinsx=spec.bins))
        fig.update_layout(barmode="overlay")
        return fig

    def _density_2d(self, spec, df):
        x = spec.x_col or df.select_dtypes(include="number").columns[0]
        y = spec.y_col or df.select_dtypes(include="number").columns[1]
        return px.density_contour(df, x=x, y=y, color=spec.color_col,
                                  marginal_x="histogram",
                                  marginal_y="histogram",
                                  color_discrete_sequence=px.colors.qualitative.Plotly)

    def _parallel_coords(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()
        return px.parallel_coordinates(df, dimensions=cols,
                                       color=spec.color_col or cols[0],
                                       color_continuous_scale=spec.color_scheme)

    # ── Scientific ───────────────────────────────────────────

    def _fft(self, spec, df):
        col = spec.y_col or df.select_dtypes(include="number").columns[0]
        sig = df[col].dropna().values
        N   = len(sig)
        sr  = spec.fft_sample_rate
        fft_vals  = np.abs(np.fft.rfft(sig))
        fft_freqs = np.fft.rfftfreq(N, d=1.0 / sr)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=fft_freqs, y=fft_vals,
                                  mode="lines", name="FFT magnitude"))
        if spec.fft_log:
            fig.update_yaxes(type="log")
        fig.update_xaxes(title_text="Frequency (Hz)")
        fig.update_yaxes(title_text="Magnitude")
        return fig

    def _spectrogram(self, spec, df):
        if not _SCIPY_OK:
            raise ImportError("pip install scipy")
        from scipy import signal  # deferred — see probe at module top
        col = spec.y_col or df.select_dtypes(include="number").columns[0]
        sig = df[col].dropna().values.astype(float)
        sr  = spec.fft_sample_rate
        f, t, Sxx = signal.spectrogram(sig, fs=sr)
        fig = px.imshow(10 * np.log10(Sxx + 1e-10),
                        x=t, y=f, origin="lower",
                        color_continuous_scale=spec.color_scheme,
                        labels={"x": "Time (s)", "y": "Frequency (Hz)",
                                "color": "Power (dB)"},
                        aspect="auto")
        return fig

    def _polar(self, spec, df):
        r    = spec.y_col or df.select_dtypes(include="number").columns[0]
        theta = spec.x_col or (df.select_dtypes(include="number").columns[1]
                                if len(df.select_dtypes(include="number").columns) > 1
                                else df.index)
        fig = go.Figure()
        fig.add_trace(go.Scatterpolar(
            r=df[r], theta=df[theta] if spec.x_col else np.linspace(0, 360, len(df)),
            mode="lines", name=r,
        ))
        return fig

    def _contour(self, spec, df):
        """Contour Z over (X, Y) — and it now actually contours Z.

        `z` was resolved on its own line and then dropped on the floor: the
        call was `px.density_contour(df, x=x, y=y)` with no z at all, which
        contours the DENSITY OF POINTS — where the samples happen to sit, not
        what they measured.

        That is not a near-miss, it is a different question, and the two are
        routinely ANTI-correlated. Measured on a melt-pool field sampled
        densely in the cold corner and sparsely in the hot one, "contour
        temp_C" drew its peak over the COLDEST region. Nothing on the plot
        said otherwise — the colour bar was an unlabelled count, so the plot
        was indistinguishable from a correct one and read as the answer.
        """
        num_cols = df.select_dtypes(include="number").columns.tolist()
        if len(num_cols) < 2:
            raise ValueError(
                "A contour plot needs at least two numeric columns for the x "
                f"and y axes; this dataset has {len(num_cols)}.")
        x = spec.x_col or num_cols[0]
        y = spec.y_col or num_cols[1]
        # Fall back to None, NOT num_cols[0] — the old fallback made z the same
        # column as x whenever there were only two numeric columns, so the
        # "height" was the horizontal axis.
        z = spec.z_col or (num_cols[2] if len(num_cols) > 2 else None)
        for col in (x, y, z):
            if col is not None and col not in df.columns:
                raise ValueError(f"Column {col!r} is not in this dataset.")

        if z is None:
            # No third variable exists, so point density is the only thing
            # there is to contour. That is a fine plot — it just has to say so
            # rather than let a count masquerade as a measurement.
            fig = px.density_contour(
                df, x=x, y=y,
                color_discrete_sequence=px.colors.qualitative.Plotly)
            fig.update_traces(contours_coloring="fill", contours_showlabels=True,
                              colorbar_title_text="count")
            fig.update_layout(title=f"Density of points — {y} vs {x}")
            return fig

        # histfunc="avg" bins (x, y) and averages z inside each bin, which is
        # the right reduction for scattered samples of a field.
        fig = px.density_contour(df, x=x, y=y, z=df[z], histfunc="avg")
        fig.update_traces(contours_coloring="fill", contours_showlabels=True,
                          colorbar_title_text=z)
        fig.update_layout(title=f"{z} over {x} / {y} (mean per bin)")
        return fig

    def _surface_3d(self, spec, df):
        num_df = df.select_dtypes(include="number")
        if spec.columns:
            num_df = num_df[[c for c in spec.columns if c in num_df.columns]]
        z = num_df.values
        fig = go.Figure(data=[go.Surface(z=z,
                                          colorscale=spec.color_scheme)])
        fig.update_layout(scene=dict(
            xaxis_title=spec.x_label or "X",
            yaxis_title=spec.y_label or "Y",
            zaxis_title=spec.z_col or "Z",
        ))
        return fig

    def _scatter_3d(self, spec, df):
        num_cols = df.select_dtypes(include="number").columns.tolist()
        x = spec.x_col or num_cols[0]
        y = spec.y_col or num_cols[1]
        z = spec.z_col or (num_cols[2] if len(num_cols) > 2 else num_cols[0])
        return px.scatter_3d(df, x=x, y=y, z=z,
                             color=spec.color_col,
                             size=spec.size_col,
                             color_continuous_scale=spec.color_scheme,
                             opacity=spec.opacity)

    # ── Time series ──────────────────────────────────────────

    def _timeseries(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()[:4]
        x    = spec.x_col
        fig  = go.Figure()
        for col in cols:
            if col not in df.columns:
                continue
            fig.add_trace(go.Scatter(
                x=df[x] if x else df.index, y=df[col],
                mode="lines", name=col,
                line=dict(width=spec.line_width),
            ))
        fig.update_xaxes(title_text=spec.x_label or (x or "Index"),
                         rangeslider_visible=True)
        return fig

    def _rolling_mean(self, spec, df):
        cols = spec.columns or df.select_dtypes(include="number").columns.tolist()[:3]
        x    = spec.x_col
        fig  = go.Figure()
        for col in cols:
            if col not in df.columns:
                continue
            series = df[col]
            rolled = series.rolling(window=spec.window, center=True).mean()
            fig.add_trace(go.Scatter(
                x=df[x] if x else df.index, y=series,
                mode="lines", name=f"{col} (raw)",
                opacity=0.4, line=dict(width=1),
            ))
            fig.add_trace(go.Scatter(
                x=df[x] if x else df.index, y=rolled,
                mode="lines", name=f"{col} (rolling {spec.window})",
                line=dict(width=spec.line_width + 1),
            ))
        return fig

    def _trend(self, spec, df):
        x_col, y_col = _resolve_trend_cols(spec, df)
        x_vals, y_vals = _paired_xy(df, x_col, y_col, spec.trend_degree)

        coeffs = np.polyfit(x_vals, y_vals, spec.trend_degree)
        # Sort for the fitted line, else it zig-zags between unordered points.
        order   = np.argsort(x_vals)
        trend_x = x_vals[order]
        trend   = np.polyval(coeffs, trend_x)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_vals, y=y_vals,
                                  mode="markers", name="Data",
                                  marker=dict(size=spec.marker_size,
                                              opacity=spec.opacity)))
        fig.add_trace(go.Scatter(x=trend_x, y=trend,
                                  mode="lines", name=f"Trend (deg {spec.trend_degree})",
                                  line=dict(width=spec.line_width + 1,
                                            dash="dash")))
        fig.update_layout(xaxis_title=spec.x_label or x_col,
                          yaxis_title=spec.y_label or y_col)
        return fig

    def _anomaly(self, spec, df):
        x_vals, data, mask, mean, std, col = _anomaly_parts(spec, df)
        threshold = spec.anomaly_threshold

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_vals[~mask], y=data[~mask],
                                  mode="markers", name="Normal",
                                  marker=dict(size=spec.marker_size)))
        fig.add_trace(go.Scatter(x=x_vals[mask],  y=data[mask],
                                  mode="markers", name=f"Anomaly (>{threshold}σ)",
                                  marker=dict(size=spec.marker_size + 4,
                                              color="red", symbol="x")))
        fig.add_hline(y=mean + threshold * std, line_dash="dash",
                      annotation_text=f"+{threshold}σ", line_color="orange")
        fig.add_hline(y=mean - threshold * std, line_dash="dash",
                      annotation_text=f"-{threshold}σ", line_color="orange")
        return fig

    def _pca(self, spec, df):
        if not _SKLEARN_OK:
            raise ImportError("pip install scikit-learn")
        num_df, kept, dropped = _pca_matrix(spec, df)
        scaler     = StandardScaler()
        scaled     = scaler.fit_transform(num_df)
        n_comp     = min(3, scaled.shape[1])
        pca        = PCA(n_components=n_comp)
        components = pca.fit_transform(scaled)
        explained  = pca.explained_variance_ratio_

        # The colour column must come from the SAME rows the components did.
        # Reading it off the full frame produced a length mismatch the moment
        # any row was dropped.
        colour = None
        if spec.color_col and spec.color_col in df.columns:
            colour = df.loc[kept, spec.color_col].values

        if n_comp >= 3:
            pca_df = pd.DataFrame(components, columns=["PC1", "PC2", "PC3"])
            fig = px.scatter_3d(pca_df, x="PC1", y="PC2", z="PC3",
                                color=colour, opacity=spec.opacity)
        else:
            pca_df = pd.DataFrame(components, columns=["PC1", "PC2"][:n_comp])
            fig    = px.scatter(pca_df, x="PC1", y="PC2",
                                color=colour, opacity=spec.opacity)
        evr_str = "  ".join(f"PC{i+1}={v:.1%}" for i, v in enumerate(explained))
        fig.update_layout(title=f"PCA ({len(num_df)} rows) — "
                                f"Explained variance: {evr_str}{dropped}")
        return fig

    def _facet(self, spec, df):
        """Faceted scatter/histogram: split by a categorical column into subplots."""
        facet = spec.facet_col or (df.select_dtypes(exclude="number").columns[0]
                                   if len(df.select_dtypes(exclude="number").columns) > 0 else None)
        x = spec.x_col or (df.select_dtypes(include="number").columns[0]
                           if len(df.select_dtypes(include="number").columns) > 0 else df.columns[0])
        y = spec.y_col or (df.select_dtypes(include="number").columns[1]
                           if len(df.select_dtypes(include="number").columns) > 1 else None)
        if y:
            return px.scatter(df, x=x, y=y, facet_col=facet,
                              color=spec.color_col,
                              opacity=spec.opacity,
                              color_continuous_scale=spec.color_scheme)
        # Fall back to histogram facets when only one numeric col
        return px.histogram(df, x=x, facet_col=facet,
                            nbins=spec.bins, opacity=spec.opacity,
                            color_discrete_sequence=px.colors.qualitative.Plotly)


    def _overlay_render(self, spec: PlotSpec, ds: "DataSet", overlay_ds: "DataSet") -> str:
        """
        Multi-file overlay (#10): render spec on ds, then add overlay_ds traces.
        Works for line, scatter, area, timeseries.
        """
        if not _PLOTLY_OK:
            return self._error_html("pip install plotly")
        try:
            fig = self._dispatch(spec, ds.df.copy())
            # Add overlay traces for y_col (or all numeric cols)
            ov_df = overlay_ds.df.copy()
            y_cols = spec.columns if spec.columns else ([spec.y_col] if spec.y_col in ov_df.columns else
                      ov_df.select_dtypes(include="number").columns.tolist()[:3])
            x_ov = spec.x_col if spec.x_col and spec.x_col in ov_df.columns else None
            for col in y_cols:
                if col not in ov_df.columns:
                    continue
                x_data = ov_df[x_ov] if x_ov else ov_df.index
                fig.add_trace(go.Scatter(
                    x=x_data, y=ov_df[col],
                    mode="lines+markers",
                    name=f"{overlay_ds.name}: {col}",
                    line=dict(dash="dash"),
                    opacity=spec.opacity,
                ))
            fig.update_layout(
                title=spec.title or f"Overlay — {ds.name} vs {overlay_ds.name}",
                template=spec.theme,
                width=spec.width, height=spec.height,
                showlegend=True,
            )
            return pio.to_html(fig, full_html=True, include_plotlyjs=_PLOTLY_JS)
        except Exception as e:
            return self._error_html(f"Overlay failed: {e}")

    def _error_html(self, msg: str) -> str:
        return f"""<html><body style="background:#1e1e2e;color:#f38ba8;
            font-family:monospace;padding:20px">
            <h3>Plot Error</h3><pre>{msg}</pre></body></html>"""


# ============================================================
# Matplotlib renderer
# ============================================================

class MatplotlibRenderer:
    """Renders a PlotSpec → Matplotlib Figure for export."""

    COLORMAPS = {
        "viridis": "viridis", "plasma": "plasma",
        "inferno": "inferno", "magma": "magma",
        "Blues":   "Blues",   "Reds":  "Reds",
    }

    def render(self, spec: PlotSpec, ds: DataSet) -> Optional[Figure]:
        if not _MPL_OK:
            return None
        if ds.df is None:
            return None
        df  = ds.df.copy()
        fig = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                fig = self._dispatch(spec, df)
            except UnsupportedPlotType:
                # Return None so the caller's "export failed" branch fires with
                # the truth, instead of writing a PNG of the wrong chart.
                return None
            except Exception as e:
                fig, ax = plt.subplots(figsize=(8, 4))
                ax.text(0.5, 0.5, f"Plot error:\n{e}",
                        ha="center", va="center", transform=ax.transAxes,
                        color="red", fontsize=12)
                ax.axis("off")

        if fig and spec.title:
            fig.suptitle(spec.title or f"{spec.plot_type.replace('_',' ').title()} — {ds.name}",
                         fontsize=13)
        return fig

    def _dispatch(self, spec, df):
        t = spec.plot_type
        num = df.select_dtypes(include="number")

        if t == "line":
            fig, ax = plt.subplots(figsize=(10, 5))
            cols = spec.columns or num.columns.tolist()[:5]
            x    = df[spec.x_col] if spec.x_col else df.index
            for col in cols:
                if col in df.columns:
                    ax.plot(x, df[col], label=col, linewidth=spec.line_width)
            ax.set_xlabel(spec.x_label or (spec.x_col or "Index"))
            ax.set_ylabel(spec.y_label)
            ax.legend()
            ax.grid(spec.show_grid)
            return fig

        if t == "bar":
            fig, ax = plt.subplots(figsize=(10, 5))
            x = spec.x_col or df.columns[0]
            y = spec.y_col or num.columns[0]
            ax.bar(df[x].astype(str), df[y], alpha=spec.opacity)
            ax.set_xlabel(spec.x_label or x)
            ax.set_ylabel(spec.y_label or y)
            plt.xticks(rotation=45, ha="right")
            fig.tight_layout()
            return fig

        if t == "scatter":
            fig, ax = plt.subplots(figsize=(8, 6))
            x = spec.x_col or num.columns[0]
            y = spec.y_col or num.columns[1]
            sc = ax.scatter(df[x], df[y], alpha=spec.opacity,
                            s=spec.marker_size * 3,
                            c=df[spec.color_col] if spec.color_col else None,
                            cmap=spec.color_scheme)
            if spec.color_col:
                plt.colorbar(sc, ax=ax, label=spec.color_col)
            ax.set_xlabel(spec.x_label or x)
            ax.set_ylabel(spec.y_label or y)
            ax.grid(spec.show_grid)
            return fig

        if t == "histogram":
            cols = spec.columns or num.columns.tolist()[:3]
            fig, ax = plt.subplots(figsize=(8, 5))
            for col in cols:
                if col in df.columns:
                    ax.hist(df[col].dropna(), bins=spec.bins,
                            alpha=0.6, label=col)
            ax.legend()
            ax.grid(spec.show_grid)
            return fig

        if t == "box":
            cols = spec.columns or num.columns.tolist()
            fig, ax = plt.subplots(figsize=(max(6, len(cols) * 1.2), 6))
            ax.boxplot([df[c].dropna() for c in cols if c in df.columns],
                       labels=cols, patch_artist=True)
            plt.xticks(rotation=45, ha="right")
            fig.tight_layout()
            return fig

        if t == "violin":
            cols = spec.columns or num.columns.tolist()
            data = [df[c].dropna().values for c in cols if c in df.columns]
            fig, ax = plt.subplots(figsize=(max(6, len(cols) * 1.2), 6))
            vp = ax.violinplot(data, showmeans=True, showmedians=True)
            ax.set_xticks(range(1, len(cols) + 1))
            ax.set_xticklabels(cols, rotation=45, ha="right")
            fig.tight_layout()
            return fig

        if t == "heatmap":
            # Honour the user's column selection, exactly as the Plotly twin
            # does. This branch used every numeric column in the frame, so the
            # PNG you exported was a different chart from the one on screen:
            # pick 2 of 6 columns, see 2 interactively, export 6. The same
            # feature implemented twice, and the copies disagreed.
            num = _selected_numeric(spec, num)
            fig, ax = plt.subplots(figsize=(10, 8))
            data_m = num.values
            im = ax.imshow(data_m, cmap=spec.color_scheme, aspect="auto")
            plt.colorbar(im, ax=ax)
            ax.set_xticks(range(len(num.columns)))
            ax.set_xticklabels(num.columns, rotation=45, ha="right")
            fig.tight_layout()
            return fig

        if t == "correlation":
            corr  = _selected_numeric(spec, num).corr()
            fig, ax = plt.subplots(figsize=(max(6, len(corr) * 0.8),
                                            max(6, len(corr) * 0.8)))
            im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
            plt.colorbar(im, ax=ax)
            ax.set_xticks(range(len(corr.columns)))
            ax.set_yticks(range(len(corr.columns)))
            ax.set_xticklabels(corr.columns, rotation=45, ha="right")
            ax.set_yticklabels(corr.columns)
            for i in range(len(corr)):
                for j in range(len(corr)):
                    ax.text(j, i, f"{corr.iloc[i, j]:.2f}",
                            ha="center", va="center", fontsize=7)
            fig.tight_layout()
            return fig

        if t in ("fft", "spectrogram"):
            col = spec.y_col or num.columns[0]
            sig = df[col].dropna().values.astype(float)
            if t == "fft":
                fig, ax = plt.subplots(figsize=(10, 5))
                N     = len(sig)
                freqs = np.fft.rfftfreq(N, d=1.0 / spec.fft_sample_rate)
                mags  = np.abs(np.fft.rfft(sig))
                ax.plot(freqs, mags)
                if spec.fft_log:
                    ax.set_yscale("log")
                ax.set_xlabel("Frequency (Hz)")
                ax.set_ylabel("Magnitude")
                ax.grid(True)
                return fig
            else:
                if not _SCIPY_OK:
                    raise ImportError("pip install scipy")
                fig, ax = plt.subplots(figsize=(10, 5))
                ax.specgram(sig, Fs=spec.fft_sample_rate, cmap=spec.color_scheme)
                ax.set_xlabel("Time (s)")
                ax.set_ylabel("Frequency (Hz)")
                return fig

        if t == "surface_3d":
            from mpl_toolkits.mplot3d import Axes3D
            fig = plt.figure(figsize=(10, 7))
            ax  = fig.add_subplot(111, projection="3d")
            z   = num.values
            x   = np.arange(z.shape[1])
            y   = np.arange(z.shape[0])
            X, Y = np.meshgrid(x, y)
            ax.plot_surface(X, Y, z, cmap=spec.color_scheme, alpha=spec.opacity)
            return fig

        if t in ("timeseries", "rolling_mean", "trend", "anomaly"):
            return self._time_mpl(spec, df, t)

        if t == "pca":
            return self._pca_mpl(spec, df)

        # No silent fallback: substituting a generic numeric plot here made a
        # 'pie' export come back as 5 overlaid line series, reported as success.
        raise UnsupportedPlotType(
            f"'{t}' has no static (matplotlib) export. Supported: "
            f"{', '.join(sorted(MPL_SUPPORTED))}. Use the interactive chart, "
            "or save it as HTML instead.")

    def _time_mpl(self, spec, df, t):
        num  = df.select_dtypes(include="number")
        cols = spec.columns or num.columns.tolist()[:3]
        x    = df[spec.x_col] if spec.x_col else df.index
        fig, ax = plt.subplots(figsize=(12, 5))

        if t == "trend":
            xc, yc = _resolve_trend_cols(spec, df)
            xv, yv = _paired_xy(df, xc, yc, spec.trend_degree)
            ax.scatter(xv, yv, alpha=0.5, s=10, label="Data")
            coeffs = np.polyfit(xv, yv, spec.trend_degree)
            order  = np.argsort(xv)
            ax.plot(xv[order], np.polyval(coeffs, xv[order]), "r--",
                    label=f"Trend (deg {spec.trend_degree})", linewidth=2)
            ax.set_xlabel(xc)
            ax.set_ylabel(yc)
        elif t == "anomaly":
            x_vals, data, mask, mean, std, col = _anomaly_parts(spec, df)
            ax.plot(x_vals, data, alpha=0.6, label=col)
            ax.scatter(x_vals[mask], data[mask],
                       color="red", zorder=5, s=40,
                       label=f"Anomaly (>{spec.anomaly_threshold}σ)")
            ax.axhline(mean + spec.anomaly_threshold * std,
                       ls="--", c="orange", alpha=0.7)
            ax.axhline(mean - spec.anomaly_threshold * std,
                       ls="--", c="orange", alpha=0.7)
        else:
            for col in cols:
                if col not in df.columns:
                    continue
                ax.plot(x, df[col], alpha=0.4, linewidth=1, label=col)
                if t == "rolling_mean":
                    rolled = df[col].rolling(window=spec.window, center=True).mean()
                    ax.plot(x, rolled, linewidth=spec.line_width + 1,
                            label=f"{col} (rolling {spec.window})")

        ax.legend()
        ax.grid(spec.show_grid)
        fig.tight_layout()
        return fig

    def _pca_mpl(self, spec, df):
        if not _SKLEARN_OK:
            raise ImportError("pip install scikit-learn")
        num, _kept, _note = _pca_matrix(spec, df)
        scaled     = StandardScaler().fit_transform(num)
        n_comp     = min(3, scaled.shape[1])
        pca        = PCA(n_components=n_comp)
        components = pca.fit_transform(scaled)
        evr        = pca.explained_variance_ratio_

        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(components[:, 0], components[:, 1],
                   alpha=spec.opacity, s=spec.marker_size * 3)
        ax.set_xlabel(f"PC1 ({evr[0]:.1%})")
        ax.set_ylabel(f"PC2 ({evr[1]:.1%})" if n_comp > 1 else "")
        ax.grid(spec.show_grid)
        return fig

    def save(self, fig: Figure, path: Path, dpi: int = 150) -> Path:
        """Save figure to file. Supports PNG, SVG, PDF."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(path), dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        return path

    def to_bytes(self, fig: Figure, fmt: str = "png", dpi: int = 150) -> bytes:
        """Return figure as bytes (for embedding in Tkinter)."""
        buf = io.BytesIO()
        fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()


# ============================================================
# Data transform pipeline (#4)
# ============================================================

def apply_transforms(df: Any, transforms: list) -> tuple:
    """
    Apply a list of pre-plot transform steps to a DataFrame copy.
    Each step is a dict: {"op": <name>, "cols": [...], "params": {...}}

    Supported ops:
      normalize     — (x - min) / (max - min) per column
      standardize   — (x - mean) / std per column
      log           — log1p(x) for numeric columns (handles zeros)
      fill_nan      — fill NaN with value (params: {"value": 0})
      clip_outliers — clip values beyond N std devs (params: {"sigma": 3})
      resample      — resample time-index to freq (params: {"freq": "1D"})
      derive        — add a new column via expression (params: {"name": "col", "expr": "col_a / col_b"})
      drop_col      — drop one or more columns (params: {"cols": [...]})

    Returns (transformed_df, log_messages).
    """
    if not _PANDAS_OK:
        return df, ["pandas not available"]
    df = df.copy()
    log: list = []
    for step in transforms:
        op     = step.get("op", "")
        cols   = step.get("cols", [])
        params = step.get("params", {})
        try:
            if op == "normalize":
                target = [c for c in (cols or df.select_dtypes(include="number").columns.tolist()) if c in df]
                for c in target:
                    mn, mx = df[c].min(), df[c].max()
                    df[c] = (df[c] - mn) / (mx - mn) if mx != mn else 0.0
                log.append(f"normalize: {target}")
            elif op == "standardize":
                target = [c for c in (cols or df.select_dtypes(include="number").columns.tolist()) if c in df]
                for c in target:
                    df[c] = (df[c] - df[c].mean()) / (df[c].std() + 1e-12)
                log.append(f"standardize: {target}")
            elif op == "log":
                target = [c for c in (cols or df.select_dtypes(include="number").columns.tolist()) if c in df]
                for c in target:
                    df[c] = np.log1p(df[c].clip(lower=0))
                log.append(f"log1p: {target}")
            elif op == "fill_nan":
                val = params.get("value", 0)
                target = [c for c in (cols or df.columns.tolist()) if c in df]
                df[target] = df[target].fillna(val)
                log.append(f"fill_nan={val}: {target}")
            elif op == "clip_outliers":
                sigma  = float(params.get("sigma", 3.0))
                target = [c for c in (cols or df.select_dtypes(include="number").columns.tolist()) if c in df]
                for c in target:
                    mean, std = df[c].mean(), df[c].std()
                    df[c] = df[c].clip(mean - sigma * std, mean + sigma * std)
                log.append(f"clip {sigma}σ: {target}")
            elif op == "resample":
                freq = params.get("freq", "1D")
                df.index = pd.to_datetime(df.index, errors="coerce")
                df = df.resample(freq).mean()
                log.append(f"resample: {freq}")
            elif op == "derive":
                name = params.get("name", "derived")
                expr = params.get("expr", "")
                df[name] = df.eval(expr)
                log.append(f"derive {name}={expr}")
            elif op == "drop_col":
                drop = params.get("cols", cols)
                df = df.drop(columns=[c for c in drop if c in df], errors="ignore")
                log.append(f"drop: {drop}")
        except Exception as e:
            log.append(f"⚠ {op} failed: {e}")
    return df, log


# ============================================================
# Analysis utilities (text output alongside plots)
# ============================================================

class DataAnalyser:
    """
    Computes text summaries of data for display alongside plots.
    Used by the AI layer and the GUI stats panel.
    """

    @staticmethod
    def describe(ds: DataSet) -> str:
        if ds.df is None:
            return f"No data: {ds.load_error}"
        if not _PANDAS_OK:
            return "pandas not installed"
        lines = [f"=== {ds.name} ==="]
        try:
            desc = ds.df.describe(include="all").to_string()
            lines.append(desc)
        except Exception as e:
            lines.append(f"describe() error: {e}")
        return "\n".join(lines)

    @staticmethod
    def detect_anomalies(ds: DataSet, col: str,
                         threshold: float = 3.0) -> str:
        if ds.df is None or col not in ds.df.columns:
            return ""
        data  = ds.df[col].dropna()
        mean, std = data.mean(), data.std()
        mask  = np.abs(data - mean) > threshold * std
        count = int(mask.sum())
        pct   = count / len(data) * 100
        return (f"Anomaly detection: {col}\n"
                f"  Threshold: ±{threshold}σ  (mean={mean:.4g}, std={std:.4g})\n"
                f"  Anomalies: {count} rows ({pct:.1f}%)\n"
                f"  Row indices: {list(data.index[mask][:20])}")

    @staticmethod
    def fft_summary(ds: DataSet, col: str,
                    sample_rate: float = 1.0) -> str:
        if ds.df is None or col not in ds.df.columns:
            return ""
        sig   = ds.df[col].dropna().values.astype(float)
        N     = len(sig)
        freqs = np.fft.rfftfreq(N, d=1.0 / sample_rate)
        mags  = np.abs(np.fft.rfft(sig))
        top_n = 5
        top_idx = np.argsort(mags)[::-1][:top_n]
        lines = [f"FFT summary: {col}  (N={N}, sample_rate={sample_rate} Hz)"]
        for i, idx in enumerate(top_idx):
            lines.append(f"  Peak {i+1}: f={freqs[idx]:.4g} Hz  "
                         f"magnitude={mags[idx]:.4g}")
        return "\n".join(lines)

    @staticmethod
    def correlation_summary(ds: DataSet) -> str:
        if ds.df is None:
            return ""
        num = ds.df.select_dtypes(include="number")
        if num.shape[1] < 2:
            return "Not enough numeric columns for correlation."
        corr = num.corr()
        lines = ["Top correlations:"]
        pairs = []
        for i in range(len(corr.columns)):
            for j in range(i + 1, len(corr.columns)):
                pairs.append((corr.iloc[i, j],
                               corr.columns[i], corr.columns[j]))
        pairs.sort(key=lambda x: abs(x[0]), reverse=True)
        for r, a, b in pairs[:10]:
            lines.append(f"  {a} ↔ {b}: r={r:.3f}")
        return "\n".join(lines)
