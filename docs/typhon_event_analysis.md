# Event analysis: the Prophesee six-tab script as a Typhon plugin

Andy, 25 November 2026: "Please add the functionality of this script as a
plugin for typhon". The script was *Prophesee Integrated 6 Tabs, Smoke Plume,
Laser Proxy (Fixed)*, a Tk window. It is now the built-in Typhon plugin
**Event analysis (smoke plume, melt pool)**, id `typhon.event_analysis`.

## Where it is

| What | File |
|---|---|
| Manifest | `typhon_plugins/event_analysis/plugin.json` |
| Plugin (jobs, settings, latest recording) | `typhon_plugins/event_analysis/plugin.py` |
| The six tabs' boxes, defaults and checks | `typhon_plugins/event_analysis/forms.py` |
| Window (Qt) | `typhon_plugins/event_analysis/panel.py` |
| Analyses (numpy and OpenCV) | `council_core/event_analysis.py`, reached via `typhon_plugin_api.builtin.event_analysis()` |
| Tests (simulated events) | `tests/test_typhon_plugins_event_analysis.py` |

A plugin's folder may import only the standard library and
`typhon_plugin_api`. That's why the numpy and OpenCV code sits in
`council_core`, the same way the PLC plugin uses `council_core/blade_phase.py`.
The new module is additive: it changes no existing Typhon code. In core, only
the built-in list in `council_core/plugin_host.py` changed, plus two names in
`typhon_plugin_api/builtin.py`.

## Turning it on

The plugin is optional. A new project has it off.

- In Typhon (plugins), go to **Plugins…** and tick **Event analysis (smoke
  plume, melt pool)**. An **Event analysis** button then appears on the
  toolbar strip.
- In the Designer's **Export app…**, the same tick box decides whether the
  exported copy has it on. The code goes with every export either way.
- The plugin needs `numpy` and `opencv-python` in Typhon's Python. Without
  them, the Plugins window says so.

## The six tabs

The tabs are the script's, with the same boxes, defaults and checks.

1. **Create Homography.** Scan a dot-grid recording, then click dots of a
   known row and column:
   - left click labels a dot;
   - right click removes the nearest dot.

   Then use **Fit homography**, **Auto-match visible dots** and **Save
   JSON**. Saving fills the JSON box of every other tab.
2. **Rectify Recording.** Makes a perspective-corrected MP4.
3. **Velocity Arrows.** Tracks event clusters. Writes an MP4 and a CSV, in
   mm/s with a JSON and in px/s without one.
4. **Melt-Pool Paths.** Splits the recording into layers using the trigger
   CSV (`time_s,layer_id`). Writes a path PNG and an activity PNG per layer,
   plus a CSV and an MP4.
5. **Raindrop Tracking.** Uses an adaptive background, with an optional
   direction filter. Writes an MP4, a CSV and a trails PNG.
6. **Smoke Plumes.** Finds Farneback-flow disturbances and object candidates,
   and runs the optional reference-ROI obscuration proxy (the "laser proxy").
   On video that proxy is a brightness proxy. On events it's only an
   event-activity change indicator, not beam attenuation. Writes an MP4,
   measurement and object CSVs, and disturbance, activity and trajectory
   PNGs.

## Changes from the script

- **Reading events.** The script used metavision_core's `EventsIterator`,
  which needs h5py and writes `<name>.raw.tmp_index` beside a .raw.
  - **.raw:** read with `metavision_hal` (OpenEB) alone, as Typhon's raw
    view reads it. The slices match `EventsIterator`'s `delta_t` mode:
    counted from the time-shifted start, with empty slices included.
    `EventsIterator` is used only when the HAL is missing.
  - **.dat:** still needs metavision_core.
  - **.npz:** a file of events (`x`, `y`, `p`, `t` in µs from 0, `width`,
    `height`) works with no SDK. Simulated recordings and the tests use it,
    and `event_analysis.save_events_npz` writes one.
  - **Video:** read by OpenCV, as before.
- **No OpenCV window.** The picture being made shows in the plugin window,
  beside the processing log. The velocity tab's "live preview" box is gone
  for that reason.
- **Default output folder.** An output directory left empty becomes
  `<recording>_analysis` beside the recording.
- **Nothing is overwritten.** Outputs take the next free name (`_001`, ...).
  Rectify never asks to overwrite: it writes `<name>_001.mp4`.
- **Latest recording.** This button beside each recording box picks the
  newest recording under Typhon's capture folder, such as an EVK4 run's
  `<run>_events.raw`.
- **Settings are kept.** The boxes are saved in the project's settings on
  every start.
- **One analysis at a time.** It runs on its own thread, and Stop sets the
  script's stop flag.

## Tested, and not tested

The tests use simulated events only, since no EVK4 is available here. They
check that:
- velocity reads 1000 px/s on a blob made at 1000 px/s, and 450 mm/s through
  a homography of 0.45 mm/px;
- melt-pool speed comes out at 180 mm/s, split into the two trigger layers;
- raindrops falling at 800 px/s are tracked;
- the smoke proxy warns once a reference patch goes quiet;
- the dot grid is fitted with all 25 dots matched;
- the HAL slicing is right, checked against a stand-in HAL;
- nothing is overwritten;
- the window works in Typhon (plugins).

Not yet tested:
- a real EVK4 .raw (EVT3) through the real HAL;
- a .dat;
- speed on full-size 1280 x 720 recordings.
