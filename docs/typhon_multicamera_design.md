# Typhon with several cameras: design

Date: 12 October 2026 (research done 9-12 October 2026; revised the same day
after a review). Status: design only; nothing is built yet, and it awaits
your approval, like the velocity design it builds on
(`docs/typhon_velocity_design.md`). This doc lives on `qt-migration`; it
was written against commit 6560fca, and every Typhon file and line reference
below is to that commit. OpenEB references are
to OpenEB 5.2 at `C:/ceb/openeb` (commit 9003b54).

**What you asked for (9 October 2026, your words):** "for multi camera we
need a couple options for instance controlling multiple cameras with different
settings looking at different fields and then maybe multiple cameras looking
at the same field and then applying corrections so they line up with each
other (Think ORNLs Peregrine software)".

Your answers to the follow-up questions, which this design is built on:

- **Which cameras together:** Basler + EVK4; two or more Baslers; two or more
  EVK4s; and room for future kinds (for example thermal or infrared).
- **The shared field is a flat plane,** like Peregrine's build plate. Each
  camera is corrected onto the plane's own millimetre coordinates
  (perspective and lens distortion), so the pictures line up pixel for pixel.
- **Timing by a trigger cable:** one camera or a trigger box drives the
  others (for example the Basler's exposure signal into the EVK4's trigger
  input), so frames and events share one clock.
- **Output:** an approximate live overlay or side-by-side view while
  capturing, AND exact corrected images and exports after recording.
- **Earlier answers (velocity):** process mostly after recording, so nothing
  is lost during capture; real-world units through LED calibration, as the
  way in to homography and the other corrections.

This doc names two modes:

- **MODE 1:** several cameras, each with its own settings, each looking at
  its own field.
- **MODE 2:** several cameras looking at the same flat plane, corrected so
  their pictures line up.

**Read this first.** No physical camera has ever been attached to this
laptop. Everything below that was "measured" ran on synthetic data, on pylon's
camera emulator, or on made-up recordings played back through OpenEB. Every
number carries a label in brackets, as one of these kinds. A label at the
head of a table or list covers everything in it. Settings, defaults and file
line numbers are choices or places, not results, so they carry no label.

- **measured**: run or looked up on this laptop (HP OMEN 16, i7-14700HX,
  28 threads, 32 GB of RAM). "measured, synthetic" means it ran on made-up
  data, on pylon's emulator, or on a made-up `.raw` file.
- **computed**: arithmetic or a geometry model.
- **estimate**: judgement, not measured.
- **vendor docs**: from a maker's published pages or manuals (Basler,
  Prophesee, Sony, FLIR, lens makers); not re-checkable offline.
- **paper**: a published paper, an ORNL technical report, or a patent, named
  where it is used.
- **OpenEB source**: read from OpenEB 5.2's source code on this PC.
- **code**: read from Typhon at `qt-migration` 6560fca, given as file:line.

Four research reports fed this design: ORNL's Peregrine and the published
ways of calibrating event and frame cameras together; an audit of Typhon's
code for several cameras; the trigger-cable hardware; and lining cameras up
on a plane ("registration"). Each was checked by a separate fact-check that
re-ran most of the measurements with its own scripts. The few figures that no
fact-check re-ran, and whose first scripts are lost, are labelled "(measured
by the first report; its script is lost and no fact-check re-ran it)".
**Where a report and its fact-check disagree, this doc uses the
fact-check.** Where neither settled a point, the doc says "not settled" and
gives both figures. Some measurements came out differently on different days
(disk speed, some timings); both ranges are given.

Three plain truths up front:

1. **Pixel for pixel on the flat plate is a target, not yet a result, and
   only at the EVK4's pixel size.** Synthetic tests, given the exact lens
   numbers, reach about 0.1-0.4 EVK4 pixels (computed). A real lens
   calibration may add about 0.4-2 EVK4 pixels at the field edges (estimate,
   from the velocity design's 0.05-0.3 % for a real stage D; section
   4.5(b)). On the big Basler's own 0.03 mm pixels even the synthetic figure
   is about 1 pixel or more. Whether a real rig lines up pixel for pixel is
   decided by the cross-camera check after calibrating, which is the
   acceptance test (section 4.9). Anything above the plate (spatter in
   flight, a raised part) does not line up, by geometry (section 4.7).
2. **The full rig cannot run on this laptop.** The boA5320-150cm needs a
   CoaXPress frame grabber card in a desktop PCIe slot, and one camera at
   full speed sends about 2.4 GB/s, several times what this laptop's disk
   takes (section 7). MODE 1 can still be built and tested here with
   simulated and emulated cameras.
3. **Real accuracy will be worse than the synthetic figures,** and by how
   much is unknown until real cameras, a real LED board and a real trigger
   cable have been used.

---

## Terms used here

- **Rig**: all the cameras one Typhon window runs together, with their roles and timing.
- **Camera slot**: one camera inside the rig, with its own settings, picture, status and save folder.
- **Active camera**: the camera the settings tabs, exposure boxes and preset list currently act on.
- **Leader / follower**: on a trigger cable, the device whose signal times everyone (leader) and the devices that listen (followers). Prophesee calls EVK4s on its clock link master and slave.
- **Plate**: the shared flat plane (build plate, bench, target), with its own millimetre coordinates.
- **Plate mm**: a position on the plate in millimetres, from an origin and axes fixed by the calibration target.
- **Plate grid**: the common pixel grid laid over the plate that corrected pictures are resampled onto; its pitch is in mm per pixel.
- **Registration**: making pictures from different cameras land on the same plate positions.
- **Calibration stages A-D**: the velocity design's four steps (one LED; two LEDs; four or more LEDs for a homography; lens distortion). See section 4.2.
- **Homography**: a 3 x 3 mapping from a camera's picture to a flat plane seen at an angle; it removes perspective but not lens distortion.
- **Lens distortion**: straight lines bowing near the picture edges. Quoted here as the percentage position error at the sensor corner.
- **Intrinsics**: the lens-plus-sensor numbers (focal length in pixels, image centre, distortion terms).
- **Zhang's method**: the standard way to get intrinsics from a flat pattern seen at several angles (velocity stage D).
- **Plate fit**: a new, single-view way to get the distortion numbers from the plate's own points, fitted together with the homography (section 4.2).
- **Two-height calibration**: recording the target at two known heights, which gives each camera a full 3 x 4 camera matrix and lets any parallel plane be registered.
- **Parallax**: the shift between two cameras' views of a point that is above or below the calibrated plane.
- **Centroid noise (σ)**: the random error, in pixels, in finding the centre of an LED spot.
- **p99**: the 99th percentile; 99 % of the values are at or below it.
- **Exposure Active**: a Basler output signal that is on exactly while the sensor is exposing.
- **Trigger In**: the EVK4's input that stamps each signal edge into its own event stream on its 1 µs clock.
- **Trigger edge**: one rising or falling step of the trigger signal, recorded as an event with a time.
- **Sync In / Sync Out**: the EVK4's clock link, a 1 MHz pulse train from a master EVK4 that slave EVK4s count, so they share one clock.
- **Opto-coupled input**: an input that passes the signal through a tiny LED and light sensor, so the two sides share no electrical ground.
- **Open collector**: an output that can only pull a wire down to ground; something else (a pull-up resistor to a supply) must pull it up.
- **Pull-up**: a resistor from a signal wire to a supply voltage, so the wire reads "high" when nothing pulls it low.
- **CoaXPress (CXP-12)**: the cable standard the boA5320 uses; each link carries 12.5 Gbit/s; this camera uses two links for full speed.
- **Frame grabber**: the PCIe card in the PC that the CoaXPress cables plug into.
- **pylon / pypylon**: Basler's camera software and its Python module.
- **OpenEB**: Prophesee's open-source event-camera software (version 5.2 here); the paid Metavision SDK is not used.
- **HAL**: OpenEB's lowest software layer, which opens cameras and files and decodes events.
- **.raw / EVT2 / EVT3**: the EVK4's recording file and two of its encodings; a real EVK4 records EVT3.
- **Mev/s**: million events per second.
- **GIL**: Python's global lock; code that holds it stops other Python threads in the same program from running.
- **Nearest-neighbour / bilinear / area-average**: three ways to resample a picture onto a new grid (take the nearest pixel; blend the four nearest; average all pixels that fall in the new pixel).
- **Lookup table (LUT)**: a precomputed table giving, for each sensor pixel, where it lands on the plate grid.
- **Witness LED**: a blinking LED left in view during every recording, so each run checks its own calibration.
- **Rig record**: a new per-run JSON file (`<run>_rig.json`) describing every camera, its calibration, its role and the timing.
- **Pixel latency**: the delay between light changing at an EVK4 pixel and the event being stamped; longer in dim light.
- **Peregrine**: ORNL's software for checking powder-bed metal prints from layer images (section 8).
- **Normalised DLT**: the standard least-squares recipe for fitting a homography to matched points (as in the velocity design).
- **sha256**: a fingerprint of a file's bytes; any change to the content gives a different value (as in the velocity design).
- **raw_t_us**: a time in µs from the capture's origin, the first event the live view saw after the `.raw` began; the clock of the frames CSV (velocity section 4).
- **Plate-grid pixel centre**: where on the plate one pixel of the plate grid sits; this doc's rule is in section 4.13.
- **Orientation mark**: a feature of the LED board, seen by every kind of camera, that tells which way round the board is (section 4.3).
- **Placement id**: one name for one placement of the board on the plate, shared by every camera's calibration from that placement.
- **NUC (non-uniformity correction)**: a thermal camera's periodic flat-field correction, usually by closing a small shutter in front of the sensor.
- **Flat-field**: a picture of an evenly lit target, used to even out brightness differences across a camera's view.
- **Emissivity**: how strongly a surface gives off heat radiation at a given temperature: bare metal low, black paint high.
- **Aliasing**: false patterns (moiré, jagged edges) that appear when a fine picture is shrunk by picking samples instead of averaging them.
- **Mono8 / Mono10p / Mono12**: Basler pixel formats: 8 bits per pixel; 10 bits packed tightly with no spare bits; 12 bits stored in 16.
- **ppm**: parts per million. A clock 40 ppm fast gains 40 µs every second.
- **PCIe**: the slot for expansion cards inside a desktop PC.
- **Thunderbolt**: a laptop port that can carry PCIe to an external card box.
- **Applet**: on Basler's CoaXPress card, a loadable firmware set-up that decides how the card handles its cameras (for example "Dual" for two 2-link cameras).
- **GenTL**: the industry-standard driver interface through which camera software such as pylon can use another maker's frame grabber.
- **M8**: a small round screw-locking connector (8 mm thread), used for the Basler's I/O cable.
- **UTP**: unshielded twisted-pair cable, the kind used for wired networks.
- **TTL level**: a logic signal that is near 0 V when low and about 3.3-5 V when high.
- **SuperSpeed**: USB 3 speed (5 Gbit/s or more), as opposed to the older USB 2.
- **LineInverter**: a Basler setting that flips an I/O line's signal, so high becomes low.
- **Buffer board**: a small circuit (one transistor or a logic chip) between a camera output and other inputs, so that one output can drive several inputs at the right voltage without overloading.
- **DED**: directed energy deposition, metal 3D printing that blows powder or feeds wire into a melt pool; part tops rise as it builds (as in the velocity design).
- **CT**: X-ray computed tomography, 3D scans of a finished part.
- **HDF5**: a common file format for large scientific data sets.

---

## 1. Summary

### What will be built, and what you will see

**MODE 1: several cameras, each with its own settings and field.**

- One Typhon window runs 2-4 cameras at once, in any mix: Baslers, EVK4s,
  simulated cameras, and later other kinds. Today Typhon refuses a second
  camera (section 2.1).
- **The screen.** The active camera fills the main view, as today. The other
  cameras show as small live tiles, each with its name, frames per second and
  saved count. Click a tile and it becomes the active camera: the settings
  tabs, exposure and gain boxes, preset list and area box all follow it.
  There is also an equal grid view, and a live pop-out window per camera for
  a second monitor (section 2.4).
- **Settings and presets** stay per camera, as they already are in the
  presets file. A new "rig preset" says which preset goes on which camera.
- **One Start, one Stop** for every camera with its Record box ticked.
- **Run folders.** A one-camera run is saved exactly as today. A run with two
  or more cameras goes in `<capture folder>/<run>/<camera name>/`, and each
  camera folder looks exactly like today's capture folder, so the slider, the
  raw view, the classifiers, the timing scan and cropped export all work on
  it unchanged. A new `<run>_rig.json` at the run's top describes the whole
  rig (section 2.6).
- **Without a trigger cable,** the cameras' recordings are lined up only by
  the PC's clock, to milliseconds at best (section 3.9).
- **With a trigger cable** (stage M2), the Basler's exposure edges are stamped
  into the EVK4's own event stream. After Stop, Typhon pairs each saved Basler
  frame with its exposure on the EVK4's 1 µs clock, writes
  `<run>_triggers.csv`, and reports in the rig record whether every frame
  found its pair. Several EVK4s share one clock through Prophesee's sync link
  (section 3).

**MODE 2: several cameras on the same flat plate, corrected to line up.**

- **Calibrate once per set-up.** First each camera gets its lens numbers
  from its own stage D: a Basler from a printed checkerboard or dot grid held
  at several angles, an EVK4 from the blinking LED grid, with focus, aperture
  and filters locked (or, where a board cannot be tilted, from the plate fit).
  Then lay the LED board flat on the plate and record it with every camera at
  the same moment; that shared recording gives each camera's perspective onto
  plate millimetres. Typhon names each LED by its place on the board, shows
  each LED's leftover error as an arrow in mm and in that camera's pixels,
  and one cross-camera figure: how far apart the cameras put the same LED.
  A check recording with the board moved gives the same figure again, and
  that is the acceptance test for the rig's calibration. This reuses the
  velocity design's calibration stages C and D, kept per camera; there is no
  second calibration scheme (section 4, step by step in 4.14).
- **Live (approximate).** A side-by-side view, or one camera laid over
  another, both on a shared preview grid of the plate, refreshed 10-20 times
  a second and clearly labelled "approximate" (section 5).
- **After recording (exact).** "Export corrected" writes new files: every
  camera resampled onto one plate grid (by default at the finest camera's
  pixel size), events turned into pictures for each Basler exposure on the
  same grid, a stack per chosen time range, an 8-bit overlay picture to
  look at, a coverage map per camera, and a `<run>_registration.json` holding
  every number used. Exports per print layer need a layer signal that is not
  chosen yet (section 6.1, question 8). Frames whose pairing is uncertain are
  left out unless you ask for them (section 3.7). The recorded data is never
  touched (section 6).

### The honest limits

- **Accuracy is synthetic so far.** With the lens modelled from exact lens
  numbers, 25 LEDs and a centroid noise of 0.15 px, the worst pair of cameras
  disagrees by about 0.03 mm (p99) on a 120 x 80 mm field and about 0.09 mm
  on a 250 x 250 mm plate. That is about 1 pixel of the finest camera and
  about 0.25 pixel of the EVK4 (computed). A real stage D is expected to
  leave 0.05-0.3 % of scale (velocity 3.5, estimate): 0.4-2.2 EVK4 pixels at
  the EVK4's corner and 1.3-8 pixels at the edge of the big Basler's picture
  (computed, section 4.5(b)), so a real rig may not reach pixel for pixel
  even at the EVK4's size; the cross-camera check (4.9) says. Without the
  lens model it is 0.1-0.6 mm, and more LEDs do not help (computed, section
  4.5).
- **Only on the plate.** A point 1 mm above the plate is misregistered
  between cameras by about 0.2-0.9 mm, depending on how the cameras are
  angled (computed, section 4.7). Spatter 10 mm up is several millimetres
  off. A beam splitter, or a second calibration height, is the way round it.
- **Moving things.** The EVK4 reports a change up to about 100 µs after it
  happens in bright light (220 µs typical for the kit) and up to 1000 µs in
  dim light (vendor docs). At 1 m/s, 100-1000 µs is 0.1-1 mm of movement
  (computed), far more than the trigger cable's microsecond error. The
  average delay can be measured once and subtracted; its spread cannot. A
  still plate is not affected (section 3.8).
- **The live view is approximate** by design: it pairs pictures by when they
  reached the PC (milliseconds), and only the export after recording uses the
  trigger edges.
- **Matching Basler frames to trigger edges is by counting.** The boA5320
  sends no timestamp and no frame number that Typhon can read today (vendor
  docs). One frame lost without being counted shifts every pairing after it,
  and pylon's "skipped" counter stays at 0 in the mode Typhon records in
  (measured, synthetic). Today a failed grab is not counted at all (code
  reading, section 3.7). Section 3.7 says how the count is checked; frames
  whose pairing cannot be proved are marked "uncertain", never guessed.
- **The wiring must be checked with a meter before connecting.** The Basler's
  output and the EVK4's input are compatible on paper only with an external
  supply, a shared ground, and a Basler pull-up voltage no higher than that
  supply and no more than 2.0 V below it; Prophesee's own pages contradict
  each other about which trigger edge is which, and where the EVK4's ground
  goes is not settled (section 3.2).
- **Hardware.** The boost camera needs a desktop PC with a CoaXPress card;
  this laptop has no PCIe slot and no Thunderbolt port (measured). Even one
  boost at full speed outruns this laptop's disk several times over
  (section 7).
- **Thermal cameras** cannot see the LEDs, and some have no frame-trigger
  input at all, so they need a different target and a check of their I/O
  before purchase (sections 3.6 and 4.3).
- **Effort** (estimates, section 9): data-safety fixes 2.5-3.5 days; MODE 1
  about 24-37 days; the trigger cable about 11.5-16.5 days plus a bring-up
  day or two with the hardware; MODE 2 about 21-29 days, which needs the
  velocity design's LED finder, calibration store, clip recording and run
  linkage first. A writer fast enough for a full-speed boost is a separate,
  later piece (section 9).

---

## 2. How Typhon changes from one camera to many

### 2.1 Today: one camera from end to end

- All camera state is one module-level object, `_LIVE = _Live()`
  (`frame_camera.py:323`; the class docstring at `frame_camera.py:152-160`
  calls it "The one open camera").
- A second Connect is refused with "a camera is already open — disconnect
  first" (`frame_camera.py:430-432`; measured offscreen).
- One timer drives one live picture and one status line
  (`frame_camera.py:1367-1377`, `_tick` at 1231-1255). Settings windows are
  kept in one dictionary whose keys do not name a camera
  (`camera_settings_window.py:2327`), so opening camera B's window drops
  camera A's.
- An app is set up for one camera family: `camera_setup.py:41` lists
  "basler" and "prophesee", the wizard uses radio buttons
  (`camera_wizard.py:72-75`), and the scan hides the other family
  (`frame_camera.py:356-370`). The wizard's Basler scan is told about one
  open camera only (`frame_camera.py:1855-1863`); its comment notes that a
  second open of the same camera fails on real hardware but not on the
  emulator, so tests cannot catch that mistake.

**The lower layers are already close to ready** (all measured, synthetic):

- Two simulated cameras in one process, recording to separate folders, for
  10 s: a frame camera at 30.1 fps and an event camera at 50.2 windows/s,
  301/301 and 502/502 saved, 23-25 % of one core.
- Two emulated Baslers (`PYLON_CAMEMU=2`) opened together through Typhon's
  own `council_core.cameras`: 60-65 fps each, every frame saved, none
  skipped.
- Two `.raw` files opened together through two `EvkDevice`s, with the
  external-trigger events in each decoded alongside the pixel events.

### 2.2 The rig and its camera slots

- **A new module, `council_core/camera_rig.py`, with no Qt in it.** A `Rig`
  (one per app) holds the scan result, the set-up path, the one timer, the
  active slot, the shared run name, start-all and stop-all, add a camera,
  select a camera, and disconnect-all when the app quits. A `CameraSlot`
  (one per camera) holds that camera's device, session, folder, run, status,
  background job, lock and change listeners, plus an id, a name and a role
  (leader, follower or free).
