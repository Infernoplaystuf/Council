# ============================================================
# graph_personality.py  —  AI-assisted graphing for the council
# ============================================================
# The Analyst role takes a natural language description +
# a DataSet summary and outputs a PlotSpec JSON.
# It also generates text analysis alongside plots.
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from graph_data import DataSet
from graph_engine import PlotSpec, PLOT_TYPES, DataAnalyser

import council_engine as ce


# ============================================================
# System prompt
# ============================================================

def _build_analyst_system_prompt() -> str:
    plot_list = "\n".join(f"  - {p}" for p in PLOT_TYPES)
    return f"""You are the DATA ANALYST — a council role specialising in data visualisation and analysis.
Given a dataset description and a natural language request, you output a PlotSpec JSON
that the graph engine will render directly.

=== OUTPUT CONTRACT ===
Respond with a JSON object only. No prose, no markdown fences.
Always include a "analysis" key with a plain-text analytical insight (2-5 sentences).

=== PLOTSPEC SCHEMA ===
{{
  "plot_type":     string,          // REQUIRED — see list below
  "x_col":         string | null,   // column name for X axis
  "y_col":         string | null,   // column name for Y axis
  "z_col":         string | null,   // column name for Z (3D / contour)
  "color_col":     string | null,   // column name for color grouping
  "size_col":      string | null,   // column name for marker size
  "columns":       [string],        // multi-column plots (box, violin, heatmap, etc.)
  "title":         string,
  "x_label":       string,
  "y_label":       string,
  "color_scheme":  string,          // e.g. "viridis", "plasma", "RdBu_r"
  "bins":          integer,         // histogram bin count
  "window":        integer,         // rolling mean window size
  "fft_log":       boolean,         // FFT: log-scale y axis
  "fft_sample_rate": number,        // FFT: sample rate in Hz
  "trend_degree":  integer,         // trend polynomial degree (1=linear, 2=quadratic)
  "anomaly_threshold": number,      // anomaly: standard deviation multiplier
  "marker_size":   integer,
  "line_width":    integer,
  "opacity":       number,          // 0.0–1.0
  "renderer":      "plotly",        // always "plotly" unless user asks for export
  "analysis":      string           // plain English insight about the data
}}

=== AVAILABLE PLOT TYPES ===
{plot_list}

=== PLOT TYPE SELECTION GUIDE ===
- line / area / timeseries: ordered sequential data, trends over index or time
- bar: comparing discrete categories
- scatter: relationship between two numeric variables
- histogram / distribution: shape of a single variable's distribution
- box / violin: distribution comparison across groups or columns
- heatmap: matrix visualisation of values across rows × columns
- correlation: pairwise correlation matrix of all numeric columns
- density_2d: joint distribution of two variables
- parallel_coords: multi-dimensional comparison (5+ columns)
- fft: frequency content of a signal — use when data is a time-domain signal
- spectrogram: how frequency content changes over time
- polar: circular/angular data
- contour: 2D distribution or z=f(x,y) on a grid
- surface_3d / scatter_3d: three-variable relationships
- rolling_mean: noisy time series that needs smoothing
- trend: fitting a polynomial trend line to data
- anomaly: finding outliers using standard deviation threshold
- pca: dimensionality reduction for high-dimensional numeric data

=== COLOUR SCHEMES ===
Sequential: viridis, plasma, inferno, magma, Blues, Reds, Greens, YlOrRd
Diverging:  RdBu_r, RdYlGn, spectral
Qualitative: auto-selected by Plotly for categorical data

=== ANALYSIS GUIDANCE ===
In the "analysis" field, state:
1. What the plot reveals about the data structure
2. Any notable patterns, outliers, or relationships visible
3. What further analysis might be useful
Be specific — reference actual column names and value ranges from the dataset summary.

=== RULES ===
1. Only use column names that appear in the dataset summary
2. For FFT/spectrogram: set fft_sample_rate based on context (default 1.0 if unknown)
3. For rolling_mean: suggest window = ~5% of row count, minimum 3
4. For anomaly: threshold 2.5–3.5σ is typical; use lower for sensitive detection
5. For PCA: only applicable when there are 3+ numeric columns
6. For surface_3d: best when data forms a 2D grid (rows × columns of values)
7. Always pick the plot type that answers the user's question most directly
"""


