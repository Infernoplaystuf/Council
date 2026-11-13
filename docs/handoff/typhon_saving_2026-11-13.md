# Typhon: saving big frames faster, losing fewer (13 November 2026)

Andy's boA9344-30cm (65 MP, CoaXPress) makes 2 GB/s in Mono8 and 3.9 GB/s
in Mono12 (saved as 16-bit) at 30 fps: the disk is almost always the limit.
Andy asked for a ranked list (the doc "Typhon: faster saving, fewer lost
images") and said to start on the code items.

## Built (step 1)

- **The save folder timed before a run** (`council_core/save_speed.py`,
  `frame_camera._speed_check`). Before a run whose frames are 8 MB or more,
  once per folder per session and not on a network share, Typhon writes
  256 MB into the folder (one file of its own, created "xb", fsync'd,
  removed) and prints in the Command Prompt the MB/s and the frame rate that
  keeps up with at this frame size. When the live view's measured rate is
  more, the Start summary says how many seconds until the save queue fills.
  `TYPHON_NO_SPEED_CHECK=1` skips it. `python council_core/save_speed.py
  <folder> [MB]` times a folder by hand. Frame size: the area x 1 byte (8-bit),
  2 (10-16-bit, as saved), 3 (RGB/BGR8) (`frame_camera._frame_bytes`).
- **The saving meter.** `Stats.queue_percent` (the save queue's fill of its
  byte budget) and `Stats.save_mb_s` (MB/s reaching the disk over the last
  2 s, `FrameWriter.save_rate`), shown as "N waiting to save (queue X% full,
  saving Y MB/s)".
- **Settings** (environment, before run.bat): `TYPHON_SAVE_QUEUE_GB` (save
  queue, up to 3/4 of RAM; default 1/4 capped at 8 GB),
  `TYPHON_PYLON_BUFFER_GB` (pylon's buffers while recording, default 1 GB,
  10-200 buffers), `TYPHON_SAVE_THREADS` (1-64, default up to 8). In the
  exported README's "SAVING BIG FRAMES FASTER".
- **Frames lost before Typhon.** Gaps in the camera's own frame numbers
  (`Frame.meta["frame_id"]`, the block ID) less failed grabs between them:
  `Stats.camera_gaps` ("N lost before Typhon (missing from the camera's frame
  numbers)") and the end record's `frames_lost_before_typhon` (only when
  some were). A backwards number or a jump over 1,000,000 starts afresh.
  Whether a CXP boost fills the block ID is not yet seen on the real camera.

- **Raw recording** (`council_core/raw_frames.py`). `TYPHON_SAVE_FORMAT=raw`
  or `raw_frames.set_format("raw")`: `capture.run_recorder` gives the
  Recorder a `RawStack` writer, so every frame is appended to
  `<run>_frames.tyraw` (records: b"TYRF", header length, JSON header with the
  PNG name, dtype, shape, then the pixels) while the frames CSV names the PNG
  each frame becomes. The end record has `raw_frames` {file, frames, bytes,
  converted: false}. `python council_core/raw_frames.py <folder|file> [--remove-raw]` writes the
  PNGs ("xb": one already there is left alone). A record cut short converts
  every whole frame before it. The .tyraw is removed only when asked and every
  frame became a new PNG in that call. Pixels are stored as Typhon has them
  (Mono12 unpacked to uint16); keeping Mono12p packed is not done.
- **Record to RAM** is the save queue made big: `TYPHON_SAVE_QUEUE_GB` up to
  3/4 of RAM; the writers keep saving during and after the run (Stop waits
  `DRAIN_TIMEOUT`, then they carry on in the background).

## Not done yet

1. Two drives at once (deferred: frames in two folders break the run's one
   folder; easiest with raw recording, a .tyraw per drive).
2. pylon/grabber stream statistics in the end record.
3. Mono12p kept packed in raw runs.
4. Proposal only: a long NIR exposure from the trigger (sum, max, mean of the
   frames while the line is active); questions to Andy in the doc.

## The NIR long exposure: what the camera says (13 November, later)

Basler's online pages for the boA9344-30cm (draft, "not in series production
yet") could not be read here: their model tables render in the browser. So
`check.bat` now asks the camera itself (`pylon_env.exposure_lines`, in the
"Basler / pylon in detail" part): its ExposureMode entries (is TriggerWidth
there?), the ExposureTime range, TriggerSelector entries, SensorShutterMode,
PixelFormat entries, ExposureOverlapTimeMax and the sensor name. Checked on
pypylon's emulator (Timed, TriggerWidth, TriggerControlled; 1 us to 10 s).
The long-exposure design (sum / max / mean of the frames while the trigger
line is active) works whatever it says; the answer only decides whether a
single trigger-width exposure is an alternative.
