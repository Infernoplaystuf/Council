"""
council_core.basler_scan — the wizard's "Scan Basler cameras".

Two halves. The first runs anywhere, against a fake pylon shaped like the
real one: the rules that decide a camera cannot work (Camera Link, 3D, in use,
another subnet) and that the scan survives a broken transport layer. The
second runs the REAL scan against pylon's camera emulator wherever pypylon is
installed (on this machine: the `pylon` env), and skips otherwise.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("PYLON_CAMEMU", "2")        # before pylon starts

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import basler_scan as bs


# ======================================================================
# Pure rules
# ======================================================================
def test_a_camera_without_a_serial_is_never_keyed_na():
    """pylon answers a missing property with the string "N/A" — truthy, so
    every serial-less camera shared one key (reproduced)."""
    assert bs.camera_key({"SerialNumber": "N/A", "FullName": "Basler#1"}, "#1") == "Basler#1"
    assert bs.camera_key({"SerialNumber": "4001"}, "#1") == "4001"
    assert bs.camera_key({}, "#3") == "#3"


def test_the_gige_subnet_is_computed_not_guessed():
    assert bs.same_subnet("192.168.1.20", "255.255.255.0", "192.168.1.5") is True
    assert bs.same_subnet("169.254.3.9", "255.255.0.0", "192.168.1.5") is False
    assert bs.same_subnet("192.168.1.20", "", "") is None


def test_families_from_the_model_prefix():
    assert bs.family_of("boA5320-150cm") == "boost"
    assert bs.family_of("a2A1920-51gmBAS") == "ace 2"
    assert bs.family_of("xyz") == "unrecognised model"


def test_one_failed_check_means_it_will_not_work():
    cam = bs.CameraReport("k", "m", "f", "s", "USB3 Vision", "BaslerUsb", "free")
    cam.checks = [bs.Check(bs.PASS, "a", ""), bs.Check(bs.WARN, "b", "")]
    assert cam.verdict == bs.LIMITS
    cam.checks.append(bs.Check(bs.FAIL, "c", ""))
    assert cam.verdict == bs.NO
    cam.checks = [bs.Check(bs.PASS, "a", ""), bs.Check(bs.INFO, "b", "")]
    assert cam.verdict == bs.WORKS


# ======================================================================
# A fake pylon: enumeration only (nothing here opens a camera)
# ======================================================================
class FakeInfo:
    def __init__(self, **props):
        self.props = props

    def to_dict(self):
        return dict(self.props)


class FakeTl:
    def __init__(self, devices, access=None, all_devices=None, raises=False,
                 interfaces=()):
        self.devices = devices
        self.access = access
        self.all_devices = all_devices
        self.raises = raises
        self.interfaces = [FakeInfo(InterfaceID=i) for i in interfaces]
        if access is None:
            # A transport layer without the accessibility call at all.
            self.IsDeviceAccessibleInfo = None

    def EnumerateInterfaces(self):
        return self.interfaces

    def EnumerateDevices(self):
        if self.raises:
            raise RuntimeError("this transport layer is broken")
        return self.devices

    def IsDeviceAccessibleInfo(self, info):          # noqa: F811
        return (self.access != 3, self.access)


class FakeTlInfo:
    def __init__(self, cls):
        self.cls = cls

    def GetDeviceClass(self):
        return self.cls


class FakePylon:
    def __init__(self, layers):
        self.layers = layers
        self.opened = []
        outer = self

        class Factory:
            @staticmethod
            def GetInstance():
                return outer

        self.TlFactory = Factory

    def EnumerateTls(self):
        return [FakeTlInfo(cls) for cls in self.layers]

    def CreateTl(self, info):
        return self.layers[info.cls]

    def ReleaseTl(self, tl):
        pass

    def CreateDevice(self, info):
        self.opened.append(info.props.get("SerialNumber"))
        raise RuntimeError("the fake cannot open cameras")

    def InstantCamera(self, device):
        return device


def fake_scan(layers, **kw):
    """Windows is faked too (no pnputil, no registry) unless a test says."""
    kw.setdefault("pnp", lambda args: None)
    kw.setdefault("gentl_changed", lambda: None)
    pylon = FakePylon(layers)
    return bs.scan(pylon, object(), **kw), pylon


def one(report):
    assert len(report.cameras) == 1, report.cameras
    return report.cameras[0]


def test_camera_link_is_found_but_will_not_work():
    report, pylon = fake_scan({"BaslerCameraLink": FakeTl(
        [FakeInfo(DeviceClass="BaslerCameraLink", ModelName="acA2040-180km",
                  SerialNumber="1")], access=1)})
    cam = one(report)
    assert cam.verdict == bs.NO
    assert any("Camera Link" in c.detail for c in cam.checks)
    assert pylon.opened == [], "a Camera Link camera was opened"


def test_a_3d_camera_is_found_but_will_not_work():
    cls = "BaslerGTC/Basler/GenTL_Producer_for_Basler_blaze_101_cameras"
    report, _ = fake_scan({cls: FakeTl(
        [FakeInfo(DeviceClass=cls, ModelName="blaze-101", SerialNumber="9")],
        access=1)})
    assert one(report).verdict == bs.NO


def test_a_camera_in_use_elsewhere_is_not_opened_and_says_what_to_close():
    report, pylon = fake_scan({"BaslerUsb": FakeTl(
        [FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A1920-160umBAS",
                  SerialNumber="5")], access=3)})
    cam = one(report)
    assert cam.verdict == bs.NO and pylon.opened == []
    assert "pylon Viewer" in next(c.fix for c in cam.checks if c.level == bs.FAIL)


def test_a_gige_camera_on_another_subnet_will_not_work():
    report, pylon = fake_scan({"BaslerGigE": FakeTl(
        [FakeInfo(DeviceClass="BaslerGigE", ModelName="acA1300-30gm",
                  SerialNumber="7", IpAddress="169.254.1.2",
                  SubnetMask="255.255.0.0", Interface="192.168.1.5")],
        access=1)})
    cam = one(report)
    assert cam.verdict == bs.NO and pylon.opened == []
    assert "IP Configurator" in next(c.fix for c in cam.checks if c.level == bs.FAIL)


def test_a_camera_typhon_has_open_is_reported_not_reopened():
    report, pylon = fake_scan({"BaslerUsb": FakeTl(
        [FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A", SerialNumber="5")],
        access=1)}, held_keys=["5"])
    cam = one(report)
    assert pylon.opened == []
    assert any(c.label == "In use by Typhon" for c in cam.checks)


def test_cannot_say_whether_free_means_open_it_and_see():
    """A TL without IsDeviceAccessibleInfo (or code 0) is probed anyway —
    never assumed fine, never silently dropped."""
    report, pylon = fake_scan({"BaslerUsb": FakeTl(
        [FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A", SerialNumber="5")],
        access=None)})
    cam = one(report)
    assert pylon.opened == ["5"], "it was not tried"
    assert cam.verdict == bs.NO                     # the fake cannot open
    assert any(c.label == "Opens" and c.level == bs.FAIL for c in cam.checks)


def test_a_broken_transport_layer_does_not_hide_the_others():
    report, _ = fake_scan({
        "BaslerGigE": FakeTl([], access=1, raises=True),
        "BaslerUsb": FakeTl([FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A",
                                      SerialNumber="5")], access=3)})
    assert [c.key for c in report.cameras] == ["5"]


def test_a_coaxpress_card_with_no_camera_says_what_to_check():
    report, _ = fake_scan({bs.CXP_CLASS: FakeTl([], access=1,
                                                interfaces=["CXP12-IC-1C"])})
    note = next(n for n in report.notes if n.label == "CoaXPress card")
    assert "power" in note.fix.lower()


def test_no_coaxpress_layer_is_said_for_the_boost():
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)})
    note = next(n for n in report.notes if n.label == "CoaXPress support")
    assert "boA5320" in note.detail and "CXP" in note.fix


def test_the_scan_never_raises():
    class Exploding:
        class TlFactory:
            @staticmethod
            def GetInstance():
                raise RuntimeError("pylon is broken")

    report = bs.scan(Exploding, object(), pnp=lambda a: None)
    assert report.notes and report.notes[0].level == bs.FAIL


# ======================================================================
# The real scan, against pylon's camera emulator
# ======================================================================
def _real():
    try:
        from pypylon import genicam, pylon
    except Exception:                                   # noqa: BLE001
        pytest.skip("pypylon is not installed here")
    return pylon, genicam


def emulated(report):
    cams = [c for c in report.cameras if c.device_class == bs.EMU_CLASS]
    if not cams:
        pytest.skip("pylon's camera emulator is not enabled (PYLON_CAMEMU)")
    return cams


def test_emulated_cameras_are_found_opened_grabbed_and_ready():
    pylon, genicam = _real()
    cams = emulated(bs.scan(pylon, genicam))
    for cam in cams:
        grab = next(c for c in cam.checks if c.label == "Test grab")
        assert grab.level == bs.PASS, grab.detail
        assert cam.verdict == bs.WORKS, [(c.label, c.detail) for c in cam.checks
                                         if c.level in (bs.WARN, bs.FAIL)]


def test_the_scan_leaves_the_camera_free_for_typhon():
    """Close alone keeps a pylon device held (measured); the scan must also
    DestroyDevice, or Typhon's Connect would fail straight after."""
    pylon, genicam = _real()
    emulated(bs.scan(pylon, genicam))
    from council_core import cameras

    for info in cameras.BaslerBackend().discover():
        device = cameras.BaslerBackend().open(info)
        device.start()
        got = sum(device.read(1000) is not None for _ in range(3))
        device.close()
        assert got == 3, info.label


