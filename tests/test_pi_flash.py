"""Imaging a Pi card: the image list and download, the writer's read-back
check, and the elevated helper's refusals — all against FILES standing in for
disks. No test erases, writes or even opens a real disk, and none goes online
(the opener is replaced)."""
from __future__ import annotations

import hashlib
import io
import json
import lzma
from pathlib import Path

import pytest

from council_core.pi_setup import disks as dk
from council_core.pi_setup import firstboot as fb
from council_core.pi_setup import flash_helper as fh
from council_core.pi_setup import images as im
from council_core.pi_setup import writer as wr

CATALOG = {"imager": {}, "os_list": [
    {"name": "Raspberry Pi OS (other)", "subitems": [
        {"name": "Raspberry Pi OS Lite (64-bit)",
         "url": "https://downloads.raspberrypi.com/raspios_lite_arm64/images/x/2026-10-06-raspios-trixie-arm64-lite.img.xz",
         "extract_size": 3078619136, "extract_sha256": "B8A393DC" + "0" * 56,
         "image_download_size": 550466056, "release_date": "2026-10-06",
         "init_format": "cloudinit-rpi", "devices": ["pi5-64bit", "pi4-64bit"]},
        {"name": "Raspberry Pi OS Lite (evil mirror)",
         "url": "https://example.com/evil.img.xz", "extract_sha256": "0" * 64}]},
    {"name": "Ubuntu", "url": "https://cdimage.ubuntu.com/u.img.xz", "extract_sha256": "1" * 64},
]}


def make_image(tmp_path, size=3 * 1024 * 1024 + 100, xz=True):
    data = bytes((i * 7) % 251 for i in range(size))
    p = tmp_path / ("img.img.xz" if xz else "img.img")
    p.write_bytes(lzma.compress(data) if xz else data)
    return p, data, hashlib.sha256(data).hexdigest()


# ── the list ─────────────────────────────────────────────────────────────
def test_catalog_keeps_only_official_pi_os():
    imgs = im.parse_catalog(CATALOG)
    assert [i.name for i in imgs] == ["Raspberry Pi OS Lite (64-bit)"]
    assert imgs[0].init_format == "cloudinit-rpi"
    assert imgs[0].extract_sha256 == "b8a393dc" + "0" * 56
    assert im.default_image(imgs) is imgs[0]


@pytest.mark.parametrize("url", ["http://downloads.raspberrypi.com/x.img.xz",
                                 "https://downloads.raspberrypi.com.evil.com/x.img.xz",
                                 "https://example.com/x.img.xz"])
def test_only_the_official_host_is_fetched(url):
    with pytest.raises(ValueError):
        im._official(url)


class FakeResp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_checks_the_published_checksum(tmp_path):
    img = im.parse_catalog(CATALOG)[0]
    blob = b"pretend image bytes" * 1000
    good = hashlib.sha256(blob).hexdigest()
    served = {img.url: blob, img.url + ".sha256": f"{good}  {img.filename}\n".encode()}
    path = im.download(img, tmp_path, opener=lambda u, timeout=60: FakeResp(served[u]))
    assert path.read_bytes() == blob
    # a corrupted download is kept aside, never used
    served[img.url] = blob[:-1] + b"X"
    path.unlink()
    with pytest.raises(ValueError):
        im.download(img, tmp_path, opener=lambda u, timeout=60: FakeResp(served[u]))
    assert not path.exists() and path.with_suffix(".xz.bad").exists()


def test_local_image_must_look_like_one(tmp_path):
    (tmp_path / "a.iso").write_bytes(b"x")
    with pytest.raises(ValueError):
        im.local_image(tmp_path / "a.iso")
    (tmp_path / "2026-10-06-raspios-trixie-arm64-lite.img.xz").write_bytes(b"x")
    p = im.local_image(tmp_path / "2026-10-06-raspios-trixie-arm64-lite.img.xz")
    assert im.match_local(p, im.parse_catalog(CATALOG)).init_format == "cloudinit-rpi"


# ── the writer ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("xz", [True, False])
def test_write_then_read_back(tmp_path, xz):
    src, data, sha = make_image(tmp_path, xz=xz)
    disk = tmp_path / "card.bin"
    disk.write_bytes(b"\xff" * (8 * 1024 * 1024))
    with open(disk, "r+b") as t:
        out = wr.write_image(src, t, capacity=disk.stat().st_size, expected_sha256=sha)
    assert out == {"bytes": len(data), "sha256": sha}
    assert disk.read_bytes()[:len(data)] == data


def test_wrong_image_checksum_fails(tmp_path):
    src, _data, _sha = make_image(tmp_path)
    disk = tmp_path / "card.bin"
    disk.write_bytes(b"\0" * (8 * 1024 * 1024))
    with open(disk, "r+b") as t, pytest.raises(wr.WriteFailed, match="official checksum"):
        wr.write_image(src, t, capacity=8 * 1024 * 1024, expected_sha256="0" * 64)