- **Which fields move into the slot.** The code's own comments mark some of
  `_Live`'s fields as belonging to the app, not the camera: the reviewer,
  canvas and pop-outs (`frame_camera.py:176-180`), the listeners (228), the
  preset picker and settings tabs (233-235) and the drawn-box state
  (264-271). What `clear()` resets (290-320) is the per-connection set. The
  first research report's split did not match these comments; the slot gets
  the per-connection set, and the app-level fields stay on the `Rig`.
- **Each slot has its own background job and lock,** so a slow Basler stream
  restart does not grey out the EVK4's controls (today one job blocks
  everything: `frame_camera.py:2154-2158`, 2231-2233).
- **`frame_camera`'s functions stay, as thin wrappers.** Settings, presets,
  area, boxes and Connect act on the active slot; Start, Stop and shutdown
  act on the whole rig. With one camera the result is identical to today.
- **`frame_camera._LIVE` stays, as a compatibility view, not a plain slot.**
  The tests use `_LIVE` 259 times (234 as `frame_camera._LIVE` plus 25 as
  `fc._LIVE`), and 78 of those read or set app-level fields: `setup_path` 26,
  `reviewer` 16, `listeners` 14, `found` 10, `popouts` 3, `canvas` 2,
  `picker` 2, `tabs` 2, and `box_changed`, `box_seen` and `hooked_boxes` 3
  (measured with `git grep` at 6560fca). `frame_camera.py` itself uses
  `_LIVE` 226 times on 213 lines (measured with grep). If `_LIVE` simply
  pointed at slot 1, those 78 uses would read or set a field the slot no
  longer has. So `_LIVE` becomes a small object that forwards each name:
  per-connection names (`device`, `session`, `job`, `previewing`, and the
  rest of what `clear()` resets) go to the active slot, which is slot 1 in a
  one-camera app; app-level names (those the comments at
  `frame_camera.py:166-271` mark as about the app, plus the scan result
  `found`) go to the `Rig`. Reading and setting both forward. A test lists
  every field `_Live.__init__` creates and checks that each one resolves
  through the view, so a field added later cannot be missed. The tests then
  need no rewrite; if this view were dropped, the 78 app-level uses would
  have to be rewritten and M1 row 1 re-estimated.
- **The views already take an `api` object.** `SettingsTabs(api, book, ...)`
  (`settings_tabs.py:102-107`), the preset picker (`frame_camera.py:1476`)
  and the settings windows (`camera_settings_window.py:70-75`) are handed the
  module rather than importing it. A slot with the same function names can
  be handed to them instead. This is a reading of the code, not a test.
- **Camera names.** cam1-cam4 by default, renameable, short and safe in
  folder names (at most 16 characters, a-z, 0-9 and "-"). The name-to-camera
  map (backend|model|serial) is kept in a new project file
  `camera_rig.json`, with the same rules as `camera_presets.json`: JSON
  only, written whole to a temporary file then renamed, an OS lock while
  changing it. The same camera gets the same name next time.
- **Set-up.** `camera_setup.json` gains a "cameras" list of families. Its
  `FORMAT` stays 1 (`camera_setup.py:47`, checked at 380), so older apps are
  not asked their set-up questions again. The wizard gets check boxes; the
  scan shows cameras already open as "in use as cam2" and is told about
  every open slot.
- **New optional ports** in the wireframe: a camera chooser (combo box), an
  "Add camera" button, and `camera_folder` (the active camera's current run
  folder, for the folder tools).

### 2.3 Sessions, Start and Stop

- **One Start** for every camera with its Record box ticked, and one run name
  reserved for the whole rig (2.6). **One Stop.** Per-camera Start and Stop
  are offered only from a tile's menu, and are switched off while a rig with
  a trigger cable is recording (they would break the order below).
- **Order matters with a trigger cable** (section 3). A free-running leader
  Basler is already exposing during its live preview, and every exposure
  puts edges on the EVK4's Trigger In. Today Typhon stops a camera's preview
  only inside that camera's own Start (`frame_camera.py:597-598`) and starts
  it again for recording (`frame_camera.py:635-637`). If an EVK4's `.raw`
  opened (`frame_camera.py:625`) while the leader's preview still ran, the
  file would hold edges of preview frames that are never saved, and "the
  k-th edge is the k-th frame" would be off by an unknown count from the very
  first frame. So the rig starts in four steps:
  1. **Stop every Basler preview,** so that no camera is exposing and no edge
     is sent. A trigger box must also be quiet here: Typhon starts it in
     step 4, or it is gated off until then; a box that runs all the time
     defeats the quiet gap below.
  2. **Start the EVK4s:** the slaves, then the master on a sync link (3.3),
     each with Trigger In on and its `.raw` open. Then wait at least one EVT3
     time-high period (4.1 ms) plus a margin, for example 100 ms in all
     (estimate), so that each file begins with a stretch with no edges.
  3. **Arm the triggered Baslers** (trigger mode on, grabbing, waiting). A
     follower's status reads "armed, waiting for <leader>", not "NO
     PICTURE".
  4. **Start the leader** (the free-running Basler, or the trigger box).

  Pairing begins at the first edge after the quiet gap of step 2. Any edge
  before it is counted, reported, and ignored.
- **Stop, in order:** stop the leader; wait for its last frame plus a
  margin (until its grab loop has returned the last result, plus about
  100 ms for the EVK4's last buffers to reach the PC; estimate); stop the
  followers; then close each EVK4's `.raw`. Closed sooner, a `.raw` can miss
  the leader's last end edge while its frame still arrives. The EVK4
  streams themselves keep running for the preview, as today.
- **A test with fake devices** checks the order: a fake leader that exposes
  during its preview, a fake EVK4 that records every edge it is sent, and an
  assertion that no preview edge reaches the pairing and that the last
  frame's end edge is in the file.
- **If one EVK4 of a synced group disconnects,** the whole group must be
  reconnected. Its stream cannot be restarted (`cameras.py:1374-1384`), a
  fresh open forces it to standalone, and the sync mode can be set only
  while the stream is stopped (3.3). So Typhon stops the group's
  recordings, closes every EVK4 in the group, and reopens them in the
  slave-then-master order, saying so in the status line. A recording in
  progress ends at that point; its files up to the disconnect are kept.
- **An EVK4 stream is never restarted** (`cameras.py:1374-1384`). It starts
  with the live preview, and its `.raw` opens inside the running stream
  (`cameras.py:1399-1443`). A `.raw` started mid-stream can lose up to one
  EVT3 time-high period, 4.1 ms, at its start (the `start_raw` docstring,
  `cameras.py:1402-1409`; code). An exposure edge lost there would throw the
  frame-to-edge pairing off by one, so the EVK4 recordings are opened well
  before the leader's first exposure (step 2 above), and the edge count is
  checked after Stop (3.7).
- **Several EVK4s on the sync link** add their own rules: modes can only be
  set while not streaming, a slave does not stream until it receives the
  master's clock, and slaves start before the master (OpenEB source,
  vendor docs). That clashes with Typhon's live preview starting right after
  Connect, so in a synced rig the EVK4 previews start only at the rig's
  Start (section 3.3).
- **Software trigger.** Today Typhon refuses to start a camera that waits for
  a software trigger, because nothing would send one
  (`frame_camera.py:857-872`). In a rig the rig itself can fire it, which
  gives a no-cable "fan-out" mode and an emulator test: 40 software triggers
  at 20 Hz gave 40/40 frames on each of two emulated Baslers, arriving
  0.03-0.20 ms apart at the median (measured, synthetic; this is arrival on
  the PC, not exposure).

### 2.4 Views

- **Main view plus a strip of tiles (the default).** At the design size
  (1504 x 1016) the live view is 599 x 440, and a 16:9 picture drawn in it
  uses 599 x 336-341, leaving 73-78 rows (measured offscreen). Those rows are
  split above and below, because the canvas centres the picture
  (`gui_emit_qt.py:1724-1725`). So the strip needs the picture moved to the
  top of the view, a small canvas change, and then leaves 0-6 rows to spare:
  each tile's name, fps and saved count are drawn over the tile. At the
  smallest window (1400 x 820) only 11-16 rows are spare (measured), so the
  strip takes rows from the main view, leaving it about 457 x 257 (computed).
- **Tiles are cheap.** Live frames are handed to the canvas without a copy
  (`capture_review.py:31-35`). Drawing a full 5328 x 3040 frame into a
  128 x 72 tile took 0.06-0.2 ms, and taking every 8th pixel 0.22 ms
  (measured, offscreen). Converting 16-bit frames to 8-bit for the tiles was
  not measured.
- **Grid view:** two cameras stacked at 368 x 207 each, or four in a 2 x 2 at
  299 x 168 each, at the design size (computed).
- **Pop-out per camera,** for a second monitor; window titles name the
  camera; the window dictionary's keys include the camera name
  (`<cam>:window`, `<cam>:category:<group>`).
- **Overlay view** for MODE 2: section 5.
- **Status:** each tile has its own; the status line gives a summary for the
  rig, including "disk is behind: N GB waiting" (2.8).

### 2.5 Settings tabs, settings windows and presets

- One settings notebook, pointed at the active camera; its note names the
  camera.
- **Presets need no change.** They are already kept per camera
  (backend|model|serial, `camera_presets.py:121-140`) and already protected
  against two windows on one project (`camera_presets.py:49-55`). The new
  piece is a **rig preset** in `camera_rig.json`: which preset goes on which
  camera.
- **New settings rows** for the trigger cable (section 3.10): a Basler
  "Lines" group (LineSelector, LineMode, LineSource, LineInverter), which
  Typhon lacks today (`camera_settings.py:743-816` has trigger rows at
  792-813 but no Line rows); and EVK4 "Trigger in" and "Camera sync" rows
  (the EVK4 facility list at `camera_settings.py:1168` has neither).

### 2.6 Run folders and records

**A data-loss bug to fix first.** Two cameras saving under the same run name
into one folder silently overwrite each other's PNGs (measured, synthetic,
3 runs): 184-185 frames were reported saved, but only 92-93 PNG files were
on disk, the frames CSV held duplicated rows, and in 2 of the 3 runs its
header was not on line 1, which breaks every reader that takes the first line
as the header (`event_playback.py:393-434`). The causes (code):

- `_unique_run` only looks for existing file names (`frame_camera.py:1581-1603`),
  so two calls before anything is written return the same name (measured);
- `fast_png` opens files with "wb", which replaces (`fast_png.py:213`), and
  the Pillow fallback beside it (`capture.py:177-186`) replaces too;
- the frames CSV is opened for appending (`capture.py:305`), so a second
  writer adds its rows to the first one's file instead of failing.

The first cause is the trigger; the other two are the root: as long as a
frame write replaces whatever is there, any future name clash, from any
cause, loses frames silently. Stage M0 fixes both (2.6 "Reserving a run").

Today this needs two Typhon windows saving into one folder and pressing Start
within a short window, and only frames whose numbers overlap collide (the
first report overstated this risk). A rig saving several cameras into one
folder would hit it every time. The Council's own Capture tab can also
overwrite with one camera: it saves with the plain "frame" name
(`council_qt/tabs/capture.py:336`) and frame numbers restart at every Connect
(`cameras.py:656`) (code reading, not measured). Under the rule "never
overwrite user data" these are fixed in stage M0 (section 9).

**Layout.**

| Run | Where | Files |
|---|---|---|
| One camera | `<capture folder>/` as today | Unchanged |
| Two or more cameras | `<capture folder>/<run>/<cam>/` | Unchanged names: `<run>_frame_NNNNNN.png`, `<run>_frames.csv`, `<run>_events.raw`, `<run>_camera.json`, `<run>_calibration.json`, plus `<run>_triggers.csv` beside an EVK4's `.raw` when it records trigger edges (each edge's number, polarity, channel, and time as `raw_t_us` of that EVK4, see below) |
| | `<capture folder>/<run>/` | `<run>_rig.json` (written once at Start), `<run>_rig_end.json` (written once at Stop: counts, pairing outcome, and each EVK4's clock origin), `<run>_pairs.csv` (after pairing: frame file, camera, edge numbers, exposure start and end as `raw_t_us` of the EVK4 that recorded the edges, and a status: paired, uncertain or unpaired; 3.7) |

- **Which clock the trigger times are on.** Every time in
  `<run>_triggers.csv` and `<run>_pairs.csv` is a `raw_t_us` of the EVK4
  that recorded the edge: µs from that camera's capture origin, the same
  clock as `raw_t_us` in its own frames CSV (velocity section 4). Times are
  decoded from the `.raw` with `RawPlayback`'s origin and wrap rule: the
  file is read on the camera's clock, and an EVT3 file started after the
  24-bit clock wrapped decodes a whole number of 16.78 s behind it, which is
  put back (`event_playback.py:33-44`, 288-293; `TIME_WRAP_US` at line 83).
  An edge that came before the origin has a negative time. Each EVK4 has its
  own origin, set from the first event its live view saw after its `.raw`
  began (`cameras.py:1647-1649`), and its own wrap count, so several EVK4s
  on one sync clock still do not share a `raw_t_us` zero. So
  `<run>_rig_end.json` stores each EVK4's origin on its camera clock,
  `origin_us = timestamp_us − raw_t_us`, worked out exactly as
  `event_playback.raw_origin` does (`event_playback.py:387-398`). For EVK4s
  on one sync link, which count the same clock, a slave's time goes onto the
  master's as `raw_t_master = raw_t_slave + origin_slave − origin_master`
  (computed from these definitions; not tried on hardware).
- **Reserving a run.** A rig run is reserved by creating the `<run>` folder,
  which fails if it already exists; Typhon then tries `_2`, `_3`.
  `_unique_run` also treats run folders as taken. A one-camera run stays
  flat, so no folder reserves it: it is reserved by creating
  `<run>_frames.csv` with exclusive creation (Python's `"x"` mode, which
  fails if the file exists) and writing its header at once; on failure
  Typhon tries `_2`, `_3`. The recorder then opens that file for appending
  as today and, finding it there, writes no second header.
- **Frames are written with exclusive creation** (`"xb"`) in `fast_png` and
  in the Pillow fallback (by handing Pillow a file opened with `"xb"`), and
  the same in the Capture tab, so a name clash from any cause fails loudly
  and is reported as an unsaved frame, instead of replacing a frame already
  on disk. Tests cover both the reservation and a forced clash.
- **Path length is checked before Start.** The rig layout adds
  `<run>/<cam>/` to every path the capture writes: about 33 characters with
  a 15-character run name and a 16-character camera name (computed).
  Windows' 260-character limit applies on this PC (velocity section 4,
  measured), and a write that fails mid-capture loses frames. So before
  anything is recorded, Typhon works out, for every camera folder, the
  longest path the run will write (a `_N` run suffix, a frame number with a
  spare digit, the `.raw`, the CSVs and records, and any temporary name),
  and refuses Start with a clear message naming the camera and the length
  if any would pass 260 characters.
- **Separate drives.** One run folder puts every camera on one disk. A camera
  may instead be given its own save folder on another drive; the rig record
  lists every camera's absolute folder.
- **The frames CSV gains columns at the end only,** which the readers accept
  (they read by column name, `event_playback.py:393-434`): `host_us`, the
  arrival time on the PC's 100 ns clock (`perf_counter`; its resolution
  measured); `grab_failures`, a running count of the camera's failed grabs
  so far (3.7); and `camera_frame_id` when the camera or frame grabber gives
  one (3.7).
- **The camera record** (`<run>_camera.json`) stays as it is, one per camera
  folder, and gains additive keys without raising its `format_version`,
  because `camera_record.read()` ignores any newer version
  (`camera_record.py:209-228`): a "rig" pointer, and for an EVK4 the sync
  role and Trigger In state that Typhon itself set (3.3).
- **The rig record** (`<run>_rig.json`, format "typhon-rig-record" version 1)
  lists every camera: name, role, identity, folder, record file, active
  calibration id and sha256, plate id and placement id (section 4); the
  timing mode (none, software, cable), the leader, which Basler line carries
  Exposure Active, which EVK4 records it, the EVK4 master and slaves with the
  sync role and Trigger In state Typhon set on each; the start order. If
  the calibrations do not all name the same plate placement, the mismatch
  is recorded here (4.1). It is written once, never over anything, with the
  same rename rule as the camera record (`camera_record.py:39-46`). What is
  only known at Stop goes in `<run>_rig_end.json`: start and stop times;
  frame, grab-attempt, grab-failure and edge counts per camera; edges
  ignored before the quiet gap (2.3); each EVK4's `origin_us` (above);
  mismatches and the number of frames marked uncertain (3.7); and any
  measured latency offset used.

### 2.7 The slider, the raw view and the classifiers

- They look for `<run>_frame_` files directly inside one folder, not below it
  (`capture_review.py:96`, 114-126; `frame_roi.py:137`;
  `frame_classes.py:2489`; `frame_timing.py:206` searches below only when
  asked). Keeping today's names inside each camera folder keeps them all
  working; pointed at the run's top folder they would find nothing.
- `start()`'s docstring promises that the capture folder "is the SAME folder
  the slider shows" (`frame_camera.py:516`). In a rig the slider, Classify
  all, Scan for bad timings and Save cropped frames are re-pointed at the
  active camera's folder at every Start and every camera switch.
- Mark and Predict already work in sub-folders: the reviewer stores the
  frame's path relative to its folder (`capture_review.py:907-921`) and the
  classifier joins it back (`frame_classes.py:1116-1128`).
- The raw view ignores trigger events today: pictures from a file with
  200 trigger edges and from the same file without them were identical
  (measured, synthetic; it registers only the pixel-event callback,
  `event_playback.py:258-262`).
- Classifiers work on one camera folder at a time and need no change. A model
  trained on one camera's pictures should not be assumed to suit another
  camera (estimate).

### 2.8 Sharing the PC between cameras

- **Memory for the save queue multiplies.** Each recording session may queue
  a quarter of RAM (`capture.py:385-401`; 7.93 GiB on this laptop, computed
  from the measured RAM) and starts up to 8 writer threads
  (`capture.py:412`). Four cameras could queue all the RAM. The rig gets one
  budget and one writer pool for the whole program.
- **Basler buffers.** `POOL_BYTES` (2 GiB) is a cap on frame arrays kept for
  reuse, not memory set aside. `RECORD_BUFFER_BYTES` (1 GiB) sets pylon's
  buffer count to between 10 and 200 (`cameras.py:643-650`, 902-903): about
  66 buffers, 1.07 GB, for a 16.2 MB frame, taken when grabbing starts
  (computed). Both are sized by the number of open cameras.
- **The disk is the first limit.** One simulated full-frame camera at 20 fps
  (324 MB/s, computed) kept up. Two (648 MB/s) did not: memory peaked at
  1.9-2.6 GB, and saving finished 10.5-12.9 s after Stop in the fact-check's
  runs (2.73 s in the first report's), at 184-420 MB/s effective (measured,
  synthetic; other programs may have been using the disk).
- **The PNG writer has a ceiling** of 266 Mono8 or 141 16-bit full frames a
  second on 8 threads (`capture.py:405-408`, measured earlier), shared by
  every camera.
- **CPU:** two simulated 5328 x 3040 cameras at 20 fps each, recording, took
  222-228 % of one core (measured, synthetic, with easily compressed frames).
- **Two busy EVK4s in one program slow each other.** Through `EvkDevice`, one
  file played at 4.5-5.5 Mev/s, two at once at 2.6 Mev/s each (measured,
  synthetic). The cause is that OpenEB's decoding, called with a Python
  callback per buffer, holds the GIL: two decode threads together reached
  10.8-11.3 Mev/s against 17-20.5 Mev/s for one, while two separate programs
  reached 17-21 Mev/s each (measured, synthetic). OpenEB's own C++
  `CameraStreamSlicer` ran two streams in one program at 29-33 Mev/s each
  (measured, synthetic). **Not settled:** one program per EVK4, or the C++
  slicer in one program. The slicer's Python wrapper has no start/stop or
  trigger callbacks (OpenEB source), so a benchmark in stage M1 decides.
  Either way, every `.raw` is opened through Typhon's own `start_raw`, never
  by OpenEB's recorder (3.3).