# ============================================================
# Analyst personality
# ============================================================

@dataclass
class AnalystResult:
    spec: Optional[PlotSpec]
    analysis: str
    raw_json: str
    parse_error: str = ""


class AnalystPersonality:
    """
    Council Analyst role: natural language → PlotSpec + analysis text.
    Wraps a PersonalityModel (uses Writer as base).
    """

    def __init__(
        self,
        personality_model: ce.PersonalityModel,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model    = personality_model
        self._emit_cb = event_callback
        self.model.extra_context = _build_analyst_system_prompt()

    def _emit(self, phase: str, msg: str):
        if self._emit_cb:
            self._emit_cb(phase, msg)

    def analyse(self, request: str, dataset: DataSet) -> AnalystResult:
        """
        Given a natural language request and a loaded DataSet,
        return a PlotSpec ready for rendering + analytical insight.
        """
        self._emit("analyst_start", f"Analysing: {request[:80]}")

        prompt = self._build_prompt(request, dataset)
        raw    = self.model.respond(prompt)

        self._emit("analyst_raw", f"Response ({len(raw)} chars)")

        return self._parse(raw)

    def _build_prompt(self, request: str, ds: DataSet) -> str:
        return (
            f"DATASET SUMMARY:\n{ds.summary()}\n\n"
            f"FIRST 5 ROWS:\n{ds.head_str(5)}\n\n"
            f"USER REQUEST:\n{request}\n\n"
            f"Respond with a PlotSpec JSON for this request."
        )

    def _parse(self, raw: str) -> AnalystResult:
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        # Extract first {...} block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return AnalystResult(
                spec=None, analysis="", raw_json=raw,
                parse_error="No JSON object found in response",
            )
        try:
            data     = json.loads(match.group(0))
            analysis = data.pop("analysis", "")
            spec     = PlotSpec.from_dict(data)
            self._emit("analyst_done",
                       f"✓ Plot type: {spec.plot_type} | {analysis[:80]}")
            return AnalystResult(spec=spec, analysis=analysis, raw_json=raw)
        except Exception as e:
            return AnalystResult(
                spec=None, analysis="", raw_json=raw,
                parse_error=str(e),
            )

    def respond(self, prompt: str, **kwargs) -> str:
        """Council orchestrator compatibility."""
        # Try to extract DataSet from kwargs or return analysis text
        ds = kwargs.get("dataset")
        if ds:
            result = self.analyse(prompt, ds)
            if result.analysis:
                return result.analysis
        return self.model.respond(prompt)

    def quick_analysis(self, ds: DataSet) -> str:
        """
        Fast statistical summary without needing a plot request.
        Returns plain text for the GUI stats panel.
        """
        lines = [ds.summary(), ""]
        lines.append(DataAnalyser.correlation_summary(ds))
        for col in ds.numeric_columns[:3]:
            anom = DataAnalyser.detect_anomalies(ds, col)
            if anom:
                lines.append(anom)
        return "\n".join(lines)


# ============================================================
# Routing patch
# ============================================================

ANALYST_KEYWORDS = [
    "plot", "graph", "chart", "visualise", "visualize",
    "show me", "scatter", "histogram", "correlation",
    "trend", "anomaly", "distribution", "analyse data",
    "analyze data", "data analysis", "frequency spectrum",
    "fft", "time series", "heatmap",
]


def patch_routing(council_engine_module: Any) -> None:
    """Add analyst routing to council_engine._ROUTE_PATTERNS."""
    patterns = getattr(council_engine_module, "_ROUTE_PATTERNS", None)
    if patterns is None:
        return
    entry = ("analyst", ANALYST_KEYWORDS, 8)
    for i, e in enumerate(patterns):
        if e[0] == "writer" and not e[1]:
            patterns.insert(i, entry)
            print("[Analyst] Patched routing table")
            return
    patterns.append(entry)
    print("[Analyst] Appended to routing table")
