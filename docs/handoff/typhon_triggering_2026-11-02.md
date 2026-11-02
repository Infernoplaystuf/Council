# The Triggering window, trigger-source plugins and record runs (2 November 2026)

Branch `typhon-plugins` (built on `claude/project-thread-vqal34`, PR #65,
after the classifier and datasets: `typhon_classifier_datasets_2026-11-02.md`).

## What Andy asked for, and the answers

- "A triggering plugin" that sends a trigger to the cameras, telling them
  when to capture an image or record images.
- The plugin `typhon.trigger` already existed (Software, Timer and the
  camera's input lines; exact and snapshot runs) but had no window of its
  own. Proposed and agreed (Andy picked "Not sure yet; build 1-3 first"):
  1. a Triggering pop-out, the controls leaving the Basic tab;
  2. trigger sources as small plugins of their own;
  3. a record action.
- What will actually send the signal (camera line, a serial / USB box,
  another program on the PC) is still open; a source plugin for it is the
  next small step.

## What was built

- **The Triggering window.** `typhon_plugins/trigger/panel.py`, opened by
  "Triggering" on the plugin toolbar (`WINDOW_KEEPS` is empty now). It
  shows only the rows the mode and source use, rebuilds them when a camera
  connects or a source plugin appears, reads the status twice a second while
  shown, and has its own Capture now. The Basic tab's eight trigger shapes
  (s84-s91) left `examples/gui/typhon_plugins.gspec`; s96 says where they
  went. Capture now (F9) stays beside Start and Stop, so F9 still works.
- **Triggered: record** (`council_core/triggered.py`; offered in a "Typhon
  (plugins)" project only — `triggered.PLUGIN_MODES` and the Record for row
  appear when `triggered.plugin_sources` answers, so today's Typhon's rows,
  modes and presets are exactly as they were). The camera runs live; a trigger saves EVERY frame for "Record for"
  seconds (`capture.record_s`), or with 0 from one trigger until the next,
  which stops it (the one after starts again). A trigger during a timed
  recording ends it and starts a new one. Each trigger is a row of the
  triggers CSV; `record_s` is in the run's capture block for record runs
  only, so every other run's records read as before. A camera line can't
  drive it (like snapshot): Software, Timer or a source plugin.
- **Trigger-source plugins.** Manifest kind `"source"` is no longer refused
  (`NOT_WIRED_KINDS` is empty). Such a plugin has `sources()` (names shown
  under Trigger from, after Software and Timer) and `make_source(name)` (a
  `TriggerSource` for one run). The host lists them (`trigger_sources`),
  makes one per run guarded (`GuardedSource`: a fault switches the plugin
  off and the run goes on from Capture now), and the run's books fire from
  it (`TriggerRun.fire_from`), in exact, snapshot or record runs. A name a
  camera or Typhon owns (Line…, CC…, Counter…, Timer…, Software…, Action…)
  is refused. A source no plugin offers now refuses Start, in words.
  `triggered.plugin_sources` is set by `frame_camera` so the camera's own
  settings accept a plugin's source in a plugins project only.
- API 0.3 additions: `kind.source`, `hook.sources`, `hook.make_source`,
  `context.trigger_sources`, `context.make_source`, `type.SourcePlugin`,
  `mode.record` (`docs/typhon_plugin_api_notes.md`).

## Limits

- Third-party plugins are still never loaded before stage P5 (approval).
  Until then a real source plugin (a serial box, say) has to be a built-in
  one: a folder in `typhon_plugins/` and a row in `BUILTIN_PLUGINS`.
- One camera per run, as today; one source firing several cameras is the
  rig's (P6 / M2).

## Tests

`tests/test_typhon_plugins_trigger_sources.py` (a Metronome source plugin
adopted by the host, record runs on a fake clock and on the simulated
camera) and `tests/test_typhon_plugins_trigger_window.py` (the wireframe,
the window in the built app, a record run started from it).
