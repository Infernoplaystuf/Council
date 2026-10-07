"""
council_core.camera_categories — which tab each of a camera's settings goes
in, and which of them a tab shows first.

WHY THIS EXISTS
Typhon's left column holds the camera's settings in tabs under the image
folder (council_qt.widgets.settings_tabs), built when a camera connects from
the groups its settings come in (frame_camera.settings_list). One tab per
group would be one tab per facility: an EVK4 has eight groups (its biases,
four filters, the display, its readings, what it is), and with Basic, the
camera's area and the presets that is ten tabs — 672 px of tab strip at the
app's Arial 11 (measured) in a column 432-464 px wide, i.e. a row of scroll
arrows. So related groups SHARE a tab (the four event filters; exposure,
gain and the frame rate; a Basler's image format and its binning), each as a
section of its own. A pop-out window is still per CATEGORY — per group —
because that is what a person tunes at one time.

COMPACT ON PURPOSE
A tab is a glance and a quick change; a pop-out holds the whole category. So
a section shows its group's most-used settings first (MOST_USED, then the
order the camera describes them in), at most ROWS_ALONE of them when it has
the tab to itself and ROWS_SHARED when it shares it, and says how many more
its pop-out has.

NOTHING HERE KNOWS A CAMERA
It works on settings_list() rows — plain dicts with "key" and "group" — so
it is the same for a Basler, an EVK4 and the simulated cameras. A group this
table has never heard of gets a tab of its own, titled by its name: a model
with more features gets more tabs, never fewer settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

#: Tabs that hold more than one group: (title, its groups in order, what its
#: tooltip says). A group in none of these has a tab of its own.
SHARED_TABS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("Exposure", ("Exposure", "Gain", "Frame rate"),
     "Exposure, gain and the frame rate, with their auto modes"),
    ("Image", ("Image", "Binning"),
     "Pixel format, mirroring, gamma and binning"),
    ("Filters", ("Event rate controller", "Anti-flicker",
                 "Event trail filter", "Event rate activity filter"),
     "The event camera's filters: the event rate controller, anti-flicker, "
     "the trail (STC) filter and the activity filter"),
    ("Camera", ("Status", "Camera"),
     "What the camera is, what it reads now, and its own area"),
)

#: The tab every camera has: what it is and where on its sensor it looks.
#: The UI puts the camera's area here even when the camera describes no
#: Status or Camera group.
CAMERA_TAB = "Camera"

#: Tooltips for the tabs a group has to itself.
TIPS: Dict[str, str] = {
    "Biases": "The sensor's biases: how strong a change must be to fire an "
              "event, its filters and how long a pixel rests",
    "Display": "How the event picture is drawn — the live view and the PNGs "
               "(never the .raw)",
}

#: Each group's settings in the order a tab shows them — the ones changed
#: most often first. Keys a camera does not have are skipped; settings not
#: listed follow, in the order the camera describes them.
MOST_USED: Dict[str, Tuple[str, ...]] = {
    "Exposure": ("ExposureAuto", "ExposureTime"),
    "Gain": ("GainAuto", "Gain", "BlackLevel"),
    "Frame rate": ("AcquisitionFrameRateEnable", "AcquisitionFrameRate",
                   "ResultingFrameRate"),
    "Image": ("PixelFormat", "ReverseX", "ReverseY", "GammaEnable", "Gamma"),
    "Binning": ("BinningHorizontal", "BinningVertical"),
    "Biases": ("bias.bias_diff_on", "bias.bias_diff_off", "bias.bias_fo",
               "bias.bias_hpf", "bias.bias_refr"),
    "Event rate controller": ("erc.enabled", "erc.rate"),
    "Anti-flicker": ("afk.enabled", "afk.low_hz", "afk.high_hz"),
    "Event trail filter": ("trail.enabled", "trail.type", "trail.threshold"),
    "Event rate activity filter": ("activity.enabled",
                                   "activity.lower_bound_start",
                                   "activity.upper_bound_start"),
    "Display": ("window_ms", "display.events", "display.palette"),
    "Status": ("status.temperature", "DeviceTemperature"),
}

#: Rows a section shows when it has its tab to itself, and when it shares.
ROWS_ALONE = 6
ROWS_SHARED = 3


@dataclass(frozen=True)
class Section:
    """One group in a tab: the keys the tab shows, and the ones only its
    pop-out does."""
    group: str
    shown: Tuple[str, ...]
    more: Tuple[str, ...] = ()

    @property
    def keys(self) -> Tuple[str, ...]:
        return self.shown + self.more


@dataclass(frozen=True)
class Tab:
    title: str
    tip: str
    sections: Tuple[Section, ...]

    @property
    def groups(self) -> Tuple[str, ...]:
        return tuple(s.group for s in self.sections)


def tab_of(group: str) -> str:
    """The title of the tab `group` goes in."""
    for title, groups, _tip in SHARED_TABS:
        if group in groups:
            return title
    return group


def ordered(group: str, keys: Sequence[str]) -> List[str]:
    """`keys` (one group's, in describe order) with the most-used first."""
    first = [k for k in MOST_USED.get(group, ()) if k in keys]
    return first + [k for k in keys if k not in first]


def plan(settings: Iterable[Mapping[str, Any]],
         groups: Sequence[str] = ()) -> List[Tab]:
    """The tabs for these settings (settings_list()["settings"]), in the
    order their groups first appear (`groups`, the camera's own display
    order, then any group only a setting names).

    A tab's sections keep the order SHARED_TABS gives them, which is the
    order a person reads them in (exposure before gain); the tabs follow
    the camera's order. The CAMERA_TAB is always last of the setting tabs,
    whether or not the camera has readings to put in it.
    """
    rows = list(settings)
    by_group: Dict[str, List[str]] = {}
    for row in rows:
        by_group.setdefault(str(row.get("group") or ""), []).append(
            str(row.get("key")))
    order = [g for g in dict.fromkeys(list(groups) + list(by_group))
             if by_group.get(g)]
    titles = list(dict.fromkeys(tab_of(g) for g in order))
    if CAMERA_TAB in titles:
        titles.remove(CAMERA_TAB)
    titles.append(CAMERA_TAB)
    out: List[Tab] = []
    for title in titles:
        shared = next((t for t in SHARED_TABS if t[0] == title), None)
        members = ([g for g in shared[1] if g in by_group] if shared
                   else [g for g in order if tab_of(g) == title])
        limit = ROWS_ALONE if len(members) <= 1 else ROWS_SHARED
        sections = []
        for group in members:
            keys = ordered(group, by_group[group])
            sections.append(Section(group, tuple(keys[:limit]),
                                    tuple(keys[limit:])))
        tip = shared[2] if shared else TIPS.get(title, title)
        out.append(Tab(title, tip, tuple(sections)))
    return out
