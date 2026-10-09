# Typhon velocity: design

Date: 2026-10-09 (research done 2026-10-07). Status: design only; nothing is
built yet. It combines two pieces of research (velocity measurement, and LED
calibration), each checked by a separate fact-check, with every correction
from those checks applied. The research read `qt-migration` at 794f8b6. Since
then the camera settings tabs (`typhon/settings-tabs`) have landed on
`qt-migration`, and every file and line reference below has been re-checked
against `qt-migration` at 906bb26.

**Read this first.** There is no EVK4 on this PC and no real recording. Every
number below is labelled as one of:

- **measured**: run on this laptop (i7-14700HX) on synthetic events;
- **computed**: arithmetic or a geometry model;
- **estimate**: judgement, not measured;
- **vendor docs**: from Prophesee's or Sony's published material, not
  re-checkable offline.

Real-world accuracy will be worse than the synthetic figures. Nobody can know
how much worse until a real recording has been analysed.

---

## Terms used here

- **Event**: one pixel reporting that it got brighter (ON) or darker (OFF), with a time stamp in microseconds (µs).
- **Polarity**: whether an event is ON or OFF.
- **Mev/s**: million events per second; the load figure that decides what keeps up.
- **.raw**: the EVK4's own recording file, holding every event the sensor sent (Typhon writes `<run>_events.raw`).
- **EVT2 / EVT3**: two of Prophesee's .raw encodings. A real EVK4 records EVT3; every test file here is EVT2.
- **Picture window**: the 20 ms of events Typhon draws as one picture (`window_ms`).
- **Slice**: the short stretch of events (for example 1 ms) the velocity code looks at in one go. It is separate from the picture window.
- **Cell**: a small square of pixels (for example 4 x 4) that events are counted in.
- **Cluster (blob)**: touching cells with enough events in one slice, such as one particle or one bird.
- **Track**: the same cluster followed from slice to slice. Its velocity is how fast its centre moves.
- **px/s**: speed across the picture, in sensor pixels per second.
- **mm/px**: how many millimetres one pixel covers at the object. It turns px/s into mm/s.
- **Calibration**: the measured link between pixels and millimetres for one camera set-up.
- **Centroid**: the centre of a spot, weighted by how many events each of its pixels gave.
- **Homography**: a 3 x 3 mapping from the picture to a flat surface seen at an angle; it removes perspective.
- **Intrinsics**: the camera-model numbers for lens plus sensor (focal length in pixels, image centre).
- **Lens distortion**: straight lines bowing towards the picture edges, so the scale changes across the image.
- **Thin-lens formula**: 1/u + 1/v = 1/f, linking object distance u, image distance v and focal length f.
- **Principal plane**: the reference plane inside a lens that the thin-lens distances are measured from.
- **Normalised DLT**: the standard least-squares recipe for fitting a homography to matched points.
- **k1**: the main lens-distortion number; the more negative it is, the more straight lines bow outwards (barrel distortion).
- **Percentile**: the 99th percentile is the value that 99 % of results fall below; it shows the bad cases without the single worst one.
- **Area (ROI)**: the part of the sensor the camera is told to report; events outside it are never sent.
- **Bias**: one of the sensor's analogue settings (contrast threshold, filters, rest time).
- **Refractory period**: how long a pixel rests after firing before it can fire again.
- **Latency**: the delay between a brightness change and its event.
- **ERC (event rate controller)**: a sensor feature that drops events once the rate passes a limit.
- **Anti-flicker (AFK)**: a sensor filter that removes events from lights blinking at 50-520 Hz.
- **Trail / STC filter**: a sensor filter that keeps only some of the events in each burst at a pixel.
- **PWM**: a pin switched on and off at a fixed frequency by a microcontroller's hardware timer.
- **Duty cycle**: the share of each period the LED is on.
- **HAL**: the low-level part of Prophesee's software that opens cameras and decodes events.
- **OpenEB**: Prophesee's open-source software (Apache 2.0). Version 5.2.0 is built on this PC at `C:/ceb`.
- **SDK Pro**: Prophesee's paid, closed-source add-on modules (tracking, calibration and more).
- **Frozen copy**: a write-once copy of a calibration saved beside a run, so the run always knows the exact numbers it used.
- **Raw view**: Typhon's playback of a run's `.raw` (the **PNG / Raw** switch; `CaptureReviewer` in `council_qt/widgets/capture_review.py`). The velocity overlay is drawn there.
- **PIV (particle image velocimetry)**: measuring a flow by matching small patches between two pictures taken a moment apart.
- **FFT**: the fast Fourier transform, the usual fast way to compare two picture patches.
- **Time surface**: a picture in which each pixel holds the time of its latest event.
- **AM / DED**: additive manufacturing (metal 3D printing); directed energy deposition, the kind that blows a stream of powder into the melt.
- **OpenCV (cv2)**: a widely used open-source computer-vision library; not installed in the council env.
- **numba**: an add-on that compiles Python number-crunching to machine code; not installed.

---

## 1. Summary

### What will be built, and what you will see

Typhon gains a velocity feature for event cameras, in three stages.

**Stage 1: recordings first.** This follows your answer that processing will
"most likely" happen after recording, so that nothing is lost during capture.

1. Open a recording in Typhon's raw view (the review viewer), pick a set-up
   (Particles, Objects, or your own limits) and press **Analyse velocity**.
   A progress bar runs. The recording is only read, never changed.
2. As each part finishes, playback shows an **arrow and a short tail** on each
   moving thing, over the picture. The arrow points where the thing is going,
   and its length shows how far it moves in a fixed time. The tail shows where
   it has just been.
3. Once the **whole recording** has been processed, you get:
   - a **speed histogram** (one count per track);
   - a **speed colour legend** whose range is the run's own (from the 1st to
     the 99th percentile of its track speeds).

   The arrows then recolour to match the legend. Until the whole recording is
   done, colours use a provisional range and the legend says so, because, as
   you said, the legend is only usable once the whole video is processed.
4. Speeds are in **mm/s** once the camera set-up has an LED calibration
   (stage A: one LED, its distance and the lens focal length; stage B: two
   LEDs). Until then they are in px/s.
5. Results are new files **beside** the run. The `.raw` and the run's other
   files are never touched.

**Stage 2:** the homography (calibration stage C, four or more LEDs) for
cameras that look at the motion at an angle, and **live arrows** on the live
picture, within the rates measured in section 7.

**Stage 3:** lens-distortion correction (calibration stage D) and speed work
for heavy scenes.

All of this builds on the camera settings tabs, pop-outs and saved
configurations, which are now on `qt-migration` (section 5).

### The honest limits

- **2D only.** The tracker measures movement across the image. Motion towards
  or away from the camera reads too slow. mm/s is right only for motion in the
  plane that was calibrated. A thing 10 mm further away than that plane, at
  500 mm, is mis-scaled by 2 % (computed).
- **There is a top speed.** Willert & Klinner (2022) call about 50,000 px/s
  the upper bound for this class of sensor. The speeds they actually showed
  working were about ±10,000 px/s (12,000 px/s on a turntable, with no
  systematic error). The cause is sensor latency. Their table lists 220 µs
  for an IMX636-class Prophesee kit (EVK2-HD); the EVK4 datasheet also gives
  220 µs; Sony gives 100 µs or less for the IMX636 at 1000 lux and under
  1000 µs at 5 lux, so latency rises in dim light (vendor docs). In real
  units the limit depends on the lens. At 0.086 mm/px, 10,000 px/s is
  0.86 m/s and 50,000 px/s is 4.3 m/s (computed).
- **Speed and resolution pull against each other.** Staying under
  50,000 px/s needs at least v / 50,000 mm per pixel. That is 0.1 mm/px at
  5 m/s and 0.2 mm/px at 10 m/s, or about 1 mm/px at 10 m/s if you stay at
  the ~10,000 px/s that has been demonstrated (computed). At those scales,
  20-100 µm spatter is smaller than one pixel, so whether it is seen depends on
  how bright it is. Small, fast particles in dim light can be missed. Turning
  `bias_fo` down to cut noise adds latency, which makes this worse. A blob's
  size in a 1 ms slice is mostly its motion streak, so Typhon will not report
  it as a particle size.
- **Only changes make events.** A particle at rest makes no events. Nor does a
  ruler lying still in view, which is why calibration uses a flashing LED.
- **Crowds merge.** Things that touch or cross within a slice become one blob,
  and their tracks break or swap. The synthetic numbers in section 2.3 show how
  quickly this happens.
- **Real accuracy is unknown until a real recording is analysed.** The test
  data is synthetic and EVT2, with no sensor physics: no latency, no
  refractory period and no bandwidth limit.

---

## 2. How velocity is measured

### 2.1 The tracker

We write our own tracker in numpy and scipy, which are already in the council
env, so nothing needs installing. For each slice it:

1. Counts events per cell (`np.bincount`).
2. Keeps the cells with at least `min_count` events. This drops scattered
   noise.
3. Joins touching kept cells into clusters (`scipy.ndimage.label`).
4. Works out each cluster's centre, size and event count. A line fitted
   through the cluster's events against time gives a first velocity for a new
   track.
5. Links each cluster to the track whose predicted position falls inside a
   search gate. A track counts as real only after it has been seen in N
   slices. It ends after `max_miss` slices with nothing found (default 2).

**Velocity** is the slope of a straight-line (least-squares) fit through the
track's last ~6 centres against time. Clusters that do not move are tagged
*stationary* and hidden by default; these are flicker, glints and the melt
pool.

Two rules came out of testing:

- **Read events by field name** (`ev["x"]`, `ev["y"]`). Typhon joins the
  decoder's buffers with `np.concatenate`, which repacks Prophesee's 16-byte
  event records into 14 bytes. The prototype's raw `uint32` view of the records
  raised a ValueError on such arrays. Field access gives identical tracks and
  costs about 3 % (6.90 against 7.12 Mev/s, measured).