def test_a_camera_left_in_trigger_mode_is_reported_and_still_grabs():
    """As a previous program might leave it. Typhon's connect switches
    trigger mode off; the scan says so, and grabs as Typhon will."""
    pylon, genicam = _real()

    def armed(camera):
        camera.TriggerSelector.Value = "FrameStart"
        camera.TriggerMode.Value = "On"
        camera.TriggerSource.Value = "Line1"

    for cam in emulated(bs.scan(pylon, genicam, on_open=armed)):
        trig = next(c for c in cam.checks if c.label == "Trigger mode")
        assert trig.level == bs.WARN and "Line1" in trig.detail
        assert next(c for c in cam.checks if c.label == "Test grab").level == bs.PASS


def test_failed_frames_in_the_test_grab_are_reported():
    pylon, genicam = _real()

    def failing(camera):
        camera.ForceFailedBufferCount.Value = 3
        camera.ForceFailedBuffer.Execute()

    for cam in emulated(bs.scan(pylon, genicam, on_open=failing)):
        grab = next(c for c in cam.checks if c.label == "Test grab")
        assert grab.level in (bs.WARN, bs.FAIL) and "FAILED" in grab.detail


def test_a_slow_frame_rate_limit_is_warned_about():
    pylon, genicam = _real()

    def slow(camera):
        camera.AcquisitionFrameRateEnable.Value = True
        camera.AcquisitionFrameRate.Value = 0.5

    for cam in emulated(bs.scan(pylon, genicam, on_open=slow, test_grab=False)):
        rate = next(c for c in cam.checks if c.label == "Frame-rate limit")
        assert rate.level == bs.WARN and "dead" in rate.detail