def test_image_bigger_than_the_card_is_refused_before_writing(tmp_path):
    src, data, _ = make_image(tmp_path)
    disk = tmp_path / "card.bin"
    disk.write_bytes(b"\xff" * 1024)
    with open(disk, "r+b") as t, pytest.raises(wr.WriteFailed):
        wr.write_image(src, t, capacity=1024, expected_size=len(data))
    assert disk.read_bytes() == b"\xff" * 1024


class Flaky(io.BytesIO):
    """A card that silently loses bits — the read-back must catch it."""
    def read(self, n=-1):
        b = bytearray(super().read(n))
        if b:
            b[0] ^= 1
        return bytes(b)


def test_read_back_catches_a_bad_card(tmp_path):
    src, _data, sha = make_image(tmp_path)
    with pytest.raises(wr.WriteFailed, match="read back"):
        wr.write_image(src, Flaky(), capacity=10 ** 9, expected_sha256=sha)


# ── the elevated helper ──────────────────────────────────────────────────
SD = {"Number": 2, "FriendlyName": "Generic STORAGE DEVICE", "SerialNumber": "000000000820",
      "UniqueId": "USBSTOR-X", "BusType": "USB", "Size": 8 * 1024 * 1024,
      "IsSystem": False, "IsBoot": False, "IsReadOnly": False, "Partitions": []}
SEAGATE = {**SD, "Number": 1, "FriendlyName": "Seagate Portable", "UniqueId": "3E41",
           "Size": 5000981077504}


class FakeOps:
    def __init__(self, tmp_path, disks_json, boot_has_cloudinit=True):
        self.tmp = tmp_path
        self.disks_json = disks_json
        self.cleared = []
        self.card = tmp_path / "card.bin"
        self.card.write_bytes(b"\xff" * SD["Size"])
        self.boot = tmp_path / "bootfs"
        self.boot.mkdir(exist_ok=True)
        (self.boot / "cmdline.txt").write_text("console=tty1 rootwait\n", encoding="utf-8")
        if boot_has_cloudinit:
            (self.boot / "meta-data").write_text("# placeholder\n", encoding="utf-8")

    def list_disks(self):
        return [dk.judge(d, []) for d in dk.parse(self.disks_json)]

    def clear(self, n):
        self.cleared.append(n)

    def open_target(self, n):
        assert n == 2, "only the confirmed card may be opened"
        return open(self.card, "r+b")

    def mount_boot(self, n):
        return self.boot

    def flush(self, boot):
        pass


def _job(tmp_path, disk_json, image, sha):
    disk = dk.parse([disk_json])[0]
    files = fb.build(fb.FirstBoot(hostname="council-pi-1", username="council",
                                  password="correct-horse-42", wifi_ssid="Home",
                                  wifi_password="wifi-pass-123", wifi_country="US"),
                     fb.CLOUDINIT)
    return fh.write_job(tmp_path / "job", disk=disk, image=image,
                        init_format=fb.CLOUDINIT, firstboot_files=files,
                        extract_sha256=sha), files


def test_helper_writes_the_confirmed_card(tmp_path):
    src, data, sha = make_image(tmp_path)
    job, files = _job(tmp_path, SD, src, sha)
    ops = FakeOps(tmp_path, [SEAGATE, SD])
    final = fh.run(job, ops)
    assert final["ok"], final
    assert ops.cleared == [2]
    assert ops.card.read_bytes()[:len(data)] == data
    assert fb.verify(ops.boot, files, fb.CLOUDINIT) == []
    assert not job.exists()                       # the WPA key does not linger
    assert fh.read_status(job.parent)["phase"] == "done"


def test_helper_refuses_a_swapped_card(tmp_path):
    src, _data, sha = make_image(tmp_path)
    job, _ = _job(tmp_path, SD, src, sha)
    ops = FakeOps(tmp_path, [{**SD, "SerialNumber": "SOMEONE-ELSES"}])
    final = fh.run(job, ops)
    assert not final["ok"] and final["refused"] and "nothing was erased" in final["message"]
    assert ops.cleared == [] and ops.card.read_bytes() == b"\xff" * SD["Size"]


def test_helper_refuses_a_disk_that_is_no_longer_eligible(tmp_path):
    src, _data, sha = make_image(tmp_path)
    big = {**SD, "Size": 5000981077504}
    job, _ = _job(tmp_path, big, src, sha)
    ops = FakeOps(tmp_path, [big])
    final = fh.run(job, ops)
    assert not final["ok"] and final["refused"] and "larger than any Pi card" in final["message"]
    assert ops.cleared == []


def test_helper_reports_settings_that_did_not_stick(tmp_path):
    src, _data, sha = make_image(tmp_path)
    job, _ = _job(tmp_path, SD, src, sha)
    ops = FakeOps(tmp_path, [SD])
    real_apply = fb.apply

    def lossy_apply(boot, files, fmt):
        out = real_apply(boot, files, fmt)
        (Path(boot) / "network-config").write_text("# lost\n", encoding="utf-8")
        return out
    fb.apply, saved = lossy_apply, fb.apply
    try:
        final = fh.run(job, ops)
    finally:
        fb.apply = saved
    assert not final["ok"] and "did not stick" in final["message"]


