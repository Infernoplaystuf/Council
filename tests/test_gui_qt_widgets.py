"""
The Qt runtime's composites and ports, driven directly.

ui/widgets.py and ui/ports.py of a generated Qt project are the strings
gui_emit_qt.WIDGETS_PY and gui_emit_qt.PORTS_RUNTIME. Here they are exec'd and
their classes built one at a time — ImageCanvas with and without a region of
interest, the frame browser over a folder, a port over each kind of widget —
so a behaviour can be checked without a whole generated project around it.
tests/test_gui_qt_runtime.py is the other half: one real generated project,
imported and driven through its window.

These assertions used to run against the Tk runtime (test_gui_roi,
test_gui_sequence, test_gui_errors), and nothing checked them on Qt. The Tk
GUIs are deprecated, so the Tk versions are gone and these are what is left:
the same behaviour, on the runtime that ships.

  * the ROI: a drag draws the box in IMAGE pixels; Apply crops and zooms to it
    and keeps it cropped on every new frame; Clear goes back to the whole
    frame; a click is not a box; an oversized box is reported as clamped; and
    the box and its entry port follow each other both ways
  * the frame browser: the index resizes to the folder; a drag decodes ONE
    frame and a typed path scans ONCE (both are debounced); an empty folder
    says why it is empty; a half-typed path keeps the loaded folder; a 16-bit
    frame is scaled, not clamped to white; and stepping back past a frame
    that cannot be read shows the good one again
  * clear(): a list empties, a status bar blanks, a progress bar goes to its
    empty state, and a cleared number box reads back None — never a false 0

Offscreen, and nothing is shown: a widget that is resized and laid out has the
geometry these tests need without ever being mapped.

Run:  python -m pytest tests/test_gui_qt_widgets.py -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must happen before PySide6 is imported by anything, including the fixture.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip(
    "PySide6",
    reason="PySide6 is not installed in this interpreter; the emitter tests in "
           "test_gui_emit_qt.py cover the generated TEXT without it")
Image = pytest.importorskip("PIL.Image")

import gui_emit_qt as gq         # noqa: E402

ROI_COLOUR = 0x00E5FF              # ImageCanvas.ROI_COLOUR, as an RGB int


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication. Offscreen."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(scope="module")
def rt(qapp):
    """The real Qt runtime strings, exec'd. Not a reimplementation."""
    ns: dict = {}
    exec(compile(gq.WIDGETS_PY, "<qt widgets>", "exec"), ns)
    exec(compile(gq.PORTS_RUNTIME, "<qt ports>", "exec"), ns)
    return ns


def _pump(qapp, ms=300):
    """The frame browser debounces with QTimers, which fire only while the
    event loop turns over."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def _laid_out(widget, w, h):
    """Give ``widget`` a real size without showing it."""
    widget.resize(w, h)
    widget.layout().activate()
    return widget


def _pt(x, y):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)


def _size(qimage):
    return (qimage.width(), qimage.height())


# ============================================================
# ImageCanvas — the region of interest
# ============================================================

@pytest.fixture
def canvas(rt):
    ic = _laid_out(rt["ImageCanvas"](roi=True), 500, 400)
    ic.set_image(Image.new("RGB", (200, 100), "grey"))
    try:
        yield ic
    finally:
        ic.deleteLater()


def _draw(ic, box):
    """Draw ``box`` (image pixels) through the canvas's own mouse handlers."""
    x, y, w, h = box
    x0, y0 = ic._to_widget(x, y)
    x1, y1 = ic._to_widget(x + w, y + h)
    ic.arm_roi()
    ic._press(_pt(x0, y0))
    ic._drag(_pt((x0 + x1) / 2, (y0 + y1) / 2))
    ic._drag(_pt(x1, y1))
    ic._release(_pt(x1, y1))


def _roi_pixels(ic):
    """How many pixels of the panel are painted in the ROI colour."""
    img = ic._view_area.grab().toImage()
    return sum(1 for y in range(0, img.height(), 2)
               for x in range(img.width())
               if img.pixel(x, y) & 0xFFFFFF == ROI_COLOUR)


