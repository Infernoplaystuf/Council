"""
council_qt.tabs.pi_setup_dialog — "Set up a Pi": an existing Pi, or a new one
from a blank card. All logic is council_core.pi_setup; this dialog collects
choices, runs the slow parts on "pi-setup-*" workers, and polls the elevated
card writer's status file.

The erase step can only be reached with an ELIGIBLE card selected (disks.py
refuses system/boot disks, non-removable buses, anything over 256 GB, and any
disk holding Council or Windows folders) AND its confirm code typed exactly;
the elevated helper then re-checks the same disk before touching it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QProgressBar, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from council_core.pi_setup import disks as dk
from council_core.pi_setup import firstboot as fb
from council_core.pi_setup import flash_helper, images, pi_models, remote
from council_core.pi_setup import setup as su

from ..view import ViewHelpers, amp

RAM_CHOICES = (("Pi with 8 GB", 8), ("Pi with 16 GB", 16), ("Pi with 4 GB", 4),
               ("Pi with 2 GB", 2))


class PiSetupActions:
    """Everything the dialog asks of the system — replaced in tests."""

    def __init__(self, vault_dir: Path):
        self.vault = Path(vault_dir)

    def list_disks(self) -> List[dk.Disk]:
        return dk.list_disks()

    def catalog(self) -> List[images.OsImage]:
        return images.cached_catalog()

    def fetch_catalog(self) -> List[images.OsImage]:
        return images.fetch_catalog()

    def cached_path(self, img: images.OsImage) -> Optional[Path]:
        p = images.cache_dir() / img.filename
        return p if p.exists() else None

    def download(self, img, on_progress, cancelled) -> Path:
        return images.download(img, on_progress=on_progress, cancelled=cancelled)

    def prepare(self, **kw) -> Dict[str, Any]:
        return su.prepare_new_pi(**kw)

    def start_writer(self, job: Path) -> None:
        flash_helper.start_elevated(job)

    def read_status(self, job_dir: Path):
        return flash_helper.read_status(job_dir)

    def pending(self):
        return su.pending()

    def setup_existing(self, **kw):
        return su.setup_existing(self.vault, **kw)

    def finish(self, item, **kw):
        return su.finish_new_pi(self.vault, item, **kw)

    def abandon(self, job: Path, item) -> None:
        su.abandon_job(job, item.id if item is not None else None)

    def sweep(self) -> List[str]:
        return su.sweep_jobs()


class PiSetupDialog(ViewHelpers, QDialog):

    #: No status from the card writer this long after it was started (UAC
    #: approved) means it never ran — it writes one within a second.
    WRITER_START_TIMEOUT_S = 120

    def __init__(self, parent=None, actions: Optional[PiSetupActions] = None,
                 vault_dir: Optional[Path] = None):
        super().__init__(parent)
        self.setWindowTitle("Set up a Raspberry Pi")
        self.resize(760, 620)
        self.bridge = getattr(parent, "bridge", None)
        if actions is None:
            from council_core import paths
            actions = PiSetupActions(Path(vault_dir or paths.vault_dir()))
        self.actions = actions
        self._busy = False
        self._disks: List[dk.Disk] = []
        self._images: List[images.OsImage] = []
        self._image_path: Optional[Path] = None
        #: The list entry ``_image_path`` IS (its checksum and first-boot
        #: format), or None for a file that is not in the list. Tied to the
        #: file, not to the combo box: the combo's entry used to be sent with
        #: whatever file was chosen.
        self._image_entry: Optional[images.OsImage] = None
        self._downloading: Optional[images.OsImage] = None
        self._writer_started = 0.0
        self._job: Optional[Path] = None
        self._pending: Optional[su.Pending] = None
        self._approved_fp: Optional[str] = None
        self._cancel = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll_writer)

        outer = QVBoxLayout(self)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(170)
        outer.addWidget(self.log)
        # Card jobs that never ran or failed: their job.json (secrets) and
        # their "finish this Pi" entries go before the start page lists them.
        try:
            gone = self.actions.sweep()
        except Exception as exc:                          # noqa: BLE001
            gone = []
            self.say(f"Could not tidy unfinished card jobs: {exc}")
        if gone:
            self.say(f"Removed {len(gone)} card job(s) that never wrote a card.")
        self.page_start = self._build_start()
        self.page_existing = self._build_existing()
        self.page_card = self._build_card()
        self.page_settings = self._build_settings()
        self.page_write = self._build_write()
        for p in (self.page_start, self.page_existing, self.page_card,
                  self.page_settings, self.page_write):
            self.pages.addWidget(p)

    # ── helpers ──
    def say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _say_from_worker(self, text: str) -> None:
        self._to_ui(self.say, text)

    def _work(self, name: str, fn, done) -> None:
        self._busy = True

        def run():
            try:
                res = fn()
                self._to_ui(done, res, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(done, None, exc)
            finally:
                self._to_ui(setattr, self, "_busy", False)
        threading.Thread(target=run, name="pi-setup-worker", daemon=True).start()

    # ── page: start ──
    def _build_start(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("What are you setting up?"))
        self.existing_btn = self._button(lay, "A Pi that is already set up (I know its "
                                              "address, username and password)",
                                         lambda: self.pages.setCurrentWidget(self.page_existing))
        self.new_btn = self._button(lay, "A new Pi — erase its SD card and install "
                                         "Raspberry Pi OS", self.on_new)
        self.finish_box = QComboBox()
        self.finish_btn = QPushButton(amp("Finish setting up a Pi whose card was written"))
        self.finish_btn.clicked.connect(self.on_finish_pending)
        row = QHBoxLayout()
        row.addWidget(self.finish_box, 1)
        row.addWidget(self.finish_btn)
        lay.addLayout(row)
        lay.addStretch(1)
        self._refresh_pending()
        return w

    def _refresh_pending(self) -> None:
        items = self.actions.pending()
        self.finish_box.clear()
        for p in items:
            self.finish_box.addItem(f"{p.hostname} ({p.model})", p.id)
        self.finish_box.setVisible(bool(items))
        self.finish_btn.setVisible(bool(items))

    # ── page: existing Pi ──
    def _build_existing(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ex_host = QLineEdit()
        self.ex_host.setPlaceholderText("192.168.1.77 or raspberrypi.local")
        self.ex_user = QLineEdit("pi")
        self.ex_pass = QLineEdit()
        self.ex_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.ex_name = QLineEdit()
        self.ex_name.setPlaceholderText("a name for this Pi in the Council")
        self.ex_model = QComboBox()
        self.ex_model.addItem("Recommended for its memory", "")
        for m in dict.fromkeys(sum(pi_models.BY_RAM.values(), [])):
            self.ex_model.addItem(m, m)
        form.addRow("Address", self.ex_host)
        form.addRow("Username", self.ex_user)
        form.addRow("Password (used once, not saved)", self.ex_pass)
        form.addRow("Name", self.ex_name)
        form.addRow("Model", self.ex_model)
        self.fp_label = QLabel("")
        self.fp_label.setWordWrap(True)
        self.fp_btn = QPushButton(amp("Yes, this is my Pi — continue"))
        self.fp_btn.clicked.connect(self.on_trust_fingerprint)
        self.fp_btn.setVisible(False)
        form.addRow(self.fp_label)
        form.addRow(self.fp_btn)
        row = QHBoxLayout()
        self._button(row, "← Back", lambda: self.pages.setCurrentWidget(self.page_start))
        self.ex_go = self._button(row, "Set it up", self.on_setup_existing)
        form.addRow(row)
        return w

    def on_setup_existing(self) -> None:
        if self._busy:
            return
        host, user = self.ex_host.text().strip(), self.ex_user.text().strip()
        pw, name = self.ex_pass.text(), (self.ex_name.text().strip() or self.ex_host.text().strip())
        if not (host and user and pw):
            self.say("Enter the Pi's address, username and password.")
            return
        model = self.ex_model.currentData() or None
        fp = self._approved_fp
        self.fp_btn.setVisible(False)
        self._work("existing", lambda: self.actions.setup_existing(
            host=host, username=user, password=pw, name=name, model=model,
            approved_fingerprint=fp, say=self._say_from_worker,
            on_line=self._say_from_worker), self._existing_done)

    def _existing_done(self, out, exc) -> None:
        if isinstance(exc, remote.HostKeyUnknown):
            self._pending_fp = exc.fingerprint
            self.fp_label.setText(
                f"This is the first time the Council has seen {exc.host}. Its SSH key "
                f"fingerprint is\n{exc.fingerprint}\nIf you can, check it on the Pi with: "
                "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")
            self.fp_btn.setVisible(True)
            return
        if exc is not None:
            self.say(f"Could not set it up: {exc}")
            return
        self.ex_pass.clear()
        self.say(("✓ " if out.ok else "✗ ") + out.message)

    def on_trust_fingerprint(self) -> None:
        self._approved_fp = getattr(self, "_pending_fp", None)
        self.on_setup_existing()

    # ── page: image + card ──
    def _build_card(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("1. Raspberry Pi OS image"))
        row = QHBoxLayout()
        self.image_box = QComboBox()
        self.image_box.currentIndexChanged.connect(lambda _i: self._image_changed())
        row.addWidget(self.image_box, 1)
        self.list_btn = self._button(row, "Get the list (online)", self.on_fetch_catalog)
        self.dl_btn = self._button(row, "Download it (online)", self.on_download)
        self._button(row, "Use a file I have…", self.on_pick_file)
        lay.addLayout(row)
        self.image_note = QLabel("")
        self.image_note.setWordWrap(True)
        lay.addWidget(self.image_note)
        self.dl_bar = QProgressBar()
        self.dl_bar.setVisible(False)
        lay.addWidget(self.dl_bar)

        lay.addWidget(QLabel("2. The SD card to ERASE (put it in this PC's card reader)"))
        self.disk_list = QListWidget()
        self.disk_list.currentRowChanged.connect(lambda _r: self._disk_changed())
        lay.addWidget(self.disk_list, 1)
        row2 = QHBoxLayout()
        self._button(row2, "⟳ Look again", self.refresh_disks)
        self.confirm_label = QLabel("")
        row2.addWidget(self.confirm_label, 1)
        lay.addLayout(row2)
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setPlaceholderText("type the confirm code shown above")
        self.confirm_edit.textChanged.connect(lambda _t: self._update_card_next())
        lay.addWidget(self.confirm_edit)
        nav = QHBoxLayout()
        self._button(nav, "← Back", lambda: self.pages.setCurrentWidget(self.page_start))
        self.card_next = self._button(nav, "Next: the Pi's settings →",
                                      lambda: self.pages.setCurrentWidget(self.page_settings))
        lay.addLayout(nav)
        return w

    def on_new(self) -> None:
        self._images = self.actions.catalog()
        self._fill_images()
        self.refresh_disks()
        self.pages.setCurrentWidget(self.page_card)

    def _fill_images(self) -> None:
        self.image_box.blockSignals(True)
        self.image_box.clear()
        default = images.default_image(self._images)
        for i, img in enumerate(self._images):
            self.image_box.addItem(f"{img.name} — {img.release_date}", i)
        if default is not None:
            self.image_box.setCurrentIndex(self._images.index(default))
        self.image_box.blockSignals(False)
        self._image_changed()

    def _current_image(self) -> Optional[images.OsImage]:
        i = self.image_box.currentData()
        return self._images[i] if i is not None and 0 <= i < len(self._images) else None

    def _image_changed(self) -> None:
        """The user picked a list entry: the image is now THAT entry's file
        (if it was downloaded), never a file chosen for another entry —
        switching entries used to keep the first file and send it with the
        new entry's checksum and first-boot format."""
        img = self._current_image()
        self._image_entry = img
        self._image_path = self.actions.cached_path(img) if img is not None else None
        self._show_image()

    def _show_image(self) -> None:
        img, path = self._image_entry, self._image_path
        current = self._current_image()
        self.dl_btn.setEnabled(current is not None
                               and self.actions.cached_path(current) is None)
        if path is not None and img is None:
            fmt = images.release_format(path.name)
            self.image_note.setText(
                f"Ready: {path} · not in the official list, so it is checked only by "
                "reading the card back · " + (f"first-boot format {fmt} (from its name)"
                                              if fmt else "its first-boot format cannot "
                                              "be told from its name"))
        elif img is None:
            self.image_note.setText("No image list yet. Get the list (the only time this "
                                    "goes online), or use a file you have.")
        else:
            self.image_note.setText(
                f"{'Ready: ' + str(path) if path else 'Not downloaded yet'}"
                f" · {img.download_size / 1e6:.0f} MB download · first-boot format "
                f"{img.init_format}")
        self._update_card_next()

    def on_fetch_catalog(self) -> None:
        if self._busy:
            return
        self.say("Getting the official image list from downloads.raspberrypi.com…")
        self._work("catalog", self.actions.fetch_catalog, self._catalog_done)

    def _catalog_done(self, imgs, exc) -> None:
        if exc is not None:
            self.say(f"Could not get the list: {exc}")
            return
        own = self._image_path if self._image_entry is None else None
        self._images = imgs
        self._fill_images()
        self.say(f"{len(imgs)} Raspberry Pi OS images listed.")
        if own is not None:
            self.use_file(own)              # keep the user's file; match it to the list

    def on_download(self) -> None:
        img = self._current_image()
        if self._busy or img is None:
            return
        self._cancel.clear()
        self._downloading = img
        self.dl_bar.setVisible(True)
        self.say(f"Downloading {img.filename} from downloads.raspberrypi.com…")

        def prog(done, total):
            self._to_ui(self._dl_progress, done, total)
        self._work("download", lambda: self.actions.download(img, prog, self._cancel.is_set),
                   self._download_done)

    def _dl_progress(self, done, total) -> None:
        self.dl_bar.setMaximum(max(1, total // 1_000_000))
        self.dl_bar.setValue(done // 1_000_000)

    def _download_done(self, path, exc) -> None:
        self.dl_bar.setVisible(False)
        if exc is not None:
            self.say(f"Download failed: {exc}")
            return
        self.say(f"Downloaded and checked against its published checksum: {path}")
        img = self._downloading
        if img is not None and img in self._images:
            self.image_box.blockSignals(True)
            self.image_box.setCurrentIndex(self._images.index(img))
            self.image_box.blockSignals(False)
        self._image_path, self._image_entry = Path(path), img
        self._show_image()

    def on_pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Raspberry Pi OS image", "",
                                              "Images (*.img *.img.xz)")
        if path:
            self.use_file(Path(path))

    def use_file(self, path: Path) -> None:
        try:
            p = images.local_image(path)
        except (FileNotFoundError, ValueError) as exc:
            self.say(str(exc))
            return
        match = images.match_local(p, self._images)
        if match is not None:
            self.image_box.blockSignals(True)
            self.image_box.setCurrentIndex(self._images.index(match))
            self.image_box.blockSignals(False)
        self._image_path, self._image_entry = p, match
        self.say(f"Using {p}" + ("" if match else
                                 " (not in the official list: its checksum is not known, "
                                 "and its first-boot format is taken from its name)"))
        self._show_image()

    def refresh_disks(self) -> None:
        try:
            self._disks = self.actions.list_disks()
        except Exception as exc:                          # noqa: BLE001
            self._disks = []
            self.say(f"Could not list disks: {exc}")
        self.disk_list.clear()
        for d in self._disks:
            text = d.summary() + ("" if d.eligible else f"   — can't use: {d.why_not}")
            item = QListWidgetItem(text)
            if not d.eligible:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.disk_list.addItem(item)
        if not any(d.eligible for d in self._disks):
            self.say("No SD card found that can be used. Put the card in a reader and "
                     "press Look again.")
        self._disk_changed()

    def _current_disk(self) -> Optional[dk.Disk]:
        r = self.disk_list.currentRow()
        d = self._disks[r] if 0 <= r < len(self._disks) else None
        return d if d is not None and d.eligible else None

    def _disk_changed(self) -> None:
        d = self._current_disk()
        self.confirm_label.setText(f"Everything on it will be erased. Type:  {d.confirm_code}"
                                   if d else "Select the SD card.")
        self.confirm_edit.clear()
        self._update_card_next()

    def _update_card_next(self) -> None:
        d = self._current_disk()
        self.card_next.setEnabled(bool(d and self._image_path
                                       and self.confirm_edit.text().strip() == d.confirm_code))

    # ── page: settings ──
    def _build_settings(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.s_host = QLineEdit("council-pi-1")
        self.s_user = QLineEdit("council")
        self.s_pass = QLineEdit()
        self.s_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.s_pass2 = QLineEdit()
        self.s_pass2.setEchoMode(QLineEdit.EchoMode.Password)
        self.s_ssid = QLineEdit()
        self.s_ssid.setPlaceholderText("leave empty to use an Ethernet cable")
        self.s_wpass = QLineEdit()
        self.s_wpass.setEchoMode(QLineEdit.EchoMode.Password)
        self.s_country = QLineEdit("US")
        self.s_country.setMaxLength(2)
        self.s_hidden = QCheckBox("The network is hidden")
        self.s_ram = QComboBox()
        for label, gb in RAM_CHOICES:
            self.s_ram.addItem(label, gb)
        self.s_model = QComboBox()
        self.s_ram.currentIndexChanged.connect(lambda _i: self._fill_models())
        self._fill_models()
        for label, wdg in (("Hostname", self.s_host), ("Username", self.s_user),
                           ("Password", self.s_pass), ("Password again", self.s_pass2),
                           ("Wi-Fi network", self.s_ssid), ("Wi-Fi password", self.s_wpass),
                           ("Wi-Fi country", self.s_country), ("", self.s_hidden),
                           ("This Pi", self.s_ram), ("Model", self.s_model)):
            form.addRow(label, wdg)
        note = QLabel("Only a hash of the password and a key derived from the Wi-Fi "
                      "password go on the card; neither is kept by the Council. Many Pis "
                      "only see 2.4 GHz Wi-Fi.")
        note.setWordWrap(True)
        form.addRow(note)
        row = QHBoxLayout()
        self._button(row, "← Back", lambda: self.pages.setCurrentWidget(self.page_card))
        self.write_btn = self._button(row, "Erase the card and write it", self.on_write)
        form.addRow(row)
        return w

    def _fill_models(self) -> None:
        self.s_model.clear()
        for m in pi_models.for_ram(self.s_ram.currentData() or 8):
            self.s_model.addItem(m, m)

    def settings(self) -> fb.FirstBoot:
        return fb.FirstBoot(hostname=self.s_host.text().strip(), username=self.s_user.text().strip(),
                            password=self.s_pass.text(), wifi_ssid=self.s_ssid.text(),
                            wifi_password=self.s_wpass.text(),
                            wifi_country=self.s_country.text().strip().upper(),
                            wifi_hidden=self.s_hidden.isChecked())

    def on_write(self) -> None:
        if self._busy:
            return
        if self.s_pass.text() != self.s_pass2.text():
            self.say("The two passwords differ.")
            return
        cfg = self.settings()
        probs = cfg.problems()
        if probs:
            self.say("\n".join(probs))
            return
        disk, entry, path = self._current_disk(), self._image_entry, self._image_path
        # Checksum, size and first-boot format belong to the FILE: its own list
        # entry, or for a file not in the list no checksum (the read-back still
        # runs) and the format its name gives. Never the combo's entry for
        # another file — that erased a card and then refused it, or gave a
        # Bookworm card cloud-init files it ignores (no user, SSH or Wi-Fi).
        if entry is not None:
            fmt = (entry.init_format if entry.init_format in fb.FORMATS
                   else images.release_format(entry.filename))
            sha, size = entry.extract_sha256, entry.extract_size
        else:
            fmt = images.release_format(path.name) if path is not None else ""
            sha, size = "", 0
        if fmt not in fb.FORMATS:
            self.say("Not written: which first-boot settings this image takes cannot be "
                     "told (it is not in the official list and its name does not say "
                     "Trixie or Bookworm). Get the list and use an official image.")
            return
        try:
            out = self.actions.prepare(
                disk=disk, typed_confirm=self.confirm_edit.text(), image=path,
                init_format=fmt, extract_sha256=sha, extract_size=size, cfg=cfg,
                model=self.s_model.currentData())
        except Exception as exc:                          # noqa: BLE001
            self.say(f"Not written: {exc}")
            return
        self.s_pass.clear()
        self.s_pass2.clear()
        self.s_wpass.clear()
        self._job, self._pending = out["job"], out["pending"]
        self.pages.setCurrentWidget(self.page_write)
        self.say("Windows will ask for permission (UAC) to write the card.")
        try:
            self.actions.start_writer(self._job)
        except Exception as exc:                          # noqa: BLE001
            self.say(f"The card writer did not start (permission declined?): {exc}. "
                     "Nothing was erased, and the settings prepared for it were deleted.")
            self._abandon()
            return
        self._writer_started = time.monotonic()
        self._timer.start()

    def _abandon(self) -> None:
        """The writer never ran: delete its job (the first-boot secrets) and
        its pending record (see setup.abandon_job)."""
        if self._job is not None:
            try:
                self.actions.abandon(self._job, self._pending)
            except Exception as exc:                      # noqa: BLE001
                self.say(f"Could not delete the card job {self._job.parent}: {exc}")
        self._job, self._pending = None, None
        self.cancel_btn.setEnabled(False)
        self._refresh_pending()

    # ── page: writing / finishing ──
    def _build_write(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.w_status = QLabel("Writing the card…")
        self.w_status.setWordWrap(True)
        lay.addWidget(self.w_status)
        self.w_bar = QProgressBar()
        lay.addWidget(self.w_bar)
        row = QHBoxLayout()
        self.cancel_btn = self._button(row, "Cancel", self.on_cancel_write)
        self.find_btn = self._button(row, "I've put the card in the Pi and powered it on — "
                                          "find it", self.on_find)
        self.find_btn.setEnabled(False)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _poll_writer(self) -> None:
        if self._job is None:
            return
        st = self.actions.read_status(self._job.parent)
        if not st:
            if time.monotonic() - self._writer_started >= self.WRITER_START_TIMEOUT_S:
                # It writes 'checking' within a second of starting, before it
                # touches the card; none at all means it never ran (or was
                # handed a path it refused) — do not wait forever.
                self._timer.stop()
                self.say("✗ The card writer never reported back, so nothing was "
                         "erased. Its settings were deleted; try again.")
                self._abandon()
            return
        total = int(st.get("total") or 0)
        if total:
            self.w_bar.setMaximum(1000)
            self.w_bar.setValue(int(1000 * int(st.get("done") or 0) / total))
        self.w_status.setText(st.get("message", ""))
        if st.get("phase") in ("done", "error"):
            self._timer.stop()
            self.cancel_btn.setEnabled(False)
            ok = st.get("phase") == "done"
            self.say(("✓ " if ok else "✗ ") + st.get("message", ""))
            self.find_btn.setEnabled(ok)
            self._refresh_pending()

    def on_cancel_write(self) -> None:
        if self._job is not None:
            (self._job.parent / "cancel").write_text("", encoding="utf-8")
            self.say("Cancelling — the card will need writing again.")

    def on_finish_pending(self) -> None:
        pid = self.finish_box.currentData()
        self._pending = next((p for p in self.actions.pending() if p.id == pid), None)
        if self._pending:
            self.pages.setCurrentWidget(self.page_write)
            self.w_status.setText(f"Finishing {self._pending.hostname}.")
            self.cancel_btn.setEnabled(False)
            self.find_btn.setEnabled(True)

    def on_find(self) -> None:
        if self._busy or self._pending is None:
            return
        item = self._pending
        self.find_btn.setEnabled(False)
        self._cancel.clear()
        self._work("finish", lambda: self.actions.finish(
            item, say=self._say_from_worker, on_line=self._say_from_worker,
            cancelled=self._cancel.is_set), self._finish_done)

    def _finish_done(self, out, exc) -> None:
        if exc is not None:
            self.say(f"✗ {exc}")
            self.find_btn.setEnabled(True)
            return
        self.say(("✓ " if out.ok else "✗ ") + out.message)
        self.find_btn.setEnabled(not out.ok)
        self._refresh_pending()

    def done(self, r) -> None:                            # noqa: D401 — Qt's name
        self._cancel.set()
        self._timer.stop()
        super().done(r)