- **Keep sensor coordinates throughout.** Subtract the area's origin only when
  drawing.

### 2.2 Presets, and limits you can change

You said the subject "can be anything" and will change with each use. So the
two presets are only starting points: every limit can be changed, and a set of
limits can be saved under a name.

| Limit | What you set | What it changes inside |
|---|---|---|
| **Size** | The smallest and largest blob, in px (or mm once calibrated) | Cell size and the minimum events per cluster. Blobs above the maximum are flagged as probable merges |
| **Speed** | The slowest and fastest speed expected | The slice length (Typhon suggests one that keeps a blob within a few cells per slice), the search gate, and the cut-off below which a blob counts as stationary |
| **Density** | Sparse or crowded | Cell size and slice length. Typhon warns when merges become frequent |
| **Polarity** | ON only, or ON + OFF | Which events are used |
| **Confirm / gaps** | Slices needed to confirm a track; slices it may miss | Fewer false tracks against fewer broken tracks |

| | **Particles** | **Objects** |
|---|---|---|
| Meant for | Spatter or powder: small, fast and numerous | Birds, parts, anything larger and slower |
| Events used | ON only | ON + OFF |
| Slice | 1 ms (range 0.25-2 ms). Use 0.25 ms above about 20,000 px/s, because 1 ms slices found only 84 % of the fast particles | 10-20 ms |
| Cell size | 4 px (range 2-4) | 16 px when sparse, 8 px when crowded (see 2.3) |
| Velocity from | Slope of the last ~6 centres, with the in-slice fit as the first estimate | Slope of the last ~6 centres. The in-slice fit is poor for large, slow objects (20-47 % error) |
| Speeds tested | 1,000-20,000 px/s (a "fast" set: 20,000-100,000) | 100-3,000 px/s |

The research proposed a **Kalman filter** (which smooths a position estimate
over time) for Objects, but it was never run on the object test streams. It was
run only on 3 rigid rings moving at constant speed. Fed positions only, it gave
0.8-1.5 % median error; fed the in-slice velocity too, it gave 10-12 %. So the
Kalman filter stays an option to try on real recordings, not the default.

### 2.3 Accuracy on synthetic data

The synthetic scenes are bright discs moving in straight lines at known,
constant speeds. They include 20 % random noise events, 30 µs timing jitter
and a 50 % chance of a second event per pixel crossing. Error means
|measured − true| / true. All of this is **measured** on synthetic data.

| Case | Median error | Notes |
|---|---|---|
| Particles, 1,000-20,000 px/s, over a whole track | 0.3-2 % | From a single slice: 6-13 % |
| Fast particles, 20,000-100,000 px/s | 1.3 % at 0.25 ms slices (99 % found); 0.9 % at 1 ms (84 % found) | **Checks the code only.** The generator has no latency, bandwidth, refractory or dead time, so above about 50,000 px/s (and probably above about 10,000) these numbers say nothing about real accuracy |
| Objects at 1 Mev/s, 16 px cells | 3 % | 1 track in 10 is off by 39 % or more. Real birds flap, so expect worse |
| Objects at 3 Mev/s | 36 % with 16 px cells; 12 % with 8 px | Crowding |
| Objects at 10 Mev/s | 16 px: 2 % of objects found, 96 % error. 8 px: 69 % found, 33 % error | Crowding. Birds gather at the bath, which is exactly this case |
| Particles at 50 Mev/s with 5 ms slices | 86 % | Slices too long for the density, so clusters merge |

So the right cell size depends on how crowded the scene is. Check the density
before trusting the Objects preset. Noise blobs that join into false tracks
were the main failure seen.

### 2.4 Where it runs in Typhon

- **`council_core/velocity.py`** holds `VelocityTracker`: no Qt, numpy and
  scipy only. `feed(events)` returns the tracks.
- **Recordings (stage 1).** An analysis job runs on its own thread. The UI's
  QTimer polls its progress, completion and errors, as it already does for
  `RawPlayback` (`council_core/event_playback.py`).
  - The job must read the .raw again, because the saved pictures and PNGs have
    lost the time stamps and the repeated events.
  - `RawPlayback` hands out finished pictures only: it keeps just the changed
    pixels of each window (`_store_window`, lines 359-386). So a small
    **event-block reader** is factored out of `RawPlayback._read`
    (lines 234-329). It keeps `build_index=False`, so nothing is written
    beside the .raw. It also keeps the capture origin and the EVT3 24-bit
    wrap (`TIME_WRAP_US`, `raw_origin`), so that track times line up with
    the frames CSV and the slider. The LED detector (section 3) reads events
    through the same reader.
  - Two cheaper variants: run the tracker inside RawPlayback's own decode pass
    through an opt-in callback (one read of the file instead of two), or decode
    with `CameraStreamSlicer` (section 7). Both decoders gave identical events.
- **Live (stage 2).** A hook goes in `EvkDevice.read()` (`cameras.py:1609`).
  - `_poll` (line 1654) already joins each window's events and returns
    their x, y, polarity and time arrays; `read()` throws them away once the
    picture is drawn.
  - The hook passes those arrays, without copying them, to a VelocityWorker
    thread through a bounded queue. When the queue is full it drops the
    **oldest** window, counts the drop, and records the gap.
  - Two things to avoid: feeding the worker once per HAL callback (that slowed
    decoding 8-19x) and running on the UI thread.
- **Gaps.** A dropped 20 ms window removes 20 particle slices, which is more
  than `max_miss`, so every track ends and restarts. That would bias the track
  counts. Gaps are therefore recorded in `velocity.json` and marked in the
  tracks file, and rates such as tracks per second leave them out.

### 2.5 Drawing the arrows

Typhon's picture canvas (`ImageCanvas`, generated by `gui_emit_qt.py:1605`)
cannot show arrows today:

- Its overlay is switched off in Typhon (`"overlay": false`,
  `examples/gui/typhon.gspec:317`), and it is painted only when that switch is
  on (`gui_emit_qt.py:1951`).
- Any overlay is drawn at one overall opacity (0.5 by default, line 1959), so
  arrows would come out half-transparent.
- An overlay that is not exactly the picture's size skips the zoom-box crop
  (line 1953).

So stage 1 adds a small **vector overlay** to `ImageCanvas`: lines and
arrowheads in picture coordinates, drawn after the picture at full opacity, so
they stay sharp at any zoom. The fallback is a rasterised overlay through
`set_overlay` with opacity 1.0, which took 5.2 / 7.4 / 16.5 ms per picture for
10 / 100 / 500 tracks (PIL, measured). Existing Typhons get the change through
"Update from example".

**Pixel convention.** In Qt, picture pixel i covers [i, i+1), so a point at
sensor x is drawn at (x − area_x + 0.5) × zoom. A box drawn on the picture is
converted back the same way.

### 2.6 Other approaches considered

