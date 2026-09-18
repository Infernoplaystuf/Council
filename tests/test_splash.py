"""
The splash: the artwork, and the three behaviours it learned the hard way.

The geometry is asserted against the Tk original point for point, because the
whole claim of the extraction is that it is the SAME drawing and not a redraw
that happens to look similar.
"""
from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import splash_art as art  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_the_artwork_imports_no_toolkit():
    source = (ROOT / "council_core" / "splash_art.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "import tk"):
        assert toolkit not in source


# ============================================================
# It is the same drawing, not a redraw
# ============================================================

def test_the_cog_is_point_for_point_the_tk_one():
    import splash as tk_splash
    for rotation in (0.0, 0.7, math.pi, 4.2):
        assert art.cog_points(180, 180, 150, 130, 16, rotation) == \
            tk_splash._cog_points(180, 180, 150, 130, 16, rotation)


def test_the_flame_is_point_for_point_the_tk_one():
    import splash as tk_splash
    for jitter in (0.0, 1.5, -2.0):
        assert art.flame_points(180, 210, 46, 80, jitter) == \
            tk_splash._flame_points(180, 210, 46, 80, jitter)


def test_a_tooth_is_four_points_not_a_star():
    """Two points a tooth gives a star. Four gives a gear, which is the whole
    difference between the shape reading as a cog and as a sparkle."""
    points = art.cog_points(0, 0, 150, 130, 16, 0.0)
    assert len(points) == 16 * 4 * 2          # 4 vertices, x and y each


def test_the_flame_is_asymmetric():
    """A symmetric flame reads as a logo rather than as fire."""
    points = art.flame_points(0, 0, 46, 80, 0.0)
    xs = points[0::2]
    assert sorted(x for x in xs if x < 0) != sorted(
        -x for x in xs if x > 0), "the flame is a mirror image of itself"


def test_the_cog_turns():
    still = art.cog_points(180, 180, 150, 130, 16, 0.0)
    turned = art.cog_points(180, 180, 150, 130, 16, 0.4)
    assert still != turned


def test_one_turn_takes_about_four_seconds():
    """Slow enough to read as deliberate rather than as a spinner in
    distress."""
    frames_per_turn = (2 * math.pi) / art.ROT_PER_FRAME
    seconds = frames_per_turn * art.FRAME_MS / 1000.0
    assert 3.0 < seconds < 5.0


# ============================================================
# The animation clock
# ============================================================

def test_advancing_turns_the_cog_and_counts_the_frame():
    animation = art.Animation(rng=random.Random(1))
    animation.advance()
    assert animation.frame == 1
    assert animation.rotation == pytest.approx(art.ROT_PER_FRAME)


def test_sparks_expire_rather_than_accumulating():
    """Every live spark has life left in it.

    The first version of this asserted `len(sparks) <= MAX_SPARKS`, which the
    spawn guard already enforces on its own — so it passed with the culling
    deleted, and proved nothing. Without culling the eight sparks never die:
    they keep rising, off the top of the window and on forever, and no new one
    can ever spawn because the list is permanently full. The flame stops
    throwing sparks about a second in.
    """
    animation = art.Animation(rng=random.Random(7))
    for _ in range(3000):
        animation.advance()
    assert len(animation.sparks) <= art.MAX_SPARKS
    assert all(s.ttl > 0 for s in animation.sparks), "dead sparks still drawn"
    assert all(s.y > -art.SIZE for s in animation.sparks), (
        "a spark is rising forever, far above the window")


def test_the_flame_keeps_throwing_new_sparks():
    """Not just at the start. A full list that never empties means the effect
    stops about a second in and the cog spins over a dead flame."""
    animation = art.Animation(rng=random.Random(7))
    for _ in range(200):
        animation.advance()
    early = {id(s) for s in animation.sparks}
    for _ in range(200):
        animation.advance()
    assert {id(s) for s in animation.sparks} - early, "no spark spawned since"


def test_sparks_rise():
    animation = art.Animation(rng=random.Random(3))
    while not animation.sparks:
        animation.advance()
    spark = animation.sparks[0]
    height = spark.y
    animation.advance()
    assert spark.y < height


def test_the_same_seed_gives_the_same_picture():
    """Injectable randomness, so a rendering test is not a coin flip."""
    one, two = art.Animation(rng=random.Random(5)), art.Animation(
        rng=random.Random(5))
    for _ in range(50):
        one.advance()
        two.advance()
    assert [(s.x, s.y, s.ttl) for s in one.sparks] == \
        [(s.x, s.y, s.ttl) for s in two.sparks]


def test_the_pulse_breathes_rather_than_blinking():
    animation = art.Animation(rng=random.Random(1))
    values = []
    for _ in range(60):
        animation.advance()
        values.append(animation.pulse())
    assert 0.9 < min(values) and max(values) < 1.1


def test_painting_issues_the_five_method_protocol():
    """The same protocol the Designer canvas uses, which is why the Qt splash
    needed no drawing code of its own."""
    calls = []

    class Recorder:
        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append(name)
            return record

    animation = art.Animation(rng=random.Random(2))
    for _ in range(30):
        animation.advance()
    animation.paint(Recorder(), "#f6c14a", "#1b1b1f")
    assert calls.count("create_polygon") == 4     # cog + three flame layers
    assert "create_oval" in calls                 # the hub
    assert set(calls) <= {"create_polygon", "create_oval", "create_line",
                          "create_rectangle", "create_text"}


# ============================================================
# The Qt window
# ============================================================

pytest.importorskip("PySide6", reason="the Qt splash needs PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt import splash as qt_splash  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_SPLASH", raising=False)
    view = qt_splash.SplashWindow(manual=True)
    yield view
    view.dismiss()
    view.deleteLater()


def test_council_no_splash_gives_a_stub(monkeypatch, qapp):
    """Scripted and headless runs drive the app with its window hidden, and a
    frameless always-visible window from a background process appears over
    whatever the user is actually doing."""
    monkeypatch.setenv("COUNCIL_NO_SPLASH", "1")
    assert isinstance(qt_splash.show_splash(), qt_splash.NoSplash)


@pytest.mark.parametrize("value", ["1", "true", "YES", " 1 "])
def test_the_switch_accepts_what_people_actually_type(value, monkeypatch, qapp):
    monkeypatch.setenv("COUNCIL_NO_SPLASH", value)
    assert isinstance(qt_splash.show_splash(), qt_splash.NoSplash)


def test_the_stub_has_the_same_surface(qapp):
    """So no caller needs a branch."""
    stub = qt_splash.NoSplash()
    for name in ("pump", "dismiss", "close"):
        assert callable(getattr(stub, name))


def test_the_stub_still_hands_over(qapp):
    """It is the REVEAL. A stub that swallowed it would leave a hidden window
    and no error — exactly the failure the whole reveal chain exists to stop."""
    revealed = []
    qt_splash.NoSplash(lambda: revealed.append(1)).dismiss()
    assert revealed == [1]


def test_a_failing_handover_does_not_keep_the_stub_alive(qapp):
    def _boom():
        raise RuntimeError("no window")

    qt_splash.NoSplash(_boom).dismiss()            # must not raise


def test_the_window_is_frameless(window):
    assert window.windowFlags() & Qt.FramelessWindowHint


def test_it_starts_on_top(window):
    """It has to be seen. It just must not stay there."""
    assert window.windowFlags() & Qt.WindowStaysOnTopHint


def test_it_lets_go_of_on_top(window):
    """A permanent topmost over a slow load sat over EVERY application on the
    desktop: frameless, unmovable, nothing to click — indistinguishable from a
    hung modal."""
    window._release_topmost()
    assert not (window.windowFlags() & Qt.WindowStaysOnTopHint)


def test_it_lets_go_soon_enough_to_matter():
    assert 0 < qt_splash.TOPMOST_MS <= 2000


def test_it_can_be_dragged(window):
    """There is no title bar to grab, so without this the window cannot be
    moved at all."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    start = window.pos()

    def event(kind, x, y):
        return QMouseEvent(kind, QPointF(10, 10), QPointF(x, y),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    window.mousePressEvent(event(QMouseEvent.Type.MouseButtonPress,
                                 start.x() + 10, start.y() + 10))
    window.mouseMoveEvent(event(QMouseEvent.Type.MouseMove,
                                start.x() + 60, start.y() + 40))
    assert window.pos() != start


def test_escape_dismisses_it(window):
    """A splash that outlives its welcome must not also be a trap."""
    from PySide6.QtGui import QKeyEvent

    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                                   Qt.NoModifier))
    assert window.dismissed


def test_dismissing_twice_hands_over_once(qapp, monkeypatch):
    """Three things can call it — the timer, Escape, and the caller — and the
    handover is the reveal. Revealing twice is at best a flicker."""
    monkeypatch.delenv("COUNCIL_NO_SPLASH", raising=False)
    revealed = []
    view = qt_splash.SplashWindow(manual=True,
                                  on_done=lambda: revealed.append(1))
    view.dismiss()
    view.dismiss()
    assert revealed == [1]
    view.deleteLater()


def test_dismissing_stops_the_frame_timer(window):
    """A timer left running on a closed window is a repaint every 33ms for the
    life of the process."""
    window.dismiss()
    assert not window._frames.isActive()


def test_manual_mode_does_not_dismiss_itself(window):
    """The caller is covering a blocking build with it; a self-dismiss mid-build
    reveals a half-built window."""
    assert window.manual
    assert not window.dismissed


def test_pump_advances_a_frame_by_hand(window):
    """The caller is blocking the event loop while it builds the main window,
    so the timer cannot fire and the cog would freeze exactly when it is most
    needed."""
    before = window._animation.frame
    window.pump()
    assert window._animation.frame > before


def test_pump_after_dismissal_does_nothing(window):
    window.dismiss()
    before = window._animation.frame
    window.pump()
    assert window._animation.frame == before


def test_it_actually_paints_the_artwork(window):
    for _ in range(40):
        window._tick()
    pixmap = QPixmap(art.SIZE, art.SIZE)
    window.render(pixmap)
    image = pixmap.toImage()
    colours = {image.pixelColor(x, y).name()
               for x in range(0, art.SIZE, 4) for y in range(0, art.SIZE, 4)}
    assert len(colours) > 10, f"only {colours}"
    assert any(c in colours for c in art.FLAME_COLOURS), "no flame on screen"


def test_a_missing_theme_does_not_stop_the_launch(qapp, monkeypatch):
    """The splash must never be the thing that stops an app starting."""
    import branding
    monkeypatch.setattr(branding, "get_theme",
                        lambda _n: (_ for _ in ()).throw(KeyError("gone")))
    accent, panel = qt_splash.SplashWindow._colours("dark")
    assert accent and panel