- **No pickle between programs.** If one program per EVK4 is chosen, Python's
  `multiprocessing` must not be used to talk to it: its Queue and Pipe
  pickle everything sent through them, and on Windows starting a `Process`
  also pickles its target and arguments. The project rule is "no pickle".
  Instead, Typhon starts each helper with `subprocess`, sends control
  messages as JSON lines over its stdin and stdout (or a socket bound to
  127.0.0.1 only, never another address), and passes data through named
  shared memory or the `.raw` file itself. A test checks that the helper
  module and its caller import neither `pickle` nor `multiprocessing`'s
  queues, pipes, pools or processes, and that a helper run works with
  `pickle` blocked.
- **A budget check before Start.** Each camera's area, frame rate and pixel
  format give its data rate; Typhon adds them up and warns when the total
  passes the disk or writer limits measured on that PC. The same check
  refuses Start if any path the run will write would pass Windows'
  260-character limit (2.6, "Path length").

### 2.9 Backward compatibility

- Function signatures and the required ports (`live_view`, `capture_status`;
  `frame_camera.py:143`) stay the same. A one-camera project builds, captures
  and names files exactly as today; Barbie goes through the same wrappers.
- Old runs (flat folders) are read as before. Rig runs only add folders and
  files.
- `camera_setup.json` keeps `FORMAT` 1; `camera_presets.json` is unchanged;
  `camera_rig.json` is new.
- `app.py` is written once and `handlers.py` only grows
  (`gui_emit_qt.py:1480-1488`; `designer_examples.py:718`). "Update from
  example" adds the new optional ports and links and leaves existing links
  alone, so no "edited handler" warnings appear (`designer_examples.py:643-667`;
  read from the code, not exercised).
- A project that never adds a second camera never shows any rig controls.

### 2.10 Testing without hardware

- **N simulated cameras.** The simulated backend always lists one frame and
  one event camera (`cameras.py:1737-1743`); it gains a setting (for example
  `COUNCIL_SIM_CAMERAS=frame,frame,event`) with unique names and serials.
- **A consistent simulated rig:** one scene seen by every simulated camera
  through known mappings, a shared clock, and trigger edges fed to the
  simulated event camera, so alignment and pairing have a known right answer
  (timing: M2 row 5). For MODE 2 the scene is the LED board itself, drawn
  into each simulated camera through a known lens and pose: as blinking
  events for an EVK4, and as noisy LEDs-on and LEDs-off frames for a Basler,
  so the calibration's accuracy can be tested offline (M3 row 8).
- **Fault injection:** a fake Basler that returns a failed grab on a chosen
  frame (3.7), and a hand-made EVT3 file whose times start after the 24-bit
  clock has wrapped (2.6), each with a known right answer.
- **The fake EVK4** (`tests/fake_evk4.py`) has a fixed serial (line 31) and no
  trigger, sync or trigger-decoder fakes; it gains a serial setting, several
  instances, and those fakes.
- **pylon's emulator** with `PYLON_CAMEMU=2` to 4: streaming together, the
  Line settings (the emulator has LineSelector, LineMode, LineInverter and a
  LineSource list with ExposureActive; measured), and software-trigger
  fan-out. It cannot test clocks: it returns timestamp 0 and an invalid
  frame id (2^64−1) (measured).
- **Replay rigs** from `.raw` files written with OpenEB's writer, opened with
  indexing off and time-shifting off. A default open wrote a `.tmp_index`
  beside the file and shifted its clock; a writer given a short latency limit
  crashed the whole program (both measured). `event_playback.py:241-249`
  already sets both options.
- **EVT3 trigger files** can be made by hand: 600 hand-encoded exposure edges
  plus pixel events decoded exactly through OpenEB 5.2 (measured, synthetic).
- **Qt tests offscreen** on a Typhon built in a temporary vault with 2-4
  simulated cameras: tiles, switching the active camera, unique run folders,
  a 4-camera tick under 33 ms, no overlapping widgets at both window sizes,
  and a regression test that repeats the overwrite case above and proves
  nothing is lost.

---

## 3. Timing: one clock for every camera

### 3.1 The clocks that exist

- **The EVK4** stamps every event on its own clock in 1 µs steps, and its
  Trigger In stamps each edge of an outside signal on the same clock
  (vendor docs; OpenEB source).
- **The boA5320 has no clock Typhon can read.** Basler lists "Timestamp
  feature not supported" and "Data Chunks feature not supported" for all
  34 boost (boA) models (vendor docs), so a frame carries no camera time and
  no frame number. Typhon's frame number is counted on the PC
  (`cameras.py:954`). The field it fills, `timestamp_us`, actually holds
  camera clock ticks, not µs, and is 0 on a boost (`cameras.py:944-948`, 962);
  for a Basler family that does stamp frames (1 ns ticks, vendor docs) the
  name would be wrong by a factor of 1000.
- **The PC's clock** is fine-grained: `perf_counter` resolves 100 ns, while
  Python's `monotonic` steps every 15.6 ms (both measured). The problem is
  the delay between an exposure and its frame reaching Python, through the
  cable, the grabber and the driver: 16-20 ms from software trigger to
  arrival on the emulator (measured, synthetic), with jitter. So the PC clock
  can put frames in order but cannot line them up to microseconds.

**Hence the plan you chose:** put the leader's exposure edges into the EVK4,
so frames and events share the EVK4's clock, and join further EVK4s through
Prophesee's sync link. Published rigs do this in two ways (paper):

- TimeLens++ and HDR-ERGB send the frame camera's own exposure start and end
  into the event camera, as you plan;
- DSEC and eWand use a microcontroller as leader that triggers every camera;
  eWand joins its second EVK4 by the sync link.

Prophesee's own advice for a frame camera with an event camera is the frame
camera as leader, its frame signal into the EVK4 Trigger In (vendor docs).

### 3.2 Basler + EVK4: the wiring

**Basler side (boA5320, M8 6-pin connector; vendor docs):**

- Line 1 (pin 2) is an opto-coupled input: logic 1 above 2.2 V, more than
  5 mA, safe from 0 to 24 V. Pin 3 is its ground.
- Lines 2 and 3 (pins 4 and 5) are general-purpose lines. As outputs they are
  open collector, with an internal pull-up of about 650 Ω to a voltage Basler
  does not state, 3.3-24 V, at most 50 mA, about 0.4 V when on. As inputs
  they read logic 1 above 2.0 V. Their ground is pin 6 (power and GPIO
  ground), not pin 3.
- The output can carry Exposure Active, Flash Window, Frame Trigger Wait,
  Timer 1/2 Active, User Output 1-3 and others.

**EVK4 side (Hirose IX80G-B-10P socket; mating plug IX30G-B-10S-CV(7.0) or
IX31G-B-10S-CV(7.0); vendor docs):**

- Pins 5 (TRIG_IN_P) and 6 (TRIG_IN_N) are an opto-coupled input. It reads
  low at 0-2 V and high at 2.5-5.5 V (3.3 V typical), and draws 2.7-5.4 mA
  at 3.3 V.
- Pins 1/2 are Sync Out (open collector, sinks 100 mA), pins 3/4 Sync In
  (opto-coupled), pins 7-10 unused. No ground pin is listed.

**The connection, in words** (the plain scheme from the research; an
estimate, never tried):

1. A separate, small supply of voltage Vs (between 3.3 and about 5.9 V; see
   the checks below). Its + goes to EVK4 pin 5 (TRIG_IN_P).
2. EVK4 pin 6 (TRIG_IN_N) goes to the Basler's Line 3 output (M8 pin 5).
3. The supply's ground goes to the Basler's GPIO ground (M8 pin 6).
4. When the Basler output switches on (during exposure), it pulls pin 6 to
   ground, current flows through the EVK4's opto input, and the EVK4 records
   an edge. Use a short twisted pair.

**The same, drawn** (Vpu is the Basler's internal pull-up voltage, which
Basler does not state):

```
  Separate supply          EVK4, Hirose IX socket              Basler boA5320, M8 6-pin
  Vs = 3.3 to ~5.9 V       (opto-coupled trigger input)

  (+) -------------------- pin 5  TRIG_IN_P
                              |
                           [opto LED inside the EVK4]
                              |
                           pin 6  TRIG_IN_N ------------------ pin 5  Line 3 output
                                                                  |  (open collector;
                                                                  |   ~650 ohm inside
                                                                  |   up to Vpu)
                                                                  |
                                                               [switch to ground,
                                                                closed during exposure]
                                                                  |
  (-) -------------------------------------------------------- pin 6  GPIO ground

  Output OFF: the EVK4 input sees at most Vs - Vpu  -> must read LOW  (<= 2.0 V)
  Output ON : the EVK4 input sees about  Vs - 0.4 V -> must read HIGH (2.5-5.5 V)
```

The trigger input has both its pins on the connector, so its own current
loop closes through the separate supply and needs no EVK4 ground (a reading
of the pin table; unverified). Where the EVK4's ground is, if one is
needed, is an open point (3.3).

**Safety and the checks before connecting:**

- Never more than 5.5 V on the EVK4 input; never a 24 V PLC signal; at most
  50 mA through the Basler output (vendor docs).
- **Measure the Basler's pull-up voltage Vpu first,** on Line 3 against M8
  pin 6, with the output off and nothing connected. Then require all of
  these (computed from the vendor thresholds above):
  - **Vpu ≤ Vs.** Otherwise the scheme above would drive the EVK4's opto
    input backwards while the output is off.
  - **Vs − Vpu ≤ 2.0 V.** While the Basler output is off, the EVK4 input
    sees up to Vs − Vpu, and it reads low only below 2.0 V (high above
    2.5 V; vendor docs). With a 5 V supply, Vpu must therefore be at least
    3.0 V. Otherwise the input sits high, or in the undefined band between
    2.0 and 2.5 V, and no clean edges are recorded.
  - **Vs between 3.3 V and about 5.9 V.** Basler says its GPIO output is
    unreliable below 3.3 V (vendor docs, re-read on 12 October 2026); with
    the output on (about 0.4 V), the EVK4 sees about Vs − 0.4 V, which must
    stay at or below 5.5 V.

  If any of these fails, use the buffer board below instead.
- The pull-up alone cannot drive the EVK4: if it went to 3.3 V it would give
  only (3.3 − 2.5) / 650 = 1.2 mA at the 2.5 V threshold, below the 2.7 mA
  minimum (computed; the rail voltage is an assumption).
- **Meter (or oscilloscope) check in both states, before the first
  recording.** With everything connected and `LineInverter` off, set Line 3
  to a user output (LineSource = UserOutput1) and switch it by hand from the
  settings: with the output off, the voltage across EVK4 pins 5 and 6 must
  read at most 2.0 V; with it on, between 2.5 and 5.5 V. Only then set
  LineSource = ExposureActive and look at the pulses on a scope, or at the
  edges Typhon counts.
- **The safe route is a one-transistor or logic-buffer board** between the
  two, built and checked with a meter or oscilloscope by someone comfortable
  with it (estimate). Neither vendor documents this exact pairing.

**Settings:**

- Basler: LineSelector = Line3, LineMode = Output, LineSource =
  ExposureActive, LineInverter as found in the check below. These survive
  Typhon's open, which resets only the trigger mode (pylon's
  AcquireContinuousConfiguration, `cameras.py:625-636`; measured on the
  emulator).
- EVK4: Trigger In channel Main switched on before the stream starts. It is
  off whenever the camera opens, and the EVK4 has only that one channel
  (OpenEB source: `imx636_tz_trigger_event.cpp:19-30`,
  `gen41_tz_trigger_event.cpp:19-23`).
- Load: 149.81 frames/s gives 299.6 edges/s (computed), far inside the
  input's limits of 500 ns minimum pulse, 1 µs minimum period and up to
  1 MHz (vendor docs).

**Which edge is which: not settled by the vendor.** Prophesee's timing page
says the EVK4 inverts its trigger input (an ON event on the falling edge);
its synchronisation page says a frame camera's rising edge produces ON
(vendor docs). The open-collector stage may invert too. Typhon therefore
tells exposure start from end by the pulse width, which equals the exposure
time. That fails when the exposure is close to half the frame period, so
either avoid that setting or establish the polarity once with a deliberately
short exposure, and record the answer in the rig record.

### 3.3 Two or more EVK4s: the sync link

- **Wiring (vendor docs):** the master's Sync Out (pins 1/2, open collector)
  to each slave's Sync In (pins 3/4, opto-coupled), carrying a 1 MHz pulse
  train. Prophesee says ground must be connected, that with several slaves
  the electrical loads must be checked and Sync Out may need boosting, and
  (in a forum post, undated) that chaining from slave to slave is not
  supported natively. Two EVK4s on one link are documented; three or more
  probably need a small fan-out buffer (estimate).
- **Where the ground goes is not settled.** Prophesee says ground must be
  connected, but the connector's pin table lists no ground pin (vendor
  docs). It may be the connector shell or the cable shield; that is
  unverified. Check the shell and shield with a meter against the USB
  connector's shell, or ask Prophesee, before building the cable (question 4).
- **The cable is home-made.** Prophesee's pages name no sync-cable product;
  the manual describes ordinary UTP cable with Hirose IX plugs, using 4 of
  its 8 cores (vendor docs). A slave that must also record a trigger needs
  pins 3-6, so a 6-core harness, not a 4-core sync cable.
- **Software rules (OpenEB source):** the mode (master, slave, standalone)
  can be set only while the stream is stopped
  (`tz_camera_synchronization.cpp:30-64`), and every camera is forced to
  standalone when opened (`tz_device_builder.cpp:160-163`). Slaves start
  before the master and stop before it
  (`synced_camera_streams_slicer.cpp:159-165`, 205-208). Slaves send events
  stamped 0 until the master starts; those are dropped.
- **What OpenEB already offers in Python** (OpenEB source; measured,
  synthetic): `SyncedCameraSystemBuilder` opens a master and slaves by serial
  number, sets their modes and records one `.raw` per camera; an offline mode
  replays a master and slave pair in step (100 slices, 200,000 events each,
  200 master trigger edges on a made-up pair). Its limits: each slice carries
  the master's trigger edges only, and the Python wrapper has no start/stop
  or trigger callbacks.
- **If the builder is reused, its own recording stays off.** It records
  through OpenEB's own recorder, as `recording_<serial>_<date>.raw`, with the
  date to the second (`synced_camera_system_factory.cpp:21-88`; OpenEB
  source). OpenEB's raw logging silently truncates an existing file
  (1,700,147 bytes cut to 208, measured; `cameras.py:1411-1412`), which is
  why Typhon's `start_raw` refuses an existing name. So every `.raw` is
  opened through Typhon's `start_raw`, with Typhon's names and its
  no-overwrite check, never by the builder.
- **Recording each EVK4's role.** The Python bindings cannot read back the
  sync mode: `get_mode()` is bound (`i_camera_synchronization_python.cpp:31`)
  but returns a `SyncMode` type that is not exposed to Python (OpenEB
  source). OpenEB's camera-settings JSON (`Camera.save()`) does include the
  sync mode and Trigger In state (`camera_serialization.cpp:93-109`,
  269-282), but in Python a `Camera` can only be made by
  `from_first_available`, `from_serial` or `from_file` (`camera_python.cpp:55-67`;
  OpenEB source). Typhon opens EVK4s through the HAL (`EvkDevice`, opened with
  `DeviceDiscovery.open` at `cameras.py:1163`), so it cannot call
  `Camera.save()` on the device it already has open without reopening the
  camera through the SDK (unverified whether any other route exists). So
  Typhon records the sync role and the Trigger In state that **it itself
  set**, as additive keys in each `<run>_camera.json` and in the rig record
  (2.6). `Camera.save()` is kept only if `EvkDevice` is ever moved onto the
  SDK's `Camera`.
- **USB:** at most 1.6 Gbps (200 MB/s) per EVK4 (vendor docs, computed).
  This laptop has 4 user SuperSpeed ports on one Intel controller (measured).
  Whether two EVK4s keep up on two separate ports of that one controller is
  **not settled**: the trigger-hardware research judged it fine, the
  Typhon-audit fact-check borderline (both estimates). Not behind one hub
  (estimate). Two live EVK4s were never tried (only file playback).
- **Accuracy:** no drift between EVK4s, because they count one clock. The
  fixed offset between master and slave is not published; microseconds are
  expected (estimate); check it with one LED flash seen by both.

### 3.4 Two or more Baslers

- **One trigger source for all of them,** in a star: a trigger box (a
  microcontroller; the velocity design's LED driver could double as it,
  velocity question 2), the CoaXPress card itself, or **one leader Basler
  driving the others**, as you described ("one camera drives the others").
- **The leader-Basler star.** The leader runs free; its Timer 1 Active or
  Exposure Active output goes through a buffer board to every follower's
  GPIO input (LineMode Input) and to the EVK4's Trigger In, one buffer
  output per input. A follower starts exposing later than the leader, by its
  own trigger-to-exposure delay: up to 21,459 µs on Basler's ace 2 examples
  (vendor docs); not published for the boost. So each follower's own
  Exposure Active should be recorded too, on its own synced EVK4 (below), or
  its delay measured once at bring-up and stored in the rig record.
- **Never a chain** (leader into follower 1, follower 1's output into
  follower 2, and so on). Each hop adds that camera's own trigger-to-exposure
  delay and jitter, so the delays add up from camera to camera, and one
  camera that stops (or misses a trigger) silences every camera after it.
- **The CoaXPress card** (Basler CXP-12 Interface Card 4C; vendor docs): its
  front input (opto, 4.5-28 V, 40-80 ns delay, at least 200 ns pulses) can be
  forwarded to every camera as CxpTrigger0; it has an internal trigger
  generator (`AreaTriggerMode = Generator`); and two TTL outputs (3.8-5 V, up
  to 18 mA) that suit the EVK4's input electrically. Whether the generator's
  pulse can be sent out on a front output, to drive the EVK4 too, is not
  documented (unverified). Its "Dual" applet takes two 2-link cameras:
  4.85 GB/s against the card's 6.52 GB/s (vendor docs, computed).
- **Use a Basler GPIO line as the trigger input** (LineMode Input): under
  1 µs of delay, against 4.5-28 µs for the opto Line 1, on Basler's published
  example camera (the ace USB; boost figures are not published; vendor docs).
  Keep the opto input for where isolation is needed; it needs more than 5 mA
  per camera, so drive it through a buffer, one output per camera.
- **Exposure starts some time after the trigger,** reported per camera and
  setting in `BslExposureStartDelay` (vendor docs; whether a boost has that
  parameter is unverified). On ace 2 cameras it ranges from 14 µs to
  21,459 µs depending on model and settings (vendor docs). Even out the
  cameras with `TriggerDelay`, or better, record their Exposure Active.