def test_a_drag_draws_the_box_in_image_pixels(canvas):
    assert _roi_pixels(canvas) == 0
    _draw(canvas, (20, 10, 60, 40))
    assert canvas.get_roi() == (20, 10, 60, 40)
    assert _roi_pixels(canvas) > 0, "the box is not on screen"


def test_apply_crops_the_view_and_zooms_to_it(canvas):
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    assert _size(canvas._view) == (60, 40)
    # zoomed to FIT the crop, not left at the whole-frame scale
    area = canvas._view_area
    assert 60 * canvas._scale > 0.9 * area.width() or \
        40 * canvas._scale > 0.9 * area.height()


def test_an_applied_roi_survives_every_new_frame(canvas):
    """A scrubbed or live sequence must stay zoomed on the region, not snap
    back to the whole frame each time a new image arrives."""
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    for shade in ("red", "blue", "white"):
        canvas.set_image(Image.new("RGB", (200, 100), shade))
        assert _size(canvas._view) == (60, 40)


def test_clear_returns_to_the_whole_frame(canvas):
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    canvas.clear_roi()
    assert canvas.get_roi() is None
    assert _size(canvas._view) == (200, 100)


def test_a_click_without_a_drag_keeps_the_existing_box(canvas):
    _draw(canvas, (20, 10, 60, 40))
    x, y = canvas._to_widget(100, 50)
    canvas.arm_roi()
    canvas._press(_pt(x, y))
    canvas._release(_pt(x, y))
    assert canvas.get_roi() == (20, 10, 60, 40)


def test_drawing_is_disabled_while_a_crop_is_applied(canvas):
    """A box drawn on a zoomed crop is ambiguous — Clear first."""
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    assert not canvas._btn_draw.isEnabled()
    canvas.arm_roi()
    assert canvas._roi_armed is False


def test_an_oversized_box_is_reported_clamped_not_as_typed(canvas):
    """A 400x300 box on a 200x100 frame shows 200 x 100 — never a size whose
    pixels do not exist."""
    canvas.set_roi((0, 0, 400, 300))
    canvas.apply_roi()
    assert _size(canvas._view) == (200, 100)
    note = canvas._roi_note.text()
    assert "200 x 100" in note and "clamped" in note


def test_roi_is_off_by_default_so_drag_still_pans(rt):
    ic = _laid_out(rt["ImageCanvas"](), 400, 300)
    try:
        ic.set_image(Image.new("RGB", (50, 50)))
        ox = ic._ox
        ic._press(_pt(10, 10))
        ic._drag(_pt(30, 10))
        ic._release(_pt(30, 10))
        assert ic.roi_enabled is False and ic.get_roi() is None
        assert ic._ox == pytest.approx(ox + 20)
    finally:
        ic.deleteLater()


# ============================================================
# ImageCanvas — saying why it is empty
# ============================================================

def test_the_panel_says_why_it_is_empty(rt):
    ic = _laid_out(rt["ImageCanvas"](), 300, 200)
    try:
        ic.show_message("Pillow is not installed")
        assert ic._base is None
        assert ic._message == "Pillow is not installed"
        ic._view_area.grab()                      # painting it must not raise
        ic.set_image(Image.new("RGB", (10, 10)))
        assert ic._message == "", "an image arriving must replace the message"
    finally:
        ic.deleteLater()


# ============================================================
# The frame browser
# ============================================================