def test_every_pixel_format_the_emulator_offers_is_classified():
    pylon, genicam = _real()
    for cam in emulated(bs.scan(pylon, genicam, test_grab=False)):
        formats = cam.capabilities["formats"]
        assert formats, "no pixel formats were read"
        assert formats.get("Mono8", ("",))[0] == "yes"
        assert all(v in ("yes", "warn", "no") for v, _ in formats.values())



def test_a_camera_that_errors_while_examined_stays_in_the_list():
    """Dropped with only a note, a camera the user can see on the desk is
    simply missing from the scan — found when a render showed none."""
    pylon, genicam = _real()

    def breaks(camera):
        raise RuntimeError("something odd about this camera")

    cams = emulated(bs.scan(pylon, genicam, on_open=breaks))
    for cam in cams:
        assert cam.verdict == bs.NO
        assert any(c.label == "Examining" and "something odd" in c.detail
                   for c in cam.checks)



# ======================================================================
# The PC: pnputil's XML (shaped exactly as Windows 11's, captured here)
# ======================================================================
def pnp_xml(*devices):
    body = "".join(devices) or "No devices were found on the system."
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<PnpUtil Version="10.0.26200" Command="/enum-devices">\n'
            f"{body}\n</PnpUtil>")


def pnp_device(instance, name, status="Started", props=None, problem=None):
    extra = f"<ProblemCode>{problem}</ProblemCode>" if problem else ""
    body = "".join(
        f'<Property Key="{k}" Type="UINT32"><Value>{v}</Value></Property>'
        for k, v in (props or {}).items())
    return (f'<Device InstanceId="{instance}"><DeviceDescription>{name}'
            f"</DeviceDescription><Status>{status}</Status>{extra}"
            f"<Properties>{body}</Properties></Device>")