- **The EVK4 has one trigger channel,** so it can record one signal. Either
  record the shared trigger pulse (and add each camera's start delay), or
  give each Basler's Exposure Active its own EVK4 on the sync link; then
  Typhon reads each EVK4's own trigger edges, because OpenEB's synced slices
  carry only the master's.
- **The trigger mode must be set again after every open** (Typhon's open
  turns it off, `cameras.py:625-636`).
- **Other Basler families** carry what the boost lacks: ace USB cameras have
  1 ns timestamps and a Counter Value chunk, a frame counter (vendor docs),
  which make exact pairing and drop detection easy. GigE Baslers can share
  a network clock (PTP) without a cable, but Basler gives no accuracy figure
  and it does not reach the EVK4 (vendor docs).

### 3.5 Summary by combination

| Cameras | Leader | Cable | What Typhon records |
|---|---|---|---|
| Basler + EVK4 (your plan) | The Basler, free-running | Basler Line 2/3 (Exposure Active) into EVK4 Trigger In, through a buffer | Each exposure's start and end on the EVK4 clock |
| Basler + EVK4, with an LED board or trigger box in use | The box | Box into the Basler's GPIO input and into the EVK4 Trigger In | The trigger pulses; exposure start = pulse + the camera's start delay |
| Two or more EVK4s | The master EVK4 | Sync Out into each slave's Sync In | One shared clock; each EVK4 can still record one trigger signal |
| Two or more Baslers (+ EVK4) | A trigger box or the CXP card | One pulse to every Basler; the same pulse into the EVK4 | The shared pulse on the EVK4 clock |
| Two or more Baslers (+ EVK4), one camera driving the others | A leader Basler, free-running | The leader's Exposure Active (or Timer) output, through a buffer, in a star to every follower's GPIO input and the EVK4 Trigger In; never a chain | The leader's exposures on the EVK4 clock; each follower's exposure starts later by its own delay, so record its Exposure Active on its own synced EVK4 or measure the delay once |
| Baslers + several EVK4s | Basler or box | The above, plus the EVK4 sync link | Each Basler's Exposure Active on its own synced EVK4 |
| Thermal camera (future) | Depends on its inputs | Its frame-sync input, if it has one | See 3.6 |

### 3.6 Future thermal and infrared cameras

- **Check the camera's inputs before buying** (vendor docs via retailer
  copies of FLIR data; FLIR's own datasheets were not opened):
  - FLIR Ax5 series (for example A65sc): a 3.3 V, non-isolated frame-sync
    input (low below 0.8 V, high above 2.0 V) that must not get 5 V; its
    sync output is meant for another Ax5; also one opto input (2-25 V) and
    one opto output.
  - FLIR A400 class: its inputs are listed for non-uniformity correction
    (NUC, the camera's periodic shutter flat-field: starting one, or holding
    it off) and alarms, its outputs are relays; no frame trigger is
    documented, so it cannot join the shared clock by cable.
- **NUC pauses.** On many thermal cameras a NUC closes a shutter for a
  moment and pauses or drops frames (estimate; the length varies by model).
  For pairing by counting that is a lost frame. So each NUC is logged in the
  frames CSV and the rig record (from the camera's own NUC status where it
  reports one, or by holding NUC off during a recording where the camera
  allows it), and frames around a NUC are kept out of frame pairing, as
  "uncertain" (3.7).
- Uncooled thermal sensors blur each picture over roughly 10 ms (estimate,
  no source found), so millisecond timing may be enough for them; cooled
  cameras would need the microsecond path.
- A thermal camera needs its own camera backend; the camera slot (2.2) must
  not assume pylon or OpenEB.

### 3.7 What Typhon records, and how frames are paired

- **Trigger edges** go into the `.raw` whatever Typhon does with them: OpenEB
  writes each buffer to the file before decoding it
  (`i_events_stream.cpp:637-662`; OpenEB source). Typhon adds a decoder for
  them: edges are counted live and written to `<run>_triggers.csv` beside
  the `.raw` (edge number, polarity, channel, and time as `raw_t_us` of that
  EVK4; the clock is defined in 2.6); the raw view collects them in its one
  pass, with the same origin and wrap rule. The file is the authority: after
  Stop the edges are read back from the `.raw` and the live count is checked
  against them. A round trip of 600 exposure edges in EVT2 and in
  hand-encoded EVT3 came back exact (measured, synthetic). A test adds an
  EVT3 file whose times start after the 24-bit clock wrapped (more than
  16.78 s in), whose edges must come back at their known `raw_t_us`.
- **Pairing after Stop:** pairing starts at the first edge after the quiet
  gap at Start (2.3, step 2); edges before it are counted and ignored. From
  there the k-th exposure-start edge belongs to the k-th exposure of the
  leader, counting failed grabs (below). Results go to `<run>_pairs.csv`,
  one row per frame with a status: **paired** (proved by the counts or by a
  camera frame number), **uncertain**, or **unpaired**; the outcome goes to
  `<run>_rig_end.json`.
- **Failed grabs must be counted** (code reading, not measured). A grab that
  comes back failed (`GrabSucceeded()` false, for example an incomplete frame
  on the CoaXPress link) raises `CameraError` (`cameras.py:940-942`) before
  the frame number is advanced (`cameras.py:954`), and the grab loop simply
  carries on (`capture.py:879-892`). That frame was exposed and has a
  trigger edge, but it leaves no gap in the file numbers and no CSV row, so
  every pairing after it would shift by one, and none of the checks below
  would see why. (The session does count errors, but every kind together,
  with no position in the run.) So stage M0 makes a failed grab advance the
  frame number, leaving a gap in the file numbers like a frame the save queue
  skipped, and adds the running `grab_failures` column to the frames CSV and
  the totals of grab attempts and failures to `<run>_rig_end.json` (2.6). A
  test injects a failed grab into a fake Basler and checks the gap, the
  column and the totals.
- **Finding lost frames is the hard part.** pylon's skipped-image count works
  only in its "latest image" grab modes; Typhon records in "one by one" mode
  (`cameras.py:887-903`), where it stays 0 (vendor docs; measured on the
  emulator: 0 skipped, against 161 in "latest image only" with a slow
  reader). The comment at `cameras.py:891-892` that says drops are counted is
  wrong for recording and is corrected in stage M0. So Typhon checks:
  1. the edge count against the number of exposures, that is frames saved
     plus failed grabs plus frames the save queue skipped; equal is good,
     and every frame is "paired";
  2. if they differ, where: the gaps between edges are compared with the
     gaps between PC arrival times (`host_us`); a lost frame shows as a
     double gap. This **locates** a loss but cannot prove it (below);
  3. a frame counter from the camera link, if one can be read: every
     CoaXPress image header carries a 16-bit image number (Basler
     VisualApplets docs; vendor docs). Whether pylon hands it to Python, for
     example as the block id, is unverified; ask Basler. If it does, pairing
     is exact at any frame rate;
  4. frames that Typhon's own save queue skipped, and (after M0) failed
     grabs, leave gaps in the file numbers (code);
  5. for a thermal camera, frames around a logged NUC (3.6).
- **How well step 2 works is not settled.** On a synthetic model (2.5 ms
  delivery delay, 0.3-3 ms jitter, 1 % of frames late, 1 % lost, 40 ppm
  clock drift):
  - up to 150 fps both studies matched almost every frame: 0.04-0.07 %
    wrong at 0.3-1 ms jitter in the first (measured by the first report; its
    script is lost and no fact-check re-ran it); in the fact-check 0-5 of
    2,968-2,979 frames, at most 0.17 % (measured, synthetic;
    `match_sim_out.jsonl`);
  - above that, the result depends on the matching method and the jitter:
    at 500 fps the first study had up to 84 % wrong (measured by the first
    report; its script is lost and no fact-check re-ran it), the
    fact-check's better matcher 0.3-0.9 % at jitter up to 1 ms and 15-19 %
    at 3 ms; at 1000 fps the fact-check had 2-38 % wrong as the jitter rose
    from 0.3 to 3 ms (measured, synthetic), the first study up to 85 %
    (measured by the first report; its script is lost and no fact-check
    re-ran it);
  - with no frames lost, the fact-check's matcher made no errors at any rate
    up to 1000 fps (measured, synthetic): lost frames combined with jitter
    cause the errors, not the frame rate itself.
- **So arrival times never decide a pairing on their own.** Even at 150 fps
  the few wrong pairs look exactly like right ones, so trusting them would be
  a silent guess. When the edge count and the exposure count differ (step 1)
  and no camera frame number (step 3) settles it, every frame between the
  last certain match before the loss and the next certain match after it is
  marked **uncertain** in `<run>_pairs.csv`. A match is certain where the
  count proves it: counting forwards from the start of the run up to the
  first doubtful gap, counting backwards from the end (Stop waits for the
  last frame, 2.3) down to the last doubtful gap, and at any frame with a
  camera frame number. A gap counts as doubtful when it is outside the
  normal spread of the run's gaps, set generously (estimate; untested). The
  arrival-time match is still written, as a hint, in its own column. Exact
  exports leave uncertain frames out by default (6.1), and the report after
  Stop says so, for example "boA5320: 1 frame lost; 31 frames uncertain
  (1,204-1,234), left out of exact exports".
- **Two checks with the first real hardware:** that trigger edges appear in a
  Typhon recording at all (a Prophesee community post reports a recording
  with none, unresolved), and whether the EVK4's event-rate limiter can drop
  trigger edges at high event rates (probably not, because they are added
  outside the pixel array; estimate).

### 3.8 Accuracy

| Source of error | Size | Label |
|---|---|---|
| EVK4 time step | 1 µs | vendor docs |
| Basler GPIO output delay | under 0.5 µs (falling) and 2.5 µs (rising) on the published ace example; boost not published | vendor docs |
| Basler internal jitter | under 100 ns peak to peak | vendor docs |
| Basler opto input, if used as trigger input | 4.5-28 µs on the ace example | vendor docs |
| CXP card front input | 40-80 ns | vendor docs |
| EVK4 opto input delay; master-slave offset | not published | — |
| Two clocks with no cable | 1 µs per second at 1 ppm; 1.2-3.0 ms per minute at 20-50 ppm | computed |
| **EVK4 pixel latency** | 220 µs typical (EVK4); at most 100 µs at 1000 lux, under 1000 µs at 5 lux (IMX636) | vendor docs |

What a time error does to a moving object (computed): at 10,000 px/s, 1 µs
is 0.01 px, 10 µs 0.1 px, 220 µs 2.2 px and 1000 µs 10 px; at 50,000 px/s,
220 µs is 11 px.

- **Space and time are separate.** On a still plate, latency has no effect
  on lining pictures up; registration is limited by calibration (section 4).
  For moving things the EVK4's latency, not the cable, dominates.
- **So the exact export compares events with a frame over the frame's whole
  exposure,** from start edge to end edge, shifted by the latency, never at
  one instant. The mean latency can be measured once per set-up (an LED
  pulsed from the same trigger edge, seen by the EVK4) and stored in the rig
  record; its spread from pixel to pixel and with brightness remains.
- The fixed delays of the Basler output and the EVK4 input (a few µs,
  estimate) are measured once with an oscilloscope or the LED test.

### 3.9 Without a cable

- Clearly labelled in the UI as "no cable: timing to milliseconds".
- **PC arrival times**, refitted through the run: millisecond-level
  (estimate, 1-10 ms; not tested).
- **A blinking LED seen by every camera** (the velocity design's LED): 0.1-1 ms
  (estimate, not tested), biased by the EVK4's latency.
- **Event cameras among themselves** can be aligned in software by event
  density to under 10 ms (paper: Li et al., arXiv 2507.04314, presented at
  ICIRA; its own pages give 2023 and 2024). That method covers event cameras
  only, not a Basler with an EVK4.
- **Software trigger fan-out** to several Baslers (2.3), and PTP for GigE
  Baslers, keep Baslers together but do not reach the EVK4.

### 3.10 What this needs in the code

- `EvkDevice` (`cameras.py:1322-1366`, start at 1580): set the sync mode
  before the stream starts, switch Trigger In on, add the trigger decoder and
  write `<run>_triggers.csv` with times on its `raw_t_us` clock (2.6); keep
  its origin (`cameras.py:1647-1649`) so that the origin on the camera clock
  can go into `<run>_rig_end.json`; remember the sync role and Trigger In
  state it set, for the camera record and rig record.
- `RawPlayback` (`event_playback.py:258-262`): collect trigger edges in the
  same pass as the pixel events, with the same origin and EVT3 wrap rule
  (`event_playback.py:288-293`). This is built on the velocity design's
  event-block reader (velocity stage 1, row 1), which refactors the same
  `RawPlayback._read`, so that reader comes first (section 9).
- Basler settings: the "Lines" group, and the trigger mode applied after
  every open.
- A failed grab advances the frame number and is counted (3.7; stage M0).
- A clear name for the Basler clock field (`timestamp_us` holds ticks).
- The rig's Start and Stop order (2.3), with per-camera Start and Stop off
  while a cable rig records.
- The pairing step after Stop, writing `<run>_pairs.csv` (with the
  paired / uncertain / unpaired status) and `<run>_rig_end.json`.
- The comment at `cameras.py:891-892` corrected.

---

## 4. Lining cameras up on the plate

### 4.1 One plate, one millimetre frame, one calibration per camera

- Each set-up has one named **plate**, with its origin and axes taken from
  the calibration target (for example x left to right, y front to back, z
  up).
- Each camera maps its own pixels to plate mm through its **own calibration**,
  kept in the velocity design's per-camera store (`camera_calibrations.json`,
  velocity 3.7) and applied by the velocity design's `to_plane()` (velocity
  3.6): lens model first, then homography. There is no second calibration
  scheme. Which pixel is where, on the sensor and on the plate grid, is fixed
  once in 4.13.
- The pictures line up because every camera was calibrated against the same
  physical target, in the same place, on the working plane.
- Each calibration entry gains an additive "plane" block (plate id,
  placement id, height). Before a MODE 2 run, the rig checks that every
  camera's active calibration names the same plate placement. A failed check
  **warns** (in Start's summary and the status line) and is recorded in the
  rig record; it **never refuses Start**, because a calibration is "never a
  reason not to capture" (velocity 3.9). The run is recorded as usual, and
  its exports say which cameras did not share the placement.
- **The recorded data is never touched.** The mappings are used only for the
  live preview and for new export files.

### 4.2 Which calibration stage serves which purpose

| Stage (velocity 3.5) | What it is | What it does in multi-camera Typhon |
|---|---|---|
| **A** | One LED, plus distance and focal length | MODE 1 only: one scale per camera, square-on, for velocity in mm/s. Not used to line cameras up |
| **B** | Two LEDs a known distance apart | MODE 1 only: scale and axis per camera. No perspective, so not enough to line cameras up |
| **C** | Four or more LEDs on the plate; homography by normalised DLT | **Required for MODE 2**: maps each camera onto plate mm. Alone it leaves a lens "floor" (4.5), so Typhon warns when a MODE 2 camera has stage C without a lens model |
| **D** | Lens distortion, Zhang's method, 10-50 views of a grid board in different poses | **Required for pixel-for-pixel work** with real lenses. Baslers do it on an ordinary printed checkerboard or dot grid, because frame cameras see still targets; EVK4s on the blinking LED grid. Either way with focus, aperture and filters locked, and fitted with cv2 or a numpy version of Zhang's method (velocity question 5). Stage C is then re-fitted on undistorted points from the shared board recording (4.4) |
| **Plate fit** (new) | Homography plus k1, k2 and the distortion centre, fitted to 25 or more plate points in one view | A second route to stage D's numbers, for cameras behind a chamber window or where tilting a board in front of the camera is impractical (4.5). Stored in the same fields, marked `source: plate_single_view` |
| **Two heights** (new) | The board recorded at two known heights | A 3 x 4 camera matrix per camera, for heights other than the plate (4.7) |

Two rules follow:

- A plate-fit lens model uses a nominal focal length, so it is valid **for
  that plate only.** Velocity's stage D row says parallel planes at other
  heights follow from the pose; the code must refuse that for a plate fit,
  not merely label it.
- The stage-C LED board also serves the one-time timing check of 3.8 (the
  same LEDs pulsed from the trigger).

### 4.3 The target: one board every camera can see

- **The velocity design's LED grid board,** laid flat on the plate with the
  LED tops at the working plane: 5 x 5 at least, 7 x 7 better.
- **How the LEDs are told apart.** Frequency cannot name more than 7 LEDs:
  the velocity design's rule (frequencies at least 10 % apart, the highest
  at most 1.8 times the lowest; velocity 3.3) allows at most 7 different
  frequencies, because 1.1^6 = 1.77 but 1.1^7 = 1.95 (computed); an RP2040
  has only 8 independent PWM slices (vendor docs; velocity 3.3); and a frame
  camera cannot see the blink frequency at all. So:
  - **A grid board (more than 7 LEDs) uses the stage-D scheme:** every LED
    blinks at one frequency, and each LED is named by its place in the grid
    (row and column). The EVK4's detector finds all the spots at that one
    frequency (velocity 3.2); a frame camera finds the lit spots (below).
  - **An orientation mark both kinds of camera can see.** A square grid of
    identical spots fits the board in 8 ways (4 rotations, each also
    mirrored), so "named by place" needs a mark. The best is one extra LED
    off the grid, placed so that no rotation or mirror of the grid lands on
    it (for example half a pitch outside one edge, one pitch in from a
    corner: on a 5 x 5 grid that leaves only the one right fit; computed).
    One corner LED left out also works but leaves one mirror symmetry
    (along that corner's diagonal; computed), so it is enough only when the
    picture is known not to be mirrored (Typhon converts `ReverseX` /
    `ReverseY` back to sensor coordinates, velocity 3.8). A third way is
    for the LED driver to light a known pattern over a few frames (for
    example only the corner LED for 0.2 s, then all of them), which needs a
    programmable driver (velocity question 2). The velocity design's
    stage-D mark, "one row at another frequency", is invisible to a Basler,
    so it is not used on a shared board.
  - **Separate frequencies only for at most 7 LEDs:** small stage-C boards
    (4-7 LEDs) and the witness LEDs (4.9), within the velocity rule above.
  - **The same LED in every camera.** Each camera names each spot by its
    grid place, using the orientation mark; the same name is the same
    physical LED in every camera, and its plate mm come from the board
    definition. So every camera must see the whole board including the mark
    (or the mark and enough of the grid around it that the counting is
    unambiguous); otherwise Typhon asks you to click one named LED in that
    camera's picture. A wrong naming shows as large leftover errors in the
    fit and is caught there (4.9).
- **The EVK4** finds the LEDs by their frequency, with the velocity design's
  detector (velocity 3.2): 0.03-0.05 px on synthetic clips, 0.1-0.3 px
  expected on a real camera (measured, synthetic; estimate).
- **A frame camera cannot see kHz blinking;** it sees a steady lit spot.
  Finding those spots is new work, not in the velocity design: subtract an
  LEDs-off frame from an LEDs-on frame, take the brightness-weighted centre,
  and name the LEDs by their grid place and the orientation mark (above).
  Expected precision 0.02-0.1 px (estimate); the photon-noise limit is about
  0.007 px for a 3 px spot with 200,000 electrons (computed).
- **The LED board is not how a Basler gets its lens numbers.** Stage D needs
  10-50 views of a pattern in different poses. A frame camera sees still
  targets, so a Basler does its stage D on an ordinary printed checkerboard
  or dot grid, fitted with cv2 or a numpy version of Zhang's method
  (velocity question 5); an EVK4 does it on the blinking grid. Focus,
  aperture and filters are locked first (4.14, steps 1-2). The shared board
  recording then gives each camera's perspective onto the plate. One flat
  recording of the board can also give lens numbers, through the plate fit,
  but 1.2-1.5 times less accurately (4.5(c)).
- **Blinking through an exposure.** With a blink period T and an exposure
  T_e, the spot's brightness changes from frame to frame by at most T / T_e
  peak to peak, and not at all when T_e is a whole number of periods
  (computed). For a 1 ms period the worst case is 10 % (for example at
  10.5 ms). With the whole grid at one frequency, every LED can be a whole
  number of periods in one exposure; LEDs at different frequencies (a small
  stage-C board, witness LEDs) cannot all be. A uniform change in brightness
  does not move a spot's centre unless the spot saturates, so this matters
  little. Rule: expose for at least 10 LED periods, or gate the LEDs with
  the Basler's exposure signal.
- **Spot size:** an emitter 0.5-1.2 mm across is 4-10 EVK4 pixels at
  0.118 mm/px and 16-40 boost pixels at 0.03 mm/px (computed).
- **A published LED-board result is not a verdict on this plan.** E2Calib
  found an LED board on event cameras to only 4.4-13.9 px (paper), but by
  simple blob detection on accumulated events, with only 0.06 % of attempts
  detected on the 1280 x 720 camera (paper), not with a frequency-matched,
  weighted centre.
- **Other targets considered:**
  - a printed or etched dot grid under a flash: excellent for frame cameras;
    the EVK4 sees only edges at each flash, and its accuracy there is
    unmeasured;
  - a screen with a blinking checkerboard: bench only, for stage D. Its
    cover glass puts the pattern 0.5-1.5 mm above the pixels (estimate),
    worth 0.3-1.4 mm between tilted cameras (computed), and E2Calib saw
    screen failures on some cameras (paper);
  - marks on the build plate, or dots written by the machine's own laser:
    ties the plate to machine coordinates; untested.
- **Thermal cameras cannot see the LEDs** (long-wave infrared, and too slow
  for kHz blinking; estimate). They need heated pads or an
  emissivity-contrast mask (bare metal against black paint, gently heated)
  at the LED positions, as ORNL uses a heated checkerboard for its FLIR
  Boson cameras (paper). Infrared windows (germanium, zinc selenide) bend
  rays far more than glass (estimate), so such a camera is calibrated through
  its own window.
- **Not used:** neural-network calibration (E2VID/E2Calib). The reasons are
  its pickled model file, its non-US origin and reproducibility. (Torch is
  already in the council env, measured, so the dependency is not the reason.)

### 4.4 Calibrate every camera at once

- Every camera records the board **in one placement**. Errors in the board's
  own LED positions then cancel between cameras (computed, 120 x 80 mm rig,
  5 x 5 LEDs, σ = 0.15 px): at 0.1 mm of board error, the worst pair stays at
  0.028-0.034 mm (p99) with one shared placement, against 0.19-0.20 mm when
  each camera sees its own placement. Absolute accuracy in mm still suffers
  (0.14 mm p99).
- Recording **at the same moment** guards against something moving between
  cameras' clips; the shared placement is what cancels the board's errors.
  The cancellation does not apply to LEDs that only one camera sees.
- **Robust fit and many points:** 25-49 LEDs in a grid at one frequency,
  each named by its grid place (4.3), with outliers (reflections) rejected
  and every point's leftover error shown. Peregrine fits 225 points with a
  robust method (paper).
- **Each EVK4 records with the velocity design's clip settings** (velocity
  3.4): ERC off ("required, not just advised"), the trail/STC filter off,
  anti-flicker off, biases as you set them. Typhon switches these off on
  each EVK4 for the clip only and restores them per camera in a `finally`
  block, so they come back even on an error; a setting that did not come
  back is named, per camera (velocity 3.10, step 3).
