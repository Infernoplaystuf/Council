"""
typhon.event_analysis — Event analysis: Andy's "Prophesee Integrated 6 Tabs
(smoke plume, laser proxy)" script as a Typhon plugin (Andy, 25 November
2026: "add the functionality of this script as a plugin for typhon").

WHAT IT IS
One pop-out window with the script's six tabs, its boxes, defaults and
checks unchanged (forms.py):
  1. Create Homography   a dot-grid recording -> pixels-to-mm JSON
  2. Rectify Recording   a perspective-corrected MP4
  3. Velocity Arrows     event clusters tracked, MP4 + CSV
  4. Melt-Pool Paths     per-layer scan paths and heatmaps, MP4 + CSV
  5. Raindrop Tracking   droplets behind frosted glass, MP4 + CSV + trails
  6. Smoke Plumes        flow disturbances, object candidates and the
                         optical obscuration / laser proxy, MP4 + CSVs +
                         heatmaps
The analyses are the script's own code, in council_core/event_analysis.py
(numpy and OpenCV, which a plugin's folder may not import), reached through
typhon_plugin_api.builtin.event_analysis().

OPTIONAL, PLUGIN ONLY
Built in but off in a new project (OPTIONAL_BUILTINS): tick "Event
analysis" in Plugins… (or in the Designer's Export app…) and its button
appears on the toolbar strip. Nothing in Typhon's capture changes: it reads
recordings that are already on disk, the latest run's included ("Latest
recording"), and never writes into a run folder unless the user picks one.

WHAT IT ADDS TO THE SCRIPT
  * A .raw is read with the HAL alone (no h5py, no index file written beside
    it); .dat still needs metavision_core; a .npz of events works with no
    SDK (simulated events).
  * The picture being made is shown in the window (no OpenCV window).
  * An output directory left empty is "<recording>_analysis" beside it.
  * Nothing is overwritten: outputs take the next free name.
  * The boxes are kept in the project's settings.

THREADS
One analysis at a time, on a thread of its own; Stop sets the script's stop
flag. The window polls log lines, the preview and the state on a timer:
nothing calls into Qt from the worker.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import typhon_plugin_api as api
from typhon_plugin_api import builtin

from . import forms

#: Recordings "Latest recording" looks for in the capture folder.
RECORDING_SUFFIXES = ('.raw', '.dat', '.npz', '.avi', '.mp4', '.mkv', '.mov')
#: How deep under the capture folder it looks (<folder>/<run>/<camera>/).
SEARCH_DEPTH = 3
#: The preview's longer side, and how often a new one is made.
PREVIEW_SIDE = 960
PREVIEW_SECONDS = 0.1
LOG_LINES = 2000


def engine() -> Any:
    """council_core.event_analysis."""
    return builtin.event_analysis()


class Job:
    """One analysis running (or finished): its log, its newest picture and
    how it ended."""

    def __init__(self, tab: str) -> None:
        self.tab = tab
        self.stop = threading.Event()
        self.lines: deque = deque(maxlen=LOG_LINES)
        self.logged = 0
        self.preview: Optional[Tuple[int, int, bytes]] = None
        self.preview_n = 0
        self.result: Any = None
        self.error = ''
        self.done = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._last_preview = 0.0

    def log(self, message: Any) -> None:
        self.lines.append(str(message))
        self.logged += 1

    def new_lines(self, seen: int) -> List[str]:
        """The lines logged after the first `seen` (those still kept)."""
        lines = list(self.lines)
        fresh = self.logged - seen
        return lines[-fresh:] if fresh > 0 else []

    def show(self, frame: Any) -> None:
        now = time.monotonic()
        if now - self._last_preview < PREVIEW_SECONDS:
            return
        self._last_preview = now
        self.preview = engine().rgb_bytes(frame, PREVIEW_SIDE)
        self.preview_n += 1

    @property
    def running(self) -> bool:
        return self.thread is not None and not self.done.is_set()


class EventAnalysisPlugin:
    """Event analysis (feature plugin, rig scope, a panel)."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None
        self.job: Optional[Job] = None
        self.dots: Any = None
        self._values: Optional[Dict[str, Any]] = None

    # -- life ---------------------------------------------------------
    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx
        self._values = None

    def deactivate(self) -> None:
        if self.job is not None:
            self.job.stop.set()
        self.ctx = None

    def missing(self) -> str:
        """'' when numpy and OpenCV are there, else what to install."""
        try:
            return str(builtin.event_analysis_missing() or '')
        except ImportError as exc:                    # pragma: no cover
            return f'the event analysis cannot load: {exc}'

    # -- the boxes ----------------------------------------------------
    def values(self) -> Dict[str, Any]:
        if self._values is None:
            saved: Mapping[str, Any] = {}
            if self.ctx is not None:
                try:
                    saved = self.ctx.settings()
                except Exception:                     # noqa: BLE001
                    saved = {}
            self._values = forms.merged(saved)
        return dict(self._values)

    def remember(self, values: Mapping[str, Any]) -> str:
        """Keep the boxes in the project; '' or why not (never fatal)."""
        clean = forms.merged(values)
        self._values = clean
        if self.ctx is None:
            return ''
        try:
            self.ctx.save_settings(clean)
        except Exception as exc:                      # noqa: BLE001
            return f'(the boxes were not kept: {exc})'
        return ''

    # -- recordings ---------------------------------------------------
    def capture_folder(self) -> Optional[Path]:
        ctx = self.ctx
        if ctx is None:
            return None
        try:
            folder = ctx.capture_folder(ctx.active_camera())
        except Exception:                             # noqa: BLE001
            folder = None
        return Path(folder) if folder else None

    def latest_recording(self, folder: Optional[Path] = None,
                         events_only: bool = False) -> Optional[Path]:
        """The newest recording under the capture folder (an EVK4 run's
        <run>_events.raw, a video ...), or None."""
        folder = folder if folder is not None else self.capture_folder()
        if folder is None or not Path(folder).is_dir():
            return None
        wanted = forms.EVENT_EXT if events_only else (
            forms.EVENT_EXT | forms.VIDEO_EXT)
        best: Optional[Tuple[float, Path]] = None
        base = Path(folder)
        for root, dirs, files in os.walk(base):
            depth = len(Path(root).relative_to(base).parts)
            if depth >= SEARCH_DEPTH:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.endswith('_analysis')]
            for name in files:
                if os.path.splitext(name)[1].lower() not in wanted:
                    continue
                path = Path(root) / name
                try:
                    when = path.stat().st_mtime
                except OSError:
                    continue
                if best is None or when > best[0]:
                    best = (when, path)
        return best[1] if best else None

    # -- running ------------------------------------------------------
    def busy(self) -> bool:
        return self.job is not None and self.job.running

    def stop(self) -> None:
        if self.job is not None:
            self.job.stop.set()

    def start(self, tab: str, values: Mapping[str, Any],
              finished: Optional[Callable[[Job], None]] = None
              ) -> Tuple[Optional[Job], str]:
        """Check the tab's boxes and start its analysis on a thread:
        (job, '') or (None, why not)."""
        if self.busy():
            return None, 'An analysis is already running: Stop it first.'
        why = self.missing()
        if why:
            return None, why
        check = forms.CHECKS.get(tab)
        if check is None:
            return None, f'no tab {tab!r}'
        try:
            args = check(values)
        except (ValueError, KeyError) as exc:
            return None, str(exc)
        note = self.remember(values)
        work = self._work(tab, args)
        job = Job(tab)
        if note:
            job.log(note)

        def run() -> None:
            try:
                job.result = work(job)
            except Exception as exc:                  # noqa: BLE001
                job.error = str(exc) or type(exc).__name__
                job.log(traceback.format_exc().rstrip())
            finally:
                job.done.set()
                if finished is not None:
                    try:
                        finished(job)
                    except Exception:                 # noqa: BLE001
                        pass
        job.thread = threading.Thread(target=run, daemon=True,
                                      name=f'event-analysis-{tab}')
        self.job = job
        job.thread.start()
        return job, ''

    def _work(self, tab: str, a: Dict[str, Any]) -> Callable[[Job], Any]:
        ea = engine()
        if tab == 'cal':
            def cal(job: Job) -> Any:
                return ea.accumulation(a['path'], a['slice_us'],
                                       a['window_slices'], a['polarity'],
                                       job.stop, job.log)
            return cal
        if tab == 'rect':
            def rect(job: Job) -> Any:
                folder = os.path.dirname(os.path.abspath(a['output']))
                os.makedirs(folder, exist_ok=True)
                stem = os.path.splitext(os.path.basename(a['output']))[0]
                out = ea.free_path(folder, stem, '.mp4')
                if out != os.path.abspath(a['output']) and \
                        os.path.exists(a['output']):
                    job.log(f'{a["output"]} exists; writing {out} instead.')
                ea.rectify(a['path'], a['json'], out, a['scale'], a['margin'],
                           a['slice_us'], a['polarity'], job.stop, job.log,
                           show=job.show)
                return out
            return rect

        def folder_of(job: Job) -> str:
            os.makedirs(a['folder'], exist_ok=True)
            return a['folder']
        if tab == 'vel':
            return lambda job: ea.make_velocity_video(
                a['path'], folder_of(job), a['name'], a['settings'],
                a['json'], job.stop, job.log, show=job.show)
        if tab == 'melt':
            return lambda job: ea.analyze_meltpool(
                a['path'], a['json'], a['triggers'], folder_of(job),
                a['name'], a['settings'], job.stop, job.log, show=job.show)
        if tab == 'rain':
            return lambda job: ea.analyze_raindrops(
                a['path'], folder_of(job), a['name'], a['json'],
                a['settings'], job.stop, job.log, show=job.show)
        if tab == 'smoke':
            return lambda job: ea.analyze_smoke(
                a['path'], folder_of(job), a['name'], a['json'],
                a['settings'], job.stop, job.log, show=job.show)
        raise ValueError(f'no tab {tab!r}')

    # -- the calibration tab's dots ------------------------------------
    def calibration(self) -> Any:
        """The dot labels (council_core.event_analysis.DotLabels)."""
        if self.dots is None:
            self.dots = engine().DotLabels()
        return self.dots

    def save_calibration(self, path: str, values: Mapping[str, Any]
                         ) -> Tuple[str, str]:
        """Write the homography JSON and its preview; every tab's JSON box
        then names it. (json path, preview path)."""
        dots = self.calibration()
        rows, cols, spacing = forms.grid(values)
        record = dots.record(source=str(values.get('cal_input', '')),
                             rows=rows, cols=cols, spacing=spacing,
                             distance=float(values.get('distance', 0)),
                             distance_unit=str(values.get('distance_unit',
                                                          'inches')))
        saved = dots.save(path, record, str(values.get('cal_view',
                                                       'Full accumulation')))
        new = dict(values)
        for key in forms.JSON_KEYS:
            new[key] = saved[0]
        self.remember(new)
        return saved

    def picture_bytes(self, frame: Any) -> Tuple[int, int, bytes]:
        """(width, height, RGB bytes) of a picture, for the window."""
        return engine().rgb_bytes(frame, 0)

    # -- the window ---------------------------------------------------
    def panel(self, parent: Any = None) -> Any:
        from .panel import EventAnalysisPanel
        return EventAnalysisPanel(self, parent)