def test_cancel_stops_the_write(tmp_path):
    src, _data, sha = make_image(tmp_path)
    job, _ = _job(tmp_path, SD, src, sha)
    (job.parent / "cancel").write_text("", encoding="utf-8")
    final = fh.run(job, FakeOps(tmp_path, [SD]))
    assert not final["ok"] and "cancelled" in final["message"]


# ── review fixes (merge of knowledge-graph into qt-migration) ─────────────
def _argv(command_line: str):
    """How a Windows program splits its command line (CommandLineToArgvW)."""
    import ctypes
    from ctypes import wintypes
    f = ctypes.windll.shell32.CommandLineToArgvW
    f.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    f.restype = ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int()
    arr = f(command_line, ctypes.byref(n))
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(arr)


def test_the_elevated_launch_passes_paths_with_spaces_and_apostrophes_whole(
        tmp_path, monkeypatch):
    # Start-Process joins an -ArgumentList ARRAY with spaces and quotes
    # nothing: a profile 'C:\Users\John Smith' reached the helper as two
    # arguments, and an apostrophe in the interpreter's path ('O'Brien')
    # broke the -FilePath string. PowerShell itself evaluates the command
    # here, with Start-Process replaced by a function that only prints what
    # it was given - nothing is started, nothing is elevated.
    import base64
    import shutil
    import subprocess
    import sys
    if sys.platform != "win32" or not shutil.which("powershell"):
        pytest.skip("Windows PowerShell only")
    job = tmp_path / "John Smith's PC" / "jobs" / "1c0e8a4e-7a43-4e0a-9d43-2b8f0c1a2b3c" / "job.json"
    fake_python = r"C:\Users\O'Brien Smith\miniconda3\envs\council\python.exe"
    monkeypatch.setattr(sys, "executable", fake_python)
    scripts = []
    monkeypatch.setattr(fh, "_ps", lambda script, timeout=120: scripts.append(script) or "")
    fh.start_elevated(job)
    assert scripts[0].startswith("Start-Process ")
    # Renamed so the test's desktop guard does not take it for an opener;
    # the name does not change how PowerShell parses the arguments.
    script = "Show-StartArgs " + scripts[0][len("Start-Process "):]
    # One line per token it was given; an array is joined with spaces, as
    # Start-Process joins an -ArgumentList array.
    shim = "function Show-StartArgs { foreach ($a in $args) { Write-Output \"$a\" } }\n"
    enc = base64.b64encode((shim + script).encode("utf-16-le")).decode("ascii")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, text=True, timeout=120)
    # (stderr may carry a CLIXML progress record; only errors count)
    assert out.returncode == 0 and 'S="Error"' not in out.stderr, out.stderr
    toks = out.stdout.splitlines()
    got = {toks[i].rstrip(":").lower(): toks[i + 1] for i in range(0, len(toks) - 1, 2)}
    assert got["-filepath"] == fake_python and got["-verb"] == "RunAs"
    assert Path(got["-workingdirectory"]) == Path(fh.__file__).resolve().parents[2]
    assert _argv('"x.exe" ' + got["-argumentlist"])[1:] == [
        "-m", "council_core.pi_setup.flash_helper", str(job)]


def test_the_helper_refuses_a_path_that_is_not_a_job_and_writes_nothing(tmp_path, monkeypatch):
    # A mangled path ('C:\Users\John') made the elevated helper write
    # status.json as administrator into the parent folder.
    ran = []
    monkeypatch.setattr(fh, "run", lambda p, ops=None: ran.append(p) or {"ok": True})
    (tmp_path / "Users").mkdir()
    before = sorted(tmp_path.rglob("*"))
    for bad in (tmp_path / "Users" / "John", tmp_path / "Users" / "job.json",
                tmp_path / "jobs" / "not-a-uuid" / "job.json"):
        assert fh.main(["flash_helper", str(bad)]) != 0
    assert fh.main(["flash_helper"]) != 0
    assert ran == [] and sorted(tmp_path.rglob("*")) == before
    good = tmp_path / "pi_setup" / "jobs" / "1c0e8a4e-7a43-4e0a-9d43-2b8f0c1a2b3c" / "job.json"
    good.parent.mkdir(parents=True)
    good.write_text("{}", encoding="utf-8")
    assert fh.main(["flash_helper", str(good)]) == 0 and ran == [good]


def test_first_boot_files_must_be_known_names(tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "cmdline.txt").write_text("console=tty1\n", encoding="utf-8")
    for bad in ("C:evil.txt", "autorun.inf", "user-data:stream"):
        with pytest.raises(ValueError):
            fb.apply(boot, {bad: "x"}, fb.CLOUDINIT)
    assert sorted(p.name for p in boot.iterdir()) == ["cmdline.txt"]