class _Rig:
    """FilePicker + Scrubber + ImageCanvas (+ an optional ROI entry), with
    the ports a generated Ports would give them and a _FrameBrowser over
    them — exactly the objects a `drives` link builds."""

    def __init__(self, rt, qapp, *, roi=False):
        from PySide6.QtWidgets import QLineEdit
        self.qapp = qapp
        self.pick = rt["FilePicker"](mode="folder")
        self.scrub = rt["Scrubber"](from_=0, to=0, show_total=True)
        self.canvas = _laid_out(rt["ImageCanvas"](roi=roi), 500, 400)
        port = rt["_WidgetPort"]
        self.p_folder = port("capture_folder", self.pick, kind="file_picker",
                             type="str", direction="io")
        self.p_index = port("frame", self.scrub, kind="scrubber", type="int",
                            direction="io")
        self.p_target = rt["_ProxyPort"]("live_view", self.canvas,
                                         writer="set_image", type="image",
                                         direction="out")
        self.p_roi = None
        if roi:
            self.entry = QLineEdit()
            self.p_roi = port("roi", self.entry, kind="entry", type="str",
                              direction="io")
        self.fb = rt["_FrameBrowser"](self.p_folder, self.p_index,
                                      self.p_target, roi_port=self.p_roi)
        # The browser defers its first load to the event loop; with no folder
        # that load CLEARS the canvas. Let it run before anything else.
        self.pump()

    def pump(self, ms=300):
        _pump(self.qapp, ms)

    def load(self, folder):
        self.p_folder.set(str(folder))
        self.pump()

    def close(self):
        for w in (self.pick, self.scrub, self.canvas):
            w.deleteLater()
        if self.p_roi is not None:
            self.entry.deleteLater()


@pytest.fixture
def rig(rt, qapp, tmp_path):
    r = _Rig(rt, qapp)
    folder = tmp_path / "cap"
    folder.mkdir()
    for n in (1, 2, 10):
        Image.new("RGB", (40, 30), (n, n, n)).save(folder / f"frame_{n}.png")
    r.folder, r.tmp = folder, tmp_path
    try:
        yield r
    finally:
        r.close()


def _count_decodes(monkeypatch):
    seen = []
    real = Image.open

    def counting(fp, *a, **k):
        seen.append(str(fp))
        return real(fp, *a, **k)

    monkeypatch.setattr(Image, "open", counting)
    return seen


def test_the_index_resizes_itself_to_the_folder(rig):
    rig.load(rig.folder)
    assert rig.fb.count() == 3
    # the scrubber's range now matches, so the slider cannot run past the end
    assert rig.scrub.slider.maximum() == 2


def test_a_drag_decodes_once_not_once_per_step(rig, monkeypatch):
    """A slider fires once per integer crossed. The `_last` guard cannot help:
    every intermediate value really is a different frame."""
    rig.load(rig.folder)
    seen = _count_decodes(monkeypatch)
    for i in (0, 1, 2, 1, 2, 0, 2):
        rig.p_index.set(i)
    rig.pump()
    assert len(seen) == 1, f"decoded {len(seen)} times during one drag"


def test_typing_a_path_scans_once_not_once_per_keystroke(rig, monkeypatch):
    full = str(rig.folder)
    calls = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir",
                        lambda p=".": (calls.append(str(p)), real(p))[1])
    for i in range(1, len(full) + 1):
        rig.p_folder.set(full[:i])
    rig.pump()
    assert len(calls) == 1, f"scanned {len(calls)} times while typing one path"
    assert rig.fb.count() == 3


def test_an_empty_folder_says_why_the_panel_is_empty(rig):
    """With no status port, the reason goes into the panel itself — the user
    is looking there. (That the stale frame is cleared is checked in
    test_gui_qt_runtime.)"""
    rig.load(rig.folder)
    assert rig.canvas._base is not None
    empty = rig.tmp / "empty"
    empty.mkdir()
    rig.load(empty)
    assert rig.canvas._base is None
    assert "No images in" in rig.canvas._message


def test_a_half_typed_path_does_not_wipe_the_loaded_folder(rig):
    rig.load(rig.folder)
    rig.p_folder.set(str(rig.folder)[:6])        # mid-typing
    rig.pump()
    assert rig.fb.count() == 3


def test_sixteen_bit_frames_are_scaled_not_clamped(rig):
    """A 16-bit CT or layer slice shown as-is clamps to near-white, so the
    scan would come out blank."""
    from PySide6.QtGui import QImage
    d = rig.tmp / "ct"
    d.mkdir()
    Image.new("I;16", (8, 8), 4096).save(d / "a.tif")
    rig.load(d)
    base = rig.canvas._base
    assert base is not None
    assert base.format() == QImage.Format.Format_Grayscale8
    assert base.pixelColor(0, 0).red() == 16          # 4096 / 256


