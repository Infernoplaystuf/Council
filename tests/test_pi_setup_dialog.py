"""The "Set up a Pi" dialog (council_qt/tabs/pi_setup_dialog.py), offscreen,
with fake actions: no disk is listed for real, nothing is erased, no UAC
prompt, nothing goes online."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core.pi_setup import disks as dk  # noqa: E402
from council_core.pi_setup import images as im  # noqa: E402
from council_core.pi_setup import remote  # noqa: E402
from council_core.pi_setup import setup as su  # noqa: E402
from council_qt.tabs.pi_setup_dialog import PiSetupDialog  # noqa: E402

from tests.test_pi_disks import REAL, SD_CARD  # noqa: E402
from tests.test_pi_flash import CATALOG  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def drive(qapp, dlg, seconds=10):
    end = time.time() + seconds
    while (dlg._busy or any(t.name.startswith("pi-setup") and t.is_alive()
                            for t in threading.enumerate())) and time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


class FakeActions:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.prepared = None
        self.started = None
        self.status = None
        self.existing_calls = []
        self.finished = []
        self._pending = []
        self.image_file = tmp_path / "2026-10-06-raspios-trixie-arm64-lite.img.xz"
        self.image_file.write_bytes(b"xz")

    def list_disks(self):
        return [dk.judge(d, []) for d in dk.parse(REAL + [SD_CARD])]

    def catalog(self):
        return im.parse_catalog(CATALOG)

    def fetch_catalog(self):
        return self.catalog()

    def cached_path(self, img):
        return self.image_file

    def download(self, img, on_progress, cancelled):
        return self.image_file

    def prepare(self, **kw):
        self.prepared = kw
        job_dir = self.tmp / "job"
        job_dir.mkdir(exist_ok=True)
        item = su.Pending(id="job", hostname=kw["cfg"].hostname,
                          username=kw["cfg"].username, host_public="ssh-ed25519 AAAA",
                          model=kw["model"])
        self._pending = [item]
        return {"job": job_dir / "job.json", "pending": item}

    def start_writer(self, job):
        self.started = job

    def read_status(self, job_dir):
        return self.status

    def pending(self):
        return list(self._pending)

    def setup_existing(self, **kw):
        self.existing_calls.append(kw)
        if kw.get("approved_fingerprint") is None:
            raise remote.HostKeyUnknown(kw["host"], "SHA256:abc123")
        return su.Outcome(True, kw["host"], kw["name"], "llama3.1:8b", "pi-a is ready")

    def finish(self, item, **kw):
        self.finished.append(item)
        self._pending = []
        return su.Outcome(True, "192.168.1.80", item.hostname, item.model, "ready")


@pytest.fixture
def dlg(qapp, tmp_path):
    d = PiSetupDialog(actions=FakeActions(tmp_path))
    yield d
    drive(qapp, d)
    d.done(0)
    d.deleteLater()
    qapp.processEvents()


def test_this_pcs_disks_cannot_be_picked(dlg):
    dlg.on_new()
    texts = [dlg.disk_list.item(i).text() for i in range(dlg.disk_list.count())]
    assert any("Seagate Portable" in t and "can't use" in t for t in texts)
    dlg.disk_list.setCurrentRow(1)                     # the Seagate
    assert dlg._current_disk() is None and not dlg.card_next.isEnabled()


def test_next_needs_the_exact_confirm_code(dlg):
    dlg.on_new()
    dlg.disk_list.setCurrentRow(2)
    code = dlg._current_disk().confirm_code
    assert code in dlg.confirm_label.text()
    dlg.confirm_edit.setText("yes erase it")
    assert not dlg.card_next.isEnabled()
    dlg.confirm_edit.setText(code)
    assert dlg.card_next.isEnabled()


def _to_settings(dlg):
    dlg.on_new()
    dlg.disk_list.setCurrentRow(2)
    dlg.confirm_edit.setText(dlg._current_disk().confirm_code)
    dlg.card_next.click()
    dlg.s_host.setText("council-pi-2")
    dlg.s_pass.setText("correct-horse-42")
    dlg.s_pass2.setText("correct-horse-42")
    dlg.s_ssid.setText("Home")
    dlg.s_wpass.setText("wifi-pass-123")


def test_mismatched_passwords_write_nothing(dlg):
    _to_settings(dlg)
    dlg.s_pass2.setText("different-42")
    dlg.on_write()
    assert dlg.actions.prepared is None and "differ" in dlg.log.toPlainText()


def test_write_then_find_the_pi(qapp, dlg):
    _to_settings(dlg)
    dlg.write_btn.click()
    acts = dlg.actions
    assert acts.prepared["typed_confirm"] == acts.list_disks()[2].confirm_code
    assert acts.prepared["init_format"] == "cloudinit-rpi"
    assert acts.prepared["model"] in ("llama3.1:8b",)
    assert acts.started is not None
    assert dlg.s_pass.text() == "" and dlg.s_wpass.text() == ""
    acts.status = {"phase": "writing", "done": 500, "total": 1000, "message": "Writing"}
    dlg._poll_writer()
    assert dlg.w_bar.value() == 500 and not dlg.find_btn.isEnabled()
    acts.status = {"phase": "done", "message": "The card is ready."}
    dlg._poll_writer()
    assert dlg.find_btn.isEnabled()
    dlg.find_btn.click()
    drive(qapp, dlg)
    assert acts.finished and "✓ ready" in dlg.log.toPlainText()


def test_writer_error_is_shown_and_find_stays_off(dlg):
    _to_settings(dlg)
    dlg.write_btn.click()
    dlg.actions.status = {"phase": "error", "message": "refusing to erase disk 2: swapped"}
    dlg._poll_writer()
    assert not dlg.find_btn.isEnabled() and "✗ refusing" in dlg.log.toPlainText()


def test_existing_pi_asks_about_its_key(qapp, dlg):
    dlg.ex_host.setText("192.168.1.252")
    dlg.ex_user.setText("pi")
    dlg.ex_pass.setText("hunter22")
    dlg.on_setup_existing()
    drive(qapp, dlg)
    assert dlg.fp_btn.isVisible() or not dlg.fp_btn.isHidden()
    assert "SHA256:abc123" in dlg.fp_label.text()
    dlg.fp_btn.click()
    drive(qapp, dlg)
    assert dlg.actions.existing_calls[-1]["approved_fingerprint"] == "SHA256:abc123"
    assert "✓ pi-a is ready" in dlg.log.toPlainText()
    assert dlg.ex_pass.text() == ""


def test_pending_pi_can_be_finished_later(qapp, tmp_path):
    acts = FakeActions(tmp_path)
    acts._pending = [su.Pending(id="j9", hostname="council-pi-9", username="council",
                                host_public="ssh-ed25519 AAAA", model="llama3.2:3b")]
    d = PiSetupDialog(actions=acts)
    assert d.finish_box.count() == 1
    d.on_finish_pending()
    assert d.find_btn.isEnabled()
    d.on_find()
    drive(qapp, d)
    assert acts.finished[0].hostname == "council-pi-9"
    d.done(0)
    d.deleteLater()
    qapp.processEvents()