- **Each Basler exposes for at least 10 LED periods** (or the LEDs are gated
  by its exposure signal; 4.3), records several LEDs-on frames and several
  LEDs-off frames (the LED driver switches the LEDs off on command, or you
  do), averaged before the spots are found, and is checked for saturation:
  a spot with any pixel at the pixel format's top value is refused and
  Typhon asks for a shorter exposure or a lower LED current. Its own
  settings for the clip (exposure, gain, pixel format, area, binning) are
  restored afterwards the same way.
- **Where the clips are kept.** Each camera's clip goes in its own
  calibration's folder, `<project>/calibration/<id>/`, where `<id>` is that
  camera's new calibration id: `clip_NNN.raw` for an EVK4 (as velocity 3.7),
  and the LEDs-on and LEDs-off frames as 16-bit PNGs (`on_NNN.png`,
  `off_NNN.png`) for a Basler. The cameras' calibrations from one board
  placement are linked by one shared **placement id**, kept in each
  calibration's "plane" block and in `camera_rig.json` (4.12). Each entry
  records its camera's state during the clip, as velocity 3.7 does: for an
  EVK4 the biases, ERC, anti-flicker and trail on or off, the area and the
  camera's own readings; for a Basler the exposure, gain, pixel format,
  area, binning, mirroring, and the number of on and off frames.
- **Step by step:** section 4.14.

### 4.5 How accurate, on synthetic data

**The test rigs** (geometry chosen by the registration research, an estimate
of typical set-ups; distances from the lens; mm/px computed):

- **Rig S1, a 120 x 80 mm field:** boA5320 with a 25 mm lens at 10° tilt,
  304 mm away (0.029-0.032 mm/px); EVK4 with 16 mm at 30°, 378 mm
  (0.109-0.127 mm/px); a smaller Basler (ace acA2040-120um, 2048 x 1536,
  3.45 µm pixels, vendor docs) with 12 mm at 25°, 337 mm (0.085-0.112 mm/px).
- **Rig S2, a 250 x 250 mm plate:** boA5320 with 16 mm at 541 mm
  (0.085-0.096); EVK4 with 8 mm at 594 mm (0.32-0.44); ace with 8 mm at
  576 mm (0.21-0.31).
- Lens distortion of -0.5 %, -2 % or -5 % at the sensor corner; 30 noise draws
  per case; error measured over the whole field.

**Are those distortions realistic?** The velocity design's k1 = -0.1 / -0.3
equal about -0.45 / -1.4 % at the EVK4's corner with a 16 mm lens, and
-1.9 / -5.8 % with 8 mm (computed). A Computar M0814-MP2 lists -1.1 % at 4 mm
image height, close to the EVK4's 3.57 mm half-diagonal; a Kowa LM16HC
lists -1.0 % "TV distortion", roughly -1.6 to -3 % at the corner (vendor
docs; conversion computed). So -0.5 to -2 % is realistic.

**(a) Stage C alone leaves a floor that more LEDs do not lower** (computed):

- with every camera distorting alike, the worst pair is 0.16-0.24 mm (p99)
  on S1 and 0.19-0.30 mm on S2 at -2 %, from 4 to 81 LEDs, because their
  errors partly cancel (the fact-check's `mc1_results.json`);
- with unlike lenses, the realistic case, one camera at -2 % and the others
  near 0 gives 0.31-0.38 mm (S1) and 0.58-0.59 mm (S2); one camera at -0.5 %
  gives 0.10-0.16 mm;
- on the boost's own pixels at -2 % that is 7.5-12.6 pixels (S1).

**(b) With the lens modelled (stage D, then C),** each camera's error in its
own pixels depends only on the centroid noise σ and the number of LEDs N.
The p99 is about 2.2-3.2σ with 4 LEDs, 1.7-2.2σ with 9, 1.2-1.5σ with 25 and
0.75-0.95σ with 81, for every lens, angle and resolution tested (computed,
with exact lens numbers). Worst pair of cameras, p99, at -2 % and
σ = 0.15 px. Each mm cell gives two figures: first with the exact lens
numbers (computed by the fact-check, `mc1_results.json`), then with a
synthetic Zhang fit (measured by the first report; its script is lost and no
fact-check re-ran it). The pixel columns use both figures, at the mm per
pixel stated in each column heading (computed):

| LEDs | S1 (mm): exact lens / Zhang fit | S2 (mm): exact lens / Zhang fit | S1 in the boost's pixels (0.031 mm/px) | In the EVK4's pixels (S1 at 0.118, S2 at 0.38 mm/px) |
|---|---|---|---|---|
| 4 | 0.065 / 0.056 | 0.168 / 0.197 | 1.8-2.1 | 0.44-0.55 |
| 9 | 0.047 / 0.044 | 0.133 / 0.132 | 1.4-1.5 | 0.35-0.40 |
| 25 | 0.033 / 0.031 | 0.088 / 0.090 | 1.0-1.1 | 0.23-0.28 |
| 81 | 0.022 / 0.019 | 0.060 / 0.055 | 0.6-0.7 | 0.14-0.19 |

The worst pair is usually the EVK4 with the small Basler: the coarser camera
(the EVK4) sets the limit.

**What a real calibration adds.** These figures assume the lens numbers are
known exactly (or come from a perfect synthetic Zhang fit). The velocity
design estimates that a real stage D leaves 0.05-0.3 % of scale (velocity
3.5, row D; estimate). If that error acts like a scale error about the
picture centre, it grows with the distance from the centre: at the EVK4's
corner, about 734 px from the centre, it is 0.37-2.2 EVK4 pixels; at the
edge of the boost's picture, 2,664 px from the centre, 1.3-8 boost pixels,
and 1.5-9.2 at its corner (computed). That is larger than every figure in
the table, so **even pixel for pixel at the EVK4's size is not assured.**
The cross-camera check of 4.9 measures what a real rig reaches, and it is
the acceptance test for a rig calibration (4.14, step 9).

**(c) The plate fit** (centre fitted, same conditions) gave S1 0.064-0.072 mm
with 9 LEDs, 0.044-0.045 with 16, 0.035-0.036 with 25 and 0.026-0.029 with
81; S2 0.305-0.327, 0.137-0.152, 0.109-0.119 and 0.078-0.080 mm (computed).
That is **1.2-1.5 times worse than stage D + C** at the same number of
points (the first report's "about as accurate" was corrected by the
fact-check): roughly twice the points buy the same accuracy. It is unstable
with 9 points (2.2 px p99 on S2). With the centre fixed at the picture's
middle, a floor of 0.020-0.030 mm (S1, -2 %) and 0.11-0.16 mm (S2, -5 %)
remains (computed). One fit takes 9-12 ms (measured).

**(d) Behind a chamber window,** the plate fit wins. A flat window 6-20 mm
thick shifts rays by 0.005-7.8 mm (computed). Calibrated through the window,
lab-measured lens numbers plus a homography leave 0.009-0.137 mm per camera
and up to 0.21 mm between cameras; the plate fit leaves 0.002-0.017 mm per
camera and up to 0.030 mm between cameras (computed, no noise; a flat window
parallel to the plate).

**(e) LEDs needed** (computed; the counts grow with σ², so they wait on the
real centroid noise): at σ = 0.15 px, a quarter of a coarser-camera pixel
needs 25-49 LEDs with stage D (49-81 with the plate fit); 0.05 mm on S1
needs 6-9 (16); 0.1 mm on S2 needs 25 (49-81). At σ = 0.3 px no board up to
81 LEDs reached a quarter pixel.

**(f) The stock EVK4 lens is 8 mm** (vendor docs: SFA 0820-5M, 41.4°
across). In a two-camera check with it (EVK4 against a Basler on a
180 x 100 mm field), uncorrected k1 = -0.3 / -0.1 left 3.0 EVK4 pixels mean,
9.5 px p99 and 2.0 mm at worst; stage D brought it back to 0.08 px
(computed). **With the kit lens, the lens model is essential.**

### 4.6 Events and frames on one grid

- **Event positions** (tracks, LED centres, velocity) go to plate mm through
  a per-pixel lookup table that includes the lens: 38-60 Mev/s on one thread
  (measured, synthetic), so 10 Mev/s costs a sixth to a quarter of one core
  (10/60 to 10/38 = 0.17-0.26; computed). Per event, a float64 homography
  ran at 12.6-19.9 Mev/s, and an exact lens correction per event at only
  2.0-3.2 Mev/s, which cannot keep up (measured). The EVK4's table is 7.4 MB
  (computed). It is indexed by full-sensor pixel, with the pixel rules of
  4.13.
- **Event pictures are counted on the sensor, then warped like frames.**
  Dropping events straight into plate cells smaller than about twice the
  EVK4's own pixel leaves holes: 94 %, 75 % and 7-9 % of cells empty at a
  quarter, half and one EVK4 pixel, and at one pixel the counts vary by
  37-42 % on a perfectly uniform scene (computed by the fact-check; the
  first run gave 26-32 %), and by 47-48 % at one and a half pixels.
- **Typhon has no event counter today.** `accumulate_events` paints one
  colour per pixel that fired and calls itself "a VIEWING convenience, not a
  measurement" (`cameras.py:282-322`). Counting needs new code (numpy
  `bincount`).
- **A plate-area map per camera** (mm² per sensor pixel) lets event densities
  from different cameras be compared.
- **A native-resolution map per camera** goes with every export, because a
  tilted camera resolves differently across the plate (Halsey 2021, paper):
  in the test rigs mm/px varied by 11-13 % across the boost's field, 17-37 %
  across the EVK4's and 31-46 % across the small Basler's (computed).

### 4.7 Off the plate: parallax

- **Two cameras looking down,** a distance B apart at height H, see a point h
  above the plate shifted by B·h / (H − h) (computed). At H = 500 mm and
  B = 100 mm: 0.010 mm for a 0.05 mm powder layer, 0.20 mm for 1 mm (about
  1.4 EVK4 px), 2.04 mm for 10 mm. At H = 300 mm and B = 200 mm, 1 mm gives
  0.67 mm (computed). The 8 mm kit lens, working closer, doubles the parallax
  per mm (computed).
- **Tilted cameras** shift by tan(ray angle) × h each. In the test rigs
  (tilts of 10°, 30° and 25° from three directions) pairs differ by
  0.6-0.9 mm per mm of height, so spatter 10 mm up is 6-9 mm off (computed).
- **The target's own height matters the same way.** LED tops 0.5 / 1 / 2 /
  5 mm above the working plane put 0.46 / 0.92 / 1.84 / 4.6 mm between the
  tilted cameras (computed). For cameras looking straight down at 500 mm,
  1 mm of target height is a 0.2 % scale error (0.33 % at 300 mm) and a
  0.10-0.33 mm shift 50-100 mm off centre (computed). So the LED tops sit
  flush with the working plane, or their height goes into a two-height
  calibration.
- **Two-height calibration.** Record the board at z = 0 and again at
  z = -10 mm (platform lowered, or a gauge block). Each camera gets a 3 x 4
  camera matrix, and any parallel plane can be registered: S1 pairs
  0.016-0.026 mm at z = 0, 0.021-0.035 mm at +5 mm, 0.047-0.075 mm at +20 mm;
  S2 0.045-0.075, 0.058-0.102 and 0.115-0.228 mm (computed). Using the
  single-plate calibration at +5 / +20 mm instead gives 3.0-4.7 /
  12.6-19.6 mm (computed). This assumes the platform moves exactly
  vertically by a known amount.
- In laser powder-bed fusion the powder surface stays at the recoater
  height, so one plane suffices; in DED the part tops rise (estimate).
- **A beam splitter** removes parallax (TimeLens++ kept about 0.6 mm of
  baseline; paper), if both cameras must see particles in flight
  identically.

### 4.8 Depth of field on a tilted plate

A tilted camera cannot keep the whole plate sharp. With a thin-lens and
diffraction model (green light, focus at the centre), the S1 boost at 10°
tilt blurs about 12-15 µm at the field edge, 4.5-5.5 of its pixels, at about
f/6-7; the S2 boost about 2.7 px; the EVK4 1.2-2.1 px (computed). So a
0.03 mm grid oversamples what the optics deliver at the edges. The cures are
less tilt, or a tilted-lens (Scheimpflug) mount, which needs a camera model
with sensor tilt that the velocity design's lens model does not have.

**Which f-number.** Opening the aperture blurs the plate's near and far
edges (defocus); closing it blurs everything (diffraction). The best
compromise, where the worst blur over the field is smallest, was re-computed
for this revision with the same kind of model (thin lens, an Airy-disc
diameter of 2.44 × wavelength × f-number at 550 nm, the two blurs added in
quadrature, the test rigs' geometry from the registration fact-check):
about f/7.7 for the S1 boost (14.6 µm, 5.3 px), f/5.4 for the S1 EVK4
(2.1 px), f/5.7 for the S1 small Basler (3.1 px); f/3.9, f/3.1 and f/3.5 for
the same cameras on S2 (2.7, 1.2 and 1.9 px) (computed). The blur figures
agree with the paragraph above; the S1 boost's best f-number comes out a
little higher than its f/6-7, probably because the first model measured the
blur spot differently (its script is not on record, so this is unconfirmed).
In short: **about f/6-8 for a long lens on a
small field, about f/3-4 for short lenses on a large plate** (computed for
the test rigs only); work out your own rig's value from its geometry, set it
before calibrating, and lock it (4.14, step 1).

### 4.9 Checks you will see

1. **At calibration:** each LED's leftover error as an arrow on a plate
   drawing, in mm and in that camera's pixels; RMS, worst, and
   leave-one-out.
2. **Check clip:** the board shifted half a pitch, or a separate check
   board; leftover errors against the expected noise.
3. **Cross-camera:** the same LEDs mapped by every camera, and their
   disagreement in mm and in each camera's pixels. **This is the acceptance
   test for a rig calibration:** measured on the check clip of item 2 (the
   board moved, so the fit's own points are not reused), the worst pair's
   p99 must be below a tolerance you set (for example half an EVK4 pixel;
   your choice, question 10). Typhon shows the figure and marks the rig
   calibration "accepted" or "not accepted"; only an accepted one is
   called pixel for pixel. Recording is never refused either way (4.1).
4. **Witness LEDs** (velocity 3.8): three or four at the plate corners,
   outside the work area, in every recording. A shift of more than about
   0.5 px marks the calibration stale (estimate); with four, a small per-run
   correction is possible.
5. **A gauge-block height check,** when two heights are calibrated.
6. **A visual swipe or checkerboard comparison** of the corrected pictures.

These readings run about 3 times the true error (2.95-3.28 times for
leave-one-out; computed), because each includes one recording's own noise.
They catch blunders and drift, not small calibration errors. Averaging k
clips lowers the noise part by √k, but not steady biases (an LED package
that is not symmetric, blur on tilted views, colour differences between the
LED and the light being imaged), which were not estimated. Because the
reading includes the check clip's own noise, a rig that passes the
acceptance test is at least as good as the reading says; one that fails by a
little may be fine, and averaging a few check clips tells which.

### 4.10 MODE 1 on one shared map

Cameras with different fields can still be put on one map. Each camera
calibrates on its own placement of a board; the placements are tied
together by one large board, a fixture with known offsets, or overlap zones
holding LEDs or marks both cameras see (ORNL's four thermal cameras over one
plate use overlap zones with shared features; paper). Not by matching
picture content over featureless powder. Seams need overlap and blending;
each camera's coverage map shows where only it is valid; and the
board-error cancellation of 4.4 does not hold across placements.

### 4.11 What multi-camera changes for velocity

The velocity design (`docs/typhon_velocity_design.md`) is not edited; these
are the changes it will meet:

- **Velocity runs per camera.** Each EVK4 slot runs its own tracker on its own
  `.raw`, with its own tracker set-up and its own frozen
  `<run>_calibration.json` in its camera folder. The rig record points at
  each.
- **Millimetres from the shared plate calibration.** An EVK4 with a MODE 2
  calibration reports velocity in plate mm/s on the plate's own axes, so
  speeds from different cameras are directly comparable. `to_plane()` is
  unchanged; only the calibration entry gains its "plane" block.
- **Tracks joined across cameras, later.** With a shared clock (the EVK4
  sync link) and shared plate mm, a track leaving one camera's field can be
  joined to one entering another, best through an overlap zone. This is a
  later option, not part of the velocity stages.
- **Against Basler frames,** velocity's event times are compared with each
  frame's exposure window (3.8), never a single instant.
- **Witness LEDs** are masked out of velocity per camera.
- **A plate-fit lens model** must not be used for velocity on any other
  plane (4.2).

### 4.12 Where it is kept

- **Per camera:** the velocity store, `<project>/camera_calibrations.json`
  (velocity 3.7), with additive keys only and no format-version change
  (velocity 3.9): `intrinsics.source` ("zhang" or "plate_single_view"), the
  "plane" block, and an optional `projection_3x4` from a two-height
  calibration.
- **Per rig:** `<project>/camera_rig.json` (2.2) also holds each plate's
  definition (the board's LED layout and orientation mark, and any tie to
  machine coordinates, 4.14 step 8), each placement id with the calibration
  ids (and their sha256 fingerprints) that belong to it, the chosen plate
  grid and its pixel-centre rule (4.13), and the check history with the
  acceptance result (4.9). Same rules as the other stores: JSON only,
  temporary file then rename, OS lock, damaged files moved aside rather than
  overwritten.
- **Calibration clips:** `<project>/calibration/<id>/`, one folder per
  camera's calibration id: `clip_NNN.raw` for an EVK4, `on_NNN.png` and
  `off_NNN.png` for a Basler (4.4).
- **Per run:** each camera folder's frozen `<run>_calibration.json`
  (velocity 3.9; written at Start by velocity stage 1, row 9); the rig
  record lists each id, sha256, the plate id and the placement id.

### 4.13 Coordinates: which pixel is where

A half-pixel mix-up anywhere costs 0.5 px, more than the whole synthetic
budget of 0.23-0.28 EVK4 pixels at 25 LEDs (4.5(b)). So the rules are fixed
once, here:

- **On the sensor:** calibration points, lookup tables and warp tables use
  **full-sensor, unbinned pixel coordinates, with pixel (x, y) centred at
  index (x, y)** (velocity 3.8; stored as
  `"coords": "sensor_px_unbinned_centre_at_index"`).
- **From a picture to the sensor** (velocity 3.8): EVK4 events are already
  in sensor coordinates; an EVK4 picture is relative to the area (sensor =
  picture + area origin). A Basler picture also needs binning b
  (x_sensor = (x_pic + area_x) × b + (b − 1)/2), decimation the same way,
  and mirroring (x_sensor = W − 1 − x). Every live table and every export
  applies this conversion; none assumes the picture is the full sensor.
- **On the plate grid:** plate-grid pixel (i, j) is centred at plate mm
  `origin + (i, j) × pitch`, the same "centre at index" rule as the sensor;
  the grid's outer edge lies half a pitch outside the first and last
  centres. This rule is written into `<run>_registration.json` (6.4) and
  `camera_rig.json` as `"grid_coords": "plate_mm_centre_at_origin_plus_index_times_pitch"`.
- **Nearest-neighbour lookup rounds** to the nearest index, as
  `floor(x + 0.5)`, never plain `floor(x)`, which would shift every picture
  by half a pixel. scipy's `map_coordinates` already treats an index as a
  pixel centre, so bilinear lookups through it need no offset.
- **Drawing in Qt adds 0.5:** in Qt, picture pixel i covers [i, i + 1), so a
  point at sensor x is drawn at (x − area_x + 0.5) × zoom (velocity 2.5).
  The same holds for Qt's perspective painting (the fallback of 5.2) and for
  Pillow's warps: both map pixel corners, not centres, so a homography in
  this doc's coordinates is wrapped as shift(+0.5) · H · shift(−0.5) before
  it is handed to them. (These are the libraries' documented conventions as
  read, not yet tested here; the synthetic test below confirms each path.)
- **The warp tables are keyed** by calibration id and sha256, area, binning,
  decimation, mirroring and the target grid, and are rebuilt whenever any of
  these changes: 2.7-3.4 s for a 5328 x 3040 camera (5.2). While a table is
  rebuilt, that camera's tile shows its uncorrected picture marked
  "re-mapping".
- **A synthetic test** puts a known plate point through a known lens and
  pose into a synthetic picture (a small spot at a sub-pixel place), with no
  binning, binning 2, an area offset and `ReverseX` in turn, and checks that
  the full chain (sensor conversion, lens, homography, plate grid) puts it
  within 0.01 px of where it belongs on the plate grid.

### 4.14 Calibrating a rig in Typhon, step by step

For an engineer setting up, say, three cameras over a build plate. Steps 1-2
are per camera, once per lens set-up; steps 3-9 are per board placement.

1. **Fix the optics first, as they will be in use.** Mount each camera with
   its chamber window and any laser-blocking or neutral-density (ND)
   filters in place, because a window or filter shifts and bends rays
   (4.5(d)) and a calibration made without it is wrong with it. Focus on
   the working plane, set the aperture (about f/6-8 for a long lens on a
   small field, f/3-4 for short lenses on a large plate in the test rigs;
   work out your own from 4.8), then **lock focus and aperture** (locking
   screws or tape). Any later change of focus, aperture, filter, window or
   mount makes the calibration stale (velocity 3.8).
2. **Lens numbers per camera (stage D).** A Basler: 10-50 pictures of an
   ordinary printed checkerboard or dot grid on a flat, stiff backing, held
   at varied angles across the whole picture; frame cameras see still
   targets, so nothing needs to blink. An EVK4: the same with the blinking
   LED grid (velocity 3.10, step 8). Both fitted with Zhang's method (cv2 or
   a numpy version; velocity question 5). Where a board cannot be tilted in
   front of a camera (behind a chamber window), use the plate fit in step 7
   instead (4.2, 4.5(d)).
3. **Place the LED board.** Lay it on the plate inside the shared field,
   pushed against the plate's datum (edge stops or pins) if step 8 is
   wanted. Every camera must see the whole board, including its
   orientation mark (4.3).
4. **Bring the LED tops to the working plane.** In a powder-bed machine,
   lower the build platform by the board's height from its underside to
   the LED tops (board thickness plus LED height, measured with calipers),
   so the LED tops sit where the powder surface will be. Check with a dial
   indicator or a feeler gauge under the recoater blade, or a straightedge
   across the plate's edges. Each 0.01 mm of height error puts about
   0.009 mm between the test rigs' tilted cameras (from 4.7's 0.92 mm per
   mm; computed), so staying inside the 0.03 mm synthetic result needs the
   LED tops within about 0.03 mm of the plane (computed). Enter the
   measured height left over; it goes into the "plane" block. If the board
   cannot be made flush, use a two-height calibration (4.7).
