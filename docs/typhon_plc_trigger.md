# PLC trigger (Do-More): taking a picture of every bottle

The **PLC trigger (Do-More)** plugin connects Typhon to an AutomationDirect
Do-More PLC (BRX, H2-DM1E or T1H-DM1E, programmed in Do-More Designer). A
photo-eye on the conveyor tells the PLC when a bottle passes. The plugin
makes sure the camera takes the picture when the bottle is in the middle of
the view, allowing for the belt speed and the delays along the way.

It is a built-in plugin that is **off** in a new project. To turn it on, open
**Plugins…** in Typhon (plugins) (the Council makes that project in its
Designer by itself, `typhon_plugins`), tick **PLC trigger (Do-More)**, and press
its button on the toolbar strip to open its window.

For a **fan** with no sensor, where you want one blade in the same spot in
every picture, see section 9 (the strobe lock). The PLC's timer stays as
the backup, switched by hand.

---

## 1. Pick a mode

| | **Wire** (recommended) | **Network** |
|---|---|---|
| What fires the camera | A PLC output wired to the camera's trigger input (a Basler `Line1`, or `CxpTrigger0` through the frame grabber) | Typhon, by software trigger, when the PLC's bottle count goes up |
| Timing | Microseconds: the camera waits its own **TriggerDelay** | A few milliseconds of spread (one poll plus Windows scheduling) |
| Wiring | One output to the camera or grabber | None; just Ethernet or a serial cable |
| Good for | Production, fast belts | Slow belts, bench tests, trying things out |
| Typhon's Triggering window | Mode **Triggered: exact**, Trigger from **Line1** / **CxpTrigger0** | Trigger from **PLC (network)**, mode exact, snapshot or record |

Both modes read the bottle count and the belt speed over one of two
connections. The window's **Connection** box picks it:

- **Ethernet (Modbus TCP)**: the default. It is fast, with a reply in about
  1 ms, and needs nothing installed.
- **Serial (Modbus RTU)**: a serial cable (RS-232, or RS-485 through a USB
  adapter) from the PC to the PLC's serial port. Each read takes the time
  its bytes need on the wire, plus up to one PLC scan: about 2 to 5 ms at
  115200 baud, and 20 ms or more at 9600. That is fine for **wire mode**,
  where serial only reads the speed and the count. In network mode it adds
  that much spread, so use Ethernet there if you can. Serial needs
  **pyserial** in the Python that Typhon runs under (`pip install pyserial`).
  Typhon never installs it; without it the window says what to install.

The window's **Mode** box picks wire or network mode. The **PLC (network)** source appears under
Trigger from only in network mode.

A third wiring also works in wire mode: wire the photo-eye **straight to the
camera's input** and let the PLC only count bottles. That removes the PLC's
scan time from the latency.

---

## 2. How the timing is worked out

```
travel        = photo-eye-to-view distance / belt speed
camera delay  = travel - fixed latency + calibration offset      (wire)
fire at       = crossing + travel - fixed latency + offset       (network)
```

- **Photo-eye to view** is measured along the belt, from the photo-eye's
  beam to the middle of the camera's view.
- **Fixed latency** is the sum of the delays that don't depend on speed: the
  PLC's scan time, the output switching on, and the camera's own trigger
  latency. Start at 0, or about 1 to 2 ms for a PLC in the loop, and let
  calibration correct it.
- **Calibration offset** is what auto-calibration adds. A positive value
  fires later.
- In **network mode**, Typhon only sees the count change when it next reads
  it. It assumes the bottle crossed half a poll and half a network round trip
  earlier.

In wire mode, the window shows the delay needed and the delay the camera
has. **Apply delay to camera** sets it on the camera shown in the main
window (Typhon's `capture.delay_us`, which is the camera's **TriggerDelay**).
With **Keep the camera's delay matched** ticked, Typhon sets it again
**between runs** whenever the speed changes. During a run the delay stays
fixed, because the camera's trigger is armed. If the belt speed changes a lot
during runs, see "Speed that changes during a run" below.

Cameras limit how long TriggerDelay can be (many Baslers allow up to 1 s).
If the delay you need is longer, the camera clamps it and the window says
what it took. In that case, move the photo-eye closer to the camera.

