# Typhon: the EVK4 records its .raw alone (17 November 2026)

Andy, after testing a Basler and an EVK4 together: "can we have the EVK
still record the video and not convert into images while recording?"
Agreed plan (Andy said yes):

1. An EVK4 run records only the .raw by default: no PNGs while it records.
   It keeps the .raw, the camera record, the end record and the frames CSV
   (a row per picture window, with its camera time, so a Basler and an EVK4
   run still line up).
2. A "Save pictures while recording" box for the EVK4, off by default.
3. The slider opens such a run in its raw view by itself.
4. "Make pictures from .raw…" afterwards writes the PNGs and fills in the
   CSV, for the classifier, frame timing and the ROI tools.

The Basler is unchanged.

## How it works

- `cameras.Device.save_pictures` (True); `EvkDevice.save_pictures` False.
  Only read when the device has `records_raw`.
- `camera_settings.display_settings` adds `record.pictures` ("Save pictures
  while recording", BOOL, the Display group) for a device with
  `records_raw`. It is kept in presets and the camera record like the other
  display settings.
- `frame_camera._start_one` sets `recorder.pictures = not records_raw or
  save_pictures` just before the recording starts.
- `capture.Recorder.pictures` False: `FrameWriter.submit` logs the frame's
  CSV row at once with an empty `file` (`Recorder.log(frame, None)`,
  counted in `Recorder.logged`); nothing is queued or encoded. The end
  record gets `"pictures": {"saved": false, "windows": N}`. `Stats.raw_only`
  puts "pictures off: every event goes to the .raw" on the status line, and
  the "last run" note says no pictures were saved.
- `event_playback.raw_origin` / `run_window_us` read those rows as before;
  `frame_times` skips rows with no file.
- `capture_review.CaptureReviewer`: when a run finishes with a .raw and no
  PNG of its own, the next tick after the camera has closed the .raw opens
  it in the raw view (`_raw_next`, `_open_raw_only_run`). The raw view's
  line says "no pictures: right-click to make them".
- Right-click on the picture: "Make pictures from .raw…"
  (`CaptureReviewer.make_pictures`, a thread of its own, progress on the
  view line, the folder listed again at the end). No new button or public
  frame_camera function, so the generated layout and typhon_recreate are
  unchanged.
- `council_core/raw_pictures.py`: draws each row's window from the .raw
  the way the raw view does (mid-grey, white ON, black OFF; the run's own
  window, origin and area), writes `<run>_frame_NNNNNN.png` by the row's
  frame number (exclusive create; a PNG already there is left alone and
  named), and rewrites the CSV atomically with the file names filled in.
  By hand: `python _council/council_core/raw_pictures.py <folder or .raw>`.

## Known differences from pictures saved live

- The made PNGs use the raw view's standard grey, not the "Events shown" or
  "Colours" display choice the live view had.
- Each PNG is the raw view's fixed window nearest the live picture
  (`event_playback.window_for`), not a window ending exactly at its last
  event.
- A Downsample set for the run is not applied to the made PNGs.

## Tests

`tests/test_raw_pictures.py`; in `tests/test_frame_camera.py`
`test_an_evk4_run_records_the_raw_alone_by_default` and
`test_save_pictures_while_recording_saves_them_too`; in
`tests/test_capture_review.py` the four tests under "A run that recorded its
.raw alone"; in `tests/test_camera_settings.py` the `record.pictures` tests.

Not yet tried on a real EVK4.