def card(speed, width, top_speed=3, top_width=8):
    return pnp_device(
        "PCI\\VEN_1AE8&amp;DEV_0B52\\4&amp;1&amp;0", "Basler CXP-12 Interface Card 2C",
        props={"DEVPKEY_PciDevice_CurrentLinkSpeed": f"0x{speed:08x}",
               "DEVPKEY_PciDevice_CurrentLinkWidth": f"0x{width:08x}",
               "DEVPKEY_PciDevice_MaxLinkSpeed": f"0x{top_speed:08x}",
               "DEVPKEY_PciDevice_MaxLinkWidth": f"0x{top_width:08x}"})


def windows(problems=(), cards=()):
    def pnp(args):
        return pnp_xml(*(problems if "/problem" in args else cards))
    return pnp


def test_pnputil_xml_is_read_including_the_link_properties():
    got = bs.pnp_devices(pnp_xml(card(3, 4)))
    assert got[0]["name"] == "Basler CXP-12 Interface Card 2C"
    assert got[0]["id"].startswith("PCI\\VEN_1AE8&DEV_0B52")
    assert got[0]["props"]["DEVPKEY_PciDevice_CurrentLinkWidth"] == ["0x00000004"]
    assert bs.pnp_devices(pnp_xml()) == []


def test_a_card_in_a_slower_slot_than_itself_is_warned_about():
    slow = bs.pcie_check(bs.pnp_devices(pnp_xml(card(3, 4)))[0])
    assert slow.level == bs.WARN and "Gen3 x4" in slow.detail and "x8" in slow.fix
    assert bs.pcie_check(bs.pnp_devices(pnp_xml(card(3, 8)))[0]).level == bs.PASS
    assert bs.pcie_check({"name": "c", "props": {}}).level == bs.INFO


def test_a_basler_usb_camera_without_a_driver_is_found_although_pylon_cannot_see_it():
    """pylon lists nothing without the driver: without this the scan could
    only say "no camera found"."""
    usb = pnp_device("USB\\VID_2676&amp;PID_BA02\\40012345", "USB3 Vision Camera",
                     status="Problem", problem="28")
    other = pnp_device("USB\\VID_046D&amp;PID_C52B\\1", "a mouse", problem="28")
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)},
                          pnp=windows(problems=[usb, other]))
    bad = [n for n in report.notes if n.label == "Windows driver"]
    assert len(bad) == 1 and bad[0].level == bs.FAIL
    assert "code 28" in bad[0].detail and "USB Camera Driver" in bad[0].fix


def test_no_driver_problem_is_said_and_no_pnputil_is_not_a_failure():
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)}, pnp=windows())
    assert any(n.label == "Windows drivers" and n.level == bs.PASS for n in report.notes)
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)})
    assert any(n.label == "Windows drivers" and n.level == bs.INFO for n in report.notes)