---

## 3. Set up the PLC in Do-More Designer

The names below are Do-More Designer's. Menus can differ a little between
versions.

### 3.1 Turn on the Modbus TCP server

1. Open **System Configuration** and the internal Ethernet port's settings
   (`@IntEthernet` on a BRX).
2. Tick **Enable Modbus/TCP Server**. The port is 502.
3. Give the PLC a fixed IP address on the camera PC's network, for example
   `192.168.1.10`, and write the program to the PLC.

Do-More's Modbus server shows its **MHR** block as holding registers:
`MHR1` is Modbus `40001`, `MHR2` is `40002`, and so on. Its **MIR** block
shows as input registers: `MIR1` is `30001`. Typhon accepts either spelling.

### 3.1b Or: turn on Modbus RTU on the serial port

For **Connection: Serial (Modbus RTU)**:

1. In **System Configuration**, open the internal serial port's settings
   (`@IntSerial`), and set its protocol to **Modbus RTU** as a
   **server** (slave).
2. Note its **station number** (node address), **baud rate**, **parity** and
   **stop bits**. Typhon's window must match all four (**station**, **baud**,
   **parity**, **stop bits**). Use 8 data bits and the fastest baud rate the
   cable allows (115200 if you can).
3. Wire the PC to that port. For RS-232, use a straight serial cable or a USB
   to RS-232 adapter. For RS-485, use a USB to RS-485 adapter, A to A and B to
   B, with termination at the ends of a long line.
4. In Typhon, pick the PC's port under **Serial port** (`COM3` and so on).
   A USB adapter appears as a COM port once its driver is installed.

The serial port serves the same MHR/MIR blocks as Ethernet, so 3.2 to 3.5
are the same either way. Menus and names can differ a little between
Do-More models and Designer versions. Check against your PLC's serial port
settings.

### 3.2 The registers Typhon reads

| Register (default) | What the ladder keeps there | Typhon setting |
|---|---|---|
| `MHR1` | Bottle count: add 1 per bottle (wrapping past 65535 is fine) | **Bottle count** |
| `MHR2` | Belt speed in mm/s (or tenths: set **units per mm/s** to 10) | **Speed register** (when **Belt speed** is *From PLC register*) |
| `MHR3` | Fire request: Typhon writes 1, the ladder pulses the camera and writes 0 back (3.4b) | **Fire request** |
| `MHR4` | Presses the ladder took (optional, for your own records) | none |
| `MHR5` | Timer switch: 0 = the PLC's timer triggers the camera, 1 = the timer is off and the computer triggers it (section 9.4) | **Timer switch** |

If the belt always runs at one speed, choose **Belt speed: Fixed**, type in
the speed, and leave `MHR2` out of the ladder.

### 3.3 Ladder: count bottles (both modes)

```
Rung 1   STRPD X0                 (photo-eye: on its rising edge)
         INC   MHR1               (one more bottle)
```

### 3.4 Ladder: fire the camera (wire mode only)

Use a **transistor (DC) output**, not a relay: a relay adds several
milliseconds and bounces. Give the camera a clean pulse of a few
milliseconds:

```
Rung 2   STRPD X0                 SET Y0          (output on at the bottle)
Rung 3   STR   Y0                 TMR T0  5 ms    (time the pulse)
Rung 4   STR   T0.Done            RST Y0          (output off)
```

Wire `Y0` to the camera's opto-isolated trigger input (a Basler's `Line1`)
or to the frame grabber's trigger input that forwards `CxpTrigger0`. Check
the camera's or grabber's manual for the voltage and polarity it needs. Set
the edge (**RisingEdge**) and, if the line is noisy, a debounce in Typhon's
Triggering window.

### 3.4b Ladder: let Typhon fire the camera through the PLC

The window's **Fire camera via PLC** button lets the GUI take a picture
during a run that is triggered from the PLC's wire. A Basler camera listens
to one trigger source at a time. While it is set to **CxpTrigger0** or
**Line1** for the PLC, it ignores Typhon's software trigger, so the GUI asks
the PLC to pulse the same output instead. The picture goes down the same
wire, with the same trigger delay as a bottle.