5. **Lights and room as in use,** laser off. Run "What is blinking?"
   (velocity 3.10, step 2) on an EVK4 to pick an LED frequency clear of
   everything else in view.
6. **Record the board with every camera at once** (4.4). Typhon puts each
   EVK4 on the clip settings (ERC, trail and anti-flicker off) and each
   Basler on an exposure of at least 10 LED periods, records each EVK4's
   clip and each Basler's LEDs-on and LEDs-off frames, restores every
   camera's settings, checks the Basler spots for saturation, and files
   everything under one placement id.
7. **Detect, name and fit.** Typhon finds the spots in every camera, names
   each LED by its grid place using the orientation mark, and shows the
   names on each camera's picture for you to confirm (or asks you to click
   one named LED where a camera's view is ambiguous). It then fits each
   camera (stage C on undistorted points, or the plate fit) and shows each
   LED's leftover arrow and the cross-camera figure (4.9).
8. **Tie plate mm to the machine (optional; untested).** Plate mm come from
   the board. To tie them to the machine's coordinates or a CAD file,
   either push the board against the plate's datum pins or edges, whose
   positions in machine coordinates are known, and enter that offset and
   rotation; or have the machine itself mark a few spots at known machine
   coordinates (laser marks on a sacrificial sheet, or small printed
   cones, as Peregrine does), let every camera find them, and fit a
   rotation and shift (with a scale check) from plate mm to machine mm,
   stored in the plate definition. Peregrine reports about 250 µm between
   CAD and images (paper); how close Typhon's version gets is unknown.
9. **Check and accept.** Shift the board half a pitch (or lay a separate
   check board), record a check clip, and read the cross-camera figure: the
   acceptance test (4.9, item 3). Typhon marks the rig calibration accepted
   or not and keeps the result in `camera_rig.json`.
10. **Witness LEDs** (4.9, item 4): leave three or four at the plate
    corners, each at its own frequency, for every run, so each run checks
    itself.

---

## 5. Live view: side by side and the approximate overlay

### 5.1 What you see

- **MODE 1** needs no correction: the main view, the tiles and the grid of
  section 2.4 show each camera as it is.
- **MODE 2, side by side:** each camera's latest picture, corrected onto a
  shared preview grid of the plate (about 1-2 megapixels, for example
  1200 x 1200), shown next to the others at the same plate scale, with mm
  rulers.
- **MODE 2, overlay:** one camera as the base, another laid over it at an
  opacity you choose, or in a colour (for example EVK4 events in colour over
  a grey Basler frame). Typhon's canvas can already draw one overlay picture
  at one opacity over the base (`gui_emit_qt.py:1946-1961`); a picture of a
  different size is simply stretched into the base's rectangle, which is
  harmless here because every camera is put on the same preview grid first.
  The overlay is switched off in Typhon today (`examples/gui/typhon.gspec:317`).
- **Only cameras calibrated on the same plate placement** are offered for
  side by side or overlay.
- **The EVK4's part** is its latest picture window of events, counted on the
  sensor and corrected with the same map as frames (4.6).
- **Refreshed 10-20 times a second,** not at the cameras' full rate; when the
  worker is busy, older pictures are dropped, never queued.
- **Labelled "approximate".** Live pictures are paired by when they reached
  the PC, so they can be a frame period plus milliseconds apart; moving
  things will not line up; things above the plate will not line up
  (sections 3.8 and 4.7). Only the export after recording uses the trigger
  edges.

### 5.2 How it is done, and what it costs

A worker thread, not the window's own thread, corrects each camera's latest
8-bit display picture onto the preview grid through a precomputed
nearest-neighbour table that includes the lens model and the picture-to-
sensor conversion for the camera's current area, binning, decimation and
mirroring (4.13).

| Step (one thread unless stated; synthetic pictures) | Cost (measured) |
|---|---|
| A 5328 x 3040 frame onto a 1280 x 720 grid, nearest-neighbour table | 2.8-3.7 ms (fact-check; the first run gave 1.3-1.4 ms) |
| The same, bilinear (scipy `map_coordinates`) | 42-44 ms |
| Any camera's display picture onto a 1200 x 1200 preview grid, nearest-neighbour, lens included | 8.1-12.2 ms; 2.8-4.2 ms on 8 threads |
| Pillow perspective warp onto the same grid | 29-32.5 ms bilinear; 11-19 ms nearest (measured by the first report; its script is lost and no fact-check re-ran it) |
| Qt painting through a perspective transform, 1200 x 1200 (no lens model) | 4.7-6.3 ms fast; 11.4-15.0 ms smooth |
| The same into 1920 x 1080 | 8.7-9.9 ms fast; 19.8-20.8 ms smooth (measured by the first report; its script is lost and no fact-check re-ran it) |
| An EVK4 picture window onto the grid, lookup table plus counting | 4.0 ms at 20,000 events; 7.4-9.0 ms at 200,000 |
| Shrinking a full Basler frame by 4 x 4 averaging (16 strided 16-bit adds) | 20-21 ms (a plain floating-point mean: 103-117 ms) |

- **What that adds up to** (computed): at 20 corrected pictures a second, the
  nearest-neighbour table costs 0.16-0.24 of a core per camera, so four
  cameras need about one core; at 30 a second it would be 25-37 % of a core
  per camera. Today's live view already draws 26-28.5 full boA5320 pictures
  a second (`docs/camera_quickstart.md:994`; measured earlier).
- **Building the tables** for one 5328 x 3040 camera takes about 2.7 s for
  nearest-neighbour and 3.4 s with bilinear tables (computed from measured
  parts); they are rebuilt from the calibration numbers, not saved, and
  rebuilt again whenever the camera's calibration, area, binning, decimation
  or mirroring changes (the tables are keyed by all of these, 4.13).
- **Qt's perspective painting** is a cheap fallback that knows nothing of
  lens distortion, so it is wrong near the edges with a distorting lens. It
  also maps pixel corners, not centres, so its transform gets the half-pixel
  wrap of 4.13.
- **Not measured:** these costs while the cameras are also grabbing, saving
  PNGs and writing `.raw` files; and whether Pillow lets other threads run
  during its warps.

---

## 6. After recording: exact corrected exports

### 6.1 What "Export corrected" does

- It runs **after recording, never during capture,** on chosen time ranges
  or chosen exposures, and optionally a region of the plate, with a progress
  bar and Cancel. Per print layer only once a layer source is chosen (below).
- **For each chosen Basler exposure** (paired with its trigger edges, 3.7):
  - every frame camera's frame is resampled onto the plate grid;
  - each EVK4's events between that exposure's start and end edges (shifted
    by the measured mean latency, 3.8) are counted per sensor pixel, ON and
    OFF separately, then resampled onto the same grid (4.6);
  - for chosen ranges, the events' positions are also written in plate mm.
- **Uncertain frames are left out by default.** Frames whose pairing is
  marked "uncertain" (3.7) are skipped unless you ask for them; if you do,
  they are written with "uncertain" in their names and in
  `<run>_registration.json`.
- **Resampling looks backwards:** for each plate-grid pixel, where it falls in
  each camera, with the pixel rules of 4.13. Shrinking uses area-averaging
  (the Basler is about 4 times finer than the EVK4); enlarging uses
  bilinear. Plain bilinear is never used to shrink a 4-times-finer picture,
  because it aliases (makes false fine patterns).