def test_a_fitted_card_without_coaxpress_support_will_not_work():
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)},
                          pnp=windows(cards=[card(3, 8)]))
    note = next(n for n in report.notes if n.label == "CoaXPress support")
    assert note.level == bs.FAIL and "card is fitted" in note.detail
    assert any(n.label == "CoaXPress card" and n.level == bs.PASS for n in report.notes)


def test_camera_software_installed_since_the_app_started_asks_for_a_restart():
    report, _ = fake_scan({"BaslerUsb": FakeTl([], access=1)},
                          gentl_changed=lambda: r"c:\program files\basler\cxp")
    note = next(n for n in report.notes if n.label == "Restart needed")
    assert note.level == bs.WARN and "basler" in note.detail


def test_the_real_windows_check_runs_here_without_a_window_or_an_error():
    if sys.platform != "win32":
        pytest.skip("Windows only")
    xml = bs._pnputil(["/enum-devices", "/connected", "/problem"])
    assert xml is not None and "<PnpUtil" in xml
    bs.pnp_devices(xml)                                # parses
    bs._gentl_changed()                                # never raises


# ======================================================================
# CoaXPress rules (the boA5320-150cm is CXP-12 x2; no card here — fakes)
# ======================================================================
def test_the_cxp_link_is_held_against_the_one_the_camera_is_made_for():
    one_link = bs.cxp_link_check("CXP12_X1", "CXP12_X2", "boA5320-150cm")
    assert one_link.level == bs.WARN and "75.71" in one_link.detail
    assert "cable" in one_link.fix
    assert bs.cxp_link_check("CXP12_X2", "CXP12_X2", "boA5320-150cm").level == bs.PASS
    assert bs.cxp_link_check("CXP6_X2", "CXP12_X2", "x").level == bs.WARN
    assert bs.cxp_link_check("Auto", None, "x").level == bs.INFO


class FakeGen:
    @staticmethod
    def IsAvailable(node):
        return True

    IsReadable = IsAvailable


class FakeValue:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value


class FakeNodeMap:
    def __init__(self, **values):
        self.values = values

    def GetNode(self, name):
        return FakeValue(self.values[name]) if name in self.values else None


class FakeInterface:
    def __init__(self, nodemap):
        self.nodemap = nodemap
        self.closed = False

    def Open(self):
        pass

    def GetNodeMap(self):
        return self.nodemap

    def Close(self):
        self.closed = True


class CardTl(FakeTl):
    def __init__(self, nodemap):
        super().__init__([], access=1, interfaces=["CXP12-IC-2C"])
        self.itf = FakeInterface(nodemap)
        self.destroyed = False

    def CreateInterface(self, info):
        return self.itf

    def DestroyInterface(self, itf):
        self.destroyed = True


def test_a_card_that_cut_power_to_the_camera_says_so_and_is_released():
    tl = CardTl(FakeNodeMap(CxpPort0PowerState="Tripped", CxpPort1PowerState="On",
                            ExternalPowerPresent=False,
                            InterfaceApplet="Acq_SingleCXP12Area"))
    notes = bs._cxp_cards(tl, FakeGen, cameras_found=0)
    tripped = next(n for n in notes if n.level == bs.FAIL)
    assert "port(s) 0" in tripped.detail
    assert any("6-pin" in n.detail and n.level == bs.WARN for n in notes)
    assert any("applet Acq_SingleCXP12Area" in n.detail for n in notes)
    assert tl.itf.closed and tl.destroyed


def test_a_card_listing_simulated_cameras_is_warned_about():
    tl = CardTl(FakeNodeMap(DiscoveryMethod="EmulationDiscovery"))
    note = next(n for n in bs._cxp_cards(tl, FakeGen, 1) if n.level == bs.WARN)
    assert "SIMULATED" in note.detail


def test_a_card_with_no_nodes_says_nothing_rather_than_guessing():
    assert bs._cxp_cards(CardTl(FakeNodeMap()), FakeGen, 0) == []