```
Rung 5   STR   MHR3 <> 0          SET  Y0         (Typhon asked: output on)
                                  INC  MHR4       (count the press)
                                  MOVE 0 -> MHR3  (request taken)
```

`Y0` is turned off again by rungs 3 and 4, as for a bottle. Use a compare
contact for `MHR3 <> 0`, with all three instructions on that one rung. The press reaches
the camera about one PLC scan after Typhon's write, so allow 1 to 3 ms over
Ethernet and a few ms more over serial. The camera's trigger delay applies to every
pulse on its line, so a press waits the same delay as a bottle.

If the button says *the last press was still waiting*, `MHR3` was still 1
from the press before, which means the fire rung isn't in the ladder or the
PLC isn't running.

### 3.5 Belt speed (optional)

If there is an encoder, have the PLC turn its count rate into mm/s and keep
it in `MHR2`. On a BRX, use its high-speed counter's rate, times mm per
count. Without an encoder, use **Belt speed: Fixed**.

---

## 4. Set up Typhon

1. In the plugin's window, pick the **Connection**. For Ethernet, type the
   PLC's **Address**. For serial, pick the **Serial port** and match its
   baud, parity, stop bits and station to the PLC's. Then pick the **Mode**,
   and fill in **Photo-eye to view**, **Belt speed** and the registers.
2. Press **Test connection**. It should say *The PLC answered* with the
   count and speed.
3. **Wire mode:** in the Triggering window, choose **Triggered: exact** from
   **Line1** (or **CxpTrigger0**), then press **Apply delay to camera**.
   **Network mode:** choose Trigger from **PLC (network)**.
4. Start a run, let a few bottles through, and press Stop.

To try it without hardware, tick **Use a simulated PLC**. It answers on
either connection; on serial it stands in for the port at 115200 baud. A pretend PLC and
belt run on this PC at the fixed speed you typed, one bottle per 200 mm.

---

## 5. Auto-calibrate

After a short run with bottles in the pictures:

1. Set **View along belt**: how many mm of belt one picture spans along
   the belt's direction. Set **Bottles move** to the belt's direction in the
   picture.
2. Press **Calibrate from newest pictures**. Typhon finds the bottle in the
   newest pictures (10 by default) by its edges. It measures how far the
   bottle sits from the middle and turns that into milliseconds at the belt
   speed. It then corrects **Calibration offset**.
3. Press **Apply delay to camera** (wire mode), run again, and calibrate
   again if needed. Each pass should bring the bottles closer to the middle.

Calibration works best on a plain belt. A picture where no bottle stands out
is skipped, and the result says so. You can always type the offset by hand.

---

## 6. The missed-bottle check

When the plugin is on and the PLC can be read, every run records the PLC's
count at Start and again at the end. The run's end record
(`<run>_run_end.json`, under `plugins` → `typhon.plc`) then holds:

- `bottles`: how many bottles the PLC counted during the run;
- `presses`: how many **Fire camera via PLC** presses the PLC took during
  the run;
- `frames_saved` per camera, and `missed` (frames short of (bottles +
  presses) × frames per trigger);
- in network mode, `network.triggers` (how many Typhon fired), `late`
  (bottles fired late, after the time they were due) and `merged` (bottles
  that arrived between two reads);
- the speed at Start and at the end, and the delay that speed made.

If any frames are short, the status line says so.

---

## 7. Speed that changes during a run

During a run, the camera's delay is fixed by the speed at Start. If the belt
speeds up or slows down a lot during runs, you can:

- use **network mode**, which reads the speed every quarter second and times
  each bottle with it; or