- **Per layer needs a layer source, which is not chosen yet.** A recording
  is continuous, and nothing in it says where a print layer starts: the
  EVK4's only trigger channel is already taken by Exposure Active (3.4), and
  a boost frame carries no line status, because the boost supports no data
  chunks (vendor docs). The options:
  - the machine's recoater or layer signal into a spare input, for example
    the Trigger In of a second EVK4 on the sync link, so layer starts land
    on the same clock (needs that EVK4, and an electrical check of the
    machine's signal as in 3.2);
  - the machine's own log, matched after recording by time; the PC clock is
    good to milliseconds (3.9), ample for layers seconds apart, if the
    machine's log clock and the PC's are tied together (estimate);
  - finding the recoater pass in the pictures themselves (a large sweep of
    change across the plate), which needs no wiring but is untested.

  Until one is chosen, exports are by time range or exposure only
  (question 8).
- **The plate grid** defaults to the finest camera's typical mm per pixel,
  rounded (0.03 mm in rig S1, 0.09 mm in S2; computed), as Peregrine resamples
  to its finest camera (paper). You can override it (for example 0.05 or
  0.1 mm). Typhon shows how much each camera is enlarged: in S1 the EVK4 3.9
  times and the small Basler 3.2 times; in S2 4.2 and 2.85 times (computed).
- **Grid sizes** (computed): S1 at 0.0305 mm is 3933 x 2622 pixels,
  10.3 megapixels, 20.6 MB per 16-bit picture; S2 at 0.0904 mm is
  2765 x 2765, 7.65 megapixels, 15.3 MB; 0.05 mm over 250 x 250 mm is
  25 megapixels, 50 MB.

### 6.2 What it costs

One 5328 x 3040 Basler frame onto a grid of the same size (measured,
synthetic pictures; one thread unless stated):

| Method | Time per frame |
|---|---|
| Nearest-neighbour table | 93-131 ms; 26-42 ms on 8 threads |
| Pillow perspective (no lens model) | 321-325 ms bilinear; 122-126 ms nearest |
| Pillow mesh warp, 64 x 64 patches, lens included (within 0.03-0.05 px of the exact map, computed) | 297-349 ms |
| numpy bilinear | 740 ms; 180 ms on 8 threads (measured by the first report; its script is lost and no fact-check re-ran it) |
| scipy `map_coordinates`, bilinear | 753-760 ms |
| Working out every pixel's position each frame instead of using a table | 735 ms (homography); 1208 ms (with lens) (measured by the first report; its script is lost and no fact-check re-ran it) |
| Writing one full 16-bit frame as PNG (whole file, `fast_png`) | 222-232 ms; Mono8 26 ms; a 2765 x 2765 16-bit grid picture 31 ms |

- The tables for one such camera take 65 MB (nearest) or 389 MB (bilinear)
  of memory (computed).
- **A minute is a lot.** One minute at 149.81 fps is 8,989 frames (computed).
  At 0.76 s a frame on one core (scipy's bilinear, re-run by the fact-check)
  that is about 1.9 hours (computed). At 0.18 s on 8 threads it would be
  about 27 minutes (computed), but that 0.18 s is the numpy bilinear figure
  that no fact-check re-ran (table above), so the 27 minutes is unconfirmed.
  Stored at the Basler's own pitch in 16 bits, those frames would fill about
  291 GB, or 146 GB in 8 bits (computed), against 42-45 GB free on this
  laptop (measured). On rig S2, which has two frame cameras and one EVK4,
  two frames plus the EVK4's ON and OFF counts are 4 channels of 15.3 MB,
  61 MB per exposure; each further channel (another camera, or a combined
  event count) adds 15.3 MB, so 5 channels are 76.5 MB. At 30 exposures a
  second that is 1.8-2.3 GB/s (computed).
- **So exports are chosen, not total:** for chosen ranges (per layer once a
  layer source exists), at the finest pitch for short ranges, and at the
  EVK4's pitch (or a chosen pitch) for long ones.

### 6.3 A trap found

Pillow 12.0.0's bilinear warps (perspective, affine and mesh) on 16-bit
pictures (mode "I;16") silently return wrong values: 22 where 1906 was
expected on a test ramp (measured). Nearest-neighbour on "I;16" is right, and
bilinear on 32-bit integer ("I") or floating-point ("F") pictures is right
(measured). Typhon saves Mono12 frames as 16-bit PNGs (`capture.py:142-185`),
so every 16-bit export goes through 32-bit integers or numpy.

### 6.4 Files written

All new files, in `<capture folder>/<run>/<run>_corrected/` (short names,
because Windows' 260-character path limit applies on this PC; velocity
section 4):

| File | What it holds |
|---|---|
| `<run>_registration.json` | The plate grid (origin, pitch, size, axes) and its pixel-centre rule (`"grid_coords"`, 4.13); per camera: identity, calibration id, sha256, placement id, stage and model numbers (lens, homography or 3 x 4 matrix), the sensor-coordinate rule (`"coords": "sensor_px_unbinned_centre_at_index"`) and the conversions for area, binning, decimation and mirroring that were applied (velocity 3.8), its valid region on the plate, its check residuals in mm and whether the rig calibration was accepted (4.9), the latency offset used, the resampling method; the pairing summary, including the frames left out as uncertain; the software commit and the date |
| `<cam>_coverage.png`, `<cam>_mm_per_px.npy`, `<cam>_px_area_mm2.npy` | Where each camera is valid, its native resolution across the plate, and its plate area per sensor pixel |
| `<cam>_sensor_to_plate_mm.npy` | For each event camera, the per-pixel table to plate mm (float32, 7.4 MB) |
| `tNNNNNN_<cam>.png` | Each frame camera's corrected frame, 16-bit, values as captured |
| `tNNNNNN_<cam>_on.png`, `_off.png` | Each event camera's ON and OFF counts over that exposure |
| `range_NNNN_stack.npy` | All channels for a chosen range (channels x height x width, 16-bit); channel names in the JSON. Named `layer_NNNN_stack.npy` instead once a layer source exists (6.1) |
| `tNNNNNN_overlay.png` | An 8-bit colour picture for looking at |
| `<cam>_events_plate_mm.npy` | For chosen ranges: each event's time, plate x and y in mm, polarity |

- The warp tables are not saved: they are rebuilt from the numbers in
  2.7-3.4 s per 5328 x 3040 camera (5.2).
- **Never over anything:** each file is written to a temporary name and
  renamed with a rename that refuses an existing name (velocity section 4).
  Exporting again writes `<run>_corrected_2/`.
- The new name endings (`_rig.json`, `_rig_end.json`, `_pairs.csv`,
  `_triggers.csv`, `_corrected`) join the list `_unique_run` checks
  (`frame_camera.py:1594-1595`).

### 6.5 Formats, and no pickle

- PNG (16-bit, through Typhon's own `fast_png`), TIFF (16-bit, single and
  multi-page, through Pillow), numpy `.npy` read with `allow_pickle=False`,
  JSON and CSV. Each round-tripped exactly (measured).
- **No pickle anywhere.** Peregrine's own Python data mixed numpy files with
  pickle, which its C++ port could not read, so ORNL had to convert it to
  HDF5 (paper).
- **HDF5** (what Peregrine publishes its data in; paper) would need `h5py` in
  the council env, which has numpy 2.3.5, scipy 1.17.1 and Pillow 12.0.0 but
  no `h5py`, `cv2` or `tifffile`; the `pylon` env has `h5py` 3.16.0, `cv2`
  4.5.5 and `tifffile` 2026.3.3 (measured). Only with your OK (section 10,
  question 9).

---

## 7. What this laptop can and cannot do

### 7.1 The laptop (measured)

| Part | What it is |
|---|---|
| Computer | HP OMEN 16 laptop; i7-14700HX (20 cores, 28 threads); 32 GB RAM (2 x 16 GB DDR5-5600); RTX 4070 Laptop GPU |
| USB | One Intel USB 3.2 controller; 4 user SuperSpeed ports (3 Type-A, 1 Type-C) plus 2 USB-2-only ports. Each port's speed (5 or 10 Gbps) cannot be read with nothing plugged in |
| Expansion | No PCIe slot; no Thunderbolt or USB4 port |
| Network | One Realtek 1 Gbit Ethernet port (unplugged), Wi-Fi |
| Disk | Samsung 1 TB NVMe, about 96 % full, 42-45 GB free. Sustained writes of 0.09-0.27 GB/s on 12 October 2026 (with antivirus running) and 0.36-0.58 GB/s in an earlier run; Typhon's PNG saving managed 184-420 MB/s |
| Basler software | pypylon 26.8 in the `pylon` env, with only the USB and GigE transport layers; no pylon Software Suite, so no CoaXPress support (Typhon's own hint says CoaXPress needs the Suite with "CXP Camera Support": `cameras.py:2002-2007`) |
| Prophesee software | OpenEB 5.2 built at `C:/ceb/build`, with the Python trigger and sync interfaces present |

### 7.2 What it can do

- **Build and test all of MODE 1** with simulated cameras, emulated Baslers
  (`PYLON_CAMEMU`) and replayed `.raw` files (section 2.10).
- **Run EVK4s,** one per SuperSpeed port. At 10 Mev/s an EVK4 sends about
  20-60 MB/s (computed from 2-6 bytes per event; a made-up EVT3 file came to
  5.8 bytes per event, measured), well inside one port.
- **Run USB or GigE Baslers,** which unlike the boost carry timestamps and a
  frame counter (ace USB; vendor docs).
- **Test the trigger software:** trigger edges in made-up EVT2 and EVT3
  files, the Basler Line settings on the emulator, the pairing and its
  checks.
- **Develop MODE 2** (calibration maths, preview warps, exports) on synthetic
  data and, later, on recordings made elsewhere.

### 7.3 What it cannot do

- **Hold a CoaXPress card.** Basler's CXP-12 Interface Card 4C needs a PCIe
  3.0 x8 slot and the 6-pin PCIe power connector for power over CoaXPress
  (vendor docs); this laptop has neither, nor a Thunderbolt port for an
  external PCIe box (measured). Thunderbolt-attached CoaXPress grabbers from
  other makers may exist; not checked. **So no boA5320 runs on this laptop.**
- **Keep up with a boost at full speed.** Full frame in Mono8 at 149.81 fps
  is about 2.43 GB/s (computed), about 97 % of what two CXP-12 links carry
  (computed). That is roughly 4-27 times this laptop's measured disk speed,
  would fill its free space in about 18 s (computed), and exceeds Typhon's
  PNG writer ceiling of 266 Mono8 frames a second (`capture.py:405-408`).
  Compressing every frame would take roughly 8.5 cores per camera (estimate,
  from 57 ms of CPU per frame, computed from the measured 228 % of a core at
  40 frames a second).
- **10-bit at full speed** does not fit the camera's own link: Mono10p at
  149.81 fps would be 3.02 GB/s against about 2.5 GB/s of payload (computed;
  estimate), so roughly 120 fps (estimate). Basler measured 149.81 fps with
  an 8-bit pixel format (vendor docs).
- **Check the real timing:** trigger polarity, delays, pixel latency, the
  CoaXPress frame counter. These wait for real hardware.
- **Even small cameras need disk room.** A USB ace at full rate (for example
  acA1920-40um, 1920 x 1200 at 41 fps, about 94 MB/s in Mono8, computed) plus
  an EVK4's `.raw` could fill the disk's 0.09-0.27 GB/s measured on
  12 October 2026. Free space before long multi-camera runs.

### 7.4 What a boost rig needs (estimates unless marked)

- A desktop with a PCIe 3.0 x8 slot and 6-pin power per card; two full-speed
  boosts fit one 4C card with its "Dual" applet (vendor docs).
- The pylon Software Suite with CoaXPress support, or, for another maker's
  grabber, that maker's GenTL driver. Basler warns that drivers can
  conflict (vendor docs, seen only in a search summary); untested with
  pypylon.
- Fast storage: about 2.5 GB/s of sustained writes per full-speed boost, a
  separate drive per camera, or a smaller area or frame rate.
- A plain binary frame writer instead of PNG for high frame rates, so frames
  are saved as they come and compressed later, if at all. One boost in
  Mono8 at 149.81 fps sends about 2.43 GB/s (computed, 7.3), far past the
  PNG writer's ceiling. This writer is **not** in stages M0-M3: it is
  stage M4 (section 9), out of scope until the desktop PC and frame grabber
  exist, and started when they do, or sooner if a camera this laptop can run
  is set to a data rate the budget check of 2.8 says the PNG writer cannot
  keep up with.

---

## 8. What we learn from Peregrine (and we do not use its code)

### 8.1 What Peregrine is

- ORNL Manufacturing Demonstration Facility software for checking powder-bed
  metal prints (laser powder-bed fusion, electron beam, binder jet), under
  development since 2018. In 2024 it was version 21.6, Python 3.9 with
  59 dependencies and a Tkinter window, being ported to C++ with Qt, OpenCV
  and HDF5 (paper: ORNL/TM-2020/1708, ORNL/TM-2024/3632).
- Each layer it photographs the bed after melting and after recoating,
  labels every pixel with its own neural network (the "DSCNN", later the
  "DMSCNN") and builds 3D defect maps (paper).
- **It is not open source.** Its DOE CODE record (005935IBMPC00, 16 August
  2019) offers it only under a signed site licence and says "Software
  requests are temporarily unavailable" (vendor docs: OSTI page). RTX
  Technology Research Center has licensed it (paper: ORNL/TM-2023/3216).
  UT-Battelle's patent US 11,458,542 B2 (filed 17 November 2020, granted
  4 October 2022) covers the neural-network defect detection (paper); the
  patent never names Peregrine, but shares its inventors.
- **Typhon uses none of Peregrine's code and copies nothing patented.**
  Everything below is a published idea.
- Its cameras range from ordinary visible cameras (a 5 MP 16-bit OEM camera;
  a 24.4 MP Basler a2A5328 at 57 µm per pixel on one rig) to near-infrared
  imagers, photodiodes laid onto the same grid, a mid-wave infrared camera,
  and four FLIR Boson long-wave infrared cameras being stitched over one
  machine's quadrants (paper; ORNL news: "4 to 20 megapixels" in most cases,
  tested on seven printers).

### 8.2 How Peregrine lines its cameras up (paper)

- **Each camera gets its own perspective correction onto the plate's mm
  frame.** The published method for one machine (Scime et al. 2023): print a
  15 x 15 grid of cones 12 mm apart; find their centres (background
  subtraction, blur, an automatic brightness threshold, connected blobs);
  fit a homography with a robust method that ignores outliers (OpenCV);
  crop to 245 x 245 mm and resample to
  1842 x 1842 pixels, about 0.13 mm per pixel; add a lighting-correction
  mask.
- **The patent** describes an operator marking four corners and points along
  the edges, then perspective, lighting and colour corrections stored in a
  calibration file, and every camera resized by bilinear interpolation to one
  shared resolution, usually the finest camera's.
- **Several sensors on one grid:** visible and near-infrared images each get
  their homography, photodiode signals are laid onto the plate grid, and
  everything is resampled to the finest optical pixel (Snow et al. 2023).
- **Lens distortion** is handled only in the newer thermal work, with a heated
  checkerboard for the lens numbers and overlap zones with shared features
  for pairwise alignment and stitching (ORNL/TM-2025/4056). The earlier
  method describes only a homography plus lighting correction.
- **Accuracy is coarse by our standard:** about 250 µm between CAD and images
  at about 130 µm pixels, about 100 µm between CT scans and sensors, and a
  resolving power of 220-280 µm. The patent calls perspective correction
  "generally immaterial" to its neural network.
- **Timing is per layer** (seconds): machine logs at about 5 Hz, written only
  on change; the laser path at about 40 kHz, placed through the build log's
  melt-start time. Nothing is microsecond-synchronised.
- **Data:** its Python version mixed numpy files with pickle, which its C++
  port could not read, so ORNL converted everything to HDF5; its public
  datasets are HDF5.
- **Its results, for scale:** of pixels it labelled printed or powder, 98.3 %
  and 98.9 % were right; for large CT-visible flaws, 46.3 % of its labels
  were right and it found 39.4 % of them; none of its "recoater streaking"
  or "small CT flaw" labels were right (ORNL/TM-2023/3216; the first report
  said these were never predicted, which the fact-check corrected).
- **One table to read with care:** the patent's per-machine table lists 20 to
  290 µm per pixel, but dividing each plate size by its pixel count disagrees
  for three of six machines (103 against 110, 158-170 against 130, and 33.8
  against 30 µm; computed), so its "resolution" is not always the pixel
  size.

### 8.3 What Typhon takes from it, in spirit

| Peregrine | Typhon |
|---|---|
| One plate frame in mm | 4.1 |
| A calibration per camera, kept in a calibration file | The velocity store, per camera, frozen into every run (4.12) |
| A robust fit over many points | 25-49 LEDs at one frequency, named by grid place, outliers rejected, every leftover error shown (4.3, 4.4) |
| CAD registration to about 250 µm | An optional tie from plate mm to machine mm (4.14, step 8); untested |
| Resampling to the finest camera's pixel | The default plate grid (6.1) |
| A lighting-correction mask per camera | A later option for exports: a smoothed flat-field picture per camera |
| Overlap zones for stitching cameras with different fields | MODE 1 on one map (4.10) |
| A heated checkerboard for thermal cameras | The thermal target (4.3) |
| HDF5, never pickle | No pickle; HDF5 only with your OK (6.5) |
| Cameras sequenced "to reduce any interference" (patent) | Flashes: a frame camera's flash makes every EVK4 pixel fire. Prefer steady light or colour filters, or mark the EVK4's windows during known flashes using the exposure edges |

### 8.4 Where Typhon differs

- **An event camera cannot see a still target,** so Typhon uses blinking LEDs,
  not clicked corners or printed cones, for the shared board. (Frame cameras
  still use an ordinary printed pattern for their own lens numbers, 4.14.)
- **One microsecond clock,** not per-layer timing.
- **The lens is modelled from the start,** because the goal is pixel for
  pixel, not defect labels.
- **Local only:** none of Peregrine's server, alert e-mail or online-platform
  pieces.
- **No neural-network calibration and no defect network.**
- **No stitching by picture content** over featureless powder.

### 8.5 What others report for event-plus-frame calibration (paper)

| Method | What it uses | Reported error |
|---|---|---|
| E2Calib (CVPR workshops 2021) | Events turned into pictures by a neural network, then a standard calibrator | 0.21-0.45 px; its LED board by blob detection 4.4-13.9 px; a screen failed on some cameras, 0.79-0.81 px on others |
| E-Calib (2023) | A circle grid, no blinking | 0.13 px |
| EF-Calib (2024) | One pattern for both camera kinds; no hardware sync | 0.10-0.13 (unit not stated); camera-to-camera 0.36-0.66 mm |
| eWand (2023-24) | Three LEDs blinking at 500 Hz on a wand; 4 frame cameras and 2 EVK4s | 0.32-0.40 px (frames), 0.55-0.60 px (EVK4) |
| TimeLens++ | A beam splitter; homography plus one shift per sequence | No pixel figure given |
| EventAid | Homography from a blinking screen | "difficult to avoid pixel-level misalignment" |

Prophesee's paid SDK calibrates event cameras only ("So far, only
event-based pattern detectors have been implemented"; vendor docs), and
OpenEB 5.2 has no geometric calibration (its modules are base, core, core_ml,
stream and ui; OpenEB source). Typhon writes its own, as the velocity design
already plans.

---

## 9. Staged plan

All efforts are **estimates**: working days for one developer who knows the
codebase, tests included. The first research put MODE 1 at 18-28 days and
the trigger cable at 8-12; the rows below add what the fact-checks found
(the GIL limit with several EVK4s, finding lost frames, the data-safety
fixes), and MODE 2, which no report estimated. A review then added the rows
and widened the estimates marked "(review)" below.

### Stage M0: data-safety fixes (about 2.5-3.5 days, estimate)

| # | Work | Estimate |
|---|---|---|
| 1 | Reserve each run's name so it cannot be shared, even by two windows started in the same second into one folder: a rig run by creating its folder, a one-camera (flat) run by creating `<run>_frames.csv` with exclusive creation (`"x"`) and writing its header at once (2.6) | 0.5-1 day (review) |
| 2 | The Council's Capture tab: a unique name per recording, so a reconnect cannot overwrite earlier frames | 0.5 day |
| 3 | The root cause: frames written with exclusive creation (`"xb"`) in `fast_png`, in the Pillow fallback and in the Capture tab, so a name clash fails loudly as an unsaved frame instead of replacing one (2.6) | 0.25 day (review) |
| 4 | Count failed grabs: a failed grab advances the frame number (leaving a gap in the file numbers), the running `grab_failures` column in the frames CSV, grab attempts and failures in the records (3.7) | 0.5 day (review) |
| 5 | Correct the comment that says pylon counts dropped frames while recording (`cameras.py:891-892`), and say plainly that `timestamp_us` holds camera ticks | 0.25 day |
| 6 | Regression tests: both overwrite cases, the flat-run reservation, a forced name clash that must fail loudly, and a fake Basler with an injected failed grab | 0.5-1 day (review) |

**What you see:** nothing new, except that no frame can be lost this way,
and a failed grab shows as a counted gap instead of vanishing.

**Status: built** (branch `typhon/data-safety`, then `typhon/data-safety-fixed`
after its review; not yet merged into `qt-migration`). What it added beyond
the rows above, from the review:

- A one-camera run also gets an end record, `<run>_run_end.json`
  (format "typhon-run-end", written once at the end of the recording,
  never over anything): frames saved and not saved, grab failures and grab
  attempts, the first and last frame number, and the write that stopped
  the recording, if one did. Without it, failed grabs after the last saved
  frame were nowhere on disk. A rig run will still have `<run>_rig_end.json`
  (M1); the per-camera end record does not replace it.
- A clash or any other failed write is said in Typhon's own status line:
  every live line starts `RECORDING STOPPED — frame N not saved — …` until
  Stop, frames grabbed after it are counted "NOT saved (recording stopped)",
  and the Stop line starts with it. The first failed write still ends the
  recording (the FrameWriter's rule); skipping one frame and carrying on
  instead is a choice not made here.
- A failed grab is counted once (`grab_failures`), not also among errors.
- The reserved CSV stays open from its creation to the recorder (no window
  in which another program can take it), and Start opens the recorder
  before the live view stops.
- Not fixable in code: a Typhon from before M0 still picks its run name by
  looking at the folder, so an old and a new Typhon starting in the same
  second into one folder can still pick one name (the review measured it in
  2 of 3 tries; after these fixes 0 of 3, which is timing, not a fix). The
  new one never replaces a file: where the old one saved a frame number
  first, the new one's recording stops there and says so (measured: at
  frame 1). The old one still writes "wb", so where the new one saved first
  the old one replaces its frames silently (code reading, not measured).
  Every copy sharing a folder must be on M0 or later.

### Stage M1: MODE 1, several cameras with their own settings (about 24-37 days, estimate)

| # | Work | Estimate |
|---|---|---|
| 1 | `Rig` and `CameraSlot`, with `frame_camera`'s functions kept as wrappers; `_LIVE` as a compatibility view that sends per-connection names to the active slot and app-level names to the `Rig`, with a test that every field resolves (2.2); every existing test passing unchanged | 4.5-7 days (review) |
| 2 | One save budget and writer pool for the program; Basler buffers sized by camera count; "disk is behind" status; the data-rate check before Start | 2-3 days |
| 3 | Run layout, run reservation, rig records, the `host_us` column, the camera-record pointer, separate save drives; the path-length check before Start for every camera folder (2.6) | 2.5-3.5 days (review) |
| 4 | `camera_rig.json` (names, rig presets); several camera families in the set-up; wizard check boxes; "in use as cam2" in the scan | 2-3 days |
| 5 | Settings tabs, presets and windows following the active camera; window names per camera; folder tools re-pointed | 2-3 days |
| 6 | Tile strip (with the picture moved to the top of the view), grid view, live pop-outs | 3-5 days |
| 7 | Wireframe update and layout tests at both window sizes | 1-2 days |
| 8 | Test fakes: N simulated cameras, fake EVK4 serials and trigger/sync fakes, replay rigs | 3-4 days |
| 9 | Two or more busy EVK4s: measure, then build either OpenEB's C++ slicer in one program or one program per EVK4 (2.8); if separate programs, `subprocess` with JSON messages (no `multiprocessing`, no pickle) and a test that no pickle is used; every `.raw` opened through Typhon's `start_raw` | 3.5-5.5 days (review) |
| 10 | Software-trigger fan-out for Baslers (the rig fires the trigger) | 0.5-1 day |

**What you see:** two to four cameras in one Typhon window (simulated cameras,
emulated Baslers, replayed or real EVK4s, real USB Baslers), each with its own
settings and presets, small live tiles under the main view, one Start and
one Stop, and a run folder per camera that the slider, raw view and
classifiers open as today. All of it can be tested on this laptop.

### Stage M2: the trigger cable (about 11.5-16.5 days, plus 1-2 days with the hardware, estimate)

**Needs first:** M1, and the velocity design's event-block reader (velocity
stage 1, row 1, 1-1.5 days). Both that row and M2 row 2 change
`RawPlayback._read`, so the reader is built first and the trigger edges are
collected through it, not patched into the old reader.

| # | Work | Estimate |
|---|---|---|
| 1 | Basler "Lines" settings; trigger mode applied again after every open | 1-2 days |
| 2 | EVK4 Trigger In and sync mode in the settings; trigger edges decoded live and in the raw view (through the event-block reader), on each EVK4's `raw_t_us` clock with the origin and wrap rule; each EVK4's `origin_us` in `<run>_rig_end.json`; `<run>_triggers.csv`; the sync role and Trigger In state Typhon set, recorded as additive keys in `<run>_camera.json` and the rig record (not `Camera.save()`, 3.3) | 2.5-3.5 days (review) |
| 3 | The rig's Start order (previews stopped, EVK4s with the quiet gap, followers armed, leader last) and Stop order (leader, wait for its last frame, followers, then each `.raw` closed); per-camera Start and Stop off while a cable rig records; roles, the "armed" status; EVK4 previews held until Start in a synced rig; reconnecting a whole sync group (2.3) | 2-3 days (review) |
| 4 | Pairing after Stop: counts including failed grabs, edges before the quiet gap ignored, the gap check, "uncertain" marking, the CoaXPress frame counter if pylon gives it, `<run>_pairs.csv`, `<run>_rig_end.json`, the report on screen | 2.5-3.5 days (review) |
| 5 | Synthetic tests: EVT3 trigger files, including one that starts after the 24-bit clock wrapped; a simulated rig with trigger edges; the Start/Stop order with fake devices (a leader exposing during its preview); emulator Line tests | 2.5-3.5 days (review) |
| 6 | A helper to measure polarity, fixed delays and the EVK4's mean latency with an LED pulsed from the trigger | 1 day |
| 7 | Bring-up with real hardware: the meter checks of 3.2 (Vpu, both output states), the ground question, polarity, the first paired run | 1-2 days, when the hardware is here |

**What you see:** after Stop, a line such as "boA5320: 1,234 frames; EVK4:
1,234 exposures; all paired, none missing", or a clear warning naming where
a frame went missing and which frames are uncertain; each frame's exposure
start and end on the EVK4's clock; several EVK4s on one clock.

### Stage M3: MODE 2, cameras lined up on one plate (about 21-29 days, estimate)

**Needs first, from the velocity design:**

- the LED finder and the calibration store (velocity stage 1, rows 6-7,
  4-6 days);
- recording a calibration clip with ERC, trail and anti-flicker switched off
  and restored in a `finally` block, plus Check (velocity stage 1, row 8,
  4-5 days; the multi-camera clip of 4.4 builds on it);
- the frozen `<run>_calibration.json` written at Start and the record
  pointer (velocity stage 1, row 9, 2-3 days), which 4.12 relies on;
- stage C (velocity stage 2, row 1, 3-4 days).