def test_a_cxp_layer_reads_its_cards_in_the_scan():
    pylon = FakePylon({bs.CXP_CLASS: CardTl(FakeNodeMap(
        DiscoveryMethod="EmulationDiscovery"))})
    report = bs.scan(pylon, FakeGen, pnp=lambda a: None, gentl_changed=lambda: None)
    assert any("SIMULATED" in n.detail for n in report.notes)


# ======================================================================
# Cameras: vanished, Auto IP
# ======================================================================
def test_a_camera_unplugged_between_listing_and_checking_is_said():
    class Vanishing(FakeTl):
        def IsDeviceAccessibleInfo(self, info):
            raise RuntimeError("No device is available or no device contains "
                               "the provided device info properties")

    report, _ = fake_scan({"BaslerUsb": Vanishing(
        [FakeInfo(DeviceClass="BaslerUsb", ModelName="a2A", SerialNumber="7")],
        access=1)})
    free = next(c for c in one(report).checks if c.label == "Free")
    assert "unplugged" in free.detail


def test_an_automatic_gige_address_is_warned_about():
    report, _ = fake_scan({"BaslerGigE": FakeTl([FakeInfo(
        DeviceClass="BaslerGigE", ModelName="a2A1920-51gmBAS", SerialNumber="8",
        IpAddress="169.254.10.2", SubnetMask="255.255.0.0",
        Interface="169.254.10.1")], access=1)})
    ip = next(c for c in one(report).checks if c.label == "IP address")
    assert ip.level == bs.WARN and "persistent" in ip.fix


# ======================================================================
# Real emulator: the verification pass's findings
# ======================================================================
def test_a_slow_camera_does_not_hold_the_wizard():
    """Per-frame timeouts alone: 34 s for ONE camera at 0.3 fps (measured)."""
    import time as _time

    pylon, genicam = _real()

    def crawl(camera):
        camera.AcquisitionFrameRateEnable.Value = True
        camera.AcquisitionFrameRate.Value = 0.3

    began = _time.monotonic()
    cams = emulated(bs.scan(pylon, genicam, on_open=crawl))
    assert _time.monotonic() - began < 3 * len(cams) + 2
    for cam in cams:
        grab = next(c for c in cam.checks if c.label == "Test grab")
        assert grab.level == bs.WARN and "skipped" in grab.detail


def test_the_burst_stops_at_its_time_budget():
    pylon, genicam = _real()

    def one_a_second(camera):
        camera.AcquisitionFrameRateEnable.Value = True
        camera.AcquisitionFrameRate.Value = 1.0

    for cam in emulated(bs.scan(pylon, genicam, on_open=one_a_second, burst=10)):
        grab = next(c for c in cam.checks if c.label == "Test grab")
        assert grab.level == bs.PASS, grab.detail
        frames = int(grab.detail.split(" frame")[0])
        assert 1 <= frames <= 4, grab.detail


def test_exposure_left_to_a_trigger_or_to_the_camera_is_reported():
    pylon, genicam = _real()

    def odd(camera):
        camera.ExposureMode.Value = "TriggerWidth"
        camera.ExposureAuto.Value = "Continuous"

    for cam in emulated(bs.scan(pylon, genicam, on_open=odd, test_grab=False)):
        mode = next(c for c in cam.checks if c.label == "Exposure mode")
        assert mode.level == bs.WARN and "TriggerWidth" in mode.detail
        auto = next(c for c in cam.checks if c.label == "Automatic exposure")
        assert auto.level == bs.INFO


@pytest.mark.parametrize("fmt, level", [
    ("Mono8", "yes"), ("Mono12", "yes"), ("RGB8Packed", "yes"),
    ("BGR8Packed", "yes"),            # Typhon swaps it (test_basler_real)
    ("BGRA8Packed", "warn"),          # converted (it used to end the capture)
    ("RGB16Packed", "warn"),
    ("BayerRG8", "warn"),             # a grey mosaic, not colour
])
def test_pixel_formats_are_judged_by_what_typhon_actually_saves(fmt, level):
    pylon, _ = _real()
    assert bs.saveable(pylon, fmt)[0] == level, bs.saveable(pylon, fmt)