- keep the wire, and have the PLC delay its output by **encoder counts**
  instead of time. Fire `Y0` a set number of counts after the photo-eye,
  then set Typhon's distance to 0. The bottle is then always in the same
  place whatever the speed. (Typhon doesn't write that count to the PLC yet.
  It's a possible next step.)

---

## 8. When something is wrong

| What you see | What to do |
|---|---|
| *cannot reach the PLC at …* | Check the IP address and the cable, and that the PC is on the PLC's subnet. Try `ping`. |
| *cannot open COM… (… pip install pyserial …)* | Install pyserial into Typhon's Python, or use Ethernet. |
| *cannot open COM… (… access denied / could not open port …)* | Another program holds the port (Do-More Designer's serial link, a terminal). Close it. Check the port name in Device Manager. |
| *no answer from the PLC on COM…* | The cable, or the baud, parity, stop bits or station number don't match the PLC's (3.1b). On RS-485, try swapping A and B. |
| *a garbled reply on COM…* | Baud or parity mismatch, or noise on a long RS-485 line (terminate it). |
| *the PLC refused: illegal function* | Modbus/TCP Server is not enabled (3.1). |
| *the PLC refused: illegal data address* | That MHR/MIR isn't in the Modbus map. Check the register name. |
| Bottle count never changes | The photo-eye input or the `INC` rung. Watch `MHR1` in Designer's Data View. |
| *camera has no setting called 'capture.delay_us'* | The camera shown has no hardware trigger delay (the simulated or an event camera). Show the Basler in the main window. |
| Bottles are always off-centre by the same amount | Calibrate (5), or adjust **Fixed latency**. |
| Bottles drift off-centre as the speed changes | Use *Belt speed: From PLC register*, and see 7. |

The plugin's code is in `typhon_plugins/plc/` (`modbus.py` is the Modbus
client, `timing.py` the sums and `simulator.py` the pretend PLC). The
picture check is `council_core/bottle_position.py`, which the plugin reaches
through `typhon_plugin_api.builtin` because it needs numpy and Pillow. Its tests are in
`tests/test_typhon_plugins_plc.py`.

---

## 9. A fan blade in the same spot every picture (strobe lock)

The bottles above were only an example. The real set-up (Andy, 24
November 2026) is a fan. The PLC's timer triggers the camera every second,
and the goal is a picture of **one blade in the same spot every time**.
The fan's speed is set for each run and is steady once it is up to speed.
**There is no sensor on the fan** (no once-per-turn pulse), and none will be
added.

### 9.1 How it works

Without a sensor, nothing outside the camera knows where the blade is. So
the plugin uses the clock with the least latency, which is **the camera's
own frame clock**. The camera free-runs (**TriggerMode Off**) at a frame
rate that makes each picture a whole number of turns after the last:

    frame rate = (rpm / 60) / K        K = whole turns per picture

The window picks K so the rate stays at or under **pictures at most**
(1 per second by default). At 1200 rpm that is K = 20 and 1 picture per
second. The camera's clock is steady to the microsecond, with no wire, PLC
scan or network between the decision and the exposure.

The rpm you type is never exact, so the blade would creep a little each
picture. The **pictures themselves** say how far:

- **Face-on** (looking down the axle): the plugin reads the brightness on
  a ring around the hub, all the way round, and compares it with the
  reference picture's ring. This gives the angle the fan has turned, to
  about 0.1 to 0.3 degrees on a sharp picture.
- **Side-on** (the blades pass across the view): the plugin reads the
  brightness along a box across the blades' path and compares it with the
  reference's. This gives how many pixels the blades moved. The plugin
  measures how many pixels a degree is by itself at the start of each run:
  it speeds the frame rate up very slightly once and watches the drift
  change.

From the last few pictures (5 by default), the lock works out the creep
per picture and the distance from the spot. It then nudges the camera's
**AcquisitionFrameRate** to stop the creep and bring the blade back.
Pictures reach the disk a picture or two late, so after each nudge the lock
waits for pictures taken after it.

On a drawn fan, the lock holds the blade to within about ±0.5 degree after
30 to 40 pictures. It does this from a start 0.25 % off in rpm, which is
18 degrees of creep per picture at K = 20. A real fan's small speed wobble
adds to that. Expect about ±1 to 3 degrees on a steady fan, and less with
more blades in the ring and a short exposure.

**Blur:** the blade turns `rpm × 6 × exposure` degrees while the shutter is
open. At 1200 rpm that is 0.72 degree per 100 µs, and the window shows it.
Keep the exposure short and add light.

### 9.2 Any blade, or this blade

- **Any blade** (the default): with N alike blades, the picture repeats
  every 360/N degrees, so whichever blade is nearest the spot counts. The
  lock can then settle at a rate a whole blade off, and that picture still
  looks the same. The rpm it reports is the one nearest what you typed.
- **This blade (mark it)**: the same blade every time. Alike blades look
  the same to the camera, so **mark that blade** with a strip of tape near
  its tip.

### 9.3 Set it up

1. In **Fan blade**, type the fan's speed (rpm), how many pictures a second
   you want at most, the number of blades, any or this blade, and which
   way you are looking (face-on or side-on).
2. With no run going, press **Set camera for the fan**. It sets the active
   camera to TriggerMode Off, turns the frame rate limit on, and sets the
   frame rate. A trigger setting can't change in the middle of a run.
3. Record a few seconds of the spinning fan.
   - **Face-on:** press **Find hub from newest pictures**. The pixels that
     change between pictures are the blades, which form a ring around the
     hub. Check the hub and the ring numbers, and correct them if needed
     (the ring is best across the blades' outer half).
   - **Side-on:** type the box (x, y, width, height in pixels) across the
     blades' path, and which way they move (left-right or up-down).
4. Move the slider to a picture with the blade where you want it, and press
   **Use this frame as reference**. The profile is kept in the plugin's
   data folder. Changing the hub, the ring, the box or the blade count
   means taking the reference again, and the window says so. **Hold at**
   moves the spot by a few degrees from the reference.
5. Tick **Hold the blade still during runs** and press **Start**. Record in
   **Continuous** mode, from the camera's own clock.
6. Typhon must save **PNG pictures**, because the lock reads them from the
   run's folder. Raw recording (`TYPHON_SAVE_FORMAT=raw`) gives the lock
   nothing to read, and the end record says so.

Each run gets `<run>_blade_phase.csv` beside its frames, with one row per
picture:

- `turn_deg`: the angle from the reference (pixels, side-on);
- `error_deg`: the distance from the spot;
- `in_tolerance`: whether it was within **Tolerance**;
- `rate_fps`: the frame rate at the time;
- `note`: `behind` when the lock skipped a picture to keep up. It measures
  the newest picture, and a 65 MP PNG takes about a second to read.

To keep only the good pictures, filter on `in_tolerance = 1`. Nothing is
deleted. The run's end record has a `"fan"` block under
`"plugins" → "typhon.plc"` with:

- pictures measured and in tolerance;
- the nudges made;
- the final frame rate;
- the rpm the lock measured;
- the recent median error;
- any problem, in words (also said in the status line).

The lock sets the frame rate on the **active camera** only. If you switch
cameras mid-run, it stops nudging and says so. If the camera won't change
its frame rate while it is capturing, the end record counts
`rate_not_set`. A Basler normally allows it.

### 9.4 The PLC's timer as the backup

The timer stays in the ladder as the backup, in case Typhon's lock fails.
It runs only while `MHR5` is 0. Typhon never turns it back on by itself:
only the window's **PLC timer on** button, or a switch you wire, does that.

```
Rung 6   STR   MHR5 == 0          (Typhon has not taken over)
         OR    X1                 (optional: a panel switch forces the timer)
         TMR   T1  1000 ms        (the 1 s timer, as today)
Rung 7   STR   T1.Done            SET Y0          (pulse the camera)
                                  RST T1          (start the next second)
```

`Y0` is turned off again by rungs 3 and 4. Use your existing timer rungs if
you have them: they only need the `MHR5 == 0` contact (and the optional
`X1`) in front.

- **PLC timer off (computer)** writes 1 to `MHR5`. The timer stops, and the
  computer triggers the camera: through **Fire camera via PLC**, or with
  the camera free-running for the fan lock.
- **PLC timer on** writes 0, and the timer runs again.

The readout shows which one is in charge. In the fan lock the camera
ignores its trigger wire (TriggerMode Off), so the timer's pulses do no
harm while it runs. Turn the timer off anyway when the computer is in
charge, so the two never mix in a triggered run.

### 9.5 Limits

- The fan must be steady. A speed that wanders faster than the lock's few
  pictures can follow smears the spot.
- **Side-on** needs the rpm typed close to the fan's: the blades must
  creep less than about a quarter of the box between pictures. If they
  jump further, the lock says so. Lock face-on, or type the rpm the lock
  measured in an earlier run.
- The lock acts a few pictures late, because pictures are read from the
  disk after they are saved. Typhon does not hand plugins live frames yet.