def test_stepping_back_past_an_unreadable_frame_shows_the_good_one(rig):
    """MEASURED on the Tk runtime: frame 1 unreadable, step 0 -> 1 -> 0, and
    frame 0 never came back — _last still said 0, so show(0) returned early
    and the 'Cannot read' message stayed up with no image."""
    d = rig.tmp / "bad"
    d.mkdir()
    Image.new("RGB", (40, 30), (9, 9, 9)).save(d / "f_0.png")
    (d / "f_1.png").write_bytes(b"\x89PNG\r\n\x1a\n truncated")
    Image.new("RGB", (40, 30), (9, 9, 9)).save(d / "f_2.png")
    rig.load(d)
    assert rig.canvas._base is not None
    rig.p_index.set(1)
    rig.pump()
    assert rig.canvas._base is None
    assert "Cannot read" in rig.canvas._message
    rig.p_index.set(0)
    rig.pump()
    assert rig.canvas._base is not None, "frame 0 did not come back"
    assert rig.canvas._message == "", "the 'Cannot read' message stayed"


def test_the_box_and_the_entry_stay_in_sync_both_ways(rt, qapp):
    r = _Rig(rt, qapp, roi=True)
    try:
        r.canvas.set_image(Image.new("RGB", (200, 100)))

        _draw(r.canvas, (20, 10, 60, 40))
        r.pump(50)
        assert r.p_roi.get() == "20, 10, 60, 40", \
            "drawing did not reach the entry"

        r.p_roi.set("5, 6, 70, 30")
        r.pump(50)
        assert r.canvas.get_roi() == (5, 6, 70, 30), \
            "typing did not move the box"

        r.p_roi.set("5, 6, 7")                   # half-typed: leave it alone
        r.pump(50)
        assert r.canvas.get_roi() == (5, 6, 70, 30)

        r.p_roi.set("")
        r.pump(50)
        assert r.canvas.get_roi() is None
    finally:
        r.close()


# ============================================================
# Ports: clear() shows nothing — never a zero
# ============================================================

def test_clearing_a_list_port_empties_it(rt):
    from PySide6.QtWidgets import QListWidget
    lb = QListWidget()
    try:
        port = rt["_ListPort"]("l", lb, direction="io", type="list")
        port.set(["a", "b"])
        port.clear()
        assert lb.count() == 0 and port.items() == []
    finally:
        lb.deleteLater()


def test_a_cleared_number_box_reads_back_none_not_zero(rt):
    """The same false zero clear() keeps off the screen, handed to whatever
    reads the box next."""
    from PySide6.QtWidgets import QLineEdit
    ent = QLineEdit()
    try:
        port = rt["_WidgetPort"]("n", ent, kind="entry", type="int",
                                 direction="io", default="7")
        assert port.get() == 7
        port.clear()
        assert ent.text() == ""
        assert port.get() is None
    finally:
        ent.deleteLater()


def test_clearing_a_status_bar_blanks_its_text(rt):
    """Left as it was, the previous folder's answer sat beside the new folder
    after its scan failed (adversarial review, 2026-09)."""
    sb = rt["StatusBar"]()
    try:
        port = rt["_ProxyPort"]("st", sb, writer="set", type="str",
                                direction="out")
        port.set("3 of 5 frames captured on a bad timing")
        port.clear()
        assert sb.message.text() == ""
    finally:
        sb.deleteLater()


def test_clearing_a_progress_bar_empties_it(rt):
    from PySide6.QtWidgets import QProgressBar
    bar = QProgressBar()
    try:
        port = rt["_WidgetPort"]("pb", bar, kind="progressbar", type="float",
                                 direction="out")
        port.set(30.0)
        assert bar.value() == 30
        port.clear()
        assert bar.value() == 0
    finally:
        bar.deleteLater()