For the lens model, either velocity stage D (stage 3, row 1: 5-8 days with
cv2, plus 3-5 without it; velocity question 5), with Baslers on a printed
pattern (4.14, step 2), or the plate fit (row 3 below).

| # | Work | Estimate |
|---|---|---|
| 1 | LED spots in frame cameras (LEDs-on minus LEDs-off, weighted centre); naming every LED by its grid place with the orientation mark, in frames and in events, the same name in every camera; asking for a click where a view is ambiguous (4.3) | 3-4 days (review) |
| 2 | Rig calibration: every camera records the board at once, each EVK4 with the clip settings switched off and restored per camera, each Basler with on and off frames, at least 10 LED periods, and a saturation check; clips under `<project>/calibration/<id>/` linked by one placement id; per-camera fits; the "plane" block; the cross-camera figure as the acceptance test; plate definitions in `camera_rig.json`; the walk-through of 4.14 | 4-5 days (review) |
| 3 | The plate fit (single view, numpy), refused for other planes | 2-3 days |
| 4 | Live preview: worker thread, tables keyed by calibration, area, binning, decimation and mirroring and rebuilt when they change (4.13), side by side and overlay, "approximate" labels | 3-4 days |
| 5 | Exact export: resampling (area-average and bilinear, safe for 16-bit), event counts per exposure, uncertain frames left out, the per-camera maps, `<run>_registration.json` with the pixel rules, ranges, progress and Cancel | 5-7 days |
| 6 | Checks: check clip, witness LEDs, cross-camera figure, swipe view | 2-3 days |
| 7 | Optional: two-height calibration | 2-3 days |
| 8 | Synthetic rig recordings with known answers: the LED board drawn into each simulated camera through a known lens and pose, as blinking events for EVK4s and as noisy on and off frames for Baslers; the sub-pixel landing test of 4.13 (a known plate point within 0.01 px) | 2-3 days (review) |
| 9 | Later, by need: lighting correction per camera; one map for MODE 1 cameras through overlap zones; tracks joined across cameras; a layer source for per-layer exports (6.1) | Not estimated |

**What you see:** a Calibrate step that records the board with every camera
and shows each camera's leftover errors in mm and the cameras' disagreement,
with an accepted or not-accepted verdict; a live side-by-side or overlay
view on the plate, labelled approximate; and after recording, corrected
pictures of every camera on one plate grid, with the event pictures for each
exposure, ready to compare pixel for pixel.

### Stage M4, later: a writer for a full-speed boost (about 3-5 days, estimate; not in the totals)

| # | Work | Estimate |
|---|---|---|
| 1 | A plain binary frame writer (frames appended as they come to a pre-sized file per camera, with an index CSV), a reader for the slider and the raw view, a later conversion to PNG, and the same no-overwrite and path rules | 3-5 days |

Out of scope until the desktop PC and the CoaXPress frame grabber exist
(question 2): one boost in Mono8 at 149.81 fps sends about 2.43 GB/s
(computed, 7.3), which no PNG writer and no disk in this laptop can take.
The work starts when that hardware arrives, or sooner if the budget check
before Start (2.8) shows that a camera this laptop can run is set faster
than the PNG writer keeps up with.

### How this interleaves with the velocity stages (estimate)

1. **M0 now:** it is small and protects data today.
2. **M1** needs nothing from velocity and can be tested here. Velocity stage 1
   can go before, after or alongside it; once M1 exists, velocity simply runs
   per camera (4.11).
3. **Velocity stage 1, row 1** (the event-block reader, 1-1.5 days) goes
   before M2, because M2 row 2 changes the same `RawPlayback._read`.
4. **M2** then needs M1 only. Build it before the trigger hardware arrives,
   and do its bring-up in the same session as velocity's own bring-up
   (velocity stage 2, row 5): one day with the EVK4, a Basler and the LEDs
   measures the real centroid noise, the polarity, the delays and the
   latency together.
5. **Velocity stage 1 rows 6-9 and stage 2 row 1** (LED finder, calibration
   store, clip recording with filters off and restored, the frozen per-run
   copy, stage C) are built once and serve both designs.
6. **M3** follows, with the lens model from velocity stage D, or the plate fit
   first if cv2 is not allowed.
7. **Velocity stage 2 rows 2-5 and stage 3** continue as that design says.
8. **M4** waits for the boost hardware.

In total, M0-M3 are about 60-88 days including the hardware bring-up and
excluding the optional and later rows and M4 (computed from the estimates
above: M0 2.5-3.5, M1 24-37, M2 11.5-16.5 plus 1-2 with the hardware, M3
21-29), on top of the velocity design's own stages (35-50 days for its
stages 1-3, computed from its estimates), less the pieces they share. The
review's additions account for 10-11 of those days.

---

## 10. Open questions for you

The velocity design's questions still stand and are not repeated here: lens
and focal lengths (velocity question 1), the LED driver hardware (2), fields
of view and viewports (3), the accuracy needed (4), OpenCV for stage D (5), a
compiled helper (6), the EVK4 and SDK Pro (7), and real recordings (8). The
questions below are only about several cameras.

1. **Which cameras first, and how many?** Two, three or four at once? Which
   combination should work first: Basler + EVK4, two Baslers, or two EVK4s?
2. **A desktop and a frame grabber for the boost.** Is there a desktop PC
   with a PCIe 3.0 x8 slot and 6-pin power? Which CoaXPress card (make and
   model), and would two boosts share it? This laptop cannot take one
   (section 7).
3. **More Baslers: boosts, or USB/GigE cameras?** More boA5320s need that
   desktop. USB or GigE Baslers run on this laptop and carry timestamps and a
   frame counter (vendor docs), which make pairing exact.
4. **The trigger hardware.** The Basler's Exposure Active straight into the
   EVK4 (through a small buffer board), or a separate trigger box? Could the
   LED driver of velocity question 2 also be the trigger box? With several
   Baslers, should one leader Basler drive the others in a star (3.4)? Is
   someone available to build the buffer and the Hirose cables and check
   them with a meter or oscilloscope before the first connection
   (section 3.2)? Prophesee says the EVK4's ground must be connected, but its
   connector's pin table lists no ground pin: can that someone check the
   connector shell and cable shield, or should we ask Prophesee (3.3)?
5. **One PC or two?** Will the Basler and the EVK4 run on the same PC? This
   affects grounding, and whether one program sees both clocks.
6. **Frame rates and exposures** for multi-camera runs: full frame at
   149.81 fps, or smaller areas and rates? Without a frame counter from the
   camera link, any lost frame leaves a stretch of "uncertain" pairs, and
   arrival times locate the loss less well above about 150 fps
   (section 3.7); full speed needs about 2.4 GB/s of storage per camera
   (section 7) and the later writer of stage M4.
7. **Flash or strobe lighting** for the frame cameras? Every flash makes the
   whole EVK4 fire (section 8.3).
8. **The working plane and what rises above it.** Is the plate the powder
   plane (powder-bed fusion) or rising part tops (DED)? Can the platform be
   lowered by a known amount, or a gauge block used, for a two-height
   calibration? How high do the things you care about travel (spatter,
   plume)? If they must line up too, is a beam splitter acceptable
   (section 4.7)? **Layers:** do you want exports per print layer, and if so,
   where should Typhon learn when a layer starts: the machine's recoater or
   layer signal wired into a spare input (for example a second synced
   EVK4's Trigger In), the machine's own log matched after recording, or
   the recoater pass found in the pictures (section 6.1)? Should plate mm be
   tied to the machine's or the CAD file's coordinates (4.14, step 8)?
9. **Export formats.** May `h5py` be added to the council env for HDF5
   exports, as Peregrine uses, or should exports stay PNG, TIFF, `.npy` and
   JSON (section 6.5)? Should the plate grid default to the finest camera's
   pixel size, or a fixed 0.05 or 0.1 mm to keep files small? Until a layer
   source exists (question 8), exports are for chosen time ranges.
10. **Whose pixels for "pixel for pixel", and what tolerance?** The EVK4's
    (about 0.1-0.4 mm in the examples) or the big Basler's (about 0.03 mm)?
    Synthetic tests with exact lens numbers reach about 0.1-0.4 EVK4 pixels
    and only roughly 1 Basler pixel; a real lens calibration may add about
    0.4-2 EVK4 pixels at the field edges (estimate, section 4.5(b)), so even
    the EVK4's size is not assured. What worst-pair figure should the
    cross-camera check accept (for example half an EVK4 pixel; section
    4.9)? This sets 25 against 49-81 LEDs (section 4.5), and refines
    velocity question 4.
11. **Chamber windows.** Will cameras look through windows? If so, how thick,
    what material, at what angle? That decides between stage D and the plate
    fit (section 4.5), and adds to velocity question 3.
12. **The shared board.** Can one flush LED board (5 x 5 or 7 x 7 at one
    frequency, plus an orientation LED off the grid; LED tops at the working
    plane) cover the whole shared field, be seen whole by every camera at
    once, and stay put while they record? Can the LED driver switch the LEDs
    off on command (for the Baslers' LEDs-off frames) and light a known
    pattern? Or must cameras be calibrated one at a time?
13. **Screen and folders.** Which live layout should be the default: main
    view with tiles, an equal grid, or a pop-out per camera? Is
    `<capture folder>/<run>/<camera name>/` right for runs with several
    cameras? Are default names cam1-cam4 (renameable, used in folder names)
    acceptable?
14. **Start and Stop.** One Start for all cameras, with a Record box each, or
    do some cameras need to start and stop on their own during one session?
    (With a trigger cable, per-camera Start and Stop are switched off while
    the rig records, to keep the order of 2.3.)
15. **Thermal cameras.** Any candidates yet: cooled or uncooled, USB or GigE?
    Does it have a frame-sync input (section 3.6)?
16. **Disks.** Can each high-rate camera have its own drive?

---

## What has not been verified

- **Nothing ran on real hardware:** no EVK4, no boost camera, no CoaXPress
  card, no LED board, no trigger cable. Basler results come from pylon's
  emulator; EVK4 results from made-up `.raw` files and fakes.
- **Every accuracy figure is synthetic,** and the test rigs' geometry and
  lenses are assumptions. The registration figures used exact lens numbers
  (or a perfect synthetic Zhang fit); a real stage D may add 0.05-0.3 % of
  scale (velocity estimate), 0.4-2.2 EVK4 pixels at its corner (computed,
  4.5(b)). The real centroid noise sets the LED counts (they grow with its
  square). Tangential distortion, refocusing, thermal drift, tilted or
  wedged windows, reflections on metal or powder, and how flat a real LED
  board is were not modelled.
- **LED naming:** the orientation mark and the grid naming (4.3) are a
  design, not yet tried on any picture, synthetic or real.
- **Pixel conventions:** that Pillow's and Qt's warps map pixel corners
  (4.13) is read from their conventions, not tested here.
- **Trigger electrics:** the Basler output's pull-up voltage Vpu (which
  decides whether the plain wiring can work at all: Vpu ≤ Vs and
  Vs − Vpu ≤ 2.0 V, 3.2) and its polarity with `LineInverter` off; the EVK4
  input's delay; whether the plain wiring of 3.2 works without a buffer;
  which edge the EVK4 calls ON (Prophesee's pages disagree); **where the
  EVK4's ground is** (Prophesee says it is mandatory, but the connector's pin
  table lists no ground pin; perhaps the shell or the cable shield;
  unresolved, question 4).
- **Recording the EVK4's sync role:** that `Camera.save()` cannot reach a
  device Typhon opened through the HAL is a reading of OpenEB's Python
  bindings, not tested (3.3).
- **Failed grabs:** that a failed Basler grab leaves no trace in the frame
  numbers or the CSV is a reading of the code (3.7), not measured; how often
  a real CoaXPress link returns one is unknown.
- **The synced clocks:** that a slave EVK4's times go onto the master's by
  the difference of their origins (2.6) follows from the definitions, not
  from hardware.
- **Timing:** the boost's I/O delays and exposure start delay (only an ace
  example is published); whether a boost has `BslExposureStartDelay`; the
  EVK4 master-to-slave offset; whether the event-rate limiter or a USB
  backlog can drop trigger edges; whether trigger edges appear in a real
  recording at all.
- **Frame numbering:** whether pylon passes on the CoaXPress image number,
  and whether the grabber stamps frames; the arrival-time matching figures
  come from a model with assumed delays.
- **The CoaXPress card:** whether its trigger generator can drive a front
  output; how a third-party grabber's driver behaves with pypylon.
- **The sync link:** pull-up and ground wiring, cable length, the largest
  number of slaves (Prophesee's detailed notes need a login).
- **Performance under load:** warp and export costs were measured alone, not
  while cameras grab and save; two live EVK4s over USB were not tried (only
  file playback); the cost of 2-4 live tiles in Qt was not measured; whether
  Pillow lets other threads run during its warps is unknown.
- **The code reading:** that the settings views accept a camera slot as
  `api`, and how "Update from example" treats the new optional ports in a
  hand-edited project, were read from the code, not exercised.
- **Thermal cameras:** FLIR I/O figures come from retailer copies of FLIR
  data; the 10 ms blur of uncooled sensors is an unsourced estimate.
- **Peregrine:** its code and HDF5 layout were not read (no dataset was
  downloaded); some patent wording came through a fetch tool's summaries;
  the 2020 journal paper with its accuracy tables is paywalled and was not
  read.
- **The first research reports' own scripts** were not found at the paths
  they listed, so the fact-checks re-derived most numbers with their own
  scripts (next section); where the two differ, this doc uses the
  fact-check. They did **not** re-run these, which therefore rest only on
  the first reports and are labelled "(measured by the first report; its
  script is lost and no fact-check re-ran it)" where they appear:
  - the synthetic Zhang-fit figures in the table of 4.5(b);
  - numpy bilinear export, 740 ms, and 180 ms on 8 threads (6.2), and so the
    "27 minutes on 8 threads" for a minute of boost frames;
  - working out every pixel's position each frame, 735 / 1208 ms (6.2);
  - the Pillow warp onto a 1200 x 1200 preview grid, 29-32.5 / 11-19 ms
    (5.2);
  - Qt painting into 1920 x 1080 (5.2);
  - the first matching study's 0.04-0.07 % and its 84-85 % (3.7).
- **All efforts are estimates.**

---

## Where the numbers come from

- **Research and fact-checks** (session scratch, temporary): four reports
  (Peregrine and event-plus-frame calibration; a Typhon code audit; trigger
  hardware; registration on a plane), each with a fact-check. The
  fact-checks' scripts are under
  `C:/Users/apkun/AppData/Local/Temp/claude/C--Users-apkun-Downloads-Council-Demo-Council-Demo--claude-worktrees-priceless-vaughan-cc9023/486b5624-72b4-418a-b22e-cbd5562732ac/scratchpad/mc/`:
  - `fc-peregrine/`: `api_check.py`, `evt3_trig.py`, `two_cam_check.py`,
    `two_cam_8mm.py`, `overlay_check.py`;
  - `fc-typhon_audit/`: `s_connect.py`, `two_sim_light.py`, `two_sim_s4.py`,
    `two_noise_fc.py`, `two_smooth_fc.py`, `two_pylon_fc.py`,
    `pylon_nodes.py`, `pylon_skip.py`, `two_evk_fc.py`, `evk_decode_fc.py`,
    `evk_latency_crash.py`, `geom_fc.py`, `tile_cost.py`;
  - `fc-hardware_sync/`: `typhon_raw_trig.py`, `trig_rt.py`, `evt3enc.py`,
    `probe_hal.py`, `probe_pylon.py`, `acq_cont.py`, `gil_bench.py`,
    `slicer_bench.py`, `synced_offline.py`, `match_sim.py`, `usb_ports*.py`,
    `disk_write.py`, `pdftext.py`;
  - `fc-registration/`: `g.py`, `geomcheck.py`, `mc1.py`, `mc2.py`,
    `platefit.py`, `window.py`, `twoheight.py`, `holes.py`, `bench.py`,
    `bench2.py`, `pil16.py`, `pilmesh.py`, `fpng.py`;
  - `reviser/` (this revision): `checks.py` re-derives every figure the
    review changed (the 7-frequency limit, the orientation marks, the
    4.5(a)/(b) figures from `mc1_results.json`, the real-calibration pixels,
    the 150 fps matching errors from `match_sim_out.jsonl`, the 6.2 sizes,
    the wiring thresholds); `dof.py` computes the best f-number of 4.8 from
    the registration fact-check's camera model. The `_LIVE` counts of 2.2
    were taken with `git grep` on the tests and `grep` on `frame_camera.py`
    at 6560fca.
- **Typhon code** at `qt-migration` 6560fca: `frame_camera.py`,
  `council_core/cameras.py`, `capture.py`, `event_playback.py`,
  `camera_settings.py`, `camera_setup.py`, `camera_presets.py`,
  `camera_record.py`, `fast_png.py`, `designer_examples.py`;
  `council_qt/widgets/settings_tabs.py`, `camera_settings_window.py`,
  `camera_wizard.py`, `capture_review.py`, `preset_picker.py`;
  `council_qt/tabs/capture.py`; `frame_roi.py`, `frame_classes.py`,
  `frame_timing.py`; `gui_emit_qt.py`; `examples/gui/typhon.gspec`;
  `tests/fake_evk4.py` and the tests' uses of `_LIVE`;
  `docs/camera_quickstart.md`; and `docs/typhon_velocity_design.md`
  (sections 2.5, 3.2-3.10, 4, 7 and 8, and its questions in section 9).
- **OpenEB 5.2 source** at `C:/ceb/openeb` (9003b54):
  `hal_psee_plugins/src/devices/imx636/imx636_tz_trigger_event.cpp` and
  `imx636_tz_device.cpp`; `.../gen41/gen41_tz_trigger_event.cpp`;
  `src/facilities/tz_camera_synchronization.cpp`;
  `src/devices/treuzell/tz_device_builder.cpp`;
  `hal/cpp/src/facilities/i_events_stream.cpp`;
  `hal/cpp/include/metavision/hal/decoders/evt3/evt3_decoder.h` and
  `evt3_event_types.h`; `hal/cpp/include/metavision/hal/facilities/i_trigger_in.h`;
  `hal/python/bindings/*.cpp` (among them
  `i_camera_synchronization_python.cpp`);
  `sdk/modules/stream/python/bindings/camera_python.cpp`;
  `sdk/modules/stream/cpp/src/synced_camera_streams_slicer.cpp`,
  `synced_camera_system_factory.cpp` and `camera_serialization.cpp`;
  the `hal_sync` sample.
- **Vendor docs** (not re-checkable offline): Basler pages for the
  boA5320-150cm, line sources, timestamp, data chunks, I/O timing,
  acquisition timing, exposure start delay, the CXP-12 Interface Card 4C and
  its configuration, the acA1920-40um and acA2040-120um, precision time
  protocol, the pylon C++ API (skipped images, block id) and the
  VisualApplets CoaXPress operator; Prophesee's EVK4 HD camera manual
  (versions 1.1 and 1.2, October 2024), timing-interfaces, synchronisation,
  calibration-pipeline and ground-plane-calibration pages, the IMX636
  product brief and a community forum post; the OSTI / DOE CODE record for
  Peregrine; an ORNL news page; FLIR, Computar and Kowa data through
  retailer copies.
- **Papers and reports:** ORNL/TM-2020/1708, ORNL/TM-2023/3216,
  ORNL/TM-2024/3632 and ORNL/TM-2025/4056; Scime et al., Materials 16:7293
  (2023); Halsey et al., Front. Mech. Eng. 7:767444 (2021); Snow et al.,
  Addit. Manuf. 61:103298 and 78:103817 (2023); US patent 11,458,542 B2; the
  MS&T26 abstract by Scime et al.; E2Calib (arXiv 2105.12362); E-Calib
  (2306.09078); EF-Calib (2405.17278); eWand (2309.12685); TimeLens++
  (2203.17191); HDR-ERGB (2203.06622); DSEC (2103.06011); EventAid
  (2312.08220); Li et al., hardware-free event-camera synchronisation
  (2507.04314); Zhang 2000 (camera calibration); Hartley and Zisserman
  (normalised DLT).