| Approach | What you see | Accuracy (synthetic) | Speed (this PC) | Verdict |
|---|---|---|---|---|
| **Grid clusters + tracks** (2.1) | An arrow and tail per thing, a track table, a histogram | Section 2.3 | Section 7 | **Chosen** |
| Event PIV (cross-correlating short picture pairs) | A grid of arrows (a flow field), not individual particles | Not measured here | 25.1 fields/s at 880 arrows and 7.9 at 3,476 with an unpadded 32 x 32 FFT; 8.2 and 2.2 when zero-padded to 64 x 64, which avoids wrap-around. `workers=-1` did not help | Stage 3 option for dense powder clouds; needs dense, even seeding |
| Dense normal flow (plane fit on a time surface) | An arrow at every edge pixel | Reads low: 783 against a true 854 px/s | 0.35 Mev/s | Not recommended: arrows follow edge normals, which is poor for round particles and flapping wings |
| Contrast maximisation (offline refinement) | A sharper speed for a few chosen objects | No better than the chosen method in a quick, rough test | 14-21 ms per object | Stage 3 option |
| DBSCAN clustering (as in STELLA) | Same as the chosen method | STELLA (Exp Fluids 2026, 67:110) reports a lowest median absolute error of 1.2 % *of the peak velocity* on synthetic data. That is a different measure from ours and not comparable | 0.12-0.17 Mev/s. scikit-learn 1.8.0 is already installed | Offline only; not needed |
| Two-line time of flight (in the style of Prophesee's PSM) | The speed of things crossing two lines | Not measured | Very cheap | Worth adding for directed streams (a DED powder stream, recoater-driven powder) |
| Prophesee SDK Pro trackers | Same as the chosen method, from vendor C++ | Untested | Vendor says efficient | See section 6 |

---

## 3. Calibration: pixels to millimetres

### 3.1 Why a flashing LED

An event camera sees only change, so nothing in a still scene can be pointed
at. An LED blinking at a known frequency makes a spot that fires steadily, at
a rate nothing else in the scene shares. Its frequency is its name, so several
LEDs can be told apart without matching anything up.

Stage A below is exactly the system you described: a flashing LED, plus the
distance from the sensor to it. This calibration is also the start of the
general camera corrections you mentioned (homography, then lens
distortion). It is stored per camera in full-sensor coordinates (3.7, 3.8),
so it can serve uses other than velocity later.

### 3.2 Finding the LED in events

Our own numpy detector, `council_core/led_locate.py`, reads events through the
event-block reader in 2.4. Nothing usable for this is in OpenEB on this PC
(section 6). Steps:

1. **Rate gate.** Count events per pixel. A pixel watching an LED at f Hz
   fires at least about 2f events/s, so keep pixels with at least
   f × duration events. This removes most noise and moving edges before any
   sorting.
2. **Transitions.** Sort each pixel's events by time. A *rise* is an ON event
   after an OFF; a *fall* is the reverse. Burst length does not matter.
3. **Periods.** Take the interval between transitions of the same kind at the
   same pixel. It is in band when it is within 5 % of the expected period.
4. **Pixel test.** A pixel passes when it has enough in-band intervals (at
   least 15 % of the expected 2 × f × duration) and at least half of all its
   intervals are in band. Hot pixels and busy textures fail.
5. **Spots.** Group passing pixels into 8-connected spots of 2 to 2000 px.
6. **Centroid.** Grow the spot by a one-pixel ring, then take the mean of
   pixel centres weighted by event count.
7. **Measured frequency.** 1e6 / mean in-band interval (µs). Each polarity is
   weighted by the inverse of its interval variance, measured on the clip
   itself. (A published finding that falling edges time better could not be
   re-checked, and the synthetic model simply assumed it, so it is not built
   in.)
8. **Survey ("what is blinking?").** For every pixel, the median transition
   interval when it is steady (spread within 5 %), histogrammed on a log
   frequency axis. It finds every periodic source in view.
   - The fast setting (lowest frequency 400 Hz) lists only kHz sources, such
     as PWM-dimmed lighting and LEDs.
   - Mains flicker (100/120 Hz) appears only with the lowest frequency at
     50 Hz, which is slower (1188 ms against 144 ms per 0.25 s of events at
     10 Mev/s, measured).
9. **Frequency rule.** Run the survey first, store the **measured** frequency
   of each LED in its LED or board definition, then accept a spot only when
   its measured frequency is within 1 % of that stored value.
   - Do not check against a round, typed number. A common Arduino Uno PWM pin
     runs at 976.56 Hz. Searched as "1000 Hz", it was found every time (at
     977.8 Hz) and then rejected 5/5 by a 1 % rule around 1000 Hz (measured).
     The measured value is also biased by about +1.2 Hz when the true period
     sits off-centre in the band.
   - Checked against the stored value, the 1 % rule still does its job. A
     1050 Hz decoy beside a 1 kHz target passes the 5 % band but reads
     1040.5 Hz, so it is rejected. 1100 Hz and 1150 Hz decoys were never
     reported (0/10).
10. **ON-only mode** is for streams whose OFF events are switched off by the
    biases. A burst start is an ON event more than a quarter-period after the
    pixel's previous event. With one event per blink, this mode needs the rate
    gate lowered to at most 0.5 × f × duration (at the default gate the LED was
    found 0/10 times) and gives about 0.13 px (measured), not the 0.04-0.05 px
    of the normal mode.

**Synthetic results (measured).**

| Case | Found | False spots | Centroid error, mean | Time per second of events |
|---|---|---|---|---|
| 1 Mev/s, 1 kHz, 5 px spot | 40/40 (re-run 10/10) | 0 | 0.048 px (re-run 0.032) | 98-107 ms |
| 10 Mev/s, 1 kHz, 5 px spot | 12/12 (re-run 3/3) | 0 | 0.044 px (re-run 0.032) | 311-377 ms |
| 0.25 s / 0.1 s clip at 1 Mev/s | 30/30 each | 0 | 0.043 px | 45 / 35 ms per clip |
| Spot 2 / 3 / 8 px | all | 0 | 0.065 / 0.044 / 0.035 px | ~0.1 s |
| LED inside a 100 Hz flicker region | 30/30 | 0 | 0.047 px | 103 ms |
| 300 Hz / 2 kHz LED | 20/20 each | 0 | 0.042 / 0.037 px | ~0.1 s |
| 10 % duty cycle | 20/20 | 0 | 0.046 px | ~0.1 s |
| Unweighted centroid (each pixel counted once), same clips | | | 0.088-0.15 px | |
| LED absent | 0 spots in 10/10 | 0 | | |

- **Frequency error:** 0.009 Hz at 1 kHz over 1 s; 0.12 Hz inside flicker.
- **Memory:** peak numpy use 69 MB at 1 Mev/s and 157 MB at 10 Mev/s, plus
  16 bytes per event.
- **Real centroid error:** about 0.1-0.3 px (estimate). The synthetic model
  leaves out pixel non-uniformity, bloom, ERC drops, EVT3, clock drift between
  LED and camera, and low-light latency.

**When the scene is busy.**

- A lamp in view with deep modulation can give each pixel several events per
  half-cycle, at or above 1 kHz. Such pixels pass the rate gate. (The synthetic
  generator assumed about 240 events/s per flicker pixel, which is a guess, not
  a measurement.) The period test still rejects the lamp, but the run time
  moves towards the no-gate case: 6.6 s per second of events at 10 Mev/s,
  against 124 ms at 1 Mev/s.
- A dense, non-periodic 80 x 80 px region (9.6 of 13.6 Mev/s) took 1.54 s,
  and the LED was still found (0.064 px).
- **The real protection** is the camera's area, or a box you draw around the
  LEDs, together with a short clip.

### 3.3 The LED to use

- **Frequency.** 1 kHz by default; 0.6-2 kHz works. Accuracy was flat from
  300 Hz to 2 kHz (0.037-0.048 px, measured).
  - Stay above about 600 Hz: in Censi et al.'s tests (2013), motion made
    transitions only below about 600 Hz. That also keeps the LED clear of the
    anti-flicker band (50-520 Hz).
  - Avoid 490 Hz (the other Arduino default), which is inside both of those
    bands.
  - The upper limit is unknown. The synthetic detector broke down at 5 kHz,
    but that came from the assumed burst model, not from a measurement. The
    real limit depends on the IMX636 pixel dead time, which
    `I_Monitoring.get_pixel_dead_time()` reads from the camera; Typhon's
    Camera tab already shows it as "Pixel dead time".
- **Several LEDs (stages B and C).** Space their frequencies at least 10 %
  apart, with no 2:1 ratio between any two, so they share no harmonics: for
  example 900, 1100, 1300, 1500, 1700, 1900 Hz. Mains flicker is not a reason
  to avoid round numbers: a 1 kHz LED inside a 100 Hz flicker region was found
  30/30 times, because the detector compares periods. Run the survey to see
  what else in the room blinks. Each LED needs its own hardware timer. An
  ATmega328P (Arduino Uno) has 3 timers; an RP2040 has 8 independent PWM
  slices. Toggling pins from software adds jitter.
- **Driver.** A microcontroller's hardware PWM pin (crystal-timed) with a
  series resistor. RC or 555 timers drift. Typhon measures the frequency
  (3.2, step 9) rather than trusting a typed value.
- **Duty cycle.** 50 %. The method compares transitions of the same kind, so
  the duty cycle does not matter (10 % measured 0.046 px), and 50 % gives equal
  ON and OFF data.
- **Spot size.** Aim for **3-8 px** on the sensor. The emitter should be about
  3-8 times the mm/px at the working distance; at 0.15 mm/px that is
  0.5-1.2 mm, such as an 0603 or 0805 SMD LED, or a diffused 3 mm LED behind a
  1 mm hole. Slight defocus is fine, because an even blur keeps the centre.
- **Brightness.** Clearly above the background but not blooming. If a halo of
  events surrounds the spot, lower the LED current rather than the biases.
- **Height.** An LED die sits 1-5 mm above its board. At 500 mm, 3 mm of
  height is a 0.6 % scale error, the size of the whole stage-B budget
  (computed). Mount LEDs flush with the motion plane, or record the emitter
  height so Typhon can correct for it.
- **Reflections.** A glint of the LED on a glossy surface, or on shiny
  particles, is a second spot at *exactly* the LED's frequency. In stage C that
  makes "frequency = name" ambiguous. Typhon rejects such spots by size and
  brightness, or asks you to confirm. Plan baffles, a black surround and a low
  LED current.

### 3.4 Camera settings for the calibration clip

Typhon switches ERC, the trail filter and anti-flicker off for the clip only,
then restores them (3.10). It leaves the biases as you set them; the bias
advice below is a starting point (no physical camera was used).

- **Biases:** start from the defaults (0). The directions below are from
  Prophesee's bias manual (vendor docs, not re-checkable offline).
  - Do not lower `bias_fo`: lower narrows the bandwidth and can lose kHz edges.
  - `bias_hpf` may be raised (within 0-120) to suppress slow background
    changes.
  - Raise `bias_diff_on` / `bias_diff_off` only if background noise is high.
  - The IMX636 has two sets of ranges (OpenEB 5.2 source). The
    **recommended** ranges are `diff_on` -85..140, `diff_off` -35..190, `fo`
    -35..55, `hpf` 0..120, `refr` -20..235. The wider **allowed** ranges are
    `fo` -150..200, `hpf` 0..255, `diff_on`/`diff_off` -150..200, `refr`
    -50..255. By default the HAL **refuses** any value outside the
    recommended range (`I_LL_Biases::set`); only a device opened with
    `biases_range_check_bypass` accepts the rest. Typhon never sets that
    bypass.
  - Typhon's Biases tab shows each bias's *allowed* range, with a green bar
    marking the recommended one (`camera_settings.py:1402-1415`). So a value
    such as `hpf` 140 can be dialled in Typhon but is refused by the camera.
    (The simulated camera's table, `cameras.py:1773-1779`, uses the
    recommended ranges.)
  - Prophesee's active-marker bias set, meant to give one event per blink
    (`diff_off` 180, `diff_on` 60, `fo` 30, `hpf` 140, `refr` 0; vendor docs),
    is therefore **not used**: its `hpf` 140 is refused by default, and it
    would also need the weaker ON-only mode.
- **ERC: off. Required, not just advised.**
  - In OpenEB's IMX636 driver (`gen41_erc.cpp`) the ERC drops events in
    **time slots**: `t_drop` only, with horizontal and vertical dropping
    written as 0. The reference period is 200 µs, and the default target is
    4000 events per period (20 Mev/s). (Prophesee's manual says the ERC drops
    "spatially and temporally"; the open driver uses only the time part.)
  - What hurts is missed LED edges, not uneven weighting. Losing whole 200 µs
    slots: 20 % loss still found the LED 6/6 (0.036 px); 30 % found it 5/6
    (0.085 px); 50 % found it 0/10 (measured).
  - By contrast, dropping 50 % of events at random still gave 0.064 px, and
    dropping 50 % on every other column shifted x by only 0.008 px.
- **Trail / STC filter: off.** (A *burst* is a run of same-polarity events
  at one pixel within the threshold time.)
  - TRAIL keeps the first event of a burst, and STC_CUT_TRAIL keeps the
    second. Keeping one event per burst turns the weighted centroid into the
    unweighted one (0.12-0.15 px instead of 0.04-0.05, measured).
  - STC_KEEP_TRAIL keeps every trailing event, but both STC modes delete a
    pixel's lone events. With one event per blink, STC would remove the LED
    entirely.
  - The threshold is in whole milliseconds, minimum 1000 µs, which is exactly
    one period of a 1 kHz LED.
- **Anti-flicker: off.** The detector rejects mains flicker by itself (30/30
  inside a 100 Hz region).
  - The IMX636 filter acts on 50-520 Hz (OpenEB source; Prophesee's docs give
    50-500 Hz). Its period is set in 128 µs steps and its duty cycle in
    sixteenths.
  - It cannot touch a 1 kHz LED. However, missed LED cycles create intervals
    of 2T (500 Hz for a 1 kHz LED), which fall inside the band. If it must be
    on, narrow the band (for example 90-130 Hz).
  - Prophesee's docs say it works on 4 x 4-pixel blocks, so a block shared by
    a lamp and the LED could lose LED events. That claim is not in the OpenEB
    source and is unverified.
- **Area:** the camera's area, or a drawn box, around the LEDs.
- **Clip length:** 0.25-0.5 s (0.1 s already gave 0.043 px). 1 s at 1 Mev/s is
  about 3.9 MB as EVT2.

### 3.5 The four geometry stages

Each stage keeps the earlier stages' data and adds to it.

**What one LED plus a distance can and cannot give.**

- The scale uses the thin-lens formula with pixel pitch p = 4.86 µm (IMX636;
  the EVK4 datasheet and Willert & Klinner's table agree). The distance D is measured from the **sensor
  plane**, so u + v = D, u = (D + √(D² − 4fD)) / 2, and **mm/px = p × u / v**.
- The common shortcut p × D / f overestimates the scale (computed): 16 mm lens
  at 500 mm +7.0 %, at 1000 mm +3.3 %; 8 mm at 500 mm +3.3 %; 25 mm at 500 mm
  +11.5 %; 50 mm at 1000 mm +11.5 %. Typhon uses the thin-lens form. A real
  lens is thick, and where its principal plane sits is not known, so a few %
  remain at short range.
- Worked example (computed): a 16 mm lens with the object 300 mm from the
  lens's principal plane gives 4.86 µm × 284 / 16 ≈ 0.086 mm/px, so
  10,000 px/s ≈ 0.86 m/s.
- **To measure to the sensor plane,** measure to the lens mounting face and
  add the flange distance: **17.526 mm for C-mount, 12.526 mm for CS-mount**
  (the EVK4 takes both, with an adapter). Typhon records which one was used.
- **The distance must be along the optical axis.** If you measure straight to
  an off-axis LED, Typhon corrects by cos(atan(r_px / f_px)); left
  uncorrected, the error is 2.4 % at the image corner with a 16 mm lens
  (computed).
- **One LED cannot give:** the tilt of the motion plane, the scale at other
  depths, lens distortion, the exact focal length (the nominal value is
  rounded and changes with focus), the image centre, or the direction of world
  axes.

| Stage | Hardware | You enter | Gives | Speed accuracy | Assumes |
|---|---|---|---|---|---|
| **A** | 1 LED | Focal length (lens list or typed), distance, how it was measured (C or CS mount, to the LED or along the axis), emitter height | mm/px at the LED's depth | About ±3-10 % (estimate), mostly from focal-length tolerance, the distance reference and where the principal plane is | Motion in a plane through the LED, **parallel to the sensor**, near the centre of view; lens focused near that distance |
| **B** | 2 LEDs a known distance L apart, or 1 LED moved L along a ruler or rail (two clips) | L | Direct mm/px = L / pixel distance; the in-plane axis (speeds can be reported along and across it); an A-against-B check; the focal length (see below) | About 0.5-1.5 % (estimate) for motion parallel to the sensor: a ruler good to ±0.5 mm per 100 mm is 0.5 %, and centroids (0.05-0.2 px over a 500 px baseline) add 0.01-0.04 %. Tilt adds an error that depends on direction (below) | One plane, parallel to the sensor; a single scale |
| **C** | 4 or more LEDs on one flat surface, no 3 in a line (6-9 recommended, so there is a residual to check), each at its own frequency; or 1 LED moved to 4 or more marked spots | Each point's (X, Y) in mm | Homography (normalised DLT): perspective removed for that plane; per-point residuals | 0.02 % mean with no distortion (computed). With radial distortion k1 = -0.1 / -0.3: 16 mm lens 0.12 / 0.37 % mean (99th percentile 0.30 / 0.90 %); 8 mm lens 0.49 / 1.49 % mean (1.2 / 3.5 %) | Motion in **that** plane (points off it are mis-scaled by the depth ratio); distortion not corrected |
| **D** | A blinking LED grid board (one row at another frequency to fix its orientation), or a blinking checkerboard on a screen; 10-50 views | Grid pitch | Intrinsics and distortion (Zhang's method, as in OpenCV `calibrateCamera`). Points are then undistorted and C is re-fitted from the stored points **without re-recording**. The plane's pose follows, so parallel planes at other heights become possible | 0.016-0.027 % mean, 99th percentile ≤ 0.10 %, with **exact** intrinsics (computed upper bound). A real calibration's residual adds to this: 0.05-0.3 % (estimate) | A rigid, flat pattern and varied poses. Needs cv2 (not in the council env) or a numpy/scipy version of Zhang's method |

Stage C was also run end to end on a synthetic event clip: 6 LEDs at
900-2300 Hz on a tilted plane at about 2 px/mm. Centroid errors were
0.007-0.072 px, and plane errors 0.022 mm mean (0.038 max) with 4 LEDs and
0.015 mm mean (0.033 max) with 6 (measured). The numpy fit matches OpenCV's
`findHomography` to within 1.1e-5 mm with 4 points and 1.3e-3 mm with 9, and
the numpy undistortion matches `cv2.undistortPoints` to within 5e-10 px
(checked with cv2 4.5.5 in the `pylon` env).

**Tilt, for stages A and B.** A single scale cannot follow a plane seen at an
angle, and the error depends on the direction of motion, so it is quoted as
"up to" (computed):

- Motion along the two-LED baseline is always measured right, because the
  scale was taken along it. Motion in other directions is not:
  - with the baseline along the axis the plane tilts about (the axis that is
    not shortened), motion across it reads **low** by 1 − cos(tilt): 2°
    0.06 %, 5° 0.38 %, 10° 1.52 %, 20° 6.0 %;
  - with the baseline on the shortened axis, motion along the other axis
    reads **high** by 1/cos(tilt) − 1: 10° 1.54 %, 15° 3.53 %, 20° 6.42 %.
- On a plane tilted 15° and 10° at 500 mm, the worst error over all motion
  directions is **10.6 %** with a 16 mm lens over 120 x 70 mm (4.4-10.6 %
  depending on direction), and **17.0 %** with an 8 mm lens over 240 x 135 mm
  (8.5-17.0 %).
- Distortion alone, on a plane facing the camera, gives a worst single-scale
  error of 0.48 % (k1 = -0.1) to 1.45 % (k1 = -0.3). The stock EVK4 lens has a
  47.7° diagonal field of view and distorts. AM cameras often look at an angle
  through a viewport.

**Focal length from stage B.** Two different "focal" numbers come out of
stage B, and they must not be mixed up, so both are stored under clear names:

- `focal_thin_lens_px` = mD/(1+m)² / p, where m = p / (mm/px) is the
  magnification;
- `principal_distance_px` = mD/(1+m) / p, which is what OpenCV's fx means.

At 500 mm with a 16 mm lens they are 3301 px and 3414 px, 3.4 % apart
(computed). Only the principal distance is valid for the pinhole camera model
at that focus. Reusing the focal length at another distance assumes the lens
is refocused there.

**Conclusion.** For "anything" scenes, stage B is the minimum honest
calibration when the camera faces the motion square-on. Stage C is the first
stage that survives a camera at an angle. From C onward, the tracker, not the
calibration, limits velocity accuracy.

### 3.6 From px/s to mm/s

The tracker works in sensor px and µs. Arrows stay in picture pixels; colour,
histogram and legend use mm/s.

```python
def to_plane(cal, p):                        # (N,2) sensor px -> (N,2) mm on the plane
    p = np.asarray(p, float)
    if cal.intrinsics:                       # stage D: undistort first
        p = undistort(cal.K, cal.dist, p)
    if cal.H is not None:                    # stage C (fitted on undistorted px when D exists)
        q = np.c_[p, np.ones(len(p))] @ cal.H.T
        return q[:, :2] / q[:, 2:3]
    return (p - cal.origin_px) @ cal.R.T * cal.mm_per_px   # stage A/B: scale + axis

def velocity_mm_s(cal, p_px, v_px_s, h=0.5):
    """Velocity on the plane: a central difference of about h px along the motion."""
    p = np.asarray(p_px, float)
    v = np.asarray(v_px_s, float)
    speed = np.hypot(v[:, 0], v[:, 1])[:, None]             # px/s
    u = v / np.maximum(speed, 1e-12)                        # direction of motion
    mm_per_px = (to_plane(cal, p + h * u) - to_plane(cal, p - h * u)) / (2 * h)
    return mm_per_px * speed
```

- The step must be small. A fixed 1 ms step grows with speed (70 px at
  70,000 px/s, 200 px at 200,000 px/s). Through stages C and D with an 8 mm
  lens (k1 -0.3) on a tilted plane, such a step was off by 0.09 % and 0.11 %,
  more than stage D's whole 0.02 % budget (computed). A step of about 0.5 px is
  accurate.
- For stage C without D, the exact derivative (the homography's Jacobian) can
  be used instead. With w = h31·x + h32·y + h33 and (X, Y) the mapped point:
  J = (1/w)·[[h11 − X·h31, h12 − X·h32], [h21 − Y·h31, h22 − Y·h32]], and
  v_mm = J·v_px.
- Every output carries the calibration's stage and accuracy note, for example
  "±1 %, stage B".

### 3.7 Where calibrations are kept

The store is **`<project>/camera_calibrations.json`**, built on the same rules
as `camera_presets.py`:

- JSON only, never pickle; read as UTF-8 with or without a byte-order mark.
- Written whole to a temporary file, then renamed into place.
- An OS lock on `.camera_calibrations.json.lock` while changing it.
- Validated on every read. A damaged file is never overwritten silently;
  `repair=True` moves it aside first. A file written by a NEWER version is
  refused and never rewritten.
- Kept per camera (`Identity.key`: backend|model|serial). The presets'
  `legacy_keys` handling is reused, so a calibration saved under
  `prophesee|EVK4|<serial>` is found as `prophesee|IMX636|<serial>` and is not
  orphaned.
- **Several set-ups per camera.** A camera moved from the bird bath to the AM
  rig keeps its serial, so each calibration also carries a set-up name (rig,
  lens, distance). You pick which one is active, and the picker shows set-up,
  lens, distance, stage and date.
- **Per project.** The same rig calibrated in project A is invisible in
  project B, so Typhon offers to import or copy it.
- **Revisions are never changed.** Saving creates a new id, and the camera's
  `active` pointer moves to it, so a run that names an id always finds the
  exact numbers.
- **Clips are kept.** Calibration clips are stored at
  `<project>/calibration/<id>/clip_NNN.raw`, so they can be re-analysed later.
- **Each entry records** the clip's camera state (biases; ERC, anti-flicker
  and trail on or off; the area), the camera's own readings at the time
  (`I_Monitoring`: pixel dead time, illumination, temperature), and a quality
  figure per clip, such as the share of doubled (2T) intervals. Missed edges
  are the failure that ERC or a USB overflow causes.

Example entry (stage B; numbers consistent with the 16 mm lens at 500 mm):

```json
{
  "format": "typhon-camera-calibrations", "format_version": 1,
  "cameras": {
    "prophesee|IMX636|<serial>": {
      "active": "cal-20261007-142233-3f9a",
      "calibrations": [{
        "id": "cal-20261007-142233-3f9a", "name": "Bench 16 mm @ 500 mm",
        "setup": "Bench", "parent": null,
        "created": "2026-10-07T14:22:33", "host": "LAB-PC",
        "camera": {"backend": "prophesee", "model": "IMX636", "serial": "<serial>"},
        "sensor": {"width": 1280, "height": 720, "pixel_pitch_um": 4.86},
        "coords": "sensor_px_unbinned_centre_at_index",
        "captured_with": {"area": [0, 0, 1280, 720], "binning": [1, 1],
                          "mirror": [false, false], "decimation": [1, 1],
                          "biases": {"bias_diff_on": 0, "bias_diff_off": 0, "bias_fo": 0,
                                     "bias_hpf": 0, "bias_refr": 0},
                          "erc": false, "antiflicker": false, "trail": false,
                          "monitoring": {"pixel_dead_time_us": null, "illumination": null,
                                         "temperature_c": null}},
        "lens": {"label": "16 mm f/1.4", "mount": "C", "focal_length_mm": 16.0,
                 "source": "lens_list", "focus_note": "focused at 500 mm, locked"},
        "stage": "B",
        "points": [
          {"id": "L1", "freq_hz": 1000.2, "px": [287.31, 358.77], "px_sd": 0.05,
           "plane_mm": [0.0, 0.0], "emitter_height_mm": 0.0,
           "clip": "clip_001.raw", "t_us": [0, 500000], "quality": {"double_period_share": 0.002}},
          {"id": "L2", "freq_hz": 1300.4, "px": [993.62, 361.02], "px_sd": 0.05,
           "plane_mm": [100.0, 0.0], "emitter_height_mm": 0.0,
           "clip": "clip_001.raw", "t_us": [0, 500000], "quality": {"double_period_share": 0.001}}
        ],
        "distance": {"mm": 500.0, "from": "sensor_plane", "along": "optical_axis"},
        "models": {
          "scale": {"mm_per_px": 0.14158, "method": "two_point", "axis_deg": 0.18,
                    "stage_a_mm_per_px": 0.14199, "a_vs_b_pct": 0.30},
          "focal_thin_lens_px": 3301.0,
          "principal_distance_px": 3414.4,
          "homography": null,
          "intrinsics": null
        },
        "accuracy": {"speed_pct_estimate": 1.0,
                     "valid_for": "motion in the LED plane, parallel to the sensor"},
        "detector": {"name": "led_locate", "version": 1, "tol": 0.05},
        "checks": []
      }]
    }
  }
}
```

At stage C, `homography` holds `{"H", "fitted_on": "raw_px" | "undistorted_px",
"rms_px", "rms_mm", "n"}`. At stage D, `intrinsics` holds `{"K", "dist":
[k1, k2, p1, p2, k3], "model": "opencv5", "rms_px", "n_views"}`.

### 3.8 Coordinates: area, binning, mirroring

- Everything is stored in **full-sensor, unbinned pixel coordinates**, with
  pixel (x, y) centred at index (x, y).
- EVK4 events in a .raw are already in sensor coordinates, whatever the area.
  The live picture, the PNGs and the raw view are relative to the area:
  sensor = picture + area origin (`EvkDevice.read` subtracts it).
- So a change of area **never invalidates** a calibration; it is only a
  conversion. Typhon warns when the current area does not cover the calibrated
  points or plane.
- For frame cameras (Basler), more is involved. None of it applies to the
  EVK4:
  - binning b: x_sensor = (x_pic + area_x) × b + (b − 1)/2;
  - decimation, the same way;
  - mirroring (`ReverseX` / `ReverseY`): x_sensor = W − 1 − x.

  The camera record's `sensor` on a binned Basler is in **binned** pixels
  (`frame_camera.py:1655`; see `camera_settings.py:79-83` and
  `camera_record.py:18-21`), so Typhon multiplies by binning and decimation
  before comparing sizes, and reads mirroring (`ReverseX` / `ReverseY`,
  `camera_settings.py:763-778`) from the record's settings. Otherwise a valid
  calibration would be wrongly refused.

**Checks when a calibration is used.**

- Camera identity does not match: refuse, or offer "copy to this camera" with
  a warning.
- Sensor size or pixel pitch does not match (after the conversion above):
  refuse.
- Lens, focus or camera moved: metadata cannot detect this. You confirm, or
  run **Check**, which re-records the LEDs in place and compares positions. A
  shift above about 0.5 px means the calibration is stale.
- An optional **witness LED** can stay in a corner of every recording, so each
  run checks itself; its events are masked out of velocity. Its light still
  makes particles flicker at its frequency, and masking does not undo that, so
  keep it out of the light path or baffle it.

### 3.9 Each run's frozen copy

- **At Start.** If the camera has an active calibration, Typhon writes
  `<run>_calibration.json` beside `<run>_camera.json`. It is a frozen copy of
  the whole entry plus the sha256 (a fingerprint) of its canonical JSON. It is
  written once, with the same no-overwrite rename as the camera record. Like
  the camera record, it is **never a reason not to capture**: a failure is
  reported in Start's summary and never raised.
- **Optional pointer in the camera record.** The record may gain an additive
  key `"calibration": {"id", "name", "stage", "sha256"}` **without** raising
  `format_version`. `camera_record.read()` returns None for any version above
  the one it knows, so raising it would make older Typhons ignore new records
  entirely.
- **In the velocity output.** The analysis records the calibration it
  **actually used** (id, sha256, stage, model numbers). You may apply a later
  calibration (for example one made after recording) if camera identity and
  sensor match and you confirm, or a Check shows, that camera and lens did not
  move.

### 3.10 What calibrating looks like in Typhon

1. **Calibrate...** shows the current camera, its calibrations and which is
   active, and offers "New" or "Add stage".
2. **What is blinking?** (optional) runs a 0.25 s survey and lists the
   frequencies found. Use it to pick LED frequencies clear of room lights and
   to store each LED's measured frequency.
3. **Record calibration clip** (0.5 s) into the project's
   `calibration/<id>/` folder. ERC, trail and anti-flicker (and the camera's
   area, if the clip narrowed it) are switched off for the clip only and
   restored in a `finally` block, so they come back even on an error. Typhon
   decides `preset_changed` by comparing the camera with the preset in use at
   the next Start (`_preset_still_in_use`, `frame_camera.py:1682`). A clean
   restore therefore leaves the next run's record naming the preset; a
   restore that failed shows up there as a difference, and the Calibrate
   window says which setting did not come back. The area or a drawn box
   limits the region.
4. **Detect** draws a cross-hair and circle on each spot, labelled for example
   "1000.2 Hz, 23 px, ±0.05 px". When there are several spots, you click the
   right one.
5. **Stage A form:** lens (from the project lens list, or typed), distance in
   mm, how it was measured ("along the axis" or "straight to the LED"), the
   reference ("from the sensor", or "from the lens mount face" with C +17.53 mm
   or CS +12.53 mm), and the emitter height. Typhon shows the mm/px, a 10 mm
   ruler drawn on the picture to eyeball, and the stage-A accuracy note.
6. **Save.** The new calibration becomes the active one.
7. **Add stage B or C points:** record again with 2 or more LEDs (or the moved
   LED), then enter distances or (X, Y), or pick a saved LED-board
   definition. Typhon shows each point's residual and a plane grid, then saves
   a new revision.
8. **Stage D:** a guided multi-pose capture of the blinking grid. It shows the
   reprojection error, then re-fits C automatically.
9. **Check:** record 0.25 s, then show each point's shift and OK or stale.

---

## 4. What gets saved per run

The capture folder is raw data. Analysis writes **new** files beside the run
and never writes over anything.

| File | When | What it holds |
|---|---|---|
| `<run>_events.raw`, `<run>_frames.csv`, PNGs, `<run>_camera.json` | Capture (exists today) | Untouched. The .raw is opened with indexing off, so no `.tmp_index` file appears beside it |
| `<run>_calibration.json` | At Start, if the camera has an active calibration | The frozen copy and its sha256 (3.9) |
| `<run>_tracks.csv` | After analysis | **One row per track:** id; first and last time (µs from the capture's origin, the same clock as `raw_t_us` in the frames CSV); slices and events; start and end position (sensor px); mean vx, vy and speed in px/s and, when calibrated, mm/s; direction; a flag when the track touched a gap; a flag when it looked like a merge |
| `<run>_track_points.npy` | After analysis | The positions that draw the arrows and tails during playback: a structured numpy array, read with `allow_pickle=False`. One row per track per slice is 32 bytes, about 2.6 MB per recorded second at 10 Mev/s (computed from 81,866 rows/s); it can be thinned to one point every few ms |
| `<run>_velocity.json` | After analysis | The set-up and every limit used; the calibration actually used (id, sha256, stage, accuracy note); coverage (time analysed, each gap's start and end, windows dropped); counts; the histogram bins; the legend range (1st-99th percentile); the decoder used; timings; the software commit |

- **Why not one row per track per slice in the CSV:** the synthetic particles
  gave 81,866 such rows per second at 10 Mev/s and 355,000 at 50 Mev/s. That
  is roughly 8-35 MB/s of CSV, or gigabytes per minute. The per-track summary
  is what you open in a spreadsheet or the Inferno plotting tools; the `.npy`
  holds the detail.
- **Never over anything.** Each file is written whole to a hidden temporary
  name beside it (`.<name>.<pid>.tmp`, a new file of ours, removed if the
  write fails) and then renamed into place with a rename that refuses an
  existing name, exactly as `camera_record.write` does (Windows' `os.rename`
  refuses by itself; elsewhere a hard link). Running the analysis again
  writes `<run>_velocity_2.json`, `<run>_tracks_2.csv` and so on.
- **Run names.** The new suffixes (`_calibration.json`, `_tracks`,
  `_track_points`, `_velocity`) are added to the list in `_unique_run`
  (`frame_camera.py:1594-1595`) that marks a run name as taken.
- **Long paths.** Windows' 260-character path limit applies system-wide on
  this PC (`LongPathsEnabled=0`): Python's own `makedirs` failed past it during
  testing, and so did writing a benchmark result file. Deep capture folders
  can hit it. The short-name spelling from commit 9b595c2 is the existing way
  round it.
- **Calibration clips** are not run files. They live in the project folder
  (3.7).

---

## 5. Camera settings that matter, and how the settings tabs serve them

### 5.1 Settings that affect velocity

| Setting | Effect on velocity | Starting point |
|---|---|---|
| **Area (ROI)** | Cuts events at the sensor. The biggest lever for keeping up, live or offline | As small as the motion allows |
| **Trail / STC filter** | Removes noise, which was the main failure measured. But **both STC modes drop the first event of every burst and every lone event**: STC_CUT_TRAIL keeps only the second event, STC_KEEP_TRAIL the second and later ones. A pixel that fires once as a small, fast particle crosses it is deleted, so STC can erase exactly the particles being measured. TRAIL keeps only the first event of each burst. Threshold 1-100 ms, rounded to whole ms (OpenEB source) | TRAIL for fast particles, or test STC on a real recording first |
| `bias_diff_on` / `bias_diff_off` | Contrast threshold: lower means more sensitive, and more noise | Defaults; raise if noise forms false tracks |
| `bias_fo` | Low-pass filter. Lower narrows the bandwidth: it smears fast particles and adds latency | Do not lower for fast motion |
| `bias_hpf` | High-pass filter. Higher removes slow background changes | Raise against slow drifts in light |
| `bias_refr` | **Raising it shortens the rest period, which gives more events**; lowering it gives fewer events per crossing | Lower only to thin heavy streams |
| **Anti-flicker** | Needed under mains lighting (100/120 Hz flicker) | A narrow band such as 90-130 Hz |
| **ERC** | Caps the rate. Prophesee's manual says it drops "spatially and temporally" and that signal quality degrades once it triggers; OpenEB's IMX636 driver drops whole 200 µs time slots. For tracks that means gaps in time | Off where possible; prefer the area and filters. If used as a safety cap, its state is recorded with the run |
| **Event-rate activity filter** | Not on the EVK4. OpenEB 5.2 implements it only for Gen3.1 and GenX320 sensors, so `get_i_event_rate` should return None and Typhon's `_activity` (`camera_settings.py:1509-1512`) hides the group | Confirm with a real camera |
| **Picture window vs slice** | Independent: the picture can stay at 20 ms while Particles use 1 ms slices | Leave the picture at 20 ms |

The bias directions above are from Prophesee's bias manual and could not be
re-checked offline.

**Three help texts in Typhon need fixing to match** (small, separate fixes):

- `bias_refr` (`camera_settings.py:1299-1300`) gives no direction. It should
  say "higher = shorter rest = more events".
- ERC (`camera_settings.py:1423`) says it "drops events evenly". It actually
  drops in time slots, and quality degrades once it acts.
- The trail filter (`camera_settings.py:1487-1489`) says STC "keeps only
  confirmed events". It should also say that STC deletes a pixel's lone
  events, which is what a small, fast particle makes.

### 5.2 How the settings tabs and saved configurations serve this

The prelude (`typhon/settings-tabs`, now on `qt-migration` at 906bb26) gives
Typhon tabs under the image folder: Basic, then the connected camera's own
categories (for an EVK4: Biases, Filters, Display and Camera), then Presets.
Each category has a pop-out window, and configurations (presets) are saved
per camera in the project's `camera_presets.json`, with Export and Import
that never write over a file or a preset. The Biases tab already marks each
bias's recommended range, and the Camera tab already shows the sensor's
readings (temperature, illumination, pixel dead time), which the calibration
record stores (3.7).

- **A velocity preset is an ordinary camera preset.** "Spatter velocity" or
  "Bird bath" saves the biases, the filters and the camera's area, exactly as
  presets already do. The run's camera record already notes which preset was
  applied and whether it still matched (`preset` / `preset_changed`), and
  `velocity.json` points at that record.
- **The tracker's set-ups are saved separately**, in
  `<project>/velocity_setups.json`, which follows the same store rules. Each
  set-up (Particles, Objects or your own limits) can name the camera preset it
  goes with, and choosing it offers to apply that preset too. They are not put
  inside the presets file because `camera_presets.save`
  (`camera_presets.py:419-493`) rebuilds a preset from its settings, area,
  dates and note only. Any extra block would be lost the next time that
  preset is saved, including by an older Typhon. Keeping them apart never puts
  your presets at risk.
- **A calibration clip is not a preset.** The Calibrate flow switches ERC,
  trail and anti-flicker off for the clip and puts your settings back (3.10),
  so your set-up returns exactly. If you calibrate often, a "Calibration"
  preset is still useful for the area around the LEDs.
- **Pop-outs side by side.** The Biases and Filters pop-outs can sit beside
  the velocity window. Live (stage 2), you can watch the noise blobs and the
  track count change as you turn a setting.
- **The velocity window** (histogram, legend, track table, direction view,
  counters) is its own pop-out. The settings column is narrow (432-464 px),
  and ten tabs were measured at 672 px of tab strip, which is why related
  settings already share tabs (`camera_categories.py:9-16`). So velocity
  opens from the raw view's **Analyse velocity** and from a button, rather
  than adding another tab.
- **Live (stage 2):** the velocity window shows the event rate against the
  live budget (section 7), with links straight to the Area and Filters
  settings.

`typhon/settings-tabs` has landed on `qt-migration` (906bb26), so the
velocity work can start from `qt-migration` directly.

---

## 6. Prophesee software and licences, and why we write our own

### 6.1 OpenEB 5.2.0 (on this PC, open source, Apache 2.0)

- **Built Python modules:** `metavision_hal`, `metavision_sdk_base`, `_core`,
  `_stream` and `_ui` (`C:/ceb/build/py3/Release`). The HAL needs
  `MV_HAL_PLUGIN_PATH=C:\ceb\build\lib\metavision\hal\plugins`.
- **In the source tree but not importable here:**
  - `metavision_core`, the pure-Python `EventsIterator` / `RawReader` package
    (`sdk/modules/core/python/pypkg`). It is not on the build's PYTHONPATH,
    and it cannot be imported without h5py.
  - `metavision_core_ml`, which includes an Apache-2.0 event simulator
    (`video_to_event`). It models latency, refractory period, shot noise and
    leak, so it would make more realistic test recordings than ours. It imports
    numba, so it becomes usable only if numba is approved.
- **Useful to us:**
  - HAL decode, which is Typhon's path today.
  - `CameraStreamSlicer`, which decodes in C++ and hands back one array per
    slice. Use it with the hint `index=false`; without the hint it writes
    `<name>.raw.tmp_index` beside the recording. That index is written in the
    background: it appeared within 3 s of opening, but a quick read that closed
    the camera first left none. Test the "no index" behaviour with a camera
    kept open.
  - `RAWEvt2EventFileWriter`, which writes synthetic `.raw` files that read
    back exactly. These make the test recordings. It writes EVT2 only.
  - The core algorithms (polarity and area filters, time surfaces, picture
    generators), which take Typhon's numpy arrays directly.
- **Not in OpenEB:** the cv, analytics, calibration and cv3d modules, so no
  optical flow, tracking, clustering, particle or speed measurement, frequency
  maps, LED markers or camera calibration, and no software noise filter.
- **The sensor itself** does not compute motion. (The event format has an
  optical-flow type, but the IMX636 never produces it.) Its on-chip filters
  (trail/STC, anti-flicker, ERC, area) are already in `camera_settings.py`.

### 6.2 Metavision SDK Pro (paid, proprietary; not on this PC)

What its modules do, from vendor docs (nothing tested here, not re-checkable
offline):

| Module | What it does | Fit |
|---|---|---|
| CV: SparseOpticalFlow | Tracks small, edge-like features with a Luenberger estimator (a standard way to follow a moving thing's state) and outputs EventOpticalFlow in px/s, **one estimate per feature**, so a bird yields many arrows, not one | Partial |
| CV: plane fitting, triplet matching, time-gradient flow | Normal flow only (motion across an edge, not the full motion); Prophesee calls them costly at high rates | Poor |
| Analytics: SpatterTracker | Grid clusters matched by distance; outputs boxes with ids but **no velocity**; suited only to objects that do not collide. Default minimum object size is 10 x 10 px and its example uses 50 µs accumulation, so small spatter is hidden unless retuned | Same idea as ours |
| Analytics: PSM (particle size measurement) | vx, vy (in px/µs) and size, assuming particles fall straight across lines at constant speed | Too narrow for spatter |
| Analytics: Tracking | General object tracker. Motion models SIMPLE, INSTANT, SMOOTH and KALMAN (constant velocity or acceleration; adaptive noise or measurement trust); default object size 10 x 10 to 300 x 300 px; **no min/max speed setting** | Possible for birds |
| Analytics: FrequencyMap / PeriodMap | Per-pixel frequency (7 equal periods, 1500 µs tolerance, defaults 10-150 Hz for vibration) | Same idea as our LED detector |
| CV: ModulatedLightDetector + ActiveMarkerTracker | LED markers, each sending an id coded in the gaps between rising edges (2a = 0, 3a = 1, 4a = start, a = 200 µs) | Not needed |
| Calibration | Blinking frame generator, blinking dot-grid detector (first row 125 Hz, others 166 Hz), blinking chessboard detector, pinhole estimator; shows patterns on a screen from HTML pages and suggests about 50 detections for a 9 x 6 board | Stage D equivalent |

**Licence and practical issues.**

- **Which version you get depends on the purchase date.** Prophesee USB EVKs
  bought on or after 7 Oct 2024 came with SDK 5 Pro. Earlier ones got
  SDK 4.6, whose Windows build supports only Python 3.8 and 3.9. "Free,
  including commercial use" is confirmed only for Metavision Intelligence 3.0
  (June 2022). The 4.6.2 docs mention a premium "SDK Pro" source tier, and its
  installer needed an account (a request form plus a company sign-in).
- **Current terms.** Prophesee's release notes say that from SDK 5.3 onward
  the SDK ships under a development licence by default (5.3.0 released
  01/04/2026, 5.3.1 on 28/04/2026; the local OpenEB 5.2.0 matches SDK 5.2.0 of
  12/01/2026). The March 2026 terms:
  - allow non-commercial internal evaluation, development, testing and
    demonstration only;
  - allow one copy per Developer, where Developers are the employees named in
    a Subscription Form (the number is set by the subscription), plus five
    Devices for Demonstration;
  - are revocable and run for one year;
  - forbid redistribution or building the SDK into products;
  - are governed by French law (Paris courts), include an export-control
    clause, and supersede earlier licence terms.

  The October 2024 launch said commercial use was allowed, so the terms have
  changed. Whether an in-house AM lab counts as commercial is a legal question.
- **Getting it:** the installers sit behind a Prophesee login.
- **Python:** SDK 5.3.1 supports Python 3.10-3.12 on Windows 11, so it would
  load in the council's 3.11 env. Loading its DLLs and OpenEB 5.2.0's in one
  process is untested and may clash.
- **Origin:** Prophesee is French. Your US-origin rule covers ML models; this
  is not an ML model.
- **ML is ruled out anyway.** OpenEB's event-to-video network has a flow
  option, but its checkpoint is a pickle, it is not US-origin, and it needs
  pytorch_lightning and kornia, which are not installed.

### 6.3 Why we write our own

- What we need (the tracker and the LED detector) is not in OpenEB, and each
  is small: numpy and scipy, nothing to install, Python 3.11, fully offline.
- The SDK's trackers do not give what you asked for without work. SpatterTracker
  gives no velocity and hides small spatter by default; SparseOpticalFlow gives
  many arrows per object; PSM assumes straight falls.
- The SDK's terms are now non-commercial by default, revocable and yearly, the
  installer needs a login, and it may clash with OpenEB 5.2.0.
- Our own code lets Typhon decide exactly what is written beside a run, and
  guarantee that nothing is written over it.

---

## 7. Performance on this laptop

The laptop is an i7-14700HX (8 performance and 12 efficiency cores,
28 threads), using the council env (Python 3.11.14, numpy 2.3.5,
scipy 1.17.1). Unless stated, the code ran on a single thread on synthetic
EVT2 recordings.

**Timings swing by up to about 30 % between runs** (the same clustering ran at
11.5 Mev/s once and 16.4 another time), so ranges are quoted.

**EVT3 is untested.** A real EVK4 records EVT3, which neither decode speed nor
the 24-bit clock wrap (every 16.78 s) has been tested on.

### 7.1 Measured numbers

| Step | Measured | Notes |
|---|---|---|
| HAL decode, Typhon's copy-per-callback loop | 15.6-17.7 Mev/s | Each callback carries at most 320 events, whatever `n_events_to_read` is set to |
| `CameraStreamSlicer`, decode only | 52-73 Mev/s | Depends on the file and slice length. Identical events to the HAL loop (81,866 / 16,374 / 2,955 rows) |
| Tracker, Particles, 1 ms slices (events already loaded) | 1.2x real time at 1 Mev/s; 0.93x at 5; 0.70x at 10; 0.19x at 50 | Above 1x keeps up; below 1x falls behind |
| Tracker, Particles, 2 ms slices | 2.25x at 1 Mev/s; 1.58x at 5; 0.93-0.98x at 10 | Doubling the slice nearly doubles the speed at low rates. The prototype counts and labels the whole sensor's cell grid in every slice, so much of its cost is per slice, not per event |
| Tracker, Objects | 11.4x real time at 1 Mev/s | |
| Whole job: HAL decode + tracker, Particles 1 ms | 0.85x (threaded) to 1.05x (inline) at 1 Mev/s; 0.21-0.26x at 10 Mev/s | A 10 s recording at 10 Mev/s takes about 40-50 s |
| Thread pool: slices clustered in parallel, linked in order | 11.5 → 34-35 Mev/s with 4 or more threads (2 ms slices, 50 Mev/s); only about 1.2x at 1 ms slices and 10 Mev/s | numpy releases Python's lock (the GIL) on large arrays |
| Existing raw view (`RawPlayback`) | 1.8-4.7 Mev/s | Only 1.8 on a 1 Mev/s recording (0.91 with 1 ms windows), because every window pays 5-9 ms of whole-sensor drawing (921,600 pixels). Its docstring's "tens of millions of events a second" (`event_playback.py:29`) is wrong and should be corrected |
| Live grab loop (`EvkDevice`), before any velocity work | 5.3-5.5 Mev/s | Without .raw logging, the PNG writer, the frames CSV or the display, so a real capture's budget is lower |
| Live worker, Particles 1 ms, at 1 Mev/s | 88 % busy (1.77 s of work per 2 s) | |
| Flat-index `accumulate_events` | 1.83-1.91x faster, identical pixels | Headroom for the live picture |
| Overlay rasterised with PIL | 5.2 / 7.4 / 16.5 ms for 10 / 100 / 500 tracks | The vector overlay (2.5) avoids this |
| LED detector | 98-107 ms per second of events at 1 Mev/s; 311-377 ms at 10 Mev/s | Plus HAL decode: 53-62 ms per million events (EVT2). 1 M events decoded bit-identically (3.87 MiB) in 0.10 s including opening the file |
| GPU (torch 2.5.1, RTX 4070 Laptop), simple per-slice port | 22 Mev/s against 16 for numpy | Not worth it |

**Computed from the decode rates:** at 50 Mev/s, decoding one recorded second
takes about 3 s through the HAL loop, or about 0.7-0.8 s through the slicer
(60.7-73.2 Mev/s on that file). The fact-check quoted "about 1.5 s" and
"about 0.4 s"; those are for its 50 Mev/s test file, which is only 0.5 s
long (25 million events), so per recorded second they double.

**About the live bench.** It replayed a `.raw` through `EvkDevice`, whose
windows run on the wall clock (`cameras.py:1663`). A file replays as fast as
it decodes, so each "20 ms" window held 0.1-0.7 s of events. Its window counts
and drop figures are therefore not valid. (It also dropped the newest entry
when full; the design drops the oldest.) The 5.3-5.5 Mev/s throughput is still
fair. The same wall-clock rule matters with a real camera: once the grab loop
falls behind, each window silently covers more than `window_ms` of events.

### 7.2 What that means

- **Offline (stage 1):** any recorded rate can be analysed; it just takes
  longer. About real time at 1 Mev/s; about 4-5x the recording's length at
  10 Mev/s. At 50 Mev/s a single thread needs roughly 6 s (slicer decode) to
  8.5 s (HAL loop) per recorded second (computed from the tracker and decode
  rates above).
- **Live Objects:** fine at 1 Mev/s (11.4x). Birds are expected at
  0.1-5 Mev/s (estimate), but crowding limits accuracy before speed does
  (2.3).
- **Live Particles:** with 1 ms slices, up to about 3-4 Mev/s. With 2 ms
  slices the tracker reaches 5-10 Mev/s, but the grab loop's own budget (about
  5 Mev/s, lower in a real capture) caps it first. Above that, record and
  analyse afterwards, which is your plan anyway.

### 7.3 Getting faster, in order of cost

1. **Fewer events:** a smaller area, the filters, and 2 ms slices where the
   speeds allow.
2. **The thread pool** for clustering (measured about 3x at 2 ms slices and
   high rates; little gain at 1 ms). No install.
3. **`CameraStreamSlicer`** for analysis decoding (52-73 against
   15.6-17.7 Mev/s), or riding RawPlayback's decode pass so the file is read
   once.
4. **Fixing the raw view's per-window cost** and using the flat-index
   `accumulate_events` (1.83-1.91x).
5. **A compiled helper,** only if the steps above are not enough: for example,
   if real recordings at your rates take too long to analyse, or live particles
   are needed above about 3-4 Mev/s.
   - Options: numba (needs your OK to install), or a small C extension. MSVC
     14.42 (Visual Studio Community 2022, 17.12.4) is already installed, but the
     Community licence limits use inside larger organisations.
   - Expected 5-10x on the per-event step (estimate, not measured);
     3-5 days.

---

## 8. Staged plan

All efforts are **estimates**: working days for one developer who knows the
codebase, tests included.

### Stage 1: velocity on recordings, in mm/s (about 19-27 days, estimate)

| # | Work | Estimate |
|---|---|---|
| 1 | Event-block reader factored out of `RawPlayback._read` (origin, EVT3 wrap, no index), with an opt-in callback so the raw view and velocity can share one read; an EVT3 check once an EVT3 file exists | 1-1.5 days |
| 2 | `VelocityTracker`: Particles and Objects presets, adjustable limits, field access, gaps and merge flags; synthetic `.raw` test recordings with known speeds written by `RAWEvt2EventFileWriter`; tests | 3-4 days |
| 3 | The **Analyse velocity** job in the raw view (worker thread, progress, cancel) and the saved files (section 4) | 1.5-2 days |
| 4 | Vector overlay in `ImageCanvas`: arrows and tails that follow the slider | 1.5-2 days |
| 5 | Velocity window (pop-out): histogram, colour legend after full processing, track table, direction view, counters; saved tracker set-ups | 2-3 days |
| 6 | `led_locate`: detector, survey, measured-frequency rule, ON-only mode, polarity weighting, tests | 2-3 days |
| 7 | `camera_calibration` store: rules from `camera_presets`, legacy keys, set-ups, revisions, import/copy, coordinate conversions (area, binning, decimation, mirroring) | 2-3 days |
| 8 | Stages A and B: clip recording with filters off and restored, detect-and-confirm overlay, lens list, distance form (C/CS mount, emitter height), ruler overlay, Check | 4-5 days |
| 9 | Run linkage (`<run>_calibration.json` at Start, the record pointer) and the px-to-mm layer (3.6), with the legend in mm/s | 2-3 days |

**What you see:**

- **After steps 1-5 (about 9-13 days):** open a recording, press **Analyse
  velocity**, watch the progress, then scrub through arrows and tails, with
  the histogram, legend and track table, in px/s.
- **After steps 6-9:** the same in mm/s, with each result labelled by its
  calibration stage and accuracy.

No camera is needed until the hardware bring-up. Until LED calibration
exists, a typed mm/px can stand in, marked "typed, unchecked".

### Stage 2: homography and live arrows (about 8-11 days, estimate)

| # | Work | Estimate |
|---|---|---|
| 1 | Stage C: multi-LED or moved-LED points, normalised DLT with residuals (numpy; matches OpenCV's `findHomography` to within 1.1e-5 mm with 4 points and 1.3e-3 mm with 9), plane grid overlay, saved LED-board definitions, rejection of reflections | 3-4 days |
| 2 | Live: the `EvkDevice.read` hook, the VelocityWorker (drop-oldest queue, counted drops, gaps), live arrows, a rolling live histogram, a rate-budget meter | 2-3 days |
| 3 | A paced, timestamped simulated event camera for testing the live path without an EVK4. File replay is unpaced, and `SyntheticDevice`'s event mode (`cameras.py:1918-1941`) has no per-event timestamps. `tests/fake_evk4.py` (now on `qt-migration`) is a fake EVK4 that hands out one timestamped buffer per 10 ms of wall clock; it could be fed particle events from the test generator | 1 day |
| 4 | Tracker set-ups linked to camera presets in the settings tabs | 1 day |
| 5 | Bring-up with a real EVK4 and LEDs: bias and filter tuning, the real centroid error, the first real velocity numbers | 1-2 days, when the camera is here |

**What you see:**

- Arrows on the **live** picture and a live histogram, within the limits in
  7.2.
- Live colours use a fixed speed range that you choose, clearly marked as
  fixed. The real legend still comes from processing the whole recording.
- Correct mm/s for cameras that look at the motion at an angle.

### Stage 3: lens distortion and speed (about 8-12 days for rows 1-3, estimate; rows 4-5 only if needed)

| # | Work | Estimate |
|---|---|---|
| 1 | Stage D: grid-board detection and ordering, multi-pose capture, `calibrateCamera`, automatic re-fit of C. cv2 would be an optional dependency (needs your OK; the council env has none, the `pylon` env has cv2 4.5.5 on Python 3.11.16, and `mechanicus` has cv2 4.13.0 on 3.12, which crashes on import unless its `Library\bin` is on PATH) | 5-8 days; +3-5 days for a numpy/scipy version if cv2 is not allowed |
| 2 | Thread pool for clustering | 1-2 days |
| 3 | `CameraStreamSlicer` for analysis, the raw view's per-window cost, flat-index `accumulate_events` | 2 days |
| 4 | Compiled helper (numba or C), only if still needed (needs your OK) | 3-5 days |
| 5 | Options by need: event-PIV mode for dense powder clouds; optimal track assignment for crossing tracks (`scipy.optimize.linear_sum_assignment`); two-line time of flight for directed streams; contrast-maximisation refinement; Kalman options; re-tuning on real recordings | 1-3 days each |

**What you see:** correct mm/s across wide-angle lenses, faster analysis of
heavy recordings, higher live rates, and a flow-field view for dense clouds if
wanted.

---

## 9. Open questions for you

1. **Lens and focal length.** Which lenses will you use (focal lengths), are
   they C- or CS-mount, and will focus be locked after calibrating? Is the
   stock EVK4 lens one of them?
2. **LED hardware.** Is there a microcontroller to drive the LEDs, and how
   many LEDs at once? An RP2040 board can run up to 8 independent frequencies;
   an Arduino Uno has 3 timers. The Uno's default PWM rate on most pins
   (490 Hz) is too low, while its 976.56 Hz pins are usable because Typhon
   measures the frequency. Can the LEDs be mounted flush on a flat board?
3. **Typical fields of view.** Roughly how many mm across, at what working
   distance? Does the motion stay mostly in one plane facing the camera, or
   does the camera look at an angle (for example through a viewport)? This
   decides whether stage B is enough or stage C is needed first.
4. **Accuracy needed.** Is a speed distribution enough, or must each particle
   be within ±X %? As a guide: stage B gives about 0.5-1.5 % square-on; stage C
   is needed at an angle; stage D matters for wide lenses (estimates).

---

## Where the numbers come from

- **Research write-ups and fact-checks** (session scratch, temporary):
  `velo_result/answer.md` and `check.json` (18 corrections, 17 additions);
  `ledcal_result/design.md`, `measurements.json` and `check.json`
  (17 corrections, 13 additions). Every correction and addition is applied
  above. Three of them were refined against the code or the raw results when
  this design was finished: the 50 Mev/s decode times (section 7.1), what
  sets `preset_changed` (3.10), and where Typhon's bias limits come from
  (3.4).
- **Scripts:** `velo/` (`typhon-integration/`: generator `gen.py`, trackers,
  `bench.py`, `rawbench.py`, `livebench.py`, `slicerbench.py`, `threads.py`;
  `algorithms/`: papers and benches; `factcheck/`: `haldecode.py`,
  `slicer_decode.py`, `accum.py`, `piv2.py`, `torchclus.py`) and `ledcal/`
  (`verify/`: `synth.py`, `led_detect.py`, `run_measure.py`, `extra.py`,
  `speed_err.py`, `on_only.py`, `cv2_check.py`; `factcheck/`: `math_check.py`,
  `geom_check.py`, `det_check.py`, `erc_check.py`, `hal_check.py`), under
  `C:/Users/apkun/AppData/Local/Temp/claude/C--Users-apkun-Downloads-Council-Demo-Council-Demo--claude-worktrees-priceless-vaughan-cc9023/486b5624-72b4-418a-b22e-cbd5562732ac/scratchpad/`.
- **OpenEB 5.2.0 source** at `C:/ceb/openeb` (9003b54): `antiflicker_filter.cpp`,
  `gen41_erc.cpp`, `event_trail_filter.cpp`, `i_event_trail_filter_module.h`,
  `imx636_bias_settings.h`, `i_ll_biases.cpp` (`I_LL_Biases::set` refuses a
  value outside `get_bias_range` unless the range check is bypassed).
- **Papers:** Willert & Klinner 2022 (event-based imaging velocimetry);
  Censi et al., IROS 2013 (active LED markers); STELLA, Exp Fluids 2026,
  67:110; Pfrommer 2022 (Frequency Cam); Zhang 2000 (camera calibration);
  Hartley & Zisserman (normalised DLT).
- **Vendor docs (not re-checkable offline):** Prophesee IMX636 product brief
  (4.86 µm pitch, latency, 25 % contrast threshold); EVK4 datasheet and
  1stVision page (220 µs, C/CS mount); Prophesee biases and event-signal-
  processing manuals; SDK module, analytics, CV and calibration API pages;
  SDK release notes and the March 2026 terms.
- **Typhon code read** (line numbers are at `qt-migration` 906bb26, which
  includes the settings tabs): `council_core/cameras.py`, `event_playback.py`,
  `camera_settings.py`, `camera_record.py`, `camera_presets.py`,
  `camera_categories.py`, `frame_camera.py`,
  `council_qt/widgets/capture_review.py`, `gui_emit_qt.py`,
  `examples/gui/typhon.gspec`, `tests/fake_evk4.py` and
  `docs/camera_quickstart.md`.
